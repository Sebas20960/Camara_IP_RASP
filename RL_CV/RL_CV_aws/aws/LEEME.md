# aws/ — Servidor en la nube (dashboard web + historial)

Va en tu instancia de **AWS Lightsail** (IP `3.144.43.2`). Recibe de la Raspberry los datos cada 1 s y el video con las cajas, guarda el **historial por minuto** (SQLite) y sirve el dashboard en `http://3.144.43.2`.

| Archivo | Qué es |
|---|---|
| `servidor.py` | El servidor (FastAPI) |
| `static/index.html` | El dashboard web (video 2x2 con cajas interpoladas, conteos, historial, CSV) |
| `requirements.txt` | Dependencias |
| `instalar_aws.sh` | Instala y deja el servidor como servicio `conteo4-nube` (arranca solo al reiniciar la instancia) |

En la instancia, el servidor vive en **`~/servidor`**. Ahí están tu token **`.token`** (el que usa la Raspberry) y tu historial **`historial.db`**. Por eso siempre se copian los archivos **dentro de esa carpeta**.

## Actualizar tu servidor (ya creado) — desde PowerShell en la carpeta `V_final_aws`

```powershell
cd "C:\Users\SEBASTIAN\OneDrive\Desktop\Proyecto I\YOLO\Versiones\V_final_final\V_final_aws"
$KEY = "$HOME\.ssh\LightsailDefaultKey-us-east-2.pem"
scp -i $KEY aws\servidor.py aws\requirements.txt aws\instalar_aws.sh ubuntu@3.144.43.2:~/servidor/
scp -i $KEY aws\static\index.html ubuntu@3.144.43.2:~/servidor/static/
ssh -i $KEY ubuntu@3.144.43.2 "cd ~/servidor && bash instalar_aws.sh"
```

El último comando reinstala y reinicia el servidor y al final muestra la URL y el **mismo token** de siempre (`CQkVMFR-...`). Si mostrara uno distinto, algo se copió en otra carpeta: revisa que todo esté en `~/servidor`.

> ⚠️ No copies la carpeta `aws` completa a `~/aws` ni a otro lado: `instalar_aws.sh` crearía un servidor nuevo con OTRO token y sin tu historial (y además chocaría con el que ya usa el puerto 80).
>
> ⚠️ El servicio se sigue llamando `conteo4-nube` (es el que ya existe en tu instancia). No le cambies el nombre en `instalar_aws.sh`.

Comprueba después en el navegador: `http://3.144.43.2` (Ctrl+F5 para que cargue el dashboard nuevo).

## Instalar desde cero (solo si algún día creas otra instancia)

1. Lightsail → **Create instance** → Linux → OS Only → **Ubuntu 24.04** → plan de 1 GB → crear.
2. Pestaña **Networking**: **Create static IP** (asígnala a la instancia) y verifica que **HTTP (TCP 80)** esté abierto.
3. Descarga la llave SSH de esa región (Account → SSH keys) a `C:\Users\SEBASTIAN\.ssh\` (nunca dentro del proyecto).
4. Desde PowerShell en `V_final_aws` (cambia `IP` por la IP estática nueva):
   ```powershell
   $KEY = "$HOME\.ssh\LightsailDefaultKey-us-east-2.pem"
   scp -i $KEY -r aws ubuntu@IP:~/servidor
   ssh -i $KEY ubuntu@IP "cd ~/servidor && bash instalar_aws.sh"
   ```
5. Anota la URL y el **token** que muestra al final y ponlos en `raspberry/conteo4_aws.py` (`NUBE_URL`, `NUBE_TOKEN`), o pásalos con `--nube IP --token TOKEN`.

## Comandos útiles (dentro del servidor, tras `ssh -i $KEY ubuntu@3.144.43.2`)

```bash
sudo journalctl -u conteo4-nube -f        # ver lo que pasa en vivo (Ctrl+C para salir)
sudo systemctl restart conteo4-nube       # reiniciar
cat ~/servidor/.token                     # ver el token de la Raspberry
ls -lh ~/servidor/historial.db            # tamaño del historial
exit                                      # volver a tu laptop
```

## Datos que conviene saber

- **Video hacia AWS**: SOLO mientras alguien tiene el dashboard abierto. Tope de 4 cuadros/s por cámara (`FPS_VIDEO_NUBE` en `raspberry/conteo4_aws.py` y `FPS_VIDEO_WEB` en `instalar_aws.sh`; manda el menor). Con 4 cámaras ≈ 560-600 KB/s ≈ 4,5 Mbps de **subida** desde donde esté la Pi. Si el internet sube poco: `--fps-video 2` en la Pi (≈ 2,3 Mbps) o `1` (≈ 1,1 Mbps).
- **Sin nadie mirando**: la Pi solo manda los datos (~1 KB/s). El historial y el CSV se llenan igual.
- **Sin internet**: la Pi guarda hasta 6 h de datos y los manda al reconectar.
- **Seguridad**: solo la Pi con el token puede enviar datos; el dashboard es público para quien tenga la IP y va sin cifrar (http). Para HTTPS hace falta un dominio + Caddy.
- **Prueba local sin AWS** (en tu laptop): `cd aws` → `python servidor.py` → `http://localhost:8000` (token `cambia-esto`). En la Pi (o en la misma laptop): `python conteo4_aws.py --nube IP_LAPTOP:8000 --token cambia-esto`.
