@echo off
chcp 65001 >nul
setlocal
set "TARGET_DIR=F:\CodeX\04-Codex������\tools\catia-fillet-assistant"
set "VENV_PY=%TARGET_DIR%\venv\Scripts\python.exe"

if not exist "%TARGET_DIR%" mkdir "%TARGET_DIR%"
if not exist "%VENV_PY%" (
  echo 正在创建虚拟环境：%TARGET_DIR%\venv
  python -m venv "%TARGET_DIR%\venv" || goto :fail
)
echo 正在安装依赖（pywin32 + comtypes）...
"%VENV_PY%" -m pip install --upgrade pip pywin32 comtypes || goto :fail
echo 依赖安装完成。
exit /b 0

:fail
echo [错误] 安装失败，请检查 Python 是否在 PATH 中。
pause
exit /b 1
