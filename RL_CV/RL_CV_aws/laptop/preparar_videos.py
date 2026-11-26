# =============================================================================
# preparar_videos.py  -  Deja los videos (p. ej. descargados de YouTube) a 640 px y 10 FPS,
# sin audio. Sirve para dos cosas:
#   1) Cámaras simuladas (lo normal): pon el resultado en laptop\videos\ y usa
#      iniciar_camaras.bat. camfalsa4.py gasta mucha menos CPU de la laptop con un video
#      de 640 px que con uno 4K (prueba_1.mp4 es 4K), y la Pi recibe lo mismo.
#   2) Usar los videos DIRECTO en la Raspberry (sin cámaras): con un archivo, la Pi
#      decodifica TODOS sus frames; un 1080p/4K a 30-60 FPS x 4 videos satura la CPU antes
#      de que YOLO haga nada. Un 640 px a 10 FPS cuesta ~10-20 veces menos.
#
# Uso (en la PC):
#     python preparar_videos.py video1.mp4 video2.mp4            -> carpeta videos_pi/
#     python preparar_videos.py carpeta_con_videos/               (todos los videos)
#     python preparar_videos.py v.mp4 --ancho 640 --fps 10 --salida videos_pi
#     python preparar_videos.py v.mp4 --inicio 30 --duracion 300  (solo 5 min desde el s 30)
#     python preparar_videos.py v.mp4 --opencv                    (no usar ffmpeg)
#
# En la Pi (caso 2):
#     python conteo4_aws.py videos_pi/video1_640p10.mp4 videos_pi/video2_640p10.mp4 ...
#
# Usa ffmpeg si está instalado (H.264: mejor calidad y archivos ~5x más chicos);
# si no, OpenCV (MPEG-4, ya lo tienes). Ambos se decodifican rápido en la Pi.
# Requisitos: pip install opencv-python   (opcional: ffmpeg en el PATH)
# =============================================================================
import argparse
import os
import shutil
import subprocess
import sys
import time

import cv2

EXTENSIONES = (".mp4", ".mkv", ".webm", ".avi", ".mov", ".m4v", ".flv", ".ts")


def listar_videos(entradas):
    videos = []
    for e in entradas:
        if os.path.isdir(e):
            videos += sorted(os.path.join(e, f) for f in os.listdir(e)
                             if f.lower().endswith(EXTENSIONES))
        elif os.path.isfile(e):
            videos.append(e)
        else:
            print(f"[AVISO] No existe: {e}")
    return videos


def info(ruta):
    cap = cv2.VideoCapture(ruta)
    if not cap.isOpened():
        return None
    d = {"w": int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)), "h": int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)),
         "fps": cap.get(cv2.CAP_PROP_FPS) or 30.0, "n": int(cap.get(cv2.CAP_PROP_FRAME_COUNT))}
    cap.release()
    d["seg"] = d["n"] / d["fps"] if d["n"] > 0 else 0
    return d


def tam_salida(w, h, ancho):
    """Reduce al ancho pedido (si el video es más grande) con lados pares (H.264 lo exige)."""
    if w > ancho:
        h = round(h * ancho / w)
        w = ancho
    return w - w % 2, h - h % 2


def con_ffmpeg(ent, sal, w, h, fps, inicio, duracion):
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-stats"]
    if inicio:
        cmd += ["-ss", str(inicio)]
    cmd += ["-i", ent]
    if duracion:
        cmd += ["-t", str(duracion)]
    cmd += ["-vf", f"fps={fps},scale={w}:{h}:flags=area",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "26",
            "-profile:v", "main", "-pix_fmt", "yuv420p",
            "-g", str(int(fps * 2)),        # un keyframe cada 2 s: el bucle de la Pi salta rápido
            "-an", "-movflags", "+faststart", sal]
    return subprocess.run(cmd).returncode == 0


