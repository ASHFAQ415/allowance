@echo off
:: ALLOWANCE Desktop Dongle — Windows Startup Launcher
:: Place this in shell:startup  OR run with --enable-startup to register via registry.

set SCRIPT=%~dp0scripts\allowance_tray.py
set BACKEND=%ALLOWANCE_BACKEND%
if "%BACKEND%"=="" set BACKEND=http://localhost:8000

:: Start the backend in a minimized window, then launch the tray
start "ALLOWANCE Backend" /min cmd /c "py -3.11 scripts\dev_server.py"
timeout /t 2 /nobreak >nul

:: Launch the tray widget (no console window)
start "ALLOWANCE Tray" pythonw "%SCRIPT%"
