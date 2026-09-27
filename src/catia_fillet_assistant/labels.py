"""GenericNaming（BRep 标签）解析与改写。

CATIA 里任何一条边线/面，都能用 SelectedElement.Reference.DisplayName 读出一个
「GenericNaming 标签」，例如一条竖直边线：

    REdge:(Edge:(Face:(Brp:(Pad.1;0:(Brp:(Sketch.1;1)));None:();Cf8:());
                Face:(Brp:(Pad.1;0:(Brp:(Sketch.1;2)));None:();Cf8:());
                None:(Limits1:();Limits2:());Cf8:());
          WithTemporaryBody;WithoutBuildError;WithSelectingFeatureSupport;MFBRepVersion_CXR3_SP2)

这个字符串同时说明了两件事：
    * 这条边由哪个特征生成（Pad.1）；
    * 由哪些生成元素组成（草图 Sketch.1 的第 1、2 条线拉出来的两个侧面）。
因此只要按对称关系把草图元素序号换掉，就能「算」出对称的那条边线，
再交给 Part.CreateReferenceFromBRepName 变成真正的可选对象。

本模块只做字符串解析/改写，不依赖 CATIA，便于单元测试。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

KIND_EDGE = "edge"
KIND_FACE = "face"
KIND_VERTEX = "vertex"
KIND_UNKNOWN = "unknown"

_PREFIX_TO_KIND = {
    "REdge": KIND_EDGE,
    "RSur": KIND_FACE,
    "RFace": KIND_FACE,
    "RVertex": KIND_VERTEX,
}

# 手动拾取时 CATIA 会加前缀，例如：
#   Selection_RSur:(Face:(Brp:(Pad.1;2);None:();Cf8:()));yz-plane;Z
_SELECTION_PREFIXES = ("Selection_", "Selection:", "Selection.")

# Brp:(名字;序号  —— 名字可能是 Pad.1 / Sketch.2 / Pocket.3 等
_BRP_RE = re.compile(r"Brp:\(\s*([A-Za-z_][A-Za-z0-9_]*\.\d+)\s*;\s*(\d+)")

_SKETCH_NAME_RE = re.compile(r"^Sketch\.\d+$")


@dataclass(frozen=True)
class SketchRef:
    """标签里引用到的草图元素：草圖名 + 元素序号（1 起）。"""

    sketch: str
    index: int


@dataclass(frozen=True)
class FeatureRef:
    """标签里引用到的特征面：特征名 + 面序号（0 通常是侧面，1/2 是两个端面）。"""

    feature: str
    face_index: int


@dataclass
class LabelInfo:
    raw: str
    kind: str
    sketches: list[SketchRef] = field(default_factory=list)
    features: list[FeatureRef] = field(default_factory=list)

    @property
    def first_feature(self) -> str | None:
        return self.features[0].feature if self.features else None

    @property
    def sketch_names(self) -> list[str]:
        seen: list[str] = []
        for s in self.sketches:
            if s.sketch not in seen:
                seen.append(s.sketch)
        return seen

    @property
    def is_edge(self) -> bool:
        return self.kind == KIND_EDGE

    @property
    def is_face(self) -> bool:
        return self.kind == KIND_FACE

    def describe(self) -> str:
        sk = ", ".join(f"{s.sketch};{s.index}" for s in self.sketches) or "-"
        ft = ", ".join(f"{f.feature};{f.face_index}" for f in self.features) or "-"
        return f"{self.kind} 特征=[{ft}] 草图元素=[{sk}]"


def parse_label(raw: str) -> LabelInfo:
    """解析 GenericNaming 标签。解析失败不抛异常，返回 kind=unknown。"""
    text = (raw or "").strip()
    for prefix in _SELECTION_PREFIXES:
        if text.startswith(prefix):
            text = text[len(prefix) :]
            break
    kind = KIND_UNKNOWN
    for prefix, k in _PREFIX_TO_KIND.items():
        if text.startswith(prefix + ":"):
            kind = k
            break
    if kind == KIND_UNKNOWN:
        # 兜底：标签里任意位置出现 REdge/RSur/RFace 也能判断
        if "REdge:" in text:
            kind = KIND_EDGE
        elif "RSur:" in text or "RFace:" in text:
            kind = KIND_FACE
        elif "RVertex:" in text:
            kind = KIND_VERTEX

    sketches: list[SketchRef] = []
    features: list[FeatureRef] = []
    for name, idx_text in _BRP_RE.findall(text):
        idx = int(idx_text)
        if _SKETCH_NAME_RE.match(name):
            ref = SketchRef(name, idx)
            if ref not in sketches:
                sketches.append(ref)
        else:
            ref = FeatureRef(name, idx)
            if ref not in features:
                features.append(ref)
    return LabelInfo(raw=text, kind=kind, sketches=sketches, features=features)


def replace_sketch_refs(raw: str, mapping: dict[tuple[str, int], tuple[str, int] | None]) -> str | None:
    """按 mapping 替换标签里的草图引用；mapping 里映射为 None 表示该组合法不存在。"""

    def _sub(match: re.Match[str]) -> str:
        name, idx_text = match.group(1), match.group(2)
        idx = int(idx_text)
        if name not in _SKETCH_NAME_RE.pattern and not _SKETCH_NAME_RE.match(name):
            return match.group(0)
        target = mapping.get((name, idx))
        if target is None:
            return match.group(0)
        new_name, new_idx = target
        return f"Brp:({new_name};{new_idx}"

    out = _BRP_RE.sub(_sub, raw)
    return out if out != raw else None


def replace_feature_refs(
    raw: str,
    feature_map: dict[str, str],
    sketch_map: dict[str, str] | None = None,
) -> str:
    """把标签里的特征名（如 Pad.1→Pad.2）和草图名（Sketch.1→Sketch.5）整体替换。"""
    out = raw
    for old, new in feature_map.items():
        out = re.sub(rf"Brp:\(\s*{re.escape(old)}\s*;", f"Brp:({new};", out)
    for old, new in (sketch_map or {}).items():
        out = re.sub(rf"Brp:\(\s*{re.escape(old)}\s*;", f"Brp:({new};", out)
    return out


def feature_names(raw: str) -> list[str]:
    return [f.feature for f in parse_label(raw).features]


def sketch_refs(raw: str) -> list[SketchRef]:
    return list(parse_label(raw).sketches)


def is_edge(raw: str) -> bool:
    return parse_label(raw).is_edge


def is_face(raw: str) -> bool:
    return parse_label(raw).is_face


def looks_like_reference_label(raw: str) -> bool:
    """判断一个 Reference.DisplayName 是不是拓扑标签（而不是普通特征名）。"""
    text = (raw or "").strip()
    return any(text.startswith(p + ":") for p in _PREFIX_TO_KIND)
