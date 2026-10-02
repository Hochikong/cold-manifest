@echo off
rem cold-manifest web server launcher. Default data root: <package root>\data
rem NOTE: keep this file ASCII-only (cmd.exe parses batch files in the OEM codepage,
rem       so non-ASCII bytes here can produce "The syntax of the command is incorrect").
rem       Chinese messages are therefore passed to PowerShell as base64 UTF-16
rem       (-EncodedCommand); they render correctly while this file stays pure ASCII.
rem Force UTF-8 console codepage + Python IO encoding (avoid GBK encode crashes)
chcp 65001 >nul
set "PYTHONIOENCODING=utf-8"
setlocal
cd /d "%~dp0..\.."

if not exist ".venv-win\Scripts\cldm.exe" (
    echo ERROR: .venv-win\Scripts\cldm.exe not found. Run packaging\windows\setup.cmd first.
    exit /b 1
)

rem ---- Admin check: SMART on USB-bridge / physical disks needs elevation ----
net session >nul 2>&1
if errorlevel 1 (
    powershell -NoProfile -EncodedCommand VwByAGkAdABlAC0ASABvAHMAdAAgACcAJwA7ACAAVwByAGkAdABlAC0ASABvAHMAdAAgACcAPQA9AD0APQA9AD0APQA9AD0APQA9AD0APQA9AD0APQA9AD0APQA9AD0APQA9AD0APQA9AD0APQA9AD0APQA9AD0APQA9AD0APQA9AD0APQA9AD0APQA9AD0APQA9AD0APQAnACAALQBGAG8AcgBlAGcAcgBvAHUAbgBkAEMAbwBsAG8AcgAgAFkAZQBsAGwAbwB3ADsAIABXAHIAaQB0AGUALQBIAG8AcwB0ACAAJwBbAGaLSlRdACAAU19NUg1OL2blTqF7BnRYVKuO/U7Qj0yIAjAnACAALQBGAG8AcgBlAGcAcgBvAHUAbgBkAEMAbwBsAG8AcgAgAFkAZQBsAGwAbwB3ADsAIABXAHIAaQB0AGUALQBIAG8AcwB0ACAAJwAgACAAVQBTAEIAIABlaKVj2HYgAC8AIABpcgZ0wXjYdoR2IABTAE0AQQBSAFQAIADHkcaWGk8xWSWNCP9OAFYATQBlACAA2HYNTtdTcV/NVAn/DP8nACAALQBGAG8AcgBlAGcAcgBvAHUAbgBkAEMAbwBsAG8AcgAgAFkAZQBsAGwAbwB3ADsAIABXAHIAaQB0AGUALQBIAG8AcwB0ACAAJwAgACAApWIZlWJfglkgAE8AcABlAG4AIABmAGEAaQBsAGUAZAAsACAARQByAHIAbwByAD0ANQACMCcAIAAtAEYAbwByAGUAZwByAG8AdQBuAGQAQwBvAGwAbwByACAAWQBlAGwAbABvAHcAOwAgAFcAcgBpAHQAZQAtAEgAbwBzAHQAIAAnAD0APQA9AD0APQA9AD0APQA9AD0APQA9AD0APQA9AD0APQA9AD0APQA9AD0APQA9AD0APQA9AD0APQA9AD0APQA9AD0APQA9AD0APQA9AD0APQA9AD0APQA9AD0APQA9AD0AJwAgAC0ARgBvAHIAZQBnAHIAbwB1AG4AZABDAG8AbABvAHIAIABZAGUAbABsAG8AdwA=
    choice /C YN /N /M "Restart as administrator now? (Y/N): "
    if errorlevel 2 goto :run
    rem Re-launch elevated, preserving the working directory (quotes survive
    rem spaces / non-ASCII in the install path). NOTE: keep `exit /b` on its own
    rem line - PowerShell's -Command swallows everything after it on the same line.
    powershell -NoProfile -Command "Start-Process -Verb RunAs -FilePath '%~f0' -WorkingDirectory '%~dp0..\..'"
    exit /b
)

:run
if "%CLDM_DATA_ROOT%"=="" set "CLDM_DATA_ROOT=%~dp0..\..\data"
echo Data root: %CLDM_DATA_ROOT%
echo Serving on http://0.0.0.0:8765  ^(local: http://localhost:8765, Ctrl+C to stop^)
".venv-win\Scripts\cldm.exe" serve --host 0.0.0.0 --port 8765
endlocal
