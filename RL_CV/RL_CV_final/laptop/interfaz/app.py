# =============================================================================
# app.py  -  Programa de escritorio "Monitor de tráfico" (laptop).
#
# Muestra lo que manda la Raspberry Pi por WebSocket: video de las 4 cámaras con las
# cajas de YOLO, conteos en vivo, historial con gráficas y la configuración de conexión.
# Toda la parte de red/datos está en nucleo.py; esta ventana solo usa su API
# (ver API_NUCLEO.md) consultándola con temporizadores (QTimer).
#
# Uso:
#     python app.py                         # escucha en el puerto 8000, token "cambia-esto"
#     python app.py --puerto 8000 --token mi-token
#     python app.py --falso                 # datos sintéticos (sin Raspberry), para probar
# =============================================================================
import argparse
import datetime
import math
import os
import sys
import threading
import time

from PySide6 import QtCore, QtGui, QtWidgets
from PySide6.QtCore import Qt

import numpy as np
import pyqtgraph as pg

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    import semaforo_ui                   # pestaña del semáforo con RL
except Exception as _e:                 # sin ella la app funciona igual
    semaforo_ui = None
    print(f"[AVISO] Sin pestaña del semáforo: {_e!r}", file=sys.stderr)

# ----------------------------------------------------------------------------- tema
BG = "#111110"
S1 = "#1a1a19"
S2 = "#232321"
BORDE = "#2e2e2b"
GRID = "#2a2a28"
T1 = "#f0efec"
T2 = "#c3c2b7"
T3 = "#8f8e86"
VERDE = "#3fb75e"
ROJO = "#f06a6a"
AMBAR = "#e0a030"
NEUTRO = "#9d9c95"
SERIES = ["#3987e5", "#d95926", "#199e70", "#c98500"]     # colores por cámara (orden de la Pi)

NOMBRE_CAT = {"carro": "Carro", "moto": "Moto", "bus": "Bus", "pesado": "Pesado", "especial": "Especial"}
RANGOS = [(1, "1 h", 60), (6, "6 h", 300), (24, "24 h", 900), (168, "7 días", 3600)]
NOMBRE_PASO = {60: "minuto", 300: "5 min", 900: "15 min", 3600: "hora"}
DIAS = ["lun", "mar", "mié", "jue", "vie", "sáb", "dom"]

AQUI = os.path.dirname(os.path.abspath(__file__))
RAIZ = os.path.dirname(AQUI)

nucleo = None            # módulo nucleo (real o falso); se asigna en main()


# ----------------------------------------------------------------------------- utilidades
def fmt_n(n, decimales=1):
    """Número con formato español: 1.234 y 2,5."""
    if n is None:
        return "–"
    if isinstance(n, float) and not n.is_integer():
        return f"{n:,.{decimales}f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"{int(round(n)):,}".replace(",", ".")


def hace(seg):
    if seg < 2:
        return "ahora"
    if seg < 60:
        return f"hace {int(seg)} s"
    if seg < 3600:
        return f"hace {int(seg // 60)} min"
    return f"hace {int(seg // 3600)} h"


def etiqueta_tiempo(ts, paso, con_dia=None):
    d = datetime.datetime.fromtimestamp(ts)
    hm = d.strftime("%H:%M")
    if con_dia is None:
        con_dia = paso >= 3600 or d.date() != datetime.date.today()
    return f"{DIAS[d.weekday()]} {d.day} {hm}" if con_dia else hm


def color_texto_sobre(hex_color):
    c = QtGui.QColor(hex_color)
    lum = 0.299 * c.red() + 0.587 * c.green() + 0.114 * c.blue()
    return QtGui.QColor("#111111") if lum > 125 else QtGui.QColor("#ffffff")


def etiqueta(texto, rol=None, parent=None):
    lbl = QtWidgets.QLabel(texto, parent)
    if rol:
        lbl.setProperty("rol", rol)
    return lbl


def tarjeta(titulo=None, sub=None):
    """Recuadro con fondo de superficie. Devuelve (frame, layout_vertical, label_sub)."""
    f = QtWidgets.QFrame()
    f.setObjectName("tarjeta")
    lay = QtWidgets.QVBoxLayout(f)
    lay.setContentsMargins(14, 12, 14, 14)
    lay.setSpacing(10)
    lbl_sub = None
    if titulo:
        cab = QtWidgets.QHBoxLayout()
        cab.setSpacing(8)
        cab.addWidget(etiqueta(titulo, "h2"))
        if sub is not None:
            lbl_sub = etiqueta(sub, "sub")
            cab.addWidget(lbl_sub)
        cab.addStretch(1)
        lay.addLayout(cab)
        f.cabecera = cab
    return f, lay, lbl_sub


class _Puente(QtCore.QObject):
    """Lleva al hilo de Qt el resultado de trabajos hechos en otro hilo."""
    listo = QtCore.Signal(object, object, object)

    def __init__(self):
        super().__init__()
        self.listo.connect(self._entregar, Qt.QueuedConnection)

    @QtCore.Slot(object, object, object)
    def _entregar(self, callback, resultado, error):
        try:
            callback(resultado, error)
        except Exception as e:           # un error al pintar no debe cerrar la ventana
            print("Error en callback:", repr(e), file=sys.stderr)


_puente = None


def en_fondo(fn, callback, *args):
    """Ejecuta fn(*args) en un hilo aparte y luego callback(resultado, error) en el hilo de Qt."""
    def correr():
        try:
            r, err = fn(*args), None
        except Exception as e:
            r, err = None, e
        _puente.listo.emit(callback, r, err)
    threading.Thread(target=correr, daemon=True).start()


# ----------------------------------------------------------------------------- widgets básicos
class Punto(QtWidgets.QWidget):
    def __init__(self, color=T3, d=8):
        super().__init__()
        self._color = QtGui.QColor(color)
        self.setFixedSize(d, d)

    def set_color(self, color):
        c = QtGui.QColor(color)
        if c != self._color:
            self._color = c
            self.update()

    def paintEvent(self, _):
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.Antialiasing)
        p.setPen(Qt.NoPen)
        p.setBrush(self._color)
        p.drawEllipse(self.rect())


class Chip(QtWidgets.QFrame):
    """Pastilla con punto de estado opcional y texto enriquecido."""

    def __init__(self, texto="", con_punto=False):
        super().__init__()
        self.setObjectName("chip")
        lay = QtWidgets.QHBoxLayout(self)
        lay.setContentsMargins(10, 4, 10, 4)
        lay.setSpacing(6)
        self.punto = Punto() if con_punto else None
        if self.punto:
            lay.addWidget(self.punto, 0, Qt.AlignVCenter)
        self.lbl = QtWidgets.QLabel(texto)
        self.lbl.setProperty("rol", "chip")
        lay.addWidget(self.lbl)

    def set(self, html, color_punto=None):
        if self.lbl.text() != html:
            self.lbl.setText(html)
        if self.punto and color_punto:
            self.punto.set_color(color_punto)


class Kpi(QtWidgets.QFrame):
    def __init__(self, titulo, grande=False):
        super().__init__()
        self.setObjectName("kpi")
        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(12, 9, 12, 10)
        lay.setSpacing(0)
        lay.addWidget(etiqueta(titulo, "sub"))
        self.val = QtWidgets.QLabel("–")
        self.val.setProperty("rol", "kpi-grande" if grande else "kpi")
        lay.addWidget(self.val)

    def set(self, v):
        t = fmt_n(v)
        if self.val.text() != t:
            self.val.setText(t)


class BarrasCategoria(QtWidgets.QWidget):
    """Barras horizontales simples (nombre, barra neutra, número) para las 5 categorías."""

    def __init__(self):
        super().__init__()
        self.valores = {c: 0 for c in NOMBRE_CAT}
        self.setMinimumHeight(5 * 24)
        self.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Fixed)

    def sizeHint(self):
        return QtCore.QSize(200, 5 * 24)

    def set(self, valores):
        v = {c: int(valores.get(c, 0) or 0) for c in NOMBRE_CAT}
        if v != self.valores:
            self.valores = v
            self.update()

    def paintEvent(self, _):
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.Antialiasing)
        w = self.width()
        col_nombre = 84
        col_num = 40
        maximo = max(1, *self.valores.values())
        for i, c in enumerate(NOMBRE_CAT):
            y = i * 24
            cy = y + 12
            # muestra del color de la caja de esa categoría
            p.setPen(Qt.NoPen)
            p.setBrush(QtGui.QColor(nucleo.COLORES.get(c, NEUTRO)))
            p.drawRoundedRect(QtCore.QRectF(0, cy - 4, 8, 8), 2, 2)
            p.setPen(QtGui.QColor(T1))
            p.drawText(QtCore.QRectF(14, y, col_nombre - 14, 24), Qt.AlignVCenter | Qt.AlignLeft, NOMBRE_CAT[c])
            ancho_barra = max(10, w - col_nombre - col_num - 8)
            fondo = QtCore.QRectF(col_nombre, cy - 4, ancho_barra, 8)
            p.setPen(Qt.NoPen)
            p.setBrush(QtGui.QColor(S2))
            p.drawRoundedRect(fondo, 4, 4)
            v = self.valores[c]
            if v:
                p.setBrush(QtGui.QColor(NEUTRO))
                p.drawRoundedRect(QtCore.QRectF(col_nombre, cy - 4, max(8, ancho_barra * v / maximo), 8), 4, 4)
            p.setPen(QtGui.QColor(T2))
            p.drawText(QtCore.QRectF(w - col_num, y, col_num, 24), Qt.AlignVCenter | Qt.AlignRight, fmt_n(v))
        p.end()


class MiniDato(QtWidgets.QFrame):
    """Dato pequeño: etiqueta tenue arriba y valor abajo."""

    def __init__(self, titulo):
        super().__init__()
        self.setObjectName("kpi")
        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(10, 7, 10, 8)
        lay.setSpacing(1)
        lay.addWidget(etiqueta(titulo, "muted"))
        self.val = QtWidgets.QLabel("–")
        self.val.setProperty("rol", "mini")
        lay.addWidget(self.val)

    def set(self, texto, color=None):
        if self.val.text() != texto:
            self.val.setText(texto)
        estilo = f"color: {color};" if color else ""
        if self.val.styleSheet() != estilo:
            self.val.setStyleSheet(estilo)


class FilaDato(QtWidgets.QWidget):
    """'Clave ........ valor' para el panel de estado."""

    def __init__(self, clave, con_punto=False):
        super().__init__()
        lay = QtWidgets.QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)
        lay.addWidget(etiqueta(clave, "sub"))
        lay.addStretch(1)
        self.punto = Punto() if con_punto else None
        if self.punto:
            lay.addWidget(self.punto, 0, Qt.AlignVCenter)
        self.val = QtWidgets.QLabel("–")
        self.val.setProperty("rol", "valor")
        self.val.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        lay.addWidget(self.val)

    def set(self, texto, color=None, color_punto=None):
        if self.val.text() != texto:
            self.val.setText(texto)
        estilo = f"color: {color};" if color else ""
        if self.val.styleSheet() != estilo:
            self.val.setStyleSheet(estilo)
        if self.punto and color_punto:
            self.punto.set_color(color_punto)


# ----------------------------------------------------------------------------- video
_ESTILOS_CAT = {}
_PLUMA_SOMBRA = None


def _estilo_cat(cat):
    """(QColor, QPen de 2 px, color del texto) por categoría, creados UNA vez."""
    global _PLUMA_SOMBRA
    e = _ESTILOS_CAT.get(cat)
    if e is None:
        if _PLUMA_SOMBRA is None:
            _PLUMA_SOMBRA = QtGui.QPen(QtGui.QColor(0, 0, 0, 120), 4)
        hexa = nucleo.COLORES.get(cat, NEUTRO)
        color = QtGui.QColor(hexa)
        e = _ESTILOS_CAT[cat] = (color, QtGui.QPen(color, 2), color_texto_sobre(hexa))
    return e


