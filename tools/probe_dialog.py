"""探测：倒圆角对话框里「要圆角化的对象」列表控件长什么样、能不能读出内容。"""

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

EDGE_NAME = (
    "REdge:(Edge:(Face:(Brp:(Pad.1;0:(Brp:(Sketch.1;1)));None:());"
    "Face:(Brp:(Pad.1;0:(Brp:(Sketch.1;2)));None:());None:(Limits1:();Limits2:()));"
    "WithTemporaryBody;WithoutBuildError;WithSelectingFeatureSupport)"
)
FACE_NAME = (
    "RSur:(Face:(Brp:(Pad.1;2);None:());WithTemporaryBody;WithoutBuildError;WithSelectingFeatureSupport)"
)

LB_GETCOUNT = 0x018B
LB_GETTEXT = 0x0189
LB_GETTEXTLEN = 0x018A


def out(msg: str = "") -> None:
    print(msg, flush=True)


def dump_dialog(hwnd: int, tag: str) -> None:
    out(f"---- 对话框内容 [{tag}] ----")
    for child in win32util.children(hwnd):
        cls = win32gui.GetClassName(child)
        txt = win32gui.GetWindowText(child)
        if cls.lower() == "listbox":
            count = win32gui.SendMessage(child, LB_GETCOUNT, 0, 0)
            out(f"    ListBox hwnd={child} 条目数={count} 文本={txt!r}")
            for i in range(min(int(count), 20)):
                ln = int(win32gui.SendMessage(child, LB_GETTEXTLEN, i, 0))
                buf = ctypes.create_unicode_buffer(max(32, ln + 4))
                win32gui.SendMessage(child, LB_GETTEXT, i, buf)
                out(f"        [{i}] len={ln} 文本={buf.value!r}")
        elif txt:
            out(f"    {cls:<18} {txt!r}")


def main() -> int:
    app = dynamic.Dispatch(wc.GetActiveObject("CATIA.Application")._oleobj_)
    app.DisplayFileAlerts = False
    doc = dynamic.Dispatch(app.Documents.Add("Part")._oleobj_)
    part_dyn = doc.Part
    sketch = part_dyn.MainBody.Sketches.Add(part_dyn.OriginElements.PlaneXY)
    f2d = sketch.OpenEdition()
    f2d.CreateLine(0.0, 0.0, 100.0, 0.0)
    f2d.CreateLine(100.0, 0.0, 100.0, 60.0)
    f2d.CreateLine(100.0, 60.0, 0.0, 60.0)
    f2d.CreateLine(0.0, 60.0, 0.0, 0.0)
    sketch.CloseEdition()
    part_dyn.Update()
    part_dyn.ShapeFactory.AddNewPad(sketch, 40.0)
    part_dyn.Update()
    out("[准备] 临时零件已建立")

    session = CatiaSession()
    part = session.active_part()
    pad = session.object_by_name(part, "Pad.1")
    edge_ref = session.reference_from_label(part, EDGE_NAME, pad)
    face_ref = session.reference_from_label(part, FACE_NAME, pad)

    try:
        ok = win32util.start_edge_fillet(session.connect())
        dialog = win32util.find_fillet_dialog()
        out(f"[命令] 打开={ok} 对话框={dialog.title if dialog else None}")
        if not ok:
            return 1
        dump_dialog(dialog.hwnd, "刚打开")

        session.clear_selection()
        session.add_to_selection(edge_ref)
        out(f"[加入边线] 文档选择集 Count2={session.selection_count()}")
        time.sleep(0.6)
        dump_dialog(dialog.hwnd, "加入一条边线后")

        session.add_to_selection(face_ref)
        out(f"[再加面] 文档选择集 Count2={session.selection_count()}")
        time.sleep(0.6)
        dump_dialog(dialog.hwnd, "再加一个面后")
        return 0
    finally:
        for hwnd, cls, title in win32util.catia_windows():
            if cls == "#32770" and ("圆角" in title):
                win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
        time.sleep(0.4)
        try:
            doc.Close()
            out("[清理] 已关闭临时文档（未保存）")
        except Exception as exc:
            out(f"[清理] 关闭失败: {exc}")


if __name__ == "__main__":
    raise SystemExit(main())
