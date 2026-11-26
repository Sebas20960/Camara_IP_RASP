# =============================================================================
# cliente_nube.py  -  Envía lo que cuenta conteo4 a uno o varios DESTINOS (protocolo v2,
# ver PROTOCOLO_v2.md): el programa de escritorio de la laptop y/o el servidor en AWS.
#
# Cada destino es independiente: SU PROPIO HILO, su conexión WebSocket, su reconexión,
# su buffer sin internet y su demanda de video. La red nunca frena a YOLO ni a otro destino.
#   - Cada 1 s manda los datos (texto JSON, ~1 KB), igual que v1 (el historial depende de esto).
#     Si se cae la conexión, los guarda en memoria (hasta BUFFER_HORAS) y los manda al volver.
#   - Video: SOLO si el destino dice que alguien mira ("visores" con video > 0):
#       * un mensaje 'det' (texto) por cada análisis de YOLO, con las cajas y el ID de
#         ByteTrack, ligado al número de secuencia (seq) del cuadro analizado;
#       * el JPEG ORIGINAL de cada cámara tal como llegó de la cámara (sin decodificar ni
#         recomprimir: solo se copian bytes al socket), con una cabecera de 14 bytes,
#         hasta min(fps que pide el receptor, tope local de ese destino) cuadros/s por cámara.
#     Siempre se manda el cuadro MÁS RECIENTE: si un destino va lento, los intermedios se
#     descartan solos (cola de 1 por cámara). Sin nadie mirando: 0 cuadros y 0 'det'.
#   - Fuentes sin JPEG (rtsp, archivos): se comprime el cuadro ya analizado (320 px), una sola
#     vez aunque haya dos destinos, y solo a la tasa de análisis.
#   - CONTROL DE FLUJO: si el receptor lo admite ("ack" en 'visores'), confirma los bytes de
#     video que le llegan y la Pi nunca tiene más de una "ventana" sin confirmar. La ventana
#     se ajusta sola según la demora medida. Si la subida no alcanza, se saltan cuadros (sale
#     el más reciente) en vez de amontonarlos: el retraso no crece y los datos y los pings
#     no quedan atrapados detrás del video (antes eso cortaba la conexión con AWS).
# Cada 10 s imprime por destino: cuadros/s, KB/s, det/s, la CPU que gastó su hilo, la demora
# de la red y "RED AL TOPE" si se están saltando cuadros (entonces conviene bajar el FPS).
# Requisito: pip install websockets
# =============================================================================
import json
import struct
import threading
import time
from collections import deque

import cv2

INTERVALO_DATOS = 1.0      # s entre envíos de datos
FPS_VIDEO = 10             # tope por defecto de cuadros/s POR CÁMARA (cada destino puede tener el suyo)
CALIDAD_JPEG = 70          # solo para fuentes sin JPEG original (rtsp/archivos)
BUFFER_HORAS = 6           # datos guardados sin conexión (1 muestra/s ≈ 1 KB -> ~20 MB)
LOTE = 300                 # muestras por mensaje al vaciar el buffer
MAX_DET_PENDIENTES = 64    # 'det' en cola por destino; si va lento se botan los más viejos
INTERVALO_ESTAD = 10.0     # s entre líneas de estadísticas
PAUSA_VIDEO = 0.02         # s de espera MÍNIMA del bucle con video (más corto = más despertares = más CPU).
                           # Si el próximo cuadro toca más tarde, el hilo duerme hasta entonces (máx. 0.1 s).
VENTANA_BYTES = 160_000    # control de flujo: bytes de video "en el aire" (enviados y aún no
                           # confirmados por el receptor) al empezar. Luego se ajusta sola entre
VENTANA_MIN = 80_000       # VENTANA_MIN y VENTANA_MAX: crece mientras la demora no sube (aprovecha
VENTANA_MAX = 1_500_000    # la subida que haya) y se achica si la red empieza a acumular cola.
COLA_OBJETIVO = 0.25       # s de cola tolerados en la red (demora medida - demora mínima)
SIN_ACK = 8.0              # s sin confirmaciones con video pendiente -> se reinicia la cuenta
ESPERA_MAX = 10.0          # s máximos entre reintentos de conexión
PING_INTERVALO = 20        # s entre pings WebSocket (detectan una conexión muerta)
PING_ESPERA = 30           # s que se espera el pong antes de dar la conexión por caída
CABECERA = struct.Struct(">BBId")   # tipo(0x01), cámara, seq uint32, t_cap float64 = 14 bytes
CATS = ["carro", "moto", "bus", "pesado", "especial"]


