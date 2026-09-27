"""探测：能否用名字（FindObjectByName）拿到 BRep 单元，并量到它们的几何。

背景：倒圆角命令运行中，用户点选的边线被命令收进对话框，Document.Selection 是空的；
但对话框列表里能看到类似「凸台.1\\目标边线.1」的名字。如果这个名字能解析成对象、
并且能拿到几何，就可以绕开"选择集读不到"的问题。
"""

from __future__ import annotations

import ctypes
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from catia_fillet_assistant import win32util  # noqa: E402
from catia_fillet_assistant.catia import CatiaSession  # noqa: E402

LB_GETCOUNT = 0x018B
LB_GETTEXT = 0x0189
LB_GETTEXTLEN = 0x018A


def out(msg: str = "") -> None:
    print(msg, flush=True)


def dialog_entries() -> list[str]:
    """读倒圆角对话框「要圆角化的对象」列表内容。"""
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
        count = int(win32gui.SendMessage(child, LB_GETCOUNT, 0, 0))
        for i in range(min(count, 10)):
            length = int(win32gui.SendMessage(child, LB_GETTEXTLEN, i, 0))
            buf = ctypes.create_unicode_buffer(max(64, length + 4))
            win32gui.SendMessage(child, LB_GETTEXT, i, buf)
            if buf.value and buf.value != "无选择":
                entries.append(buf.value)
    return entries


def main() -> int:
    session = CatiaSession()
    part = session.active_part()
    out(f"[连接] part = {part}")
    if part is None:
        return 2

    entries = dialog_entries()
    out(f"[对话框] 当前倒圆角对象列表：{entries or '（空/未打开对话框）'}")

    names: list[str] = []
    names += entries
    for entry in entries:
        if "\\" in entry:
            feature, cell = entry.split("\\", 1)
            names += [cell, feature, f"{feature}\\{cell}"]
    for stem in ("边线", "目标边线", "棱边", "Edge", "面", "平面", "圆柱面", "曲面", "Face"):
        for n in range(1, 9):
            names.append(f"{stem}.{n}")

    seen: set[str] = set()
    resolved: list[tuple[str, object]] = []
    for name in names:
        if name in seen:
            continue
        seen.add(name)
        obj = session.object_by_name(part, name)
        if obj is None:
            continue
        ref = None
        try:
            ref = part.CreateReferenceFromObject(obj)
        except Exception:
            pass
        display = None
        try:
            display = ref.DisplayName if ref is not None else None
        except Exception:
            pass
        out(f"  ✔ {name!r} -> {type(obj).__name__}  ref.DisplayName={display!r}")
        resolved.append((name, obj))

    out("")
    out("[几何] 试着量这些对象的长度/面积：")
    wb = session.workbench()
    for name, obj in resolved[:8]:
        try:
            ref = part.CreateReferenceFromObject(obj)
            m = wb.GetMeasurable(ref)
        except Exception as exc:
            out(f"  {name!r}: GetMeasurable 失败 {repr(exc)[:70]}")
            continue
        for attr in ("Length", "Area", "Perimeter", "Radius"):
            try:
                value = getattr(m, attr)
            except Exception:
                continue
            out(f"  {name!r}: {attr} = {value}")
        for meth in ("GetPointsOnCurve", "GetPoint", "GetCOG"):
            try:
                if meth == "GetPointsOnCurve":
                    lp = None
                    for spec in session.mod("spa").Measurable._methods_:
                        if spec.name == meth:
                            lp = spec.argtypes[0]
                    if lp is None:
                        continue
                    buf = lp.create([0.0] * 90)
                    res = getattr(m, meth)(buf)
                else:
                    res = getattr(m, meth)(session._buf(3))
                out(f"  {name!r}: {meth} -> {str(res)[:120]}")
            except Exception as exc:
                out(f"  {name!r}: {meth} 失败 {repr(exc)[:70]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
