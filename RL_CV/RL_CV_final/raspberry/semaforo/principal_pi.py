#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
principal_pi.py - Programa principal del semáforo en la Raspberry Pi.

Une, sin cambiarlos, el Controlador, el Observador y la Politica de semaforo.py con los pines GPIO
(transistor + LED por luz y calle) y con la fuente de conteos (enlace.py): hoy SUMO en la PC
(puente_sumo.py, hardware-in-the-loop: Bullock et al., 2004); después, YOLO en la misma Pi.
La red nunca toca los pines: los pines siguen a Controlador.luces().

ARCHIVOS EN LA PI (una carpeta)
    principal_pi.py  enlace.py  semaforo.py  pi.json
    config.json  modelo.npz        los dos de la MISMA carpeta de salida del entrenamiento
  Dependencias (Raspberry Pi OS): sudo apt install python3-numpy python3-gpiozero. Usar el Python
  del sistema; un entorno virtual necesita --system-site-packages para ver gpiozero.

USO
    python principal_pi.py --modo lockstep --velocidad 0     validación exacta contra SUMO (puente_sumo.py)
    python principal_pi.py --modo tiempo_real                operación con reloj de 1 s
    ... --sin-hardware --mostrar                             en la PC, sin Raspberry (pines simulados)
    python principal_pi.py --probar [--config config.json]   pruebas (sin SUMO ni pines)
  Opciones: --config (config.json del modelo; modelo.npz va en su carpeta), --pi (pi.json),
  --host/--puerto (reemplazan los de pi.json), --duracion S (detenerse en t = S),
  --velocidad V (segundos del controlador por segundo real; 0 = sin pausas, solo en lockstep;
  con una fuente real, 1), --mostrar (una línea por segundo con el LED encendido de cada calle).

CADA SEGUNDO t (mismo orden que Entorno._segundo y Entorno._frame de entorno.py)
  1. Si es un instante de decisión: estado -> máscara -> Politica.elegir (o el plan de respaldo)
     -> Controlador.decidir().
  2. Controlador.luces() del segundo [t, t+1) a los pines y, si enlace.enviar_luces, a la fuente.
  3. lockstep: espera la respuesta (la medición de t+1). tiempo_real: espera el fin del segundo y
     toma el último conteo recibido.
  4. Controlador.avanzar().
  5. Si toca frame (t múltiplo de intervalo_frames_s, igual que en el entrenamiento):
     Observador.actualizar(t+1, conteos, luces de t+1).
  6. Una fila en registros/pi_<fecha>.csv.
  La marca de tiempo de los frames es el reloj del controlador, no la hora del conteo: los frames
  caen en la misma fase que en el entrenamiento y la antigüedad del conteo la cubre el retraso
  sorteado de sim_sumo.py (especificacion_adaptador.md, sección 5).

MODOS
  lockstep     la Pi espera la respuesta de la fuente antes de seguir: reproduce el entrenamiento
               exactamente (puente_sumo.py --verificar lo comprueba). Si la fuente no responde en
               enlace.espera_respuesta_s, se detiene con error.
  tiempo_real  el reloj no espera a nadie. Es el modo de la calle (YOLO) y, con SUMO, mide qué
               pasa cuando las respuestas llegan tarde (se cuentan en el resumen).

RESPALDO (solo tiempo_real)
  Si pasan más de respaldo.sin_conteos_s sin conteos nuevos, el agente deja de decidir y se sigue
  un ciclo fijo (respaldo.plan; por defecto N+S 30 s / E+O 30 s, el mejor plan fijo de
  prueba_perfil.py con el perfil nominal). Pasa por el mismo Controlador: ámbar, despeje, verdes
  mínimos y escudo del rojo máximo se mantienen. Adaptación del "recall" que usan los
  controladores actuados ante la falla de un detector (FHWA, Traffic Detector Handbook, 3.ª ed.,
  2006): allí se llama siempre a la fase del detector caído; aquí se pasa a un ciclo fijo completo.
  Cuando vuelven los conteos, el Observador empieza de cero (su espera estimada acumuló una cola
  que ya no es real) y el agente vuelve tras respaldo.volver_tras_s (por defecto, rojo máximo +
  intervalo entre frames: para entonces cada calle tuvo un verde visto en un frame y su espera
  estimada es la del Algoritmo 1 sin huecos). sin_conteos_s = 15 s y esa espera son criterios
  propios, sin fuente. Si se corta la conexión, se reintenta cada enlace.reintento_s.

RELOJ
  Un segundo nunca dura menos de 1 s: si el programa se atrasa, se corre el reloj en vez de
  acortar el segundo siguiente (un ámbar acortado es inseguro; MUTCD, 2009, sección 4D.26: ámbar
  de 3 a 6 s). Criterio propio de implementación.

LÍMITES
  - Al terminar (fin, error o Ctrl+C) pone todo en rojo y libera los pines; después, lo que muestren
    los LEDs depende de cómo gpiozero suelta el pin y de la resistencia de base a GND (con ella, se
    apagan). En un cruce real nada de eso es seguro: hace falta una unidad de monitoreo independiente
    (MMU, NEMA TS 2) que ponga el cruce en destello. Si Python se cuelga sin terminar, los pines
    quedan como estaban: solo un watchdog de hardware lo cubre. Nada de eso está implementado.
  - Con YOLO: medir el retraso real del conteo y cumplir especificacion_adaptador.md, sección 5.
  - Escrito y probado sin SUMO y sin gpiozero real (pines simulados en memoria).