class VideoCompartido:
    """
    Lo comparten todos los destinos: recibe las cajas de cada análisis (desde el hilo de
    detección, rapidísimo: solo guarda referencias) y entrega a cada destino el último
    cuadro de cada cámara. Enciende pub.det_activo solo si algún destino quiere video.
    """

    def __init__(self, camaras, pub):
        self.camaras = camaras
        self.pub = pub
        self.destinos = []
        n = len(camaras)
        self._analizado = [None] * n       # (seq, t, frame) para fuentes sin JPEG
        self._cache = [None] * n           # (jpeg, seq, t) ya comprimido (se comparte)
        self._lock = threading.Lock()
        pub.suscribir_det(self._llega_det)

    def registrar(self, destino):
        self.destinos.append(destino)

    def actualizar_bandera(self):
        activo = any(d.video_activo for d in self.destinos)
        if not activo:
            self._analizado = [None] * len(self.camaras)
        self.pub.det_activo = activo

    def _llega_det(self, c, seq, t, ancho, alto, cajas, frame):
        # Hilo de detección: nada de trabajo pesado aquí (la normalización la hace cada hilo de envío).
        self._analizado[c] = (seq, t, frame)
        item = (c, seq, t, ancho, alto, cajas)
        for d in self.destinos:
            if d.video_activo:
                d.dets.append(item)           # deque con maxlen: append es seguro entre hilos

    def cuadro(self, c):
        """(jpeg, seq, t) más reciente de la cámara c, o None."""
        r = self.camaras[c].fuente.ultimo_jpeg()
        if r is not None:
            return r                           # JPEG original: tal cual, sin tocarlo
        a = self._analizado[c]
        if a is None:
            return None
        seq, t, frame = a
        with self._lock:                       # se comprime UNA vez aunque lo pidan 2 destinos
            ca = self._cache[c]
            if ca is None or ca[1] != seq:
                ok, jpg = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, CALIDAD_JPEG])
                if not ok:
                    return None
                ca = self._cache[c] = (jpg.tobytes(), seq, t)
        return ca


def _mensaje_det(c, seq, t, ancho, alto, cajas):
    """Cajas en píxeles del cuadro analizado -> 'det' del protocolo (0..10000, conf 0..100)."""
    fx, fy = 10000.0 / ancho, 10000.0 / alto
    b = [[int(x1 * fx), int(y1 * fy), int(x2 * fx), int(y2 * fy), ci, tid, num or 0, int(cf * 100)]
         for x1, y1, x2, y2, ci, num, cf, tid in cajas]
    # t sin redondear: debe ser IDÉNTICO al t de la cabecera del cuadro con el mismo seq
    return json.dumps({"tipo": "det", "c": c, "s": seq, "t": t, "b": b},
                      separators=(",", ":"))


