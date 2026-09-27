"""CATIA V5 自动化能力探测器（配合插件开发使用）。

目的：在**真实运行中的 CATIA 实例**上验证下面这些能力到底是否可用：
    1. Selection.Search 的查询语法（能不能按类型取到边线/面）
    2. SelectedElement.Type / FilterCorrespondence 能否区分「边线」和「面」
    3. 通过 CreateReferenceFromBRepName 拿到边线/面后的几何采样能力（Measurable）
    4. 交互命令（Edge Fillet）的窗口类名/标题，以及宏能否把元素塞进正在运行的命令

安全约定：
    * 只新建自己的临时 Part 文档，结束时 Close() 不保存；
    * 调用前记录已打开文档清单，绝不关闭别人的文档；
    * 所有实验在同一个临时文档里进行。

用法：
    python probe_catia.py info
    python probe_catia.py search
    python probe_catia.py classify
    python probe_catia.py geometry
    python probe_catia.py windows
    python probe_catia.py command
    python probe_catia.py all
"""

from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes as wt
import sys
import time
import traceback
from typing import Any

import pythoncom
import win32api
import win32com.client as wc
import win32con
import win32gui
import win32process

PROG_ID = "CATIA.Application"


def out(msg: str = "") -> None:
    print(msg, flush=True)


def hr(title: str) -> None:
    out("")
    out("=" * 78)
    out(title)
    out("=" * 78)


def attach(start_if_missing: bool = False):
    try:
        app = wc.GetActiveObject(PROG_ID)
        out("[连接] 已附加到正在运行的 CATIA 实例")
        return app
    except pythoncom.com_error:
        if not start_if_missing:
            raise SystemExit("CATIA 没有在运行。请先启动 CATIA V5，或加 --start 参数让脚本自己拉起。")
        out("[连接] 未发现运行中的实例，正在启动新的 CATIA ...")
        return wc.Dispatch(PROG_ID)


def open_doc_names(app) -> list[str]:
    names = []
    try:
        for i in range(1, app.Documents.Count + 1):
            names.append(app.Documents.Item(i).Name)
    except Exception as exc:
        out(f"[警告] 读取文档列表失败: {exc}")
    return names


def catia_windows() -> list[tuple[int, int, str, str]]:
    """返回属于 CNEXT.exe 的所有顶层窗口：(hwnd, pid, class, title)。"""
    result: list[tuple[int, int, str, str]] = []

    def cb(hwnd, _param):
        pid = win32process.GetWindowThreadProcessId(hwnd)[1]
        try:
            handle = win32api.OpenProcess(win32con.PROCESS_QUERY_INFORMATION | win32con.PROCESS_VM_READ, False, pid)
            exe = win32process.GetModuleFileNameEx(handle, 0)
            win32api.CloseHandle(handle)
        except Exception:
            return True
        if exe.lower().endswith("cnext.exe"):
            result.append((hwnd, pid, win32gui.GetClassName(hwnd), win32gui.GetWindowText(hwnd)))
        return True

    win32gui.EnumWindows(cb, None)
    return result


def click_child_button(dialog_hwnd: int, texts: tuple[str, ...]) -> bool:
    """在对话框里找按钮控件并点它（按标题文字匹配）。"""
    for child, cls, txt in enum_children_deep(dialog_hwnd):
        if txt.strip() in texts and "button" in cls.lower():
            out(f"[点击] 控件 {cls} '{txt}' hwnd={child}")
            win32gui.SendMessage(child, win32con.BM_CLICK, 0, 0)
            return True
    return False


def enum_children_deep(parent: int) -> list[tuple[int, str, str]]:
    """列出父窗口下所有后代控件（EnumChildWindows 本身是递归的）。"""
    found: list[tuple[int, str, str]] = []

    def cb(hwnd, _param):
        try:
            found.append((hwnd, win32gui.GetClassName(hwnd), win32gui.GetWindowText(hwnd)))
        except Exception:
            found.append((hwnd, "?", ""))
        return True

    try:
        win32gui.EnumChildWindows(parent, cb, None)
    except Exception:
        pass
    return found


# --------------------------------------------------------------------------------------
# 临时测试文档
# --------------------------------------------------------------------------------------


