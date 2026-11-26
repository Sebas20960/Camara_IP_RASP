# =============================================================================
# servidor.py  -  Servidor en la nube (AWS) para conteo4
#
# - Recibe de la Raspberry, por UNA conexión WebSocket (/ws/pi), protocolo v2
#   (ver PROTOCOLO_v2.md):
#     * datos cada 1 s (texto JSON: conteo por cámara y categoría, vehículos nuevos, °C...)
#     * SOLO mientras alguien mira: 'det' (cajas de cada análisis de YOLO, texto) y los
#       cuadros JPEG originales de cada cámara (binario con cabecera de 14 bytes)
# - Reparte todo en vivo a los navegadores (/ws/web) y sirve el dashboard (/). Los 'det' y
#   los cuadros se reenvían TAL CUAL (sin abrirlos) y solo a quien tiene el video activo;
#   el navegador arma el video e interpola las cajas.
# - Guarda el historial por minuto en SQLite (historial.db) para las gráficas y el CSV.
# - Control remoto: el agente de la Raspberry (raspberry/agente.py) se conecta a /ws/agente.
#   Desde el dashboard, quien escriba la CLAVE DE CONTROL puede iniciar/detener el conteo,
#   encender las cámaras simuladas de la laptop (el agente reenvía la orden), reiniciar o
#   apagar la Pi y ver su registro. Sin la clave el dashboard solo muestra el estado.
#
# Uso en tu PC (prueba):   python servidor.py            -> http://localhost:8000
# Uso en AWS:              bash instalar_aws.sh          (ver LEEME.md)
#
# Variables de entorno (opcionales):
#   PI_TOKEN        clave que debe mandar la Raspberry (por defecto "cambia-esto")
#   PUERTO          puerto HTTP (por defecto 8000)
#   DB_RUTA         archivo SQLite (por defecto historial.db junto a este script)
#   DIAS_HISTORIAL  días que se guarda el historial (por defecto 90)
#   FPS_VIDEO_WEB   cuadros/s por cámara que se le piden a la Raspberry (por defecto 4;
#                   la Pi usa el menor entre esto y su propio tope)
#   CONTROL_CLAVE   clave para manejar la Pi desde el dashboard (instalar_aws.sh la genera;
#                   vacía = control desactivado)
#
# Requisitos: pip install -r requirements.txt   (fastapi, uvicorn[standard])
# =============================================================================
import asyncio
import contextlib
import csv
import io
import json
import os
import secrets
import sqlite3
import time
from collections import deque
from datetime import datetime, timedelta, timezone

import uvicorn
from fastapi import FastAPI, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

CARPETA = os.path.dirname(os.path.abspath(__file__))
PI_TOKEN = os.environ.get("PI_TOKEN", "cambia-esto")
PUERTO = int(os.environ.get("PUERTO", "8000"))
DB_RUTA = os.environ.get("DB_RUTA", os.path.join(CARPETA, "historial.db"))
DIAS_HISTORIAL = int(os.environ.get("DIAS_HISTORIAL", "90"))
FPS_VIDEO_WEB = float(os.environ.get("FPS_VIDEO_WEB", "4"))
CONTROL_CLAVE = os.environ.get("CONTROL_CLAVE", "cambia-esto")
MAX_FALLOS_CLAVE = 5           # intentos de clave fallidos antes de cortar la conexión
ACCIONES = {"iniciar", "iniciar_todo", "detener", "reiniciar", "camaras_iniciar",
            "camaras_detener", "reiniciar_pi", "apagar_pi", "log"}
MAX_FRAME = 2_000_000          # bytes máx. de un cuadro (un JPEG 640x360 pesa ~25 KB)
MAX_DETS_PENDIENTES = 32       # 'det' en espera por navegador lento (se botan los más viejos)
PREFIJO_DET = '{"tipo":"det"'  # la Pi manda JSON compacto: se reconoce sin decodificarlo

CATS = ["carro", "moto", "bus", "pesado", "especial"]
PASOS = {60, 300, 900, 3600}   # segundos por punto de la gráfica: 1 min, 5 min, 15 min, 1 h

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