class Recuadro(QtWidgets.QWidget):
    """Un recuadro de video: JPEG escalado + cajas de YOLO + cabecera. Se pinta en paintEvent."""
    doble_clic = QtCore.Signal(int)

    def __init__(self, cam):
        super().__init__()
        self.cam = cam
        self.nombre = f"Cam{cam + 1}"
        self.n_imagen = None
        self.seq = None
        self.img = None
        self.cajas = []
        self._pix = None
        self._clave_pix = None
        self.aviso = "Esperando a la Raspberry…"
        self.ampliado = False
        self.setMinimumSize(220, 140)
        self.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Expanding)
        self.setToolTip("Doble clic para ver esta cámara en grande")
        self.setAttribute(Qt.WA_OpaquePaintEvent, True)     # se pinta entero: Qt no borra antes
        self._esquinas = None                                # (tamaño, path) cacheado
        self._fuente_cajas = None                            # (ancho, QFont, QFontMetricsF)
        self._anchos_texto = {}                              # texto de etiqueta -> ancho (px)

    # ---- datos
    def actualizar(self, receptor):
        r = receptor.cuadro(self.cam)
        if r is None:
            if self.img is not None or self.cajas:
                self.img, self.seq, self.cajas, self._pix = None, None, [], None
                self.update()
            return
        seq, jpeg, cajas = r
        cambio = False
        if seq != self.seq:
            img = QtGui.QImage.fromData(jpeg)
            if not img.isNull():
                self.img = img
                self._pix = None
            self.seq = seq
            cambio = True
        if cajas != self.cajas:
            self.cajas = cajas
            cambio = True
        if cambio:
            self.update()

    def limpiar(self, aviso):
        self.img, self.seq, self.cajas, self._pix = None, None, [], None
        self.set_aviso(aviso)
        self.update()

    def set_aviso(self, aviso):
        if aviso != self.aviso:
            self.aviso = aviso
            if self.img is None:
                self.update()

    def set_info(self, nombre, n_imagen):
        if (nombre, n_imagen) != (self.nombre, self.n_imagen):
            self.nombre, self.n_imagen = nombre, n_imagen
            self.update()

    # ---- pintado
    def _rect_imagen(self):
        r = self.rect()
        if self.img is None:
            return QtCore.QRect(r)
        tam = self.img.size().scaled(r.size(), Qt.KeepAspectRatio)
        return QtCore.QRect(r.x() + (r.width() - tam.width()) // 2, r.y() + (r.height() - tam.height()) // 2,
                            tam.width(), tam.height())

    def _path_esquinas(self):
        """Lo que queda FUERA del rectángulo redondeado (se pinta con el color de la tarjeta)."""
        tam = (self.width(), self.height())
        if self._esquinas is None or self._esquinas[0] != tam:
            r = QtCore.QRectF(self.rect())
            fuera = QtGui.QPainterPath()
            fuera.addRect(r)
            dentro = QtGui.QPainterPath()
            dentro.addRoundedRect(r.adjusted(0.5, 0.5, -0.5, -0.5), 8, 8)
            marco = QtGui.QPainterPath()
            marco.addRoundedRect(r.adjusted(0.5, 0.5, -0.5, -0.5), 8, 8)
            self._esquinas = (tam, fuera.subtracted(dentro), marco)
        return self._esquinas[1], self._esquinas[2]

    def paintEvent(self, _):
        p = QtGui.QPainter(self)
        r = QtCore.QRectF(self.rect())
        esquinas, marco = self._path_esquinas()
        p.fillRect(self.rect(), QtGui.QColor("#000000"))
        p.setClipRect(self.rect())

        if self.img is not None:
            ri = self._rect_imagen()
            clave = (self.seq, ri.size().width(), ri.size().height())
            if self._pix is None or self._clave_pix != clave:
                self._pix = QtGui.QPixmap.fromImage(
                    self.img.scaled(ri.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation))
                self._clave_pix = clave
            p.drawPixmap(ri.topLeft(), self._pix)
            p.setRenderHint(QtGui.QPainter.Antialiasing)
            self._pintar_cajas(p, QtCore.QRectF(ri))
        else:
            p.setPen(QtGui.QColor(T3))
            f = p.font()
            f.setPointSizeF(f.pointSizeF() + 1)
            p.setFont(f)
            p.drawText(r, Qt.AlignCenter, self.aviso)
            p.setFont(self.font())

        p.setRenderHint(QtGui.QPainter.Antialiasing)
        self._pintar_cabecera(p, r)
        p.setClipping(False)
        p.fillPath(esquinas, QtGui.QColor(S1))           # esquinas redondeadas (fondo de la tarjeta)
        p.setPen(QtGui.QPen(QtGui.QColor(BORDE), 1))
        p.setBrush(Qt.NoBrush)
        p.drawPath(marco)
        p.end()

    def _pintar_cajas(self, p, ri):
        if not self.cajas:
            return
        tam = round(max(7.5, min(9.0, ri.width() / 80)) * 4) / 4
        if self._fuente_cajas is None or self._fuente_cajas[0] != tam:
            f = QtGui.QFont(self.font())
            f.setPointSizeF(tam)
            f.setBold(True)
            self._fuente_cajas = (tam, f, QtGui.QFontMetricsF(f))
        _, f, fm = self._fuente_cajas
        p.setFont(f)
        # Rectángulos: todos juntos y SIN antialiasing (son rectos: se ven igual y cuestan
        # mucho menos); primero la sombra, luego uno por color. Solo las pastillas con texto
        # (redondeadas) usan antialiasing.
        ox, oy, w, h = ri.x(), ri.y(), ri.width(), ri.height()
        rects, por_color, pastillas = [], {}, []
        th = fm.height() + 4
        anchos = self._anchos_texto
        for c in self.cajas:
            x1 = int(ox + c["x1"] * w)
            y1 = int(oy + c["y1"] * h)
            x2 = int(ox + c["x2"] * w)
            y2 = int(oy + c["y2"] * h)
            caja = QtCore.QRect(x1, y1, max(1, x2 - x1), max(1, y2 - y1))
            rects.append(caja)
            cat = c.get("cat")
            por_color.setdefault(cat, []).append(caja)
            texto = c.get("texto") or cat or ""
            if texto:
                tw = anchos.get(texto)
                if tw is None:
                    if len(anchos) > 500:
                        anchos.clear()
                    tw = anchos[texto] = fm.horizontalAdvance(texto) + 12
                px = min(max(ri.left(), x1 - 1), ri.right() - tw)
                py = y1 - th - 2
                if py < ri.top():
                    py = y1 + 2                      # sin espacio arriba: dentro de la caja
                pastillas.append((cat, QtCore.QRectF(px, py, tw, th), texto))
        p.setRenderHint(QtGui.QPainter.Antialiasing, False)
        p.setBrush(Qt.NoBrush)
        _estilo_cat(None)                            # crea _PLUMA_SOMBRA la primera vez
        p.setPen(_PLUMA_SOMBRA)
        p.drawRects(rects)
        for cat, lista in por_color.items():
            p.setPen(_estilo_cat(cat)[1])
            p.drawRects(lista)
        p.setRenderHint(QtGui.QPainter.Antialiasing, True)
        r = th / 2
        for cat, pastilla, texto in pastillas:
            color, _pluma, color_txt = _estilo_cat(cat)
            p.setPen(Qt.NoPen)
            p.setBrush(color)
            p.drawRoundedRect(pastilla, r, r)
            p.setPen(color_txt)
            p.drawText(pastilla, Qt.AlignCenter, texto)
        p.setFont(self.font())

    def _pintar_cabecera(self, p, r):
        f = QtGui.QFont(self.font())
        f.setBold(True)
        fm = QtGui.QFontMetricsF(f)
        n = self.n_imagen if self.n_imagen is not None else (len(self.cajas) if self.img is not None else None)
        detalle = "" if n is None else (f"  ·  {n} vehículo en imagen" if n == 1 else f"  ·  {n} vehículos en imagen")
        if self.ampliado:
            detalle += "  ·  doble clic para volver"
        f2 = QtGui.QFont(self.font())
        fm2 = QtGui.QFontMetricsF(f2)
        w = fm.horizontalAdvance(self.nombre) + fm2.horizontalAdvance(detalle) + 20
        h = fm.height() + 8
        caja = QtCore.QRectF(r.x() + 8, r.y() + 8, min(w, r.width() - 16), h)
        p.setPen(Qt.NoPen)
        p.setBrush(QtGui.QColor(20, 20, 19, 200))
        p.setPen(QtGui.QPen(QtGui.QColor(255, 255, 255, 40), 1))
        p.drawRoundedRect(caja, h / 2, h / 2)
        p.setFont(f)
        p.setPen(QtGui.QColor("#ffffff"))
        x = caja.x() + 10
        p.drawText(QtCore.QRectF(x, caja.y(), caja.width() - 10, h), Qt.AlignVCenter | Qt.AlignLeft, self.nombre)
        x += fm.horizontalAdvance(self.nombre)
        p.setFont(f2)
        p.setPen(QtGui.QColor("#d8d7d0"))
        p.drawText(QtCore.QRectF(x, caja.y(), max(0.0, caja.right() - x - 6), h),
                   Qt.AlignVCenter | Qt.AlignLeft, fm2.elidedText(detalle, Qt.ElideRight, max(0.0, caja.right() - x - 6)))
        p.setFont(self.font())

    def mouseDoubleClickEvent(self, ev):
        self.doble_clic.emit(self.cam)
        super().mouseDoubleClickEvent(ev)


class Mosaico(QtWidgets.QWidget):
    """Cuadrícula de recuadros: 1x1, 2x1 o 2x2 según el número de cámaras; doble clic amplía uno."""

    def __init__(self):
        super().__init__()
        self.grid = QtWidgets.QGridLayout(self)
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setSpacing(8)
        self.recuadros = [Recuadro(i) for i in range(4)]
        for rc in self.recuadros:
            rc.doble_clic.connect(self._alternar_foco)
        self.n = 4
        self.foco = None
        self._acomodar()

    def set_n(self, n):
        n = max(1, min(4, n))
        if n != self.n:
            self.n = n
            if self.foco is not None and self.foco >= n:
                self.foco = None
            self._acomodar()

    def _alternar_foco(self, cam):
        self.foco = None if self.foco is not None else cam
        self._acomodar()

    def _acomodar(self):
        for rc in self.recuadros:
            self.grid.removeWidget(rc)
            rc.hide()
            rc.ampliado = False
        for i in range(2):
            self.grid.setRowStretch(i, 0)
            self.grid.setColumnStretch(i, 0)
        if self.foco is not None:
            rc = self.recuadros[self.foco]
            rc.ampliado = True
            self.grid.addWidget(rc, 0, 0)
            rc.show()
            self.grid.setRowStretch(0, 1)
            self.grid.setColumnStretch(0, 1)
            return
        if self.n == 1:
            pos = [(0, 0)]
        elif self.n == 2:
            pos = [(0, 0), (0, 1)]
        else:
            pos = [(0, 0), (0, 1), (1, 0), (1, 1)]
        for i in range(self.n):
            self.grid.addWidget(self.recuadros[i], *pos[i])
            self.recuadros[i].show()
        filas = 1 if self.n <= 2 else 2
        for i in range(filas):
            self.grid.setRowStretch(i, 1)
        for i in range(1 if self.n == 1 else 2):
            self.grid.setColumnStretch(i, 1)

    def visibles(self):
        return [rc for rc in self.recuadros if rc.isVisible()]


class TarjetaCamara(QtWidgets.QFrame):
    def __init__(self, nombre, color):
        super().__init__()
        self.setObjectName("tarjeta")
        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(12, 10, 12, 12)
        lay.setSpacing(6)
        cab = QtWidgets.QHBoxLayout()
        cab.setSpacing(8)
        self.muestra = QtWidgets.QFrame()
        self.muestra.setFixedSize(12, 3)
        cab.addWidget(self.muestra, 0, Qt.AlignVCenter)
        self.lbl_nombre = etiqueta(nombre, "h2")
        cab.addWidget(self.lbl_nombre)
        cab.addStretch(1)
        self.punto = Punto(T3, 7)
        cab.addWidget(self.punto, 0, Qt.AlignVCenter)
        self.lbl_estado = etiqueta("", "muted")
        cab.addWidget(self.lbl_estado)
        lay.addLayout(cab)
        nums = QtWidgets.QHBoxLayout()
        nums.setSpacing(14)
        self.v_ahora, self.v_hora = QtWidgets.QLabel("–"), QtWidgets.QLabel("–")
        for v, et in ((self.v_ahora, "en imagen"), (self.v_hora, "en 1 h")):
            col = QtWidgets.QVBoxLayout()
            col.setSpacing(0)
            v.setProperty("rol", "kpi-cam")
            col.addWidget(v)
            col.addWidget(etiqueta(et, "muted"))
            nums.addLayout(col)
        nums.addStretch(1)
        lay.addLayout(nums)
        self.detalle = etiqueta("Sin datos", "muted")
        self.detalle.setWordWrap(True)
        lay.addWidget(self.detalle)
        self.set_color(color)

    def _estado(self, texto, color):
        self.punto.set_color(color)
        self.lbl_estado.setText(texto)
        self.lbl_estado.setToolTip("análisis de YOLO por segundo en esta cámara" if color == VERDE else "")

    def set_color(self, color):
        self.muestra.setStyleSheet(f"background: {color}; border-radius: 1px;")

    def set(self, cam, flujo_1h, pi_ok):
        if cam is None or not pi_ok:
            self._estado("sin datos", T3)
            self.v_ahora.setText("–")
        else:
            if cam.get("conectada"):
                self._estado(f"{fmt_n(float(cam.get('fps') or 0))} an./s", VERDE)
            else:
                self._estado("sin señal", ROJO)
            self.v_ahora.setText(fmt_n(cam.get("total")))
            por = cam.get("por_categoria") or {}
            partes = [f"{NOMBRE_CAT.get(k, k)} {v}" for k, v in por.items() if v]
            self.detalle.setText(" · ".join(partes) if partes else "Sin vehículos en imagen")
        self.v_hora.setText(fmt_n(flujo_1h))


