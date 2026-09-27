"""R5 指纹探针（只读）：把每条边的**原始**测量数据全打出来，用来定位
"长度 60 却只量到半条"/"GetCOG 不在中点" 这类不一致。

输出：每条边的 Length / GetCOG / GetDirection / GetCenter / Radius / Angle /
      GetPointsOnCurve 原始点列（含重复测量稳定性）。
所有操作都在新建临时件上，测完关闭不保存。
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from comtypes.automation import BSTR  # noqa: E402

from catia_fillet_assistant import shape_symmetry as sym  # noqa: E402
from catia_fillet_assistant.catia import CatiaSession  # noqa: E402
from catia_fillet_assistant.cellgeom import _flat_points, polyline_length  # noqa: E402
from catia_fillet_assistant.edge_scan import EdgeScanner  # noqa: E402

OUT_DIR = ROOT / "docs" / "API探测"


def out(message: str = "") -> None:
    print(message, flush=True)


def make_box(app, session: CatiaSession):
    dyn_app = session.dyn(app)
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


def raw_measure(session: CatiaSession, reference) -> dict:
    """直接调 Measurable 的每个接口，全部原样记录（不做任何修补）。"""
    data = {}
    workbench = session.workbench()
    measurable = workbench.GetMeasurable(reference)
    for attr in ("Length", "Area", "Perimeter", "Radius", "Angle"):
        try:
            data[attr] = getattr(measurable, attr)
        except Exception as exc:
            data[attr] = f"<err {str(exc)[:60]}>"
    for method, size in (("GetCOG", 3), ("GetCenter", 3), ("GetDirection", 3), ("GetAxis", 3), ("GetPoint", 3)):
        fn = getattr(measurable, method, None)
        if fn is None:
            data[method] = "<无此方法>"
            continue
        try:
            data[method] = list(fn(session._buf(size)))
        except Exception as exc:
            data[method] = f"<err {str(exc)[:60]}>"
    layout = None
    for spec in session.mod("spa").Measurable._methods_:
        if spec.name == "GetPointsOnCurve":
            layout = spec.argtypes[0]
    if layout is not None:
        for count in (12, 60):
            try:
                buffer = layout.create([0.0] * (3 * count))
                points = _flat_points(measurable.GetPointsOnCurve(buffer), count)
                data[f"points_{count}"] = [[round(v, 4) for v in p.as_tuple()] for p in points]
                data[f"points_{count}_polyline"] = round(polyline_length(points), 4)
            except Exception as exc:
                data[f"points_{count}"] = f"<err {str(exc)[:60]}>"
    try:
        data["GeometryName"] = str(measurable.GeometryName)
    except Exception as exc:
        data["GeometryName"] = f"<err {str(exc)[:60]}>"
    return data


def main() -> int:
    session = CatiaSession()
    app = session.connect()
    out(f"[连接] CATIA 已连接")
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

    report = {"timestamp": datetime.now().isoformat(timespec="seconds"), "edges": [], "planes": []}
    doc = None
    try:
        doc, part = make_box(app, session)
        selection = session.selection()
        selection.Clear()
        selection.Add(part.MainBody)  # 用 comtypes 具体接口对象，别混动态包装
        selection.Search(BSTR("Topology.CGMEdge,sel"))
        count = int(selection.Count2)
        out(f"[扫描] Topology.CGMEdge,sel → {count} 个单元")
        for index in range(1, count + 1):
            element = selection.Item2(index)
            reference = element.Reference
            entry = {"index": index}
            try:
                entry["type"] = str(element.Type)
            except Exception:
                entry["type"] = "?"
            try:
                entry["display"] = str(reference.DisplayName)[:60]
            except Exception:
                entry["display"] = ""
            entry["raw"] = raw_measure(session, reference)
            report["edges"].append(entry)
            length = entry["raw"].get("Length")
            cog = entry["raw"].get("GetCOG")
            pts = entry["raw"].get("points_12")
            poly = entry["raw"].get("points_12_polyline")
            first_last = ""
            if isinstance(pts, list) and len(pts) >= 2:
                first_last = f" 首末={pts[0]}→{pts[-1]}"
            out(
                f"  #{index:2d} {entry['type']:<28} 长={length}"
                f" COG={cog} 折线长={poly}{first_last}"
            )

        # 顺带看看平面评分（用扫描器的指纹）
        scanner = EdgeScanner(session, ttl=0.0)
        edges = scanner.scan(part, force=True)
        points = sym.plane_points(edges)
        for plane in sym.bbox_center_planes(points):
            score = sym.score_plane(edges, plane, tolerance=0.05)
            report["planes"].append({"label": plane.label(), "ratio": score.ratio, "matched": score.matched,
                                     "total": score.total, "mean_error": score.mean_error})
            out(f"[平面] {score.describe()}")
        target = OUT_DIR / f"r5_fingerprint_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        out(f"[输出] {target}")
        return 0
    finally:
        if doc is not None:
            try:
                doc.Close()
                out("[清理] 临时文档已关闭（未保存）")
            except Exception as exc:
                out(f"[清理] 关闭失败：{exc}")
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


if __name__ == "__main__":
    raise SystemExit(main())
