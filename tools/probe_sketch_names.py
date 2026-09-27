"""只读：打印草图里每个元素的【原始序号 / 名字 / 类型 / 坐标】，用来核对 BRep 编号（Sketch.1;N）到底对应谁。"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from catia_fillet_assistant import featuregeom, win32util  # noqa: E402
from catia_fillet_assistant.catia import CatiaSession  # noqa: E402


def out(msg: str = "") -> None:
    print(msg, flush=True)


def main() -> int:
    session = CatiaSession()
    part = session.active_part()
    dialog = win32util.find_fillet_dialog()
    entries = win32util.fillet_dialog_entries(dialog.hwnd) if dialog else []
    feature_local = entries[0].split("\\", 1)[0].strip() if entries else "凸台.1"
    feature = session.object_by_name(part, feature_local)
    out(f"[特征] {feature_local} → {feature}")
    sketch = featuregeom.feature_attr(session, feature, "Sketch")
    sk = sketch.QueryInterface(session.mod("mec").Sketch)
    geoms = sk.GeometricElements
    count = int(geoms.Count)
    out(f"[草图] {featuregeom.feature_attr(session, sketch, 'Name', '?')}  元素总数={count}")
    out(" 原始序号 | 类型 | 名字            | 采样点")
    measured = session.read_sketch_elements(sketch)
    measured_by_index = {el.index: el for el in measured}
    for i in range(1, count + 1):
        el = geoms.Item(i)
        geo_type = int(el.GeometricType)
        name = str(el.Name)
        got = measured_by_index.get(i)
        pts = ""
        if got is not None:
            pts = " ".join(f"({p[0]:.1f},{p[1]:.1f})" for p in got.points[:3])
        note = "" if got is not None else "  ← 我们没采到点（不参与候选）"
        out(f" {i:>8} | {geo_type:>4} | {name:<15} | {pts}{note}")
    out("")
    out("[读回来的元素索引] " + ", ".join(str(el.index) for el in measured))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
