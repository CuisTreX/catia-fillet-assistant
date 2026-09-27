"""运行路径解析：源码运行、PyInstaller 冻结 exe、只读目录三种情况都要能用。

便携版的目标是「拷过去双击就能用」，所以要满足：
    * 配置/日志优先写在 exe 同目录（U 盘、本地目录都能用）；
    * 目录只读（共享盘、光盘、被策略锁）时自动退到 %LOCALAPPDATA% 或 %TEMP%；
    * 无控制台（pythonw / --noconsole）时，print 不能炸，诊断信息写文件。
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

APP_NAME = "CatiaFilletAssistant"
CONFIG_NAME = "config.json"
REPORT_NAME = "诊断报告.txt"


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def exe_dir() -> Path:
    """程序所在目录（冻结版 = exe 所在目录；源码运行 = 项目根目录）。"""
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[2]


def is_writable(path: Path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".write_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return True
    except Exception:
        return False


def data_dir() -> Path:
    """可写目录：exe 同目录 → %LOCALAPPDATA%\\CatiaFilletAssistant → %TEMP%\\CatiaFilletAssistant。"""
    base = exe_dir()
    if is_writable(base):
        return base
    local = os.environ.get("LOCALAPPDATA")
    if local:
        cand = Path(local) / APP_NAME
        if is_writable(cand):
            return cand
    cand = Path(tempfile.gettempdir()) / APP_NAME
    cand.mkdir(parents=True, exist_ok=True)
    return cand


def config_path(explicit: Path | None = None) -> Path:
    if explicit is not None:
        return explicit
    beside = exe_dir() / CONFIG_NAME
    if beside.exists():
        return beside
    return data_dir() / CONFIG_NAME


def log_dir() -> Path:
    d = data_dir() / "logs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def gen_dir() -> Path:
    """comtypes 生成模块的落地目录（冻结版里 package 目录是只读的，必须重定向）。"""
    d = data_dir() / "comtypes_gen"
    d.mkdir(parents=True, exist_ok=True)
    return d


def report_path() -> Path:
    return data_dir() / REPORT_NAME


class Reporter:
    """诊断输出：既写文件，也（有控制台时）打印，最后可弹窗展示。"""

    def __init__(self) -> None:
        self.lines: list[str] = []

    def __call__(self, msg: str = "") -> None:
        self.lines.append(str(msg))
        if sys.stdout is not None:
            try:
                print(msg, flush=True)
            except Exception:
                pass

    def text(self) -> str:
        return "\n".join(self.lines)

    def write(self, path: Path | None = None) -> Path:
        target = path or report_path()
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(self.text() + "\n", encoding="utf-8")
        except Exception:
            fallback = Path(tempfile.gettempdir()) / REPORT_NAME
            fallback.write_text(self.text() + "\n", encoding="utf-8")
            return fallback
        return target

    def show_dialog(self, title: str = "CATIA 倒圆角助手 - 诊断报告", extra: str = "") -> None:
        """无控制台时用弹窗展示（有控制台就不用弹）。"""
        if sys.stdout is not None:
            return
        try:
            import tkinter as tk

            root = tk.Tk()
            root.title(title)
            root.attributes("-topmost", True)
            root.geometry("900x620")
            text = tk.Text(root, wrap="word", font=("Consolas", 9))
            text.insert("1.0", self.text() + ("\n\n" + extra if extra else ""))
            text.configure(state="disabled")
            text.pack(fill="both", expand=True)
            tk.Button(root, text="关闭", command=root.destroy, height=2).pack(fill="x")
            root.mainloop()
        except Exception:
            pass
