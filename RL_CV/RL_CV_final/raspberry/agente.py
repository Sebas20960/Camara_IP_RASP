# =============================================================================
# agente.py  -  "Mando a distancia" de la Raspberry (corre SIEMPRE, como servicio).
#
# Es un programa chiquito (~20 MB de RAM, casi 0 % de CPU, NO carga YOLO) que:
#   - Se conecta (como cliente, igual que conteo4_nube) al programa de la laptop y al
#     servidor de AWS por la ruta /ws/agente, así que funciona detrás del router sin abrir
#     puertos. Desde la ventana de la laptop o desde el dashboard web se le puede ordenar:
#         iniciar / detener / reiniciar el conteo (conteo4_nube.py)
#         "iniciar todo" (pide a la laptop que encienda las cámaras simuladas y luego cuenta)
#         encender / apagar las cámaras simuladas de la laptop (reenvía la orden)
#         reiniciar o apagar la Raspberry
#   - Lanza conteo4_nube.py como subproceso, lee su salida (el registro se ve en la laptop
#     y en la web) y lo REVIVE si se cae (espera 5 s, 10 s, 20 s... hasta 60 s).
#   - Encuentra solo la IP de la laptop: el programa de la laptop la anuncia por la red
#     local (UDP 37020) cada 3 s. Si la IP de la laptop cambia, se reconecta solo.
#   - Recuerda si el conteo estaba corriendo (agente_estado.json): tras un corte de luz
#     vuelve a dejarlo como estaba.
#   - Informa el estado de la Pi: temperatura, RAM libre, carga, disco y si la Pi se está
#     frenando por calor o mala fuente (vcgencmd get_throttled).
#
# La configuración (URLs y tokens) es la MISMA de conteo4_nube.py: no hay que repetirla.
#
# Uso (en la Raspberry, en la carpeta raspberry/):
#     python agente.py                  (lo normal es instalarlo como servicio: instalar_pi.sh)
#     python agente.py --sin-autoinicio (no arranca el conteo solo al empezar)
#     python agente.py --nube "" --laptop localhost   (mismas opciones que conteo4_nube.py:
#                                        --nube, --token, --laptop, --token-laptop; se pasan
#                                        también al conteo que lance)
# Requisito: pip install websockets
# =============================================================================
import collections
import json
import os
import queue
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time

AQUI = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, AQUI)
import conteo4_nube as cfg      # noqa: E402  (solo constantes y url_ws: no carga YOLO)

# ---- CONFIGURACIÓN -------------------------------------------------------------
AUTO_INICIAR = True          # la 1.ª vez (sin agente_estado.json) arranca el conteo solo
DESCUBRIR_LAPTOP = True      # escuchar el anuncio de la laptop (IP automática)
PUERTO_ANUNCIO = 37020       # UDP; el mismo que usa nucleo.py en la laptop
INTERVALO_ESTADO = 5.0       # s entre mensajes de estado a la laptop/AWS
ESPERA_CAMARAS = 15.0        # s que espera a que la laptop encienda las cámaras ("iniciar todo")
TIEMPO_DETENER = 12.0        # s para que conteo4 cierre limpio antes de matarlo
PERMITIR_APAGAR = True       # aceptar "reiniciar_pi" / "apagar_pi" (necesita sudo sin clave)
# -----------------------------------------------------------------------------

SCRIPT_CONTEO = os.path.join(AQUI, "conteo4_nube.py")
RUTA_ESTADO = os.path.join(AQUI, "agente_estado.json")
LINEAS_LOG = 400
ES_WINDOWS = os.name == "nt"


def _log(txt):
    print(f"[AGENTE] {txt}", flush=True)


def _host_puerto(url_ws):
    """ws://1.2.3.4:8000/ws/pi -> '1.2.3.4:8000' (para comparar y para --laptop)."""
    if not url_ws:
        return ""
    e = url_ws.split("://", 1)[-1]
    return e.split("/", 1)[0]


def _url_agente(url_ws_pi):
    """ws://host/ws/pi -> ws://host/ws/agente"""
    if not url_ws_pi:
        return ""
    base = url_ws_pi.split("?", 1)[0]
    if base.endswith("/ws/pi"):
        return base[:-len("/ws/pi")] + "/ws/agente"
    return base.rstrip("/") + "/ws/agente"


