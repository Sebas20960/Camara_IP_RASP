#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
semaforo.py - Controlador, observador y política del semáforo (va en la Raspberry Pi).

Solo Python estándar + NumPy (Python 3.9 o superior; NumPy: solo funciones básicas).
No usa SUMO, TraCI ni PyTorch. El entrenamiento importará este mismo archivo: así la
simulación y la Pi usan el mismo controlador y construyen el mismo estado.

    python semaforo.py --probar                      ejecuta las pruebas
    python semaforo.py --probar --config otra.json   ídem con otra configuración

Contenido (decisiones.md, secciones 2, 3 y 5):
  cargar_config()       lee y valida config.json; calcula la región segura del rojo
                        máximo y comprueba que el arranque esté dentro (factibilidad).
  Controlador           fases, verdes mínimos y máximos, rojo máximo por calle (escudo con
                        región segura exacta, ver _Escudo), ámbar, despeje, máscara de
                        acciones y luz de cada calle segundo a segundo.
  Observador            estado a partir de los conteos por calle de la cámara.
  Politica              red online en NumPy (modelo.npz); elige la acción permitida con
                        mayor Q.
  guardar_modelo_npz()  escribe el .npz (lo usará la exportación del entrenamiento).

Convenciones:
  - Calles en el orden de config.json ("calles"): N, S, E, O.
  - Acción a (0..5) = fase a+1 de decisiones.md (1 = N+S, 2 = E+O, 3..6 = una calle).
  - Luces: "V" verde, "A" ámbar, "R" rojo.
  - El controlador cuenta segundos enteros desde el arranque.

Bucle previsto (main de la Pi; el entrenamiento hace lo mismo con el simulador):
    cfg = cargar_config("config.json")
    ctrl, obs, pol = Controlador(cfg), Observador(cfg), Politica(cfg)
    cada segundo:
        if ctrl.esperando_decision():
            accion, duracion = ctrl.decidir(pol.elegir(obs.estado(ctrl), ctrl.mascara()))
        aplicar(ctrl.luces())            # luces del segundo [t, t+1)
        ctrl.avanzar()
    cada frame de YOLO:
        obs.actualizar(marca_de_tiempo_s, conteos_por_calle, ctrl.luces())

Formato de modelo.npz:
    formato    "semaforo-npz-1"
    huella     resumen de config.json (calles, fases, controlador, observador). Si no
               coincide con el config.json en uso, la política se niega a cargar.
    variables  nombres de las entradas de la red, en orden.
    W0, b0, W1, b1, ...   capas lineales. Wi tiene forma (salidas, entradas), igual que
               nn.Linear.weight de PyTorch. ReLU entre capas; la última capa es lineal.
