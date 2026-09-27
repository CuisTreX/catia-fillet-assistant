"""R5/R14「形状级对称」第一步实测：能否枚举"当前实体的全部 BRep 边"。

对应《R5/R14 形状级对称开发任务 Prompt》§15 / §35 / §72 的第一轮验证问题：
    A 能否枚举当前 Tip 的全部 Edge？
    B 一共多少条？
    C Item2(i).Reference 是否稳定可读？
    D GetMeasurable 是否可用？
    E 能否取到 P0/P1/P2（曲线采样点）？
    F 圆角生成的新边能否也被扫描到？
    G 扫描到的对象能否再次作为 Fillet 输入？

安全约定（与项目历史一致）：
    * 只在**新开的临时 Part** 里操作，测完关闭且不保存；
    * 不触碰用户已打开的任何文档；
    * 只做只读扫描 + 在临时件上补一刀圆角（临时件本来就是测试件）。

用法：
    python tools/probe_r5_edge_scan.py
"""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from comtypes.automation import BSTR  # noqa: E402

from catia_fillet_assistant import cellgeom  # noqa: E402
from catia_fillet_assistant.catia import CatiaSession  # noqa: E402
from catia_fillet_assistant.geometry import V3  # noqa: E402

OUT_DIR = ROOT / "docs" / "API探测"

# 候选搜索语法：第一条是本次的关键假设，其余为对照/伴生类型
QUERIES = (
    "Topology.CGMEdge,all",
    "Topology.CGMEdge,sel",
    "Topology.CGMFace,all",
    "Topology.CGMVertex,all",
    "Topology.CGMEdge",
    "CGMEdge,all",
    "CGMEdge,sel",
    "CATPrtSearch.CGMEdge,all",
    "Topology.Edge,all",  # 已知失败（v0.2 探测过），留作对照
    "Name=*,all",  # 已知成功，留作对照
)

# 选择集准备方式：空 / 选中 PartBody / 选中 Pad 特征
SELECTION_MODES = ("empty", "body", "pad")

# probe_padinfo.log 里验证过"能解析成功"的两条竖直边标签（Pad.1 的两条竖边）
EDGE_LABELS = (
    "REdge:(Edge:(Face:(Brp:(Pad.1;0:(Brp:(Sketch.1;1)));None:();Cf8:());"
    "Face:(Brp:(Pad.1;0:(Brp:(Sketch.1;2)));None:());None:(Limits1:();Limits2:()))",
    "REdge:(Edge:(Face:(Brp:(Pad.1;0:(Brp:(Sketch.1;2)));None:();Cf8:());"
    "Face:(Brp:(Pad.1;0:(Brp:(Sketch.1;3)));None:());None:(Limits1:();Limits2:()))",
)


def out(msg: str = "") -> None:
    print(msg, flush=True)


