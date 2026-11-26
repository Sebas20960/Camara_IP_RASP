# =============================================================================
# rl_conteos.py  -  Puente YOLO -> semáforo con RL (principal_pi.py), contrato v1
#                   (ver docs/CONTRATO_RL.md).
#
# Corre DENTRO del proceso de conteo (conteo4): usa las cajas de cada análisis de YOLO que
# ya existen (cero inferencias extra). Abre un servidor TCP en 127.0.0.1:5050 y el
# semáforo (otro proceso en la misma Pi) se conecta a él. Son dos procesos a propósito:
# YOLO usa casi toda la CPU y el semáforo necesita un reloj de 1 s exacto (ámbar).
#
# Qué mide (sección 7 del contrato): por calle, cuántos vehículos hay AHORA (en el último
# análisis de su cámara) dentro de su ZONA: un polígono en la imagen = los 60 m antes de
# la línea de parada, carriles de entrada. Un vehículo está dentro si el punto medio del
# borde inferior de su caja (donde toca el piso) cae en el polígono. Sin polígono = todo
# el cuadro. Clases: las de "categorias" (por defecto carro, bus, pesado y especial; moto
# no, pendiente de decidir).
#
# Reglas de robustez:
#   - Si la cámara de una calle no tiene un análisis reciente (caída, congelada), NO se
#     manda nada: mandar 0 haría creer al agente que la calle está vacía. El semáforo pasa
#     solo a su ciclo fijo de respaldo a los 15 s y vuelve cuando regresan los conteos.
#   - El "listo" se manda con conteos REALES. Si aún no hay (YOLO cargando), se espera
#     hasta 8 s; si siguen sin llegar se cierra la conexión y el semáforo reintenta en 5 s.
#   - Al cerrarse el conteo NO se manda "fin" (principal_pi.py se detendría del todo y
#     al reiniciarlo empezaría con 60 s en rojo): se corta la conexión y el semáforo
#     sigue en respaldo hasta que el conteo vuelve. Configurable ("fin_al_cerrar").
#   - "retraso_min_s": edad mínima del conteo al enviarlo. El agente se entrenó con
#     conteos de 2 s de antigüedad (config.json: sumo.retraso_conteo_s); 0 = lo más fresco.
#
# Interfaz ("interfaz": true en rl_zonas.json): el semáforo manda cada segundo sus luces y un
# resumen (control, fase, decisión del agente) por esta misma conexión (mensaje "luces", que el
# contrato ya permite). Aquí se guarda en pub.semaforo y de ahí lo muestran la laptop, el
# dashboard web y la ventana de conteo4 (--pantalla). Solo se muestra: no cambia el control.
#
# Configuración: rl_zonas.json (junto a este archivo). Zonas: python definir_zonas.py
# Solo Python estándar + lo que ya usa conteo4 (nada que instalar).
# =============================================================================
import json
import os
import socket
import threading
import time
from collections import deque

AQUI = os.path.dirname(os.path.abspath(__file__))
RUTA_CONFIG = os.path.join(AQUI, "rl_zonas.json")
VERSION = 1
CALLES = ("N", "S", "E", "O")
MAX_LINEA = 64 * 1024

POR_DEFECTO = {
    "activo": True,
    "host": "127.0.0.1",
    "puerto": 5050,
    "calles": {"N": "Cam1", "S": "Cam2", "E": "Cam3", "O": "Cam4"},
    "categorias": ["carro", "bus", "pesado", "especial"],
    "zonas": {},
    "intervalo_s": 1.0,
    "retraso_min_s": 2.0,
    "max_edad_s": 4.0,
    "fin_al_cerrar": False,
    "interfaz": True,
}
CATS = ["carro", "moto", "bus", "pesado", "especial"]     # orden de conteo4.ORDEN_CAT


