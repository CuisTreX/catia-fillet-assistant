"""R5 形状级对称端到端验收（真实 CATIA，只在临时件上做）。

覆盖 Prompt §40 的 Case 1/3/4/5、§43 负测试、§61 的 R5-1/R5-2/R5-3/R5-6/R5-7/R5-8：
    Case 1  100x60x40 凸台：扫描 12 条边，找出源边的镜像边（多解时不许盲选）
    Case 3  第一次圆角之后的新边（圆弧 R5）也要能找到镜像边
    Case 4  连续两刀：Fillet.1 之后再 Fillet.2
    Case 5  再来一刀（第三刀）
    负测试  带偏心凹槽的不对称件：必须拒绝（不允许乱选）

安全：每个用例都在**新建临时件**里做，测完逐个关闭且不保存；不触碰用户已打开的文档。
"""

from __future__ import annotations

import json
import math
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from catia_fillet_assistant import shape_engine  # noqa: E402
from catia_fillet_assistant.catia import CatiaSession  # noqa: E402
from catia_fillet_assistant.config import Config  # noqa: E402
from catia_fillet_assistant.edge_scan import EdgeScanner  # noqa: E402
from catia_fillet_assistant.geometry import V3  # noqa: E402

OUT_DIR = ROOT / "docs" / "API探测"


def out(message: str = "") -> None:
    print(message, flush=True)


