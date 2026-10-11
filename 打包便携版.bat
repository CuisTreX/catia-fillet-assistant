@echo off
chcp 65001 >nul
setlocal
set "VENV_PY=F:\CodeX\04-Codex¹¤×÷Çø\tools\catia-fillet-assistant\venv\Scripts\python.exe"
set "HERE=%~dp0"

if not exist "%VENV_PY%" (
  echo [ERROR] venv not found: %VENV_PY%
  echo Please run "install env" bat first.
  pause
  exit /b 1
)

"%VENV_PY%" "%HERE%tools\build_portable.py"
pause
