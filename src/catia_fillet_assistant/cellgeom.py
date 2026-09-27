"""BRep 单元的枚举与测量（几何配对的底座）。

关键实测（V5R20 中文版）：
    * `Part.FindObjectByName("边线.1")` 能拿到模型里的边线单元对象；
    * 对**这种命名的单元对象**做 `Measurable` 是可用的：
        Length = 20.0，GetPointsOnCurve -> (0,40,30),(10,40,30),(20,40,30)...
      而用 `CreateReferenceFromBRepName` 造出来的临时引用量不出几何。
    * 名字前缀随界面语言变化（中文"边线"/"面"，英文"Edge"/"Face"），所以支持多前缀 + 配置。

用途：
    * 倒圆角命令运行时，用户点选被命令收进对话框（文档选择集为空），
      但对话框列表里能看到 "凸台.1\\目标边线.1" 这样的名字 → 解析出单元名 → 直接量几何；
    * 按几何（镜像/旋转后比对采样点）找对称单元，不再局限于"由草图元素生成"的边线。
"""

from __future__ import annotations

import logging
import math
import re
import time
from dataclasses import dataclass, field
from typing import Any, Iterable

from .geometry import V3

log = logging.getLogger(__name__)

# 单元名前缀（可被 config 追加）
EDGE_PREFIXES = ("边线", "棱边", "Edge", "邊線")
FACE_PREFIXES = ("面", "平面", "圆柱面", "球面", "锥面", "曲面", "圆角面", "Face", "Plane", "Cylinder", "Sphere", "Surface")

KIND_EDGE = "edge"
KIND_FACE = "face"
KIND_VERTEX = "vertex"
KIND_UNKNOWN = "unknown"


@dataclass
class Cell:
    name: str
    obj: Any
    kind: str
    length: float | None = None
    area: float | None = None
    perimeter: float | None = None
    samples: list[V3] = field(default_factory=list)

    def describe(self) -> str:
        if self.kind == KIND_EDGE:
            return f"边线 {self.name}（长 {self.length:.3g}）"
        if self.kind == KIND_FACE:
            return f"面 {self.name}（面积 {self.area:.3g}）"
        return f"{self.name}（{self.kind}）"


def _flat_points(result: Any, max_points: int) -> list[V3]:
    """把 GetPointsOnCurve 返回的扁平数组切成三维点，并去掉尾部补零。"""
    if not isinstance(result, (tuple, list)):
        return []
    values = [float(v) for v in result]
    triplets: list[V3] = []
    for i in range(0, len(values) - 2, 3):
        triplets.append(V3(values[i], values[i + 1], values[i + 2]))
    while triplets and triplets[-1].as_tuple() == (0.0, 0.0, 0.0):
        triplets.pop()
    return triplets[:max_points]


def polyline_length(points: list[V3]) -> float:
    total = 0.0
    for a, b in zip(points, points[1:]):
        total += (b - a).norm()
    return total


