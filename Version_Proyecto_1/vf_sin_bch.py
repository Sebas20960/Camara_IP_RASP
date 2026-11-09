# =============================================================================
# conteo_carriles_camara_ip_rl_sin_benchmark.py  (Raspberry Pi 4 - CONTEO de vehículos POR CARRIL)
# Detecta vehículos con YOLO (Ultralytics, modelo NCNN) y cuenta cuántos hay en cada
# carril. La ÚNICA fuente de video es una CÁMARA IP (celular con IP Webcam, cámara falsa
# con video de prueba o cámara de seguridad real). No hay modo video ni modo pantalla.
# Los carriles se dibujan con el ratón sobre el primer frame CADA VEZ que se inicia
# el programa (no se guardan en ningún archivo).
# NO mide velocidad ni distancias. Ignora peatones, bicicletas y todo lo que no sea vehículo.
#
# SALIDA: solo hacia el algoritmo de RL (no se envía nada a ninguna interfaz gráfica).
#   - La detección corre de forma continua sobre el frame más reciente de la cámara.
#   - El estado se entrega al RL cada INTERVALO_RL segundos (ver configuración).
#
# PANTALLA: arriba el TOTAL y debajo el total por carril/avenida (sin desglose por clase).
#
# Uso:
#     python conteo_carriles_camara_ip_rl_sin_benchmark.py 192.168.1.33:8080     (IP:puerto del celular)
#     python conteo_carriles_camara_ip_rl_sin_benchmark.py 192.168.1.33
#     python conteo_carriles_camara_ip_rl_sin_benchmark.py http://192.168.1.33:8080/video
#     python conteo_carriles_camara_ip_rl_sin_benchmark.py rtsp://192.168.1.33:8554/live
#     python conteo_carriles_camara_ip_rl_sin_benchmark.py                       (te pide la IP o la URL)
#   (también acepta el formato anterior: --camara 192.168.1.33:8080)
#   Por defecto, DESPUÉS de elegir los carriles NO se muestra ninguna ventana: el script
#   sigue corriendo y enviando datos al RL (Ctrl+C para detenerlo). Añade --pantalla si
#   quieres ver el video con las cajas y los conteos (en ese modo sales con la tecla q).
#
# Al arrancar se abre una ventana con el primer frame para dibujar los carriles:
#     clic izquierdo = agregar una esquina del carril
#     ENTER          = cerrar el carril (te pide el nombre en la consola; ENTER = nombre por defecto)
#     z              = deshacer la última esquina
#     r              = borrar todos los carriles y empezar de nuevo
#     q / ESC        = terminar de dibujar y arrancar el conteo
# Si no dibujas ningún carril, se cuenta todo el frame.
#
# Requisitos: pip install ultralytics opencv-python numpy pyyaml   (torch en su versión CPU)
#             v1_camara.py (módulo de la cámara IP) debe estar en la MISMA carpeta
# =============================================================================
import os
from pickle import TRUE
import sys

from sympy import true

# En Linux (Raspberry Pi OS con Wayland) OpenCV necesita el plugin xcb para abrir ventanas.
# Debe definirse ANTES de importar cv2.
if sys.platform.startswith("linux"):
    os.environ.setdefault("QT_QPA_PLATFORM", "xcb")

# Para URLs rtsp:// usar TCP (más estable que UDP en WiFi). También ANTES de importar cv2.
os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", "rtsp_transport;tcp")

import time
import queue
import threading
import traceback

import cv2
import numpy as np
import yaml
from ultralytics import YOLO

# Módulo de la cámara IP (archivo v1_camara.py, en la misma carpeta que este script)
try:
    from v1_camara import CamaraIP, armar_url
except ImportError as e:
    sys.exit(f"[ERROR] No pude importar CamaraIP desde v1_camara.py: {e}\n"
             f"        Revisa que el archivo esté en la misma carpeta y tenga la clase CamaraIP.")

# -----------------------------------------------------------------------------
# CONFIGURACIÓN GENERAL
# -----------------------------------------------------------------------------
RUTA_MODELO = "modelo_combinado_ncnn_model"
CONF = 0.4
MAX_DET = 50                          # máximo de detecciones por frame
INTERVALO_RL = 0.5                    # segundos mínimos entre cada envío de estado al RL.
                                      # (se envía el conteo de la detección más reciente; si la
                                      #  detección es más lenta que esto, se envía tras cada una)


