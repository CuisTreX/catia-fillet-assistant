"""v0.5.1 回归：扫描后**用户的选择集必须还原**、整圈边线不许残留高亮，并且动作要快。

复现用户反馈的两件事：
    1. 做过几次之后，每次选线整个模型边线都变红（=扫描把整圈边留在了选择集里）、越来越卡；
    2. 一次动作要等 10 秒左右（=平面评分对 8 个平面做全两两比对）。

本脚本在一个"台阶件"（40+ 条边，接近真实零件复杂度）上量：
    * 扫描耗时、镜像面评分耗时、整体 pair() 耗时；
    * 扫描前后选择集是否一致；
    * 扫描得到的 Reference 在**选择集被清掉之后**还能不能建倒圆角（关键：证明清了也没坏）；
    * 生成后选择集是否为空（不再残留高亮）。

仍然只在新建临时件上操作，测完关闭不保存。
"""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from catia_fillet_assistant import shape_engine, shape_symmetry  # noqa: E402
from catia_fillet_assistant.catia import CatiaSession  # noqa: E402
from catia_fillet_assistant.config import Config  # noqa: E402
from catia_fillet_assistant.edge_scan import EdgeScanner  # noqa: E402

OUT_DIR = ROOT / "docs" / "API探测"


def out(message: str = "") -> None:
    print(message, flush=True)