"""

import argparse
import copy
import hashlib
import itertools
import json
import math
import sys
import tempfile
import time
import traceback
from pathlib import Path

import numpy as np

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

VERDE, AMBAR, ROJO = "V", "A", "R"
FORMATO_NPZ = "semaforo-npz-1"
MAX_ESTADOS_REGION = 50_000_000     # tope de memoria para la región segura
DIR_BASE = Path(__file__).resolve().parent


# =====================================================================
# 1. CONFIGURACION
# =====================================================================
class Fase:
    """Una fase del semáforo = una acción del agente."""

    def __init__(self, indice, ident, nombre, verde, verde_min, verde_max):
        self.indice = indice                  # acción del agente (0..n_fases-1)
        self.id = ident                       # número en decisiones.md (1..n_fases)
        self.nombre = nombre
        self.verde = tuple(verde)             # índices de las calles con verde
        self.bits = sum(1 << k for k in self.verde)
        self.verde_min = verde_min
        self.verde_max = verde_max            # None: sin límite propio

    def __repr__(self):
        return f"Fase {self.id} ({self.nombre})"


def _seccion(datos, nombre):
    s = datos.get(nombre)
    if not isinstance(s, dict):
        raise ValueError(f"config: falta la sección '{nombre}'")
    return s


def _valor(seccion, clave, donde):
    if clave not in seccion:
        raise ValueError(f"config: falta '{donde}.{clave}'")
    return seccion[clave]


def _entero(v, nombre, minimo):
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    if isinstance(v, bool) or not isinstance(v, int):
        raise ValueError(f"config: {nombre} debe ser un entero (es {v!r})")
    if v < minimo:
        raise ValueError(f"config: {nombre} debe ser >= {minimo} (es {v})")
    return v


def _positivo(v, nombre):
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v <= 0:
        raise ValueError(f"config: {nombre} debe ser un número positivo (es {v!r})")
    return float(v)


def _mezclar(base, cambios):
    for k, v in cambios.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _mezclar(base[k], v)
        else:
            base[k] = copy.deepcopy(v)


class Configuracion:
    """Parámetros validados de config.json (las claves que empiezan con '_' se ignoran).

    Al crearla se calcula la región segura del rojo máximo y se comprueba que el arranque
    esté dentro (regla 5 del controlador); si no, se lanza ValueError con el mínimo factible.
    No modificar sus atributos después de crearla: para otros valores, usar variante().
    """

    def __init__(self, datos, carpeta=None):
        if not isinstance(datos, dict):
            raise ValueError("config: el archivo debe contener un objeto JSON")
        self.datos = copy.deepcopy(datos)
        self.carpeta = Path(carpeta) if carpeta is not None else Path.cwd()

        calles = datos.get("calles")
        if (not isinstance(calles, list) or len(calles) < 2
                or not all(isinstance(c, str) and c for c in calles)
                or len(set(calles)) != len(calles)):
            raise ValueError("config: 'calles' debe ser una lista de al menos 2 nombres distintos")
        self.calles = list(calles)
        self.n_calles = len(calles)
        posicion = {c: k for k, c in enumerate(calles)}

        lista = datos.get("fases")
        if not isinstance(lista, list) or len(lista) < 2:
            raise ValueError("config: 'fases' debe ser una lista con al menos 2 fases")
        self.fases = []
        vistas = set()
        for i, f in enumerate(lista):
            donde = f"fases[{i}]"
            if not isinstance(f, dict):
                raise ValueError(f"config: {donde} debe ser un objeto")
            ident = _entero(_valor(f, "id", donde), f"{donde}.id", 1)
            if ident != i + 1:
                raise ValueError(f"config: las fases deben tener id 1, 2, 3... en orden "
                                 f"({donde}.id = {ident})")
            verde = _valor(f, "verde", donde)
            if (not isinstance(verde, list) or not verde or len(set(verde)) != len(verde)
                    or any(c not in posicion for c in verde)):
                raise ValueError(f"config: {donde}.verde debe listar calles de 'calles', sin repetir")
            ks = tuple(sorted(posicion[c] for c in verde))
            if len(ks) == self.n_calles:
                raise ValueError(f"config: {donde} da verde a todas las calles")
            if ks in vistas:
                raise ValueError(f"config: {donde} repite el verde de otra fase")
            vistas.add(ks)
            vmin = _entero(_valor(f, "verde_min_s", donde), f"{donde}.verde_min_s", 1)
            vmax = _valor(f, "verde_max_s", donde)
            if vmax is not None:
                vmax = _entero(vmax, f"{donde}.verde_max_s", vmin)
            self.fases.append(Fase(i, ident, str(f.get("nombre", f"Fase {ident}")), ks, vmin, vmax))
        self.n_fases = len(self.fases)
        cubiertas = {k for f in self.fases for k in f.verde}
        sin_verde = [c for k, c in enumerate(self.calles) if k not in cubiertas]
        if sin_verde:
            raise ValueError(f"config: ninguna fase da verde a {sin_verde}")

        c = _seccion(datos, "controlador")
        self.ambar = _entero(_valor(c, "ambar_s", "controlador"), "controlador.ambar_s", 1)
        self.despeje = _entero(_valor(c, "despeje_s", "controlador"), "controlador.despeje_s", 0)
        self.rojo_max = _entero(_valor(c, "rojo_max_s", "controlador"), "controlador.rojo_max_s", 1)
        self.delta = _entero(_valor(c, "intervalo_decision_s", "controlador"),
                             "controlador.intervalo_decision_s", 1)
        inicial = _entero(_valor(c, "fase_inicial", "controlador"), "controlador.fase_inicial", 1)
        if inicial > self.n_fases:
            raise ValueError(f"config: controlador.fase_inicial = {inicial} no existe")
        self.fase_inicial = inicial - 1                 # índice (acción)
        self.transicion = self.ambar + self.despeje     # no cuenta como verde

        o = _seccion(datos, "observador")
        self.intervalo_frames = _positivo(_valor(o, "intervalo_frames_s", "observador"),
                                          "observador.intervalo_frames_s")
        self.alcance = _positivo(_valor(o, "alcance_camara_m", "observador"),
                                 "observador.alcance_camara_m")
        self.cap_cola = _positivo(_valor(o, "cap_cola_veh", "observador"), "observador.cap_cola_veh")

        p = datos.get("politica") or {}
        if not isinstance(p, dict):
            raise ValueError("config: 'politica' debe ser un objeto")
        modelo = Path(str(p.get("modelo", "modelo.npz")))
        self.ruta_modelo = modelo if modelo.is_absolute() else self.carpeta / modelo

        # Orden de las entradas de la red (decisiones.md, sección 5).
        self.nombres_variables = ([f"cola_{x}" for x in self.calles]
                                  + [f"espera_{x}" for x in self.calles]
                                  + [f"rojo_{x}" for x in self.calles]
                                  + [f"fase_{f.id}" for f in self.fases]
                                  + ["t_fase"])
        self.n_variables = len(self.nombres_variables)
        self.huella = self._huella()
        self.escudo = _Escudo(self)
        comprobar_factibilidad(self)

    def _huella(self):
        """Resumen de todo lo que define el estado y el comportamiento del controlador."""
        obj = {
            "formato": FORMATO_NPZ,
            "calles": self.calles,
            "fases": [[[self.calles[k] for k in f.verde], f.verde_min, f.verde_max]
                      for f in self.fases],
            "controlador": [self.ambar, self.despeje, self.rojo_max, self.delta,
                            self.fase_inicial + 1],
            "observador": [self.intervalo_frames, self.alcance, self.cap_cola],
            "variables": self.nombres_variables,
        }
        texto = json.dumps(obj, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(texto.encode("utf-8")).hexdigest()[:16]

    def variante(self, cambios):
        """Copia con algunos valores cambiados, p. ej. {"controlador": {"rojo_max_s": 90}}."""
        datos = copy.deepcopy(self.datos)
        _mezclar(datos, cambios)
        return Configuracion(datos, self.carpeta)


def cargar_config(ruta=None):
    """Lee config.json (por defecto, el de la carpeta de este archivo)."""
    ruta = Path(ruta) if ruta is not None else DIR_BASE / "config.json"
    if not ruta.exists():
        raise FileNotFoundError(f"No encuentro {ruta}")
    with open(ruta, encoding="utf-8") as f:
        try:
            datos = json.load(f)
        except json.JSONDecodeError as e:
            raise ValueError(f"{ruta.name} no es un JSON válido: {e}") from None
    return Configuracion(datos, ruta.parent)


# =====================================================================
# 2. ESCUDO DEL ROJO MAXIMO (región segura exacta)
# =====================================================================
class _Escudo:
    """Garantía del rojo máximo por calle: decide qué acciones están permitidas.

    Estado de decisión: la fase p está en verde y ya cumplió su verde mínimo; rojo[k] son
    los segundos que la calle k lleva en rojo (0 si está en verde). Un estado es seguro si
    existe alguna secuencia futura de cambios que dé verde a cada calle antes de superar
    rojo_max. La máscara solo permite acciones que dejan un estado seguro: es un escudo
    preventivo ("preemptive shield", Alshiekh et al., 2018, Safe Reinforcement Learning
    via Shielding). Como el arranque es seguro (se comprueba al cargar la configuración),
    siempre queda al menos una acción permitida y el tope nunca se supera.

    La región segura es exacta (no restringe más de lo necesario): se calcula una vez como
    el mayor punto fijo de un juego de seguridad. Se parte de todos los estados con
    rojo <= rojo_max - transición y se eliminan, hasta que nada cambie, los estados desde
    los que ningún cambio lleva a otro estado de la región. Basta considerar cambios con
    cada fase sostenida solo su verde mínimo, porque sostener más una fase solo aumenta el
    rojo de las demás; por eso la región es cerrada hacia abajo (si un estado es seguro,
    uno con menos rojo también lo es).
    """

    def __init__(self, cfg, rojo_max=None):
        self.n = cfg.n_calles
        self.nf = cfg.n_fases
        self.bits = [f.bits for f in cfg.fases]
        self.vmin = [f.verde_min for f in cfg.fases]
        self.vmax = [f.verde_max for f in cfg.fases]
        self.C = cfg.despeje
        self.T = cfg.transicion
        self.R = cfg.rojo_max if rojo_max is None else rojo_max
        self.lim = self.R - self.T          # rojo admisible en un instante de decisión
        self.delta = cfg.delta
        self.rojas = [[k for k in range(self.n) if not (b >> k) & 1] for b in self.bits]
        self.region = self._calcular_region() if self.lim >= 0 else None

    def _calcular_region(self):
        L = self.lim + 1
        estados = sum(L ** len(r) for r in self.rojas)
        if estados > MAX_ESTADOS_REGION:
            raise ValueError(f"rojo máximo demasiado grande para calcular la región segura "
                             f"({estados} estados)")
        region = [np.ones((L,) * len(r), dtype=bool) for r in self.rojas]
        while True:
            hubo_cambio = False
            for p in range(self.nf):
                forma = region[p].shape
                coords = np.indices(forma)
                rojo = {k: coords[i] for i, k in enumerate(self.rojas[p])}
                alguno = np.zeros(forma, dtype=bool)
                for q in range(self.nf):
                    if q == p:
                        continue
                    valido = np.ones(forma, dtype=bool)
                    indices = []
                    for k in self.rojas[q]:
                        if (self.bits[p] >> k) & 1:          # pierde el verde
                            v = self.C + self.vmin[q]
                            if v > self.lim:
                                valido[...] = False
                            indices.append(np.full(forma, min(v, self.lim)))
                        else:                                # sigue en rojo
                            v = rojo[k] + self.T + self.vmin[q]
                            valido &= v <= self.lim
                            indices.append(np.minimum(v, self.lim))
                    # Las calles que ganan verde ya cumplen rojo <= lim por estar en la región.
                    alguno |= valido & region[q][tuple(indices)]
                nueva = region[p] & alguno
                if not np.array_equal(nueva, region[p]):
                    region[p] = nueva
                    hubo_cambio = True
            if not hubo_cambio:
                return region

    def seguro(self, p, rojo):
        """¿El estado de decisión (p, rojo) está en la región segura?"""
        if self.region is None:
            return False
        idx = []
        for k in self.rojas[p]:
            if rojo[k] > self.lim:
                return False
            idx.append(rojo[k])
        return bool(self.region[p][tuple(idx)])

    def tras_cambio(self, p, q, rojo):
        """Rojo de cada calle en la primera decisión de q si ahora se cambia de p a q
        (transición + verde mínimo de q). None si alguna calle ya no puede cumplir el tope."""
        lim = self.lim
        bp, bq = self.bits[p], self.bits[q]
        espera = self.T + self.vmin[q]
        pierde = self.C + self.vmin[q]
        nuevo = []
        for k in range(self.n):
            b = 1 << k
            if bq & b:
                if not bp & b and rojo[k] > lim:      # recibe verde a los T s
                    return None
                nuevo.append(0)
            elif bp & b:                              # pierde el verde
                if pierde > lim:
                    return None
                nuevo.append(pierde)
            else:                                     # sigue en rojo
                r = rojo[k] + espera
                if r > lim:
                    return None
                nuevo.append(r)
        return tuple(nuevo)

    def tras_quedarse(self, p, rojo, s):
        bp = self.bits[p]
        return tuple(0 if (bp >> k) & 1 else rojo[k] + s for k in range(self.n))

    def mascara(self, p, t_fase, rojo):
        """(máscara, s): acciones permitidas y segundos que puede durar el paso si se queda
        en p (0 = quedarse está prohibido)."""
        m = [False] * self.nf
        for q in range(self.nf):
            if q != p:
                nuevo = self.tras_cambio(p, q, rojo)
                m[q] = nuevo is not None and self.seguro(q, nuevo)
        lim = self.delta
        if self.vmax[p] is not None:
            lim = min(lim, self.vmax[p] - t_fase)     # se corta en el verde máximo
        s = 0
        if lim >= 1:
            if self.seguro(p, self.tras_quedarse(p, rojo, lim)):
                s = lim
            else:                                     # se acorta para no superar el tope
                bajo, alto = 0, lim                   # bajo: seguro; alto: no seguro
                while alto - bajo > 1:
                    medio = (bajo + alto) // 2
                    if self.seguro(p, self.tras_quedarse(p, rojo, medio)):
                        bajo = medio
                    else:
                        alto = medio
                s = bajo
        m[p] = s >= 1
        return m, s

    def emergencia(self, p, rojo, mascara):
        """Cambio permitido que atiende primero a la calle con más tiempo en rojo (plazo más
        próximo, EDF: Liu y Layland, 1973); en empate, la fase que atiende más calles."""
        mejor = None
        for q in range(self.nf):
            if q == p or not mascara[q]:
                continue
            atiende = [rojo[k] for k in self.rojas[p] if (self.bits[q] >> k) & 1]
            clave = (-max(atiende, default=-1), -len(atiende), q)
            if mejor is None or clave < mejor:
                mejor = clave
        return None if mejor is None else mejor[2]


def _rojo_inicial(cfg):
    """Rojo de cada calle en la primera decisión (arranque todo en rojo + verde mínimo)."""
    p0 = cfg.fases[cfg.fase_inicial]
    espera = cfg.transicion + p0.verde_min
    return tuple(0 if (p0.bits >> k) & 1 else espera for k in range(cfg.n_calles))


def _factible(cfg, rojo_max, escudo=None):
    """¿Se cumple rojo_max desde el arranque? (Después, el escudo mantiene la garantía.)"""
    if cfg.transicion > rojo_max:                     # la fase inicial espera la transición
        return False
    escudo = escudo if escudo is not None else _Escudo(cfg, rojo_max)
    return escudo.seguro(cfg.fase_inicial, _rojo_inicial(cfg))


def rojo_max_minimo(cfg, tope=1000):
    """Menor rojo máximo factible con los verdes mínimos, ámbar y despeje de cfg."""
    for r in range(cfg.transicion, tope + 1):
        try:
            if _factible(cfg, r):
                return r
        except ValueError:                            # región demasiado grande
            return None
    return None


def comprobar_factibilidad(cfg):
    if not _factible(cfg, cfg.rojo_max, cfg.escudo):
        minimo = rojo_max_minimo(cfg)
        detalle = f"al menos {minimo} s" if minimo is not None else "mucho mayor"
        raise ValueError(f"config no factible: con estos verdes mínimos, ámbar y despeje, "
                         f"el rojo máximo debe ser {detalle} (ahora {cfg.rojo_max} s)")


# =====================================================================
# 3. CONTROLADOR
# =====================================================================
class Controlador:
    """Máquina de estados del semáforo con paso de 1 s (decisiones.md, sección 3).

    Etapas: "transicion" (ámbar + despeje; también el arranque, todo en rojo) y "verde".
    Se decide solo en verde: al cumplirse el verde mínimo y luego cada Δ s, o antes si se
    llega al verde máximo o si hace falta para no superar el rojo máximo de alguna calle.

    Transición (5 s, siempre que cambia el conjunto de calles en verde): las calles que
    pierden el verde ven 3 s de ámbar y 2 s de rojo; las que lo mantienen siguen en verde;
    las que lo ganan esperan los 5 s. El verde mínimo de la nueva fase cuenta desde el final
    de la transición. El rojo de una calle se cuenta desde que termina su ámbar.
    """

    def __init__(self, cfg):
        self.cfg = cfg
        self._escudo = cfg.escudo
        self._bits = [f.bits for f in cfg.fases]
        self.reiniciar()

    def reiniciar(self):
        """Arranque: una transición todo en rojo y luego la fase inicial."""
        c = self.cfg
        self.t = 0
        self.fase = c.fase_inicial          # durante una transición: la fase que viene
        self.etapa = "transicion"
        self.t_etapa = 0
        self.t_fase = 0                     # segundos de verde de la fase actual
        self._bits_previos = 0              # verdes antes de la transición (0 al arrancar)
        self._inicio_rojo = [0] * c.n_calles
        self._t_decision = c.transicion + c.fases[self.fase].verde_min
        self._mascara = None
        self._s_quedarse = 0
        self.pedida = None                  # última acción pedida
        self.ejecutada = None               # última acción ejecutada (va a la memoria)
        self.forzada = False                # True si el controlador anuló la pedida

    def luces(self):
        """Luz de cada calle durante el segundo actual [t, t+1): 'V', 'A' o 'R'."""
        n = self.cfg.n_calles
        nuevos = self._bits[self.fase]
        if self.etapa == "verde":
            return [VERDE if (nuevos >> k) & 1 else ROJO for k in range(n)]
        previos = self._bits_previos
        en_ambar = self.t_etapa < self.cfg.ambar
        out = []
        for k in range(n):
            if (previos >> k) & 1 and (nuevos >> k) & 1:
                out.append(VERDE)                         # mantiene el verde
            elif (previos >> k) & 1:
                out.append(AMBAR if en_ambar else ROJO)   # pierde el verde
            else:
                out.append(ROJO)                          # gana el verde o sigue en rojo
        return out

    def rojo(self):
        """Segundos que cada calle lleva en rojo (0 si está en verde o en ámbar)."""
        return [0 if i is None else self.t - i for i in self._inicio_rojo]

    def esperando_decision(self):
        return self.etapa == "verde" and self.t == self._t_decision

    def _exigir_decision(self):
        if not self.esperando_decision():
            raise RuntimeError("no es un instante de decisión (usa esperando_decision())")

    def mascara(self):
        """Acciones permitidas ahora (una por fase). Solo en instantes de decisión."""
        self._exigir_decision()
        if self._mascara is None:
            self._mascara, self._s_quedarse = self._escudo.mascara(self.fase, self.t_fase,
                                                                   tuple(self.rojo()))
            if not any(self._mascara):
                raise RuntimeError("máscara vacía: no debería ocurrir (revisa la configuración)")
        return list(self._mascara)

    def accion_emergencia(self):
        """Cambio permitido que atiende primero a la calle con más tiempo en rojo."""
        m = self.mascara()
        q = self._escudo.emergencia(self.fase, self.rojo(), m)
        if q is None:
            raise RuntimeError("no hay cambio permitido: no debería ocurrir")
        return q

    def decidir(self, accion):
        """Aplica la acción pedida y devuelve (acción ejecutada, duración del paso en s).

        Si la acción no está permitida: un cambio prohibido se reemplaza por quedarse (si
        se puede); si quedarse tampoco se puede, se ejecuta el cambio de emergencia.
        """
        m = self.mascara()
        try:
            a = int(accion)
        except (TypeError, ValueError):
            raise ValueError(f"acción no válida: {accion!r}") from None
        if a != accion or not 0 <= a < self.cfg.n_fases:
            raise ValueError(f"acción no válida: {accion!r} (debe ser 0..{self.cfg.n_fases - 1})")
        self.pedida = a
        if not m[a]:
            a = self.fase if (a != self.fase and m[self.fase]) else self.accion_emergencia()
        self.ejecutada = a
        self.forzada = a != self.pedida
        if a == self.fase:
            duracion = self._s_quedarse
        else:
            self._bits_previos = self._bits[self.fase]
            self.fase = a
            self.etapa = "transicion"
            self.t_etapa = 0
            self.t_fase = 0
            duracion = self.cfg.transicion + self.cfg.fases[a].verde_min
        self._t_decision = self.t + duracion
        self._mascara = None
        return a, duracion

    def avanzar(self):
        """Pasa 1 s y devuelve las luces del nuevo segundo."""
        if self.esperando_decision():
            raise RuntimeError("falta decidir: llama a decidir() antes de avanzar()")
        antes = self.luces()
        self.t += 1
        if self.etapa == "transicion":
            self.t_etapa += 1
            if self.t_etapa >= self.cfg.transicion:
                self.etapa = "verde"
                self.t_fase = 0
        else:
            self.t_fase += 1
        despues = self.luces()
        for k in range(self.cfg.n_calles):
            if despues[k] != ROJO:
                self._inicio_rojo[k] = None
            elif antes[k] != ROJO:
                self._inicio_rojo[k] = self.t
        self._mascara = None
        return despues

    def resumen(self):
        """Estado legible, para registros."""
        return {"t": self.t, "fase": self.cfg.fases[self.fase].id, "etapa": self.etapa,
                "t_fase": self.t_fase, "rojo": self.rojo(), "luces": "".join(self.luces())}


# =====================================================================
# 4. OBSERVADOR
# =====================================================================
class Observador:
    """Estado de la red a partir de los conteos por calle (decisiones.md, sección 5).

    D_k (espera acumulada estimada) sigue el Algoritmo 1 de Raeis y Leon-Garcia (2021),
    adaptado: paso de 1 s -> intervalo medido entre frames (D_k += q_k * dt); "rojo" por
    calle; durante el ámbar D_k ni se acumula ni se reinicia. D_k vuelve a 0 cuando la
    calle recibe verde. Entre dos frames se mantienen el último conteo y la última luz:
    si se pierde un frame, el siguiente cubre todo el intervalo.

    Estado (sin recortes, para que nada sature):
        cola_k   = q_k / cap_cola
        espera_k = D_k / max(q_k, 1) / rojo_max
        rojo_k   = segundos en rojo / rojo_max     (exacto, del controlador)
        fase     = one-hot de la fase actual
        t_fase   = segundos de verde de la fase / rojo_max
    """

    def __init__(self, cfg):
        self.cfg = cfg
        self.reiniciar()

    def reiniciar(self):
        n = self.cfg.n_calles
        self.D = [0.0] * n
        self.q = [0.0] * n
        self._luces = [ROJO] * n
        self.t_ultimo = None
        self.frames = 0

    def actualizar(self, t, conteos, luces):
        """Registra un frame.

        t        marca de tiempo del frame en segundos (creciente; p. ej. time.monotonic()).
        conteos  vehículos detectados por calle dentro del alcance (orden de config.calles).
        luces    luz de cada calle en el instante t (Controlador.luces()).
        """
        n = self.cfg.n_calles
        t = float(t)
        conteos = [float(x) for x in conteos]
        luces = list(luces)
        if not math.isfinite(t):
            raise ValueError("marca de tiempo no válida")
        if len(conteos) != n or any(not math.isfinite(x) or x < 0 for x in conteos):
            raise ValueError(f"conteos no válidos: se esperan {n} números >= 0, llegó {conteos}")
        if len(luces) != n or any(l not in (VERDE, AMBAR, ROJO) for l in luces):
            raise ValueError(f"luces no válidas: {luces}")
        if self.t_ultimo is not None:
            dt = t - self.t_ultimo
            if dt <= 0:
                raise ValueError(f"marca de tiempo no creciente ({t} <= {self.t_ultimo})")
            for k in range(n):
                if self._luces[k] == ROJO:
                    self.D[k] += self.q[k] * dt
        for k in range(n):
            if luces[k] == VERDE:
                self.D[k] = 0.0
        self.q = conteos
        self._luces = luces
        self.t_ultimo = t
        self.frames += 1

    def estado(self, ctrl):
        """Vector de entrada de la red (float32, orden de cfg.nombres_variables)."""
        c = self.cfg
        n, R = c.n_calles, float(c.rojo_max)
        q = np.array(self.q, dtype=np.float64)
        x = np.zeros(c.n_variables, dtype=np.float32)
        x[0:n] = q / c.cap_cola
        x[n:2 * n] = np.array(self.D, dtype=np.float64) / np.maximum(q, 1.0) / R
        x[2 * n:3 * n] = np.array(ctrl.rojo(), dtype=np.float64) / R
        x[3 * n + ctrl.fase] = 1.0
        x[-1] = ctrl.t_fase / R
        return x


# =====================================================================
# 5. POLITICA (inferencia en NumPy)
# =====================================================================
def _validar_capas(pesos, sesgos, cfg):
    if not pesos or len(pesos) != len(sesgos):
        raise ValueError("el modelo debe tener al menos una capa y un sesgo por capa")
    entrada = cfg.n_variables
    for i, (W, b) in enumerate(zip(pesos, sesgos)):
        if W.ndim != 2 or b.ndim != 1 or W.shape[0] != b.shape[0]:
            raise ValueError(f"capa {i}: formas no válidas {W.shape} y {b.shape}")
        if W.shape[1] != entrada:
            raise ValueError(f"capa {i}: espera {W.shape[1]} entradas y recibe {entrada}")
        if not (np.all(np.isfinite(W)) and np.all(np.isfinite(b))):
            raise ValueError(f"capa {i}: tiene valores no finitos")
        entrada = W.shape[0]
    if entrada != cfg.n_fases:
        raise ValueError(f"la última capa da {entrada} salidas; deben ser {cfg.n_fases} (una por fase)")


def guardar_modelo_npz(ruta, pesos, sesgos, cfg):
    """Escribe modelo.npz. pesos[i]: forma (salidas, entradas), como nn.Linear.weight.

    Exportación desde PyTorch (entrenamiento):
        capas = [m for m in agente.online.red if isinstance(m, nn.Linear)]
        guardar_modelo_npz("modelo.npz", [m.weight.detach().cpu().numpy() for m in capas],
                           [m.bias.detach().cpu().numpy() for m in capas], cfg)
    """
    pesos = [np.asarray(W, dtype=np.float32) for W in pesos]
    sesgos = [np.asarray(b, dtype=np.float32) for b in sesgos]
    _validar_capas(pesos, sesgos, cfg)
    datos = {"formato": np.array(FORMATO_NPZ), "huella": np.array(cfg.huella),
             "variables": np.array(cfg.nombres_variables)}
    for i, (W, b) in enumerate(zip(pesos, sesgos)):
        datos[f"W{i}"] = W
        datos[f"b{i}"] = b
    ruta = Path(ruta)
    with open(ruta, "wb") as f:          # con archivo abierto, NumPy no cambia la extensión
        np.savez(f, **datos)
    return ruta


class Politica:
    """Red online (Q por fase) en NumPy. Solo inferencia: sin red objetivo, memoria ni
    exploración (decisiones.md, sección 2)."""

    def __init__(self, cfg, ruta=None):
        self.cfg = cfg
        ruta = Path(ruta) if ruta is not None else cfg.ruta_modelo
        if not ruta.exists():
            raise FileNotFoundError(f"No encuentro el modelo {ruta}")
        with np.load(str(ruta), allow_pickle=False) as d:
            claves = set(d.files)
            for k in ("formato", "huella", "variables"):
                if k not in claves:
                    raise ValueError(f"{ruta.name}: falta '{k}' (no es un modelo de semaforo.py)")
            if str(d["formato"]) != FORMATO_NPZ:
                raise ValueError(f"{ruta.name}: formato {d['formato']}, se esperaba {FORMATO_NPZ}")
            if str(d["huella"]) != cfg.huella:
                raise ValueError(f"{ruta.name} se entrenó con otra configuración (huella "
                                 f"{d['huella']}; config.json en uso: {cfg.huella}). Usa el "
                                 f"config.json que viaja con el modelo o reentrena.")
            if [str(v) for v in d["variables"]] != cfg.nombres_variables:
                raise ValueError(f"{ruta.name}: las variables de entrada no coinciden")
            pesos, sesgos, i = [], [], 0
            while f"W{i}" in claves:
                if f"b{i}" not in claves:
                    raise ValueError(f"{ruta.name}: falta b{i}")
                pesos.append(np.array(d[f"W{i}"], dtype=np.float64))
                sesgos.append(np.array(d[f"b{i}"], dtype=np.float64))
                i += 1
        _validar_capas(pesos, sesgos, cfg)
        self.pesos, self.sesgos = pesos, sesgos

    def valores_q(self, estado):
        h = np.asarray(estado, dtype=np.float64).reshape(-1)
        if h.shape[0] != self.cfg.n_variables:
            raise ValueError(f"el estado tiene {h.shape[0]} valores; se esperan {self.cfg.n_variables}")
        ultima = len(self.pesos) - 1
        for i, (W, b) in enumerate(zip(self.pesos, self.sesgos)):
            h = W.dot(h) + b
            if i < ultima:
                h = np.maximum(h, 0.0)
        return h

    def elegir(self, estado, mascara):
        """Acción permitida con mayor Q (en empate, la de menor índice)."""
        m = np.asarray(mascara, dtype=bool).reshape(-1)
        if m.shape[0] != self.cfg.n_fases:
            raise ValueError(f"la máscara debe tener {self.cfg.n_fases} valores")
        permitidas = np.flatnonzero(m)
        if permitidas.size == 0:
            raise ValueError("máscara vacía")
        if permitidas.size == 1:
            return int(permitidas[0])
        q = self.valores_q(estado)
        return int(permitidas[np.argmax(q[permitidas])])


# =====================================================================
# 6. PRUEBAS  (python semaforo.py --probar)
# =====================================================================
class _Omitida(Exception):
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


def _patron(cfg, bits):
    return "".join(VERDE if (bits >> k) & 1 else ROJO for k in range(cfg.n_calles))


def _es_referencia(cfg):
    """True si calles y fases son las de decisiones.md (las pruebas con valores exactos lo suponen)."""
    fases = [(tuple(cfg.calles[k] for k in f.verde), f.verde_min, f.verde_max) for f in cfg.fases]
    return (cfg.calles == ["N", "S", "E", "O"] and cfg.ambar == 3 and cfg.despeje == 2
            and cfg.fase_inicial == 0
            and fases == [(("N", "S"), 15, None), (("E", "O"), 15, None), (("N",), 10, 15),
                          (("S",), 10, 15), (("E",), 10, 15), (("O",), 10, 15)])


def _referencia(ctx):
    if not _es_referencia(ctx["cfg"]):
        raise _Omitida("config.json no tiene las calles y fases de decisiones.md")
    return ctx["cfg"].variante({"controlador": {"rojo_max_s": 60, "intervalo_decision_s": 10}})


def _avanzar_hasta_decision(ctrl, luces=None):
    while not ctrl.esperando_decision():
        if luces is not None:
            luces.append("".join(ctrl.luces()))
        ctrl.avanzar()


def _simular(cfg, agente, segundos):
    """Ejecuta el controlador; devuelve las luces de cada segundo y las decisiones."""
    ctrl = Controlador(cfg)
    luces, decisiones = [], []
    while ctrl.t < segundos:
        if ctrl.esperando_decision():
            t0 = time.perf_counter()
            m = ctrl.mascara()
            ms = 1000 * (time.perf_counter() - t0)
            d = {"t": ctrl.t, "fase": ctrl.fase, "t_fase": ctrl.t_fase,
                 "rojo": tuple(ctrl.rojo()), "mascara": m, "ms": ms}
            d["pedida"] = int(agente(ctrl, m))
            d["ejecutada"], d["dur"] = ctrl.decidir(d["pedida"])
            decisiones.append(d)
        luces.append("".join(ctrl.luces()))
        ctrl.avanzar()
    return luces, decisiones


def _verificar(cfg, luces, decisiones):
    """Comprueba las reglas mirando solo las luces de cada segundo y las decisiones."""
    n, A, T, R = cfg.n_calles, cfg.ambar, cfg.transicion, cfg.rojo_max
    F, p0 = cfg.fases, cfg.fase_inicial
    prohibidas = {(VERDE, ROJO): "verde -> rojo sin ámbar", (AMBAR, VERDE): "ámbar -> verde",
                  (ROJO, AMBAR): "rojo -> ámbar"}
    for t in range(T):
        _comprobar(luces[t] == ROJO * n, f"arranque: t={t} luces {luces[t]}")
    _comprobar(luces[T] == _patron(cfg, F[p0].bits), "arranque: no empieza la fase inicial")

    rachas = []                              # rachas[k][t] = segundos seguidos en rojo antes de t
    for k in range(n):
        previa, largo = None, 0
        racha = [0] * (len(luces) + 1)
        for t, l in enumerate(luces):
            x = l[k]
            racha[t + 1] = racha[t] + 1 if x == ROJO else 0
            if previa is not None and (previa, x) in prohibidas:
                raise AssertionError(f"calle {cfg.calles[k]}, t={t}: {prohibidas[(previa, x)]}")
            if x == previa:
                largo += 1
                continue
            if previa == AMBAR:
                _comprobar(largo == A, f"calle {cfg.calles[k]}: ámbar de {largo} s (t={t})")
            previa, largo = x, 1
        _comprobar(max(racha) <= R, f"calle {cfg.calles[k]}: {max(racha)} s en rojo > {R} s")
        rachas.append(racha)

    # El modelo del escudo debe predecir exactamente el rojo real de la decisión siguiente.
    esc, prediccion = cfg.escudo, _rojo_inicial(cfg)
    fase, inicio_verde, esperado = p0, T, T + F[p0].verde_min
    for d in decisiones:
        t0, q, dur, m = d["t"], d["ejecutada"], d["dur"], d["mascara"]
        _comprobar(t0 == esperado, f"decisión en t={t0}; se esperaba en t={esperado}")
        _comprobar(d["rojo"] == prediccion,
                   f"t={t0}: el escudo predijo rojo {prediccion}; real {d['rojo']}")
        _comprobar(d["fase"] == fase, f"t={t0}: fase inesperada")
        _comprobar(any(m), f"t={t0}: máscara vacía")
        _comprobar(m[q], f"t={t0}: se ejecutó una acción no permitida ({q})")
        if m[d["pedida"]]:
            _comprobar(q == d["pedida"], f"t={t0}: se anuló una acción permitida")
        verde = t0 - inicio_verde
        _comprobar(verde == d["t_fase"], f"t={t0}: t_fase {d['t_fase']} != {verde}")
        _comprobar(verde >= F[fase].verde_min, f"t={t0}: decisión antes del verde mínimo")
        for k in range(n):
            _comprobar(d["rojo"][k] == rachas[k][t0],
                       f"t={t0}: rojo de {cfg.calles[k]} = {d['rojo'][k]}; real {rachas[k][t0]}")
        vmax = F[fase].verde_max
        if q == fase:
            prediccion = esc.tras_quedarse(fase, d["rojo"], dur)
            _comprobar(1 <= dur <= cfg.delta, f"t={t0}: paso de {dur} s al quedarse")
            _comprobar(vmax is None or verde + dur <= vmax, f"t={t0}: supera el verde máximo")
            tramo = [(t, _patron(cfg, F[fase].bits)) for t in range(t0, t0 + dur)]
        else:
            prediccion = esc.tras_cambio(fase, q, d["rojo"])
            _comprobar(vmax is None or verde <= vmax, f"t={t0}: supera el verde máximo")
            _comprobar(dur == T + F[q].verde_min, f"t={t0}: paso de {dur} s al cambiar")
            tramo = []
            for u in range(T):
                s = ""
                for k in range(n):
                    b = 1 << k
                    if F[fase].bits & b and F[q].bits & b:
                        s += VERDE
                    elif F[fase].bits & b:
                        s += AMBAR if u < A else ROJO
                    else:
                        s += ROJO
                tramo.append((t0 + u, s))
            tramo += [(t, _patron(cfg, F[q].bits)) for t in range(t0 + T, t0 + dur)]
            fase, inicio_verde = q, t0 + T
        for t, s in tramo:
            if t < len(luces):
                _comprobar(luces[t] == s, f"t={t}: luces {luces[t]}; se esperaban {s}")
        esperado = t0 + dur


def _agentes(cfg, semilla):
    rng = np.random.default_rng(semilla)
    nf = cfg.n_fases
    una = [f.indice for f in cfg.fases if len(f.verde) == 1] or list(range(nf))

    def aleatorio(ctrl, m):          # pide cualquier fase, también las prohibidas
        return rng.integers(nf)

    def permitido(ctrl, m):          # como la exploración epsilon-greedy con máscara
        return rng.choice(np.flatnonzero(m))

    def quedarse(ctrl, m):           # nunca quiere cambiar
        return ctrl.fase

    def una_calle(ctrl, m):          # solo fases de una calle
        return rng.choice(una)

    def hambre(ctrl, m):             # adversario: retrasa a las calles con más rojo
        if m[ctrl.fase]:
            return ctrl.fase
        rojo = ctrl.rojo()
        return min(np.flatnonzero(m), key=lambda q: max(rojo[k] for k in cfg.fases[q].verde))

    return [("aleatorio", aleatorio), ("permitido", permitido), ("quedarse", quedarse),
            ("una_calle", una_calle), ("hambre", hambre)]


def _modelo_aleatorio(cfg, ruta, semilla):
    rng = np.random.default_rng(semilla)
    capas = [cfg.n_variables, 32, 32, cfg.n_fases]
    pesos = [rng.normal(0, 0.5, (capas[i + 1], capas[i])) for i in range(3)]
    sesgos = [rng.normal(0, 0.1, capas[i + 1]) for i in range(3)]
    guardar_modelo_npz(ruta, pesos, sesgos, cfg)
    return pesos, sesgos


def _plan_explicito(esc, p, rojo, prof, memo):
    """Oráculo independiente de la región: ¿hay un plan explícito de a lo sumo `prof`
    cambios (cada fase sostenida su verde mínimo) que termine alternando dos fases que
    cubren todas las calles sin superar el tope?"""
    clave = (p, rojo, prof)
    if clave in memo:
        return memo[clave]
    res = False
    if all(rojo[k] <= esc.lim for k in esc.rojas[p]):
        for q in range(esc.nf):
            cubre = q != p and all((esc.bits[q] >> k) & 1 for k in esc.rojas[p])
            ida = not esc.bits[p] & ~esc.bits[q] or esc.C + esc.vmin[q] <= esc.lim
            vuelta = not esc.bits[q] & ~esc.bits[p] or esc.C + esc.vmin[p] <= esc.lim
            if cubre and ida and vuelta:
                res = True
                break
        if not res and prof > 0:
            for q in range(esc.nf):
                if q != p:
                    nuevo = esc.tras_cambio(p, q, rojo)
                    if nuevo is not None and _plan_explicito(esc, q, nuevo, prof - 1, memo):
                        res = True
                        break
    memo[clave] = res
    return res


def prueba_configuracion(ctx):
    cfg = ctx["cfg"]
    _comprobar(cfg.transicion == cfg.ambar + cfg.despeje, "transición != ámbar + despeje")
    _comprobar(cfg.n_variables == 3 * cfg.n_calles + cfg.n_fases + 1, "número de variables")
    h = cfg.huella
    _comprobar(cfg.variante({"_nota": "x", "entrenamiento": {"alpha": 0.5}}).huella == h,
               "las notas o la sección de entrenamiento cambian la huella")
    fases = copy.deepcopy(cfg.datos["fases"])
    fases[0]["nombre"] = "otro nombre"
    _comprobar(cfg.variante({"fases": fases}).huella == h, "el nombre de una fase cambia la huella")
    for cambios in ({"controlador": {"rojo_max_s": cfg.rojo_max + 30}},
                    {"controlador": {"intervalo_decision_s": cfg.delta + 5}},
                    {"observador": {"cap_cola_veh": cfg.cap_cola + 1}},
                    {"observador": {"alcance_camara_m": cfg.alcance + 10}},
                    {"observador": {"intervalo_frames_s": cfg.intervalo_frames * 2}}):
        _comprobar(cfg.variante(cambios).huella != h, f"{cambios} no cambia la huella")
    return f"huella {h}"


def prueba_factibilidad(ctx):
    cfg = ctx["cfg"]
    minimo = rojo_max_minimo(cfg)
    _comprobar(minimo is not None and minimo <= cfg.rojo_max, "config.json no es factible")
    if _es_referencia(cfg):
        # E y O esperan 5 s de arranque + 15 s de verde mínimo + 5 s de transición = 25 s.
        _comprobar(minimo == 25, f"rojo máximo mínimo = {minimo} s; se esperaban 25 s")
    for r in (minimo, 60, 90):
        cfg.variante({"controlador": {"rojo_max_s": r}})
    _debe_fallar(lambda: cfg.variante({"controlador": {"rojo_max_s": minimo - 1}}),
                 ValueError, str(minimo))

    def fases_con(i, **campos):
        f = copy.deepcopy(cfg.datos["fases"])
        f[i].update(campos)
        return {"fases": f}

    calles = cfg.calles
    malas = [
        {"controlador": {"intervalo_decision_s": 0}},
        {"controlador": {"ambar_s": 0}},
        {"controlador": {"fase_inicial": cfg.n_fases + 1}},
        {"controlador": {"rojo_max_s": "60"}},
        {"observador": {"cap_cola_veh": 0}},
        fases_con(0, verde_max_s=cfg.fases[0].verde_min - 1),
        fases_con(1, verde=list(cfg.datos["fases"][0]["verde"])),
        fases_con(0, verde=list(calles)),
        fases_con(0, verde=["X"]),
        fases_con(0, id=2),
        {"fases": [{"id": 1, "verde": [calles[0]], "verde_min_s": 10, "verde_max_s": None},
                   {"id": 2, "verde": [calles[1]], "verde_min_s": 10, "verde_max_s": None}]},
    ]
    for cambios in malas:
        _debe_fallar(lambda: cfg.variante(cambios), ValueError)
    return f"rojo máximo mínimo factible = {minimo} s; {len(malas)} configuraciones inválidas rechazadas"


def prueba_region_exacta(ctx):
    """La región segura coincide con un oráculo independiente (planes explícitos) en todos
    los estados posibles, y con verdes mínimos asimétricos no restringe de más."""
    cfg = _referencia(ctx)
    total = 0
    for r in (25, 30):
        esc = cfg.variante({"controlador": {"rojo_max_s": r}}).escudo
        memo = {}
        for p in range(esc.nf):
            for punto in itertools.product(range(esc.lim + 1), repeat=len(esc.rojas[p])):
                rojo = [0] * esc.n
                for i, k in enumerate(esc.rojas[p]):
                    rojo[k] = punto[i]
                rojo = tuple(rojo)
                _comprobar(esc.seguro(p, rojo) == _plan_explicito(esc, p, rojo, 6, memo),
                           f"rojo_max={r}: región y oráculo difieren en fase {p + 1}, rojo {rojo}")
                total += 1
    # Asimétrica (verde mínimo N+S = 5 s, E+O = 30 s): alternar N+S <-> E+O exige 37 s,
    # pero el ciclo N+S -> solo E -> solo O cumple con 32 s (rojo de N y S: 2+10+5+10+5).
    fases = copy.deepcopy(cfg.datos["fases"])
    fases[0]["verde_min_s"], fases[1]["verde_min_s"] = 5, 30
    asim = cfg.variante({"fases": fases, "controlador": {"rojo_max_s": 32}})
    _comprobar(rojo_max_minimo(asim) == 32, f"asimétrica: mínimo {rojo_max_minimo(asim)} s; se esperaban 32 s")
    ciclo = {0: 4, 4: 5, 5: 0}
    luces, decs = _simular(asim, lambda ctrl, m: ciclo.get(ctrl.fase, 0), ctx["segundos"])
    _verificar(asim, luces, decs)
    _comprobar(all(d["ejecutada"] == d["pedida"] for d in decs),
               "asimétrica: el escudo prohibió el ciclo N+S -> E -> O, que es seguro")
    return f"{total} estados iguales al oráculo; asimétrica: mínimo exacto 32 s y ciclo permitido"


def prueba_transiciones(ctx):
    cfg = _referencia(ctx)
    NS, EO, N = 0, 1, 2
    ctrl, luces = Controlador(cfg), []
    _avanzar_hasta_decision(ctrl, luces)
    _comprobar(ctrl.t == 20 and ctrl.rojo() == [0, 0, 20, 20], f"primera decisión: {ctrl.resumen()}")
    _comprobar(ctrl.decidir(N) == (N, 15), "N+S -> solo N")
    _avanzar_hasta_decision(ctrl, luces)
    _comprobar(ctrl.t == 35 and ctrl.rojo() == [0, 12, 35, 35], f"t=35: {ctrl.resumen()}")
    _comprobar(ctrl.decidir(NS) == (NS, 20), "solo N -> N+S")
    _avanzar_hasta_decision(ctrl, luces)
    m = ctrl.mascara()
    _comprobar(ctrl.t == 55 and ctrl.rojo() == [0, 0, 55, 55], f"t=55: {ctrl.resumen()}")
    _comprobar(m == [False, True, False, False, False, False],
               f"en t=55 solo debe permitirse E+O (anticipación del rojo máximo): {m}")
    _comprobar(ctrl.decidir(EO) == (EO, 20), "N+S -> E+O")
    _avanzar_hasta_decision(ctrl, luces)
    esperado = (["RRRR"] * 5 + ["VVRR"] * 15                    # arranque y fase 1
                + ["VARR"] * 3 + ["VRRR"] * 2 + ["VRRR"] * 10   # a solo N: el Sur ve ámbar
                + ["VRRR"] * 5 + ["VVRR"] * 15                  # a N+S: sin ámbar; el Sur espera 5 s
                + ["AARR"] * 3 + ["RRRR"] * 2 + ["RRVV"] * 15)  # a E+O
    _comprobar(luces == esperado, "la secuencia de luces no es la esperada")
    _comprobar(ctrl.rojo() == [17, 17, 0, 0], f"t=75: {ctrl.resumen()}")
    return "arranque, solo pierde, solo gana y cambio completo; E y O reciben verde a los 60 s justos"


def prueba_verde_maximo(ctx):
    cfg = _referencia(ctx)
    EO, N = 1, 2
    ctrl = Controlador(cfg)
    _avanzar_hasta_decision(ctrl)
    ctrl.decidir(N)
    _avanzar_hasta_decision(ctrl)                       # t=35: 10 s de verde en solo N
    _comprobar(ctrl.decidir(N) == (N, 5), "quedarse debe cortarse en el verde máximo (15 s)")
    _avanzar_hasta_decision(ctrl)                       # t=40: 15 s de verde
    m = ctrl.mascara()
    _comprobar(ctrl.t_fase == 15 and not m[N], "con el verde máximo cumplido no se puede seguir")
    a, _ = ctrl.decidir(N)
    _comprobar(a == EO and ctrl.forzada and m[a], f"se esperaba cambio forzado a E+O; se ejecutó {a}")
    return "solo N: paso de 5 s hasta 15 s de verde y luego cambio forzado"


def prueba_rojo_maximo(ctx):
    cfg = _referencia(ctx)
    for delta, pasos in ((10, [10, 10, 10, 5, 20]), (20, [20, 15, 20])):
        c = cfg.variante({"controlador": {"intervalo_decision_s": delta}})
        luces, decs = _simular(c, lambda ctrl, m: ctrl.fase, 80)
        _verificar(c, luces, decs)
        durs = [d["dur"] for d in decs[:len(pasos)]]
        _comprobar(durs == pasos, f"Δ={delta}: pasos {durs}; se esperaban {pasos}")
        d = decs[len(pasos) - 1]
        _comprobar(d["t"] == 55 and d["pedida"] == 0 and d["ejecutada"] == 1,
                   f"Δ={delta}: no se forzó E+O en t=55")
        _comprobar(luces[59] == "RRRR" and luces[60] == "RRVV", f"Δ={delta}: E y O no reciben verde a los 60 s")
    return "agente que nunca cambia: E+O forzada a tiempo con Δ = 10 y 20"


def prueba_estres(ctx):
    cfg = ctx["cfg"]
    minimo = rojo_max_minimo(cfg)
    variantes = [{"controlador": {"rojo_max_s": r, "intervalo_decision_s": d}}
                 for r in (60, 90) for d in (5, 10, 15, 20)]
    variantes += [{"controlador": {"rojo_max_s": minimo}},
                  {"controlador": {"rojo_max_s": minimo + 5}},
                  {"controlador": {"fase_inicial": min(3, cfg.n_fases)}}]
    fases = copy.deepcopy(cfg.datos["fases"])
    for f in fases:
        if f.get("verde_max_s") is not None:
            f["verde_max_s"] = max(25, f["verde_min_s"])
    variantes.append({"fases": fases})
    ctx["tiempos_ms"] = []
    n_dec, forzadas, semilla = 0, 0, 0
    for cambios in variantes:
        c = cfg.variante(cambios)
        for nombre, agente in _agentes(c, semilla):
            semilla += 1
            luces, decs = _simular(c, agente, ctx["segundos"])
            try:
                _verificar(c, luces, decs)
            except AssertionError as e:
                raise AssertionError(f"[rojo_max={c.rojo_max} s, Δ={c.delta} s, agente {nombre}] {e}")
            n_dec += len(decs)
            forzadas += sum(d["ejecutada"] != d["pedida"] for d in decs)
            ctx["tiempos_ms"] += [d["ms"] for d in decs]
    return (f"{len(variantes)} configuraciones x 5 agentes x {ctx['segundos']} s: "
            f"{n_dec} decisiones ({forzadas} anuladas por el controlador), sin violaciones")


def prueba_observador(ctx):
    cfg = ctx["cfg"]
    n, R, cap = cfg.n_calles, cfg.rojo_max, cfg.cap_cola

    def luz(x):
        return [x] + [ROJO] * (n - 1)

    def conteo(q):
        return [q] + [0] * (n - 1)

    obs = Observador(cfg)
    # verde -> ámbar (no acumula) -> rojo (acumula q*dt con el conteo anterior) -> frames perdidos
    pasos = [(0.0, 2, VERDE, 0), (1.0, 2, AMBAR, 0), (4.0, 3, ROJO, 0), (5.0, 3, ROJO, 3),
             (6.0, 4, ROJO, 6), (9.0, 4, ROJO, 18)]
    for t, q, l, d in pasos:
        obs.actualizar(t, conteo(q), luz(l))
        _comprobar(abs(obs.D[0] - d) < 1e-9, f"t={t}: D={obs.D[0]}; se esperaba {d}")
    _comprobar(obs.D[1:] == [0.0] * (n - 1), "las calles sin autos acumularon espera")

    ctrl = Controlador(cfg)
    _avanzar_hasta_decision(ctrl)
    x = obs.estado(ctrl)
    _comprobar(x.shape == (cfg.n_variables,) and x.dtype == np.float32, "forma del estado")
    _comprobar(abs(x[0] - 4 / cap) < 1e-6, "cola normalizada")
    _comprobar(abs(x[n] - 18 / 4 / R) < 1e-6, "espera media normalizada")
    _comprobar(np.allclose(x[2 * n:3 * n], np.array(ctrl.rojo()) / R), "rojo normalizado")
    _comprobar(x[3 * n + cfg.fase_inicial] == 1 and x[3 * n:3 * n + cfg.n_fases].sum() == 1, "one-hot")
    _comprobar(abs(x[-1] - ctrl.t_fase / R) < 1e-6, "tiempo en la fase")

    obs.actualizar(10.0, conteo(4), luz(VERDE))
    _comprobar(obs.D[0] == 0.0, "el verde no reinició D")
    obs.actualizar(11.0, conteo(1.5 * cap), luz(ROJO))
    obs.actualizar(11.0 + 2 * R, conteo(0), luz(ROJO))
    x = obs.estado(ctrl)
    _comprobar(abs(x[0]) < 1e-9, "cola vacía")
    _comprobar(abs(x[n] - 1.5 * cap * 2 * R / 1 / R) < 1e-3, "espera con q=0 debe usar q mínimo de 1")
    obs.actualizar(12.0 + 2 * R, conteo(1.5 * cap), luz(ROJO))
    x = obs.estado(ctrl)
    _comprobar(x[0] > 1 and x[n] > 1, "la normalización no debe recortar en 1")
    return "Algoritmo 1 por frames (ámbar, rojo, verde, frames perdidos) y estado sin saturación"


def prueba_politica(ctx):
    cfg = ctx["cfg"]
    rng = np.random.default_rng(3)
    with tempfile.TemporaryDirectory() as carpeta:
        ruta = Path(carpeta) / "modelo.npz"
        pesos, sesgos = _modelo_aleatorio(cfg, ruta, 3)
        pol = Politica(cfg, ruta)
        for _ in range(300):
            x = rng.random(cfg.n_variables) * 2
            h = x
            for i, (W, b) in enumerate(zip(pesos, sesgos)):
                h = W.astype(np.float32).astype(np.float64).dot(h) + b.astype(np.float32)
                if i < len(pesos) - 1:
                    h = np.maximum(h, 0.0)
            q = pol.valores_q(x)
            _comprobar(np.allclose(q, h, atol=1e-6), "la inferencia no coincide con el cálculo directo")
            m = rng.random(cfg.n_fases) < 0.5
            if not m.any():
                m[rng.integers(cfg.n_fases)] = True
            a = pol.elegir(x, m)
            _comprobar(m[a] and q[a] == q[m].max(), "no eligió la acción permitida con mayor Q")
        otra = cfg.variante({"controlador": {"rojo_max_s": cfg.rojo_max + 30}})
        _debe_fallar(lambda: Politica(otra, ruta), ValueError, "otra configuración")
        _debe_fallar(lambda: guardar_modelo_npz(Path(carpeta) / "x.npz", [pesos[0][:, :-1]] + pesos[1:],
                                                sesgos, cfg), ValueError)
        _debe_fallar(lambda: guardar_modelo_npz(Path(carpeta) / "x.npz", pesos[:-1], sesgos[:-1], cfg),
                     ValueError)
        with open(Path(carpeta) / "otro.npz", "wb") as f:
            np.savez(f, a=np.zeros(3))
        _debe_fallar(lambda: Politica(cfg, Path(carpeta) / "otro.npz"), ValueError)
        _debe_fallar(lambda: pol.valores_q(np.zeros(cfg.n_variables + 1)), ValueError)
        _debe_fallar(lambda: pol.elegir(np.zeros(cfg.n_variables), [False] * cfg.n_fases), ValueError)
    return "pesos aleatorios: inferencia, máscara, huella y formas"


def prueba_integracion(ctx):
    """Controlador + observador + política aleatoria con una cola sintética (no es un
    modelo de tráfico: solo comprueba que las piezas funcionan juntas)."""
    cfg = ctx["cfg"]
    n = cfg.n_calles
    with tempfile.TemporaryDirectory() as carpeta:
        ruta = Path(carpeta) / "modelo.npz"
        _modelo_aleatorio(cfg, ruta, 11)
        pol = Politica(cfg, ruta)
    rng = np.random.default_rng(11)
    llegada = ([0.12, 0.12, 0.06, 0.06] + [0.06] * n)[:n]
    ctrl, obs = Controlador(cfg), Observador(cfg)
    cola, luces, decs, suma = [0] * n, [], [], 0
    obs.actualizar(0.0, cola, ctrl.luces())
    while ctrl.t < ctx["segundos"]:
        if ctrl.esperando_decision():
            x = obs.estado(ctrl)
            _comprobar(np.all(np.isfinite(x)), "estado no finito")
            m = ctrl.mascara()
            d = {"t": ctrl.t, "fase": ctrl.fase, "t_fase": ctrl.t_fase, "rojo": tuple(ctrl.rojo()),
                 "mascara": m, "pedida": pol.elegir(x, m)}
            d["ejecutada"], d["dur"] = ctrl.decidir(d["pedida"])
            decs.append(d)
        l = ctrl.luces()
        luces.append("".join(l))
        for k in range(n):
            if rng.random() < llegada[k]:
                cola[k] += 1
            if l[k] != ROJO and cola[k] > 0 and rng.random() < 0.5:
                cola[k] -= 1
        ctrl.avanzar()
        obs.actualizar(float(ctrl.t), cola, ctrl.luces())
        suma += sum(cola)
    _verificar(cfg, luces, decs)
    _comprobar(all(d["ejecutada"] == d["pedida"] for d in decs), "la política eligió una acción prohibida")
    cambios = sum(d["ejecutada"] != d["fase"] for d in decs)
    return f"{len(decs)} decisiones, {cambios} cambios de fase, cola total media {suma / ctx['segundos']:.1f} veh"


def prueba_uso_incorrecto(ctx):
    cfg = ctx["cfg"]
    n = cfg.n_calles
    ctrl = Controlador(cfg)
    _debe_fallar(lambda: ctrl.decidir(0), RuntimeError)
    _debe_fallar(ctrl.mascara, RuntimeError)
    _avanzar_hasta_decision(ctrl)
    _debe_fallar(ctrl.avanzar, RuntimeError)
    _debe_fallar(lambda: ctrl.decidir(cfg.n_fases), ValueError)
    _debe_fallar(lambda: ctrl.decidir(1.5), ValueError)
    obs = Observador(cfg)
    _debe_fallar(lambda: obs.actualizar(0.0, [0] * (n - 1), [ROJO] * n), ValueError)
    _debe_fallar(lambda: obs.actualizar(0.0, [-1] + [0] * (n - 1), [ROJO] * n), ValueError)
    _debe_fallar(lambda: obs.actualizar(0.0, [0] * n, ["X"] * n), ValueError)
    obs.actualizar(1.0, [0] * n, [ROJO] * n)
    _debe_fallar(lambda: obs.actualizar(1.0, [0] * n, [ROJO] * n), ValueError)
    return "errores claros ante usos incorrectos"


def prueba_rendimiento(ctx):
    t = ctx.get("tiempos_ms")
    if not t:
        raise _Omitida("requiere la prueba de estrés")
    t = np.array(t)
    _comprobar(t.max() < 1000, f"una máscara tardó {t.max():.0f} ms")
    arranque = []
    for r in (60, 90):
        t0 = time.perf_counter()
        _Escudo(ctx["cfg"], rojo_max=r)
        arranque.append(f"{r} s: {time.perf_counter() - t0:.2f} s")
    return (f"máscara por decisión: media {t.mean():.3f} ms, máx. {t.max():.2f} ms; "
            f"región segura al arrancar ({', '.join(arranque)})")


def ejecutar_pruebas(ruta_config):
    print(f"Pruebas de semaforo.py (Python {sys.version.split()[0]}, NumPy {np.__version__})")
    try:
        cfg = cargar_config(ruta_config)
    except Exception as e:
        print(f"FALLO     cargar {ruta_config}: {e}")
        return 1
    print(f"Config: {Path(ruta_config).resolve()} | rojo máximo {cfg.rojo_max} s | "
          f"Δ {cfg.delta} s | huella {cfg.huella}\n")
    ctx = {"cfg": cfg, "segundos": 3600}
    pruebas = [
        ("configuración y huella", prueba_configuracion),
        ("factibilidad", prueba_factibilidad),
        ("región segura exacta", prueba_region_exacta),
        ("transiciones y ámbar", prueba_transiciones),
        ("verde máximo", prueba_verde_maximo),
        ("rojo máximo forzado", prueba_rojo_maximo),
        ("estrés (reglas en todas las configuraciones)", prueba_estres),
        ("observador", prueba_observador),
        ("política con pesos aleatorios", prueba_politica),
        ("integración", prueba_integracion),
        ("uso incorrecto", prueba_uso_incorrecto),
        ("rendimiento", prueba_rendimiento),
    ]
    fallos, omitidas, inicio = 0, 0, time.perf_counter()
    for nombre, prueba in pruebas:
        t0 = time.perf_counter()
        try:
            detalle = prueba(ctx)
            print(f"OK        {nombre} ({time.perf_counter() - t0:.1f} s): {detalle}")
        except _Omitida as e:
            omitidas += 1
            print(f"OMITIDA   {nombre}: {e}")
        except AssertionError as e:
            fallos += 1
            print(f"FALLO     {nombre}: {e}")
        except Exception:
            fallos += 1
            print(f"ERROR     {nombre}:")
            traceback.print_exc()
    total = len(pruebas)
    print(f"\n{total - fallos - omitidas}/{total} pruebas correctas, {omitidas} omitidas, "
          f"{fallos} con fallos ({time.perf_counter() - inicio:.0f} s)")
    return 0 if fallos == 0 else 1


# =====================================================================
# 7. PUNTO DE ENTRADA
# =====================================================================
def main():
    ap = argparse.ArgumentParser(description="Semáforo DDQN para la Raspberry Pi: "
                                             "controlador, observador y política.")
    ap.add_argument("--probar", action="store_true", help="ejecutar las pruebas")
    ap.add_argument("--config", default=str(DIR_BASE / "config.json"), help="ruta de config.json")
    args = ap.parse_args()
    if args.probar:
        sys.exit(ejecutar_pruebas(args.config))
    ap.print_help()


if __name__ == "__main__":
    main()
