"""在**副本**上验证卸载入口：两步确认对话框 → 退出助手 → 删除程序目录与数据目录。

不会碰正式的 dist 包（先把包复制到临时目录再测）。
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import win32gui  # noqa: E402

from catia_fillet_assistant import win32util  # noqa: E402

SRC_PACKAGE = ROOT / "dist" / "CATIA倒圆角助手-便携版-v0.3.0"
TEST_ROOT = ROOT.parent / ".." / ".." / "04-Codex工作区" / "temp" / "uninstall-test"


def out(msg: str = "") -> None:
    print(msg, flush=True)


def find_top_window(title_part: str, timeout: float = 20.0) -> int | None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        for hwnd, _cls, title in win32util.catia_windows():
            if title_part in title:
                return hwnd
        # Tk 窗口不属于 CATIA 进程，单独枚举
        found: list[int] = []

        def cb(h, _p):
            try:
                if title_part in win32gui.GetWindowText(h):
                    found.append(h)
            except Exception:
                pass
            return True

        win32gui.EnumWindows(cb, None)
        if found:
            return found[0]
        time.sleep(0.5)
    return None


def click_text(hwnd: int, text: str) -> bool:
    for child in win32util.children(hwnd):
        try:
            if win32gui.GetWindowText(child).strip() == text:
                win32gui.SendMessage(child, 0x00F5, 0, 0)  # BM_CLICK
                return True
        except Exception:
            continue
    return False


def send_enter(hwnd: int) -> None:
    """Tk 的按钮不是 Win32 子窗口，用回车确认（对话框已绑定 <Return>）。"""
    import ctypes

    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [
            ("wVk", ctypes.c_ushort),
            ("wScan", ctypes.c_ushort),
            ("dwFlags", ctypes.c_ulong),
            ("time", ctypes.c_ulong),
            ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
        ]

    class _U(ctypes.Union):
        _fields_ = [("ki", KEYBDINPUT), ("padding", ctypes.c_ubyte * 32)]

    class INPUT(ctypes.Structure):
        _fields_ = [("type", ctypes.c_ulong), ("u", _U)]

    win32gui.SetForegroundWindow(hwnd)
    time.sleep(0.4)
    for up in (False, True):
        item = INPUT(type=1)
        item.u.ki = KEYBDINPUT(wVk=0x0D, wScan=0, dwFlags=0x0002 if up else 0, time=0, dwExtraInfo=None)
        ctypes.windll.user32.SendInput(1, ctypes.byref(item), ctypes.sizeof(INPUT))
        time.sleep(0.05)


def main() -> int:
    target = (ROOT.parent / ".." / "04-Codex工作区" / "temp" / "uninstall-test" / "pkg").resolve()
    if target.exists():
        shutil.rmtree(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(SRC_PACKAGE, target)
    exe = target / "CatiaFilletAssistant.exe"
    out(f"[准备] 副本：{target}")
    if not exe.exists():
        out("!! 复制失败")
        return 1

    proc = subprocess.Popen([str(exe), "--uninstall"])
    time.sleep(6)
    step1 = find_top_window("第 1 / 2 步", timeout=15)
    out(f"[第1步] 对话框 hwnd={step1}")
    if step1 is None:
        proc.kill()
        return 1
    send_enter(step1)
    out("    已按回车确认（继续）")
    time.sleep(2)
    step2 = find_top_window("第 2 / 2 步", timeout=15)
    out(f"[第2步] 对话框 hwnd={step2}")
    if step2 is None:
        proc.kill()
        return 1
    send_enter(step2)
    out("    已按回车确认（确认卸载）")
    time.sleep(8)

    gone = not target.exists()
    out(f"[结果] 副本目录是否已删除：{gone}")
    out(f"       进程是否还在：{proc.poll() is None}")
    if proc.poll() is None:
        proc.kill()
    return 0 if gone else 1


if __name__ == "__main__":
    raise SystemExit(main())
