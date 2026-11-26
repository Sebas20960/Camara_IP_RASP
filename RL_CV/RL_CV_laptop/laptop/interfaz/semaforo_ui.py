# =============================================================================
# semaforo_ui.py  -  Pestaña "Semáforo" del Monitor de tráfico (app.py).
#
# Muestra en vivo el semáforo con RL que corre en la Raspberry (semaforo/principal_pi.py):
# quién controla (agente RL / ciclo fijo), las luces de cada calle en un dibujo del cruce,
# los conteos que ve el agente, su última decisión con el valor Q de cada fase, las
# últimas decisiones y una línea de tiempo de las fases.
#
# Los datos llegan por la conexión de siempre (mensaje 'semaforo', ver nucleo.semaforo()).
# Se activa o desactiva en la Raspberry: raspberry/rl_zonas.json -> "interfaz": true/false.
# Si la Pi no manda el estado, la pestaña no aparece.
# =============================================================================
import datetime
import time

from PySide6 import QtCore, QtGui, QtWidgets
from PySide6.QtCore import Qt

# ---- mismo tema que app.py
BG = "#111110"
S1 = "#1a1a19"
S2 = "#232321"
BORDE = "#2e2e2b"
T1 = "#f0efec"
T2 = "#c3c2b7"
T3 = "#8f8e86"
VERDE = "#3fb75e"
ROJO = "#f06a6a"
AMBAR = "#e0a030"
AZUL = "#3987e5"
APAGADO = "#2a2a28"
ASFALTO = "#262624"
# un color por fase (orden de config.json)
COLORES_FASE = ["#3987e5", "#d95926", "#16a394", "#4fb3d9", "#8e6ad8", "#d1497b"]

# nombre de cada calle (contrato: N/S = Universitaria, E = Dulanto, O = La Mar)
CALLES = ("N", "S", "E", "O")
NOMBRE_CALLE = {"N": "Norte", "S": "Sur", "E": "Este", "O": "Oeste"}
VIA = {"N": "Av. Universitaria", "S": "Av. Universitaria", "E": "Dulanto", "O": "La Mar"}

CONTROL = {
    "agente": ("Agente RL", VERDE, "El modelo DDQN decide qué fase va en verde."),
    "recuperando": ("Recuperando", AZUL, "Volvieron los conteos: el agente retoma el control en unos segundos."),
    "respaldo": ("Ciclo fijo", AMBAR, "Respaldo: no llegan conteos de YOLO, el semáforo usa su plan fijo."),
}
VENTANA_LINEA_S = 15 * 60


def resumen(sem):
    """(texto corto, color, explicación) del estado general. sem = receptor.semaforo()."""
    if sem is None:
        return "Sin datos", T3, "Todavía no llega información del semáforo."
    d = sem.get("datos") or {}
    if not sem.get("pi_conectada"):
        return "Sin Raspberry", ROJO, "La Raspberry no está conectada: no se sabe qué hace el semáforo."
    if not d.get("conectado"):
        return "Desconectado", ROJO, ("El semáforo no está conectado al conteo"
                                      + (f" ({d.get('motivo')})." if d.get("motivo") else "."))
    if sem.get("edad", 0) > 6:
        return "Sin datos", ROJO, f"Hace {int(sem['edad'])} s que no llega el estado del semáforo."
    est = d.get("estado")
    if not est:
        return "Conectado", T3, "Conectado, pero sin detalle: en la Pi pon enviar_luces = true en semaforo/pi.json."
    return CONTROL.get(est.get("control"), (str(est.get("control")), T3, ""))


def _color_fase(fid, ids):
    try:
        return COLORES_FASE[ids.index(fid) % len(COLORES_FASE)]
    except ValueError:
        return T3


def _etiqueta(texto, rol=None):
    lbl = QtWidgets.QLabel(texto)
    if rol:
        lbl.setProperty("rol", rol)
    return lbl


def _tarjeta(titulo):
    f = QtWidgets.QFrame()
    f.setObjectName("tarjeta")
    lay = QtWidgets.QVBoxLayout(f)
    lay.setContentsMargins(14, 12, 14, 14)
    lay.setSpacing(10)
    cab = QtWidgets.QHBoxLayout()
    cab.setSpacing(8)
    cab.addWidget(_etiqueta(titulo, "h2"))
    cab.addStretch(1)
    lay.addLayout(cab)
    f.cabecera = cab
    return f, lay


