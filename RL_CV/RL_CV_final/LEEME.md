> **RL_CV_final: esta versión incluye el semáforo con RL.** Para lo del semáforo (zonas, `rl_zonas.json`, `bash instalar_rl.sh`) mira [`../LEEME.md`](../LEEME.md) y [`docs/CONTRATO_RL.md`](docs/CONTRATO_RL.md). Lo de abajo es la guía original de V_final_final (donde diga `V_final_final`, en la Pi usa `RL_CV_final`; para instalar usa `instalar_rl.sh` en vez de los servicios de abajo).

# V_final_final — Conteo de vehículos con YOLO (Raspberry Pi 4 + laptop + AWS)

## ¿Qué versión uso?

| Carpeta | En la Pi se ejecuta | Incluye | Úsala si… |
|---|---|---|---|
| **esta carpeta** (raspberry/, laptop/, aws/) | `agente.py` (que lanza `conteo4_nube.py`) | YOLO + cámaras simuladas + programa de laptop + AWS **a la vez** + **control remoto** (encender/apagar todo desde la laptop o la web) | quieres todo |
| [`V_final_sola/`](V_final_sola/LEEME.md) | `conteo4.py` | YOLO + cámaras simuladas (sin interfaz; guarda un CSV por día) | solo contar, lo más liviano para la Pi |
| [`V_final_aws/`](V_final_aws/LEEME.md) | `conteo4_aws.py` | lo de "sola" + servidor AWS (dashboard web) | verlo desde cualquier lugar por internet |
| [`V_final_laptop/`](V_final_laptop/LEEME.md) | `conteo4_laptop.py` | lo de "sola" + programa de escritorio en la laptop | verlo en tu laptop, sin internet ni AWS |

Cada subcarpeta es independiente y tiene su propio `LEEME.md` con el paso a paso. Todas usan los videos de `laptop/videos/` de esta carpeta (o los que pongas en su propia `laptop/videos/`). A la Pi solo se copia la carpeta `raspberry/` de la versión elegida.

## Qué hay en cada carpeta

```
V_final_final/
├── raspberry/        ← VA EN LA RASPBERRY
│   ├── instalar_pi.sh          ✅ SE EJECUTA UNA VEZ: deja el agente como servicio
│   ├── agente.py               "mando a distancia": siempre encendido, lanza/vigila el conteo
│   ├── conteo4_nube.py         el conteo (YOLO + envío a laptop y AWS); lo lanza el agente
│   ├── conteo4.py              motor: 4 cámaras + YOLO + conteo (aquí van CAMARAS y ajustes)
│   ├── motor_ncnn.py           YOLO directo con ncnn (más rápido que Ultralytics). Va junto a conteo4.py
│   ├── cliente_nube.py         envía datos y video a la laptop y a AWS
│   ├── modelo_5clases_ncnn_model/   el modelo YOLO (NCNN)
│   ├── conteo4-nube.service    (OBSOLETO: lo reemplaza el agente)
│   └── requirements.txt
├── laptop/           ← SE QUEDA EN TU LAPTOP
│   ├── interfaz/
│   │   ├── iniciar_interfaz.bat    ✅ DOBLE CLIC: abre el programa "Monitor de tráfico"
│   │   ├── app.py                  la ventana (En vivo / Historial / Control / Conexión)
│   │   ├── nucleo.py               recibe a la Pi, interpola las cajas, guarda el historial
│   │   ├── pi_falsa.py             (pruebas) imita a la Pi sin tenerla
│   │   └── _nucleo_falso.py        (pruebas) datos falsos: python app.py --falso
│   ├── camfalsa4.py            las 4 cámaras IP simuladas (el programa lo lanza solo)
│   ├── preparar_videos.py      reduce videos de YouTube a 640 px / 10 FPS
│   └── videos/                 tus videos de prueba (prueba_1..4.mp4)
├── aws/              ← VA EN EL SERVIDOR DE AWS (ya está instalado en 3.144.43.2)
│   ├── servidor.py, static/index.html, instalar_aws.sh, requirements.txt
│   └── LEEME.md                cómo actualizarlo
└── docs/             PROTOCOLO_v2.md (cómo se comunican las partes) y API_NUCLEO.md
```

## Cómo funciona

