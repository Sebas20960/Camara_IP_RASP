# Protocolo v2 — Raspberry → receptores (AWS y laptop)

Contrato común entre: la Raspberry (`raspberry/`), el servidor AWS + dashboard web (`aws/`) y el programa de escritorio de la laptop (`laptop/interfaz/`). **Nadie cambia este contrato sin avisar.**

## Escenario

```
LAPTOP (misma red que la Pi)                            RASPBERRY PI 4 (4 GB)
camfalsa4.py: 4 cámaras MJPEG (8080..8083) ───────────▶ conteo4 (YOLO, 1 modelo, 4 cámaras)
                                                          │
programa de escritorio (laptop/interfaz)  ◀── WebSocket ───┤  destino 1: laptop  (ws://IP_LAPTOP:8000/ws/pi)
                                                          │
servidor AWS (nube/servidor) ◀────────── WebSocket ───────┘  destino 2: AWS     (ws://3.144.43.2/ws/pi)
        └──▶ navegadores (dashboard web)
```

La Pi es SIEMPRE el cliente: abre una conexión WebSocket a cada destino (`/ws/pi?token=TOKEN`). Destinos independientes: cada uno con su conexión, reconexión, buffer sin internet y demanda de video.

## Idea clave para video fluido sin cargar la Pi

1. La Pi ya recibe cada cuadro de cada cámara como **JPEG** (MJPEG). Lo **reenvía tal cual** (sin decodificar ni recomprimir → casi 0 CPU), limitado a N cuadros/s por cámara y SOLO si el destino tiene a alguien viendo.
2. Por cada análisis de YOLO manda un mensaje `det` con las cajas, ligado al **número de secuencia (seq)** del cuadro analizado.
3. El receptor muestra el video con **~1 s de retraso** y **interpola** las cajas entre dos análisis por ID de ByteTrack → las cajas se mueven suaves aunque YOLO analice cada cámara solo ~2 veces/s.

## Mensajes Pi → receptor

Texto (JSON UTF-8):

| tipo | cuándo | contenido |
|---|---|---|
| `hola` | al conectar | `{"tipo":"hola","version":2,"camaras":["Cam1","Cam2","Cam3","Cam4"]}` |
| `datos` | cada 1 s (siempre) | **igual que v1** (no cambia, el historial depende de esto): `{"tipo":"datos","t":<epoch>,"fps":<inf/s>,"temp":<°C o null>,"total":<int>,"camaras":[{"nombre":str,"conectada":bool,"total":int,"por_categoria":{cat:int},"nuevos":{cat:int},"vistos":int,"fps":float}]}` |
| `lote` | al reconectar | `{"tipo":"lote","muestras":[<datos>, ...]}` (lo guardado sin internet) |
| `semaforo` | ~1 por segundo (y cada 3 s aunque no cambie), **solo si** `rl_zonas.json` tiene `"interfaz": true` | estado del semáforo con RL: `{"tipo":"semaforo","n":<contador>,"t":<epoch Pi>,"edad":<s desde que se generó, medido en la Pi>,"conectado":bool,"enviando":bool,"motivo":str\|null,"calles":{"N":"Cam1",...},"luces":{"N":"V\|A\|R",...}\|null,"estado":{control, fase, etapa, t_fase, prox_decision_s, rojo[], conteos[], edad_conteos_s, decision{t,quien,pedida,ejecutada,anulada,paso_s,cambio,q[],mascara[]}, config{fases[],calles[],ambar_s,...}}\|null}`. Solo en vivo: no se guarda en el historial. Para saber si está viejo se usa `edad` (no depende de los relojes). |
| `det` | cada análisis de YOLO, **solo si el destino pidió video** | `{"tipo":"det","c":<idx cámara>,"s":<seq>,"t":<t_cap>,"b":[[x1,y1,x2,y2,cat,id,num,conf], ...]}` |

