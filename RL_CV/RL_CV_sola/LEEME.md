> **RL_CV_sola: esta versión incluye el semáforo con RL.** Para lo del semáforo (zonas, `rl_zonas.json`, `bash instalar_rl.sh`) mira [`../LEEME.md`](../LEEME.md) y [`docs/CONTRATO_RL.md`](docs/CONTRATO_RL.md). Lo de abajo es la guía original de V_final_sola (donde diga `V_final_sola`, en la Pi usa `RL_CV_sola`; para instalar usa `instalar_rl.sh` en vez de los servicios de abajo).

# V_final_sola — Conteo de vehículos con YOLO SOLO en la Raspberry (sin interfaz)

Versión mínima: **la Raspberry Pi 4 cuenta vehículos en 4 cámaras y nada más**. No hay programa de laptop ni servidor de AWS. La laptop solo hace de "cámaras" (simula 4 cámaras IP con videos) mientras no tengas cámaras reales.

## Qué hay en cada carpeta

```
V_final_sola/
├── raspberry/        ← VA EN LA RASPBERRY
│   ├── conteo4.py              ✅ EL PROGRAMA: 4 cámaras + YOLO + conteo + CSV (aquí van CAMARAS y ajustes)
│   ├── motor_ncnn.py           YOLO directo con ncnn (más rápido que Ultralytics). Va junto a conteo4.py
│   ├── modelo_5clases_ncnn_model/   el modelo YOLO (NCNN)
│   ├── conteo4.service         (opcional) para que arranque solo al encender la Pi
│   ├── requirements.txt
│   └── registros/              (se crea sola) un CSV por día con el conteo por minuto
└── laptop/           ← SE QUEDA EN TU LAPTOP
    ├── iniciar_camaras.bat     ✅ DOBLE CLIC: enciende las 4 cámaras simuladas
    ├── camfalsa4.py            las cámaras simuladas (el .bat lo lanza solo)
    ├── preparar_videos.py      reduce videos de YouTube a 640 px / 10 FPS
    └── videos/                 pon aquí tus videos (si está vacía usa V_final_final\laptop\videos)
```

## Cómo funciona

```
LAPTOP (192.168.1.33)                                   RASPBERRY PI 4 (misma red WiFi/cable)
iniciar_camaras.bat → camfalsa4.py                      conteo4.py
  Cam1 :8080 ┐                                            ├─ YOLO en las 4 cámaras (por turnos)
  Cam2 :8081 ├──── video MJPEG (10 cuadros/s) ──────────▶ ├─ ByteTrack + conteo por cámara
  Cam3 :8082 │                                            ├─ consola: [ESTADO] cada 10 s
  Cam4 :8083 ┘                                            ├─ registros/conteo_AAAA-MM-DD.csv
                                                          └─ enviar_a_rl() → tu agente RL
```

La Pi no envía nada a ningún lado: todo queda en la propia Pi (consola + CSV). Por eso es la versión más liviana: no gasta CPU en red ni en reenviar video.

---

## PREPARACIÓN (una sola vez)

### 1. En la Raspberry: averigua su IP y usuario
```bash
hostname -I      # ej. 192.168.1.50
whoami           # ej. pi
```
Activa SSH si no lo está: `sudo raspi-config` → *Interface Options* → *SSH* → *Enable*.

### 2. Copia la carpeta `raspberry` a la Pi
En la Pi: `mkdir -p ~/V_final_sola`. Luego, en tu laptop (PowerShell, **dentro de `V_final_sola`**), cambiando `pi@192.168.1.50` por lo del paso 1:
```powershell
scp -r raspberry pi@192.168.1.50:~/V_final_sola/
```
(o cópiala con una USB a `~/V_final_sola/raspberry`). Debe quedar `~/V_final_sola/raspberry/conteo4.py`.

### 3. Instala las librerías en la Pi
Dentro del mismo entorno virtual donde usas YOLO (si usas uno):
```bash
cd ~/V_final_sola/raspberry
pip install -r requirements.txt
```