```
LAPTOP (192.168.1.33)                                     RASPBERRY PI 4 (misma red WiFi/cable)
camfalsa4.py: 4 cámaras (puertos 8080..8083) ───────────▶ conteo4_nube.py (YOLO en las 4 cámaras)
Programa "Monitor de tráfico" (puerto 8000) ◀── datos + video (10 cuadros/s por cámara) ──┤
                                AWS http://3.144.43.2 ◀── datos + video (4 cuadros/s) ────┘

Control:  programa de la laptop ◀──┐
                                   ├── agente.py (siempre encendido en la Pi; lanza y vigila conteo4_nube.py)
          dashboard web (AWS) ◀────┘   órdenes: iniciar / detener / reiniciar / cámaras / reiniciar o apagar la Pi
```

La Pi reenvía el JPEG de cada cámara **tal cual** (casi sin gastar CPU) más las cajas de YOLO. La laptop y la web muestran el video con ~1 s de retraso y **mueven las cajas suavemente** entre análisis. Si nadie está mirando un destino, a ese destino no se le manda video.

El **agente** es un programa chiquito (~20 MB de RAM, no carga YOLO) que se conecta él mismo a la laptop y a AWS, así que funciona detrás del router sin abrir puertos. Además:
- **Encuentra solo la IP de la laptop**: el programa de la laptop la anuncia por la red local cada 3 s. Si el router le cambia la IP, la Pi se reconecta sola.
- **Revive el conteo** si se cae y, tras un corte de luz, lo deja como estaba (corriendo o detenido).
- Desde la web puede **encender las cámaras simuladas de la laptop** (le pasa la orden a la laptop por la red local), siempre que el programa de la laptop esté abierto.

---

## PREPARACIÓN (una sola vez)

### 1. En la Raspberry: averigua su IP y usuario
```bash
hostname -I      # ej. 192.168.1.50
whoami           # ej. pi
```
Activa SSH si no lo está: `sudo raspi-config` → *Interface Options* → *SSH* → *Enable*.

### 2. Copia la carpeta `raspberry` a la Pi
En la Pi: `mkdir -p ~/V_final_final`. Luego, en tu laptop (PowerShell, dentro de `V_final_final`), cambiando `pi@192.168.1.50` por lo del paso 1:
```powershell
scp -r raspberry pi@192.168.1.50:~/V_final_final/
```
(o cópiala con una USB a `~/V_final_final/raspberry`).

### 3. Instala las librerías en la Pi
Dentro del mismo entorno virtual donde usas YOLO (si usas uno):
```bash
cd ~/V_final_final/raspberry
pip install -r requirements.txt
```

### 4. Revisa la configuración (ya viene lista)
- `raspberry/conteo4_nube.py` → `LAPTOP_URL = "192.168.1.33:8000"`, `NUBE_URL = "http://3.144.43.2"` y tu `NUBE_TOKEN`. El agente usa esta misma configuración.
- `raspberry/conteo4.py` → `CAMARAS`: solo se usan si eliges "las cámaras configuradas en la Raspberry" (cámaras reales). Con las simuladas, la laptop le pasa las direcciones sola.
- Si la IP de la laptop cambia, **ya no hay que corregir nada**: el agente la encuentra por la red local.

### 5. Actualiza el servidor de AWS
Ver `aws/LEEME.md` (tres comandos desde PowerShell). Al final te muestra la **clave de control** para manejar la Pi desde la web: guárdala.

### 6. En la laptop: primera apertura
Doble clic en `laptop\interfaz\iniciar_interfaz.bat`. La primera vez instala PySide6 (tarda unos minutos). Cuando Windows pregunte por el **firewall**, marca **"Redes privadas"** → **Permitir** (si lo niegas, la Pi no podrá conectarse).

### 7. Instala el agente en la Pi (la última vez que entras a la Pi)
Con el entorno virtual de YOLO activado (si usas uno):
```bash
source ~/TU_ENTORNO/bin/activate
cd ~/V_final_final/raspberry
bash instalar_pi.sh
```
Deja el agente como servicio (arranca solo al encender la Pi), apaga el servicio viejo `conteo4-nube` si lo tenías y da permiso para reiniciar/apagar la Pi desde lejos. Desde ahora todo se maneja desde la laptop o la web.

---

## USO (cada vez)

**Desde la laptop:**
1. Enciende la Pi y doble clic en `laptop\interfaz\iniciar_interfaz.bat`.
2. Pulsa **Iniciar todo** (arriba a la derecha). Enciende las cámaras simuladas con los videos de `laptop\videos\` y le dice a la Pi que cuente con ellas. La primera vez revisa los videos en **Conexión → Cámaras simuladas**.
3. Mira la pestaña **En vivo**. La pestaña **Control** muestra el estado de la Pi (temperatura, RAM, si se frena por calor o mala fuente), su registro y los botones para reiniciar o apagar.
4. **Detener todo** para el conteo y las cámaras. Cerrar el programa apaga las cámaras simuladas, pero la Pi sigue encendida esperando órdenes.

**Desde cualquier lugar (web):** abre `http://3.144.43.2`, escribe la **clave de control** en la tarjeta *Raspberry Pi* y usa los botones. **Iniciar todo** también enciende las cámaras simuladas de la laptop si su programa está abierto. Sin la clave, la web solo muestra el estado.

