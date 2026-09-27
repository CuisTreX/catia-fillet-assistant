"""R5/R14 形状级对称引擎：从"用户点的那条边"推到"对称边 + 一个倒圆角特征"。

流程（对应 Prompt §15–§27、§29、§51、§63）：

    源边几何指纹 → 扫描当前实体全部边 → 识别镜像平面 → 镜像源边 → 候选打分
      → 安全校验（类型/长度/半径/中心距离/子段重叠）→ 唯一则直接生成，多解则让人选

只在最后一步写模型（AddNewEdgeFilletWithConstantRadius + Part.Update），识别阶段 100% 只读。
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Any, List, Optional, Sequence, Tuple

from . import pairing
from . import shape_symmetry as sym
from .catia import CatiaSession
from .edge_scan import EdgeScanner, ScannedEdge, measure_reference
from .geometry import V3

log = logging.getLogger(__name__)

# 状态（对应 Prompt §45）
STATE_OK = "SYMMETRY_FOUND"
STATE_AMBIGUOUS = "AMBIGUOUS"
STATE_SELF = "SELF_SYMMETRIC"
STATE_NO_SYMMETRY = "NO_SYMMETRY"
STATE_NO_CONFIDENT = "NO_CONFIDENT_SYMMETRY"
STATE_NO_SOURCE = "SOURCE_NOT_FOUND"
STATE_INVALID = "INVALID_GEOMETRY"
STATE_EMPTY = "NO_EDGES"


@dataclass
class SourceHint:
    """源边几何（可能来自对话框里的命名单元，也可能来自用户选中的 Reference）。"""

    length: Optional[float] = None
    samples: List[V3] = field(default_factory=list)
    cog: Optional[V3] = None
    direction: Optional[V3] = None
    radius: Optional[float] = None
    center: Optional[V3] = None
    axis: Optional[V3] = None
    is_line: bool = False
    is_circular: bool = False
    label: str = ""

    @classmethod
    def from_cell(cls, cell: Any) -> "SourceHint":
        hint = cls(label=str(getattr(cell, "name", "") or ""))
        hint.length = getattr(cell, "length", None)
        samples = list(getattr(cell, "samples", None) or [])
        hint.samples = samples
        if samples:
            total = V3(0.0, 0.0, 0.0)
            for p in samples:
                total = total + p
            hint.cog = total * (1.0 / len(samples))
        return hint

    @classmethod
    def from_edge(cls, edge: ScannedEdge) -> "SourceHint":
        return cls(
            length=edge.length,
            samples=list(edge.samples),
            cog=edge.cog or edge.midpoint(),
            direction=edge.direction,
            radius=edge.radius,
            center=edge.center,
            axis=edge.axis,
            is_line=edge.is_line,
            is_circular=edge.is_circular,
            label=edge.label,
        )


@dataclass
class ShapeOutcome:
    state: str
    message: str = ""
    source: Optional[ScannedEdge] = None
    target: Optional[ScannedEdge] = None
    plane: Optional[sym.PlaneScore] = None
    candidates: List[sym.EdgeMatch] = field(default_factory=list)
    planes: List[sym.PlaneScore] = field(default_factory=list)
    edge_count: int = 0
    log_lines: List[str] = field(default_factory=list)

    @property
    def needs_choice(self) -> bool:
        return self.state == STATE_AMBIGUOUS and len(self.candidates) > 1

    @property
    def ok(self) -> bool:
        return self.state == STATE_OK and self.source is not None and self.target is not None


def build_match_config(config: Any) -> sym.MatchConfig:
    """把配置里的容差/阈值/权重搬进匹配器（全部可配置，不写死）。"""
    return sym.MatchConfig(
        tolerance=float(getattr(config, "shape_tolerance", 0.05) or 0.05),
        length_tol_ratio=float(getattr(config, "shape_length_tol_ratio", 0.02) or 0.02),
        radius_tol_ratio=float(getattr(config, "shape_radius_tol_ratio", 0.02) or 0.02),
        overlap_min=float(getattr(config, "shape_overlap_min", 0.7) or 0.7),
        min_score=float(getattr(config, "shape_min_score", 0.72) or 0.72),
        score_margin=float(getattr(config, "shape_score_margin", 0.06) or 0.06),
        weight_position=float(getattr(config, "shape_weight_position", 0.40)),
        weight_length=float(getattr(config, "shape_weight_length", 0.20)),
        weight_geometry=float(getattr(config, "shape_weight_geometry", 0.25)),
        weight_topology=float(getattr(config, "shape_weight_topology", 0.15)),
        center_rel_tol=float(getattr(config, "shape_center_rel_tol", 0.05)),
        center_abs_tol=float(getattr(config, "shape_center_abs_tol", 1.0)),
        allow_subsegment=bool(getattr(config, "shape_allow_subsegment", True)),
    )


def _bbox_center(points: Sequence[V3]) -> Optional[V3]:
    if not points:
        return None
    mins = [min(p.as_tuple()[i] for p in points) for i in range(3)]
    maxs = [max(p.as_tuple()[i] for p in points) for i in range(3)]
    return V3(
        (mins[0] + maxs[0]) / 2.0,
        (mins[1] + maxs[1]) / 2.0,
        (mins[2] + maxs[2]) / 2.0,
    )


def resolve_source(
    edges: Sequence[ScannedEdge],
    hint: SourceHint,
    cfg: sym.MatchConfig,
) -> Optional[ScannedEdge]:
    """在扫描到的边里认出"用户点的那条"（按长度 + 采样几何 + 中点）。"""
    best: Optional[Tuple[float, ScannedEdge]] = None
    for edge in edges:
        if hint.length and edge.length:
            ratio = abs(edge.length - hint.length) / max(hint.length, 1e-9)
            if ratio > max(cfg.length_tol_ratio * 3.0, 0.05):
                continue
        error = 0.0
        if hint.samples and edge.samples:
            error = sym.curve_distance(list(hint.samples), list(edge.samples), samples=14)
        elif hint.cog is not None and edge.midpoint() is not None:
            error = (hint.cog - edge.midpoint()).norm()
        if hint.cog is not None and edge.midpoint() is not None:
            error = max(error, (hint.cog - edge.midpoint()).norm())
        if error > max(cfg.tolerance * 20.0, 1.0):
            continue
        if best is None or error < best[0]:
            best = (error, edge)
    return best[1] if best is not None else None


def pair(
    session: CatiaSession,
    part: Any,
    hint: SourceHint,
    config: Any,
    scanner: Optional[EdgeScanner] = None,
    source_edge: Optional[ScannedEdge] = None,
    force_scan: bool = True,
) -> ShapeOutcome:
    """识别阶段（只读）：返回源边、镜像平面与候选目标边。"""
    cfg = build_match_config(config)
    own_scanner = scanner is None
    scanner = scanner or EdgeScanner(
        session,
        ttl=float(getattr(config, "shape_cache_seconds", 6.0) or 6.0),
        max_edges=int(getattr(config, "shape_max_edges", 600) or 600),
    )
    queries = getattr(config, "shape_edge_queries", None)
    if queries:
        scanner.queries = tuple(queries)

    edges = scanner.scan(part, force=force_scan)
    if not edges:
        return ShapeOutcome(
            state=STATE_EMPTY,
            message="扫描不到当前实体的边线（可能这个 CATIA 版本的搜索语法不同，见 诊断报告）",
            edge_count=0,
        )
    if len(edges) < 2:
        return ShapeOutcome(
            state=STATE_INVALID,
            message=f"只扫到 {len(edges)} 条边，无法做对称匹配",
            edge_count=len(edges),
        )

    source = source_edge or resolve_source(edges, hint, cfg)
    if source is None:
        return ShapeOutcome(
            state=STATE_NO_SOURCE,
            message="在扫描结果里认不出你点的那条边（几何指纹对不上）",
            edge_count=len(edges),
        )

    center = _bbox_center(sym.plane_points(edges))
    gate = float(getattr(config, "shape_plane_min_ratio", 0.7) or 0.7)
    local_gate = float(getattr(config, "shape_plane_min_ratio_local", 0.6) or 0.6)
    min_local_pairs = int(getattr(config, "shape_min_local_pairs", 3) or 3)
    planes = sym.rank_planes(
        edges,
        source=source,
        tolerance=cfg.tolerance,
        min_ratio=gate,
        max_planes=int(getattr(config, "shape_max_planes", 4) or 4) * 3,
        keep_all=True,
    )
    lines = [
        "===== SHAPE ENGINE START =====",
        f"当前 Tip 扫描到 {len(edges)} 条三维边",
        f"源边：{source.describe()}（标签订阅：{source.short_label()}）",
    ]
    for item in planes:
        lines.append(f"候选镜像面：{item.describe()}")
    if not planes:
        lines.append("没有找到可信的镜像平面")
        return ShapeOutcome(
            state=STATE_NO_SYMMETRY,
            message="这个形状上没找到可信的轴对称关系",
            source=source,
            edge_count=len(edges),
            log_lines=lines,
        )

    best_plane = planes[0]
    if sym.self_symmetric(source, best_plane.plane, cfg.tolerance):
        lines.append("源边本身就在对称面上（镜像后还是自己）")
        if best_plane.ratio < gate:
            message = (
                f"这条边本身就在 {best_plane.plane.label()} 上（镜像后还是它自己）；"
                f"注意整个零件在这里并不对称（整体匹配率只有 {best_plane.ratio * 100:.0f}%），"
                "所以没有另一条镜像边可补。"
            )
        else:
            message = (
                f"这条边本身就在对称面上（{best_plane.plane.label()}），"
                "它没有另一条镜像边 —— 直接对它倒圆角即可。"
            )
        return ShapeOutcome(
            state=STATE_SELF,
            message=message,
            source=source,
            plane=best_plane,
            planes=planes,
            edge_count=len(edges),
            log_lines=lines,
        )

    # 每个候选平面各算一遍源边的镜像候选；连续多刀之后整体可能已不对称，
    # 这时只要"源边自己的镜像误差极小"，就接受该平面的局部解释（Prompt §44）。
    # 性能：只试前 shape_max_planes_try 个平面；一旦"强平面 + 高置信唯一候选"就提前收工
    # （v0.5.0 在真实零件上要算 8 个平面 × 全两两比对，一次动作卡 9 秒）。
    max_try = max(1, int(getattr(config, "shape_max_planes_try", 2) or 2))
    strong_first = [p for p in planes if p.ratio >= gate] + [p for p in planes if p.ratio < gate]
    per_plane: List[Tuple[sym.PlaneScore, List[sym.EdgeMatch]]] = []
    tried = 0
    for plane_score in strong_first:
        if tried >= max_try:
            lines.append(f"  （已试 {tried} 个平面，其余跳过以节省时间；可在 config 调 shape_max_planes_try）")
            break
        tried += 1
        plane_matches = sym.rank_candidates(edges, source, plane_score.plane, cfg, center=center)
        strong = plane_score.ratio >= gate
        if not plane_matches:
            lines.append(f"  平面 {plane_score.describe()} → 无候选")
            continue
        # 整体对称率不足时，只接受"局部解释"：匹配率/支撑边数/源边镜像误差三条都要达标
        local_ok = (
            plane_score.ratio >= local_gate
            and plane_score.matched >= min_local_pairs
            and plane_score.source_error is not None
            and plane_score.source_error <= cfg.tolerance * 2.0
        )
        if not strong and not local_ok:
            lines.append(
                f"  平面 {plane_score.describe()} → 有候选但整体对称率/支撑边数/源边误差不达标，跳过"
            )
            continue
        lines.append(
            f"  平面 {plane_score.describe()} → 最高候选 {plane_matches[0].score * 100:.1f}%"
            f"{'' if strong else '（局部解释）'}"
        )
        per_plane.append((plane_score, plane_matches))
        # 还有别的"强平面"可能给出另一条同样合法的镜像边（矩形件既有左右镜像也有上下镜像）：
        # 那就继续评估，宁可多花 0.3 秒也要让用户自己选，绝不替他猜。
        remaining_strong = [p for p in strong_first[tried:] if p.ratio >= gate]
        if (
            strong
            and plane_matches[0].score >= cfg.min_score
            and len(plane_matches) == 1
            and not remaining_strong
        ):
            break  # 唯一高置信候选，没必要再算别的平面

    if not per_plane:
        lines.append("所有候选平面都给不出可信的镜像边")
        return ShapeOutcome(
            state=STATE_NO_CONFIDENT,
            message="未找到高置信度的轴对称边（不会盲选）",
            source=source,
            planes=planes,
            edge_count=len(edges),
            log_lines=lines + ["===== SHAPE ENGINE END ====="],
        )

    top_score = max(item[1][0].score for item in per_plane)
    matches: List[sym.EdgeMatch] = []
    seen_targets = set()
    chosen: List[sym.PlaneScore] = []
    for plane_score, plane_matches in per_plane:
        if plane_matches[0].score < top_score - cfg.score_margin:
            continue
        chosen.append(plane_score)
        for match in plane_matches:
            key = getattr(match.target, "label", "") or str(id(match.target))
            if key in seen_targets:
                continue
            seen_targets.add(key)
            matches.append(match)
    matches.sort(key=lambda m: -m.score)
    chosen.sort(key=lambda s: (-s.ratio, s.mean_error))
    if chosen:
        best_plane = chosen[0]

    state = sym.decide(matches, cfg)
    lines.append(f"候选 {len(matches)} 条：")
    for match in matches[:6]:
        lines.append(f"    {match.describe()}（{'; '.join(match.notes)}）")
    lines.append(f"结论：{state}")
    lines.append("===== SHAPE ENGINE END =====")

    if state == STATE_OK:
        message = f"找到对称边：{matches[0].describe()}"
    elif state == STATE_AMBIGUOUS:
        message = "这条边有多个同样合理的镜像候选，请选一个"
    else:
        message = "未找到高置信度的轴对称边（不会盲选）"

    return ShapeOutcome(
        state=state,
        message=message,
        source=source,
        target=matches[0].target if matches and state == STATE_OK else None,
        plane=best_plane,
        candidates=matches,
        planes=planes,
        edge_count=len(edges),
        log_lines=lines,
    )


def pair_from_cell(
    session: CatiaSession,
    part: Any,
    cell: Any,
    config: Any,
    scanner: Optional[EdgeScanner] = None,
) -> ShapeOutcome:
    """对话框路径：源边几何来自命令里的命名单元。"""
    hint = SourceHint.from_cell(cell)
    if hint.length is None and not hint.samples:
        return ShapeOutcome(state=STATE_INVALID, message="这条边量不到几何，无法做形状级配对")
    return pair(session, part, hint, config, scanner=scanner)


def pair_from_reference(
    session: CatiaSession,
    part: Any,
    reference: Any,
    config: Any,
    scanner: Optional[EdgeScanner] = None,
) -> ShapeOutcome:
    """选择集路径：源边几何直接量用户选中的 Reference。"""
    edge = measure_reference(session, reference)
    if edge is None:
        return ShapeOutcome(state=STATE_INVALID, message="选中的对象量不到曲线几何（是不是选到面了？）")
    return pair(session, part, SourceHint.from_edge(edge), config, scanner=scanner, source_edge=None)


def create_fillet(
    session: CatiaSession,
    part: Any,
    outcome: ShapeOutcome,
    radius: float,
    propagation: int = 1,
) -> Tuple[bool, str]:
    """写模型阶段（唯一允许的写入）：用扫到的两条边生成**一个**倒圆角特征。"""
    if outcome.source is None or outcome.target is None:
        return False, "没有可用的源边/目标边"
    references = [outcome.source.reference, outcome.target.reference]
    lengths = [outcome.source.length or 0.0, outcome.target.length or 0.0]
    ok, message = pairing.create_edge_fillet(
        session,
        part,
        references,
        radius=radius,
        propagation=propagation,
        expected_lengths=[L for L in lengths if L],
    )
    for line in outcome.log_lines:
        log.debug("形状引擎：%s", line)  # 已由界面层写过一次，这里只留调试级别
    log.info(
        "形状引擎生成结果：ok=%s 半径=%s 源=%s 目标=%s 说明=%s",
        ok,
        radius,
        outcome.source.describe(),
        outcome.target.describe(),
        message,
    )
    return ok, message


def describe_candidates(matches: Sequence[sym.EdgeMatch], limit: int = 4) -> List[Tuple[str, int]]:
    """给气泡按钮用的文案（中点/长度/类型/匹配度，不暴露 BRep 细节）。"""
    items: List[Tuple[str, int]] = []
    for index, match in enumerate(matches[:limit]):
        items.append((match.describe(), index))
    return items
