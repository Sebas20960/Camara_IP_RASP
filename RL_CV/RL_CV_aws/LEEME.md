> **RL_CV_aws: esta versión incluye el semáforo con RL.** Para lo del semáforo (zonas, `rl_zonas.json`, `bash instalar_rl.sh`) mira [`../LEEME.md`](../LEEME.md) y [`docs/CONTRATO_RL.md`](docs/CONTRATO_RL.md). Lo de abajo es la guía original de V_final_aws (donde diga `V_final_aws`, en la Pi usa `RL_CV_aws`; para instalar usa `instalar_rl.sh` en vez de los servicios de abajo).

# V_final_aws — Conteo de vehículos con YOLO (Raspberry Pi 4 → servidor AWS)

Versión **solo AWS**: la Raspberry cuenta los vehículos de 4 cámaras y manda los datos y el video al dashboard web `http://3.144.43.2`. **No usa el programa de escritorio de la laptop**: la laptop solo hace de "4 cámaras" (con videos) durante las pruebas.

## Qué hay en cada carpeta

```
V_final_aws/
├── raspberry/        ← VA EN LA RASPBERRY
│   ├── conteo4_aws.py          ✅ EL PROGRAMA QUE SE EJECUTA en la Pi (aquí van NUBE_URL, NUBE_TOKEN, FPS_VIDEO_NUBE)
│   ├── conteo4.py              motor: 4 cámaras + YOLO + conteo (aquí van CAMARAS y ajustes)
│   ├── motor_ncnn.py           YOLO directo con ncnn (más rápido que Ultralytics). Va junto a conteo4.py
│   ├── cliente_nube.py         envía datos y video a AWS
│   ├── modelo_5clases_ncnn_model/   el modelo YOLO (NCNN)
│   ├── conteo4-aws.service     (opcional) para que arranque solo al encender la Pi
│   └── requirements.txt
├── laptop/           ← SE QUEDA EN TU LAPTOP (solo para las pruebas con videos)
│   ├── iniciar_camaras.bat     ✅ DOBLE CLIC: enciende las 4 cámaras simuladas (puertos 8080..8083)
│   ├── camfalsa4.py            el simulador de cámaras IP (lo lanza el .bat)
│   ├── preparar_videos.py      reduce videos grandes a 640 px / 10 FPS
│   └── videos/                 pon aquí tus .mp4 (si está vacía usa V_final_final\laptop\videos)
├── aws/              ← VA EN EL SERVIDOR DE AWS (ya instalado en 3.144.43.2)
│   ├── servidor.py, static/index.html, instalar_aws.sh, requirements.txt
│   └── LEEME.md                cómo actualizarlo (o instalarlo desde cero)
└── LEEME.md          este archivo
```

## Cómo funciona

```
LAPTOP (192.168.1.33)                          RASPBERRY PI 4 (misma red WiFi/cable)
iniciar_camaras.bat -> 4 cámaras  ───────────▶ conteo4_aws.py (YOLO en las 4 cámaras)
   (puertos 8080..8083, MJPEG)                        │
                                                      │ datos cada 1 s (siempre)
                                                      │ + video 4 cuadros/s por cámara
                                                      ▼   (solo si alguien mira)
                              AWS  http://3.144.43.2  (dashboard: video con cajas,
                                                       conteos, historial, CSV)
```

La Pi reenvía el JPEG de cada cámara **tal cual** (casi sin gastar CPU) más las cajas de YOLO; el navegador **mueve las cajas suavemente** entre análisis. Si nadie tiene el dashboard abierto, no se sube video (solo ~1 KB/s de datos), pero el historial se sigue llenando.

---

## PREPARACIÓN (una sola vez)

### 1. En la Raspberry: averigua su IP y usuario
```bash
hostname -I      # ej. 192.168.1.50
whoami           # ej. pi
```
Activa SSH si no lo está: `sudo raspi-config` → *Interface Options* → *SSH* → *Enable*.

### 2. Copia la carpeta `raspberry` a la Pi
En la Pi: `mkdir -p ~/V_final_aws`. Luego, en tu laptop (PowerShell), cambiando `pi@192.168.1.50` por lo del paso 1:
```powershell
cd "C:\Users\SEBASTIAN\OneDrive\Desktop\Proyecto I\YOLO\Versiones\V_final_final\V_final_aws"
scp -r raspberry pi@192.168.1.50:~/V_final_aws/
```
(o cópiala con una USB a `~/V_final_aws/raspberry`).