### 4. Revisa la configuración (ya viene lista)
En `raspberry/conteo4.py` (parte de arriba, sección CONFIGURACIÓN):
- `CAMARAS` apunta a `192.168.1.33:8080` … `8083` (tu laptop con las cámaras simuladas).
- `MOSTRAR_VIDEO = False` (sin ventana: lo más liviano), `TRABAJADORES = 3`, `GUARDAR_CSV = True`.
- **Si la IP de tu laptop cambia** (el router a veces la cambia), corrige `CAMARAS`. La IP actual la muestra `iniciar_camaras.bat` al arrancar (o `ipconfig` en la laptop). Para evitarlo, reserva una IP fija para la laptop en el router.

### 5. En la laptop: primera apertura de las cámaras
Doble clic en `laptop\iniciar_camaras.bat`. Si falta OpenCV lo instala solo (solo la primera vez). Cuando Windows pregunte por el **firewall**, marca **"Redes privadas"** → **Permitir** (si lo niegas, la Pi no podrá ver las cámaras).

### 6. (Opcional) Que la Pi arranque sola al encenderse
En la Pi: edita `~/V_final_sola/raspberry/conteo4.service` (cambia `pi` por tu usuario y, si usas entorno virtual, `/usr/bin/python3` por el python de tu entorno, p. ej. `/home/pi/yolo_env/bin/python`) y luego:
```bash
sudo cp ~/V_final_sola/raspberry/conteo4.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now conteo4
```
Ver lo que hace: `sudo journalctl -u conteo4 -f`. Detenerlo: `sudo systemctl stop conteo4`. Que ya no arranque solo: `sudo systemctl disable --now conteo4`.

---

## USO (cada vez)

1. **Laptop** → doble clic en `laptop\iniciar_camaras.bat`. Usa (en orden alfabético, máximo 4) los `.mp4` de `V_final_sola\laptop\videos`; si esa carpeta no tiene ninguno, los de `V_final_final\laptop\videos`. Verás algo así (deja esa ventana abierta):
   ```
    Cam1: http://192.168.1.33:8080/video   (...\prueba_1.mp4)
    ...
    En la Raspberry: python conteo4.py 192.168.1.33:8080 192.168.1.33:8081 ...
   ```
   Puedes comprobar una cámara abriendo `http://localhost:8080/video` en el navegador de la laptop.
2. **Raspberry** (si no hiciste el paso 6):
   ```bash
   cd ~/V_final_sola/raspberry
   python conteo4.py --sin-pantalla
   ```
   (Si la IP de la laptop no es la de `CAMARAS`, pásalas: `python conteo4.py --sin-pantalla 192.168.1.40:8080 192.168.1.40:8081 192.168.1.40:8082 192.168.1.40:8083`.)
3. **Mira los resultados** (ver la sección siguiente).
4. **Terminar**: en la Pi `Ctrl+C` (o `sudo systemctl stop conteo4` si es servicio) y en la laptop cierra la ventana de las cámaras.

## Cómo ver los resultados (sin interfaz)

### 1) La consola: líneas `[ESTADO]` cada 10 s
```
[ESTADO] YOLO 7.8 inf/s | Cam1:5(1.9fps) | Cam2:3(1.9fps) | Cam3:9(2.0fps) | Cam4:4(1.9fps) | total 21 | CPU 64°C
```
- `YOLO 7.8 inf/s`: análisis por segundo entre las 4 cámaras (en la Pi 4 lo normal es ~6–9).
- `Cam1:5(1.9fps)`: 5 vehículos **en imagen ahora mismo** en Cam1, que se analiza 1,9 veces por segundo. `SIN SEÑAL` = la cámara no llega.
- `total 21`: suma de vehículos en imagen en las 4 cámaras.
- `CPU 64°C`: temperatura de la Pi. Si llega a 80 °C se avisa con `¡CALIENTE!`.

Si corre como servicio, las mismas líneas se ven con `sudo journalctl -u conteo4 -f`.

