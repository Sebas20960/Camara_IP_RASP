# Contrato de conteos: programa YOLO → semáforo (Raspberry Pi)

Versión del protocolo: **1** · Fecha: 23 nov 2026

> **Implementación en RL_CV** (`raspberry/rl_conteos.py`, configurado en `raspberry/rl_zonas.json`). Cumple este contrato, probado contra el `enlace.py` real del semáforo. Hay tres decisiones que lo hacen más seguro, todas configurables:
> 1. **`listo` solo con conteos reales.** Si YOLO todavía está cargando, no manda `{"N":0,...}`: espera hasta 8 s y, si sigue sin conteos, cierra la conexión, y el semáforo reintenta a los 5 s. Un cero falso le haría creer al agente que las calles están vacías.
> 2. **No manda conteos de una cámara caída.** Tampoco manda ceros: deja de enviar, y el semáforo pasa solo a respaldo a los 15 s (sección 6).
> 3. **No manda `fin` al cerrarse** (`fin_al_cerrar: false`). `principal_pi.py` se detiene del todo al recibir `fin` y, al volver a arrancar, pasa 60 s en rojo. Sin `fin`, el semáforo sigue en respaldo y se reconecta cuando el conteo vuelve, por ejemplo si el agente lo reinicia.
>
> **Interfaz (RL_CV):** con `"enviar_luces": true` en `semaforo/pi.json`, el semáforo manda cada segundo el mensaje `luces` (sección 4.4) con un campo opcional de más, `estado` (control, fase, decisión del agente con sus valores Q...). El contrato no cambia: `enlace.validar` ignora campos de más y el conteo nunca le contesta nada. `rl_conteos.py` solo lo reenvía para mostrarlo (laptop, web, ventana de conteo4) si `rl_zonas.json` tiene `"interfaz": true`. Luces mal formadas se ignoran sin cortar la conexión.
>
> Retraso: `retraso_min_s = 2` manda el conteo de hace ≥ 2 s, igual que `sumo.retraso_conteo_s` del entrenamiento (sección 8: 2–6 s).

---

## 1. Arquitectura

```
Servidor de video (cámaras)
        │  RTSP / HTTP  (YOLO es CLIENTE del video)
        ▼
yolo_conteos.py  ── TCP 127.0.0.1:5050 ──▶  principal_pi.py  ──▶  GPIO → transistores → LEDs
 (proceso 1, en la Pi)   YOLO escucha el puerto     (proceso 2, en la Pi)
```

- Son **dos programas separados** en la misma Raspberry Pi 4. No se debe meter YOLO dentro de `principal_pi.py`: YOLO tarda segundos por ronda y alargaría el reloj de 1 s del semáforo (ámbar y verdes).
- **YOLO abre el puerto 5050 y espera** (`127.0.0.1`, puerto 5050). `principal_pi.py` se conecta a él.
- Si YOLO se cae o se cuelga, el semáforo sigue funcionando con un ciclo fijo de respaldo (ver sección 6).

(En RL_CV el proceso 1 es el conteo de cada versión, `conteo4.py` / `conteo4_aws.py` / `conteo4_laptop.py` / `conteo4_nube.py`, con `rl_conteos.py` adentro.)

---

## 2. Transporte

| Regla | Valor |
|---|---|
| Protocolo | TCP |
| Dirección | `127.0.0.1` (misma Pi) |
| Puerto | `5050` |
| Formato | **JSON Lines**: un objeto JSON por línea, UTF-8, terminado en `\n` |
| Tamaño máximo por mensaje | 64 KB |
| Cifrado | Ninguno (texto plano; no sale de la Pi) |
| Quién inicia | `principal_pi.py` se conecta; YOLO acepta |

Cada mensaje debe ir **completo en una sola línea**. Nada de JSON con saltos de línea internos (no usar `indent`).

---

## 3. Calles

| Clave | Calle |
|---|---|
| `"N"` | Norte (Universitaria, acceso norte) |
| `"S"` | Sur (Universitaria, acceso sur) |
| `"E"` | Este (Dulanto) |
| `"O"` | Oeste (La Mar) |

> Confirmar en campo qué acceso de Universitaria es "N" y cuál "S" según la orientación de las cámaras.

- El orden de las claves dentro del objeto no importa.
- Si falta o sobra una clave, la Pi **rechaza el mensaje y corta la conexión**.

---

## 4. Mensajes

### 4.1 La Pi saluda al conectarse (Pi → YOLO)

```json
{"tipo":"hola","version":1,"calles":["N","S","E","O"],"huella":"8fb49c380481222c","modo":"tiempo_real","t":0,"modelo":"b903818f8a1f6558"}
```

