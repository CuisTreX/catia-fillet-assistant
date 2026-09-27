"""按特征参数推出候选边线的几何与标签（解决"命令内点选读不到"的问题）。

思路：
    1. 用户在倒圆角对话框里点选的边线，虽然不在 Document.Selection 里，
       但能从对话框列表读到名字（如 凸台.1\\目标边线.1），并解析成可量测的单元 → **有几何采样**；
    2. 按"生成它的特征"（这里是凸台/Pad）读出草图与拉伸参数，**算出这个特征所有边的几何与标签**；
    3. 用几何把"点选的边"对应到候选（得到它是第几条草图元素生成的什么边）；
    4. 对候选做镜像/旋转，再和候选集合比对 → 找到对称那条边的**标签**；
    5. 标签交给 CreateReferenceFromBRepName → 得到可加入倒圆角对话框的对象。

实测支撑（V5R20 中文版）：
    * Pad → `FirstLimit.Dimension.Value` 能读拉伸长度，`Sketch` 能拿到草图；
    * 竖直边标签 `REdge:(Edge:(Face:(Brp:(F;0:(Brp:(S;i))))…Face:(Brp:(F;0:(Brp:(S;j))))…))` 解析成功；
    * 顶/底边标签（侧面 + 端面 1/2）解析成功；
    * 手动点选的单元可用 FindObjectByName 得到并量到采样点。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Iterable

from .cellgeom import curve_distance, sample_distance
from .geometry import SketchElement, SketchFrame, Transform, V3

log = logging.getLogger(__name__)

# 内部名候选（BRep 标签里用的名字，随语言版本不同，靠"试解析"确定）
FEATURE_NAME_PATTERNS = (
    "Pad",
    "Pocket",
    "Prism",
    "Revolve",
    "Shaft",
    "凸台",
    "凹槽",
    "棱柱",
    "旋转体",
)
SKETCH_NAME_PATTERNS = ("Sketch", "草图")

# 界面里的本地化特征名 → BRep 标签里用的内部名
# （不靠猜：先按这张表排前面，再用"源边几何能不能对上"来验证）
LOCAL_FEATURE_NAMES = {
    "凸台": "Pad",
    "凹槽": "Pocket",
    "棱柱": "Prism",
    "旋转体": "Revolve",
    "旋转": "Revolve",
    "开槽": "Groove",
    "孔": "Hole",
    "槽": "Slot",
    "加强肋": "Rib",
    "倒圆角": "EdgeFillet",
    "倒角": "Chamfer",
    "拔模": "Draft",
    "变半径倒圆角": "VarRadEdgeFillet",
}


@dataclass
class EdgeCandidate:
    label: str
    samples: list[V3]
    length: float
    kind: str  # vertical | bottom | top
    element_index: int
    partner_index: int | None = None

    def describe(self) -> str:
        if self.kind == "vertical":
            return f"竖直边(元素 {self.element_index}-{self.partner_index})"
        return f"{'底边' if self.kind == 'bottom' else '顶边'}(元素 {self.element_index})"


def feature_attr(session: Any, feature: Any, name: str, default: Any = None) -> Any:
    """取特征属性：typed 接口上没有的（如 Sketch/FirstLimit），走动态派发再试一次。"""
    try:
        return getattr(feature, name)
    except Exception:
        pass
    try:
        return getattr(session.dyn(feature), name)
    except Exception:
        return default


def _polyline_length(points: list[V3]) -> float:
    total = 0.0
    for a, b in zip(points, points[1:]):
        total += (b - a).norm()
    return total


def _label_vertical(feature: str, sketch: str, i: int, j: int) -> str:
    return (
        f"REdge:(Edge:(Face:(Brp:({feature};0:(Brp:({sketch};{i})));None:());"
        f"Face:(Brp:({feature};0:(Brp:({sketch};{j})));None:());None:(Limits1:();Limits2:()));"
        "WithTemporaryBody;WithoutBuildError;WithSelectingFeatureSupport)"
    )


def _label_end(feature: str, sketch: str, i: int, end_face: int) -> str:
    return (
        f"REdge:(Edge:(Face:(Brp:({feature};0:(Brp:({sketch};{i})));None:());"
        f"Face:(Brp:({feature};{end_face});None:());None:(Limits1:();Limits2:()));"
        "WithTemporaryBody;WithoutBuildError;WithSelectingFeatureSupport)"
    )


def _index_from_name(name: str) -> int | None:
    text = (name or "").strip()
    if "." not in text:
        return None
    tail = text.rsplit(".", 1)[1]
    return int(tail) if tail.isdigit() else None


def discover_names(session: Any, part: Any, feature: Any, feature_obj_local_name: str = "") -> tuple[str, str] | None:
    """确定 BRep 标签里该用的「特征内部名 + 草图内部名」。

    `feature_obj_local_name` 是界面上的本地化名字（如 凸台.1），用它取序号。
    """
    localized = feature_obj_local_name
    if not localized:
        try:
            localized = str(feature_attr(session, feature, "Name", ""))
        except Exception:
            localized = ""
    feature_index = _index_from_name(localized) or 1
    sketch = None
    sketch_index = 1
    try:
        sketch = feature_attr(session, feature, "Sketch")
        sketch_index = _index_from_name(str(feature_attr(session, sketch, "Name", ""))) or 1
    except Exception:
        sketch = None
    for fpat in FEATURE_NAME_PATTERNS:
        feature_name = f"{fpat}.{feature_index}"
        for spat in SKETCH_NAME_PATTERNS:
            sketch_name = f"{spat}.{sketch_index}"
            probe = _label_end(feature_name, sketch_name, 1, 1)
            if session.reference_from_label(part, probe, feature) is not None:
                log.info("特征内部名确定为 %s / %s", feature_name, sketch_name)
                return feature_name, sketch_name
    return None


def name_candidates(
    session: Any,
    part: Any,
    feature: Any,
    feature_obj_local_name: str = "",
) -> list[tuple[str, str]]:
    """列出可能的（特征内部名, 草图内部名）组合，**按可能性排序**。

    例：本地化名"凹槽.2" → 优先 ("Pocket.2", "Sketch.1")，再退化成其它组合。
    调用方应当逐个试，并用"源边几何能否匹配候选"来验证哪个才是真的。
    """
    localized = feature_obj_local_name or str(feature_attr(session, feature, "Name", ""))
    index = _index_from_name(localized) or 1
    sketch = feature_attr(session, feature, "Sketch")
    sketch_index = _index_from_name(str(feature_attr(session, sketch, "Name", ""))) or 1

    ordered: list[str] = []
    prefix = localized.split(".", 1)[0].strip()
    mapped = LOCAL_FEATURE_NAMES.get(prefix)
    if mapped:
        ordered.append(mapped)
    if prefix and prefix not in ordered:
        ordered.append(prefix)  # 万一内部名就是中文
    for pattern in FEATURE_NAME_PATTERNS:
        if pattern not in ordered:
            ordered.append(pattern)

    out: list[tuple[str, str]] = []
    for fpat in ordered:
        for spat in SKETCH_NAME_PATTERNS:
            pair = (f"{fpat}.{index}", f"{spat}.{sketch_index}")
            if pair not in out:
                out.append(pair)
    return out


def read_limits(session: Any, feature: Any) -> tuple[float, float, bool]:
    """返回 (第一限制, 第二限制, 是否对称)。"""
    first = second = 0.0
    symmetric = False

    def attr(name: str, default: Any = None) -> Any:
        return feature_attr(session, feature, name, default)

    try:
        raw = attr("FirstLimit")
        dim = getattr(raw, "Dimension", None)
        value = getattr(dim, "Value", None)
        if isinstance(value, (int, float)):
            first = float(value)
    except Exception:
        pass
    try:
        raw = attr("SecondLimit")
        dim = getattr(raw, "Dimension", None)
        value = getattr(dim, "Value", None)
        if isinstance(value, (int, float)):
            second = float(value)
    except Exception:
        pass
    try:
        symmetric = bool(attr("IsSymmetric", False))
    except Exception:
        symmetric = False
    return first, second, symmetric


def _z_ranges(first: float, second: float, symmetric: bool) -> list[tuple[float, float]]:
    ranges: list[tuple[float, float]] = []
    if symmetric and first:
        ranges.append((-first / 2.0, first / 2.0))
    if second > 0:
        ranges.append((-second, first))
    if first:
        ranges.append((0.0, first))
        ranges.append((-first, 0.0))
    return ranges


def prismatic_candidates(
    session: Any,
    part: Any,
    feature: Any,
    feature_name: str,
    sketch_name: str,
    frame: SketchFrame,
    elements: list[SketchElement],
    z_ranges: Iterable[tuple[float, float]],
    endpoint_tol: float = 0.05,
    max_vertical: int = 400,
    validate: bool = False,
) -> list[EdgeCandidate]:
    """算出该特征所有可构造的边（竖直边 + 顶/底边）的几何与标签。

    validate=False 时不逐条解析标签（省掉几十次 COM 调用，快很多）；
    最终要用的那条边在调用方解析一次校验即可。
    """
    out: list[EdgeCandidate] = []
    ranges = list(z_ranges)
    # 顶/底边：每个草图元素在两个端面各一条
    for z0, z1 in ranges:
        for end_face, z in ((1, z0), (2, z1)):
            for el in elements:
                if not el.points:
                    continue
                samples = [frame.to3d((u, v)) + frame.w * z for u, v in el.points]
                label = _label_end(feature_name, sketch_name, el.index, end_face)
                if validate and session.reference_from_label(part, label, feature) is None:
                    continue
                out.append(
                    EdgeCandidate(
                        label=label,
                        samples=samples,
                        length=_polyline_length(samples),
                        kind="bottom" if end_face == 1 else "top",
                        element_index=el.index,
                    )
                )
    # 竖直边：草图元素端点两两重合处
    endpoints: list[tuple[int, tuple[float, float]]] = []
    for el in elements:
        if len(el.points) >= 2:
            endpoints.append((el.index, el.points[0]))
            endpoints.append((el.index, el.points[-1]))
    made = 0
    for idx_a in range(len(endpoints)):
        for idx_b in range(idx_a + 1, len(endpoints)):
            i, (u1, v1) = endpoints[idx_a]
            j, (u2, v2) = endpoints[idx_b]
            if i == j:
                continue
            if abs(u1 - u2) > endpoint_tol or abs(v1 - v2) > endpoint_tol:
                continue
            for z0, z1 in ranges:
                base = frame.to3d((u1, v1))
                samples = [base + frame.w * z0, base + frame.w * z1]
                label = _label_vertical(feature_name, sketch_name, i, j)
                if validate and session.reference_from_label(part, label, feature) is None:
                    continue
                out.append(
                    EdgeCandidate(
                        label=label,
                        samples=samples,
                        length=_polyline_length(samples),
                        kind="vertical",
                        element_index=i,
                        partner_index=j,
                    )
                )
                made += 1
                if made >= max_vertical:
                    return out
    return out


def best_match(
    samples: list[V3],
    candidates: list[EdgeCandidate],
    tolerance: float = 0.05,
    partial: bool = False,
) -> tuple[EdgeCandidate | None, float]:
    """找最匹配的候选边。

    partial=True：允许被测边只是候选边的**一部分**（圆角把相邻边削短后仍能认出来），
    判据是"被测边上的点都落在候选线上"。
    """
    if not samples:
        return None, float("inf")
    length = _polyline_length(samples)
    best: EdgeCandidate | None = None
    best_dist = float("inf")
    for cand in candidates:
        if partial:
            # 被测边不能比候选边更长（允许一点误差）
            if length > cand.length + max(0.5, tolerance * 10):
                continue
            # 单方向：被测边的点是否都贴在候选线上
            from .cellgeom import resample_polyline, point_to_segment_distance

            segs = list(zip(cand.samples, cand.samples[1:])) or [(cand.samples[0], cand.samples[0])]
            worst = 0.0
            for point in resample_polyline(samples, 12):
                worst = max(worst, min(point_to_segment_distance(point, a, b) for a, b in segs))
            dist = worst
        else:
            if abs(cand.length - length) > max(0.5, tolerance * 10):
                continue
            dist = curve_distance(samples, cand.samples)
        if dist < best_dist:
            best, best_dist = cand, dist
    if best is None or best_dist > tolerance:
        return None, best_dist
    return best, best_dist


@dataclass
class Pairing:
    transform: Transform
    source: EdgeCandidate
    target: EdgeCandidate
    distance: float

    def describe(self) -> str:
        return f"{self.transform.label}: {self.source.describe()} → {self.target.describe()}"
