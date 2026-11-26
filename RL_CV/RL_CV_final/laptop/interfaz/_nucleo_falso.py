# =============================================================================
# _nucleo_falso.py  -  Núcleo FALSO para probar la ventana (app.py) sin Raspberry.
#
# Implementa la MISMA API que nucleo.py (ver API_NUCLEO.md) con datos sintéticos:
#   - cuadros JPEG reales tomados de prueba_1..4.mp4 (o imágenes generadas si no hay videos),
#   - cajas de vehículos que cruzan la imagen con IDs y categorías,
#   - mensajes 'datos' que cambian cada segundo (la temperatura a veces pasa de 80 °C),
#   - historial con huecos (horas en que la "Pi" estuvo apagada),
#   - un Simulador que no lanza nada, solo finge.
#
# Uso: python app.py --falso
# =============================================================================
import collections
import csv
import math
import os
import random
import socket
import threading
import time

try:
    import cv2
    import numpy as np
except Exception:          # sin OpenCV se generan cuadros con Qt no; simplemente no habrá video
    cv2 = None
    np = None

CATS = ["carro", "moto", "bus", "pesado", "especial"]
COLORES = {"carro": "#28aaeb", "moto": "#fac828", "bus": "#d25ac8", "pesado": "#ff821e", "especial": "#eb3c46"}

_AQUI = os.path.dirname(os.path.abspath(__file__))
_RAIZ = os.path.dirname(_AQUI)
_NOMBRES = ["Cam1", "Cam2", "Cam3", "Cam4"]
_PROPORCION = {"carro": 0.62, "moto": 0.2, "bus": 0.07, "pesado": 0.09, "especial": 0.02}
_INICIO_HIST = time.time() - 5 * 86400     # el historial "empieza" hace 5 días


def ip_local() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def _buscar_videos():
    rutas = []
    for i in range(1, 5):
        for carpeta in (os.path.join(_RAIZ, "videos"), _RAIZ, os.path.dirname(_RAIZ)):
            r = os.path.join(carpeta, f"prueba_{i}.mp4")
            if os.path.exists(r):
                rutas.append(r)
                break
    while rutas and len(rutas) < 4:
        rutas.append(rutas[len(rutas) % 2])
    return rutas


def _elegir_cat(rng):
    x, acum = rng.random(), 0.0
    for c in CATS:
        acum += _PROPORCION[c]
        if x <= acum:
            return c
    return "carro"


