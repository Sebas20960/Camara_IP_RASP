# =============================================================================
# camara_falsa_video.py  -  Simula una cámara IP que emite un VIDEO de prueba en bucle
# Expone el video como stream MJPEG por HTTP, igual que la app "IP Webcam":
#     http://IP_DE_ESTE_EQUIPO:8080/video
# Así el script de la Raspberry (camara_ip_celular.py) se conecta sin cambiar nada.
#
# Uso (en el equipo que "hace de cámara"):
#     python camara_falsa_video.py prueba_1.mp4
#     python camara_falsa_video.py prueba_1.mp4 --puerto 8080 --ancho 640 --calidad 80
#
# Requisitos: pip install opencv-python
# =============================================================================
import argparse
import socket
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import cv2

CONFIG = {"ruta": None, "ancho": 640, "calidad": 80}


def ip_local():
    """IP de este equipo en la red local (para decirte a qué dirección conectarte)."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))          # no envía nada; solo elige la interfaz de red
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        try:
            return socket.gethostbyname(socket.gethostname())
        except Exception:
            return "IP_DE_ESTE_EQUIPO"


class Manejador(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):      # silencia el log por cada petición
        pass

    def do_GET(self):
        if self.path not in ("/", "/video"):
            self.send_error(404)
            return

        cap = cv2.VideoCapture(CONFIG["ruta"])
        if not cap.isOpened():
            self.send_error(500, "No se pudo abrir el video")
            return
        fps = cap.get(cv2.CAP_PROP_FPS) or 30.0

        self.send_response(200)
        self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        print(f"[CAMARA FALSA] Cliente conectado: {self.client_address[0]}")

        t_sig = time.time()
        fallos = 0
        try:
            while True:
                ok, frame = cap.read()
                if not ok:                          # fin del video: volver al inicio (bucle)
                    fallos += 1
                    if fallos > 3:
                        print("[CAMARA FALSA] No puedo leer el video.")
                        break
                    cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                    continue
                fallos = 0

                if CONFIG["ancho"] and frame.shape[1] > CONFIG["ancho"]:
                    e = CONFIG["ancho"] / frame.shape[1]
                    frame = cv2.resize(frame, None, fx=e, fy=e, interpolation=cv2.INTER_AREA)

                ok, jpg = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, CONFIG["calidad"]])
                if not ok:
                    continue
                datos = jpg.tobytes()

                self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\n"
                                 + f"Content-Length: {len(datos)}\r\n\r\n".encode()
                                 + datos + b"\r\n")

                # mantener la velocidad real del video
                t_sig += 1.0 / fps
                espera = t_sig - time.time()
                if espera > 0:
                    time.sleep(espera)
                else:
                    t_sig = time.time()             # voy atrasado: no acumular retraso
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass                                    # el cliente se desconectó
        finally:
            cap.release()
            print(f"[CAMARA FALSA] Cliente desconectado: {self.client_address[0]}")


def main():
    ap = argparse.ArgumentParser(description="Simula una cámara IP con un video en bucle")
    ap.add_argument("video", help="ruta del video de prueba")
    ap.add_argument("--puerto", type=int, default=8080)
    ap.add_argument("--ancho", type=int, default=640, help="ancho máximo del stream (0 = original)")
    ap.add_argument("--calidad", type=int, default=80, help="calidad JPEG 1-100")
    a = ap.parse_args()

    cap = cv2.VideoCapture(a.video)
    ok = cap.isOpened() and cap.grab()
    cap.release()
    if not ok:
        sys.exit(f"[ERROR] No se pudo leer el video: {a.video}")

    CONFIG.update(ruta=a.video, ancho=a.ancho, calidad=a.calidad)
    ip = ip_local()
    print("=" * 60)
    print(" Cámara falsa lista. Desde la Raspberry conéctate a:")
    print(f"     http://{ip}:{a.puerto}/video")
    print(" Prueba local en este mismo equipo:")
    print(f"     http://127.0.0.1:{a.puerto}/video")
    print(" Ctrl+C para detener.")
    print("=" * 60)

    servidor = ThreadingHTTPServer(("0.0.0.0", a.puerto), Manejador)
    try:
        servidor.serve_forever()
    except KeyboardInterrupt:
        print("\n[CAMARA FALSA] Detenida.")
    finally:
        servidor.server_close()


if __name__ == "__main__":
    main()