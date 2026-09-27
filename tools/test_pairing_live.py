"""在真实 CATIA 上验证对称配对引擎（新建并关闭临时零件，不保存）。

测试件：100 x 60 矩形草图拉伸 40。四条竖直边分别在四个角：
    (100,0) (100,60) (0,60) (0,0)  ←→ 草图元素对 (1,2) (2,3) (3,4) (4,1)

预期（源 = 元素对 1,2 即角点 (100,0)）：
    * 关于 X 法向中面镜像 -> (2,3)  角点 (100,60)
    * 关于 Y 法向中面镜像 -> (4,1)  角点 (0,0)
    * 绕 Z 轴 180°        -> (3,4)  角点 (0,60)
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import win32com.client as wc  # noqa: E402
from win32com.client import dynamic  # noqa: E402

from catia_fillet_assistant.catia import CatiaSession  # noqa: E402
from catia_fillet_assistant.config import Config  # noqa: E402
from catia_fillet_assistant.pairing import find_counterparts  # noqa: E402

EDGE_NAME = (
    "REdge:(Edge:(Face:(Brp:(Pad.1;0:(Brp:(Sketch.1;1)));None:());"
    "Face:(Brp:(Pad.1;0:(Brp:(Sketch.1;2)));None:());None:(Limits1:();Limits2:()));"
    "WithTemporaryBody;WithoutBuildError;WithSelectingFeatureSupport)"
)

EXPECT = {
    # 关于 X 法向的中面 = 平面 x=50：角点 (100,0) → (0,0)，即元素对 (4,1)
    ("mirror", "x"): "1-4",
    # 关于 Y 法向的中面 = 平面 y=30：角点 (100,0) → (100,60)，即元素对 (2,3)
    ("mirror", "y"): "2-3",
    # 绕 Z 轴 180°：(100,0) → (0,60)，即元素对 (3,4)
    ("rotate", "z"): "3-4",
}


def out(msg: str = "") -> None:
    print(msg, flush=True)


def make_part(app) -> tuple[object, object]:
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
    pad = part.ShapeFactory.AddNewPad(sketch, 40.0)
    part.Update()
    return doc, pad


def main() -> int:
    app_dyn = dynamic.Dispatch(wc.GetActiveObject("CATIA.Application")._oleobj_)
    app_dyn.DisplayFileAlerts = False
    doc_dyn, pad_dyn = make_part(app_dyn)
    out("[准备] 临时零件 Part1.CATPart（100x60x40）已建立")

    session = CatiaSession()
    failures = 0
    try:
        part = session.active_part()
        out(f"[连接] comtypes part = {part}")
        if part is None:
            out("!! 活动文档不是 CATPart")
            return 1

        sketch = session.object_by_name(part, "Sketch.1")
        frame = session.read_sketch_frame(sketch)
        if frame is None:
            out("!! 草图绝对坐标系读取失败")
            return 1
        out(f"[草图坐标系] O={frame.origin.as_tuple()} U={frame.u.as_tuple()} V={frame.v.as_tuple()}")
        elements = session.read_sketch_elements(sketch)
        for el in elements:
            pts = ", ".join(f"({p[0]:.1f},{p[1]:.1f})" for p in el.points[:3])
            out(f"    元素 #{el.index} {el.kind:<7} {pts}")
        if len(elements) != 4:
            out(f"!! 期望 4 个草图元素，实际 {len(elements)}")
            failures += 1

        cog = session.part_cog(part)
        out(f"[零件重心] {tuple(round(c, 2) for c in cog.as_tuple()) if cog else None}")

        ref = session.reference_from_label(part, EDGE_NAME, session.object_by_name(part, "Pad.1"))
        if ref is None:
            out("!! 源边线引用创建失败")
            return 1
        session.clear_selection()
        session.add_to_selection(ref)
        items = session.selection_items()
        if not items:
            out("!! 选择集读取失败")
            return 1
        item = items[0]
        out(f"[源边线] {item.describe()}")
        if item.label:
            out(f"    {item.label.describe()}")

        for (mode, axis), expect in EXPECT.items():
            cfg = Config()
            cfg.symmetry_mode = mode
            cfg.mirror_axis = axis
            cfg.rotate_axis = axis
            cfg.rotate_angle = 180.0
            session.clear_selection()
            session.add_to_selection(ref)
            item = session.selection_items()[0]
            cands, msg = find_counterparts(session, part, item, cfg)
            got: list[str] = []
            for cand in cands:
                session.clear_selection()
                session.add_to_selection(cand.reference)
                shown = session.selection_items()
                if shown and shown[0].label:
                    idx = sorted(s.index for s in shown[0].label.sketches)
                    got.append("-".join(str(i) for i in idx))
            ok = expect in got
            out(f"[{mode}:{axis}] {msg} -> 结果 {got} 期望 {expect} {'✔' if ok else '✘'}")
            if not ok:
                failures += 1

        # 自动模式：应给出多种解释
        cfg = Config()
        cfg.symmetry_mode = "auto"
        session.clear_selection()
        session.add_to_selection(ref)
        item = session.selection_items()[0]
        cands, msg = find_counterparts(session, part, item, cfg)
        labels = sorted({c.transform.label for c in cands})
        out(f"[auto] {msg}")
        for label in labels:
            out(f"    - {label}")
        return 0 if failures == 0 else 1
    finally:
        try:
            doc_dyn.Close()
            out("[清理] 已关闭临时文档（未保存）")
        except Exception as exc:
            out(f"[清理] 关闭失败: {exc}")


if __name__ == "__main__":
    raise SystemExit(main())
