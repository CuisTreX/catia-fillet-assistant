"""R5/R14 形状级对称：当前实体 BRep 边线的枚举与指纹（**只读**）。

实测依据（见 docs/R5可行性实测-20260925.md）：
    * `Selection.Add(MainBody)` + `Search("Topology.CGMEdge,sel")` 能枚举当前实体的全部边；
    * 枚举到的 Reference 既能 Measurable 量几何，也能直接当 Fillet 输入（无需再猜标签）；
    * 不选任何东西时的 `",all"` 会混进草图/线框单元（类型名带 MonoDim、标签含 Wire），必须过滤；
    * `GetPointsOnCurve` 会偶发只返回半条边，因此必须做"折线长 vs Length"校验，
      不完整时用 GetCOG / GetDirection / GetRadius / GetCenter / GetAxis 兜底（这几个是可靠的）。

本模块只查不写：不创建特征、不改模型、不动用户文档。
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .cellgeom import _flat_points, polyline_length
from .cellgeom import resample_polyline
from .geometry import V3

try:  # 纯数学单测/没装 comtypes 的机器上也要能 import 本模块
    from comtypes.automation import BSTR
except Exception:  # pragma: no cover
    BSTR = None

if TYPE_CHECKING:  # 只用于类型标注，避免运行时强依赖 CATIA 封装
    from .catia import CatiaSession

log = logging.getLogger(__name__)

# 候选搜索语法（旧版本 CATIA 若第一条不可用，会在配置里追加/替换）
DEFAULT_EDGE_QUERIES = ("Topology.CGMEdge,sel", "Topology.CGMEdge,all")

# Search 出来的显示名带这个前缀，剥掉才是真正的 BRep 标签
SELECTION_PREFIX = "Selection_"

# 草图线框等的类型/标签特征（不是实体的三维边，必须排除）
NON_TRIDIM_MARKERS = ("MonoDim", "BiDim", "Wire")


@dataclass
class ScannedEdge:
    """当前实体里的一条边（几何指纹）。"""

    index: int
    reference: Any
    display_name: str
    label: str
    type_name: str
    length: Optional[float] = None
    cog: Optional[V3] = None
    direction: Optional[V3] = None
    radius: Optional[float] = None
    center: Optional[V3] = None
    axis: Optional[V3] = None
    angle: Optional[float] = None
    samples: List[V3] = field(default_factory=list)
    sample_complete: bool = False
    samples_synth: bool = False
    is_line: bool = False
    is_circular: bool = False
    _resampled_cache: dict = field(default_factory=dict, repr=False)

    def resampled(self, count: int = 12) -> List[V3]:
        """按弧长重采样（缓存）：匹配时反复用，避免每次重算。"""
        cached = self._resampled_cache.get(count)
        if cached is None:
            cached = resample_polyline(list(self.samples), count)
            self._resampled_cache[count] = cached
        return cached

    # ------------------------------------------------------------ 便捷属性
    def midpoint(self) -> Optional[V3]:
        if self.cog is not None:
            return self.cog
        if self.samples:
            total = V3(0.0, 0.0, 0.0)
            for p in self.samples:
                total = total + p
            return total * (1.0 / len(self.samples))
        return None

    def endpoints(self) -> Optional[Tuple[V3, V3]]:
        if len(self.samples) >= 2:
            return (self.samples[0], self.samples[-1])
        return None

    def describe(self) -> str:
        mid = self.midpoint()
        where = ""
        if mid is not None:
            where = f"中点({mid.x:.1f}, {mid.y:.1f}, {mid.z:.1f})"
        length = f"长{self.length:.2f}" if self.length is not None else "长?"
        kind = "圆弧" if self.is_circular else ("直线" if self.is_line else "曲线")
        return f"{kind} {length} {where}".strip()

    def short_label(self) -> str:
        """诊断日志用：只留标签骨架，避免刷屏。"""
        text = self.label or self.display_name
        return text[:110]


def _to_v3(values: Any) -> Optional[V3]:
    if isinstance(values, (tuple, list)) and len(values) >= 3:
        try:
            return V3(float(values[0]), float(values[1]), float(values[2]))
        except Exception:
            return None
    return None


def _normalize_display(name: str) -> str:
    text = (name or "").strip()
    if text.startswith(SELECTION_PREFIX):
        text = text[len(SELECTION_PREFIX) :]
    return text


def is_tridim_edge(type_name: str, label: str) -> bool:
    """区分"实体三维边"和"草图/线框单元"。"""
    text = f"{type_name} {label}"
    return not any(marker in text for marker in NON_TRIDIM_MARKERS)


def _point_segment(p: V3, a: V3, b: V3) -> float:
    ab = b - a
    denom = ab.dot(ab)
    if denom <= 1e-18:
        return (p - a).norm()
    t = (p - a).dot(ab) / denom
    t = 0.0 if t < 0.0 else (1.0 if t > 1.0 else t)
    return (p - (a + ab * t)).norm()


def repair_line_samples(
    samples: Sequence[V3],
    direction: V3,
    length: float,
    tolerance: float = 0.05,
) -> Optional[List[V3]]:
    """采样残缺时，用「已知端点 + 方向 + 长度」把直线补全。

    实测：GetPointsOnCurve 对个别边只回 2 个点（只覆盖半条边），
    而边的 GetCOG/GetCenter 在 V5R20 上是"尚未实现" —— 只有 GetDirection 可靠。
    做法：以已知点为锚，沿 ±方向各取一条长度=Length 的线段，取"能包含全部已知点"的那条。
    """
    if not samples or length <= 0:
        return None
    try:
        unit = direction.unit()
    except Exception:
        return None
    tolerance = max(tolerance, 0.05)
    for anchor in (samples[0], samples[-1]):
        for start, end in (
            (anchor, anchor + unit * length),
            (anchor - unit * length, anchor),
        ):
            if all(_point_segment(p, start, end) <= tolerance for p in samples):
                return [start, (start + end) * 0.5, end]
    return None


class EdgeScanner:
    """当前实体边线扫描器（带 TTL 缓存；模型一变就失效）。"""

    def __init__(
        self,
        session: CatiaSession,
        ttl: float = 6.0,
        max_edges: int = 600,
        sample_points: int = 12,
        restore_selection: bool = True,
    ) -> None:
        self.session = session
        self.ttl = ttl
        self.max_edges = max_edges
        self.sample_points = sample_points
        # 扫描会把整个边集放进选择集才能读；扫完必须把用户原来的选择集还回去，
        # 否则模型里会一直"整圈边线高亮"、CATIA 重绘变慢（v0.5.0 用户实测问题）。
        self.restore_selection = restore_selection
        self.queries: Tuple[str, ...] = DEFAULT_EDGE_QUERIES
        self._edges: List[ScannedEdge] = []
        self._stamp = 0.0
        self._part_key: Optional[str] = None
        self._notes: List[str] = []
        # 边线的 GetCOG/GetCenter 在部分版本上是"尚未实现"：试一次失败后就不再试
        self._supports: Dict[str, bool] = {"GetCOG": True, "GetCenter": True}

    # ------------------------------------------------------------------ 缓存
    def invalidate(self) -> None:
        self._edges = []
        self._stamp = 0.0
        self._part_key = None
        self._notes = []

    def _part_key_of(self, part: Any) -> str:
        try:
            doc = self.session.active_document()
            name = str(getattr(doc, "Name", "")) if doc is not None else ""
            return f"{name}|{id(part)}"
        except Exception:
            return f"|{id(part)}"

    @property
    def notes(self) -> List[str]:
        return list(self._notes)

    # ------------------------------------------------------------------ 扫描
    def scan(self, part: Any, force: bool = False) -> List[ScannedEdge]:
        key = self._part_key_of(part)
        fresh = (time.time() - self._stamp) < self.ttl
        if not force and fresh and self._edges and self._part_key == key:
            return list(self._edges)
        edges, notes = self._scan_once(part)
        self._edges = edges
        self._notes = notes
        self._stamp = time.time()
        self._part_key = key
        log.info("形状引擎：扫描到 %d 条三维边（%s）", len(edges), "；".join(notes) if notes else "正常")
        return list(edges)

    def _scan_once(self, part: Any) -> Tuple[List[ScannedEdge], List[str]]:
        notes: List[str] = []
        selection = None
        try:
            selection = self.session.selection()
        except Exception as exc:
            notes.append(f"取选择集失败: {exc}")
            return [], notes
        if selection is None:
            notes.append("当前没有可用选择集")
            return [], notes
        saved = self._snapshot_selection(selection) if self.restore_selection else []
        try:
            return self._scan_with_selection(selection, part, notes)
        finally:
            if self.restore_selection:
                self._restore_selection(selection, saved)

    # --------------------------------------------------- 选择集快照 / 还原
    def _snapshot_selection(self, selection: Any) -> List[Any]:
        """记下用户当前选择集里的对象（Reference），扫描完原样还回去。"""
        saved: List[Any] = []
        try:
            count = int(selection.Count2)
        except Exception:
            return saved
        for index in range(1, count + 1):
            try:
                element = selection.Item2(index)
            except Exception:
                continue
            ref = None
            for attr in ("Reference", "Value"):
                try:
                    ref = getattr(element, attr)
                except Exception:
                    ref = None
                if ref is not None:
                    break
            if ref is not None:
                saved.append(ref)
        return saved

    def _restore_selection(self, selection: Any, saved: List[Any]) -> None:
        try:
            selection.Clear()
        except Exception:
            pass
        for ref in saved:
            try:
                selection.Add(ref)
            except Exception as exc:
                log.debug("还原选择集失败（忽略）: %s", exc)

    def _scan_with_selection(self, selection: Any, part: Any, notes: List[str]) -> Tuple[List[ScannedEdge], List[str]]:

        for query in self.queries:
            try:
                selection.Clear()
            except Exception:
                pass
            try:
                selection.Add(self._body_of(part))
            except Exception as exc:
                notes.append(f"选择 MainBody 失败: {exc}")
            try:
                selection.Search(BSTR(query) if BSTR is not None else query)
            except Exception as exc:
                notes.append(f"搜索「{query}」失败: {exc}")
                continue
            try:
                count = int(selection.Count2)
            except Exception as exc:
                notes.append(f"搜索「{query}」后读数量失败: {exc}")
                continue
            edges: List[ScannedEdge] = []
            seen: set = set()
            for index in range(1, min(count, self.max_edges) + 1):
                edge = self._read_item(selection, index, len(edges) + 1, notes)
                if edge is None:
                    continue
                if edge.label in seen:
                    continue
                seen.add(edge.label)
                edges.append(edge)
            if edges:
                notes.append(f"「{query}」命中 {count} 个单元，其中三维边 {len(edges)} 条")
                return edges, notes
            notes.append(f"「{query}」命中 {count} 个单元，但都不是三维边")
        return [], notes

    @staticmethod
    def _body_of(part: Any) -> Any:
        try:
            return getattr(part, "MainBody")
        except Exception:
            return part

    def _read_item(self, selection: Any, index: int, fallback_index: int, notes: List[str]) -> Optional[ScannedEdge]:
        try:
            element = selection.Item2(index)
        except Exception as exc:
            notes.append(f"第 {index} 个单元读取失败: {exc}")
            return None
        type_name = ""
        try:
            type_name = str(element.Type)
        except Exception:
            type_name = "?"
        reference = None
        try:
            reference = element.Reference
        except Exception:
            reference = None
        if reference is None:
            return None
        display = ""
        try:
            display = str(reference.DisplayName)
        except Exception:
            display = ""
        label = _normalize_display(display)
        if not is_tridim_edge(type_name, label):
            return None
        edge = ScannedEdge(
            index=fallback_index,
            reference=reference,
            display_name=display,
            label=label,
            type_name=type_name,
        )
        self._fill_fingerprint(edge)
        if edge.length is None:
            return None
        return edge

    # -------------------------------------------------------------- 指纹读取
    def _fill_fingerprint(self, edge: ScannedEdge) -> None:
        wb = self.session.workbench()
        if wb is None:
            return
        try:
            measurable = wb.GetMeasurable(edge.reference)
        except Exception as exc:
            log.debug("GetMeasurable 失败(%s): %s", edge.index, exc)
            return
        edge.length = self._read_float(measurable, "Length")
        edge.radius = self._read_float(measurable, "Radius")
        edge.direction = self._read_vector(measurable, "GetDirection")
        edge.samples = self._read_samples(measurable)
        # 圆弧才需要角度/轴向；直线不必多打两次 COM
        if edge.radius is not None:
            edge.angle = self._read_float(measurable, "Angle")
            edge.axis = self._read_vector(measurable, "GetAxis")
        # 边线的 GetCOG / GetCenter 在 V5R20 上是"尚未实现"：试一次失败后整轮不再试
        if self._supports.get("GetCOG"):
            edge.cog = self._read_point(measurable, "GetCOG")
            if edge.cog is None:
                self._supports["GetCOG"] = False
        if self._supports.get("GetCenter") and edge.radius is not None:
            edge.center = self._read_point(measurable, "GetCenter")
            if edge.center is None:
                self._supports["GetCenter"] = False

        poly = polyline_length(edge.samples) if edge.samples else 0.0
        tolerance = max(0.02, (edge.length or 0.0) * 0.01)
        edge.sample_complete = bool(
            edge.samples and edge.length is not None and abs(poly - edge.length) <= tolerance
        )
        # 直线：采样残缺时用「端点 + 方向 + 长度」补全（GetCOG 对边线不可用，只能靠方向）
        if not edge.sample_complete and edge.direction is not None and edge.length:
            repaired = repair_line_samples(edge.samples, edge.direction, edge.length)
            if repaired is not None:
                edge.samples = repaired
                edge.sample_complete = True
                edge.samples_synth = True
                log.debug("边 #%d 采样残缺（折线长 %.3f / 实际 %.3f），已用方向+长度补全",
                          edge.index, poly, edge.length)
        edge.is_circular = bool(edge.radius is not None or (edge.angle is not None and edge.axis is not None))
        edge.is_line = bool(edge.direction is not None and not edge.is_circular)

    @staticmethod
    def _read_float(measurable: Any, attr: str) -> Optional[float]:
        try:
            value = getattr(measurable, attr)
        except Exception:
            return None
        if isinstance(value, (int, float)):
            return float(value)
        return None

    def _read_point(self, measurable: Any, method: str) -> Optional[V3]:
        fn = getattr(measurable, method, None)
        if fn is None:
            return None
        for size in (3, 6):
            try:
                return _to_v3(fn(self.session._buf(size)))
            except Exception:
                continue
        return None

    def _read_vector(self, measurable: Any, method: str) -> Optional[V3]:
        value = self._read_point(measurable, method)
        if value is None:
            return None
        if value.norm() <= 1e-9:
            return None
        return value

    def _read_samples(self, measurable: Any) -> List[V3]:
        layout = None
        try:
            for spec in self.session.mod("spa").Measurable._methods_:
                if spec.name == "GetPointsOnCurve":
                    layout = spec.argtypes[0]
                    break
        except Exception:
            layout = None
        if layout is None:
            return []
        try:
            buffer = layout.create([0.0] * (3 * self.sample_points))
            return _flat_points(measurable.GetPointsOnCurve(buffer), self.sample_points)
        except Exception:
            return []


def measure_reference(session: CatiaSession, reference: Any, sample_points: int = 12) -> Optional[ScannedEdge]:
    """把一个 Reference（例如用户直接选中的边）量成指纹，供 Shape Engine 当"源边"用。"""
    scanner = EdgeScanner(session, ttl=0.0, sample_points=sample_points)
    edge = ScannedEdge(
        index=0,
        reference=reference,
        display_name="",
        label="",
        type_name="",
    )
    try:
        edge.display_name = str(reference.DisplayName)
    except Exception:
        pass
    edge.label = _normalize_display(edge.display_name)
    scanner._fill_fingerprint(edge)
    if edge.length is None:
        return None
    return edge


def scan_tip_edges(
    session: CatiaSession,
    part: Any,
    queries: Optional[Sequence[str]] = None,
    ttl: float = 0.0,
    sample_points: int = 12,
) -> List[ScannedEdge]:
    """一次性扫描（不缓存）。"""
    scanner = EdgeScanner(session, ttl=ttl, sample_points=sample_points)
    if queries:
        scanner.queries = tuple(queries)
    return scanner.scan(part, force=True)
