@echo off
setlocal enabledelayedexpansion

title Allowance - Autonomous Financial Platform
cd /d "%~dp0"

echo ======================================================================
echo   ALLOWANCE (v5.0) - Autonomous AI Spending Control Platform
echo   "Give your AI agent an allowance, not your wallet."
echo ======================================================================
echo.

:: 1. Check Python installation
where python >nul 2>nul
if %ERRORLEVEL% neq 0 (
    echo [ERROR] Python is not installed or not in your system PATH.
    echo Please install Python 3.10+ from https://www.python.org/
    echo.
    pause
    exit /b 1
)

:: Display Python version
for /f "tokens=*" %%v in ('python --version 2^>^&1') do set PYTHON_VER=%%v
echo [*] Python detected: %PYTHON_VER%

:: 2. Check if frontend build exists
if not exist "frontend\dist\index.html" (
    echo [*] Frontend build not found. Attempting to build frontend...
    where npm >nul 2>nul
    if %ERRORLEVEL% equ 0 (
        cd frontend
        echo [*] Installing dependencies and building frontend UI...
        call npm install
        call npm run build
        cd ..
    ) else (
        echo [WARNING] npm is not installed. Frontend static assets may be missing.
    )
) else (
    echo [*] Frontend production bundle verified (frontend\dist)
)

:: 3. Set environment variables
if "%PORT%"=="" set PORT=8000
set PYTHONPATH=.

echo.
echo [*] Starting Allowance Development Server on port %PORT%...
echo [*] Opening Dashboard in your browser: http://localhost:%PORT%
echo.

:: 4. Automatically open default browser after 2 seconds
start "" /b cmd /c "timeout /t 2 /nobreak >nul && start http://localhost:%PORT%"

:: 5. Launch the Server
python scripts/dev_server.py

:: If server terminates, keep window open so error messages are visible
echo.
echo [INFO] Server has terminated.
pause
