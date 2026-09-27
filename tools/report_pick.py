"""只读取证：把"对话框里点选那条边"的实测几何，与"每条候选标签推算的几何"并排列出来。

用途：确定 CATIA BRep 编号与草图元素编号之间到底是怎样的对应（是循环位移就有规律可循）。
绝对不修改模型，只做读取/量测。

用法：在倒圆角对话框里点一条边 → 运行本脚本 → 把输出发回。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from catia_fillet_assistant import featuregeom, win32util  # noqa: E402
from catia_fillet_assistant.catia import CatiaSession  # noqa: E402
from catia_fillet_assistant.cellgeom import CellIndex  # noqa: E402
from catia_fillet_assistant.config import Config  # noqa: E402


def mid(samples) -> str:
    if not samples:
        return "?"
    x = sum(p.x for p in samples) / len(samples)
    y = sum(p.y for p in samples) / len(samples)
    z = sum(p.z for p in samples) / len(samples)
    return f"({x:.1f},{y:.1f},{z:.1f})"


def main() -> int:
    session = CatiaSession()
    cells = CellIndex(session)
    cfg = Config()
    part = session.active_part()
    if part is None:
        print("活动文档不是 CATPart")
        return 2
    dialog = win32util.find_fillet_dialog(cfg.extra_dialog_titles)
    entries = win32util.fillet_dialog_entries(dialog.hwnd) if dialog else []
    print("=" * 78)
    print(f"[对话框] {dialog.title if dialog else '未打开'} 列表={entries}")
    if not entries:
        print("请先在倒圆角对话框里点一条边，再运行本脚本")
        return 2
    entry = entries[0]
    cell = cells.find_by_name(part, entry)
    if cell is None:
        print(f"[点选] {entry} → 解析不到几何")
        return 1
    print(f"[点选] {entry}")
    print(f"    实测：类型={cell.kind} 长度={cell.length} 中点={mid(cell.samples)}")
    print(f"    采样点={[tuple(round(c,1) for c in p.as_tuple()) for p in cell.samples]}")

    feature_local = entry.split("\\", 1)[0].strip() if "\\" in entry else ""
    feature = session.object_by_name(part, feature_local)
    names = featuregeom.name_candidates(session, part, feature, feature_local)
    print(f"[特征] {feature_local} 内部名候选={names[:3]}")
    fname, sname = names[0]
    sketch = featuregeom.feature_attr(session, feature, "Sketch")
    frame = session.read_sketch_frame(sketch)
    elements = session.read_sketch_elements(sketch)
    print(f"[草图] {featuregeom.feature_attr(session, sketch, 'Name', '?')} 可读元素 {len(elements)} 个：")
    for el in elements:
        pts = " ".join(f"({p[0]:.0f},{p[1]:.0f})" for p in el.points[:2])
        print(f"    #{el.index:<3} {el.kind:<7} {pts}")
    first, second, symmetric = featuregeom.read_limits(session, feature)
    print(f"[拉伸] 限制1={first} 限制2={second} 对称={symmetric}")
    candidates = featuregeom.prismatic_candidates(
        session, part, feature, fname, sname, frame, elements,
        featuregeom._z_ranges(first, second, symmetric),
    )
    print(f"[候选] {len(candidates)} 条（长度/中点/与实测的距离）：")
    seen = set()
    for cand in candidates:
        key = (cand.kind, cand.element_index, cand.partner_index)
        if key in seen:
            continue
        seen.add(key)
        info = featuregeom.best_match(cell.samples, [cand], tolerance=1e9)
        dist = info[1] if info[1] != float("inf") else -1
        flag = "  ← 与实测完全重合" if dist >= 0 and dist < 0.05 else ""
        print(
            f"    {cand.describe():<22} 长={cand.length:7.2f} 中点={mid(cand.samples):>20} "
            f"距离={dist:8.3f}{flag}"
        )
    print("=" * 78)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