def cargar_config(ruta=RUTA_CONFIG):
    cfg = json.loads(json.dumps(POR_DEFECTO))
    if os.path.isfile(ruta):
        with open(ruta, encoding="utf-8") as f:
            datos = json.load(f)
        cfg.update({k: v for k, v in datos.items() if not k.startswith("_")})
    calles = cfg["calles"]
    if not isinstance(calles, dict) or sorted(calles) != sorted(CALLES):
        raise ValueError(f"rl_zonas.json: 'calles' debe tener exactamente {list(CALLES)} -> nombre de cámara")
    if len(set(calles.values())) != len(calles):
        raise ValueError("rl_zonas.json: cada calle debe usar una cámara distinta "
                         "(un vehículo cuenta en una sola calle)")
    malas = [c for c in cfg["categorias"] if c not in CATS]
    if malas:
        raise ValueError(f"rl_zonas.json: categorías desconocidas {malas} (válidas: {CATS})")
    for nombre, poli in (cfg.get("zonas") or {}).items():
        if poli is None:
            continue
        if (not isinstance(poli, list) or len(poli) < 3
                or not all(isinstance(p, list) and len(p) == 2 and all(0 <= v <= 1 for v in p) for p in poli)):
            raise ValueError(f"rl_zonas.json: la zona de {nombre} debe ser una lista de >= 3 puntos [x, y] "
                             f"entre 0 y 1 (fracción del ancho/alto de la imagen)")
    for k in ("intervalo_s", "max_edad_s"):
        if not 0 < float(cfg[k]) <= 5:
            raise ValueError(f"rl_zonas.json: '{k}' debe estar entre 0 y 5 s")
    if not 0 <= float(cfg["retraso_min_s"]) <= 6:
        raise ValueError("rl_zonas.json: 'retraso_min_s' debe estar entre 0 y 6 s")
    return cfg


def dentro(px, py, poli):
    """Punto en polígono (rayos, regla par-impar)."""
    res = False
    j = len(poli) - 1
    for i in range(len(poli)):
        xi, yi = poli[i]
        xj, yj = poli[j]
        if (yi > py) != (yj > py) and px < (xj - xi) * (py - yi) / ((yj - yi) or 1e-12) + xi:
            res = not res
        j = i
    return res


def _linea(msg):
    return (json.dumps(msg, ensure_ascii=False, allow_nan=False, separators=(",", ":")) + "\n").encode("utf-8")


