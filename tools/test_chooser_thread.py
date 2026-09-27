"""验收"气泡按钮回调"路径：在**新线程**里（模拟点选择框按钮）关掉命令并生成一个含两条边的倒圆角。"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import comtypes  # noqa: E402
import win32com.client as wc  # noqa: E402
import win32con  # noqa: E402
import win32gui  # noqa: E402
from win32com.client import dynamic  # noqa: E402

from catia_fillet_assistant import featuregeom, pairing, win32util  # noqa: E402
from catia_fillet_assistant.app import FilletAssistant  # noqa: E402
from catia_fillet_assistant.catia import CatiaSession  # noqa: E402
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
    assistant = FilletAssistant(ROOT, Config())  # 只用来调用 _finish_pairing，不启动线程
    assistant.config.label_autocreate = True  # 本测试验证"直接生成"这条路径
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
        source = next(
            c for c in cands
            if c.kind == "vertical" and abs(c.samples[0].x - 100) < 0.2 and abs(c.samples[0].y) < 0.2
        )
        target = next(
            c for c in cands
            if c.kind == "vertical" and abs(c.samples[0].x - 100) < 0.2 and abs(c.samples[0].y - 60) < 0.2
        )
        out(f"[准备] {source.describe()} → {target.describe()}；打开倒圆角命令")
        win32util.start_edge_fillet(session.connect())
        time.sleep(0.8)
        before_vol = session.volume_of(part)
        out(f"    特征树：{shapes_of(session, part)}")

        done: list[str] = []

        def worker() -> None:
            comtypes.CoInitialize()
            try:
                assistant._finish_pairing(part, "Pad.1", source, target, "（验收）关于Y法向中面镜像")
                done.append("ok")
            except Exception as exc:
                done.append(f"err: {exc}")

        thread = threading.Thread(target=worker)
        thread.start()
        thread.join(timeout=60)
        time.sleep(1.0)
        names = shapes_of(session, part)
        after_vol = session.volume_of(part)
        dialog_gone = win32util.find_fillet_dialog() is None
        out(f"[结果] 回调返回={done} 对话框关闭={dialog_gone}")
        out(f"    特征树：{names}  体积 {before_vol} → {after_vol}")
        fillets = [n for n in names if "圆角" in n]
        if done and done[0] == "ok" and dialog_gone and len(fillets) == 1:
            out("✔ 气泡按钮回调路径通过：关掉命令 + 生成一个含两条边的倒圆角")
            return 0
        out("✘ 不符合预期")
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