# ----------------------------------------------------------------------------- pestaña En vivo
class PestanaVivo(QtWidgets.QWidget):
    def __init__(self, ventana):
        super().__init__()
        self.v = ventana
        raiz = QtWidgets.QHBoxLayout(self)
        raiz.setContentsMargins(0, 12, 0, 0)
        raiz.setSpacing(16)

        # --- video
        tv, lv, _ = tarjeta("Video en vivo con detecciones")
        self.lbl_tasa = etiqueta("", "sub")
        tv.cabecera.addWidget(self.lbl_tasa)
        self.btn_video = QtWidgets.QPushButton("Pausar video")
        self.btn_video.setCheckable(True)
        self.btn_video.toggled.connect(self.v.alternar_video)
        tv.cabecera.addWidget(self.btn_video)
        self.mosaico = Mosaico()
        lv.addWidget(self.mosaico, 1)
        nota = etiqueta("Se muestra con un pequeño retraso para mover las cajas con suavidad entre "
                        "análisis de YOLO. Doble clic en una cámara para verla en grande.", "muted")
        nota.setWordWrap(True)
        lv.addWidget(nota)
        raiz.addWidget(tv, 1)

        # --- panel lateral
        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setFixedWidth(392)
        panel = QtWidgets.QWidget()
        panel.setObjectName("transparente")
        lp = QtWidgets.QVBoxLayout(panel)
        lp.setContentsMargins(0, 0, 8, 0)
        lp.setSpacing(12)

        te, le, _ = tarjeta("Raspberry Pi")
        self.punto_pi = Punto()
        self.lbl_pi = etiqueta("–", "valor")
        te.cabecera.addWidget(self.punto_pi, 0, Qt.AlignVCenter)
        te.cabecera.addWidget(self.lbl_pi)
        self.lbl_ultimo = etiqueta("", "muted")
        le.addWidget(self.lbl_ultimo)
        rej = QtWidgets.QGridLayout()
        rej.setHorizontalSpacing(10)
        rej.setVerticalSpacing(10)
        self.m_temp = MiniDato("Temperatura CPU")
        self.m_yolo = MiniDato("YOLO")
        self.m_kbps = MiniDato("Video recibido")
        self.m_cps = MiniDato("Cuadros de video")
        rej.addWidget(self.m_temp, 0, 0)
        rej.addWidget(self.m_yolo, 0, 1)
        rej.addWidget(self.m_kbps, 1, 0)
        rej.addWidget(self.m_cps, 1, 1)
        le.addLayout(rej)
        lp.addWidget(te)

        tk, lk, _ = tarjeta("Ahora")
        self.k_total = Kpi("Vehículos en imagen (todas las cámaras)", grande=True)
        lk.addWidget(self.k_total)
        fila = QtWidgets.QHBoxLayout()
        fila.setSpacing(10)
        self.k_hora = Kpi("Pasaron en la última hora")
        self.k_hoy = Kpi("Pasaron hoy")
        fila.addWidget(self.k_hora)
        fila.addWidget(self.k_hoy)
        lk.addLayout(fila)
        lk.addWidget(etiqueta("En imagen por categoría", "sub"))
        self.barras = BarrasCategoria()
        lk.addWidget(self.barras)
        lp.addWidget(tk)

        lp.addWidget(etiqueta("Cámaras", "h2"))
        self.cont_cams = QtWidgets.QGridLayout()
        self.cont_cams.setSpacing(10)
        lp.addLayout(self.cont_cams)
        lp.addStretch(1)
        self.tarjetas = {}
        scroll.setWidget(panel)
        raiz.addWidget(scroll)

    def tarjeta_cam(self, nombre):
        if nombre not in self.tarjetas:
            t = TarjetaCamara(nombre, self.v.color_cam(nombre))
            i = len(self.tarjetas)
            self.cont_cams.addWidget(t, i // 2, i % 2)
            self.tarjetas[nombre] = t
        return self.tarjetas[nombre]


# ----------------------------------------------------------------------------- pestaña Historial
class EjeTiempo(pg.DateAxisItem):
    """Eje X con horas locales en español."""

    def tickStrings(self, values, scale, spacing):
        res = []
        for v in values:
            try:
                d = datetime.datetime.fromtimestamp(v)
            except (OSError, OverflowError, ValueError):
                res.append("")
                continue
            if spacing >= 86400 or (spacing >= 3600 and d.hour == 0 and d.minute == 0):
                res.append(f"{DIAS[d.weekday()]} {d.day}")
            else:
                res.append(d.strftime("%H:%M"))
        return res


class PestanaHistorial(QtWidgets.QWidget):
    def __init__(self, ventana):
        super().__init__()
        self.v = ventana
        self.hist = None
        self.rango, self.paso = 1, 60
        self.metrica = "flujo"
        self._pedido = 0
        self.t_hist = 0.0

        raiz = QtWidgets.QVBoxLayout(self)
        raiz.setContentsMargins(0, 12, 0, 0)
        raiz.setSpacing(12)

        # --- controles
        ctr = QtWidgets.QHBoxLayout()
        ctr.setSpacing(6)
        self.grupo_rango = QtWidgets.QButtonGroup(self)
        for horas, txt, paso in RANGOS:
            b = QtWidgets.QPushButton(txt)
            b.setCheckable(True)
            b.setChecked(horas == 1)
            b.clicked.connect(lambda _=False, h=horas, p=paso: self.cambiar_rango(h, p))
            self.grupo_rango.addButton(b)
            ctr.addWidget(b)
        ctr.addSpacing(14)
        self.grupo_met = QtWidgets.QButtonGroup(self)
        for clave, txt in (("flujo", "Vehículos que pasaron"), ("ocupacion", "En imagen (promedio)")):
            b = QtWidgets.QPushButton(txt)
            b.setCheckable(True)
            b.setChecked(clave == "flujo")
            b.clicked.connect(lambda _=False, k=clave: self.cambiar_metrica(k))
            self.grupo_met.addButton(b)
            ctr.addWidget(b)
        ctr.addStretch(1)
        self.lbl_act = etiqueta("", "muted")
        ctr.addWidget(self.lbl_act)
        ctr.addSpacing(8)
        self.btn_tabla = QtWidgets.QPushButton("Ver tabla")
        self.btn_tabla.setCheckable(True)
        self.btn_tabla.toggled.connect(self.alternar_tabla)
        ctr.addWidget(self.btn_tabla)
        self.btn_csv = QtWidgets.QPushButton("Exportar CSV…")
        self.btn_csv.clicked.connect(self.exportar)
        ctr.addWidget(self.btn_csv)
        raiz.addLayout(ctr)

        # --- gráficas
        graf = QtWidgets.QHBoxLayout()
        graf.setSpacing(16)
        tl, ll, _ = tarjeta("Vehículos que pasaron por minuto")
        self.lbl_titulo = tl.findChild(QtWidgets.QLabel)
        self.leyenda = QtWidgets.QHBoxLayout()
        self.leyenda.setSpacing(16)
        self.leyenda.addStretch(1)
        ll.addLayout(self.leyenda)

        self.eje = EjeTiempo(orientation="bottom")
        self.plot = pg.PlotWidget(axisItems={"bottom": self.eje}, background=S1)
        self._estilo_plot(self.plot)
        self.plot.setMinimumHeight(260)
        pi = self.plot.getPlotItem()
        pi.showGrid(x=False, y=True, alpha=0.18)
        self.vline = pg.InfiniteLine(angle=90, movable=False, pen=pg.mkPen(T3, width=1, style=Qt.DashLine))
        self.vline.setZValue(5)
        self.txt_cursor = pg.TextItem("", anchor=(0, 0), fill=pg.mkBrush(QtGui.QColor(26, 26, 25, 235)),
                                      border=pg.mkPen(BORDE))
        self.txt_cursor.setZValue(10)
        pi.addItem(self.vline, ignoreBounds=True)
        pi.addItem(self.txt_cursor, ignoreBounds=True)
        self.vline.hide()
        self.txt_cursor.hide()
        self.plot.scene().sigMouseMoved.connect(self._mouse)
        self.plot.installEventFilter(self)
        self.curvas = []
        ll.addWidget(self.plot, 1)
        self.vacio = QtWidgets.QLabel("Todavía no hay historial en este rango.", self.plot)
        self.vacio.setAlignment(Qt.AlignCenter)
        self.vacio.setProperty("rol", "muted")
        self.vacio.hide()
        self.nota = etiqueta("", "muted")
        self.nota.setWordWrap(True)
        ll.addWidget(self.nota)
        graf.addWidget(tl, 5)

        tc, lc, self.lbl_rango_cat = tarjeta("Por categoría", "")
        self.plot_cat = pg.PlotWidget(background=S1)
        self._estilo_plot(self.plot_cat)
        self.plot_cat.setMinimumHeight(220)
        pc = self.plot_cat.getPlotItem()
        pc.showGrid(x=False, y=False)
        pc.getAxis("bottom").setStyle(showValues=False)
        pc.getAxis("bottom").setPen(pg.mkPen(None))
        eje_y = pc.getAxis("left")
        eje_y.setTicks([[(4 - i, NOMBRE_CAT[c]) for i, c in enumerate(NOMBRE_CAT)]])
        eje_y.setTextPen(T2)
        eje_y.setWidth(70)
        self.barras_cat = pg.BarGraphItem(x0=[0] * 5, y=[4, 3, 2, 1, 0], height=0.56, width=[0] * 5,
                                          brush=NEUTRO, pen=pg.mkPen(None))
        pc.addItem(self.barras_cat)
        self.txt_barras = []
        for i in range(5):
            t = pg.TextItem("", color=T2, anchor=(0, 0.5))
            pc.addItem(t)
            self.txt_barras.append(t)
        pc.setYRange(-0.6, 4.6, padding=0)
        lc.addWidget(self.plot_cat, 1)
        self.nota_cat = etiqueta("Vehículos distintos que pasaron en el rango, sumando todas las cámaras.", "muted")
        self.nota_cat.setWordWrap(True)
        lc.addWidget(self.nota_cat)
        graf.addWidget(tc, 2)
        raiz.addLayout(graf, 3)

        # --- tabla
        self.t_tabla, lt, _ = tarjeta("Datos")
        self.tabla = QtWidgets.QTableWidget()
        self.tabla.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.tabla.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.tabla.verticalHeader().hide()
        self.tabla.setAlternatingRowColors(True)
        self.tabla.setShowGrid(False)
        self.tabla.setMinimumHeight(200)
        lt.addWidget(self.tabla)
        self.t_tabla.hide()
        raiz.addWidget(self.t_tabla, 2)

    @staticmethod
    def _estilo_plot(w):
        pi = w.getPlotItem()
        pi.setMenuEnabled(False)
        pi.hideButtons()
        pi.getViewBox().setMouseEnabled(False, False)
        pi.getViewBox().setDefaultPadding(0)
        for nombre in ("left", "bottom"):
            ax = pi.getAxis(nombre)
            ax.setPen(pg.mkPen(GRID))
            ax.setTextPen(pg.mkPen(T3))
            ax.setStyle(tickTextOffset=6)
        w.setFrameStyle(QtWidgets.QFrame.NoFrame)

    def eventFilter(self, obj, ev):
        if obj is self.plot:
            if ev.type() == QtCore.QEvent.Leave:
                self.vline.hide()
                self.txt_cursor.hide()
            elif ev.type() == QtCore.QEvent.Resize:
                self.vacio.setGeometry(0, 0, self.plot.width(), self.plot.height())
        return super().eventFilter(obj, ev)

    # ---- acciones
    def cambiar_rango(self, horas, paso):
        self.rango, self.paso = horas, paso
        self.pedir()

    def cambiar_metrica(self, clave):
        self.metrica = clave
        if self.hist:
            self.pintar()

    def alternar_tabla(self, visible):
        self.t_tabla.setVisible(visible)
        self.btn_tabla.setText("Ocultar tabla" if visible else "Ver tabla")
        if visible and self.hist:
            self._llenar_tabla()

    def pedir(self):
        rec = self.v.receptor
        if rec is None:
            return
        self._pedido += 1
        n = self._pedido
        self.lbl_act.setText("Cargando…")
        en_fondo(rec.historial, lambda r, e: self._recibido(n, r, e), self.rango, self.paso)

    def _recibido(self, n, r, err):
        if n != self._pedido:
            return                           # llegó tarde: ya se pidió otro rango
        if err is not None:
            self.lbl_act.setText("Error al leer el historial")
            print("historial:", repr(err), file=sys.stderr)
            return
        self.hist = r
        self.t_hist = time.time()
        self.lbl_act.setText("Actualizado " + time.strftime("%H:%M"))
        self.pintar()

    def exportar(self):
        rec = self.v.receptor
        if rec is None:
            return
        nombre = f"historial_trafico_{time.strftime('%Y%m%d_%H%M')}.csv"
        docs = QtCore.QStandardPaths.writableLocation(QtCore.QStandardPaths.DocumentsLocation) or AQUI
        ruta, _ = QtWidgets.QFileDialog.getSaveFileName(self, "Exportar historial a CSV",
                                                        os.path.join(docs, nombre), "CSV (*.csv)")
        if not ruta:
            return
        self.btn_csv.setEnabled(False)
        self.btn_csv.setText("Exportando…")

        def listo(filas, err):
            self.btn_csv.setEnabled(True)
            self.btn_csv.setText("Exportar CSV…")
            if err is not None:
                QtWidgets.QMessageBox.warning(self, "Exportar CSV", f"No se pudo exportar:\n{err}")
            else:
                self.v.statusBar().showMessage(f"Se exportaron {fmt_n(filas)} filas a {ruta}", 8000)
        en_fondo(rec.exportar_csv, listo, ruta, float(self.rango))

    # ---- pintado
    def _serie(self, nombre):
        c = self.hist["camaras"][nombre]
        return c.get("flujo" if self.metrica == "flujo" else "ocupacion") or []

    def pintar(self):
        h = self.hist
        pi = self.plot.getPlotItem()
        for c in self.curvas:
            pi.removeItem(c)
        self.curvas = []
        while self.leyenda.count() > 1:
            it = self.leyenda.takeAt(0)
            if it.widget():
                it.widget().deleteLater()
        es_flujo = self.metrica == "flujo"
        paso = h.get("paso", self.paso)
        self.lbl_titulo.setText(f"Vehículos que pasaron por {NOMBRE_PASO.get(paso, f'{paso} s')}"
                                if es_flujo else "Vehículos en imagen (promedio)")
        self.nota.setText("Vehículos distintos detectados (cada ID cuenta una vez). Los huecos son "
                          "periodos sin datos de la Raspberry. Pasa el cursor para ver los valores."
                          if es_flujo else
                          "Promedio de vehículos visibles al mismo tiempo: indica qué tan congestionada "
                          "está la vía. Los huecos son periodos sin datos.")
        ts = np.array(h.get("ts") or [], dtype=float)
        nombres = list(h.get("camaras", {}).keys())
        maximo = 0.0
        hay = False
        for nom in nombres:
            col = self.v.color_cam(nom)
            vals = self._serie(nom)
            y = np.array([np.nan if v is None else float(v) for v in vals], dtype=float)
            if len(y) != len(ts) or not len(ts):
                continue
            finitos = np.isfinite(y)
            if finitos.any():
                hay = True
                maximo = max(maximo, float(np.nanmax(y)))
            curva = pi.plot(ts, y, pen=pg.mkPen(col, width=2), connect="finite")
            self.curvas.append(curva)
            # puntos aislados (rodeados de huecos) no se verían como línea: se marcan con un punto
            izq = np.concatenate(([False], finitos[:-1]))
            der = np.concatenate((finitos[1:], [False]))
            aislados = finitos & ~izq & ~der
            if aislados.any():
                sc = pg.ScatterPlotItem(ts[aislados], y[aislados], size=5, brush=pg.mkBrush(col), pen=None)
                pi.addItem(sc)
                self.curvas.append(sc)
            # leyenda
            item = QtWidgets.QWidget()
            li = QtWidgets.QHBoxLayout(item)
            li.setContentsMargins(0, 0, 0, 0)
            li.setSpacing(6)
            m = QtWidgets.QFrame()
            m.setFixedSize(14, 3)
            m.setStyleSheet(f"background: {col}; border-radius: 1px;")
            li.addWidget(m, 0, Qt.AlignVCenter)
            li.addWidget(etiqueta(nom, "sub"))
            self.leyenda.insertWidget(self.leyenda.count() - 1, item)
        if len(ts):
            pi.setXRange(ts[0], ts[-1], padding=0.01)
        top = max(1.0, maximo) * 1.15
        pi.setYRange(-top * 0.03, top, padding=0)
        self._ymax = top
        self.vacio.setGeometry(0, 0, self.plot.width(), self.plot.height())
        self.vacio.setVisible(not hay)
        self.vline.hide()
        self.txt_cursor.hide()

        # categorías
        cats = h.get("categorias", {}) or {}
        vals = [int(cats.get(c, 0) or 0) for c in NOMBRE_CAT]
        self.barras_cat.setOpts(width=vals)
        mx = max(1, max(vals))
        self.plot_cat.getPlotItem().setXRange(0, mx * 1.28, padding=0)
        for i, v in enumerate(vals):
            self.txt_barras[i].setText(fmt_n(v))
            self.txt_barras[i].setPos(v + mx * 0.02, 4 - i)
        rango_txt = "7 días" if self.rango == 168 else f"{self.rango} h"
        self.lbl_rango_cat.setText(f"· {fmt_n(h.get('flujo_total', 0))} en {rango_txt}")
        if self.btn_tabla.isChecked():
            self._llenar_tabla()

    def _mouse(self, pos):
        h = self.hist
        if not h or not h.get("ts"):
            return
        vb = self.plot.getPlotItem().getViewBox()
        if not vb.sceneBoundingRect().contains(pos):
            self.vline.hide()
            self.txt_cursor.hide()
            return
        x = vb.mapSceneToView(pos).x()
        ts = h["ts"]
        paso = h.get("paso", self.paso)
        i = int(round((x - ts[0]) / paso))
        i = max(0, min(len(ts) - 1, i))
        self.vline.setPos(ts[i])
        filas = [f"<div style='color:{T1}; font-weight:600'>{etiqueta_tiempo(ts[i], paso)}</div>"]
        for nom in h["camaras"]:
            serie = self._serie(nom)
            v = serie[i] if i < len(serie) else None
            txt = "sin datos" if v is None else fmt_n(v)
            filas.append(f"<div style='color:{T2}'><span style='color:{self.v.color_cam(nom)}'>&#9632;</span> "
                         f"{nom}: <b style='color:{T1}'>{txt}</b></div>")
        self.txt_cursor.setHtml("<div style='padding:2px 4px'>" + "".join(filas) + "</div>")
        (x0, x1), (y0, y1) = vb.viewRange()
        derecha = ts[i] > (x0 + x1) / 2
        self.txt_cursor.setAnchor((1.0, 0.0) if derecha else (0.0, 0.0))
        desplaz = (x1 - x0) * 0.008
        self.txt_cursor.setPos(ts[i] - desplaz if derecha else ts[i] + desplaz, y1)
        self.vline.show()
        self.txt_cursor.show()

    def _llenar_tabla(self):
        h = self.hist
        nombres = list(h["camaras"].keys())
        ts = h["ts"]
        paso = h.get("paso", self.paso)
        self.tabla.setUpdatesEnabled(False)
        self.tabla.clear()
        self.tabla.setColumnCount(1 + len(nombres))
        for j, titulo in enumerate(["Hora"] + nombres):
            it = QtWidgets.QTableWidgetItem(titulo)
            it.setTextAlignment((Qt.AlignLeft if j == 0 else Qt.AlignRight) | Qt.AlignVCenter)
            self.tabla.setHorizontalHeaderItem(j, it)
        self.tabla.setRowCount(len(ts))
        series = [self._serie(n) for n in nombres]
        for fila, i in enumerate(range(len(ts) - 1, -1, -1)):     # lo más reciente arriba
            it = QtWidgets.QTableWidgetItem(etiqueta_tiempo(ts[i], paso))
            self.tabla.setItem(fila, 0, it)
            for j, s in enumerate(series):
                v = s[i] if i < len(s) else None
                it = QtWidgets.QTableWidgetItem("–" if v is None else fmt_n(v))
                it.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                if v is None:
                    it.setForeground(QtGui.QColor(T3))
                self.tabla.setItem(fila, j + 1, it)
        cab = self.tabla.horizontalHeader()
        cab.setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeToContents)
        for j in range(len(nombres)):
            cab.setSectionResizeMode(j + 1, QtWidgets.QHeaderView.Stretch)
        self.tabla.setUpdatesEnabled(True)


