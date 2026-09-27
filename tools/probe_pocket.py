"""实验：凹槽(Pocket)的边，用现有候选生成器能不能识别（读草图/限制/方向是否可用）。"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import win32com.client as wc  # noqa: E402
from win32com.client import dynamic  # noqa: E402

from catia_fillet_assistant import featuregeom  # noqa: E402
from catia_fillet_assistant.catia import CatiaSession  # noqa: E402


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
    # 在上表面挖一个 40x30 的凹槽（深 15）
    top = part.OriginElements.PlaneXY
    pocket_sketch = part.MainBody.Sketches.Add(top)
    f2 = pocket_sketch.OpenEdition()
    f2.CreateLine(20.0, 15.0, 60.0, 15.0)
    f2.CreateLine(60.0, 15.0, 60.0, 45.0)
    f2.CreateLine(60.0, 45.0, 20.0, 45.0)
    f2.CreateLine(20.0, 45.0, 20.0, 15.0)
    pocket_sketch.CloseEdition()
    part.Update()
    pocket = part.ShapeFactory.AddNewPocket(pocket_sketch, 15.0)
    part.Update()
    return doc, pocket


def main() -> int:
    app = dynamic.Dispatch(wc.GetActiveObject("CATIA.Application")._oleobj_)
    app.DisplayFileAlerts = False
    doc, pocket_dyn = make_part(app)
    session = CatiaSession()
    try:
        part = session.active_part()
        shapes = session.dyn(part).MainBody.Shapes
        out(f"[特征树] {int(shapes.Count)} 个：")
        for i in range(1, int(shapes.Count) + 1):
            out(f"    {i}. {shapes.Item(i).Name}")
        for name in ("Pocket.1", "凹槽.1", "Pad.1", "凸台.1"):
            feature = session.object_by_name(part, name)
            if feature is None:
                continue
            out(f"[特征] {name} → {feature}")
            sketch = featuregeom.feature_attr(session, feature, "Sketch")
            out(f"    Sketch={sketch}  名字={featuregeom.feature_attr(session, sketch, 'Name', '?') if sketch else '-'}")
            if sketch is not None:
                frame = session.read_sketch_frame(sketch)
                elements = session.read_sketch_elements(sketch)
                out(f"    坐标系 O={frame.origin.as_tuple() if frame else None} W={frame.w.as_tuple() if frame else None}")
                out(f"    元素 {len(elements)} 个：{[ (e.index, e.kind, e.points[0] if e.points else None) for e in elements ]}")
            first, second, symmetric = featuregeom.read_limits(session, feature)
            out(f"    限制 first={first} second={second} symmetric={symmetric} 区间={featuregeom._z_ranges(first, second, symmetric)}")
            names = featuregeom.discover_names(session, part, feature, name)
            out(f"    内部名={names}")
            if names and sketch is not None:
                cands = featuregeom.prismatic_candidates(
                    session, part, feature, names[0], names[1],
                    session.read_sketch_frame(sketch), session.read_sketch_elements(sketch),
                    featuregeom._z_ranges(first, second, symmetric),
                )
                out(f"    候选边 {len(cands)} 条")
                for c in cands[:6]:
                    pts = " ".join(f"({p.x:.0f},{p.y:.0f},{p.z:.0f})" for p in c.samples[:2])
                    out(f"        {c.describe():<22} 长={c.length:7.2f} {pts}")
        return 0
    finally:
        try:
            doc.Close()
            out("[清理] 已关闭临时文档（未保存）")
        except Exception as exc:
            out(f"[清理] 关闭失败: {exc}")


if __name__ == "__main__":
    raise SystemExit(main())