# ----------------------------------------------------------------------------- piezas
class Dato(QtWidgets.QFrame):
    """Tarjeta pequeña: título, valor grande (con punto de color opcional) y detalle."""

    def __init__(self, titulo, con_punto=False, con_barra=False):
        super().__init__()
        self.setObjectName("kpi")
        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(14, 10, 14, 11)
        lay.setSpacing(2)
        lay.addWidget(_etiqueta(titulo, "sub"))
        fila = QtWidgets.QHBoxLayout()
        fila.setSpacing(8)
        self.punto = None
        if con_punto:
            self.punto = QtWidgets.QLabel()
            self.punto.setFixedSize(10, 10)
            fila.addWidget(self.punto, 0, Qt.AlignVCenter)
        self.val = QtWidgets.QLabel("–")
        self.val.setProperty("rol", "kpi-sem")
        fila.addWidget(self.val)
        fila.addStretch(1)
        lay.addLayout(fila)
        self.barra = Progreso() if con_barra else None
        if self.barra:
            lay.addWidget(self.barra)
        self.det = QtWidgets.QLabel("")
        self.det.setProperty("rol", "muted")
        self.det.setWordWrap(True)
        lay.addWidget(self.det)
        lay.addStretch(1)

    def set(self, valor, detalle="", color=None, color_punto=None):
        if self.val.text() != valor:
            self.val.setText(valor)
        estilo = f"color: {color};" if color else ""
        if self.val.styleSheet() != estilo:
            self.val.setStyleSheet(estilo)
        if self.det.text() != detalle:
            self.det.setText(detalle)
        if self.punto is not None:
            e = f"background: {color_punto or T3}; border-radius: 5px;"
            if self.punto.styleSheet() != e:
                self.punto.setStyleSheet(e)


class Progreso(QtWidgets.QWidget):
    def __init__(self):
        super().__init__()
        self.setFixedHeight(6)
        self.frac, self.color = 0.0, VERDE

    def set(self, frac, color=VERDE):
        frac = max(0.0, min(1.0, float(frac)))
        if abs(frac - self.frac) > 1e-3 or color != self.color:
            self.frac, self.color = frac, color
            self.update()

    def paintEvent(self, _):
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.Antialiasing)
        p.setPen(Qt.NoPen)
        r = QtCore.QRectF(self.rect())
        p.setBrush(QtGui.QColor(BORDE))
        p.drawRoundedRect(r, 3, 3)
        if self.frac > 0:
            p.setBrush(QtGui.QColor(self.color))
            p.drawRoundedRect(QtCore.QRectF(0, 0, max(6.0, r.width() * self.frac), r.height()), 3, 3)
        p.end()


