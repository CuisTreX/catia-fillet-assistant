"""只读验证「标签法」：用新编号规则造一条你零件上的边引用，跑一遍配对，看能不能解析出对称边。

只改选择集，不生成任何特征。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from catia_fillet_assistant import pairing  # noqa: E402
from catia_fillet_assistant.catia import CatiaSession  # noqa: E402
from catia_fillet_assistant.config import Config  # noqa: E402

LABELS = {
    "外角(直线1-4)": (
        "REdge:(Edge:(Face:(Brp:(Pad.1;0:(Brp:(Sketch.1;1)));None:());"
        "Face:(Brp:(Pad.1;0:(Brp:(Sketch.1;4)));None:());None:(Limits1:();Limits2:()));"
        "WithTemporaryBody;WithoutBuildError;WithSelectingFeatureSupport)"
    ),
    "内角(直线5-8)": (
        "REdge:(Edge:(Face:(Brp:(Pad.1;0:(Brp:(Sketch.1;5)));None:());"
        "Face:(Brp:(Pad.1;0:(Brp:(Sketch.1;8)));None:());None:(Limits1:();Limits2:()));"
        "WithTemporaryBody;WithoutBuildError;WithSelectingFeatureSupport)"
    ),
}


def out(msg: str = "") -> None:
    print(msg, flush=True)


def main() -> int:
    session = CatiaSession()
    cfg = Config()
    part = session.active_part()
    if part is None:
        out("当前文档不是 CATPart")
        return 2
    feature = session.object_by_name(part, "Pad.1")
    out(f"[特征] Pad.1 → {feature}")
    for tag, label in LABELS.items():
        ref = session.reference_from_label(part, label, feature)
        out(f"[{tag}] 引用创建={'成功' if ref is not None else '失败'}")
        if ref is None:
            continue
        session.clear_selection()
        session.add_to_selection(ref)
        items = session.selection_items()
        if not items:
            out("    选择集为空")
            continue
        item = items[0]
        out(f"    选择集：{item.describe()}")
        if item.label:
            out(f"        标签：{item.label.describe()}")
        cands, message = pairing.find_counterparts(session, part, item, cfg)
        out(f"    配对：{message}")
        for cand in cands[:6]:
            out(f"        ✔ {cand.transform.label} → 标签草图引用 "
                f"{[(s.sketch, s.index) for s in (cand.reference and item.label or item.label).sketches] if False else ''}")
            out(f"           目标标签片段：{cand.label[:110]}")
    session.clear_selection()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