def make_test_document(app):
    """新建一个 Part 文档：100 x 60 的矩形 sketch 拉伸 40mm。返回 (doc, part, pad)。"""
    existing = open_doc_names(app)
    out(f"[准备] 当前已打开文档: {existing or '（无）'}")
    doc = app.Documents.Add("Part")
    part = doc.Part
    body = part.MainBody
    sketch = body.Sketches.Add(part.OriginElements.PlaneXY)
    f2d = sketch.OpenEdition()
    f2d.CreateLine(0.0, 0.0, 100.0, 0.0)
    f2d.CreateLine(100.0, 0.0, 100.0, 60.0)
    f2d.CreateLine(100.0, 60.0, 0.0, 60.0)
    f2d.CreateLine(0.0, 60.0, 0.0, 0.0)
    sketch.CloseEdition()
    part.Update()
    pad = part.ShapeFactory.AddNewPad(sketch, 40.0)
    part.Update()
    out(f"[准备] 临时文档 {doc.Name} 已建立，Pad.1 已创建（100x60x40）")
    return doc, part, pad


EDGE_NAME = (
    "REdge:(Edge:(Face:(Brp:(Pad.1;0:(Brp:(Sketch.1;1)));None:());"
    "Face:(Brp:(Pad.1;0:(Brp:(Sketch.1;2)));None:());None:(Limits1:();Limits2:()));"
    "WithTemporaryBody;WithoutBuildError;WithSelectingFeatureSupport)"
)
FACE_NAME = "RSur:(Face:(Brp:(Pad.1;2);None:());WithTemporaryBody;WithoutBuildError;WithSelectingFeatureSupport)"


def describe_selected(sel, index: int) -> str:
    info = [f"    #{index}"]
    try:
        item = sel.Item2(index)
    except Exception as exc:
        return f"    #{index} Item2 失败: {exc}"
    for prop in ("Type", "Value", "Reference"):
        try:
            val = getattr(item, prop)
            if prop == "Value":
                info.append(f"{prop}={type_name(val)}")
            elif prop == "Reference":
                info.append(f"{prop}.DisplayName={safe(lambda: val.DisplayName)}")
            else:
                info.append(f"{prop}={val!r}")
        except Exception as exc:
            info.append(f"{prop}<err {exc}>")
    try:
        coords = item.GetCoordinates()
        info.append(f"GetCoordinates={coords}")
    except Exception as exc:
        info.append(f"GetCoordinates<err {exc}>")
    return "  ".join(info)


def type_name(obj: Any) -> str:
    try:
        return str(obj._oleobj_.GetTypeInfo().GetDocumentation(-1)[0])
    except Exception:
        try:
            return type(obj).__name__
        except Exception:
            return "?"


def safe(fn):
    try:
        return fn()
    except Exception as exc:
        return f"<err {exc}>"


# --------------------------------------------------------------------------------------
# 各项探测
# --------------------------------------------------------------------------------------


def probe_info(app) -> None:
    hr("[1] 基本信息")
    out(f"FullName        : {safe(lambda: app.FullName)}")
    out(f"Path            : {safe(lambda: app.Path)}")
    out(f"SystemConfig    : {safe(lambda: app.SystemConfiguration)}")
    out(f"Caption         : {safe(lambda: app.Caption)}")
    out(f"Documents.Count : {safe(lambda: app.Documents.Count)}")
    out(f"ActiveDoc       : {safe(lambda: app.ActiveDocument.Name)}")
    out(f"GetWorkbenchId  : {safe(lambda: app.GetWorkbenchId())}")
    out(f"Batch/Visible   : {safe(lambda: app.Batch)} / {safe(lambda: app.Visible)}")


SEARCH_QUERIES = [
    "Name=*,all",
    "Type=Pad,all",
    "Type=Edge,all",
    "Type=Face,all",
    "Type=TriDimFeatEdge,all",
    "Topology.Edge,all",
    "Topology.Face,all",
    "Edge,all",
    "Face,all",
    "Surface,all",
    "('CATIA V4'.PLN + 'CATIA V4'.AXS),all",
]


def probe_search(app, part, pad) -> None:
    hr("[2] Selection.Search 语法探测")
    sel = part.Parent.Selection
    for q in SEARCH_QUERIES:
        sel.Clear()
        try:
            sel.Search(q)
            n = sel.Count2
            sample = describe_selected(sel, 1) if n else "      （无结果）"
            out(f"  [{q}] -> 命中 {n}")
            if n:
                out(sample)
        except Exception as exc:
            out(f"  [{q}] -> 失败: {exc}")
    sel.Clear()


