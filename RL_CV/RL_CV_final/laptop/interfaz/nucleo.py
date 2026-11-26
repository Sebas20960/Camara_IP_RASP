# =============================================================================
# nucleo.py  -  Núcleo (sin Qt) del programa de escritorio de la laptop
#
# - Receptor: servidor WebSocket (hilo propio, asyncio + websockets) que recibe a la
#   Raspberry en ws://0.0.0.0:<puerto>/ws/pi?token=<token> con el protocolo v2
#   (../../docs/PROTOCOLO_v2.md): datos de conteo, cuadros JPEG de las cámaras y cajas (det).
#   Guarda los cuadros y las cajas en buffers por cámara y, cuando la ventana pregunta
#   con cuadro(cam), aplica el algoritmo de interpolación del protocolo.
# - Agente: recibe al agente de la Raspberry (raspberry/agente.py) en /ws/agente para
#   iniciar/detener el conteo desde la ventana, ver su registro y el estado de la Pi.
#   El agente también reenvía órdenes del dashboard web (AWS) para las cámaras simuladas.
# - Anuncio: cada 3 s avisa por la red local (UDP 37020) "aquí estoy, puerto N", así la
#   Raspberry encuentra sola la IP de la laptop aunque el router se la cambie.
# - Historial: SQLite por minuto, misma lógica y mismo formato que el servidor AWS.
# - Simulador: lanza ../camfalsa4.py (4 cámaras IP falsas) como subproceso.
#
# La ventana (app.py) solo usa lo descrito en API_NUCLEO.md, sondeando con un QTimer.
# Requisitos: pip install websockets numpy opencv-python   (ver requirements.txt)
# =============================================================================
import asyncio
import bisect
import collections
import contextlib
import copy
import csv
import json
import os
import secrets
import socket
import sqlite3
import struct
import subprocess
import sys
import threading
import time
import urllib.parse
from datetime import datetime

from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed

CARPETA = os.path.dirname(os.path.abspath(__file__))

CATS = ["carro", "moto", "bus", "pesado", "especial"]
COLORES = {"carro": "#28aaeb", "moto": "#fac828", "bus": "#d25ac8",
           "pesado": "#ff821e", "especial": "#eb3c46"}

PASOS = {60, 300, 900, 3600}     # segundos por punto de la gráfica del historial
DIAS_HISTORIAL = 90              # días que se guarda el historial
MAX_MENSAJE = 4 * 1024 * 1024    # tamaño máximo de un mensaje WebSocket (bytes)
MAX_CAMARAS = 8                  # índices de cámara aceptados: 0..7
CABECERA = struct.Struct(">BBId")    # tipo, cámara, seq (uint32), t_cap (float64) = 14 bytes
TIPO_CUADRO = 0x01

# Reglas del algoritmo de interpolación (PROTOCOLO_v2.md)
MAX_HUECO_DET = 2.0       # si d1.t - d0.t >= 2.0 s no se interpola (se usan las de d0)
MAX_EDAD_D0 = 1.5         # sin d1: las cajas de d0 valen hasta 1.5 s
GUARDA_CUADROS = 4.0      # segundos de cuadros que se guardan por cámara
GUARDA_DETS = 5.0         # segundos de det que se guardan por cámara
SIN_VIDEO = 5.0           # s sin recibir cuadros de una cámara -> cuadro() devuelve None
RETRASO_MAX_AUTO = 2.5    # el retraso automático no pasa de esto (s)

PUERTO_ANUNCIO = 37020    # UDP: anuncio de la laptop para que el agente de la Pi la encuentre
INTERVALO_ANUNCIO = 3.0
LINEAS_LOG_AGENTE = 600


def ip_local():
    """IP de la laptop en la red local (la que hay que ponerle a la Raspberry)."""
    try:
        with contextlib.closing(socket.socket(socket.AF_INET, socket.SOCK_DGRAM)) as s:
            s.connect(("8.8.8.8", 80))       # no envía nada: solo elige la interfaz de red
            return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"


# =============================================================================
# HISTORIAL (SQLite, una fila por cámara y minuto) — misma lógica que servidor.py
# =============================================================================
ESQUEMA = """
CREATE TABLE IF NOT EXISTS minuto (
    ts       INTEGER NOT NULL,      -- inicio del minuto (epoch UTC, segundos)
    camara   TEXT    NOT NULL,
    muestras INTEGER NOT NULL,      -- cuántas muestras (≈ segundos) llegaron en ese minuto
    suma     INTEGER NOT NULL,      -- suma de 'vehículos en imagen' -> promedio = suma/muestras
    maximo   INTEGER NOT NULL,      -- máximo de 'vehículos en imagen'
    carro INTEGER NOT NULL, moto INTEGER NOT NULL, bus INTEGER NOT NULL,
    pesado INTEGER NOT NULL, especial INTEGER NOT NULL,   -- vehículos NUEVOS (flujo)
    PRIMARY KEY (ts, camara)
);
"""
UPSERT = f"""
INSERT INTO minuto (ts, camara, muestras, suma, maximo, {", ".join(CATS)})
VALUES (?, ?, ?, ?, ?, {", ".join("?" * len(CATS))})
ON CONFLICT(ts, camara) DO UPDATE SET
    muestras = muestras + excluded.muestras,
    suma     = suma + excluded.suma,
    maximo   = MAX(maximo, excluded.maximo),
    {", ".join(f"{c} = {c} + excluded.{c}" for c in CATS)};
"""