### 4.2 YOLO responde (YOLO → Pi), **en menos de 10 s**

```json
{"tipo":"listo","version":1,"calles":["N","S","E","O"],"huella":null,"simulado":false,"t":1795480100.0,"conteos":{"N":4,"S":2,"E":1,"O":3},"info":"YOLO 4 camaras"}
```

| Campo | Valor obligatorio |
|---|---|
| `tipo` | `"listo"` |
| `version` | `1` |
| `calles` | `["N","S","E","O"]` |
| `huella` | `null` |
| `simulado` | `false` |
| `t` | número ≥ 0 (hora Unix en segundos) |
| `conteos` | primer conteo disponible |
| `info` | texto libre |

### 4.3 YOLO envía cada conteo nuevo (YOLO → Pi), **al menos cada 5 s**

```json
{"tipo":"conteos","seq":17,"t":1795480123.4,"conteos":{"N":6,"S":3,"E":5,"O":9}}
```

| Campo | Tipo | Significado |
|---|---|---|
| `tipo` | texto | `"conteos"` |
| `seq` | entero ≥ 0 | Número de mensaje; sube de 1 en 1 |
| `t` | número ≥ 0 | Hora de **captura** del frame (Unix, s) |
| `conteos` | objeto | Un número ≥ 0 por calle |

Valores **no válidos** (la Pi corta la conexión): negativos, `NaN`, `Infinity`, `true`/`false`, texto, `null`.

### 4.4 Mensajes opcionales

`{"tipo":"fin","t":...,"motivo":"apagado"}` (YOLO termina), `{"tipo":"error","mensaje":"..."}`. La Pi también puede enviar `fin`: YOLO cierra esa conexión y **vuelve a esperar**. Con YOLO, `luces` no hace falta para contar (en RL_CV se manda con `"enviar_luces": true` solo para mostrar el semáforo en las interfaces; el conteo nunca lo contesta).

---

## 6. Qué hace la Pi en cada situación (ya implementado)

| Situación | Comportamiento de la Pi |
|---|---|
| YOLO no está al arrancar | Todo en rojo 60 s, luego ciclo fijo de respaldo; reintenta cada 5 s |
| Llegan conteos normales | El agente (modelo DDQN) decide las fases |
| Más de **15 s** sin un mensaje `conteos` | Ciclo fijo de respaldo (N+S 30 s / E+O 30 s) |
| Vuelven los conteos | Espera ~95 s y devuelve el control al agente |
| Se corta la conexión | Sigue en respaldo y reintenta cada 5 s |
| Mensaje inválido | Envía `error`, cierra y reintenta |

---

## 7. Qué debe medir cada número (lo más importante)

1. **Instantáneo, no acumulado**: cuántos vehículos hay **en ese frame** dentro de la zona.
2. **Zona por calle**: los **60 m antes de la línea de parada**, **todos los carriles de entrada**. Un polígono por calle en la imagen (en RL_CV: `laptop/definir_zonas.py`). **Fuera**: el interior del cruce, los carriles de salida y la curva de los desvíos.
3. **Detenidos o en movimiento**: se cuentan todos.
4. **Clases**: `car`, `bus`, `truck` → en el modelo de 5 clases: `carro`, `bus`, `pesado` (+ `especial`). `moto`: **pendiente de decidir**.
5. Un vehículo cuenta en **una sola** calle.

---

## 8. Requisitos de tiempo

| Requisito | Valor |
|---|---|
| Intervalo entre conteos | **≤ 5 s** (RL_CV: 1 s) |
| Retraso captura → envío | **entre 2 y 6 s** (RL_CV: `retraso_min_s = 2`) |
| Respuesta al `hola` | < 10 s |
| Sin conteos antes de respaldo | 15 s |

Medir en la **Raspberry Pi 4 real**, con todas las cámaras a la vez, **sostenido al menos 30 min**.

---

## 9–12. Configuración, ejecución, comprobación y limitaciones

`semaforo/pi.json`: `"enlace": {"host": "127.0.0.1", "puerto": 5050, "enviar_luces": true, ...}` (ya configurado en RL_CV; true solo para la interfaz, false también funciona).

Comprobación: `semaforo/registros/pi_*.csv`. Revisa que `edad_conteos_s` sea ≤ 5 casi siempre, que `control` sea `agente` y que `conteo_N`…`conteo_O` coincidan con lo que se ve.

Limitaciones: el video grabado es circuito abierto (los autos no obedecen a los LEDs). El alcance de la cámara debe ser de 60 m. Las oclusiones hacen que se cuente de menos. El largo de los vehículos en SUMO es ~7,5 m por vehículo en cola. No hay cifrado, lo cual es aceptable porque la conexión no sale de la Pi.
