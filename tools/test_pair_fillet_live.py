"""验收「选中边线 → 补对称 → 直接生成倒圆角」这条主流程（不使用交互对话框）。"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import win32com.client as wc  # noqa: E402
from win32com.client import dynamic  # noqa: E402

from catia_fillet_assistant import pairing  # noqa: E402
from catia_fillet_assistant.catia import CatiaSession  # noqa: E402
from catia_fillet_assistant.config import Config  # noqa: E402

EDGE_NAME = (
    "REdge:(Edge:(Face:(Brp:(Pad.1;0:(Brp:(Sketch.1;1)));None:());"
    "Face:(Brp:(Pad.1;0:(Brp:(Sketch.1;2)));None:());None:(Limits1:();Limits2:()));"
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
        ref = session.reference_from_label(part, EDGE_NAME, pad)
        session.clear_selection()
        session.add_to_selection(ref)
        out(f"[选中] 1 条边线，Selection.Count2 = {session.selection_count()}")

        cfg = Config()
        cfg.symmetry_mode = "mirror"
        cfg.mirror_axis = "y"
        outcome = pairing.pair_selection(session, cfg)
        out(f"[配对] {outcome.message}  分组={list(outcome.groups)}")
        refs = session.selection_references()
        out(f"[选择集] 现在有 {len(refs)} 条边线引用")
        if len(refs) < 2:
            out("✘ 没有补上对称边线")
            return 1

        before = session.volume_of(part)
        ok, message = pairing.create_edge_fillet(session, part, refs, radius=8.0, propagation=1)
        after = session.volume_of(part)
        out(f"[倒圆角] {ok} {message}")
        out(f"[体积] {before} → {after}")
        if ok and isinstance(after, (int, float)) and isinstance(before, (int, float)) and after < before:
            out("✔ 主流程通过：选中一条边 → 补对称 → 直接生成倒圆角")
            return 0
        out("✘ 主流程未通过")
        return 1
    finally:
        try:
            doc.Close()
            out("[清理] 已关闭临时文档（未保存）")
        except Exception as exc:
            out(f"[清理] 关闭失败: {exc}")


if __name__ == "__main__":
    raise SystemExit(main())