# ----------------------------------------------------------------------------- pestaña Conexión
class PestanaConexion(QtWidgets.QWidget):
    def __init__(self, ventana):
        super().__init__()
        self.v = ventana
        self.ip = nucleo.ip_local()
        mono = QtGui.QFontDatabase.systemFont(QtGui.QFontDatabase.FixedFont)
        mono.setPointSizeF(10)

        scroll = QtWidgets.QScrollArea(self)
        scroll.setWidgetResizable(True)
        cont = QtWidgets.QWidget()
        cont.setObjectName("transparente")
        scroll.setWidget(cont)
        ext = QtWidgets.QVBoxLayout(self)
        ext.setContentsMargins(0, 12, 0, 0)
        ext.addWidget(scroll)

        raiz = QtWidgets.QHBoxLayout(cont)
        raiz.setContentsMargins(0, 0, 8, 0)
        raiz.setSpacing(16)
        izq = QtWidgets.QVBoxLayout()
        izq.setSpacing(16)
        der = QtWidgets.QVBoxLayout()
        der.setSpacing(16)
        raiz.addLayout(izq, 1)
        raiz.addLayout(der, 1)

        # --- comando para la Pi
        t1, l1, _ = tarjeta("Conectar la Raspberry")
        txt = etiqueta("La Raspberry y esta laptop deben estar en la <b>misma red</b> (el mismo Wi-Fi o "
                       "cable). <b>Con el agente instalado no hace falta</b>: usa la pestaña <i>Control</i>. "
                       "Sin agente, en la Raspberry ejecuta:", "sub")
        txt.setWordWrap(True)
        l1.addWidget(txt)
        fila = QtWidgets.QHBoxLayout()
        self.cmd = QtWidgets.QLineEdit()
        self.cmd.setReadOnly(True)
        self.cmd.setFont(mono)
        fila.addWidget(self.cmd, 1)
        b = QtWidgets.QPushButton("Copiar")
        b.clicked.connect(lambda: self._copiar(self.cmd.text(), b))
        fila.addWidget(b)
        l1.addLayout(fila)
        self.lbl_datos = etiqueta("", "sub")
        self.lbl_datos.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.lbl_datos.setWordWrap(True)
        l1.addWidget(self.lbl_datos)
        self.f_receptor = FilaDato("Receptor", con_punto=True)
        self.f_pi = FilaDato("Raspberry", con_punto=True)
        l1.addWidget(self.f_receptor)
        l1.addWidget(self.f_pi)
        aviso = QtWidgets.QLabel(
            "<b>Firewall de Windows:</b> la primera vez que abras el programa, Windows pregunta si Python "
            "puede usar la red. Marca <b>«Redes privadas»</b> y pulsa <b>Permitir</b>. Si lo negaste, ve a "
            "<i>Firewall de Windows Defender → Permitir una aplicación</i> y habilita Python en redes privadas. "
            "Si la laptop tiene varias redes, confirma la IP con <code>ipconfig</code>.")
        aviso.setObjectName("aviso")
        aviso.setWordWrap(True)
        l1.addWidget(aviso)
        izq.addWidget(t1)

        # --- ajustes del receptor
        t2, l2, _ = tarjeta("Ajustes del receptor")
        form = QtWidgets.QFormLayout()
        form.setHorizontalSpacing(16)
        form.setVerticalSpacing(10)
        form.setLabelAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self.sp_puerto = QtWidgets.QSpinBox()
        self.sp_puerto.setRange(1024, 65535)
        self.sp_puerto.setValue(self.v.puerto)
        self.ed_token = QtWidgets.QLineEdit(self.v.token)
        self.sp_retraso = QtWidgets.QDoubleSpinBox()
        self.sp_retraso.setRange(0.3, 3.0)
        self.sp_retraso.setSingleStep(0.1)
        self.sp_retraso.setDecimals(1)
        self.sp_retraso.setSuffix(" s")
        self.sp_retraso.setValue(self.v.retraso)
        for w in (self.sp_puerto, self.ed_token, self.sp_retraso):
            w.setMinimumWidth(200)
            w.setMaximumWidth(320)
        form.addRow(etiqueta("Puerto", "sub"), self.sp_puerto)
        form.addRow(etiqueta("Token", "sub"), self.ed_token)
        form.addRow(etiqueta("Retraso de visualización", "sub"), self.sp_retraso)
        l2.addLayout(form)
        n2 = etiqueta("Cambiar el puerto o el token reinicia el receptor (la Raspberry se reconecta sola; "
                      "recuerda usar los mismos valores en su comando). El retraso se aplica al instante: "
                      "más retraso = cajas más suaves, menos retraso = video más inmediato. Es el MÍNIMO: si "
                      "YOLO analiza más lento, sube solo lo necesario (hasta 2,5 s) para que las cajas no salten.", "muted")
        n2.setWordWrap(True)
        l2.addWidget(n2)
        fb = QtWidgets.QHBoxLayout()
        fb.addStretch(1)
        self.btn_aplicar = QtWidgets.QPushButton("Aplicar")
        self.btn_aplicar.setProperty("rol", "primario")
        self.btn_aplicar.clicked.connect(self.aplicar)
        fb.addWidget(self.btn_aplicar)
        l2.addLayout(fb)
        izq.addWidget(t2)
        izq.addStretch(1)

        # --- simulador
        t3, l3, _ = tarjeta("Cámaras simuladas")
        n3 = etiqueta("Convierte esta laptop en 4 cámaras IP falsas (MJPEG) a partir de videos, con "
                      "<code>camfalsa4.py</code>. La Raspberry las lee como si fueran cámaras reales.", "sub")
        n3.setWordWrap(True)
        l3.addWidget(n3)
        self.ed_videos = []
        rejilla = QtWidgets.QGridLayout()
        rejilla.setHorizontalSpacing(8)
        rejilla.setVerticalSpacing(8)
        cfg = QtCore.QSettings("MonitorTrafico", "InterfazLaptop")
        guardados = cfg.value("videos", None)
        if not guardados:                         # por defecto: laptop/videos/prueba_1..4.mp4
            guardados = [r for r in (os.path.join(RAIZ, "videos", f"prueba_{i}.mp4") for i in range(1, 5))
                         if os.path.exists(r)]
        if isinstance(guardados, str):
            guardados = [guardados]
        for i in range(4):
            rejilla.addWidget(etiqueta(f"Cámara {i + 1}", "sub"), i, 0)
            ed = QtWidgets.QLineEdit(guardados[i] if i < len(guardados) else "")
            ed.setPlaceholderText("Sin video")
            ed.setToolTip(ed.text())
            ed.textChanged.connect(lambda t, e=ed: e.setToolTip(t))
            rejilla.addWidget(ed, i, 1)
            be = QtWidgets.QPushButton("Elegir…")
            be.clicked.connect(lambda _=False, e=ed: self._elegir_video(e))
            rejilla.addWidget(be, i, 2)
            bq = QtWidgets.QPushButton("Quitar")
            bq.clicked.connect(lambda _=False, e=ed: e.clear())
            rejilla.addWidget(bq, i, 3)
            self.ed_videos.append(ed)
        rejilla.setColumnStretch(1, 1)
        l3.addLayout(rejilla)

        opc = QtWidgets.QHBoxLayout()
        opc.setSpacing(8)
        self.sp_sim_puerto = self._spin(1024, 65000, int(cfg.value("sim_puerto", 8080)))
        self.sp_ancho = self._spin(160, 1920, int(cfg.value("sim_ancho", 640)), paso=32)
        self.sp_fps = self._spin(1, 30, int(cfg.value("sim_fps", 10)))
        self.sp_calidad = self._spin(20, 95, int(cfg.value("sim_calidad", 75)), paso=5)
        for txt, w in (("Puerto inicial", self.sp_sim_puerto), ("Ancho", self.sp_ancho),
                       ("Cuadros/s", self.sp_fps), ("Calidad JPEG", self.sp_calidad)):
            col = QtWidgets.QVBoxLayout()
            col.setSpacing(4)
            col.addWidget(etiqueta(txt, "muted"))
            col.addWidget(w)
            opc.addLayout(col)
        opc.addStretch(1)
        l3.addLayout(opc)

        fb3 = QtWidgets.QHBoxLayout()
        self.chip_sim = Chip("Detenido", con_punto=True)
        fb3.addWidget(self.chip_sim)
        fb3.addStretch(1)
        self.btn_sim_ini = QtWidgets.QPushButton("Iniciar cámaras")
        self.btn_sim_ini.setProperty("rol", "primario")
        self.btn_sim_ini.clicked.connect(self.iniciar_sim)
        self.btn_sim_det = QtWidgets.QPushButton("Detener")
        self.btn_sim_det.clicked.connect(self.detener_sim)
        fb3.addWidget(self.btn_sim_ini)
        fb3.addWidget(self.btn_sim_det)
        l3.addLayout(fb3)

        l3.addWidget(etiqueta("Pon esto en <code>CAMARAS</code> de la Raspberry:", "sub"))
        fu = QtWidgets.QHBoxLayout()
        self.ed_urls = QtWidgets.QLineEdit()
        self.ed_urls.setReadOnly(True)
        self.ed_urls.setFont(mono)
        self.ed_urls.setPlaceholderText("Inicia las cámaras para ver las direcciones")
        fu.addWidget(self.ed_urls, 1)
        bu = QtWidgets.QPushButton("Copiar")
        bu.clicked.connect(lambda: self._copiar(self.ed_urls.text(), bu))
        fu.addWidget(bu)
        l3.addLayout(fu)
        l3.addWidget(etiqueta("Registro del simulador", "sub"))
        self.log = QtWidgets.QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setFont(mono)
        self.log.setMinimumHeight(140)
        self.log.setLineWrapMode(QtWidgets.QPlainTextEdit.NoWrap)
        self.log.setPlaceholderText("Aquí aparece la salida de camfalsa4.py")
        l3.addWidget(self.log, 1)
        der.addWidget(t3, 1)

        self._ultimo_log = None
        self._urls_cmd = []
        self.actualizar_comando()

    @staticmethod
    def _spin(a, b, v, paso=1):
        s = QtWidgets.QSpinBox()
        s.setRange(a, b)
        s.setSingleStep(paso)
        s.setValue(v)
        s.setMinimumWidth(92)
        return s

    def _copiar(self, texto, boton):
        if not texto:
            return
        QtWidgets.QApplication.clipboard().setText(texto)
        boton.setText("Copiado ✓")
        QtCore.QTimer.singleShot(1500, lambda: boton.setText("Copiar"))

    def _elegir_video(self, ed):
        inicio = os.path.dirname(ed.text()) if ed.text() else os.path.join(RAIZ, "videos")
        rutas, _ = QtWidgets.QFileDialog.getOpenFileNames(
            self, "Elegir video(s) para las cámaras simuladas", inicio,
            "Videos (*.mp4 *.avi *.mkv *.mov *.m4v);;Todos los archivos (*)")
        if not rutas:
            return
        i = self.ed_videos.index(ed)
        for k, r in enumerate(rutas[:4 - i]):              # varios elegidos: llenan las siguientes
            self.ed_videos[i + k].setText(os.path.normpath(r))

    def actualizar_comando(self):
        cmd = f"python conteo4_nube.py --laptop {self.ip}:{self.v.puerto} --token-laptop {self.v.token}"
        if self._urls_cmd:                       # con el simulador activo, también las cámaras
            cmd += " " + " ".join(self._urls_cmd)
        self.cmd.setText(cmd)
        self.cmd.setToolTip(cmd)
        self.cmd.setCursorPosition(0)
        self.cmd.setCursorPosition(0)
        self.lbl_datos.setText(f"IP de esta laptop: <b style='color:{T1}'>{self.ip}</b>  ·  Puerto: "
                               f"<b style='color:{T1}'>{self.v.puerto}</b>  ·  Token: "
                               f"<b style='color:{T1}'>{self.v.token}</b>")

    def aplicar(self):
        puerto, token = self.sp_puerto.value(), self.ed_token.text().strip() or "cambia-esto"
        self.ed_token.setText(token)
        self.v.aplicar_ajustes(puerto, token, self.sp_retraso.value())

    def iniciar_sim(self, silencioso=False):
        """Enciende las cámaras simuladas. Devuelve (ok, error). silencioso = orden remota
        (desde la web): sin ventanas de aviso, el error se devuelve."""
        if self.v.simulador.activo:
            return True, None
        videos = [e.text().strip() for e in self.ed_videos if e.text().strip()]
        faltan = [v for v in videos if not os.path.exists(v)]
        error = ("No hay videos elegidos para las cámaras simuladas" if not videos else
                 "No existe el archivo: " + ", ".join(faltan) if faltan else None)
        if error:
            if not silencioso:
                QtWidgets.QMessageBox.warning(self, "Cámaras simuladas", error)
            return False, error
        cfg = QtCore.QSettings("MonitorTrafico", "InterfazLaptop")
        cfg.setValue("videos", [e.text().strip() for e in self.ed_videos])
        cfg.setValue("sim_puerto", self.sp_sim_puerto.value())
        cfg.setValue("sim_ancho", self.sp_ancho.value())
        cfg.setValue("sim_fps", self.sp_fps.value())
        cfg.setValue("sim_calidad", self.sp_calidad.value())
        try:
            self.v.simulador.iniciar(videos, puerto=self.sp_sim_puerto.value(), ancho=self.sp_ancho.value(),
                                     fps=self.sp_fps.value(), calidad=self.sp_calidad.value())
        except Exception as e:
            error = f"No se pudo iniciar el simulador: {e}"
            if not silencioso:
                QtWidgets.QMessageBox.warning(self, "Cámaras simuladas", error)
            self.refrescar_sim()
            return False, error
        self.refrescar_sim()
        return True, None

    def detener_sim(self):
        self.btn_sim_det.setEnabled(False)
        en_fondo(self.v.simulador.detener, lambda r, e: self.refrescar_sim())

    def refrescar_sim(self):
        sim = self.v.simulador
        try:
            activo = sim.activo
            urls = sim.urls() if activo else []
            log = sim.log()
        except Exception as e:
            activo, urls, log = False, [], [f"Error: {e}"]
        self.chip_sim.set(f"Activo · {len(urls)} cámara{'s' if len(urls) != 1 else ''}" if activo else "Detenido",
                          VERDE if activo else T3)
        self.btn_sim_ini.setEnabled(not activo)
        self.btn_sim_det.setEnabled(activo)
        for w in (self.sp_sim_puerto, self.sp_ancho, self.sp_fps, self.sp_calidad, *self.ed_videos):
            w.setEnabled(not activo)
        # mismo formato que CAMARAS en conteo4.py: lista de (nombre, dirección)
        texto = ("CAMARAS = [" + ", ".join(f'("Cam{k + 1}", "{u}")' for k, u in enumerate(urls)) + "]") if urls else ""
        if urls != self._urls_cmd:
            self._urls_cmd = list(urls)
            self.actualizar_comando()
        if self.ed_urls.text() != texto:
            self.ed_urls.setText(texto)
            self.ed_urls.setCursorPosition(0)
        if log != self._ultimo_log:
            self._ultimo_log = list(log)
            barra = self.log.verticalScrollBar()
            al_final = barra.value() >= barra.maximum() - 4
            self.log.setPlainText("\n".join(log))
            if al_final:
                barra.setValue(barra.maximum())

    def refrescar_estado(self, est, receptor_ok):
        self.f_receptor.set(f"escuchando en el puerto {self.v.puerto}" if receptor_ok else "reiniciando…",
                            color_punto=VERDE if receptor_ok else AMBAR)
        if est and est.get("pi_conectada"):
            self.f_pi.set(f"conectada desde {est.get('ip_pi') or '?'}", color_punto=VERDE)
        else:
            self.f_pi.set("esperando conexión…", color_punto=ROJO)