class Cruce(QtWidgets.QWidget):
    """Dibujo del cruce visto desde arriba (norte arriba), con un semáforo por calle."""

    def __init__(self):
        super().__init__()
        self.setMinimumSize(460, 300)
        self.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Expanding)
        self.luces = {}
        self.info = {}          # calle -> (cámara, conteo, rojo_s)
        self.aviso = "Esperando datos del semáforo…"
        self.atenuado = True

    def set(self, luces, info, aviso, atenuado):
        self.luces, self.info, self.aviso, self.atenuado = dict(luces or {}), info, aviso, atenuado
        self.update()

    def paintEvent(self, _):
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.Antialiasing)
        w, h = self.width(), self.height()
        cx, cy = w / 2, h / 2
        a = max(34.0, min(w, h) * 0.12)                 # medio ancho de la vía
        p.fillRect(self.rect(), QtGui.QColor(S1))

        # vías
        asf = QtGui.QColor(ASFALTO)
        p.setPen(Qt.NoPen)
        p.setBrush(asf)
        p.drawRect(QtCore.QRectF(0, cy - a, w, 2 * a))
        p.drawRect(QtCore.QRectF(cx - a, 0, 2 * a, h))
        # bordes de vereda
        p.setPen(QtGui.QPen(QtGui.QColor("#3a3a37"), 1.2))
        for x0, x1 in ((0, cx - a), (cx + a, w)):
            p.drawLine(QtCore.QPointF(x0, cy - a), QtCore.QPointF(x1, cy - a))
            p.drawLine(QtCore.QPointF(x0, cy + a), QtCore.QPointF(x1, cy + a))
        for y0, y1 in ((0, cy - a), (cy + a, h)):
            p.drawLine(QtCore.QPointF(cx - a, y0), QtCore.QPointF(cx - a, y1))
            p.drawLine(QtCore.QPointF(cx + a, y0), QtCore.QPointF(cx + a, y1))
        # eje central discontinuo
        pen = QtGui.QPen(QtGui.QColor("#5a5953"), 1.6)
        pen.setDashPattern([6, 6])
        p.setPen(pen)
        p.drawLine(QtCore.QPointF(0, cy), QtCore.QPointF(cx - a - 16, cy))
        p.drawLine(QtCore.QPointF(cx + a + 16, cy), QtCore.QPointF(w, cy))
        p.drawLine(QtCore.QPointF(cx, 0), QtCore.QPointF(cx, cy - a - 16))
        p.drawLine(QtCore.QPointF(cx, cy + a + 16), QtCore.QPointF(cx, h))
        # pasos peatonales (cebra)
        p.setPen(Qt.NoPen)
        p.setBrush(QtGui.QColor("#3b3b38"))
        n_barras = 7
        paso = 2 * a / n_barras
        for i in range(n_barras):
            t = -a + i * paso + paso * 0.25
            p.drawRect(QtCore.QRectF(cx + t, cy - a - 14, paso * 0.5, 10))
            p.drawRect(QtCore.QRectF(cx + t, cy + a + 4, paso * 0.5, 10))
            p.drawRect(QtCore.QRectF(cx - a - 14, cy + t, 10, paso * 0.5))
            p.drawRect(QtCore.QRectF(cx + a + 4, cy + t, 10, paso * 0.5))

        # carril de entrada de cada calle (circulación por la derecha) y su línea de parada
        entrada = {"N": QtCore.QRectF(cx - a, 0, a, cy - a - 16),
                   "S": QtCore.QRectF(cx, cy + a + 16, a, h - cy - a - 16),
                   "E": QtCore.QRectF(cx + a + 16, cy - a, w - cx - a - 16, a),
                   "O": QtCore.QRectF(0, cy, cx - a - 16, a)}
        parada = {"N": (QtCore.QPointF(cx - a, cy - a - 18), QtCore.QPointF(cx, cy - a - 18)),
                  "S": (QtCore.QPointF(cx, cy + a + 18), QtCore.QPointF(cx + a, cy + a + 18)),
                  "E": (QtCore.QPointF(cx + a + 18, cy - a), QtCore.QPointF(cx + a + 18, cy)),
                  "O": (QtCore.QPointF(cx - a - 18, cy), QtCore.QPointF(cx - a - 18, cy + a))}
        for c in CALLES:
            luz = self.luces.get(c)
            if luz in ("V", "A") and not self.atenuado:
                col = QtGui.QColor(VERDE if luz == "V" else AMBAR)
                col.setAlpha(38)
                p.setPen(Qt.NoPen)
                p.setBrush(col)
                p.drawRect(entrada[c])
                self._flecha(p, c, entrada[c], VERDE if luz == "V" else AMBAR)
            col_parada = {"V": VERDE, "A": AMBAR, "R": ROJO}.get(luz, T3) if not self.atenuado else T3
            p.setPen(QtGui.QPen(QtGui.QColor(col_parada), 3, Qt.SolidLine, Qt.FlatCap))
            p.drawLine(*parada[c])

        # semáforos y rótulos en las cuatro esquinas (junto a la entrada de cada calle)
        m = 18
        esquinas = {"N": QtCore.QRectF(m, m, cx - a - 2 * m, cy - a - 2 * m),
                    "E": QtCore.QRectF(cx + a + m, m, w - cx - a - 2 * m, cy - a - 2 * m),
                    "S": QtCore.QRectF(cx + a + m, cy + a + m, w - cx - a - 2 * m, h - cy - a - 2 * m),
                    "O": QtCore.QRectF(m, cy + a + m, cx - a - 2 * m, h - cy - a - 2 * m)}
        for c in CALLES:
            self._rotulo(p, c, esquinas[c])

        if self.aviso:
            f = p.font()
            f.setPointSizeF(10)
            f.setWeight(QtGui.QFont.DemiBold)
            p.setFont(f)
            fm = QtGui.QFontMetricsF(f)
            ancho = min(w - 40, fm.horizontalAdvance(self.aviso) + 36)
            r = QtCore.QRectF(cx - ancho / 2, cy - 20, ancho, 40)
            p.setPen(QtGui.QPen(QtGui.QColor(BORDE), 1))
            p.setBrush(QtGui.QColor(20, 20, 19, 235))
            p.drawRoundedRect(r, 10, 10)
            p.setPen(QtGui.QColor(T2))
            p.drawText(r, Qt.AlignCenter, fm.elidedText(self.aviso, Qt.ElideRight, ancho - 24))
        p.end()

    def _flecha(self, p, c, r, color):
        """Flecha en el sentido del tránsito que tiene paso."""
        col = QtGui.QColor(color)
        col.setAlpha(150)
        p.setPen(Qt.NoPen)
        p.setBrush(col)
        L, A = 26.0, 9.0
        if c == "N":
            x, y, d = r.center().x(), r.bottom() - 24, (0, 1)
        elif c == "S":
            x, y, d = r.center().x(), r.top() + 24, (0, -1)
        elif c == "E":
            x, y, d = r.left() + 24, r.center().y(), (-1, 0)
        else:
            x, y, d = r.right() - 24, r.center().y(), (1, 0)
        dx, dy = d
        px, py = -dy, dx
        punta = QtCore.QPointF(x + dx * L / 2, y + dy * L / 2)
        base = QtCore.QPointF(x - dx * L / 2, y - dy * L / 2)
        cuerpo = QtGui.QPolygonF([
            QtCore.QPointF(base.x() + px * 3, base.y() + py * 3),
            QtCore.QPointF(punta.x() - dx * 10 + px * 3, punta.y() - dy * 10 + py * 3),
            QtCore.QPointF(punta.x() - dx * 10 + px * A, punta.y() - dy * 10 + py * A),
            punta,
            QtCore.QPointF(punta.x() - dx * 10 - px * A, punta.y() - dy * 10 - py * A),
            QtCore.QPointF(punta.x() - dx * 10 - px * 3, punta.y() - dy * 10 - py * 3),
            QtCore.QPointF(base.x() - px * 3, base.y() - py * 3)])
        p.drawPolygon(cuerpo)

    def _rotulo(self, p, c, r):
        luz = None if self.atenuado else self.luces.get(c)
        cam, conteo, rojo = self.info.get(c, ("", None, None))
        # semáforo (3 luces en horizontal), en la esquina del cuadrante junto a la línea de parada;
        # el texto va al lado, hacia afuera
        d = 18.0
        cab = QtCore.QRectF(0, 0, 3 * d + 4 * 7, d + 14)
        derecha = c in ("E", "S")          # cuadrante a la derecha del cruce
        arriba = c in ("N", "E")           # cuadrante arriba del cruce
        cab.moveTo(r.left() if derecha else r.right() - cab.width(),
                   r.bottom() - cab.height() if arriba else r.top())
        p.setPen(QtGui.QPen(QtGui.QColor("#3a3a37"), 1))
        p.setBrush(QtGui.QColor("#0c0c0b"))
        p.drawRoundedRect(cab, 9, 9)
        for j, letra in enumerate("RAV"):
            centro = QtCore.QPointF(cab.left() + 7 + d / 2 + j * (d + 7), cab.center().y())
            if luz == letra:
                base = QtGui.QColor({"R": ROJO, "A": AMBAR, "V": VERDE}[letra])
                halo = QtGui.QRadialGradient(centro, d)
                c1 = QtGui.QColor(base)
                c1.setAlpha(110)
                c2 = QtGui.QColor(base)
                c2.setAlpha(0)
                halo.setColorAt(0, c1)
                halo.setColorAt(1, c2)
                p.setPen(Qt.NoPen)
                p.setBrush(halo)
                p.drawEllipse(centro, d, d)
                p.setBrush(base)
            else:
                p.setPen(Qt.NoPen)
                p.setBrush(QtGui.QColor(APAGADO))
            p.drawEllipse(centro, d / 2, d / 2)

        # texto: calle, vía, cámara · vehículos, tiempo en rojo
        f = p.font()
        alto = 0
        lineas = []
        f1 = QtGui.QFont(f)
        f1.setPointSizeF(11.5)
        f1.setWeight(QtGui.QFont.DemiBold)
        lineas.append((f"{c} · {NOMBRE_CALLE[c]}", f1, T1))
        f2 = QtGui.QFont(f)
        f2.setPointSizeF(9)
        lineas.append((VIA[c] + (f"  ·  {cam}" if cam else ""), f2, T3))
        f3 = QtGui.QFont(f)
        f3.setPointSizeF(9.5)
        if conteo is not None:
            lineas.append((f"{conteo:g} vehículos en cola", f3, T2))
        if luz == "R" and rojo:
            lineas.append((f"en rojo hace {rojo} s", f3, ROJO))
        elif luz == "V":
            lineas.append(("con paso", f3, VERDE))
        elif luz == "A":
            lineas.append(("ámbar", f3, AMBAR))
        alturas = [QtGui.QFontMetricsF(fl).height() for _, fl, _ in lineas]
        alto = sum(alturas) + 2 * (len(lineas) - 1)
        y = r.bottom() - alto if arriba else r.top()
        y = max(r.top(), min(y, r.bottom() - alto)) if alto <= r.height() else r.top()
        sep = 14
        if derecha:
            caja_x, caja_w = cab.right() + sep, r.right() - cab.right() - sep
        else:
            caja_x, caja_w = r.left(), cab.left() - sep - r.left()
        for (texto, fl, col), hh in zip(lineas, alturas):
            p.setFont(fl)
            p.setPen(QtGui.QColor(col))
            fm = QtGui.QFontMetricsF(fl)
            caja = QtCore.QRectF(caja_x, y, max(10.0, caja_w), hh)
            p.drawText(caja, (Qt.AlignLeft if derecha else Qt.AlignRight) | Qt.AlignVCenter,
                       fm.elidedText(texto, Qt.ElideRight, max(10.0, caja_w)))
            y += hh + 2
        p.setFont(f)