### 2) El CSV por minuto: `raspberry/registros/conteo_AAAA-MM-DD.csv`
Un archivo por día, **una fila por cámara y por minuto**:
```
fecha_hora,camara,pasaron,carro,moto,bus,pesado,especial,promedio_en_imagen,maximo_en_imagen,segundos
2026-11-23 12:25,Cam1,58,50,0,8,0,0,10.47,19,60
2026-11-23 12:25,Cam2,65,54,1,10,0,0,6.13,11,60
```
| Columna | Significado |
|---|---|
| `fecha_hora` | inicio del minuto (hora de la Pi) |
| `pasaron` | vehículos **distintos** que aparecieron en ese minuto (cada ID de ByteTrack cuenta una vez) |
| `carro` … `especial` | lo mismo, por categoría |
| `promedio_en_imagen` / `maximo_en_imagen` | cuántos vehículos había a la vez en la imagen (promedio y máximo del minuto) |
| `segundos` | segundos de ese minuto con datos: 60 = minuto completo; menos = el programa arrancó/se detuvo en ese minuto o la cámara no tenía señal (vacío en promedio/máximo = sin señal todo el minuto) |

- Se escribe **una vez por minuto** (no gasta la tarjeta SD) y al detener el programa se guarda también el minuto incompleto.
- Para traerlo a la laptop (PowerShell): `scp pi@192.168.1.50:~/V_final_sola/raspberry/registros/*.csv .`
- Si al abrirlo en **Excel** todo sale en una sola columna, usa *Datos → Desde texto/CSV* (separador coma), o cambia `SEPARADOR_CSV = ";"` en `conteo4.py`.
- Para no guardarlo: `python conteo4.py --sin-csv` (o `GUARDAR_CSV = False`). Otra carpeta: `--carpeta-csv /ruta`.

### 3) Ventana local con el video (opcional, solo si la Pi tiene monitor)
Con monitor/teclado conectados a la Pi (o escritorio remoto VNC), en una terminal del escritorio:
```bash
python conteo4.py --pantalla
```
Muestra un mosaico 2×2 con las cajas de YOLO. Teclas: `q`/`ESC` = salir, `r` = reiniciar la numeración de IDs. Gasta algo más de CPU: úsala para revisar y luego vuelve a `--sin-pantalla`. Por SSH o como servicio no hay pantalla: el programa sigue solo sin video (no se cae).

### 4) El agente RL (gancho)
En `conteo4.py`, la función `enviar_a_rl(estado)` recibe cada 0,5 s un vector `numpy.float32` con el estado de las cámaras. Reemplaza su contenido por la llamada a tu agente. Qué contiene lo eliges con `ESTADO_RL`:
- `"camara"` → un valor por cámara (vehículos en imagen), p. ej. `[5, 3, 9, 4]`.
- `"camara_categoria"` → por cámara, las 5 categorías seguidas (4 × 5 = 20 valores).
- `"equivalente"` → por cámara, "carros equivalentes" con `PESOS_EQUIVALENTES`.

Al arrancar se imprime el orden: `[RL ] orden del estado: ['Cam1', 'Cam2', 'Cam3', 'Cam4']`. Con `IMPRIMIR_RL = True` se imprime cada vector (para revisar).

## Ajustes útiles (en `raspberry/conteo4.py`)

| Ajuste | Por defecto | Para qué |
|---|---|---|
| `CAMARAS` | laptop `192.168.1.33:8080..8083` | Tus cámaras. Acepta `IP:puerto`, `http://...`, `rtsp://...` (cámara de seguridad: usa el sub-stream) o un archivo `.mp4` |
| `FPS_MAX_POR_CAMARA` | `0` (sin límite) | Pon `2` para menos CPU y temperatura (2 análisis/s por cámara bastan para contar) |
| `TRABAJADORES` | `3` | YOLO en paralelo: 3 redes NCNN de 1 hilo, cada una toma la siguiente cámara libre. Aguanta la carga mucho mejor que una red de 3 hilos. `1` = como antes (usa `HILOS_NCNN`) |
| `CONF` | `0.4` | Confianza mínima para contar un vehículo nuevo (sube si cuenta cosas que no son) |
| `MOSTRAR_VIDEO` | `False` | `True` = ventana siempre (igual que `--pantalla`) |
| `GUARDAR_CSV` / `CARPETA_CSV` / `SEPARADOR_CSV` | `True` / `registros` / `,` | El registro por minuto |
| `ESTADO_RL`, `IMPRIMIR_RL` | `"camara"`, `False` | Qué recibe el RL (ver arriba) |
| `INTERVALO_LOG` | `10` s | Cada cuánto sale `[ESTADO]` |

