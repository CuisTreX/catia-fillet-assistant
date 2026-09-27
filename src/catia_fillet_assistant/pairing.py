"""对称边线配对引擎。

思路：
    1. 从选中的边线标签里读出「由哪个特征、哪几条草图元素生成」；
    2. 用「零件对称变换」（镜像 / 旋转）把草图元素序号映射到对称位置；
    3. 把映射后的序号代回标签，得到对称边线的标签；
    4. 交给 Part.CreateReferenceFromBRepName 生成真正可选的边线对象。

第 2 步的映射是拿草图几何算出来的：把元素采样点变换后投影回草图平面，
找重合的那个元素。所以镜像面/旋转轴只要穿过零件重心即可，不用手工建基准。
"""

from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass
from dataclasses import field

from . import labels as lbl
from .catia import CatiaSession, SelectionItem
from .cellgeom import Cell, CellIndex
from .config import Config
from . import featuregeom
from .geometry import Transform, V3, build_index_mapping

log = logging.getLogger(__name__)

_AXES = {
    "x": V3(1.0, 0.0, 0.0),
    "y": V3(0.0, 1.0, 0.0),
    "z": V3(0.0, 0.0, 1.0),
}


@dataclass
class Candidate:
    label: str
    reference: object
    transform: Transform
    source_index: int
    matched: int
    total: int

    @property
    def ratio(self) -> float:
        return self.matched / self.total if self.total else 0.0


def axis_vector(name: str) -> V3:
    return _AXES.get((name or "x").lower(), _AXES["x"])