def _imgsz_del_modelo(ruta, por_defecto):
    """Lee el imgsz con el que exportaste el modelo NCNN (metadata.yaml dentro de la carpeta)."""
    if not os.path.isdir(ruta):
        return por_defecto
    try:
        with open(os.path.join(ruta, "metadata.yaml"), "r", encoding="utf-8") as f:
            v = yaml.safe_load(f).get("imgsz")
        return int(v[0]) if isinstance(v, (list, tuple)) else int(v)
    except Exception:
        print(f"[AVISO] No pude leer imgsz de {ruta}/metadata.yaml; uso {por_defecto}")
        return por_defecto


IMGSZ        = _imgsz_del_modelo(RUTA_MODELO, 320)
LADO_TRABAJO = 640   # el frame se reduce a este lado mayor ANTES de todo

# -----------------------------------------------------------------------------
# CONFIGURACIÓN DEL PREPROCESAMIENTO
# -----------------------------------------------------------------------------
PRE_REDIMENSIONAR = True
PRE_LADO_MAX      = IMGSZ
PRE_BLUR          = False
PRE_CLAHE         = False

# -----------------------------------------------------------------------------
# CONFIGURACIÓN DE LA PANTALLA
# -----------------------------------------------------------------------------
MOSTRAR_VIDEO          = TRUE   # False = tras elegir los carriles NO se abre ninguna ventana
                                 # (se puede forzar con el argumento --pantalla)
MOSTRAR_ETIQUETA_CLASE = False   # True = escribe el nombre de la clase (car, omnibus...) sobre cada caja
MOSTRAR_FPS            = TRUE   # True = escribe los FPS de detección en la pantalla

_clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))


# -----------------------------------------------------------------------------
# CARGA DEL MODELO (NCNN ya exportado)
# -----------------------------------------------------------------------------
if not os.path.exists(RUTA_MODELO):
    sys.exit(f"[ERROR] No encuentro el modelo: {RUTA_MODELO}\n"
             f"        Ejecuta el script desde la carpeta donde está, o pon la ruta completa en RUTA_MODELO.")
print(f"[MODELO] {RUTA_MODELO} | imgsz={IMGSZ}")
model = YOLO(RUTA_MODELO, task="detect")
CLASES = list(model.names.values())

# -----------------------------------------------------------------------------
# QUÉ CUENTA COMO VEHÍCULO
# -----------------------------------------------------------------------------
GRUPOS = {
    "transportistas": ['car', 'taxi', 'suv', 'pickup', 'camioneta', 'minivan', 'van', 'policecar',
                       'motorbike', 'scooter', 'bicimoto', 'trimoto', 'mototaxi',
                       'auto rickshaw', 'three wheelers -CNG-',
                       'bus', 'minibus', 'omnibus', 'omnibus-articulado', 'human hauler',
                       'truck', 'semi-trailer', 'trailer', 'garbagevan',
                       'ambulance', 'army vehicle', 'especiales'],
}
VEHICULOS = {c for cls in GRUPOS.values() for c in cls}

IDS_VEHICULOS = [i for i, nombre in model.names.items() if nombre in VEHICULOS] or None
CLASES_VEHICULO = [c for c in CLASES if c in VEHICULOS]
_ignoradas = [c for c in CLASES if c not in VEHICULOS]
print(f"[MODELO] Cuenta {len(CLASES_VEHICULO)} clases de vehículo. Ignora: {_ignoradas}")
if IDS_VEHICULOS is None:
    print("[AVISO] Ninguna clase del modelo coincide con la lista de vehículos: revisa los "
          "nombres en GRUPOS. Se detectará TODO sin filtrar.")


# =============================================================================
# CARRILES (se definen al iniciar; no se guardan)
# =============================================================================
CARRILES = {}      # {"nombre": [(x, y), ...]} en coords del frame de trabajo
_POLIGONOS = {}    # {"nombre": np.array(...)} para pointPolygonTest
_CENTROS = {}      # {"nombre": (cx, cy)} donde se escribe el conteo del carril


