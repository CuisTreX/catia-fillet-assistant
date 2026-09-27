"""只读验证：在当前打开的零件上跑一遍配对，只报告结果，不创建特征、不改模型。

用法：在 CATIA 里选好一条边（或打开倒圆角对话框点一条边），然后运行本脚本。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from catia_fillet_assistant import pairing, win32util  # noqa: E402
from catia_fillet_assistant.catia import CatiaSession  # noqa: E402
from catia_fillet_assistant.cellgeom import CellIndex  # noqa: E402
from catia_fillet_assistant.config import Config  # noqa: E402


def out(msg: str = "") -> None:
    print(msg, flush=True)


def main() -> int:
    session = CatiaSession()
    cells = CellIndex(session)
    cfg = Config()
    part = session.active_part()
    if part is None:
        out("当前文档不是 CATPart")
        return 2
    doc = session.active_document()
    out(f"[文档] {doc.Name}  体积={session.volume_of(part)}")

    dialog = win32util.find_fillet_dialog(cfg.extra_dialog_titles)
    entries = win32util.fillet_dialog_entries(dialog.hwnd) if dialog else []
    items = session.selection_items()
    out(f"[对话框] {dialog.title if dialog else '未打开'} 列表={entries}")
    out(f"[选择集] {len(items)} 个：")
    for item in items:
        out(f"    {item.describe()}")
        if item.label:
            out(f"        {item.label.describe()}")

    if entries:
        cell = cells.find_by_name(part, entries[0])
        out(f"[几何] {entries[0]} → {cell.describe() if cell else '解析失败'}")
        if cell is not None:
            outcome = pairing.pair_from_source_cell(session, part, cell, entries[0], cfg, cells)
            out(f"[配对] 源={outcome.source_desc or '(未知)'}")
            if outcome.groups:
                for label, pairs in outcome.groups.items():
                    for pair in pairs:
                        out(
                            f"    ✔ {label}: {pair.source.describe()} → {pair.target.describe()}"
                            f"（距离 {pair.distance:.4f}）"
                        )
            else:
                out(f"    ✘ {outcome.message}")
    elif items:
        for item in items:
            if not item.is_edge:
                out(f"    （跳过非边线：{item.type_name}）")
                continue
            cands, message = pairing.find_counterparts(session, part, item, cfg)
            out(f"[配对] {item.type_name}: {message}")
            for cand in cands[:6]:
                out(f"    ✔ {cand.transform.label}（草图映射 {cand.matched}/{cand.total}）")
    else:
        out("请先在 CATIA 里选一条边线，或打开倒圆角对话框点一条边，再运行本脚本")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
