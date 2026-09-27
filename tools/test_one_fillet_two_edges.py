"""验收：只生成**一个**倒圆角特征，且里面同时含"原来的边 + 对称边"（用体积变化验证两条边都进去了）。"""

from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import win32com.client as wc  # noqa: E402
import win32con  # noqa: E402
import win32gui  # noqa: E402
from win32com.client import dynamic  # noqa: E402

from catia_fillet_assistant import featuregeom, pairing, win32util  # noqa: E402
from catia_fillet_assistant.catia import CatiaSession  # noqa: E402
from catia_fillet_assistant.cellgeom import Cell, CellIndex  # noqa: E402
from catia_fillet_assistant.config import Config  # noqa: E402


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


def shapes_of(session, part) -> list[str]:
    shapes = session.dyn(part).MainBody.Shapes
    return [str(shapes.Item(i).Name) for i in range(1, int(shapes.Count) + 1)]


def main() -> int:
    app = dynamic.Dispatch(wc.GetActiveObject("CATIA.Application")._oleobj_)
    app.DisplayFileAlerts = False
    doc = make_part(app)
    session = CatiaSession()
    cells = CellIndex(session)
    cfg = Config()
    cfg.symmetry_mode = "mirror"
    cfg.mirror_axis = "y"
    radius = 5.0
    try:
        part = session.active_part()
        feature = session.object_by_name(part, "Pad.1")
        fname, sname = featuregeom.name_candidates(session, part, feature, str(feature.Name))[0]
        sketch = featuregeom.feature_attr(session, feature, "Sketch")
        frame = session.read_sketch_frame(sketch)
        elements = session.read_sketch_elements(sketch)
        first, second, sym = featuregeom.read_limits(session, feature)
        cands = featuregeom.prismatic_candidates(
            session, part, feature, fname, sname, frame, elements,
            featuregeom._z_ranges(first, second, sym),
        )
        source_cand = next(
            c
            for c in cands
            if c.kind == "vertical" and abs(c.samples[0].x - 100) < 0.2 and abs(c.samples[0].y) < 0.2
        )
        out(f"[模拟点选] {source_cand.describe()} 采样={[p.as_tuple() for p in source_cand.samples]}")

        out("[步骤] 打开倒圆角命令（会产生进行中的特征）")
        win32util.start_edge_fillet(session.connect())
        time.sleep(0.8)
        out(f"    特征树：{shapes_of(session, part)}")

        cell = Cell(
            name=f"{feature.Name}\\\\目标边线.1",
            obj=None,
            kind="edge",
            length=source_cand.length,
            samples=list(source_cand.samples),
        )
        outcome = pairing.pair_from_source_cell(session, part, cell, cell.name, cfg, cells)
        groups = list(outcome.groups.values())
        if not groups or not groups[0]:
            out(f"✘ 配对失败：{outcome.message}")
            return 1
        target = groups[0][0].target
        out(f"[配对] {source_cand.describe()} → {target.describe()}（距离 {groups[0][0].distance:.4f}）")

        out("[步骤] 关掉进行中的命令，避免留下多余特征")
        dialog = win32util.find_fillet_dialog()
        if dialog is not None:
            win32gui.PostMessage(dialog.hwnd, win32con.WM_CLOSE, 0, 0)
            time.sleep(1.2)
        out(f"    特征树：{shapes_of(session, part)}  体积={session.volume_of(part)}")

        ref_source = session.reference_from_label(part, source_cand.label, feature)
        ref_target = session.reference_from_label(part, target.label, feature)
        before = session.volume_of(part)
        ok, message = pairing.create_edge_fillet(
            session,
            part,
            [ref_source, ref_target],
            radius=radius,
            propagation=1,
            expected_lengths=[source_cand.length, target.length],
        )
        after = session.volume_of(part)
        names = shapes_of(session, part)
        fillets = [n for n in names if "圆角" in n or "Fillet" in n]
        out(f"[结果] {ok} {message}")
        out(f"    特征树：{names}")
        out(f"    倒圆角特征数={len(fillets)}  体积 {before} → {after}")
        r_m = radius / 1000.0
        expected = sum(
            (1 - 3.14159265358979 / 4) * r_m * r_m * (length / 1000.0)
            for length in (source_cand.length, target.length)
        )
        delta = abs((after or 0) - (before or 0))
        out(f"    预期体积变化≈{expected * 1e9:.1f} mm³  实际={delta * 1e9:.1f} mm³")
        if ok and len(fillets) == 1 and expected > 0 and abs(delta - expected) / expected < 0.35:
            out("✔ 一个倒圆角特征里确实包含了两条边（体积变化与两条边吻合）")
            return 0
        out("✘ 结果不符合预期")
        return 1
    finally:
        for hwnd, cls, title in win32util.catia_windows():
            if cls == "#32770" and "圆角" in title:
                win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
        time.sleep(0.4)
        try:
            doc.Close()
            out("[清理] 已关闭临时文档（未保存）")
        except Exception as exc:
            out(f"[清理] 关闭失败: {exc}")


if __name__ == "__main__":
    raise SystemExit(main())
