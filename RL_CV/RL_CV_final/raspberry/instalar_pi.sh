#!/usr/bin/env bash
# =============================================================================
# instalar_pi.sh  -  Deja el AGENTE corriendo como servicio en la Raspberry (una sola vez).
#
# Después de esto ya no hace falta entrar a la Pi: al encenderla arranca el agente, y el
# conteo se maneja desde el programa de la laptop (pestaña Control) o desde el dashboard web.
#
# Uso (en la Pi, con el entorno virtual de YOLO ACTIVADO si usas uno):
#     source ~/yolo_env/bin/activate        # (el nombre de tu entorno)
#     cd ~/V_final_final/raspberry
#     bash instalar_pi.sh
#
# Qué hace:
#   1. Comprueba que el python que usarás tiene ultralytics, websockets y OpenCV.
#   2. Apaga el servicio viejo conteo4-nube (si existe): con los dos se contaría dos veces.
#   3. Crea el servicio conteo4-agente (arranca al encender, se revive si se cae).
#   4. Permite al agente reiniciar/apagar la Pi sin clave (SOLO esos dos comandos).
# Volver a ejecutarlo es seguro (actualiza el servicio).
# =============================================================================
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
USUARIO="$(id -un)"
SERVICIO=conteo4-agente

if [ -n "${VIRTUAL_ENV:-}" ]; then
    PY="$VIRTUAL_ENV/bin/python"
else
    PY="${PYTHON:-$(command -v python3)}"
fi
echo "==> Python: $PY"
if ! "$PY" -c "import ultralytics, websockets, cv2" 2>/dev/null; then
    echo "[ERROR] A ese python le faltan librerías."
    echo "        Activa el entorno donde instalaste YOLO (source ~/TU_ENTORNO/bin/activate)"
    echo "        y ejecuta:  pip install -r requirements.txt   Luego vuelve a correr este script."
    exit 1
fi

if systemctl list-unit-files 2>/dev/null | grep -q '^conteo4-nube\.service'; then
    echo "==> Apagando el servicio viejo conteo4-nube (el agente lo reemplaza)..."
    sudo systemctl disable --now conteo4-nube || true
fi

echo "==> Creando el servicio $SERVICIO..."
sudo tee /etc/systemd/system/$SERVICIO.service > /dev/null <<EOF
[Unit]
Description=conteo4 - agente (control remoto del conteo de vehiculos)
After=network-online.target
Wants=network-online.target

[Service]
User=$USUARIO
WorkingDirectory=$DIR
Environment=PYTHONUNBUFFERED=1
ExecStart=$PY $DIR/agente.py
Restart=always
RestartSec=5
# SIGTERM solo al agente: él cierra el conteo limpio (guarda datos, suelta cámaras)
KillMode=mixed
TimeoutStopSec=25

[Install]
WantedBy=multi-user.target
EOF

SC="$(command -v systemctl)"
echo "==> Permiso para reiniciar/apagar la Pi desde la laptop o la web (solo esos comandos)..."
echo "$USUARIO ALL=(root) NOPASSWD: $SC reboot, $SC poweroff" | sudo tee /etc/sudoers.d/$SERVICIO > /dev/null
sudo chmod 440 /etc/sudoers.d/$SERVICIO
if ! sudo visudo -cf /etc/sudoers.d/$SERVICIO > /dev/null; then
    echo "[AVISO] El permiso de sudo no es válido: lo quito (reiniciar/apagar desde lejos no funcionará)."
    sudo rm -f /etc/sudoers.d/$SERVICIO
fi

sudo systemctl daemon-reload
sudo systemctl enable $SERVICIO > /dev/null 2>&1
sudo systemctl restart $SERVICIO
sleep 3

echo
echo "================================================================"
if systemctl is-active --quiet $SERVICIO; then
    echo " Agente CORRIENDO. Ya puedes manejar todo desde la laptop (pestaña Control)"
    echo " o desde el dashboard web (con la clave de control)."
else
    echo " [ERROR] El agente no arrancó. Mira:  sudo journalctl -u $SERVICIO -n 50"
fi
echo
echo " Ver lo que hace (conteo incluido):  sudo journalctl -u $SERVICIO -f"
echo " Detenerlo:                          sudo systemctl stop $SERVICIO"
echo " Quitarlo del arranque:              sudo systemctl disable --now $SERVICIO"
echo "================================================================"
