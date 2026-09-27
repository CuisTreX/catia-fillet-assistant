"""探测：从 Pad（凸台）读取草图/限制/方向，并测试各类边线标签能否解析。"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import win32com.client as wc  # noqa: E402
from win32com.client import dynamic  # noqa: E402

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
    pad = part.ShapeFactory.AddNewPad(sketch, 40.0)
    part.Update()
    return doc, pad


def main() -> int:
    app = dynamic.Dispatch(wc.GetActiveObject("CATIA.Application")._oleobj_)
    app.DisplayFileAlerts = False
    doc, pad_dyn = make_part(app)
    session = CatiaSession()
    try:
        part = session.active_part()
        pad = session.object_by_name(part, "Pad.1")
        out(f"[Pad] {pad}")
        dpad = session.dyn(pad)
        for attr in ("Name", "IsSymmetric", "IsThin", "Direction"):
            try:
                out(f"    {attr} = {getattr(dpad, attr)}")
            except Exception as exc:
                out(f"    {attr} 失败: {repr(exc)[:70]}")
        try:
            out(f"    Sketch = {dpad.Sketch}  Name={dpad.Sketch.Name}")
        except Exception as exc:
            out(f"    Sketch 失败: {repr(exc)[:70]}")
        for lim in ("FirstLimit", "SecondLimit"):
            try:
                limit = getattr(dpad, lim)
                dim = getattr(limit, "Dimension", None)
                value = getattr(dim, "Value", None) if dim is not None else None
                out(f"    {lim}: Dimension.Value = {value}  LimitMode={getattr(limit, 'LimitMode', '?')}")
            except Exception as exc:
                out(f"    {lim} 失败: {repr(exc)[:80]}")
        # 方向向量
        try:
            direction = dpad.Direction
            values = []
            for i in range(3):
                try:
                    values.append(direction.Direction(i))
                except Exception as exc:
                    values.append(f"<{exc.__class__.__name__}>")
            out(f"    Direction 分量 = {values}")
        except Exception as exc:
            out(f"    Direction 读取失败: {repr(exc)[:80]}")

        out("")
        out("[标签解析测试] CreateReferenceFromBRepName：")
        templates = {
            "竖直边(元素1,2)": "REdge:(Edge:(Face:(Brp:(Pad.1;0:(Brp:(Sketch.1;1)));None:());"
            "Face:(Brp:(Pad.1;0:(Brp:(Sketch.1;2)));None:());None:(Limits1:();Limits2:()));"
            "WithTemporaryBody;WithoutBuildError;WithSelectingFeatureSupport)",
            "底边(元素1,端面1)": "REdge:(Edge:(Face:(Brp:(Pad.1;0:(Brp:(Sketch.1;1)));None:());"
            "Face:(Brp:(Pad.1;1);None:());None:(Limits1:();Limits2:()));"
            "WithTemporaryBody;WithoutBuildError;WithSelectingFeatureSupport)",
            "顶边(元素1,端面2)": "REdge:(Edge:(Face:(Brp:(Pad.1;0:(Brp:(Sketch.1;1)));None:());"
            "Face:(Brp:(Pad.1;2);None:());None:(Limits1:();Limits2:()));"
            "WithTemporaryBody;WithoutBuildError;WithSelectingFeatureSupport)",
            "端面-端面(1,2)": "REdge:(Edge:(Face:(Brp:(Pad.1;1);None:());"
            "Face:(Brp:(Pad.1;2);None:());None:(Limits1:();Limits2:()));"
            "WithTemporaryBody;WithoutBuildError;WithSelectingFeatureSupport)",
        }
        for tag, label in templates.items():
            ref = session.reference_from_label(part, label, pad)
            ok = ref is not None
            display = None
            if ok:
                try:
                    display = ref.DisplayName
                except Exception:
                    pass
            out(f"    {tag}: {'解析成功' if ok else '失败'}  {str(display)[:90] if display else ''}")
        return 0
    finally:
        try:
            doc.Close()
            out("[清理] 已关闭临时文档（未保存）")
        except Exception as exc:
            out(f"[清理] 关闭失败: {exc}")


if __name__ == "__main__":
    raise SystemExit(main())
