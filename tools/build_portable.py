"""打包「便携版」：单文件 exe + 配置文件 + 启动/诊断脚本 + 中文说明。

用法（在项目目录下）：
    D:\\Programs\\CodexTools\\catia-fillet-assistant\\venv\\Scripts\\python.exe tools\\build_portable.py

产物：
    dist\\CATIA倒圆角助手-便携版\\
        CatiaFilletAssistant.exe     单文件，自带 Python/pywin32/comtypes/Tk
        config.json                  默认配置（可改热键/对称方式/catia_bin）
        启动助手.bat / 诊断.bat       双击即用
        使用说明.txt                 一页说明（UTF-8 BOM，记事本可读）
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
DIST = ROOT / "dist"

sys.path.insert(0, str(SRC))
from catia_fillet_assistant import __version__ as ASSISTANT_VERSION  # noqa: E402

PACKAGE_NAME = f"CATIA倒圆角助手-便携版-v{ASSISTANT_VERSION}"
PACKAGE_DIR = DIST / PACKAGE_NAME
DIR_MODE_NAME = f"CATIA倒圆角助手-文件夹模式-v{ASSISTANT_VERSION}"
DIR_MODE_PACKAGE = DIST / DIR_MODE_NAME
EXE_NAME = "CatiaFilletAssistant"
BUILD_DIR = ROOT / "build"

HIDDEN_IMPORTS = [
    "win32com.client",
    "win32com.client.dynamic",
    "pythoncom",
    "pywintypes",
    "win32api",
    "win32con",
    "win32gui",
    "win32process",
    "win32timezone",
    "comtypes.client",
    "comtypes.automation",
    "comtypes.safearray",
    "tkinter",
    "tkinter.ttk",
]

README_TXT = """CATIA 倒圆角助手（便携版）
=================================

【怎么用】
1. 把整个文件夹拷到目标电脑（不需要装 Python、不需要联网、不写注册表）。
2. 双击「CatiaFilletAssistant.exe」，右上角会弹一条"已启动"，助手就在后台工作。
3. 打开 CATIA 正常做倒圆角：
   - 选到面（不是边线）会立刻弹红色提醒，可一键"移除误选"；
   - 选中一条边线后 按鼠标右键（或 Ctrl+Alt+R）= 自动算出对称边并【直接生成倒圆角】；
     半径优先取「倒圆角定义」对话框里填的值，对话框没开就用 config.json 的 fillet_radius（默认 5）；
   - 在倒圆角对话框里点选的边也支持：助手会读到它、算出对称边、关掉对话框后直接出圆角。
4. 退出：在 CATIA 里按 Ctrl+Alt+Q。

【关于右键】
默认 rmb_mode = "any"：只要当前选中的是边线，右键就被助手接管（不再弹 CATIA 原生右键菜单）。
想保留原生菜单：config.json 里改成 "dialog"（仅倒圆角对话框内接管）或 "off"（只用热键）。

【热键】
Ctrl+Alt+R  补对称边线
Ctrl+Alt+X  移除误选
Ctrl+Alt+F  打开「边线倒圆角」命令
Ctrl+Alt+Q  退出助手

【首次在新电脑上，建议先做一次自检】
双击「诊断.bat」，会生成「诊断报告.txt」并弹窗显示，里面包含：
  - 程序位置、可写目录、comtypes 缓存目录
  - 找到的 CATIA 类型库目录（也是判断能不能用的关键）
  - CATIA 是否连上、当前文档、倒圆角对话框是否被识别
  - 当前选择集里每个元素的判定结果（边线/面）

【退出 / 卸载】
· 退出助手：双击「退出助手.bat」（或快捷方式「退出助手」），也可以在 CATIA 里按 Ctrl+Alt+Q。
· 卸载助手：双击「卸载助手.bat」（或快捷方式「卸载助手」），会有【两次确认】，
  确认后：退出正在运行的助手 → 删除程序目录与日志目录（%LOCALAPPDATA%\\CatiaFilletAssistant）。
  不会动 CATIA 本身（不写注册表、不改 CATIA 设置）。
  静默卸载（脚本用，跳过确认）：CatiaFilletAssistant.exe --uninstall --yes

【如果诊断里说"找不到 CATIA 类型库目录"】
打开 config.json，把 catia_bin 改成你这台机器 CATIA 的实际目录，例如：
  "catia_bin": "D:\\Programs\\CATIA\\win_b64\\code\\bin"
