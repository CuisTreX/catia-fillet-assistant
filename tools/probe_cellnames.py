"""只读探测：活动文档里哪些 BRep 单元名字能被 FindObjectByName 解析（含几何）。"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from catia_fillet_assistant.catia import CatiaSession  # noqa: E402
from catia_fillet_assistant.cellgeom import CellIndex, polyline_length  # noqa: E402


def main() -> int:
    session = CatiaSession()
    part = session.active_part()
    doc = session.active_document()
    if part is None:
        print("活动文档不是 CATPart")
        return 2
    print(f"活动文档: {doc.Name}")
    index = CellIndex(session, ttl=0.0)

    for prefix in ("边线", "棱边", "Edge", "目标边线", "面", "平面", "圆柱面", "曲面", "圆角面", "顶点"):
        hits: list[str] = []
        for n in range(1, 61):
            cell = index.find_by_name(part, f"{prefix}.{n}")
            if cell is None:
                continue
            extra = ""
            if cell.length is not None:
                extra = f" 长={cell.length:.3f}"
            elif cell.area is not None:
                extra = f" 面积={cell.area:.3f}"
            if cell.samples:
                extra += f" 折线长={polyline_length(cell.samples):.2f} 起点={cell.samples[0].as_tuple()}"
            hits.append(f"{prefix}.{n}[{cell.kind}{extra}]")
        print(f"{prefix}.N -> {len(hits)} 个")
        for item in hits[:14]:
            print(f"    {item}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
