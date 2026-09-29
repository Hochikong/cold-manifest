@echo off
rem cold-manifest CLI wrapper. Example: cldm.cmd collect D:\ --serial XXX
rem NOTE: keep this file ASCII-only (cmd.exe parses batch files in the OEM codepage)
setlocal
set "HERE=%~dp0..\.."
if not exist "%HERE%\.venv-win\Scripts\cldm.exe" (
    echo ERROR: .venv-win\Scripts\cldm.exe not found. Run packaging\windows\setup.cmd first.
    exit /b 1
)
"%HERE%\.venv-win\Scripts\cldm.exe" %*
endlocal
