"""R5 形状级对称的纯数学单测（不依赖 CATIA，任何 Python 3.8+ 都能跑）。"""

from __future__ import annotations

import math
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from catia_fillet_assistant import shape_symmetry as sym  # noqa: E402
from catia_fillet_assistant.edge_scan import repair_line_samples  # noqa: E402
from catia_fillet_assistant.geometry import V3  # noqa: E402


@dataclass
class FakeEdge:
    """测试用的假边线（字段与 edge_scan.ScannedEdge 对齐）。"""

    label: str
    samples: List[V3]
    length: float
    direction: Optional[V3] = None
    radius: Optional[float] = None
    is_line: bool = True
    is_circular: bool = False
    type_name: str = "RectilinearTriDimFeatEdge"

    def midpoint(self) -> V3:
        total = V3(0.0, 0.0, 0.0)
        for p in self.samples:
            total = total + p
        return total * (1.0 / len(self.samples))

    def describe(self) -> str:
        return self.label

    def short_label(self) -> str:
        return self.label


def line(label: str, a: V3, b: V3) -> FakeEdge:
    mid = (a + b) * 0.5
    return FakeEdge(
        label=label,
        samples=[a, mid, b],
        length=(b - a).norm(),
        direction=(b - a).unit(),
    )


def arc(label: str, center: V3, radius: float, axis: V3, start: V3, end: V3) -> FakeEdge:
    mid = center + ((start - center) + (end - center)).unit() * radius
    length = math.pi / 2.0 * radius
    return FakeEdge(
        label=label,
        samples=[start, mid, end],
        length=length,
        radius=radius,
        is_line=False,
        is_circular=True,
        type_name="TriDimFeatEdge",
    )


def box_edges(w: float = 100.0, d: float = 60.0, h: float = 40.0) -> List[FakeEdge]:
    """100x60x40 长方体的 12 条边（与实测临时件一致）。"""
    x0, x1, y0, y1, z0, z1 = 0.0, w, 0.0, d, 0.0, h
    edges: List[FakeEdge] = []
    for y in (y0, y1):
        for z in (z0, z1):
            edges.append(line(f"X@{y:.0f},{z:.0f}", V3(x0, y, z), V3(x1, y, z)))
    for x in (x0, x1):
        for z in (z0, z1):
            edges.append(line(f"Y@{x:.0f},{z:.0f}", V3(x, y0, z), V3(x, y1, z)))
    for x in (x0, x1):
        for y in (y0, y1):
            edges.append(line(f"Z@{x:.0f},{y:.0f}", V3(x, y, z0), V3(x, y, z1)))
    return edges


# ----------------------------------------------------------------- 基础数学
def test_mirror_point():
    assert sym.Plane(V3(1, 0, 0), V3(0, 0, 0)).mirror(V3(10, 0, 0)).as_tuple() == (-10.0, 0.0, 0.0)
    out = sym.Plane(V3(1, 0, 0), V3(5, 0, 0)).mirror(V3(10, 20, 30))
    assert abs(out.x - 0.0) < 1e-9 and abs(out.y - 20.0) < 1e-9 and abs(out.z - 30.0) < 1e-9
    out = sym.Plane(V3(0, 1, 0), V3(0, 0, 0)).mirror(V3(1, 2, 3))
    assert abs(out.y + 2.0) < 1e-9 and abs(out.x - 1.0) < 1e-9


# ------------------------------------------------------------- 平面识别
def test_bbox_planes_on_box():
    edges = box_edges()
    planes = sym.rank_planes(edges, tolerance=0.05, min_ratio=0.7)
    axes = {tuple(round(v) for v in p.plane.normal.as_tuple()) for p in planes}
    assert (1.0, 0.0, 0.0) in axes
    assert (0.0, 1.0, 0.0) in axes
    assert (0.0, 0.0, 1.0) in axes
    for item in planes:
        assert item.ratio >= 0.7, item.describe()


def test_self_symmetric_edge_is_matched():
    """垂直于/落在对称面上的边镜像后是自己 —— 必须算命中（实测过的坑）。"""
    edges = box_edges()
    plane = sym.Plane(V3(1, 0, 0), V3(50.0, 0.0, 0.0))
    along_x = [e for e in edges if e.label.startswith("X@")]
    assert along_x, "测试件里应当有沿 X 的边"
    for edge in along_x:
        assert sym.self_symmetric(edge, plane, 0.05), edge.label
    score = sym.score_plane(edges, plane, tolerance=0.05)
    assert score.matched == score.total == 12, score.describe()


def test_asymmetric_part_is_rejected():
    """一边倒圆角之后就不再关于 X 中面对称 → 匹配率必须掉下来。"""
    edges = box_edges()
    # 把 x=100 一侧的两条竖边换成 R5 圆弧（模拟圆角后的新形状）
    edges = [e for e in edges if e.label not in ("Z@100,0", "Z@100,60")]
    edges.append(arc("ARC@100,0", V3(95, 0, 5), 5.0, V3(0, 0, 1), V3(100, 0, 5), V3(95, 0, 0)))
    edges.append(arc("ARC@100,60", V3(95, 60, 5), 5.0, V3(0, 0, 1), V3(100, 60, 5), V3(95, 60, 0)))
    plane = sym.Plane(V3(1, 0, 0), V3(50.0, 0.0, 0.0))
    score = sym.score_plane(edges, plane, tolerance=0.05)
    assert score.ratio < 0.7, f"不对称了却仍报高匹配率：{score.describe()}"
    # 关于 Y 中面仍然对称
    ok_plane = sym.Plane(V3(0, 1, 0), V3(0.0, 30.0, 0.0))
    assert sym.score_plane(edges, ok_plane, tolerance=0.05).ratio >= 0.7