（该目录里应当有 InfTypeLib.tlb、MecModTypeLib.tlb、SPATypeLib.tlb）

【如果对话框没被识别（英文/其它语言版本）】
在 config.json 的 extra_dialog_titles 里追加你机器上的对话框标题，例如：
  "extra_dialog_titles": ["Edge Fillet Definition"]

【配置文件 config.json 常用项】
symmetry_mode      auto(默认，多种对称时弹窗让你选) | mirror | rotate | off
mirror_axis        镜像用法向轴：x / y / z（穿过零件重心的中面）
rotate_axis/angle  旋转对称：轴 + 角度
right_click_pairing 右键补线开关
rmb_mode        any(默认) | dialog | off —— 右键接管范围
symmetric_action fillet(默认，直接生成倒圆角) | select(只把对称边加进选择集)
fillet_radius   对话框没开时用的圆角半径（默认 5）
auto_remove_wrong_pick  选到面时自动移除（默认 false，只提醒）
hotkey_*           四个热键
poll_interval      监视轮询间隔（秒）

【日志】
logs\\assistant.log（若程序目录不可写，日志会写到
%LOCALAPPDATA%\\CatiaFilletAssistant\\logs\\assistant.log）

【已知限制】
1. 对称补线目前支持"由草图元素生成"的边线（Pad/Pocket/回转体等）；
   圆角、阵列、曲面生成的边会提示"暂不支持"，不会乱选。
2. 一条边可能有多种对称解释（比如矩形件），此时会弹窗让你选用哪一种。
3. 要在 CATIA 原生右键菜单里加菜单项需要 CAA RADE 授权，本工具用
   "全局右键 + 自动化"实现同样效果，不需要 RADE。
4. 本版在 CATIA V5R20（中文）上实测通过；其它版本/语言请先跑「诊断.bat」，
   必要时按上面的说明补 catia_bin 与 extra_dialog_titles。