def _sketch_center(session: CatiaSession, part, info: lbl.LabelInfo) -> V3 | None:
    """重心取不到时的兜底：用草图元素包围盒中心当对称中心。"""
    xs: list[float] = []
    ys: list[float] = []
    zs: list[float] = []
    for sketch_name in info.sketch_names:
        sketch = session.object_by_name(part, sketch_name)
        if sketch is None:
            continue
        frame = session.read_sketch_frame(sketch)
        if frame is None:
            continue
        for el in session.read_sketch_elements(sketch):
            for u, v in el.points:
                p = frame.to3d((u, v))
                xs.append(p.x)
                ys.append(p.y)
                zs.append(p.z)
    if not xs:
        return None
    return V3((min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2, (min(zs) + max(zs)) / 2)


def _flips_extrusion(tf: Transform, frame) -> bool:
    """这个对称变换是否把"拉伸方向"翻了个（决定顶边↔底边要不要互换）。"""
    try:
        moved = tf.apply(frame.origin + frame.w) - tf.apply(frame.origin)
        if moved.norm() <= 1e-12:
            return False
        return moved.unit().dot(frame.w) < -0.5
    except Exception:
        return False


def _swap_end_faces(label: str, features: set[str]) -> str:
    """把标签里的端面 1 ↔ 2 互换（顶边镜像成底边时用）。"""
    out = label
    for feature in features:
        for a, b in (("1", "2"), ("2", "1")):
            out = re.sub(rf"Brp:\(\s*{re.escape(feature)}\s*;\s*{a}\s*\)", f"Brp:({feature};{b})", out)
    return out


def _edge_direction(samples: list[V3]) -> V3 | None:
    for a, b in zip(samples, samples[1:]):
        d = b - a
        if d.norm() > 1e-9:
            return d.unit()
    return None


def loop_ranges(elements: list, tol: float = 0.5) -> list[list[int]]:
    """把草图线条按端点连成闭合环，返回每圈的编号区间（如 [[1,2,3,4],[5,6,7,8]]）。

    CATIA 的 BRep 编号是"按圈编号"的，每圈各有一个位移，必须分圈处理。
    """
    remaining = {
        el.index: (el.points[0], el.points[-1])
        for el in elements
        if el.kind == "line" and len(el.points) >= 2
    }
    loops: list[list[int]] = []
    while remaining:
        start = min(remaining)
        chain = [start]
        current = remaining[start][1]
        del remaining[start]
        while remaining:
            nxt = None
            for key, (a, b) in remaining.items():
                if abs(a[0] - current[0]) <= tol and abs(a[1] - current[1]) <= tol:
                    nxt, end = key, b
                    break
                if abs(b[0] - current[0]) <= tol and abs(b[1] - current[1]) <= tol:
                    nxt, end = key, a
                    break
            if nxt is None:
                break
            chain.append(nxt)
            current = end
            del remaining[nxt]
        loops.append(sorted(chain))
    return loops


def shift_within_loop(index: int, delta: int, loops: list[list[int]]) -> int:
    """把编号在**它所属的那一圈内**做循环位移（绝不跨圈）。"""
    for loop in loops:
        if index in loop:
            position = (loop.index(index) + delta) % len(loop)
            return loop[position]
    return index


def loop_of(index: int, loops: list[list[int]]) -> list[int] | None:
    for loop in loops:
        if index in loop:
            return loop
    return None


def _keeps_edge_direction(tf: Transform, direction: V3 | None) -> bool:
    """对称变换后，这条边的方向是否还保持原方向。

    * True：**在侧面镜像**（镜像面包含这条边）——就是用户要的"另一侧的对称边"；
    * False：镜像法向沿着这条边本身（会跑到这条边的"另一端"）或方向被改变——不是用户要的。
    """
    if direction is None:
        return True
    try:
        if tf.kind == "mirror" and tf.normal is not None:
            n = tf.normal.unit()
            moved = direction - n * (2.0 * direction.dot(n))
        else:
            moved = tf.apply(tf.point + direction) - tf.apply(tf.point)
    except Exception:
        return True
    if moved.norm() <= 1e-9:
        return False
    return moved.unit().dot(direction) > 0.9


def transforms_for(config: Config, center: V3) -> list[Transform]:
    """按配置生成候选对称变换（按优先级排序）。"""
    mode = (config.symmetry_mode or "auto").lower()
    out: list[Transform] = []
    if mode == "off":
        return out
    if mode in ("auto", "mirror"):
        for axis in ("x", "y", "z"):
            out.append(Transform.mirror(f"关于{axis.upper()}法向的中面镜像", center, axis_vector(axis)))
    # 默认只做**轴对称（镜像）**；需要中心对称/旋转对称时把 config.symmetry_types 设为 "mirror+rotate"
    allow_rotate = mode == "rotate" or str(getattr(config, "symmetry_types", "mirror")).lower().endswith("+rotate")
    if allow_rotate and mode in ("auto", "rotate"):
        angles = [config.rotate_angle] if mode == "rotate" else [180.0, 90.0, 120.0, 60.0, 45.0, 30.0]
        for axis in (("z", "y", "x") if mode == "rotate" else ("x", "y", "z")):
            for ang in angles:
                out.append(Transform.rotate(f"绕{axis.upper()}轴旋转{ang:g}°", center, axis_vector(axis), ang))
                if mode == "auto" and ang != 180.0:
                    out.append(Transform.rotate(f"绕{axis.upper()}轴旋转-{ang:g}°", center, axis_vector(axis), -ang))
    if mode == "mirror":
        out = [Transform.mirror(f"关于{config.mirror_axis.upper()}法向的中面镜像", center, axis_vector(config.mirror_axis))]
    if mode == "rotate":
        out = [
            Transform.rotate(
                f"绕{config.rotate_axis.upper()}轴旋转{config.rotate_angle:g}°",
                center,
                axis_vector(config.rotate_axis),
                config.rotate_angle,
            )
        ]
    return out


def sketch_local_transforms(
    sketch_frames: dict[str, Any],
    sketch_elements: dict[str, list[Any]],
) -> list[Transform]:
    """从草图自身推断对称：关于草图两条中线/两条坐标轴的镜像，以及绕草图法向 180°。

    很多零件的对称面并不是"过重心、法向是全局 X/Y/Z"的中面，而是**草图里的中线**。
    这里把草图的两条中心线、两条草图轴、以及草图中心点的 180° 旋转都作为候选。
    """
    out: list[Transform] = []
    for name, frame in sketch_frames.items():
        elements = sketch_elements.get(name) or []
        uvs = [p for el in elements for p in el.points]
        if not uvs:
            continue
        us = [u for u, _v in uvs]
        vs = [v for _u, v in uvs]
        center = frame.to3d(((min(us) + max(us)) / 2.0, (min(vs) + max(vs)) / 2.0))
        # 关于草图 U 向中线（法向 = 草图 V 方向）镜像
        out.append(Transform.mirror(f"{name} 关于草图水平中线镜像", center, frame.v))
        # 关于草图 V 向中线（法向 = 草图 U 方向）镜像
        out.append(Transform.mirror(f"{name} 关于草图竖直中线镜像", center, frame.u))
        # 关于草图自身两条坐标轴（过草图原点）镜像
        out.append(Transform.mirror(f"{name} 关于草图横轴镜像", frame.origin, frame.v))
        out.append(Transform.mirror(f"{name} 关于草图纵轴镜像", frame.origin, frame.u))
        # 绕草图法向 180°（中心对称图形）
        out.append(Transform.rotate(f"{name} 绕草图法向 180°", center, frame.w, 180.0))
    return out


def find_counterparts(
    session: CatiaSession,
    part,
    item: SelectionItem,
    config: Config,
    exclude_labels: set[str] | None = None,
) -> tuple[list[Candidate], str]:
    """为一条边线找对称边线。返回 (候选列表, 说明文字)。"""
    info = item.label
    if info is None or not info.is_edge:
        return [], "选中的不是边线"
    if not info.sketches:
        return [], "这条边线的标签里没有草图信息（可能是圆角/阵列生成的边），暂不支持"

    features = {f.feature for f in info.features}
    context = None
    for name in features:
        context = session.object_by_name(part, name)
        if context is not None:
            break
    if context is None:
        context = getattr(part, "MainBody", None)

    exclude = exclude_labels or set()
    found: list[Candidate] = []
    best_ratio = 0.0

    # 草图几何只读一次（每个变换都重读会慢很多）
    sketch_data: dict[str, tuple[Any, Any, list[Any]]] = {}
    for sketch_name in info.sketch_names:
        sketch = session.object_by_name(part, sketch_name)
        if sketch is None:
            continue
        frame = session.read_sketch_frame(sketch)
        if frame is None:
            continue
        elements = session.read_sketch_elements(sketch)
        if not elements:
            continue
        sketch_data[sketch_name] = (sketch, frame, elements)

    center = session.part_cog(part) or _sketch_center(session, part, info) or V3(0.0, 0.0, 0.0)
    transforms = transforms_for(config, center)
    if not transforms:
        return [], "对称功能已关闭"
    if (config.symmetry_mode or "auto").lower() == "auto":
    # 先试"草图自己的中线/草图轴"——很多零件的对称面在这里，而不是全局坐标中面
        local = [t for t in sketch_local_transforms(
            {name: data[1] for name, data in sketch_data.items()},
            {name: data[2] for name, data in sketch_data.items()},
        ) if t.kind == "mirror"]  # 轴对称：只要镜像，不要绕法向 180°（中心对称）
        transforms = local + transforms
    log.info("对称方案 %d 个（含草图自身中线推断）", len(transforms))

    min_ratio = min(float(config.symmetry_min_ratio or 0.9), 0.6)
    for tf in transforms:
        mapping_all: dict[str, dict[int, int | None]] = {}
        ratios: list[float] = []
        flip_faces = False
        for sketch_name in info.sketch_names:
            cached = sketch_data.get(sketch_name)
            if cached is None:
                continue
            _sketch, frame, elements = cached
            if _flips_extrusion(tf, frame):
                flip_faces = True
            mapping = build_index_mapping(elements, frame, tf, tol=config.tolerance)
            mapping_all[sketch_name] = mapping
            matched = sum(1 for v in mapping.values() if v is not None)
            ratios.append(matched / len(mapping) if mapping else 0.0)
        if not mapping_all:
            continue
        ratio = min(ratios) if ratios else 0.0

        ref_map: dict[tuple[str, int], tuple[str, int] | None] = {}
        changed = False
        source_refs_mapped = True
        for ref in info.sketches:
            mapping = mapping_all.get(ref.sketch)
            if mapping is None:
                ref_map[(ref.sketch, ref.index)] = None
                source_refs_mapped = False
                continue
            target = mapping.get(ref.index)
            if target is None:
                ref_map[(ref.sketch, ref.index)] = None
                source_refs_mapped = False
                continue
            ref_map[(ref.sketch, ref.index)] = (ref.sketch, target)
            if target != ref.index:
                changed = True
        # 放宽判据：整张草图映射比例够高，或者"这条边自己的草图元素能映射"就试
        if ratio < min_ratio and not (source_refs_mapped and changed):
            log.debug("跳过 %s：草图映射比例 %.2f 且源边元素映射不全", tf.label, ratio)
            continue
        if not changed and not flip_faces:
            continue
        new_label = lbl.replace_sketch_refs(info.raw, ref_map)
        # 顶边/底边：如果这个变换把拉伸方向翻了个，端面 1↔2 也要换
        if new_label and flip_faces:
            new_label = _swap_end_faces(new_label, features)
        if not new_label or new_label == info.raw or new_label in exclude:
            continue
        reference = session.reference_from_label(part, new_label, context)
        if reference is None:
            log.debug("候选标签无法解析: %s", new_label[:80])
            continue
        log.info("对称方案命中：%s（草图映射比例 %.2f）", tf.label, ratio)
        matched = sum(1 for v in sum((list(m.values()) for m in mapping_all.values()), []) if v is not None)
        total = sum(len(m) for m in mapping_all.values())
        if ratio >= best_ratio:
            found.append(
                Candidate(
                    label=new_label,
                    reference=reference,
                    transform=tf,
                    source_index=item.index,
                    matched=matched,
                    total=total,
                )
            )
            best_ratio = max(best_ratio, ratio)

    if not found:
        return [], "没有找到对称边线（该边可能不是由草图元素生成，或对称方式不匹配）"

    # 去重：同一个标签只保留优先度最高的变换
    seen: dict[str, Candidate] = {}
    for cand in found:
        if cand.label not in seen:
            seen[cand.label] = cand
    return list(seen.values()), f"找到 {len(seen)} 条对称边线"


@dataclass
class PairingOutcome:
    added: int = 0
    message: str = ""
    groups: dict[str, list[Candidate]] = field(default_factory=dict)

    @property
    def needs_choice(self) -> bool:
        return len(self.groups) > 1


def pair_selection(session: CatiaSession, config: Config, transform_label: str | None = None) -> PairingOutcome:
    """给当前选择集里的每条边线补齐对称边线。

    * 只有一种对称解释时直接补上；
    * 有多种解释（例如矩形零件既可上下镜像也可左右镜像）时返回 groups，
      交给界面让你选择用哪一种，避免猜错。
    """
    part = session.active_part()
    if part is None:
        return PairingOutcome(message="当前文档不是 CATPart")
    items = session.selection_items()
    edges = [it for it in items if it.is_edge]
    if not edges:
        return PairingOutcome(message="请先选中要倒圆角的边线")

    existing = {it.display_name for it in items}
    groups: dict[str, list[Candidate]] = {}
    notes: list[str] = []
    for item in edges:
        cands, msg = find_counterparts(session, part, item, config, exclude_labels=existing)
        if not cands:
            notes.append(f"#{item.index}: {msg}")
            continue
        for cand in cands:
            groups.setdefault(cand.transform.label, []).append(cand)

    if not groups:
        return PairingOutcome(message="; ".join(notes) or "没有找到对称边线")

    if transform_label is None and len(groups) > 1:
        return PairingOutcome(
            message="检测到多种对称关系，请选择",
            groups=groups,
            added=0,
        )

    label = transform_label or next(iter(groups))
    added = 0
    for cand in groups.get(label, []):
        if session.add_to_selection(cand.reference):
            added += 1
    msg = f"按「{label}」补了 {added} 条对称边线" if added else f"「{label}」没有可补的边线"
    if notes:
        msg += "；" + "; ".join(notes)
    return PairingOutcome(added=added, message=msg, groups=groups)


@dataclass
class GeometricOutcome:
    """命令运行中（对话框内点选）的配对结果。"""

    added: int = 0
    message: str = ""
    groups: dict[str, list[featuregeom.Pairing]] = field(default_factory=dict)
    source_desc: str = ""

    @property
    def needs_choice(self) -> bool:
        return len(self.groups) > 1


def dedupe_groups(groups: dict[str, list[featuregeom.Pairing]]) -> dict[str, list[featuregeom.Pairing]]:
    """同一个目标边只保留一种解释（草图自身中线优先，因为它们在列表前面）。"""
    seen: set[str] = set()
    out: dict[str, list[featuregeom.Pairing]] = {}
    for label, items in groups.items():
        keep = []
        for item in items:
            if item.target.label in seen:
                continue
            seen.add(item.target.label)
            keep.append(item)
        if keep:
            out[label] = keep
    return out


def shift_candidate_label(
    cand: featuregeom.EdgeCandidate,
    offset: int,
    feature_name: str,
    sketch_name: str,
) -> str | None:
    """把候选边的标签按编号偏移重建（用于校准"我们的编号 ↔ CATIA BRep 编号"）。"""
    if offset == 0:
        return cand.label
    if cand.kind == "vertical" and cand.partner_index is not None:
        a, b = cand.element_index + offset, cand.partner_index + offset
        if a < 1 or b < 1:
            return None
        return featuregeom._label_vertical(feature_name, sketch_name, a, b)
    if cand.kind in ("top", "bottom"):
        index = cand.element_index + offset
        if index < 1:
            return None
        return featuregeom._label_end(feature_name, sketch_name, index, 2 if cand.kind == "top" else 1)
    return None


def calibrate_index_offset(
    session: CatiaSession,
    part,
    feature,
    feature_name: str,
    sketch_name: str,
    source: featuregeom.EdgeCandidate,
    radius: float,
) -> int:
    """确定编号偏移：用"单条边 + 不传播"的小圆角探针，体积对得上才认为标签指向的就是这条边。"""
    probe_radius = max(1.0, min(float(radius), 5.0))
    for offset in (0, 1, -1, 2, -2):
        label = shift_candidate_label(source, offset, feature_name, sketch_name)
        if not label:
            continue
        ref = session.reference_from_label(part, label, feature)
        if ref is None:
            continue
        ok, message = create_edge_fillet(
            session,
            part,
            [ref],
            radius=probe_radius,
            propagation=0,
            expected_lengths=[source.length],
            strict=True,
        )
        log.info("编号偏移 %+d 探针：%s（%s）", offset, ok, message)
        if ok:
            # 探针成功 → 删掉这个试探圆角，再返回偏移
            try:
                sel = session._selection()
                names = session.dyn(part).MainBody.Shapes
                last = names.Item(int(names.Count))
                sel.Clear()
                sel.Add(last)
                sel.Delete()
            except Exception as exc:
                log.warning("删除探针圆角失败：%s", exc)
            return offset
    return 0


def probe_label_geometry(
    session: CatiaSession,
    part,
    feature,
    label: str,
    radius: float,
) -> tuple[float, V3 | None] | None:
    """给一个"标签造的边"做单边探针圆角：返回（被倒边长度 mm, 圆角特征重心），随后删掉探针。

    这是唯一能"几何地"验证标签指向哪条边的办法（BRep 引用的 Measurable 不可用，
    但生成的特征可以量体积和重心）。
    """
    ref = session.reference_from_label(part, label, feature)
    if ref is None:
        return None
    part = session.active_part() or part
    part_dyn = session.dyn(part)
    volume_before = session.volume_of(part)
    try:
        fillet = part_dyn.ShapeFactory.AddNewEdgeFilletWithConstantRadius(ref, 0, float(radius))
        part.Update()
    except Exception as exc:
        log.debug("探针圆角创建失败（%s）：%s", label[:40], exc)
        return None
    volume_after = session.volume_of(part)
    length_mm = 0.0
    if isinstance(volume_before, float) and isinstance(volume_after, float):
        r_m = float(radius) / 1000.0
        factor = (1.0 - math.pi / 4.0) * r_m * r_m
        if factor > 0:
            length_mm = abs(volume_after - volume_before) / factor * 1000.0
    cog: V3 | None = None
    last = None
    try:
        shapes = part_dyn.MainBody.Shapes
        last = shapes.Item(int(shapes.Count))
        measurable = session.workbench().GetMeasurable(part.CreateReferenceFromObject(last))
        coords = measurable.GetCOG(session._buf(3))
        if isinstance(coords, (tuple, list)) and len(coords) >= 3:
            cog = V3(float(coords[0]), float(coords[1]), float(coords[2]))
    except Exception as exc:
        log.debug("探针圆角重心读取失败：%s", exc)
    if last is not None:
        try:
            sel = session._selection()
            sel.Clear()
            sel.Add(last)
            sel.Delete()
        except Exception as exc:
            log.warning("删除探针圆角失败：%s", exc)
    return length_mm, cog


def identify_by_probe(
    session: CatiaSession,
    part,
    feature,
    candidates: list,
    expected_samples: list[V3],
    expected_length: float,
    radius: float,
    length_tol: float = 0.25,
) -> tuple[object | None, float]:
    """在候选里用"探针圆角"认出真正对应的那条边：比长度、再比重心位置。"""
    if not expected_samples:
        return None, float("inf")
    mid = V3(
        sum(p.x for p in expected_samples) / len(expected_samples),
        sum(p.y for p in expected_samples) / len(expected_samples),
        sum(p.z for p in expected_samples) / len(expected_samples),
    )
    best = None
    best_score = float("inf")
    for cand in candidates:
        if expected_length > 0 and abs(cand.length - expected_length) > expected_length * length_tol:
            continue
        probed = probe_label_geometry(session, part, feature, cand.label, radius)
        if probed is None:
            continue
        probed_length, cog = probed
        if expected_length > 0 and abs(probed_length - expected_length) > expected_length * length_tol:
            continue
        score = (cog - mid).norm() if cog is not None else float("inf")
        log.info(
            "探针 %s：长度 %.1f（期望 %.1f）重心 %s 距离 %.2f",
            cand.describe(),
            probed_length,
            expected_length,
            tuple(round(c, 1) for c in cog.as_tuple()) if cog else None,
            score,
        )
        if score < best_score:
            best, best_score = cand, score
    return best, best_score


def pair_from_source_cell(
    session: CatiaSession,
    part,
    cell: Cell,
    entry: str,
    config: Config,
    cell_index: CellIndex,
) -> GeometricOutcome:
    """用「对话框里那条被点选的边」的实测几何，推算出对称边线的标签。

    entry 形如 `凸台.1\\目标边线.1`，前半段是生成该边的特征。
    """
    feature_local = entry.split("\\", 1)[0].strip() if "\\" in entry else ""
    feature = session.object_by_name(part, feature_local) if feature_local else None
    if feature is None:
        return GeometricOutcome(message=f"读到了对话框里的对象「{entry}」，但找不到生成它的特征")
    if cell.kind != "edge":
        return GeometricOutcome(
            message=f"对话框里选中的是「{cell.name}」，不是边线（{cell.kind}）——对称补线只对边线生效",
            source_desc=cell.describe(),
        )

    sketch = featuregeom.feature_attr(session, feature, "Sketch")
    if sketch is None:
        return GeometricOutcome(
            message="这条边不是由草图拉伸生成的（可能是圆角/阵列产生的），暂不支持自动配对",
            source_desc=cell.describe(),
        )
    frame = session.read_sketch_frame(sketch)
    elements = session.read_sketch_elements(sketch)
    if frame is None or not elements:
        return GeometricOutcome(message="读不到草图几何，无法配对", source_desc=cell.describe())

    first, second, symmetric = featuregeom.read_limits(session, feature)
    # 逐个试"特征内部名"组合，并用**源边几何**验证哪一组才是真的
    # （例：凹槽如果错当成 Pad，标签也能解析，但指向的是别的边 —— 必须靠几何排除）
    tolerance = max(config.tolerance, 0.05)
    candidates: list = []
    source = None
    used_names: tuple[str, str] | None = None
    tried: list[str] = []
    for fname, sname in featuregeom.name_candidates(session, part, feature, feature_local)[:8]:
        tried.append(f"{fname}/{sname}")
        cands = featuregeom.prismatic_candidates(
            session, part, feature, fname, sname, frame, elements,
            featuregeom._z_ranges(first, second, symmetric),
        )
        if not cands:
            continue
        # 用"子段匹配"：圆角削短过的边也能认出来（连续做第二个圆角时就靠这个）
        src, dist = featuregeom.best_match(cell.samples, cands, tolerance=tolerance, partial=True)
        if src is not None:
            candidates, source, used_names = cands, src, (fname, sname)
            log.info("特征内部名确定为 %s / %s（源边匹配 %s，距离 %.4f）", fname, sname, src.describe(), dist)
            break
    if source is None or used_names is None:
        # 失败时把"最接近的候选有多远"写出来，便于定位（形状变化后常常是边被削短）
        nearest = []
        for cand in candidates[:8]:
            _match, dist = featuregeom.best_match(
                cell.samples, [cand], tolerance=1e9, partial=True
            )
            nearest.append((dist, cand.describe(), cand.length))
        nearest.sort(key=lambda item: item[0])
        log.warning(
            "源边识别失败：实测长=%.1f，最近的候选=%s",
            cell.length or -1,
            [(n[1], round(n[2], 1), round(n[0], 2)) for n in nearest[:4]],
        )
        return GeometricOutcome(
            message=(
                f"这条边（{cell.describe()}）不在按草图推算的候选里 —— "
                "它可能是圆角/阵列/扫掠(槽)/布尔运算产生的边，暂不支持自动配对"
            ),
            source_desc=cell.describe(),
        )
    # 关键一步（非侵入）：把"你点选的那条边"包成 Reference 读它的**真实 BRep 标签**，
    # 从而知道"我们的元素编号"与"CATIA 的 Sketch.1;N 编号"差多少 —— 只改选择集，不建任何特征。
    index_shift = 0
    try:
        if cell.obj is not None:
            # 关键：把**单元对象本身**放进选择集（包成 Reference 读不到东西，实测为空）
            for target in (cell.obj,):
                session.clear_selection()
                if not session.add_to_selection(target):
                    continue
                items = session.selection_items()
                real = items[0].display_name if items else ""
                session.clear_selection()
                if real:
                    info = lbl.parse_label(real)
                    real_indices = [s.index for s in info.sketches]
                    our_indices = [i for i in (source.element_index, source.partner_index) if i]
                    log.info("点选边真实标签索引=%s，我们算的索引=%s", real_indices, our_indices)
                    if real_indices and our_indices:
                        # **按圈**求位移：真实编号与我们的编号在同一圈内的循环位移
                        loops = loop_ranges(elements)
                        log.info("草图分圈结果：%s", loops)
                        from collections import Counter

                        deltas: list[int] = []
                        for real_index in real_indices:
                            for our_index in our_indices:
                                loop = loop_of(our_index, loops)
                                if loop is None or real_index not in loop:
                                    continue
                                deltas.append((loop.index(real_index) - loop.index(our_index)) % len(loop))
                        if deltas:
                            index_shift = Counter(deltas).most_common(1)[0][0]
                            log.info("据此得到**同一圈内**的编号位移 = %+d（圈长 %s）", index_shift, len(loop_of(our_indices[0], loops) or []))
                            # 关键：**源边自己**也要按位移换编号，否则建出来的源边仍然是旁边那条
                            if index_shift:
                                a2 = shift_within_loop(source.element_index, index_shift, loops)
                                b2 = (
                                    shift_within_loop(source.partner_index, index_shift, loops)
                                    if source.partner_index is not None
                                    else None
                                )
                                if source.kind == "vertical" and b2 is not None:
                                    new_label = featuregeom._label_vertical(
                                        used_names[0], used_names[1], a2, b2
                                    )
                                else:
                                    new_label = featuregeom._label_end(
                                        used_names[0],
                                        used_names[1],
                                        a2,
                                        2 if source.kind == "top" else 1,
                                    )
                                source = featuregeom.EdgeCandidate(
                                    label=new_label,
                                    samples=list(source.samples),
                                    length=source.length,
                                    kind=source.kind,
                                    element_index=a2,
                                    partner_index=b2,
                                )
                                log.info("源边编号已按位移修正为 %s（%s）", source.describe(), new_label[40:110])
                    break
                log.warning("读数失败：把单元对象放进选择集后没有可读的 DisplayName")
        else:
            log.warning("读数失败：点选单元没有对象句柄（cell.obj 为空）")
    except Exception as exc:
        log.warning("读取点选边真实标签失败（忽略）：%s", exc)
    # 注意：v0.3.8 的"探针圆角"方案已废弃 —— 它会往用户模型里真的建特征，删除一旦失败就会堆一堆废特征。
    # 现在只做纯几何推算，绝不创建任何试探特征；落点可靠性由"两边都选中再触发"的方式保证。
    offset = 0
    # 命中的那条边解析一次，确认标签在模型里真的存在
    if session.reference_from_label(part, source.label, feature) is None:
        return GeometricOutcome(
            message="推算出的边线标签在模型里解析失败，请把日志发给开发者",
            source_desc=source.describe(),
        )

    center = session.part_cog(part)
    if center is None:
        xs = [p.x for c in candidates for p in c.samples] or [0.0]
        ys = [p.y for c in candidates for p in c.samples] or [0.0]
        zs = [p.z for c in candidates for p in c.samples] or [0.0]
        center = V3((min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2, (min(zs) + max(zs)) / 2)

    transforms = transforms_for(config, center)
    if (config.symmetry_mode or "auto").lower() == "auto":
        # 先试"这条边所在草图自己的中线/草图轴"，很多零件的对称面在这里
        sketch_label = str(featuregeom.feature_attr(session, sketch, "Name", "草图"))
        # 轴对称：只保留镜像方案，去掉"绕草图法向 180°"这类中心对称
        local = [
            t
            for t in sketch_local_transforms({sketch_label: frame}, {sketch_label: elements})
            if t.kind == "mirror"
        ]
        transforms = local + transforms
    log.info("几何配对候选对称方案 %d 个", len(transforms))

    groups: dict[str, list[featuregeom.Pairing]] = {}
    kept_flags: dict[str, bool] = {}
    direction = _edge_direction(source.samples)
    for tf in transforms:
        moved = [tf.apply(p) for p in source.samples]
        # 目标边也用"子段匹配"：源边被圆角削短后，镜像出来仍然只是候选线的一段
        target, distance = featuregeom.best_match(
            moved, candidates, tolerance=tolerance, partial=True
        )
        if target is None:
            continue
        # 按"点选边真实标签"求出的编号位移修正对称边标签（"每次都倒到相邻边"就出在这里）
        current_shift = offset or index_shift
        if current_shift:
            # **只在同一圈内**换编号；跨圈的对称边（如外圈↔内圈）无法用位移表达，直接跳过
            loops = loop_ranges(elements)
            target_loop = loop_of(target.element_index, loops)
            source_loop = loop_of(source.element_index, loops)
            if target_loop is None or target_loop != source_loop:
                log.info("跳过跨圈方案：%s（不在源边所在的圈 %s 内）", target.describe(), source_loop)
                continue
            new_a = shift_within_loop(target.element_index, current_shift, loops)
            new_b = (
                shift_within_loop(target.partner_index, current_shift, loops)
                if target.partner_index is not None
                else None
            )
            if new_a == new_b and new_b is not None:
                continue
            if target.kind == "vertical" and target.partner_index is not None:
                shifted_target = featuregeom._label_vertical(used_names[0], used_names[1], new_a, new_b)
            else:
                shifted_target = featuregeom._label_end(
                    used_names[0], used_names[1], new_a, 2 if target.kind == "top" else 1
                )
            target = featuregeom.EdgeCandidate(
                label=shifted_target,
                samples=list(target.samples),
                length=target.length,
                kind=target.kind,
                element_index=new_a,
                partner_index=new_b,
            )
        # 保险（用户思路）：目标边"到零件中心的距离"必须和源边相近，否则否决
        def _dist_to_center(samples) -> float:
            if not samples:
                return float("nan")
            return min((p - center).norm() for p in samples)

        d_src = _dist_to_center(source.samples)
        d_tgt = _dist_to_center(target.samples)
        if d_src == d_src and d_tgt == d_tgt and abs(d_src - d_tgt) > max(1.0, d_src * 0.05):
            log.info("否决 %s：到中心距离 %.2f 与源边 %.2f 不一致", target.describe(), d_tgt, d_src)
            continue
        if target.label == source.label:
            continue
        groups.setdefault(tf.label, []).append(
            featuregeom.Pairing(transform=tf, source=source, target=target, distance=distance)
        )
        # 只保留"在侧面镜像"的：①方向不变 ②不把拉伸方向翻转（顶↔底不是用户要的"对称边"）
        kept_flags[tf.label] = (
            _keeps_edge_direction(tf, direction) and not _flips_extrusion(tf, frame)
        )

    if not groups:
        return GeometricOutcome(
            message=f"没找到「{source.describe()}」的对称边线（对称方式可能不匹配）",
            source_desc=source.describe(),
        )
    # 只保留"在侧面镜像/旋转"的那一类（把"以这条边方向当法向"的方案剔除）
    same_direction = {label: items for label, items in groups.items() if kept_flags.get(label)}
    if same_direction:
        dropped = [label for label in groups if label not in same_direction]
        if dropped:
            log.info("按方向过滤掉 %d 个方案（镜像法向沿着这条边的方向）", len(dropped))
        groups = same_direction
    # 只解析最终要用的标签（每条一次），解析不过的丢弃
    verified: dict[str, list[featuregeom.Pairing]] = {}
    for label, items in groups.items():
        keep = [
            item
            for item in items
            if session.reference_from_label(part, item.target.label, feature) is not None
        ]
        if keep:
            verified[label] = keep
    if not verified:
        return GeometricOutcome(
            message="推算出的对称边线标签在模型里解析失败，请把日志发给开发者",
            source_desc=source.describe(),
        )
    return GeometricOutcome(groups=dedupe_groups(verified), source_desc=source.describe())


def add_geometric_pairing(session: CatiaSession, part, feature_name_local: str, pairing: featuregeom.Pairing) -> bool:
    """把推算出的对称边线标签变成对象并加入选择集。"""
    feature = session.object_by_name(part, feature_name_local)
    if feature is None:
        return False
    ref = session.reference_from_label(part, pairing.target.label, feature)
    if ref is None:
        return False
    return session.add_to_selection(ref)


def _shape_names(session: CatiaSession, part) -> list[str]:
    """当前实体特征树里的形状名（用来在异常时定位半成品特征）。"""
    try:
        dyn = session.dyn(part)
        shapes = dyn.MainBody.Shapes
        return [str(shapes.Item(i).Name) for i in range(1, int(shapes.Count) + 1)]
    except Exception:
        return []


def _cleanup_new_shapes(session: CatiaSession, part, before: list[str]) -> int:
    """把本次新建的特征删掉（异常路径专用：绝不在零件里留半成品/废特征）。"""
    after = _shape_names(session, part)
    new_shapes = [name for name in after if name not in before]
    if not new_shapes:
        return 0
    selection = session.selection()
    if selection is None:
        return 0
    removed = 0
    for name in new_shapes:
        try:
            obj = session.object_by_name(part, name)
            if obj is None:
                continue
            selection.Clear()
            selection.Add(obj)
            selection.Delete()
            removed += 1
            log.info("已清理未完成的特征：%s", name)
        except Exception as exc:
            log.warning("清理未完成特征 %s 失败：%s", name, exc)
    return removed


def create_edge_fillet(
    session: CatiaSession,
    part,
    references: list,
    radius: float,
    propagation: int = 1,
    expected_lengths: list[float] | None = None,
    strict: bool = False,
) -> tuple[bool, str]:
    """用官方 API 直接生成倒圆角（不依赖交互对话框）。

    实测：100x60x40 的凸台，两条竖直边 R5 倒圆角 → 体积 240000 → 239570 mm³。
    """
    if not references:
        return False, "没有可用的边线引用"
    # 可能跑在气泡回调线程里：重新拿一次文档/部件，避免用别线程的 COM 指针
    part = session.active_part() or part
    volume_before = session.volume_of(part)
    shapes_before = _shape_names(session, part)  # 异常时用它找出"半成品特征"并清掉
    part_dyn = session.dyn(part)
    try:
        factory = part_dyn.ShapeFactory
        fillet = factory.AddNewEdgeFilletWithConstantRadius(references[0], int(propagation), float(radius))
        for ref in references[1:]:
            fillet.AddObjectToFillet(ref)
        try:
            fillet.Radius.Value = float(radius)
        except Exception:
            pass
        part.Update()
    except Exception as exc:
        log.warning("生成倒圆角失败: %s", exc)
        removed = _cleanup_new_shapes(session, part, shapes_before)
        message = f"生成倒圆角失败：{exc}"
        if removed:
            message += f"（已清理 {removed} 个未完成的特征，模型未被改动）"
        return False, message
    name = ""
    try:
        name = str(fillet.Name)
    except Exception:
        pass
    # 体积自检：圆角削掉/补上的体积应当与"半径、边长"对得上；
    # 对不上说明落到了别的边（或没生效）→ 撤销并报错，避免留下错的特征
    if expected_lengths and isinstance(volume_before, float):
        volume_after = session.volume_of(part)
        if isinstance(volume_after, float):
            r_m = float(radius) / 1000.0
            expected = sum((1.0 - math.pi / 4.0) * r_m * r_m * (L / 1000.0) for L in expected_lengths)
            delta = abs(volume_after - volume_before)
            # 注意：对话框里若开了"相切传播"，圆角会沿整条相切链延伸，体积变化会明显大于"两条边"的预期，
            # 这是正常结果，不能据此撤销；只在"体积几乎没变"（说明没生效/落在别的边上）时才撤销。
            bad = expected > 0 and (
                (delta < expected * (0.65 if strict else 0.25))
                or (strict and delta > expected * 1.35)
            )
            if bad:
                log.warning(
                    "倒圆角体积变化异常：实际 %.1f mm³，预期 %.1f mm³（strict=%s），已撤销",
                    delta * 1e9,
                    expected * 1e9,
                    strict,
                )
                try:
                    sel = session._selection()
                    sel.Clear()
                    sel.Add(fillet)
                    sel.Delete()
                except Exception as exc:
                    log.warning("撤销可疑圆角失败: %s", exc)
                return False, (
                    f"生成的圆角体积变化 {delta * 1e9:.1f} mm³ 与预期 {expected * 1e9:.1f} mm³ 不符，"
                    "可能落到了别的边上，已自动撤销"
                )
    return True, f"已生成倒圆角 {name}（半径 {radius:g}，{len(references)} 条边）"
