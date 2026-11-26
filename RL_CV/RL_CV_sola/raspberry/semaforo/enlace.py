#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
enlace.py - Contrato de mensajes entre la Pi (principal_pi.py) y la fuente de conteos.

La Pi no sabe quién le manda los conteos: hoy es puente_sumo.py (SUMO en la PC); después será
el programa de YOLO en la misma Pi. Cualquier programa que cumpla este contrato le sirve, igual
que interfaz_sim.py lo es para el entrenamiento. Ocultar detrás de una interfaz lo que puede
cambiar es el criterio de modularización de Parnas (1972); aplicarlo a este mensaje es
adaptación propia.

Solo Python estándar (va en la Pi y en la PC).

TRANSPORTE
  TCP, un objeto JSON (UTF-8) por línea. La fuente escucha y la Pi se conecta: cambiar de fuente
  es cambiar host y puerto en pi.json. Las calles van por nombre, nunca por posición: un mensaje
  con calles de más o de menos se rechaza.

MENSAJES (campo "tipo")
  Pi -> fuente
    hola     {version, calles, huella, modo, t, modelo}   al conectarse (modelo: huella_archivo
             de modelo.npz, para avisar si la PC verifica con otro archivo)
    luces    {seq, t, luces: {"N": "V", ...}}          cada segundo (si pi.json lo pide)
    fin      {t, motivo}                                la Pi se detiene
  fuente -> Pi
    listo    {version, calles, huella, simulado, t, conteos: {...}, info}
    conteos  {seq, t, conteos: {"N": 3, ...}}
             simulador: respuesta a "luces" con la misma seq y la medición de t + 1;
             YOLO: un mensaje por conteo, con seq creciente propia.
    fin      {t, motivo, resumen}                       la fuente terminó (simulador)
  cualquiera de las dos
    error    {mensaje}                                  y cierra la conexión
  huella: la de semaforo.py (calles, fases, controlador y observador). Si las dos partes la
  envían y no coinciden, el modelo de la Pi no corresponde a la configuración de la fuente (por
  ejemplo, otro alcance L) y se rechaza la sesión. Una fuente sin configuración (YOLO) envía null.