class BarrasQ(QtWidgets.QWidget):
    """Valor Q de cada fase en la última decisión (más alto = el agente la prefiere)."""
    FILA = 25

    def __init__(self):
        super().__init__()
        self.filas = []          # (nombre, q, color, permitida, elegida, pedida_anulada)
        self.vacio = "Aún no hay decisiones"
        self.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Fixed)
        self.setFixedHeight(6 * self.FILA)

    def set(self, filas):
        if filas != self.filas:
            self.filas = filas
            self.setFixedHeight(max(1, len(filas)) * self.FILA)
            self.update()

    def paintEvent(self, _):
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.Antialiasing)
        w = self.width()
        if not self.filas:
            p.setPen(QtGui.QColor(T3))
            p.drawText(self.rect(), Qt.AlignCenter, self.vacio)
            p.end()
            return
        qs = [f[1] for f in self.filas]
        lo, hi = min(qs), max(qs)
        col_nom, col_val, col_tag = 112, 52, 96
        x0 = col_nom
        ancho = max(20, w - col_nom - col_val - col_tag - 12)
        fm = QtGui.QFontMetricsF(p.font())
        for i, (nombre, q, color, permitida, elegida, anulada) in enumerate(self.filas):
            y = i * self.FILA
            cy = y + self.FILA / 2
            p.setPen(Qt.NoPen)
            p.setBrush(QtGui.QColor(color))
            p.drawRoundedRect(QtCore.QRectF(0, cy - 5, 4, 10), 2, 2)
            f = p.font()
            f.setWeight(QtGui.QFont.DemiBold if elegida else QtGui.QFont.Normal)
            p.setFont(f)
            p.setPen(QtGui.QColor(T1 if elegida else (T2 if permitida else T3)))
            p.drawText(QtCore.QRectF(12, y, col_nom - 14, self.FILA), Qt.AlignVCenter | Qt.AlignLeft,
                       fm.elidedText(nombre, Qt.ElideRight, col_nom - 14))
            p.setPen(Qt.NoPen)
            p.setBrush(QtGui.QColor(S2))
            p.drawRoundedRect(QtCore.QRectF(x0, cy - 4, ancho, 8), 4, 4)
            frac = (q - lo) / (hi - lo) if hi > lo else 1.0
            barra = QtGui.QColor(VERDE if elegida else (AMBAR if anulada else ("#6b6a64" if permitida else "#3a3a37")))
            p.setBrush(barra)
            p.drawRoundedRect(QtCore.QRectF(x0, cy - 4, max(8.0, ancho * frac), 8), 4, 4)
            f.setWeight(QtGui.QFont.Normal)
            p.setFont(f)
            p.setPen(QtGui.QColor(T2 if permitida else T3))
            p.drawText(QtCore.QRectF(x0 + ancho + 6, y, col_val, self.FILA), Qt.AlignVCenter | Qt.AlignRight,
                       f"{q:.2f}".replace(".", ","))
            tag, col_t = None, None
            if elegida:
                tag, col_t = "elegida", VERDE
            elif anulada:
                tag, col_t = "pedida · anulada", AMBAR
            elif not permitida:
                tag, col_t = "no permitida", T3
            if tag:
                f2 = QtGui.QFont(f)
                f2.setPointSizeF(8.5)
                p.setFont(f2)
                fm2 = QtGui.QFontMetricsF(f2)
                tw = fm2.horizontalAdvance(tag) + 14
                rt = QtCore.QRectF(w - tw, cy - 10, tw, 20)
                fondo = QtGui.QColor(col_t)
                fondo.setAlpha(34)
                p.setPen(Qt.NoPen)
                p.setBrush(fondo)
                p.drawRoundedRect(rt, 10, 10)
                p.setPen(QtGui.QColor(col_t))
                p.drawText(rt, Qt.AlignCenter, tag)
                p.setFont(f)
        p.end()


