# =============================================================================
# conteo_vehiculos_rpi_simple.py  (Raspberry Pi 4 - solo CONTEO de vehículos)
# Detecta y cuenta vehículos EN LA ZONA DE LA PISTA con YOLO (Ultralytics) y dibuja
# la caja que encierra cada uno. NO mide velocidad ni distancias. Ignora peatones,
# bicicletas, carretillas y cualquier clase que no esté en la lista de vehículos.
#
# Entradas:  A) video (ruta por argumento o explorador)   |   B) pantalla compartida (opcional)
# Salidas:   C) conteos hacia el algoritmo de RL y hacia la interfaz gráfica
#
# Uso:
#     python conteo_vehiculos_rpi_simple.py prueba_1.mp4
#     python conteo_vehiculos_rpi_simple.py                  (te pregunta la fuente)
#
# Optimizaciones que se mantienen:
#   - Modelo YOLO en formato .pt (PyTorch), sin convertir a NCNN.
#   - Frame reducido a LADO_TRABAJO antes de todo.
#   - Tres hilos: lector de video | detección YOLO | ventana (video fluido).
#   - Video en tiempo real (si la máquina no alcanza, salta frames).
# Además, al no medir velocidad ya no hace falta el seguimiento (tracking): es más
# liviano, y YOLO solo busca las clases de vehículos (menos trabajo).
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

import time
import queue
import threading
import traceback

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
# Modelo YOLO en formato .pt (junto a este script, o pon la ruta completa).
RUTA_MODELO = "modelo_combinado.pt"
CONF = 0.4
MAX_DET = 50                          # máximo de detecciones por frame (menos trabajo en NMS)
INTERVALO_ENVIO = 0.5                 # segundos entre cada envío de datos al RL y a la GUI

# Tamaños (las perillas principales de velocidad):
IMGSZ        = 320   # tamaño de inferencia de YOLO. 320 = más rápido; 416 o 640 = más detalle
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
# CARGA DEL MODELO (.pt)
# -----------------------------------------------------------------------------
if not os.path.exists(RUTA_MODELO):
    sys.exit(f"[ERROR] No encuentro el modelo: {RUTA_MODELO}\n"
             f"        Ejecuta el script desde la carpeta donde está, o pon la ruta completa en RUTA_MODELO.")
print(f"[MODELO] {RUTA_MODELO} | imgsz={IMGSZ}")
model = YOLO(RUTA_MODELO)                  # carga una sola vez
CLASES = list(model.names.values())        # todas las clases del modelo

# -----------------------------------------------------------------------------
# QUÉ CUENTA COMO VEHÍCULO
# Solo estas clases se detectan, se cuentan y se dibujan. Todo lo demás
# (peatón, bicicleta, rickshaw, carretilla...) se ignora.
# Usa los nombres EXACTOS del modelo. Para añadir o quitar una clase, edita la lista.
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

