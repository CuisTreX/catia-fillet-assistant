"""决定性实验：命令运行中宏加的选择到底有没有被接受？

E1. 先在文档选择集里放两条边 → 再开倒圆角命令 → 看对话框列表/体积（预选生效的判据）。
E2. 试三种"点确定"的方式（BM_CLICK / 回车 / SendInput 鼠标点击），看对话框是否关闭、体积是否变化。
E3. 命令开着时用宏加两条边 → 看体积（有没有实时预览 = 命令是否接受宏加的边）。
"""

from __future__ import annotations

import ctypes
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import win32com.client as wc  # noqa: E402
import win32con  # noqa: E402
import win32gui  # noqa: E402
from win32com.client import dynamic  # noqa: E402

from catia_fillet_assistant import win32util  # noqa: E402
from catia_fillet_assistant.catia import CatiaSession  # noqa: E402

EDGE_A = (
    "REdge:(Edge:(Face:(Brp:(Pad.1;0:(Brp:(Sketch.1;1)));None:());"
    "Face:(Brp:(Pad.1;0:(Brp:(Sketch.1;2)));None:());None:(Limits1:();Limits2:()));"
    "WithTemporaryBody;WithoutBuildError;WithSelectingFeatureSupport)"
)
EDGE_B = (
    "REdge:(Edge:(Face:(Brp:(Pad.1;0:(Brp:(Sketch.1;2)));None:());"
    "Face:(Brp:(Pad.1;0:(Brp:(Sketch.1;3)));None:());None:(Limits1:();Limits2:()));"
    "WithTemporaryBody;WithoutBuildError;WithSelectingFeatureSupport)"
)


def out(msg: str = "") -> None:
    print(msg, flush=True)


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


def send_enter(hwnd: int) -> None:
    win32gui.PostMessage(hwnd, win32con.WM_KEYDOWN, win32con.VK_RETURN, 0)
    win32gui.PostMessage(hwnd, win32con.WM_KEYUP, win32con.VK_RETURN, 0)


def send_mouse_click(hwnd: int) -> bool:
    """按屏幕坐标真实点击（SendInput），比 BM_CLICK 更接近人手。"""
    rect = win32gui.GetWindowRect(hwnd)
    cx = (rect[0] + rect[2]) // 2
    cy = (rect[1] + rect[3]) // 2
    user32 = ctypes.windll.user32
    user32.SetCursorPos(cx, cy)
    time.sleep(0.15)
    user32.mouse_event(0x0002, 0, 0, 0, 0)  # LEFTDOWN
    time.sleep(0.05)
    user32.mouse_event(0x0004, 0, 0, 0, 0)  # LEFTUP
    return True


def button_rect(dialog: int, labels: tuple[str, ...]) -> tuple[int, int] | None:
    for child in win32util.children(dialog):
        try:
            if win32gui.GetClassName(child).lower() != "button":
                continue
            if win32gui.GetWindowText(child).strip() not in labels:
                continue
            rect = win32gui.GetWindowRect(child)
            return ((rect[0] + rect[2]) // 2, (rect[1] + rect[3]) // 2)
        except Exception:
            continue
    return None


def click_at(x: int, y: int) -> None:
    user32 = ctypes.windll.user32
    user32.SetCursorPos(x, y)
    time.sleep(0.15)
    user32.mouse_event(0x0002, 0, 0, 0, 0)
    time.sleep(0.05)
    user32.mouse_event(0x0004, 0, 0, 0, 0)


def main() -> int:
    app = dynamic.Dispatch(wc.GetActiveObject("CATIA.Application")._oleobj_)
    app.DisplayFileAlerts = False
    doc = make_part(app)
    session = CatiaSession()
    try:
        part = session.active_part()
        pad = session.object_by_name(part, "Pad.1")
        ref_a = session.reference_from_label(part, EDGE_A, pad)
        ref_b = session.reference_from_label(part, EDGE_B, pad)
        out(f"[准备] 体积={session.volume_of(part)}  两条边引用={ref_a is not None}/{ref_b is not None}")

        out("")
        out("== E1：先预选两条边，再开命令 ==")
        session.clear_selection()
        session.add_to_selection(ref_a)
        session.add_to_selection(ref_b)
        out(f"    预选后 Selection.Count2 = {session.selection_count()}")
        ok = win32util.start_edge_fillet(session.connect())
        dialog = win32util.find_fillet_dialog()
        time.sleep(0.6)
        out(f"    命令打开={ok} 对话框={dialog.title if dialog else None}")
        out(f"    对话框列表 = {win32util.fillet_dialog_entries(dialog.hwnd) if dialog else []}")
        out(f"    体积 = {session.volume_of(part)}")

        out("")
        out("== E2：三种方式点「确定」 ==")
        if dialog is not None:
            ok_btn = button_rect(dialog.hwnd, ("确定", "OK", "Ok"))
            out(f"    确定按钮中心 = {ok_btn}")
            if ok_btn:
                click_at(*ok_btn)
                time.sleep(1.5)
            gone = win32util.find_fillet_dialog() is None
            out(f"    SendInput 点击后：对话框关闭={gone} 体积={session.volume_of(part)}")
            if not gone:
                send_enter(dialog.hwnd)
                time.sleep(1.5)
                gone = win32util.find_fillet_dialog() is None
                out(f"    回车后：对话框关闭={gone} 体积={session.volume_of(part)}")
            if not gone:
                win32util.click_button(dialog.hwnd, ("确定", "OK", "Ok"))
                time.sleep(1.5)
                gone = win32util.find_fillet_dialog() is None
                out(f"    BM_CLICK 后：对话框关闭={gone} 体积={session.volume_of(part)}")
        # 清理：确保对话框关掉
        for hwnd, cls, title in win32util.catia_windows():
            if cls == "#32770" and "圆角" in title:
                win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
        time.sleep(0.6)
        return 0
    finally:
        for hwnd, cls, title in win32util.catia_windows():
            if cls == "#32770" and "圆角" in title:
                win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
        time.sleep(0.4)
        try:
            doc.Close()
            out("[清理] 已关闭临时文档（未保存）")
        except Exception as exc:
            out(f"[清理] 关闭失败: {exc}")


if __name__ == "__main__":
    raise SystemExit(main())
