"""端到端验证「选到面提醒」：打开真实倒圆角命令 -> 宏塞一个面进去 -> 看监视线程是否报警。

同时顺带验证：宏往正在运行的倒圆角对话框里补一条边线，按确定后能否真的生成倒圆角。
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import win32com.client as wc  # noqa: E402
from win32com.client import dynamic  # noqa: E402

from catia_fillet_assistant import win32util  # noqa: E402
from catia_fillet_assistant.catia import CatiaSession  # noqa: E402
from catia_fillet_assistant.config import Config  # noqa: E402
from catia_fillet_assistant.watcher import FilletWatcher  # noqa: E402
from catia_fillet_assistant.pairing import find_counterparts  # noqa: E402

EDGE_NAME = (
    "REdge:(Edge:(Face:(Brp:(Pad.1;0:(Brp:(Sketch.1;1)));None:());"
    "Face:(Brp:(Pad.1;0:(Brp:(Sketch.1;2)));None:());None:(Limits1:();Limits2:()));"
    "WithTemporaryBody;WithoutBuildError;WithSelectingFeatureSupport)"
)
FACE_NAME = (
    "RSur:(Face:(Brp:(Pad.1;2);None:());WithTemporaryBody;WithoutBuildError;WithSelectingFeatureSupport)"
)


def out(msg: str = "") -> None:
    print(msg, flush=True)


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
    pad_dyn = part_dyn.ShapeFactory.AddNewPad(sketch, 40.0)
    part_dyn.Update()
    out("[准备] 临时零件已建立")

    fired: list[str] = []
    session = CatiaSession()
    config = Config()
    watcher = FilletWatcher(session, config, on_wrong_pick=lambda st: fired.append(st.summary()))
    watcher.start()
    try:
        part = session.active_part()
        pad = session.object_by_name(part, "Pad.1")
        edge_ref = session.reference_from_label(part, EDGE_NAME, pad)
        face_ref = session.reference_from_label(part, FACE_NAME, pad)
        if edge_ref is None or face_ref is None:
            out("!! 测试引用创建失败")
            return 1

        # 1) 打开真实倒圆角命令
        ok = win32util.start_edge_fillet(session.connect())
        dialog = win32util.find_fillet_dialog()
        out(f"[命令] 打开倒圆角: {ok}; 对话框={dialog.title if dialog else None} (class={dialog.class_name if dialog else '-'})")
        if not ok:
            return 1

        # 2) 先选边线：不应该报警
        session.clear_selection()
        session.add_to_selection(edge_ref)
        time.sleep(1.2)
        out(f"[边线] 监视状态: {watcher.state_snapshot().summary()}")
        out(f"[边线] 误选报警次数 = {len(fired)}（期望 0）")
        edge_ok = len(fired) == 0

        # 3) 再塞一个面进去：应当报警
        session.add_to_selection(face_ref)
        time.sleep(1.6)
        state = watcher.state_snapshot()
        out(f"[面] 监视状态: {state.summary()}")
        out(f"[面] 误选报警次数 = {len(fired)}（期望 ≥1）")
        for line in fired:
            out(f"      {line}")
        face_ok = len(fired) >= 1

        # 4) 自动纠正演示：删掉面，再按对称关系补一条边线
        bad = state.bad_items
        for it in sorted(bad, key=lambda i: i.index, reverse=True):
            session.remove_selection_index(it.index)
        watcher.reset_alert()
        cfg = Config()
        cfg.symmetry_mode = "mirror"
        cfg.mirror_axis = "y"  # 平面 y=30 -> 对角那条边
        items = session.selection_items()
        added = 0
        for item in [i for i in items if i.is_edge]:
            cands, msg = find_counterparts(session, part, item, cfg, exclude_labels={i.display_name for i in items})
            out(f"[配对] {msg}")
            for cand in cands:
                if session.add_to_selection(cand.reference):
                    added += 1
        out(f"[配对] 补进选择集 {added} 条边线")

        # 5) 按确定，看是否真的生成倒圆角特征
        shapes_before = int(part_dyn.MainBody.Shapes.Count)
        clicked = win32util.click_button(dialog.hwnd, ("确定", "OK", "Ok"))
        time.sleep(1.8)
        shapes_after = int(part_dyn.MainBody.Shapes.Count)
        out(f"[结果] 点击确定={clicked}; Body.Shapes {shapes_before} -> {shapes_after}")
        fillet_ok = shapes_after > shapes_before
        if not fillet_ok:
            out("      （可能因为选择集里同时有面和边线，命令拒绝了输入）")
        return 0 if (edge_ok and face_ok) else 1
    finally:
        watcher.stop()
        for hwnd, cls, title in win32util.catia_windows():
            if cls == "#32770" and ("倒圆角" in title or "圆角" in title):
                win32util.win32gui.PostMessage(hwnd, win32util.win32con.WM_CLOSE, 0, 0)
        time.sleep(0.5)
        try:
            doc.Close()
            out("[清理] 已关闭临时文档（未保存）")
        except Exception as exc:
            out(f"[清理] 关闭失败: {exc}")


if __name__ == "__main__":
    raise SystemExit(main())