# =============================================================================
# SISTEMA (sin dependencias: /proc y /sys de Linux)
# =============================================================================
_throttled = {"t": 0.0, "v": None}


def _leer(ruta):
    try:
        with open(ruta, encoding="utf-8") as f:
            return f.read()
    except OSError:
        return None


def info_sistema():
    d = {"host": socket.gethostname(), "temp": None, "ram_libre_mb": None, "ram_total_mb": None,
         "carga": None, "disco_libre_gb": None, "encendida_s": None, "throttled": None}
    t = _leer("/sys/class/thermal/thermal_zone0/temp")
    if t:
        try:
            d["temp"] = round(int(t.strip()) / 1000.0, 1)
        except ValueError:
            pass
    mem = _leer("/proc/meminfo")
    if mem:
        kv = {}
        for linea in mem.splitlines():
            partes = linea.split()
            if len(partes) >= 2 and partes[1].isdigit():
                kv[partes[0].rstrip(":")] = int(partes[1])
        if "MemTotal" in kv:
            d["ram_total_mb"] = kv["MemTotal"] // 1024
            d["ram_libre_mb"] = kv.get("MemAvailable", kv.get("MemFree", 0)) // 1024
    carga = _leer("/proc/loadavg")
    if carga:
        try:
            d["carga"] = [float(x) for x in carga.split()[:3]]
        except ValueError:
            pass
    up = _leer("/proc/uptime")
    if up:
        try:
            d["encendida_s"] = int(float(up.split()[0]))
        except ValueError:
            pass
    try:
        d["disco_libre_gb"] = round(shutil.disk_usage(AQUI).free / 1e9, 1)
    except OSError:
        pass
    # vcgencmd cada 30 s: 0x0 = todo bien; bit 0 = bajo voltaje ahora, bit 2 = frenada
    # ahora, bit 3 = límite de temperatura ahora; bits 16-19 = "pasó desde que encendió"
    ahora = time.time()
    if ahora - _throttled["t"] > 30 and shutil.which("vcgencmd"):
        _throttled["t"] = ahora
        try:
            r = subprocess.run(["vcgencmd", "get_throttled"], capture_output=True, text=True, timeout=3)
            _throttled["v"] = r.stdout.strip().split("=", 1)[-1] or None
        except (OSError, subprocess.SubprocessError):
            _throttled["v"] = None
    d["throttled"] = _throttled["v"]
    return d


def otros_conteos(excepto=None):
    """PIDs de otros conteo4_nube.py / conteo4.py corriendo (p. ej. el servicio viejo)."""
    if ES_WINDOWS or not shutil.which("pgrep"):
        return []
    try:
        r = subprocess.run(["pgrep", "-f", r"conteo4(_nube)?\.py"], capture_output=True,
                           text=True, timeout=3)
    except (OSError, subprocess.SubprocessError):
        return []
    pids = []
    for p in r.stdout.split():
        if p.isdigit() and int(p) not in (os.getpid(), excepto):
            pids.append(int(p))
    return pids


# =============================================================================
# DESCUBRIMIENTO DE LA LAPTOP (anuncio UDP)
# =============================================================================
class Descubridor:
    """Escucha el anuncio {"app":"monitor-trafico","puerto":8000} de la laptop.
    La dirección elegida es 'pegajosa': solo cambia si aparece OTRA dirección fresca y la
    actual lleva más de 15 s sin anunciarse (evita saltos con laptops de varias redes)."""

    def __init__(self):
        self.actual = None             # "ip:puerto"
        self._visto = {}               # "ip:puerto" -> último time.monotonic()
        self._lock = threading.Lock()
        threading.Thread(target=self._correr, daemon=True, name="descubridor").start()

    def _correr(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("", PUERTO_ANUNCIO))
        except OSError as e:
            _log(f"No pude escuchar el anuncio de la laptop (UDP {PUERTO_ANUNCIO}): {e}")
            return
        s.settimeout(2.0)
        while True:
            try:
                datos, (ip, _) = s.recvfrom(2048)
            except socket.timeout:
                continue
            except OSError:
                time.sleep(1)
                continue
            try:
                d = json.loads(datos.decode("utf-8"))
                if d.get("app") != "monitor-trafico":
                    continue
                puerto = int(d.get("puerto") or 8000)
            except (ValueError, AttributeError, UnicodeDecodeError, TypeError):
                continue
            dir_ = f"{ip}:{puerto}"
            ahora = time.monotonic()
            with self._lock:
                self._visto[dir_] = ahora
                if self.actual is None:
                    self.actual = dir_
                    _log(f"Laptop encontrada en la red: {dir_}")
                elif dir_ != self.actual and ahora - self._visto.get(self.actual, 0) > 15:
                    _log(f"La laptop cambió de dirección: {self.actual} -> {dir_}")
                    self.actual = dir_

    def direccion(self):
        with self._lock:
            return self.actual


