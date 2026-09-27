"""验收几何配对底座：枚举边线 + 量采样点 + 按几何找对称边线（用临时测试零件）。

测试件：100 x 60 矩形草图拉伸 40，共 12 条边。选角点 (100,0) 的竖直边，
关于 Y 法向中面（y=30）镜像后应得到角点 (100,60) 的竖直边。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import win32com.client as wc  # noqa: E402
from win32com.client import dynamic  # noqa: E402

from catia_fillet_assistant.catia import CatiaSession  # noqa: E402
from catia_fillet_assistant.cellgeom import CellIndex, geometry_matches, polyline_length  # noqa: E402
from catia_fillet_assistant.geometry import Transform, V3  # noqa: E402


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
    index = CellIndex(session, ttl=0.0)
    try:
        part = session.active_part()
        out(f"[连接] part = {part}")
        cog = session.part_cog(part)
        out(f"[重心] {cog.as_tuple() if cog else None}")

        edges = index.edges(part, force=True)
        out(f"[枚举] 找到 {len(edges)} 条边线：")
        for cell in edges:
            pts = " ".join(
                f"({p.x:.0f},{p.y:.0f},{p.z:.0f})" for p in cell.samples[:2]
            )
            ok_len = ""
            if cell.samples and cell.length:
                ok_len = f" 采样折线长={polyline_length(cell.samples):.1f}"
            out(f"    {cell.name:<10} 长={cell.length!s:<8} {pts}{ok_len}")

        if len(edges) < 12:
            out("!! 枚举到的边线少于 12 条，可能名字前缀不对")
            return 1

        # 找角点 (100,0) 的竖直边（采样点 x=100,y=0）
        source = None
        for cell in edges:
            if cell.samples and abs(cell.samples[0].x - 100) < 0.1 and abs(cell.samples[0].y - 0) < 0.1:
                source = cell
                break
        if source is None:
            out("!! 没找到角点 (100,0) 的竖直边")
            return 1
        out(f"[源边线] {source.name} 起点={source.samples[0].as_tuple()} 长度={source.length}")

        transform = Transform.mirror("关于Y法向中面", V3(50, 30, 20), V3(0, 1, 0))
        matches = []
        for cell in edges:
            if cell.name == source.name:
                continue
            ok, dist = geometry_matches(source, cell, transform, tolerance=0.05)
            if ok:
                matches.append((cell, dist))
        out(f"[配对] 关于 Y 法向中面镜像，命中 {len(matches)} 条：")
        for cell, dist in matches:
            out(f"    {cell.name}  距离={dist:.4f}  采样起点={cell.samples[0].as_tuple()}")
        if len(matches) == 1 and abs(matches[0][0].samples[0].y - 60) < 0.1:
            out("✔ 几何配对正确（对应角点 (100,60)）")
            return 0
        out("✘ 几何配对结果不符合预期")
        return 1
    finally:
        try:
            doc.Close()
            out("[清理] 已关闭临时文档（未保存）")
        except Exception as exc:
            out(f"[清理] 关闭失败: {exc}")


if __name__ == "__main__":
    raise SystemExit(main())
