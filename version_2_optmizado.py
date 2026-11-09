# =============================================================================
# conteo_vehiculos_rpi.py  (OPTIMIZADO para Raspberry Pi 4 con 4 GB de RAM)
# Detección, conteo y velocidad de vehículos EN LA ZONA DE LA PISTA con YOLO (Ultralytics)
# Entradas:  A) video (ruta por argumento o explorador)   |   B) pantalla compartida (opcional)
# Salidas:   C) datos hacia el algoritmo de RL y hacia la interfaz gráfica
#
# Uso:
#     python conteo_vehiculos_rpi.py video_prueba.mp4
#     python conteo_vehiculos_rpi.py                      (te pregunta la fuente)
#
# QUÉ CAMBIÓ PARA IR MÁS RÁPIDO (resumen):
#   1) Usa un modelo NCNN ya exportado (carpeta *_ncnn_model), más rápido que .pt en CPU ARM.
#   2) Tamaño de inferencia más chico (IMGSZ = 320).
#   3) Todo el procesamiento y la ventana trabajan sobre un frame reducido (LADO_TRABAJO).
#   4) Tres hilos: lector de video | detección YOLO | ventana. El video ya no se frena
#      esperando a YOLO: se ve fluido y a velocidad real, y las cajas se actualizan
#      cada vez que YOLO termina un frame.
#   5) El video avanza en TIEMPO REAL (si la máquina no alcanza, se saltan frames en
#      vez de verse en cámara lenta). La velocidad se calcula con el tiempo del video,
#      así que sigue siendo correcta aunque se salten frames.
#   6) Menos trabajo por frame: max_det limitado y lectura de cajas en bloque.
#
# Requisitos: pip install ultralytics opencv-python numpy   (torch en su versión CPU)
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
import threading
import traceback
from collections import deque

import cv2
import numpy as np
import yaml
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
# Carpeta del modelo NCNN que ya exportaste (junto a este script, o pon la ruta completa).
RUTA_MODELO = "modelo_combinado_ncnn_model"
CONF = 0.4
MAX_DET = 50                          # máximo de detecciones por frame (menos trabajo en NMS)
INTERVALO_ENVIO = 0.5                 # segundos entre cada envío de datos al RL y a la GUI

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


# Tamaños (las perillas principales de velocidad):
# IMGSZ se toma automáticamente del modelo NCNN (el que usaste al exportar: 320, 416...).
# Un modelo NCNN solo funciona bien con el tamaño con el que fue exportado.
IMGSZ        = _imgsz_del_modelo(RUTA_MODELO, 320)
LADO_TRABAJO = 640   # el frame se reduce a este lado mayor ANTES de todo. Sube a 800-960
                     # solo si pierdes vehículos lejanos; bájalo a 480 para ir aún más rápido

# -----------------------------------------------------------------------------
# CONFIGURACIÓN DEL PREPROCESAMIENTO (activa/desactiva cada paso para comparar)
# -----------------------------------------------------------------------------
PRE_ROI           = True    # recorta el frame al rectángulo que envuelve la zona
PRE_MARGEN_ROI    = 20      # píxeles extra alrededor de la zona
PRE_MASCARA       = False   # True = pinta de negro lo que está fuera del polígono
PRE_REDIMENSIONAR = True    # reduce si el lado mayor supera PRE_LADO_MAX
PRE_LADO_MAX      = IMGSZ   # el recorte se reduce a este tamaño (igual que IMGSZ)
PRE_BLUR          = False   # desenfoque gaussiano 3x3 (solo si hay mucho ruido)
PRE_CLAHE         = False   # mejora de contraste en luminancia (poca luz / sobreexposición)

_clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))


# -----------------------------------------------------------------------------
# CARGA DEL MODELO (NCNN ya exportado)
# -----------------------------------------------------------------------------
if not os.path.exists(RUTA_MODELO):
    sys.exit(f"[ERROR] No encuentro el modelo: {RUTA_MODELO}\n"
             f"        Ejecuta el script desde la carpeta donde está, o pon la ruta completa en RUTA_MODELO.")
