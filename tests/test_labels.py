"""标签解析/改写单元测试（不依赖 CATIA）。"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from catia_fillet_assistant import labels as lbl  # noqa: E402

EDGE = (
    "REdge:(Edge:(Face:(Brp:(Pad.1;0:(Brp:(Sketch.1;1)));None:();Cf8:());"
    "Face:(Brp:(Pad.1;0:(Brp:(Sketch.1;2)));None:();Cf8:());None:(Limits1:();Limits2:());Cf8:());"
    "WithTemporaryBody;WithoutBuildError;WithSelectingFeatureSupport;MFBRepVersion_CXR3_SP2)"
)
FACE = "RSur:(Face:(Brp:(Pad.1;2);None:();Cf8:());WithTemporaryBody;WithoutBuildError;WithSelectingFeatureSupport)"


def test_kind() -> None:
    assert lbl.parse_label(EDGE).is_edge
    assert lbl.parse_label(FACE).is_face
    assert not lbl.parse_label(EDGE).is_face
    assert not lbl.parse_label("Pad.1").is_edge


def test_refs() -> None:
    info = lbl.parse_label(EDGE)
    assert [(s.sketch, s.index) for s in info.sketches] == [("Sketch.1", 1), ("Sketch.1", 2)]
    assert [(f.feature, f.face_index) for f in info.features] == [("Pad.1", 0)]
    face = lbl.parse_label(FACE)
    assert face.sketches == []
    assert [(f.feature, f.face_index) for f in face.features] == [("Pad.1", 2)]


def test_replace() -> None:
    mapping = {("Sketch.1", 1): ("Sketch.1", 3), ("Sketch.1", 2): ("Sketch.1", 4)}
    out = lbl.replace_sketch_refs(EDGE, mapping)
    assert out is not None
    got = [(s.sketch, s.index) for s in lbl.parse_label(out).sketches]
    assert got == [("Sketch.1", 3), ("Sketch.1", 4)]
    # 无变化时返回 None
    same = {("Sketch.1", 1): ("Sketch.1", 1), ("Sketch.1", 2): ("Sketch.1", 2)}
    assert lbl.replace_sketch_refs(EDGE, same) is None


def test_replace_feature() -> None:
    out = lbl.replace_feature_refs(EDGE, {"Pad.1": "Pad.2"}, {"Sketch.1": "Sketch.5"})
    info = lbl.parse_label(out)
    assert info.first_feature == "Pad.2"
    assert info.sketch_names == ["Sketch.5"]
