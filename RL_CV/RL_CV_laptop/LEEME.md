> **RL_CV_laptop: esta versión incluye el semáforo con RL.** Para lo del semáforo (zonas, `rl_zonas.json`, `bash instalar_rl.sh`) mira [`../LEEME.md`](../LEEME.md) y [`docs/CONTRATO_RL.md`](docs/CONTRATO_RL.md). Lo de abajo es la guía original de V_final_laptop (donde diga `V_final_laptop`, en la Pi usa `RL_CV_laptop`; para instalar usa `instalar_rl.sh` en vez de los servicios de abajo).

# V_final_laptop — Conteo de vehículos con YOLO (Raspberry Pi 4 + laptop, SIN AWS)

Versión que funciona **solo en tu red local**: la laptop simula las 4 cámaras y muestra todo
en el programa de escritorio; la Raspberry cuenta los vehículos con YOLO. No usa internet ni
el servidor de AWS.

## Qué hay en cada carpeta

```
V_final_laptop/
├── raspberry/        ← VA EN LA RASPBERRY
│   ├── conteo4_laptop.py       ✅ EL PROGRAMA QUE SE EJECUTA en la Pi
│   ├── conteo4.py              motor: 4 cámaras + YOLO + conteo (aquí van CAMARAS y ajustes)
│   ├── motor_ncnn.py           YOLO directo con ncnn (más rápido que Ultralytics). Va junto a conteo4.py
│   ├── cliente_nube.py         envía datos y video a la laptop
│   ├── modelo_5clases_ncnn_model/   el modelo YOLO (NCNN)
│   ├── conteo4-laptop.service  (opcional) para que arranque solo al encender la Pi
│   └── requirements.txt
└── laptop/           ← SE QUEDA EN TU LAPTOP
    ├── interfaz/
    │   ├── iniciar_interfaz.bat    ✅ DOBLE CLIC: abre el programa "Monitor de tráfico"
    │   ├── app.py                  la ventana (En vivo / Historial / Conexión)
    │   ├── nucleo.py               recibe a la Pi, interpola las cajas, guarda el historial
    │   ├── pi_falsa.py             (pruebas) imita a la Pi sin tenerla
    │   ├── _nucleo_falso.py        (pruebas) datos falsos: python app.py --falso
    │   ├── requirements.txt, LEEME.md
    ├── camfalsa4.py            las 4 cámaras IP simuladas (el programa lo lanza solo)
    ├── preparar_videos.py      reduce videos de YouTube a 640 px / 10 FPS
    └── videos/                 pon aquí tus videos (o usa los de V_final_final\laptop\videos)
```

## Cómo funciona

```
LAPTOP (192.168.1.33)                                   RASPBERRY PI 4 (misma red WiFi/cable)
camfalsa4.py: 4 cámaras (puertos 8080..8083) ─────────▶ conteo4_laptop.py (YOLO en las 4 cámaras)
Programa "Monitor de tráfico" (puerto 8000) ◀──── datos cada 1 s + video (hasta 10 cuadros/s por cámara)
```

- La Pi reenvía el JPEG de cada cámara **tal cual** (casi sin gastar CPU) más las cajas de
  YOLO; el programa muestra el video con ~1 s de retraso y **mueve las cajas suavemente**
  entre análisis.
- Si nadie mira el video (botón *Pausar video* o programa cerrado), la Pi **no manda video**:
  solo los conteos.
- Si cierras el programa o se cae el WiFi, la Pi **sigue contando y guarda los datos** (hasta
  6 h); al volver a abrir el programa se los manda y el historial queda completo.
- Pensado para la Pi 4 de 4 GB: un solo modelo YOLO para las 4 cámaras, YOLO en 3 de los 4
  núcleos (`TRABAJADORES = 3`), sin ventana en la Pi (`MOSTRAR_VIDEO = False`) y sin
  decodificar ni recomprimir el video que se envía.

---

## PREPARACIÓN (una sola vez)

### 1. En la Raspberry: averigua su IP y usuario
```bash
hostname -I      # ej. 192.168.1.50
whoami           # ej. pi
```
Activa SSH si no lo está: `sudo raspi-config` → *Interface Options* → *SSH* → *Enable*.

### 2. Copia la carpeta `raspberry` a la Pi
En la Pi: `mkdir -p ~/V_final_laptop`. Luego, en tu laptop (PowerShell, dentro de
`V_final_laptop`), cambiando `pi@192.168.1.50` por lo del paso 1:
```powershell
scp -r raspberry pi@192.168.1.50:~/V_final_laptop/
```
(o cópiala con una USB a `~/V_final_laptop/raspberry`).

