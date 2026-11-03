# =============================================================================
# conteo_vehiculos.py  (versión para Raspberry Pi / Linux)
# Detección, conteo y velocidad de vehículos EN LA ZONA DE LA PISTA con YOLO (Ultralytics)
# Entradas:  A) video (ruta por argumento o explorador)   |   B) pantalla compartida (opcional)
# Salidas:   C) datos hacia el algoritmo de RL y hacia la interfaz gráfica
#
# Uso:
#     python conteo_vehiculos.py video_prueba.mp4
#     python conteo_vehiculos.py                      (te pregunta la fuente)
#
# Requisitos: pip install ultralytics opencv-python numpy
#             (mss es opcional: solo se usa en el modo pantalla)
#             sudo apt install python3-tk libgl1      (tkinter es opcional)
# =============================================================================
import os
import sys

# En Linux (Raspberry Pi OS con Wayland) OpenCV necesita el plugin xcb para abrir ventanas.
# Debe definirse ANTES de importar cv2.
if sys.platform.startswith("linux"):
    os.environ.setdefault("QT_QPA_PLATFORM", "xcb")

import math
import time
import queue
from collections import deque

import cv2
import numpy as np
from ultralytics import YOLO

# tkinter y mss son opcionales: si no están, el script sigue funcionando
try:
    import tkinter as tk
    from tkinter import filedialog
except ImportError:
    tk = None
    filedialog = None

try:
    import mss
except ImportError:
    mss = None

# -----------------------------------------------------------------------------
# CONFIGURACIÓN GENERAL
# -----------------------------------------------------------------------------
# Puedes poner el .pt, o la carpeta del modelo exportado a NCNN (más rápido en Raspberry):
#   RUTA_MODELO = "modelo_combinado_ncnn_model"
RUTA_MODELO = "modelo_combinado.pt"
CONF = 0.4
INTERVALO_ENVIO = 0.5   # segundos entre cada envío de datos al RL y a la GUI

# -----------------------------------------------------------------------------
# CONFIGURACIÓN DEL PREPROCESAMIENTO (activa/desactiva cada paso para comparar)
# -----------------------------------------------------------------------------
PRE_ROI           = True    # recorta el frame al rectángulo que envuelve la zona
PRE_MARGEN_ROI    = 20      # píxeles extra alrededor de la zona
PRE_MASCARA       = False   # True = pinta de negro lo que está fuera del polígono
PRE_REDIMENSIONAR = True    # reduce si el lado mayor supera PRE_LADO_MAX
PRE_LADO_MAX      = 416     # Raspberry: 416 o 480. En PC potente puedes subir a 640
PRE_BLUR          = False   # desenfoque gaussiano 3x3 (solo si hay mucho ruido)
PRE_CLAHE         = False   # mejora de contraste en luminancia (poca luz / sobreexposición)

IMGSZ = PRE_LADO_MAX        # tamaño de inferencia de YOLO
# Si exportas a NCNN, usa el mismo imgsz en la exportación:
#   YOLO("modelo_combinado.pt").export(format="ncnn", imgsz=416)
_clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))


model = YOLO(RUTA_MODELO, task="detect")   # carga una sola vez
CLASES = list(model.names.values())        # las clases reales del modelo