【卸载】
直接删掉这个文件夹即可（不写注册表、不改 CATIA 设置）。
"""

BAT_START = """@echo off
setlocal
start "" "%~dp0{exe}.exe"
exit /b 0
""".format(exe=EXE_NAME)

BAT_DIAG = """@echo off
setlocal
"%~dp0{exe}.exe" --check
exit /b 0
""".format(exe=EXE_NAME)

BAT_QUIT = """@echo off
setlocal
"%~dp0{exe}.exe" --quit
exit /b 0
""".format(exe=EXE_NAME)

BAT_UNINSTALL = """@echo off
setlocal
"%~dp0{exe}.exe" --uninstall
exit /b 0
""".format(exe=EXE_NAME)


def ensure_pyinstaller(version: str = "") -> None:
    """准备 PyInstaller。

    version 非空时按**精确版本**安装 —— Win7 目标机要用 PyInstaller 4.10
    （更高的 5.x/6.x 打包出的 exe 在 Win7 上跑不起来），见 docs/兼容性-Win7与旧版CATIA-Python.md。
    """
    if version:
        current = ""
        try:
            import PyInstaller

            current = str(getattr(PyInstaller, "__version__", ""))
        except ImportError:
            current = ""
        if current == version:
            print(f"[1/4] PyInstaller {current} 已安装（按指定版本）")
            return
        print(f"[1/4] 安装 PyInstaller {version}（当前 {current or '未安装'}）...")
        subprocess.check_call(
            [sys.executable, "-m", "pip", "install", f"pyinstaller=={version}"]
        )
        return
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        print("[1/4] 安装 PyInstaller ...")
        subprocess.check_call([sys.executable, "-m", "pip", "install", "--upgrade", "pyinstaller"])
    else:
        print("[1/4] PyInstaller 已安装")


def run_pyinstaller(onefile: bool, out_root: Path, work_root: Path) -> Path:
    mode = "单文件" if onefile else "文件夹"
    print(f"[2/4] 打包（{mode}模式，首次约 1-3 分钟）...")
    args = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--onefile" if onefile else "--onedir",
        "--noconsole",
        "--name",
        EXE_NAME,
        "--paths",
        str(SRC),
        "--distpath",
        str(out_root),
        "--workpath",
        str(work_root),
        "--specpath",
        str(work_root / "spec"),
    ]
    for item in HIDDEN_IMPORTS:
        args += ["--hidden-import", item]
    args += ["--collect-submodules", "comtypes", str(ROOT / "portable_entry.py")]
    print("    " + " ".join(args[:6]) + " ...")
    subprocess.check_call(args)
    exe = out_root / (f"{EXE_NAME}.exe" if onefile else f"{EXE_NAME}/{EXE_NAME}.exe")
    if not exe.exists():
        raise SystemExit(f"打包失败，没找到 {exe}")
    return exe


def write_default_config(path: Path) -> None:
    sys.path.insert(0, str(SRC))
    from catia_fillet_assistant.config import Config

    cfg = Config()
    cfg.save(path)


def assemble(exe: Path, package: Path, onefile: bool) -> Path:
    print(f"[3/4] 组装目录：{package.name} ...")
    if package.exists():
        shutil.rmtree(package)
    if onefile:
        package.mkdir(parents=True, exist_ok=True)
        shutil.copy2(exe, package / exe.name)
    else:
        shutil.copytree(exe.parent, package)
    write_default_config(package / "config.json")
    (package / "启动助手.bat").write_text(BAT_START, encoding="ascii")
    (package / "诊断.bat").write_text(BAT_DIAG, encoding="ascii")
    (package / "退出助手.bat").write_text(BAT_QUIT, encoding="ascii")
    (package / "卸载助手.bat").write_text(BAT_UNINSTALL, encoding="ascii")
    note = README_TXT + (
        "\n\n【本包模式】单文件 exe（运行时自解压到临时目录）\n"
        if onefile
        else "\n\n【本包模式】文件夹模式（exe + _internal 依赖目录，启动更快，"
        "不落地临时目录；如果系统禁止 exe 从临时目录运行，用这个版本）\n"
    )
    (package / "使用说明.txt").write_text(note, encoding="utf-8-sig")
    return package


def make_shortcut(package: Path) -> None:
    """建几个中文名快捷方式（失败不影响便携版可用）。"""
    try:
        import win32com.client  # type: ignore

        shell = win32com.client.Dispatch("WScript.Shell")
        items = [
            ("双击这里启动助手.lnk", "", "CATIA V5 倒圆角助手（选错面提醒 + 右键对称倒圆角）"),
            ("退出助手.lnk", "--quit", "退出正在运行的 CATIA 倒圆角助手"),
            ("卸载助手.lnk", "--uninstall", "卸载 CATIA 倒圆角助手（二次确认后删除程序与日志）"),
            ("诊断.lnk", "--check", "生成诊断报告（排查环境问题）"),
        ]
        for name, arguments, description in items:
            link = shell.CreateShortCut(str(package / name))
            link.TargetPath = str(package / f"{EXE_NAME}.exe")
            link.Arguments = arguments
            link.WorkingDirectory = str(package)
            link.Description = description
            link.Save()
    except Exception as exc:
        print(f"    （快捷方式创建失败，不影响使用：{exc}）")


def main() -> int:
    import argparse
    import os

    parser = argparse.ArgumentParser(description="打便携版（单文件 + 文件夹模式）")
    parser.add_argument(
        "--pyinstaller-version",
        default=os.environ.get("CATIA_FILLET_PYINSTALLER", ""),
        help="指定 PyInstaller 版本（Win7 目标机用 4.10；留空=用当前环境已装的）",
    )
    args = parser.parse_args()
    ensure_pyinstaller(args.pyinstaller_version)
    # 1) 单文件版：一个 exe 拷走最省事
    exe1 = run_pyinstaller(True, BUILD_DIR / "exe_onefile", BUILD_DIR / "work_onefile")
    pkg1 = assemble(exe1, PACKAGE_DIR, onefile=True)
    make_shortcut(pkg1)
    # 2) 文件夹版：不走临时目录，适合策略更严的封闭环境
    exe2 = run_pyinstaller(False, BUILD_DIR / "exe_onedir", BUILD_DIR / "work_onedir")
    pkg2 = assemble(exe2, DIR_MODE_PACKAGE, onefile=False)
    make_shortcut(pkg2)

    print("[4/4] 完成：")
    for pkg in (pkg1, pkg2):
        size_mb = sum(f.stat().st_size for f in pkg.rglob("*") if f.is_file()) / 1024 / 1024
        print(f"      {pkg}   约 {size_mb:.1f} MB")
    print("      两个文件夹任选其一拷到目标机器；先双击「诊断.bat」看自检结果。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
