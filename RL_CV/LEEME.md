# RL_CV: conteo con YOLO + semáforo con RL, en la Raspberry Pi 4

Cada subcarpeta es una de las versiones de `V_final_final`, con el semáforo (RL) ya conectado:

| Carpeta | Conteo (YOLO) | Además | Semáforo |
|---|---|---|---|
| `RL_CV_sola` | `conteo4.py` | CSV por día | ✅ |
| `RL_CV_aws` | `conteo4_aws.py` | dashboard web en AWS | ✅ |
| `RL_CV_laptop` | `conteo4_laptop.py` | programa de escritorio en la laptop | ✅ |
| `RL_CV_final` | `agente.py` → `conteo4_nube.py` | laptop + AWS + control remoto | ✅ |

A la Pi se copia solo la carpeta `raspberry/` de la versión elegida (ya trae `semaforo/` adentro).

> **Videos de prueba:** no están en GitHub (pesan ~117 MB). Copia tus `prueba_1.mp4` … `prueba_4.mp4` a `laptop/videos/` de la versión que uses.

## Cómo están unidos

```
cámaras ──▶ PROCESO 1: conteo YOLO (conteo4...)          PROCESO 2: semaforo/principal_pi.py
            ├─ envío a laptop / AWS (según versión)           (agente DDQN + GPIO → LEDs)
            └─ rl_conteos.py: servidor 127.0.0.1:5050 ◀── TCP ──┘  se conecta, recibe conteos N/S/E/O
```

- **Dos procesos**, como pide el contrato (`docs/CONTRATO_RL.md`). YOLO usa casi toda la CPU y el semáforo necesita un reloj de 1 s exacto. Probado con 2 núcleos al 100 %: el reloj del semáforo no se atrasó nunca.
- **El puente (`rl_conteos.py`) va dentro del proceso de conteo.** Reutiliza las cajas que YOLO ya calcula, así que no hay inferencias extra, ni otro programa que arrancar, ni RAM adicional.
- **Por cada calle se cuentan los vehículos que hay ahora** dentro de su zona (polígono de 60 m antes de la línea de parada), solo en las clases elegidas. Se manda 1 conteo por segundo, con 2 s de antigüedad (lo mismo que en el entrenamiento).
- **Si una cámara se cae, no se mandan ceros falsos.** El semáforo pasa solo a su ciclo fijo de respaldo y vuelve al agente cuando regresan los conteos.
- **Los dos lados se reconectan solos**, sin importar en qué orden arranquen o si se reinicia el conteo.

## Lo que tienes que configurar: `raspberry/rl_zonas.json`

1. **`calles`**: qué cámara mira cada calle. Por defecto `N=Cam1, S=Cam2, E=Cam3, O=Cam4`. Confirma en campo cuál acceso de Universitaria es N y cuál S.
2. **`zonas`**: dibújalas en la laptop, con las cámaras encendidas o con un video:
   ```powershell
   cd RL_CV_final\laptop
   python definir_zonas.py                              # cámaras simuladas 127.0.0.1:8080..8083
   python definir_zonas.py Cam1=videos\prueba_1.mp4 Cam2=192.168.1.40:8080 ...
   ```
   Clic = punto · clic derecho = quitar · Enter = guardar · t = todo el cuadro. Guarda en `raspberry/rl_zonas.json`. Luego cópialo a la Pi. Sin zonas se cuenta todo el cuadro, y el conteo sale más alto de lo que vio el agente en el entrenamiento.
3. **`categorias`**: por defecto `carro, bus, pesado, especial`. **`moto` está pendiente**: agrégala solo si la demanda de SUMO del entrenamiento tenía motos.

`semaforo/pi.json` ya viene para YOLO en la misma Pi (`127.0.0.1:5050`, `enviar_luces: true` para la interfaz). Revisa ahí los pines GPIO.

## Ver el semáforo en las interfaces (se activa con true / false)

Muestra en vivo **quién controla** (agente RL, ciclo fijo o recuperando), las **luces de cada calle** sobre un dibujo del cruce, la fase en verde y cuánto falta para la próxima decisión, los **conteos que ve el agente**, su **última decisión con el valor Q de cada fase**, las decisiones recientes y una **línea de tiempo** de 15 min. Si algo falla (semáforo apagado, YOLO sin conteos, Pi desconectada) lo dice con un aviso.

**Encender o apagar:** en la Pi, `raspberry/rl_zonas.json`:
```json
"interfaz": true      // true = se muestra · false = no se muestra
```
Reinicia el conteo para que tome el cambio. El semáforo funciona igual en los dos casos: la interfaz solo **muestra**, nunca cambia el control. (También hace falta `"enviar_luces": true` en `semaforo/pi.json`, que ya viene así.)