### 3. Instala las librerías en la Pi
Dentro del mismo entorno virtual donde usas YOLO (si usas uno, actívalo antes, p. ej. `source ~/yolo_env/bin/activate`):
```bash
cd ~/V_final_aws/raspberry
pip install -r requirements.txt
```

### 4. Revisa la configuración (ya viene lista)
- `raspberry/conteo4.py` → `CAMARAS` apunta a `192.168.1.33:8080` … `8083` (tu laptop), `MOSTRAR_VIDEO = False`, `TRABAJADORES = 3`.
- `raspberry/conteo4_aws.py` → `NUBE_URL = "http://3.144.43.2"`, tu `NUBE_TOKEN` y `FPS_VIDEO_NUBE = 4`.
- **Si la IP de tu laptop cambia** (el router a veces la cambia), corrige `CAMARAS` o pásalas por consola (ver Uso). La IP actual la muestra `iniciar_camaras.bat` al arrancar (líneas `Cam1: http://IP:8080/video`) o el comando `ipconfig`. Para evitarlo, reserva una IP fija para la laptop en el router.

### 5. Actualiza el servidor de AWS
Ver `aws/LEEME.md` (tres comandos desde PowerShell; conserva tu token y tu historial).

### 6. En la laptop: primera vez con las cámaras
Doble clic en `laptop\iniciar_camaras.bat`. Si falta OpenCV lo instala solo. Cuando Windows pregunte por el **firewall**, marca **"Redes privadas"** → **Permitir** (si lo niegas, la Pi no podrá leer las cámaras).

### 7. (Opcional) Que la Pi arranque sola al encenderse
Edita `~/V_final_aws/raspberry/conteo4-aws.service` (usuario, ruta y python del entorno virtual; las instrucciones están dentro) y luego:
```bash
sudo cp ~/V_final_aws/raspberry/conteo4-aws.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now conteo4-aws
```

---

## USO (cada vez)

1. **Laptop** → doble clic en `laptop\iniciar_camaras.bat`. Deja esa ventana abierta (es la que "transmite" las cámaras).
2. **Raspberry** (si no hiciste el paso 7):
   ```bash
   cd ~/V_final_aws/raspberry
   python conteo4_aws.py
   ```
   Si la IP de la laptop cambió: `python conteo4_aws.py IP:8080 IP:8081 IP:8082 IP:8083`.
3. **Mira** desde cualquier navegador: `http://3.144.43.2` (video 2x2 con cajas, conteos, gráfica por minuto, botón **Descargar CSV**).
4. **Terminar**: en la Pi `Ctrl+C` (o `sudo systemctl stop conteo4-aws` si es servicio) y cierra la ventana de las cámaras en la laptop.

En la Pi, cada 10 s verás líneas como estas (sirven para saber si va bien):
```
[ESTADO] YOLO 7.8 inf/s | Cam1:5(1.9fps) | Cam2:3(1.9fps) | ... | total 21 | CPU 64°C
[NUBE] video: 16.0 cuadros/s (4.0 por cámara), 580 KB/s (4.5 Mbps), 36.0 KB/cuadro | det 7.8/s | CPU del hilo 1.5%
```
- `inf/s`: análisis de YOLO por segundo (todas las cámaras). `fps` de cada cámara ≈ inf/s ÷ 4.
- `CPU 64°C`: temperatura. Si pasa de 80 °C aparece un aviso.
- La línea `[NUBE] video` solo sale mientras alguien mira el dashboard: muestra cuánto se sube a internet.

## Ajustes útiles