def brief(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {str(exc)[:150]}"


def as_comtypes(obj):
    """comtypes 动态对象 -> comtypes 指针（动态包装里存的是 _comobj）。"""
    return getattr(obj, "_comobj", obj)


# --------------------------------------------------------------- 临时测试件
def make_box(app):
    """100 x 60 x 40 凸台（与历史验收件一致）。"""
    dyn_app = CatiaSession.dyn(app)
    doc = dyn_app.Documents.Add("Part")
    part = doc.Part
    sketch = part.MainBody.Sketches.Add(part.OriginElements.PlaneXY)
    f2d = sketch.OpenEdition()
    f2d.CreateLine(0.0, 0.0, 100.0, 0.0)
    f2d.CreateLine(100.0, 0.0, 100.0, 60.0)
    f2d.CreateLine(100.0, 60.0, 0.0, 60.0)
    f2d.CreateLine(0.0, 60.0, 0.0, 0.0)
    sketch.CloseEdition()
    part.Update()
    part.ShapeFactory.AddNewPad(sketch, 40.0)
    part.Update()
    return doc


def shape_names(session, part) -> list:
    try:
        dyn = session.dyn(part)
        shapes = dyn.MainBody.Shapes
        return [str(shapes.Item(i).Name) for i in range(1, int(shapes.Count) + 1)]
    except Exception as exc:
        return ["<读取失败 " + brief(exc) + ">"]


# --------------------------------------------------------------- 几何测量
def measure(session, ref) -> dict:
    """对 Reference 做 Measurable：长度 / 面积 / 采样点（P0 P1 P2）。"""
    info = {}
    wb = session.workbench()
    if wb is None:
        info["measurable_error"] = "SPAWorkbench 不可用"
        return info
    try:
        measurable = wb.GetMeasurable(as_comtypes(ref))
    except Exception as exc:
        info["measurable_error"] = brief(exc)
        return info
    info["measurable"] = True
    for attr in ("Length", "Area", "Perimeter", "Radius"):
        try:
            value = getattr(measurable, attr)
        except Exception:
            continue
        if isinstance(value, (int, float)):
            info[attr.lower()] = round(float(value), 6)
    try:
        layout = None
        for spec in session.mod("spa").Measurable._methods_:
            if spec.name == "GetPointsOnCurve":
                layout = spec.argtypes[0]
        if layout is not None:
            buf = layout.create([0.0] * 36)
            points = cellgeom._flat_points(measurable.GetPointsOnCurve(buf), 12)
            info["sample_count"] = len(points)
            if points:
                middle = points[len(points) // 2]
                info["p0"] = [round(v, 4) for v in points[0].as_tuple()]
                info["p1"] = [round(v, 4) for v in middle.as_tuple()]
                info["p2"] = [round(v, 4) for v in points[-1].as_tuple()]
                info["polyline_length"] = round(cellgeom.polyline_length(points), 4)
                info["samples"] = [[round(v, 4) for v in p.as_tuple()] for p in points]
    except Exception as exc:
        info["points_error"] = brief(exc)
    return info


# --------------------------------------------------------------- 扫描一条语法
def prepare_selection(session, part, sel, mode):
    """按模式准备选择集，返回错误说明（成功为 None）。"""
    try:
        sel.Clear()
    except Exception as exc:
        return "Clear: " + brief(exc)
    if mode == "empty":
        return None
    if mode == "body":
        try:
            sel.Add(as_comtypes(part.MainBody))
        except Exception as exc:
            return "Add(MainBody): " + brief(exc)
        return None
    target_name = last_shape_name(session, part) if mode == "tip" else "凸台.1"
    obj = session.object_by_name(part, target_name) or session.object_by_name(part, "Pad.1")
    if obj is None:
        return "找不到特征 " + target_name
    try:
        sel.Add(as_comtypes(obj))
    except Exception as exc:
        return "Add(" + target_name + "): " + brief(exc)
    return None


def last_shape_name(session, part) -> str:
    names = shape_names(session, part)
    return names[-1] if names else "凸台.1"


# --------------------------------------------------- 镜像平面评分（纯几何，只读）
def symmetry_report(edges, tolerance: float = 0.05) -> dict:
    """对扫到的边做"三轴中面镜像匹配率"评分（Prompt §19 的最小验证版）。"""
    usable = [e for e in edges if len(e.get("samples") or []) >= 2]
    if len(usable) < 2:
        return {"error": "可用边不足"}
    coords = [p for edge in usable for p in edge["samples"]]
    mins = [min(p[i] for p in coords) for i in range(3)]
    maxs = [max(p[i] for p in coords) for i in range(3)]
    centers = [(mins[i] + maxs[i]) / 2.0 for i in range(3)]
    report: dict = {"bbox_center": [round(c, 4) for c in centers], "planes": []}
    for axis, axis_name in enumerate("XYZ"):
        matched = 0
        errors: list = []
        for source in usable:
            mirrored = [
                V3(*(p[:axis] + [2.0 * centers[axis] - p[axis]] + p[axis + 1 :]))
                for p in source["samples"]
            ]
            source_points = [V3(*p) for p in source["samples"]]
            # 自映射：边自身关于该平面镜像成自己（垂直于/落在镜像面上的边）
            best = cellgeom.curve_distance(mirrored, source_points, samples=12)
            for candidate in usable:
                if candidate is source:
                    continue
                distance = cellgeom.curve_distance(
                    mirrored, [V3(*p) for p in candidate["samples"]], samples=12
                )
                if distance < best:
                    best = distance
            errors.append(best)
            if best <= tolerance:
                matched += 1
        total = len(usable)
        finite = [e for e in errors if e != float("inf")]
        report["planes"].append(
            {
                "plane_normal": [1 if i == axis else 0 for i in range(3)],
                "origin": round(centers[axis], 4),
                "matched": matched,
                "total": total,
                "matched_ratio": round(matched / total, 3) if total else 0.0,
                "mean_error": round(sum(finite) / len(finite), 4) if finite else None,
            }
        )
    return report


def scan_query(session, part, sel, query, mode, limit=6):
    """执行一次 Search，返回(记录, 可用 Reference 列表)。"""
    record = {"query": query, "mode": mode}
    started = time.time()
    add_error = prepare_selection(session, part, sel, mode)
    if add_error:
        record["prepare_error"] = add_error
    try:
        sel.Search(BSTR(query))
    except Exception as exc:
        record["ok"] = False
        record["error"] = brief(exc)
        record["elapsed"] = round(time.time() - started, 2)
        return record, []
    try:
        count = int(sel.Count2)
    except Exception as exc:
        record["ok"] = True
        record["count_error"] = brief(exc)
        return record, []
    record["ok"] = True
    record["count"] = count
    record["elapsed"] = round(time.time() - started, 2)

    references = []
    items = []
    for index in range(1, min(count, limit) + 1):
        item = {"index": index}
        try:
            element = sel.Item2(index)
        except Exception as exc:
            items.append({"index": index, "item_error": brief(exc)})
            continue
        try:
            item["type"] = str(element.Type)
        except Exception:
            pass
        try:
            item["value_type"] = CatiaSession._type_name(element.Value)
        except Exception:
            pass
        ref = None
        try:
            ref = element.Reference
        except Exception as exc:
            item["reference_error"] = brief(exc)
        if ref is not None:
            item["reference_ok"] = True
            try:
                item["display_name"] = str(ref.DisplayName)
            except Exception:
                pass
            item.update(measure(session, ref))
            if item.get("measurable"):
                references.append(as_comtypes(ref))
        items.append(item)
    record["items"] = items
    return record, references


# --------------------------------------------------------------- 圆角（仅临时件）
def create_temp_fillet(session, part, refs):
    """在临时件上用两条边建一个倒圆角，返回结果与引用来源。"""
    result = {}
    used = list(refs[:2])
    result["reference_source"] = "扫描得到的 Reference" if len(used) >= 2 else "BRep 标签兜底"
    if len(used) < 2:
        used = []
        try:
            pad = session.object_by_name(part, "凸台.1") or session.object_by_name(part, "Pad.1")
            labels = []
            for label in EDGE_LABELS:
                ref = part.CreateReferenceFromBRepName(BSTR(label), pad)
                labels.append(str(ref.DisplayName)[:40])
                used.append(ref)
            result["label_refs"] = labels
        except Exception as exc:
            result["label_error"] = brief(exc)
            return result
    if len(used) < 2:
        result["error"] = "拿不到两条可用边"
        return result
    dyn_part = session.dyn(part)
    try:
        factory = dyn_part.ShapeFactory
        fillet = factory.AddNewEdgeFilletWithConstantRadius(used[0], 1, 5.0)
        fillet.AddObjectToFillet(used[1])
        part.Update()
        result["ok"] = True
    except Exception as exc:
        result["ok"] = False
        result["error"] = brief(exc)
    result["volume"] = session.volume_of(part)
    result["shapes"] = shape_names(session, part)
    return result


# --------------------------------------------------------------------- 主流程
def main() -> int:
    session = CatiaSession()
    app = session.connect()
    out(f"[连接] CATIA 已连接，版本={getattr(app, 'Version', '?')}")
    previous_doc = None
    try:
        previous_doc = app.ActiveDocument
        out(f"[环境] 用户当前文档：{getattr(previous_doc, 'Name', '?')}（不动它）")
    except Exception:
        previous_doc = None

    alerts_backup = None
    try:
        alerts_backup = bool(app.DisplayFileAlerts)
        app.DisplayFileAlerts = False
    except Exception:
        alerts_backup = None

    payload = {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "catia_version": str(getattr(app, "Version", "?")),
        "scans": [],
        "rescan_after_fillet": [],
    }
    doc = None
    try:
        doc = make_box(app)
        part = session.active_part()
        out(f"[准备] 临时件已建：{shape_names(session, part)} 体积={session.volume_of(part)}")
        payload["base_volume"] = session.volume_of(part)
        payload["base_shapes"] = shape_names(session, part)

        selection = app.ActiveDocument.Selection

        # ---- 全量扫描（圆角前）：回答 A/B/D/E，并做镜像可行性验证 ----
        out("")
        out("================ 全量扫描：圆角前（Pad.1，Topology.CGMEdge,sel） ================")
        pre_record, pre_refs = scan_query(
            session, part, selection, "Topology.CGMEdge,sel", "pad", limit=40
        )
        payload["pre_edges"] = pre_record
        payload["symmetry_pre"] = symmetry_report(pre_record.get("items", []))
        out(f"  边数 = {pre_record.get('count', pre_record.get('error'))}")
        for item in pre_record.get("items", []):
            out(
                "    长={l:<9}{t:<32}采样={s} {n}".format(
                    l=str(item.get("length", "-")),
                    t=str(item.get("type", "?")),
                    s=item.get("sample_count", "-"),
                    n=str(item.get("display_name", ""))[8:78],
                )
            )
        out(f"[镜像评分-前] {json.dumps(payload['symmetry_pre'], ensure_ascii=False)}")

        working = []
        out("")
        out("================ 语法扫描（临时件：仅 Pad.1） ================")
        for mode in SELECTION_MODES:
            for query in QUERIES:
                record, refs = scan_query(session, part, selection, query, mode)
                payload["scans"].append(record)
                flag = "OK" if record.get("ok") else "XX"
                detail = record.get("count", record.get("error", ""))
                out(f"  {flag} [{mode:<5}] {query:<28} -> {detail}")
                if record.get("ok") and (record.get("count") or 0) > 0 and refs:
                    working.append((mode, query, refs))
                    for item in record["items"][:3]:
                        out(
                            "         - {t} 长={l} 采样={s} {n}".format(
                                t=item.get("type", "?"),
                                l=item.get("length", "-"),
                                s=item.get("sample_count", "-"),
                                n=str(item.get("display_name", ""))[:70],
                            )
                        )

        payload["working_queries"] = [m + "|" + q for m, q, _ in working]
        out("")
        out(f"[结论] 可用枚举语法：{payload['working_queries'] or '（无）'}")

        # 圆角 -> 复扫（回答 F / G）
        refs = pre_refs if len(pre_refs) >= 2 else (working[0][2] if working else [])
        fillet = create_temp_fillet(session, part, refs)
        payload["fillet"] = fillet
        out("")
        out(
            "[圆角] 引用来源={} ok={} 体积={} 特征树={}".format(
                fillet.get("reference_source"),
                fillet.get("ok"),
                fillet.get("volume"),
                fillet.get("shapes"),
            )
        )
        if fillet.get("error"):
            out(f"[圆角] 失败：{fillet['error']}")

        # ---- 全量扫描（圆角后）：回答 F（新边能不能扫到）+ 复算镜像 ----
        for mode in ("tip", "body"):
            record, _ = scan_query(
                session, part, selection, "Topology.CGMEdge,sel", mode, limit=40
            )
            payload["post_edges_" + mode] = record
            payload["symmetry_post_" + mode] = symmetry_report(record.get("items", []))
            out("")
            out(f"================ 全量扫描：圆角后（{mode}）: "
                f"{record.get('count', record.get('error'))} 条 ================")
            for item in record.get("items", []):
                out(
                    "    长={l:<9}{t:<32}采样={s} {n}".format(
                        l=str(item.get("length", "-")),
                        t=str(item.get("type", "?")),
                        s=item.get("sample_count", "-"),
                        n=str(item.get("display_name", ""))[8:78],
                    )
                )
            out(f"[镜像评分-后/{mode}] "
                f"{json.dumps(payload['symmetry_post_' + mode], ensure_ascii=False)}")

        for mode, query, _ in working:
            record, _ = scan_query(session, part, selection, query, mode)
            payload["rescan_after_fillet"].append(record)
            out(f"  [复扫 {mode}] {query} -> {record.get('count', record.get('error'))}")

        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        target = OUT_DIR / ("r5_edge_scan_" + stamp + ".json")
        target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        out("")
        out(f"[输出] {target}")
        return 0
    except Exception as exc:
        out(f"[异常] {brief(exc)}")
        import traceback

        traceback.print_exc()
        return 1
    finally:
        if doc is not None:
            try:
                doc.Close()
                out("[清理] 临时文档已关闭（未保存）")
            except Exception as exc:
                out(f"[清理] 关闭失败：{brief(exc)}")
        if previous_doc is not None:
            try:
                previous_doc.Activate()
                out(f"[清理] 已切回用户文档 {getattr(previous_doc, 'Name', '?')}")
            except Exception:
                pass
        if alerts_backup is not None:
            try:
                app.DisplayFileAlerts = alerts_backup
            except Exception:
                pass


if __name__ == "__main__":
    raise SystemExit(main())