# =============================================================================
# CONEXIÓN CON UN CONTROLADOR (laptop o AWS)
# =============================================================================
class Controlador:
    """Un hilo: conexión WebSocket a /ws/agente, reconexión, cola de salida propia
    (solo este hilo escribe en el socket). Las órdenes que llegan van a la cola del agente."""

    def __init__(self, nombre, agente, obtener_url, token):
        self.nombre = nombre
        self.agente = agente
        self.obtener_url = obtener_url        # función: url actual (puede cambiar) o ""
        self.token = token
        self.conectado = False
        self.salida = queue.Queue(maxsize=500)
        self._url_activa = ""
        threading.Thread(target=self._correr, daemon=True, name=f"ctl-{nombre}").start()

    def enviar(self, msg):
        if not self.conectado:
            return
        try:
            self.salida.put_nowait(json.dumps(msg, separators=(",", ":")))
        except queue.Full:
            pass

    def _correr(self):
        try:
            from websockets.sync.client import connect
        except ImportError:
            _log("Falta la librería: pip install websockets")
            return
        espera, avisado = 2.0, ""
        while True:
            url = self.obtener_url()
            if not url:
                time.sleep(2)
                continue
            completa = f"{url}{'&' if '?' in url else '?'}token={self.token}"
            try:
                with connect(completa, open_timeout=8, close_timeout=2, compression=None,
                             max_size=2 ** 20, ping_interval=20, ping_timeout=20) as ws:
                    self._url_activa = url
                    self.conectado = True
                    espera, avisado = 2.0, ""
                    _log(f"[{self.nombre}] Conectado a {url}")
                    while not self.salida.empty():          # descartar lo viejo
                        self.salida.get_nowait()
                    self.agente.al_conectar(self)
                    self._sesion(ws, url)
            except Exception as e:
                motivo = f"{type(e).__name__}: {e}"
                if motivo != avisado:
                    _log(f"[{self.nombre}] Sin conexión con {url} ({motivo}). Reintento...")
                    avisado = motivo
            finally:
                if self.conectado:
                    self.conectado = False
                    self.agente.al_desconectar(self)
            time.sleep(espera)
            espera = min(espera * 2, 30.0)

    def _sesion(self, ws, url):
        while True:
            if self.obtener_url() != url:               # la laptop cambió de IP
                _log(f"[{self.nombre}] Cambio de dirección: me reconecto")
                return
            try:
                msg = ws.recv(timeout=0.25)
            except TimeoutError:
                msg = None
            if isinstance(msg, str):
                try:
                    d = json.loads(msg)
                except json.JSONDecodeError:
                    d = None
                if isinstance(d, dict):
                    self.agente.ordenes.put((self, d))
            while True:
                try:
                    ws.send(self.salida.get_nowait())
                except queue.Empty:
                    break