print(f"[MODELO] {RUTA_MODELO} | imgsz={IMGSZ}")
model = YOLO(RUTA_MODELO, task="detect")   # carga una sola vez
CLASES = list(model.names.values())                                # las clases reales del modelo

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
# Como la detección corre a pocos cuadros por segundo en la Raspberry, la ventana de
# tiempo es un poco más larga que en la versión para PC (así hay suficientes muestras).
VEL_ACTIVAR     = True
VEL_VENTANA_S   = 1.0    # segundos de historial usados para calcular la velocidad
VEL_MIN_DT_S    = 0.4    # tiempo mínimo entre primera y última muestra
VEL_SUAVIZADO   = 0.4    # 0-1: peso de la medición nueva (menor = más estable)
VEL_UMBRAL_KMH  = 3      # por debajo de esto se muestra 0 (carro detenido / ruido de la caja)
VEL_MAX_KMH     = 120    # mediciones mayores se descartan como ruido
VEL_OLVIDAR_S   = 2.0    # segundos sin ver un id antes de borrar su historial

# clases que no son vehículos (no se les mide velocidad)
NO_VEHICULO = {c for g in GRUPOS_QUE_NO_SON_VEHICULO for c in GRUPOS[g]}


class MedidorVelocidad:
    def __init__(self):
        self.H = None            # homografía píxeles -> metros
        self.reiniciar()

    @property
    def listo(self):
        return self.H is not None

    def reiniciar(self):
        self.hist = {}    # id -> deque[(t, X_m, Y_m)]
        self.vel = {}     # id -> velocidad suavizada (km/h)
        self.visto = {}   # id -> último instante visto
        self.pos = {}     # id -> (X_m, Y_m) de los vehículos del frame actual

    def _a_metros(self, x, y):
        p = cv2.perspectiveTransform(np.array([[[x, y]]], dtype=np.float32), self.H)[0][0]
        return float(p[0]), float(p[1])

    def actualizar(self, mediciones, t):
        """
        mediciones: lista de (id, x_px, y_px) con el punto de contacto con el suelo.
        t: instante del frame en segundos (video: indice/fps; pantalla: reloj real).
        """
        self.pos = {}

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
    (x0, y0) y escala permiten volver las cajas a coordenadas del frame de trabajo.
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
def contar_vehiculos(frame, zona=None, t=0.0, conf=CONF):
    """
    frame: imagen BGR (ya reducida a LADO_TRABAJO, sin preprocesar)
    zona:  polígono [(x,y), ...] de la pista, en coordenadas de ese mismo frame
    t:     instante del frame en segundos (para la velocidad)
    return: (conteo_por_clase, conteo_por_grupo, total_vehiculos, boxes_en_zona, pares)
    pares: ((x_a, y_a), (x_b, y_b), distancia_m) entre cada vehículo y su vecino más cercano
    boxes_en_zona: (x1, y1, x2, y2, nombre_clase, velocidad_kmh o None) en coords del frame
    """
    img, (x0, y0, escala) = preprocesar(frame, zona)          # <- PREPROCESO

    if VEL_ACTIVAR and MEDIDOR.listo:
        res = model.track(img, persist=True, conf=conf, imgsz=IMGSZ, max_det=MAX_DET,
                          tracker="bytetrack.yaml", verbose=False)[0]
    else:
        res = model.predict(img, conf=conf, imgsz=IMGSZ, max_det=MAX_DET, verbose=False)[0]

    # Lectura de todas las cajas en bloque (más rápido que caja por caja)
    n = len(res.boxes)
    if n:
        xyxy = res.boxes.xyxy.cpu().numpy()
        clases = res.boxes.cls.int().cpu().tolist()
        ids = res.boxes.id.int().cpu().tolist() if res.boxes.id is not None else [None] * n
    else:
        xyxy, clases, ids = [], [], []
    poligono = np.array(zona, dtype=np.int32) if zona else None

    por_clase = {c: 0 for c in CLASES}
    detectados = []      # (x1, y1, x2, y2, nombre, id)
    mediciones = []      # (id, x_suelo, y_suelo) para el medidor

    for (x1, y1, x2, y2), c, tid in zip(xyxy, clases, ids):
        nombre = res.names[c]

        # volver de la imagen preprocesada al frame de trabajo
        x1, x2 = float(x1) / escala + x0, float(x2) / escala + x0
        y1, y2 = float(y1) / escala + y0, float(y2) / escala + y0

        punto = (float((x1 + x2) / 2), float(y2))   # contacto con el suelo
        if poligono is not None:
            if cv2.pointPolygonTest(poligono, punto, False) < 0:
                continue

        por_clase[nombre] = por_clase.get(nombre, 0) + 1
        detectados.append((int(x1), int(y1), int(x2), int(y2), nombre, tid))
        if tid is not None and nombre not in NO_VEHICULO:
            mediciones.append((tid, punto[0], punto[1]))

    if MEDIDOR.listo:
        MEDIDOR.actualizar(mediciones, t)

    pares = []   # ((x_a, y_a), (x_b, y_b), distancia_m) en píxeles del frame de trabajo
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
    total = sum(n_ for g, n_ in por_grupo.items() if g not in GRUPOS_QUE_NO_SON_VEHICULO)
    return por_clase, por_grupo, total, boxes_en_zona, pares


