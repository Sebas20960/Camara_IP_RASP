# =============================================================================
# pi_falsa.py  -  Imita a la Raspberry (protocolo v2) SIN YOLO, para probar la laptop.
#
# Uso:
#     python pi_falsa.py --destino localhost:8000 --token cambia-esto video1.mp4 [video2 ...]
#
# - 1 a 4 videos. Si das uno solo, se repite en 4 cámaras (cámbialo con --camaras N).
# - Manda 'hola' al conectar, 'datos' cada 1 s (con 'nuevos' plausibles), y SOLO si el
#   receptor pidió video ({"tipo":"visores","video":1,...}): cuadros JPEG (640 px de ancho,
#   calidad 75, cabecera binaria de 14 bytes) a ~10 cuadros/s por cámara y 'det' a ~2 Hz
#   por cámara con cajas falsas que se mueven suave y con IDs estables.
# - Respeta "fps" del mensaje visores (usa min(fps, --tope)). Reconexión simple.
# - --lote N manda al conectar (solo la 1ª vez) N muestras atrasadas (como sin internet).
# =============================================================================
import argparse
import asyncio
import contextlib
import json
import math
import random
import struct
import threading
import time

import cv2
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidStatus

CATS = ["carro", "moto", "bus", "pesado", "especial"]
CABECERA = struct.Struct(">BBId")       # 0x01, cámara, seq, t_cap -> 14 bytes
CARRILES = 4                            # vehículos falsos simultáneos por cámara (máx.)


class Estado:
    """Lo que el receptor pidió (lo escribe el hilo asyncio, lo leen los hilos de cámara)."""
    def __init__(self, tope):
        self.video = 0
        self.fps = tope
        self.tope = tope

    def fps_envio(self):
        return max(0.5, min(self.fps or self.tope, self.tope))


class Salida:
    """Cola de salida: 1 cuadro pendiente por cámara (si va lento se descartan los viejos)
    y una lista de textos (det). La lee el hilo asyncio."""
    def __init__(self, n):
        self.lock = threading.Lock()
        self.cuadros = [None] * n
        self.textos = []
        self.loop = None
        self.evento = None
        self.descartados = 0

    def _avisar(self):
        if self.loop is not None:
            with contextlib.suppress(RuntimeError):
                self.loop.call_soon_threadsafe(self.evento.set)

    def poner_cuadro(self, k, datos):
        with self.lock:
            if self.cuadros[k] is not None:
                self.descartados += 1
            self.cuadros[k] = datos
        self._avisar()

    def poner_texto(self, t):
        with self.lock:
            self.textos = (self.textos + [t])[-50:]
        self._avisar()

    def sacar(self):
        with self.lock:
            textos, self.textos = self.textos, []
            cuadros = [c for c in self.cuadros if c is not None]
            self.cuadros = [None] * len(self.cuadros)
        return textos, cuadros

    def vaciar(self):
        with self.lock:
            self.textos = []
            self.cuadros = [None] * len(self.cuadros)


