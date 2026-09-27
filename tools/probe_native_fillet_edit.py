"""【已停用 · 请勿直接运行】继续找"让原生对话框接管已有 Fillet"的命令 ID。

⚠ 危险记录（2026-09-25 实测）：本脚本试到 `StartCommand("Definition")` 时，
CATIA 弹出模态框「超级输入消息」并让 COM 调用**永久阻塞**（只能杀进程 + WM_CLOSE 关模态框）。
结论已经拿到（见 docs/原生Fillet集成-实测结论.md §1/§3），本文件仅作证据保留；
若要再试任何 `StartCommand`，必须先加**看门狗**：超时 → `win32util.dismiss_unknown_command_dialogs()` → 放弃。

背景（实测）：
    * 预选两条边 + StartCommand("Edge Fillet") → 对话框开了，但对象列表是「无选择」（FAIL-2）；
    * 选中已建好的 倒圆角.1 + StartCommand("Edge Fillet") → 打开的是**新建**对话框，不是编辑它。

本探针把候选"编辑已有对象定义"的命令 ID 挨个试一遍，看哪个能把
已建好的 倒圆角.1（含两条边、R5）连同它的对象列表/半径一起带到原生对话框里。

仍然只在临时件上做；每次都用 WM_CLOSE 取消，绝不点 OK。
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

# 候选：打开"选中对象的定义"的命令 ID（不同版本/语言可能是其中之一）
EDIT_COMMANDS = (
    "Definition",
    "Object Definition",
    "Edit Definition",
    "Definition...",
    "Edit",
    "Edit Object",
)


def out(message: str = "") -> None:
    print(message, flush=True)


def brief(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {str(exc)[:150]}"


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


def wait_dialog(cfg: Config, seconds: float = 4.0):
    deadline = time.time() + seconds
    while time.time() < deadline:
        dialog = win32util.find_fillet_dialog(cfg.extra_dialog_titles)
        if dialog is not None:
            return dialog
        time.sleep(0.25)
    return None


def cancel(dialog) -> None:
    try:
        import win32con
        import win32gui

        win32gui.PostMessage(dialog.hwnd, win32con.WM_CLOSE, 0, 0)
    except Exception as exc:
        out(f"    [取消] 失败：{brief(exc)}")
    time.sleep(0.7)


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
    report = {"timestamp": datetime.now().isoformat(timespec="seconds"), "attempts": []}
    doc = None
    winner = None
    try:
        doc, part = make_box(session)
        scanner = EdgeScanner(session, ttl=0.0, restore_selection=True)
        edges = scanner.scan(part, force=True)
        source = next(e for e in edges if e.is_line and e.length and abs(e.length - 40.0) < 0.1)
        outcome = shape_engine.pair(
            session, part, shape_engine.SourceHint.from_edge(source), cfg,
            scanner=scanner, source_edge=source,
        )
        outcome.target = outcome.target or outcome.candidates[0].target
        ok, message = shape_engine.create_fillet(
            session, part, outcome, radius=cfg.fillet_radius, propagation=cfg.fillet_propagation
        )
        out(f"[准备] 已建原生特征：ok={ok} {message}")
        if not ok:
            raise SystemExit("建特征失败，终止")

        selection = session.selection()
        for command in EDIT_COMMANDS:
            selection.Clear()
            feature = session.object_by_name(part, "倒圆角.1") or session.object_by_name(part, "EdgeFillet.1")
            if feature is None:
                out("[准备] 找不到 倒圆角.1")
                break
            selection.Add(feature)
            out("")
            out(f"===== 试命令 '{command}'（已选中 倒圆角.1，Selection.Count2={int(selection.Count2)}）=====")
            try:
                app.StartCommand(BSTR(command))
            except Exception as exc:
                out(f"    StartCommand 异常：{brief(exc)}")
                report["attempts"].append({"command": command, "error": brief(exc)})
                continue
            dialog = wait_dialog(cfg, 3.0)
            if dialog is None:
                out("    没有打开任何倒圆角对话框")
                report["attempts"].append({"command": command, "dialog": False})
                continue
            entries = win32util.fillet_dialog_entries(dialog.hwnd)
            radius = win32util.fillet_dialog_radius(dialog.hwnd)
            out(f"    对话框=『{dialog.title}』 对象列表={entries} 半径={radius}")
            report["attempts"].append(
                {"command": command, "dialog": True, "title": dialog.title,
                 "entries": entries, "radius": radius}
            )
            cancel(dialog)
            if entries and (radius is not None):
                winner = command
                out(f"    ✔ 这个命令把已有特征的定义打开了（对象列表 {len(entries)} 项、半径 {radius}）")
                break
            # 如果不是编辑而是新建：把它取消掉就行（WM_CLOSE），继续试下一个
    except SystemExit as exc:
        out(f"[终止] {exc}")
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

    report["winner"] = winner
    out("")
    out(f"===== 结论：{'命令 ' + winner if winner else '没有命令能直接打开已有特征定义'} =====")
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    target_path = OUT_DIR / f"native_fillet_edit_{stamp}.json"
    target_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    out(f"[输出] {target_path}")
    return 0 if winner else 1


if __name__ == "__main__":
    raise SystemExit(main())
