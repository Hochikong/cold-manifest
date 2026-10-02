@echo off
rem cold-manifest CLI wrapper. Example: cldm.cmd collect D:\ --serial XXX
rem NOTE: keep this file ASCII-only (cmd.exe parses batch files in the OEM codepage)
rem Force UTF-8 console codepage + Python IO encoding (P1-1: avoid GBK encode crashes)
chcp 65001 >nul
set "PYTHONIOENCODING=utf-8"
setlocal
set "HERE=%~dp0..\.."
if not exist "%HERE%\.venv-win\Scripts\cldm.exe" (
    echo ERROR: .venv-win\Scripts\cldm.exe not found. Run packaging\windows\setup.cmd first.
    exit /b 1
)

rem ---- Non-admin notice: SMART on USB-bridge / PhysicalDrive disks will fail ----
net session >nul 2>&1
if errorlevel 1 (
    rem Chinese warning text goes through PowerShell so it renders regardless
    rem of the batch file's own codepage (this .cmd stays pure ASCII).
    powershell -NoProfile -Command "Write-Host '[WARNING] 未以管理员身份运行：SMART 采集 USB 桥接盘/PhysicalDrive 会失败（NVMe 盘不受影响）。建议以管理员身份运行本命令。' -ForegroundColor Yellow"
)

rem Default data root: <package root>\data (same as start.cmd); an explicit
rem --data-root argument always wins.
if "%CLDM_DATA_ROOT%"=="" set "CLDM_DATA_ROOT=%HERE%\data"
"%HERE%\.venv-win\Scripts\cldm.exe" %*
endlocal