Dónde aparece, según la versión:

| Versión | Dónde se ve |
|---|---|
| `RL_CV_sola` | ventana **"Semaforo (RL)"** al lado del video, con `conteo4.py --pantalla` |
| `RL_CV_aws` | sección **"Semáforo inteligente (RL)"** en el dashboard web + chip en la cabecera |
| `RL_CV_laptop` | pestaña **"Semáforo"** del programa de escritorio + chip en la cabecera |
| `RL_CV_final` | las dos: pestaña en la laptop y sección en la web |

Con `"interfaz": false` la pestaña o la sección **no aparecen**. Para ver la pestaña sin Raspberry: `python app.py --falso` (semáforo simulado).

Para AWS hay que **volver a subir el servidor** (`aws/servidor.py` y `aws/static/index.html` cambiaron).

## Instalar en la Pi (una vez)

```bash
# en la laptop (PowerShell, dentro de RL_CV\RL_CV_final, o la versión que uses):
scp -r raspberry pi@IP_DE_LA_PI:~/RL_CV_final/
# en la Pi:
source ~/TU_ENTORNO/bin/activate          # el entorno donde está YOLO (si usas uno)
cd ~/RL_CV_final/raspberry
pip install -r requirements.txt
bash instalar_rl.sh
```

`instalar_rl.sh` detecta la versión y deja dos servicios que arrancan solos al encender la Pi:
- **Conteo**: `conteo4-rl` (sola/aws/laptop) o el agente `conteo4-agente` (final; el conteo se inicia con "Iniciar todo" desde la laptop o la web). Además apaga los servicios viejos para no contar dos veces.
- **`semaforo-rl`**: `principal_pi.py --modo tiempo_real --velocidad 1`, con prioridad de CPU alta. Al detenerlo pone **todo en rojo** y suelta los pines: usa SIGINT, porque con SIGTERM los LEDs quedarían como estaban.

Para el semáforo usa el `python3` del sistema, que necesita `numpy` y `gpiozero`: el script los instala con apt si faltan.

## Comprobar que funciona

```bash
sudo journalctl -u semaforo-rl -f          # "fuente conectada ... (cámaras): YOLO 4 cámaras"
sudo journalctl -u conteo4-rl -f | grep RL # "[RL] Semáforo conectado ... Primer conteo {...}"
```
En `semaforo/registros/pi_*.csv` revisa que `control` sea `agente`, que `edad_conteos_s` sea ≤ 5 y que los `conteo_N..O` coincidan con lo que se ve. El contrato pide medirlo **30 min seguidos en la Pi real**, con las 4 cámaras.

Es normal ver al arrancar el aviso "la fuente no envía huella": el contrato lo indica así.

## Probado (en la PC, con las 4 cámaras simuladas y el `principal_pi.py` real)

| Prueba | Resultado |
|---|---|
| Las 4 versiones de punta a punta | agente 100 % del tiempo, 1 conteo/s, 0 s de atraso |
| Cámaras apagadas 40 s | deja de mandar → respaldo a los 15 s exactos → recuperando al volver |
| Conteo (YOLO) reiniciado | se reconecta solo |
| Semáforo arrancado antes que YOLO | espera y se conecta cuando YOLO está listo |
| Mensajes basura, calles distintas, `fin`, puerto ocupado, config inválida | todos manejados (validados con el `enlace.py` del semáforo) |
| Versión final completa en 2 núcleos al 100 % (YOLO + video a laptop y AWS + semáforo) | reloj del semáforo sin atrasos; laptop 10 cuadros/s por cámara; web sin cortes |
| Interfaz del semáforo, las 4 versiones con YOLO + `principal_pi.py` reales | laptop y web muestran "Agente RL", luces y decisiones; semáforo apagado → "Desconectado" en ambas; `interfaz: false` → no se publica nada y los conteos siguen igual; reloj del semáforo sin atrasos |
| Pruebas propias del semáforo (`principal_pi.py --probar`) | 8/10 OK, 1 aviso (gpiozero no está en la PC), 1 omitida (solo PC de entrenamiento) |

**Error encontrado y corregido en las pruebas:** en la versión `sola`, un fallo del puente detenía el hilo de YOLO completo. Ahora cualquier error del enlace con el semáforo se registra y el conteo sigue.

## Pendiente de decidir (ver `docs/CONTRATO_RL.md`)

- Contar motos o no.
- Qué cámara es cada calle (N/S en Universitaria) y las zonas de 60 m en las cámaras reales.
- Confirmar que la cámara real ve 60 m (si ve menos, el modelo no corresponde).
