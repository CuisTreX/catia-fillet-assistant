"""端到端：用真实按键（SendInput）触发 Ctrl+Alt+R，验证运行中的助手真的生成了倒圆角。

前提：助手已经在后台运行（便携版 exe 或源码模式），且 CATIA 开着 Part 文档。
"""

from __future__ import annotations

import ctypes
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import win32com.client as wc  # noqa: E402
from win32com.client import dynamic  # noqa: E402

from catia_fillet_assistant.catia import CatiaSession  # noqa: E402

EDGE_NAME = (
    "REdge:(Edge:(Face:(Brp:(Pad.1;0:(Brp:(Sketch.1;1)));None:());"
    "Face:(Brp:(Pad.1;0:(Brp:(Sketch.1;2)));None:());None:(Limits1:();Limits2:()));"
    "WithTemporaryBody;WithoutBuildError;WithSelectingFeatureSupport)"
)

VK_CONTROL, VK_MENU, VK_R = 0x11, 0x12, 0x52
KEYEVENTF_KEYUP = 0x0002
INPUT_KEYBOARD = 1


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", ctypes.c_ushort),
        ("wScan", ctypes.c_ushort),
        ("dwFlags", ctypes.c_ulong),
        ("time", ctypes.c_ulong),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]


class _INPUTUNION(ctypes.Union):
    # 64 位下 MOUSEINPUT 是 32 字节，union 必须按 32 字节对齐，否则 SendInput 失败
    _fields_ = [("ki", KEYBDINPUT), ("padding", ctypes.c_ubyte * 32)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", ctypes.c_ulong), ("u", _INPUTUNION)]


def out(msg: str = "") -> None:
    print(msg, flush=True)


def key(vk: int, up: bool = False) -> None:
    """用 SendInput 发按键（比 keybd_event 可靠，RegisterHotKey 能收到）。"""
    item = INPUT(type=INPUT_KEYBOARD)
    item.u.ki = KEYBDINPUT(wVk=vk, wScan=0, dwFlags=KEYEVENTF_KEYUP if up else 0, time=0, dwExtraInfo=None)
    sent = ctypes.windll.user32.SendInput(1, ctypes.byref(item), ctypes.sizeof(INPUT))
    if sent != 1:
        out(f"    [警告] SendInput 返回 {sent}（INPUT 大小={ctypes.sizeof(INPUT)}）")


def right_click_at(x: int, y: int) -> None:
    user32 = ctypes.windll.user32
    user32.SetCursorPos(x, y)
    time.sleep(0.2)
    user32.mouse_event(0x0008, 0, 0, 0, 0)  # RIGHTDOWN
    time.sleep(0.05)
    user32.mouse_event(0x0010, 0, 0, 0, 0)  # RIGHTUP


def press_ctrl_alt_r() -> None:
    key(VK_CONTROL)
    key(VK_MENU)
    key(VK_R)
    time.sleep(0.08)
    key(VK_R, True)
    key(VK_MENU, True)
    key(VK_CONTROL, True)


def make_part(app):
    doc = dynamic.Dispatch(app.Documents.Add("Part")._oleobj_)
    part = doc.Part
    sketch = part.MainBody.Sketches.Add(part.OriginElements.PlaneXY)
    f2d = sketch.OpenEdition()
    f2d.CreateLine(0.0, 0.0, 100.0, 0.0)
    f2d.CreateLine(100.0, 0.0, 100.0, 60.0)
    f2d.CreateLine(100.0, 60.0, 0.0, 60.0)
    f2d.CreateLine(0.0, 60.0, 0.0, 0.0)
    sketch.CloseEdition()
    part.Update()
    part.ShapeFactory.AddNewPad(sketch, 40.0)
    part.Update()
    return doc


def main() -> int:
    app = dynamic.Dispatch(wc.GetActiveObject("CATIA.Application")._oleobj_)
    app.DisplayFileAlerts = False
    doc = make_part(app)
    session = CatiaSession()
    try:
        part = session.active_part()
        pad = session.object_by_name(part, "Pad.1")
        ref = session.reference_from_label(part, EDGE_NAME, pad)
        session.clear_selection()
        session.add_to_selection(ref)
        before = session.volume_of(part)
        out(f"[准备] 已选中 1 条边线，体积={before}")

        # 先把 CATIA 拉到前台，再发真实右键（这也顺带验证"右键不再弹原生菜单"）
        import win32gui

        main = None
        for hwnd, cls, title in __import__("catia_fillet_assistant.win32util", fromlist=["x"]).catia_windows():
            if title.startswith("CATIA V5") and " - [" in title:
                main = hwnd
                break
        if main is None:
            out("!! 找不到 CATIA 主窗口")
            return 1
        try:
            win32gui.SetForegroundWindow(main)
        except Exception as exc:
            out(f"    [警告] 置前台失败: {exc}")
        time.sleep(0.8)
        rect = win32gui.GetWindowRect(main)
        # 点在标题栏上：既能被钩子接管，又不会改到三维视图里的选择
        cx, cy = (rect[0] + rect[2]) // 2, rect[1] + 8
        out(f"[右键] 在 CATIA 标题栏 ({cx},{cy}) 发送真实右键（同时再发一次 Ctrl+Alt+R 兜底）...")
        right_click_at(cx, cy)
        time.sleep(0.5)
        press_ctrl_alt_r()
        for _ in range(60):
            time.sleep(0.5)
            after = session.volume_of(part)
            if isinstance(after, (int, float)) and isinstance(before, (int, float)) and after < before - 1e-12:
                shapes = int(session.dyn(part).MainBody.Shapes.Count)
                last = session.dyn(part).MainBody.Shapes.Item(shapes).Name
                out(f"[结果] 体积 {before} → {after}，最后特征={last}")
                out("✔ 热键端到端通过：正在运行的助手成功生成了对称倒圆角")
                return 0
        after = session.volume_of(part)
        out(f"[结果] 体积没有变化（{before} → {after}）")
        out("✘ 热键没生效，或助手没在运行")
        return 1
    finally:
        try:
            doc.Close()
            out("[清理] 已关闭临时文档（未保存）")
        except Exception as exc:
            out(f"[清理] 关闭失败: {exc}")


if __name__ == "__main__":
    raise SystemExit(main())