**Sin agente (modo manual, como antes):** inicia las cámaras en *Conexión* y en la Pi ejecuta `python conteo4_nube.py`.

En el registro de la Pi (pestaña Control o la web), cada 10 s verás líneas como estas, útiles para saber si va bien:
```
[ESTADO] YOLO 7.8 inf/s | Cam1:5(1.9fps) | ... | total 21 | CPU 64°C
[NUBE] video: 40.0 cuadros/s (10.0 por cámara), ... | CPU del hilo 6.1%
```

## Ajustes útiles

| Dónde | Ajuste | Para qué |
|---|---|---|
| `raspberry/conteo4.py` | `FPS_MAX_POR_CAMARA = 2` | Menos CPU y temperatura en la Pi (por defecto 0 = sin límite) |
| `raspberry/conteo4.py` | `TRABAJADORES = 3` | 3 redes NCNN de 1 hilo en paralelo (deja 1 núcleo libre). Aguanta la carga mucho mejor que una red de 3 hilos. 1 = como antes (`HILOS_NCNN`) |
| `raspberry/conteo4.py` | `MOTOR = "ncnn"`, `ENTRADA_RECTANGULAR = True` | YOLO directo con ncnn y entrada 320x192 (~1,7-2x más rápido, mismas detecciones). Si algo falla, `MOTOR = "ultralytics"` vuelve a lo de antes |
| `raspberry/conteo4_nube.py` | `FPS_VIDEO_NUBE = 4` | Bájalo a 2 si el internet de la Pi sube poco |
| `raspberry/conteo4_nube.py` | `NUBE_URL = ""` / `LAPTOP_URL = ""` | Desactivar un destino |
| Programa → Conexión | Retraso de visualización | Es el MÍNIMO: si YOLO va lento sube solo (hasta 2,5 s) para que las cajas no salten. Más = más suave; menos = más inmediato |

## Problemas comunes

| Síntoma | Qué revisar |
|---|---|
| La Pi: `No pude conectar a http://192.168.1.33:8080` | Cámaras no encendidas o firewall de Windows |
| Control: "Agente desconectado" | ¿Pi encendida y en la misma red? ¿Hiciste `bash instalar_pi.sh`? En la Pi: `sudo journalctl -u conteo4-agente -n 50` |
| El programa: "Raspberry desconectada" (con el agente conectado) | Mira el registro en la pestaña Control: el conteo puede estar cargando el modelo (~20 s) o con error |
| Reiniciar/apagar la Pi dice "Sin permiso" | Vuelve a ejecutar `bash instalar_pi.sh` |
| AWS no muestra nada nuevo | ¿Actualizaste el servidor (`aws/LEEME.md`)? ¿La Pi tiene internet? ¿`NUBE_TOKEN` correcto? |
| Temperatura ≥ 80 °C | Ventilador / disipador, o `FPS_MAX_POR_CAMARA = 2` |
| `ModuleNotFoundError` en la Pi | `pip install -r requirements.txt` en el mismo entorno donde está ultralytics |

## Probar todo SOLO en la laptop (sin la Pi)
```powershell
# Terminal 1: el programa
cd laptop\interfaz;  python app.py
# Terminal 2: el agente hace de Raspberry (con YOLO real). Luego pulsa "Iniciar todo" en el programa.
cd raspberry;  python agente.py --sin-autoinicio --nube "" --laptop 127.0.0.1:8000
```
Ver la pestaña Control sin nada más: `python app.py --falso` (agente simulado).

## Seguridad
- **No guardes la llave `.pem` de AWS dentro de estas carpetas** (está en `C:\Users\SEBASTIAN\.ssh\`).
- La Pi necesita token para enviar datos a AWS y a la laptop. El dashboard web es público para quien tenga la IP, pero **manejar la Pi exige la clave de control** (5 intentos fallidos cortan la conexión).
- La web va por `http` (sin cifrar): la clave viaja visible en la red. Para HTTPS hace falta un dominio + Caddy (ver sugerencias). No uses esa clave en otra cuenta.
