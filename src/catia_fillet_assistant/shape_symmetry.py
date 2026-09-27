"""形状级对称：镜像平面识别 + 边线镜像匹配评分（纯数学，不依赖 CATIA）。

对应《R5/R14 开发任务 Prompt》的 §7–§27，并补了三条实测教训（见 docs/R5可行性实测-20260925.md）：
    1. **自对称边**：垂直于镜像面、或落在镜像面上的边，镜像后就是它自己 —— 必须先判，否则必然误判；
    2. 采样可能残缺（GetPointsOnCurve 偶发只回半条边）→ 匹配不能只看采样点，必须结合长度/方向/半径；
    3. 旧的"中点相近就算对称"必须禁止：位置 + 长度 + 几何类型 + 半径 + 方向 + 子段重叠 一起判。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .cellgeom import curve_distance
from .cellgeom import resample_polyline
from .geometry import V3, mirror_point

EPS = 1e-9


# --------------------------------------------------------------------- 平面
@dataclass
class Plane:
    normal: V3
    origin: V3
    kind: str = "bbox"  # bbox | pca | pair
    detail: str = ""

    def label(self) -> str:
        n = self.normal
        axis = "X" if abs(n.x) > 0.9 else ("Y" if abs(n.y) > 0.9 else ("Z" if abs(n.z) > 0.9 else "任意"))
        if axis != "任意":
            value = getattr(self.origin, axis.lower())
            return f"{axis} = {value:.2f} 的中面（法向 {axis}）"
        return (
            f"过点({self.origin.x:.1f},{self.origin.y:.1f},{self.origin.z:.1f})、"
            f"法向({n.x:.2f},{n.y:.2f},{n.z:.2f})"
        )

    def mirror(self, point: V3) -> V3:
        return mirror_point(point, self.origin, self.normal)

    def mirror_many(self, points: Sequence[V3]) -> List[V3]:
        return [self.mirror(p) for p in points]

    def distance(self, point: V3) -> float:
        return abs((point - self.origin).dot(self.normal.unit()))


@dataclass
class PlaneScore:
    plane: Plane
    matched: int = 0
    total: int = 0
    mean_error: float = 0.0
    max_error: float = 0.0
    source_error: Optional[float] = None  # 源边镜像后与最近边的距离

    @property
    def ratio(self) -> float:
        return (self.matched / self.total) if self.total else 0.0

    def describe(self) -> str:
        return (
            f"{self.plane.label()}：匹配 {self.matched}/{self.total}（{self.ratio * 100:.0f}%），"
            f"平均误差 {self.mean_error:.3f} mm"
        )


def bbox_center_planes(points: Sequence[V3]) -> List[Plane]:
    """包围盒三个方向的中面（对应 Prompt §18 Level 2）。"""
    if not points:
        return []
    mins = [min(p.as_tuple()[i] for p in points) for i in range(3)]
    maxs = [max(p.as_tuple()[i] for p in points) for i in range(3)]
    planes: List[Plane] = []
    for axis in range(3):
        value = (mins[axis] + maxs[axis]) / 2.0
        normal = V3(
            1.0 if axis == 0 else 0.0,
            1.0 if axis == 1 else 0.0,
            1.0 if axis == 2 else 0.0,
        )
        origin = V3(
            value if axis == 0 else 0.0,
            value if axis == 1 else 0.0,
            value if axis == 2 else 0.0,
        )
        planes.append(Plane(normal=normal, origin=origin, kind="bbox", detail="包围盒中面"))
    return planes


def _principal_axes(points: Sequence[V3]) -> List[V3]:
    """点集的主方向（3x3 协方差矩阵的 Jacobi 特征分解）。"""
    n = len(points)
    if n < 3:
        return []
    mean = V3(0.0, 0.0, 0.0)
    for p in points:
        mean = mean + p
    mean = mean * (1.0 / n)
    matrix = [[0.0] * 3 for _ in range(3)]
    for p in points:
        d = (p - mean).as_tuple()
        for i in range(3):
            for j in range(3):
                matrix[i][j] += d[i] * d[j]
    for i in range(3):
        for j in range(3):
            matrix[i][j] /= n
    values, vectors = _jacobi_eigen(matrix)
    order = sorted(range(3), key=lambda i: -values[i])
    axes: List[V3] = []
    for i in order:
        v = vectors[i]
        if v.norm() > 1e-9:
            axes.append(v.unit())
    return axes


def _jacobi_eigen(matrix: List[List[float]], iterations: int = 32) -> Tuple[List[float], List[V3]]:
    """对称 3x3 矩阵的特征值/特征向量（Jacobi 旋转）。"""
    a = [row[:] for row in matrix]
    v = [[1.0 if i == j else 0.0 for j in range(3)] for i in range(3)]
    for _ in range(iterations):
        p, q, best = 0, 1, abs(a[0][1])
        for i in range(3):
            for j in range(i + 1, 3):
                if abs(a[i][j]) > best:
                    best = abs(a[i][j])
                    p, q = i, j
        if best < 1e-13:
            break
        theta = 0.5 * math.atan2(2.0 * a[p][q], a[q][q] - a[p][p])
        c, s = math.cos(theta), math.sin(theta)
        for k in range(3):
            akp, akq = a[k][p], a[k][q]
            a[k][p] = c * akp - s * akq
            a[k][q] = s * akp + c * akq
        for k in range(3):
            apk, aqk = a[p][k], a[q][k]
            a[p][k] = c * apk - s * aqk
            a[q][k] = s * apk + c * aqk
        for k in range(3):
            vkp, vkq = v[k][p], v[k][q]
            v[k][p] = c * vkp - s * vkq
            v[k][q] = s * vkp + c * vkq
    values = [a[i][i] for i in range(3)]
    vectors = [V3(v[0][i], v[1][i], v[2][i]) for i in range(3)]
    return values, vectors


def pca_planes(points: Sequence[V3]) -> List[Plane]:
    """过形心、法向为主惯性轴的三个平面（对应 Prompt §21 Level 3）。"""
    axes = _principal_axes(points)
    if not axes:
        return []
    n = len(points)
    center = V3(0.0, 0.0, 0.0)
    for p in points:
        center = center + p
    center = center * (1.0 / n)
    return [Plane(normal=axis, origin=center, kind="pca", detail="主惯性面") for axis in axes]


def pair_inferred_planes(source: Any, candidates: Sequence[Any]) -> List[Plane]:
    """由"源边 + 等长候选边"反推镜像平面（对应 Prompt §22 Level 4）。"""
    planes: List[Plane] = []
    p = source.midpoint() if hasattr(source, "midpoint") else None
    if p is None:
        return planes
    for cand in candidates:
        q = cand.midpoint() if hasattr(cand, "midpoint") else None
        if q is None:
            continue
        axis = q - p
        if axis.norm() < 1e-6:
            continue
        middle = (p + q) * 0.5
        planes.append(
            Plane(normal=axis.unit(), origin=middle, kind="pair", detail="由一对候选边反推")
        )
    return planes


def plane_points(edges: Iterable[Any]) -> List[V3]:
    """把一组边的采样点摊平（做包围盒/主方向用）。"""
    points: List[V3] = []
    for edge in edges:
        samples = getattr(edge, "samples", None) or []
        points.extend(samples)
        cog = edge.midpoint() if hasattr(edge, "midpoint") else None
        if cog is not None:
            points.append(cog)
    return points


# --------------------------------------------------------------- 平面评分
def edge_error(edge: Any, plane: Plane, candidate: Any) -> float:
    """把 edge 关于 plane 镜像后，与 candidate 的贴合误差（毫米）。

    先用"长度 + 中点"做必要条件的廉价过滤（两段能重合，中点必然靠得足够近），
    只有通过过滤的少数配对了才做重采样贴合计算 —— 这是 v0.5.0 卡顿的主因。
    """
    if not plausible_pair(edge, plane, candidate):
        return math.inf
    count = 8
    return polyline_distance(
        plane.mirror_many(resampled_points(edge, count)),
        resampled_points(candidate, count),
    )


def resampled_points(edge: Any, count: int = 12) -> List[V3]:
    """按弧长重采样（优先用边自带的缓存，避免重复计算）。"""
    getter = getattr(edge, "resampled", None)
    if callable(getter):
        try:
            return list(getter(count))
        except Exception:
            pass
    return resample_polyline(list(getattr(edge, "samples", None) or []), count)


def polyline_distance(a: Sequence[V3], b: Sequence[V3]) -> float:
    """两条**已按同一采样数**重采样的折线的最大贴合误差（双向）。"""
    if len(a) < 2 or len(b) < 2:
        return math.inf
    worst = 0.0
    for p in a:
        best = min(_point_segment(p, c, d) for c, d in zip(b, b[1:]))
        worst = max(worst, best)
    for p in b:
        best = min(_point_segment(p, c, d) for c, d in zip(a, a[1:]))
        worst = max(worst, best)
    return worst


def plausible_pair(edge: Any, plane: Plane, candidate: Any, tolerance: float = 0.05) -> bool:
    """廉价必要条件：镜像后的中点必须落在候选边附近（允许子段/加长）。"""
    mid = edge.midpoint() if hasattr(edge, "midpoint") else None
    cand_mid = candidate.midpoint() if hasattr(candidate, "midpoint") else None
    if mid is None or cand_mid is None:
        return True
    mirrored_mid = plane.mirror(mid)
    reach = ((getattr(edge, "length", 0.0) or 0.0) + (getattr(candidate, "length", 0.0) or 0.0)) / 2.0
    return (mirrored_mid - cand_mid).norm() <= reach + max(tolerance, 0.05)


def self_symmetric(edge: Any, plane: Plane, tolerance: float) -> bool:
    """边自身关于该平面镜像成自己（垂直于/落在对称面上的边）。"""
    samples = getattr(edge, "samples", None) or []
    if len(samples) < 2:
        return False
    return curve_distance(plane.mirror_many(samples), list(samples), samples=14) <= tolerance


def score_plane(
    edges: Sequence[Any],
    plane: Plane,
    tolerance: float = 0.05,
    source: Optional[Any] = None,
) -> PlaneScore:
    """全局镜像评分：多少比例的边能在这个平面下找到镜像伙伴（对应 Prompt §19）。"""
    score = PlaneScore(plane=plane, total=len(edges))
    errors: List[float] = []
    for edge in edges:
        if self_symmetric(edge, plane, tolerance):
            score.matched += 1
            errors.append(0.0)
            continue
        best = math.inf
        for candidate in edges:
            if candidate is edge:
                continue
            best = min(best, edge_error(edge, plane, candidate))
            if best <= tolerance:
                break  # 已经找到镜像伙伴，不必再比下去（v0.5.0 卡顿主因之一）
        if best <= tolerance:
            score.matched += 1
        if best != math.inf:
            errors.append(best)
        if source is not None and edge is source:
            score.source_error = best if best != math.inf else None
    if errors:
        score.mean_error = sum(errors) / len(errors)
        score.max_error = max(errors)
    return score


def rank_planes(
    edges: Sequence[Any],
    source: Optional[Any] = None,
    tolerance: float = 0.05,
    min_ratio: float = 0.7,
    max_planes: int = 6,
    keep_all: bool = False,
) -> List[PlaneScore]:
    """候选镜像平面排序：包围盒中面 → 主惯性面 → 由候选边反推的面。

    keep_all=True 时不过滤"整体匹配率"——连续多刀之后整体可能已不对称，
    但源边本身仍可能有镜像伙伴（Prompt §44：局部对称 ≠ 整体对称）。
    """
    points = plane_points(edges)
    planes: List[Plane] = []
    planes.extend(bbox_center_planes(points))
    planes.extend(pca_planes(points))
    if source is not None:
        source_length = getattr(source, "length", None)
        similar = [
            item
            for item in edges
            if item is not source
            and source_length
            and getattr(item, "length", None)
            and abs(item.length - source_length) <= max(0.05, source_length * 0.02)
        ]
        planes.extend(pair_inferred_planes(source, similar[:3]))

    scored = [score_plane(edges, plane, tolerance=tolerance, source=source) for plane in planes]
    unique: List[PlaneScore] = []
    for item in sorted(scored, key=lambda s: (-s.ratio, s.mean_error)):
        duplicate = False
        for kept in unique:
            n1, n2 = item.plane.normal.unit(), kept.plane.normal.unit()
            if abs(abs(n1.dot(n2)) - 1.0) < 3.5e-4:  # ≈1.5° 内的近似平行面视为同一个
                distance = (item.plane.origin - kept.plane.origin).norm()
                if distance < max(0.05, tolerance):
                    duplicate = True
                    break
        if not duplicate:
            unique.append(item)
    if keep_all:
        ordered = sorted(
            unique,
            key=lambda s: (s.source_error if s.source_error is not None else 1e9, -s.ratio, s.mean_error),
        )
        return ordered[: max(max_planes, 1)]
    good = [item for item in unique if item.ratio >= min_ratio]
    return (good or unique)[:max_planes]


# --------------------------------------------------------------- 候选匹配
@dataclass
class EdgeMatch:
    target: Any
    score: float
    position_error: float
    length_error: float
    geometry_error: float
    self_match: bool = False
    overlap: float = 0.0
    subsegment: bool = False
    notes: List[str] = field(default_factory=list)
    plane: Optional[Plane] = None

    def describe(self) -> str:
        target = self.target
        mid = target.midpoint() if hasattr(target, "midpoint") else None
        where = f"中点({mid.x:.1f}, {mid.y:.1f}, {mid.z:.1f})" if mid else "中点未知"
        length = f"{target.length:.2f} mm" if getattr(target, "length", None) else "长度未知"
        if getattr(target, "is_circular", False):
            kind = "圆弧"
        elif getattr(target, "is_line", False):
            kind = "直线"
        else:
            kind = "曲线"
        via = f"｜{self.plane.label()}" if self.plane is not None else ""
        return f"{where}，长 {length}，{kind}，匹配度 {self.score * 100:.1f}%{via}"


def _direction_error(a: Any, b: Any) -> float:
    da, db = getattr(a, "direction", None), getattr(b, "direction", None)
    if da is None or db is None:
        return 0.0  # 圆弧没有直线方向，交给半径/圆心去判
    try:
        return 1.0 - abs(da.unit().dot(db.unit()))
    except Exception:
        return 1.0


def _radius_error(a: Any, b: Any) -> float:
    ra, rb = getattr(a, "radius", None), getattr(b, "radius", None)
    if ra is None or rb is None:
        return 0.0
    return abs(ra - rb) / max(abs(ra), EPS)


def _point_segment(p: V3, a: V3, b: V3) -> float:
    ab = b - a
    denom = ab.dot(ab)
    if denom <= 1e-18:
        return (p - a).norm()
    t = (p - a).dot(ab) / denom
    t = 0.0 if t < 0.0 else (1.0 if t > 1.0 else t)
    return (p - (a + ab * t)).norm()


def _overlap_ratio(source_pts: Sequence[V3], target_pts: Sequence[V3], tolerance: float) -> float:
    """两条折线在空间中的重叠比例（用于"圆角把相邻边削短"的子段匹配）。"""
    if len(source_pts) < 2 or len(target_pts) < 2:
        return 0.0

    def covered(points: Sequence[V3], other: Sequence[V3]) -> float:
        total = 0.0
        inside = 0.0
        for a, b in zip(points, points[1:]):
            segment = (b - a).norm()
            total += segment
            mid = (a + b) * 0.5
            best = math.inf
            for c, d in zip(other, other[1:]):
                best = min(best, _point_segment(mid, c, d))
            if best <= max(tolerance, 0.05):
                inside += segment
        return (inside / total) if total > 0 else 0.0

    return min(covered(source_pts, target_pts), covered(target_pts, source_pts))


def _one_way_error(points: Sequence[V3], other: Sequence[V3]) -> float:
    """单向误差：points 上的点全都落在 other 上时接近 0（用于子段匹配）。"""
    if len(points) < 2 or len(other) < 2:
        return math.inf
    from .cellgeom import resample_polyline

    worst = 0.0
    for p in resample_polyline(list(points), 12):
        best = math.inf
        for a, b in zip(other, other[1:]):
            best = min(best, _point_segment(p, a, b))
        worst = max(worst, best)
    return worst


@dataclass
class MatchConfig:
    tolerance: float = 0.05          # 位置容差（mm）
    length_tol_ratio: float = 0.02   # 长度相对容差
    radius_tol_ratio: float = 0.02   # 半径相对容差
    overlap_min: float = 0.7         # 子段重叠下限
    min_score: float = 0.72          # 自动接受的分数下限
    score_margin: float = 0.06       # 第一名与第二名的差距下限
    weight_position: float = 0.40
    weight_length: float = 0.20
    weight_geometry: float = 0.25
    weight_topology: float = 0.15
    center_rel_tol: float = 0.05     # 到中心距离一致性（Prompt §29.2）
    center_abs_tol: float = 1.0
    allow_subsegment: bool = True


def match_candidate(
    source: Any,
    candidate: Any,
    plane: Plane,
    cfg: MatchConfig,
    center: Optional[V3] = None,
) -> Optional[EdgeMatch]:
    """源边镜像后与某条候选边的综合匹配（对应 Prompt §10–§12、§23、§24）。"""
    if source is candidate:
        return None
    # 一级过滤：几何类型必须相容
    if bool(getattr(source, "is_circular", False)) != bool(getattr(candidate, "is_circular", False)):
        return None
    radius_error = _radius_error(source, candidate)
    if radius_error > cfg.radius_tol_ratio:
        return None

    source_samples = list(getattr(source, "samples", None) or [])
    cand_samples = list(getattr(candidate, "samples", None) or [])
    if len(source_samples) < 2 or len(cand_samples) < 2:
        return None
    if not plausible_pair(source, plane, candidate, cfg.tolerance):
        return None
    mirrored = plane.mirror_many(source_samples)
    position_error = curve_distance(mirrored, cand_samples, samples=10)

    length_error = 0.0
    subsegment = False
    overlap = 0.0
    if source.length and candidate.length:
        length_error = abs(candidate.length - source.length) / max(source.length, EPS)
    if length_error > cfg.length_tol_ratio:
        # 允许"被圆角削短（或加长）的子段"：候选必须完全落在预测的镜像曲线上
        if not cfg.allow_subsegment:
            return None
        overlap = _overlap_ratio(mirrored, cand_samples, cfg.tolerance)
        if overlap < cfg.overlap_min:
            return None
        sub_error = (
            _one_way_error(cand_samples, mirrored)
            if (candidate.length or 0.0) <= (source.length or 0.0)
            else _one_way_error(mirrored, cand_samples)
        )
        if sub_error > cfg.tolerance:
            return None
        subsegment = True
        position_error = min(position_error, sub_error)
    elif position_error > cfg.tolerance * 4.0:
        return None

    # 到对称中心的距离一致性（Prompt §29.2）
    if center is not None:
        p, q = source.midpoint(), candidate.midpoint()
        if p is not None and q is not None:
            d1, d2 = (p - center).norm(), (q - center).norm()
            limit = max(cfg.center_abs_tol, abs(d1) * cfg.center_rel_tol)
            if abs(d1 - d2) > limit:
                return None

    sigma = max(cfg.tolerance * 3.0, 0.05)
    s_position = math.exp(-position_error / sigma)
    s_length = (
        max(0.0, min(1.0, overlap))
        if subsegment
        else max(0.0, 1.0 - length_error / max(cfg.length_tol_ratio, EPS))
    )
    s_geometry = max(0.0, 1.0 - radius_error / max(cfg.radius_tol_ratio, EPS)) * (
        1.0 - _direction_error(source, candidate)
    )
    s_topology = 1.0  # 自动化拿不到相邻面：记为中性，权重在 config 里可关
    weights = (
        cfg.weight_position + cfg.weight_length + cfg.weight_geometry + cfg.weight_topology
    ) or 1.0
    score = (
        cfg.weight_position * s_position
        + cfg.weight_length * s_length
        + cfg.weight_geometry * s_geometry
        + cfg.weight_topology * s_topology
    ) / weights
    if subsegment:
        score = min(1.0, score * (0.85 + 0.15 * overlap))
    return EdgeMatch(
        target=candidate,
        score=score,
        position_error=position_error,
        length_error=length_error,
        geometry_error=radius_error + _direction_error(source, candidate),
        overlap=overlap,
        subsegment=subsegment,
        notes=[f"位置误差 {position_error:.3f}mm", f"长度误差 {length_error * 100:.2f}%"],
    )


def rank_candidates(
    edges: Sequence[Any],
    source: Any,
    plane: Plane,
    cfg: MatchConfig,
    center: Optional[V3] = None,
    exclude: Optional[Sequence[Any]] = None,
) -> List[EdgeMatch]:
    """对所有边打分并排序（高分在前）。"""
    skip = set(id(item) for item in (exclude or []))
    matches: List[EdgeMatch] = []
    for candidate in edges:
        if id(candidate) in skip:
            continue
        match = match_candidate(source, candidate, plane, cfg, center=center)
        if match is not None:
            match.plane = plane
            matches.append(match)
    matches.sort(key=lambda m: -m.score)
    return matches


# ------------------------------------------------------------------ 决策
def decide(matches: Sequence[EdgeMatch], cfg: MatchConfig) -> str:
    """按分数与差距给出状态（对应 Prompt §20、§27、§45）。"""
    if not matches:
        return "NO_SYMMETRY"
    best = matches[0]
    if best.score < cfg.min_score:
        return "NO_CONFIDENT_SYMMETRY"
    if len(matches) > 1 and (best.score - matches[1].score) < cfg.score_margin:
        # 两条边几何完全一样（例如正方体四个角）→ 属于"多解"，交给人选
        return "AMBIGUOUS"
    return "SYMMETRY_FOUND"
