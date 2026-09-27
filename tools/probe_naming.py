"""受控实验：BRep 单元的"名字"什么时候出现、能否因此量到几何。

背景：倒圆角命令运行时，点选的边线会被命令收进对话框（Document.Selection 为空），
但对话框里能看到名字（如 凸台.1\\目标边线.1），而且这个名字当时能解析成可量测的对象。
本实验要搞清楚：名字是命令带来的，还是选择集带来的？能否借此把"标签造的候选边线"变成可测量的对象？
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

from catia_fillet_assistant import win32util  # noqa: E402
from catia_fillet_assistant.catia import CatiaSession  # noqa: E402
from catia_fillet_assistant.cellgeom import CellIndex  # noqa: E402

EDGE_NAME = (
    "REdge:(Edge:(Face:(Brp:(Pad.1;0:(Brp:(Sketch.1;1)));None:());"
    "Face:(Brp:(Pad.1;0:(Brp:(Sketch.1;2)));None:());None:(Limits1:();Limits2:()));"
    "WithTemporaryBody;WithoutBuildError;WithSelectingFeatureSupport)"
)
LB_GETCOUNT, LB_GETTEXT, LB_GETTEXTLEN = 0x018B, 0x0189, 0x018A


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


def probe_names(session, part, tag: str) -> None:
    """看看当前有哪些单元名字能解析、能不能量。"""
    index = CellIndex(session, ttl=0.0)
    hits = []
    for prefix in ("边线", "Edge", "目标边线", "面", "平面"):
        for n in range(1, 9):
            cell = index.find_by_name(part, f"{prefix}.{n}")
            if cell is not None:
                hits.append(f"{prefix}.{n}[{cell.kind} len={cell.length} area={cell.area} 点={len(cell.samples)}]")
    out(f"  [{tag}] 可解析单元：{hits if hits else '（无）'}")


def dialog_entries() -> list[str]:
    import win32gui

    dialog = win32util.find_fillet_dialog()
    if dialog is None:
        return []
    entries: list[str] = []
    for child in win32util.children(dialog.hwnd):
        if win32gui.GetClassName(child).lower() != "listbox":
            continue
        if win32gui.GetWindowText(child) != "Edt":
            continue
        for i in range(min(int(win32gui.SendMessage(child, LB_GETCOUNT, 0, 0)), 10)):
            ln = int(win32gui.SendMessage(child, LB_GETTEXTLEN, i, 0))
            buf = ctypes.create_unicode_buffer(max(64, ln + 4))
            win32gui.SendMessage(child, LB_GETTEXT, i, buf)
            if buf.value and buf.value != "无选择":
                entries.append(buf.value)
    return entries


def main() -> int:
    app = dynamic.Dispatch(wc.GetActiveObject("CATIA.Application")._oleobj_)
    app.DisplayFileAlerts = False
    doc = make_part(app)
    session = CatiaSession()
    try:
        part = session.active_part()
        pad = session.object_by_name(part, "Pad.1")
        ref = session.reference_from_label(part, EDGE_NAME, pad)
        out("[准备] 测试零件 + 边线引用就绪")
        probe_names(session, part, "建part后")

        out("[A] 把引用加进文档选择集（无命令）")
        session.clear_selection()
        session.add_to_selection(ref)
        out(f"    Selection.Count2 = {session.selection_count()}")
        probe_names(session, part, "加入选择集后")

        out("[B] 打开倒圆角命令，再把引用加进选择集")
        ok = win32util.start_edge_fillet(session.connect())
        out(f"    命令打开 = {ok}")
        session.clear_selection()
        session.add_to_selection(ref)
        time.sleep(0.8)
        out(f"    Selection.Count2 = {session.selection_count()}  对话框列表 = {dialog_entries()}")
        probe_names(session, part, "命令内加引用后")

        out("[C] 再从对话框列表名字反查（关键：命令内能否量到点选的对象）")
        for entry in dialog_entries():
            index = CellIndex(session, ttl=0.0)
            cell = index.find_by_name(part, entry)
            out(f"    {entry!r} -> {cell.describe() if cell else '解析失败'}")
        return 0
    finally:
        for hwnd, cls, title in win32util.catia_windows():
            if cls == "#32770" and "圆角" in title:
                win32util.win32gui.PostMessage(hwnd, win32util.win32con.WM_CLOSE, 0, 0)
        time.sleep(0.4)
        try:
            doc.Close()
            out("[清理] 已关闭临时文档（未保存）")
        except Exception as exc:
            out(f"[清理] 关闭失败: {exc}")


if __name__ == "__main__":
    raise SystemExit(main())