class LineaTiempo(QtWidgets.QWidget):
    """Fase en verde y modo de control de los últimos 15 min."""

    def __init__(self):
        super().__init__()
        self.setFixedHeight(92)
        self.historia, self.fases, self.ids = [], {}, []

    def set(self, historia, fases):
        self.historia, self.fases = historia, fases
        self.ids = list(fases)
        self.update()

    def paintEvent(self, _):
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.Antialiasing, False)
        w = self.width()
        ahora = time.time()
        t0 = ahora - VENTANA_LINEA_S
        izq, der = 92, 8
        ancho = max(10, w - izq - der)

        def x_de(t):
            return izq + (t - t0) / VENTANA_LINEA_S * ancho

        filas = (("Fase en verde", 6, 28), ("Control", 42, 12))
        f = p.font()
        f.setPointSizeF(8.8)
        p.setFont(f)
        for nombre, y, alto in filas:
            p.setPen(QtGui.QColor(T3))
            p.drawText(QtCore.QRectF(0, y, izq - 10, alto), Qt.AlignVCenter | Qt.AlignRight, nombre)
            p.fillRect(QtCore.QRectF(izq, y, ancho, alto), QtGui.QColor(S2))
        # tramos: cada registro dura hasta el siguiente (máx. 3 s si hay un hueco)
        hist = [h for h in self.historia if h[0] >= t0 - 3]
        for i, (t, fase, control, etapa) in enumerate(hist):
            t_fin = hist[i + 1][0] if i + 1 < len(hist) else ahora
            t_fin = min(t_fin, t + 3)
            xa, xb = max(izq, x_de(t)), min(izq + ancho, x_de(t_fin))
            if xb <= xa:
                continue
            col = QtGui.QColor(_color_fase(fase, self.ids))
            if etapa != "verde":
                col = QtGui.QColor(AMBAR)
                col.setAlpha(150)
            p.fillRect(QtCore.QRectF(xa, 6, xb - xa + 0.6, 28), col)
            cc = QtGui.QColor({"agente": VERDE, "recuperando": AZUL, "respaldo": AMBAR}.get(control, T3))
            p.fillRect(QtCore.QRectF(xa, 42, xb - xa + 0.6, 12), cc)
        # nombre de la fase dentro de los tramos largos
        p.setPen(QtGui.QColor("#ffffff"))
        inicio = None
        for i in range(len(hist) + 1):
            fin_tramo = i == len(hist) or (inicio is not None and (hist[i][1], hist[i][3]) != (hist[inicio][1], hist[inicio][3]))
            if fin_tramo and inicio is not None:
                ta = hist[inicio][0]
                tb = hist[i][0] if i < len(hist) else ahora
                xa, xb = max(izq, x_de(ta)), min(izq + ancho, x_de(tb))
                if hist[inicio][3] == "verde" and xb - xa > 60:
                    nombre = (self.fases.get(hist[inicio][1]) or {}).get("nombre", "")
                    p.drawText(QtCore.QRectF(xa + 4, 6, xb - xa - 8, 28), Qt.AlignVCenter | Qt.AlignLeft,
                               QtGui.QFontMetricsF(f).elidedText(nombre, Qt.ElideRight, xb - xa - 8))
                inicio = None
            if i < len(hist) and inicio is None:
                inicio = i
        # eje de tiempo
        p.setPen(QtGui.QColor(T3))
        paso = 5 * 60
        t = (int(t0) // paso + 1) * paso
        while t <= ahora:
            x = x_de(t)
            if x > izq + ancho - 70:                 # no pisar "ahora"
                break
            p.drawLine(QtCore.QPointF(x, 58), QtCore.QPointF(x, 62))
            p.drawText(QtCore.QRectF(x - 30, 62, 60, 16), Qt.AlignCenter,
                       datetime.datetime.fromtimestamp(t).strftime("%H:%M"))
            t += paso
        p.drawText(QtCore.QRectF(izq + ancho - 60, 62, 60, 16), Qt.AlignVCenter | Qt.AlignRight, "ahora")
        if not hist:
            p.drawText(QtCore.QRectF(izq, 6, ancho, 28), Qt.AlignCenter, "Se llena a medida que llegan datos")
        p.end()


class Leyenda(QtWidgets.QWidget):
    """Dos filas: colores de las fases y colores del modo de control."""

    def __init__(self):
        super().__init__()
        self.lay = QtWidgets.QVBoxLayout(self)
        self.lay.setContentsMargins(0, 0, 0, 0)
        self.lay.setSpacing(4)
        self._clave = None

    @staticmethod
    def _fila(items):
        w = QtWidgets.QWidget()
        lay = QtWidgets.QHBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)
        for col, nombre in items:
            muestra = QtWidgets.QLabel()
            muestra.setFixedSize(10, 10)
            muestra.setStyleSheet(f"background: {col}; border-radius: 2px;")
            lay.addWidget(muestra)
            lay.addWidget(_etiqueta(nombre, "muted"))
            lay.addSpacing(8)
        lay.addStretch(1)
        return w

    def set(self, fases):
        clave = tuple((fid, f.get("nombre")) for fid, f in fases.items())
        if clave == self._clave:
            return
        self._clave = clave
        while self.lay.count():
            it = self.lay.takeAt(0)
            if it.widget():
                it.widget().deleteLater()
        ids = list(fases)
        self.lay.addWidget(self._fila([(_color_fase(fid, ids), f.get("nombre", str(fid))) for fid, f in fases.items()]
                                      + [(AMBAR, "Cambio (ámbar + despeje)")]))
        self.lay.addWidget(self._fila([(VERDE, "Control: agente RL"), (AMBAR, "ciclo fijo"),
                                       (AZUL, "recuperando")]))