class CellIndex:
    """零件里 BRep 单元的索引（按需枚举 + TTL 缓存）。"""

    def __init__(self, session: Any, ttl: float = 8.0, limit: int = 400) -> None:
        self.session = session
        self.ttl = ttl
        self.limit = limit
        self._cells: dict[str, Cell] = {}
        self._stamp = 0.0
        self._part_key: str | None = None
        self.edge_prefixes = EDGE_PREFIXES
        self.face_prefixes = FACE_PREFIXES

    # ------------------------------------------------------------ 单元测量
    def measure(self, part: Any, obj: Any, name: str = "", max_points: int = 30) -> Cell:
        cell = Cell(name=name, obj=obj, kind=KIND_UNKNOWN)
        ref = None
        try:
            ref = part.CreateReferenceFromObject(obj)
        except Exception:
            pass
        measurable = None
        if ref is not None:
            try:
                measurable = self.session.workbench().GetMeasurable(ref)
            except Exception:
                measurable = None
        if measurable is None:
            return cell
        for attr, field_name in (("Length", "length"), ("Area", "area"), ("Perimeter", "perimeter")):
            try:
                value = getattr(measurable, attr)
            except Exception:
                continue
            if isinstance(value, (int, float)):
                setattr(cell, field_name, float(value))
        # 采样（只有曲线类才拿得到点）
        try:
            lp = None
            for spec in self.session.mod("spa").Measurable._methods_:
                if spec.name == "GetPointsOnCurve":
                    lp = spec.argtypes[0]
            if lp is not None:
                buf = lp.create([0.0] * (3 * max_points))
                cell.samples = _flat_points(measurable.GetPointsOnCurve(buf), max_points)
        except Exception:
            cell.samples = []
        if cell.samples and cell.length is not None:
            cell.kind = KIND_EDGE
        elif cell.area is not None:
            cell.kind = KIND_FACE
        elif cell.length is not None:
            cell.kind = KIND_EDGE
        elif cell.perimeter is not None:
            cell.kind = KIND_FACE
        return cell

    def find_by_name(self, part: Any, name: str) -> Cell | None:
        """按名字（含对话框里的 '凸台.1\\目标边线.1' 形式）拿单元。

        对话框里的单元名可能是"命令内部名"（如 目标边线.1，量不出几何），
        所以除了原名，还会用序号拼出标准前缀名（边线.1 / Edge.1 / 面.1 …）逐个试。
        """
        raw = (name or "").strip()
        if not raw:
            return None
        candidates: list[str] = []
        if "\\" in raw:
            feature, cell_name = raw.split("\\", 1)
            feature, cell_name = feature.strip(), cell_name.strip()
            candidates += [cell_name, raw, f"{feature}\\{cell_name}"]
        else:
            candidates.append(raw)
        # 用序号补出标准前缀变体（关键：目标边线.1 → 边线.1）
        for text in list(candidates):
            match = re.search(r"(\d+)\s*$", text)
            if not match:
                continue
            index = match.group(1)
            for prefix in EDGE_PREFIXES + FACE_PREFIXES:
                candidates.append(f"{prefix}.{index}")
            if "\\" in text:
                feat = text.split("\\", 1)[0]
                for prefix in EDGE_PREFIXES + FACE_PREFIXES:
                    candidates.append(f"{feat}\\{prefix}.{index}")
        tried: list[str] = []
        for cand in candidates:
            if cand in tried:
                continue
            tried.append(cand)
            obj = self.session.object_by_name(part, cand)
            if obj is None:
                continue
            cell = self.measure(part, obj, cand)
            if cell.kind != KIND_UNKNOWN:
                log.info("对话框单元解析成功：%s → %s（长=%s 面积=%s）", cand, cell.kind, cell.length, cell.area)
                return cell
            log.debug("候选单元 %s 解析到对象但量不到几何（可能是命令内部对象）", cand)
        log.warning("对话框单元解析失败，已尝试：%s", tried[:20])
        return self._newest_named_cell(part)

    def _newest_named_cell(self, part: Any, want: str = KIND_EDGE, limit: int = 40) -> Cell | None:
        """兜底：扫描已命名的单元，取序号最大的那个（通常就是用户刚点选的那个）。

        名字是连续的，连续 10 个都找不到就换下一个前缀，避免上百次 COM 调用。
        """
        best: tuple[int, Cell] | None = None
        for prefix in EDGE_PREFIXES:
            misses = 0
            for index in range(1, limit + 1):
                name = f"{prefix}.{index}"
                obj = self.session.object_by_name(part, name)
                if obj is None:
                    misses += 1
                    if misses >= 10:
                        break
                    continue
                misses = 0
                cell = self.measure(part, obj, name)
                if cell.kind != want:
                    continue
                if best is None or index > best[0]:
                    best = (index, cell)
        if best is not None:
            log.info("兜底取到最近命名的单元：%s（%s）", best[1].name, best[1].describe())
            return best[1]
        return None

    # ---------------------------------------------------------------- 枚举
    def _enumerate_prefix(self, part: Any, prefix: str, want: str) -> list[Cell]:
        found: list[Cell] = []
        misses = 0
        misses_allowed = 20
        index = 1
        while index <= self.limit and misses < misses_allowed:
            name = f"{prefix}.{index}"
            obj = self.session.object_by_name(part, name)
            if obj is None:
                misses += 1
                index += 1
                continue
            cell = self.measure(part, obj, name)
            misses = 0
            if cell.kind == want:
                found.append(cell)
            index += 1
        if found:
            log.info("枚举到 %s.%d 共 %d 个%s", prefix, index - 1, len(found), "边线" if want == KIND_EDGE else "面")
        return found

    def cells(self, part: Any, want: str = KIND_EDGE, force: bool = False) -> list[Cell]:
        key = f"{want}"
        if not force and self._cells.get(key) and (time.time() - self._stamp) < self.ttl:
            return list(self._cells[key])  # type: ignore[arg-type]
        prefixes = self.edge_prefixes if want == KIND_EDGE else self.face_prefixes
        result: list[Cell] = []
        for prefix in prefixes:
            result.extend(self._enumerate_prefix(part, prefix, want))
            if len(result) >= self.limit:
                break
        self._cells[key] = result  # type: ignore[assignment]
        self._stamp = time.time()
        return list(result)

    def edges(self, part: Any, force: bool = False) -> list[Cell]:
        return self.cells(part, KIND_EDGE, force=force)

    def invalidate(self) -> None:
        self._cells.clear()
        self._stamp = 0.0


