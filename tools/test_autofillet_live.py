"""验证「自动化直接生成倒圆角」：官方 API，不依赖交互对话框。

    fillet = part.ShapeFactory.AddNewEdgeFilletWithConstantRadius(edgeRef, 1, radius)
    fillet.AddObjectToFillet(anotherEdgeRef)
    part.Update()

判据：体积变小（倒圆角削掉材料）+ 特征树里出现倒圆角特征。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import win32com.client as wc  # noqa: E402
from win32com.client import dynamic  # noqa: E402

from catia_fillet_assistant import featuregeom  # noqa: E402
from catia_fillet_assistant.catia import CatiaSession  # noqa: E402
from catia_fillet_assistant.config import Config  # noqa: E402
from catia_fillet_assistant.pairing import pair_from_source_cell  # noqa: E402
from catia_fillet_assistant.cellgeom import Cell, CellIndex  # noqa: E402


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
    cells = CellIndex(session)
    try:
        part = session.active_part()
        feature = session.object_by_name(part, "Pad.1")
        localized = str(feature.Name)
        names = featuregeom.discover_names(session, part, feature, localized)
        feature_name, sketch_name = names
        frame = session.read_sketch_frame(featuregeom.feature_attr(session, feature, "Sketch"))
        elements = session.read_sketch_elements(featuregeom.feature_attr(session, feature, "Sketch"))
        first, second, symmetric = featuregeom.read_limits(session, feature)
        candidates = featuregeom.prismatic_candidates(
            session, part, feature, feature_name, sketch_name, frame, elements,
            featuregeom._z_ranges(first, second, symmetric),
        )
        source = next(
            c
            for c in candidates
            if c.kind == "vertical" and abs(c.samples[0].x - 100) < 0.2 and abs(c.samples[0].y) < 0.2
        )
        out(f"[源边线] {source.describe()} {[p.as_tuple() for p in source.samples]}")

        cfg = Config()
        cfg.symmetry_mode = "mirror"
        cfg.mirror_axis = "y"
        cell = Cell(name=f"{localized}\\目标边线.1", obj=None, kind="edge", length=source.length, samples=list(source.samples))
        outcome = pair_from_source_cell(session, part, cell, cell.name, cfg, cells)
        group = next(iter(outcome.groups.values()))
        target = group[0].target
        out(f"[对称边线] {target.describe()} {[p.as_tuple() for p in target.samples]}（距离 {group[0].distance:.4f}）")

        vol_before = session.volume_of(part)
        radius = 5.0
        ref_source = session.reference_from_label(part, source.label, feature)
        ref_target = session.reference_from_label(part, target.label, feature)
        out(f"[引用] 源={ref_source is not None} 对称={ref_target is not None} 体积前={vol_before}")

        shape_factory = session.dyn(part).ShapeFactory
        fillet = shape_factory.AddNewEdgeFilletWithConstantRadius(ref_source, 1, radius)
        fillet.AddObjectToFillet(ref_target)
        part.Update()
        vol_after = session.volume_of(part)
        shapes = int(session.dyn(part).MainBody.Shapes.Count)
        last = session.dyn(part).MainBody.Shapes.Item(shapes).Name
        out(f"[结果] 体积 {vol_before} → {vol_after}；特征数={shapes} 最后特征={last}")
        if isinstance(vol_after, (int, float)) and isinstance(vol_before, (int, float)) and vol_after < vol_before:
            out("✔ 自动化直接生成对称倒圆角成功")
            return 0
        out("✘ 体积没有变化，倒圆角可能没生效")
        return 1
    finally:
        try:
            doc.Close()
            out("[清理] 已关闭临时文档（未保存）")
        except Exception as exc:
            out(f"[清理] 关闭失败: {exc}")


if __name__ == "__main__":
    raise SystemExit(main())