class EnlaceRL:
    """Servidor del contrato v1. Se engancha a conteo4 con pub.suscribir_analisis()."""

    def __init__(self, camaras, pub, cfg=None, log=print):
        self.cfg = cfg or cargar_config()
        self.log = lambda txt: log(f"[RL] {txt}")
        nombres = [c.nombre for c in camaras]
        self.idx_calle = {}                       # índice de cámara -> calle
        faltan = []
        for calle, cam in self.cfg["calles"].items():
            if cam in nombres:
                self.idx_calle[nombres.index(cam)] = calle
            else:
                faltan.append(f"{calle}={cam}")
        if faltan:
            self.log(f"AVISO: estas cámaras de rl_zonas.json no existen {faltan} (cámaras: {nombres}). "
                     f"Sin ellas no se mandan conteos (el semáforo queda en respaldo).")
        self.zonas = {}
        for k, calle in self.idx_calle.items():
            poli = (self.cfg.get("zonas") or {}).get(nombres[k])
            self.zonas[k] = [tuple(p) for p in poli] if poli else None
        sin_zona = [nombres[k] for k, z in self.zonas.items() if z is None]
        if sin_zona:
            self.log(f"AVISO: sin zona para {sin_zona}: se cuenta TODO el cuadro. Define los 60 m antes de la "
                     f"línea de parada con definir_zonas.py.")
        self.cats_ok = {CATS.index(c) for c in self.cfg["categorias"]}
        self._muestras = {k: deque(maxlen=200) for k in self.idx_calle}   # (t_cap, conteo)
        self._seq = 0
        self.pub = pub
        self.interfaz = bool(self.cfg.get("interfaz", True))
        self._n_sem = 0
        self._sem_base = {"conectado": False, "enviando": False, "motivo": None}
        self._parar = threading.Event()
        self._conn = None
        self.estado = {"conectado": False, "enviados": 0, "ultimo": None, "sesiones": 0}
        self._srv = None
        try:
            self._srv = self._escuchar()
        except OSError as e:
            self.log(f"ERROR: no pude abrir {self.cfg['host']}:{self.cfg['puerto']} ({e}). "
                     f"¿Otro conteo ya corre? Sigo SIN enlace con el semáforo.")
            return
        pub.suscribir_analisis(self._analisis)
        self._publicar_sem(conectado=False, enviando=False, motivo="esperando al semáforo", luces=None, estado=None)
        self._hilo = threading.Thread(target=self._servir, name="enlace-rl", daemon=True)
        self._hilo.start()
        mapa = ", ".join(f"{c}={self.cfg['calles'][c]}" for c in CALLES)
        self.log(f"Esperando al semáforo en {self.cfg['host']}:{self.cfg['puerto']} | {mapa} | "
                 f"clases {self.cfg['categorias']} | retraso {self.cfg['retraso_min_s']:g} s | "
                 f"interfaz {'sí' if self.interfaz else 'no'}")

    # ------------------------------------------------------------ interfaz
    def _publicar_sem(self, **campos):
        """Deja el estado del semáforo en pub.semaforo (lo leen cliente_nube y la ventana).
        Se reemplaza el dict completo (asignación atómica entre hilos)."""
        if not self.interfaz:
            return
        self._sem_base.update(campos)
        self._n_sem += 1
        self.pub.semaforo = dict(self._sem_base, n=self._n_sem, t=round(time.time(), 3),
                                 calles=dict(self.cfg["calles"]))

    def _llegan_luces(self, msg):
        luces = msg.get("luces")
        if not (isinstance(luces, dict) and sorted(luces) == sorted(CALLES)
                and all(v in ("V", "A", "R") for v in luces.values())):
            return                                    # solo se muestra: no se corta por esto
        estado = msg.get("estado")
        if not isinstance(estado, dict) or len(json.dumps(estado)) > 16384:
            estado = None
        self._publicar_sem(luces=dict(luces), estado=estado)

    # ------------------------------------------------------------ hilo de detección
    def _analisis(self, k, t_cap, ancho, alto, cajas):
        """Tras CADA análisis de YOLO (hilo de detección): contar es barato (pocas cajas)."""
        if k not in self._muestras:
            return
        poli = self.zonas.get(k)
        n = 0
        for x1, y1, x2, y2, ci, _num, _cf, _tid in cajas:
            if ci not in self.cats_ok:
                continue
            if poli is None or dentro((x1 + x2) / (2.0 * ancho), y2 / alto, poli):
                n += 1
        self._muestras[k].append((t_cap, n))

    # ------------------------------------------------------------ conteos a mandar
    def conteos(self, ahora=None):
        """(t, {calle: n}) para enviar ahora, o (None, motivo) si alguna calle no tiene dato fresco."""
        ahora = time.time() if ahora is None else ahora
        limite = ahora - float(self.cfg["retraso_min_s"])
        max_edad = float(self.cfg["max_edad_s"])
        res, ts = {}, []
        for calle in CALLES:
            k = next((i for i, c in self.idx_calle.items() if c == calle), None)
            if k is None:
                return None, f"la calle {calle} no tiene cámara"
            elegido = None
            for t, n in reversed(self._muestras[k]):
                if t <= limite:
                    elegido = (t, n)
                    break
            if elegido is None or limite - elegido[0] > max_edad:
                return None, f"sin análisis reciente de {self.cfg['calles'][calle]} (calle {calle})"
            res[calle] = elegido[1]
            ts.append(elegido[0])
        return min(ts), res

    # ------------------------------------------------------------ red
    def _escuchar(self):
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        if os.name != "nt":
            srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind((self.cfg["host"], int(self.cfg["puerto"])))
        srv.listen(2)
        srv.settimeout(0.5)
        return srv

    def _servir(self):
        while not self._parar.is_set():
            try:
                sock, dir_ = self._srv.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                self._sesion(sock, dir_)
            except Exception as e:                    # nunca tumbar el conteo por el enlace
                self.log(f"Sesión terminada por error: {type(e).__name__}: {e}")
            finally:
                self.estado["conectado"] = False
                self._conn = None
                self._publicar_sem(conectado=False, enviando=False, motivo="semáforo desconectado",
                                   luces=None, estado=None)
                try:
                    sock.close()
                except OSError:
                    pass

    def _sesion(self, sock, dir_):
        sock.settimeout(0.25)
        try:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass
        self._conn = sock
        buf = bytearray()

        def recibir(espera):
            """Siguiente mensaje (dict) o None si no llegó en `espera` s. Lanza EOFError si cerró."""
            limite = time.monotonic() + espera
            while True:
                i = buf.find(b"\n")
                if i >= 0:
                    linea = bytes(buf[:i])
                    del buf[:i + 1]
                    if not linea.strip():
                        continue
                    try:
                        msg = json.loads(linea.decode("utf-8"))
                    except (UnicodeDecodeError, ValueError):
                        raise ValueError("mensaje que no es JSON válido") from None
                    if not isinstance(msg, dict):
                        raise ValueError("el mensaje debe ser un objeto JSON")
                    return msg
                if len(buf) > MAX_LINEA:
                    raise ValueError(f"mensaje de más de {MAX_LINEA} bytes")
                if time.monotonic() >= limite or self._parar.is_set():
                    return None
                try:
                    trozo = sock.recv(65536)
                except socket.timeout:
                    continue
                if not trozo:
                    raise EOFError
                buf.extend(trozo)

        def error_y_cerrar(texto):
            self.log(f"Mensaje no válido del semáforo: {texto}")
            try:
                sock.sendall(_linea({"tipo": "error", "mensaje": texto}))
            except OSError:
                pass

        try:
            hola = recibir(10.0)
            if hola is None:
                self.log(f"{dir_[0]} se conectó pero no saludó en 10 s: cierro")
                return
            if hola.get("tipo") != "hola":
                return error_y_cerrar(f"se esperaba 'hola' y llegó {hola.get('tipo')!r}")
            if hola.get("version") != VERSION:
                return error_y_cerrar(f"versión {hola.get('version')!r}; este programa usa {VERSION}")
            calles = hola.get("calles")
            if not isinstance(calles, list) or sorted(map(str, calles)) != sorted(CALLES):
                return error_y_cerrar(f"calles {calles!r}; este programa cuenta {list(CALLES)}")
            # "listo" con conteos reales: hasta 8 s esperando el primero (YOLO arrancando)
            limite = time.monotonic() + 8.0
            t, c = self.conteos()
            while t is None and time.monotonic() < limite and not self._parar.is_set():
                if recibir(0.5) is not None:
                    pass                                  # (nada que hacer con otros mensajes aquí)
                t, c = self.conteos()
            if t is None:
                self.log(f"Semáforo conectado pero aún no hay conteos ({c}). Cierro; reintentará en 5 s.")
                return
            sock.sendall(_linea({"tipo": "listo", "version": VERSION, "calles": list(CALLES), "huella": None,
                                 "simulado": False, "t": round(t, 3), "conteos": c,
                                 "info": f"YOLO {len(self.idx_calle)} cámaras (conteo4)"}))
            self.estado.update(conectado=True, sesiones=self.estado["sesiones"] + 1, ultimo=(t, c))
            self.log(f"Semáforo conectado desde {dir_[0]} (modelo {hola.get('modelo')}). Primer conteo {c}")
            self._publicar_sem(conectado=True, enviando=True, motivo=None, modelo=hola.get("modelo"))
            avisado = None
            t_sig = time.monotonic() + float(self.cfg["intervalo_s"])
            while not self._parar.is_set():
                msg = recibir(max(0.0, t_sig - time.monotonic()))
                if msg is not None:
                    tipo = msg.get("tipo")
                    if tipo == "fin":
                        self.log(f"El semáforo se detuvo ({msg.get('motivo', '')}). Espero que vuelva.")
                        return
                    if tipo == "error":
                        self.log(f"El semáforo informó un error: {msg.get('mensaje')}")
                        return
                    if tipo == "luces":
                        if self.interfaz:
                            self._llegan_luces(msg)       # solo para mostrarlo
                        continue
                    if tipo == "hola":
                        continue
                    return error_y_cerrar(f"tipo de mensaje inesperado: {tipo!r}")
                if time.monotonic() < t_sig:
                    continue
                t_sig = max(t_sig + float(self.cfg["intervalo_s"]), time.monotonic() - 0.5)
                t, c = self.conteos()
                if t is None:
                    if avisado != c:
                        self.log(f"No mando conteos: {c}. El semáforo pasará a respaldo a los 15 s.")
                        avisado = c
                        self._publicar_sem(enviando=False, motivo=c)
                    continue
                if avisado is not None:
                    self.log("Vuelven los conteos.")
                    avisado = None
                    self._publicar_sem(enviando=True, motivo=None)
                self._seq += 1
                sock.sendall(_linea({"tipo": "conteos", "seq": self._seq, "t": round(t, 3), "conteos": c}))
                self.estado["enviados"] += 1
                self.estado["ultimo"] = (t, c)
            if self.cfg.get("fin_al_cerrar"):
                sock.sendall(_linea({"tipo": "fin", "t": round(time.time(), 3), "motivo": "apagado"}))
        except ValueError as e:
            error_y_cerrar(str(e))
        except (EOFError, OSError):
            self.log("El semáforo cerró la conexión. Espero que vuelva.")

    def detener(self):
        self._parar.set()
        if self._srv is not None:
            try:
                self._srv.close()
            except OSError:
                pass
        h = getattr(self, "_hilo", None)
        if h is not None:
            h.join(timeout=3)


