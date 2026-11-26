@echo off
REM ==========================================================================
REM  iniciar_interfaz.bat - Abre el programa "Monitor de trafico" (doble clic).
REM
REM  La primera vez instala lo que falte (PySide6, pyqtgraph, websockets...).
REM  La Raspberry, en la misma red, se conecta a esta laptop con el comando que
REM  aparece en la pestana "Conexion" del programa.
REM
REM  Opciones (editar la ultima linea):  --puerto 8000  --token cambia-esto
REM  Prueba sin Raspberry:                --falso
REM ==========================================================================
cd /d "%~dp0"
python -c "import PySide6, pyqtgraph, websockets, numpy, cv2" 2>nul || (
    echo Instalando dependencias del programa...
    python -m pip install -r requirements.txt
)
python app.py %*
if errorlevel 1 pause