| Dónde | Ajuste | Para qué |
|---|---|---|
| `raspberry/conteo4_aws.py` o `--fps-video N` | `FPS_VIDEO_NUBE = 4` | Cuadros/s por cámara hacia AWS. **Bájalo si tu internet sube poco**: 2 ≈ 2,3 Mbps, 1 ≈ 1,1 Mbps, 0 = solo datos (sin video) |
| `laptop\iniciar_camaras.bat` (o `set CAM_CALIDAD=60` antes de ejecutarlo) | calidad JPEG 70 | Menos = cuadros más livianos (60 ≈ -15 % de subida respecto a 70) |
| `raspberry/conteo4.py` | `FPS_MAX_POR_CAMARA = 2` | Menos CPU y temperatura en la Pi (por defecto 0 = sin límite). Útil si no tiene ventilador |
| `raspberry/conteo4.py` | `TRABAJADORES = 3` | 3 redes NCNN de 1 hilo en paralelo (deja 1 núcleo libre). Aguanta la carga mucho mejor que una red de 3 hilos. 1 = como antes (`HILOS_NCNN`) |
| `raspberry/conteo4.py` | `MOTOR = "ncnn"`, `ENTRADA_RECTANGULAR = True` | YOLO directo con ncnn y entrada 320x192 (~1,7-2x más rápido, mismas detecciones). Si algo falla, `MOTOR = "ultralytics"` vuelve a lo de antes |
| `raspberry/conteo4.py` | `CAMARAS` | IP de la laptop / cámaras reales (`rtsp://...`) |
| `raspberry/conteo4_aws.py` o `--nube IP --token X` | `NUBE_URL`, `NUBE_TOKEN` | Otro servidor (o uno de prueba en tu laptop) |

### Cuánto internet de subida usa (medido con los videos de prueba, 640 px, calidad 70)
| Video hacia AWS | Subida (4 cámaras) |
|---|---|
| 4 cuadros/s por cámara (por defecto) | ≈ 560-600 KB/s ≈ 4,5 Mbps |
| 2 cuadros/s | ≈ 2,3 Mbps |
| 1 cuadro/s | ≈ 1,1 Mbps |
| Nadie mirando el dashboard | ≈ 1 KB/s (solo datos) |

## Problemas comunes

| Síntoma | Qué revisar |
|---|---|
| La Pi: `No pude conectar a http://192.168.1.33:8080` | ¿Está abierta la ventana de `iniciar_camaras.bat`? ¿Cambió la IP de la laptop? ¿Firewall de Windows (Redes privadas → Permitir)? ¿Pi y laptop en la misma red? |
| `iniciar_camaras.bat`: `No hay videos .mp4` | Copia tus videos a `laptop\videos\` (o deja los de `V_final_final\laptop\videos`) |
| La Pi: `[NUBE] Sin conexión con ws://3.144.43.2/ws/pi` | ¿La Pi tiene internet (`ping 8.8.8.8`)? ¿El servidor corre (`aws/LEEME.md`, comandos útiles)? Mientras tanto la Pi guarda los datos y los manda al volver |
| El servidor dice `Conexión rechazada (token incorrecto)` | `NUBE_TOKEN` de `conteo4_aws.py` ≠ `cat ~/servidor/.token` en AWS |
| El dashboard abre pero "Raspberry desconectada" | ¿Corre `conteo4_aws.py` (no `conteo4.py`, que no envía nada)? |
| Hay conteos pero el video se traba o va con saltos | Subida de internet insuficiente: `--fps-video 2` (o 1) |
| El dashboard se ve igual que antes | ¿Actualizaste el servidor (`aws/LEEME.md`)? Recarga con Ctrl+F5 |
| Temperatura ≥ 80 °C | Ventilador / disipador, o `FPS_MAX_POR_CAMARA = 2` en `conteo4.py` |
| `ModuleNotFoundError` en la Pi | `pip install -r requirements.txt` en el mismo entorno donde está ultralytics |
| La laptop va lenta con las cámaras | Videos 4K (como `prueba_1.mp4`): redúcelos con `python preparar_videos.py MI_VIDEO.mp4 --salida videos` |

## Probar todo SOLO en la laptop (sin la Pi y sin tocar AWS)
Tres consolas de PowerShell, en la carpeta `V_final_aws`:
```powershell
# 1) Servidor de prueba (hace de AWS): http://localhost:8000
cd aws;  python servidor.py
# 2) Cámaras
laptop\iniciar_camaras.bat
# 3) Hace de Raspberry (YOLO real), enviando al servidor de prueba
cd raspberry;  python conteo4_aws.py --nube localhost:8000 --token cambia-esto 127.0.0.1:8080 127.0.0.1:8081 127.0.0.1:8082 127.0.0.1:8083
```
Abre `http://localhost:8000`. El historial de prueba queda en `aws\historial.db` (bórralo al terminar; no lo copies a AWS).

## Seguridad
- **No guardes la llave `.pem` de AWS dentro de estas carpetas** (está en `C:\Users\SEBASTIAN\.ssh\`).
- La Pi necesita el token para enviar datos a AWS. El dashboard web es público para quien tenga la IP.
