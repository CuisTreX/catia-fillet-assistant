@echo off
chcp 65001 >nul
setlocal
set "VENV_PY=F:\CodeX\04-Codex������\tools\catia-fillet-assistant\venv\Scripts\python.exe"
set "HERE=%~dp0"

if not exist "%VENV_PY%" (
  echo [错误] 没找到运行环境，请先运行「安装环境.bat」。
  pause
  exit /b 1
)
"%VENV_PY%" "%HERE%run_assistant.py" --check
pause
