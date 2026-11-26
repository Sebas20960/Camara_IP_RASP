# =============================================================================
# motor_ncnn.py  -  YOLO (exportado a NCNN por Ultralytics) ejecutado DIRECTO con ncnn,
# sin pasar por model.predict() de Ultralytics. Lo usa conteo4.py (MOTOR = "ncnn").
#
# Por qué es más rápido en la Raspberry Pi 4:
#   1) Sin el pre/post-proceso de Ultralytics (torch, LetterBox, Results...): eso costaba
#      ~15-30 ms por inferencia en la Pi, solo en Python.
#   2) Entrada RECTANGULAR: un cuadro 16:9 reducido a 320x180 se analiza como 320x192 (solo
#      se rellenan 12 filas), no como 320x320 (140 filas grises que YOLO igual procesaba).
#      Son ~40 % menos cálculos con el mismo modelo y la misma resolución útil. Ultralytics
#      no puede hacerlo con NCNN porque el modelo exportado trae las "anclas" fijas para
#      320x320; aquí se leen las salidas de las 3 escalas ANTES de esa parte fija y la
#      decodificación (DFL + anclas) se hace con numpy, solo para las cajas candidatas.
#   3) La caja final y la NMS se calculan con numpy sobre pocas candidatas (microsegundos).
#
# Si el modelo no tiene la forma esperada, usa la salida normal "out0" con 320x320
# (igual que Ultralytics). Y si ncnn falla del todo, conteo4.py vuelve a Ultralytics.
# =============================================================================
import os

import cv2
import numpy as np

REG_MAX = 16
RELLENO = 114          # gris del "letterbox" (el mismo que usa Ultralytics)
OPENMP_BLOCKTIME = None   # None = el de ncnn (20 ms girando tras cada capa); ver conteo4.py


class _BoxesNP:
    """Lo mínimo de ultralytics.engine.results.Boxes que usan conteo4 y ByteTrack, en numpy.
    data: (N, 6) float32 = x1, y1, x2, y2, conf, cls."""

    def __init__(self, data):
        self.data = data

    def __len__(self):
        return len(self.data)

    def __getitem__(self, idx):
        return _BoxesNP(self.data[idx])

    def cpu(self):
        return self

    def numpy(self):
        return self

    @property
    def xyxy(self):
        return self.data[:, :4]

    @property
    def conf(self):
        return self.data[:, 4]

    @property
    def cls(self):
        return self.data[:, 5]

    @property
    def xywh(self):
        d = self.data
        out = np.empty((len(d), 4), dtype=np.float32)
        out[:, 0] = (d[:, 0] + d[:, 2]) * 0.5
        out[:, 1] = (d[:, 1] + d[:, 3]) * 0.5
        out[:, 2] = d[:, 2] - d[:, 0]
        out[:, 3] = d[:, 3] - d[:, 1]
        return out


class Resultado:
    __slots__ = ("boxes",)

    def __init__(self, data):
        self.boxes = _BoxesNP(data)


def _leer_param(ruta_param):
    """Capas del .param: lista de (tipo, nombre, entradas, salidas, {clave: valor})."""
    capas = []
    with open(ruta_param, encoding="utf-8") as f:
        lineas = f.read().split("\n")
    for linea in lineas[2:]:
        p = linea.split()
        if len(p) < 4:
            continue
        tipo, nombre, n_in, n_out = p[0], p[1], int(p[2]), int(p[3])
        ent = p[4:4 + n_in]
        sal = p[4 + n_in:4 + n_in + n_out]
        args = {}
        for a in p[4 + n_in + n_out:]:
            if "=" in a:
                k, v = a.split("=", 1)
                args[k] = v
        capas.append((tipo, nombre, ent, sal, args))
    return capas


def _blobs_por_escala(capas, nc):
    """Busca, para cada escala (8, 16, 32), el blob de cajas (64 canales) y el de clases
    (nc canales) justo antes de los Reshape del cabezal Detect. None si no se reconoce."""
    caja, clase = [], []
    for tipo, _n, ent, _s, args in capas:
        if tipo != "Reshape" or len(ent) != 1 or "1" not in args or "0" not in args or "2" in args:
            continue
        try:
            n_anclas, canales = int(args["0"]), int(args["1"])
        except ValueError:
            continue
        if canales == 4 * REG_MAX:
            caja.append((n_anclas, ent[0]))
        elif canales == nc:
            clase.append((n_anclas, ent[0]))
    if len(caja) != 3 or len(clase) != 3:
        return None
    caja.sort(reverse=True)                  # más anclas = stride más chico (8, 16, 32)
    clase.sort(reverse=True)
    if [a for a, _ in caja] != [a for a, _ in clase]:
        return None
    return [(8, caja[0][1], clase[0][1]), (16, caja[1][1], clase[1][1]), (32, caja[2][1], clase[2][1])]


