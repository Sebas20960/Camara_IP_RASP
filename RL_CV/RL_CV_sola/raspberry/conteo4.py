# =============================================================================
# conteo4.py   (V_final_sola - Raspberry Pi 4, 4 GB, modelo de 5 clases)
#
# VERSIÓN SOLA: solo YOLO en la Pi, SIN interfaz de laptop ni AWS. Los resultados se ven en
# la consola ([ESTADO] cada 10 s), en la ventana local opcional (--pantalla) y en el CSV
# por minuto de la carpeta registros/ (ver [CSV] abajo). El RL se engancha en enviar_a_rl().
#
# Cuenta vehículos en 4 CÁMARAS EN VIVO a la vez (1 a 4, en realidad) con UN SOLO modelo
# YOLO NCNN. Se detecta en TODO el cuadro de cada cámara: no hay carriles.
# Cada cámara tiene su propio ByteTrack y su propia numeración por categoría
# ('id:3 carro 0.91' en la cámara Norte no tiene nada que ver con 'id:3 carro' en la Sur).
#
# QUÉ CAMBIÓ RESPECTO A VF3_2 Y POR QUÉ (todo pensado para la Pi 4):
#   [4 CAM]   Un solo modelo cargado (cargar 4 modelos = 4x RAM y los 4 pelean por los
#             mismos 4 núcleos). Un hilo de inferencia atiende a las cámaras por turnos
#             (round-robin): si la Pi hace ~8 inferencias/s, cada cámara recibe ~2/s.
#   [TRACK]   model.track() guarda UN tracker: con 4 cámaras mezclaría los autos de todas.
#             Aquí cada cámara tiene su propio BYTETracker y se usa model.predict().
#   [MJPEG]   Para cámaras http (IP Webcam, simulador) el stream se lee "a mano": se guarda
#             solo el JPEG más reciente SIN decodificarlo, y se decodifica únicamente el
#             frame que YOLO va a analizar (~2/s por cámara en vez de 30/s). Si la imagen es
#             grande se decodifica ya reducida (IMREAD_REDUCED_*), que es casi gratis.
#             Es el ahorro de CPU más grande del programa.
#   [RTSP]    Para rtsp:// (cámaras de seguridad) se usa OpenCV: grab() en cada frame (para
#             no acumular retraso) y retrieve() (conversión a BGR) solo del frame pedido.
#   [PREFETCH] Mientras YOLO analiza la cámara i, la cámara i+1 ya prepara su frame.
#   [CARRILES] Eliminados (y la ROI, la máscara y el detector de bucle del simulador).
#   [VIDEO]   MOSTRAR_VIDEO = True/False (o --pantalla / --sin-pantalla). Sin video no se
#             dibuja NADA (cero costo). Con video: mosaico 2x2 que solo redibuja la cámara
#             que cambió, en tamaño 320 (el mismo frame que analizó YOLO, sin reescalar).
#   [CONTEO]  Un vehículo cuenta en cuanto YOLO lo ve con confianza >= CONF (no espera a
#             que ByteTrack lo confirme). Las detecciones débiles solo cuentan si ByteTrack
#             las une a un vehículo que ya se seguía.
#   [DATOS]   Publicador.suscribir_datos(fn): diccionario con todo (por cámara, por
#             categoría, vistos, FPS, conectada). Es el punto de enganche para mandar los
#             datos a otro programa (aquí lo usa el CSV).
#   [CSV]     GUARDAR_CSV = True (o --sin-csv para apagarlo): un archivo por día en
#             registros/conteo_AAAA-MM-DD.csv con UNA fila por cámara y por minuto
#             (vehículos que pasaron, por categoría, y cuántos había en imagen). Se escribe
#             una vez por minuto desde un hilo aparte: casi nada de CPU ni de tarjeta SD.
#   [Pi 4]    - TRABAJADORES = 3: 3 redes NCNN de 1 hilo en paralelo (ver la constante);
#               deja 1 núcleo para cámaras/ventana. (HILOS_NCNN solo si TRABAJADORES = 1.) Se fija al
#               CARGAR el modelo (cambiarlo después lo hace ~4x más lento).
#             - FPS_MAX_POR_CAMARA: tope opcional de análisis/s (menos CPU y temperatura).
#             - [ESTADO] muestra la temperatura de la CPU y avisa si pasa de 80 °C.
#             - RTSP con timeouts: una cámara colgada se reconecta sola (antes se quedaba
#               trabada para siempre). Acepta pipelines de GStreamer (decodificación H.264
#               por hardware) si tu OpenCV tiene GStreamer (el de apt: python3-opencv).
#             - Sin escritorio (SSH / servicio al arrancar) pasa solo a modo sin video, en
#               vez de que Qt mate el programa. Igual si tienes opencv-python-headless.
#             - Cerrar la ventana con la X termina el programa limpiamente.
#
# Uso:
#     python conteo4.py                                   (usa la lista CAMARAS de abajo)
#     python conteo4.py 192.168.1.33:8080 192.168.1.34:8080 192.168.1.35:8080 192.168.1.36:8080
#     python conteo4.py Norte=192.168.1.33:8080 Sur=rtsp://192.168.1.40:554/stream2
#     python conteo4.py videos/prueba_1.mp4 videos/prueba_2.mp4   (archivos: se leen a velocidad
#                                                          real y en bucle)
#     python conteo4.py --sin-pantalla                    (sin ventana, solo datos)
#     python conteo4.py --pantalla                        (fuerza la ventana)
#     python conteo4.py --sin-csv                         (no guarda el CSV por minuto)
#     python conteo4.py --carpeta-csv /ruta/otra          (guarda el CSV en otra carpeta)
#     python conteo4.py "rtspsrc location=rtsp://IP:554/stream2 latency=200 ! rtph264depay
#                        ! h264parse ! v4l2h264dec ! videoconvert ! appsink drop=1"
#                                                         (GStreamer, decodificación por HW)
#
# Teclas (con ventana):  q / ESC = salir    r = reiniciar IDs de todas las cámaras
#
# Requisitos: pip install ultralytics ncnn opencv-python numpy pyyaml
# Este archivo no necesita otros .py: solo la carpeta modelo_5clases_ncnn_model/ a su lado.
# =============================================================================
import os
import sys

# Linux con Wayland: OpenCV necesita el plugin xcb para abrir ventanas (ANTES de cv2).
if sys.platform.startswith("linux"):
    os.environ.setdefault("QT_QPA_PLATFORM", "xcb")
# rtsp:// por TCP (más estable que UDP en WiFi). También ANTES de importar cv2.
os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", "rtsp_transport;tcp")

import time
import queue
import socket
import threading
import traceback
import urllib.request

import cv2
import numpy as np
import yaml

CARPETA = os.path.dirname(os.path.abspath(__file__))

# =============================================================================
# CONFIGURACIÓN
# =============================================================================
# Cámaras: (nombre, IP/URL/archivo). Se usan si no pasas nada por la consola.
#   "192.168.1.33:8080"            -> IP Webcam / simulador (http://.../video, MJPEG)
#   "http://192.168.1.33:8080/video"
#   "rtsp://usuario:clave@192.168.1.40:554/stream2"   -> cámara de seguridad (usa el
#                                                        SUB-STREAM de baja resolución)
#   "videos/prueba_1.mp4"          -> archivo de video (ruta a un .mp4 que exista)
# Por defecto: las 4 cámaras simuladas por camfalsa4.py en la laptop 192.168.1.33.
# Si la IP de tu laptop cambia, corrígela aquí (o pasa las cámaras por la consola).
CAMARAS = [
    ("Cam1", "10.100.150.60:8080"),
    ("Cam2", "10.100.150.60:8081"),
    ("Cam3", "10.100.150.60:8082"),
    ("Cam4", "10.100.150.60:8083"),
]
MAX_CAMARAS = 4

# Ventana con el video y los recuadros. False = sin ventana (lo más liviano).
# Desde la consola: --pantalla o --sin-pantalla (ganan sobre este valor).
MOSTRAR_VIDEO = True

# Modelo (carpeta NCNN; busca primero junto a este script y luego en la carpeta de arriba)
RUTA_MODELO = "modelo_5clases_ncnn_model"
CONF = 0.4                 # confianza mínima para que un vehículo NUEVO cuente
CONF_SEGUIMIENTO = 0.15    # detecciones entre esto y CONF solo mantienen vivo un ID existente
MAX_DET = 50
IOU_NMS = 0.5
AGNOSTIC_NMS = True        # evita contar doble el mismo vehículo con dos clases parecidas
CALENTAR_MODELO = True