# Agrupación para el estado del RL, usando los nombres EXACTOS del modelo.
# El orden de este diccionario define el orden del vector que recibe el agente.
"""
GRUPOS = {
    "livianos":   ['car', 'taxi', 'suv', 'pickup', 'camioneta', 'minivan', 'van', 'policecar'],
    "motos":      ['motorbike', 'scooter', 'bicimoto', 'trimoto', 'mototaxi',
                   'auto rickshaw', 'three wheelers -CNG-'],
    "transporte": ['bus', 'minibus', 'omnibus', 'omnibus-articulado', 'human hauler'],
    "pesados":    ['truck', 'semi-trailer', 'trailer', 'garbagevan'],
    "especiales": ['ambulance', 'army vehicle', 'especiales'],
    "otros":      ['bicycle', 'rickshaw', 'wheelbarrow', 'peaton'],  # no cuentan como vehículo
}
GRUPOS_QUE_NO_SON_VEHICULO = {"otros"}
"""
GRUPOS = {
    "transportistas": ['car', 'taxi', 'suv', 'pickup', 'camioneta', 'minivan', 'van', 'policecar',
                       'motorbike', 'scooter', 'bicimoto', 'trimoto', 'mototaxi',
                       'auto rickshaw', 'three wheelers -CNG-',
                       'bus', 'minibus', 'omnibus', 'omnibus-articulado', 'human hauler',
                       'truck', 'semi-trailer', 'trailer', 'garbagevan',
                       'ambulance', 'army vehicle', 'especiales'],
    "otros":          ['bicycle', 'rickshaw', 'wheelbarrow', 'peaton'],  # no cuentan como vehículo
}
GRUPOS_QUE_NO_SON_VEHICULO = {"otros"}


# Aviso si alguna clase del modelo no quedó en ningún grupo (así no se pierde en silencio)
_en_grupos = {c for cls in GRUPOS.values() for c in cls}
_sin_grupo = [c for c in CLASES if c not in _en_grupos]
if _sin_grupo:
    print(f"[AVISO] Clases sin grupo (no se contarán): {_sin_grupo}")


# -----------------------------------------------------------------------------
# MEDICIÓN DE VELOCIDAD
# -----------------------------------------------------------------------------
VEL_ACTIVAR     = True
VEL_VENTANA_S   = 0.8    # segundos de historial usados para calcular la velocidad
VEL_MIN_DT_S    = 0.3    # tiempo mínimo entre primera y última muestra
VEL_SUAVIZADO   = 0.4    # 0-1: peso de la medición nueva (menor = más estable)
VEL_UMBRAL_KMH  = 3      # por debajo de esto se muestra 0 (carro detenido / ruido de la caja)
VEL_MAX_KMH     = 120    # mediciones mayores se descartan como ruido
VEL_OLVIDAR_S   = 2.0    # segundos sin ver un id antes de borrar su historial

# clases que no son vehículos (no se les mide velocidad)
NO_VEHICULO = {c for g in GRUPOS_QUE_NO_SON_VEHICULO for c in GRUPOS[g]}


class MedidorVelocidad:
    def __init__(self):
        self.H = None            # homografía píxeles -> metros
        self.fps_video = None    # fps del video (None = usar reloj real, p. ej. pantalla)
        self.reiniciar()

    @property
    def listo(self):
        return self.H is not None

    def reiniciar(self):
        self.hist = {}    # id -> deque[(t, X_m, Y_m)]
        self.vel = {}     # id -> velocidad suavizada (km/h)
        self.visto = {}   # id -> último instante visto
        self.pos = {}     # id -> (X_m, Y_m) de los vehículos del frame actual
        self._n = 0       # contador de frames

    def _a_metros(self, x, y):
        p = cv2.perspectiveTransform(np.array([[[x, y]]], dtype=np.float32), self.H)[0][0]
        return float(p[0]), float(p[1])

    def actualizar(self, mediciones):
        """mediciones: lista de (id, x_px, y_px) con el punto de contacto con el suelo."""
        self._n += 1
        self.pos = {}
        # video: el tiempo sale de los fps (así no importa qué tan lento procese la máquina)
        # pantalla: se usa el reloj real
        t = self._n / self.fps_video if self.fps_video else time.time()

        for tid, x, y in mediciones:
            X, Y = self._a_metros(x, y)
            self.pos[tid] = (X, Y)
            h = self.hist.setdefault(tid, deque())
            h.append((t, X, Y))
            self.visto[tid] = t
            while h and t - h[0][0] > VEL_VENTANA_S:
                h.popleft()
            if len(h) >= 2 and h[-1][0] - h[0][0] >= VEL_MIN_DT_S:
                t0, X0, Y0 = h[0]
                v = math.hypot(X - X0, Y - Y0) / (t - t0) * 3.6   # m/s -> km/h
                if v <= VEL_MAX_KMH:
                    prev = self.vel.get(tid)
                    self.vel[tid] = v if prev is None else VEL_SUAVIZADO * v + (1 - VEL_SUAVIZADO) * prev

        for tid in [k for k, tv in self.visto.items() if t - tv > VEL_OLVIDAR_S]:
            self.hist.pop(tid, None)
            self.vel.pop(tid, None)
            self.visto.pop(tid, None)

    def velocidad(self, tid):
        """km/h del vehículo, o None si todavía no hay suficientes muestras."""
        v = self.vel.get(tid)
        if v is None:
            return None
        return 0.0 if v < VEL_UMBRAL_KMH else v


