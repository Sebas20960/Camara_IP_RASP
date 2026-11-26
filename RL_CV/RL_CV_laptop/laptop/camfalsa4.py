# =============================================================================
# camfalsa4.py  -  Simula VARIAS cámaras IP (MJPEG por http, como IP Webcam) desde videos.
# Cada video sale por su propio puerto: 8080, 8081, 8082, 8083...
#
# Uso (en la PC que "hace de cámaras"):
#     python camfalsa4.py videos/prueba_1.mp4 videos/prueba_2.mp4 videos/prueba_3.mp4 videos/prueba_4.mp4
#     (lo normal es NO usarlo a mano: el programa lo lanza desde la pestaña Conexión)
#     python camfalsa4.py a.mp4 b.mp4 --puerto 8080 --ancho 640 --fps 10 --calidad 75
#
# En la Raspberry (programa FINAL: cuenta y envía a la laptop):
#     python conteo4_laptop.py IP_PC:8080 IP_PC:8081 IP_PC:8082 IP_PC:8083
#     (o sin argumentos si ya pusiste estas URLs en CAMARAS de conteo4.py)
#
# --fps 10 (por defecto) imita una cámara configurada a 10 FPS: menos red y menos CPU en la Pi.
# =============================================================================
import argparse
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import cv2


def ip_local():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "IP_DE_ESTA_PC"


def crear_manejador(ruta, ancho, fps_max, calidad):
    class Manejador(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            pass

        def do_GET(self):
            if self.path not in ("/", "/video"):
                self.send_error(404)
                return
            cap = cv2.VideoCapture(ruta)
            if not cap.isOpened():
                self.send_error(500, "No se pudo abrir el video")
                return
            fps_video = cap.get(cv2.CAP_PROP_FPS) or 30.0
            fps = min(fps_video, fps_max) if fps_max else fps_video
            saltar = max(1, round(fps_video / fps))       # frames del video por cada enviado
            self.send_response(200)
            self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            print(f"[{ruta}] cliente conectado: {self.client_address[0]}")
            t_sig, fallos = time.time(), 0
            try:
                while True:
                    for _ in range(saltar - 1):
                        cap.grab()
                    ok, frame = cap.read()
                    if not ok:
                        fallos += 1
                        if fallos > 3:
                            break
                        cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                        continue
                    fallos = 0
                    if ancho and frame.shape[1] > ancho:
                        e = ancho / frame.shape[1]
                        frame = cv2.resize(frame, None, fx=e, fy=e, interpolation=cv2.INTER_AREA)
                    ok, jpg = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, calidad])
                    if not ok:
                        continue
                    d = jpg.tobytes()
                    self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\n"
                                     + f"Content-Length: {len(d)}\r\n\r\n".encode() + d + b"\r\n")
                    t_sig += saltar / fps_video
                    espera = t_sig - time.time()
                    if espera > 0:
                        time.sleep(espera)
                    else:
                        t_sig = time.time()
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass
            finally:
                cap.release()
                print(f"[{ruta}] cliente desconectado: {self.client_address[0]}")
    return Manejador


def main():
    ap = argparse.ArgumentParser(description="Simula varias cámaras IP MJPEG desde videos")
    ap.add_argument("videos", nargs="+", help="1 a 4 videos (uno por cámara)")
    ap.add_argument("--puerto", type=int, default=8080, help="puerto de la 1ª cámara")
    ap.add_argument("--ancho", type=int, default=640, help="ancho máximo (0 = original)")
    ap.add_argument("--fps", type=float, default=10, help="FPS máximos por cámara (0 = los del video)")
    ap.add_argument("--calidad", type=int, default=75, help="calidad JPEG 1-100")
    a = ap.parse_args()

    servidores = []
    ip = ip_local()
    print("=" * 60)
    for k, v in enumerate(a.videos):
        cap = cv2.VideoCapture(v)
        ok = cap.isOpened() and cap.grab()
        cap.release()
        if not ok:
            sys.exit(f"[ERROR] No se pudo leer el video: {v}")
        puerto = a.puerto + k
        srv = ThreadingHTTPServer(("0.0.0.0", puerto),
                                  crear_manejador(v, a.ancho, a.fps, a.calidad))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        servidores.append(srv)
        print(f" Cam{k + 1}: http://{ip}:{puerto}/video   ({v})")
    print(" En la Raspberry: python conteo4_laptop.py " +
          " ".join(f"{ip}:{a.puerto + k}" for k in range(len(a.videos))))
    print("   (o pon esas direcciones en CAMARAS de conteo4.py y ejecuta solo: python conteo4_laptop.py)")
    print(" Ctrl+C para detener.")
    print("=" * 60)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nDetenido.")
    finally:
        for s in servidores:
            s.shutdown()
            s.server_close()


if __name__ == "__main__":
    main()
