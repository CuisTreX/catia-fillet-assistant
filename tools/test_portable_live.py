"""验收便携版 exe：在真实 CATIA 上跑一次「补对称边线」。

流程：
    1. 等 CATIA 起来（COM 能附加）；
    2. 建临时零件 100x60x40，选中角点 (100,0) 那条竖直边；
    3. 用**便携版 exe**（CatiaFilletAssistant.exe --pair --config 测试配置）补对称边线；
    4. 回读选择集，确认多出来的那条边正是关于 Y 法向中面镜像的 (2,3)；
    5. 关掉临时文档（不保存）。

用法：
    python tools\\test_portable_live.py [--exe <便携版exe路径>] [--wait 180]
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import comtypes.client as cc  # noqa: E402
import win32com.client as wc  # noqa: E402
from win32com.client import dynamic  # noqa: E402

from catia_fillet_assistant.catia import CatiaSession  # noqa: E402

DEFAULT_EXE = ROOT / "dist" / "CATIA倒圆角助手-便携版" / "CatiaFilletAssistant.exe"
EDGE_NAME = (
    "REdge:(Edge:(Face:(Brp:(Pad.1;0:(Brp:(Sketch.1;1)));None:());"
    "Face:(Brp:(Pad.1;0:(Brp:(Sketch.1;2)));None:());None:(Limits1:();Limits2:()));"
    "WithTemporaryBody;WithoutBuildError;WithSelectingFeatureSupport)"
)


def out(msg: str = "") -> None:
    print(msg, flush=True)


def wait_catia(timeout: int) -> object | None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            cc.GetActiveObject("CATIA.Application")
        except Exception:
            time.sleep(3)
            continue
        try:
            return dynamic.Dispatch(wc.GetActiveObject("CATIA.Application")._oleobj_)
        except Exception:
            time.sleep(3)
    return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--exe", type=Path, default=DEFAULT_EXE)
    parser.add_argument("--wait", type=int, default=240, help="等 CATIA 启动的秒数")
    args = parser.parse_args()

    if not args.exe.exists():
        out(f"!! 找不到便携版 exe：{args.exe}")
        return 2

    out(f"[1/5] 等 CATIA 就绪（最多 {args.wait}s）...")
    app = wait_catia(args.wait)
    if app is None:
        out("!! CATIA 没起来（COM 附加失败）")
        return 1
    app.DisplayFileAlerts = False
    out("      CATIA 已就绪")

    out("[2/5] 建立临时零件并选中一条边线 ...")
    doc = dynamic.Dispatch(app.Documents.Add("Part")._oleobj_)
    part_dyn = doc.Part
    sketch = part_dyn.MainBody.Sketches.Add(part_dyn.OriginElements.PlaneXY)
    f2d = sketch.OpenEdition()
    f2d.CreateLine(0.0, 0.0, 100.0, 0.0)
    f2d.CreateLine(100.0, 0.0, 100.0, 60.0)
    f2d.CreateLine(100.0, 60.0, 0.0, 60.0)
    f2d.CreateLine(0.0, 60.0, 0.0, 0.0)
    sketch.CloseEdition()
    part_dyn.Update()
    pad_dyn = part_dyn.ShapeFactory.AddNewPad(sketch, 40.0)
    part_dyn.Update()

    session = CatiaSession()
    part = session.active_part()
    pad = session.object_by_name(part, "Pad.1")
    edge_ref = session.reference_from_label(part, EDGE_NAME, pad)
    session.clear_selection()
    session.add_to_selection(edge_ref)
    before = session.selection_items()
    out(f"      选中 {len(before)} 条边线：{before[0].label.describe() if before and before[0].label else '?'}")

    test_cfg = args.exe.parent / "portable_test_config.json"
    test_cfg.write_text(
        json.dumps({"symmetry_mode": "mirror", "mirror_axis": "y"}, ensure_ascii=False),
        encoding="utf-8",
    )
    try:
        out(f"[3/5] 调便携版 exe 补对称边线：{args.exe.name} --config {test_cfg.name} --pair")
        proc = subprocess.run(
            [str(args.exe), "--config", str(test_cfg), "--pair"],
            capture_output=True,
            timeout=120,
        )
        out(f"      exe 退出码={proc.returncode}")
        time.sleep(1.5)

        out("[4/5] 回读选择集 ...")
        after = session.selection_items()
        out(f"      现在选择集里有 {len(after)} 条元素")
        ok = False
        for item in after:
            desc = item.label.describe() if item.label else item.describe()
            out(f"        - {desc}")
            if item.label and not item.label.is_edge:
                continue
            if item.label and item.label.is_edge:
                idx = sorted(s.index for s in item.label.sketches)
                if idx == [2, 3]:
                    ok = True
        if len(after) == 2 and ok:
            out("      ✔ 便携版补线成功：新增的正是关于 Y 法向中面镜像的那条边 (2,3)")
            code = 0
        else:
            out("      ✘ 结果不符合预期（期望选择集里出现 2 条边，且新增的是 (2,3)）")
            code = 1
    finally:
        out("[5/5] 清理 ...")
        try:
            test_cfg.unlink()
        except Exception:
            pass
        try:
            doc.Close()
            out("      已关闭临时文档（未保存）")
        except Exception as exc:
            out(f"      关闭临时文档失败：{exc}")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