# -----------------------------------------------------------------------------
# DISTANCIA ENTRE VEHÍCULOS (usa la misma calibración que la velocidad)
# -----------------------------------------------------------------------------
DIST_ACTIVAR = True
DIST_MAX_M   = 30    # no se muestran pares separados por más de esta distancia (metros)


def distancias_vehiculos(posiciones):
    """
    posiciones: dict id -> (X_m, Y_m), punto de contacto de cada vehículo con el suelo.
    Devuelve una lista de (id_a, id_b, distancia_m): para cada vehículo, su vecino más
    cercano (si dos vehículos son vecinos mutuos, el par aparece una sola vez).
    """
    pares = {}
    for a, (xa, ya) in posiciones.items():
        mejor = None
        for b, (xb, yb) in posiciones.items():
            if a == b:
                continue
            d = math.hypot(xa - xb, ya - yb)
            if mejor is None or d < mejor[1]:
                mejor = (b, d)
        if mejor is not None and mejor[1] <= DIST_MAX_M:
            pares[tuple(sorted((a, mejor[0])))] = mejor[1]
    return [(a, b, d) for (a, b), d in pares.items()]


MEDIDOR = MedidorVelocidad()


def calibrar_velocidad(frame):
    """
    Marca 4 puntos sobre el PAVIMENTO que formen un rectángulo de medidas reales conocidas.
    Orden (como se ve en la imagen): 1 sup-izq, 2 sup-der, 3 inf-der, 4 inf-izq.
    Luego escribe en la consola las medidas: lado 1-2 (ancho) y lado 2-3 (largo), en metros.
    ENTER = confirmar (con 4 puntos), R = reiniciar, ESC = sin medición de velocidad.
    """
    puntos = []

    def on_click(event, x, y, flags, param):
        if event == cv2.EVENT_LBUTTONDOWN and len(puntos) < 4:
            puntos.append((x, y))

    win = "Calibracion velocidad: 4 puntos en el suelo (ENTER=ok, R=reiniciar, ESC=omitir)"
    cv2.namedWindow(win)
    cv2.setMouseCallback(win, on_click)
    while True:
        vista = frame.copy()
        for i, p in enumerate(puntos):
            cv2.circle(vista, p, 5, (0, 0, 255), -1)
            cv2.putText(vista, str(i + 1), (p[0] + 8, p[1] - 8),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 0, 255), 2)
        if len(puntos) > 1:
            cv2.polylines(vista, [np.array(puntos, dtype=np.int32)], len(puntos) == 4, (255, 0, 255), 2)
        cv2.imshow(win, vista)
        k = cv2.waitKey(30) & 0xFF
        if k == 13 and len(puntos) == 4:
            break
        if k in (ord('r'), ord('R')):
            puntos.clear()
        if k == 27:
            cv2.destroyWindow(win)
            MEDIDOR.H = None
            print("[VEL] Calibración omitida: no se medirá velocidad.")
            return False
    cv2.destroyWindow(win)
    cv2.waitKey(1)

    try:
        ancho = float(input("Medida real del lado 1-2 (metros): ").replace(",", "."))
        largo = float(input("Medida real del lado 2-3 (metros): ").replace(",", "."))
    except ValueError:
        MEDIDOR.H = None
        print("[VEL] Medida no válida: no se medirá velocidad.")
        return False

    src = np.array(puntos, dtype=np.float32)
    dst = np.array([(0, 0), (ancho, 0), (ancho, largo), (0, largo)], dtype=np.float32)
    MEDIDOR.H = cv2.getPerspectiveTransform(src, dst)
    print("[VEL] Calibración lista.")
    return True