"""

import argparse
import copy
import csv
import json
import math
import queue
import socket
import sys
import tempfile
import threading
import time
import traceback
import types
from datetime import datetime
from pathlib import Path

import numpy as np

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

DIR_BASE = Path(__file__).resolve().parent


def _agregar_rutas():
    """semaforo.py: en esta carpeta (Pi) o en la de arriba (PC: raspberry/ dentro del proyecto; o
    claude/ en el Proyecto). entrenamiento/ solo se usa en la prueba de equivalencia (PC)."""
    if str(DIR_BASE) not in sys.path:
        sys.path.insert(0, str(DIR_BASE))
    for c in (DIR_BASE, DIR_BASE.parent):
        if (c / "semaforo.py").exists():
            for d in (c, c / "entrenamiento"):
                if d.is_dir() and str(d) not in sys.path:
                    sys.path.append(str(d))
            return


_agregar_rutas()
try:
    import semaforo as sem          # noqa: E402
    import enlace as E              # noqa: E402
except ImportError as _e:
    raise ImportError(f"principal_pi.py necesita semaforo.py y enlace.py en su carpeta o en la de arriba ({_e})") from None

LETRAS_PIN = ("R", "A", "V")
PINES_RESERVADOS = {0: "ID_SD (EEPROM de los HAT)", 1: "ID_SC (EEPROM de los HAT)"}
PINES_AVISO = {
    2: "I2C SDA: resistencia pull-up física de 1,8 kΩ en la placa; el LED puede encenderse al arrancar",
    3: "I2C SCL: resistencia pull-up física de 1,8 kΩ en la placa; el LED puede encenderse al arrancar",
    14: "UART TX: cambia durante el arranque si la consola serie está activa",
    15: "UART RX: lo usa la consola serie si está activa",
}
TEXTO_PULL_UP = ("tiene pull-up interno al arrancar (hoja de datos BCM2711): el LED puede encenderse débilmente "
                 "hasta que arranca el programa. Usa una resistencia de base a GND de 4,7 kΩ o menos, o un pin "
                 "del 9 al 27")
PLAN_POR_DEFECTO = [{"fase": 1, "verde_s": 30}, {"fase": 2, "verde_s": 30}]
ENLACE_POR_DEFECTO = {"host": "127.0.0.1", "puerto": E.PUERTO, "enviar_luces": True, "espera_respuesta_s": 30,
                      "espera_conexion_s": 60, "reintento_s": 5}


def nombre_fase(cfg, a):
    return "+".join(cfg.calles[k] for k in cfg.fases[a].verde)


# =====================================================================
# 1. CONFIGURACIÓN DE LA PI (pi.json)
# =====================================================================
def revisar_gpio(gpio, calles):
    """(errores, avisos) del mapa de pines {calle: {"R": pin, "A": pin, "V": pin}} (numeración BCM)."""
    errores, avisos = [], []
    if not isinstance(gpio, dict):
        return ["pi.json: 'gpio' debe ser un objeto {calle: {\"R\": pin, \"A\": pin, \"V\": pin}}"], avisos
    faltan = [c for c in calles if c not in gpio]
    sobran = [k for k in gpio if not str(k).startswith("_") and k not in calles]
    if faltan:
        errores.append(f"pi.json: faltan los pines de {faltan}")
    if sobran:
        errores.append(f"pi.json: 'gpio' tiene calles que no están en config.json: {sobran}")
    usados = {}
    for c in calles:
        d = gpio.get(c)
        if not isinstance(d, dict):
            continue
        letras = sorted(k for k in d if not str(k).startswith("_"))
        if letras != sorted(LETRAS_PIN):
            errores.append(f"pi.json: gpio.{c} debe tener exactamente R, A y V (tiene {letras})")
        for letra in LETRAS_PIN:
            if letra not in d:
                continue
            p = d[letra]
            donde = f"gpio.{c}.{letra}"
            if isinstance(p, bool) or not isinstance(p, int) or not 0 <= p <= 27:
                errores.append(f"pi.json: {donde} debe ser un número de GPIO (BCM) de 0 a 27 (es {p!r})")
                continue
            if p in usados:
                errores.append(f"pi.json: el GPIO{p} está en {usados[p]} y en {donde}")
            usados[p] = donde
            if p in PINES_RESERVADOS:
                errores.append(f"pi.json: {donde} = GPIO{p} está reservado ({PINES_RESERVADOS[p]})")
            elif p in PINES_AVISO:
                avisos.append(f"{donde} = GPIO{p}: {PINES_AVISO[p]}")
            elif 4 <= p <= 8:
                avisos.append(f"{donde} = GPIO{p} {TEXTO_PULL_UP}")
    return errores, avisos


def leer_plan(plan, cfg):
    """[(acción, verde_s)] del plan de respaldo. Las fases van por su id de config.json."""
    if not isinstance(plan, list) or len(plan) < 2:
        raise ValueError("pi.json: respaldo.plan debe ser una lista de al menos 2 fases {\"fase\": id, \"verde_s\": s}")
    out = []
    for i, p in enumerate(plan):
        donde = f"respaldo.plan[{i}]"
        if not isinstance(p, dict):
            raise ValueError(f"pi.json: {donde} debe ser un objeto")
        ident, verde = p.get("fase"), p.get("verde_s")
        if isinstance(ident, bool) or not isinstance(ident, int) or not 1 <= ident <= cfg.n_fases:
            raise ValueError(f"pi.json: {donde}.fase debe ser un id de fase de config.json (1 a {cfg.n_fases}; es {ident!r})")
        f = cfg.fases[ident - 1]
        if isinstance(verde, bool) or not isinstance(verde, int) or verde < f.verde_min:
            raise ValueError(f"pi.json: {donde}.verde_s debe ser un entero >= {f.verde_min} (verde mínimo de "
                             f"{f.nombre}; es {verde!r})")
        if f.verde_max is not None and verde > f.verde_max:
            raise ValueError(f"pi.json: {donde}.verde_s supera el verde máximo de {f.nombre} ({f.verde_max} s)")
        out.append((f.indice, verde))
    fases = [a for a, _ in out]
    if len(set(fases)) != len(fases):
        raise ValueError("pi.json: respaldo.plan repite una fase")
    cubiertas = {k for a in fases for k in cfg.fases[a].verde}
    if len(cubiertas) != cfg.n_calles:
        sin = [c for k, c in enumerate(cfg.calles) if k not in cubiertas]
        raise ValueError(f"pi.json: respaldo.plan deja sin verde a {sin}")
    return out


def _positivo(v, nombre, cero=False):
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v < 0 or (v == 0 and not cero):
        raise ValueError(f"pi.json: {nombre} debe ser un número {'>= 0' if cero else '> 0'} (es {v!r})")
    return float(v)


class ConfigPi:
    """pi.json validado contra config.json (calles y fases)."""

    def __init__(self, datos, cfg, carpeta=None, host=None, puerto=None):
        if not isinstance(datos, dict):
            raise ValueError("pi.json debe contener un objeto JSON")
        carpeta = Path(carpeta) if carpeta is not None else Path.cwd()
        errores, self.avisos = revisar_gpio(datos.get("gpio"), cfg.calles)
        if errores:
            raise ValueError("\n".join(errores))
        self.pines = {c: {l: int(datos["gpio"][c][l]) for l in LETRAS_PIN} for c in cfg.calles}
        self.activo_en_alto = datos.get("gpio_activo_en_alto", True)
        if not isinstance(self.activo_en_alto, bool):
            raise ValueError("pi.json: gpio_activo_en_alto debe ser true o false")

        en = dict(ENLACE_POR_DEFECTO)
        en.update({k: v for k, v in (datos.get("enlace") or {}).items() if not str(k).startswith("_")})
        self.host = str(host if host is not None else en["host"])
        self.puerto = puerto if puerto is not None else en["puerto"]
        if not self.host or isinstance(self.puerto, bool) or not isinstance(self.puerto, int) or not 1 <= self.puerto <= 65535:
            raise ValueError(f"pi.json: enlace.host o enlace.puerto no válidos ({self.host!r}, {self.puerto!r})")
        if not isinstance(en["enviar_luces"], bool):
            raise ValueError("pi.json: enlace.enviar_luces debe ser true o false")
        self.enviar_luces = en["enviar_luces"]
        self.espera_respuesta_s = _positivo(en["espera_respuesta_s"], "enlace.espera_respuesta_s")
        self.espera_conexion_s = _positivo(en["espera_conexion_s"], "enlace.espera_conexion_s", cero=True)
        self.reintento_s = _positivo(en["reintento_s"], "enlace.reintento_s")

        re = datos.get("respaldo") or {}
        self.sin_conteos_s = _positivo(re.get("sin_conteos_s", 15), "respaldo.sin_conteos_s")
        self.plan = leer_plan(re.get("plan", PLAN_POR_DEFECTO), cfg)
        volver = re.get("volver_tras_s")
        self.volver_tras_s = (float(cfg.rojo_max + cfg.intervalo_frames) if volver is None
                              else _positivo(volver, "respaldo.volver_tras_s", cero=True))

        rg = datos.get("registro") or {}
        c = rg.get("carpeta", "registros")
        if c is not None and not (isinstance(c, str) and c.strip()):
            raise ValueError("pi.json: registro.carpeta debe ser una ruta o null (sin registro)")
        self.carpeta_registro = None if c is None else (Path(c) if Path(c).is_absolute() else carpeta / c)

    def texto_plan(self, cfg):
        return " -> ".join(f"{nombre_fase(cfg, a)} {v} s" for a, v in self.plan)


def cargar_pi(ruta, cfg, host=None, puerto=None):
    ruta = Path(ruta)
    if not ruta.exists():
        raise FileNotFoundError(f"No encuentro {ruta} (pines, enlace y respaldo de la Pi)")
    with open(ruta, encoding="utf-8") as f:
        try:
            datos = json.load(f)
        except json.JSONDecodeError as e:
            raise ValueError(f"{ruta.name} no es un JSON válido: {e}") from None
    return ConfigPi(datos, cfg, ruta.parent, host, puerto)


# =====================================================================
# 2. PINES
# =====================================================================
class SalidaGPIO:
    """Un LED (vía transistor) por luz y calle, con gpiozero. Numeración BCM. En cada calle se
    apagan primero las luces que no corresponden y luego se enciende la que sí: nunca hay dos
    encendidas a la vez. activo_en_alto: True si el pin en alto enciende el LED (transistor NPN con
    el emisor a GND); False para un PNP en el lado positivo."""

    def __init__(self, pines, calles, fabrica=None, activo_en_alto=True):
        try:
            from gpiozero import LED
        except ImportError:
            raise RuntimeError("falta gpiozero. En Raspberry Pi OS: sudo apt install python3-gpiozero "
                               "(en la PC, usa --sin-hardware)") from None
        self.calles = list(calles)
        self.leds = {}
        self._actual = {}
        try:
            for c in self.calles:
                self.leds[c] = {}
                for letra in LETRAS_PIN:
                    self.leds[c][letra] = LED(pines[c][letra], active_high=bool(activo_en_alto),
                                              initial_value=False, pin_factory=fabrica)
        except Exception:
            self.cerrar()
            raise

    def escribir(self, luces):
        for c in self.calles:
            luz = luces[c]
            if self._actual.get(c) == luz:
                continue
            for letra, led in self.leds[c].items():
                if letra != luz:
                    led.off()
            self.leds[c][luz].on()
            self._actual[c] = luz

    def estado(self):
        return {c: {l: bool(led.is_lit) for l, led in d.items()} for c, d in self.leds.items()}

    def todo_rojo(self):
        self.escribir({c: "R" for c in self.calles})

    def cerrar(self):
        for d in self.leds.values():
            for led in d.values():
                try:
                    led.close()
                except Exception:
                    pass
        self.leds = {}


class SalidaSimulada:
    """Pines en memoria, para probar sin Raspberry (objeto simulado: Mackinnon, Freeman y Craig,
    2000, Endo-Testing: Unit Testing with Mock Objects)."""

    def __init__(self, pines, calles):
        self.calles = list(calles)
        self.pines = {c: dict(pines[c]) for c in self.calles}
        self.nivel = {p: False for c in self.calles for p in self.pines[c].values()}

    def escribir(self, luces):
        for c in self.calles:
            for letra, pin in self.pines[c].items():
                self.nivel[pin] = letra == luces[c]

    def estado(self):
        return {c: {l: self.nivel[p] for l, p in d.items()} for c, d in self.pines.items()}

    def todo_rojo(self):
        self.escribir({c: "R" for c in self.calles})

    def cerrar(self):
        pass


def crear_salida(cpi, calles, sin_hardware=False):
    """(salida, descripción). Sin hardware: gpiozero con MockFactory si está instalado (mismo código
    que en la Pi); si no, pines en memoria."""
    if not sin_hardware:
        return (SalidaGPIO(cpi.pines, calles, activo_en_alto=cpi.activo_en_alto),
                f"GPIO real (gpiozero; LED encendido con el pin en {'alto' if cpi.activo_en_alto else 'bajo'})")
    try:
        from gpiozero.pins.mock import MockFactory
    except ImportError:
        return SalidaSimulada(cpi.pines, calles), "pines simulados en memoria (gpiozero no está instalado)"
    return (SalidaGPIO(cpi.pines, calles, fabrica=MockFactory(), activo_en_alto=cpi.activo_en_alto),
            "pines simulados con gpiozero (MockFactory)")


def luz_encendida(estado_calle):
    """Letra del único LED encendido de una calle, o '?' si no hay uno solo."""
    on = [l for l, v in estado_calle.items() if v]
    return on[0] if len(on) == 1 else "?"


# =====================================================================
# 3. FUENTE DE CONTEOS (cliente del enlace)
# =====================================================================
class Fuente:
    """Cliente del enlace. Un hilo se conecta (y en tiempo real se reconecta), saluda y deja cada
    mensaje recibido en una cola: el bucle principal solo espera a la red en lockstep.

    Eventos de la cola: ("listo", msg), ("conteos", msg), ("fin", msg), ("desconexion", texto),
    ("fatal", texto). "fatal" = configuración incompatible (huella o calles): no se reintenta."""

    def __init__(self, cfg, host, puerto, modo, reconectar, reintento_s=5.0, espera_saludo_s=10.0, modelo=None):
        self.cfg = cfg
        self.calles = list(cfg.calles)
        self.modelo = modelo                    # huella_archivo de modelo.npz (viaja en "hola")
        self.host, self.puerto, self.modo = host, int(puerto), modo
        self.reconectar = bool(reconectar)
        self.reintento_s = float(reintento_s)
        self.espera_saludo_s = float(espera_saludo_s)
        self.eventos = queue.Queue()
        self._conn = None
        self._parar = threading.Event()
        self._hilo = None
        self.t_pi = 0
        self.ultimo_error = None
        self.conexiones = 0

    @property
    def conectado(self):
        return self._conn is not None

    def iniciar(self, espera_s):
        """Arranca el hilo y espera hasta espera_s el primer "listo". Devuelve ese mensaje, o None si
        no hubo conexión (el hilo sigue intentando)."""
        self._hilo = threading.Thread(target=self._trabajar, name="fuente-de-conteos", daemon=True)
        self._hilo.start()
        limite = time.monotonic() + espera_s
        while True:
            resto = limite - time.monotonic()
            if resto <= 0:
                return None
            try:
                tipo, msg = self.eventos.get(timeout=min(resto, 0.25))
            except queue.Empty:
                continue
            if tipo == "listo":
                return msg
            if tipo == "fatal":
                raise E.Rechazo(msg)

    def _trabajar(self):
        while not self._parar.is_set():
            try:
                conn, listo = self._conectar()
            except (E.Rechazo, E.ErrorProtocolo) as e:
                self.eventos.put(("fatal", str(e)))
                return
            except (OSError, E.ErrorEnlace) as e:
                self.ultimo_error = str(e) or type(e).__name__
                if self.conexiones and not self.reconectar:
                    return
                self._parar.wait(self.reintento_s)
                continue
            self._conn = conn
            self.conexiones += 1
            self.eventos.put(("listo", listo))
            fin = self._recibir(conn)
            self._conn = None
            conn.cerrar()
            if fin or self._parar.is_set() or not self.reconectar:
                return
            self._parar.wait(self.reintento_s)

    def _conectar(self):
        conn = E.Conexion.conectar(self.host, self.puerto)
        try:
            conn.enviar(E.msg_hola(self.calles, self.cfg.huella, self.modo, self.t_pi, self.modelo))
            msg = conn.recibir(self.espera_saludo_s)
            if msg is None:
                raise E.ErrorEnlace("la fuente no respondió al saludo")
            if msg.get("tipo") == "error":
                raise E.Rechazo(f"la fuente rechazó la sesión: {msg.get('mensaje')}")
            if msg.get("tipo") != "listo":
                raise E.ErrorProtocolo(f"se esperaba 'listo' y llegó {msg.get('tipo')!r}")
            E.validar(msg, self.calles)
            h = msg.get("huella")
            if h is not None and h != self.cfg.huella:
                raise E.Rechazo(f"la huella de la fuente ({h}) no coincide con la del modelo de la Pi "
                                f"({self.cfg.huella})")
            return conn, msg
        except BaseException:
            conn.cerrar()
            raise

    def _recibir(self, conn):
        """Recibe hasta que se cierra la conexión. Devuelve True si llegó "fin"."""
        try:
            while not self._parar.is_set():
                msg = conn.recibir(0.5)
                if msg is None:
                    continue
                tipo = msg.get("tipo")
                if tipo in ("conteos", "fin"):
                    E.validar(msg, self.calles)
                    self.eventos.put((tipo, msg))
                    if tipo == "fin":
                        return True
                elif tipo == "error":
                    self.eventos.put(("desconexion", f"la fuente informó un error: {msg.get('mensaje')}"))
                    return False
                else:
                    raise E.ErrorProtocolo(f"mensaje inesperado de la fuente: {tipo!r}")
        except E.ErrorProtocolo as e:
            try:
                conn.enviar(E.msg_error(e))
            except E.ErrorEnlace:
                pass
            self.eventos.put(("desconexion", f"mensaje no válido de la fuente: {e}"))
        except E.ErrorEnlace as e:
            if not self._parar.is_set():
                self.eventos.put(("desconexion", str(e)))
        return False

    def enviar_luces(self, t, luces, estado=None):
        self.t_pi = t
        conn = self._conn
        if conn is None:
            return False
        msg = E.msg_luces(t, t, self.calles, luces)
        if estado is not None:
            msg["estado"] = estado              # [RL_CV] opcional: solo para mostrarlo
        try:
            conn.enviar(msg)
            return True
        except E.ErrorEnlace:
            return False

    def esperar_respuesta(self, seq, espera_s):
        """lockstep: ("conteos", msg) con esa seq, o ("fin", msg)."""
        limite = time.monotonic() + espera_s
        while True:
            resto = limite - time.monotonic()
            if resto <= 0:
                raise E.ErrorEnlace(f"la fuente no respondió en {espera_s:g} s (lockstep, segundo {seq})")
            try:
                tipo, msg = self.eventos.get(timeout=min(resto, 0.25))
            except queue.Empty:
                continue
            if tipo == "conteos":
                if msg["seq"] != seq:
                    raise E.ErrorProtocolo(f"respuesta con seq {msg['seq']}; se esperaba {seq}")
                return tipo, msg
            if tipo == "fin":
                return tipo, msg
            if tipo == "fatal":
                raise E.Rechazo(msg)
            if tipo == "desconexion":
                raise E.ErrorEnlace(f"se perdió la fuente en lockstep: {msg}")
            raise E.ErrorEnlace(f"evento inesperado en lockstep: {tipo}")

    def drenar(self):
        out = []
        while True:
            try:
                out.append(self.eventos.get_nowait())
            except queue.Empty:
                return out

    def cerrar(self, t=0, motivo=""):
        self._parar.set()
        conn = self._conn
        if conn is not None:
            try:
                conn.enviar(E.msg_fin(t, motivo))
            except E.ErrorEnlace:
                pass
            conn.cerrar()
        if self._hilo is not None:
            self._hilo.join(2.0)


# =====================================================================
# 4. RESPALDO: CICLO FIJO POR EL MISMO CONTROLADOR
# =====================================================================
class Respaldo:
    """Ciclo fijo en el orden del plan. En cada decisión se queda en la fase actual mientras su
    verde no llegue al del plan (y el escudo lo permita); si no, pasa a la siguiente del plan que
    esté permitida. Misma lógica que TiempoFijo de evaluar_sumo.py, generalizada a N fases. El verde
    real se redondea hacia arriba a verde mínimo + k·Δ (el controlador solo deja quedarse de Δ en Δ)."""

    def __init__(self, cfg, plan):
        self.cfg = cfg
        self.orden = [a for a, _ in plan]
        self.verde = dict(plan)

    def elegir(self, ctrl, mascara):
        p = ctrl.fase
        if p in self.verde and ctrl.t_fase < self.verde[p] and mascara[p]:
            return p
        i = self.orden.index(p) + 1 if p in self.orden else 0
        for k in range(len(self.orden)):
            q = self.orden[(i + k) % len(self.orden)]
            if q != p and mascara[q]:
                return q
        return p if mascara[p] else list(mascara).index(True)


# =====================================================================
# 5. REGISTRO
# =====================================================================
def columnas_registro(cfg):
    return (["t", "hora", "control", "conectado", "fase", "etapa", "t_fase", "luces", "decision", "pedida",
             "ejecutada", "anulada", "paso_s", "mascara"]
            + [f"q_{f.id}" for f in cfg.fases] + [f"rojo_{c}" for c in cfg.calles] + ["frame"]
            + [f"conteo_{c}" for c in cfg.calles] + ["edad_conteos_s", "seq_conteos", "atraso_s"])


class RegistroCSV:
    """Una fila por segundo (el segundo [t, t+1)). Se vacía al disco en cada fila."""

    def __init__(self, ruta, columnas):
        self.ruta = Path(ruta)
        self.ruta.parent.mkdir(parents=True, exist_ok=True)
        self._f = open(self.ruta, "w", newline="", encoding="utf-8")
        self._w = csv.DictWriter(self._f, fieldnames=columnas, extrasaction="ignore")
        self._w.writeheader()
        self.filas = 0

    def escribir(self, fila):
        self._w.writerow({k: ("" if v is None else v) for k, v in fila.items()})
        self._f.flush()
        self.filas += 1

    def cerrar(self):
        if not self._f.closed:
            self._f.close()


# =====================================================================
# 6. BUCLE PRINCIPAL
# =====================================================================
class Programa:
    """Controlador + Observador + Politica de semaforo.py, pines y fuente de conteos (ver el inicio
    del archivo). correr() devuelve un resumen; las excepciones salen después de poner todo en rojo."""

    def __init__(self, cfg, cpi, pol, salida, fuente, modo="tiempo_real", velocidad=1.0, registro=None,
                 duracion=None, mostrar=False, historial=False, imprimir=print):
        if modo not in E.MODOS:
            raise ValueError(f"modo debe ser uno de {E.MODOS}")
        velocidad = float(velocidad)
        if not math.isfinite(velocidad) or velocidad < 0:
            raise ValueError("la velocidad debe ser un número >= 0")
        if modo == "tiempo_real" and velocidad == 0:
            raise ValueError("en tiempo real la velocidad debe ser > 0 (1 = reloj real)")
        if modo == "lockstep" and not cpi.enviar_luces:
            raise ValueError("lockstep necesita enlace.enviar_luces = true: la fuente avanza con cada mensaje de luces")
        if duracion is not None and int(duracion) < 1:
            raise ValueError("--duracion debe ser >= 1")
        self.cfg, self.cpi, self.pol, self.salida, self.fuente = cfg, cpi, pol, salida, fuente
        self.modo, self.velocidad, self.registro = modo, velocidad, registro
        self.duracion = None if duracion is None else int(duracion)
        self.mostrar, self.imprimir = mostrar, imprimir
        self.calles = list(cfg.calles)
        self.ctrl = sem.Controlador(cfg)
        self.obs = sem.Observador(cfg)
        self.respaldo = Respaldo(cfg, cpi.plan)
        self.control = "agente"                 # "agente", "respaldo" o "recuperando"
        self.ultimo = None                      # últimos conteos recibidos (orden de cfg.calles)
        self.seq_ultimo = None
        self.t_ultimo = None                    # segundo del controlador en que llegaron
        self._nuevo = False
        self._desde_recuperacion = None
        self._prox_frame = 0.0
        self.simulada = None                    # la fuente es un simulador (dice "listo")
        self.historial = [] if historial else None      # decisiones, formato de semaforo._verificar
        self.estados = [] if historial else None
        self.luces_seg = [] if historial else None
        self.cuenta = {"decisiones": 0, "cambios": 0, "anuladas": 0, "frames": 0, "respaldo_s": 0,
                       "atrasados": 0, "conteos_tarde": 0, "reconexiones": 0}
        self.atraso_max = 0.0
        self.eventos = []
        self.resumen_fuente = None
        self._ult_decision = None               # [RL_CV] última decisión, para la interfaz
        self._cfg_interfaz = None

    # ----- eventos -----
    def _evento(self, texto):
        self.eventos.append((self.ctrl.t, texto))
        self.imprimir(f"t={self.ctrl.t:6d} s | {texto}")

    def _cambiar(self, nuevo, porque):
        self.control = nuevo
        self._evento(f"control: {nuevo} ({porque})")

    def _conectada(self, listo):
        simulada = bool(listo["simulado"])
        if not simulada and self.velocidad != 1:
            raise E.Rechazo("con una fuente real (no simulada) la velocidad debe ser 1")
        if self.simulada is not None:
            self.cuenta["reconexiones"] += 1
        self.simulada = simulada
        self._evento(f"fuente conectada en {self.fuente.host}:{self.fuente.puerto} "
                     f"({'simulador' if simulada else 'cámaras'}): {listo.get('info') or 'sin descripción'}")
        if listo.get("huella") is None:
            self._evento("AVISO: la fuente no envía huella: no se comprobó que use la misma configuración "
                         "(alcance L, intervalo entre conteos)")
        self._conteos(listo)

    def _conteos(self, msg):
        self.ultimo = E.conteos_de(msg, self.calles)
        self.seq_ultimo = msg.get("seq")
        self._nuevo = True

    def _terminar_fuente(self, fin):
        self.resumen_fuente = fin.get("resumen")

    @staticmethod
    def _motivo_fin(fin):
        return f"la fuente terminó ({fin.get('motivo') or 'sin motivo'})"

    def _eventos(self):
        """tiempo_real: procesa lo que llegó durante el segundo. Devuelve el mensaje "fin" o None."""
        for tipo, msg in self.fuente.drenar():
            if tipo == "conteos":
                self._conteos(msg)
            elif tipo == "listo":
                self._conectada(msg)
            elif tipo == "fin":
                return msg
            elif tipo == "desconexion":
                self._evento(f"fuente desconectada: {msg}")
            elif tipo == "fatal":
                raise E.Rechazo(msg)
        return None

    # ----- un segundo -----
    def _decidir(self):
        ctrl, cfg = self.ctrl, self.cfg
        x = self.obs.estado(ctrl)
        m = ctrl.mascara()
        q = self.pol.valores_q(x)
        if self.control == "agente":
            pedida = self.pol.elegir(x, m)
        else:
            pedida = self.respaldo.elegir(ctrl, m)
        fase, t_fase, rojo = ctrl.fase, ctrl.t_fase, tuple(ctrl.rojo())
        ejecutada, paso = ctrl.decidir(pedida)
        self.cuenta["decisiones"] += 1
        self.cuenta["cambios"] += int(ejecutada != fase)
        self.cuenta["anuladas"] += int(ctrl.forzada)
        if self.historial is not None:
            self.historial.append({"t": ctrl.t, "fase": fase, "t_fase": t_fase, "rojo": rojo, "mascara": list(m),
                                   "pedida": int(pedida), "ejecutada": int(ejecutada), "dur": int(paso)})
            self.estados.append(np.array(x, copy=True))
        if ejecutada == fase:
            que = f"se queda en {nombre_fase(cfg, fase)} {paso} s"
        else:
            que = (f"CAMBIA a {nombre_fase(cfg, ejecutada)} ({cfg.transicion} s de cambio + "
                   f"{cfg.fases[ejecutada].verde_min} s de verde mínimo)")
        if ctrl.forzada:
            que += f" [el controlador anuló {nombre_fase(cfg, pedida)}]"
        qs = "  ".join(f"{nombre_fase(cfg, a)} {q[a]:.2f}" + ("*" if a == ejecutada else "") + ("" if m[a] else "x")
                       for a in range(cfg.n_fases))
        if self.obs.t_ultimo is None:
            visto = "sin frames"
        else:
            visto = (" ".join(f"{c} {v:g}" for c, v in zip(self.calles, self.obs.q))
                     + f" (frame de hace {ctrl.t - self.obs.t_ultimo:g} s)")
        self.imprimir(f"t={ctrl.t:6d} s | {self.control:<11} | verde {nombre_fase(cfg, fase)} hace {t_fase} s | "
                      f"estado: {visto} | {que} | Q {qs}")
        self._ult_decision = {"t": int(ctrl.t), "quien": self.control,
                              "pedida": cfg.fases[pedida].id, "ejecutada": cfg.fases[ejecutada].id,
                              "anulada": bool(ctrl.forzada), "paso_s": int(paso), "cambio": ejecutada != fase,
                              "q": [round(float(v), 4) for v in q], "mascara": [bool(v) for v in m]}
        fila = {"decision": 1, "pedida": cfg.fases[pedida].id, "ejecutada": cfg.fases[ejecutada].id,
                "anulada": int(ctrl.forzada), "paso_s": int(paso), "mascara": "".join("1" if v else "0" for v in m)}
        for a, f in enumerate(cfg.fases):
            fila[f"q_{f.id}"] = round(float(q[a]), 5)
        return fila

    def _edad(self):
        return None if self.t_ultimo is None else self.ctrl.t - self.t_ultimo

    def _texto_conteos(self):
        if self.ultimo is None:
            return "sin conteos"
        return " ".join(f"{c} {v:g}" for c, v in zip(self.calles, self.ultimo)) + f" (hace {self._edad()} s)"

    def _inicio_segundo(self, t):
        ctrl = self.ctrl
        fila = {"t": t}
        if ctrl.esperando_decision():
            fila.update(self._decidir())
        luces = ctrl.luces()
        self.salida.escribir(dict(zip(self.calles, luces)))
        if self.cpi.enviar_luces:
            self.fuente.enviar_luces(t, luces, self._estado_interfaz())
        texto = "".join(luces)
        if self.luces_seg is not None:
            self.luces_seg.append(texto)
        rojo = ctrl.rojo()
        fila.update({"control": self.control, "fase": self.cfg.fases[ctrl.fase].id, "etapa": ctrl.etapa,
                     "t_fase": ctrl.t_fase, "luces": texto})
        for k, c in enumerate(self.calles):
            fila[f"rojo_{c}"] = rojo[k]
        if self.control != "agente":
            self.cuenta["respaldo_s"] += 1
        if self.mostrar:
            est = self.salida.estado()
            leds = "  ".join(f"{c}:{luz_encendida(est[c])}" for c in self.calles)
            self.imprimir(f"t={t:6d} s | LED {leds} | {self.control} | conteos {self._texto_conteos()}")
        return fila

    def _estado_interfaz(self):
        """[RL_CV] Resumen del segundo actual para mostrarlo en una interfaz (laptop, web, ventana
        de conteo4). Va en el campo opcional 'estado' del mensaje 'luces': la otra parte lo puede
        ignorar (enlace.validar no mira campos de más), así que no cambia el contrato."""
        ctrl, cfg = self.ctrl, self.cfg
        if self._cfg_interfaz is None:
            self._cfg_interfaz = {
                "fases": [{"id": f.id, "nombre": f.nombre, "verde": [cfg.calles[k] for k in f.verde],
                           "verde_min_s": f.verde_min, "verde_max_s": f.verde_max} for f in cfg.fases],
                "calles": list(cfg.calles), "ambar_s": cfg.ambar, "transicion_s": cfg.transicion,
                "rojo_max_s": cfg.rojo_max}
        return {"t": int(ctrl.t), "control": self.control, "fase": cfg.fases[ctrl.fase].id,
                "etapa": ctrl.etapa, "t_fase": int(ctrl.t_fase),
                "prox_decision_s": max(0, int(getattr(ctrl, "_t_decision", ctrl.t) - ctrl.t)),
                "rojo": [int(v) for v in ctrl.rojo()],             # en el orden de config.calles
                "conteos": None if self.ultimo is None else [float(v) for v in self.ultimo],
                "edad_conteos_s": self._edad(), "decision": self._ult_decision,
                "atraso_max_s": round(self.atraso_max, 3), "config": self._cfg_interfaz}

    def _pausa(self, t0, t, fila):
        """Espera el fin del segundo t. Si ya pasó, corre el reloj (el segundo siguiente dura un
        segundo entero: nunca se acorta un ámbar). Devuelve el nuevo origen del reloj."""
        if self.velocidad <= 0:
            return t0
        resto = t0 + (t + 1) / self.velocidad - time.perf_counter()
        if resto > 0:
            time.sleep(resto)
            fila["atraso_s"] = 0.0
            return t0
        atraso = -resto * self.velocidad              # en segundos del controlador
        fila["atraso_s"] = round(atraso, 3)
        self.atraso_max = max(self.atraso_max, atraso)
        if atraso > 0.1:
            self.cuenta["atrasados"] += 1
        return t0 - resto

    def _transiciones(self):
        ctrl = self.ctrl
        if self._nuevo:
            self.t_ultimo, self._nuevo = ctrl.t, False
        if self.modo != "tiempo_real":
            return
        if (self.simulada and self.fuente.conectado and self.seq_ultimo is not None
                and self.seq_ultimo != ctrl.t - 1):
            self.cuenta["conteos_tarde"] += 1         # no llegó la respuesta del segundo que terminó
        edad = self._edad()
        fresco = edad is not None and edad <= self.cpi.sin_conteos_s
        if self.control == "agente" and not fresco:
            self._cambiar("respaldo", f"{edad} s sin conteos nuevos" if edad is not None else "sin conteos")
        elif self.control == "respaldo" and fresco:
            self.obs.reiniciar()
            self._desde_recuperacion = ctrl.t
            self._cambiar("recuperando", f"volvieron los conteos; el Observador empieza de cero y el agente "
                                         f"vuelve en {self.cpi.volver_tras_s:g} s")
        elif self.control == "recuperando":
            if not fresco:
                self._cambiar("respaldo", f"{edad} s sin conteos nuevos")
            elif ctrl.t - self._desde_recuperacion >= self.cpi.volver_tras_s:
                self._cambiar("agente", "el Observador ya tiene una ventana completa")

    def _frame(self):
        """Frame en los múltiplos de intervalo_frames_s, como Entorno._frame. En respaldo no se
        alimenta al Observador. Devuelve True si hubo frame."""
        t = self.ctrl.t
        if t + 1e-9 < self._prox_frame:
            return False
        while self._prox_frame <= t + 1e-9:
            self._prox_frame += self.cfg.intervalo_frames
        if self.ultimo is None or self.control == "respaldo":
            return False
        self.obs.actualizar(float(t), self.ultimo, self.ctrl.luces())
        self.cuenta["frames"] += 1
        return True

    def _registrar(self, fila):
        if self.registro is None:
            return
        fila["hora"] = datetime.now().isoformat(timespec="milliseconds")
        fila["conectado"] = int(self.fuente.conectado)
        for k, c in enumerate(self.calles):
            fila[f"conteo_{c}"] = "" if self.ultimo is None else self.ultimo[k]
        fila["edad_conteos_s"] = self._edad()
        fila["seq_conteos"] = self.seq_ultimo
        self.registro.escribir(fila)

    # ----- todo -----
    def correr(self):
        ctrl = self.ctrl
        motivo = "error"
        self.salida.todo_rojo()
        try:
            listo = self.fuente.iniciar(self.cpi.espera_conexion_s)
            if listo is None:
                if self.modo == "lockstep":
                    raise E.ErrorEnlace(f"no hubo conexión con la fuente en {self.fuente.host}:{self.fuente.puerto} "
                                        f"en {self.cpi.espera_conexion_s:g} s ({self.fuente.ultimo_error or 'sin respuesta'})")
                self._cambiar("respaldo", "no hay fuente de conteos al arrancar; se sigue intentando")
            else:
                self._conectada(listo)
                self.t_ultimo, self._nuevo = 0, False
            self._frame()                                       # frame de t = 0 (Entorno.reiniciar)
            t0 = time.perf_counter()
            while True:
                t = ctrl.t
                if self.duracion is not None and t >= self.duracion:
                    motivo = f"--duracion {self.duracion} s"
                    break
                fila = self._inicio_segundo(t)
                if self.modo == "lockstep":
                    tipo, msg = self.fuente.esperar_respuesta(t, self.cpi.espera_respuesta_s)
                    fin = msg if tipo == "fin" else None
                    if fin is not None:                 # este segundo ya no se simuló
                        self._terminar_fuente(fin)
                        motivo = self._motivo_fin(fin)
                        self._registrar(fila)
                        break
                    self._conteos(msg)
                    t0 = self._pausa(t0, t, fila)
                else:
                    t0 = self._pausa(t0, t, fila)
                    fin = self._eventos()
                ctrl.avanzar()
                self._transiciones()
                fila["frame"] = int(self._frame())
                self._registrar(fila)
                if fin is not None:                     # tiempo real: el segundo se mostró entero
                    self._terminar_fuente(fin)
                    motivo = self._motivo_fin(fin)
                    break
        except KeyboardInterrupt:
            motivo = "Ctrl+C"
        finally:
            self.salida.todo_rojo()
            self.fuente.cerrar(ctrl.t, motivo)
            if self.registro is not None:
                self.registro.cerrar()
        return dict(self.cuenta, segundos=ctrl.t, motivo=motivo, atraso_max_s=round(self.atraso_max, 3),
                    resumen_fuente=self.resumen_fuente)


def texto_resumen(r, cfg):
    lineas = [f"Fin: {r['motivo']} | {r['segundos']} s | {r['decisiones']} decisiones, {r['cambios']} cambios de fase, "
              f"{r['anuladas']} anuladas por el controlador | {r['frames']} frames"]
    if r["respaldo_s"]:
        lineas.append(f"   {r['respaldo_s']} s sin el agente (respaldo o recuperación)")
    if r["reconexiones"]:
        lineas.append(f"   {r['reconexiones']} reconexiones con la fuente")
    if r["atrasados"] or r["atraso_max_s"] > 0.1:
        lineas.append(f"   {r['atrasados']} segundos atrasados más de 0,1 s (máx. {r['atraso_max_s']:.2f} s): el reloj "
                      f"se corrió, ningún segundo se acortó")
    if r["conteos_tarde"]:
        lineas.append(f"   {r['conteos_tarde']} segundos sin la respuesta del simulador a tiempo (se usó el conteo "
                      f"anterior): en lockstep no pasaría")
    rf = r.get("resumen_fuente")
    if rf:
        lineas.append("   Según la fuente: " + ", ".join(
            f"{k} {v:.2f}" if k == "jain" else (f"{k} {v:.1f}" if isinstance(v, float) else f"{k} {v}")
            for k, v in rf.items()))
    return "\n".join(lineas)


# =====================================================================
# 7. PRUEBAS  (python principal_pi.py --probar)
# =====================================================================
class _Omitida(Exception):
    pass


class _Aviso(Exception):
    pass


def _comprobar(condicion, mensaje):
    if not condicion:
        raise AssertionError(mensaje)


def _debe_fallar(funcion, tipo, texto=None):
    try:
        funcion()
    except tipo as e:
        if texto is not None and texto not in str(e):
            raise AssertionError(f"el error no menciona '{texto}': {e}")
        return
    raise AssertionError(f"se esperaba {tipo.__name__}")


_POOL_PINES = [17, 27, 22, 16, 20, 21, 23, 24, 25, 12, 19, 26, 9, 10, 11, 13, 18]


def _pines_prueba(calles):
    if 3 * len(calles) > len(_POOL_PINES):
        raise _Omitida("demasiadas calles para los pines de prueba")
    it = iter(_POOL_PINES)
    return {c: {l: next(it) for l in LETRAS_PIN} for c in calles}


def _plan_prueba(cfg):
    cubiertas = {k for f in cfg.fases[:2] for k in f.verde}
    if len(cubiertas) != cfg.n_calles:
        raise _Omitida("las fases 1 y 2 de config.json no dan verde a todas las calles (plan de respaldo)")
    return [{"fase": f.id, "verde_s": max(f.verde_min, min(30, f.verde_max or 30))} for f in cfg.fases[:2]]


def _cpi(cfg, puerto=E.PUERTO, enlace=None, respaldo=None):
    datos = {"gpio": _pines_prueba(cfg.calles),
             "enlace": dict({"host": "127.0.0.1", "puerto": puerto, "espera_respuesta_s": 5, "espera_conexion_s": 5,
                             "reintento_s": 0.05}, **(enlace or {})),
             "respaldo": dict({"plan": _plan_prueba(cfg)}, **(respaldo or {})),
             "registro": {"carpeta": None}}
    return ConfigPi(datos, cfg)


class _SimSintetico:
    """Colas sintéticas con llegadas aleatorias (no es un modelo de tráfico: solo para las pruebas)."""

    def __init__(self, cfg):
        self.n = cfg.n_calles
        self.p = ([0.30, 0.30, 0.12, 0.12] + [0.15] * self.n)[:self.n]

    def reiniciar(self, semilla):
        self.rng = np.random.default_rng(semilla)
        self.cola = [0] * self.n
        return types.SimpleNamespace(conteos=[0.0] * self.n)

    def avanzar(self, luces):
        for k in range(self.n):
            if self.rng.random() < self.p[k]:
                self.cola[k] += 1
            if luces[k] == "V" and self.cola[k] > 0 and self.rng.random() < 0.6:
                self.cola[k] -= 1
        return types.SimpleNamespace(conteos=[float(min(c, 30)) for c in self.cola])


class _FuentePrueba:
    """Fuente de conteos en un hilo (SesionSimulador + un simulador de las pruebas). Graba las luces
    recibidas. callar(t) -> True: no responde ese segundo. cortar_en: en la primera sesión, corta la
    conexión sin "fin" en ese segundo (y acepta otra sesión)."""

    def __init__(self, cfg, duracion, sim, semilla=7, huella="cfg", calles=None, callar=None, cortar_en=None,
                 sesiones=1):
        self.cfg, self.duracion, self.sim, self.semilla = cfg, duracion, sim, semilla
        self.huella = cfg.huella if huella == "cfg" else huella
        self.calles = list(calles or cfg.calles)
        self.callar, self.cortar_en, self.sesiones = callar, cortar_en, sesiones
        self.srv = E.escuchar("127.0.0.1", 0)
        self.puerto = self.srv.getsockname()[1]
        self.luces, self.segundos, self.errores = [], [], []
        self._t = 0
        self.hilo = threading.Thread(target=self._correr, daemon=True)
        self.hilo.start()

    def _reiniciar(self):
        self._t = 0
        return list(self.sim.reiniciar(self.semilla).conteos)

    def _avanzar(self, luces):
        self.luces.append("".join(luces))
        if self.cortar_en is not None and len(self.segundos) == 0 and self._t >= self.cortar_en:
            raise E.ConexionCerrada("corte de prueba")
        conteos = list(self.sim.avanzar(luces).conteos)
        self._t += 1
        if self.callar is not None and self.callar(self._t):
            return None
        return conteos

    def _correr(self):
        try:
            for _ in range(self.sesiones):
                conn = E.aceptar(self.srv, 20)
                if conn is None:
                    return
                s = E.SesionSimulador(conn, self.calles, self.huella, self.duracion, self._reiniciar, self._avanzar,
                                      info="prueba", espera_s=10)
                try:
                    self.segundos.append(s.correr())
                except E.ConexionCerrada as e:
                    self.segundos.append(s.segundos)
                    self.errores.append(e)
        except Exception as e:
            self.errores.append(e)
        finally:
            self.srv.close()


def _correr_pi(ctx, cpi, modo, velocidad, duracion=None, registro=None):
    cfg = ctx["cfg"]
    salida = SalidaSimulada(cpi.pines, cfg.calles)
    fuente = Fuente(cfg, cpi.host, cpi.puerto, modo, reconectar=modo == "tiempo_real", reintento_s=cpi.reintento_s)
    prog = Programa(cfg, cpi, ctx["pol"], salida, fuente, modo, velocidad, registro=registro, duracion=duracion,
                    historial=True, imprimir=lambda *a, **k: None)
    return prog, prog.correr(), salida


def _verificar_reglas(cfg, prog):
    """Reglas del controlador (semaforo._verificar) sobre las luces y decisiones de la Pi."""
    sem._verificar(cfg, prog.luces_seg, prog.historial)


def prueba_mensajes(ctx):
    cfg = ctx["cfg"]
    calles = cfg.calles
    a, b = socket.socketpair()
    ca, cb = E.Conexion(a), E.Conexion(b)
    try:
        msgs = [E.msg_hola(calles, "abc", "lockstep"), E.msg_luces(3, 3, calles, ["V", "A"] + ["R"] * (len(calles) - 2)),
                E.msg_conteos(3, 4, calles, [1, 2.5] + [0] * (len(calles) - 2)), E.msg_fin(4, "prueba")]
        for m in msgs:
            ca.enviar(m)
        for m in msgs:
            r = cb.recibir(2)
            _comprobar(r == m, f"ida y vuelta: {r} != {m}")
            E.validar(r, calles)
        _comprobar(cb.recibir(0.3) is None, "sin mensajes debe devolver None")
        texto = json.dumps(msgs[2]).encode() + b"\n"
        for i in range(0, len(texto), 7):                      # llega en trozos
            a.sendall(texto[i:i + 7])
        _comprobar(cb.recibir(2) == msgs[2], "mensaje en trozos")
        a.sendall(b"{no es json}\n")
        _debe_fallar(lambda: cb.recibir(2), E.ErrorProtocolo, "JSON")
        a.sendall(b"x" * (E.MAX_LINEA + 10))
        _debe_fallar(lambda: cb.recibir(2), E.ErrorProtocolo, "bytes")
    finally:
        ca.cerrar()
        cb.cerrar()
    malos = []
    base = E.msg_conteos(1, 2, calles, [0] * len(calles))
    for cambio, texto in ((lambda m: m["conteos"].pop(calles[0]), "faltan"),
                          (lambda m: m["conteos"].__setitem__("X", 1), "sobran"),
                          (lambda m: m["conteos"].__setitem__(calles[0], -1), "conteo"),
                          (lambda m: m["conteos"].__setitem__(calles[0], float("nan")), "conteo"),
                          (lambda m: m["conteos"].__setitem__(calles[0], True), "conteo"),
                          (lambda m: m.__setitem__("seq", -1), "seq"),
                          (lambda m: m.__setitem__("tipo", "otro"), "desconocido")):
        m = copy.deepcopy(base)
        cambio(m)
        malos.append(m)
        _debe_fallar(lambda: E.validar(m, calles), E.ErrorProtocolo, texto)
    luz = E.msg_luces(0, 0, calles, ["R"] * len(calles))
    luz["luces"][calles[0]] = "X"
    _debe_fallar(lambda: E.validar(luz, calles), E.ErrorProtocolo, "luz")
    _debe_fallar(lambda: E.validar(E.msg_hola(calles + ["X"], None, "lockstep"), calles), E.Rechazo, "calles")
    _debe_fallar(lambda: E.msg_luces(0, 0, calles, ["R"]), ValueError)
    _comprobar(tuple(E.LETRAS) == (sem.VERDE, sem.AMBAR, sem.ROJO), "letras distintas de semaforo.py")
    return f"ida y vuelta, mensajes en trozos, JSON roto, línea larga y {len(malos) + 3} mensajes inválidos rechazados"


def prueba_pi_json(ctx):
    cfg = ctx["cfg"]
    pines = _pines_prueba(cfg.calles)
    cpi = ConfigPi({"gpio": pines, "respaldo": {"plan": _plan_prueba(cfg)}}, cfg)
    _comprobar(cpi.volver_tras_s == cfg.rojo_max + cfg.intervalo_frames and cpi.sin_conteos_s == 15
               and cpi.puerto == E.PUERTO and cpi.enviar_luces, "valores por defecto")
    c0, c1 = cfg.calles[0], cfg.calles[1]

    def con(cambio):
        d = {"gpio": copy.deepcopy(pines), "respaldo": {"plan": _plan_prueba(cfg)}}
        cambio(d)
        return lambda: ConfigPi(d, cfg)

    f0 = cfg.fases[0]
    malos = [
        (lambda d: d["gpio"][c1].__setitem__("R", d["gpio"][c0]["R"]), "está en"),
        (lambda d: d["gpio"].pop(c0), "faltan"),
        (lambda d: d["gpio"].__setitem__("X", {"R": 9, "A": 10, "V": 11}), "no están"),
        (lambda d: d["gpio"][c0].pop("A"), "exactamente"),
        (lambda d: d["gpio"][c0].__setitem__("V", 0), "reservado"),
        (lambda d: d["gpio"][c0].__setitem__("V", 30), "0 a 27"),
        (lambda d: d["gpio"][c0].__setitem__("V", True), "0 a 27"),
        (lambda d: d["respaldo"].__setitem__("plan", [{"fase": 99, "verde_s": 30}] * 2), "id de fase"),
        (lambda d: d["respaldo"].__setitem__("plan", [{"fase": f0.id, "verde_s": f0.verde_min - 1},
                                                      _plan_prueba(cfg)[1]]), "verde mínimo"),
        (lambda d: d["respaldo"].__setitem__("plan", [_plan_prueba(cfg)[0]] * 2), "repite"),
        (lambda d: d["respaldo"].__setitem__("plan", [_plan_prueba(cfg)[0]]), "al menos 2"),
        (lambda d: d.__setitem__("enlace", {"puerto": 0}), "puerto"),
        (lambda d: d.__setitem__("gpio_activo_en_alto", "si"), "gpio_activo_en_alto"),
        (lambda d: d.__setitem__("respaldo", {"sin_conteos_s": 0, "plan": _plan_prueba(cfg)}), "sin_conteos_s"),
    ]
    una = [f for f in cfg.fases if len(f.verde) == 1]
    if len(una) >= 2:
        malos.append((lambda d: d["respaldo"].__setitem__("plan", [{"fase": una[0].id, "verde_s": una[0].verde_min},
                                                                   {"fase": una[1].id, "verde_s": una[1].verde_min}]),
                      "sin verde"))
    con_max = [f for f in cfg.fases if f.verde_max is not None]
    if con_max:
        f = con_max[0]
        otra = next(g for g in cfg.fases if g.indice != f.indice)
        malos.append((lambda d: d["respaldo"].__setitem__("plan", [{"fase": f.id, "verde_s": f.verde_max + 1},
                                                                   {"fase": otra.id, "verde_s": otra.verde_min}]),
                      "verde máximo"))
    for cambio, texto in malos:
        _debe_fallar(con(cambio), ValueError, texto)
    avisos = {}
    for p in (5, 6, 2, 14):
        d = copy.deepcopy(pines)
        d[c0]["V"] = p
        avisos[p] = ConfigPi({"gpio": d, "respaldo": {"plan": _plan_prueba(cfg)}}, cfg).avisos
    _comprobar(all("pull-up" in " ".join(avisos[p]) for p in (5, 6, 2)) and "UART" in " ".join(avisos[14]),
               f"avisos de pines: {avisos}")
    real = DIR_BASE / "pi.json"
    if real.exists():
        cpi_real = cargar_pi(real, cfg)
        detalle = (f"; pi.json de la carpeta: válido, plan {cpi_real.texto_plan(cfg)}, "
                   f"{len(cpi_real.avisos)} avisos de pines")
    else:
        detalle = ""
    return f"valores por defecto, {len(malos)} configuraciones inválidas rechazadas y avisos de pines 2, 5, 6 y 14{detalle}"


def prueba_salida(ctx):
    import itertools
    cfg = ctx["cfg"]
    pines = _pines_prueba(cfg.calles)
    salidas = [("en memoria", SalidaSimulada(pines, cfg.calles))]
    try:
        from gpiozero.pins.mock import MockFactory
        salidas.append(("gpiozero MockFactory", SalidaGPIO(pines, cfg.calles, fabrica=MockFactory())))
    except ImportError:
        pass
    combos = 0
    try:
        for nombre, s in salidas:
            for luces in itertools.product("VAR", repeat=cfg.n_calles):
                s.escribir(dict(zip(cfg.calles, luces)))
                est = s.estado()
                for c, l in zip(cfg.calles, luces):
                    _comprobar(est[c] == {x: x == l for x in LETRAS_PIN}, f"{nombre}: {c} con {l}: {est[c]}")
                if isinstance(s, SalidaSimulada):
                    for c, l in zip(cfg.calles, luces):
                        _comprobar(s.nivel[pines[c][l]] and sum(s.nivel[p] for p in pines[c].values()) == 1,
                                   f"{c}: no se encendió el GPIO{pines[c][l]}")
                combos += 1
            s.todo_rojo()
            _comprobar(all(luz_encendida(v) == "R" for v in s.estado().values()), f"{nombre}: todo rojo")
    finally:
        for _, s in salidas:
            s.cerrar()
    if len(salidas) == 1:
        raise _Aviso(f"{combos} combinaciones de luces: un LED por calle, en su pin. gpiozero no está instalado: "
                     f"no se probó SalidaGPIO (en la PC: pip install gpiozero)")
    return f"{combos} combinaciones de luces: un LED por calle, en su pin (en memoria y con gpiozero MockFactory)"


def prueba_plan_respaldo(ctx):
    """El plan de respaldo, solo con el Controlador: verdes reales = verde mínimo + k·Δ >= verde_s."""
    cfg = ctx["cfg"]
    cpi = _cpi(cfg)
    ctrl, resp = sem.Controlador(cfg), Respaldo(cfg, cpi.plan)
    luces, decs = [], []
    while ctrl.t < 1800:
        if ctrl.esperando_decision():
            m = ctrl.mascara()
            d = {"t": ctrl.t, "fase": ctrl.fase, "t_fase": ctrl.t_fase, "rojo": tuple(ctrl.rojo()), "mascara": m}
            d["pedida"] = resp.elegir(ctrl, m)
            d["ejecutada"], d["dur"] = ctrl.decidir(d["pedida"])
            decs.append(d)
        luces.append("".join(ctrl.luces()))
        ctrl.avanzar()
    sem._verificar(cfg, luces, decs)
    verdes = {}
    for d in decs:
        if d["ejecutada"] != d["fase"]:
            verdes.setdefault(d["fase"], []).append(d["t_fase"])
    for a, v in cpi.plan:
        f = cfg.fases[a]
        esperado = f.verde_min + math.ceil(max(0, v - f.verde_min) / cfg.delta) * cfg.delta
        if f.verde_max is not None:
            esperado = min(esperado, f.verde_max)
        reales = verdes.get(a, [])[1:]
        _comprobar(reales and all(x == esperado for x in reales),
                   f"{nombre_fase(cfg, a)}: verdes {sorted(set(reales))}; se esperaban {esperado} s")
    orden = [d["ejecutada"] for d in decs if d["ejecutada"] != d["fase"]]
    plan = [a for a, _ in cpi.plan]
    _comprobar(all(orden[i + 1] == plan[(plan.index(orden[i]) + 1) % len(plan)] for i in range(len(orden) - 1)),
               "el respaldo no sigue el orden del plan")
    return f"{cpi.texto_plan(cfg)}: verdes reales iguales a los del plan, en su orden, y reglas del controlador"


def prueba_lockstep(ctx):
    cfg, D = ctx["cfg"], 1800
    fuente = _FuentePrueba(cfg, D, _SimSintetico(cfg))
    with tempfile.TemporaryDirectory() as d:
        reg = RegistroCSV(Path(d) / "r.csv", columnas_registro(cfg))
        prog, r, salida = _correr_pi(ctx, _cpi(cfg, fuente.puerto), "lockstep", 0, registro=reg)
        with open(Path(d) / "r.csv", encoding="utf-8") as f:
            filas = list(csv.DictReader(f))
    fuente.hilo.join(5)
    _comprobar(not fuente.errores, f"la fuente falló: {fuente.errores}")
    _comprobar(r["segundos"] == D and "fuente terminó" in r["motivo"], f"terminó en {r['segundos']} s: {r['motivo']}")
    _comprobar(fuente.luces == prog.luces_seg[:D], "la fuente no recibió las luces de los pines")
    _verificar_reglas(cfg, prog)
    _comprobar(all(luz_encendida(v) == "R" for v in salida.estado().values()), "al terminar no quedó todo en rojo")
    _comprobar(len(filas) == D + 1 and list(filas[0].keys()) == columnas_registro(cfg), "registro CSV")
    _comprobar(sum(int(f["decision"] or 0) for f in filas) == r["decisiones"] and
               sum(int(f["frame"] or 0) for f in filas) + 1 == r["frames"] == D // int(cfg.intervalo_frames) + 1,
               "decisiones o frames del registro")
    ctx["lockstep"] = prog
    return (f"{D} s por TCP: {r['decisiones']} decisiones, {r['cambios']} cambios, {r['frames']} frames; luces de "
            f"los pines = luces recibidas por la fuente; reglas del controlador; registro de {len(filas)} filas")


def prueba_igual_al_entrenamiento(ctx):
    """Pi en lockstep contra SimPrueba por TCP = Entorno + correr_episodio del entrenamiento con la
    misma política y semilla: mismos estados en cada decisión y mismas luces cada segundo."""
    try:
        from entorno import Entorno, correr_episodio
        from sim_prueba import SimPrueba
    except ImportError:
        raise _Omitida("solo en la PC (necesita entrenamiento/entorno.py y sim_prueba.py)") from None
    cfg, D, semilla = ctx["cfg"], 1800, 21

    class Grabar(SimPrueba):
        def reiniciar(self, s):
            self.grabadas = []
            return super().reiniciar(s)

        def avanzar(self, luces):
            self.grabadas.append("".join(luces))
            return super().avanzar(luces)

    sim_ref = Grabar(cfg)
    estados_ref = []

    def elegir(estado, mascara):
        estados_ref.append(np.array(estado, copy=True))
        return ctx["pol"].elegir(estado, mascara)

    correr_episodio(Entorno(cfg, sim_ref, 0.0, 1.0, D, None), elegir, semilla, cortar=False)
    fuente = _FuentePrueba(cfg, D, SimPrueba(cfg), semilla=semilla)
    prog, r, _ = _correr_pi(ctx, _cpi(cfg, fuente.puerto), "lockstep", 0)
    fuente.hilo.join(5)
    _comprobar(not fuente.errores and r["segundos"] == D, f"sesión: {fuente.errores} {r['motivo']}")
    pi = [x for x, d in zip(prog.estados, prog.historial) if d["t"] < D]
    _comprobar(len(pi) == len(estados_ref), f"{len(pi)} decisiones en la Pi y {len(estados_ref)} en el entrenamiento")
    for i, (a, b) in enumerate(zip(pi, estados_ref)):
        _comprobar(np.array_equal(a, b), f"decisión {i}: el estado de la Pi difiere del del entrenamiento")
    _comprobar(fuente.luces == sim_ref.grabadas, "las luces difieren")
    return f"{len(pi)} estados idénticos (float32, bit a bit) y {D} s de luces idénticas a las de entorno.Entorno"


def prueba_tiempo_real(ctx):
    cfg, D, v = ctx["cfg"], 600, 100
    ref = ctx.get("lockstep")
    if ref is None:
        raise _Omitida("requiere la prueba de lockstep")
    fuente = _FuentePrueba(cfg, D, _SimSintetico(cfg))
    prog, r, _ = _correr_pi(ctx, _cpi(cfg, fuente.puerto), "tiempo_real", v)
    fuente.hilo.join(5)
    _comprobar(not fuente.errores and D <= r["segundos"] <= D + 1 and "fuente terminó" in r["motivo"],
               f"sesión: {fuente.errores}; {r['segundos']} s, {r['motivo']}")
    _verificar_reglas(cfg, prog)
    iguales = prog.luces_seg[:D] == ref.luces_seg[:D]
    texto = (f"{D} s a velocidad {v}: {r['conteos_tarde']} respuestas tardías, {r['atrasados']} segundos atrasados "
             f"(máx. {r['atraso_max_s']:.2f} s)")
    if r["conteos_tarde"] == 0:
        _comprobar(iguales, "sin respuestas tardías, las luces deberían ser las de lockstep")
        return texto + "; luces idénticas a las de lockstep; reglas del controlador"
    raise _Aviso(texto + ("; aun así, luces idénticas a lockstep" if iguales else
                          "; luces distintas de lockstep (esperable con respuestas tardías)"))


def prueba_respaldo(ctx):
    """Conteos perdidos entre t = 200 y 400 (el tráfico sigue): respaldo, recuperación y vuelta del agente."""
    cfg, D, v = ctx["cfg"], 900, 200
    fuente = _FuentePrueba(cfg, D, _SimSintetico(cfg), callar=lambda t: 200 <= t < 400)
    cpi = _cpi(cfg, fuente.puerto)
    prog, r, _ = _correr_pi(ctx, cpi, "tiempo_real", v)
    fuente.hilo.join(5)
    _comprobar(not fuente.errores and D <= r["segundos"] <= D + 1, f"sesión: {fuente.errores}; {r['segundos']} s, "
                                                                     f"{r['motivo']}")
    _verificar_reglas(cfg, prog)
    cambios = [(t, x.split(" ")[1]) for t, x in prog.eventos if x.startswith("control:")]
    nombres = [c for _, c in cambios]
    _comprobar(nombres == ["respaldo", "recuperando", "agente"], f"cambios de control: {cambios}")
    (t1, _), (t2, _), (t3, _) = cambios
    s = cpi.sin_conteos_s
    # El último conteo (medición de t = 199) llega en t = 199; respaldo cuando su edad supera s.
    _comprobar(199 + s + 1 <= t1 <= 199 + s + 3, f"entró en respaldo en t = {t1}; se esperaba en t = {199 + s + 1:g}")
    _comprobar(400 <= t2 <= 403, f"empezó a recuperarse en t = {t2}")
    _comprobar(t3 - t2 >= cpi.volver_tras_s and t3 - t2 <= cpi.volver_tras_s + 2, f"volvió el agente en t = {t3}")
    resp = Respaldo(cfg, cpi.plan)
    en_respaldo = [d for d in prog.historial if t1 <= d["t"] < t3]
    _comprobar(en_respaldo, "no hubo decisiones en respaldo")
    for d in en_respaldo:
        falso = types.SimpleNamespace(fase=d["fase"], t_fase=d["t_fase"])
        _comprobar(d["pedida"] == resp.elegir(falso, d["mascara"]), f"t = {d['t']}: decidió el agente en respaldo")
    return (f"sin conteos desde t = 200: respaldo en t = {t1}, recuperación en t = {t2}, agente en t = {t3}; "
            f"{len(en_respaldo)} decisiones del plan; reglas del controlador en todo momento")


def prueba_reconexion(ctx):
    """La fuente corta la conexión sin "fin" en t = 150; la Pi sigue en respaldo, se reconecta y se recupera."""
    cfg, v = ctx["cfg"], 200
    fuente = _FuentePrueba(cfg, 500, _SimSintetico(cfg), cortar_en=150, sesiones=2)
    cpi = _cpi(cfg, fuente.puerto, enlace={"reintento_s": 0.2})          # 0,2 s reales = 40 s del controlador
    prog, r, _ = _correr_pi(ctx, cpi, "tiempo_real", v)
    fuente.hilo.join(5)
    _verificar_reglas(cfg, prog)
    nombres = [x.split(" ")[1] for _, x in prog.eventos if x.startswith("control:")]
    _comprobar(r["reconexiones"] == 1 and len(fuente.segundos) == 2, f"reconexiones {r['reconexiones']}, sesiones {fuente.segundos}")
    _comprobar(nombres == ["respaldo", "recuperando", "agente"], f"cambios de control: {nombres}")
    _comprobar("fuente terminó" in r["motivo"], f"motivo: {r['motivo']}")
    return (f"corte en t = 150, reconexión, recuperación y fin de la segunda sesión en t = {r['segundos']} s; "
            f"reglas del controlador")


def prueba_rechazos(ctx):
    cfg = ctx["cfg"]
    detalle = []
    f = _FuentePrueba(cfg, 100, _SimSintetico(cfg), huella="otra")
    _debe_fallar(lambda: _correr_pi(ctx, _cpi(cfg, f.puerto), "lockstep", 0), E.Rechazo, "huella")
    detalle.append("huella distinta")
    f = _FuentePrueba(cfg, 100, _SimSintetico(cfg), calles=list(cfg.calles) + ["X"])
    _debe_fallar(lambda: _correr_pi(ctx, _cpi(cfg, f.puerto), "tiempo_real", 50), E.Rechazo, "calles")
    detalle.append("otras calles")
    f = _FuentePrueba(cfg, 100, _SimSintetico(cfg), callar=lambda t: True)
    salidas = []

    def sin_respuesta():
        cpi = _cpi(cfg, f.puerto, enlace={"espera_respuesta_s": 0.5})
        salida = SalidaSimulada(cpi.pines, cfg.calles)
        salidas.append(salida)
        fuente = Fuente(cfg, cpi.host, cpi.puerto, "lockstep", False, cpi.reintento_s)
        Programa(cfg, cpi, ctx["pol"], salida, fuente, "lockstep", 0, imprimir=lambda *a: None).correr()

    _debe_fallar(sin_respuesta, E.ErrorEnlace, "no respondió")
    _comprobar(all(luz_encendida(v) == "R" for v in salidas[0].estado().values()), "tras el error no quedó todo en rojo")
    detalle.append("lockstep sin respuesta (termina en rojo)")
    srv = E.escuchar("127.0.0.1", 0)
    puerto = srv.getsockname()[1]

    def camaras():
        conn = E.aceptar(srv, 10)
        conn.recibir(5)
        conn.enviar(E.msg_listo(cfg.calles, None, [0] * cfg.n_calles, False, 0, "cámaras de prueba"))
        try:
            conn.recibir(5)
        except E.ErrorEnlace:
            pass
        conn.cerrar()
        srv.close()

    threading.Thread(target=camaras, daemon=True).start()
    _debe_fallar(lambda: _correr_pi(ctx, _cpi(cfg, puerto), "tiempo_real", 50), E.Rechazo, "velocidad")
    detalle.append("fuente real con velocidad distinta de 1")
    _debe_fallar(lambda: _correr_pi(ctx, _cpi(cfg, 1, enlace={"espera_conexion_s": 0.3}), "lockstep", 0),
                 E.ErrorEnlace, "no hubo conexión")
    detalle.append("lockstep sin fuente")
    _debe_fallar(lambda: Programa(cfg, _cpi(cfg, enlace={"enviar_luces": False}), ctx["pol"], None, None, "lockstep"),
                 ValueError, "enviar_luces")
    return "; ".join(detalle)


def ejecutar_pruebas(ruta_config):
    print(f"Pruebas de principal_pi.py (Python {sys.version.split()[0]}, NumPy {np.__version__})")
    try:
        cfg = sem.cargar_config(ruta_config)
    except Exception as e:
        print(f"FALLO     cargar {ruta_config}: {e}")
        return 1
    print(f"Config: {Path(ruta_config).resolve()} | huella {cfg.huella} | rojo máximo {cfg.rojo_max} s | "
          f"Δ {cfg.delta} s | conteos cada {cfg.intervalo_frames:g} s\n")
    tmp = tempfile.TemporaryDirectory()
    rng = np.random.default_rng(5)
    capas = [cfg.n_variables, 32, 32, cfg.n_fases]
    ruta = Path(tmp.name) / "modelo.npz"
    sem.guardar_modelo_npz(ruta, [rng.normal(0, 0.5, (capas[i + 1], capas[i])) for i in range(3)],
                           [rng.normal(0, 0.1, capas[i + 1]) for i in range(3)], cfg)
    ctx = {"cfg": cfg, "pol": sem.Politica(cfg, ruta)}
    pruebas = [
        ("mensajes del enlace", prueba_mensajes),
        ("pi.json", prueba_pi_json),
        ("pines", prueba_salida),
        ("plan de respaldo", prueba_plan_respaldo),
        ("lockstep por TCP", prueba_lockstep),
        ("igual que el entrenamiento", prueba_igual_al_entrenamiento),
        ("tiempo real", prueba_tiempo_real),
        ("pérdida de conteos y respaldo", prueba_respaldo),
        ("corte y reconexión", prueba_reconexion),
        ("rechazos y errores", prueba_rechazos),
    ]
    fallos = avisos = omitidas = 0
    inicio = time.perf_counter()
    try:
        for nombre, prueba in pruebas:
            t0 = time.perf_counter()
            try:
                detalle = prueba(ctx)
                print(f"OK        {nombre} ({time.perf_counter() - t0:.1f} s): {detalle}")
            except _Omitida as e:
                omitidas += 1
                print(f"OMITIDA   {nombre}: {e}")
            except _Aviso as e:
                avisos += 1
                print(f"AVISO     {nombre} ({time.perf_counter() - t0:.1f} s): {e}")
            except AssertionError as e:
                fallos += 1
                print(f"FALLO     {nombre}: {e}")
            except Exception:
                fallos += 1
                print(f"ERROR     {nombre}:")
                traceback.print_exc()
    finally:
        tmp.cleanup()
    total = len(pruebas)
    print(f"\n{total - fallos - avisos - omitidas}/{total} correctas, {avisos} con aviso, {omitidas} omitidas, "
          f"{fallos} con fallos ({time.perf_counter() - inicio:.0f} s)")
    return 0 if fallos == 0 else 1


# =====================================================================
# 8. PUNTO DE ENTRADA
# =====================================================================
def main():
    ap = argparse.ArgumentParser(description="Semáforo DDQN en la Raspberry Pi: controlador, pines y fuente de conteos.")
    ap.add_argument("--config", default=str(DIR_BASE / "config.json"), help="config.json del modelo (modelo.npz en su carpeta)")
    ap.add_argument("--pi", default=str(DIR_BASE / "pi.json"), help="pines, enlace, respaldo y registro")
    ap.add_argument("--modo", choices=E.MODOS, default="tiempo_real")
    ap.add_argument("--velocidad", type=float, default=1.0,
                    help="segundos del controlador por segundo real (1 = reloj real; 0 = sin pausas, solo lockstep)")
    ap.add_argument("--host", default=None, help="dirección de la fuente (reemplaza la de pi.json)")
    ap.add_argument("--puerto", type=int, default=None, help="puerto de la fuente (reemplaza el de pi.json)")
    ap.add_argument("--duracion", type=int, default=None, help="detenerse en t = DURACION s")
    ap.add_argument("--sin-hardware", action="store_true", help="pines simulados (para la PC)")
    ap.add_argument("--mostrar", action="store_true", help="una línea por segundo con el LED encendido de cada calle")
    ap.add_argument("--probar", action="store_true", help="ejecutar las pruebas")
    args = ap.parse_args()
    if args.probar:
        return ejecutar_pruebas(args.config)
    salida = None
    try:
        cfg = sem.cargar_config(args.config)
        pol = sem.Politica(cfg)
        cpi = cargar_pi(args.pi, cfg, args.host, args.puerto)
        for a in cpi.avisos:
            print(f"AVISO: {a}")
        if args.mostrar and args.velocidad == 0:
            print("AVISO: --mostrar con --velocidad 0 imprime miles de líneas por segundo")
        salida, texto_salida = crear_salida(cpi, cfg.calles, args.sin_hardware)
        fuente = Fuente(cfg, cpi.host, cpi.puerto, args.modo, reconectar=args.modo == "tiempo_real",
                        reintento_s=cpi.reintento_s, modelo=E.huella_archivo(cfg.ruta_modelo))
        registro = None
        if cpi.carpeta_registro is not None:
            registro = RegistroCSV(cpi.carpeta_registro / f"pi_{datetime.now():%Y%m%d-%H%M%S}.csv", columnas_registro(cfg))
        prog = Programa(cfg, cpi, pol, salida, fuente, args.modo, args.velocidad, registro, args.duracion, args.mostrar)
        print(f"Modelo: {cfg.ruta_modelo} (archivo {fuente.modelo}) | huella {cfg.huella} | Δ {cfg.delta} s | rojo "
              f"máximo {cfg.rojo_max} s | conteos cada {cfg.intervalo_frames:g} s | L = {cfg.alcance:g} m")
        print(f"Pines: {texto_salida} | " + " | ".join(
            f"{c}: R{cpi.pines[c]['R']} A{cpi.pines[c]['A']} V{cpi.pines[c]['V']}" for c in cfg.calles))
        print(f"Modo {args.modo}, velocidad {args.velocidad:g} | fuente {cpi.host}:{cpi.puerto} | respaldo "
              f"{cpi.texto_plan(cfg)} tras {cpi.sin_conteos_s:g} s sin conteos"
              + (f" | registro {registro.ruta}" if registro else ""))
        print(f"Esperando a la fuente (hasta {cpi.espera_conexion_s:g} s)... Ctrl+C para detener.\n")
        r = prog.correr()
        print("\n" + texto_resumen(r, cfg))
        return 0
    except (ValueError, FileNotFoundError, RuntimeError, E.ErrorEnlace) as e:
        print(f"\nERROR: {e}")
        return 1
    finally:
        if salida is not None:
            salida.cerrar()
            if not args.sin_hardware:
                print("Pines liberados: lo que muestren ahora los LEDs depende de la resistencia de base a GND "
                      "(en un cruce real no sería un estado seguro).")


if __name__ == "__main__":
    sys.exit(main())