class Receptor:
    """Receptor falso: misma interfaz que nucleo.Receptor."""

    def __init__(self, puerto: int = 8000, token: str = "cambia-esto", db_ruta: str | None = None,
                 retraso: float = 1.0, fps_video: int = 10):
        self.puerto, self.token, self.retraso, self.fps_video = puerto, token, retraso, fps_video
        self._video = True
        self._parar = threading.Event()
        self._lock = threading.Lock()
        self._cuadros = {}           # cam -> (seq, jpeg)
        self._bytes = []             # (t, n_bytes) para kbps
        self._hilos = []
        self._t0 = None
        # agente falso: obedece órdenes y escribe un registro creíble
        self._ag = {"conteo": "corriendo", "detalle": "Contando vehículos", "desde": time.time() - 3600,
                    "reinicios": 0, "camaras": None, "laptop": f"{ip_local()}:{puerto}",
                    "laptop_conectada": True, "nube_conectada": True, "sim": None}
        self._ag_log = ["[AGENTE] (agente falso) Iniciando conteo · cámaras: las de CAMARAS (conteo4.py)",
                        "[MODELO] NCNN usará 3 hilos"]
        self._ag_resp = []
        self._ag_pedidos = []

    # ------------------------------------------------------------------ control
    def iniciar(self) -> None:
        self._t0 = time.time()
        self._parar.clear()
        videos = _buscar_videos() if cv2 is not None else []
        for cam in range(4):
            ruta = videos[cam] if cam < len(videos) else None
            h = threading.Thread(target=self._hilo_video, args=(cam, ruta), daemon=True)
            h.start()
            self._hilos.append(h)

    def detener(self) -> None:
        self._parar.set()
        for h in self._hilos:
            h.join(timeout=2)
        self._hilos = []

    def set_video(self, activo: bool) -> None:
        self._video = bool(activo)

    def set_retraso(self, segundos: float) -> None:
        self.retraso = float(segundos)

    # ------------------------------------------------------------------ agente (falso)
    def agente(self, con_log=False):
        ahora = time.time()
        if self._ag["conteo"] == "corriendo" and int(ahora) % 10 == 0:
            linea = f"[ESTADO] YOLO 7.{int(ahora) % 9} inf/s | total {int(ahora) % 13} | CPU 66°C"
            if not self._ag_log or self._ag_log[-1] != linea:
                self._ag_log.append(linea)
        est = dict(self._ag, tipo="agente", v=1, t=ahora, deseado="corriendo",
                   pid=4242 if self._ag["conteo"] == "corriendo" else None,
                   sistema={"host": "raspberrypi", "temp": round(66 + 5 * math.sin(ahora / 30), 1),
                            "ram_libre_mb": 2870, "ram_total_mb": 3793, "carga": [2.9, 3.1, 3.0],
                            "disco_libre_gb": 41.2, "encendida_s": int(ahora - _INICIO_HIST) % 86400,
                            "throttled": "0x0"})
        d = {"conectado": True, "ip": "192.168.1.57", "estado": est, "n_log": len(self._ag_log),
             "respuestas": list(self._ag_resp)}
        if con_log:
            d["log"] = list(self._ag_log[-600:])
        return d

    def orden_agente(self, accion, **extra):
        msg = {"iniciar": "Conteo iniciado", "detener": "Conteo detenido", "reiniciar": "Conteo reiniciado",
               "reiniciar_pi": "(falso) no reinicio nada", "apagar_pi": "(falso) no apago nada"}.get(accion, "OK")
        if accion in ("iniciar", "reiniciar"):
            self._ag.update(conteo="corriendo", detalle="Contando vehículos", desde=time.time())
            if "camaras" in extra:
                self._ag["camaras"] = extra["camaras"]
        elif accion == "detener":
            self._ag.update(conteo="detenido", detalle="Detenido por orden remota", desde=None)
        self._ag_log.append(f"[AGENTE] {accion}: {msg}")
        self._ag_resp.append((time.time(), accion, True, msg))
        return True

    def pedidos_sim(self):
        r, self._ag_pedidos = self._ag_pedidos, []
        return r

    def informar_sim(self, activo, listo, urls, error=None, forzar=False):
        self._ag["sim"] = {"activo": bool(activo), "listo": bool(listo), "urls": list(urls or []),
                           "error": error, "t": time.time()}

    # ------------------------------------------------------------------ simulación
    def _conectada(self):
        return self._t0 is not None and time.time() - self._t0 > 1.5

    def _cam_sin_senal(self, cam):
        # la Cam4 pierde la señal 6 s de cada 45 s, para probar el aviso "sin señal"
        return cam == 3 and (time.time() % 45) > 39

    def _hilo_video(self, cam, ruta):
        if cv2 is None:
            return
        cap = cv2.VideoCapture(ruta) if ruta else None
        seq, periodo = 0, 1.0 / max(1, self.fps_video)
        t_sig = time.time()
        while not self._parar.is_set():
            ok, img = (cap.read() if cap is not None else (False, None))
            if cap is not None and not ok:
                cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                ok, img = cap.read()
            if not ok:
                img = np.zeros((360, 640, 3), np.uint8)
                img[:] = (40 + cam * 30, 50, 60)
                cv2.putText(img, f"Camara sintetica {cam + 1}", (40, 180), cv2.FONT_HERSHEY_SIMPLEX, 1.2,
                            (230, 230, 230), 2)
            else:
                # saltar cuadros para no ir más rápido que el video original (~30 fps -> 10 fps)
                for _ in range(2):
                    cap.grab()
            h, w = img.shape[:2]
            if w != 640:
                img = cv2.resize(img, (640, int(h * 640 / w)))
            ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 72])
            if ok and self._video and self._conectada() and not self._cam_sin_senal(cam):
                seq += 1
                datos = buf.tobytes()
                with self._lock:
                    self._cuadros[cam] = (seq, datos)
                    self._bytes.append((time.time(), len(datos)))
            elif self._cam_sin_senal(cam):
                with self._lock:
                    self._cuadros.pop(cam, None)
            t_sig += periodo
            espera = t_sig - time.time()
            if espera > 0:
                self._parar.wait(espera)
            else:
                t_sig = time.time()
        if cap is not None:
            cap.release()

    def _vehiculos(self, cam, ahora):
        """Vehículos visibles ahora en la cámara (sin estado: depende solo del tiempo)."""
        res = []
        intervalo = 1.8 + cam * 0.35
        slot_act = int(ahora / intervalo)
        for slot in range(slot_act - 12, slot_act + 1):
            rng = random.Random(cam * 1_000_003 + slot)
            if rng.random() < 0.25:
                continue
            t_ini = slot * intervalo + rng.random() * intervalo
            cat = _elegir_cat(rng)
            vel = rng.uniform(0.07, 0.16)                       # fracción del ancho por segundo
            carril = rng.choice([0.42, 0.55, 0.68, 0.8])
            tam = {"carro": 0.13, "moto": 0.06, "bus": 0.22, "pesado": 0.2, "especial": 0.14}[cat]
            prog = (ahora - t_ini) * vel
            if prog < 0 or prog > 1 + tam:
                continue
            izq_a_der = (slot % 2 == 0)
            x1 = prog - tam if izq_a_der else 1 - prog
            ancho, alto = tam, tam * (0.75 if cat != "moto" else 1.3) * 1.4
            y1 = carril - alto / 2 + 0.03 * math.sin(prog * 6)
            caja = [max(0.0, x1), max(0.0, y1), min(1.0, x1 + ancho), min(1.0, y1 + alto)]
            if caja[2] - caja[0] < 0.02:
                continue
            ident = slot * 4 + cam
            num = (slot % 97) + 1 if prog > 0.12 else 0          # provisional al entrar
            conf = 55 + (slot * 7) % 44
            res.append((caja, cat, ident, num, conf))
        return res

    # ------------------------------------------------------------------ lecturas
    def estado(self) -> dict:
        ahora = time.time()
        con = self._conectada()
        with self._lock:
            self._bytes = [b for b in self._bytes if ahora - b[0] < 2.0]
            kbps = sum(n for _, n in self._bytes) * 8 / 1000 / 2.0
            cps = len(self._bytes) / 2.0
        ultimo = None
        if con:
            camaras, total = [], 0
            for i, nom in enumerate(_NOMBRES):
                sin = self._cam_sin_senal(i)
                por = {c: 0 for c in CATS}
                if not sin:
                    for _, cat, _, _, _ in self._vehiculos(i, ahora):
                        por[cat] += 1
                n = sum(por.values())
                total += n
                camaras.append({"nombre": nom, "conectada": not sin, "total": n,
                                "por_categoria": {c: v for c, v in por.items() if v},
                                "nuevos": {}, "vistos": n,
                                "fps": 0.0 if sin else round(1.1 + 0.3 * math.sin(ahora / 7 + i), 2)})
            ultimo = {"tipo": "datos", "t": ahora, "fps": round(4.6 + 0.5 * math.sin(ahora / 11), 2),
                      "temp": round(72 + 10 * math.sin(ahora / 25), 1), "total": total, "camaras": camaras}
        return {"pi_conectada": con, "ip_pi": "192.168.1.57" if con else None,
                "camaras": list(_NOMBRES) if con else [], "t_ultimo": ahora if con else 0.0,
                "ultimo": ultimo, "video_kbps": kbps if self._video else 0.0,
                "cuadros_por_s": cps if self._video else 0.0}

    def cuadro(self, cam: int):
        with self._lock:
            c = self._cuadros.get(cam)
        if c is None:
            return None
        seq, jpeg = c
        cajas = []
        for (x1, y1, x2, y2), cat, ident, num, conf in self._vehiculos(cam, time.time() - self.retraso):
            texto = f"id:{num} {cat} {conf / 100:.2f}" if num else f"{cat} {conf / 100:.2f}"
            cajas.append({"x1": x1, "y1": y1, "x2": x2, "y2": y2, "cat": cat, "id": ident,
                          "num": num or None, "conf": conf / 100, "texto": texto})
        return seq, jpeg, cajas

    def _valor(self, cam, t):
        """Vehículos por minuto en el instante t (patrón diario + ruido determinista)."""
        hora = time.localtime(t).tm_hour + time.localtime(t).tm_min / 60
        base = 4 + 9 * math.exp(-((hora - 7.5) ** 2) / 3) + 11 * math.exp(-((hora - 17.5) ** 2) / 4)
        base *= [1.0, 0.75, 0.55, 0.35][cam]
        rng = random.Random(int(t // 60) * 13 + cam)
        return max(0.0, base * rng.uniform(0.7, 1.3))

    def _apagada(self, cam, t):
        if t < _INICIO_HIST:
            return True
        h = int(t // 3600)
        if h % 9 == 4:                                  # la Pi estuvo apagada 1 h de cada 9
            return True
        if cam == 3 and int(t // 1200) % 5 == 2:        # la Cam4 falla a ratos
            return True
        return False

    def historial(self, horas: float, paso: int) -> dict:
        ahora = time.time()
        fin = int(ahora // paso) * paso
        n = max(1, int(math.ceil(horas * 3600 / paso)))
        ts = [fin - (n - 1 - k) * paso for k in range(n)]
        camaras, cats_tot = {}, {c: 0 for c in CATS}
        for i, nom in enumerate(_NOMBRES):
            flujo, ocup, maxi = [], [], []
            por = {c: 0 for c in CATS}
            acum = {c: 0.0 for c in CATS}
            for t in ts:
                minutos = [t + m * 60 for m in range(paso // 60)
                           if t + m * 60 <= ahora and not self._apagada(i, t + m * 60)]
                if not minutos:
                    flujo.append(None), ocup.append(None), maxi.append(None)
                    continue
                f = int(round(sum(self._valor(i, m) for m in minutos)))
                o = round(sum(self._valor(i, m) for m in minutos) / len(minutos) * 0.35, 2)
                flujo.append(f), ocup.append(o), maxi.append(int(o * 2 + 1))
                resto = f
                for c in CATS[1:]:              # reparte con acumulador para no perder fracciones
                    acum[c] += f * _PROPORCION[c]
                    v = min(resto, int(acum[c]))
                    acum[c] -= v
                    por[c] += v
                    resto -= v
                por["carro"] += resto
            for c in CATS:
                cats_tot[c] += por[c]
            camaras[nom] = {"flujo": flujo, "ocupacion": ocup, "maximo": maxi, "por_categoria": por,
                            "flujo_total": sum(v for v in flujo if v)}
        time.sleep(0.05)                                 # simula la consulta a SQLite
        return {"desde": ts[0], "hasta": fin + paso, "paso": paso, "ts": ts, "camaras": camaras,
                "categorias": cats_tot, "flujo_total": sum(c["flujo_total"] for c in camaras.values())}

    def exportar_csv(self, ruta: str, horas: float) -> int:
        h = self.historial(horas, 60)
        filas = 0
        with open(ruta, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["hora_local", "camara", "flujo", "ocupacion", "maximo"])
            for nom, c in h["camaras"].items():
                for k, t in enumerate(h["ts"]):
                    if c["flujo"][k] is None:
                        continue
                    w.writerow([time.strftime("%Y-%m-%d %H:%M", time.localtime(t)), nom,
                                c["flujo"][k], c["ocupacion"][k], c["maximo"][k]])
                    filas += 1
        return filas


class Simulador:
    """Simulador falso: finge lanzar camfalsa4.py."""

    def __init__(self, ruta_script: str | None = None):
        self.error = None
        self._activo = False
        self._puerto = 8080
        self._n = 0
        self._log = []

    def iniciar(self, videos: list[str], puerto: int = 8080, ancho: int = 640,
                fps: float = 10, calidad: int = 75) -> None:
        self._activo, self._puerto, self._n = True, puerto, len(videos)
        ip = ip_local()
        self._log = [f"(simulador falso) ancho={ancho} fps={fps} calidad={calidad}"]
        for i, v in enumerate(videos):
            self._log.append(f"Cámara {i + 1}: {os.path.basename(v)} -> http://{ip}:{puerto + i}/video")
        self._log.append("[prueba_1.mp4] cliente conectado: 192.168.1.57")

    def detener(self) -> None:
        if self._activo:
            self._log.append("Simulador detenido.")
        self._activo = False

    @property
    def activo(self) -> bool:
        return self._activo

    @property
    def listo(self) -> bool:
        return self._activo

    def urls(self) -> list[str]:
        if not self._activo:
            return []
        ip = ip_local()
        return [f"{ip}:{self._puerto + i}" for i in range(self._n)]

    def log(self) -> list[str]:
        return list(self._log[-200:])


# ============================================================================ semáforo RL (falso)
# Para probar la pestaña "Semáforo" sin Raspberry (python app.py --falso). Imita a
# principal_pi.py: 6 fases, transición de 5 s (3 ámbar + 2 despeje), decisión al cumplirse el
# verde mínimo y luego cada 10 s, y de vez en cuando un rato en ciclo fijo (respaldo).
_FASES_SEM = [{"id": 1, "nombre": "Norte + Sur", "verde": ["N", "S"], "verde_min_s": 30, "verde_max_s": None},
              {"id": 2, "nombre": "Este + Oeste", "verde": ["E", "O"], "verde_min_s": 20, "verde_max_s": None},
              {"id": 3, "nombre": "Solo Norte", "verde": ["N"], "verde_min_s": 10, "verde_max_s": 15},
              {"id": 4, "nombre": "Solo Sur", "verde": ["S"], "verde_min_s": 10, "verde_max_s": 15},
              {"id": 5, "nombre": "Solo Este", "verde": ["E"], "verde_min_s": 15, "verde_max_s": 45},
              {"id": 6, "nombre": "Solo Oeste", "verde": ["O"], "verde_min_s": 15, "verde_max_s": 45}]
_CALLES_SEM = ["N", "S", "E", "O"]


class _SemaforoFalso:
    def __init__(self):
        self.rng = random.Random(7)
        self.t = 0
        self.fase, self.previa = 0, set()
        self.etapa, self.t_etapa, self.t_fase = "transicion", 0, 0
        self.t_dec = 5 + _FASES_SEM[0]["verde_min_s"]
        self.inicio_rojo = {c: 0 for c in _CALLES_SEM}
        self.control, self.dec = "agente", None
        self.hist, self.decs = collections.deque(maxlen=1800), collections.deque(maxlen=60)
        self.t0 = time.time() - 900                      # 15 min de historia ya hechos
        self.cfg = {"fases": _FASES_SEM, "calles": _CALLES_SEM, "ambar_s": 3, "transicion_s": 5, "rojo_max_s": 90}

    def _luces(self):
        nuevas = set(_FASES_SEM[self.fase]["verde"])
        out = {}
        for c in _CALLES_SEM:
            if self.etapa == "verde":
                out[c] = "V" if c in nuevas else "R"
            elif c in self.previa and c in nuevas:
                out[c] = "V"
            elif c in self.previa:
                out[c] = "A" if self.t_etapa < 3 else "R"
            else:
                out[c] = "R"
        return out

    def _conteos(self):
        h = self.t0 + self.t
        return [max(0, round(8 + 6 * math.sin(h / 90 + k * 1.7) + self.rng.random() * 2)) for k in range(4)]

    def _paso(self):
        hora = self.t0 + self.t
        ciclo = int(hora) % 600                         # 10 min: 40 s de respaldo y 30 s recuperando
        self.control = "respaldo" if 360 <= ciclo < 400 else ("recuperando" if 400 <= ciclo < 430 else "agente")
        if self.etapa == "verde" and self.t == self.t_dec:
            f = _FASES_SEM[self.fase]
            masc = [True] * 6
            if f["verde_max_s"] and self.t_fase >= f["verde_max_s"]:
                masc[self.fase] = False
            if self.control == "respaldo":
                pedida = 1 if self.fase == 0 else 0
            elif max((self.t - v) for v in self.inicio_rojo.values() if v is not None) >= 70 if any(
                    v is not None for v in self.inicio_rojo.values()) else False:
                pedida = 1 if self.fase == 0 else 0      # como el escudo: nadie pasa de 90 s en rojo
            elif self.t_fase < 30 + self.rng.randint(0, 20) and masc[self.fase]:
                pedida = self.fase
            else:
                pedida = self.rng.choice([1, 1, 4, 5]) if self.fase == 0 else self.rng.choice([0, 0, 0, 2, 3])
            q = [round(self.rng.uniform(-0.6, 0.6), 4) for _ in range(6)]
            q[pedida] = round(max(q) + self.rng.uniform(0.05, 0.4), 4)
            anulada = not masc[pedida]
            ejecutada = (0 if self.fase != 0 else 1) if anulada else pedida
            cambio = ejecutada != self.fase
            paso = 5 + _FASES_SEM[ejecutada]["verde_min_s"] if cambio else 10
            self.dec = {"t": self.t, "quien": self.control, "pedida": pedida + 1, "ejecutada": ejecutada + 1,
                        "anulada": anulada, "paso_s": paso, "cambio": cambio, "q": q, "mascara": masc}
            self.decs.append(dict(self.dec, hora=hora))
            if cambio:
                self.previa = set(_FASES_SEM[self.fase]["verde"])
                self.fase, self.etapa, self.t_etapa, self.t_fase = ejecutada, "transicion", 0, 0
            self.t_dec = self.t + paso
        luces = self._luces()
        for c in _CALLES_SEM:
            if luces[c] == "R":
                if self.inicio_rojo[c] is None:
                    self.inicio_rojo[c] = self.t
            else:
                self.inicio_rojo[c] = None
        self.hist.append((hora, self.fase + 1, self.control, self.etapa))
        estado = {"t": self.t, "control": self.control, "fase": self.fase + 1, "etapa": self.etapa,
                  "t_fase": self.t_fase, "prox_decision_s": max(0, self.t_dec - self.t),
                  "rojo": [0 if self.inicio_rojo[c] is None else self.t - self.inicio_rojo[c] for c in _CALLES_SEM],
                  "conteos": self._conteos(), "edad_conteos_s": 2, "decision": self.dec,
                  "atraso_max_s": 0.0, "config": self.cfg}
        # avanzar 1 s
        self.t += 1
        if self.etapa == "transicion":
            self.t_etapa += 1
            if self.t_etapa >= 5:
                self.etapa, self.t_fase = "verde", 0
        else:
            self.t_fase += 1
        return luces, estado

    def estado(self):
        ahora = time.time()
        luces = estado = None
        while self.t0 + self.t <= ahora:
            luces, estado = self._paso()
        if estado is None:
            luces, estado = self._ultimo
        self._ultimo = (luces, estado)
        datos = {"tipo": "semaforo", "n": self.t, "t": ahora, "edad": 0.2, "conectado": True,
                 "enviando": self.control != "respaldo",
                 "motivo": None if self.control != "respaldo" else "sin análisis reciente de Cam2 (calle S)",
                 "calles": {"N": "Cam1", "S": "Cam2", "E": "Cam3", "O": "Cam4"},
                 "luces": luces, "estado": estado}
        return {"datos": datos, "edad": 0.2, "pi_conectada": True,
                "historia": list(self.hist), "decisiones": list(self.decs)}


def _semaforo_falso(self):
    if not hasattr(self, "_sem_falso"):
        self._sem_falso = _SemaforoFalso()
    return self._sem_falso.estado()


Receptor.semaforo = _semaforo_falso
