# =============================================================================
# definir_zonas.py  -  Dibuja, sobre una imagen de cada cámara, la ZONA que cuenta para el
# semáforo: los 60 m antes de la línea de parada, solo los carriles de ENTRADA (fuera: el
# interior del cruce, los carriles de salida y la curva de los desvíos). Guarda los
# polígonos en ../raspberry/rl_zonas.json (luego copia ese archivo a la Pi).
#
# Uso (en la laptop, con pantalla):
#     python definir_zonas.py                                  (cámaras de rl_zonas.json en las
#                                                               cámaras simuladas 127.0.0.1:8080..)
#     python definir_zonas.py Cam1=192.168.1.40:8080 Cam2=videos/prueba_2.mp4 ...
#     python definir_zonas.py Cam1=rtsp://usuario:clave@192.168.1.40:554/stream2
#     python definir_zonas.py --zonas ruta/a/rl_zonas.json ...
#
# En la ventana:
#     clic izquierdo  = agregar punto          clic derecho / Retroceso = quitar el último
#     c = borrar todo   Enter / s = guardar y pasar a la siguiente cámara
#     t = esta cámara SIN zona (cuenta todo el cuadro)   n = saltar sin cambiar   ESC = salir
#     espacio = tomar otra imagen (si el video no muestra autos en ese momento)
# Un vehículo cuenta si el CENTRO DEL BORDE INFERIOR de su caja (donde toca el piso) cae dentro.
# =============================================================================
import json
import os
import re
import sys

import cv2
import numpy as np

AQUI = os.path.dirname(os.path.abspath(__file__))
RUTA = os.path.join(AQUI, "..", "raspberry", "rl_zonas.json")
ANCHO_VENTANA = 1100


def url_de(entrada):
    e = entrada.strip()
    if e.startswith(("http://", "https://", "rtsp://")) or os.path.isfile(e):
        return e
    if ":" not in e:
        e += ":8080"
    return f"http://{e}/video"


def tomar_imagen(url, saltar=0):
    cap = cv2.VideoCapture(url)
    if not cap.isOpened():
        return None
    if os.path.isfile(url) and saltar:
        n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        if n:
            cap.set(cv2.CAP_PROP_POS_FRAMES, (saltar * 97) % n)
    img = None
    for _ in range(1 + (0 if os.path.isfile(url) else 5 + 10 * saltar)):   # stream: el más reciente
        ok, f = cap.read()
        if ok:
            img = f
    cap.release()
    return img


def editar(nombre, calle, url, poli):
    """Devuelve la lista de puntos (0..1), None (= todo el cuadro) o 'saltar' / 'salir'."""
    saltar = 0
    img = tomar_imagen(url)
    if img is None:
        print(f"[ERROR] No pude leer una imagen de {nombre} ({url}). Salto esta cámara.")
        return "saltar"
    puntos = [tuple(p) for p in (poli or [])]
    titulo = f"Zona de {nombre} (calle {calle})"
    cv2.namedWindow(titulo, cv2.WINDOW_AUTOSIZE)
    estado = {"img": img}

    def escala():
        h, w = estado["img"].shape[:2]
        e = min(1.0, ANCHO_VENTANA / w)
        return e, w, h

    def raton(ev, x, y, *_):
        e, w, h = escala()
        if ev == cv2.EVENT_LBUTTONDOWN:
            puntos.append((round(x / e / w, 4), round(y / e / h, 4)))
        elif ev == cv2.EVENT_RBUTTONDOWN and puntos:
            puntos.pop()

    cv2.setMouseCallback(titulo, raton)
    while True:
        e, w, h = escala()
        vista = cv2.resize(estado["img"], None, fx=e, fy=e) if e != 1.0 else estado["img"].copy()
        pts = np.array([[int(px * w * e), int(py * h * e)] for px, py in puntos], np.int32)
        if len(pts) >= 3:
            capa = vista.copy()
            cv2.fillPoly(capa, [pts], (60, 200, 90))
            vista = cv2.addWeighted(capa, 0.3, vista, 0.7, 0)
        if len(pts) >= 2:
            cv2.polylines(vista, [pts], len(pts) >= 3, (60, 220, 90), 2, cv2.LINE_AA)
        for p in pts:
            cv2.circle(vista, tuple(int(v) for v in p), 4, (0, 255, 255), -1, cv2.LINE_AA)
        ayuda = (f"{nombre} = calle {calle} | clic: punto  der.: quitar  c: borrar  Enter: guardar  "
                 f"t: todo el cuadro  espacio: otra imagen  n: saltar  ESC: salir")
        cv2.rectangle(vista, (0, 0), (vista.shape[1], 24), (20, 20, 20), -1)
        cv2.putText(vista, ayuda, (6, 17), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (240, 240, 240), 1, cv2.LINE_AA)
        cv2.imshow(titulo, vista)
        k = cv2.waitKey(30) & 0xFF
        if k in (13, 10, ord("s")):
            if 0 < len(puntos) < 3:
                print("  Hacen falta al menos 3 puntos (o 't' para todo el cuadro).")
                continue
            cv2.destroyWindow(titulo)
            return [list(p) for p in puntos] if puntos else None
        if k == ord("t"):
            cv2.destroyWindow(titulo)
            return None
        if k == ord("n"):
            cv2.destroyWindow(titulo)
            return "saltar"
        if k == 27:
            cv2.destroyWindow(titulo)
            return "salir"
        if k == ord("c"):
            puntos.clear()
        if k == 8 and puntos:
            puntos.pop()
        if k == ord(" "):
            saltar += 1
            nueva = tomar_imagen(url, saltar)
            if nueva is not None:
                estado["img"] = nueva
        try:
            if cv2.getWindowProperty(titulo, cv2.WND_PROP_VISIBLE) < 1:
                return "salir"
        except cv2.error:
            return "salir"