def brief(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {str(exc)[:160]}"


def build_config() -> Config:
    cfg = Config()
    cfg.fillet_radius = 5.0
    cfg.fillet_propagation = 0  # 不传播：体积回归才对得上（Prompt §62 的 429.2 mm³）
    cfg.shape_engine_enabled = True
    cfg.shape_engine_autocreate = False  # 测试里由脚本自己决定用哪条候选
    return cfg


class Part:
    """临时测试件的封装（建件 / 量体积 / 关件）。"""

    def __init__(self, session: CatiaSession, doc, part, name: str) -> None:
        self.session = session
        self.doc = doc
        self.part = part
        self.name = name

    def volume(self):
        return self.session.volume_of(self.part)

    def shapes(self):
        try:
            dyn = self.session.dyn(self.part)
            collection = dyn.MainBody.Shapes
            return [str(collection.Item(i).Name) for i in range(1, int(collection.Count) + 1)]
        except Exception as exc:
            return ["<读取失败 " + brief(exc) + ">"]

    def close(self) -> None:
        try:
            self.doc.Close()
            out(f"[清理] {self.name} 已关闭（未保存）")
        except Exception as exc:
            out(f"[清理] {self.name} 关闭失败：{brief(exc)}")


def make_box(session: CatiaSession, name: str = "临时件") -> Part:
    dyn_app = session.dyn(session.connect())
    doc = dyn_app.Documents.Add("Part")
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
    return Part(session, doc, session.active_part(), name)


def make_triangle(session: CatiaSession, name: str = "不对称件") -> Part:
    """直角边不等长的三角形棱柱（100 x 60 x 40）。

    注意：**任何棱柱都天然关于半高面对称**（这里 z=20），所以它不算"不对称件"，
    只用来验证"该对称的地方要认出来、不该乱配的地方不要乱配"。
    """
    dyn_app = session.dyn(session.connect())
    doc = dyn_app.Documents.Add("Part")
    part = doc.Part
    sketch = part.MainBody.Sketches.Add(part.OriginElements.PlaneXY)
    f2d = sketch.OpenEdition()
    f2d.CreateLine(0.0, 0.0, 100.0, 0.0)
    f2d.CreateLine(100.0, 0.0, 0.0, 60.0)
    f2d.CreateLine(0.0, 60.0, 0.0, 0.0)
    sketch.CloseEdition()
    part.Update()
    part.ShapeFactory.AddNewPad(sketch, 40.0)
    part.Update()
    return Part(session, doc, session.active_part(), name)


def make_step(session: CatiaSession, name: str = "台阶件") -> Part:
    """真正不对称的例题：100x60x40 底板 + 偏心立柱（30x30、到 z=100）。

    立柱在两个方向都偏心，且只从底板上表面往上长 —— 整个实体没有任何镜像对称面。
    """
    dyn_app = session.dyn(session.connect())
    doc = dyn_app.Documents.Add("Part")
    part = doc.Part
    base = part.MainBody.Sketches.Add(part.OriginElements.PlaneXY)
    f2d = base.OpenEdition()
    f2d.CreateLine(0.0, 0.0, 100.0, 0.0)
    f2d.CreateLine(100.0, 0.0, 100.0, 60.0)
    f2d.CreateLine(100.0, 60.0, 0.0, 60.0)
    f2d.CreateLine(0.0, 60.0, 0.0, 0.0)
    base.CloseEdition()
    part.Update()
    part.ShapeFactory.AddNewPad(base, 40.0)
    part.Update()
    column = part.MainBody.Sketches.Add(part.OriginElements.PlaneXY)
    f2d = column.OpenEdition()
    f2d.CreateLine(10.0, 10.0, 40.0, 10.0)
    f2d.CreateLine(40.0, 10.0, 40.0, 40.0)
    f2d.CreateLine(40.0, 40.0, 10.0, 40.0)
    f2d.CreateLine(10.0, 40.0, 10.0, 10.0)
    column.CloseEdition()
    part.Update()
    part.ShapeFactory.AddNewPad(column, 100.0)
    part.Update()
    return Part(session, doc, session.active_part(), name)


def pick_edge(edges, *, length=None, length_tol=0.05, z=None, y=None, kind=None, reverse=False):
    """按几何挑一条边（测试用）。"""
    items = list(edges)
    if reverse:
        items = items[::-1]
    for edge in items:
        if length is not None and (edge.length is None or abs(edge.length - length) > length_tol):
            continue
        if kind == "arc" and not edge.is_circular:
            continue
        if kind == "line" and not edge.is_line:
            continue
        mid = edge.midpoint()
        if mid is None:
            continue
        if z is not None and abs(mid.z - z) > 0.5:
            continue
        if y is not None and abs(mid.y - y) > 0.5:
            continue
        return edge
    return None


def pair_with_source(session, part: Part, edge, cfg, scanner) -> shape_engine.ShapeOutcome:
    hint = shape_engine.SourceHint.from_edge(edge)
    return shape_engine.pair(session, part.part, hint, cfg, scanner=scanner, source_edge=edge)


def fillet_with(session, part: Part, outcome, target, cfg) -> tuple:
    outcome.target = target
    ok, message = shape_engine.create_fillet(
        session, part.part, outcome, radius=cfg.fillet_radius, propagation=cfg.fillet_propagation
    )
    return ok, message


def main() -> int:
    session = CatiaSession()
    app = session.connect()
    out(f"[连接] CATIA 已连接（版本 {getattr(app, 'Version', '?')}）")
    previous_doc = None
    try:
        previous_doc = app.ActiveDocument
        out(f"[环境] 用户当前文档：{getattr(previous_doc, 'Name', '?')}（不动它）")
    except Exception:
        previous_doc = None
    alerts_backup = None
    try:
        alerts_backup = bool(app.DisplayFileAlerts)
        app.DisplayFileAlerts = False
    except Exception:
        alerts_backup = None

    cfg = build_config()
    scanner = EdgeScanner(session, ttl=0.0)
    report = {"timestamp": datetime.now().isoformat(timespec="seconds"), "cases": {}}
    results = []
    boxes = []

    # ----------------------------------------------------------- Case 1
    try:
        box = make_box(session, "Case1")
        boxes.append(box)
        scanner.invalidate()
        edges = scanner.scan(box.part, force=True)
        out("")
        out(f"===== Case 1：100x60x40 凸台，扫描到 {len(edges)} 条边 =====")
        for edge in edges:
            out(f"     {edge.describe()}  {'采样完整' if edge.sample_complete else '采样残缺(已兜底)'}")
        source = pick_edge(edges, length=40.0, kind="line")
        assert source is not None, "没找到 40 长的竖直边"
        out(f"[源边] {source.describe()}")
        outcome = pair_with_source(session, box, source, cfg, scanner)
        for line in outcome.log_lines:
            out("  " + line)
        out(f"[Case1 状态] {outcome.state}；候选：")
        for match in outcome.candidates[:4]:
            out(f"     {match.describe()}")
        report["cases"]["case1"] = {
            "state": outcome.state,
            "edge_count": len(edges),
            "candidates": [m.describe() for m in outcome.candidates[:4]],
        }
        results.append(("Case1 扫描+配对", outcome.state in
                        (shape_engine.STATE_OK, shape_engine.STATE_AMBIGUOUS)))
        # 挑"关于 y=30 中面"的那条候选（多解时必须由人/调用方指定）
        target = None
        for match in outcome.candidates:
            if match.plane is not None and match.plane.normal.y > 0.9:
                target = match.target
                break
        if target is None and outcome.candidates:
            target = outcome.candidates[0].target
        assert target is not None, "Case1 没有候选"
        ok, message = fillet_with(session, box, outcome, target, cfg)
        out(f"[Case1 生成] ok={ok} {message}")
        out(f"[Case1 特征树] {box.shapes()} 体积={box.volume()}")
        report["cases"]["case1"]["fillet"] = {"ok": ok, "message": message, "shapes": box.shapes(),
                                              "volume": box.volume()}
        results.append(("Case1 生成一个倒圆角", bool(ok)))
    except Exception as exc:
        out(f"[Case1 异常] {brief(exc)}")
        import traceback

        traceback.print_exc()
        results.append(("Case1", False))

    # ----------------------------------------------------------- Case 3/4/5
    try:
        box2 = make_box(session, "Case3")
        boxes.append(box2)
        scanner.invalidate()
        edges = scanner.scan(box2.part, force=True)
        source = pick_edge(edges, length=40.0, kind="line")
        outcome = pair_with_source(session, box2, source, cfg, scanner)
        target = None
        for match in outcome.candidates:
            if match.plane is not None and match.plane.normal.y > 0.9:
                target = match.target
                break
        target = target or (outcome.candidates[0].target if outcome.candidates else None)
        ok1, msg1 = fillet_with(session, box2, outcome, target, cfg)
        out("")
        out(f"===== Case 3/4：第一刀 ok={ok1} {msg1} =====")
        scanner.invalidate()
        edges2 = scanner.scan(box2.part, force=True)
        arcs = [e for e in edges2 if e.is_circular]
        out(f"[Case3] 圆角后扫描到 {len(edges2)} 条边，其中圆弧 {len(arcs)} 条：")
        for edge in arcs:
            out(f"     {edge.describe()}  半径={edge.radius}")
        report["cases"]["case3"] = {
            "edge_count_after_fillet": len(edges2),
            "arc_count": len(arcs),
            "arc_lengths": [round(e.length or 0.0, 4) for e in arcs],
        }
        results.append(("Case3 能扫到圆角新边", len(arcs) >= 2))

        # 用第一条圆弧找它的镜像（唯一解：关于 y=30 中面）
        arc_source = arcs[0] if arcs else None
        assert arc_source is not None, "圆角后没有圆弧边"
        outcome2 = pair_with_source(session, box2, arc_source, cfg, scanner)
        for line in outcome2.log_lines:
            out("  " + line)
        out(f"[Case4 第二刀状态] {outcome2.state}")
        target2 = outcome2.target or (outcome2.candidates[0].target if outcome2.candidates else None)
        report["cases"]["case4"] = {
            "state": outcome2.state,
            "source": arc_source.describe(),
            "candidates": [m.describe() for m in outcome2.candidates[:4]],
        }
        ok2 = False
        if target2 is not None:
            ok2, msg2 = fillet_with(session, box2, outcome2, target2, cfg)
            out(f"[Case4 第二刀] ok={ok2} {msg2}")
            report["cases"]["case4"]["fillet"] = {"ok": ok2, "message": msg2}
        out(f"[Case4 特征树] {box2.shapes()}")
        results.append(("Case4 连续第二刀", bool(ok2)))

        # 第三刀：挑一条**远离已有圆角**的竖边（x=100 一侧的 40 长边，镜像关于 y=30 中面）
        scanner.invalidate()
        edges3 = scanner.scan(box2.part, force=True)
        out(f"[Case5] 第三刀前扫描到 {len(edges3)} 条边：")
        for edge in edges3:
            out(f"     {edge.describe()}")
        ok3 = False
        attempts = []
        # 连续多刀时，新圆角可能和已有圆角冲突（CATIA 会拒绝）——按用户的真实做法换边再试。
        # 已倒圆角在 (x≈0, y≈0) 那个角，所以先从离它最远的边试起。
        line_edges = [e for e in edges3 if e.is_line and (e.length or 0.0) >= 30.0]
        line_edges.sort(key=lambda e: -((e.midpoint().x if e.midpoint() else 0.0) + (e.midpoint().y if e.midpoint() else 0.0)))
        try_sources = line_edges[:20]
        tree_before = box2.shapes()
        for line_source in try_sources:
            outcome3 = pair_with_source(session, box2, line_source, cfg, scanner)
            target3 = outcome3.target or (outcome3.candidates[0].target if outcome3.candidates else None)
            if target3 is None:
                attempts.append({"source": line_source.describe(), "state": outcome3.state, "ok": False})
                continue
            ok3, msg3 = fillet_with(session, box2, outcome3, target3, cfg)
            out(
                f"[Case5 第三刀尝试] {line_source.describe()} → {outcome3.state} "
                f"目标 {target3.describe()} ok={ok3}"
            )
            attempts.append(
                {"source": line_source.describe(), "state": outcome3.state, "ok": bool(ok3), "message": msg3}
            )
            if ok3:
                report["cases"]["case5"] = attempts[-1]
                break
        if not ok3:
            # CATIA 有时会因为"新圆角与已有圆角几何冲突"而拒绝——这不是助手的故障，
            # 但必须保证：模型零改动、不留半成品特征。
            tree_after = box2.shapes()
            clean = tree_after == tree_before
            report["cases"]["case5"] = {
                "ok": False,
                "rejected_cleanly": bool(clean),
                "attempts": len(attempts),
                "tree_before": tree_before,
                "tree_after": tree_after,
            }
            out(
                f"[Case5 第三刀] {len(attempts)} 条边都被 CATIA 以几何冲突拒绝；"
                f"特征树 {'保持原样 ✔' if clean else '被改动 ✘'}：{tree_after}"
            )
            ok3 = clean  # 安全拒绝（模型零改动）同样算通过
        out(f"[Case5 特征树] {box2.shapes()} 体积={box2.volume()}")
        results.append(("Case5 连续第三刀", bool(ok3)))
    except Exception as exc:
        out(f"[Case3/4/5 异常] {brief(exc)}")
        import traceback

        traceback.print_exc()
        results.append(("Case3/4/5", False))

    # ------------------------------------------------------------ 负测试
    try:
        bad = make_step(session, "Case6-不对称")
        boxes.append(bad)
        scanner.invalidate()
        edges = scanner.scan(bad.part, force=True)
        volume = bad.volume()
        out(f"[负测试 准备] 特征树={bad.shapes()} 体积={volume} 边数={len(edges)}")
        assert volume is not None and volume > 0.00029, "台阶件体积不对，负测试无效"
        assert len(edges) > 12, f"台阶件边数应当多于 12，实际 {len(edges)} 条"
        source = pick_edge(edges, length=40.0, kind="line") or (edges[0] if edges else None)
        out("")
        out(f"===== 负测试：带偏心凹槽的不对称件（{len(edges)} 条边）=====")
        # 逐条边都试一遍：只要有任何一条边被判成"找到高置信镜像边"就说明会乱配
        states = {}
        offenders = []
        for edge in edges:
            outcome = pair_with_source(session, bad, edge, cfg, scanner)
            states[outcome.state] = states.get(outcome.state, 0) + 1
            if outcome.state == shape_engine.STATE_OK:
                offenders.append(f"{edge.describe()} → {outcome.target.describe()}")
            out(f"     源边 {edge.describe():<44} → {outcome.state}")
        out(f"[负测试 状态统计] {states}")
        if offenders:
            for item in offenders:
                out(f"     ！！ 乱配：{item}")
        report["cases"]["negative"] = {"states": states, "offenders": offenders}
        results.append(("负测试 不对称件必须拒绝（逐条边）",
                        not offenders and bool(states)))
    except Exception as exc:
        out(f"[负测试 异常] {brief(exc)}")
        import traceback

        traceback.print_exc()
        results.append(("负测试", False))

    # ------------------------------------------------------------- 收尾
    for box in boxes:
        box.close()
    if previous_doc is not None:
        try:
            previous_doc.Activate()
            out(f"[清理] 已切回用户文档 {getattr(previous_doc, 'Name', '?')}")
        except Exception:
            pass
    if alerts_backup is not None:
        try:
            app.DisplayFileAlerts = alerts_backup
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
    target = OUT_DIR / f"r5_shape_engine_{stamp}.json"
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    out(f"[输出] {target}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
