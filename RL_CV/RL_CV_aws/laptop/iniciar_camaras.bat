@echo off
rem ==========================================================================
rem iniciar_camaras.bat - DOBLE CLIC: enciende las 4 camaras IP simuladas
rem (camfalsa4.py) para que la Raspberry las lea en los puertos 8080..8083.
rem
rem Videos: usa los .mp4 de la carpeta "videos" (junto a este archivo). Si no
rem hay ninguno, usa los de V_final_final\laptop\videos. Toma maximo 4, en
rem orden alfabetico (Cam1 = el primero, Cam2 = el segundo...).
rem Calidad JPEG 70: la Pi reenvia estos cuadros tal cual a AWS, asi se sube
rem ~9 %% menos que con 75 y la imagen se ve igual.
rem
rem Opcional (sin editar este archivo): set CAM_PUERTO=8090 / set CAM_CALIDAD=60
rem antes de ejecutarlo desde una consola.
rem Para detener: Ctrl+C o cerrar esta ventana.
rem ==========================================================================
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"
title Camaras simuladas (camfalsa4) - NO cierres esta ventana

if not defined CAM_PUERTO set "CAM_PUERTO=8080"
if not defined CAM_CALIDAD set "CAM_CALIDAD=70"

where python >nul 2>nul
if errorlevel 1 (
    echo [ERROR] No encuentro Python. Instala Python desde python.org y marca
    echo         "Add python.exe to PATH" durante la instalacion.
    goto fin
)

python -c "import cv2" >nul 2>nul
if errorlevel 1 (
    echo Falta OpenCV. Instalando opencv-python ^(solo la primera vez^)...
    python -m pip install opencv-python
    if errorlevel 1 (
        echo [ERROR] No se pudo instalar opencv-python. Revisa tu internet.
        goto fin
    )
)

set "CARPETA=%~dp0videos"
if not exist "%CARPETA%\*.mp4" set "CARPETA=%~dp0..\..\laptop\videos"
if not exist "%CARPETA%\*.mp4" (
    echo [ERROR] No hay videos .mp4 ni en "%~dp0videos"
    echo         ni en "%~dp0..\..\laptop\videos".
    echo         Copia tus videos ^(prueba_1.mp4 ...^) a la carpeta videos.
    goto fin
)
for %%I in ("%CARPETA%") do set "CARPETA=%%~fI"

set "LISTA="
set /a N=0
for /f "delims=" %%F in ('dir /b /a-d /on "%CARPETA%\*.mp4"') do (
    if !N! LSS 4 (
        set "LISTA=!LISTA! "%CARPETA%\%%F""
        set /a N+=1
    )
)

echo Carpeta de videos: %CARPETA%
echo Videos usados ^(!N!^):%LISTA%
echo Puerto inicial: %CAM_PUERTO%   Calidad JPEG: %CAM_CALIDAD%
echo.
echo Si Windows pregunta por el FIREWALL, marca "Redes privadas" y pulsa Permitir
echo ^(si no, la Raspberry no podra leer las camaras^).
echo.
python camfalsa4.py %LISTA% --puerto %CAM_PUERTO% --calidad %CAM_CALIDAD%

:fin
echo.
pause