def probe_classify(app, part, pad) -> None:
    hr("[3] 选择内容分类：边线 vs 面")
    sel = part.Parent.Selection
    edge_ref = part.CreateReferenceFromBRepName(EDGE_NAME, pad)
    face_ref = part.CreateReferenceFromBRepName(FACE_NAME, pad)

    for label, ref in (("边线引用", edge_ref), ("面引用", face_ref)):
        sel.Clear()
        try:
            sel.Add(ref)
        except Exception as exc:
            out(f"  {label}: 加入选择失败 {exc}")
            continue
        fit_edge = safe(lambda: sel.FilterCorrespondence(["TriDimFeatEdge"]))
        fit_face = safe(lambda: sel.FilterCorrespondence(["TriDimFeatFace"]))
        fit_any = safe(lambda: sel.FilterCorrespondence(["AnyObject"]))
        out(f"  {label}: Count2={safe(lambda: sel.Count2)}")
        out(
            f"      FilterCorrespondence: Edge={fit_edge} Face={fit_face} AnyObject={fit_any}"
        )
        out(describe_selected(sel, 1))
        out(f"      Reference.DisplayName={safe(lambda: ref.DisplayName)}")
    sel.Clear()


def probe_geometry(app, part, pad) -> None:
    hr("[4] 几何能力：Measurable 采样 / 长度")
    spa = part.Parent.GetWorkbench("SPAWorkbench")
    edge_ref = part.CreateReferenceFromBRepName(EDGE_NAME, pad)
    face_ref = part.CreateReferenceFromBRepName(FACE_NAME, pad)

    for label, ref in (("边线", edge_ref), ("面", face_ref)):
        try:
            m = spa.GetMeasurable(ref)
        except Exception as exc:
            out(f"  {label}: GetMeasurable 失败 {exc}")
            continue
        out(f"  {label}: Length={safe(lambda: m.Length)} Area={safe(lambda: m.Area)}")
        pts = safe(lambda: m.GetPointsOnCurve())
        if isinstance(pts, tuple) and len(pts) == 2:
            coords, flags = pts
            out(f"      GetPointsOnCurve -> {len(coords)} 个点, 采样标记={flags[:1]}")
            out(f"      首点={tuple(round(c, 4) for c in coords[0])}")
            out(f"      末点={tuple(round(c, 4) for c in coords[-1])}")
        else:
            out(f"      GetPointsOnCurve -> {pts}")

    # 边线是否可以 Extract + Boundary（面 → 边界边线的几何途径）
    try:
        hsf = part.HybridShapeFactory
        extract = hsf.AddNewExtract(face_ref)
        extract.PropagationType = 1
        gs = part.HybridBodies.Add()
        gs.Name = "Probe_Temp"
        gs.AppendHybridShape(extract)
        part.Update()
        out("  面引用 Extract 成功（临时 Geometrical Set: Probe_Temp）")
        bnd = hsf.AddNewBoundary(extract, None, 1)
        gs.AppendHybridShape(bnd)
        part.Update()
        m2 = spa.GetMeasurable(part.CreateReferenceFromObject(bnd))
        out(f"  Boundary 采样: Length={safe(lambda: m2.Length)}")
    except Exception as exc:
        out(f"  Extract/Boundary 失败: {exc}")


def probe_windows(app) -> None:
    hr("[5] CATIA 窗口清单（用于识别命令对话框）")
    wins = catia_windows()
    out(f"  共 {len(wins)} 个顶层窗口：")
    for hwnd, pid, cls, title in wins:
        out(f"    hwnd={hwnd:<10} pid={pid:<7} class={cls:<28} title={title!r}")


CMD_IDS = ["EdgeFillet", "Edge Fillet", "Fillet", "EdgeFilletCmd"]