### 3. Instala las librerías en la Pi
Dentro del mismo entorno virtual donde usas YOLO (si usas uno):
```bash
cd ~/V_final_laptop/raspberry
pip install -r requirements.txt
```

### 4. Revisa la configuración (ya viene lista)
- `raspberry/conteo4.py` → `CAMARAS` apunta a `192.168.1.33:8080` … `8083` (tu laptop con el
  simulador), `MOSTRAR_VIDEO = False` y `TRABAJADORES = 3`.
- `raspberry/conteo4_laptop.py` → `LAPTOP_URL = "192.168.1.33:8000"` y `LAPTOP_TOKEN = "cambia-esto"`.
- **Si la IP de tu laptop cambia** (el router a veces la cambia), corrige esas dos cosas (o usa
  las opciones `--laptop` y las cámaras en el comando, ver *Uso*). El programa de la laptop
  muestra siempre la IP actual en la pestaña **Conexión**. Para evitarlo, reserva una IP fija
  para la laptop en el router.

### 5. En la laptop: primera apertura
Doble clic en `laptop\interfaz\iniciar_interfaz.bat`. La primera vez instala PySide6 y lo
demás (tarda unos minutos). Cuando Windows pregunte por el **firewall**, marca
**"Redes privadas"** → **Permitir** (si lo niegas, la Pi no podrá conectarse ni leer las
cámaras simuladas).

### 6. Videos de prueba
Pon tus videos en `laptop\videos` (ver `laptop\videos\LEEME.txt`) o usa directamente los de
`V_final_final\laptop\videos` (prueba_1..4.mp4): si `laptop\videos` está vacía, el programa
los propone solo. Si son videos grandes, redúcelos antes:
```powershell
cd laptop
python preparar_videos.py MI_VIDEO.mp4 --salida videos
```

### 7. (Opcional) Que la Pi arranque sola al encenderse
En la Pi: edita `~/V_final_laptop/raspberry/conteo4-laptop.service` (cambia `pi` por tu
usuario y, si usas entorno virtual, `/usr/bin/python3` por el python de tu entorno, p. ej.
`/home/pi/yolo_env/bin/python`) y luego:
```bash
sudo cp ~/V_final_laptop/raspberry/conteo4-laptop.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now conteo4-laptop
```
No importa si la laptop todavía está apagada: la Pi reintenta sola cada pocos segundos.

---

## USO (cada vez)

1. **Laptop** → doble clic en `laptop\interfaz\iniciar_interfaz.bat`.
2. **Pestaña Conexión → Cámaras simuladas** → revisa los 4 videos (botón *Elegir…*), puerto
   inicial **8080**, y pulsa **Iniciar cámaras**. El indicador pasa a "Activo · 4 cámaras" y aparecen las
   direcciones `192.168.1.33:8080 …`.
3. **Raspberry** (si no hiciste el paso 7):
   ```bash
   cd ~/V_final_laptop/raspberry
   python conteo4_laptop.py
   ```
   Si la IP de la laptop no es la de `LAPTOP_URL`, copia el comando que muestra la pestaña
   **Conexión** (botón *Copiar*), que ya trae la IP, el puerto, el token y las cámaras:
   ```bash
   python conteo4_laptop.py --laptop 192.168.1.33:8000 --token-laptop cambia-esto 192.168.1.33:8080 192.168.1.33:8081 192.168.1.33:8082 192.168.1.33:8083
   ```
4. **Mira** la pestaña **En vivo**: "Raspberry en línea", las 4 cámaras con sus cajas, los
   conteos y la temperatura de la Pi. La pestaña **Historial** guarda todo por minuto.
5. **Terminar**: cierra el programa (también apaga las cámaras simuladas) y en la Pi `Ctrl+C`
   (o `sudo systemctl stop conteo4-laptop` si es servicio).

En la Pi, cada 10 s verás líneas como estas (útiles para saber si va bien):
```
[ESTADO] YOLO 7.8 inf/s | Cam1:5(1.9fps) | ... | total 21 | CPU 64°C
[LAPTOP] video: 40.0 cuadros/s (10.0 por cámara), 1450 KB/s (11.3 Mbps), ... | CPU del hilo 3.0%
```

## Ajustes útiles