# =============================================================================
# Panel del semáforo para la ventana de conteo4 (--pantalla). Solo OpenCV + numpy.
# =============================================================================
_BGR = {"bg": (16, 17, 17), "s1": (25, 26, 26), "s2": (33, 35, 35), "borde": (43, 46, 46),
        "t1": (236, 239, 240), "t2": (183, 194, 195), "t3": (134, 142, 143),
        "V": (94, 183, 63), "A": (48, 160, 224), "R": (106, 106, 240), "azul": (229, 135, 57),
        "apagado": (44, 46, 46), "calle": (44, 45, 44), "linea": (82, 84, 84)}
CONTROL_UI = {"agente": ("AGENTE RL", "V"), "recuperando": ("RECUPERANDO", "azul"),
              "respaldo": ("CICLO FIJO", "A")}
NOMBRE_CALLE = {"N": "Norte", "S": "Sur", "E": "Este", "O": "Oeste"}


def _sin_tildes(txt):
    import unicodedata
    return unicodedata.normalize("NFKD", str(txt)).encode("ascii", "ignore").decode("ascii")


def resumen_control(sem, ahora=None):
    """(texto, clave de color, detalle) del estado general del semáforo."""
    ahora = time.time() if ahora is None else ahora
    if not sem:
        return "SIN DATOS", "t3", "el conteo no informa del semáforo"
    if not sem.get("conectado"):
        return "DESCONECTADO", "R", sem.get("motivo") or "el semáforo no está conectado"
    if ahora - float(sem.get("t") or 0) > 5:
        return "SIN DATOS", "R", "hace más de 5 s que no llega el estado del semáforo"
    est = sem.get("estado")
    if not est:
        return "CONECTADO", "t3", "pon enviar_luces = true en semaforo/pi.json para ver el detalle"
    txt, col = CONTROL_UI.get(est.get("control"), (str(est.get("control")).upper(), "t3"))
    det = {"agente": "el modelo DDQN decide las fases",
           "recuperando": "volvieron los conteos; el agente retoma en unos segundos",
           "respaldo": "ciclo fijo de respaldo: no llegan conteos de YOLO"}.get(est.get("control"), "")
    return txt, col, det


