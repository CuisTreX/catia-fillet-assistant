"""几何变换与草图映射单元测试（不依赖 CATIA）。"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from catia_fillet_assistant.geometry import (  # noqa: E402
    SketchElement,
    SketchFrame,
    Transform,
    V3,
    build_index_mapping,
    match_element,
)

FRAME = SketchFrame(origin=V3(0, 0, 0), u=V3(1, 0, 0), v=V3(0, 1, 0), w=V3(0, 0, 1))

RECT = [
    SketchElement(index=1, kind="line", points=[(0.0, 0.0), (100.0, 0.0)]),
    SketchElement(index=2, kind="line", points=[(100.0, 0.0), (100.0, 60.0)]),
    SketchElement(index=3, kind="line", points=[(100.0, 60.0), (0.0, 60.0)]),
    SketchElement(index=4, kind="line", points=[(0.0, 60.0), (0.0, 0.0)]),
]


def test_mirror_x() -> None:
    # 关于 x=50 的平面镜像：x -> 100-x
    # 元素 1 (0,0)-(100,0) 与元素 3 (100,60)-(0,60) 各自映射到自己，
    # 元素 2 (100,0)-(100,60) 与元素 4 (0,60)-(0,0) 互换。
    tf = Transform.mirror("x", V3(50, 30, 20), V3(1, 0, 0))
    mapping = build_index_mapping(RECT, FRAME, tf)
    assert mapping[1] == 1 and mapping[3] == 3
    assert mapping[2] == 4 and mapping[4] == 2


def test_mirror_y() -> None:
    tf = Transform.mirror("y", V3(50, 30, 20), V3(0, 1, 0))
    mapping = build_index_mapping(RECT, FRAME, tf)
    assert mapping[1] == 3 and mapping[3] == 1
    assert mapping[2] == 2 and mapping[4] == 4  # 落在对称面内的边映射到自己


def test_rotate_180() -> None:
    tf = Transform.rotate("z", V3(50, 30, 20), V3(0, 0, 1), 180.0)
    mapping = build_index_mapping(RECT, FRAME, tf)
    assert mapping[1] == 3 and mapping[2] == 4


def test_off_plane_returns_none() -> None:
    # 绕 X 轴旋转 180° 会把草图平面翻到 z=40 平面，不该给出映射
    tf = Transform.rotate("x", V3(50, 30, 20), V3(1, 0, 0), 180.0)
    mapping = build_index_mapping(RECT, FRAME, tf)
    assert all(v is None for v in mapping.values())


def test_match_element_symmetric_pair() -> None:
    moved = [(100.0, 60.0, 0.0), (0.0, 60.0, 0.0)]
    assert match_element(moved, RECT, 1e-3) == 3
