# =============================================================================
# conteo4_nube.py  -  conteo4 + envío de datos y video a DOS destinos a la vez (protocolo v2):
#   1) el programa de escritorio de la laptop (misma red local)   -> LAPTOP_URL
#   2) el servidor en AWS (dashboard web)                         -> NUBE_URL
# Cualquiera de los dos puede quedar vacío (= desactivado). Cada uno tiene su conexión, su
# reconexión, su buffer sin internet y su demanda de video: si nadie mira en un destino, a
# ese destino no se le manda video.
#
# Es EXACTAMENTE conteo4.py (mismas cámaras, opciones y teclas) con el envío añadido.
# conteo4.py, cliente_nube.py y modelo_5clases_ncnn_model/ deben estar en ESTA misma carpeta.
#
# Uso (en la Raspberry):
#     python conteo4_nube.py                                   (usa la configuración de abajo)
#     python conteo4_nube.py --laptop 192.168.1.33 --sin-pantalla IP:8080 IP:8081 IP:8082 IP:8083
#     python conteo4_nube.py --nube 3.85.10.20 --token XXXX --laptop 192.168.1.33:8000 --token-laptop YYYY
#     python conteo4_nube.py --nube "" --laptop 192.168.1.33   (solo la laptop, sin AWS)
#     python conteo4_nube.py --nube "" --laptop localhost ../laptop/videos/prueba_1.mp4   (prueba en la PC)
#     python conteo4_nube.py --fps-video 6 ...                 (tope de cuadros/s por cámara en
#                                                               AMBOS destinos)
#     python conteo4_nube.py --sin-pantalla ...                (lo normal en la Pi)
# Requisito extra: pip install websockets
# =============================================================================
import os
import sys

# ---- CONFIGURACIÓN DE LOS DESTINOS -------------------------------------------
# Acepta "1.2.3.4", "1.2.3.4:8000", "http://mi-dominio.com" o "ws://.../ws/pi". "" = desactivado.
# AWS: IP pública (estática) de tu servidor.
NUBE_URL = "http://3.144.43.2"
NUBE_TOKEN = "CQkVMFR-AuSLhSZopqtiKi6k7RtgtMwp"      # el mismo PI_TOKEN que te mostró instalar_aws.sh
# Laptop con el programa de escritorio (laptop/interfaz), en la misma red. Sin puerto usa 8000.
LAPTOP_URL = "10.100.151.73:8000"                     # IP de tu laptop (la muestra el programa en "Conexión"); "" = desactivado
LAPTOP_TOKEN = "cambia-esto"

# Tope de cuadros de video por segundo POR CÁMARA y por destino (el receptor puede pedir menos).
# Cada cuadro es el JPEG original de la cámara (medido: ~30-47 KB a 640x360, calidad 75): la Pi
# solo copia bytes (~0,04 ms de CPU por cuadro en la PC).
#  - Laptop (red local, sobra ancho de banda): 10 = todos los cuadros de una cámara a 10 FPS.
#  - AWS: lo limita la SUBIDA del internet donde está la Pi: 4 cám x 4 cuadros/s x ~37 KB
#    ≈ 600 KB/s ≈ 5 Mbps. Más se nota poco, porque las cajas ya se mueven suave por la
#    interpolación del dashboard. Si tu subida es pobre, baja a 2-3 (o baja la calidad JPEG
#    de las cámaras: con calidad 60 cada cuadro pesa ~20-25 % menos).
FPS_VIDEO_LAPTOP = 10
FPS_VIDEO_NUBE = 4
# -----------------------------------------------------------------------------

AQUI = os.path.dirname(os.path.abspath(__file__))
for carpeta in (AQUI, os.path.dirname(os.path.dirname(AQUI))):
    if os.path.isfile(os.path.join(carpeta, "conteo4.py")):
        sys.path.insert(0, carpeta)
        break
else:
    sys.exit("[ERROR] No encuentro conteo4.py: debe estar en la misma carpeta que conteo4_nube.py.")
sys.path.insert(0, AQUI)


def url_ws(entrada, puerto_defecto=None):
    """'1.2.3.4' -> ws://1.2.3.4/ws/pi ; 'https://dominio' -> wss://dominio/ws/pi.
    Con puerto_defecto, 'IP' sin puerto -> ws://IP:puerto/ws/pi (para la laptop)."""
    e = entrada.strip().rstrip("/")
    if not e or e.lower() in ("no", "0", "ninguno"):
        return ""
    if e.startswith(("ws://", "wss://")):
        return e if "/ws/" in e else e + "/ws/pi"
    if e.startswith("https://"):
        return "wss://" + e[len("https://"):] + "/ws/pi"
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
    nube = url_ws(_sacar_opcion("--nube", NUBE_URL))
    token = _sacar_opcion("--token", NUBE_TOKEN)
    laptop = url_ws(_sacar_opcion("--laptop", LAPTOP_URL), puerto_defecto=8000)
    token_laptop = _sacar_opcion("--token-laptop", LAPTOP_TOKEN)
    fps_video = _sacar_opcion("--fps-video", None)
    fps_laptop, fps_nube = FPS_VIDEO_LAPTOP, FPS_VIDEO_NUBE
    if fps_video:
        fps_laptop = fps_nube = max(0.0, float(fps_video))

    import conteo4
    from cliente_nube import EnvioDestinos

    destinos = []
    if laptop:
        destinos.append(("LAPTOP", laptop, token_laptop, fps_laptop))
    if nube:
        destinos.append(("NUBE", nube, token, fps_nube))
    if not destinos:
        print("[AVISO] Sin destinos (NUBE_URL y LAPTOP_URL vacíos): corre como conteo4 normal.")
    for nombre, url, _, fps in destinos:
        print(f"[{nombre}] Destino: {url} | video máx. {fps:g} cuadros/s por cámara")

    import rl_conteos

    def extension(camaras, pub, detener, estado, lado):
        envio = EnvioDestinos(destinos, camaras, pub) if destinos else None
        return rl_conteos.Extensiones(envio, rl_conteos.crear(camaras, pub))   # + semáforo (RL)

    conteo4.main(extension=extension)


if __name__ == "__main__":
    main()