class CamaraFalsa(threading.Thread):
    """Lee un video como si fuera una cámara MJPEG a --fps-camara cuadros/s y hace un
    'análisis' falso a --det-hz por segundo (cajas que se mueven suave, IDs estables)."""

    def __init__(self, k, ruta, a, estado, salida):
        super().__init__(daemon=True, name=f"Cam{k + 1}")
        self.k, self.ruta, self.a = k, ruta, a
        self.estado, self.salida = estado, salida
        self.lock = threading.Lock()
        self.seq = 0
        self.cajas = []                  # últimas cajas del análisis
        self.nums = {}                   # id -> num asignado
        self.contador = dict.fromkeys(CATS, 0)
        self.nuevos = dict.fromkeys(CATS, 0)   # desde el último 'datos'
        self.vistos = 0
        self.enviados = 0
        self.bytes = 0
        self.parar = False
        rnd = random.Random(1000 + k)
        # cada carril: periodo de paso (s), fase, dirección y franja vertical
        self.carriles = [(rnd.uniform(7, 13), rnd.uniform(0, 13), rnd.choice((1, -1)),
                          0.35 + 0.15 * j) for j in range(CARRILES)]

    # ---------------------------------------------------------------- cajas falsas
    def cajas_en(self, t):
        """Cajas falsas en el instante t: [[x1,y1,x2,y2,cat,id,num,conf], ...] (0..10000)."""
        res = []
        for j, (periodo, fase, sentido, ybase) in enumerate(self.carriles):
            vuelta, p = divmod((t + fase) / periodo, 1.0)
            if p < 0.04 or p > 0.96:            # entrando/saliendo de la imagen
                continue
            ident = int(vuelta) * CARRILES + j + 1 + 1000 * self.k
            cat = CATS[(int(vuelta) * 7 + j * 3) % len(CATS)]
            if cat == "especial" and int(vuelta) % 3:       # 'especial' es raro
                cat = "carro"
            w = {"carro": 0.13, "moto": 0.06, "bus": 0.22, "pesado": 0.2, "especial": 0.13}[cat]
            h = w * 0.8 * (0.8 + 0.4 * p)               # crece un poco (perspectiva)
            cx = p if sentido > 0 else 1 - p
            cy = ybase + 0.05 * math.sin(p * math.pi)
            x1, x2 = max(0.0, cx - w / 2), min(1.0, cx + w / 2)
            y1, y2 = max(0.0, cy - h / 2), min(1.0, cy + h / 2)
            conf = int(70 + 20 * math.sin(t * 0.7 + j))
            res.append([int(x1 * 10000), int(y1 * 10000), int(x2 * 10000), int(y2 * 10000),
                        CATS.index(cat), ident, 0, conf, p])
        return res

    def analizar(self, t):
        """'YOLO' falso: asigna num cuando el vehículo pasa del 20 % (como el conteo real)."""
        cajas = self.cajas_en(t)
        with self.lock:
            for c in cajas:
                ident, p = c[5], c.pop()
                if ident not in self.nums and p > 0.2:
                    cat = CATS[c[4]]
                    self.contador[cat] += 1
                    self.nums[ident] = self.contador[cat]
                    self.nuevos[cat] += 1
                    self.vistos += 1
                c[6] = self.nums.get(ident, 0)
            if len(self.nums) > 200:                    # olvida IDs viejos
                for ident in list(self.nums)[:100]:
                    del self.nums[ident]
            self.cajas = cajas
        return cajas

    def resumen(self):
        """Entrada de esta cámara para el mensaje 'datos' (y reinicia 'nuevos')."""
        with self.lock:
            por_cat = dict.fromkeys(CATS, 0)
            for c in self.cajas:
                por_cat[CATS[c[4]]] += 1
            nuevos, self.nuevos = self.nuevos, dict.fromkeys(CATS, 0)
            return {"nombre": f"Cam{self.k + 1}", "conectada": True, "total": len(self.cajas),
                    "por_categoria": por_cat, "nuevos": nuevos, "vistos": self.vistos,
                    "fps": float(self.a.det_hz)}

    # ---------------------------------------------------------------- bucle
    def run(self):
        cap = cv2.VideoCapture(self.ruta)
        if not cap.isOpened():
            print(f"[Cam{self.k + 1}] ERROR: no se pudo abrir {self.ruta}")
            return
        fps_video = cap.get(cv2.CAP_PROP_FPS) or 30.0
        saltar = max(1, round(fps_video / self.a.fps_camara))
        intervalo = 1.0 / self.a.fps_camara
        t_sig = time.time()
        t_envio = 0.0
        t_analisis = time.time() + random.uniform(0, 1.0 / self.a.det_hz)
        pendientes = []                                 # [(enviar_en, texto_det)]
        while not self.parar:
            quiere_video = self.estado.video > 0
            for _ in range(saltar - 1):
                cap.grab()
            if quiere_video:
                ok, frame = cap.read()
            else:
                ok, frame = cap.grab(), None            # sin video: no decodifica
            if not ok:
                cap.set(cv2.CAP_PROP_POS_FRAMES, 0)     # el video se repite
                continue
            self.seq += 1
            t_cap = time.time()
            # ---- reenvío del cuadro (limitado a min(fps pedido, tope))
            if quiere_video and t_cap - t_envio >= 1.0 / self.estado.fps_envio() - 0.01:
                t_envio = t_cap
                if self.a.ancho and frame.shape[1] > self.a.ancho:
                    e = self.a.ancho / frame.shape[1]
                    frame = cv2.resize(frame, None, fx=e, fy=e, interpolation=cv2.INTER_AREA)
                ok, jpg = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, self.a.calidad])
                if ok:
                    datos = CABECERA.pack(0x01, self.k, self.seq & 0xFFFFFFFF, t_cap) + jpg.tobytes()
                    self.salida.poner_cuadro(self.k, datos)
                    self.enviados += 1
                    self.bytes += len(datos)
            # ---- análisis falso a det_hz (siempre, como YOLO); det solo si hay video
            if t_cap >= t_analisis:
                t_analisis += 1.0 / self.a.det_hz
                if t_analisis < t_cap:
                    t_analisis = t_cap + 1.0 / self.a.det_hz
                cajas = self.analizar(t_cap)
                if quiere_video:
                    det = {"tipo": "det", "c": self.k, "s": self.seq & 0xFFFFFFFF, "t": t_cap,
                           "b": cajas}
                    # el resultado de YOLO sale ~0.25 s después de capturar el cuadro
                    pendientes.append((t_cap + self.a.latencia, json.dumps(det, separators=(",", ":"))))
            ahora = time.time()
            while pendientes and pendientes[0][0] <= ahora:
                texto = pendientes.pop(0)[1]
                if self.estado.video > 0:
                    self.salida.poner_texto(texto)
            # ---- ritmo de la "cámara"
            t_sig += intervalo
            espera = t_sig - time.time()
            if espera > 0:
                time.sleep(espera)
            else:
                t_sig = time.time()
        cap.release()


