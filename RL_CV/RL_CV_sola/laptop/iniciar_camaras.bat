@echo off
REM ============================================================================
REM iniciar_camaras.bat  -  DOBLE CLIC: simula las 4 camaras IP con tus videos.
REM Usa los .mp4 de V_final_sola\laptop\videos (maximo 4). Si no hay ninguno, usa
REM los de V_final_final\laptop\videos. Camaras en los puertos 8080, 8081, 8082, 8083.
REM (Opcional, desde una consola: iniciar_camaras.bat 9000  -> empieza en el puerto 9000)
REM Para detener: cierra esta ventana o pulsa Ctrl+C.
REM ============================================================================
setlocal EnableDelayedExpansion
cd /d "%~dp0"
title Camaras simuladas (V_final_sola)

set "PUERTO=8080"
if not "%~1"=="" set "PUERTO=%~1"

REM --- Python ---
set "PY="
where python >nul 2>nul && set "PY=python"
if not defined PY where py >nul 2>nul && set "PY=py"
if not defined PY (
    echo [ERROR] No encuentro Python. Instalalo desde https://www.python.org
    echo         marcando "Add python.exe to PATH" y vuelve a abrir este archivo.
    pause
    exit /b 1
)

REM --- OpenCV (lo unico que necesita camfalsa4.py) ---
%PY% -c "import cv2" >nul 2>nul
if errorlevel 1 (
    echo [INFO] Instalando opencv-python (solo la primera vez^)...
    %PY% -m pip install opencv-python
)

REM --- Videos: primero los de esta carpeta, si no los de V_final_final\laptop\videos ---
set "CARPETA=%~dp0videos"
call :buscar
if !N! EQU 0 (
    for %%D in ("%~dp0..\..\laptop\videos") do set "CARPETA=%%~fD"
    call :buscar
)
if !N! EQU 0 (
    echo [ERROR] No hay videos .mp4 en:
    echo         %~dp0videos
    echo         ni en !CARPETA!
    echo         Copia tus videos a la carpeta videos y vuelve a abrir este archivo.
    pause
    exit /b 1
)

echo ============================================================
echo  Videos de: !CARPETA!
echo  !N! camara(s) desde el puerto %PUERTO%. Abajo salen las URLs:
echo  ponlas en CAMARAS de conteo4.py en la Raspberry (o pasalas
echo  por la consola). En el navegador de esta PC puedes verlas en
echo  http://localhost:%PUERTO%/video
echo  Si Windows pregunta por el firewall: "Redes privadas" - Permitir.
echo ============================================================
%PY% camfalsa4.py !LISTA! --puerto %PUERTO%
echo.
echo Camaras detenidas.
pause
exit /b 0

:buscar
set "LISTA="
set /a N=0
if not exist "!CARPETA!\" exit /b 0
for /f "delims=" %%F in ('dir /b /a-d /on "!CARPETA!\*.mp4" 2^>nul') do (
    if !N! LSS 4 (
        set "LISTA=!LISTA! "!CARPETA!\%%F""
        set /a N+=1
    )
)
exit /b 0
