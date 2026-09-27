"""POC-01：预选两条边 → StartCommand("Edge Fillet") → 看原生命令认不认。

对应《原生 Edge Fillet 命令集成》Prompt §6–§7、§60：

    sel.Clear()
    sel.Add(edge1)
    sel.Add(edge2)
    catia.StartCommand("Edge Fillet")

然后自动读「要圆角化的对象」列表判断：
    PASS   两个 Edge 都在列表里 → 首选方案成立
    FAIL-1 只有一条            → 需要继续研究多对象预选
    FAIL-2 一条都没有          → 转 B 方案（原生命令 + 真实鼠标点击）

安全：全程只在**新建临时件**上做；用 WM_CLOSE 取消对话框（助手绝不点 CATIA 的 OK），
并校验模型零改动（特征树、体积都不变）。测完关闭临时件且不保存。
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
COMMAND_IDS = ("Edge Fillet", "EdgeFillet", "Fillet")


def out(message: str = "") -> None:
    print(message, flush=True)


def brief(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {str(exc)[:180]}"


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


def close_dialog(dialog) -> None:
    """取消原生命令（绝不点确定）。"""
    try:
        import win32con
        import win32gui

        win32gui.PostMessage(dialog.hwnd, win32con.WM_CLOSE, 0, 0)
    except Exception as exc:
        out(f"    [取消] WM_CLOSE 失败：{brief(exc)}")
    time.sleep(0.8)


def safe_start_command(app, command: str, timeout: float = 6.0):
    """带看门狗的 StartCommand。

    实测教训：传一个 CATIA 不认识的命令 ID（比如 "Definition"）会弹出模态框「超级输入消息」
    并把 COM 调用**永久阻塞**。所以这里放到子线程里跑，超时就关掉模态框并放弃。
    """
    import threading

    result = {}

    def worker():
        try:
            import comtypes

            comtypes.CoInitialize()
        except Exception:
            pass
        try:
            app.StartCommand(BSTR(command))
            result["ok"] = True
        except Exception as exc:
            result["error"] = brief(exc)

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    thread.join(timeout)
    if thread.is_alive():
        try:
            win32util.dismiss_unknown_command_dialogs()
        except Exception:
            pass
        return False, "超时（可能弹了模态框，已尝试关闭）"
    if result.get("ok"):
        return True, "ok"
    return False, result.get("error", "未知错误")


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
    report = {"timestamp": datetime.now().isoformat(timespec="seconds"), "steps": []}
    doc = None
    verdict = "UNKNOWN"
    try:
        doc, part = make_box(session)
        shapes_before = shapes_of(session, part)
        volume_before = session.volume_of(part)
        out(f"[准备] 临时件：{shapes_before} 体积={volume_before}")

        # ---- 用形状引擎找一对镜像边（顺便验证集成路径）----
        scanner = EdgeScanner(session, ttl=0.0, restore_selection=True)
        edges = scanner.scan(part, force=True)
        source = None
        for edge in edges:
            if edge.is_line and edge.length and abs(edge.length - 40.0) < 0.1:
                source = edge
                break
        assert source is not None, "没找到 40mm 的竖直边"
        outcome = shape_engine.pair(
            session,
            part,
            shape_engine.SourceHint.from_edge(source),
            cfg,
            scanner=scanner,
            source_edge=source,
        )
        for line in outcome.log_lines:
            out("    " + line)
        target = outcome.target or (outcome.candidates[0].target if outcome.candidates else None)
        assert target is not None, f"形状引擎没给出对称边（状态 {outcome.state}）"
        out(f"[配对] 源边 {source.describe()}")
        out(f"       目标 {target.describe()}（状态 {outcome.state}）")

        # ---- 关键 1：先把两条边放进选择集，再启动原生命令 ----
        selection = session.selection()
        selection.Clear()
        selection.Add(source.reference)
        selection.Add(target.reference)
        count_before = int(selection.Count2)
        selected_names = [it.display_name for it in session.selection_items()]
        out("")
        out("===== NATIVE FILLET POC =====")
        out(f"Selection.Count2 before StartCommand = {count_before}")
        for index, name in enumerate(selected_names, 1):
            out(f"{index}. {name[:100]}")
        report["steps"].append(
            {"name": "preselect", "count": count_before, "items": selected_names}
        )

        # ---- 关键 2：启动原生 Edge Fillet ----
        started_with = None
        for command in COMMAND_IDS:
            ok, reason = safe_start_command(app, command)
            if ok:
                started_with = command
                break
            out(f"    StartCommand('{command}') 失败/超时：{reason}")
        out(f"StartCommand = {started_with!r}")
        report["steps"].append({"name": "start_command", "command": started_with})

        dialog = None
        deadline = time.time() + 10.0
        while time.time() < deadline:
            dialog = win32util.find_fillet_dialog(cfg.extra_dialog_titles)
            if dialog is not None:
                break
            time.sleep(0.25)
        if dialog is None:
            out("Dialog Found = False（10 秒内没等到倒圆角对话框）")
            report["steps"].append({"name": "dialog", "found": False})
        else:
            out(f"Dialog Found = True（{dialog.title} / {dialog.class_name}）")
            entries = win32util.fillet_dialog_entries(dialog.hwnd)
            radius = win32util.fillet_dialog_radius(dialog.hwnd)
            out(f"Object List（{len(entries)} 项）：")
            for index, entry in enumerate(entries, 1):
                out(f"    {index}. {entry}")
            out(f"Radius 输入框读出 = {radius}")
            report["steps"].append(
                {
                    "name": "dialog",
                    "found": True,
                    "title": dialog.title,
                    "entries": entries,
                    "radius": radius,
                }
            )
            if len(entries) >= 2:
                verdict = "PASS"
            elif len(entries) == 1:
                verdict = "FAIL-1"
            else:
                verdict = "FAIL-2"
            close_dialog(dialog)
            time.sleep(0.6)
            still = win32util.find_fillet_dialog(cfg.extra_dialog_titles)
            out(f"[取消] 对话框已关闭：{'否（还在，需要手动关）' if still else '是'}")
            report["steps"].append({"name": "cancel", "closed": still is None})

        # ---- 模型必须零改动（我们没点 OK）----
        shapes_after = shapes_of(session, part)
        volume_after = session.volume_of(part)
        unchanged = shapes_after == shapes_before and (
            volume_after is None or volume_before is None or abs(volume_after - volume_before) < 1e-12
        )
        out(f"[校验] 特征树 {shapes_after}；体积 {volume_after}；模型零改动={'是' if unchanged else '否'}")
        report["steps"].append(
            {
                "name": "model_unchanged",
                "shapes": shapes_after,
                "volume": volume_after,
                "ok": bool(unchanged),
            }
        )
        report["verdict"] = verdict
    except Exception as exc:
        out(f"[异常] {brief(exc)}")
        import traceback

        traceback.print_exc()
        report["verdict"] = "ERROR"
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

    out("")
    out(f"===== 结论：{report.get('verdict')} =====")
    if report.get("verdict") == "PASS":
        out("原生 Fillet 接受预选的两条边 → 首选方案（Preselection + StartCommand）成立。")
    elif report.get("verdict") == "FAIL-1":
        out("只认了一条边 → 继续研究 CATIA 对多对象预选的处理（§7 FAIL-1）。")
    elif report.get("verdict") == "FAIL-2":
        out("一条都没认 → 转 B 方案：原生命令 + 真实鼠标点击（§8）。")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    target_path = OUT_DIR / f"poc_native_fillet_{stamp}.json"
    target_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    out(f"[输出] {target_path}")
    return 0 if report.get("verdict") == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