En `det.b`:
- `x1,y1,x2,y2`: enteros **0..10000** normalizados al ancho/alto del cuadro (independiente de la resolución).
- `cat`: índice en `CATS = ["carro","moto","bus","pesado","especial"]`.
- `id`: ID de ByteTrack (int) o `-1` si no tiene.
- `num`: número por categoría (el "id:N" de la etiqueta) o `0` si aún es provisional.
- `conf`: confianza 0..100 (entero).

Binario (cuadro de video), **solo si el destino pidió video**:

```
byte 0       : 0x01 (tipo = cuadro JPEG)
byte 1       : índice de cámara (0..3)
bytes 2..5   : seq, uint32 big-endian
bytes 6..13  : t_cap, float64 big-endian (time.time() de la Pi cuando LLEGÓ ese JPEG)
bytes 14..   : JPEG
```

- `seq`: contador por cámara de los JPEG que la Pi recibe de esa cámara (crece de 1 en 1; los reenviados son un subconjunto).
- Un `det` y un cuadro con el mismo `(c, s)` son **exactamente el mismo cuadro** → caja exacta.
- Todos los `t` son reloj de la Pi. El receptor NO necesita sincronizar relojes (ver interpolación).

## Mensajes receptor → Pi

`{"tipo":"visores","video":<n>,"total":<m>,"fps":<F>}`
- `video`: cuántos quieren video. `0` → la Pi NO manda `det` ni cuadros a ese destino.
- `fps` (opcional): cuadros/s por cámara que el receptor quiere. La Pi usa `min(fps, su tope para ese destino)`. Si falta, usa su tope.
- `ack` (opcional, `true`): el receptor confirma el video recibido (ver abajo). Sin esta clave la Pi manda como antes (sin control de flujo).
- El receptor lo manda al conectar la Pi y cada vez que cambia.

`{"tipo":"ack","b":<bytes>}` — **control de flujo**. Tras cada cuadro binario, el receptor manda el total de bytes de cuadros (cabecera incluida) que recibió en ESTA conexión. La Pi no deja más de una "ventana" de bytes sin confirmar (empieza en 160 KB y se ajusta sola entre 80 KB y 1.5 MB según la demora medida); si la red no da, salta cuadros (siempre sale el más reciente). Así el retraso no crece sin límite y los `datos` y pings no quedan atrapados detrás del video (eso cortaba la conexión con AWS cuando la subida de internet era menor que el video pedido).

## Servidor AWS → navegadores

Reenvía tal cual los `det` (texto) y los cuadros (binario) SOLO a los visores con video activo, más `datos`, `semaforo` y `pi` como en v1 (a quien recién abre le manda el último `semaforo`). El navegador interpola igual que la laptop.

## Algoritmo de visualización con interpolación (igual en JS y en Python)

Por cámara:
- `cuadros`: cola de `(seq, t, jpeg)` de los últimos ~4 s.
- `dets`: cola de `(seq, t, cajas)` de los últimos ~5 s.
- `t_pi`: mayor `t` recibido (cuadro o det) y `local_llegada`: reloj local cuando llegó.

Cada repintado (~30 veces/s):
1. `pi_ahora = t_pi + (reloj_local - local_llegada)` (avanza solo aunque no lleguen mensajes).
2. `T = pi_ahora - RETRASO`. `RETRASO` por defecto 1.0 s (adaptativo opcional: 1.5 × mediana del intervalo entre `det`, limitado a 0.4–2.5 s).
3. **Cuadro**: el último con `t ≤ T` (si no hay, el más viejo). Llamemos `tc` a su `t`.
4. **Cajas en `tc`** (el instante del cuadro mostrado, NO en T: así coinciden exactamente con la imagen; si ese cuadro fue analizado, la caja es la exacta): `d0` = último det con `t ≤ tc`; `d1` = primer det con `t > tc`.
   - Si existen ambos y `d1.t - d0.t < 2.0`: para cada `id ≥ 0` presente en los dos → interpolación lineal con `a = (tc - d0.t) / (d1.t - d0.t)`. Los IDs solo en `d0` → caja de `d0`. Cajas con `id = -1` → las de `d0`. La etiqueta (cat, num, conf) sale de `d0`.
   - Si `d1` no existe (o está a ≥ 2.0 s) y `tc - d0.t < 1.5` → cajas de `d0`. Si no → sin cajas.
