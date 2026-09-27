"""只读调试：把"对话框里点选的边"与"按特征推算的候选边"逐条打出来，看为什么匹配不上。"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from catia_fillet_assistant import featuregeom, pairing, win32util  # noqa: E402
from catia_fillet_assistant.catia import CatiaSession  # noqa: E402
from catia_fillet_assistant.cellgeom import CellIndex, curve_distance  # noqa: E402
from catia_fillet_assistant.config import Config  # noqa: E402


def out(msg: str = "") -> None:
    print(msg, flush=True)


def main() -> int:
    session = CatiaSession()
    cells = CellIndex(session)
    cfg = Config()
    part = session.active_part()
    dialog = win32util.find_fillet_dialog(cfg.extra_dialog_titles)
    entries = win32util.fillet_dialog_entries(dialog.hwnd) if dialog else []
    if not entries:
        out("请先在倒圆角对话框里点一条边线，再运行本脚本")
        return 2
    entry = entries[0]
    cell = cells.find_by_name(part, entry)
    out(f"[源] {entry} → {cell.describe() if cell else '解析失败'}")
    if cell is None:
        return 1
    out(f"     采样点={[tuple(round(c,2) for c in p.as_tuple()) for p in cell.samples]}")

    feature_local = entry.split("\\", 1)[0].strip()
    feature = session.object_by_name(part, feature_local)
    out(f"[特征] {feature_local} → {feature}")
    names = featuregeom.discover_names(session, part, feature, feature_local)
    out(f"[内部名] {names}")
    if names is None:
        return 1
    feature_name, sketch_name = names
    sketch = featuregeom.feature_attr(session, feature, "Sketch")
    out(f"[草图] {sketch}")
    frame = session.read_sketch_frame(sketch)
    elements = session.read_sketch_elements(sketch)
    out(f"[坐标系] O={frame.origin.as_tuple()} U={frame.u.as_tuple()} V={frame.v.as_tuple()} W={frame.w.as_tuple()}")
    out(f"[元素] {len(elements)} 个：")
    for el in elements:
        pts = " ".join(f"({p[0]:.1f},{p[1]:.1f})" for p in el.points[:4])
        out(f"    #{el.index} {el.kind:<7} {pts}")
    first, second, symmetric = featuregeom.read_limits(session, feature)
    out(f"[限制] first={first} second={second} symmetric={symmetric}  区间={featuregeom._z_ranges(first, second, symmetric)}")
    candidates = featuregeom.prismatic_candidates(
        session, part, feature, feature_name, sketch_name, frame, elements,
        featuregeom._z_ranges(first, second, symmetric),
    )
    out(f"[候选] {len(candidates)} 条，按与源采样点的距离排序（前 8 条）：")
    scored = []
    for cand in candidates:
        dist = curve_distance(cell.samples, cand.samples)
        scored.append((dist, cand))
    scored.sort(key=lambda pair: pair[0])
    for dist, cand in scored[:8]:
        pts = " ".join(f"({p.x:.1f},{p.y:.1f},{p.z:.1f})" for p in cand.samples[:2])
        out(f"    距离={dist:9.3f}  {cand.describe():<24} 长={cand.length:7.2f} {pts}")
    best_dist, best = scored[0] if scored else (None, None)
    out(f"[判定] 容差={max(cfg.tolerance, 0.05)}，最近距离={best_dist:.3f} → {'匹配' if best_dist and best_dist <= max(cfg.tolerance,0.05) else '不匹配'}")
    if best_dist and best_dist > max(cfg.tolerance, 0.05):
        out("        （源边的采样点与所有候选都不重合：可能是圆角/阵列/布尔产生的边，")
        out("          或者该特征不是普通拉伸，或者草图元素采样与 BRep 编号不一致）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