# -----------------------------------------------------------------------------
# ETAPA DE PREPROCESAMIENTO DE IMAGEN
# -----------------------------------------------------------------------------
def preprocesar(frame, zona=None):
    """
    Devuelve (imagen_preprocesada, (x0, y0, escala)).
    (x0, y0) y escala permiten volver las cajas a coordenadas del frame original.
    """
    h, w = frame.shape[:2]
    x0, y0 = 0, 0
    img = frame

    # 1) ROI: recorte al rectángulo que envuelve la zona de la pista
    if PRE_ROI and zona:
        pts = np.array(zona, dtype=np.int32)
        bx, by, bw, bh = cv2.boundingRect(pts)
        x0 = max(bx - PRE_MARGEN_ROI, 0)
        y0 = max(by - PRE_MARGEN_ROI, 0)
        x1 = min(bx + bw + PRE_MARGEN_ROI, w)
        y1 = min(by + bh + PRE_MARGEN_ROI, h)
        img = frame[y0:y1, x0:x1]

    # 1b) Máscara opcional: negro fuera del polígono
    if PRE_MASCARA and zona:
        mascara = np.zeros(img.shape[:2], dtype=np.uint8)
        pts_local = np.array(zona, dtype=np.int32) - np.array([x0, y0], dtype=np.int32)
        cv2.fillPoly(mascara, [pts_local], 255)
        img = cv2.bitwise_and(img, img, mask=mascara)

    # 2) Redimensionado (solo reduce, nunca agranda)
    escala = 1.0
    if PRE_REDIMENSIONAR:
        lado = max(img.shape[:2])
        if lado > PRE_LADO_MAX:
            escala = PRE_LADO_MAX / lado
            img = cv2.resize(img, None, fx=escala, fy=escala, interpolation=cv2.INTER_AREA)

    # 3) Reducción de ruido (opcional)
    if PRE_BLUR:
        img = cv2.GaussianBlur(img, (3, 3), 0)

    # 4) CLAHE sobre la luminancia (opcional); se conservan los 3 canales de color
    if PRE_CLAHE:
        l, a, b = cv2.split(cv2.cvtColor(img, cv2.COLOR_BGR2LAB))
        img = cv2.cvtColor(cv2.merge((_clahe.apply(l), a, b)), cv2.COLOR_LAB2BGR)

    # La normalización 0-1 y el letterbox los hace YOLO internamente.
    return img, (x0, y0, escala)


