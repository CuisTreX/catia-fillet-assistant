"""验收几何配对链路（模拟"命令里点选了一条边"的完整流程）。

步骤：
    1. 建 100x60x40 的凸台；
    2. 按特征参数算出全部候选边（这一步是新增能力）；
    3. 假装用户点选了角点 (100,0) 的竖直边（用它的实测几何当输入）；
    4. 走 pair_from_source_cell → 应算出对称边 = 角点 (100,60) 的竖直边；
    5. 打开真实倒圆角命令，把两条边加入选择集，点确定 → 验证真的生成倒圆角。
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import win32com.client as wc  # noqa: E402
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


def main() -> int:
    app = dynamic.Dispatch(wc.GetActiveObject("CATIA.Application")._oleobj_)
    app.DisplayFileAlerts = False
    doc = make_part(app)
    session = CatiaSession()
    cells = CellIndex(session)
    try:
        part = session.active_part()
        feature = session.object_by_name(part, "Pad.1")
        localized = str(feature.Name)  # 例如 凸台.1
        out(f"[特征] {localized}")

        names = featuregeom.discover_names(session, part, feature, localized)
        out(f"[内部名] {names}")
        if names is None:
            return 1
        feature_name, sketch_name = names
        frame = session.read_sketch_frame(featuregeom.feature_attr(session, feature, "Sketch"))
        elements = session.read_sketch_elements(featuregeom.feature_attr(session, feature, "Sketch"))
        first, second, symmetric = featuregeom.read_limits(session, feature)
        out(f"[参数] 限制1={first} 限制2={second} 对称={symmetric} 草图元素={len(elements)}")
        candidates = featuregeom.prismatic_candidates(
            session, part, feature, feature_name, sketch_name, frame, elements,
            featuregeom._z_ranges(first, second, symmetric),
        )
        out(f"[候选] 共 {len(candidates)} 条可构造边线：")
        for cand in candidates[:20]:
            pts = " ".join(f"({p.x:.0f},{p.y:.0f},{p.z:.0f})" for p in cand.samples[:2])
            out(f"    {cand.describe():<24} 长={cand.length:.1f} {pts}")
        if not candidates:
            return 1

        # 找角点 (100,0) 的竖直边当"用户点选"的那条
        source_cand = None
        for cand in candidates:
            s0 = cand.samples[0]
            if cand.kind == "vertical" and abs(s0.x - 100) < 0.2 and abs(s0.y - 0) < 0.2:
                source_cand = cand
                break
        if source_cand is None:
            out("!! 没找到角点 (100,0) 的竖直边")
            return 1
        out(f"[模拟点选] {source_cand.describe()} 采样={[p.as_tuple() for p in source_cand.samples]}")

        cell = Cell(name=f"{localized}\\目标边线.1", obj=None, kind="edge",
                    length=source_cand.length, samples=list(source_cand.samples))
        cfg = Config()
        cfg.symmetry_mode = "mirror"
        cfg.mirror_axis = "y"  # 关于 y=30 中面镜像 → 角点 (100,60)
        outcome = pairing.pair_from_source_cell(session, part, cell, cell.name, cfg, cells)
        out(f"[配对] 分组={list(outcome.groups)}  说明={outcome.message or '(无)'}")
        if not outcome.groups:
            return 1
        group = next(iter(outcome.groups.values()))
        target = group[0].target
        out(f"[结果] {group[0].describe()}  距离={group[0].distance:.4f}")
        out(f"    目标采样={[p.as_tuple() for p in target.samples]}")
        if not (abs(target.samples[0].x - 100) < 0.2 and abs(target.samples[0].y - 60) < 0.2):
            out("!! 目标不是角点 (100,60)")
            return 1

        # 端到端：真实倒圆角命令里注入两条边 → 点确定 → 看是否生成特征
        dyn_part = session.dyn(part)
        vol_before = session.volume_of(part)
        out(f"[计数] 建完零件 Body.Shapes = {int(dyn_part.MainBody.Shapes.Count)} 体积={vol_before}")
        ok = win32util.start_edge_fillet(session.connect())
        dialog = win32util.find_fillet_dialog()
        out(f"[命令] 打开={ok} 对话框={dialog.title if dialog else None}")
        session.clear_selection()
        ref_source = feature = session.reference_from_label(part, source_cand.label, feature)
        ref_target = session.reference_from_label(part, target.label, feature)
        out(f"[注入] 源边线引用={ref_source is not None} 对称边线引用={ref_target is not None}")
        if ref_source is not None:
            session.add_to_selection(ref_source)
        if ref_target is not None:
            session.add_to_selection(ref_target)
        time.sleep(0.5)
        clicked = win32util.click_button(dialog.hwnd, ("确定", "OK", "Ok")) if dialog else False
        time.sleep(1.8)
        dialog_gone = win32util.find_fillet_dialog() is None
        volume_after = session.volume_of(part)
        shapes_after = int(dyn_part.MainBody.Shapes.Count)
        try:
            last = dyn_part.MainBody.Shapes.Item(shapes_after).Name
        except Exception:
            last = "?"
        out(f"[端到端] 点击确定={clicked} 对话框已关闭={dialog_gone} 最后一个特征={last}")
        out(f"[端到端] 体积 {vol_before} → {volume_after}（倒圆角应让体积略微变小）")
        changed = (
            isinstance(vol_before, (int, float))
            and isinstance(volume_after, (int, float))
            and volume_after < vol_before
        )
        if dialog_gone and changed:
            out("✔ 几何配对 + 命令内注入 + 生成倒圆角 全链路通过")
            return 0
        out("✘ 端到端验收未通过（对话框未关闭或体积未变化）")
        return 1
    finally:
        for hwnd, cls, title in win32util.catia_windows():
            if cls == "#32770" and "圆角" in title:
                win32util.win32gui.PostMessage(hwnd, win32util.win32con.WM_CLOSE, 0, 0)
        time.sleep(0.4)
        try:
            doc.Close()
            out("[清理] 已关闭临时文档（未保存）")
        except Exception as exc:
            out(f"[清理] 关闭失败: {exc}")


if __name__ == "__main__":
    raise SystemExit(main())