"""

import hashlib
import json
import math
import numbers
import os
import socket
import threading
import time

VERSION = 1
PUERTO = 5050
LETRAS = ("V", "A", "R")                  # iguales a semaforo.VERDE, AMBAR y ROJO
MODOS = ("lockstep", "tiempo_real")
MAX_LINEA = 64 * 1024                     # bytes por mensaje
PASO_SOCKET_S = 0.25                      # espera máxima de cada recv/send (permite Ctrl+C y cierres)


class ErrorEnlace(Exception):
    """Problema del enlace."""


class ConexionCerrada(ErrorEnlace):
    """La conexión se cerró o se perdió."""


class ErrorProtocolo(ErrorEnlace):
    """Llegó un mensaje que no cumple el contrato."""


class Rechazo(ErrorEnlace):
    """La sesión no puede seguir: configuración incompatible o error informado por la otra parte."""


# =====================================================================
# 1. MENSAJES
# =====================================================================
def _numero(v):
    return isinstance(v, numbers.Real) and not isinstance(v, bool) and math.isfinite(v)


def _entero(msg, clave):
    v = msg.get(clave)
    if isinstance(v, bool) or not isinstance(v, int) or v < 0:
        raise ErrorProtocolo(f"'{clave}' debe ser un entero >= 0 (es {v!r})")
    return v


def _tiempo(msg, clave="t"):
    v = msg.get(clave)
    if not _numero(v) or v < 0:
        raise ErrorProtocolo(f"'{clave}' debe ser un número >= 0 (es {v!r})")
    return v


def _lista_calles(v):
    if (not isinstance(v, list) or len(v) < 2 or not all(isinstance(c, str) and c for c in v)
            or len(set(v)) != len(v)):
        raise ErrorProtocolo(f"'calles' debe ser una lista de nombres distintos (es {v!r})")
    return v


def _por_calle(msg, clave, calles):
    d = msg.get(clave)
    if not isinstance(d, dict):
        raise ErrorProtocolo(f"'{clave}' debe ser un objeto {{calle: valor}} (es {d!r})")
    faltan = [c for c in calles if c not in d]
    sobran = [k for k in d if k not in calles]
    if faltan or sobran:
        partes = ([f"faltan {faltan}"] if faltan else []) + ([f"sobran {sobran}"] if sobran else [])
        raise ErrorProtocolo(f"'{clave}': {' y '.join(partes)} (calles: {list(calles)})")
    return [d[c] for c in calles]


def conteos_de(msg, calles):
    """Conteos del mensaje en el orden de `calles` (números finitos >= 0)."""
    valores = _por_calle(msg, "conteos", calles)
    for c, v in zip(calles, valores):
        if not _numero(v) or v < 0:
            raise ErrorProtocolo(f"conteo no válido para {c}: {v!r} (debe ser un número >= 0)")
    return [float(v) for v in valores]


def luces_de(msg, calles):
    """Luces del mensaje en el orden de `calles` ('V', 'A' o 'R')."""
    valores = _por_calle(msg, "luces", calles)
    for c, v in zip(calles, valores):
        if v not in LETRAS:
            raise ErrorProtocolo(f"luz no válida para {c}: {v!r} (debe ser V, A o R)")
    return list(valores)


def validar(msg, calles):
    """Comprueba la forma de un mensaje recibido y lo devuelve. `calles`: las de esta parte."""
    if not isinstance(msg, dict):
        raise ErrorProtocolo("el mensaje debe ser un objeto JSON")
    tipo = msg.get("tipo")
    if tipo in ("hola", "listo"):
        if msg.get("version") != VERSION:
            raise ErrorProtocolo(f"versión del protocolo {msg.get('version')!r}; esta parte usa {VERSION}")
        otras = _lista_calles(msg.get("calles"))
        if set(otras) != set(calles):
            raise Rechazo(f"la otra parte tiene las calles {otras} y esta {list(calles)}")
        h = msg.get("huella")
        if h is not None and not isinstance(h, str):
            raise ErrorProtocolo("'huella' debe ser un texto o null")
        _tiempo(msg)
        if tipo == "hola":
            if msg.get("modo") not in MODOS:
                raise ErrorProtocolo(f"'modo' debe ser uno de {MODOS} (es {msg.get('modo')!r})")
            if msg.get("modelo") is not None and not isinstance(msg.get("modelo"), str):
                raise ErrorProtocolo("'modelo' debe ser un texto o null")
        else:
            if not isinstance(msg.get("simulado"), bool):
                raise ErrorProtocolo("'simulado' debe ser true o false")
            conteos_de(msg, calles)
    elif tipo == "luces":
        _entero(msg, "seq")
        _entero(msg, "t")
        luces_de(msg, calles)
    elif tipo == "conteos":
        _entero(msg, "seq")
        _tiempo(msg)
        conteos_de(msg, calles)
    elif tipo == "fin":
        if not isinstance(msg.get("motivo", ""), str):
            raise ErrorProtocolo("'motivo' debe ser un texto")
        if msg.get("resumen") is not None and not isinstance(msg.get("resumen"), dict):
            raise ErrorProtocolo("'resumen' debe ser un objeto o null")
    elif tipo == "error":
        if not isinstance(msg.get("mensaje", ""), str):
            raise ErrorProtocolo("'mensaje' debe ser un texto")
    else:
        raise ErrorProtocolo(f"tipo de mensaje desconocido: {tipo!r}")
    return msg


def _dict(calles, valores, nombre):
    valores = list(valores)
    if len(valores) != len(calles):
        raise ValueError(f"{nombre}: se esperan {len(calles)} valores, hay {len(valores)}")
    return dict(zip(calles, valores))


def huella_archivo(ruta):
    """Resumen del contenido de un archivo (p. ej. modelo.npz): distingue dos modelos con el mismo
    config.json, que la huella de semaforo.py no distingue."""
    with open(ruta, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()[:16]


def msg_hola(calles, huella, modo, t=0, modelo=None):
    return {"tipo": "hola", "version": VERSION, "calles": list(calles), "huella": huella, "modo": modo,
            "t": int(t), "modelo": modelo}


def msg_listo(calles, huella, conteos, simulado, t=0, info=""):
    return {"tipo": "listo", "version": VERSION, "calles": list(calles), "huella": huella,
            "simulado": bool(simulado), "t": t, "conteos": _dict(calles, conteos, "conteos"), "info": info}


def msg_luces(seq, t, calles, luces):
    return {"tipo": "luces", "seq": int(seq), "t": int(t), "luces": _dict(calles, luces, "luces")}


def msg_conteos(seq, t, calles, conteos):
    return {"tipo": "conteos", "seq": int(seq), "t": t, "conteos": _dict(calles, conteos, "conteos")}


def msg_fin(t, motivo, resumen=None):
    return {"tipo": "fin", "t": t, "motivo": str(motivo), "resumen": resumen}


def msg_error(texto):
    return {"tipo": "error", "mensaje": str(texto)}


# =====================================================================
# 2. CONEXIÓN
# =====================================================================
class Conexion:
    """Un socket TCP con mensajes JSON por línea. enviar() se puede llamar desde un hilo mientras
    otro llama a recibir()."""

    def __init__(self, sock):
        sock.settimeout(PASO_SOCKET_S)
        try:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)   # sin demora de Nagle
        except OSError:
            pass
        self._sock = sock
        self._buf = bytearray()
        self._candado = threading.Lock()
        self.cerrada = False

    @classmethod
    def conectar(cls, host, puerto, espera_s=3.0):
        return cls(socket.create_connection((host, int(puerto)), timeout=espera_s))

    def enviar(self, msg):
        datos = (json.dumps(msg, ensure_ascii=False, allow_nan=False, separators=(",", ":")) + "\n").encode("utf-8")
        with self._candado:
            if self.cerrada:
                raise ConexionCerrada("la conexión está cerrada")
            try:
                self._sock.sendall(datos)
            except socket.timeout:
                self.cerrar()                     # pudo quedar medio mensaje: la conexión no sirve
                raise ConexionCerrada("no se pudo enviar a tiempo") from None
            except OSError as e:
                # La otra parte cerró. No se cierra aquí: el hilo que recibe todavía puede leer lo
                # que llegó antes (por ejemplo, el mensaje "fin").
                raise ConexionCerrada(f"no se pudo enviar ({e})") from None

    def recibir(self, espera_s=None):
        """Siguiente mensaje (dict), o None si pasan espera_s segundos sin uno completo
        (espera_s = None: espera sin límite)."""
        limite = None if espera_s is None else time.monotonic() + espera_s
        while True:
            i = self._buf.find(b"\n")
            if i >= 0:
                linea = bytes(self._buf[:i])
                del self._buf[:i + 1]
                if not linea.strip():
                    continue
                try:
                    msg = json.loads(linea.decode("utf-8"))
                except (UnicodeDecodeError, ValueError) as e:
                    raise ErrorProtocolo(f"mensaje que no es JSON válido ({e})") from None
                if not isinstance(msg, dict):
                    raise ErrorProtocolo("el mensaje debe ser un objeto JSON")
                return msg
            if len(self._buf) > MAX_LINEA:
                raise ErrorProtocolo(f"mensaje de más de {MAX_LINEA} bytes (¿falta el salto de línea?)")
            if limite is not None and time.monotonic() >= limite:
                return None
            if self.cerrada:
                raise ConexionCerrada("la conexión está cerrada")
            try:
                trozo = self._sock.recv(65536)
            except socket.timeout:
                continue
            except OSError as e:
                raise ConexionCerrada("la conexión está cerrada" if self.cerrada
                                      else f"se perdió la conexión ({e})") from None
            if not trozo:
                raise ConexionCerrada("la otra parte cerró la conexión")
            self._buf += trozo

    def cerrar(self):
        self.cerrada = True
        for accion in (lambda: self._sock.shutdown(socket.SHUT_RDWR), self._sock.close):
            try:
                accion()
            except OSError:
                pass


def escuchar(host, puerto):
    """Socket que espera a la Pi (puerto 0: uno libre, para las pruebas)."""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if os.name != "nt":                            # en Windows, SO_REUSEADDR permite robar el puerto
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((host, int(puerto)))
    srv.listen(1)
    srv.settimeout(PASO_SOCKET_S)
    return srv


def aceptar(srv, espera_s=None):
    """Conexion con la Pi, o None si pasan espera_s segundos (None: sin límite)."""
    limite = None if espera_s is None else time.monotonic() + espera_s
    while True:
        try:
            sock, _ = srv.accept()
            return Conexion(sock)
        except socket.timeout:
            if limite is not None and time.monotonic() >= limite:
                return None


# =====================================================================
# 3. SESIÓN DEL LADO DE UN SIMULADOR
# =====================================================================
class SesionSimulador:
    """Lado de la fuente para un simulador de paso fijo de 1 s (puente_sumo.py y las pruebas).

    reiniciar() -> conteos de t = 0 (orden de `calles`)
    avanzar(luces) -> conteos de t + 1, con las luces del segundo [t, t + 1); None: no responder
                      ese segundo (solo para probar la pérdida de conteos)
    resumen() -> dict opcional que viaja en el mensaje "fin"
    Por cada mensaje "luces" avanza 1 s y responde con la misma seq. Termina al llegar a
    duracion_s (envía "fin") o cuando la Pi envía "fin". correr() devuelve los segundos simulados.
    """

    def __init__(self, conexion, calles, huella, duracion_s, reiniciar, avanzar, info="", resumen=None,
                 espera_s=120.0):
        self.conexion = conexion
        self.calles = list(calles)
        self.huella = huella
        self.duracion = int(duracion_s)
        self._reiniciar, self._avanzar, self._resumen = reiniciar, avanzar, resumen
        self.info = info
        self.espera_s = espera_s
        self.segundos = 0
        self.motivo = ""
        self.modo_pi = None
        self.hola = None

    def correr(self):
        c = self.conexion
        try:
            msg = c.recibir(self.espera_s)
            if msg is None:
                raise ErrorEnlace(f"la Pi no saludó en {self.espera_s:g} s")
            if msg.get("tipo") == "error":
                raise Rechazo(f"la Pi informó un error: {msg.get('mensaje')}")
            if msg.get("tipo") != "hola":
                raise ErrorProtocolo(f"se esperaba 'hola' y llegó {msg.get('tipo')!r}")
            validar(msg, self.calles)
            h = msg.get("huella")
            if h is not None and self.huella is not None and h != self.huella:
                raise Rechazo(f"la huella del modelo de la Pi ({h}) no coincide con la de la configuración de la "
                              f"fuente ({self.huella}): fases, controlador u observador distintos (por ejemplo, "
                              f"otro alcance L). Usa el config.json que viaja con el modelo en los dos lados.")
            self.modo_pi = msg["modo"]
            self.hola = msg
            c.enviar(msg_listo(self.calles, self.huella, self._reiniciar(), True, 0, self.info))
            base = None
            while self.segundos < self.duracion:
                msg = c.recibir(self.espera_s)
                if msg is None:
                    raise ErrorEnlace(f"la Pi no envió luces en {self.espera_s:g} s")
                tipo = msg.get("tipo")
                if tipo == "fin":
                    self.motivo = f"la Pi se detuvo ({msg.get('motivo', '')})"
                    return self.segundos
                if tipo == "error":
                    raise Rechazo(f"la Pi informó un error: {msg.get('mensaje')}")
                if tipo != "luces":
                    raise ErrorProtocolo(f"se esperaba 'luces' y llegó {tipo!r}")
                validar(msg, self.calles)
                if base is None:
                    base = msg["seq"]
                if msg["seq"] != base + self.segundos:
                    raise ErrorProtocolo(f"luces con seq {msg['seq']}; se esperaba {base + self.segundos} "
                                         f"(¿se perdió un mensaje?)")
                conteos = self._avanzar(luces_de(msg, self.calles))
                self.segundos += 1
                if conteos is not None:
                    c.enviar(msg_conteos(msg["seq"], self.segundos, self.calles, conteos))
            self.motivo = "duración del episodio"
            c.enviar(msg_fin(self.segundos, "duracion", self._resumen() if self._resumen else None))
            self._esperar_cierre()
            return self.segundos
        except (ErrorProtocolo, Rechazo) as e:
            try:
                c.enviar(msg_error(e))
            except ErrorEnlace:
                pass
            raise
        finally:
            c.cerrar()

    def _esperar_cierre(self, espera_s=2.0):
        """Tras "fin", deja que la Pi lo lea y cierre ella: si se cierra antes y la Pi alcanza a
        enviar otro mensaje, el reinicio de la conexión (RST) puede borrar el "fin" sin leer."""
        limite = time.monotonic() + espera_s
        try:
            while time.monotonic() < limite:
                self.conexion.recibir(max(0.0, limite - time.monotonic()))
        except ErrorEnlace:
            pass
