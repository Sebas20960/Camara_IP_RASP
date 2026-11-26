# aws/ — Servidor en la nube (dashboard web + historial)

Va en tu instancia de **AWS Lightsail** (IP `3.144.43.2`). Recibe de la Raspberry los datos cada 1 s y el video con las cajas, guarda el **historial por minuto** (SQLite) y sirve el dashboard en `http://3.144.43.2`.

| Archivo | Qué es |
|---|---|
| `servidor.py` | El servidor (FastAPI) |
| `static/index.html` | El dashboard web (video 2x2 con cajas interpoladas, conteos, historial, CSV) |
| `requirements.txt` | Dependencias |
| `instalar_aws.sh` | Instala y deja el servidor como servicio (arranca solo al reiniciar la instancia) |

## Actualizar tu servidor (ya creado) — desde PowerShell en la carpeta `V_final_final`

En la instancia, el servidor vive en **`~/servidor`** (ahí están tu token `.token` y tu historial `historial.db`). Copia los archivos **dentro de esa carpeta** para conservarlos:

```powershell
$KEY = "$HOME\.ssh\LightsailDefaultKey-us-east-2.pem"
scp -i $KEY aws\servidor.py aws\requirements.txt aws\instalar_aws.sh ubuntu@3.144.43.2:~/servidor/
scp -i $KEY aws\static\index.html ubuntu@3.144.43.2:~/servidor/static/
ssh -i $KEY ubuntu@3.144.43.2 "cd ~/servidor && bash instalar_aws.sh"
```

El último comando reinstala y reinicia el servidor y al final muestra la URL, el **mismo token** de siempre y la **clave de control** (se crea la primera vez; verla otra vez: `cat ~/servidor/.clave_control`).

## Control remoto de la Raspberry desde el dashboard

La tarjeta **Raspberry Pi** del dashboard muestra a todos el estado del conteo y de la Pi (temperatura, RAM, carga, si se frena por calor o por mala fuente). Quien escriba la **clave de control** ve además el registro de la Pi y los botones: *Iniciar todo* (enciende las cámaras simuladas de la laptop si su programa está abierto, y luego el conteo), *Iniciar / Reiniciar / Detener conteo*, encender o apagar las cámaras de la laptop, y *Reiniciar / Apagar Raspberry*. Para que funcione, la Pi debe tener el agente instalado (`raspberry/instalar_pi.sh`).

- Sin visores con clave, la web pública no ve direcciones internas (IPs de cámaras, nombre de la Pi).
- 5 claves equivocadas seguidas cortan la conexión (y cada intento fallido tarda 1 s).
- Para desactivar el control, deja vacía `Environment=CONTROL_CLAVE=` en `/etc/systemd/system/conteo4-nube.service` y reinicia el servicio.

> ⚠️ No copies la carpeta `aws` completa a `~/aws`: `instalar_aws.sh` crearía un servidor nuevo con OTRO token y sin tu historial.

## Instalar desde cero (si algún día creas otra instancia)

1. Lightsail → **Create instance** → Linux → OS Only → **Ubuntu 24.04** → plan de 1 GB → crear.
2. Pestaña **Networking**: **Create static IP** (asígnala) y verifica que **HTTP (TCP 80)** esté abierto.
3. Desde PowerShell en `V_final_final`: `scp -i $KEY -r aws ubuntu@IP:~/servidor`
4. `ssh -i $KEY ubuntu@IP "cd ~/servidor && bash instalar_aws.sh"` → anota la URL y el **token** y ponlos en `raspberry/conteo4_nube.py` (`NUBE_URL`, `NUBE_TOKEN`).

## Comandos útiles (dentro del servidor, tras `ssh -i $KEY ubuntu@3.144.43.2`)

```bash
sudo journalctl -u conteo4-nube -f        # ver lo que pasa en vivo (Ctrl+C para salir)
sudo systemctl restart conteo4-nube       # reiniciar
cat ~/servidor/.token                     # ver el token de la Raspberry
exit                                      # volver a tu laptop
```

## Datos que conviene saber

- **Video hacia AWS**: tope de 4 cuadros/s por cámara (`FPS_VIDEO_NUBE` en la Pi y `FPS_VIDEO_WEB` en el servidor) ≈ 5 Mbps de subida desde donde esté la Pi, SOLO mientras alguien tiene el dashboard abierto. Si el internet sube poco, baja `FPS_VIDEO_NUBE` a 2.
- **Sin internet**: la Pi guarda hasta 6 h de datos y los manda al reconectar (el historial queda completo).
- **Seguridad**: solo la Pi con el token puede enviar datos; el dashboard es público para quien tenga la IP (así se pidió) y va sin cifrar (http). Para HTTPS hace falta un dominio + Caddy.
- **Prueba local sin AWS** (opcional): `cd aws` → `python servidor.py` → `http://localhost:8000` (token `cambia-esto`).