def sample_boundary(part, boundary) -> tuple[str, str, str]:
    """返回 (类型名, 长度, 首末点) 的可读描述，失败时返回错误文本。"""
    spa = part.Parent.GetWorkbench("SPAWorkbench")
    try:
        ref = part.CreateReferenceFromObject(boundary)
    except Exception as exc:
        return (type_name(boundary), f"CreateReferenceFromObject 失败: {exc}", "")
    try:
        m = spa.GetMeasurable(ref)
    except Exception as exc:
        return (type_name(boundary), f"GetMeasurable 失败: {exc}", "")
    length = safe(lambda: m.Length)
    pts = safe(lambda: m.GetPointsOnCurve())
    if isinstance(pts, tuple) and len(pts) == 2 and len(pts[0]) > 1:
        coords = pts[0]
        head = ",".join(f"{c:.2f}" for c in coords[0])
        tail = ",".join(f"{c:.2f}" for c in coords[-1])
        geom = f"{len(coords)} 点 [{head}] -> [{tail}]"
    else:
        geom = str(pts)
    return (type_name(boundary), str(length), geom)


def probe_labels(app, part, pad) -> None:
    hr("[7] 标签 → 边界对象 → 几何（对称边线的关键链路）")
    sel = part.Parent.Selection
    shapes = part.MainBody.Shapes

    ref = part.CreateReferenceFromBRepName(EDGE_NAME, pad)
    sel.Clear()
    sel.Add(ref)
    label = sel.Item2(1).Reference.DisplayName
    out("  选中竖直边线 canonical label:")
    out(f"    {label}")

    # 1) 用 canonical label 取 Boundary 对象
    try:
        b = shapes.GetBoundary(label)
        t, ln, geom = sample_boundary(part, b)
        out(f"  Shapes.GetBoundary(canonical) -> {t}  Length={ln}")
        out(f"      采样: {geom}")
        out(f"      Boundary.Parent = {safe(lambda: b.Parent.Name)}")
    except Exception as exc:
        out(f"  Shapes.GetBoundary(canonical) 失败: {exc}")

    # 2) 用「样例里那种不带 Cf8/版本后缀」的标签行不行
    try:
        b2 = shapes.GetBoundary(EDGE_NAME)
        t, ln, geom = sample_boundary(part, b2)
        out(f"  Shapes.GetBoundary(简化标签) -> {t}  Length={ln}  采样: {geom}")
    except Exception as exc:
        out(f"  Shapes.GetBoundary(简化标签) 失败: {exc}")

    # 3) 替换 sketch 元素序号，看能否构造出其它边线（对称候选）
    import re

    out("  标签变体（把 sketch 元素序号换成别的）:")
    seen: set[str] = set()
    for a in range(1, 5):
        for c in range(1, 5):
            if a >= c:
                continue
            variant = re.sub(r"Sketch\.(\d+);(\d+)", lambda m: f"Sketch.{m.group(1)};{a}", label)
            variant2 = re.sub(r"Sketch\.(\d+);(\d+)", lambda m: f"Sketch.{m.group(1)};{c}", variant)
            key = f"{a}-{c}"
            if key in seen:
                continue
            seen.add(key)
            try:
                bv = shapes.GetBoundary(variant2)
                t, ln, geom = sample_boundary(part, bv)
                out(f"    序号({a},{c}) -> {t} Length={ln} 采样={geom}")
            except Exception as exc:
                out(f"    序号({a},{c}) -> 失败: {exc}")

    # 4) 面标签 → 面的边界边线
    out("  面标签探测:")
    face_ref = part.CreateReferenceFromBRepName(FACE_NAME, pad)
    sel.Clear()
    sel.Add(face_ref)
    face_label = sel.Item2(1).Reference.DisplayName
    out(f"    面 canonical label: {face_label}")
    try:
        bf = shapes.GetBoundary(face_label)
        t, ln, geom = sample_boundary(part, bf)
        out(f"    Shapes.GetBoundary(面) -> {t} Length={ln} 采样={geom}")
    except Exception as exc:
        out(f"    Shapes.GetBoundary(面) 失败: {exc}")
    # 顶面 = Pad.1;2 → 顶面边线应当是 side face(Pad.1;0;(Sketch.1;k)) ∩ top face(Pad.1;2)
    out("    顶面边线候选（side face ∩ top face）:")
    for k in range(1, 5):
        cand = (
            "REdge:(Edge:(Face:(Brp:(Pad.1;0:(Brp:(Sketch.1;%d)));None:());"
            "Face:(Brp:(Pad.1;2);None:());None:(Limits1:();Limits2:()));"
            "WithTemporaryBody;WithoutBuildError;WithSelectingFeatureSupport)" % k
        )
        try:
            bc = shapes.GetBoundary(cand)
            t, ln, geom = sample_boundary(part, bc)
            out(f"      k={k} -> {t} Length={ln} 采样={geom}")
        except Exception as exc:
            out(f"      k={k} -> 失败: {exc}")
    sel.Clear()


