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

if "%CLDM_DATA_ROOT%"=="" set "CLDM_DATA_ROOT=%~dp0..\..\data"
echo Data root: %CLDM_DATA_ROOT%
echo Serving on http://0.0.0.0:8765  ^(local: http://localhost:8765, Ctrl+C to stop^)
".venv-win\Scripts\cldm.exe" serve --host 0.0.0.0 --port 8765
endlocal