# ----------------------------------------------------------------------------- pestaña Control
ESTADOS_CONTEO = {"corriendo": ("Contando", VERDE), "arrancando": ("Arrancando…", AMBAR),
                  "deteniendo": ("Deteniendo…", AMBAR), "detenido": ("Detenido", T3),
                  "error": ("Con error", ROJO)}


def texto_throttled(valor):
    """vcgencmd get_throttled -> texto. bits 0-3 = ahora; 16-19 = pasó desde que encendió."""
    if not valor:
        return "–", None
    try:
        v = int(str(valor), 16)
    except ValueError:
        return str(valor), None
    if v == 0:
        return "Sin problemas", VERDE
    ahora = [t for b, t in ((0, "bajo voltaje"), (2, "frenada"), (3, "límite de calor")) if v >> b & 1]
    if ahora:
        return "Ahora: " + ", ".join(ahora), ROJO
    antes = [t for b, t in ((16, "bajo voltaje"), (18, "frenada"), (19, "calor")) if v >> b & 1]
    return "Hubo: " + ", ".join(antes), AMBAR


def duracion(seg):
    seg = int(max(seg, 0))
    d, h, m = seg // 86400, seg % 86400 // 3600, seg % 3600 // 60
    return f"{d} d {h} h" if d else f"{h} h {m} min" if h else f"{m} min"