def dibujar(frame, zona, boxes, por_grupo, total, pares=(), fps_det=None):
    """Dibuja la zona, las cajas (con velocidad) y el conteo sobre una COPIA del frame."""
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
    if fps_det:
        y += 25
        cv2.putText(out, f"Deteccion: {fps_det:.1f} FPS", (10, y),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 200, 255), 2)
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
    OJO: ahora se llama desde el hilo de detección, no desde el hilo principal.
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


def _hilo_inferencia(frames, zona, publicador, fuente, resultado, detener):
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
            t, frame = dato

            t_ini = time.time()
            por_clase, por_grupo, total, boxes, pares = contar_vehiculos(frame, zona, t)
            dt = time.time() - t_ini
            if dt > 0:
                fps_det = 1.0 / dt if fps_det is None else 0.8 * fps_det + 0.2 * (1.0 / dt)

            resultado.poner((boxes, por_grupo, total, pares, fps_det))

            ahora = time.time()
            if ahora - ultimo_envio >= INTERVALO_ENVIO:
                publicador.publicar(por_clase, por_grupo, total, fuente)
                ultimo_envio = ahora
    except Exception:
        print("[ERROR] Falló el hilo de detección:")
        traceback.print_exc()
        detener.set()


def procesar_fuente(primer_frame, lanzar_lector, fuente, publicador, mostrar=True):
    """
    primer_frame: frame ya reducido, solo para definir zona y calibración.
    lanzar_lector(destino, detener, fin): función que corre en un hilo y va dejando
                  en 'destino' tuplas (t_segundos, frame_reducido).
    """
    # La zona y la calibración se hacen sobre una imagen quieta, ANTES de arrancar el video
    zona = seleccionar_zona(primer_frame)
    if VEL_ACTIVAR:
        calibrar_velocidad(primer_frame)

    MEDIDOR.reiniciar()            # limpia historial de velocidades
    model.predictor = None         # reinicia el tracker entre fuentes

    frames = UltimoValor()         # frames nuevos (lector -> detección y ventana)
    resultado = UltimoValor()      # detecciones (detección -> ventana)
    detener = threading.Event()
    fin = threading.Event()

    h_lector = threading.Thread(target=lanzar_lector, args=(frames, detener, fin), daemon=True)
    h_inf = threading.Thread(target=_hilo_inferencia,
                             args=(frames, zona, publicador, fuente, resultado, detener),
                             daemon=True)
    h_lector.start()
    h_inf.start()

    ver_f = ver_r = 0
    ultimo_res = ([], {g: 0 for g in GRUPOS}, 0, [], None)
    try:
        while not detener.is_set():
            dato, ver_f = frames.tomar(ver_f)
            if dato is None:
                if fin.is_set():
                    break                       # el video terminó y ya se mostró el último frame
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
                cv2.imshow("Conteo (q = salir)", dibujar(frame, zona, *ultimo_res))
                if cv2.waitKey(1) & 0xFF == ord('q'):
                    break
    finally:
        detener.set()
        h_lector.join(timeout=2)
        h_inf.join(timeout=3)
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