# Índices de las clases de vehículo: YOLO solo busca estas (menos trabajo y sin falsos "peatón")
IDS_VEHICULOS = [i for i, nombre in model.names.items() if nombre in VEHICULOS] or None
CLASES_VEHICULO = [c for c in CLASES if c in VEHICULOS]
_ignoradas = [c for c in CLASES if c not in VEHICULOS]
print(f"[MODELO] Cuenta {len(CLASES_VEHICULO)} clases de vehículo. Ignora: {_ignoradas}")
if IDS_VEHICULOS is None:
    print("[AVISO] Ninguna clase del modelo coincide con la lista de vehículos: revisa los "
          "nombres en GRUPOS. Se detectará TODO sin filtrar.")


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
# NÚCLEO: detección + conteo dentro de la zona (lo usan A y B)
# -----------------------------------------------------------------------------
def contar_vehiculos(frame, zona=None, conf=CONF):
    """
    frame: imagen BGR (ya reducida a LADO_TRABAJO, sin preprocesar)
    zona:  polígono [(x,y), ...] de la pista, en coordenadas de ese mismo frame
    return: (conteo_por_clase, conteo_por_grupo, total_vehiculos, boxes_en_zona)
    boxes_en_zona: (x1, y1, x2, y2, nombre_clase) en coords del frame
    """
    img, (x0, y0, escala) = preprocesar(frame, zona)          # <- PREPROCESO

    # classes=IDS_VEHICULOS: YOLO solo busca vehículos (ignora peatones, bicicletas, etc.)
    res = model.predict(img, conf=conf, imgsz=IMGSZ, max_det=MAX_DET,
                        classes=IDS_VEHICULOS, verbose=False)[0]

    # Lectura de todas las cajas en bloque (más rápido que caja por caja)
    if len(res.boxes):
        xyxy = res.boxes.xyxy.cpu().numpy()
        clases = res.boxes.cls.int().cpu().tolist()
    else:
        xyxy, clases = [], []
    poligono = np.array(zona, dtype=np.int32) if zona else None

    por_clase = {c: 0 for c in CLASES_VEHICULO}
    boxes_en_zona = []

    for (x1, y1, x2, y2), c in zip(xyxy, clases):
        nombre = res.names[c]
        if nombre not in VEHICULOS:          # doble seguro: nada que no sea vehículo
            continue

        # volver de la imagen preprocesada al frame de trabajo
        x1, x2 = float(x1) / escala + x0, float(x2) / escala + x0
        y1, y2 = float(y1) / escala + y0, float(y2) / escala + y0

        # Solo cuenta si el punto de contacto con el suelo (centro-abajo) cae dentro de la zona
        punto = (float((x1 + x2) / 2), float(y2))
        if poligono is not None:
            if cv2.pointPolygonTest(poligono, punto, False) < 0:
                continue

        por_clase[nombre] += 1
        boxes_en_zona.append((int(x1), int(y1), int(x2), int(y2), nombre))

    por_grupo = {g: sum(por_clase.get(c, 0) for c in cls) for g, cls in GRUPOS.items()}
    total = sum(por_grupo.values())
    return por_clase, por_grupo, total, boxes_en_zona


def dibujar(frame, zona, boxes, por_clase, total, fps_det=None):
    """Dibuja la zona, la caja de cada vehículo y el conteo sobre una COPIA del frame."""
    out = frame.copy()
    if zona:
        cv2.polylines(out, [np.array(zona, dtype=np.int32)], True, (0, 255, 255), 2)
    for x1, y1, x2, y2, nombre in boxes:
        cv2.rectangle(out, (x1, y1), (x2, y2), (0, 255, 0), 2)
        cv2.putText(out, nombre, (x1, max(y1 - 5, 12)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 1)
    y = 25
    cv2.putText(out, f"TOTAL: {total}", (10, y), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 0, 255), 2)
    for c, n in por_clase.items():           # solo las clases que hay en este momento
        if n > 0:
            y += 22
            cv2.putText(out, f"{c}: {n}", (10, y), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (255, 255, 255), 2)
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
    OJO: se llama desde el hilo de detección, no desde el hilo principal.
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
        # --- datos para el RL: vector numérico en orden fijo (un valor por grupo en GRUPOS) ---
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
            _, frame = dato

            t_ini = time.time()
            por_clase, por_grupo, total, boxes = contar_vehiculos(frame, zona)
            dt = time.time() - t_ini
            if dt > 0:
                fps_det = 1.0 / dt if fps_det is None else 0.8 * fps_det + 0.2 * (1.0 / dt)

            resultado.poner((boxes, por_clase, total, fps_det))

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
    primer_frame: frame ya reducido, solo para definir la zona.
    lanzar_lector(destino, detener, fin): función que corre en un hilo y va dejando
                  en 'destino' tuplas (t_segundos, frame_reducido).
    """
    # La zona se define sobre una imagen quieta, ANTES de arrancar el video
    zona = seleccionar_zona(primer_frame)

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
    ultimo_res = ([], {c: 0 for c in CLASES_VEHICULO}, 0, None)
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
    (en vez de verse en cámara lenta).
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
        # python conteo_vehiculos_rpi_simple.py video.mp4
        modo_video(pub, sys.argv[1])
    else:
        modo = input("Fuente [1 = subir video, 2 = compartir pantalla]: ").strip()
        if modo == "1":
            modo_video(pub)
        else:
            modo_pantalla(pub)