def probe_geometry2(app, part, pad) -> None:
    hr("[8] 几何第二方案：Extract + Update + Measurable 采样")
    spa = part.Parent.GetWorkbench("SPAWorkbench")
    hsf = part.HybridShapeFactory
    edge_ref = part.CreateReferenceFromBRepName(EDGE_NAME, pad)
    face_ref = part.CreateReferenceFromBRepName(FACE_NAME, pad)

    try:
        m0 = spa.GetMeasurable(part.CreateReferenceFromObject(pad))
        out(f"  Pad 特征: Volume={safe(lambda: m0.Volume)} Area={safe(lambda: m0.Area)}")
    except Exception as exc:
        out(f"  Pad 特征 measurable 失败: {exc}")

    gs = part.HybridBodies.Add()
    gs.Name = "Probe_Temp"
    made: list[tuple[str, Any]] = []
    for label, ref in (("边线", edge_ref), ("面", face_ref)):
        try:
            ex = hsf.AddNewExtract(ref)
            try:
                ex.PropagationType = 2  # 相切连续
            except Exception:
                pass
            try:
                ex.IsSuperMode = True
            except Exception:
                pass
            gs.AppendHybridShape(ex)
            made.append((label, ex))
            out(f"  {label} Extract 已加入临时 Set")
        except Exception as exc:
            out(f"  {label} Extract 失败: {exc}")

    if made:
        try:
            part.Update()
            out("  part.Update() 成功")
        except Exception as exc:
            out(f"  part.Update() 失败: {exc}")

    for label, ex in made:
        try:
            ref = part.CreateReferenceFromObject(ex)
            m = spa.GetMeasurable(ref)
            out(f"  [{label} Extract] Length={safe(lambda: m.Length)} Area={safe(lambda: m.Area)}")
            pts = safe(lambda: m.GetPointsOnCurve())
            if isinstance(pts, tuple) and len(pts) == 2 and len(pts[0]) > 1:
                coords = pts[0]
                head = ",".join(f"{c:.2f}" for c in coords[0])
                tail = ",".join(f"{c:.2f}" for c in coords[-1])
                out(f"      {len(coords)} 个采样点: [{head}] -> [{tail}]")
                out(f"      全部: {[tuple(round(c,2) for c in p) for p in coords][:8]}")
            else:
                out(f"      GetPointsOnCurve -> {pts}")
        except Exception as exc:
            out(f"  [{label} Extract] measurable 失败: {exc}")


def probe_command(app, part, pad) -> None:
    hr("[6] 交互命令探测：能否启动 EdgeFillet，宏能否把元素塞进命令")
    sel = part.Parent.Selection
    before = {(hwnd, cls, title) for hwnd, _pid, cls, title in catia_windows()}
    shapes_before = safe(lambda: part.MainBody.Shapes.Count)
    out(f"  命令前 Body.Shapes.Count = {shapes_before}")

    for cmd in CMD_IDS:
        opened = None
        try:
            app.StartCommand(cmd)
        except Exception as exc:
            out(f"  StartCommand('{cmd}') 失败: {exc}")
            continue
        time.sleep(1.5)
        after = catia_windows()
        new = [w for w in after if (w[0], w[2], w[3]) not in before]
        out(f"  StartCommand('{cmd}') -> 新增窗口 {len(new)} 个")
        for hwnd, pid, cls, title in new:
            out(f"      hwnd={hwnd:<10} class={cls:<30} title={title!r}")
        if new:
            opened = new[0][0]

        if opened:
            edge_ref = part.CreateReferenceFromBRepName(EDGE_NAME, pad)
            sel.Clear()
            time.sleep(0.3)
            try:
                sel.Add(edge_ref)
                out(f"      [命令中] 宏加入边线后 Count2={sel.Count2}")
            except Exception as exc:
                out(f"      [命令中] 宏加入边线失败: {exc}")

            childs = enum_children_deep(opened)
            out(f"      对话框子控件 {len(childs)} 个（前 25 个）：")
            for c, cls, txt in childs[:25]:
                out(f"        {c:<10} {cls:<32} {txt!r}")

            clicked = click_child_button(opened, ("OK", "Ok", "确定", "确 定"))
            out(f"      点击 OK 按钮: {clicked}")
            time.sleep(1.5)
            shapes_after = safe(lambda: part.MainBody.Shapes.Count)
            out(f"      命令后 Body.Shapes.Count = {shapes_after}（前值 {shapes_before}）")
            if shapes_after != shapes_before:
                out("      => 宏加入的选择被命令接受，倒圆角已生成 ✔")
            else:
                out("      => 未见新特征（可能 OK 未生效 / 命令未接受宏选择）")
            # 若对话框还在，关掉它，别留给用户
            still = [(h, c, t) for h, _p, c, t in catia_windows() if h == opened]
            if still:
                out(f"      清理：关闭残留对话框 hwnd={opened}")
                win32gui.PostMessage(opened, win32con.WM_CLOSE, 0, 0)
                time.sleep(0.8)
            cleanup_new_windows(before)
            return
    out("  所有候选命令 ID 都未能打开对话框，需要换 ID 再试")
    cleanup_new_windows(before)


