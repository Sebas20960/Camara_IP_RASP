# =============================================================================
# camara_ip_celular.py  -  Celular como cámara IP para la Raspberry Pi (SIN YOLO)
# Solo se conecta al stream del celular, muestra el video y reconecta si se corta.
#
# Uso:
#     python camara_ip_celular.py                       (te pide la IP o la URL)
#     python camara_ip_celular.py 192.168.1.50          (solo la IP: arma la URL de IP Webcam)
#     python camara_ip_celular.py http://192.168.1.50:8080/video
#     python camara_ip_celular.py rtsp://192.168.1.50:8554/live
#
# Teclas en la ventana:  q = salir
#
# Requisitos: pip install opencv-python
# =============================================================================
import os
import sys

# En Linux (Raspberry Pi OS con Wayland) OpenCV necesita el plugin xcb para abrir ventanas.
# Debe definirse ANTES de importar cv2.
if sys.platform.startswith("linux"):
    os.environ.setdefault("QT_QPA_PLATFORM", "xcb")

# Para URLs rtsp:// usar TCP (más estable que UDP en WiFi). Debe definirse ANTES de importar cv2.
os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", "rtsp_transport;tcp")

import time
import threading

import cv2

# -----------------------------------------------------------------------------
# CONFIGURACIÓN
# -----------------------------------------------------------------------------
PUERTO_IP_WEBCAM = 8080          # puerto por defecto de la app "IP Webcam" (Android)
RUTA_IP_WEBCAM = "/video"        # ruta del stream de IP Webcam
ESPERA_RECONEXION = 2.0          # segundos antes de reintentar si se corta la conexión
TIEMPO_SIN_FRAMES = 5.0          # si pasan estos segundos sin frames, se reconecta


def armar_url(entrada):
    """
    Acepta una URL completa o solo la IP.
      '192.168.1.50'               -> 'http://192.168.1.50:8080/video'
      '192.168.1.50:8080'          -> 'http://192.168.1.50:8080/video'
      'http://...' o 'rtsp://...'  -> se usa tal cual
    """
    entrada = entrada.strip()
    if entrada.startswith(("http://", "https://", "rtsp://")):
        return entrada
    if ":" not in entrada:
        entrada = f"{entrada}:{PUERTO_IP_WEBCAM}"
    return f"http://{entrada}{RUTA_IP_WEBCAM}"


class CamaraIP:
    """
    Lee el stream en un hilo aparte y guarda SOLO el frame más reciente.
    Así el video no se atrasa (evita el buffer acumulado de OpenCV) y, si el WiFi
    se corta, se reconecta solo.
    """

    def __init__(self, url):
        self.url = url
        self._lock = threading.Lock()
        self._frame = None
        self._ver = 0
        self._detener = threading.Event()
        self.conectada = False
        self.fps_lectura = 0.0
        self._hilo = threading.Thread(target=self._leer, daemon=True)

    def iniciar(self):
        self._hilo.start()
        return self

    def _abrir(self):
        cap = cv2.VideoCapture(self.url)
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)      # buffer mínimo (menos retraso)
        return cap

    def _leer(self):
        while not self._detener.is_set():
            print(f"[CAMARA] Conectando a {self.url} ...")
            cap = self._abrir()
            if not cap.isOpened():
                self.conectada = False
                print(f"[CAMARA] No se pudo conectar. Reintento en {ESPERA_RECONEXION:.0f} s...")
                cap.release()
                self._detener.wait(ESPERA_RECONEXION)
                continue

            print("[CAMARA] Conectada.")
            self.conectada = True
            ultimo_ok = time.time()
            t_fps, n_fps = time.time(), 0

            while not self._detener.is_set():
                ok, frame = cap.read()
                if not ok:
                    if time.time() - ultimo_ok > TIEMPO_SIN_FRAMES:
                        print("[CAMARA] Se perdió la señal.")
                        break
                    time.sleep(0.01)
                    continue
                ultimo_ok = time.time()
                with self._lock:
                    self._frame = frame
                    self._ver += 1

                n_fps += 1
                if time.time() - t_fps >= 1.0:
                    self.fps_lectura = n_fps / (time.time() - t_fps)
                    t_fps, n_fps = time.time(), 0

            self.conectada = False
            cap.release()
            if not self._detener.is_set():
                self._detener.wait(ESPERA_RECONEXION)

    def leer(self, ver_visto=0):
        """Devuelve (frame, version). Si no hay frame nuevo: (None, ver_visto)."""
        with self._lock:
            if self._frame is None or self._ver == ver_visto:
                return None, ver_visto
            return self._frame, self._ver

    def detener(self):
        self._detener.set()
        self._hilo.join(timeout=3)


def main():
    if len(sys.argv) > 1:
        entrada = sys.argv[1]
    else:
        entrada = input("IP del celular (o URL completa del stream): ").strip()
    if not entrada:
        print("No ingresaste ninguna IP/URL.")
        return

    url = armar_url(entrada)
    cam = CamaraIP(url).iniciar()

    ver = 0
    try:
        while True:
            frame, ver = cam.leer(ver)
            if frame is not None:
                vista = frame.copy()
                alto, ancho = vista.shape[:2]
                cv2.putText(vista, f"{ancho}x{alto} | {cam.fps_lectura:.1f} FPS",
                            (10, 25), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2)
                cv2.imshow("Camara IP (q = salir)", vista)
            elif not cam.conectada:
                time.sleep(0.05)

            if cv2.waitKey(10) & 0xFF == ord('q'):
                break
    finally:
        cam.detener()
        cv2.destroyAllWindows()


if __name__ == "__main__":
    main()