ASIGNAR_ID = True          # False = solo detección (sin ByteTrack, sin "vistos")
FRAMES_PARA_ETIQUETA = 3   # detecciones para fijar la categoría y el número de un vehículo

# ByteTrack (uno por cámara). Cada cámara se analiza a ~2 FPS (8 FPS / 4 cámaras), así
# que entre dos análisis los autos se mueven bastante: se tolera poco solape.
AJUSTES_TRACKER = {
    "track_high_thresh": CONF,
    "track_low_thresh": CONF_SEGUIMIENTO,
    "new_track_thresh": CONF,
    "track_buffer": 8,      # análisis que se recuerda un vehículo oculto (~4 s a 2 FPS/cám)
    "match_thresh": 0.9,    # costo máx. = 1 - IoU  -> basta un IoU de 0.1 para seguir el ID
    "fuse_score": True,
}

# Rendimiento (Raspberry Pi 4)
TRABAJADORES = 3           # [Pi 4] YOLO en paralelo: 3 hilos, cada uno con SU red NCNN de 1 hilo y
                           # un grupo fijo de cámaras. Medido (estrés): una red de 3 hilos se hunde
                           # en cuanto algo más usa la CPU (en cada capa los hilos se esperan entre
                           # sí: 40 -> 3 inf/s) y 3 redes de 1 hilo no (y rinden ~2x más). Deja 1
                           # núcleo para leer cámaras, red y semáforo. 1 = como antes (una red de
                           # HILOS_NCNN hilos). Solo con MOTOR = "ncnn".
HILOS_NCNN = 3             # núcleos para YOLO SOLO si TRABAJADORES = 1 (con más, cada red usa 1).
                           # la ventana y el CSV. Con 4 YOLO va algo más rápido
                           # pero todo lo demás se traba. Prueba 3 vs 4 mirando "inf/s" en
                           # [ESTADO]. Se fija AL CARGAR el modelo (NCNN no deja cambiarlo luego).
MOTOR = "ncnn"             # "ncnn" = YOLO directo con ncnn (motor_ncnn.py): ~1.7-2x más rápido
                           # y mismas detecciones. "ultralytics" = model.predict() (lo de antes).
                           # Si "ncnn" falla al cargar, se usa Ultralytics solo.
ENTRADA_RECTANGULAR = True # (motor "ncnn") cuadro 16:9 -> 320x192 en vez de 320x320: ~40 % menos
                           # cálculo. False = cuadrada, idéntico a Ultralytics.
HILOS_OPENCV = 1           # OpenCV solo decodifica/redimensiona/dibuja frames pequeños
HILOS_TORCH = 1            # torch solo hace el pre/post-proceso (NMS): no necesita más
FPS_MAX_POR_CAMARA = 0     # 0 = sin límite. Ej. 2 = cada cámara se analiza máx. 2 veces/s:
                           # baja el uso de CPU y la temperatura (útil si la Pi no tiene
                           # ventilador). Para contar autos en un semáforo 2/s es suficiente.
TEMP_AVISO = 80.0          # °C: la Pi 4 empieza a bajar su velocidad (throttling) a ~80-85 °C
LADO_TRABAJO = None        # None = el imgsz del modelo (320). Es el tamaño del frame que
                           # analiza YOLO y también el de cada recuadro del mosaico.
TIEMPO_SIN_FRAMES = 5.0    # s sin frames -> se considera desconectada y se reconecta
ESPERA_RECONEXION = 2.0
TIMEOUT_CONEXION = 6.0

# Envío de datos
INTERVALO_RL = 0.5         # s mínimos entre envíos al RL / a suscriptores de datos (y entre
                           # muestras del CSV)
INTERVALO_LOG = 10.0       # s entre líneas de estado en la consola
IMPRIMIR_RL = False        # True = imprime el vector del RL en cada envío (ruidoso)

# [CSV] Registro por minuto (un archivo por día). --sin-csv lo apaga desde la consola.
GUARDAR_CSV = True
CARPETA_CSV = "registros"  # relativa a la carpeta de este script (o ruta absoluta)
SEPARADOR_CSV = ","        # "," estándar. Usa ";" si tu Excel (en español) junta todo en una columna

# [RL] Qué vector recibe el agente
#   "camara"           -> un valor por cámara (total de vehículos visibles en esa cámara)
#   "camara_categoria" -> por cámara, las 5 categorías seguidas (cámaras x 5)
#   "equivalente"      -> por cámara, suma ponderada en "carros equivalentes"
ESTADO_RL = "camara"
PESOS_EQUIVALENTES = {"carro": 1.0, "moto": 0.5, "bus": 2.5, "pesado": 2.5, "especial": 1.0}

# Categorías (orden del conteo y del estado del RL)
ORDEN_CAT = ["carro", "moto", "bus", "pesado", "especial"]
# Solo para el modelo ANTIGUO de 31 clases (con el de 5 clases no se usa).
ALIAS_MODELO_ANTIGUO = {
    "carro":    ['car', 'taxi', 'suv', 'pickup', 'camioneta', 'minivan', 'van'],
    "moto":     ['motorbike', 'scooter', 'bicimoto', 'trimoto', 'mototaxi',
                 'auto rickshaw', 'three wheelers -CNG-'],
    "bus":      ['bus', 'minibus', 'omnibus', 'omnibus-articulado', 'human hauler'],
    "pesado":   ['truck', 'semi-trailer', 'trailer', 'garbagevan'],
    "especial": ['ambulance', 'policecar', 'army vehicle', 'especiales'],
}
CLASE_A_CAT = {c: c for c in ORDEN_CAT}
CLASE_A_CAT.update({cl: cat for cat, cls in ALIAS_MODELO_ANTIGUO.items() for cl in cls})

# Visual
FORMATO_ETIQUETA = "id:{n} {nombre} {conf:.2f}"   # {n} número por categoría, {nombre}, {conf}
FORMATO_PROVISIONAL = "{nombre} {conf:.2f}"
MOSTRAR_ETIQUETAS = True
ESCALA_ETIQUETA = 0.33
ESCALA_PANEL = 0.4
FPS_MAX_PANTALLA = 15      # tope de refresco de la ventana
ESCALA_VENTANA = 1.0       # 1.5 / 2.0 = ventana más grande (cuesta un resize por refresco)
FUENTE = cv2.FONT_HERSHEY_SIMPLEX
AA = cv2.LINE_AA
COLOR_CATEGORIA = {                       # BGR
    "carro":    (235, 170, 40),
    "moto":     (40, 200, 250),
    "bus":      (200, 90, 210),
    "pesado":   (30, 130, 255),
    "especial": (70, 60, 235),
}
COLOR_ACENTO = (90, 220, 120)
ABREV = {"carro": "c", "moto": "m", "bus": "b", "pesado": "p", "especial": "e"}

cv2.setNumThreads(HILOS_OPENCV)


# =============================================================================
# MODELO
# =============================================================================
def _resolver_modelo(ruta):
    """Busca la carpeta NCNN real (la que tiene el .param), aquí o en la carpeta de arriba."""
    for cand in (ruta, os.path.join(CARPETA, ruta), os.path.join(os.path.dirname(CARPETA), ruta)):
        if os.path.exists(cand):
            ruta = cand
            break
    else:
        sys.exit(f"[ERROR] No encuentro el modelo '{ruta}' (busqué aquí y en la carpeta de arriba).")
    if not os.path.isdir(ruta):
        return ruta
    for raiz, _, archivos in os.walk(ruta):
        if any(a.endswith(".param") for a in archivos):
            return raiz
    sys.exit(f"[ERROR] {ruta} no tiene model.ncnn.param: vuelve a exportar/descomprimir el modelo.")


def _imgsz_del_modelo(ruta, por_defecto=320):
    try:
        with open(os.path.join(ruta, "metadata.yaml"), "r", encoding="utf-8") as f:
            v = yaml.safe_load(f).get("imgsz")
        return int(v[0]) if isinstance(v, (list, tuple)) else int(v)
    except Exception:
        return por_defecto