def transform_samples(points: Iterable[V3], transform: Any) -> list[V3]:
    return [transform.apply(p) for p in points]


def sample_distance(a: list[V3], b: list[V3]) -> float:
    """双向最近点最大距离（对采样密度不敏感，用于判断两条曲线是否重合）。"""
    if not a or not b:
        return math.inf

    def one_way(src: list[V3], dst: list[V3]) -> float:
        worst = 0.0
        for p in src:
            best = min((p - q).norm() for q in dst)
            worst = max(worst, best)
        return worst

    return max(one_way(a, b), one_way(b, a))


def _segments(points: list[V3]) -> list[tuple[V3, V3]]:
    return list(zip(points, points[1:]))


def point_to_segment_distance(p: V3, a: V3, b: V3) -> float:
    """点到线段的最短距离（处理"同一条直线采样点数不同"的情况）。"""
    ab = b - a
    denom = ab.dot(ab)
    if denom <= 1e-18:
        return (p - a).norm()
    t = (p - a).dot(ab) / denom
    t = 0.0 if t < 0.0 else (1.0 if t > 1.0 else t)
    return (p - (a + ab * t)).norm()


def resample_polyline(points: list[V3], count: int = 12) -> list[V3]:
    """按弧长把折线重采样成 count 个点（直线/圆弧都能对齐采样密度）。"""
    if len(points) < 2 or count < 2:
        return list(points)
    lengths = [0.0]
    for a, b in _segments(points):
        lengths.append(lengths[-1] + (b - a).norm())
    total = lengths[-1]
    if total <= 1e-12:
        return [points[0]] * count
    out: list[V3] = []
    for i in range(count):
        target = total * i / (count - 1)
        # 找到 target 所在的线段
        idx = 0
        while idx < len(lengths) - 2 and lengths[idx + 1] < target:
            idx += 1
        seg_len = lengths[idx + 1] - lengths[idx]
        t = 0.0 if seg_len <= 1e-12 else (target - lengths[idx]) / seg_len
        a, b = points[idx], points[idx + 1]
        out.append(a + (b - a) * t)
    return out


def curve_distance(a: list[V3], b: list[V3], samples: int = 12) -> float:
    """两条曲线的贴合度：先按弧长重采样，再做"点到点 + 点到线段"的双向最大距离。

    这样"同一条直线，一边 2 个点、一边 3 个点"也会判为 0 距离。
    """
    if not a or not b:
        return math.inf
    ra = resample_polyline(a, samples)
    rb = resample_polyline(b, samples)
    segs_b = _segments(b) or [(b[0], b[0])]
    segs_a = _segments(a) or [(a[0], a[0])]
    worst = 0.0
    for p in ra:
        best = min(point_to_segment_distance(p, s0, s1) for s0, s1 in segs_b)
        worst = max(worst, best)
    for p in rb:
        best = min(point_to_segment_distance(p, s0, s1) for s0, s1 in segs_a)
        worst = max(worst, best)
    # 兜底：重采样点之间的直接距离（曲线端点错开时更敏感）
    worst = max(worst, sample_distance(ra, rb) * 0.5)
    return worst


def geometry_matches(
    source: Cell,
    candidate: Cell,
    transform: Any,
    tolerance: float = 0.05,
    length_tolerance: float = 0.05,
) -> tuple[bool, float]:
    """把 source 变换后与 candidate 比对（同时看采样点与长度）。"""
    if source.kind != candidate.kind:
        return False, math.inf
    if source.length is not None and candidate.length is not None:
        if abs(source.length - candidate.length) > max(length_tolerance, tolerance):
            return False, math.inf
    moved = transform_samples(source.samples, transform)
    dist = sample_distance(moved, candidate.samples)
    return dist <= tolerance, dist