# =============================================================================
# CONEXIÓN (asyncio)
# =============================================================================
async def sesion(uri, camaras, estado, salida, a, primera):
    async with connect(uri, compression=None, max_size=4 * 1024 * 1024, proxy=None,
                       open_timeout=5, ping_interval=20, ping_timeout=20) as ws:
        print(f"[PI_FALSA] Conectada a {uri.split('?')[0]}")
        estado.video = 0                       # hasta que el receptor diga otra cosa
        salida.vaciar()
        await ws.send(json.dumps({"tipo": "hola", "version": 2,
                                  "camaras": [f"Cam{k + 1}" for k in range(len(camaras))]}))
        if primera and a.lote > 0:
            ahora = time.time()
            muestras = [muestra_datos(camaras, ahora - 60 * (a.lote - i), falsa=True)
                        for i in range(a.lote)]
            await ws.send(json.dumps({"tipo": "lote", "muestras": muestras}))
            print(f"[PI_FALSA] Mandé un lote de {len(muestras)} muestras atrasadas")

        async def recibir():
            async for msg in ws:
                if isinstance(msg, str):
                    with contextlib.suppress(ValueError, TypeError):
                        d = json.loads(msg)
                        if d.get("tipo") == "visores":
                            estado.video = int(d.get("video") or 0)
                            if d.get("fps"):
                                estado.fps = float(d["fps"])
                            if not estado.video:
                                salida.vaciar()
                            print(f"[PI_FALSA] visores: video={estado.video} "
                                  f"fps={estado.fps_envio():g}")

        async def enviar():
            while True:
                await salida.evento.wait()
                salida.evento.clear()
                textos, cuadros = salida.sacar()
                for t in textos:
                    await ws.send(t)
                if estado.video:
                    for c in cuadros:
                        await ws.send(c)

        async def datos():
            while True:
                await asyncio.sleep(1.0 - time.time() % 1.0)
                await ws.send(json.dumps(muestra_datos(camaras, time.time())))

        tareas = [asyncio.create_task(f()) for f in (recibir, enviar, datos)]
        try:
            hecho, _ = await asyncio.wait(tareas, return_when=asyncio.FIRST_COMPLETED)
            for t in hecho:
                t.result()                       # propaga el error (p. ej. conexión cerrada)
        finally:
            for t in tareas:
                t.cancel()


