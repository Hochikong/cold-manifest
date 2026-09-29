@echo off
rem cold-manifest Windows offline setup: create venv and install deps from wheels\
rem NOTE: keep this file ASCII-only (cmd.exe parses batch files in the OEM codepage)
setlocal
cd /d "%~dp0..\.."

echo [1/3] Creating venv .venv-win ...
py -3 -m venv .venv-win
if errorlevel 1 (
    echo ERROR: failed to create venv. Install Python 3.10+ with the py launcher first.
    echo Fallback ^(online^): .venv-win\Scripts\python -m pip install -e .
    exit /b 1
)

echo [2/3] Upgrading pip from wheels\ ...
".venv-win\Scripts\python" -m pip install --no-index --find-links wheels --upgrade pip
if errorlevel 1 echo WARN: pip upgrade failed, continuing.

echo [3/3] Installing cold-manifest offline from wheels\ ...
".venv-win\Scripts\python" -m pip install --no-index --find-links wheels -e .
if errorlevel 1 (
    echo ERROR: offline install failed. Check:
    echo   1. wheels\ exists and is complete - re-extract the full zip
    echo   2. Python is 3.10 - wheels are built for cp310 / win_amd64
    echo Fallback ^(online^): .venv-win\Scripts\python -m pip install -e .
    exit /b 1
)

".venv-win\Scripts\cldm.exe" --version
if errorlevel 1 exit /b 1
echo Setup done. Run packaging\windows\start.cmd to start the web server.
endlocal
