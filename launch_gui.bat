@echo off
REM launch_gui.bat: MOTHBALL FURY ground-station GUI, natively on Windows.
REM
REM   launch_gui.bat              GUI on http://localhost:8080 (Ctrl-C stops)
REM   launch_gui.bat 8081         alternate TCP port
REM   launch_gui.bat 8081 47841   ...and an alternate UDP discovery port, so this
REM                               GUI can run beside another one holding 47811
REM
REM Creates/activates .venv next to this file, installs requirements-gui.txt
REM (the GUI set: no posix_ipc, no libsidekiq; the ground station never loads
REM the radio library) and runs run_gui_link.py. Mirrors launch_gui.sh --native.
REM
REM Windows Firewall must allow inbound UDP to this venv's python.exe (ANNOUNCE
REM on 47811 and the telemetry replies on the GUI's link port); see
REM docs/WINDOWS_CONSOLE.md.
setlocal
cd /d "%~dp0"

set "PORT=%~1"
if "%PORT%"=="" set "PORT=8080"
set "DISCO=%~2"
set "URL=http://localhost:%PORT%"

REM --- find a Python 3 interpreter (py launcher first, then python) ----------
set "PY="
py -3 -c "import sys" >nul 2>&1 && set "PY=py -3"
if not defined PY python -c "import sys" >nul 2>&1 && set "PY=python"
if not defined PY (
    echo ERROR: no Python 3 found. Install Python 3.11+ from python.org and tick "Add to PATH".
    exit /b 1
)

REM --- venv ------------------------------------------------------------------
if not exist ".venv\Scripts\python.exe" (
    echo creating virtual environment .venv
    %PY% -m venv .venv || (echo ERROR: venv creation failed & exit /b 1)
)
call ".venv\Scripts\activate.bat"

echo installing GUI requirements (requirements-gui.txt)
python -m pip install -q --disable-pip-version-check -r requirements-gui.txt || (
    echo ERROR: pip install failed. Is the laptop online, or is a wheel cache configured?
    exit /b 1
)

REM --- open the browser once the GUI answers (background poller) -------------
start "" /b cmd /c "for /l %%i in (1,1,120) do (curl -fs -o NUL %URL% >nul 2>&1 && (start "" %URL% & exit /b) || timeout /t 1 /nobreak >nul)"

echo starting native GUI on %URL% (Ctrl-C to stop)
if "%DISCO%"=="" (
    python run_gui_link.py %PORT%
) else (
    echo discovery port %DISCO%
    python run_gui_link.py %PORT% %DISCO%
)
endlocal
