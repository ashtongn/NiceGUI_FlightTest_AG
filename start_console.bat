@echo off
REM start_console.bat: the whole DPMG console on a Windows laptop, natively.
REM
REM   start_console.bat        GUI on http://localhost:8080, with the operator
REM                            view (/), the engineering view (/engineering)
REM                            and the mission planner (/mission)
REM
REM Creates or reuses .venv next to this file, installs requirements-windows.txt,
REM starts the GUI (run_gui_link.py 8080) in its own window, then opens the
REM operator view and the mission planner in the browser. Close the window, or
REM press Ctrl-C in it, to stop the GUI.
REM
REM Radios are reached by their saved addresses (Connect by IP in the SDR tab
REM writes output\saved_radios.json); the mission planner uses the
REM same list. UDP discovery is not relied on here. Windows Firewall must allow
REM inbound UDP to this venv's python.exe; see docs\WINDOWS_CONSOLE.md.
REM
REM Offline maps for the planner: tools\maps\vendor_map_assets.py and
REM tools\maps\build_base_map.py, run once with the venv's python.
setlocal
cd /d "%~dp0"
set "REPO=%CD%"
set "GUI_PORT=8080"

if not "%~1"=="" (
    echo Note: start_console.bat takes no arguments. Add radios with Connect by IP
    echo       in the SDR tab (Remote Radios); the planner uses the same saved list.
)

REM --- find a Python 3 interpreter (py launcher first, then python) ----------
set "PY="
py -3 -c "import sys; assert sys.version_info >= (3, 10)" >nul 2>&1 && set "PY=py -3"
if not defined PY python -c "import sys; assert sys.version_info >= (3, 10)" >nul 2>&1 && set "PY=python"
if not defined PY (
    echo ERROR: no Python 3.10+ found. Install Python 3.11+ from python.org, tick "Add to PATH"
    echo        and keep "tcl/tk and IDLE" ticked ^(the GUI imports tkinter at load^).
    exit /b 1
)

REM --- venv ------------------------------------------------------------------
if not exist ".venv\Scripts\python.exe" (
    echo creating virtual environment .venv
    %PY% -m venv .venv || (echo ERROR: venv creation failed & exit /b 1)
)
call ".venv\Scripts\activate.bat"

echo installing console requirements ^(requirements-windows.txt^)
python -m pip install -q --disable-pip-version-check -r requirements-windows.txt || (
    echo ERROR: pip install failed. Is the laptop online, or is a wheel cache configured?
    exit /b 1
)
python -c "import tkinter" >nul 2>&1 || echo WARNING: tkinter is missing; rerun the python.org installer with "tcl/tk and IDLE" ticked.

REM --- the GUI keeps its saved radio list in the repo's output folder ----------
set "DPMG_SAVED_RADIOS=%REPO%\output\saved_radios.json"
if not exist "%REPO%\output" mkdir "%REPO%\output"

REM --- GUI -------------------------------------------------------------------
echo starting the GUI on http://localhost:%GUI_PORT%
start "DPMG GUI (%GUI_PORT%)" cmd /k ""%REPO%\.venv\Scripts\python.exe" run_gui_link.py %GUI_PORT%"

REM --- open the browser once the GUI answers (background poller) --------------
start "" /b cmd /c "for /l %%i in (1,1,120) do (curl -fs -o NUL http://localhost:%GUI_PORT% >nul 2>&1 && (start "" http://localhost:%GUI_PORT% & start "" http://localhost:%GUI_PORT%/mission & exit /b) || timeout /t 1 /nobreak >nul)"

echo.
echo GUI:     http://localhost:%GUI_PORT%
echo planner: http://localhost:%GUI_PORT%/mission
echo The GUI runs in its own window; close the window to stop it.
endlocal
