@echo off
setlocal
cd /d "%~dp0"

echo Creating Desktop shortcut for Allowance...

powershell -NoProfile -ExecutionPolicy Bypass -Command "$ws = New-Object -ComObject WScript.Shell; $desktop = [Environment]::GetFolderPath('Desktop'); $shortcut = $ws.CreateShortcut(\"$desktop\Allowance.lnk\"); $shortcut.TargetPath = '%~dp0run.bat'; $shortcut.WorkingDirectory = '%~dp0'; $shortcut.Description = 'Allowance - Autonomous Financial Spending Control Platform'; $shortcut.Save()"

if %ERRORLEVEL% equ 0 (
    echo.
    echo ======================================================================
    echo  [SUCCESS] Desktop shortcut 'Allowance' created on your Desktop!
    echo  You can now launch the platform by double-clicking 'Allowance' on your Desktop.
    echo ======================================================================
) else (
    echo [ERROR] Could not create Desktop shortcut.
)
echo.
pause