def definir_carriles(frame):
    """
    Ventana para dibujar los carriles sobre 'frame' (ya reducido a LADO_TRABAJO).
    Rellena CARRILES, _POLIGONOS y _CENTROS. Se llama una vez al iniciar.
    """
    WIN = "Dibuja los carriles (ENTER=cerrar carril, z=deshacer, r=reiniciar, q=terminar)"
    nuevos, actual = {}, []

    def clic(ev, x, y, flags, _):
        if ev == cv2.EVENT_LBUTTONDOWN:
            actual.append((x, y))

    cv2.namedWindow(WIN)
    cv2.setMouseCallback(WIN, clic)
    print("\n[CARRILES] Dibuja los carriles: clic = esquina | ENTER = cerrar carril | "
          "z = deshacer | r = reiniciar | q = terminar")
    while True:
        vista = frame.copy()
        for n, p in nuevos.items():
            pts = np.array(p, dtype=np.int32)
            cv2.polylines(vista, [pts], True, (0, 255, 0), 2)
            cv2.putText(vista, n, tuple(int(v) for v in pts[0]),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 255, 0), 2)
        for pt in actual:
            cv2.circle(vista, pt, 4, (0, 0, 255), -1)
        if len(actual) > 1:
            cv2.polylines(vista, [np.array(actual, dtype=np.int32)], False, (0, 200, 255), 2)
        cv2.putText(vista, f"Carriles: {len(nuevos)} | puntos del actual: {len(actual)}",
                    (10, 20), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 2)
        cv2.imshow(WIN, vista)

        k = cv2.waitKey(20) & 0xFF
        if k == 13 and len(actual) >= 3:                       # ENTER: cerrar carril
            por_defecto = f"Carril_{len(nuevos) + 1}"
            nombre = input(f"Nombre del carril (ENTER = {por_defecto}): ").strip() or por_defecto
            while nombre in nuevos:                             # evitar nombres repetidos
                nombre += "_b"
            nuevos[nombre] = actual.copy()
            actual.clear()
        elif k == 13:
            print("[CARRILES] Un carril necesita al menos 3 esquinas.")
        elif k == ord('z') and actual:
            actual.pop()
        elif k == ord('r'):
            nuevos.clear()
            actual.clear()
        elif k in (ord('q'), 27):                               # q o ESC
            break
    cv2.destroyWindow(WIN)
    cv2.waitKey(1)

    CARRILES.clear()
    CARRILES.update({n: [tuple(pt) for pt in p] for n, p in nuevos.items()})
    _POLIGONOS.clear()
    _CENTROS.clear()
    for n, p in CARRILES.items():
        poly = np.array(p, dtype=np.int32)
        _POLIGONOS[n] = poly
        cx, cy = poly.mean(axis=0)
        _CENTROS[n] = (int(cx), int(cy))

    if CARRILES:
        print(f"[CARRILES] {len(CARRILES)} definidos: {list(CARRILES)}")
    else:
        print("[CARRILES] No definiste ninguno: se contará todo el frame.")


def carril_de(x1, y1, x2, y2):
    """Devuelve el nombre del carril donde cae el punto de referencia de la caja, o None."""
    px, py = (x1 + x2) / 2, y2            # centro inferior de la caja
    for nombre, poly in _POLIGONOS.items():
        if cv2.pointPolygonTest(poly, (float(px), float(py)), False) >= 0:
            return nombre
    return None


# -----------------------------------------------------------------------------
# ETAPA DE PREPROCESAMIENTO DE IMAGEN
# -----------------------------------------------------------------------------
def preprocesar(frame):
    """Devuelve (imagen_preprocesada, escala)."""
    img = frame

    escala = 1.0
    if PRE_REDIMENSIONAR:
        lado = max(img.shape[:2])
        if lado > PRE_LADO_MAX:
            escala = PRE_LADO_MAX / lado
            img = cv2.resize(img, None, fx=escala, fy=escala, interpolation=cv2.INTER_AREA)

    if PRE_BLUR:
        img = cv2.GaussianBlur(img, (3, 3), 0)

    if PRE_CLAHE:
        l, a, b = cv2.split(cv2.cvtColor(img, cv2.COLOR_BGR2LAB))
        img = cv2.cvtColor(cv2.merge((_clahe.apply(l), a, b)), cv2.COLOR_LAB2BGR)

    return img, escala