def dibujar_panel(sem, ancho=380):
    """Imagen BGR con el cruce, las luces y la decisión del agente."""
    import cv2
    import numpy as np
    F, AA = cv2.FONT_HERSHEY_SIMPLEX, cv2.LINE_AA
    C = _BGR
    W, H = ancho, 612
    img = np.full((H, W, 3), C["bg"], np.uint8)

    def col_de(c):
        return C[c] if isinstance(c, str) else c

    def texto(t, x, y, esc=0.42, col="t1", der=False, centro=False):
        t = _sin_tildes(t)
        if der or centro:
            w = cv2.getTextSize(t, F, esc, 1)[0][0]
            x = x - w if der else x - w // 2
        cv2.putText(img, t, (int(x), int(y)), F, esc, col_de(col), 1, AA)

    def caja(x1, y1, x2, y2, col):
        cv2.rectangle(img, (int(x1), int(y1)), (int(x2), int(y2)), col_de(col), -1)

    ahora = time.time()
    estado_txt, estado_col, detalle = resumen_control(sem, ahora)
    est = (sem or {}).get("estado") or {}
    cfg = est.get("config") or {}
    fases = {f["id"]: f for f in cfg.get("fases") or []}
    fresco = bool(sem) and sem.get("conectado") and ahora - float(sem.get("t") or 0) <= 5
    luces = (sem or {}).get("luces") if fresco else None
    if not fresco:
        est = {}
    camaras = (sem or {}).get("calles") or {}

    # --- cabecera
    texto("Semaforo inteligente", 14, 26, 0.55)
    bw = cv2.getTextSize(estado_txt, F, 0.42, 1)[0][0] + 28
    caja(W - 14 - bw, 11, W - 14, 35, "s2")
    cv2.circle(img, (W - 14 - bw + 12, 23), 4, C[estado_col], -1, AA)
    texto(estado_txt, W - 14 - bw + 21, 28, 0.42)
    texto(detalle[:60], 14, 50, 0.36, "t3")

    # --- cruce
    cx, cy, a = W // 2, 205, 34
    caja(0, cy - a, W, cy + a, "calle")
    caja(cx - a, 64, cx + a, 348, "calle")
    for x0, x1 in ((0, cx - a), (cx + a, W)):
        for x in range(x0 + 4, x1 - 8, 18):
            cv2.line(img, (x, cy), (x + 9, cy), C["linea"], 1, AA)
    for y0, y1 in ((64, cy - a), (cy + a, 348)):
        for y in range(y0 + 4, y1 - 8, 18):
            cv2.line(img, (cx, y), (cx, y + 9), C["linea"], 1, AA)
    # líneas de parada (carril de entrada de cada calle, circulación por la derecha)
    cv2.line(img, (cx - a, cy - a - 3), (cx, cy - a - 3), C["t2"], 2)
    cv2.line(img, (cx, cy + a + 3), (cx + a, cy + a + 3), C["t2"], 2)
    cv2.line(img, (cx + a + 3, cy - a), (cx + a + 3, cy), C["t2"], 2)
    cv2.line(img, (cx - a - 3, cy), (cx - a - 3, cy + a), C["t2"], 2)

    orden = cfg.get("calles") or list(CALLES)       # orden de 'conteos' y 'rojo'
    conteos = dict(zip(orden, est.get("conteos") or []))
    rojos = dict(zip(orden, est.get("rojo") or []))
    pos = {"N": ((cx - a - 52, cy - a - 46), (cx - a - 128, cy - a - 80)),
           "S": ((cx + a + 52, cy + a + 46), (cx + a + 14, cy + a + 76)),
           "E": ((cx + a + 52, cy - a - 46), (cx + a + 14, cy - a - 80)),
           "O": ((cx - a - 52, cy + a + 46), (cx - a - 128, cy + a + 76))}
    for calle in CALLES:
        (hx, hy), (tx, ty) = pos[calle]
        luz = (luces or {}).get(calle)
        caja(hx - 31, hy - 12, hx + 31, hy + 12, (10, 10, 10))
        cv2.rectangle(img, (hx - 31, hy - 12), (hx + 31, hy + 12), C["borde"], 1, AA)
        for j, letra in enumerate("RAV"):
            cv2.circle(img, (hx - 19 + 19 * j, hy), 7, C[letra] if luz == letra else C["apagado"], -1, AA)
        texto(f"{calle}  {NOMBRE_CALLE[calle]}", tx, ty, 0.42)
        sub = camaras.get(calle, "")
        if calle in conteos:
            sub = f"{sub} | {conteos[calle]:g} veh"
        texto(sub, tx, ty + 16, 0.36, "t2")
        if luz == "R" and rojos.get(calle):
            texto(f"rojo {rojos[calle]} s", hx, hy + 27, 0.34, "t3", centro=True)
    if not luces:
        caja(cx - 118, cy - 15, cx + 118, cy + 15, "s1")
        texto("sin luces" if fresco else "sin datos del semaforo", cx, cy + 5, 0.42, "t2", centro=True)

    # --- fase y decisión
    y = 368
    caja(10, y, W - 10, H - 10, "s1")
    if est:
        f = fases.get(est.get("fase"), {})
        nombre = f.get("nombre") or f"fase {est.get('fase')}"
        if est.get("etapa") == "verde":
            texto(f"Verde: {nombre}", 22, y + 25, 0.5, "V")
            texto(f"hace {est.get('t_fase', 0)} s", W - 22, y + 25, 0.4, "t2", der=True)
        else:
            texto(f"Cambiando a: {nombre}", 22, y + 25, 0.5, "A")
            texto("ambar + despeje", W - 22, y + 25, 0.4, "t2", der=True)
        texto(f"Proxima decision en {int(est.get('prox_decision_s') or 0)} s", 22, y + 46, 0.38, "t2")
        dec = est.get("decision") or {}
        q, masc = dec.get("q") or [], dec.get("mascara") or []
        if dec:
            quien = "agente" if dec.get("quien") == "agente" else "ciclo fijo"
            hace = max(0, int(est.get("t", 0)) - int(dec.get("t", 0)))
            ej = fases.get(dec.get("ejecutada"), {}).get("nombre", dec.get("ejecutada"))
            texto(f"Ultima decision ({quien}, hace {hace} s): {ej}", 22, y + 66, 0.36)
            if dec.get("anulada"):
                ped = fases.get(dec.get("pedida"), {}).get("nombre", dec.get("pedida"))
                texto(f"pidio {ped}: anulada por seguridad", 22, y + 82, 0.34, "A")
        if q and fases:
            titulo_q = ("Valor Q del agente por fase (mas alto = preferida)" if dec.get("quien") == "agente"
                        else "Valor Q del agente (solo referencia: decide el ciclo fijo)")
            texto(titulo_q, 22, y + 102, 0.34, "t3")
            lo, hi = min(q), max(q)
            for i, (fid, v) in enumerate(zip([f["id"] for f in cfg["fases"]], q)):
                yy = y + 114 + i * 15
                elegido = fid == dec.get("ejecutada")
                permitido = i >= len(masc) or masc[i]
                texto(fases[fid]["nombre"][:15], 22, yy + 9, 0.33, "t1" if elegido else ("t2" if permitido else "t3"))
                x0, x1 = 140, W - 78
                caja(x0, yy + 2, x1, yy + 9, "s2")
                frac = (v - lo) / (hi - lo) if hi > lo else 1.0
                caja(x0, yy + 2, x0 + max(2, int((x1 - x0) * frac)), yy + 9,
                     "V" if elegido else ("linea" if permitido else "apagado"))
                texto(f"{v:.2f}" + ("" if permitido else " x"), W - 22, yy + 9, 0.32, "t2", der=True)
    else:
        texto("Sin detalle del semaforo", 22, y + 26, 0.45, "t2")
        texto(((sem or {}).get("motivo") or "")[:55], 22, y + 48, 0.36, "t3")
    if fresco and not sem.get("enviando"):
        texto("Sin conteos: " + (sem.get("motivo") or "")[:48], 22, H - 20, 0.34, "R")
    elif est.get("edad_conteos_s") is not None:
        texto(f"Conteos que ve el agente: de hace {est['edad_conteos_s']} s", 22, H - 20, 0.34, "t3")
    return img


class Extensiones:
    """Varias extensiones de conteo4 juntas (envío a laptop/AWS + enlace RL): un solo .detener()."""

    def __init__(self, *exts):
        self.exts = [e for e in exts if e is not None]

    def detener(self):
        for e in self.exts:
            try:
                e.detener()
            except Exception as ex:
                print(f"[RL] error al detener {type(e).__name__}: {ex}")


def crear(camaras, pub):
    """EnlaceRL según rl_zonas.json, o None si está desactivado o la configuración es inválida
    (el conteo sigue igual sin semáforo)."""
    try:
        cfg = cargar_config()
    except (ValueError, OSError) as e:
        print(f"[RL] ERROR en rl_zonas.json: {e}. Sigo SIN enlace con el semáforo.")
        return None
    if not cfg.get("activo", True):
        print("[RL] Enlace con el semáforo desactivado (rl_zonas.json: activo = false).")
        return None
    return EnlaceRL(camaras, pub, cfg)