def muestra_datos(camaras, t, falsa=False):
    if falsa:                                    # muestra inventada para el lote
        cams = [{"nombre": f"Cam{k + 1}", "conectada": True, "total": random.randint(0, 4),
                 "por_categoria": dict.fromkeys(CATS, 0),
                 "nuevos": {"carro": random.randint(0, 2), "moto": random.randint(0, 1),
                            **dict.fromkeys(CATS[2:], 0)},
                 "vistos": 0, "fps": 2.0} for k in range(len(camaras))]
    else:
        cams = [c.resumen() for c in camaras]
    return {"tipo": "datos", "t": t, "fps": sum(c["fps"] for c in cams), "temp": 55.0,
            "total": sum(c["total"] for c in cams), "camaras": cams}


async def principal(a):
    n = a.camaras or (4 if len(a.videos) == 1 else len(a.videos))
    estado = Estado(a.tope)
    salida = Salida(n)
    salida.loop = asyncio.get_running_loop()
    salida.evento = asyncio.Event()
    camaras = [CamaraFalsa(k, a.videos[k % len(a.videos)], a, estado, salida) for k in range(n)]
    for c in camaras:
        c.start()
    uri = f"ws://{a.destino}/ws/pi?token={a.token}"
    fin = time.time() + a.duracion if a.duracion else None

    async def informe():
        ant = [(0, 0)] * n
        while True:
            await asyncio.sleep(5)
            act = [(c.enviados, c.bytes) for c in camaras]
            cps = sum(x[0] - y[0] for x, y in zip(act, ant)) / 5
            kbps = sum(x[1] - y[1] for x, y in zip(act, ant)) * 8 / 1000 / 5
            ant = act
            print(f"[PI_FALSA] video={estado.video} enviando {cps:.1f} cuadros/s "
                  f"({kbps:.0f} kbps), descartados={salida.descartados}")

    tarea_informe = asyncio.create_task(informe())
    primera = True
    try:
        while fin is None or time.time() < fin:
            try:
                restante = None if fin is None else max(0.1, fin - time.time())
                await asyncio.wait_for(sesion(uri, camaras, estado, salida, a, primera), restante)
                print("[PI_FALSA] El receptor cerró la conexión; reintento en 2 s")
            except asyncio.TimeoutError:
                break
            except InvalidStatus as e:
                print(f"[PI_FALSA] Rechazada por el receptor: HTTP {e.response.status_code}"
                      + (" (¿token incorrecto?)" if e.response.status_code == 403 else ""))
            except (OSError, ConnectionClosed, asyncio.TimeoutError) as e:
                print(f"[PI_FALSA] Sin conexión ({type(e).__name__}: {e}); reintento en 2 s")
            primera = False
            estado.video = 0
            await asyncio.sleep(2)
    finally:
        tarea_informe.cancel()
        for c in camaras:
            c.parar = True


def main():
    ap = argparse.ArgumentParser(description="Raspberry falsa (protocolo v2, sin YOLO)")
    ap.add_argument("videos", nargs="+", help="1 a 4 videos (uno por cámara)")
    ap.add_argument("--destino", default="localhost:8000", help="IP:puerto del receptor")
    ap.add_argument("--token", default="cambia-esto")
    ap.add_argument("--camaras", type=int, default=0,
                    help="número de cámaras (por defecto: 4 si das 1 video, si no uno por video)")
    ap.add_argument("--tope", type=float, default=10, help="tope de cuadros/s por cámara")
    ap.add_argument("--fps-camara", type=float, default=10, help="cuadros/s de la 'cámara'")
    ap.add_argument("--det-hz", type=float, default=2, help="análisis (det) por s por cámara")
    ap.add_argument("--latencia", type=float, default=0.25, help="s que 'tarda' YOLO")
    ap.add_argument("--ancho", type=int, default=640)
    ap.add_argument("--calidad", type=int, default=75)
    ap.add_argument("--lote", type=int, default=0, help="muestras atrasadas a mandar al conectar")
    ap.add_argument("--duracion", type=float, default=0, help="s antes de terminar (0 = nunca)")
    a = ap.parse_args()
    if not 1 <= len(a.videos) <= 4:
        ap.error("da de 1 a 4 videos")
    try:
        asyncio.run(principal(a))
    except KeyboardInterrupt:
        print("\n[PI_FALSA] Detenida.")


if __name__ == "__main__":
    main()
