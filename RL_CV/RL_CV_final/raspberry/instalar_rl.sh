#!/usr/bin/env bash
# =============================================================================
# instalar_rl.sh  -  Deja el SEMÁFORO (principal_pi.py + modelo RL) y el CONTEO (YOLO) como
# servicios de la Raspberry: arrancan solos al encenderla y se reviven si se caen.
# Sirve para las 4 versiones (RL_CV_sola / _aws / _laptop / _final): detecta cuál es.
#
# Uso (en la Pi, con el entorno virtual de YOLO ACTIVADO si usas uno):
#     source ~/yolo_env/bin/activate        # (el nombre de tu entorno)
#     cd ~/RL_CV_final/raspberry            # (la carpeta raspberry de tu versión)
#     bash instalar_rl.sh
#
# Qué hace:
#   1. Comprueba el python de YOLO (ultralytics, ncnn, OpenCV) y el del semáforo
#      (python3 del sistema con numpy y gpiozero; si faltan, los instala con apt).
#   2. Conteo: versión final -> el agente (instalar_pi.sh) lanza el conteo; las demás ->
#      servicio conteo4-rl (y apaga los servicios viejos conteo4 / conteo4-aws / -laptop).
#   3. Servicio semaforo-rl: principal_pi.py --modo tiempo_real --velocidad 1, con prioridad
#      de CPU ALTA (el reloj de 1 s y el ámbar no deben atrasarse por YOLO) y KillSignal=SIGINT
#      (al detenerlo pone todo en ROJO y suelta los pines; con SIGTERM quedarían como estaban).
# Volver a ejecutarlo es seguro (actualiza los servicios).
# =============================================================================
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
USUARIO="$(id -un)"

if [ -n "${VIRTUAL_ENV:-}" ]; then PY="$VIRTUAL_ENV/bin/python"; else PY="${PYTHON:-$(command -v python3)}"; fi
PY_SEM="/usr/bin/python3"
echo "==> Python de YOLO:      $PY"
echo "==> Python del semáforo: $PY_SEM"

if ! "$PY" -c "import cv2, numpy, yaml, ncnn" 2>/dev/null; then
    echo "[ERROR] Al python de YOLO le faltan librerías. Activa el entorno de YOLO y:  pip install -r requirements.txt"
    exit 1
fi
if ! "$PY_SEM" -c "import numpy, gpiozero" 2>/dev/null; then
    echo "==> Instalando numpy y gpiozero para el semáforo (apt)..."
    sudo apt-get update -qq
    sudo apt-get install -y -qq python3-numpy python3-gpiozero
fi
if ! id -nG "$USUARIO" | grep -qw gpio; then
    echo "==> Agregando $USUARIO al grupo gpio (para manejar los pines)..."
    sudo usermod -aG gpio "$USUARIO"
fi

# ---- 1) conteo (YOLO + enlace al semáforo en 127.0.0.1:5050)
if [ -f "$DIR/agente.py" ]; then
    VERSION=final
    SERV_CONTEO=conteo4-agente
    if ! systemctl list-unit-files 2>/dev/null | grep -q "^$SERV_CONTEO\.service"; then
        echo "==> Instalando el agente (instalar_pi.sh): él lanza el conteo..."
        bash "$DIR/instalar_pi.sh"
    else
        echo "==> El agente ya está instalado: actualizo su servicio por si cambió la carpeta..."
        bash "$DIR/instalar_pi.sh"
    fi
else
    if [ -f "$DIR/conteo4_aws.py" ]; then VERSION=aws; SCRIPT=conteo4_aws.py; VIEJO=conteo4-aws
    elif [ -f "$DIR/conteo4_laptop.py" ]; then VERSION=laptop; SCRIPT=conteo4_laptop.py; VIEJO=conteo4-laptop
    else VERSION=sola; SCRIPT=conteo4.py; VIEJO=conteo4; fi
    SERV_CONTEO=conteo4-rl
    for s in conteo4 conteo4-aws conteo4-laptop conteo4-nube conteo4-agente; do
        if systemctl list-unit-files 2>/dev/null | grep -q "^$s\.service"; then
            echo "==> Apagando el servicio viejo $s (con dos se contaría dos veces y chocarían en el puerto 5050)..."
            sudo systemctl disable --now "$s" || true
        fi
    done
    echo "==> Creando el servicio $SERV_CONTEO ($SCRIPT)..."
    sudo tee /etc/systemd/system/$SERV_CONTEO.service > /dev/null <<EOF
[Unit]
Description=conteo4 + enlace con el semaforo RL ($VERSION)
After=network-online.target
Wants=network-online.target

[Service]
User=$USUARIO
WorkingDirectory=$DIR
Environment=PYTHONUNBUFFERED=1
ExecStart=$PY $DIR/$SCRIPT --sin-pantalla
# YOLO con prioridad algo menor: el semáforo nunca debe esperar por él
Nice=5
Restart=always
RestartSec=5
KillSignal=SIGINT
TimeoutStopSec=20

[Install]
WantedBy=multi-user.target
EOF
fi
echo "==> Versión detectada: $VERSION (conteo: $SERV_CONTEO)"

# ---- 2) semáforo
echo "==> Creando el servicio semaforo-rl..."
sudo tee /etc/systemd/system/semaforo-rl.service > /dev/null <<EOF
[Unit]
Description=Semaforo con RL (principal_pi.py) - conteos de YOLO por 127.0.0.1:5050
After=$SERV_CONTEO.service
Wants=$SERV_CONTEO.service

[Service]
User=$USUARIO
WorkingDirectory=$DIR/semaforo
Environment=PYTHONUNBUFFERED=1
ExecStart=$PY_SEM $DIR/semaforo/principal_pi.py --modo tiempo_real --velocidad 1
# prioridad de CPU alta: el reloj de 1 s (ámbar, verdes) no debe atrasarse aunque YOLO
# use los 4 núcleos
Nice=-10
# SIGINT = Ctrl+C: principal_pi.py pone todo en ROJO y suelta los pines antes de salir
KillSignal=SIGINT
TimeoutStopSec=10
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
if [ "$VERSION" != final ]; then
    sudo systemctl enable "$SERV_CONTEO" > /dev/null 2>&1
    sudo systemctl restart "$SERV_CONTEO"
fi
sudo systemctl enable semaforo-rl > /dev/null 2>&1
sudo systemctl restart semaforo-rl
sleep 3
echo
echo "================================================================"
for s in $SERV_CONTEO semaforo-rl; do
    if systemctl is-active --quiet $s; then echo " $s: CORRIENDO"; else echo " $s: [ERROR] no arrancó -> sudo journalctl -u $s -n 50"; fi
done
echo
echo " Ver el semáforo en vivo:   sudo journalctl -u semaforo-rl -f"
echo " Ver el conteo / enlace:    sudo journalctl -u $SERV_CONTEO -f | grep RL"
echo " Registro de cada corrida:  $DIR/semaforo/registros/pi_*.csv"
echo " Detener el semáforo (queda todo en rojo):  sudo systemctl stop semaforo-rl"
if [ "$VERSION" = final ]; then
    echo " El conteo lo inicia el agente: 'Iniciar todo' en la laptop o en la web."
fi
echo " (Si se te agregó al grupo gpio, reinicia la Pi una vez: sudo reboot)"
echo "================================================================"
