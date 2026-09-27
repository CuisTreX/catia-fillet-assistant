"""原生 Fillet 集成路线实测：把对话框控件树 dump 出来，并对比两条启动路线。

背景：POC-01（预选两条边 → StartCommand）读到的对象列表是 0 项，需要排除"读错控件"。

本探针（临时件，全程不点 CATIA 的 OK）：
    A 路线：Selection 里放两条边 → StartCommand("Edge Fillet")
              → 连续几次读对象列表 + dump 所有列表框/控件文本
    B 路线：先由 API 建出原生 EdgeFillet（两条边）→ 选中该特征 → StartCommand("Edge Fillet")
              → 看原生命令是否进入"编辑已有特征"模式（对象列表/半径是否带出来）

两条路线都在临时件上做，测完关闭不保存。
"""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from comtypes.automation import BSTR  # noqa: E402

from catia_fillet_assistant import shape_engine, win32util  # noqa: E402
from catia_fillet_assistant.catia import CatiaSession  # noqa: E402
from catia_fillet_assistant.config import Config  # noqa: E402
from catia_fillet_assistant.edge_scan import EdgeScanner  # noqa: E402

OUT_DIR = ROOT / "docs" / "API探测"
LB_GETCOUNT, LB_GETTEXT, LB_GETTEXTLEN = 0x018B, 0x0189, 0x018A


def out(message: str = "") -> None:
    print(message, flush=True)