# ------------------------------------------------------------- 候选匹配
def test_candidate_match_finds_mirror_edge():
    edges = box_edges()
    source = next(e for e in edges if e.label == "Z@100,0")
    plane = sym.Plane(V3(0, 1, 0), V3(0.0, 30.0, 0.0))
    matches = sym.rank_candidates(edges, source, plane, sym.MatchConfig())
    assert matches, "没找到任何候选"
    assert matches[0].target.label == "Z@100,60", matches[0].describe()
    assert matches[0].score > 0.9


def test_subsegment_match_after_fillet():
    """圆角把相邻边削短后，仍要能认出它是同一条边的子段。"""
    edges = box_edges()
    source = next(e for e in edges if e.label == "X@0,0")  # 从 (0,0,0) 到 (100,0,0)
    shortened = line("X@0,0-short", V3(0, 0, 0), V3(80, 0, 0))
    plane = sym.Plane(V3(0, 1, 0), V3(0.0, 30.0, 0.0))
    cfg = sym.MatchConfig()
    long_pair = line("X@60,0", V3(0, 60, 0), V3(80, 60, 0))
    match = sym.match_candidate(source, long_pair, plane, cfg)
    assert match is not None
    assert match.subsegment, "削短的同轴边应判为子段匹配"

    short_source = line("X@0,0b", V3(0, 0, 0), V3(80, 0, 0))
    match2 = sym.match_candidate(short_source, long_pair, plane, cfg)
    assert match2 is not None and match2.score > 0.7


def test_arc_match_uses_radius():
    a1 = arc("ARC-A", V3(95, 0, 5), 5.0, V3(0, 0, 1), V3(100, 0, 5), V3(95, 0, 0))
    a2 = arc("ARC-B", V3(95, 60, 5), 5.0, V3(0, 0, 1), V3(100, 60, 5), V3(95, 60, 0))
    plane = sym.Plane(V3(0, 1, 0), V3(0.0, 30.0, 0.0))
    match = sym.match_candidate(a1, a2, plane, sym.MatchConfig())
    assert match is not None and match.score > 0.8, match.describe() if match else "无匹配"
    # 半径不同必须被否掉
    a3 = arc("ARC-C", V3(90, 60, 10), 10.0, V3(0, 0, 1), V3(100, 60, 10), V3(90, 60, 0))
    assert sym.match_candidate(a1, a3, plane, sym.MatchConfig()) is None


def test_decide_thresholds():
    cfg = sym.MatchConfig(min_score=0.72, score_margin=0.06)
    strong = [
        sym.EdgeMatch(target=None, score=0.97, position_error=0.0, length_error=0.0, geometry_error=0.0),
        sym.EdgeMatch(target=None, score=0.73, position_error=0.0, length_error=0.0, geometry_error=0.0),
    ]
    assert sym.decide(strong, cfg) == "SYMMETRY_FOUND"
    tied = [
        sym.EdgeMatch(target=None, score=0.90, position_error=0.0, length_error=0.0, geometry_error=0.0),
        sym.EdgeMatch(target=None, score=0.89, position_error=0.0, length_error=0.0, geometry_error=0.0),
    ]
    assert sym.decide(tied, cfg) == "AMBIGUOUS"
    weak = [
        sym.EdgeMatch(target=None, score=0.55, position_error=0.0, length_error=0.0, geometry_error=0.0),
    ]
    assert sym.decide(weak, cfg) == "NO_CONFIDENT_SYMMETRY"
    assert sym.decide([], cfg) == "NO_SYMMETRY"


def test_center_distance_safety():
    """到对称中心距离差太多必须否决（Prompt §29.2）。"""
    edges = box_edges()
    source = next(e for e in edges if e.label == "X@0,0")
    plane = sym.Plane(V3(0, 1, 0), V3(0.0, 30.0, 0.0))
    far = line("FAR", V3(0, 30, 0), V3(100, 30, 0))
    cfg = sym.MatchConfig()
    assert sym.match_candidate(source, far, plane, cfg, center=V3(50.0, 30.0, 20.0)) is None
    near = line("NEAR", V3(0, 60, 0), V3(100, 60, 0))
    assert sym.match_candidate(source, near, plane, cfg, center=V3(50.0, 30.0, 20.0)) is not None


def test_repair_partial_line_samples():
    """实测坑：GetPointsOnCurve 对个别边只回 2 个点（只覆盖半条边）→ 要用方向+长度补全。"""
    # 一条 (0,0,0)→(0,60,0) 的边，CATIA 只回 [0,60,0] 和 [0,30,0]
    partial = [V3(0.0, 60.0, 0.0), V3(0.0, 30.0, 0.0)]
    repaired = repair_line_samples(partial, V3(0.0, -1.0, 0.0), 60.0)
    assert repaired is not None, "半条边的采样应当能补全"
    assert repaired[0].as_tuple() == (0.0, 60.0, 0.0)
    assert repaired[-1].as_tuple() == (0.0, 0.0, 0.0)
    # 方向写反也一样要能补对（用已知点约束）
    repaired2 = repair_line_samples(partial, V3(0.0, 1.0, 0.0), 60.0)
    assert repaired2 is not None
    ends = {repaired2[0].as_tuple(), repaired2[-1].as_tuple()}
    assert ends == {(0.0, 60.0, 0.0), (0.0, 0.0, 0.0)}
    # 采样本来就完整时不需要补：给一条与已知点矛盾的方向应当拒绝
    assert repair_line_samples([V3(0.0, 0.0, 0.0), V3(10.0, 0.0, 0.0)], V3(0.0, 1.0, 0.0), 60.0) is None