# -----------------------------------------------------------------------------
# NÚCLEO: detección + conteo por carril
# -----------------------------------------------------------------------------
def contar_vehiculos(frame, conf=CONF):
    """
    frame: imagen BGR (ya reducida a LADO_TRABAJO, sin preprocesar)
    return: (por_clase, por_grupo, total, boxes, por_carril)
    boxes: (x1, y1, x2, y2, nombre_clase) en coords del frame
    Si hay carriles definidos, solo se cuentan (y dibujan) los vehículos que caen dentro de alguno.
    """
    img, escala = preprocesar(frame)

    res = model.predict(img, conf=conf, imgsz=IMGSZ, max_det=MAX_DET,
                        classes=IDS_VEHICULOS, verbose=False)[0]

    if len(res.boxes):
        xyxy = res.boxes.xyxy.cpu().numpy()
        clases = res.boxes.cls.int().cpu().tolist()
    else:
        xyxy, clases = [], []

    por_clase = {c: 0 for c in CLASES_VEHICULO}
    por_carril = {n: 0 for n in CARRILES}
    boxes = []

    for (x1, y1, x2, y2), c in zip(xyxy, clases):
        nombre = res.names[c]
        if nombre not in VEHICULOS:
            continue

        x1, x2 = float(x1) / escala, float(x2) / escala
        y1, y2 = float(y1) / escala, float(y2) / escala

        if _POLIGONOS:                       # hay carriles: ignorar lo que cae fuera de ellos
            carril = carril_de(x1, y1, x2, y2)
            if carril is None:
                continue
            por_carril[carril] += 1

        por_clase[nombre] += 1
        boxes.append((int(x1), int(y1), int(x2), int(y2), nombre))

    por_grupo = {g: sum(por_clase.get(c, 0) for c in cls) for g, cls in GRUPOS.items()}
    total = sum(por_grupo.values())
    return por_clase, por_grupo, total, boxes, por_carril


def dibujar(frame, boxes, por_clase, total, fps_det=None, por_carril=None):
    """
    Dibuja los carriles, la caja de cada vehículo y, arriba a la izquierda, un panel con:
    el TOTAL y debajo el total de cada carril/avenida. No muestra el desglose por clase.
    """
    out = frame.copy()
    fuente = cv2.FONT_HERSHEY_SIMPLEX

    for poly in _POLIGONOS.values():
        cv2.polylines(out, [poly], True, (255, 150, 0), 2)

    for x1, y1, x2, y2, nombre in boxes:
        cv2.rectangle(out, (x1, y1), (x2, y2), (0, 255, 0), 2)
        if MOSTRAR_ETIQUETA_CLASE:
            cv2.putText(out, nombre, (x1, max(y1 - 5, 12)), fuente, 0.5, (0, 255, 0), 1)

    # panel: (texto, color BGR, escala)
    lineas = [(f"TOTAL: {total}", (0, 0, 255), 0.8)]
    if por_carril:
        for n, cant in por_carril.items():
            lineas.append((f"{n}: {cant}", (255, 255, 255), 0.6))
    if MOSTRAR_FPS and fps_det:
        lineas.append((f"Deteccion: {fps_det:.1f} FPS", (0, 200, 255), 0.6))

    ancho = max(cv2.getTextSize(txt, fuente, esc, 2)[0][0] for txt, _, esc in lineas)
    alto = 12 + 26 * len(lineas)
    roi = out[0:alto, 0:ancho + 20]                      # fondo oscuro semitransparente
    cv2.addWeighted(roi, 0.4, np.zeros_like(roi), 0.6, 0, roi)
    y = 26
    for txt, color, esc in lineas:
        cv2.putText(out, txt, (10, y), fuente, esc, color, 2)
        y += 26
    return out


