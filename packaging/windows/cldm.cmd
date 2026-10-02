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

rem ---- Non-admin notice: SMART on USB-bridge / physical disks will fail ----
net session >nul 2>&1
if errorlevel 1 (
    rem Chinese warning goes through PowerShell as base64 UTF-16 (-EncodedCommand)
    rem so it renders correctly while this .cmd stays pure ASCII.
    powershell -NoProfile -EncodedCommand VwByAGkAdABlAC0ASABvAHMAdAAgACcAKmflTqF7BnRYVKuO/U7Qj0yIGv9TAE0AQQBSAFQAIADHkcaWIABVAFMAQgAgAGVopWPYdi8AaXIGdMF42HYaTzFZJY0I/04AVgBNAGUAIAANTtdTcV/NVAn/DP/6Xq6L5U6hewZ0WFSrjv1O0I9MiCxnfVTkTgIwJwAgAC0ARgBvAHIAZQBnAHIAbwB1AG4AZABDAG8AbABvAHIAIABZAGUAbABsAG8AdwA=
)

rem Default data root: <package root>\data (same as start.cmd); an explicit
rem --data-root argument always wins.
if "%CLDM_DATA_ROOT%"=="" set "CLDM_DATA_ROOT=%HERE%\data"
"%HERE%\.venv-win\Scripts\cldm.exe" %*
endlocal
