"""探测：草图元素接口查询 + 带 out 参数的几何读取（CATIA V5R20）。

目标：找到在 pywin32 下能真正读出坐标的调用方式。
"""

from __future__ import annotations

import array
import ctypes
import sys
import traceback

import pythoncom
import win32com.client as wc
from win32com.client import VARIANT, dynamic, gencache

IIDS = {
    "Point2D": "{6ED8AC55-6B19-11D1-A280-0000F87546FD}",
    "Line2D": "{A014ABDE-6B19-11D1-A280-0000F87546FD}",
    "Circle2D": "{80EC7321-4B09-0000-0280-030BA6000000}",
}


def out(msg: str = "") -> None:
    print(msg, flush=True)


def try_call(label: str, fn, *args):
    try:
        ret = fn(*args)
        out(f"    {label} -> 返回 {ret}")
        return ret
    except Exception as exc:
        out(f"    {label} -> 失败 {repr(exc)[:110]}")
        return None


def main() -> int:
    app = dynamic.Dispatch(wc.GetActiveObject("CATIA.Application")._oleobj_)
    app.DisplayFileAlerts = False
    doc = dynamic.Dispatch(app.Documents.Add("Part")._oleobj_)
    part = doc.Part
    sk_dyn = part.MainBody.Sketches.Add(part.OriginElements.PlaneXY)
    f = sk_dyn.OpenEdition()
    f.CreateLine(10.0, 20.0, 100.0, 20.0)
    f.CreateLine(100.0, 20.0, 100.0, 80.0)
    f.CreateCircle(50.0, 40.0, 10.0, 0.0, 3.14159265358979)
    sk_dyn.CloseEdition()
    part.Update()

    sketch = gencache.EnsureDispatch(sk_dyn)
    out(f"元素数量 = {sketch.GeometricElements.Count}")
    for i in range(1, sketch.GeometricElements.Count + 1):
        el = sketch.GeometricElements.Item(i)
        out(f"  #{i} 接口={type(el).__name__} GeometricType={getattr(el, 'GeometricType', '?')} Name={getattr(el, 'Name', '?')}")

    out("\n[1] 用 IID 查询元素的具体接口")
    typed = {}
    for i in range(1, sketch.GeometricElements.Count + 1):
        raw = sketch.GeometricElements.Item(i)._oleobj_
        for name, iid in IIDS.items():
            try:
                ptr = raw.QueryInterface(pythoncom.MakeIID(iid))
            except Exception:
                continue
            try:
                obj = gencache.EnsureDispatch(ptr)
            except Exception as exc:
                out(f"    #{i} QI->{name} 成功但包装失败: {exc}")
                continue
            typed.setdefault(name, []).append((i, obj))
            out(f"    #{i} 可以当作 {name}（{type(obj).__name__}）")

    out("\n[2] 各种 buffer 写法读 Point2D 坐标")
    point = None
    for name, lst in typed.items():
        if name == "Point2D" and lst:
            point = lst[0][1]
            break
    if point is None and "Line2D" in typed:
        point = typed["Line2D"][0][1].StartPoint
    out(f"    目标点对象: {type(point).__name__}")

    strategies = [
        ("数组 array('d')", lambda: array.array("d", [0.0, 0.0])),
        ("ctypes double[2]", lambda: (ctypes.c_double * 2)()),
        ("list[0.0,0.0]", lambda: [0.0, 0.0]),
        ("VARIANT(VT_ARRAY|VT_R8)", lambda: VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, [0.0, 0.0])),
        ("VARIANT(VT_ARRAY|VT_VARIANT, list)", lambda: VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_VARIANT, [0.0, 0.0])),
        ("VARIANT(VT_ARRAY|VT_VARIANT, tuple-of-tuple)", lambda: VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_VARIANT, ((0.0, 0.0),))),
    ]
    for label, make in strategies:
        buf = None
        try:
            buf = make()
        except Exception as exc:
            out(f"    {label}: 构造失败 {repr(exc)[:80]}")
            continue
        ret = try_call(label, point.GetCoordinates, buf)
        try:
            shown = buf.value if hasattr(buf, "value") else list(buf)
        except Exception:
            shown = buf
        out(f"        buffer 变成 {shown}")

    out("\n[3] 直接调用不带参数（看是否有默认值）")
    try_call("GetCoordinates()", point.GetCoordinates)
    try_call("StartPoint.GetCoordinates()", lambda: typed["Line2D"][0][1].StartPoint.GetCoordinates())

    out("\n[4] Line2D 参数采样")
    if "Line2D" in typed:
        line = typed["Line2D"][0][1]
        for label, make in strategies[:3]:
            buf = make()
            try_call(f"GetParamExtents({label})", line.GetParamExtents, buf)
            out(f"        buffer = {list(buf)}")
        buf = array.array("d", [0.0, 0.0])
        try_call("GetPointAtParam(0.5, buffer)", line.GetPointAtParam, 0.5, buf)
        out(f"        buffer = {buf.tolist()}")

    doc.Close()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        traceback.print_exc()
        sys.exit(1)
