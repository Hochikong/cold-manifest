@echo off
rem cold-manifest web server launcher. Default data root: <package root>\data
rem NOTE: keep this file ASCII-only (cmd.exe parses batch files in the OEM codepage)
rem Force UTF-8 console codepage + Python IO encoding (P1-1: avoid GBK encode crashes)
chcp 65001 >nul
set "PYTHONIOENCODING=utf-8"
setlocal
cd /d "%~dp0..\.."

if not exist ".venv-win\Scripts\cldm.exe" (
    echo ERROR: .venv-win\Scripts\cldm.exe not found. Run packaging\windows\setup.cmd first.
    exit /b 1
)

rem ---- Admin check: SMART on USB-bridge / PhysicalDrive disks needs elevation ----
net session >nul 2>&1
if errorlevel 1 (
    rem Chinese warning text goes through PowerShell so it renders regardless
    rem of the batch file's own codepage (this .cmd stays pure ASCII).
    powershell -NoProfile -Command "Write-Host ''; Write-Host '=================================================' -ForegroundColor Yellow; Write-Host '[WARNING] 当前不是管理员身份运行。' -ForegroundColor Yellow; Write-Host '非管理员时 SMART 采集会失败（USB 桥接盘/PhysicalDrive 需要管理员权限，' -ForegroundColor Yellow; Write-Host 'NVMe 盘不受影响）。报错形如 Open failed, Error=5。' -ForegroundColor Yellow; Write-Host '=================================================' -ForegroundColor Yellow"
    choice /C YN /N /M "Restart as administrator now? (Y/N): "
    if errorlevel 2 goto :run
    rem Re-launch elevated, preserving the working directory (quotes survive
    rem spaces / non-ASCII in the install path).
    powershell -NoProfile -Command "Start-Process -Verb RunAs -FilePath '%~f0' -WorkingDirectory '%~dp0..\..'"; exit /b
)

:run
if "%CLDM_DATA_ROOT%"=="" set "CLDM_DATA_ROOT=%~dp0..\..\data"
echo Data root: %CLDM_DATA_ROOT%
echo Serving on http://0.0.0.0:8765  ^(local: http://localhost:8765, Ctrl+C to stop^)
".venv-win\Scripts\cldm.exe" serve --host 0.0.0.0 --port 8765
endlocal