# =============================================================================
# ENVÍO DE DATOS AL ALGORITMO DE RL
# =============================================================================
class Publicador:
    """
    Entrega el estado de los carriles al algoritmo de RL, de dos formas (usa la que prefieras):
        pub.suscribir_rl(mi_funcion_rl)   # callback: se llama con el vector de estado
        estado = pub.cola_rl.get()        # cola: guarda solo el estado MÁS RECIENTE
    OJO: el callback se llama desde el hilo de detección, no desde el hilo principal.

    Estado para el RL (np.float32):
      - con carriles: un valor por carril, en el orden en que los dibujaste (list(CARRILES))
      - sin carriles: un valor por grupo (list(GRUPOS))
    """

    def __init__(self):
        self._rl = []
        self.cola_rl = queue.Queue(maxsize=1)   # guarda solo el estado más reciente

    def suscribir_rl(self, fn):
        self._rl.append(fn)

    @staticmethod
    def _poner_ultimo(cola, dato):
        """Reemplaza el dato viejo por el nuevo (el RL siempre quiere el estado actual)."""
        if cola.full():
            try:
                cola.get_nowait()
            except queue.Empty:
                pass
        cola.put_nowait(dato)

    def publicar(self, por_grupo, por_carril):
        if CARRILES:
            estado_rl = np.array([por_carril[n] for n in CARRILES], dtype=np.float32)
        else:
            estado_rl = np.array([por_grupo[g] for g in GRUPOS], dtype=np.float32)

        self._poner_ultimo(self.cola_rl, estado_rl)
        for fn in self._rl:
            fn(estado_rl)


# Función de prueba: reemplázala por la real de tu agente RL
def enviar_a_rl(estado):
    orden = list(CARRILES) if CARRILES else list(GRUPOS)
    print(f"[RL ] estado = {estado.tolist()}  (orden: {orden})")


# =============================================================================
# CANALES ENTRE HILOS Y BUCLE COMÚN
# =============================================================================
class UltimoValor:
    """Guarda solo el dato más reciente. Quien lee se salta lo que no alcanzó a leer."""

    def __init__(self):
        self._lock = threading.Lock()
        self._v = None
        self._ver = 0

    def poner(self, v):
        with self._lock:
            self._v = v
            self._ver += 1

    def tomar(self, ver_visto):
        """Devuelve (dato, version). Si no hay nada nuevo desde ver_visto: (None, ver_visto)."""
        with self._lock:
            if self._ver == ver_visto:
                return None, ver_visto
            return self._v, self._ver


def _reducir(frame):
    """Reduce el frame a LADO_TRABAJO (solo reduce). Todo el sistema trabaja sobre este tamaño."""
    lado = max(frame.shape[:2])
    if lado > LADO_TRABAJO:
        e = LADO_TRABAJO / lado
        frame = cv2.resize(frame, None, fx=e, fy=e, interpolation=cv2.INTER_AREA)
    return frame


def _hilo_inferencia(frames, publicador, resultado, detener):
    """Toma siempre el frame más reciente, corre YOLO y deja el resultado listo para dibujar."""
    ver = 0
    ultimo_envio = 0.0
    fps_det = None
    try:
        while not detener.is_set():
            dato, ver = frames.tomar(ver)
            if dato is None:
                time.sleep(0.003)
                continue
            _, frame = dato

            t_ini = time.time()
            por_clase, por_grupo, total, boxes, por_carril = contar_vehiculos(frame)
            dt = time.time() - t_ini
            if dt > 0:
                fps_det = 1.0 / dt if fps_det is None else 0.8 * fps_det + 0.2 * (1.0 / dt)

            resultado.poner((boxes, por_clase, total, fps_det, por_carril))

            ahora = time.time()
            if ahora - ultimo_envio >= INTERVALO_RL:
                publicador.publicar(por_grupo, por_carril)
                ultimo_envio = ahora
    except Exception:
        print("[ERROR] Falló el hilo de detección:")
        traceback.print_exc()
        detener.set()


