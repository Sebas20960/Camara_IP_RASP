#!/usr/bin/env bash
# =============================================================================
# instalar_aws.sh  -  Instala el servidor en una instancia Ubuntu de AWS (Lightsail o EC2)
# y lo deja corriendo como servicio (arranca solo si la instancia se reinicia).
#
# Uso (dentro de la carpeta 'servidor' ya copiada a la instancia):
#     bash instalar_aws.sh
# Volver a ejecutarlo actualiza el código y conserva el token, la clave de control y el historial.
# =============================================================================
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
PUERTO="${PUERTO:-80}"
SERVICIO=conteo4-nube

echo "==> Instalando Python (venv)..."
sudo apt-get update -qq
sudo apt-get install -y -qq python3-venv python3-pip > /dev/null

echo "==> Creando entorno e instalando dependencias..."
python3 -m venv "$DIR/venv"
"$DIR/venv/bin/pip" install -q --upgrade pip
"$DIR/venv/bin/pip" install -q -r "$DIR/requirements.txt"

# Token que debe usar la Raspberry: se genera una vez y se guarda en .token
if [ ! -s "$DIR/.token" ]; then
    python3 -c 'import secrets; print(secrets.token_urlsafe(24))' > "$DIR/.token"
    chmod 600 "$DIR/.token"
fi
TOKEN="$(cat "$DIR/.token")"

# Clave para manejar la Raspberry desde el dashboard (iniciar/detener, reiniciar la Pi...)
if [ ! -s "$DIR/.clave_control" ]; then
    python3 -c 'import secrets; print(secrets.token_urlsafe(9))' > "$DIR/.clave_control"
    chmod 600 "$DIR/.clave_control"
fi
CLAVE="$(cat "$DIR/.clave_control")"

echo "==> Creando el servicio $SERVICIO (puerto $PUERTO)..."
sudo tee /etc/systemd/system/$SERVICIO.service > /dev/null <<EOF
[Unit]
Description=conteo4 - servidor en la nube (dashboard + historial)
After=network-online.target
Wants=network-online.target

[Service]
User=$USER
WorkingDirectory=$DIR
Environment=PI_TOKEN=$TOKEN
Environment=CONTROL_CLAVE=$CLAVE
Environment=PUERTO=$PUERTO
Environment=DB_RUTA=$DIR/historial.db
Environment=DIAS_HISTORIAL=90
Environment=FPS_VIDEO_WEB=4
Environment=PYTHONUNBUFFERED=1
ExecStart=$DIR/venv/bin/python $DIR/servidor.py
Restart=always
RestartSec=3
# permite usar el puerto 80 sin ser root
AmbientCapabilities=CAP_NET_BIND_SERVICE

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now $SERVICIO > /dev/null 2>&1
sudo systemctl restart $SERVICIO
sleep 2

IP="$(curl -s --max-time 3 https://checkip.amazonaws.com || echo IP_PUBLICA)"
URL="http://$IP"; [ "$PUERTO" != "80" ] && URL="$URL:$PUERTO"
echo
echo "================================================================"
if systemctl is-active --quiet $SERVICIO; then
    echo " Servidor CORRIENDO."
else
    echo " [ERROR] El servicio no arrancó. Mira: sudo journalctl -u $SERVICIO -n 50"
fi
echo
echo " Dashboard:   $URL"
echo " Clave de control (para manejar la Pi desde el dashboard):  $CLAVE"
echo "     (verla otra vez:  cat ~/servidor/.clave_control)"
echo
echo " En la Raspberry (conteo4_nube.py):"
echo "     NUBE_URL   = \"$URL\""
echo "     NUBE_TOKEN = \"$TOKEN\""
echo
echo " Recuerda abrir el puerto $PUERTO (TCP) en el firewall de Lightsail / Security Group de EC2."
echo " Ver registros:   sudo journalctl -u $SERVICIO -f"
echo "================================================================"
