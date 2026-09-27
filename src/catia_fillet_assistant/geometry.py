"""点、三维变换、草图元素匹配（纯数学，不依赖 CATIA）。"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable, Sequence


@dataclass(frozen=True)
class V3:
    x: float
    y: float
    z: float

    def __add__(self, o: "V3") -> "V3":
        return V3(self.x + o.x, self.y + o.y, self.z + o.z)

    def __sub__(self, o: "V3") -> "V3":
        return V3(self.x - o.x, self.y - o.y, self.z - o.z)

    def __mul__(self, k: float) -> "V3":
        return V3(self.x * k, self.y * k, self.z * k)

    __rmul__ = __mul__

    def dot(self, o: "V3") -> float:
        return self.x * o.x + self.y * o.y + self.z * o.z

    def cross(self, o: "V3") -> "V3":
        return V3(
            self.y * o.z - self.z * o.y,
            self.z * o.x - self.x * o.z,
            self.x * o.y - self.y * o.x,
        )

    def norm(self) -> float:
        return math.sqrt(self.dot(self))

    def unit(self) -> "V3":
        n = self.norm()
        if n <= 1e-12:
            raise ValueError("零向量无法归一化")
        return self * (1.0 / n)

    def as_tuple(self) -> tuple[float, float, float]:
        return (self.x, self.y, self.z)


def mirror_point(p: V3, plane_point: V3, plane_normal: V3) -> V3:
    n = plane_normal.unit()
    d = (p - plane_point).dot(n)
    return p - n * (2.0 * d)


def rotate_point(p: V3, axis_point: V3, axis_dir: V3, angle_deg: float) -> V3:
    a = axis_dir.unit()
    v = p - axis_point
    c, s = math.cos(math.radians(angle_deg)), math.sin(math.radians(angle_deg))
    return axis_point + v * c + a.cross(v) * s + a * (a.dot(v) * (1.0 - c))


@dataclass(frozen=True)
class Transform:
    """对称变换：绕某点做镜像或绕某轴旋转。"""

    kind: str  # "mirror" | "rotate"
    label: str
    point: V3
    normal: V3 | None = None
    axis: V3 | None = None
    angle: float = 0.0

    def apply(self, p: V3) -> V3:
        if self.kind == "mirror":
            assert self.normal is not None
            return mirror_point(p, self.point, self.normal)
        if self.kind == "rotate":
            assert self.axis is not None
            return rotate_point(p, self.point, self.axis, self.angle)
        raise ValueError(f"未知变换: {self.kind}")

    def apply_many(self, pts: Iterable[V3]) -> list[V3]:
        return [self.apply(p) for p in pts]

    @staticmethod
    def mirror(label: str, point: V3, normal: V3) -> "Transform":
        return Transform(kind="mirror", label=label, point=point, normal=normal)

    @staticmethod
    def rotate(label: str, point: V3, axis: V3, angle: float) -> "Transform":
        return Transform(kind="rotate", label=label, point=point, axis=axis, angle=angle)


@dataclass
class SketchFrame:
    """草图的绝对坐标系：原点 + 两个平面内方向 + 法向。"""

    origin: V3
    u: V3
    v: V3
    w: V3

    def to3d(self, uv: Sequence[float]) -> V3:
        return self.origin + self.u * uv[0] + self.v * uv[1]

    def to2d(self, p: V3) -> tuple[float, float, float]:
        d = p - self.origin
        return (d.dot(self.u), d.dot(self.v), d.dot(self.w))


@dataclass
class SketchElement:
    """草图元素（只保留做对称匹配需要的几何信息）。"""

    index: int
    kind: str  # "line" | "circle" | "other"
    points: list[tuple[float, float]]  # line: 起终点
    center: tuple[float, float] | None = None
    radius: float | None = None

    def key_points_2d(self) -> list[tuple[float, float]]:
        if self.kind == "line":
            return list(self.points)
        if self.kind == "circle" and self.center is not None:
            return [self.center]
        return list(self.points)


def _approx_eq(a: float, b: float, tol: float) -> bool:
    return abs(a - b) <= tol


def _same_uv(a: tuple[float, float], b: tuple[float, float], tol: float) -> bool:
    return _approx_eq(a[0], b[0], tol) and _approx_eq(a[1], b[1], tol)


def element_in_3d(el: SketchElement, frame: SketchFrame) -> list[V3]:
    return [frame.to3d(p) for p in el.key_points_2d()]


def project_points(pts3d: Iterable[V3], frame: SketchFrame) -> list[tuple[float, float, float]]:
    return [frame.to2d(p) for p in pts3d]


def match_element(
    transformed: list[tuple[float, float, float]],
    candidates: list[SketchElement],
    tol: float,
) -> int | None:
    """在候选草图元素里找与变换后点集重合的那个，返回其序号。"""
    if any(abs(w) > tol for _u, _v, w in transformed):
        return None  # 变换把元素打出了草图平面
    uv = [(u, v) for u, v, _w in transformed]
    for cand in candidates:
        pts = cand.key_points_2d()
        if len(pts) != len(uv):
            continue
        if len(uv) == 1:
            if _same_uv(pts[0], uv[0], tol):
                return cand.index
        elif len(uv) == 2:
            if (_same_uv(pts[0], uv[0], tol) and _same_uv(pts[1], uv[1], tol)) or (
                _same_uv(pts[0], uv[1], tol) and _same_uv(pts[1], uv[0], tol)
            ):
                return cand.index
        else:
            if all(any(_same_uv(p, q, tol) for q in uv) for p in pts):
                return cand.index
    return None


def build_index_mapping(
    elements: list[SketchElement],
    frame: SketchFrame,
    transform: Transform,
    tol: float = 1e-3,
) -> dict[int, int | None]:
    """算出「草图元素序号 → 变换后的草图元素序号」的映射表。

    * 只在元素变换后仍落在同一草图平面内、且类型/尺寸一致时才给映射；
    * 找不到对应元素时值为 None，调用方可据此放弃该候选。
    """
    mapping: dict[int, int | None] = {}
    for el in elements:
        if el.kind == "other":
            mapping[el.index] = None
            continue
        pts3d = element_in_3d(el, frame)
        moved = [transform.apply(p) for p in pts3d]
        proj = project_points(moved, frame)
        target = match_element(proj, elements, tol)
        if target is not None:
            cand = next((c for c in elements if c.index == target), None)
            if cand is not None and cand.kind != el.kind:
                target = None
        mapping[el.index] = target
    return mapping