En la laptop, `camfalsa4.py` acepta `--fps` (10), `--ancho` (640) y `--calidad` (75). Valores más bajos = menos trabajo para la Pi.

## Para que la Pi 4 no se sature ni se caliente
- Usa siempre `--sin-pantalla` cuando no estés mirando (la ventana cuesta CPU).
- Ponle **disipador y ventilador**. Sin ventilador, usa `FPS_MAX_POR_CAMARA = 2`.
- Videos/cámaras a **~640 px y ~10 FPS** (`preparar_videos.py` para videos de YouTube; en cámaras reales, el sub-stream).
- Deja `TRABAJADORES = 3`: así queda un núcleo libre para leer las 4 cámaras.
- Mira la temperatura en `[ESTADO]` o con `vcgencmd measure_temp`.

## Problemas comunes

| Síntoma | Qué revisar |
|---|---|
| La Pi: `No pude conectar a http://192.168.1.33:8080` | ¿Está abierta la ventana de `iniciar_camaras.bat`? ¿Cambió la IP de la laptop (`ipconfig`)? ¿Permitiste el firewall de Windows en "Redes privadas"? ¿Pi y laptop en la misma red? |
| `[ESTADO] ... SIN SEÑAL` en una cámara | Esa cámara se cortó: se reconecta sola. Si dura, revisa la laptop/red |
| `iniciar_camaras.bat`: "No hay videos .mp4" | Copia tus `.mp4` a `laptop\videos` |
| `iniciar_camaras.bat`: "No encuentro Python" | Instala Python marcando "Add python.exe to PATH" |
| `YOLO` muy bajo (< 4 inf/s) o temperatura ≥ 80 °C | Ventilador / disipador, `FPS_MAX_POR_CAMARA = 2`, videos más chicos (`preparar_videos.py`) |
| No aparece la carpeta `registros` / CSV vacío | Se escribe al terminar cada minuto: espera 1–2 min. ¿Usaste `--sin-csv`? |
| `ModuleNotFoundError` en la Pi | `pip install -r requirements.txt` en el mismo entorno donde está ultralytics |
| `No encuentro el modelo` | La carpeta `modelo_5clases_ncnn_model/` debe estar junto a `conteo4.py` |
| `--pantalla` no abre ventana | Por SSH no hay pantalla (es normal). Hazlo en el escritorio de la Pi, o instala `opencv-python` en vez de `opencv-python-headless` |

## Probar todo SOLO en la laptop (sin la Pi)
```powershell
# Terminal 1 (o doble clic en laptop\iniciar_camaras.bat)
cd laptop;  python camfalsa4.py videos\prueba_1.mp4 videos\prueba_2.mp4 videos\prueba_3.mp4 videos\prueba_4.mp4
# Terminal 2: hace de Raspberry (con YOLO real)
cd raspberry;  python conteo4.py --pantalla 127.0.0.1:8080 127.0.0.1:8081 127.0.0.1:8082 127.0.0.1:8083
```
(Si `laptop\videos` está vacía, usa la ruta de `..\..\laptop\videos\...`.) El CSV de esta prueba queda en `raspberry\registros\`: bórralo si no lo quieres.


**Velocidad:** `conteo4.py` usa `motor_ncnn.py` (YOLO directo con ncnn, entrada 320x192): ~1,7-2x más rápido que antes con las mismas detecciones. Si algo falla, pon `MOTOR = "ultralytics"`.