def procesar_fuente(lanzar_lector, publicador, mostrar=True):
    """
    lanzar_lector(destino, detener, fin): función que corre en un hilo y va dejando
                  en 'destino' tuplas (t_segundos, frame_reducido).
    Los carriles ya deben estar definidos antes de llamar a esta función.
    """
    frames = UltimoValor()
    resultado = UltimoValor()
    detener = threading.Event()
    fin = threading.Event()

    h_lector = threading.Thread(target=lanzar_lector, args=(frames, detener, fin), daemon=True)
    h_inf = threading.Thread(target=_hilo_inferencia,
                             args=(frames, publicador, resultado, detener),
                             daemon=True)
    h_lector.start()
    h_inf.start()

    ver_f = ver_r = 0
    ultimo_res = ([], {c: 0 for c in CLASES_VEHICULO}, 0, None, {n: 0 for n in CARRILES})
    try:
        if not mostrar:
            # Sin ventana: el hilo principal solo espera mientras trabajan los demás hilos
            print("[INFO] Corriendo SIN pantalla. Ctrl+C para detener.")
            try:
                while not detener.is_set() and not fin.is_set():
                    time.sleep(0.2)
            except KeyboardInterrupt:
                print("\n[INFO] Detenido por el usuario.")
        while mostrar and not detener.is_set():
            dato, ver_f = frames.tomar(ver_f)
            if dato is None:
                if fin.is_set():
                    break
                if mostrar:
                    if cv2.waitKey(3) & 0xFF == ord('q'):
                        break
                else:
                    time.sleep(0.005)
                continue

            r, ver_r = resultado.tomar(ver_r)
            if r is not None:
                ultimo_res = r

            if mostrar:
                _, frame = dato
                cv2.imshow("Conteo por carril (q = salir)", dibujar(frame, *ultimo_res))
                if cv2.waitKey(1) & 0xFF == ord('q'):
                    break
    finally:
        detener.set()
        h_lector.join(timeout=2)
        h_inf.join(timeout=3)
        try:
            cv2.destroyAllWindows()
        except cv2.error:
            pass                      # OpenCV sin soporte de ventanas: no hay nada que cerrar


# =============================================================================
# ENTRADA: CÁMARA IP (celular con IP Webcam, cámara falsa o cámara de seguridad real)
# Usa la clase CamaraIP del módulo v1_camara.py (debe estar en la misma carpeta).
# =============================================================================
def _hilo_camara_ip(cam, destino, detener, fin):
    """Pasa siempre el frame más reciente de la cámara IP al sistema de detección."""
    ver = 0
    try:
        while not detener.is_set():
            frame, ver = cam.leer(ver)
            if frame is None:
                time.sleep(0.005)
                continue
            destino.poner((time.time(), _reducir(frame)))
    finally:
        cam.detener()
        fin.set()


def modo_camara_ip(publicador, entrada=None, espera_max=20.0, mostrar=False):
    entrada = entrada or input("IP del celular / cámara (o URL completa del stream): ").strip()
    if not entrada:
        print("No ingresaste ninguna IP/URL.")
        return
    cam = CamaraIP(armar_url(entrada)).iniciar()

    # esperar el primer frame (sirve también para dibujar los carriles)
    print(f"[CAMARA] Esperando video (máx. {espera_max:.0f} s)...")
    t0 = time.time()
    primer, _ = cam.leer(0)
    while primer is None and time.time() - t0 < espera_max:
        time.sleep(0.1)
        primer, _ = cam.leer(0)
    if primer is None:
        print("[CAMARA] No llegó ningún frame. Revisa la IP, que ambos estén en la misma red "
              "y que el servidor de la cámara esté iniciado.")
        cam.detener()
        return

    definir_carriles(_reducir(primer))       # <- te pide los carriles cada vez que inicias

    procesar_fuente(lambda destino, detener, fin: _hilo_camara_ip(cam, destino, detener, fin),
                    publicador, mostrar=mostrar)


# =============================================================================
# MAIN
# =============================================================================
if __name__ == "__main__":
    pub = Publicador()
    pub.suscribir_rl(enviar_a_rl)     # <- aquí conectas tu agente RL

    # python script.py 192.168.1.33:8080      (también vale: --camara 192.168.1.33:8080)
    args = [a for a in sys.argv[1:] if a not in ("--camara", "--pantalla")]
    mostrar = MOSTRAR_VIDEO or ("--pantalla" in sys.argv)
    modo_camara_ip(pub, args[0] if args else None, mostrar=mostrar)