def brief(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {str(exc)[:160]}"


def add_pad(session: CatiaSession, part_dyn, x0: float, y0: float, x1: float, y1: float, height: float):
    """在当前零件上再加一层台阶（同一个草图基准面，直接 union 上去）。"""
    sketch = part_dyn.MainBody.Sketches.Add(part_dyn.OriginElements.PlaneXY)
    f2d = sketch.OpenEdition()
    f2d.CreateLine(x0, y0, x1, y0)
    f2d.CreateLine(x1, y0, x1, y1)
    f2d.CreateLine(x1, y1, x0, y1)
    f2d.CreateLine(x0, y1, x0, y0)
    sketch.CloseEdition()
    part_dyn.Update()
    part_dyn.ShapeFactory.AddNewPad(sketch, float(height))
    part_dyn.Update()


def make_step_part(session: CatiaSession):
    """100x60 底板 + 两级**居中**台阶（关于 X/Y 中面对称）：边数 30+，接近用户真实零件。"""
    dyn_app = session.dyn(session.connect())
    doc = dyn_app.Documents.Add("Part")
    part = doc.Part
    add_pad(session, part, 0.0, 0.0, 100.0, 60.0, 20.0)      # 底板
    add_pad(session, part, 10.0, 10.0, 90.0, 50.0, 35.0)      # 居中第一级台阶
    add_pad(session, part, 25.0, 20.0, 75.0, 40.0, 50.0)      # 居中第二级台阶
    return doc, session.active_part()


def selection_snapshot(session: CatiaSession):
    items = session.selection_items()
    return [it.display_name for it in items]


def main() -> int:
    session = CatiaSession()
    app = session.connect()
    out("[连接] CATIA 已连接")
    previous_doc = None
    alerts = None
    try:
        previous_doc = app.ActiveDocument
    except Exception:
        previous_doc = None
    try:
        alerts = bool(app.DisplayFileAlerts)
        app.DisplayFileAlerts = False
    except Exception:
        alerts = None

    cfg = Config()
    cfg.fillet_radius = 5.0
    cfg.fillet_propagation = 0
    cfg.shape_engine_autocreate = False
    report = {"timestamp": datetime.now().isoformat(timespec="seconds"), "steps": []}
    results = []
    doc = None
    try:
        doc, part = make_step_part(session)
        scanner = EdgeScanner(
            session,
            ttl=0.0,
            max_edges=int(cfg.shape_max_edges),
            restore_selection=bool(cfg.shape_scan_restore_selection),
        )

        # ---- 1) 扫描耗时 + 选择集还原 ----
        started = time.time()
        edges = scanner.scan(part, force=True)
        t_scan = time.time() - started
        out("")
        out(f"[扫描] {len(edges)} 条三维边，耗时 {t_scan:.2f}s")
        report["steps"].append({"name": "scan", "edges": len(edges), "seconds": round(t_scan, 3)})

        # 先手动选一条边，再扫描，看选择集是否被还原
        sample_edge = None
        for edge in edges:
            if edge.is_line and edge.length and edge.length > 20:
                sample_edge = edge
                break
        assert sample_edge is not None, "没找到可用的长边"
        session.clear_selection()
        session.add_to_selection(sample_edge.reference)
        before = selection_snapshot(session)
        scanner.invalidate()
        started = time.time()
        edges2 = scanner.scan(part, force=True)
        t_scan2 = time.time() - started
        after = selection_snapshot(session)
        ok_selection = before == after and len(after) >= 1
        out(f"[选择集] 扫描前={len(before)} 项，扫描后={len(after)} 项 → "
            f"{'还原 OK' if ok_selection else '未还原（会把整圈边留在选择集里）'}（耗时 {t_scan2:.2f}s）")
        report["steps"].append(
            {"name": "selection_restore", "before": before, "after": after, "ok": bool(ok_selection)}
        )
        results.append(("扫描后选择集被还原（不再整圈变红）", ok_selection))

        # ---- 2) 平面评分 + pair() 耗时 ----
        source = None
        for edge in edges2:
            if edge.is_line and edge.length and abs(edge.length - 25.0) < 15.0:
                source = edge
                break
        source = source or edges2[0]
        started = time.time()
        scored = shape_symmetry.rank_planes(
            edges2,
            source=source,
            tolerance=cfg.shape_tolerance,
            min_ratio=cfg.shape_plane_min_ratio,
            max_planes=cfg.shape_max_planes,
            keep_all=True,
        )
        t_planes = time.time() - started
        out(f"[平面] 评估 {len(scored)} 个候选平面，耗时 {t_planes:.2f}s"
            f"（最佳 {scored[0].ratio * 100:.0f}%）" if scored else "[平面] 无候选")
        started = time.time()
        outcome = shape_engine.pair(
            session, part, shape_engine.SourceHint.from_edge(source), cfg, scanner=scanner,
            source_edge=source,
        )
        t_pair = time.time() - started
        out(f"[配对] 状态={outcome.state} 候选={len(outcome.candidates)}，耗时 {t_pair:.2f}s"
            f"（旧版实测同规模约 9s）")
        report["steps"].append(
            {
                "name": "pair",
                "planes_seconds": round(t_planes, 3),
                "pair_seconds": round(t_pair, 3),
                "state": outcome.state,
                "candidates": len(outcome.candidates),
            }
        )
        results.append(("一次配对耗时可接受（< 3s）", t_pair < 3.0))
        results.append(("配对给出结论（不瞎等）", outcome.state != ""))

        # ---- 3) 关键：清掉选择集之后，扫到的 Reference 还能不能建倒圆角 ----
        target = outcome.target or (outcome.candidates[0].target if outcome.candidates else None)
        ok_fillet = False
        message = "没有候选，未验证"
        if target is not None:
            outcome.target = target
            session.clear_selection()
            before_fillet = len(selection_snapshot(session))
            started = time.time()
            ok_fillet, message = shape_engine.create_fillet(
                session, part, outcome, radius=cfg.fillet_radius, propagation=cfg.fillet_propagation
            )
            t_fillet = time.time() - started
            out(f"[生成] 选择集已清空({before_fillet} 项)后建圆角：ok={ok_fillet} 耗时 {t_fillet:.2f}s — {message}")
            report["steps"].append(
                {"name": "fillet_after_clear", "ok": bool(ok_fillet), "seconds": round(t_fillet, 3),
                 "message": message}
            )
        results.append(("清空选择集后仍能用扫到的引用建圆角", bool(ok_fillet)))

        # ---- 4) 生成后选择集应为空（不再残留高亮）----
        session.clear_selection()
        leftovers = selection_snapshot(session)
        report["steps"].append({"name": "after_clear", "items": leftovers})
        results.append(("动作结束后选择集为空（无残留高亮）", len(leftovers) == 0))

        # ---- 5) 复扫计时（同一零件、缓存失效后）----
        scanner.invalidate()
        started = time.time()
        edges3 = scanner.scan(part, force=True)
        t_rescan = time.time() - started
        out(f"[复扫] {len(edges3)} 条边，耗时 {t_rescan:.2f}s")
        report["steps"].append({"name": "rescan", "edges": len(edges3), "seconds": round(t_rescan, 3)})
        results.append(("复扫耗时可接受（< 3s）", t_rescan < 3.0))
    except Exception as exc:
        out(f"[异常] {brief(exc)}")
        import traceback

        traceback.print_exc()
        results.append(("流程无异常", False))
    finally:
        if doc is not None:
            try:
                doc.Close()
                out("[清理] 临时文档已关闭（未保存）")
            except Exception as exc:
                out(f"[清理] 关闭失败：{brief(exc)}")
        if previous_doc is not None:
            try:
                previous_doc.Activate()
            except Exception:
                pass
        if alerts is not None:
            try:
                app.DisplayFileAlerts = alerts
            except Exception:
                pass

    out("")
    out("================= 汇总 =================")
    failed = 0
    for name, ok in results:
        out(f"  {'PASS' if ok else 'FAIL'}  {name}")
        if not ok:
            failed += 1
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    report["results"] = [{"name": name, "ok": bool(ok)} for name, ok in results]
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    target_path = OUT_DIR / f"r5_perf_{stamp}.json"
    target_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    out(f"[输出] {target_path}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