def cleanup_new_windows(before: set[tuple[int, str, str]]) -> None:
    """关闭本次实验新出现的所有 CATIA 顶层窗口，避免把模态框留给用户。"""
    time.sleep(0.4)
    for hwnd, _pid, cls, title in catia_windows():
        if (hwnd, cls, title) in before:
            continue
        if cls.startswith("Afx:") and "CATIA V5" in title:
            continue  # 主窗口
        out(f"  [清理] 关闭新窗口 class={cls} title={title!r}")
        try:
            win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
        except Exception as exc:
            out(f"  [清理] 失败: {exc}")
    time.sleep(0.5)


# --------------------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="CATIA V5 自动化能力探测")
    parser.add_argument(
        "what",
        choices=["info", "search", "classify", "geometry", "geometry2", "labels", "windows", "command", "all"],
        help="要探测的项目",
    )
    parser.add_argument("--start", action="store_true", help="CATIA 未运行时自动启动")
    parser.add_argument("--keep", action="store_true", help="保留临时文档不关闭（排错用）")
    args = parser.parse_args(argv)

    app = attach(args.start)
    # 关文档时不要让 CATIA 弹「是否保存」，避免把模态框留给用户
    prev_alerts = safe(lambda: app.DisplayFileAlerts)
    try:
        app.DisplayFileAlerts = False
    except Exception:
        pass
    before_docs = open_doc_names(app)
    doc = None
    try:
        probe_info(app)
        if args.what == "windows":
            probe_windows(app)
            return 0
        if args.what == "info":
            return 0

        doc, part, pad = make_test_document(app)
        if args.what in ("search", "all"):
            probe_search(app, part, pad)
        if args.what in ("classify", "all"):
            probe_classify(app, part, pad)
        if args.what in ("geometry", "all"):
            probe_geometry(app, part, pad)
        if args.what in ("geometry2", "all"):
            probe_geometry2(app, part, pad)
        if args.what in ("labels", "all"):
            probe_labels(app, part, pad)
        if args.what in ("windows", "all"):
            probe_windows(app)
        if args.what in ("command", "all"):
            probe_command(app, part, pad)
        return 0
    except Exception:
        traceback.print_exc()
        return 1
    finally:
        if doc is not None and not args.keep:
            try:
                name = doc.Name
                if name not in before_docs:
                    doc.Close()
                    out(f"[清理] 已关闭临时文档 {name}（未保存）")
                else:  # pragma: no cover - 理论上不会发生
                    out(f"[清理] 跳过关闭 {name}：它在我们开始前就存在")
            except Exception as exc:
                out(f"[清理] 关闭临时文档失败，请手动关闭: {exc}")
        if prev_alerts is not None:
            try:
                app.DisplayFileAlerts = prev_alerts
            except Exception:
                pass
        out(f"[清理] 收尾时文档列表: {open_doc_names(app)}")


if __name__ == "__main__":
    raise SystemExit(main())