def _nms(cajas, puntajes, iou, max_det):
    """NMS voraz (agnóstica) en numpy. cajas (N,4) xyxy. Devuelve índices ordenados."""
    orden = np.argsort(-puntajes, kind="stable")
    x1, y1, x2, y2 = cajas[:, 0], cajas[:, 1], cajas[:, 2], cajas[:, 3]
    area = np.maximum(x2 - x1, 0) * np.maximum(y2 - y1, 0)
    quedan = []
    while orden.size and len(quedan) < max_det:
        i = orden[0]
        quedan.append(i)
        if orden.size == 1:
            break
        resto = orden[1:]
        ix1 = np.maximum(x1[i], x1[resto])
        iy1 = np.maximum(y1[i], y1[resto])
        ix2 = np.minimum(x2[i], x2[resto])
        iy2 = np.minimum(y2[i], y2[resto])
        inter = np.maximum(ix2 - ix1, 0) * np.maximum(iy2 - iy1, 0)
        union = area[i] + area[resto] - inter
        orden = resto[inter <= iou * np.maximum(union, 1e-9)]
    return np.asarray(quedan, dtype=np.int64)


def _ajustar_param(txt, imgsz, H, W):
    """Copia del .param con los Reshape "espaciales" ajustados de imgsz x imgsz a H x W:
    0=n*n -> (H/st)*(W/st) y 0=n 1=n -> 0=W/st 1=H/st, con n = imgsz/st (st = 8, 16, 32)."""
    escalas = {st: imgsz // st for st in (32, 16, 8) if imgsz % st == 0}
    lineas = txt.splitlines()
    for k, linea in enumerate(lineas):
        if not linea.startswith("Reshape"):
            continue
        p = linea.split()
        args = {a.split("=", 1)[0]: i for i, a in enumerate(p) if "=" in a and not a.startswith("-")}
        val = {c: p[i].split("=", 1)[1] for c, i in args.items()}
        for st, n in escalas.items():
            h, w = H // st, W // st
            if val.get("0") == str(n) and val.get("1") == str(n):
                p[args["0"]], p[args["1"]] = f"0={w}", f"1={h}"
                break
            if val.get("0") == str(n * n):
                p[args["0"]] = f"0={h * w}"
                break
        lineas[k] = " ".join(p)
    return "\n".join(lineas) + "\n"


class MotorNCNN:
    """
    det = MotorNCNN(carpeta_modelo, imgsz=320, hilos=3)
    r = det.predecir(img_bgr, conf=0.15, iou=0.5, max_det=50, clases=[0,1,2,3,4], agnostico=True)
    r.boxes.xyxy / .conf / .cls (numpy), en píxeles de img_bgr.
    """

    def __init__(self, carpeta, imgsz=320, hilos=3, nc=None, rectangular=True):
        import ncnn  # pyright: ignore[reportMissingImports]
        self.ncnn = ncnn
        self.imgsz = int(imgsz)
        param = os.path.join(carpeta, "model.ncnn.param")
        binario = os.path.join(carpeta, "model.ncnn.bin")
        if not (os.path.isfile(param) and os.path.isfile(binario)):
            raise FileNotFoundError(f"No encuentro model.ncnn.param/.bin en {carpeta}")
        capas = _leer_param(param)
        if nc is None:                           # nº de clases: metadata.yaml o el .param
            try:
                nc = len(nombres_de_metadata(carpeta)) or None
            except Exception:
                nc = None
            nc = nc or self._nc_de_param(capas)
        self.nc = int(nc)
        self.hilos = int(hilos)
        self._binario = binario
        with open(param, encoding="utf-8") as f:
            self._param_txt = f.read()
        self.entrada = next((s[0] for t, _n, _e, s, _a in capas if t == "Input" and s), "in0")
        self.escalas = _blobs_por_escala(capas, self.nc) if rectangular else None
        self.rectangular = self.escalas is not None
        self._redes = {}                          # (alto, ancho) -> ncnn.Net
        self.openmp_blocktime = OPENMP_BLOCKTIME
        self._anclas = {}                         # (alto, ancho) -> (puntos (N,2), stride (N,))
        self._mat_norm = [1 / 255.0] * 3
        self.net = self._red(self.imgsz, self.imgsz)   # la cuadrada siempre (y valida el modelo)

    def _red(self, H, W):
        """Red para una entrada H x W. El bloque de atención de YOLO11 (C2PSA) trae tamaños
        fijos para imgsz x imgsz (p. ej. Reshape 10x10 a 320): se ajustan al tamaño real.
        Se crea UNA vez por tamaño (normalmente uno solo: todas las cámaras 16:9)."""
        red = self._redes.get((H, W))
        if red is not None:
            return red
        if len(self._redes) >= 6:                 # tamaños raros: no llenar la RAM
            self._redes.pop(next(k for k in self._redes if k != (self.imgsz, self.imgsz)))
        txt = self._param_txt if (H, W) == (self.imgsz, self.imgsz) else _ajustar_param(
            self._param_txt, self.imgsz, H, W)
        red = self.ncnn.Net()
        red.opt.use_vulkan_compute = False
        red.opt.num_threads = self.hilos
        if self.openmp_blocktime is not None:      # ms que los hilos esperan girando (ncnn: 20)
            red.opt.openmp_blocktime = int(self.openmp_blocktime)
        if red.load_param_mem(txt) != 0 or red.load_model(self._binario) != 0:
            raise RuntimeError("ncnn no pudo cargar el modelo")
        self._redes[(H, W)] = red
        return red

    @staticmethod
    def _nc_de_param(capas):
        for tipo, _n, _e, sal, args in reversed(capas):
            if tipo == "Reshape" and "1" in args and "2" not in args:
                try:
                    c = int(args["1"])
                except ValueError:
                    continue
                if c not in (4 * REG_MAX, 4, 1):
                    return c
        return 80

    # ---------------------------------------------------------------- preparación
    def _letterbox(self, img):
        """Reduce (si hace falta) y rellena hasta un múltiplo de 32 (rectangular) o hasta
        imgsz x imgsz. Devuelve (img_lista, escala, pad_x, pad_y)."""
        h, w = img.shape[:2]
        s = self.imgsz
        e = min(s / h, s / w)
        if e != 1.0:
            nw, nh = int(round(w * e)), int(round(h * e))
            img = cv2.resize(img, (nw, nh), interpolation=cv2.INTER_LINEAR)
        else:
            nw, nh = w, h
        if self.rectangular:
            W = int(np.ceil(nw / 32) * 32)
            H = int(np.ceil(nh / 32) * 32)
        else:
            W = H = s
        dw, dh = (W - nw) / 2, (H - nh) / 2
        top, bottom = int(round(dh - 0.1)), int(round(dh + 0.1))
        left, right = int(round(dw - 0.1)), int(round(dw + 0.1))
        if top or bottom or left or right:
            img = cv2.copyMakeBorder(img, top, bottom, left, right, cv2.BORDER_CONSTANT,
                                     value=(RELLENO, RELLENO, RELLENO))
        elif not img.flags["C_CONTIGUOUS"]:
            img = np.ascontiguousarray(img)
        return img, e, left, top

    def _anclas_para(self, H, W):
        clave = (H, W)
        a = self._anclas.get(clave)
        if a is None:
            puntos, strides = [], []
            for st, _b, _c in self.escalas:
                h, w = H // st, W // st
                sx = np.arange(w, dtype=np.float32) + 0.5
                sy = np.arange(h, dtype=np.float32) + 0.5
                gx, gy = np.meshgrid(sx, sy)
                puntos.append(np.stack([gx.ravel(), gy.ravel()], 1))
                strides.append(np.full(h * w, st, dtype=np.float32))
            a = self._anclas[clave] = (np.concatenate(puntos), np.concatenate(strides))
        return a

    # ---------------------------------------------------------------- inferencia
    def predecir(self, img, conf=0.25, iou=0.5, max_det=50, clases=None, agnostico=True):
        ncnn = self.ncnn
        lista, e, px, py = self._letterbox(img)
        H, W = lista.shape[:2]
        mat = ncnn.Mat.from_pixels(lista, ncnn.Mat.PixelType.PIXEL_BGR2RGB, W, H)
        mat.substract_mean_normalize([], self._mat_norm)
        ex = self._red(H, W).create_extractor()
        ex.input(self.entrada, mat)
        if self.rectangular:
            cajas, puntajes, cls = self._decodificar_escalas(ex, H, W, conf, clases)
        else:
            cajas, puntajes, cls = self._decodificar_out0(ex, conf, clases)
        if not len(puntajes):
            return Resultado(np.zeros((0, 6), dtype=np.float32))
        if agnostico:
            keep = _nms(cajas, puntajes, iou, max_det)
        else:                                    # NMS por clase: se separan las clases en el plano
            keep = _nms(cajas + (cls.astype(np.float32) * 7680.0)[:, None], puntajes, iou, max_det)
        cajas, puntajes, cls = cajas[keep], puntajes[keep], cls[keep]
        # de vuelta a píxeles de la imagen original
        cajas[:, [0, 2]] -= px
        cajas[:, [1, 3]] -= py
        if e != 1.0:
            cajas /= e
        h0, w0 = img.shape[:2]
        cajas[:, [0, 2]] = np.clip(cajas[:, [0, 2]], 0, w0)
        cajas[:, [1, 3]] = np.clip(cajas[:, [1, 3]], 0, h0)
        datos = np.empty((len(keep), 6), dtype=np.float32)
        datos[:, :4] = cajas
        datos[:, 4] = puntajes
        datos[:, 5] = cls
        return Resultado(datos)

    def _filtrar(self, logits_o_prob, conf, clases, ya_prob):
        """(N, nc) -> índices de anclas que pasan conf, su puntaje y su clase."""
        if clases is not None and len(clases) < self.nc:
            m = np.full(self.nc, -np.inf if not ya_prob else -1.0, dtype=np.float32)
            m[list(clases)] = 0
            logits_o_prob = logits_o_prob + m
        cls = logits_o_prob.argmax(1)
        mejor = logits_o_prob[np.arange(len(cls)), cls]
        if not ya_prob:                          # umbral en logit: sigmoide solo de las que pasan
            umbral = np.log(conf / (1 - conf)) if 0 < conf < 1 else -np.inf
            idx = np.nonzero(mejor > umbral)[0]
            return idx, 1.0 / (1.0 + np.exp(-mejor[idx])), cls[idx]
        idx = np.nonzero(mejor > conf)[0]
        return idx, mejor[idx], cls[idx]

    def _decodificar_escalas(self, ex, H, W, conf, clases):
        logits, dist = [], []
        for _st, b_caja, b_clase in self.escalas:
            r1, m_cls = ex.extract(b_clase)
            r2, m_box = ex.extract(b_caja)
            if r1 != 0 or r2 != 0:
                raise RuntimeError(f"ncnn no pudo extraer {b_clase}/{b_caja}")
            c = np.array(m_cls)  # copia: la memoria del Mat se libera
            logits.append(c.reshape(self.nc, -1))
            dist.append(np.array(m_box).reshape(4 * REG_MAX, -1))
        logits = np.concatenate(logits, 1).T            # (N, nc)
        idx, puntajes, cls = self._filtrar(logits, conf, clases, ya_prob=False)
        if not len(idx):
            return np.zeros((0, 4), np.float32), np.zeros(0, np.float32), np.zeros(0, np.int64)
        d = np.concatenate(dist, 1)[:, idx].reshape(4, REG_MAX, -1)   # solo las candidatas
        d = d - d.max(1, keepdims=True)
        np.exp(d, out=d)
        d = (d * np.arange(REG_MAX, dtype=np.float32)[None, :, None]).sum(1) / d.sum(1)  # (4, n)
        puntos, strides = self._anclas_para(H, W)
        p, st = puntos[idx], strides[idx]
        cajas = np.empty((len(idx), 4), dtype=np.float32)
        cajas[:, 0] = (p[:, 0] - d[0]) * st
        cajas[:, 1] = (p[:, 1] - d[1]) * st
        cajas[:, 2] = (p[:, 0] + d[2]) * st
        cajas[:, 3] = (p[:, 1] + d[3]) * st
        return cajas, puntajes.astype(np.float32), cls

    def _decodificar_out0(self, ex, conf, clases):
        r, m = ex.extract("out0")
        if r != 0:
            raise RuntimeError("ncnn no pudo extraer out0")
        o = np.array(m).reshape(4 + self.nc, -1)          # xywh + prob. por clase
        idx, puntajes, cls = self._filtrar(o[4:].T, conf, clases, ya_prob=True)
        xywh = o[:4, idx].T
        cajas = np.empty((len(idx), 4), dtype=np.float32)
        cajas[:, :2] = xywh[:, :2] - xywh[:, 2:] / 2
        cajas[:, 2:] = xywh[:, :2] + xywh[:, 2:] / 2
        return cajas, puntajes.astype(np.float32), cls


def nombres_de_metadata(carpeta):
    """{id: nombre} desde metadata.yaml (sin cargar Ultralytics)."""
    import yaml
    with open(os.path.join(carpeta, "metadata.yaml"), encoding="utf-8") as f:
        meta = yaml.safe_load(f) or {}
    nombres = meta.get("names") or {}
    if isinstance(nombres, list):
        nombres = dict(enumerate(nombres))
    return {int(k): str(v) for k, v in nombres.items()}

