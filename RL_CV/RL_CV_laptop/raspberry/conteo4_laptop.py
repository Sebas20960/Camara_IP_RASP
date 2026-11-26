# =============================================================================
# conteo4_laptop.py  -  conteo4 + envío de datos y video al programa de escritorio de la
# LAPTOP (misma red local, protocolo v2). Versión SIN AWS.
#
# Es EXACTAMENTE conteo4.py (mismas cámaras, opciones y teclas) con el envío añadido:
#   - cada 1 s manda los conteos (si la laptop está cerrada, los guarda y los manda al volver);
#   - video (JPEG original de cada cámara + cajas de YOLO) SOLO mientras alguien lo mira en
#     el programa: con el video en pausa o la ventana cerrada, la Pi no gasta nada en video.
# conteo4.py, cliente_nube.py y modelo_5clases_ncnn_model/ deben estar en ESTA misma carpeta.
#
# Uso (en la Raspberry):
#     python conteo4_laptop.py                                 (usa la configuración de abajo)
#     python conteo4_laptop.py --laptop 192.168.1.33:8000 --token-laptop cambia-esto
#     python conteo4_laptop.py --laptop 192.168.1.33 IP:8080 IP:8081 IP:8082 IP:8083
#     python conteo4_laptop.py --fps-video 6 ...               (tope de cuadros/s por cámara)
#     python conteo4_laptop.py --laptop localhost ../laptop/videos/prueba_1.mp4   (prueba en la PC)
#     python conteo4_laptop.py --sin-pantalla ...              (sin ventana; ya es lo normal)
# Requisito extra: pip install websockets
# =============================================================================
import os
import sys

# ---- CONFIGURACIÓN DEL DESTINO -----------------------------------------------
# Laptop con el programa de escritorio (laptop/interfaz), en la misma red.
# Acepta "1.2.3.4", "1.2.3.4:8000" o "ws://.../ws/pi". Sin puerto usa 8000.
# La IP actual de la laptop la muestra el programa en la pestaña "Conexión".
LAPTOP_URL = " 10.100.150.60:8000"
LAPTOP_TOKEN = "cambia-esto"         # el mismo token que tiene el programa (pestaña Conexión)

# Tope de cuadros de video por segundo POR CÁMARA (el programa puede pedir menos).
# Cada cuadro es el JPEG original de la cámara (~30-47 KB a 640x360): la Pi solo copia
# bytes, casi sin CPU. En red local sobra ancho de banda: 4 cám x 10 cuadros/s ≈ 1,5 MB/s.
# Si el WiFi va justo, baja a 5-6.
FPS_VIDEO_LAPTOP = 10
# -----------------------------------------------------------------------------

AQUI = os.path.dirname(os.path.abspath(__file__))
if not os.path.isfile(os.path.join(AQUI, "conteo4.py")):
    sys.exit("[ERROR] No encuentro conteo4.py: debe estar en la misma carpeta que conteo4_laptop.py.")
sys.path.insert(0, AQUI)


def url_ws(entrada, puerto_defecto=8000):
    """'1.2.3.4' -> ws://1.2.3.4:8000/ws/pi ; '1.2.3.4:9000' -> ws://1.2.3.4:9000/ws/pi."""
    e = entrada.strip().rstrip("/")
    if not e or e.lower() in ("no", "0", "ninguno"):
        return ""
    if e.startswith(("ws://", "wss://")):
        return e if "/ws/" in e else e + "/ws/pi"
    if e.startswith("http://"):
        e = e[len("http://"):]
    host, _, ruta = e.partition("/")
    if puerto_defecto and ":" not in host:
        host = f"{host}:{puerto_defecto}"
    ruta = f"/{ruta}" if ruta else ""
    return f"ws://{host}{ruta}" + ("" if ruta.startswith("/ws/") else "/ws/pi")


def _sacar_opcion(nombre, por_defecto):
    """Lee y QUITA '--nombre valor' de sys.argv (conteo4 no conoce estas opciones)."""
    if nombre in sys.argv:
        i = sys.argv.index(nombre)
        if i + 1 >= len(sys.argv):
            sys.exit(f"[ERROR] Falta el valor después de {nombre}")
        valor = sys.argv[i + 1]
        del sys.argv[i:i + 2]
        return valor
    return por_defecto


def main():
    laptop = url_ws(_sacar_opcion("--laptop", LAPTOP_URL))
    token = _sacar_opcion("--token-laptop", LAPTOP_TOKEN)
    fps_video = _sacar_opcion("--fps-video", None)
    fps = FPS_VIDEO_LAPTOP
    if fps_video:
        try:
            fps = max(0.0, float(fps_video))
        except ValueError:
            sys.exit(f"[ERROR] --fps-video debe ser un número (recibí '{fps_video}')")

    import conteo4
    from cliente_nube import EnvioDestinos

    if laptop:
        print(f"[LAPTOP] Destino: {laptop} | video máx. {fps:g} cuadros/s por cámara")
    else:
        print("[AVISO] LAPTOP_URL vacío: corre como conteo4 normal (sin enviar nada).")

    import rl_conteos

    def extension(camaras, pub, detener, estado, lado):
        envio = EnvioDestinos([("LAPTOP", laptop, token, fps)], camaras, pub) if laptop else None
        return rl_conteos.Extensiones(envio, rl_conteos.crear(camaras, pub))   # + semáforo (RL)

    conteo4.main(extension=extension)


if __name__ == "__main__":
    main()
