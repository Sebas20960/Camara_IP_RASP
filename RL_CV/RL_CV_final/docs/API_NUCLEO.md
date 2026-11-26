# API entre `nucleo.py` (red + datos) y `app.py` (ventana PySide6)

`nucleo.py` NO importa Qt. `app.py` solo usa lo de abajo (sondeo con QTimer, sin callbacks entre hilos).
Protocolo con la Raspberry: ver `../PROTOCOLO_v2.md`.

```python
CATS = ["carro", "moto", "bus", "pesado", "especial"]
COLORES = {"carro": "#28aaeb", "moto": "#fac828", "bus": "#d25ac8", "pesado": "#ff821e", "especial": "#eb3c46"}

def ip_local() -> str: ...              # IP de la laptop en la red local (para darle a la Pi)

class Receptor:
    """Servidor WebSocket (hilo propio con asyncio + librería websockets) que recibe a la Pi
    en ws://0.0.0.0:<puerto>/ws/pi?token=<token>. Guarda historial en SQLite."""
    def __init__(self, puerto: int = 8000, token: str = "cambia-esto",
                 db_ruta: str = "<carpeta de nucleo.py>/historial_laptop.db",
                 retraso: float = 1.0, fps_video: int = 10): ...
    def iniciar(self) -> None: ...      # no bloquea; lanza el hilo
    def detener(self) -> None: ...      # cierra conexiones, guarda el minuto en curso
    def set_video(self, activo: bool) -> None: ...   # manda {"tipo":"visores","video":1|0,"fps":fps_video}
    def set_retraso(self, segundos: float) -> None: ...

    # ---- lecturas (seguras desde el hilo de Qt; devuelven copias/valores inmutables) ----
    def estado(self) -> dict:
        # {"pi_conectada": bool, "ip_pi": str|None, "camaras": [str], "t_ultimo": float (reloj local),
        #  "ultimo": <último mensaje 'datos' tal cual (dict) o None>,
        #  "video_kbps": float, "cuadros_por_s": float}
    def cuadro(self, cam: int) -> tuple[int, bytes, list[dict]] | None:
        # Lo que hay que mostrar AHORA en la cámara `cam` según el algoritmo de interpolación:
        # (seq, jpeg_bytes, cajas). seq permite a la UI no redecodificar el mismo JPEG.
        # cajas: [{"x1":float,"y1":float,"x2":float,"y2":float,   # 0..1 normalizados
        #          "cat":str, "id":int|None, "num":int|None, "conf":float, "texto":str}]
        # None si aún no hay video de esa cámara.
    def historial(self, horas: float, paso: int) -> dict:
        # MISMO formato que GET /api/historial del servidor AWS:
        # {"desde","hasta","paso","ts":[...], "camaras":{nombre:{"flujo":[int|None],
        #   "ocupacion":[float|None],"maximo":[int|None],"por_categoria":{cat:int},"flujo_total":int}},
        #  "categorias":{cat:int}, "flujo_total":int}
        # paso ∈ {60, 300, 900, 3600}
    def exportar_csv(self, ruta: str, horas: float) -> int: ...   # filas escritas (hora local)

    # ---- agente de la Raspberry (/ws/agente) ----
    def agente(self, con_log: bool = False) -> dict:
        # {"conectado": bool, "ip": str|None, "estado": <último mensaje 'agente'>|None,
        #  "n_log": int (cambia si hay líneas nuevas), "respuestas": [(t, accion, ok, msg)],
        #  "log": [str] (solo con con_log=True)}
    def orden_agente(self, accion: str, **extra) -> bool: ...   # False si el agente no está
    def pedidos_sim(self) -> list[str]: ...    # "iniciar"/"detener"/"estado" pedidos desde la web
    def informar_sim(self, activo, listo, urls, error=None, forzar=False) -> None: ...

class Simulador:
    """Controla camfalsa4.py (../camfalsa4.py) como subproceso: las 4 cámaras falsas."""
    def __init__(self, ruta_script: str | None = None): ...      # por defecto ../camfalsa4.py
    def iniciar(self, videos: list[str], puerto: int = 8080, ancho: int = 640,
                fps: float = 10, calidad: int = 75) -> None: ...
    def detener(self) -> None: ...
    @property
    def activo(self) -> bool: ...
    @property
    def listo(self) -> bool: ...         # además ya abrió sus puertos (acepta conexiones)
    error: str | None                    # por qué terminó, si terminó con error
    def urls(self) -> list[str]: ...     # ["IP_LAPTOP:8080", ...] para poner en CAMARAS de la Pi
    def log(self) -> list[str]: ...      # últimas líneas de salida del simulador
```
