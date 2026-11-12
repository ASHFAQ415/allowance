@echo off
REM ALLOWANCE Desktop Dongle - Windows Startup Launcher
REM Place this in shell:startup OR run with --enable-startup to register via registry.

set SCRIPT=%~dp0scripts\allowance_tray.py
set BACKEND=%ALLOWANCE_BACKEND%
if "%BACKEND%"=="" set BACKEND=http://localhost:8000

:: Start the backend in a minimized window, then launch the tray
start /min "" py -3.11 "%~dp0scripts\dev_server.py"
ping -n 3 127.0.0.1 >nul

:: Launch the tray widget (no console window)
start "" pyw -3.11 "%SCRIPT%"