# -----------------------------------------------------------------------------
# NÚCLEO: detección + conteo + velocidad dentro de la zona (lo usan A y B)
# -----------------------------------------------------------------------------
def contar_vehiculos(frame, zona=None, conf=CONF):
    """
    frame: imagen BGR (original, sin preprocesar)
    zona:  polígono [(x,y), ...] de la pista, en coordenadas del frame original
    return: (conteo_por_clase, conteo_por_grupo, total_vehiculos, boxes_en_zona, pares)
    pares: ((x_a, y_a), (x_b, y_b), distancia_m) entre cada vehículo y su vecino más cercano
    boxes_en_zona: (x1, y1, x2, y2, nombre_clase, velocidad_kmh o None) en coords del frame ORIGINAL
    """
    img, (x0, y0, escala) = preprocesar(frame, zona)          # <- PREPROCESO

    if VEL_ACTIVAR and MEDIDOR.listo:
        res = model.track(img, persist=True, conf=conf, imgsz=IMGSZ,
                          tracker="bytetrack.yaml", verbose=False)[0]
    else:
        res = model.predict(img, conf=conf, imgsz=IMGSZ, verbose=False)[0]

    ids = res.boxes.id
    ids = ids.int().tolist() if ids is not None else [None] * len(res.boxes)
    poligono = np.array(zona, dtype=np.int32) if zona else None

    por_clase = {c: 0 for c in CLASES}
    detectados = []      # (x1, y1, x2, y2, nombre, id)
    mediciones = []      # (id, x_suelo, y_suelo) para el medidor

    for box, tid in zip(res.boxes, ids):
        x1, y1, x2, y2 = box.xyxy[0].tolist()
        nombre = res.names[int(box.cls[0])]

        # volver de la imagen preprocesada al frame original
        x1, x2 = x1 / escala + x0, x2 / escala + x0
        y1, y2 = y1 / escala + y0, y2 / escala + y0

        punto = (float((x1 + x2) / 2), float(y2))   # contacto con el suelo
        if poligono is not None:
            if cv2.pointPolygonTest(poligono, punto, False) < 0:
                continue

        por_clase[nombre] = por_clase.get(nombre, 0) + 1
        detectados.append((int(x1), int(y1), int(x2), int(y2), nombre, tid))
        if tid is not None and nombre not in NO_VEHICULO:
            mediciones.append((tid, punto[0], punto[1]))

    if MEDIDOR.listo:
        MEDIDOR.actualizar(mediciones)

    pares = []   # ((x_a, y_a), (x_b, y_b), distancia_m) en píxeles del frame original
    if DIST_ACTIVAR and MEDIDOR.listo:
        pix = {tid: (x, y) for tid, x, y in mediciones}
        for a, b, d in distancias_vehiculos(MEDIDOR.pos):
            pares.append((pix[a], pix[b], d))

    boxes_en_zona = [
        (x1, y1, x2, y2, nombre,
         MEDIDOR.velocidad(tid) if (MEDIDOR.listo and tid is not None and nombre not in NO_VEHICULO) else None)
        for x1, y1, x2, y2, nombre, tid in detectados
    ]

    por_grupo = {g: sum(por_clase.get(c, 0) for c in cls) for g, cls in GRUPOS.items()}
    total = sum(n for g, n in por_grupo.items() if g not in GRUPOS_QUE_NO_SON_VEHICULO)
    return por_clase, por_grupo, total, boxes_en_zona, pares