def main():
    args = sys.argv[1:]
    ruta = RUTA
    if "--zonas" in args:
        i = args.index("--zonas")
        ruta = args[i + 1]
        del args[i:i + 2]
    ruta = os.path.abspath(ruta)
    if not os.path.isfile(ruta):
        sys.exit(f"[ERROR] No encuentro {ruta}")
    with open(ruta, encoding="utf-8") as f:
        cfg = json.load(f)
    calle_de = {cam: calle for calle, cam in cfg["calles"].items()}
    if args:
        fuentes = []
        for a in args:
            if "=" not in a:
                sys.exit(f"[ERROR] Usa Nombre=fuente (p. ej. Cam1=192.168.1.40:8080); recibí {a!r}")
            n, u = a.split("=", 1)
            fuentes.append((n, url_de(u)))
    else:
        orden = sorted(calle_de, key=lambda c: list(cfg["calles"].values()).index(c))
        fuentes = [(n, url_de(f"127.0.0.1:{8080 + k}")) for k, n in enumerate(orden)]
        print("Sin argumentos: uso las cámaras simuladas 127.0.0.1:8080.. (enciéndelas antes).")
    zonas = cfg.setdefault("zonas", {})
    cambios = 0
    for nombre, url in fuentes:
        if nombre not in calle_de:
            print(f"[AVISO] {nombre} no está en 'calles' de rl_zonas.json {cfg['calles']}: igual guardo su zona.")
        print(f"-> {nombre} (calle {calle_de.get(nombre, '?')}): {url}")
        r = editar(nombre, calle_de.get(nombre, "?"), url, zonas.get(nombre))
        if r == "salir":
            break
        if r == "saltar":
            continue
        zonas[nombre] = r
        cambios += 1
        print(f"   {'todo el cuadro' if r is None else f'{len(r)} puntos'}")
    cv2.destroyAllWindows()
    if not cambios:
        print("Sin cambios.")
        return
    texto = json.dumps(cfg, ensure_ascii=False, indent=2)
    # listas solo de números (cada punto [x, y], "calles"...) en una línea: más fácil de leer
    texto = re.sub(r"\[\s*([-0-9.eE,\s]+?)\s*\]",
                   lambda m: "[" + ", ".join(x.strip() for x in m.group(1).split(",")) + "]", texto)
    json.loads(texto)                                   # por si acaso: sigue siendo JSON válido
    with open(ruta, "w", encoding="utf-8") as f:
        f.write(texto + "\n")
    print(f"Guardado en {ruta} ({cambios} cámara(s)). Cópialo a la Pi, junto a rl_conteos.py, y reinicia el conteo.")


if __name__ == "__main__":
    main()