class ClienteNube:
    """Un destino (laptop o AWS): hilo + conexión + buffer sin internet + reconexión."""

    def __init__(self, nombre, url, token, camaras, pub, video, fps_max=FPS_VIDEO):
        self.nombre = nombre
        self.url = f"{url}{'&' if '?' in url else '?'}token={token}"
        self.url_log = url
        self.camaras = camaras
        self.pub = pub                          # pub.semaforo: estado del semáforo RL (rl_conteos)
        self.video = video
        self.fps_max = float(fps_max)           # tope local de cuadros/s por cámara (0 = sin video)
        self.fps = self.fps_max                 # min(lo que pide el receptor, el tope)
        self.video_activo = False               # lo lee el hilo de detección (bool atómico)
        self.ventana = VENTANA_BYTES
        self._rtt_min = None                    # (demora mínima vista, cuándo) para medir la cola
        self._ack = False                       # el receptor confirma lo recibido (control de flujo)
        self.dets = deque(maxlen=MAX_DET_PENDIENTES)
        self._datos = None
        self._prev = {}                         # nombre -> vistos por categoría ya enviados
        self._buffer = deque(maxlen=int(BUFFER_HORAS * 3600 / INTERVALO_DATOS))
        self._visores = 0
        self._t_muestra = 0.0
        self._detener = threading.Event()
        pub.suscribir_datos(self._llegan_datos)
        video.registrar(self)
        self._hilo = threading.Thread(target=self._correr, daemon=True, name=f"envio-{nombre}")
        self._hilo.start()

    def _log(self, txt):
        print(f"[{self.nombre}] {txt}", flush=True)

    # -- datos ----------------------------------------------------------------
    def _llegan_datos(self, d):
        self._datos = d                         # lo llama el hilo de detección (asignación atómica)

    def _muestra(self):
        """Arma la muestra y calcula los vehículos NUEVOS desde la anterior (el flujo).
        Como se calcula con el acumulado 'vistos', no se pierde nada aunque falten muestras."""
        d = self._datos
        if d is None:
            return None
        cams = []
        for c in d["camaras"]:
            act = c.get("vistos_por_categoria") or {}
            prev = self._prev.get(c["nombre"], {})
            # si el acumulado bajó es que se reiniciaron los IDs (tecla r): cuenta desde 0
            nuevos = {cat: (v - prev.get(cat, 0)) if v >= prev.get(cat, 0) else v
                      for cat, v in act.items()}
            self._prev[c["nombre"]] = dict(act)
            cams.append({"nombre": c["nombre"], "conectada": c["conectada"], "total": c["total"],
                         "por_categoria": c["por_categoria"], "nuevos": nuevos,
                         "vistos": c["vistos"], "fps": c["fps"]})
        return {"tipo": "datos", "t": time.time(), "fps": d.get("fps_deteccion"),
                "temp": d.get("temp_cpu"), "total": d["total"], "camaras": cams}

    def _toca_muestra(self):
        ahora = time.time()
        if ahora - self._t_muestra >= INTERVALO_DATOS:
            self._t_muestra = ahora
            return self._muestra()
        return None

    # -- demanda de video -----------------------------------------------------
    def _poner_video(self, visores, fps_pedido):
        fps = self.fps_max
        if isinstance(fps_pedido, (int, float)) and fps_pedido > 0:
            fps = min(fps, float(fps_pedido))
        activo = visores > 0 and fps > 0
        if visores != self._visores or fps != self.fps:
            self._log(f"{'Alguien está viendo' if visores else 'Nadie está viendo'} el video "
                      f"({visores})" + (f" -> {fps:g} cuadros/s por cámara" if activo else ""))
        self._visores, self.fps = visores, fps
        if activo != self.video_activo:
            self.video_activo = activo
            if not activo:
                self.dets.clear()
            self.video.actualizar_bandera()

    # -- conexión -------------------------------------------------------------
    def _correr(self):
        try:
            from websockets.sync.client import connect
        except ImportError:
            self._log("Falta la librería: pip install websockets   (sigo sin este destino)")
            return
        espera, avisado = 1.0, False
        while not self._detener.is_set():
            t_conectado = None
            try:
                with connect(self.url, open_timeout=10, close_timeout=2, compression=None,
                             max_size=2 ** 20, ping_interval=PING_INTERVALO,
                             ping_timeout=PING_ESPERA) as ws:
                    t_conectado = time.monotonic()
                    self._log(f"Conectado a {self.url_log}")
                    avisado = False
                    ws.send(json.dumps({"tipo": "hola", "version": 2,
                                        "camaras": [c.nombre for c in self.camaras]}))
                    self._vaciar_buffer(ws)
                    self._sesion(ws)
            except Exception as e:
                if t_conectado is not None:
                    self._log(f"Se cortó la conexión con {self.url_log} tras "
                              f"{time.monotonic() - t_conectado:.0f} s ({type(e).__name__}: {e}). "
                              f"Reconecto...")
                    avisado = True
                elif not avisado:
                    self._log(f"Sin conexión con {self.url_log} ({type(e).__name__}: {e}). "
                              f"Guardo los datos y reintento...")
                    avisado = True
            finally:
                self._poner_video(0, None)
            # una conexión que duró es un corte puntual: reintento rápido; si no, espera creciente
            if t_conectado is not None and time.monotonic() - t_conectado > 30:
                espera = 1.0
            # sin conexión: se siguen tomando muestras para no perder historial
            fin = time.time() + espera
            while time.time() < fin and not self._detener.is_set():
                m = self._toca_muestra()
                if m is not None:
                    self._buffer.append(m)
                self._detener.wait(0.2)
            espera = min(espera * 2, ESPERA_MAX)

    def _vaciar_buffer(self, ws):
        if not self._buffer:
            return
        n = len(self._buffer)
        while self._buffer and not self._detener.is_set():
            lote = [self._buffer[i] for i in range(min(LOTE, len(self._buffer)))]
            ws.send(json.dumps({"tipo": "lote", "muestras": lote}, separators=(",", ":")))
            for _ in lote:                       # se borran solo después de mandarlos
                self._buffer.popleft()
        self._log(f"Envié {n} muestras guardadas mientras no había conexión")

    def _sesion(self, ws):
        n = len(self.camaras)
        enviado = [0] * n                        # último seq mandado por cámara
        t_sig = [0.0] * n                        # cuándo puede salir el próximo cuadro (monotonic)
        t_ult = [0.0] * n                        # cuándo salió el último (para turnarse si falta red)
        # Control de flujo (si el receptor manda 'ack'): bytes de video enviados y confirmados.
        # Nunca hay más de VENTANA_BYTES "en el aire": si la subida no da, se saltan cuadros
        # (siempre sale el más reciente) en vez de amontonarlos -> retraso acotado y los
        # datos/pings nunca quedan atrapados detrás de megas de video (eso cortaba la conexión).
        tx = rx = 0
        t_rx = time.monotonic()                  # última vez que avanzó el ack
        envios = deque()                         # (bytes acumulados, t_envío) para medir la demora
        self._ack = False
        est = self._estad_nueva()
        pausa = 0.1
        n_sem, t_sem = None, 0.0                 # último estado del semáforo enviado
        while not self._detener.is_set():
            # recibir TODO lo pendiente (visores, acks); el primer recv hace de pausa del bucle
            espera = pausa
            while True:
                try:
                    msg = ws.recv(timeout=espera)
                except TimeoutError:
                    break
                espera = 0
                if not isinstance(msg, str):
                    continue
                try:
                    d = json.loads(msg)
                except json.JSONDecodeError:
                    continue
                if not isinstance(d, dict):
                    continue
                tipo = d.get("tipo")
                if tipo == "ack":
                    try:
                        b = int(d.get("b") or 0)
                    except (TypeError, ValueError):
                        continue
                    if b > rx:
                        rx, t_rx = min(b, tx), time.monotonic()
                        while envios and envios[0][0] <= rx:
                            muestra = t_rx - envios.popleft()[1]
                            est["rtt"].append(muestra)
                            self._ajustar_ventana(muestra, t_rx)
                elif tipo == "visores":
                    try:
                        self._poner_video(int(d.get("video") or 0), d.get("fps"))
                    except (TypeError, ValueError):
                        pass
                    if d.get("ack") and not self._ack:
                        self._ack = True
                        tx = rx = 0                  # el receptor cuenta desde esta conexión
                        envios.clear()
                        self.ventana, self._rtt_min = VENTANA_BYTES, None

            m = self._toca_muestra()
            if m is not None:
                ws.send(json.dumps(m, separators=(",", ":")))

            # semáforo RL (si rl_zonas.json tiene "interfaz": true): cada cambio (~1/s) y,
            # aunque no cambie, cada 3 s (así el receptor sabe que sigue vivo)
            sem = getattr(self.pub, "semaforo", None)
            if sem is not None:
                ahora_s = time.monotonic()
                if sem.get("n") != n_sem or ahora_s - t_sem >= 3.0:
                    n_sem, t_sem = sem.get("n"), ahora_s
                    # 'edad' la calcula la Pi: no depende de que los relojes coincidan
                    edad = round(max(0.0, time.time() - float(sem.get("t") or 0)), 1)
                    ws.send(json.dumps(dict(sem, tipo="semaforo", edad=edad), separators=(",", ":")))

            if self.video_activo:
                while self.dets:
                    try:
                        item = self.dets.popleft()
                    except IndexError:
                        break
                    ws.send(_mensaje_det(*item))
                    est["det"] += 1
                periodo = 1.0 / self.fps
                ahora = time.monotonic()
                if self._ack and tx > rx and ahora - t_rx > SIN_ACK:
                    rx, t_rx = tx, ahora              # el receptor no confirma: no congelar el video
                    envios.clear()
                # turno: primero la cámara que lleva más tiempo sin mandar (justo si falta red)
                for c in sorted(range(n), key=t_ult.__getitem__):
                    if ahora < t_sig[c]:
                        continue
                    r = self.video.cuadro(c)
                    if r is None or r[1] == enviado[c]:
                        continue                 # aún no llega un cuadro nuevo de esa cámara
                    jpg, seq, t = r
                    tam = len(jpg) + CABECERA.size
                    if self._ack and tx > rx and tx - rx + tam > self.ventana:
                        est["llenos"] += 1       # red al tope: espera al próximo ack
                        break
                    ws.send(CABECERA.pack(1, c, seq & 0xFFFFFFFF, t) + jpg)
                    tx += tam
                    envios.append((tx, time.monotonic()))
                    enviado[c] = seq
                    t_ult[c] = ahora
                    # ritmo fijo (sin ráfagas después de una pausa larga)
                    t_sig[c] = max(t_sig[c] + periodo, ahora - 0.5 * periodo)
                    est["n"] += 1
                    est["bytes"] += tam
                # dormir hasta el próximo cuadro que toca (si ya tocaba y no llegó, reintento corto);
                # con la red al tope, el próximo ack despierta el bucle
                futuros = [t for t in t_sig if t > ahora]
                pausa = min(max(min(futuros) - ahora, PAUSA_VIDEO), 0.1) if futuros else PAUSA_VIDEO
            else:
                pausa = 0.1

            ahora = time.monotonic()
            if ahora - est["t"] >= INTERVALO_ESTAD:
                self._log_estad(est, ahora, n)
                est = self._estad_nueva()

    def _ajustar_ventana(self, rtt, ahora):
        """Control por demora (como LEDBAT): la demora mínima es la de la red vacía; lo que
        pasa de eso es cola. Poca cola -> la ventana crece 3 %; mucha -> baja 10 %."""
        if self._rtt_min is None or rtt < self._rtt_min[0] or ahora - self._rtt_min[1] > 60:
            self._rtt_min = (rtt, ahora)        # se renueva cada minuto (la ruta puede cambiar)
        cola = rtt - self._rtt_min[0]
        if cola < COLA_OBJETIVO:
            self.ventana = min(VENTANA_MAX, self.ventana * 1.03)
        elif cola > 2 * COLA_OBJETIVO:
            self.ventana = max(VENTANA_MIN, self.ventana * 0.9)

    @staticmethod
    def _estad_nueva():
        return {"t": time.monotonic(), "cpu": time.thread_time(), "n": 0, "bytes": 0, "det": 0,
                "llenos": 0, "rtt": []}

    def _log_estad(self, est, ahora, n):
        dt, cpu = ahora - est["t"], time.thread_time() - est["cpu"]
        if not (est["n"] or est["det"]):
            return
        kb = est["bytes"] / 1024
        extra = ""
        if self._ack:
            r = sorted(est["rtt"])
            if r:
                extra += f" | demora {1000 * r[len(r) // 2]:.0f} ms, ventana {self.ventana / 1024:.0f} KB"
            if est["llenos"]:
                extra += " | RED AL TOPE (se saltan cuadros: baja FPS_VIDEO de este destino)"
        self._log(f"video: {est['n'] / dt:.1f} cuadros/s ({est['n'] / dt / n:.1f} por cámara), "
                  f"{kb / dt:.0f} KB/s ({8 * kb / dt / 1024:.1f} Mbps), "
                  f"{kb / max(est['n'], 1):.1f} KB/cuadro | det {est['det'] / dt:.1f}/s | "
                  f"CPU del hilo {100 * cpu / dt:.1f}%{extra}")

    def detener(self):
        self._detener.set()
        self._hilo.join(timeout=4)


class EnvioDestinos:
    """Agrupa los destinos (lo que conteo4.main espera de la extensión: un .detener())."""

    def __init__(self, destinos, camaras, pub):
        """destinos: lista de (nombre, url_ws, token, fps_max)."""
        self.video = VideoCompartido(camaras, pub)
        self.clientes = [ClienteNube(nombre, url, token, camaras, pub, self.video, fps)
                         for nombre, url, token, fps in destinos]

    def detener(self):
        for c in self.clientes:
            c._detener.set()
        for c in self.clientes:
            c.detener()