class Historial:
    """
    Las muestras se acumulan en memoria por (minuto, cámara) y se escriben a SQLite cuando
    el minuto termina. Todo es SUMABLE (muestras, suma, nuevos), así que si la Raspberry
    manda atrasado lo que guardó sin conexión (lote), se suma al minuto que le toca.
    Igual que en servidor.py, más un lock porque aquí lo usan varios hilos.
    """

    def __init__(self, ruta):
        self.ruta = ruta
        self.buckets = {}
        self._lock = threading.Lock()
        with contextlib.closing(self._con()) as con:
            con.executescript(ESQUEMA)

    def _con(self):
        con = sqlite3.connect(self.ruta, timeout=10)
        con.execute("PRAGMA journal_mode=WAL")
        return con

    def agregar(self, muestra):
        if not isinstance(muestra, dict):
            return
        ahora = time.time()
        t = muestra.get("t")
        if not isinstance(t, (int, float)) or t > ahora + 300 or t < ahora - DIAS_HISTORIAL * 86400:
            t = ahora                     # reloj de la Pi raro: uso la hora de la laptop
        m = int(t // 60 * 60)
        with self._lock:
            for c in muestra.get("camaras") or []:
                if not isinstance(c, dict):
                    continue
                nombre = str(c.get("nombre", "?"))[:40]
                b = self.buckets.get((m, nombre))
                if b is None:
                    b = self.buckets[(m, nombre)] = {"muestras": 0, "suma": 0, "maximo": 0,
                                                     **dict.fromkeys(CATS, 0)}
                try:
                    tot = max(int(c.get("total") or 0), 0)
                except (TypeError, ValueError):
                    tot = 0
                b["muestras"] += 1
                b["suma"] += tot
                b["maximo"] = max(b["maximo"], tot)
                nuevos = c.get("nuevos") or {}
                if isinstance(nuevos, dict):
                    for cat, v in nuevos.items():
                        if cat in CATS:
                            with contextlib.suppress(TypeError, ValueError):
                                b[cat] += max(int(v or 0), 0)

    def en_memoria(self):
        """Copia de los minutos que aún no se escribieron (para consultas)."""
        with self._lock:
            return {k: dict(b) for k, b in self.buckets.items()}

    def sacar_terminados(self, todo=False):
        """Quita de memoria los minutos ya cerrados y los devuelve como filas para SQLite."""
        limite = int(time.time() // 60 * 60)
        filas = []
        with self._lock:
            for k in [k for k in self.buckets if todo or k[0] < limite]:
                b = self.buckets.pop(k)
                filas.append((k[0], k[1], b["muestras"], b["suma"], b["maximo"],
                              *[b[c] for c in CATS]))
        return filas

    def escribir(self, filas):
        with contextlib.closing(self._con()) as con, con:
            con.executemany(UPSERT, filas)

    def purgar(self):
        limite = int(time.time() - DIAS_HISTORIAL * 86400)
        with contextlib.closing(self._con()) as con, con:
            con.execute("DELETE FROM minuto WHERE ts < ?", (limite,))

    def filas(self, desde, hasta, en_memoria):
        """Filas por minuto (de SQLite + las del minuto en curso que aún están en memoria)."""
        with contextlib.closing(self._con()) as con:
            res = con.execute(f"SELECT ts, camara, muestras, suma, maximo, {', '.join(CATS)} "
                              f"FROM minuto WHERE ts >= ? AND ts < ? ORDER BY ts", (desde, hasta))
            filas = [list(f) for f in res]
        for (m, cam), b in en_memoria.items():
            if desde <= m < hasta:
                filas.append([m, cam, b["muestras"], b["suma"], b["maximo"], *[b[c] for c in CATS]])
        return filas

    def consultar(self, horas, paso, en_memoria, orden=()):
        hasta = int(time.time() // paso * paso + paso)
        desde = hasta - int(horas * 3600)
        desde -= desde % paso
        ts = list(range(desde, hasta, paso))
        idx = {t: i for i, t in enumerate(ts)}
        camaras = {}
        cats = dict.fromkeys(CATS, 0)
        for t, cam, muestras, suma, maximo, *nuevos in self.filas(desde, hasta, en_memoria):
            d = camaras.get(cam)
            if d is None:
                d = camaras[cam] = {"flujo": [0] * len(ts), "_suma": [0] * len(ts),
                                    "_muestras": [0] * len(ts), "maximo": [0] * len(ts),
                                    "por_categoria": dict.fromkeys(CATS, 0)}
            i = idx.get(t // paso * paso)
            if i is None:
                continue
            d["flujo"][i] += sum(nuevos)
            d["_suma"][i] += suma
            d["_muestras"][i] += muestras
            d["maximo"][i] = max(d["maximo"][i], maximo)
            for c, v in zip(CATS, nuevos):
                d["por_categoria"][c] += v
                cats[c] += v
        for d in camaras.values():
            muestras = d.pop("_muestras")
            # None = no llegaron datos en ese intervalo (hueco en la gráfica, no un 0)
            d["ocupacion"] = [round(s / m, 2) if m else None for s, m in zip(d.pop("_suma"), muestras)]
            d["flujo_total"] = sum(d["flujo"])
            d["flujo"] = [f if m else None for f, m in zip(d["flujo"], muestras)]
            d["maximo"] = [x if m else None for x, m in zip(d["maximo"], muestras)]
        # mismo orden que las cámaras de la Raspberry (así cada una conserva su color)
        pos = {n: i for i, n in enumerate(orden)}
        claves = sorted(camaras, key=lambda n: (pos.get(n, len(pos)), n))
        return {"desde": desde, "hasta": hasta, "paso": paso, "ts": ts,
                "camaras": {n: camaras[n] for n in claves}, "categorias": cats,
                "flujo_total": sum(cats.values())}


# =============================================================================
# BUFFERS POR CÁMARA + INTERPOLACIÓN
# =============================================================================
def _preparar_cajas(b):
    """Convierte det.b ([[x1,y1,x2,y2,cat,id,num,conf], ...]) a tuplas listas para pintar:
    (x1, y1, x2, y2, cat, id|None, num|None, conf, texto) con coordenadas 0..1.
    Se hace UNA vez al llegar el det, así cuadro() es barato."""
    cajas = []
    for c in b or []:
        try:
            x1, y1, x2, y2 = (float(v) / 10000.0 for v in c[:4])
            icat = int(c[4])
            ident = int(c[5])
            num = int(c[6])
            conf = float(c[7]) / 100.0
        except (TypeError, ValueError, IndexError):
            continue
        cat = CATS[icat] if 0 <= icat < len(CATS) else "?"
        texto = f"id:{num} {cat} {conf:.2f}" if num else f"{cat} {conf:.2f}"
        cajas.append((x1, y1, x2, y2, cat, ident if ident >= 0 else None,
                      num if num else None, conf, texto))
    return cajas


def _caja_dict(c, x1=None, y1=None, x2=None, y2=None):
    return {"x1": c[0] if x1 is None else x1, "y1": c[1] if y1 is None else y1,
            "x2": c[2] if x2 is None else x2, "y2": c[3] if y2 is None else y2,
            "cat": c[4], "id": c[5], "num": c[6], "conf": c[7], "texto": c[8]}


def cajas_en(T, d0, d1):
    """Cajas en el instante T (reloj de la Pi) según PROTOCOLO_v2.md.
    d0 = último det con t <= T; d1 = primer det con t > T (cualquiera puede ser None).
    Cada det es (seq, t, cajas_preparadas, {id: caja})."""
    if d0 is None:
        return []
    t0, cajas0 = d0[1], d0[2]
    if d1 is not None and d1[1] - t0 < MAX_HUECO_DET:
        a = (T - t0) / (d1[1] - t0)
        por_id1 = d1[3]
        res = []
        for c in cajas0:
            c1 = por_id1.get(c[5]) if c[5] is not None else None
            if c1 is None:                       # id = -1 o id que no está en d1
                res.append(_caja_dict(c))
            else:                                # interpolación lineal por id
                res.append(_caja_dict(c, c[0] + (c1[0] - c[0]) * a, c[1] + (c1[1] - c[1]) * a,
                                      c[2] + (c1[2] - c[2]) * a, c[3] + (c1[3] - c[3]) * a))
        return res
    if T - t0 < MAX_EDAD_D0:
        return [_caja_dict(c) for c in cajas0]
    return []


class _Camara:
    """Buffers de una cámara. Listas ordenadas por t (los t son reloj de la Pi)."""
    __slots__ = ("cuadros", "t_cuadros", "dets", "t_dets", "t_pi", "local_llegada",
                 "local_cuadro", "t_det_ant", "T")

    def __init__(self):
        self.cuadros = []        # [(seq, t, jpeg)]
        self.t_cuadros = []      # [t] paralelo (para bisect)
        self.dets = []           # [(seq, t, cajas, {id: caja})]
        self.t_dets = []
        self.t_pi = None         # mayor t recibido (cuadro o det)
        self.local_llegada = 0.0  # time.monotonic() cuando llegó ese t
        self.local_cuadro = 0.0   # time.monotonic() del último cuadro recibido
        self.t_det_ant = None     # t del último det (para medir cada cuánto analiza YOLO)
        self.T = None             # último instante mostrado (el video no retrocede)

    def marcar(self, t, ahora):
        if self.t_pi is None or t > self.t_pi:
            self.t_pi = t
            self.local_llegada = ahora

    def podar(self, guarda_c, guarda_d):
        if self.t_pi is None:
            return
        k = bisect.bisect_left(self.t_cuadros, self.t_pi - guarda_c)
        if k:
            del self.cuadros[:k], self.t_cuadros[:k]
        k = bisect.bisect_left(self.t_dets, self.t_pi - guarda_d)
        if k:
            del self.dets[:k], self.t_dets[:k]


# =============================================================================
# RECEPTOR
# =============================================================================
class Receptor:
    """Servidor WebSocket (hilo propio con asyncio + librería websockets) que recibe a la Pi
    en ws://0.0.0.0:<puerto>/ws/pi?token=<token>. Guarda historial en SQLite."""

    def __init__(self, puerto=8000, token="cambia-esto",
                 db_ruta=os.path.join(CARPETA, "historial_laptop.db"),
                 retraso=1.0, fps_video=10):
        self.puerto = int(puerto)
        self.token = str(token)
        self.fps_video = fps_video
        self.hist = Historial(db_ruta)
        self.error = None                # texto del error si no se pudo abrir el puerto

        self._lock = threading.Lock()
        self._retraso = max(0.0, float(retraso))     # mínimo elegido por el usuario
        self._retraso_auto = 0.0         # lo que hace falta para interpolar (como el dashboard web)
        self._intervalos = collections.deque(maxlen=40)   # s entre dos det de la misma cámara
        self._demoras = collections.deque(maxlen=40)      # s que tarda en llegar un det
        self._video = True               # ¿la ventana quiere video?
        self._cams = {}                  # índice -> _Camara
        self._camaras = []               # nombres (del 'hola' o del último 'datos')
        self._ultimo = None              # último 'datos' (dict)
        self._t_ultimo = 0.0             # time.time() local de ese 'datos'
        self._semaforo = None            # último 'semaforo' (semáforo RL), si la Pi lo manda
        self._t_semaforo = 0.0
        self._sem_t_ctrl = None
        self._sem_t_dec = None
        self._sem_cfg = None             # última config de fases conocida (sigue al desconectarse)
        self._sem_hist = collections.deque(maxlen=1800)   # 30 min, 1 por segundo
        self._sem_dec = collections.deque(maxlen=60)
        self._ip_pi = None
        self._pi = None                  # conexión WebSocket actual (solo hilo asyncio)
        self._bytes = 0                  # contadores para las estadísticas
        self._n_cuadros = 0
        self._kbps = 0.0
        self._cps = 0.0
        # agente de la Raspberry (/ws/agente)
        self._agente = None              # conexión actual (solo hilo asyncio)
        self._agente_ip = None
        self._agente_estado = None       # último {"tipo":"agente",...}
        self._agente_log = collections.deque(maxlen=LINEAS_LOG_AGENTE)
        self._agente_n_log = 0           # crece con cada línea (la ventana sabe si cambió)
        self._agente_resp = collections.deque(maxlen=30)    # (time, accion, ok, msg)
        self._pedidos_sim = collections.deque(maxlen=10)    # órdenes para el simulador
        self._sim_informado = None

        self._hilo = None
        self._loop = None
        self._parar = None
        self._listo = threading.Event()
        self._detenido = False

    # ------------------------------------------------------------------ control
    def iniciar(self):
        """Lanza el hilo del servidor. Espera (como mucho ~3 s, normalmente milisegundos)
        a que el puerto quede abierto; si no se pudo abrir lanza OSError."""
        if self._hilo is not None:
            return
        self._hilo = threading.Thread(target=self._correr, name="ReceptorWS", daemon=True)
        self._hilo.start()
        self._listo.wait(3.0)
        if self.error:
            self._hilo.join(1.0)
            self._hilo = None
            raise OSError(self.error)

    def detener(self):
        """Cierra conexiones, para el hilo y guarda en SQLite el minuto en curso."""
        if self._detenido:
            return
        self._detenido = True
        if self._loop is not None and self._parar is not None:
            with contextlib.suppress(RuntimeError):
                self._loop.call_soon_threadsafe(self._parar.set)
        if self._hilo is not None:
            self._hilo.join(8.0)
        filas = self.hist.sacar_terminados(todo=True)
        if filas:
            try:
                self.hist.escribir(filas)
            except sqlite3.Error as e:
                print(f"[HISTORIAL] Error al guardar al cerrar: {e}")

    def set_video(self, activo):
        """Activa/desactiva el video: avisa a la Pi con {"tipo":"visores",...}.
        Al desactivar se vacían los buffers (cuadro() pasa a devolver None)."""
        activo = bool(activo)
        with self._lock:
            if activo == self._video:
                return
            self._video = activo
            if not activo:
                for c in self._cams.values():
                    c.cuadros.clear(), c.t_cuadros.clear(), c.dets.clear(), c.t_dets.clear()
                    c.t_det_ant = c.T = None
                self._intervalos.clear()
                self._demoras.clear()
        self._en_loop(self._avisar_pi())

    def set_retraso(self, segundos):
        """RETRASO MÍNIMO del video (s) respecto al último dato de la Pi (0..10 s). Si YOLO
        analiza cada cámara más despacio, el retraso sube solo lo necesario (hasta 2.5 s)
        para que las cajas sigan moviéndose suave (interpoladas) en vez de saltar."""
        with self._lock:
            self._retraso = min(max(float(segundos), 0.0), 10.0)

    def _retraso_efectivo(self):
        return max(self._retraso, self._retraso_auto)

    # ------------------------------------------------------------------ agente
    def agente(self, con_log=False):
        """Estado del agente de la Pi: {"conectado", "ip", "estado" (dict|None), "n_log",
        "respuestas": [(t, accion, ok, msg)], "log": [str] (solo con con_log=True)}."""
        with self._lock:
            d = {"conectado": self._agente is not None, "ip": self._agente_ip,
                 "estado": copy.deepcopy(self._agente_estado), "n_log": self._agente_n_log,
                 "respuestas": list(self._agente_resp)}
            if con_log:
                d["log"] = list(self._agente_log)
        return d

    def orden_agente(self, accion, **extra):
        """Manda una orden al agente (iniciar, detener, reiniciar, reiniciar_pi, apagar_pi,
        log...). iniciar acepta camaras=[...] (o None = las de conteo4.py).
        Devuelve False si el agente no está conectado."""
        if self._agente is None:
            return False
        self._en_loop(self._enviar_agente({"tipo": "orden", "accion": accion, **extra}))
        return True

    def pedidos_sim(self):
        """Órdenes para el simulador que llegaron del agente ("iniciar", "detener", "estado").
        Las saca de la cola: la ventana las ejecuta y luego llama a informar_sim()."""
        with self._lock:
            r = list(self._pedidos_sim)
            self._pedidos_sim.clear()
        return r

    def informar_sim(self, activo, listo, urls, error=None, forzar=False):
        """Le cuenta al agente cómo está el simulador (solo si cambió, o con forzar)."""
        msg = {"tipo": "sim_estado", "activo": bool(activo), "listo": bool(listo),
               "urls": list(urls or []), "error": error}
        clave = json.dumps(msg, sort_keys=True)
        if self._agente is None or (clave == self._sim_informado and not forzar):
            return
        self._sim_informado = clave
        self._en_loop(self._enviar_agente(msg))

    async def _enviar_agente(self, msg):
        ws = self._agente
        if ws is not None:
            with contextlib.suppress(Exception):
                await ws.send(json.dumps(msg))

    # ------------------------------------------------------------------ lecturas
    def estado(self):
        with self._lock:
            camaras = list(self._camaras)
            if not camaras and self._ultimo:
                camaras = [str(c.get("nombre", "?")) for c in self._ultimo.get("camaras") or []
                           if isinstance(c, dict)]
            return {"pi_conectada": self._pi is not None, "ip_pi": self._ip_pi,
                    "camaras": camaras, "t_ultimo": self._t_ultimo,
                    "ultimo": copy.deepcopy(self._ultimo),
                    "video_kbps": self._kbps, "cuadros_por_s": self._cps,
                    "retraso": round(self._retraso_efectivo(), 2)}

    # ------------------------------------------------------------------ semáforo RL
    def semaforo(self):
        """Estado del semáforo con RL que reenvía la Pi (rl_zonas.json: "interfaz": true), o None
        si nunca llegó. {"datos": último mensaje, "edad": s desde que la Pi lo generó,
        "pi_conectada", "historia": [(hora, fase, control, etapa)], "decisiones": [...]}"""
        with self._lock:
            if self._semaforo is None:
                return None
            ahora = time.time()
            edad = float(self._semaforo.get("edad") or 0) + (ahora - self._t_semaforo)
            return {"datos": copy.deepcopy(self._semaforo), "edad": edad,
                    "pi_conectada": self._pi is not None,
                    "historia": list(self._sem_hist), "decisiones": list(self._sem_dec),
                    "config": self._sem_cfg}

    def _recibir_semaforo(self, d):
        ahora = time.time()
        est = d.get("estado") if isinstance(d.get("estado"), dict) else None
        with self._lock:
            self._semaforo = d
            self._t_semaforo = ahora
            if est is None or not d.get("conectado"):
                return
            if isinstance(est.get("config"), dict):
                self._sem_cfg = est["config"]
            t_ctrl = est.get("t")
            if t_ctrl != self._sem_t_ctrl:          # un registro por segundo del semáforo
                self._sem_t_ctrl = t_ctrl
                self._sem_hist.append((ahora, est.get("fase"), est.get("control"), est.get("etapa")))
            dec = est.get("decision")
            if isinstance(dec, dict) and dec.get("t") != self._sem_t_dec:
                self._sem_t_dec = dec.get("t")
                try:
                    hora = ahora - max(0, int(t_ctrl) - int(dec.get("t")))
                except (TypeError, ValueError):
                    hora = ahora
                self._sem_dec.append(dict(dec, hora=hora))

    def cuadro(self, cam):
        """(seq, jpeg, cajas) que hay que mostrar AHORA en la cámara `cam`, o None."""
        ahora = time.monotonic()
        with self._lock:
            c = self._cams.get(cam)
            if c is None or not c.cuadros or ahora - c.local_cuadro > SIN_VIDEO:
                return None
            T = c.t_pi + (ahora - c.local_llegada) - self._retraso_efectivo()
            if c.T is not None and c.T - 0.5 < T < c.T:
                T = c.T                          # si el retraso sube, el video se frena, no retrocede
            c.T = T
            # cuadro: el último con t <= T (si no hay, el más viejo)
            k = bisect.bisect_right(c.t_cuadros, T)
            seq, t_cuadro, jpeg = c.cuadros[k - 1] if k else c.cuadros[0]
            # Las cajas se calculan en el instante del CUADRO mostrado (no en T): así coinciden
            # exactamente con la imagen (si ese cuadro fue analizado, la caja es la exacta).
            # d0 = último det con t <= t_cuadro ; d1 = primer det con t > t_cuadro
            k = bisect.bisect_right(c.t_dets, t_cuadro)
            d0 = c.dets[k - 1] if k else None
            d1 = c.dets[k] if k < len(c.dets) else None
        return seq, jpeg, cajas_en(t_cuadro, d0, d1)

    def historial(self, horas, paso):
        """MISMO formato que GET /api/historial del servidor AWS. paso ∈ {60,300,900,3600}."""
        paso = int(paso)
        if paso not in PASOS:
            raise ValueError(f"paso debe ser uno de {sorted(PASOS)}")
        horas = float(horas)
        if horas <= 0:
            raise ValueError("horas debe ser > 0")
        if horas * 3600 / paso > 5000:
            raise ValueError("demasiados puntos: usa un paso mayor")
        with self._lock:
            orden = list(self._camaras)
        return self.hist.consultar(horas, paso, self.hist.en_memoria(), orden)

    def exportar_csv(self, ruta, horas):
        """CSV por minuto (hora local del sistema). Devuelve cuántas filas escribió."""
        hasta = int(time.time() // 60 * 60 + 60)
        filas = self.hist.filas(hasta - int(float(horas) * 3600), hasta, self.hist.en_memoria())
        filas.sort(key=lambda f: (f[0], f[1]))
        with open(ruta, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(["fecha_local", "camara", "vehiculos_nuevos", *[f"nuevos_{c}" for c in CATS],
                        "en_imagen_promedio", "en_imagen_maximo", "muestras"])
            for t, cam, muestras, suma, maximo, *nuevos in filas:
                w.writerow([datetime.fromtimestamp(t).strftime("%Y-%m-%d %H:%M"), cam, sum(nuevos),
                            *nuevos, round(suma / muestras, 2) if muestras else "", maximo,
                            muestras])
        return len(filas)

    # ------------------------------------------------------------------ hilo asyncio
    def _en_loop(self, corrutina):
        """Ejecuta una corrutina en el hilo del servidor (desde cualquier hilo)."""
        if self._loop is None or self._loop.is_closed():
            corrutina.close()
            return
        try:
            asyncio.run_coroutine_threadsafe(corrutina, self._loop)
        except RuntimeError:
            corrutina.close()

    def _correr(self):
        try:
            asyncio.run(self._principal())
        except Exception as e:                       # no debería pasar
            self.error = self.error or f"{type(e).__name__}: {e}"
            print(f"[RECEPTOR] Error: {self.error}")
        finally:
            self._listo.set()

    async def _principal(self):
        self._loop = asyncio.get_running_loop()
        self._parar = asyncio.Event()
        try:
            servidor = await serve(self._manejar, "0.0.0.0", self.puerto,
                                   process_request=self._filtrar,
                                   compression=None,        # JPEG no se comprime: no gastar CPU
                                   max_size=MAX_MENSAJE, max_queue=64,
                                   ping_interval=20, ping_timeout=20)
        except OSError as e:
            self.error = f"No se pudo abrir el puerto {self.puerto}: {e}"
            print(f"[RECEPTOR] {self.error}")
            return
        print(f"[RECEPTOR] Escuchando en ws://0.0.0.0:{self.puerto}/ws/pi  "
              f"(en la Raspberry: {ip_local()}:{self.puerto})")
        self._listo.set()
        tareas = [asyncio.create_task(self._tarea_guardado()),
                  asyncio.create_task(self._tarea_estadisticas()),
                  asyncio.create_task(self._tarea_anuncio())]
        if self._detenido:
            self._parar.set()
        await self._parar.wait()
        for t in tareas:
            t.cancel()
        servidor.close()                             # cierra también la conexión de la Pi
        with contextlib.suppress(Exception):
            await asyncio.wait_for(servidor.wait_closed(), 5)

    def _filtrar(self, conexion, peticion):
        """Antes del handshake: solo /ws/pi con el token correcto."""
        u = urllib.parse.urlsplit(peticion.path)
        if u.path not in ("/ws/pi", "/ws/agente"):
            return conexion.respond(404, "Ruta no encontrada\n")
        token = urllib.parse.parse_qs(u.query).get("token", [""])[0]
        if not secrets.compare_digest(token.encode(), self.token.encode()):
            ip = conexion.remote_address[0] if conexion.remote_address else "?"
            print(f"[RECEPTOR] Conexión rechazada (token incorrecto) desde {ip}")
            return conexion.respond(403, "Token incorrecto\n")
        return None

    async def _avisar_pi(self, ws=None):
        ws = ws or self._pi
        if ws is None:
            return
        with self._lock:
            msg = {"tipo": "visores", "video": 1 if self._video else 0, "total": 1,
                   "fps": self.fps_video, "ack": True}
        with contextlib.suppress(Exception):
            await ws.send(json.dumps(msg))

    async def _manejar(self, ws):
        if urllib.parse.urlsplit(ws.request.path).path == "/ws/agente":
            await self._manejar_agente(ws)
            return
        vieja, self._pi = self._pi, ws            # una sola Pi: la nueva reemplaza a la vieja
        if vieja is not None:
            asyncio.create_task(vieja.close(1000, "reemplazada por otra conexion"))
        ip = ws.remote_address[0] if ws.remote_address else "?"
        with self._lock:
            self._ip_pi = ip
            self._cams.clear()
        print(f"[RECEPTOR] Raspberry conectada desde {ip}")
        await self._avisar_pi(ws)
        recibidos = 0                                # bytes de video recibidos (control de flujo)
        try:
            async for msg in ws:
                if self._pi is not ws:
                    break
                if isinstance(msg, bytes):
                    # confirmación: la Pi nunca tiene más video "en el aire" del que la red
                    # (WiFi) alcanza a entregar -> sin retraso acumulado ni cortes
                    recibidos += len(msg)
                    await ws.send(f'{{"tipo":"ack","b":{recibidos}}}')
                    self._recibir_cuadro(msg)
                else:
                    self._recibir_texto(msg)
        except ConnectionClosed:
            pass
        finally:
            if self._pi is ws:
                self._pi = None
                with self._lock:
                    self._cams.clear()
                print("[RECEPTOR] Raspberry desconectada")

    async def _manejar_agente(self, ws):
        vieja, self._agente = self._agente, ws
        if vieja is not None:
            asyncio.create_task(vieja.close(1000, "reemplazada por otra conexion"))
        ip = ws.remote_address[0] if ws.remote_address else "?"
        with self._lock:
            self._agente_ip = ip
        self._sim_informado = None                   # al agente nuevo hay que contárselo todo
        print(f"[RECEPTOR] Agente de la Raspberry conectado desde {ip}")
        try:
            async for msg in ws:
                if self._agente is not ws:
                    break
                if isinstance(msg, str):
                    self._recibir_agente(msg)
        except ConnectionClosed:
            pass
        finally:
            if self._agente is ws:
                self._agente = None
                with self._lock:
                    self._agente_estado = None
                print("[RECEPTOR] Agente de la Raspberry desconectado")

    def _recibir_agente(self, texto):
        try:
            d = json.loads(texto)
        except (json.JSONDecodeError, TypeError):
            return
        if not isinstance(d, dict):
            return
        tipo = d.get("tipo")
        with self._lock:
            if tipo == "agente":
                self._agente_estado = d
            elif tipo == "log":
                lineas = [str(x)[:500] for x in (d.get("lineas") or [])]
                if d.get("completo"):
                    self._agente_log.clear()
                self._agente_log.extend(lineas)
                self._agente_n_log += len(lineas) + (1 if d.get("completo") else 0)
            elif tipo == "respuesta":
                self._agente_resp.append((time.time(), str(d.get("accion") or ""),
                                          bool(d.get("ok")), str(d.get("msg") or "")))
            elif tipo == "sim" and d.get("accion") in ("iniciar", "detener", "estado"):
                self._pedidos_sim.append(d["accion"])

    async def _tarea_anuncio(self):
        """Cada 3 s: broadcast UDP {"app":"monitor-trafico","puerto":N} en la red local.
        Sale por la interfaz de la IP local (la de la ruta por defecto) para no anunciarse
        en adaptadores virtuales. Errores de red: se ignoran (se reintenta en 3 s)."""
        s, ip_s, t_ip = None, None, 0.0
        try:
            while True:
                try:
                    ahora = time.monotonic()
                    if s is None or ahora - t_ip > 60:             # la IP puede cambiar
                        t_ip = ahora
                        ip = ip_local()
                        if s is None or ip != ip_s:
                            if s is not None:
                                s.close()
                            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                            s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                            s.setblocking(False)
                            if ip != "127.0.0.1":
                                s.bind((ip, 0))
                            ip_s = ip
                    datos = json.dumps({"app": "monitor-trafico", "v": 1, "puerto": self.puerto,
                                        "ip": ip_s}).encode()
                    destinos = {"255.255.255.255"}
                    if ip_s and ip_s.count(".") == 3 and ip_s != "127.0.0.1":
                        destinos.add(ip_s.rsplit(".", 1)[0] + ".255")   # red /24 (lo típico)
                    for dest in destinos:
                        with contextlib.suppress(OSError):
                            s.sendto(datos, (dest, PUERTO_ANUNCIO))
                except OSError:
                    if s is not None:
                        s.close()
                    s = None
                await asyncio.sleep(INTERVALO_ANUNCIO)
        finally:
            if s is not None:
                s.close()

    def _camara(self, c):
        cam = self._cams.get(c)
        if cam is None:
            cam = self._cams[c] = _Camara()
        return cam

    def _guardas(self):
        r = self._retraso_efectivo()
        return max(GUARDA_CUADROS, r + 2.0), max(GUARDA_DETS, r + 3.0)

    def _recibir_cuadro(self, msg):
        if len(msg) <= CABECERA.size:
            return
        tipo, c, seq, t = CABECERA.unpack_from(msg, 0)
        if tipo != TIPO_CUADRO or c >= MAX_CAMARAS or t != t:     # t != t -> NaN
            return
        jpeg = msg[CABECERA.size:]
        ahora = time.monotonic()
        with self._lock:
            self._bytes += len(msg)
            self._n_cuadros += 1
            if not self._video:
                return
            cam = self._camara(c)
            if cam.t_pi is not None and t < cam.t_pi - 10:        # la Pi reinició su reloj
                self._cams[c] = cam = _Camara()
            if not cam.t_cuadros or t >= cam.t_cuadros[-1]:
                cam.cuadros.append((seq, t, jpeg))
                cam.t_cuadros.append(t)
            else:                                               # llegó desordenado
                k = bisect.bisect_right(cam.t_cuadros, t)
                cam.cuadros.insert(k, (seq, t, jpeg))
                cam.t_cuadros.insert(k, t)
            cam.local_cuadro = ahora
            cam.marcar(t, ahora)
            cam.podar(*self._guardas())

    def _recibir_det(self, d):
        try:
            c, seq, t = int(d["c"]), int(d["s"]), float(d["t"])
        except (KeyError, TypeError, ValueError):
            return
        if not 0 <= c < MAX_CAMARAS or t != t:
            return
        cajas = _preparar_cajas(d.get("b"))
        por_id = {cj[5]: cj for cj in cajas if cj[5] is not None}
        ahora = time.monotonic()
        with self._lock:
            if not self._video:
                return
            cam = self._camara(c)
            if cam.t_pi is not None and t < cam.t_pi - 10:
                self._cams[c] = cam = _Camara()
            if cam.t_pi is not None:             # para el retraso automático
                self._demoras.append(max(0.0, cam.t_pi + (ahora - cam.local_llegada) - t))
            if cam.t_det_ant is not None and t > cam.t_det_ant:
                self._intervalos.append(t - cam.t_det_ant)
            cam.t_det_ant = t if cam.t_det_ant is None else max(cam.t_det_ant, t)
            k = bisect.bisect_right(cam.t_dets, t)
            cam.dets.insert(k, (seq, t, cajas, por_id))
            cam.t_dets.insert(k, t)
            cam.marcar(t, ahora)
            cam.podar(*self._guardas())

    def _recibir_texto(self, texto):
        try:
            d = json.loads(texto)
        except (json.JSONDecodeError, TypeError):
            return
        if not isinstance(d, dict):
            return
        tipo = d.get("tipo")
        if tipo == "det":
            self._recibir_det(d)
        elif tipo == "datos":
            self.hist.agregar(d)
            with self._lock:
                self._ultimo = d
                self._t_ultimo = time.time()
        elif tipo == "lote":                         # lo que la Pi guardó sin conexión
            muestras = d.get("muestras") or []
            for m in muestras:
                self.hist.agregar(m)
            print(f"[RECEPTOR] Recibí {len(muestras)} muestras atrasadas")
        elif tipo == "semaforo":
            self._recibir_semaforo(d)
        elif tipo == "hola":
            nombres = [str(c)[:40] for c in d.get("camaras") or []][:MAX_CAMARAS]
            with self._lock:
                self._camaras = nombres
            print(f"[RECEPTOR] Raspberry protocolo v{d.get('version', '?')}, cámaras: {nombres}")

    async def _tarea_estadisticas(self):
        """Cada 1 s calcula kbps y cuadros/s de video recibidos (todas las cámaras)."""
        t_ant = time.monotonic()
        while True:
            await asyncio.sleep(1.0)
            ahora = time.monotonic()
            dt = max(ahora - t_ant, 1e-3)
            t_ant = ahora
            with self._lock:
                self._kbps = round(self._bytes * 8 / 1000 / dt, 1)
                self._cps = round(self._n_cuadros / dt, 1)
                self._bytes = self._n_cuadros = 0
                # retraso automático (mismo criterio que el dashboard web): 1.5 x la mediana del
                # intervalo entre análisis de una cámara, y siempre más que lo que tarda en
                # llegar un det + un intervalo. Cambia suave.
                if len(self._intervalos) >= 3:
                    mi = sorted(self._intervalos)[len(self._intervalos) // 2]
                    md = sorted(self._demoras)[len(self._demoras) // 2] if self._demoras else 0.0
                    obj = min(RETRASO_MAX_AUTO, max(0.4, 1.5 * mi, mi + md + 0.15))
                else:
                    obj = 0.0
                self._retraso_auto += (obj - self._retraso_auto) * 0.3

    async def _tarea_guardado(self):
        """Cada 20 s escribe los minutos cerrados (en otro hilo); una vez al día purga."""
        t_purga = 0.0
        while True:
            await asyncio.sleep(20)
            try:
                filas = self.hist.sacar_terminados()
                if filas:
                    await asyncio.to_thread(self.hist.escribir, filas)
                if time.time() - t_purga > 86400:
                    t_purga = time.time()
                    await asyncio.to_thread(self.hist.purgar)
            except Exception as e:
                print(f"[HISTORIAL] Error al guardar: {e}")


# =============================================================================
# SIMULADOR (camfalsa4.py como subproceso)
# =============================================================================
class Simulador:
    """Controla camfalsa4.py (../camfalsa4.py) como subproceso: las 4 cámaras falsas."""

    def __init__(self, ruta_script=None):
        self.ruta_script = os.path.abspath(ruta_script or os.path.join(CARPETA, "..", "camfalsa4.py"))
        self._proc = None
        self._lineas = collections.deque(maxlen=300)
        self._lock = threading.Lock()
        self._puerto = 8080
        self._n = 0
        self._listo = False          # camfalsa4 ya abrió todos sus puertos
        self.error = None            # por qué terminó (si terminó con error)

    def iniciar(self, videos, puerto=8080, ancho=640, fps=10, calidad=75):
        """Lanza camfalsa4.py con 1-4 videos (cámaras en puerto, puerto+1, ...)."""
        self.detener()
        videos = [str(v) for v in videos]
        if not videos:
            raise ValueError("Hace falta al menos un video")
        if not os.path.isfile(self.ruta_script):
            raise FileNotFoundError(f"No encuentro {self.ruta_script}")
        cmd = [sys.executable, "-u", self.ruta_script, *videos, "--puerto", str(int(puerto)),
               "--ancho", str(int(ancho)), "--fps", str(fps), "--calidad", str(int(calidad))]
        entorno = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
        extra = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
        self._puerto, self._n = int(puerto), len(videos)
        self._listo, self.error = False, None
        self._agregar_log(f"[SIMULADOR] Iniciando {len(videos)} cámara(s) desde el puerto {puerto}")
        self._proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                      stdin=subprocess.DEVNULL, text=True, encoding="utf-8",
                                      errors="replace", bufsize=1, env=entorno,
                                      cwd=os.path.dirname(self.ruta_script), **extra)
        threading.Thread(target=self._leer, args=(self._proc,), name="SimuladorLog",
                         daemon=True).start()

    def _agregar_log(self, linea):
        with self._lock:
            self._lineas.append(linea)

    def _leer(self, proc):
        for linea in proc.stdout:
            linea = linea.rstrip("\r\n")
            self._agregar_log(linea)
            if proc is self._proc:
                if "Ctrl+C para detener" in linea:         # camfalsa4 lo imprime al estar listo
                    self._listo = True
                elif "[ERROR]" in linea or "Error" in linea:
                    self.error = linea.strip()
        codigo = proc.wait()
        if codigo and proc is self._proc and not self.error:
            self.error = f"camfalsa4.py terminó con código {codigo}"
        self._agregar_log(f"[SIMULADOR] Terminó (código {codigo})")

    def detener(self):
        """Termina SOLO el subproceso de camfalsa4.py que lanzó este objeto."""
        p, self._proc = self._proc, None
        if p is None or p.poll() is not None:
            return
        p.terminate()
        try:
            p.wait(3)
        except subprocess.TimeoutExpired:
            p.kill()
            with contextlib.suppress(subprocess.TimeoutExpired):
                p.wait(3)

    @property
    def activo(self):
        return self._proc is not None and self._proc.poll() is None

    @property
    def listo(self):
        """True cuando las cámaras ya aceptan conexiones (no solo cuando el proceso existe)."""
        return self.activo and self._listo

    def urls(self):
        """["IP_LAPTOP:8080", ...] para poner en CAMARAS de la Pi ([] si no se ha iniciado)."""
        ip = ip_local()
        return [f"{ip}:{self._puerto + k}" for k in range(self._n)]

    def log(self):
        with self._lock:
            return list(self._lineas)
