@echo off
REM cd to this script's own folder (the project root), wherever it lives.
cd /d "%~dp0"

REM Open the control UI in the default browser ~4s after startup, in a parallel
REM process (python run.py below blocks until the server stops).
start "" /min cmd /c "ping -n 5 127.0.0.1 >nul & start "" "http://localhost:8080""

if exist "venv\Scripts\python.exe" (
    "venv\Scripts\python.exe" run.py
) else (
    python run.py
)

pause