class PestanaControl(QtWidgets.QWidget):
    """Encender/apagar el conteo de la Raspberry (vía raspberry/agente.py), ver su estado y su registro."""

    def __init__(self, ventana):
        super().__init__()
        self.v = ventana
        mono = QtGui.QFontDatabase.systemFont(QtGui.QFontDatabase.FixedFont)
        mono.setPointSizeF(9.5)
        raiz = QtWidgets.QHBoxLayout(self)
        raiz.setContentsMargins(0, 12, 0, 0)
        raiz.setSpacing(16)

        izq = QtWidgets.QVBoxLayout()
        izq.setSpacing(16)
        raiz.addLayout(izq, 2)

        # --- conteo
        t1, l1, _ = tarjeta("Conteo en la Raspberry")
        self.chip = Chip("–", con_punto=True)
        t1.cabecera.addWidget(self.chip)
        txt = etiqueta("Con el <b>agente</b> instalado en la Raspberry (<code>bash instalar_pi.sh</code>, una "
                       "sola vez) todo se maneja desde aquí o desde el dashboard web: no hace falta entrar a la Pi.",
                       "sub")
        txt.setWordWrap(True)
        l1.addWidget(txt)
        self.f_agente = FilaDato("Agente", con_punto=True)
        self.f_desde = FilaDato("Funcionando desde")
        self.f_camaras = FilaDato("Cámaras en uso")
        self.f_envio = FilaDato("Envía a")
        self.f_reinicios = FilaDato("Reinicios automáticos")
        for f in (self.f_agente, self.f_desde, self.f_camaras, self.f_envio, self.f_reinicios):
            l1.addWidget(f)
        self.f_camaras.val.setWordWrap(True)
        self.lbl_detalle = etiqueta("", "muted")
        self.lbl_detalle.setWordWrap(True)
        self.lbl_detalle.setTextInteractionFlags(Qt.TextSelectableByMouse)
        l1.addWidget(self.lbl_detalle)

        fila = QtWidgets.QHBoxLayout()
        fila.addWidget(etiqueta("Cámaras para el conteo", "sub"))
        self.cb_fuente = QtWidgets.QComboBox()
        self.cb_fuente.addItem("Simuladas de esta laptop (pestaña Conexión)", "sim")
        self.cb_fuente.addItem("Las configuradas en la Raspberry (CAMARAS de conteo4.py)", "pi")
        cfg = QtCore.QSettings("MonitorTrafico", "InterfazLaptop")
        i = self.cb_fuente.findData(cfg.value("fuente_camaras", "sim"))
        self.cb_fuente.setCurrentIndex(max(i, 0))
        self.cb_fuente.currentIndexChanged.connect(
            lambda _: QtCore.QSettings("MonitorTrafico", "InterfazLaptop").setValue("fuente_camaras", self.fuente()))
        fila.addWidget(self.cb_fuente, 1)
        l1.addLayout(fila)

        botones = QtWidgets.QHBoxLayout()
        self.btn_todo = QtWidgets.QPushButton("Iniciar todo")
        self.btn_todo.setProperty("rol", "primario")
        self.btn_todo.setToolTip("Enciende las cámaras simuladas (si las elegiste) y luego el conteo en la Pi")
        self.btn_todo.clicked.connect(self.v.iniciar_todo)
        self.btn_reiniciar = QtWidgets.QPushButton("Reiniciar conteo")
        self.btn_reiniciar.clicked.connect(lambda: self._orden("reiniciar"))
        self.btn_detener = QtWidgets.QPushButton("Detener conteo")
        self.btn_detener.clicked.connect(lambda: self._orden("detener"))
        for b in (self.btn_todo, self.btn_reiniciar, self.btn_detener):
            botones.addWidget(b)
        botones.addStretch(1)
        l1.addLayout(botones)
        self.lbl_resp = etiqueta("", "muted")
        self.lbl_resp.setWordWrap(True)
        l1.addWidget(self.lbl_resp)
        izq.addWidget(t1)

        # --- sistema
        t2, l2, _ = tarjeta("Estado de la Raspberry")
        self.lbl_host = etiqueta("", "muted")
        t2.cabecera.addWidget(self.lbl_host)
        rej = QtWidgets.QGridLayout()
        rej.setHorizontalSpacing(10)
        rej.setVerticalSpacing(10)
        self.m_temp, self.m_ram = MiniDato("Temperatura CPU"), MiniDato("RAM libre")
        self.m_carga, self.m_disco = MiniDato("Carga (1 min, 4 núcleos)"), MiniDato("Disco libre")
        self.m_up, self.m_energia = MiniDato("Encendida hace"), MiniDato("Energía y calor")
        for k, m in enumerate((self.m_temp, self.m_ram, self.m_carga, self.m_disco, self.m_up, self.m_energia)):
            rej.addWidget(m, k // 2, k % 2)
        l2.addLayout(rej)
        fb = QtWidgets.QHBoxLayout()
        fb.addStretch(1)
        self.btn_reboot = QtWidgets.QPushButton("Reiniciar Raspberry")
        self.btn_reboot.clicked.connect(lambda: self._apagar("reiniciar_pi"))
        self.btn_apagar = QtWidgets.QPushButton("Apagar Raspberry")
        self.btn_apagar.clicked.connect(lambda: self._apagar("apagar_pi"))
        fb.addWidget(self.btn_reboot)
        fb.addWidget(self.btn_apagar)
        l2.addLayout(fb)
        izq.addWidget(t2)
        izq.addStretch(1)

        # --- registro
        t3, l3, _ = tarjeta("Registro de la Raspberry")
        b_copiar = QtWidgets.QPushButton("Copiar")
        b_copiar.clicked.connect(lambda: QtWidgets.QApplication.clipboard().setText(self.log.toPlainText()))
        t3.cabecera.addWidget(b_copiar)
        self.log = QtWidgets.QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setFont(mono)
        self.log.setLineWrapMode(QtWidgets.QPlainTextEdit.NoWrap)
        self.log.setMaximumBlockCount(1000)
        self.log.setPlaceholderText("Aquí aparece lo que imprime conteo4_nube.py en la Raspberry "
                                    "(llega a través del agente).")
        l3.addWidget(self.log, 1)
        raiz.addWidget(t3, 3)

        self._n_log = -1
        self._n_resp = 0

    def fuente(self):
        return self.cb_fuente.currentData() or "sim"

    def _orden(self, accion, **extra):
        rec = self.v.receptor
        if rec is None or not rec.orden_agente(accion, **extra):
            self.v.sin_agente()
            return False
        self.lbl_resp.setText("Orden enviada…")
        return True

    def _apagar(self, accion):
        verbo = "reiniciar" if accion == "reiniciar_pi" else "apagar"
        extra = ("" if accion == "reiniciar_pi" else
                 "\n\nOjo: apagada, solo vuelve a encender si la desenchufas y la enchufas de nuevo.")
        r = QtWidgets.QMessageBox.question(self, "Raspberry", f"¿Seguro que quieres {verbo} la Raspberry?{extra}")
        if r == QtWidgets.QMessageBox.Yes:
            self._orden(accion)

    def refrescar(self, ag, visible):
        """ag = receptor.agente(con_log=visible) (o None si no hay receptor)."""
        ag = ag or {"conectado": False}
        est = ag.get("estado") or {}
        conectado = bool(ag.get("conectado"))
        conteo = est.get("conteo") if conectado else None
        txt, col = ESTADOS_CONTEO.get(conteo, ("Agente desconectado", ROJO))
        self.chip.set(txt, col)
        for b in (self.btn_reiniciar, self.btn_detener, self.btn_reboot, self.btn_apagar):
            b.setEnabled(conectado)
        resp = ag.get("respuestas") or []
        if len(resp) != self._n_resp:
            self._n_resp = len(resp)
            if resp:
                t, accion, ok, msg = resp[-1]
                hora = datetime.datetime.fromtimestamp(t).strftime("%H:%M:%S")
                self.lbl_resp.setText(f"<span style='color:{VERDE if ok else ROJO}'>{hora} · {msg}</span>")
        if not visible:
            return
        if conectado:
            self.f_agente.set(f"conectado desde {ag.get('ip') or '?'}", color_punto=VERDE)
        else:
            self.f_agente.set("no conectado (¿instalaste el agente con instalar_pi.sh?)", T3, ROJO)
        desde = est.get("desde")
        self.f_desde.set(f"{datetime.datetime.fromtimestamp(desde):%H:%M} ({duracion(time.time() - desde)})"
                         if (conectado and desde) else "–")
        cams = est.get("camaras")
        self.f_camaras.set("–" if not conectado else (", ".join(cams) if cams else "las de CAMARAS (conteo4.py)"))
        destinos = []
        if est.get("laptop"):
            destinos.append(f"laptop {est['laptop']}" + ("" if est.get("laptop_conectada") else " (sin conexión)"))
        if "nube_conectada" in est:
            destinos.append("AWS" + ("" if est.get("nube_conectada") else " (sin conexión)"))
        self.f_envio.set(" · ".join(destinos) if (conectado and destinos) else "–")
        self.f_reinicios.set(str(est.get("reinicios", "–")) if conectado else "–")
        detalle = est.get("detalle") or ""
        self.lbl_detalle.setText(detalle if conectado else "")

        si = est.get("sistema") or {}
        self.lbl_host.setText(si.get("host") or "")
        temp = si.get("temp")
        self.m_temp.set("–" if temp is None else f"{fmt_n(float(temp))} °C", ROJO if (temp or 0) >= 80 else None)
        if si.get("ram_libre_mb") is not None:
            libre = si["ram_libre_mb"]
            self.m_ram.set(f"{fmt_n(libre / 1024)} de {fmt_n(si.get('ram_total_mb', 0) / 1024)} GB",
                           ROJO if libre < 300 else None)
        else:
            self.m_ram.set("–")
        carga = si.get("carga")
        self.m_carga.set("–" if not carga else fmt_n(float(carga[0]), 2), AMBAR if carga and carga[0] > 4 else None)
        disco = si.get("disco_libre_gb")
        self.m_disco.set("–" if disco is None else f"{fmt_n(float(disco))} GB", ROJO if (disco or 99) < 1 else None)
        self.m_up.set("–" if si.get("encendida_s") is None else duracion(si["encendida_s"]))
        self.m_energia.set(*texto_throttled(si.get("throttled")))

        n = ag.get("n_log", 0)
        if "log" in ag and n != self._n_log:
            self._n_log = n
            barra = self.log.verticalScrollBar()
            al_final = barra.value() >= barra.maximum() - 4
            self.log.setPlainText("\n".join(ag["log"]))
            if al_final:
                barra.setValue(barra.maximum())


# ----------------------------------------------------------------------------- ventana
class Ventana(QtWidgets.QMainWindow):
    def __init__(self, puerto=8000, token="cambia-esto", retraso=1.0, falso=False, db=None):
        super().__init__()
        self.db = db
        self.puerto, self.token, self.retraso, self.falso = puerto, token, retraso, falso
        self.receptor = None
        self.simulador = nucleo.Simulador()
        self.video_activo = True
        self.colores = {}
        self.est = None
        self.hist1h = None
        self.flujo_hoy = None
        self._sim_error = None           # error del último intento remoto de encender las cámaras
        self._espera_sim = 0             # ticks esperando a que las cámaras estén listas

        self.setWindowTitle("Monitor de tráfico" + ("  (datos de prueba)" if falso else ""))
        self.resize(1440, 900)
        self.setMinimumSize(1100, 680)

        central = QtWidgets.QWidget()
        central.setObjectName("raiz")
        self.setCentralWidget(central)
        raiz = QtWidgets.QVBoxLayout(central)
        raiz.setContentsMargins(18, 14, 18, 10)
        raiz.setSpacing(6)

        # --- cabecera
        cab = QtWidgets.QHBoxLayout()
        tit = QtWidgets.QVBoxLayout()
        tit.setSpacing(0)
        tit.addWidget(etiqueta("Monitor de tráfico", "h1"))
        tit.addWidget(etiqueta("Conteo de vehículos con YOLO en Raspberry Pi 4" +
                               (" · datos de prueba (--falso)" if falso else ""), "sub"))
        cab.addLayout(tit)
        cab.addStretch(1)
        self.chip_pi = Chip("Iniciando…", con_punto=True)
        self.chip_act = Chip("Actualizado <b>–</b>")
        self.chip_cpu = Chip("CPU <b>–</b>")
        self.chip_yolo = Chip("YOLO <b>–</b>")
        self.chip_sem = Chip("Semáforo <b>–</b>", con_punto=True)
        self.chip_sem.hide()                 # aparece cuando la Pi manda el semáforo
        for c in (self.chip_pi, self.chip_sem, self.chip_act, self.chip_cpu, self.chip_yolo):
            cab.addWidget(c, 0, Qt.AlignVCenter)
        cab.addSpacing(8)
        self.btn_cab_todo = QtWidgets.QPushButton("Iniciar todo")
        self.btn_cab_todo.setProperty("rol", "primario")
        self.btn_cab_todo.setToolTip("Cámaras simuladas + conteo en la Raspberry (ver pestaña Control)")
        self.btn_cab_todo.clicked.connect(self.iniciar_todo)
        self.btn_cab_parar = QtWidgets.QPushButton("Detener todo")
        self.btn_cab_parar.setToolTip("Detiene el conteo en la Raspberry y las cámaras simuladas")
        self.btn_cab_parar.clicked.connect(self.detener_todo)
        cab.addWidget(self.btn_cab_todo, 0, Qt.AlignVCenter)
        cab.addWidget(self.btn_cab_parar, 0, Qt.AlignVCenter)
        raiz.addLayout(cab)

        # --- pestañas
        self.tabs = QtWidgets.QTabWidget()
        self.tabs.setDocumentMode(True)
        self.vivo = PestanaVivo(self)
        self.historial = PestanaHistorial(self)
        self.control = PestanaControl(self)
        self.conexion = PestanaConexion(self)
        self.tabs.addTab(self.vivo, "En vivo")
        self.tabs.addTab(self.historial, "Historial")
        self.tabs.addTab(self.control, "Control")
        self.tabs.addTab(self.conexion, "Conexión")
        # semáforo RL: pestaña oculta hasta que la Pi mande su estado
        self.pestana_sem = semaforo_ui.PestanaSemaforo() if semaforo_ui else None
        if self.pestana_sem is not None:
            self.tabs.insertTab(1, self.pestana_sem, "Semáforo")
            self.tabs.setTabVisible(1, False)
        self.tabs.currentChanged.connect(self._cambio_pestana)
        raiz.addWidget(self.tabs, 1)
        self.statusBar().setSizeGripEnabled(False)

        self._crear_receptor()

        # --- temporizadores
        self.t_video = QtCore.QTimer(self)
        self.t_video.setTimerType(Qt.PreciseTimer)
        self.t_video.timeout.connect(self._tic_video)
        self.t_video.start(33)
        self.t_estado = QtCore.QTimer(self)
        self.t_estado.timeout.connect(self._tic_estado)
        self.t_estado.start(1000)
        self.t_sem = QtCore.QTimer(self)
        self.t_sem.timeout.connect(self._tic_semaforo)
        self.t_sem.start(500)
        self.t_kpi = QtCore.QTimer(self)
        self.t_kpi.timeout.connect(self._tic_kpi)
        self.t_kpi.start(30000)
        self.t_hist = QtCore.QTimer(self)
        self.t_hist.timeout.connect(self.historial.pedir)
        self.t_hist.start(60000)
        QtCore.QTimer.singleShot(300, self._tic_estado)
        QtCore.QTimer.singleShot(800, self._tic_kpi)
        QtCore.QTimer.singleShot(1000, self.historial.pedir)

    # ---- receptor
    def _crear_receptor(self):
        try:
            extra = {"db_ruta": self.db} if self.db else {}
            rec = nucleo.Receptor(puerto=self.puerto, token=self.token, retraso=self.retraso, **extra)
            rec.iniciar()
            if not self.video_activo:
                rec.set_video(False)
            self.receptor = rec
        except Exception as e:
            self.receptor = None
            QtWidgets.QMessageBox.critical(
                self, "Receptor", f"No se pudo abrir el puerto {self.puerto}:\n{e}\n\n"
                                  "¿Hay otro programa usando ese puerto? Cámbialo en la pestaña Conexión.")
        self.conexion.actualizar_comando()

    def aplicar_ajustes(self, puerto, token, retraso):
        self.retraso = retraso
        if puerto == self.puerto and token == self.token and self.receptor is not None:
            self.receptor.set_retraso(retraso)
            self.statusBar().showMessage(f"Retraso de visualización: {fmt_n(retraso)} s", 5000)
            return
        viejo = self.receptor
        self.receptor = None
        self.puerto, self.token = puerto, token
        for rc in self.vivo.mosaico.recuadros:
            rc.limpiar("Reiniciando el receptor…")
        self.conexion.btn_aplicar.setEnabled(False)

        def listo(_r, _e):
            self._crear_receptor()
            self.conexion.btn_aplicar.setEnabled(True)
            self.statusBar().showMessage(f"Receptor reiniciado en el puerto {puerto}", 5000)
            self.historial.pedir()
            self._tic_kpi()
        if viejo is not None:
            en_fondo(viejo.detener, listo)
        else:
            listo(None, None)

    def alternar_video(self, pausado):
        self.video_activo = not pausado
        self.vivo.btn_video.setText("Reanudar video" if pausado else "Pausar video")
        if self.receptor is not None:
            try:
                self.receptor.set_video(self.video_activo)
            except Exception as e:
                print("set_video:", repr(e), file=sys.stderr)
        for rc in self.vivo.mosaico.recuadros:
            if pausado:
                rc.limpiar("Video pausado · la Raspberry deja de enviarlo")
            else:
                rc.set_aviso("Esperando video…")

    def color_cam(self, nombre):
        if nombre not in self.colores:
            self.colores[nombre] = SERIES[len(self.colores) % len(SERIES)]
        return self.colores[nombre]

    # ---- temporizadores
    def _cambio_pestana(self, i):
        w = self.tabs.widget(i)
        if w is self.historial and time.time() - self.historial.t_hist > 60:
            self.historial.pedir()
        if w is self.conexion:
            self.conexion.refrescar_sim()
        if w is self.control:
            self._tic_agente()

    # ---- control remoto (agente de la Raspberry)
    def sin_agente(self):
        QtWidgets.QMessageBox.information(
            self, "Raspberry",
            "El agente de la Raspberry no está conectado a esta laptop.\n\n"
            "• Instálalo una sola vez en la Pi:  cd ~/V_final_final/raspberry && bash instalar_pi.sh\n"
            "• Revisa que la Pi y la laptop estén en la misma red y que el firewall de Windows "
            "permita Python en redes privadas.\n\n"
            "Sin agente puedes seguir usando el comando manual de la pestaña Conexión.")

    def iniciar_todo(self):
        """Cámaras simuladas (si se eligieron) y luego el conteo en la Pi con esas cámaras."""
        rec = self.receptor
        if self.control.fuente() == "sim":
            ok, _ = self.conexion.iniciar_sim()
            if not ok:
                return
        if rec is None or not rec.agente()["conectado"]:
            self.sin_agente()
            return
        if self.control.fuente() == "sim":
            self.statusBar().showMessage("Esperando a que las cámaras simuladas estén listas…")
            self._espera_sim = 1
            self._esperar_sim()
        else:
            rec.orden_agente("iniciar", camaras=None)
            self.statusBar().showMessage("Orden enviada: iniciar el conteo con las cámaras de la Raspberry", 6000)

    def _esperar_sim(self):
        sim, rec = self.simulador, self.receptor
        if not self._espera_sim or rec is None:
            return
        if sim.listo:
            self._espera_sim = 0
            rec.orden_agente("iniciar", camaras=sim.urls())
            self.statusBar().showMessage("Cámaras listas · orden enviada a la Raspberry: iniciar el conteo", 8000)
        elif not sim.activo or self._espera_sim > 40:
            self._espera_sim = 0
            QtWidgets.QMessageBox.warning(self, "Cámaras simuladas", "Las cámaras simuladas no arrancaron:\n"
                                          + (sim.error or "revisa el registro en la pestaña Conexión"))
        else:
            self._espera_sim += 1
            QtCore.QTimer.singleShot(250, self._esperar_sim)

    def detener_todo(self):
        rec = self.receptor
        enviada = rec is not None and rec.orden_agente("detener")
        self._espera_sim = 0
        if self.simulador.activo:
            en_fondo(self.simulador.detener, lambda r, e: self.conexion.refrescar_sim())
        self.statusBar().showMessage("Deteniendo el conteo y las cámaras simuladas…" if enviada else
                                     "Cámaras simuladas detenidas (el agente de la Raspberry no está conectado)", 6000)

    def _tic_agente(self):
        """Órdenes para el simulador que llegan de la web (vía agente) + estado del agente."""
        rec = self.receptor
        if rec is None:
            self.control.refrescar(None, self.tabs.currentWidget() is self.control)
            return
        forzar = False
        for accion in rec.pedidos_sim():
            forzar = True
            if accion == "iniciar":
                ok, err = self.conexion.iniciar_sim(silencioso=True)
                self._sim_error = err
                self.statusBar().showMessage("Cámaras simuladas encendidas desde la web" if ok else
                                             f"La web pidió encender las cámaras: {err}", 8000)
            elif accion == "detener":
                self._sim_error = None
                en_fondo(self.simulador.detener, lambda r, e: self.conexion.refrescar_sim())
                self.statusBar().showMessage("Cámaras simuladas detenidas desde la web", 8000)
        sim = self.simulador
        activo = sim.activo
        rec.informar_sim(activo, sim.listo, sim.urls() if activo else [],
                         self._sim_error or (None if activo else sim.error), forzar=forzar)
        visible = self.tabs.currentWidget() is self.control
        try:
            ag = rec.agente(con_log=visible)
        except Exception as e:
            print("agente:", repr(e), file=sys.stderr)
            ag = None
        self.control.refrescar(ag, visible)

    def _tic_video(self):
        rec = self.receptor
        if rec is None or not self.video_activo or self.tabs.currentWidget() is not self.vivo or self.isMinimized():
            return
        for rc in self.vivo.mosaico.visibles():
            try:
                rc.actualizar(rec)
            except Exception as e:
                print("cuadro:", repr(e), file=sys.stderr)

    # ---- semáforo RL (semaforo_ui.py; se activa en la Pi: rl_zonas.json "interfaz")
    def _tic_semaforo(self):
        rec = self.receptor
        if self.pestana_sem is None or rec is None or not hasattr(rec, "semaforo"):
            return
        try:
            sem = rec.semaforo()
        except Exception as e:
            print("semaforo:", repr(e), file=sys.stderr)
            return
        i = self.tabs.indexOf(self.pestana_sem)
        visible = sem is not None
        if self.tabs.isTabVisible(i) != visible:
            self.tabs.setTabVisible(i, visible)
            self.chip_sem.setVisible(visible)
        if not visible:
            return
        texto, color, explicacion = semaforo_ui.resumen(sem)
        self.chip_sem.set(f"Semáforo <b>{texto}</b>", color)
        self.chip_sem.setToolTip(explicacion)
        if self.tabs.currentWidget() is self.pestana_sem and not self.isMinimized():
            try:
                self.pestana_sem.actualizar(sem)
            except Exception as e:
                print("semaforo (pestaña):", repr(e), file=sys.stderr)

    def _tic_estado(self):
        rec = self.receptor
        try:
            est = rec.estado() if rec is not None else None
        except Exception as e:
            print("estado:", repr(e), file=sys.stderr)
            est = None
        self.est = est
        ahora = time.time()
        pi_ok = bool(est and est.get("pi_conectada"))
        ultimo = (est or {}).get("ultimo") or None
        nombres = list((est or {}).get("camaras") or [])
        if not nombres and ultimo:
            nombres = [c.get("nombre", f"Cam{i + 1}") for i, c in enumerate(ultimo.get("camaras", []))]
        for n in nombres:
            self.color_cam(n)

        # cabecera
        if rec is None:
            self.chip_pi.set("Receptor reiniciando…", AMBAR)
        elif pi_ok:
            self.chip_pi.set("Raspberry en línea", VERDE)
        else:
            self.chip_pi.set("Raspberry desconectada", ROJO)
        t_ult = (est or {}).get("t_ultimo") or 0
        self.chip_act.set(f"Actualizado <b>{hace(ahora - t_ult) if t_ult else '–'}</b>")
        temp = ultimo.get("temp") if ultimo else None
        fps = ultimo.get("fps") if ultimo else None
        if temp is not None:
            col = ROJO if temp >= 80 else T1
            self.chip_cpu.set(f"CPU <b style='color:{col}'>{fmt_n(float(temp))} °C</b>")
        else:
            self.chip_cpu.set("CPU <b>–</b>")
        self.chip_yolo.set(f"YOLO <b>{fmt_n(float(fps))} inf/s</b>" if fps is not None else "YOLO <b>–</b>")

        # panel de estado
        v = self.vivo
        if pi_ok:
            txt_pi, col_pi = f"En línea · {est.get('ip_pi') or '?'}", VERDE
        else:
            txt_pi, col_pi = "Desconectada", ROJO
        v.punto_pi.set_color(col_pi)
        v.lbl_pi.setText(txt_pi)
        v.lbl_pi.setStyleSheet(f"color: {col_pi};")
        v.lbl_ultimo.setText(f"Último dato: {hace(ahora - t_ult)}" if t_ult else
                             f"Esperando que la Raspberry se conecte al puerto {self.puerto}…")
        if temp is not None:
            v.m_temp.set(f"{fmt_n(float(temp))} °C" + ("  ¡alta!" if temp >= 80 else ""),
                         ROJO if temp >= 80 else None)
        else:
            v.m_temp.set("–")
        v.m_yolo.set(f"{fmt_n(float(fps))} inf/s" if fps is not None else "–")
        kbps = float((est or {}).get("video_kbps") or 0)
        cps = float((est or {}).get("cuadros_por_s") or 0)
        if self.video_activo:
            v.m_kbps.set(f"{fmt_n(kbps, 0) if kbps >= 100 else fmt_n(kbps)} kbps")
            v.m_cps.set(f"{fmt_n(cps)} /s")
            ret = (est or {}).get("retraso")
            v.lbl_tasa.setText((f"{fmt_n(cps)} cuadros/s" + (f"  ·  retraso {fmt_n(float(ret))} s" if ret else ""))
                               if cps else "")
        else:
            v.m_kbps.set("en pausa", T3)
            v.m_cps.set("en pausa", T3)
            v.lbl_tasa.setText("")

        # KPIs ahora y categorías
        v.k_total.set(ultimo.get("total") if (ultimo and pi_ok) else None)
        suma = {}
        cams = (ultimo or {}).get("camaras") or []
        for c in cams:
            for k, n in (c.get("por_categoria") or {}).items():
                suma[k] = suma.get(k, 0) + (n or 0)
        v.barras.set(suma if pi_ok else {})

        # mosaico: número de cámaras y cabeceras
        n_cams = len(nombres) if nombres else 4
        v.mosaico.set_n(n_cams)
        por_nombre = {c.get("nombre"): c for c in cams}
        for i, rc in enumerate(v.mosaico.recuadros):
            nom = nombres[i] if i < len(nombres) else f"Cam{i + 1}"
            cam = por_nombre.get(nom) or (cams[i] if i < len(cams) else None)
            rc.set_info(nom, cam.get("total") if (cam and pi_ok) else None)
            if not self.video_activo:
                continue
            if rec is None:
                rc.set_aviso("Reiniciando el receptor…")
            elif not pi_ok:
                rc.set_aviso("Esperando a la Raspberry…")
            elif cam is not None and not cam.get("conectada", True):
                rc.set_aviso("Sin señal de la cámara")
            else:
                rc.set_aviso("Sin señal · esperando video…")

        # tarjetas por cámara
        for i, nom in enumerate(nombres):
            t = v.tarjeta_cam(nom)
            f1h = None
            if self.hist1h and nom in (self.hist1h.get("camaras") or {}):
                f1h = self.hist1h["camaras"][nom].get("flujo_total")
            t.set(por_nombre.get(nom), f1h, pi_ok)

        # pestaña conexión y control remoto
        self.conexion.refrescar_estado(est, rec is not None)
        if self.tabs.currentWidget() is self.conexion:
            self.conexion.refrescar_sim()
        self._tic_agente()

    def _tic_kpi(self):
        rec = self.receptor
        if rec is None:
            return

        def trabajo():
            h1 = rec.historial(1, 60)
            medianoche = datetime.datetime.combine(datetime.date.today(), datetime.time()).timestamp()
            horas_hoy = max((time.time() - medianoche) / 3600, 1 / 60)
            hoy = rec.historial(min(math.ceil(horas_hoy * 4) / 4 + 0.25, 24), 900)
            total = 0
            for c in (hoy.get("camaras") or {}).values():
                for t, val in zip(hoy.get("ts") or [], c.get("flujo") or []):
                    if val and t >= medianoche:
                        total += val
            return h1, total

        def listo(r, err):
            if err is not None:
                print("KPIs:", repr(err), file=sys.stderr)
                return
            self.hist1h, self.flujo_hoy = r
            self.vivo.k_hora.set(self.hist1h.get("flujo_total"))
            self.vivo.k_hoy.set(self.flujo_hoy)
        en_fondo(trabajo, listo)

    def closeEvent(self, ev):
        for t in (self.t_video, self.t_estado, self.t_kpi, self.t_hist):
            t.stop()
        self.statusBar().showMessage("Cerrando…")
        QtWidgets.QApplication.processEvents()
        for obj in (self.receptor, self.simulador):
            if obj is None:
                continue
            try:
                obj.detener()
            except Exception as e:
                print("al cerrar:", repr(e), file=sys.stderr)
        super().closeEvent(ev)


# ----------------------------------------------------------------------------- estilo
def _flechas():
    """Crea (una vez) las flechitas SVG de los QSpinBox en %TEMP% y devuelve sus rutas."""
    import tempfile
    carpeta = os.path.join(tempfile.gettempdir(), "monitor_trafico_ui")
    os.makedirs(carpeta, exist_ok=True)
    rutas = {}
    for nombre, puntos in (("arriba", "1,7 5,3 9,7"), ("abajo", "1,3 5,7 9,3")):
        ruta = os.path.join(carpeta, f"{nombre}.svg")
        if not os.path.exists(ruta):
            with open(ruta, "w", encoding="utf-8") as f:
                f.write(f'<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10" viewBox="0 0 10 10">'
                        f'<polyline points="{puntos}" fill="none" stroke="{T2}" stroke-width="1.6" '
                        f'stroke-linecap="round" stroke-linejoin="round"/></svg>')
        rutas[nombre] = ruta.replace("\\", "/")
    return rutas


def estilo_qss():
    fl = _flechas()
    return f"""
    QWidget {{ color: {T1}; }}
    QMainWindow, QWidget#raiz {{ background: {BG}; }}
    QWidget#transparente {{ background: transparent; }}
    QScrollArea {{ background: transparent; border: none; }}
    QScrollArea > QWidget > QWidget#transparente {{ background: transparent; }}
    QLabel {{ background: transparent; }}
    QLabel[rol="h1"] {{ font-size: 15pt; font-weight: 650; }}
    QLabel[rol="h2"] {{ font-size: 10.5pt; font-weight: 600; }}
    QLabel[rol="sub"] {{ color: {T2}; }}
    QLabel[rol="muted"] {{ color: {T3}; font-size: 8.5pt; }}
    QLabel[rol="valor"] {{ font-weight: 600; }}
    QLabel[rol="chip"] {{ color: {T2}; font-size: 9pt; }}
    QLabel[rol="kpi"] {{ font-size: 20pt; font-weight: 650; }}
    QLabel[rol="kpi-grande"] {{ font-size: 28pt; font-weight: 650; }}
    QLabel[rol="mini"] {{ font-size: 12pt; font-weight: 600; }}
    QLabel[rol="kpi-cam"] {{ font-size: 16pt; font-weight: 650; }}
    QLabel#aviso {{ color: {T2}; background: #26221a; border: 1px solid #4a3d22; border-radius: 8px;
                    padding: 9px 11px; }}
    QFrame#tarjeta {{ background: {S1}; border: 1px solid {BORDE}; border-radius: 12px; }}
    QFrame#kpi {{ background: {S2}; border: none; border-radius: 10px; }}
    QFrame#chip {{ background: {S1}; border: 1px solid {BORDE}; border-radius: 13px; }}
    QTabWidget::pane {{ border: none; border-top: 1px solid {BORDE}; }}
    QTabBar {{ background: transparent; }}
    QTabBar::tab {{ background: transparent; color: {T3}; padding: 8px 18px 9px 18px; border: none;
                    border-bottom: 2px solid transparent; font-weight: 600; font-size: 10.5pt; }}
    QTabBar::tab:selected {{ color: {T1}; border-bottom: 2px solid {T1}; }}
    QTabBar::tab:hover:!selected {{ color: {T2}; }}
    QPushButton {{ background: {S2}; color: {T1}; border: 1px solid {BORDE}; border-radius: 8px;
                   padding: 6px 12px; }}
    QPushButton:hover {{ border-color: {T3}; }}
    QPushButton:pressed {{ background: #2c2c29; }}
    QPushButton:checked {{ background: {T1}; color: {S1}; border-color: {T1}; }}
    QPushButton:disabled {{ color: #5d5c56; border-color: {BORDE}; }}
    QPushButton[rol="primario"] {{ background: #1f5a33; border-color: #2c7a46; }}
    QPushButton[rol="primario"]:hover {{ background: #246a3c; border-color: {VERDE}; }}
    QPushButton[rol="primario"]:disabled {{ background: {S2}; border-color: {BORDE}; color: #5d5c56; }}
    QComboBox {{ background: {BG}; color: {T1}; border: 1px solid {BORDE}; border-radius: 6px; padding: 5px 8px; }}
    QComboBox:focus {{ border-color: {T3}; }}
    QComboBox QAbstractItemView {{ background: {S1}; color: {T1}; border: 1px solid {BORDE};
                   selection-background-color: #2e3b4e; }}
    QLineEdit, QAbstractSpinBox, QPlainTextEdit {{ background: {BG}; color: {T1}; border: 1px solid {BORDE};
                   border-radius: 6px; padding: 5px 8px; selection-background-color: #3987e5; }}
    QAbstractSpinBox {{ padding-right: 24px; }}
    QAbstractSpinBox::up-button, QAbstractSpinBox::down-button {{ subcontrol-origin: border; width: 20px;
                   background: {S2}; border: none; border-left: 1px solid {BORDE}; }}
    QAbstractSpinBox::up-button {{ subcontrol-position: top right; border-top-right-radius: 6px; }}
    QAbstractSpinBox::down-button {{ subcontrol-position: bottom right; border-bottom-right-radius: 6px;
                   border-top: 1px solid {BORDE}; }}
    QAbstractSpinBox::up-button:hover, QAbstractSpinBox::down-button:hover {{ background: #33332f; }}
    QAbstractSpinBox::up-arrow {{ image: url("{fl['arriba']}"); width: 9px; height: 9px; }}
    QAbstractSpinBox::down-arrow {{ image: url("{fl['abajo']}"); width: 9px; height: 9px; }}
    QAbstractSpinBox::up-arrow:disabled, QAbstractSpinBox::down-arrow:disabled {{ image: none; }}
    QLineEdit:focus, QAbstractSpinBox:focus, QPlainTextEdit:focus {{ border-color: {T3}; }}
    QLineEdit:disabled, QAbstractSpinBox:disabled {{ color: {T3}; }}
    QLineEdit:read-only {{ color: {T2}; }}
    QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
    QScrollBar::handle:vertical {{ background: #3a3a37; border-radius: 3px; min-height: 30px; }}
    QScrollBar::handle:vertical:hover {{ background: #4a4a46; }}
    QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 2px; }}
    QScrollBar::handle:horizontal {{ background: #3a3a37; border-radius: 3px; min-width: 30px; }}
    QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
    QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}
    QTableWidget {{ background: {S1}; alternate-background-color: #1e1e1d; border: none;
                    gridline-color: {BORDE}; selection-background-color: #2e3b4e; selection-color: {T1}; }}
    QHeaderView::section {{ background: {S1}; color: {T2}; border: none; border-bottom: 1px solid {BORDE};
                            padding: 5px 10px; font-weight: 600; }}
    QTableCornerButton::section {{ background: {S1}; border: none; }}
    QToolTip {{ background: {S2}; color: {T1}; border: 1px solid {BORDE}; padding: 4px 6px; }}
    QStatusBar {{ color: {T2}; background: {BG}; }}
    QStatusBar::item {{ border: none; }}
    QMessageBox, QDialog {{ background: {S1}; }}
    """


def aplicar_tema(app):
    app.setStyle("Fusion")
    pal = QtGui.QPalette()
    for rol, col in ((QtGui.QPalette.Window, BG), (QtGui.QPalette.WindowText, T1), (QtGui.QPalette.Base, BG),
                     (QtGui.QPalette.AlternateBase, S1), (QtGui.QPalette.Text, T1), (QtGui.QPalette.Button, S2),
                     (QtGui.QPalette.ButtonText, T1), (QtGui.QPalette.ToolTipBase, S2),
                     (QtGui.QPalette.ToolTipText, T1), (QtGui.QPalette.Highlight, "#3987e5"),
                     (QtGui.QPalette.HighlightedText, "#ffffff"), (QtGui.QPalette.PlaceholderText, T3),
                     (QtGui.QPalette.Link, "#6aa8f0")):
        pal.setColor(rol, QtGui.QColor(col))
    pal.setColor(QtGui.QPalette.Disabled, QtGui.QPalette.Text, QtGui.QColor(T3))
    pal.setColor(QtGui.QPalette.Disabled, QtGui.QPalette.ButtonText, QtGui.QColor(T3))
    app.setPalette(pal)
    f = app.font()
    f.setPointSizeF(10)
    app.setFont(f)
    app.setStyleSheet(estilo_qss())
    pg.setConfigOptions(antialias=True, background=S1, foreground=T3)


def cargar_nucleo(falso):
    global nucleo
    if AQUI not in sys.path:
        sys.path.insert(0, AQUI)
    if falso:
        import _nucleo_falso as mod
    else:
        import nucleo as mod
    nucleo = mod
    return mod


def argumentos(argv=None):
    ap = argparse.ArgumentParser(description="Monitor de tráfico (programa de escritorio de la laptop)")
    ap.add_argument("--puerto", type=int, default=8000, help="puerto donde se conecta la Raspberry (8000)")
    ap.add_argument("--token", default="cambia-esto", help="token que debe usar la Raspberry")
    ap.add_argument("--retraso", type=float, default=1.0, help="retraso de visualización del video en s (1.0)")
    ap.add_argument("--falso", action="store_true", help="usar datos sintéticos (sin Raspberry)")
    ap.add_argument("--db", default=None, help="ruta de la base SQLite del historial (por defecto, la del núcleo)")
    return ap.parse_args(argv)


def crear_app(argv=None):
    """Crea QApplication y la ventana (también lo usan los scripts de prueba)."""
    global _puente
    args = argumentos(argv)
    cargar_nucleo(args.falso)
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv[:1])
    app.setApplicationName("Monitor de tráfico")
    aplicar_tema(app)
    _puente = _Puente()
    ventana = Ventana(puerto=args.puerto, token=args.token, retraso=args.retraso, falso=args.falso, db=args.db)
    return app, ventana


def main():
    app, ventana = crear_app()
    ventana.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