def _hilo_video(ruta, destino, detener, fin):
    """
    Lee el video a SU velocidad real. Si la máquina va atrasada, salta frames
    (en vez de verse en cámara lenta). El tiempo de cada frame sale de indice/fps.
    """
    cap = cv2.VideoCapture(ruta)
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    t0 = time.time()
    i = 0
    try:
        while not detener.is_set():
            objetivo = int((time.time() - t0) * fps)
            acabo = False
            while i < objetivo:                 # voy atrasado: salto frames sin convertirlos
                if not cap.grab():
                    acabo = True
                    break
                i += 1
            if acabo:
                break

            ok, frame = cap.read()
            if not ok:
                break
            destino.poner((i / fps, _reducir(frame)))
            i += 1

            espera = t0 + i / fps - time.time()  # voy adelantado: espero al siguiente frame
            if espera > 0:
                time.sleep(espera)
    finally:
        cap.release()
        fin.set()


def modo_video(publicador, ruta=None):
    ruta = ruta or elegir_video()
    if not ruta:
        print("No se seleccionó ningún video.")
        return
    if not os.path.exists(ruta):
        print(f"No se encontró el video: {ruta}")
        return
    cap = cv2.VideoCapture(ruta)
    ok, primero = cap.read()
    cap.release()
    if not ok:
        print(f"No se pudo leer el video: {ruta}")
        return
    procesar_fuente(_reducir(primero),
                    lambda destino, detener, fin: _hilo_video(ruta, destino, detener, fin),
                    "video", publicador)


# =============================================================================
# PARTE B: COMPARTIR PANTALLA (requiere mss; en la Raspberry normalmente no se usa)
# =============================================================================
def _hilo_pantalla(monitor_idx, region, destino, detener, fin):
    """region: dict {"left":..,"top":..,"width":..,"height":..} o None = monitor completo."""
    try:
        with mss.mss() as sct:                       # mss debe crearse dentro del hilo que lo usa
            monitor = region or sct.monitors[monitor_idx]
            while not detener.is_set():
                img = np.array(sct.grab(monitor))    # BGRA
                destino.poner((time.time(), _reducir(cv2.cvtColor(img, cv2.COLOR_BGRA2BGR))))
                time.sleep(0.01)
    finally:
        fin.set()


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
    region = elegir_region_pantalla(monitor_idx) if usar_region else None
    with mss.mss() as sct:
        monitor = region or sct.monitors[monitor_idx]
        primero = cv2.cvtColor(np.array(sct.grab(monitor)), cv2.COLOR_BGRA2BGR)
    procesar_fuente(_reducir(primero),
                    lambda destino, detener, fin: _hilo_pantalla(monitor_idx, region, destino, detener, fin),
                    "pantalla", publicador)


# =============================================================================
# MAIN
# =============================================================================
if __name__ == "__main__":
    pub = Publicador()
    pub.suscribir_rl(enviar_a_rl)     # <- aquí conectas tu agente RL
    pub.suscribir_gui(enviar_a_gui)   # <- aquí conectas tu interfaz gráfica

    if len(sys.argv) > 1:
        # python conteo_vehiculos_rpi.py video.mp4
        modo_video(pub, sys.argv[1])
    else:
        modo = input("Fuente [1 = subir video, 2 = compartir pantalla]: ").strip()
        if modo == "1":
            modo_video(pub)
        else:
            modo_pantalla(pub)