def brief(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {str(exc)[:160]}"


def dump_dialog(dialog) -> dict:
    """把对话框里所有控件（类名/文本/列表框条目）打出来。"""
    import ctypes

    import win32gui

    info = {"title": dialog.title, "class": dialog.class_name, "controls": [], "listboxes": []}
    try:
        children = win32util.children(dialog.hwnd)
        top_children = []

        def collect(hwnd_parent, depth):
            if depth > 3:
                return
            for child in win32util.children(hwnd_parent):
                top_children.append((depth, child))
                collect(child, depth + 1)

        collect(dialog.hwnd, 0)
    except Exception as exc:
        info["error"] = brief(exc)
        return info
    for depth, child in top_children[:200]:
        try:
            cls = win32gui.GetClassName(child)
            text = win32gui.GetWindowText(child)
        except Exception:
            continue
        item_count = None
        items = []
        if cls.lower() in ("listbox", "listview", "systreeview32"):
            try:
                item_count = int(win32gui.SendMessage(child, LB_GETCOUNT, 0, 0))
            except Exception:
                item_count = None
            if cls.lower() == "listbox" and item_count:
                for index in range(min(item_count, 20)):
                    try:
                        length = int(win32gui.SendMessage(child, LB_GETTEXTLEN, index, 0))
                        buffer = ctypes.create_unicode_buffer(max(64, length + 4))
                        win32gui.SendMessage(child, LB_GETTEXT, index, buffer)
                        items.append(buffer.value.strip())
                    except Exception:
                        continue
        record = {"depth": depth, "class": cls, "text": text[:60]}
        if item_count is not None:
            record["items"] = item_count
            record["first"] = items[:6]
            info["listboxes"].append(record)
        elif text:
            info["controls"].append(record)
    return info


def read_entries_polled(dialog, seconds: float = 3.0):
    """隔一会儿多读几次，排除"对话框还没填好"的可能。"""
    deadline = time.time() + seconds
    best = []
    while time.time() < deadline:
        entries = win32util.fillet_dialog_entries(dialog.hwnd)
        if len(entries) > len(best):
            best = entries
        if best:
            break
        time.sleep(0.4)
    return best


def make_box(session: CatiaSession):
    dyn_app = session.dyn(session.connect())
    doc = dyn_app.Documents.Add("Part")
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
    return doc, session.active_part()


def shapes_of(session: CatiaSession, part):
    try:
        dyn = session.dyn(part)
        collection = dyn.MainBody.Shapes
        return [str(collection.Item(i).Name) for i in range(1, int(collection.Count) + 1)]
    except Exception as exc:
        return ["<读取失败 " + brief(exc) + ">"]


def cancel(dialog) -> None:
    try:
        import win32con
        import win32gui

        win32gui.PostMessage(dialog.hwnd, win32con.WM_CLOSE, 0, 0)
    except Exception as exc:
        out(f"    [取消] 失败：{brief(exc)}")
    time.sleep(0.8)


def wait_dialog(cfg: Config, seconds: float = 8.0):
    deadline = time.time() + seconds
    while time.time() < deadline:
        dialog = win32util.find_fillet_dialog(cfg.extra_dialog_titles)
        if dialog is not None:
            return dialog
        time.sleep(0.25)
    return None


def main() -> int:
    session = CatiaSession()
    app = session.connect()
    out("[连接] CATIA 已连接")
    previous_doc = None
    alerts = None
    try:
        previous_doc = app.ActiveDocument
    except Exception:
        previous_doc = None
    try:
        alerts = bool(app.DisplayFileAlerts)
        app.DisplayFileAlerts = False
    except Exception:
        alerts = None

    cfg = Config()
    cfg.fillet_radius = 5.0
    cfg.fillet_propagation = 0
    report = {"timestamp": datetime.now().isoformat(timespec="seconds")}
    doc = None
    try:
        doc, part = make_box(session)
        scanner = EdgeScanner(session, ttl=0.0, restore_selection=True)
        edges = scanner.scan(part, force=True)
        source = next(e for e in edges if e.is_line and e.length and abs(e.length - 40.0) < 0.1)
        outcome = shape_engine.pair(
            session, part, shape_engine.SourceHint.from_edge(source), cfg,
            scanner=scanner, source_edge=source,
        )
        target = outcome.target or outcome.candidates[0].target
        out(f"[配对] {source.describe()}  ←→  {target.describe()}（{outcome.state}）")

        # ---------------- A 路线：预选 + StartCommand ----------------
        selection = session.selection()
        selection.Clear()
        selection.Add(source.reference)
        selection.Add(target.reference)
        out("")
        out("===== A 路线：预选两条边 → StartCommand =====")
        out(f"Selection.Count2 before StartCommand = {int(selection.Count2)}")
        app.StartCommand(BSTR("Edge Fillet"))
        dialog_a = wait_dialog(cfg)
        if dialog_a is None:
            out("Dialog Found = False")
            report["route_a"] = {"dialog": False}
        else:
            entries_a = read_entries_polled(dialog_a, 3.0)
            dump_a = dump_dialog(dialog_a)
            out(f"Dialog Found = True（{dialog_a.title}）")
            out(f"Object List = {entries_a}")
            out(f"Radius = {win32util.fillet_dialog_radius(dialog_a.hwnd)}")
            out("列表框 dump：")
            for item in dump_a.get("listboxes", []):
                out(f"    class={item['class']:<16} items={item.get('items')} text={item.get('text')!r}"
                    f" {item.get('first')}")
            if not dump_a.get("listboxes"):
                out("    （这个对话框里没有传统列表框）")
            report["route_a"] = {
                "dialog": True, "title": dialog_a.title, "entries": entries_a,
                "radius": win32util.fillet_dialog_radius(dialog_a.hwnd), "dump": dump_a,
            }
            cancel(dialog_a)

        # ---------------- B 路线：API 建特征 → 选中特征 → 原生命令编辑 ----------------
        out("")
        out("===== B 路线：先建原生特征，再让原生命令接管编辑 =====")
        outcome.target = target  # AMBIGUOUS 时 outcome.target 是空的，POC 里手动指定要用的那条
        ok, message = shape_engine.create_fillet(
            session, part, outcome, radius=cfg.fillet_radius, propagation=cfg.fillet_propagation
        )
        out(f"create_fillet：ok={ok} {message}")
        shapes = shapes_of(session, part)
        out(f"特征树 = {shapes}")
        report["route_b"] = {"create_ok": bool(ok), "message": message, "shapes": shapes}

        feature_name = next((name for name in reversed(shapes) if "圆角" in name or "illet" in name), None)
        if feature_name:
            selection.Clear()
            feature = session.object_by_name(part, feature_name)
            if feature is not None:
                selection.Add(feature)
                out(f"已选中特征 {feature_name}（Selection.Count2={int(selection.Count2)}）")
            app.StartCommand(BSTR("Edge Fillet"))
            dialog_b = wait_dialog(cfg)
            if dialog_b is None:
                out("Dialog Found = False（原生命令没有打开编辑界面）")
                report["route_b"]["dialog"] = False
            else:
                entries_b = read_entries_polled(dialog_b, 3.0)
                dump_b = dump_dialog(dialog_b)
                radius_b = win32util.fillet_dialog_radius(dialog_b.hwnd)
                out(f"Dialog Found = True（{dialog_b.title}）")
                out(f"Object List = {entries_b}")
                out(f"Radius = {radius_b}")
                for item in dump_b.get("listboxes", []):
                    out(f"    class={item['class']:<16} items={item.get('items')} text={item.get('text')!r}"
                        f" {item.get('first')}")
                report["route_b"]["dialog"] = {
                    "title": dialog_b.title, "entries": entries_b, "radius": radius_b, "dump": dump_b,
                }
                cancel(dialog_b)

        report["shapes_after"] = shapes_of(session, part)
        out(f"[收尾] 特征树 = {report['shapes_after']}")
    except Exception as exc:
        out(f"[异常] {brief(exc)}")
        import traceback

        traceback.print_exc()
    finally:
        if doc is not None:
            try:
                doc.Close()
                out("[清理] 临时文档已关闭（未保存）")
            except Exception as exc:
                out(f"[清理] 关闭失败：{brief(exc)}")
        if previous_doc is not None:
            try:
                previous_doc.Activate()
            except Exception:
                pass
        if alerts is not None:
            try:
                app.DisplayFileAlerts = alerts
            except Exception:
                pass

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    target_path = OUT_DIR / f"native_fillet_variants_{stamp}.json"
    target_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    out(f"[输出] {target_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