def con_opencv(ent, sal, w, h, fps, inicio, duracion, d):
    cap = cv2.VideoCapture(ent)
    if inicio:
        cap.set(cv2.CAP_PROP_POS_MSEC, inicio * 1000)
    out = cv2.VideoWriter(sal, cv2.VideoWriter_fourcc(*"mp4v"), fps, (w, h))
    if not out.isOpened():
        cap.release()
        return False
    paso = 1.0 / fps                       # segundos de video entre frames guardados
    t_src = 1.0 / d["fps"]
    t, t_sig = 0.0, 0.0
    guardados, t0, t_log = 0, time.time(), 0.0
    total = duracion or max(d["seg"] - (inicio or 0), 0)
    while True:
        if duracion and t >= duracion:
            break
        if t + 1e-6 < t_sig:               # frame que no se guarda: grab (sin convertir)
            if not cap.grab():
                break
            t += t_src
            continue
        ok, frame = cap.read()
        if not ok:
            break
        t += t_src
        t_sig += paso
        if frame.shape[1] != w or frame.shape[0] != h:
            frame = cv2.resize(frame, (w, h), interpolation=cv2.INTER_AREA)
        out.write(frame)
        guardados += 1
        if time.time() - t_log > 1.0:
            t_log = time.time()
            pct = f"{100 * t / total:5.1f}%" if total else ""
            print(f"\r    {pct} {t:7.1f} s de video | {guardados} frames | "
                  f"{t / max(time.time() - t0, 1e-6):.1f}x tiempo real", end="", flush=True)
    print()
    cap.release()
    out.release()
    return guardados > 0


def main():
    ap = argparse.ArgumentParser(description="Reduce videos a 640 px / 10 FPS para la Raspberry")
    ap.add_argument("entradas", nargs="+", help="videos o carpetas con videos")
    ap.add_argument("--ancho", type=int, default=640, help="ancho máximo (por defecto 640)")
    ap.add_argument("--fps", type=float, default=10, help="FPS de salida (por defecto 10)")
    ap.add_argument("--salida", default="videos_pi", help="carpeta de salida")
    ap.add_argument("--inicio", type=float, default=0, help="segundo desde el que empezar")
    ap.add_argument("--duracion", type=float, default=0, help="segundos a guardar (0 = todo)")
    ap.add_argument("--opencv", action="store_true", help="no usar ffmpeg aunque esté instalado")
    a = ap.parse_args()

    videos = listar_videos(a.entradas)
    if not videos:
        sys.exit("[ERROR] No encontré videos.")
    os.makedirs(a.salida, exist_ok=True)
    usar_ffmpeg = not a.opencv and shutil.which("ffmpeg") is not None
    print(f"[INFO] {len(videos)} video(s) -> {os.path.abspath(a.salida)} | "
          f"{a.ancho} px, {a.fps:g} FPS | motor: {'ffmpeg (H.264)' if usar_ffmpeg else 'OpenCV (MPEG-4)'}")

    hechos = 0
    salidas = []
    for ent in videos:
        d = info(ent)
        if d is None:
            print(f"[ERROR] No pude abrir {ent}")
            continue
        w, h = tam_salida(d["w"], d["h"], a.ancho)
        fps = min(a.fps, d["fps"])
        base = os.path.splitext(os.path.basename(ent))[0]
        sal = os.path.join(a.salida, f"{base}_{min(w, h)}p{fps:g}.mp4")    # ej. video_360p10.mp4
        print(f"\n[{hechos + 1}/{len(videos)}] {os.path.basename(ent)}: {d['w']}x{d['h']} "
              f"{d['fps']:.0f} FPS, {d['seg'] / 60:.1f} min  ->  {w}x{h} {fps:g} FPS")
        t0 = time.time()
        ok = (con_ffmpeg(ent, sal, w, h, fps, a.inicio, a.duracion) if usar_ffmpeg else
              con_opencv(ent, sal, w, h, fps, a.inicio, a.duracion, d))
        if not ok and usar_ffmpeg:
            print("    ffmpeg falló; intento con OpenCV...")
            ok = con_opencv(ent, sal, w, h, fps, a.inicio, a.duracion, d)
        if not ok:
            print(f"    [ERROR] No se pudo convertir {ent}")
            continue
        mb_ent, mb_sal = os.path.getsize(ent) / 1e6, os.path.getsize(sal) / 1e6
        print(f"    listo en {time.time() - t0:.0f} s: {sal}  ({mb_ent:.0f} MB -> {mb_sal:.1f} MB)")
        hechos += 1
        # ruta tal como quedará en la Pi: carpeta_salida/archivo
        salidas.append(f"{os.path.basename(os.path.normpath(a.salida))}/{os.path.basename(sal)}")

    print(f"\n[INFO] {hechos}/{len(videos)} convertidos en '{a.salida}'.")
    print("  - Para las cámaras simuladas: muévelos a laptop\\videos\\ y usa iniciar_camaras.bat")
    print(f"  - Para usarlos directo en la Pi: copia '{a.salida}' a la Pi (junto a conteo4_aws.py) "
          f"y ejecuta:")
    if salidas:
        print("    python conteo4_aws.py " + " ".join(f'"{s}"' if " " in s else s for s in salidas[:4]))


if __name__ == "__main__":
    main()