| Dónde | Ajuste | Para qué |
|---|---|---|
| `raspberry/conteo4.py` | `FPS_MAX_POR_CAMARA = 2` | Menos CPU y temperatura en la Pi (por defecto 0 = sin límite). Recomendado si la Pi pasa de 75-80 °C |
| `raspberry/conteo4.py` | `TRABAJADORES = 3` | 3 redes NCNN de 1 hilo en paralelo (deja 1 núcleo libre). Aguanta la carga mucho mejor que una red de 3 hilos. 1 = como antes (`HILOS_NCNN`) |
| `raspberry/conteo4.py` | `MOTOR = "ncnn"`, `ENTRADA_RECTANGULAR = True` | YOLO directo con ncnn y entrada 320x192 (~1,7-2x más rápido, mismas detecciones). Si algo falla, `MOTOR = "ultralytics"` vuelve a lo de antes |
| `raspberry/conteo4.py` | `CAMARAS` | Direcciones de las cámaras (otra IP de la laptop, o cámaras reales) |
| `raspberry/conteo4_laptop.py` | `FPS_VIDEO_LAPTOP = 10` | Tope de cuadros/s de video por cámara; bájalo a 5-6 si el WiFi va justo (o `--fps-video 5`) |
| `raspberry/conteo4_laptop.py` | `LAPTOP_URL` / `LAPTOP_TOKEN` | IP:puerto de la laptop y token (o `--laptop` / `--token-laptop`) |
| Programa → Conexión | Puerto / token | Deben coincidir con los de la Pi. Cambiarlos reinicia el receptor |
| Programa → Conexión | Retraso de visualización | Es el MÍNIMO: si YOLO va lento sube solo (hasta 2,5 s) para que las cajas no salten. Más = más suave; menos = más inmediato |
| Programa → Conexión | Ancho / Cuadros/s / Calidad del simulador | 640 / 10 / 75 es lo recomendado: más grande = más red y más CPU en la Pi |
| Programa → En vivo | *Pausar video* | La Pi deja de mandar video (los conteos siguen) |

## Problemas comunes

| Síntoma | Qué revisar |
|---|---|
| La Pi: `No pude conectar a http://192.168.1.33:8080` | Cámaras no iniciadas (Uso, paso 2), IP de la laptop cambió, o firewall de Windows (paso 5) |
| La Pi: `[LAPTOP] Sin conexión con ws://...:8000/ws/pi` | ¿Está abierto el programa? ¿IP/puerto correctos en `LAPTOP_URL`? ¿Firewall? La Pi reintenta sola y guarda los datos mientras tanto |
| El programa: "Raspberry desconectada" | ¿Corre `conteo4_laptop.py` (no `conteo4.py`, que no envía nada)? ¿Token igual en ambos lados? |
| Se ven conteos pero no video | ¿Está en pausa (*Pausar video*)? El video solo se manda mientras la pestaña lo pide |
| «No se pudo abrir el puerto» en el programa | Otro programa usa el 8000 (¿quedó abierto otro Monitor?). Ciérralo o cambia el puerto en Conexión **y** en la Pi |
| Temperatura ≥ 80 °C | Ventilador / disipador, o `FPS_MAX_POR_CAMARA = 2` |
| `ModuleNotFoundError` en la Pi | `pip install -r requirements.txt` en el mismo entorno donde está ultralytics |
| La IP de la laptop no es 192.168.1.33 | Mírala en la pestaña Conexión (o `ipconfig`) y usa el comando que muestra el programa |

## Probar todo SOLO en la laptop (sin la Pi)
```powershell
# Terminal 1: el programa (y luego Conexión → Iniciar cámaras)
cd laptop\interfaz;  python app.py
# Terminal 2: hace de Raspberry (con YOLO real; hace falta pip install -r raspberry\requirements.txt)
cd raspberry;  python conteo4_laptop.py --laptop localhost 127.0.0.1:8080 127.0.0.1:8081 127.0.0.1:8082 127.0.0.1:8083
```
Sin YOLO (más liviano): `cd laptop\interfaz; python pi_falsa.py --destino localhost:8000 ..\videos\prueba_1.mp4`.
Solo la ventana con datos inventados: `python app.py --falso`.

## Notas
- Los ajustes del programa (videos elegidos, puerto del simulador…) se guardan en el registro
  de Windows como *MonitorTrafico / InterfazLaptopV*, separados de los de V_final_final.
- El historial se guarda en `laptop\interfaz\historial_laptop.db` (SQLite). Para empezar de
  cero, cierra el programa y borra ese archivo.
- El token solo evita que otro equipo de la red se haga pasar por la Pi; cámbialo en ambos
  lados si quieres.
