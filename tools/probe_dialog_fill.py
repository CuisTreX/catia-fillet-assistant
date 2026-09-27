"""决定性实验：倒圆角命令开着时，把边线加进选择集 / 加单元对象，对话框列表认不认？

结论用来决定"能不能由助手把两个元素填进对话框"。
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
        out(f"[准备] 两条边引用：{ref_a is not None}/{ref_b is not None}")
        ok = win32util.start_edge_fillet(session.connect())
        dialog = win32util.find_fillet_dialog()
        out(f"[命令] 打开={ok} 对话框={dialog.title if dialog else None}")
        if dialog is None:
            return 1
        out(f"    初始列表：{win32util.fillet_dialog_entries(dialog.hwnd)}")
        out(f"    初始体积：{session.volume_of(part)}")

        out("[A] 加第一条边（Reference）")
        session.clear_selection()
        session.add_to_selection(ref_a)
        time.sleep(1.0)
        out(f"    列表={win32util.fillet_dialog_entries(dialog.hwnd)} 体积={session.volume_of(part)}")

        out("[B] 再加第二条边（Reference）")
        session.add_to_selection(ref_b)
        time.sleep(1.2)
        out(f"    列表={win32util.fillet_dialog_entries(dialog.hwnd)} 体积={session.volume_of(part)}")

        out("[C] 按真实鼠标点击『确定』")
        user32 = ctypes.windll.user32
        button = None
        for child in win32util.children(dialog.hwnd):
            if (
                win32gui.GetClassName(child).lower() == "button"
                and win32gui.GetWindowText(child).strip() in ("确定", "OK", "Ok")
            ):
                rect = win32gui.GetWindowRect(child)
                button = ((rect[0] + rect[2]) // 2, (rect[1] + rect[3]) // 2)
                break
        out(f"    确定按钮位置={button}")
        if button:
            user32.SetCursorPos(*button)
            time.sleep(0.2)
            user32.mouse_event(0x0002, 0, 0, 0, 0)
            time.sleep(0.05)
            user32.mouse_event(0x0004, 0, 0, 0, 0)
            time.sleep(2.0)
        gone = win32util.find_fillet_dialog() is None
        out(f"    对话框关闭={gone} 体积={session.volume_of(part)}")
        return 0
    finally:
        for hwnd, cls, title in win32util.catia_windows():
            if cls == "#32770" and "圆角" in title:
                win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
        time.sleep(0.5)
        try:
            doc.Close()
            out("[清理] 已关闭临时文档（未保存）")
        except Exception as exc:
            out(f"[清理] 关闭失败: {exc}")


if __name__ == "__main__":
    raise SystemExit(main())
