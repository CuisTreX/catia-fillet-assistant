"""探测：comtypes 下能否直接量到 BRep 边线的 3D 几何（对称匹配最理想的依据）。"""

from __future__ import annotations

import sys

import comtypes.client as cc
from comtypes.automation import BSTR
import win32com.client as wc
from win32com.client import dynamic

BIN = "D:/Programs/CATIA/win_b64/code/bin"
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
    mec = cc.GetModule(BIN + "/MecModTypeLib.tlb")
    spa = cc.GetModule(BIN + "/SPATypeLib.tlb")

    dyn = dynamic.Dispatch(wc.GetActiveObject("CATIA.Application")._oleobj_)
    dyn.DisplayFileAlerts = False
    doc_dyn = dynamic.Dispatch(dyn.Documents.Add("Part")._oleobj_)
    part_dyn = doc_dyn.Part
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
    out("[准备] 测试零件已建立")

    try:
        app = cc.GetActiveObject("CATIA.Application")
        part = app.ActiveDocument.QueryInterface(mec.PartDocument).Part
        wb = app.ActiveDocument.GetWorkbench(BSTR("SPAWorkbench"))
        out(f"[环境] part={part} workbench={type(wb).__name__}")

        pad = part.MainBody.Shapes.Item(BSTR("Pad.1"))
        for label, brep in (("边线", EDGE_NAME), ("面", FACE_NAME)):
            ref = part.CreateReferenceFromBRepName(BSTR(brep), pad)
            m = wb.GetMeasurable(ref)
            out(f"[{label}] measurable={m}")
            for attr in ("Length", "Area", "Perimeter", "Radius"):
                try:
                    out(f"    {attr} = {getattr(m, attr)}")
                except Exception as exc:
                    out(f"    {attr} 失败: {repr(exc)[:90]}")
            lp = None
            for meth in spa.Measurable._methods_:
                if meth.name == "GetPointsOnCurve":
                    lp = meth.argtypes[0]
                    out(f"    GetPointsOnCurve 参数类型={lp}")
            if lp is not None:
                for size in (2, 10, 60):
                    try:
                        buf = lp.create([0.0] * (size * 3))
                        res = m.GetPointsOnCurve(buf)
                        out(f"    GetPointsOnCurve({size} 点缓冲) -> {res}")
                        break
                    except Exception as exc:
                        out(f"    GetPointsOnCurve({size}) 失败: {repr(exc)[:90]}")
        return 0
    finally:
        try:
            doc_dyn.Close()
            out("[清理] 已关闭临时文档（未保存）")
        except Exception as exc:
            out(f"[清理] 关闭失败: {exc}")


if __name__ == "__main__":
    raise SystemExit(main())