# ----------------------------------------------------------------------------- pestaña
class PestanaSemaforo(QtWidgets.QWidget):
    def __init__(self):
        super().__init__()
        self.setStyleSheet("QLabel[rol=\"kpi-sem\"] { font-size: 17pt; font-weight: 650; }")
        raiz = QtWidgets.QVBoxLayout(self)
        raiz.setContentsMargins(0, 12, 0, 0)
        raiz.setSpacing(12)

        # aviso (desconectado, sin datos...)
        self.aviso = QtWidgets.QLabel("")
        self.aviso.setObjectName("aviso")
        self.aviso.setWordWrap(True)
        self.aviso.hide()
        raiz.addWidget(self.aviso)

        # fila de datos
        fila = QtWidgets.QHBoxLayout()
        fila.setSpacing(12)
        self.d_control = Dato("Quién controla", con_punto=True)
        self.d_fase = Dato("Fase actual")
        self.d_prox = Dato("Próxima decisión", con_barra=True)
        self.d_conteos = Dato("Conteos que ve el agente")
        for d in (self.d_control, self.d_fase, self.d_prox, self.d_conteos):
            fila.addWidget(d, 1)
        raiz.addLayout(fila)

        # cruce + decisión
        medio = QtWidgets.QHBoxLayout()
        medio.setSpacing(12)
        tc, lc = _tarjeta("Cruce en vivo")
        self.lbl_cruce = _etiqueta("", "sub")
        tc.cabecera.addWidget(self.lbl_cruce)
        self.cruce = Cruce()
        lc.addWidget(self.cruce, 1)
        medio.addWidget(tc, 1)

        der = QtWidgets.QVBoxLayout()
        der.setSpacing(12)
        tq, lq = _tarjeta("Última decisión del agente")
        self.lbl_dec = _etiqueta("", "sub")
        tq.cabecera.addWidget(self.lbl_dec)
        self.txt_dec = QtWidgets.QLabel("–")
        self.txt_dec.setWordWrap(True)
        self.txt_dec.setProperty("rol", "valor")
        lq.addWidget(self.txt_dec)
        self.barras = BarrasQ()
        lq.addWidget(self.barras)
        nota = _etiqueta("Valor Q = cuánto espera ganar el agente con cada fase. Elige la más alta "
                         "entre las permitidas por las reglas de seguridad.", "muted")
        nota.setToolTip("Reglas de seguridad del controlador: verde mínimo y máximo de cada fase y rojo "
                        "máximo por calle. Una fase no permitida no se puede elegir en ese momento.")
        nota.setWordWrap(True)
        lq.addWidget(nota)
        der.addWidget(tq)

        th, lh = _tarjeta("Decisiones recientes")
        self.tabla = QtWidgets.QTableWidget(0, 4)
        self.tabla.setHorizontalHeaderLabels(["Hora", "Decidió", "Resultado", "Por"])
        self.tabla.verticalHeader().setVisible(False)
        self.tabla.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.tabla.setSelectionMode(QtWidgets.QAbstractItemView.NoSelection)
        self.tabla.setFocusPolicy(Qt.NoFocus)
        self.tabla.setShowGrid(False)
        self.tabla.setAlternatingRowColors(True)
        hh = self.tabla.horizontalHeader()
        hh.setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(1, QtWidgets.QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(2, QtWidgets.QHeaderView.Stretch)
        hh.setSectionResizeMode(3, QtWidgets.QHeaderView.ResizeToContents)
        hh.setDefaultAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self.tabla.verticalHeader().setDefaultSectionSize(26)
        self.tabla.setMinimumHeight(110)
        lh.addWidget(self.tabla, 1)
        der.addStretch(1)
        cont_der = QtWidgets.QWidget()
        cont_der.setObjectName("transparente")
        cont_der.setLayout(der)
        der.setContentsMargins(0, 0, 8, 0)
        scroll = QtWidgets.QScrollArea()          # en pantallas bajas, la columna se desplaza
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setFixedWidth(478)
        scroll.setWidget(cont_der)
        medio.addWidget(scroll)
        raiz.addLayout(medio, 1)

        # línea de tiempo + decisiones recientes
        abajo = QtWidgets.QHBoxLayout()
        abajo.setSpacing(12)
        tl, ll = _tarjeta("Últimos 15 minutos")
        self.linea = LineaTiempo()
        ll.addWidget(self.linea)
        self.leyenda = Leyenda()
        ll.addWidget(self.leyenda)
        abajo.addWidget(tl, 1)
        th.setFixedWidth(478)
        abajo.addWidget(th)
        raiz.addLayout(abajo)
        self._n_dec = None
        self._cfg_ult = None

    # ------------------------------------------------------------------
    def actualizar(self, sem):
        texto, color, explicacion = resumen(sem)
        d = (sem or {}).get("datos") or {}
        est = d.get("estado") or {}
        fresco = texto not in ("Sin Raspberry", "Desconectado", "Sin datos")
        if not fresco:
            est = {}
        cfg = est.get("config") or {}
        if cfg.get("fases"):
            self._cfg_ult = cfg                  # se recuerda para la línea de tiempo y la tabla
        elif (sem or {}).get("config"):
            self._cfg_ult = sem["config"]        # la última que conoce el núcleo
        fases_ult = {f["id"]: f for f in (self._cfg_ult or {}).get("fases") or []}
        fases = {f["id"]: f for f in cfg.get("fases") or []}
        ids = list(fases)
        orden = cfg.get("calles") or list(CALLES)
        conteos = dict(zip(orden, est.get("conteos") or []))
        rojos = dict(zip(orden, est.get("rojo") or []))
        camaras = d.get("calles") or {}

        # aviso
        aviso = "" if fresco and est else explicacion
        if fresco and d.get("conectado") and not d.get("enviando"):
            aviso = (f"YOLO no está mandando conteos al semáforo ({d.get('motivo') or 'sin motivo'}). "
                     f"A los 15 s el semáforo pasa solo a su ciclo fijo de respaldo.")
        self.aviso.setText(aviso)
        self.aviso.setVisible(bool(aviso))

        # quién controla
        self.d_control.set(texto, explicacion, color_punto=color)

        # fase actual
        if est:
            f = fases.get(est.get("fase"), {})
            nombre = f.get("nombre") or f"Fase {est.get('fase')}"
            col_f = _color_fase(est.get("fase"), ids)
            if est.get("etapa") == "verde":
                minimo = f.get("verde_min_s")
                maximo = f.get("verde_max_s")
                lim = f"mínimo {minimo} s" + (f", máximo {maximo} s" if maximo else "")
                self.d_fase.set(nombre, f"En verde hace {est.get('t_fase', 0)} s · {lim}", color=col_f)
            else:
                self.d_fase.set(nombre, f"Cambiando: ámbar {cfg.get('ambar_s', 3)} s + despeje; "
                                        f"luego verde", color=AMBAR)
            prox = int(est.get("prox_decision_s") or 0)
            dec = est.get("decision") or {}
            total = max(1, int(dec.get("paso_s") or 10))
            self.d_prox.set(f"en {prox} s", "Al cumplir el verde mínimo y luego cada 10 s.")
            self.d_prox.barra.set(1 - prox / total if total else 0, col_f)
            edad = est.get("edad_conteos_s")
            txt_conteos = "  ".join(f"{c} {conteos[c]:g}" for c in CALLES if c in conteos) or "–"
            self.d_conteos.set(txt_conteos, f"de hace {edad} s (YOLO → semáforo)" if edad is not None else
                               "todavía no llegan", color=None if d.get("enviando") else ROJO)
        else:
            self.d_fase.set("–", "")
            self.d_prox.set("–", "")
            self.d_prox.barra.set(0)
            self.d_conteos.set("–", "")

        # cruce
        info = {c: (camaras.get(c, ""), conteos.get(c), rojos.get(c)) for c in CALLES}
        aviso_cruce = "" if (fresco and d.get("luces")) else ("Sin luces" if fresco else texto)
        self.cruce.set(d.get("luces"), info, aviso_cruce, atenuado=not (fresco and d.get("luces")))
        self.lbl_cruce.setText("Norte arriba · la flecha marca quién tiene paso")

        # decisión
        dec = est.get("decision") or {}
        if dec and fases:
            q, masc = dec.get("q") or [], dec.get("mascara") or []
            filas = []
            for i, (fid, v) in enumerate(zip(ids, q)):
                filas.append((fases[fid].get("nombre", str(fid)), float(v), _color_fase(fid, ids),
                              i >= len(masc) or bool(masc[i]), fid == dec.get("ejecutada"),
                              bool(dec.get("anulada")) and fid == dec.get("pedida")))
            self.barras.set(filas)
            hace = max(0, int(est.get("t", 0)) - int(dec.get("t", 0)))
            ej = fases.get(dec.get("ejecutada"), {}).get("nombre", "?")
            quien = "El agente" if dec.get("quien") == "agente" else "El ciclo fijo"
            accion = f"cambió a {ej}" if dec.get("cambio") else f"mantuvo {ej}"
            txt = f"{quien} {accion} por {dec.get('paso_s', '?')} s."
            if dec.get("anulada"):
                ped = fases.get(dec.get("pedida"), {}).get("nombre", "?")
                txt += f" Pidió {ped}, pero no estaba permitida (regla de seguridad)."
            if dec.get("quien") != "agente":
                txt += " Los valores Q son solo referencia."
            self.txt_dec.setText(txt)
            self.lbl_dec.setText(f"hace {hace} s")
        else:
            self.barras.vacio = "Aún no hay decisiones" if fresco else "Sin datos del semáforo"
            self.barras.set([])
            self.txt_dec.setText("Aún no hay decisiones." if fresco else "–")
            self.lbl_dec.setText("")

        # decisiones recientes
        decs = (sem or {}).get("decisiones") or []
        clave = (len(decs), decs[-1].get("t") if decs else None, tuple(fases_ult))
        if clave != self._n_dec:
            self._n_dec = clave
            self._llenar_tabla(decs, fases_ult, list(fases_ult))

        # línea de tiempo
        self.linea.set((sem or {}).get("historia") or [], fases_ult)
        self.leyenda.set(fases_ult)

    def _llenar_tabla(self, decs, fases, ids):
        filas = list(reversed(decs[-40:]))
        self.tabla.setRowCount(len(filas))
        for r, dd in enumerate(filas):
            hora = datetime.datetime.fromtimestamp(dd.get("hora") or time.time()).strftime("%H:%M:%S")
            quien = "Agente" if dd.get("quien") == "agente" else "Ciclo fijo"
            ej = fases.get(dd.get("ejecutada"), {}).get("nombre", str(dd.get("ejecutada")))
            res = ("→ " + ej) if dd.get("cambio") else ("= " + ej)
            if dd.get("anulada"):
                res += "  (anulada)"
            celdas = [hora, quien, res, f"{dd.get('paso_s', '?')} s"]
            for c, texto in enumerate(celdas):
                it = QtWidgets.QTableWidgetItem(texto)
                if c == 1:
                    it.setForeground(QtGui.QColor(VERDE if dd.get("quien") == "agente" else AMBAR))
                if c == 2:
                    it.setForeground(QtGui.QColor(_color_fase(dd.get("ejecutada"), ids) if dd.get("cambio") else T2))
                self.tabla.setItem(r, c, it)