# =============================================================================
# HISTORIAL (SQLite, una fila por cámara y minuto)
# =============================================================================
class Historial:
    """
    Las muestras se acumulan en memoria por (minuto, cámara) y se escriben a SQLite cuando
    el minuto termina. Todo es SUMABLE (muestras, suma, nuevos), así que si la Raspberry
    manda atrasado lo que guardó sin internet, simplemente se suma al minuto que le toca.
    """

    def __init__(self, ruta):
        self.ruta = ruta
        self.buckets = {}
        with contextlib.closing(self._con()) as con:
            con.executescript(ESQUEMA)

    def _con(self):
        con = sqlite3.connect(self.ruta, timeout=10)
        con.execute("PRAGMA journal_mode=WAL")
        return con

    def agregar(self, muestra):
        ahora = time.time()
        t = muestra.get("t")
        if not isinstance(t, (int, float)) or t > ahora + 300 or t < ahora - DIAS_HISTORIAL * 86400:
            t = ahora                     # reloj de la Pi raro: uso la hora del servidor
        m = int(t // 60 * 60)
        for c in muestra.get("camaras") or []:
            nombre = str(c.get("nombre", "?"))[:40]
            b = self.buckets.get((m, nombre))
            if b is None:
                b = self.buckets[(m, nombre)] = {"muestras": 0, "suma": 0, "maximo": 0,
                                                 **dict.fromkeys(CATS, 0)}
            tot = max(int(c.get("total") or 0), 0)
            b["muestras"] += 1
            b["suma"] += tot
            b["maximo"] = max(b["maximo"], tot)
            for cat, v in (c.get("nuevos") or {}).items():
                if cat in b and cat in CATS:
                    b[cat] += max(int(v or 0), 0)

    def sacar_terminados(self, todo=False):
        """Quita de memoria los minutos ya cerrados y los devuelve como filas para SQLite."""
        limite = int(time.time() // 60 * 60)
        filas = []
        for k in [k for k in self.buckets if todo or k[0] < limite]:
            b = self.buckets.pop(k)
            filas.append((k[0], k[1], b["muestras"], b["suma"], b["maximo"], *[b[c] for c in CATS]))
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
# EN VIVO: Raspberry <-> navegadores
# =============================================================================
class Visor:
    """Un navegador abierto. Guarda solo lo ÚLTIMO pendiente: si su internet es lento se
    salta cuadros (se queda con el último de cada cámara) en vez de acumular retraso, y
    nunca frena a los demás."""

    def __init__(self, ws):
        self.ws = ws
        self.quiere_video = True
        self.admin = False                # escribió la clave de control
        self.fallos = 0
        self._admin = []                  # registro/respuestas de la Pi (no se descartan)
        self._texto = []
        self._dets = deque(maxlen=MAX_DETS_PENDIENTES)
        self._frames = {}                 # cámara -> último cuadro pendiente
        self._ev = asyncio.Event()

    def poner_texto(self, t):
        self._texto = (self._texto + [t])[-5:]
        self._ev.set()

    def poner_det(self, t):
        if self.quiere_video:
            self._dets.append(t)
            self._ev.set()

    def poner_frame(self, cam, f):
        if self.quiere_video:
            self._frames[cam] = f
            self._ev.set()

    def pausar_video(self, quiere):
        self.quiere_video = quiere
        if not quiere:
            self._dets.clear()
            self._frames.clear()

    def poner_texto_admin(self, t):
        """Como poner_texto, pero sin descartar (registro y respuestas de la Pi)."""
        self._admin = (self._admin + [t])[-200:]
        self._ev.set()

    async def enviar(self):
        try:
            while True:
                await self._ev.wait()
                self._ev.clear()
                admin, self._admin = self._admin, []
                textos, self._texto = self._texto, []
                dets, self._dets = list(self._dets), deque(maxlen=MAX_DETS_PENDIENTES)
                frames, self._frames = list(self._frames.values()), {}
                for t in admin + textos + dets:
                    await self.ws.send_text(t)
                for f in frames:
                    await self.ws.send_bytes(f)
        except asyncio.CancelledError:
            raise
        except Exception:
            # el navegador se fue a medio envío: cerrar para que ws_web lo saque de la lista
            # (antes esta tarea moría en silencio y se le seguía guardando video)
            with contextlib.suppress(Exception):
                await self.ws.close()


RUTA_CAMARAS = os.path.splitext(DB_RUTA)[0] + "_camaras.json"


class Central:
    def __init__(self):
        self.pi = None
        self.visores = set()
        self.ultimo = None             # último mensaje de datos (dict)
        self.semaforo = None           # último estado del semáforo RL (si la Pi lo manda)
        self.ultimo_frame = {}         # cámara -> último cuadro (para quien recién abre)
        self.t_ultimo = 0.0
        self.agente = None             # conexión del agente de la Pi (/ws/agente)
        self.agente_estado = None
        self.agente_log = deque(maxlen=400)
        # orden de las cámaras de la Pi (se recuerda aunque el servidor se reinicie, para
        # que cada cámara conserve su color en el dashboard)
        try:
            with open(RUTA_CAMARAS, encoding="utf-8") as f:
                self.camaras = [str(c) for c in json.load(f)][:8]
        except (OSError, ValueError):
            self.camaras = []

    def guardar_camaras(self, camaras):
        self.camaras = camaras
        with contextlib.suppress(OSError):
            with open(RUTA_CAMARAS, "w", encoding="utf-8") as f:
                json.dump(camaras, f)

    @property
    def pi_conectada(self):
        return self.pi is not None

    def n_video(self):
        return sum(1 for v in self.visores if v.quiere_video)

    async def avisar_pi(self):
        """Le dice a la Raspberry cuántos quieren video (0 = no mandes 'det' ni cuadros) y
        cuántos cuadros/s por cámara queremos."""
        if self.pi is not None:
            with contextlib.suppress(Exception):
                await self.pi.send_text(json.dumps({"tipo": "visores", "video": self.n_video(),
                                                    "total": len(self.visores),
                                                    "fps": FPS_VIDEO_WEB, "ack": True}))

    def a_todos(self, msg):
        t = json.dumps(msg, separators=(",", ":"))
        for v in self.visores:
            v.poner_texto(t)

    def estado_pi(self):
        return {"tipo": "pi", "conectada": self.pi_conectada, "visores": len(self.visores),
                "t_ultimo": self.t_ultimo}

    # ---- agente (control remoto)
    def msg_agente(self, admin):
        """Estado del agente para un navegador. Sin clave se ocultan direcciones internas."""
        if self.agente is None or not self.agente_estado:
            return {"tipo": "agente", "conectado": False, "control": bool(CONTROL_CLAVE)}
        d = dict(self.agente_estado, tipo="agente", conectado=True, control=bool(CONTROL_CLAVE))
        if not admin:
            for k in ("camaras", "laptop", "pid"):
                d.pop(k, None)
            if isinstance(d.get("sim"), dict):
                d["sim"] = {k: v for k, v in d["sim"].items() if k != "urls"}
            if isinstance(d.get("sistema"), dict):
                d["sistema"] = {k: v for k, v in d["sistema"].items() if k != "host"}
        return d

    def avisar_agente(self):
        pub = json.dumps(self.msg_agente(False), separators=(",", ":"))
        adm = json.dumps(self.msg_agente(True), separators=(",", ":"))
        for v in self.visores:
            v.poner_texto(adm if v.admin else pub)

    def a_admins(self, msg):
        t = json.dumps(msg, separators=(",", ":"))
        for v in self.visores:
            if v.admin:
                v.poner_texto_admin(t)


hist = Historial(DB_RUTA)
central = Central()


async def _tarea_guardado():
    """Cada 20 s escribe los minutos cerrados; una vez al día borra lo más viejo."""
    t_purga = 0.0
    while True:
        await asyncio.sleep(20)
        try:
            filas = hist.sacar_terminados()
            if filas:
                await asyncio.to_thread(hist.escribir, filas)
            if time.time() - t_purga > 86400:
                t_purga = time.time()
                await asyncio.to_thread(hist.purgar)
        except Exception as e:
            print(f"[HISTORIAL] Error al guardar: {e}")


@contextlib.asynccontextmanager
async def _ciclo_de_vida(app):
    tarea = asyncio.create_task(_tarea_guardado())
    if PI_TOKEN == "cambia-esto":
        print("[AVISO] PI_TOKEN no definido: uso 'cambia-esto'. En AWS define uno propio "
              "(instalar_aws.sh lo hace solo).")
    if CONTROL_CLAVE == "cambia-esto":
        print("[AVISO] CONTROL_CLAVE no definida: la clave de control es 'cambia-esto'.")
    elif not CONTROL_CLAVE:
        print("[AVISO] CONTROL_CLAVE vacía: el control remoto desde la web está desactivado.")
    print(f"[SERVIDOR] Historial en {DB_RUTA} ({DIAS_HISTORIAL} días)")
    yield
    tarea.cancel()
    filas = hist.sacar_terminados(todo=True)        # no perder el minuto en curso
    if filas:
        hist.escribir(filas)


app = FastAPI(title="conteo4 - nube", lifespan=_ciclo_de_vida)


# ------------------------------ Raspberry -----------------------------------
@app.websocket("/ws/pi")
async def ws_pi(ws: WebSocket):
    token = ws.query_params.get("token") or ""
    if not secrets.compare_digest(token.encode(), PI_TOKEN.encode()):
        await ws.close(code=1008)
        print(f"[PI] Conexión rechazada (token incorrecto) desde {ws.client.host if ws.client else '?'}")
        return
    await ws.accept()
    if central.pi is not None:                       # una sola Raspberry: la nueva reemplaza
        with contextlib.suppress(Exception):
            await central.pi.close(code=1000)
    central.pi = ws
    print(f"[PI] Conectada desde {ws.client.host if ws.client else '?'}")
    central.a_todos(central.estado_pi())
    await central.avisar_pi()
    recibidos = 0                                    # bytes de video recibidos (control de flujo)
    try:
        while True:
            msg = await ws.receive()
            if msg["type"] == "websocket.disconnect":
                break
            if msg.get("bytes") is not None:         # cuadro: 0x01, cámara, seq, t, JPEG
                f = msg["bytes"]
                # confirmación: la Pi no manda más video del que la red alcanza a entregar
                recibidos += len(f)
                await ws.send_text(f'{{"tipo":"ack","b":{recibidos}}}')
                if 14 < len(f) <= MAX_FRAME and f[0] == 1:
                    cam = f[1]
                    central.ultimo_frame[cam] = f
                    for v in central.visores:
                        v.poner_frame(cam, f)
                continue
            texto = msg.get("text") or "{}"
            if texto.startswith(PREFIJO_DET):         # 'det': se reenvía tal cual, sin abrirlo
                for v in central.visores:
                    v.poner_det(texto)
                continue
            try:
                d = json.loads(texto)
            except json.JSONDecodeError:
                continue
            tipo = d.get("tipo")
            if tipo == "datos":
                hist.agregar(d)
                central.ultimo = d
                central.t_ultimo = time.time()
                central.a_todos(d)
            elif tipo == "semaforo":                 # semáforo RL: solo en vivo, no se guarda
                if len(texto) <= 32768:
                    central.semaforo = d
                    central.a_todos(d)
            elif tipo == "lote":                     # lo que guardó mientras no tenía internet
                muestras = d.get("muestras") or []
                for m in muestras:
                    hist.agregar(m)
                print(f"[PI] Recibí {len(muestras)} muestras atrasadas")
            elif tipo == "det":                      # 'det' con otro formato de JSON
                for v in central.visores:
                    v.poner_det(texto)
            elif tipo == "hola":
                central.guardar_camaras([str(c)[:40] for c in d.get("camaras") or []][:8])
                print(f"[PI] Cámaras: {central.camaras} (protocolo v{d.get('version', 1)})")
    except WebSocketDisconnect:
        pass
    except Exception as e:                           # conexión rota a medio envío, etc.
        print(f"[PI] Conexión terminada: {type(e).__name__}: {e}")
    finally:
        if central.pi is ws:
            central.pi = None
            central.ultimo_frame = {}
            central.semaforo = None
            print("[PI] Desconectada")
            central.a_todos(central.estado_pi())


# ------------------------------ Agente de la Raspberry -----------------------
@app.websocket("/ws/agente")
async def ws_agente(ws: WebSocket):
    token = ws.query_params.get("token") or ""
    if not secrets.compare_digest(token.encode(), PI_TOKEN.encode()):
        await ws.close(code=1008)
        print(f"[AGENTE] Conexión rechazada (token incorrecto) desde {ws.client.host if ws.client else '?'}")
        return
    await ws.accept()
    if central.agente is not None:
        with contextlib.suppress(Exception):
            await central.agente.close(code=1000)
    central.agente = ws
    print(f"[AGENTE] Conectado desde {ws.client.host if ws.client else '?'}")
    try:
        while True:
            texto = await ws.receive_text()
            try:
                d = json.loads(texto)
            except json.JSONDecodeError:
                continue
            if not isinstance(d, dict):
                continue
            tipo = d.get("tipo")
            if tipo == "agente":
                central.agente_estado = d
                central.avisar_agente()
            elif tipo == "log":
                lineas = [str(x)[:500] for x in d.get("lineas") or []]
                if d.get("completo"):
                    central.agente_log.clear()
                central.agente_log.extend(lineas)
                central.a_admins({"tipo": "log", "lineas": lineas, "completo": bool(d.get("completo"))})
            elif tipo == "respuesta":
                central.a_admins({"tipo": "respuesta", "accion": str(d.get("accion") or ""),
                                  "ok": bool(d.get("ok")), "msg": str(d.get("msg") or "")[:500]})
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        if central.agente is ws:
            central.agente = None
            central.agente_estado = None
            print("[AGENTE] Desconectado")
            central.avisar_agente()


async def _control_web(v, d):
    """Mensajes de control de un navegador: {"clave": ...} y {"orden": {"accion": ...}}."""
    if "clave" in d:
        clave = str(d.get("clave") or "")
        if CONTROL_CLAVE and secrets.compare_digest(clave.encode(), CONTROL_CLAVE.encode()):
            v.admin, v.fallos = True, 0
            v.poner_texto_admin(json.dumps({"tipo": "control", "ok": True, "msg": "Control desbloqueado"}))
            v.poner_texto(json.dumps(central.msg_agente(True)))
            v.poner_texto_admin(json.dumps({"tipo": "log", "lineas": list(central.agente_log), "completo": True}))
        else:
            v.fallos += 1
            await asyncio.sleep(1.0)                  # frena a quien intente adivinar
            v.poner_texto_admin(json.dumps({"tipo": "control", "ok": False, "msg": "Clave incorrecta"
                                            if CONTROL_CLAVE else "El control está desactivado en el servidor"}))
            if v.fallos >= MAX_FALLOS_CLAVE:
                await asyncio.sleep(0.2)
                await v.ws.close(code=1008)
    elif "orden" in d:
        o = d.get("orden") if isinstance(d.get("orden"), dict) else {}
        accion = str(o.get("accion") or "")
        if not v.admin:
            msg = {"tipo": "control", "ok": False, "msg": "Primero escribe la clave de control"}
        elif accion not in ACCIONES:
            msg = {"tipo": "control", "ok": False, "msg": f"Orden desconocida: {accion}"}
        elif central.agente is None:
            msg = {"tipo": "control", "ok": False,
                   "msg": "El agente de la Raspberry no está conectado (¿Pi apagada o sin internet?)"}
        else:
            orden = {"tipo": "orden", "accion": accion}
            if accion == "iniciar" and "camaras" in o:
                cams = o.get("camaras")
                orden["camaras"] = [str(c)[:200] for c in cams][:4] if isinstance(cams, list) else None
            try:
                await central.agente.send_text(json.dumps(orden))
                msg = {"tipo": "control", "ok": True, "msg": "Orden enviada a la Raspberry…"}
            except Exception:
                msg = {"tipo": "control", "ok": False, "msg": "No se pudo enviar la orden"}
        v.poner_texto_admin(json.dumps(msg))


# ------------------------------ Navegadores ---------------------------------
@app.websocket("/ws/web")
async def ws_web(ws: WebSocket):
    await ws.accept()
    v = Visor(ws)
    central.visores.add(v)
    await central.avisar_pi()
    v.poner_texto(json.dumps(central.estado_pi()))
    v.poner_texto(json.dumps(central.msg_agente(False)))
    if central.ultimo is not None:
        v.poner_texto(json.dumps(central.ultimo))
    if central.semaforo is not None:
        v.poner_texto(json.dumps(central.semaforo))
    for cam, f in list(central.ultimo_frame.items()):
        v.poner_frame(cam, f)
    envio = asyncio.create_task(v.enviar())
    try:
        while True:
            msg = await ws.receive()                  # pausar video, clave, órdenes
            if msg["type"] == "websocket.disconnect":
                break
            try:
                d = json.loads(msg.get("text") or "")
            except (json.JSONDecodeError, TypeError):
                continue
            if not isinstance(d, dict):
                continue
            if "video" in d:
                v.pausar_video(bool(d["video"]))
                await central.avisar_pi()
            if "clave" in d or "orden" in d:
                await _control_web(v, d)
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        envio.cancel()
        central.visores.discard(v)
        await central.avisar_pi()
        central.a_todos(central.estado_pi())


# ------------------------------ HTTP ----------------------------------------
@app.get("/")
async def inicio():
    return FileResponse(os.path.join(CARPETA, "static", "index.html"),
                        headers={"Cache-Control": "no-cache"})


@app.get("/api/estado")
async def api_estado():
    return {"pi_conectada": central.pi_conectada, "visores": len(central.visores),
            "semaforo": central.semaforo,
            "camaras": central.camaras, "ultimo": central.ultimo, "agente": central.msg_agente(False)}


@app.get("/api/historial")
async def api_historial(horas: float = Query(1, gt=0, le=24 * 90), paso: int = Query(60)):
    if paso not in PASOS:
        return JSONResponse({"error": f"paso debe ser uno de {sorted(PASOS)}"}, status_code=400)
    if horas * 3600 / paso > 5000:
        return JSONResponse({"error": "demasiados puntos: usa un paso mayor"}, status_code=400)
    en_memoria = {k: dict(b) for k, b in hist.buckets.items()}
    return await asyncio.to_thread(hist.consultar, horas, paso, en_memoria, list(central.camaras))


@app.get("/api/exportar.csv")
async def api_csv(horas: float = Query(24, gt=0, le=24 * 90),
                  tz: int = Query(0, ge=-900, le=900)):
    """CSV por minuto. tz = minutos que hay que SUMAR a UTC para la hora local (Perú = -300)."""
    hasta = int(time.time() // 60 * 60 + 60)
    en_memoria = {k: dict(b) for k, b in hist.buckets.items()}
    filas = await asyncio.to_thread(hist.filas, hasta - int(horas * 3600), hasta, en_memoria)
    filas.sort(key=lambda f: (f[0], f[1]))
    zona = timezone(timedelta(minutes=tz))
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["fecha_local", "camara", "vehiculos_nuevos", *[f"nuevos_{c}" for c in CATS],
                "en_imagen_promedio", "en_imagen_maximo", "muestras"])
    for t, cam, muestras, suma, maximo, *nuevos in filas:
        w.writerow([datetime.fromtimestamp(t, zona).strftime("%Y-%m-%d %H:%M"), cam, sum(nuevos),
                    *nuevos, round(suma / muestras, 2) if muestras else "", maximo, muestras])
    nombre = f"trafico_{datetime.now(zona).strftime('%Y%m%d_%H%M')}.csv"
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv",
                             headers={"Content-Disposition": f'attachment; filename="{nombre}"'})


def _ip_local():
    """IP de esta máquina en la red local (la que debe usar la Raspberry en modo local)."""
    import socket
    try:
        with contextlib.closing(socket.socket(socket.AF_INET, socket.SOCK_DGRAM)) as s:
            s.connect(("8.8.8.8", 80))           # no envía nada: solo elige la interfaz de red
            return s.getsockname()[0]
    except OSError:
        return "IP_DE_ESTA_PC"


if __name__ == "__main__":
    print("=" * 66)
    print(f" Dashboard:        http://localhost:{PUERTO}")
    print(f" En la Raspberry:  python conteo4_nube.py --nube {_ip_local()}:{PUERTO} --token {PI_TOKEN}")
    print("=" * 66)
    uvicorn.run(app, host="0.0.0.0", port=PUERTO, ws_max_size=4 * 1024 * 1024,
                ws_ping_interval=20, ws_ping_timeout=30,
                proxy_headers=True, forwarded_allow_ips="*", log_level="warning")
