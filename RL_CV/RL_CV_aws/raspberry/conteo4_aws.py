# =============================================================================
# conteo4_aws.py  -  EL PROGRAMA QUE SE EJECUTA EN LA RASPBERRY (versión solo AWS).
#
# Es EXACTAMENTE conteo4.py (mismas cámaras, opciones y teclas) + el envío de datos y
# video al servidor de AWS (dashboard web en http://3.144.43.2), protocolo v2:
#   - datos cada 1 s (siempre; si no hay internet se guardan hasta 6 h y se mandan al volver)
#   - video (JPEG original de cada cámara + cajas de YOLO) SOLO mientras alguien tiene el
#     dashboard abierto, con un tope de FPS_VIDEO_NUBE cuadros/s por cámara.
# conteo4.py, cliente_nube.py y modelo_5clases_ncnn_model/ deben estar en ESTA misma carpeta.
#
# Uso (en la Raspberry):
#     python conteo4_aws.py                                    (usa la configuración de abajo
#                                                               y las CAMARAS de conteo4.py)
#     python conteo4_aws.py 192.168.1.33:8080 192.168.1.33:8081 192.168.1.33:8082 192.168.1.33:8083
#     python conteo4_aws.py --fps-video 2                      (menos subida de internet)
#     python conteo4_aws.py --fps-video 0                      (solo datos, nunca video)
#     python conteo4_aws.py --nube 1.2.3.4 --token XXXX        (otro servidor / otro token)
#     python conteo4_aws.py --nube localhost:8000 --token cambia-esto ...   (prueba en la PC
#                                                               con aws/servidor.py local)
#     python conteo4_aws.py --pantalla                         (abre la ventana de conteo4;
#                                                               gasta CPU, solo para revisar)
# Requisito extra: pip install websockets
# =============================================================================
import os
import sys

# ---- CONFIGURACIÓN DEL SERVIDOR AWS -----------------------------------------
# Acepta "1.2.3.4", "1.2.3.4:8000", "http://mi-dominio.com" o "ws://.../ws/pi".
NUBE_URL = "http://3.144.43.2"
NUBE_TOKEN = "CQkVMFR-AuSLhSZopqtiKi6k7RtgtMwp"      # el mismo PI_TOKEN que te mostró instalar_aws.sh

# Tope de cuadros de video por segundo POR CÁMARA hacia AWS (el servidor puede pedir menos:
# FPS_VIDEO_WEB=4 en instalar_aws.sh). Lo limita la SUBIDA del internet donde está la Pi.
# Cada cuadro es el JPEG original de la cámara (medido con camfalsa4 a 640 px, calidad 70:
# ~29-43 KB, promedio ~35 KB). Con las 4 cámaras:
#     4 cuadros/s -> 4 x 4 x 35 KB ≈ 560 KB/s ≈ 4,5 Mbps   (por defecto)
#     2 cuadros/s ->                ≈ 280 KB/s ≈ 2,3 Mbps
#     1 cuadro/s  ->                ≈ 140 KB/s ≈ 1,1 Mbps
#     0           -> solo datos (~1 KB/s), nunca video
# Las cajas se mueven suave aunque haya pocos cuadros (el dashboard interpola), así que
# subir de 4 se nota poco. Solo se gasta mientras alguien mira el dashboard.
FPS_VIDEO_NUBE = 4
# -----------------------------------------------------------------------------

AQUI = os.path.dirname(os.path.abspath(__file__))
if not os.path.isfile(os.path.join(AQUI, "conteo4.py")):
    sys.exit("[ERROR] No encuentro conteo4.py: debe estar en la misma carpeta que conteo4_aws.py.")
sys.path.insert(0, AQUI)


def url_ws(entrada):
    """'1.2.3.4' -> ws://1.2.3.4/ws/pi ; 'https://dominio' -> wss://dominio/ws/pi."""
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
    fps_video = _sacar_opcion("--fps-video", None)
    try:
        fps = max(0.0, float(fps_video)) if fps_video is not None else float(FPS_VIDEO_NUBE)
    except ValueError:
        sys.exit(f"[ERROR] --fps-video debe ser un número (recibí '{fps_video}')")

    import conteo4
    from cliente_nube import EnvioDestinos

    if nube:
        print(f"[NUBE] Destino: {nube} | video máx. {fps:g} cuadros/s por cámara"
              + (" (0 = solo datos)" if not fps else ""))
    else:
        print("[AVISO] NUBE_URL vacío: corre como conteo4 normal, sin enviar nada.")

    import rl_conteos

    def extension(camaras, pub, detener, estado, lado):
        envio = EnvioDestinos([("NUBE", nube, token, fps)], camaras, pub) if nube else None
        return rl_conteos.Extensiones(envio, rl_conteos.crear(camaras, pub))   # + semáforo (RL)

    conteo4.main(extension=extension)


if __name__ == "__main__":
    main()