5. Etiqueta: `"id:{num} {cat} {conf/100:.2f}"`, o `"{cat} {conf/100:.2f}"` si `num = 0`. Color por categoría (BGR del programa original): carro `#28aaeb`, moto `#fac828`, bus `#d25ac8`, pesado `#ff821e`, especial `#eb3c46` (RGB hex).

## Límites para la Raspberry (no sobrecalentarla)

- Reenviar el JPEG original: **sin decodificar/recomprimir**. El único trabajo extra por cuadro es copiar bytes al socket.
- Tope de cuadros/s por cámara y por destino (laptop en LAN: ~10; AWS: menor, por la subida).
- Un hilo de envío por destino. Si un destino va lento, se **descartan** cuadros viejos (cola de 1 por cámara); nunca se frena a YOLO ni al otro destino.
- Sin nadie viendo en un destino → 0 cuadros y 0 `det` hacia él.
- Fuentes que no son MJPEG (rtsp, archivos): como no hay JPEG original, se puede comprimir el cuadro ya analizado (pequeño), solo a la tasa de análisis.

## Control remoto (agente, `/ws/agente`)

`raspberry/agente.py` corre siempre en la Pi (servicio `conteo4-agente`) y, como cliente, abre `/ws/agente?token=TOKEN` en la laptop y en AWS (mismos tokens que `/ws/pi`). Lanza y vigila `conteo4_nube.py`.

Agente → controlador (texto JSON):

| tipo | contenido |
|---|---|
| `agente` | cada 5 s y al cambiar: `{"conteo":"detenido|arrancando|corriendo|deteniendo|error","deseado","detalle","pid","desde","reinicios","camaras":[..]|null,"laptop":"ip:puerto","laptop_conectada","nube_conectada","sim":{activo,listo,urls,error,t}|null,"sistema":{host,temp,ram_libre_mb,ram_total_mb,carga,disco_libre_gb,encendida_s,throttled}}` |
| `log` | `{"lineas":[...],"completo":bool}` (completo = reemplaza todo el registro) |
| `respuesta` | `{"accion","ok","msg"}` resultado de una orden (va a todos los controladores) |
| `sim` | solo a la laptop: `{"accion":"iniciar"|"detener"|"estado"}` (cámaras simuladas) |

Controlador → agente: `{"tipo":"orden","accion":A}` con A ∈ `iniciar` (opcional `"camaras":[..]` o `null` = CAMARAS de conteo4.py; sin la clave = últimas usadas), `detener`, `reiniciar`, `iniciar_todo`, `camaras_iniciar`, `camaras_detener`, `reiniciar_pi`, `apagar_pi`, `log`. La laptop además manda `{"tipo":"sim_estado","activo","listo","urls","error"}`.

Navegador → AWS (`/ws/web`): `{"clave":"..."}` desbloquea; `{"orden":{"accion":A}}` se reenvía al agente. AWS responde `{"tipo":"control","ok","msg"}` y reenvía `agente` (sin IPs internas a quien no tiene clave), `log` y `respuesta` (solo a quien tiene clave).

Descubrimiento: la laptop manda cada 3 s por broadcast UDP al puerto **37020** `{"app":"monitor-trafico","v":1,"puerto":8000,"ip":...}`; el agente usa la IP de origen.

## Puertos y tokens

- Laptop (programa de escritorio): escucha en `0.0.0.0:8000`, ruta `/ws/pi`, token por defecto `cambia-esto` (configurable).
- AWS: `ws://3.144.43.2/ws/pi` (puerto 80), con su token.