def _fijar_hilos_ncnn(hilos):
    """
    NCNN fija sus hilos al CARGAR el modelo; cambiarlos después no funciona (y en la
    práctica lo vuelve 4 veces más lento). Ultralytics crea la red con ncnn.Net(), así que
    se envuelve esa función para que cada red nazca ya con el número de hilos elegido.
    """
    if not hilos:
        return
    try:
        import ncnn  # pyright: ignore[reportMissingImports]
    except ImportError:
        return                                   # Ultralytics lo instalará y usará su valor
    if getattr(ncnn.Net, "_conteo4", False):
        return
    _Net = ncnn.Net

    def Net(*a, **k):
        net = _Net(*a, **k)
        net.opt.num_threads = int(hilos)
        return net

    Net._conteo4 = True
    ncnn.Net = Net
    print(f"[MODELO] NCNN usará {hilos} hilos")


class Detector:
    """Envuelve el YOLO compartido por todas las cámaras."""

    def __init__(self):
        try:
            import torch
            torch.set_num_threads(HILOS_TORCH)
        except Exception:
            pass
        self.ruta = _resolver_modelo(RUTA_MODELO)
        self.imgsz = _imgsz_del_modelo(self.ruta)
        print(f"[MODELO] {self.ruta} | imgsz={self.imgsz}")
        self.motor = None
        self.model = None
        nombres = None
        if MOTOR == "ncnn" and os.path.isdir(self.ruta):
            try:
                from motor_ncnn import MotorNCNN, nombres_de_metadata
                nombres = nombres_de_metadata(self.ruta)
                hilos = 1 if TRABAJADORES > 1 else HILOS_NCNN
                self.motor = MotorNCNN(self.ruta, self.imgsz, hilos, nc=len(nombres) or None,
                                       rectangular=ENTRADA_RECTANGULAR)
                print(f"[MODELO] Motor NCNN directo, {hilos} hilo(s) por red, entrada "
                      f"{'rectangular (16:9 -> 320x192)' if self.motor.rectangular else 'cuadrada'}")
            except Exception as e:
                print(f"[MODELO] No pude usar el motor NCNN directo ({type(e).__name__}: {e}). "
                      f"Uso Ultralytics.")
                self.motor, nombres = None, None
        if self.motor is None:
            _fijar_hilos_ncnn(HILOS_NCNN)
            from ultralytics import YOLO
            self.model = YOLO(self.ruta, task="detect")
            # En Ultralytics nuevo cada lectura de model.names antes de predecir recarga el
            # modelo: se lee UNA vez.
            nombres = self.model.names
        nombres = {int(i): n for i, n in nombres.items()}
        self.cat_de_id = {i: CLASE_A_CAT[n] for i, n in nombres.items() if n in CLASE_A_CAT}
        self.clases = sorted(self.cat_de_id)
        if not self.clases:
            sys.exit("[ERROR] Ninguna clase del modelo corresponde a carro/moto/bus/pesado/especial.")
        ignoradas = [n for i, n in nombres.items() if i not in self.cat_de_id]
        if ignoradas:
            print(f"[MODELO] clases ignoradas: {ignoradas}")
        # Tabla id_clase -> índice de categoría (para vectorizar)
        self.tabla_cat = np.full(max(nombres) + 1, -1, dtype=np.int64)
        for i, c in self.cat_de_id.items():
            self.tabla_cat[i] = ORDEN_CAT.index(c)
        self.conf_min = CONF_SEGUIMIENTO if ASIGNAR_ID else CONF
        if CALENTAR_MODELO:
            t0 = time.time()
            for alto in (self.imgsz, self.imgsz * 9 // 16):     # cuadrada y 16:9 (cámaras)
                ficticia = np.zeros((alto, self.imgsz, 3), dtype=np.uint8)
                for _ in range(3):
                    self.predecir(ficticia)
            print(f"[MODELO] Calentamiento listo ({time.time() - t0:.1f} s)")

    def clonar(self):
        """Otro detector igual con su PROPIA red NCNN (para un trabajador en paralelo)."""
        import copy
        from motor_ncnn import MotorNCNN
        otro = copy.copy(self)
        otro.motor = MotorNCNN(self.ruta, self.imgsz, self.motor.hilos, nc=self.motor.nc,
                               rectangular=ENTRADA_RECTANGULAR)
        for alto in (self.imgsz, self.imgsz * 9 // 16):     # calentar (cuadrada y 16:9)
            otro.predecir(np.zeros((alto, self.imgsz, 3), dtype=np.uint8))
        return otro

    def predecir(self, img):
        if self.motor is not None:
            return self.motor.predecir(img, self.conf_min, IOU_NMS, MAX_DET, self.clases,
                                       AGNOSTIC_NMS)
        return self.model.predict(img, conf=self.conf_min, imgsz=self.imgsz, max_det=MAX_DET,
                                  iou=IOU_NMS, agnostic_nms=AGNOSTIC_NMS, classes=self.clases,
                                  verbose=False)[0]


def _config_tracker():
    """Ajustes de ByteTrack partiendo del bytetrack.yaml de TU versión de Ultralytics."""
    from ultralytics.utils import ROOT, IterableSimpleNamespace
    base = {}
    try:
        with open(os.path.join(str(ROOT), "cfg", "trackers", "bytetrack.yaml"), encoding="utf-8") as f:
            base = yaml.safe_load(f) or {}
    except Exception:
        pass
    base.setdefault("tracker_type", "bytetrack")
    base.update(AJUSTES_TRACKER)
    return IterableSimpleNamespace(**base)


def _nuevo_tracker(cfg):
    from ultralytics.trackers.byte_tracker import BYTETracker
    try:
        return BYTETracker(args=cfg, frame_rate=30)   # versiones antiguas (buffer = track_buffer)
    except TypeError:
        return BYTETracker(args=cfg)                  # versiones nuevas


# =============================================================================
# FUENTES DE VIDEO
# =============================================================================
def armar_url(entrada):
    """'192.168.1.50' -> http://192.168.1.50:8080/video ; URLs y archivos se dejan igual."""
    entrada = entrada.strip()
    if (entrada.startswith(("http://", "https://", "rtsp://", "rtmp://")) or " ! " in entrada
            or os.path.isfile(entrada)):
        return entrada
    if ":" not in entrada:
        entrada += ":8080"
    return f"http://{entrada}/video"


def _reducir(frame, lado_max):
    lado = max(frame.shape[:2])
    if lado > lado_max:
        e = lado_max / lado
        frame = cv2.resize(frame, None, fx=e, fy=e, interpolation=cv2.INTER_AREA)
    return frame


class _Fuente:
    """Interfaz común: iniciar(), pedir(), tomar(ver) -> (frame | None, ver), detener()."""

    def __init__(self, url, lado):
        self.url = url
        self.lado = lado
        self.conectada = False
        self.fps_llegada = 0.0
        self.ultimo_frame = 0.0
        self._lock = threading.Lock()
        self._ver = 0
        self._detener = threading.Event()
        self._hilo = threading.Thread(target=self._correr_seguro, daemon=True)
        self._t_fps, self._n_fps = time.time(), 0

    def iniciar(self):
        self._hilo.start()
        return self

    def pedir(self):
        pass

    def detener(self):
        self._detener.set()
        self._hilo.join(timeout=3)

    def _correr_seguro(self):
        try:
            self._correr()
        except Exception:
            print(f"[CAMARA] Error en la lectura de {self.url}:")
            traceback.print_exc()
            self.conectada = False

    def _llego_frame(self):
        ahora = time.time()
        self.ultimo_frame = ahora
        self._n_fps += 1
        if ahora - self._t_fps >= 2.0:
            self.fps_llegada = self._n_fps / (ahora - self._t_fps)
            self._t_fps, self._n_fps = ahora, 0


class FuenteOpenCV(_Fuente):
    """rtsp://, archivos y cualquier cosa que no sea MJPEG por http."""

    def __init__(self, url, lado):
        super().__init__(url, lado)
        self.es_archivo = os.path.isfile(url)
        self.es_gstreamer = " ! " in url        # pipeline de GStreamer (decodificación por HW)
        self._pedido = threading.Event()
        self._frame = None
        self._avisado_res = False

    def _abrir(self):
        if self.es_gstreamer:
            return cv2.VideoCapture(self.url, cv2.CAP_GSTREAMER)
        if self.es_archivo:
            return cv2.VideoCapture(self.url)
        # Timeouts: sin ellos una cámara RTSP que se cuelga bloquea grab() para siempre y
        # nunca se reconecta.
        ms = int(TIMEOUT_CONEXION * 1000)
        try:
            return cv2.VideoCapture(self.url, cv2.CAP_FFMPEG,
                                    [cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, ms,
                                     cv2.CAP_PROP_READ_TIMEOUT_MSEC, ms])
        except (cv2.error, TypeError, AttributeError):   # OpenCV < 4.6
            return cv2.VideoCapture(self.url, cv2.CAP_FFMPEG)

    def pedir(self):
        self._pedido.set()

    def tomar(self, ver):
        with self._lock:
            if self._frame is None or self._ver == ver:
                return None, ver
            return self._frame, self._ver

    def _correr(self):
        while not self._detener.is_set():
            cap = self._abrir()
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            if not cap.isOpened():
                print(f"[CAMARA] No pude abrir {self.url}. Reintento en {ESPERA_RECONEXION:.0f} s")
                cap.release()
                self._detener.wait(ESPERA_RECONEXION)
                continue
            print(f"[CAMARA] Conectada: {self.url}")
            self.conectada = True
            periodo = 1.0 / (cap.get(cv2.CAP_PROP_FPS) or 30.0) if self.es_archivo else 0.0
            t_sig = time.time()
            ultimo_ok, fallos = time.time(), 0
            while not self._detener.is_set():
                if not cap.grab():                       # grab = sin conversión a BGR
                    if self.es_archivo:                  # fin del archivo: vuelve al inicio
                        fallos += 1
                        if fallos > 3:
                            break
                        cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                        continue
                    if time.time() - ultimo_ok > TIEMPO_SIN_FRAMES:
                        print(f"[CAMARA] Se perdió la señal: {self.url}")
                        break
                    time.sleep(0.01)
                    continue
                fallos, ultimo_ok = 0, time.time()
                self._llego_frame()
                if self._pedido.is_set():                # solo el frame que YOLO va a usar
                    ok, frame = cap.retrieve()
                    if ok:
                        self._pedido.clear()
                        if not self._avisado_res and not self.es_archivo:
                            self._avisado_res = True
                            if max(frame.shape[:2]) > 2 * max(self.lado, 320):
                                print(f"[AVISO] {self.url} manda {frame.shape[1]}x{frame.shape[0]}. "
                                      f"La Pi decodifica TODOS los frames de un stream H.264: usa "
                                      f"el sub-stream (~640x360, 10-15 FPS) o se saturará la CPU.")
                        frame = _reducir(frame, self.lado)
                        with self._lock:
                            self._frame = frame
                            self._ver += 1
                if periodo:                              # archivo: a velocidad real
                    t_sig += periodo
                    espera = t_sig - time.time()
                    if espera > 0:
                        time.sleep(espera)
                    else:
                        t_sig = time.time()
            self.conectada = False
            cap.release()
            if not self._detener.is_set():
                self._detener.wait(ESPERA_RECONEXION)


class FuenteMJPEG(_Fuente):
    """
    Stream MJPEG por http (IP Webcam, simulador). Guarda solo los BYTES del último JPEG y
    lo decodifica cuando YOLO lo pide. Si el servidor no manda MJPEG, pasa a OpenCV.
    """
    SOI, EOI = b"\xff\xd8", b"\xff\xd9"

    def __init__(self, url, lado):
        super().__init__(url, lado)
        self._jpg = None
        self._flag = cv2.IMREAD_COLOR
        self._flag_listo = False
        self._delegado = None

    def pedir(self):
        if self._delegado:
            self._delegado.pedir()

    def tomar(self, ver):
        if self._delegado:
            return self._delegado.tomar(ver)
        with self._lock:
            if self._jpg is None or self._ver == ver:
                return None, ver
            jpg, v = self._jpg, self._ver
        img = cv2.imdecode(np.frombuffer(jpg, np.uint8), self._flag)
        if img is None:
            return None, v
        if not self._flag_listo:
            # 1er frame: si la cámara manda imágenes grandes, las siguientes se decodifican
            # ya reducidas a 1/2, 1/4 u 1/8 (mucho más barato que decodificar y redimensionar)
            self._flag_listo = True
            f = max(img.shape[:2]) / self.lado
            self._flag = (cv2.IMREAD_REDUCED_COLOR_8 if f >= 8 else
                          cv2.IMREAD_REDUCED_COLOR_4 if f >= 4 else
                          cv2.IMREAD_REDUCED_COLOR_2 if f >= 2 else cv2.IMREAD_COLOR)
            if max(img.shape[:2]) > 2 * self.lado:
                print(f"[CAMARA] {self.url} manda {img.shape[1]}x{img.shape[0]}: bájala a "
                      f"~640 px y ~10 FPS en la cámara para ahorrar red y CPU.")
        return _reducir(img, self.lado), v

    def _correr(self):
        while not self._detener.is_set():
            try:
                req = urllib.request.Request(self.url, headers={"User-Agent": "conteo4"})
                resp = urllib.request.urlopen(req, timeout=TIMEOUT_CONEXION)
            except Exception as e:
                print(f"[CAMARA] No pude conectar a {self.url} ({e}). "
                      f"Reintento en {ESPERA_RECONEXION:.0f} s")
                self._detener.wait(ESPERA_RECONEXION)
                continue
            if "multipart" not in resp.headers.get("Content-Type", "").lower():
                resp.close()
                print(f"[CAMARA] {self.url} no es MJPEG: la leo con OpenCV.")
                self._delegado = FuenteOpenCV(self.url, self.lado).iniciar()
                while not self._detener.is_set():          # refleja su estado
                    self.conectada = self._delegado.conectada
                    self.fps_llegada = self._delegado.fps_llegada
                    self.ultimo_frame = self._delegado.ultimo_frame
                    self._detener.wait(0.5)
                self._delegado.detener()
                return
            print(f"[CAMARA] Conectada (MJPEG): {self.url}")
            self.conectada = True
            buf = bytearray()
            inicio, busca = -1, 0       # inicio = hay un SOI en buf[0]; busca = desde dónde buscar EOI
            try:
                while not self._detener.is_set():
                    trozo = resp.read1(65536)           # timeout del socket = TIMEOUT_CONEXION
                    if not trozo:
                        break
                    buf += trozo
                    ultimo = None
                    while True:
                        if inicio < 0:
                            a = buf.find(self.SOI)
                            if a < 0:
                                del buf[:-1]
                                break
                            del buf[:a]
                            inicio, busca = 0, 2
                        b = buf.find(self.EOI, busca)
                        if b < 0:
                            busca = max(2, len(buf) - 1)
                            break
                        ultimo = bytes(buf[:b + 2])
                        del buf[:b + 2]
                        inicio = -1
                    if ultimo is not None:
                        with self._lock:
                            self._jpg = ultimo
                            self._ver += 1
                        self._llego_frame()
                    if len(buf) > 8_000_000:            # algo raro: no crecer sin límite
                        buf.clear()
                        inicio = -1
            except (socket.timeout, OSError) as e:
                print(f"[CAMARA] Se cortó {self.url} ({e})")
            finally:
                self.conectada = False
                try:
                    resp.close()
                except Exception:
                    pass
            if not self._detener.is_set():
                self._detener.wait(ESPERA_RECONEXION)


def crear_fuente(url, lado):
    if url.startswith(("http://", "https://")):
        return FuenteMJPEG(url, lado).iniciar()
    return FuenteOpenCV(url, lado).iniciar()


# =============================================================================
# IDENTIFICACIÓN (por cámara): 'carro 1', 'moto 2'... votando la categoría
# =============================================================================
class Identificador:
    def __init__(self):
        self.reiniciar()

    def reiniciar(self):
        self._votos = {}                         # tid -> [votos por categoría]
        self._fija = {}                          # tid -> (cat_idx, numero)
        self._contador = [0] * len(ORDEN_CAT)
        self._ultima = {}
        self._t_limpieza = time.time()

    def etiquetar(self, tid, ci):
        """Devuelve (cat_idx_final, numero | None)."""
        self._ultima[tid] = time.time()
        fija = self._fija.get(tid)
        if fija is not None:
            return fija
        v = self._votos.get(tid)
        if v is None:
            v = self._votos[tid] = [0] * len(ORDEN_CAT)
        v[ci] += 1
        if sum(v) < FRAMES_PARA_ETIQUETA:
            return ci, None
        g = v.index(max(v))
        self._contador[g] += 1
        self._fija[tid] = (g, self._contador[g])
        del self._votos[tid]
        return self._fija[tid]

    @property
    def vistos(self):
        return sum(self._contador)

    @property
    def vistos_por_categoria(self):
        """Vehículos distintos (IDs fijados) por categoría desde el inicio / último reinicio."""
        return dict(zip(ORDEN_CAT, self._contador))

    def limpiar(self, max_inactivo=120.0):
        ahora = time.time()
        if ahora - self._t_limpieza < 60.0:
            return
        self._t_limpieza = ahora
        for i in [i for i, t in self._ultima.items() if ahora - t > max_inactivo]:
            for d in (self._votos, self._fija, self._ultima):
                d.pop(i, None)


def _vacio():
    return {"total": 0, "por_categoria": dict.fromkeys(ORDEN_CAT, 0), "cajas": []}


class Camara:
    """Estado de una cámara: su fuente, su tracker, su numeración y su último resultado."""

    def __init__(self, nombre, url, lado, cfg_tracker):
        self.nombre = nombre
        self.url = url
        self.fuente = crear_fuente(url, lado)
        self.tracker = _nuevo_tracker(cfg_tracker) if cfg_tracker is not None else None
        self.ident = Identificador()
        self.ver = 0
        self.resultado = _vacio()
        self.salida = None           # (n, frame, resultado) para la pantalla
        self.n = 0
        self.fps = 0.0               # análisis por segundo de ESTA cámara
        self._t_ult = None

    def reiniciar(self):
        self.ident.reiniciar()
        if self.tracker is not None:
            self.tracker.reset()

    def analizar(self, det, frame):
        res = det.predecir(frame)
        self.ident.limpiar()
        cajas = []
        por_cat = [0] * len(ORDEN_CAT)
        b = res.boxes
        n = len(b)
        if n:
            bn = b.cpu().numpy()
            xyxy = bn.xyxy.astype(np.int32)
            confs = bn.conf
            cats = det.tabla_cat[bn.cls.astype(np.int64)]
            ids = [None] * n
            if self.tracker is not None:
                tr = self.tracker.update(bn, frame)
                for fila in tr:                              # [x1,y1,x2,y2,id,score,cls,idx]
                    ids[int(fila[7])] = int(fila[4])
            for k in range(n):
                ci, cf, tid = int(cats[k]), float(confs[k]), ids[k]
                if ci < 0 or (tid is None and cf < CONF):
                    continue                                 # débil y sin ID: no cuenta
                num = None
                if tid is not None:
                    ci, num = self.ident.etiquetar(tid, ci)
                por_cat[ci] += 1
                x1, y1, x2, y2 = xyxy[k].tolist()
                cajas.append((x1, y1, x2, y2, ci, num, cf, -1 if tid is None else tid))
        elif self.tracker is not None:
            self.tracker.update(b.cpu().numpy(), frame)      # para que envejezcan los IDs
        ahora = time.time()
        if self._t_ult is not None:
            dt = ahora - self._t_ult
            self.fps = 1.0 / dt if not self.fps else 0.8 * self.fps + 0.2 / dt
        self._t_ult = ahora
        self.resultado = {"total": sum(por_cat),
                          "por_categoria": dict(zip(ORDEN_CAT, por_cat)),
                          "cajas": cajas}
        return self.resultado


# =============================================================================
# PUBLICADOR (RL + datos para el CSV u otro programa)
# =============================================================================
class Publicador:
    """
        pub.suscribir_rl(fn)      fn(np.float32 vector) - para el agente RL
        pub.suscribir_datos(fn)   fn(dict)              - para el CSV (u otro programa tuyo)
        pub.cola_rl.get()         último vector del RL
    Los callbacks se llaman desde el hilo de detección cada INTERVALO_RL s: deben ser
    rápidos (si escriben a disco o mandan algo por red, que lo hagan en otro hilo / con
    una cola, como RegistroCSV).
    """

    def __init__(self):
        self._rl, self._datos, self._det = [], [], []
        self._analisis = []          # fn(idx_cam, t_cap, ancho, alto, cajas) tras CADA análisis
        self.cola_rl = queue.Queue(maxsize=1)

    def suscribir_rl(self, fn):
        self._rl.append(fn)

    def suscribir_datos(self, fn):
        self._datos.append(fn)

    def suscribir_det(self, fn):
        self._det.append(fn)

    def suscribir_analisis(self, fn):
        """fn(idx_cam, t_cap, ancho, alto, cajas) tras CADA análisis de YOLO (hilo de detección:
        debe ser rápido). La usa rl_conteos.py para contar vehículos por zona para el semáforo."""
        self._analisis.append(fn)

    def publicar_analisis(self, idx_cam, t_cap, frame, cajas):
        alto, ancho = frame.shape[:2]
        for fn in self._analisis:
            try:                     # un error del enlace con el semáforo NUNCA detiene el conteo
                fn(idx_cam, t_cap, ancho, alto, cajas)
            except Exception as e:
                if not getattr(self, "_aviso_analisis", False):
                    self._aviso_analisis = True
                    print(f"[RL] Error al contar para el semáforo (sigo contando): {type(e).__name__}: {e}")

    @staticmethod
    def orden_estado(camaras):
        if ESTADO_RL == "camara_categoria":
            return [f"{c.nombre}/{cat}" for c in camaras for cat in ORDEN_CAT]
        return [c.nombre for c in camaras]

    @staticmethod
    def armar_estado(camaras):
        if ESTADO_RL == "camara_categoria":
            v = [c.resultado["por_categoria"][cat] for c in camaras for cat in ORDEN_CAT]
        elif ESTADO_RL == "equivalente":
            v = [sum(PESOS_EQUIVALENTES[cat] * c.resultado["por_categoria"][cat] for cat in ORDEN_CAT)
                 for c in camaras]
        else:
            v = [c.resultado["total"] for c in camaras]
        return np.asarray(v, dtype=np.float32)

    @staticmethod
    def armar_datos(camaras, fps_det):
        cams = [{"nombre": c.nombre, "conectada": bool(c.fuente.conectada),
                 "total": c.resultado["total"], "por_categoria": dict(c.resultado["por_categoria"]),
                 "vistos": c.ident.vistos, "vistos_por_categoria": c.ident.vistos_por_categoria,
                 "fps": round(c.fps, 2)} for c in camaras]
        return {"t": time.time(), "total": sum(c["total"] for c in cams),
                "fps_deteccion": round(fps_det or 0.0, 2), "temp_cpu": temperatura_cpu(),
                "camaras": cams}

    def publicar(self, camaras, fps_det):
        if self._rl:
            estado = self.armar_estado(camaras)
            if self.cola_rl.full():
                try:
                    self.cola_rl.get_nowait()
                except queue.Empty:
                    pass
            self.cola_rl.put_nowait(estado)
            for fn in self._rl:
                fn(estado)
        if self._datos:
            d = self.armar_datos(camaras, fps_det)
            for fn in self._datos:
                fn(d)


def enviar_a_rl(estado):
    """Reemplázala por la de tu agente RL."""
    if IMPRIMIR_RL:
        print(f"[RL ] estado = {estado.tolist()}")


# =============================================================================
# [CSV] REGISTRO POR MINUTO (un archivo por día, una fila por cámara y por minuto)
# =============================================================================
class RegistroCSV:
    """
    Se suscribe con pub.suscribir_datos(reg.agregar): recibe el diccionario de datos cada
    INTERVALO_RL s (desde el hilo de detección) y solo suma en memoria. Al cambiar el minuto
    pasa las filas del minuto terminado a un hilo escritor, que las escribe en
    <carpeta>/conteo_AAAA-MM-DD.csv (flush por fila). Así hay UNA escritura por minuto:
    nada de CPU y muy poco desgaste de la tarjeta SD.

    Columnas:
      fecha_hora          inicio del minuto (hora local), p. ej. 2026-11-23 14:05
      camara              nombre de la cámara
      pasaron             vehículos NUEVOS en ese minuto (IDs fijados por ByteTrack)
      carro..especial     lo mismo por categoría
      promedio_en_imagen  vehículos visibles a la vez en promedio (solo con señal)
      maximo_en_imagen    máximo de vehículos visibles a la vez
      segundos            segundos de ese minuto con datos (60 = minuto completo; menos
                          = el programa arrancó, se detuvo o la cámara no tenía señal)
    'pasaron' sale del acumulado vistos_por_categoria: si baja (tecla r = reinicio de IDs),
    se cuenta desde el nuevo valor, así no se pierde nada ni sale negativo.
    """
    COLUMNAS = (["fecha_hora", "camara", "pasaron"] + ORDEN_CAT +
                ["promedio_en_imagen", "maximo_en_imagen", "segundos"])

    def __init__(self, carpeta):
        if not os.path.isabs(carpeta):
            carpeta = os.path.join(CARPETA, carpeta)
        os.makedirs(carpeta, exist_ok=True)
        self.carpeta = carpeta
        self._lock = threading.Lock()
        self._minuto = None              # int(t // 60) del minuto que se está sumando
        self._acum = {}                  # nombre -> acumuladores del minuto actual
        self._previo = {}                # nombre -> último vistos_por_categoria visto
        self._cola = queue.Queue()
        self._hilo = threading.Thread(target=self._escritor, daemon=True)
        self._hilo.start()
        print(f"[CSV] Guardando un resumen por minuto en {carpeta}{os.sep}conteo_AAAA-MM-DD.csv")

    def _nuevo_acum(self):
        return {"pasaron": dict.fromkeys(ORDEN_CAT, 0), "suma": 0, "n": 0, "max": 0, "seg": set()}

    def agregar(self, d):
        t = d["t"]
        minuto = int(t // 60)            # los husos horarios son minutos enteros: sirve igual
        with self._lock:
            if self._minuto is None:
                self._minuto = minuto
            elif minuto != self._minuto:
                self._cerrar_minuto()
                self._minuto = minuto
            seg = int(t) % 60
            for c in d["camaras"]:
                nombre = c["nombre"]
                a = self._acum.get(nombre)
                if a is None:
                    a = self._acum[nombre] = self._nuevo_acum()
                previo = self._previo.get(nombre) or dict.fromkeys(ORDEN_CAT, 0)
                actual = c["vistos_por_categoria"]
                for cat in ORDEN_CAT:
                    v = actual.get(cat, 0)
                    p = previo.get(cat, 0)
                    a["pasaron"][cat] += v - p if v >= p else v   # bajó = reinicio de IDs
                self._previo[nombre] = dict(actual)
                if c["conectada"]:
                    a["suma"] += c["total"]
                    a["n"] += 1
                    a["max"] = max(a["max"], c["total"])
                    a["seg"].add(seg)

    def _cerrar_minuto(self):
        """Arma las filas del minuto actual y se las pasa al hilo escritor (con el lock)."""
        if self._minuto is None or not self._acum:
            return
        t0 = time.localtime(self._minuto * 60)
        fecha = time.strftime("%Y-%m-%d", t0)
        fecha_hora = time.strftime("%Y-%m-%d %H:%M", t0)
        filas = []
        for nombre, a in self._acum.items():
            pas = a["pasaron"]
            prom = f"{a['suma'] / a['n']:.2f}" if a["n"] else ""
            filas.append([fecha_hora, nombre, sum(pas.values())] + [pas[c] for c in ORDEN_CAT]
                         + [prom, a["max"] if a["n"] else "", len(a["seg"])])
        self._cola.put((fecha, filas))
        self._acum = {}

    def _escritor(self):
        import csv
        f, fecha_abierta, escritor = None, None, None
        while True:
            item = self._cola.get()
            if item is None:
                break
            fecha, filas = item
            try:
                if fecha != fecha_abierta:          # cambio de día -> archivo nuevo
                    if f is not None:
                        f.close()
                    ruta = os.path.join(self.carpeta, f"conteo_{fecha}.csv")
                    nuevo = not os.path.isfile(ruta) or os.path.getsize(ruta) == 0
                    f = open(ruta, "a", newline="", encoding="utf-8")
                    escritor = csv.writer(f, delimiter=SEPARADOR_CSV)
                    fecha_abierta = fecha
                    if nuevo:
                        escritor.writerow(self.COLUMNAS)
                        f.flush()
                for fila in filas:
                    escritor.writerow(fila)
                    f.flush()
            except OSError as e:
                print(f"[CSV] No pude escribir el registro ({e}). Sigo contando.")
                if f is not None:
                    try:
                        f.close()
                    except OSError:
                        pass
                f, fecha_abierta = None, None
        if f is not None:
            f.close()

    def cerrar(self):
        """Guarda el minuto en curso (incompleto: ver la columna 'segundos') y termina."""
        with self._lock:
            self._cerrar_minuto()
        self._cola.put(None)
        self._hilo.join(timeout=3)


# =============================================================================
# DIBUJO (solo si hay ventana)
# =============================================================================
def _color_texto(bgr):
    b, g, r = bgr
    return (20, 20, 20) if (0.114 * b + 0.587 * g + 0.299 * r) > 140 else (255, 255, 255)


def dibujar_tile(frame, r, cam):
    out = frame.copy()
    for x1, y1, x2, y2, ci, num, cf, _tid in r["cajas"]:
        cv2.rectangle(out, (x1, y1), (x2, y2), COLOR_CATEGORIA[ORDEN_CAT[ci]], 1)
    if MOSTRAR_ETIQUETAS:
        for x1, y1, x2, y2, ci, num, cf, _tid in r["cajas"]:
            cat = ORDEN_CAT[ci]
            color = COLOR_CATEGORIA[cat]
            txt = (FORMATO_PROVISIONAL.format(nombre=cat, conf=cf) if num is None else
                   FORMATO_ETIQUETA.format(n=num, nombre=cat, conf=cf))
            (tw, th), _ = cv2.getTextSize(txt, FUENTE, ESCALA_ETIQUETA, 1)
            yt = y1 - th - 4 if y1 - th - 4 >= 0 else y1
            xt = min(max(x1, 0), max(out.shape[1] - tw - 4, 0))
            cv2.rectangle(out, (xt, yt), (xt + tw + 4, yt + th + 4), color, -1)
            cv2.putText(out, txt, (xt + 2, yt + th + 1), FUENTE, ESCALA_ETIQUETA,
                        _color_texto(color), 1, AA)
    # barra superior: nombre, total, categorías, FPS de la cámara
    h_barra = 18
    roi = out[0:h_barra, :]
    cv2.addWeighted(roi, 0.35, roi, 0.0, 0.0, dst=roi)
    cv2.putText(out, f"{cam.nombre}  {r['total']}", (5, 13), FUENTE, ESCALA_PANEL,
                COLOR_ACENTO, 1, AA)
    x = 5 + cv2.getTextSize(f"{cam.nombre}  {r['total']}  ", FUENTE, ESCALA_PANEL, 1)[0][0]
    for cat in ORDEN_CAT:
        v = r["por_categoria"][cat]
        if v:
            t = f"{ABREV[cat]}{v}"
            cv2.putText(out, t, (x, 13), FUENTE, ESCALA_PANEL - 0.04, COLOR_CATEGORIA[cat], 1, AA)
            x += cv2.getTextSize(t, FUENTE, ESCALA_PANEL - 0.04, 1)[0][0] + 6
    t = f"{cam.fps:.1f}fps"
    tw = cv2.getTextSize(t, FUENTE, ESCALA_PANEL - 0.06, 1)[0][0]
    cv2.putText(out, t, (out.shape[1] - tw - 5, 13), FUENTE, ESCALA_PANEL - 0.06,
                (180, 180, 180), 1, AA)
    return out


def _pegar(lienzo, img, x0, y0, tw, th):
    """Pega img centrada en el recuadro (x0, y0, tw, th), reduciéndola si no cabe."""
    h, w = img.shape[:2]
    if w > tw or h > th:
        e = min(tw / w, th / h)
        img = cv2.resize(img, (max(1, int(w * e)), max(1, int(h * e))), interpolation=cv2.INTER_AREA)
        h, w = img.shape[:2]
    ox, oy = x0 + (tw - w) // 2, y0 + (th - h) // 2
    lienzo[y0:y0 + th, x0:x0 + tw] = 0
    lienzo[oy:oy + h, ox:ox + w] = img


def _sin_senal(lienzo, cam, x0, y0, tw, th):
    lienzo[y0:y0 + th, x0:x0 + tw] = 30
    cv2.putText(lienzo, f"{cam.nombre}: sin senal", (x0 + 10, y0 + th // 2), FUENTE, 0.5,
                (0, 0, 255), 1, AA)


# =============================================================================
# HILO DE DETECCIÓN (round-robin entre cámaras)
# =============================================================================
_PEDIR_REINICIO = threading.Event()
_RUTA_TEMP = "/sys/class/thermal/thermal_zone0/temp"


def temperatura_cpu():
    """°C de la CPU (Raspberry Pi / Linux). None en Windows."""
    try:
        with open(_RUTA_TEMP) as f:
            return int(f.read().strip()) / 1000.0
    except (OSError, ValueError):
        return None


def _log_estado(camaras, fps_det):
    partes = [f"{c.nombre}:{c.resultado['total']}"
              f"({c.fps:.1f}fps{'' if c.fuente.conectada else ' SIN SEÑAL'})" for c in camaras]
    linea = (f"[ESTADO] YOLO {fps_det or 0.0:.1f} inf/s | " + " | ".join(partes) +
             f" | total {sum(c.resultado['total'] for c in camaras)}")
    temp = temperatura_cpu()
    if temp is not None:
        linea += f" | CPU {temp:.0f}°C"
        if temp >= TEMP_AVISO:
            linea += "  <- ¡CALIENTE! (la Pi se está frenando: ponle ventilador o usa FPS_MAX_POR_CAMARA)"
    print(linea, flush=True)


def _analizar_y_publicar(cam, k_cam, det, frame, pub, mostrar, estado):
    r = cam.analizar(det, frame)
    # hora de llegada del cuadro (las fuentes viejas no la guardan: el recién tomado es el actual)
    t_cap = getattr(cam.fuente, "t_tomado", 0) or time.time()
    if pub._analisis:                      # [RL] conteo por zona para el semáforo
        pub.publicar_analisis(k_cam, t_cap, frame, r["cajas"])
    if getattr(pub, "det_activo", False):  # [VIDEO v2] algún destino quiere video
        pub.publicar_det(k_cam, cam.ver, t_cap, frame, r["cajas"])
    if mostrar or estado.get("video_remoto"):   # ventana local
        cam.n += 1
        cam.salida = (cam.n, frame, r)


def _trabajador(det, camaras, pub, detener, mostrar, estado, cuenta, w, gen):
    """Toma por turnos la siguiente cámara LIBRE que tenga un cuadro nuevo (reparto parejo
    aunque haya más cámaras que trabajadores). Cada cámara tiene un candado: su tracker lo
    usa un solo hilo a la vez y sus cuadros se analizan en orden. Con su propia red NCNN de 1
    hilo, un trabajador no espera a los demás (ver TRABAJADORES)."""
    n = len(camaras)
    i = w % n
    vacias = 0
    mi_gen = gen[0]
    periodo_min = 1.0 / FPS_MAX_POR_CAMARA if FPS_MAX_POR_CAMARA else 0.0
    try:
        while not detener.is_set():
            if gen[0] != mi_gen:                       # tecla r: lo hace el coordinador
                mi_gen = gen[0]
            k_cam, cam = i, camaras[i]
            i = (i + 1) % n
            frame = None
            if cam._candado.acquire(blocking=False):  # otro trabajador la está analizando: siguiente
                try:
                    if not (periodo_min and cam._t_ult and time.time() - cam._t_ult < periodo_min):
                        frame, cam.ver = cam.fuente.tomar(cam.ver)
                    if frame is None:
                        # cámara caída: sus conteos vuelven a 0 tras TIEMPO_SIN_FRAMES
                        if ((cam.resultado["total"] or cam.salida is not None)
                                and time.time() - cam.fuente.ultimo_frame > TIEMPO_SIN_FRAMES):
                            cam.resultado = _vacio()
                            cam.salida = None
                            cam.fps = 0.0
                            cam._t_ult = None
                    else:
                        _analizar_y_publicar(cam, k_cam, det, frame, pub, mostrar, estado)
                        cuenta[w] += 1
                finally:
                    cam._candado.release()
            camaras[i].fuente.pedir()                  # [PREFETCH] la siguiente se prepara
            if frame is None:
                vacias += 1
                if vacias >= n:
                    vacias = 0
                    # ninguna cámara tiene cuadro nuevo: pausa corta; si todas están caídas,
                    # pausa larga (no gastar CPU girando mientras se reconectan)
                    if not any(c.fuente.conectada for c in camaras):
                        time.sleep(0.1)
                    else:
                        time.sleep(0.01 if periodo_min else 0.005)
            else:
                vacias = 0
    except Exception:
        print(f"[ERROR] Falló el hilo de detección {w + 1}:")
        traceback.print_exc()
        detener.set()


def hilo_deteccion(det, camaras, pub, detener, mostrar, estado):
    """Coordina: lanza los trabajadores (TRABAJADORES, cada uno con un grupo fijo de cámaras y
    su propia red) y se encarga de publicar los datos y el log aunque no lleguen cuadros."""
    n = len(camaras)
    w_total = max(1, min(TRABAJADORES, n)) if getattr(det, "motor", None) is not None else 1
    dets = [det]
    for _ in range(w_total - 1):
        try:
            dets.append(det.clonar())
        except Exception as e:                         # sin RAM, etc.: los que se pudieron
            print(f"[MODELO] No pude crear otro trabajador ({type(e).__name__}: {e}). Sigo con {len(dets)}.")
            break
    w_total = len(dets)
    for c in camaras:
        c._candado = threading.Lock()
        c.fuente.pedir()
    if w_total > 1:
        print(f"[MODELO] {w_total} trabajadores en paralelo (red NCNN de 1 hilo cada uno; "
              f"toman la siguiente cámara libre)")
    cuenta = [0] * w_total
    gen = [0]
    hilos = [threading.Thread(target=_trabajador, name=f"deteccion-{w + 1}", daemon=True,
                              args=(dets[w], camaras, pub, detener, mostrar, estado, cuenta, w, gen))
             for w in range(w_total)]
    for h in hilos:
        h.start()
    t_pub = t_log = t_fps = time.time()
    hechas = 0
    fps_det = None                     # inferencias REALES por segundo (todas las cámaras)
    try:
        while not detener.is_set():
            if _PEDIR_REINICIO.is_set():
                _PEDIR_REINICIO.clear()
                for c in camaras:
                    with c._candado:                   # nunca a mitad de un análisis
                        c.reiniciar()
                gen[0] += 1
                print("[ID] Numeración reiniciada en todas las cámaras.")
            ahora = time.time()
            if ahora - t_fps >= 2.0:
                total = sum(cuenta)
                fps_det = (total - hechas) / (ahora - t_fps)
                estado["fps_det"] = fps_det
                t_fps, hechas = ahora, total
            if ahora - t_pub >= INTERVALO_RL:
                pub.publicar(camaras, fps_det)
                t_pub = ahora
            if ahora - t_log >= INTERVALO_LOG:
                _log_estado(camaras, fps_det)
                t_log = ahora
            detener.wait(0.05)
    except Exception:
        print("[ERROR] Falló el hilo de detección:")
        traceback.print_exc()
        detener.set()
    for h in hilos:
        h.join(timeout=3)


# =============================================================================
# MAIN
# =============================================================================
def _leer_argumentos():
    banderas = {"--pantalla", "--sin-pantalla", "--sin-csv"}
    args = sys.argv[1:]
    carpeta_csv = CARPETA_CSV
    if "--carpeta-csv" in args:
        k = args.index("--carpeta-csv")
        if k + 1 >= len(args):
            sys.exit("[ERROR] Falta la carpeta después de --carpeta-csv")
        carpeta_csv = args[k + 1]
        del args[k:k + 2]
    resto = [a for a in args if a not in banderas]
    mostrar = (MOSTRAR_VIDEO or "--pantalla" in args) and "--sin-pantalla" not in args
    csv_activo = GUARDAR_CSV and "--sin-csv" not in args
    for a in resto:
        if a.startswith("--"):
            sys.exit(f"[ERROR] Opción desconocida: {a}  (válidas: --pantalla, --sin-pantalla, "
                     f"--sin-csv, --carpeta-csv RUTA)")
    if resto:
        lista = []
        for k, a in enumerate(resto):
            nombre = a.split("=", 1)[0]
            if "=" in a and nombre.isidentifier():         # Norte=url (no rompe URLs con '=')
                url = a.split("=", 1)[1]
            else:
                nombre, url = f"Cam{k + 1}", a
            lista.append((nombre, url))
    else:
        lista = [(n, u) for n, u in CAMARAS if u]
    if not lista:
        sys.exit("[ERROR] No hay cámaras: llena CAMARAS o pásalas por la consola.")
    if len(lista) > MAX_CAMARAS:
        print(f"[AVISO] Solo se usan las primeras {MAX_CAMARAS} cámaras.")
        lista = lista[:MAX_CAMARAS]
    if mostrar and not _hay_pantalla():
        print("[AVISO] No hay pantalla (¿SSH o arranque automático?): sigo SIN video.")
        mostrar = False
    return lista, mostrar, (carpeta_csv if csv_activo else None)


def _hay_pantalla():
    """En la Pi por SSH o como servicio no hay escritorio: abrir una ventana ahí haría que
    Qt cierre el programa entero de golpe (no se puede atrapar), así que se revisa antes."""
    if not sys.platform.startswith("linux"):
        return True
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def main():
    lista, mostrar, carpeta_csv = _leer_argumentos()
    det = Detector()
    lado = LADO_TRABAJO or det.imgsz
    cfg = _config_tracker() if ASIGNAR_ID else None

    camaras = [Camara(n, armar_url(u), lado, cfg) for n, u in lista]
    for c in camaras:
        print(f"[CAMARA] {c.nombre} -> {c.url}")

    pub = Publicador()
    pub.suscribir_rl(enviar_a_rl)      # <- aquí conectas tu agente RL
    print(f"[RL ] orden del estado: {Publicador.orden_estado(camaras)}")

    registro = None
    if carpeta_csv:
        registro = RegistroCSV(carpeta_csv)
        pub.suscribir_datos(registro.agregar)
    else:
        print("[CSV] Desactivado (--sin-csv o GUARDAR_CSV = False).")

    import rl_conteos                   # enlace con el semáforo (RL): rl_zonas.json
    enlace_rl = rl_conteos.crear(camaras, pub)

    detener = threading.Event()
    estado = {"fps_det": None}
    h = threading.Thread(target=hilo_deteccion,
                         args=(det, camaras, pub, detener, mostrar, estado), daemon=True)
    h.start()

    try:
        if mostrar:
            try:
                mostrar_mosaico(camaras, detener, estado, lado, pub)
            except cv2.error as e:
                # p. ej. opencv-python-headless instalado: no puede abrir ventanas
                print(f"[AVISO] No pude abrir la ventana ({str(e).strip()[:120]}). Sigo SIN video.")
                mostrar = False
        if not mostrar:
            print("[INFO] Corriendo SIN pantalla. Ctrl+C para detener.")
            while not detener.is_set():
                time.sleep(0.5)
    except KeyboardInterrupt:
        print("\n[INFO] Detenido por el usuario.")
    finally:
        detener.set()
        if enlace_rl is not None:
            enlace_rl.detener()
        h.join(timeout=3)
        if registro is not None:
            registro.cerrar()
        for c in camaras:
            c.fuente.detener()
        try:
            cv2.destroyAllWindows()
        except cv2.error:
            pass


class Mosaico:
    """
    Mosaico 2x2 (1x1, 2x1) con las cajas de YOLO y un pie con el total. Solo redibuja la
    cámara que cambió. Lo usa la ventana local (--pantalla).
    """

    def __init__(self, camaras, lado, h_pie=20):
        self.camaras = camaras
        self.lado = lado
        n = len(camaras)
        self.cols = 1 if n == 1 else 2
        self.filas = (n + self.cols - 1) // self.cols
        self.h_pie = h_pie
        self.lienzo = None
        self._vistos = [-1] * n
        self._conectada = [None] * n
        self._pie = None

    def _crear(self):
        """El alto del recuadro toma la proporción del 1er video que llega (16:9 o 4:3)."""
        sal = next((c.salida for c in self.camaras if c.salida is not None), None)
        if sal is None:
            return False
        lado = self.lado
        self.tw = lado
        self.th = min(max(int(sal[1].shape[0] * lado / sal[1].shape[1]), lado // 2), lado)
        self.lienzo = np.zeros((self.filas * self.th + self.h_pie, self.cols * self.tw, 3),
                               dtype=np.uint8)
        return True

    def actualizar(self, fps_det):
        """Redibuja lo que cambió. Devuelve True si el lienzo cambió."""
        if self.lienzo is None and not self._crear():
            return False
        lienzo, tw, th = self.lienzo, self.tw, self.th
        cambio = False
        for k, cam in enumerate(self.camaras):
            x0, y0 = (k % self.cols) * tw, (k // self.cols) * th
            sal = cam.salida
            ok = cam.fuente.conectada or sal is not None
            if sal is not None and sal[0] != self._vistos[k]:
                self._vistos[k] = sal[0]
                _pegar(lienzo, dibujar_tile(sal[1], sal[2], cam), x0, y0, tw, th)
                cambio = True
            elif sal is None and self._conectada[k] is not False and not ok:
                _sin_senal(lienzo, cam, x0, y0, tw, th)
                cambio = True
            self._conectada[k] = ok
        total = sum(c.resultado["total"] for c in self.camaras)
        fps = fps_det or 0.0
        pie = (total, round(fps, 1))
        if pie != self._pie:
            self._pie = pie
            h = lienzo.shape[0]
            lienzo[-self.h_pie:, :] = 25
            cv2.putText(lienzo, f"TOTAL {total}", (6, h - 6), FUENTE, 0.5, COLOR_ACENTO, 1, AA)
            t = f"YOLO {fps:.1f} inf/s"
            w = cv2.getTextSize(t, FUENTE, 0.4, 1)[0][0]
            cv2.putText(lienzo, t, (lienzo.shape[1] - w - 6, h - 6), FUENTE, 0.4,
                        (40, 200, 250), 1, AA)
            cambio = True
        return cambio


TITULO_SEMAFORO = "Semaforo (RL)"


def mostrar_mosaico(camaras, detener, estado, lado, pub=None):
    titulo = "conteo4 (q = salir, r = reiniciar IDs)"
    cv2.namedWindow(titulo, cv2.WINDOW_AUTOSIZE)
    print("[PANTALLA] Esperando el primer video...")
    mosaico = Mosaico(camaras, lado)
    periodo = 1.0 / FPS_MAX_PANTALLA
    t_ult = 0.0
    pendiente = False
    n_sem, t_sem, error_sem = None, 0.0, False      # [RL] ventana del semáforo
    while not detener.is_set():
        pendiente = mosaico.actualizar(estado.get("fps_det")) or pendiente
        ahora = time.time()
        # [RL] estado del semáforo (rl_zonas.json: "interfaz": true). Se redibuja al cambiar
        # (~1/s) y cada 1 s aunque no cambie, para que se note si deja de llegar.
        sem = getattr(pub, "semaforo", None) if pub is not None else None
        if sem is not None and not error_sem and (sem.get("n") != n_sem or ahora - t_sem >= 1.0):
            n_sem, t_sem = sem.get("n"), ahora
            try:
                import rl_conteos
                cv2.imshow(TITULO_SEMAFORO, rl_conteos.dibujar_panel(sem))
            except Exception as e:                  # nunca tumbar la ventana del conteo
                error_sem = True
                print(f"[RL] No pude dibujar el semáforo: {type(e).__name__}: {e}")
        if pendiente and ahora - t_ult >= periodo:
            lienzo = mosaico.lienzo
            vista = lienzo if ESCALA_VENTANA == 1.0 else cv2.resize(
                lienzo, None, fx=ESCALA_VENTANA, fy=ESCALA_VENTANA, interpolation=cv2.INTER_LINEAR)
            cv2.imshow(titulo, vista)
            t_ult = ahora
            pendiente = False               # si no tocaba mostrar, queda pendiente
        k = cv2.waitKey(20) & 0xFF
        if k in (ord('q'), 27):
            break
        if k == ord('r'):
            _PEDIR_REINICIO.set()
        if mosaico.lienzo is not None:
            try:                            # cerraron la ventana con la X
                if cv2.getWindowProperty(titulo, cv2.WND_PROP_VISIBLE) < 1:
                    break
            except cv2.error:
                break


if __name__ == "__main__":
    main()