# =============================================================================
# AGENTE
# =============================================================================
class Agente:
    def __init__(self, autoinicio=True, nube=None, token=None, laptop=None, token_laptop=None):
        self.ordenes = queue.Queue()
        self.log = collections.deque(maxlen=LINEAS_LOG)
        self._nuevas = collections.deque(maxlen=LINEAS_LOG)  # líneas aún no enviadas
        self._lock_log = threading.Lock()
        self.proc = None
        self.estado = "detenido"       # detenido | arrancando | corriendo | deteniendo | error
        self.detalle = ""
        self.desde = None
        self.reinicios = 0
        self._laptop_proc = None       # --laptop con el que se lanzó el proceso actual
        self._t_lanzado = 0.0
        self._espera_revivir = 5.0
        self._t_revivir = None
        self._pendiente = None         # ("iniciar_todo", t_límite)
        self.sim = None                # último sim_estado que mandó la laptop
        self._cambio = True
        self._t_estado = 0.0

        guardado = self._cargar()
        self.deseado = guardado.get("deseado", "corriendo" if (AUTO_INICIAR and autoinicio) else "detenido")
        if not autoinicio:
            self.deseado = "detenido"
        self.camaras = guardado.get("camaras")          # None = CAMARAS de conteo4.py

        # opciones de la consola (o las de conteo4_nube.py); se le pasan también al conteo
        self.laptop_cfg = cfg.url_ws(cfg.LAPTOP_URL if laptop is None else laptop, puerto_defecto=8000)
        self.nube = cfg.url_ws(cfg.NUBE_URL if nube is None else nube)
        self.token_nube = cfg.NUBE_TOKEN if token is None else token
        self.token_laptop = cfg.LAPTOP_TOKEN if token_laptop is None else token_laptop
        self._args_conteo = ["--nube", self.nube or "", "--token", self.token_nube,
                             "--token-laptop", self.token_laptop]
        self.desc = Descubridor() if (DESCUBRIR_LAPTOP and self.laptop_cfg) else None
        self.laptop = Controlador("LAPTOP", self, self._url_laptop_agente, self.token_laptop) \
            if self.laptop_cfg else None
        self.aws = Controlador("NUBE", self, lambda: _url_agente(self.nube), self.token_nube) \
            if self.nube else None
        self.controladores = [c for c in (self.laptop, self.aws) if c]

    # ---------------------------------------------------------------- persistencia
    @staticmethod
    def _cargar():
        try:
            with open(RUTA_ESTADO, encoding="utf-8") as f:
                d = json.load(f)
            return d if isinstance(d, dict) else {}
        except (OSError, ValueError):
            return {}

    def _guardar(self):
        try:
            tmp = RUTA_ESTADO + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"deseado": self.deseado, "camaras": self.camaras}, f)
            os.replace(tmp, RUTA_ESTADO)
        except OSError as e:
            _log(f"No pude guardar {RUTA_ESTADO}: {e}")

    # ---------------------------------------------------------------- direcciones
    def laptop_hp(self):
        """'ip:puerto' de la laptop a usar ahora (descubierta o la de la configuración)."""
        if not self.laptop_cfg:
            return ""
        d = self.desc.direccion() if self.desc else None
        return d or _host_puerto(self.laptop_cfg)

    def _url_laptop_agente(self):
        hp = self.laptop_hp()
        return f"ws://{hp}/ws/agente" if hp else ""

    # ---------------------------------------------------------------- mensajes
    def mensaje_estado(self):
        return {"tipo": "agente", "v": 1, "t": time.time(), "conteo": self.estado,
                "deseado": self.deseado, "detalle": self.detalle,
                "pid": self.proc.pid if self.proc and self.proc.poll() is None else None,
                "desde": self.desde, "reinicios": self.reinicios, "camaras": self.camaras,
                "laptop": self.laptop_hp() or None,
                "laptop_conectada": bool(self.laptop and self.laptop.conectado),
                "nube_conectada": bool(self.aws and self.aws.conectado),
                "sim": self.sim, "sistema": info_sistema()}

    def a_todos(self, msg):
        for c in self.controladores:
            c.enviar(msg)

    def anotar(self, linea):
        with self._lock_log:
            self.log.append(linea)
            self._nuevas.append(linea)

    def lineas_log(self, n=LINEAS_LOG):
        with self._lock_log:
            return list(self.log)[-n:]

    def al_conectar(self, ctl):
        ctl.enviar(self.mensaje_estado())
        ctl.enviar({"tipo": "log", "lineas": self.lineas_log(200), "completo": True})
        if ctl is self.laptop:
            ctl.enviar({"tipo": "sim", "accion": "estado"})      # que la laptop diga cómo está
        self._cambio = True

    def al_desconectar(self, ctl):
        if ctl is self.laptop:
            self.sim = None
        self._cambio = True

    def responder(self, ctl, accion, ok, msg):
        _log(f"{accion}: {msg}")
        for c in self.controladores:              # el otro controlador también se entera
            c.enviar({"tipo": "respuesta", "accion": accion, "ok": ok,
                      "msg": msg if c is ctl else f"(desde {ctl.nombre.lower()}) {msg}"})

    def _poner(self, estado, detalle=None):
        if estado != self.estado or (detalle is not None and detalle != self.detalle):
            self.estado = estado
            if detalle is not None:
                self.detalle = detalle
            self._cambio = True

    # ---------------------------------------------------------------- proceso
    def _leer_salida(self, proc):
        for linea in proc.stdout:
            linea = linea.rstrip("\r\n")
            if not linea:
                continue
            print(linea, flush=True)                    # también al journal del servicio
            self.anotar(linea)
            if self.proc is proc and self.estado == "arrancando" and (
                    "[ESTADO]" in linea or "Corriendo SIN pantalla" in linea):
                self._poner("corriendo", "Contando vehículos")

    def lanzar(self):
        if self.proc is not None and self.proc.poll() is None:
            return True
        otros = otros_conteos()
        if otros:
            self._poner("error", f"Ya hay otro conteo corriendo (PID {', '.join(map(str, otros))}). "
                                 f"Si es el servicio viejo: sudo systemctl disable --now conteo4-nube")
            return False
        if not os.path.isfile(SCRIPT_CONTEO):
            self._poner("error", f"No encuentro {SCRIPT_CONTEO}")
            return False
        cmd = [sys.executable, "-u", SCRIPT_CONTEO, "--sin-pantalla", *self._args_conteo]
        hp = self.laptop_hp()
        cmd += ["--laptop", hp]                          # "" = sin laptop
        cmd += list(self.camaras or [])
        entorno = dict(os.environ, PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8")
        extra = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if ES_WINDOWS else {}
        txt_cams = ", ".join(self.camaras) if self.camaras else "las de CAMARAS (conteo4.py)"
        self.anotar(f"[AGENTE] Iniciando conteo · cámaras: {txt_cams}")
        try:
            self.proc = subprocess.Popen(cmd, cwd=AQUI, env=entorno, stdout=subprocess.PIPE,
                                         stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                         text=True, encoding="utf-8", errors="replace",
                                         bufsize=1, **extra)
        except OSError as e:
            self._poner("error", f"No pude lanzar conteo4_nube.py: {e}")
            return False
        self._laptop_proc = hp
        self._t_lanzado = time.monotonic()
        self.desde = time.time()
        threading.Thread(target=self._leer_salida, args=(self.proc,), daemon=True,
                         name="salida-conteo").start()
        self._poner("arrancando", "Cargando el modelo YOLO y conectando las cámaras…")
        _log(f"Conteo lanzado (PID {self.proc.pid})")
        return True

    def parar(self):
        p = self.proc
        if p is None or p.poll() is not None:
            self.proc = None
            return
        self._poner("deteniendo", "Cerrando el conteo…")
        self._enviar_estado(forzar=True)
        try:
            if ES_WINDOWS:
                p.send_signal(signal.CTRL_BREAK_EVENT)
            else:
                p.send_signal(signal.SIGINT)
            p.wait(TIEMPO_DETENER)
        except subprocess.TimeoutExpired:
            _log("El conteo no cerró a tiempo: lo fuerzo")
            p.kill()
            try:
                p.wait(5)
            except subprocess.TimeoutExpired:
                pass
        except OSError:
            pass
        self.proc = None
        self.desde = None

    def _revisar_proceso(self):
        p = self.proc
        if p is not None and p.poll() is not None:            # terminó solo
            codigo = p.returncode
            self.proc = None
            self.desde = None
            vivo = time.monotonic() - self._t_lanzado
            # motivo: la última línea [ERROR] o el final de un traceback (no cualquier "WinError")
            lineas = self.lineas_log(60)
            ultima = next((l for l in reversed(lineas) if l.startswith("[ERROR]")), "")
            if not ultima and any(l.startswith("Traceback") for l in lineas):
                ultima = lineas[-1]
            ultima = ultima[:200]
            if self.deseado == "corriendo":
                if vivo > 120:
                    self._espera_revivir = 5.0
                self._t_revivir = time.monotonic() + self._espera_revivir
                self._poner("error", f"El conteo terminó (código {codigo}). "
                                     f"Lo revivo en {self._espera_revivir:.0f} s. {ultima}".strip())
                self._espera_revivir = min(self._espera_revivir * 2, 60.0)
            else:
                self._poner("detenido", f"Terminó (código {codigo})")
        if (self.deseado == "corriendo" and self.proc is None and self._pendiente is None
                and (self._t_revivir is None or time.monotonic() >= self._t_revivir)):
            if self._t_revivir is not None:
                self.reinicios += 1
            self._t_revivir = None
            if not self.lanzar():
                self._t_revivir = time.monotonic() + 30
        # la laptop cambió de IP: el conteo debe mandarle a la nueva dirección
        if (self.proc is not None and self.proc.poll() is None and self._laptop_proc
                and self.laptop_hp() != self._laptop_proc):
            self.anotar(f"[AGENTE] La laptop ahora está en {self.laptop_hp()}: reinicio el conteo")
            self.parar()                                   # _revisar_proceso lo relanza

    # ---------------------------------------------------------------- órdenes
    def _iniciar(self, camaras, reiniciar=False):
        self.deseado = "corriendo"
        cambio_cams = camaras is not ... and camaras != self.camaras
        if camaras is not ...:
            self.camaras = camaras
        self._guardar()
        self._t_revivir = None
        self._espera_revivir = 5.0
        corriendo = self.proc is not None and self.proc.poll() is None
        if corriendo and not (reiniciar or cambio_cams):
            return True, "El conteo ya estaba corriendo"
        if corriendo:
            self.parar()
        return (True, "Conteo reiniciado" if corriendo else "Conteo iniciado") if self.lanzar() \
            else (False, self.detalle)

    def atender(self, ctl, d):
        tipo = d.get("tipo")
        if tipo == "sim_estado" and ctl is self.laptop:
            self.sim = {"activo": bool(d.get("activo")), "listo": bool(d.get("listo")),
                        "urls": [str(u) for u in (d.get("urls") or [])][:4],
                        "error": d.get("error"), "t": time.time()}
            self._cambio = True
            return
        if tipo != "orden":
            return
        accion = str(d.get("accion") or "")
        if accion == "iniciar":
            cams = d["camaras"] if "camaras" in d else ...
            if isinstance(cams, list):
                # sin opciones disfrazadas de cámara ("--nube ..."): solo direcciones
                cams = [str(c).strip() for c in cams
                        if str(c).strip() and not str(c).strip().startswith("-")][:4] or None
            elif cams is not ...:
                cams = None
            self.responder(ctl, accion, *self._iniciar(cams))
        elif accion == "reiniciar":
            self.responder(ctl, accion, *self._iniciar(..., reiniciar=True))
        elif accion == "detener":
            self.deseado = "detenido"
            self._pendiente = None
            self._guardar()
            self.parar()
            self._poner("detenido", "Detenido por orden remota")
            self.responder(ctl, accion, True, "Conteo detenido")
        elif accion == "iniciar_todo":
            if self.laptop and self.laptop.conectado:
                self.laptop.enviar({"tipo": "sim", "accion": "iniciar"})
                self._pendiente = ("iniciar_todo", time.monotonic() + ESPERA_CAMARAS, ctl)
                if self.proc is None:                  # si ya cuenta, el estado no cambia
                    self._poner("arrancando", "Esperando que la laptop encienda las cámaras simuladas…")
                self.responder(ctl, accion, True, "Pidiendo a la laptop que encienda las cámaras…")
            else:
                ok, msg = self._iniciar(...)
                self.responder(ctl, accion, ok, "La laptop no está conectada: inicio el conteo con "
                                                "las últimas cámaras usadas. " + msg)
        elif accion in ("camaras_iniciar", "camaras_detener"):
            if self.laptop and self.laptop.conectado:
                self.laptop.enviar({"tipo": "sim", "accion": accion.split("_")[1]})
                self.responder(ctl, accion, True, "Orden enviada a la laptop")
            else:
                self.responder(ctl, accion, False, "La laptop no está conectada a la Raspberry "
                                                   "(¿programa cerrado o en otra red?)")
        elif accion in ("reiniciar_pi", "apagar_pi"):
            self._apagar(ctl, accion)
        elif accion == "log":
            ctl.enviar({"tipo": "log", "lineas": self.lineas_log(), "completo": True})
        else:
            self.responder(ctl, accion or "?", False, f"Orden desconocida: {accion!r}")

    def _apagar(self, ctl, accion):
        if not PERMITIR_APAGAR or ES_WINDOWS:
            self.responder(ctl, accion, False, "Apagar/reiniciar la Pi está desactivado")
            return
        verbo = "reboot" if accion == "reiniciar_pi" else "poweroff"
        sc = shutil.which("systemctl") or "/usr/bin/systemctl"
        # comprobar ANTES de cerrar el conteo que sudo no pide clave
        try:
            prueba = subprocess.run(["sudo", "-n", "-l", sc, verbo], capture_output=True, timeout=5)
        except (OSError, subprocess.SubprocessError) as e:
            self.responder(ctl, accion, False, f"No pude usar sudo: {e}")
            return
        if prueba.returncode != 0:
            self.responder(ctl, accion, False, "Sin permiso: ejecuta instalar_pi.sh (agrega el permiso "
                                               "para reiniciar/apagar sin clave)")
            return
        self.responder(ctl, accion, True, "Reiniciando la Raspberry…" if verbo == "reboot"
                       else "Apagando la Raspberry… (vuelve a encenderla desenchufándola)")
        self._enviar_estado(forzar=True)
        time.sleep(1.0)                                   # que salgan los mensajes
        self.parar()                                      # deseado se conserva: vuelve tras reiniciar
        subprocess.Popen(["sudo", "-n", sc, verbo])

    def _revisar_pendiente(self):
        if not self._pendiente:
            return
        _, limite, ctl = self._pendiente
        sim = self.sim or {}
        if sim.get("activo") and sim.get("listo") and sim.get("urls"):
            self._pendiente = None
            ok, msg = self._iniciar(list(sim["urls"]))
            self.responder(ctl, "iniciar_todo", ok, "Cámaras de la laptop listas. " + msg)
        elif sim.get("error") and sim.get("t", 0) > time.time() - ESPERA_CAMARAS:
            self._pendiente = None
            self._poner("detenido" if self.proc is None else self.estado,
                        f"La laptop no pudo encender las cámaras: {sim['error']}")
            self.responder(ctl, "iniciar_todo", False, f"La laptop no pudo encender las cámaras: {sim['error']}")
        elif time.monotonic() > limite:
            self._pendiente = None
            ok, msg = self._iniciar(...)
            self.responder(ctl, "iniciar_todo", ok, "La laptop no respondió a tiempo: inicio con las "
                                                    "últimas cámaras usadas. " + msg)

    # ---------------------------------------------------------------- bucle
    def _enviar_estado(self, forzar=False):
        ahora = time.monotonic()
        if forzar or self._cambio or ahora - self._t_estado >= INTERVALO_ESTADO:
            self._cambio = False
            self._t_estado = ahora
            self.a_todos(self.mensaje_estado())

    def correr(self):
        _log(f"Listo. Laptop: {self.laptop_hp() or '(desactivada)'} | AWS: {self.nube or '(desactivado)'} | "
             f"conteo al iniciar: {self.deseado}")
        if self.deseado == "corriendo":
            self.lanzar()
        while True:
            try:
                ctl, d = self.ordenes.get(timeout=0.5)
                try:
                    self.atender(ctl, d)
                except Exception as e:                     # una orden rara no tumba al agente
                    _log(f"Error atendiendo {d!r}: {type(e).__name__}: {e}")
            except queue.Empty:
                pass
            self._revisar_pendiente()
            self._revisar_proceso()
            if self._nuevas:
                with self._lock_log:
                    lineas = list(self._nuevas)
                    self._nuevas.clear()
                self.a_todos({"tipo": "log", "lineas": lineas})
            self._enviar_estado()

    def cerrar(self):
        self.parar()


def main():
    opc = {}
    for nombre in ("--nube", "--token", "--laptop", "--token-laptop"):
        valor = cfg._sacar_opcion(nombre, None)
        if valor is not None:
            opc[nombre[2:].replace("-", "_")] = valor
    agente = Agente(autoinicio="--sin-autoinicio" not in sys.argv, **opc)

    def salir(*_):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, salir)                  # systemctl stop: cierra el conteo limpio
    try:
        agente.correr()
    except KeyboardInterrupt:
        _log("Cerrando…")
    finally:
        agente.cerrar()


if __name__ == "__main__":
    main()