def dibujar(frame, zona, boxes, por_grupo, total, pares=()):
    """Dibuja la zona, las cajas (con velocidad) y el conteo sobre el frame."""
    out = frame.copy()
    if zona:
        cv2.polylines(out, [np.array(zona, dtype=np.int32)], True, (0, 255, 255), 2)
    for x1, y1, x2, y2, nombre, vel in boxes:
        cv2.rectangle(out, (x1, y1), (x2, y2), (0, 255, 0), 2)
        etiqueta = nombre if vel is None else f"{nombre} {vel:.0f} km/h"
        cv2.putText(out, etiqueta, (x1, max(y1 - 5, 12)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 1)
    for (xa, ya), (xb, yb), d in pares:
        pa, pb = (int(xa), int(ya)), (int(xb), int(yb))
        cv2.line(out, pa, pb, (255, 0, 255), 2)
        cv2.putText(out, f"{d:.1f} m", ((pa[0] + pb[0]) // 2, (pa[1] + pb[1]) // 2),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 0, 255), 2)
    y = 25
    cv2.putText(out, f"TOTAL: {total}", (10, y), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 0, 255), 2)
    for g, n in por_grupo.items():
        y += 25
        cv2.putText(out, f"{g}: {n}", (10, y), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 2)
    return out


def seleccionar_zona(frame):
    """
    Haz clic en los vértices de la pista. ENTER = terminar, R = reiniciar, ESC = sin zona.
    Devuelve la lista de puntos [(x,y), ...] o None.
    """
    puntos = []

    def on_click(event, x, y, flags, param):
        if event == cv2.EVENT_LBUTTONDOWN:
            puntos.append((x, y))

    win = "Define la zona de la pista (ENTER=ok, R=reiniciar, ESC=todo el frame)"
    cv2.namedWindow(win)
    cv2.setMouseCallback(win, on_click)
    while True:
        vista = frame.copy()
        for p in puntos:
            cv2.circle(vista, p, 4, (0, 0, 255), -1)
        if len(puntos) > 1:
            cv2.polylines(vista, [np.array(puntos, dtype=np.int32)], len(puntos) > 2, (0, 255, 255), 2)
        cv2.imshow(win, vista)
        k = cv2.waitKey(30) & 0xFF
        if k == 13 and len(puntos) >= 3:   # ENTER
            break
        if k in (ord('r'), ord('R')):
            puntos.clear()
        if k == 27:                         # ESC
            puntos = []
            break
    cv2.destroyWindow(win)
    return puntos or None


# =============================================================================
# PARTE C: ENVÍO DE DATOS AL ALGORITMO DE RL Y A LA INTERFAZ GRÁFICA
# =============================================================================
class Publicador:
    """
    Se encarga de mandar los conteos a quien se suscriba.
    Cuando conectes con tu otro código, solo registras tus funciones:
        pub.suscribir_rl(mi_funcion_rl)
        pub.suscribir_gui(mi_funcion_gui)
    """

    def __init__(self):
        self._rl = []
        self._gui = []
        # Alternativa por cola (útil si el RL/GUI corren en otro hilo):
        self.cola_rl = queue.Queue(maxsize=1)   # guarda solo el estado más reciente
        self.cola_gui = queue.Queue(maxsize=1)

    def suscribir_rl(self, fn):
        self._rl.append(fn)

    def suscribir_gui(self, fn):
        self._gui.append(fn)

    @staticmethod
    def _poner_ultimo(cola, dato):
        """Reemplaza el dato viejo por el nuevo (el RL siempre quiere el estado actual)."""
        if cola.full():
            try:
                cola.get_nowait()
            except queue.Empty:
                pass
        cola.put_nowait(dato)

    def publicar(self, por_clase, por_grupo, total, fuente):
        # --- datos para el RL: vector numérico en orden fijo ---
        estado_rl = np.array([por_grupo[g] for g in GRUPOS], dtype=np.float32)

        # --- datos para la GUI: diccionario completo y legible ---
        datos_gui = {
            "timestamp": time.time(),
            "fuente": fuente,            # "video" o "pantalla"
            "total_vehiculos": total,
            "por_grupo": por_grupo,
            "por_clase": por_clase,
        }

        self._poner_ultimo(self.cola_rl, estado_rl)
        self._poner_ultimo(self.cola_gui, datos_gui)
        for fn in self._rl:
            fn(estado_rl)
        for fn in self._gui:
            fn(datos_gui)


# Funciones de prueba: reemplázalas por las reales de tu otro código
def enviar_a_rl(estado):
    print(f"[RL ] estado = {estado.tolist()}  (orden: {list(GRUPOS)})")

def enviar_a_gui(datos):
    print(f"[GUI] total = {datos['total_vehiculos']} | {datos['por_grupo']}")


# =============================================================================
# BUCLE COMÚN: procesa frames de cualquier fuente y publica los datos
# =============================================================================
def procesar_fuente(generador_frames, fuente, publicador, mostrar=True):
    zona = None
    ultimo_envio = 0.0
    MEDIDOR.reiniciar()            # limpia historial de velocidades
    model.predictor = None         # reinicia el tracker entre fuentes

    for i, frame in enumerate(generador_frames):
        if i == 0:
            zona = seleccionar_zona(frame)      # la zona se define con el primer frame
            if VEL_ACTIVAR:
                calibrar_velocidad(frame)       # calibración píxeles -> metros

        por_clase, por_grupo, total, boxes, pares = contar_vehiculos(frame, zona)

        ahora = time.time()
        if ahora - ultimo_envio >= INTERVALO_ENVIO:
            publicador.publicar(por_clase, por_grupo, total, fuente)
            ultimo_envio = ahora

        if mostrar:
            cv2.imshow("Conteo (q = salir)", dibujar(frame, zona, boxes, por_grupo, total, pares))
            if cv2.waitKey(1) & 0xFF == ord('q'):
                break
    cv2.destroyAllWindows()


# =============================================================================
# PARTE A: SUBIR VIDEO
# =============================================================================
def elegir_video():
    """Abre un explorador de archivos; si no hay tkinter, pide la ruta por consola."""
    if tk is None:
        return input("Ruta del video: ").strip()
    root = tk.Tk()
    root.withdraw()
    ruta = filedialog.askopenfilename(
        title="Selecciona un video",
        filetypes=[("Videos", "*.mp4 *.avi *.mov *.mkv"), ("Todos", "*.*")])
    root.destroy()
    return ruta


def frames_de_video(ruta):
    cap = cv2.VideoCapture(ruta)
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        yield frame
    cap.release()


def modo_video(publicador, ruta=None):
    ruta = ruta or elegir_video()
    if not ruta:
        print("No se seleccionó ningún video.")
        return
    if not os.path.exists(ruta):
        print(f"No se encontró el video: {ruta}")
        return
    cap = cv2.VideoCapture(ruta)                            # fps del video para calcular velocidad
    MEDIDOR.fps_video = cap.get(cv2.CAP_PROP_FPS) or None
    cap.release()
    procesar_fuente(frames_de_video(ruta), "video", publicador)


# =============================================================================
# PARTE B: COMPARTIR PANTALLA (requiere mss; en la Raspberry normalmente no se usa)
# =============================================================================
def frames_de_pantalla(monitor_idx=1, region=None):
    """
    monitor_idx: 1 = monitor principal, 2 = segundo monitor, ...
    region: dict {"left":..,"top":..,"width":..,"height":..} para capturar solo
            la ventana donde reproduces el video (None = monitor completo)
    """
    with mss.mss() as sct:
        monitor = region or sct.monitors[monitor_idx]
        while True:
            img = np.array(sct.grab(monitor))      # BGRA
            yield cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)


def elegir_region_pantalla(monitor_idx=1):
    """Captura una imagen de la pantalla y deja dibujar el recuadro donde se ve el video."""
    with mss.mss() as sct:
        mon = sct.monitors[monitor_idx]
        img = cv2.cvtColor(np.array(sct.grab(mon)), cv2.COLOR_BGRA2BGR)
    x, y, w, h = cv2.selectROI("Selecciona el area del video (ENTER=ok)", img, False)
    cv2.destroyAllWindows()
    if w == 0 or h == 0:
        return None
    return {"left": mon["left"] + int(x), "top": mon["top"] + int(y),
            "width": int(w), "height": int(h)}


def modo_pantalla(publicador, monitor_idx=1, usar_region=True):
    if mss is None:
        print("El modo pantalla necesita 'mss' (pip install mss). Usa el modo video.")
        return
    MEDIDOR.fps_video = None                                # en pantalla se usa el reloj real
    region = elegir_region_pantalla(monitor_idx) if usar_region else None
    procesar_fuente(frames_de_pantalla(monitor_idx, region), "pantalla", publicador)


# =============================================================================
# MAIN
# =============================================================================
if __name__ == "__main__":
    pub = Publicador()
    pub.suscribir_rl(enviar_a_rl)     # <- aquí conectas tu agente RL
    pub.suscribir_gui(enviar_a_gui)   # <- aquí conectas tu interfaz gráfica

    if len(sys.argv) > 1:
        # python conteo_vehiculos.py video.mp4
        modo_video(pub, sys.argv[1])
    else:
        modo = input("Fuente [1 = subir video, 2 = compartir pantalla]: ").strip()
        if modo == "1":
            modo_video(pub)
        else:
            modo_pantalla(pub)