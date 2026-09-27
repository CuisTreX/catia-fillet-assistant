@echo off
chcp 65001 >nul
setlocal
rem Win7 打包：必须用 Python 3.8 的 venv + PyInstaller 4.10，打出来的 exe 才能在 Win7 上跑
set "VENV_PY=D:\Programs\CodexTools\catia-fillet-assistant-win7\venv\Scripts\python.exe"
set "HERE=%~dp0"

if not exist "%VENV_PY%" (
  echo [错误] 没找到 Win7 专用环境：%VENV_PY%
  echo 请先双击「安装环境-Win7-Python38.bat」。
  pause
  exit /b 1
)

"%VENV_PY%" "%HERE%tools\build_portable.py" --pyinstaller-version 4.10
pause
