@echo off
chcp 65001 >nul
setlocal
rem ============================================================================
rem  Win7 专用安装脚本：用 Python 3.8 建一个**独立** venv（不影响现有的 3.13 环境）
rem  原因：Python 3.9+ 的 exe 在 Win7 上跑不起来，必须用 3.8.x
rem ============================================================================
set "TARGET_DIR=D:\Programs\CodexTools\catia-fillet-assistant-win7"
set "VENV_PY=%TARGET_DIR%\venv\Scripts\python.exe"
set "HERE=%~dp0"

rem 优先用 py -3.8 启动器；没有的话试 python3.8；再不行请手动改 PY38
set "PY38=py -3.8"
%PY38% -c "import sys;print(sys.version)" >nul 2>nul
if errorlevel 1 (
  set "PY38=python3.8"
  %PY38% -c "import sys;print(sys.version)" >nul 2>nul
)
if errorlevel 1 (
  echo [错误] 没找到 Python 3.8。
  echo        请先装 Python 3.8.x（python.org，Windows 7 SP1 可用），
  echo        然后把本文件里的 PY38 改成它的完整路径，例如：
  echo            set "PY38=C:\Python38\python.exe"
  pause
  exit /b 1
)

if not exist "%TARGET_DIR%" mkdir "%TARGET_DIR%"
if not exist "%VENV_PY%" (
  echo 正在创建 Win7 专用虚拟环境：%TARGET_DIR%\venv
  %PY38% -m venv "%TARGET_DIR%\venv" || goto :fail
)
echo 正在安装 Win7 版依赖（comtypes / pywin32 / pyinstaller，全部锁定老版本）...
"%VENV_PY%" -m pip install --upgrade pip || goto :fail
"%VENV_PY%" -m pip install -r "%HERE%requirements-win7.txt" || goto :fail
echo.
echo 依赖安装完成。运行自检：
"%VENV_PY%" "%HERE%run_assistant.py" --check
echo.
echo 打包 Win7 便携版：双击「打包便携版-Win7.bat」
exit /b 0

:fail
echo [错误] 安装失败。请检查 Python 3.8 与网络（或改用离线 wheel 包）。
pause
exit /b 1
