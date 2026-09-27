"""只读：把零件里所有可能的"竖直边"标签逐个试解析，看哪些标签在模型里真的存在。"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from catia_fillet_assistant.catia import CatiaSession  # noqa: E402


def main() -> int:
    session = CatiaSession()
    part = session.active_part()
    feature = session.object_by_name(part, "Pad.1")
    pairs = [(1, 2), (2, 3), (3, 4), (4, 1), (5, 6), (6, 7), (7, 8), (8, 5), (1, 4), (5, 8), (7, 6), (3, 2)]
    ok_count = 0
    for a, b in pairs:
        label = (
            f"REdge:(Edge:(Face:(Brp:(Pad.1;0:(Brp:(Sketch.1;{a})));None:());"
            f"Face:(Brp:(Pad.1;0:(Brp:(Sketch.1;{b})));None:());None:(Limits1:();Limits2:()));"
            "WithTemporaryBody;WithoutBuildError;WithSelectingFeatureSupport)"
        )
        ref = session.reference_from_label(part, label, feature)
        state = "解析成功" if ref is not None else "解析失败"
        if ref is not None:
            ok_count += 1
        print(f"竖直边(元素 {a}-{b}): {state}")
    print(f"共 {ok_count}/{len(pairs)} 个标签能解析")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
