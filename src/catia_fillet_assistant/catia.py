"""CATIA 侧封装（comtypes 实现）。

为什么用 comtypes 而不是 pywin32：
    V5R20 的几何读取接口（GetAbsoluteAxisData / GetCoordinates / GetParamExtents /
    GetPointAtParam）都是 [in,out] SAFEARRAY(VARIANT) 参数，pywin32 传不进去也拿不回值；
    comtypes 用 _midlSAFEARRAY(VARIANT) 缓冲区可以正常读写（本机实测通过）。

导航（Document→Part、集合取成员）用 comtypes 的 dynamic 包装，避免基接口缺少派生属性；
需要调用带 out 参数的几何接口时再 QueryInterface 成具体接口。
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import comtypes.client as cc
from comtypes.automation import BSTR, VARIANT
from comtypes.client import dynamic
from comtypes.safearray import _midlSAFEARRAY

from . import labels as lbl
from . import paths
from .geometry import SketchElement, SketchFrame, V3

log = logging.getLogger(__name__)

PROG_ID = "CATIA.Application"

# Edge Fillet 命令头（V5R20 中文版实测：英文带空格可用）
FILLET_COMMAND_IDS = ("Edge Fillet", "EdgeFillet", "Fillet")
SPA_WORKBENCH = "SPAWorkbench"

_TLB = {
    "mec": "MecModTypeLib.tlb",
    "inf": "InfTypeLib.tlb",
    "spa": "SPATypeLib.tlb",
}

# 必需/可选类型库（spa 缺失时，重心退回"草图包围盒中心"，其余功能不受影响）
REQUIRED_TLB = ("InfTypeLib.tlb", "MecModTypeLib.tlb")
OPTIONAL_TLB = ("SPATypeLib.tlb",)

_GEN_DIR_READY = False


def ensure_comtypes_gen_dir() -> Path:
    """冻结 exe 里 comtypes 包目录是只读的，必须把生成模块重定向到可写目录。"""
    global _GEN_DIR_READY
    target = paths.gen_dir()
    if not _GEN_DIR_READY:
        try:
            cc.gen_dir = str(target)
        except Exception as exc:  # pragma: no cover
            log.warning("设置 comtypes gen 目录失败: %s", exc)
        _GEN_DIR_READY = True
    return target


class CatiaNotRunning(RuntimeError):
    pass


class CatiaComError(RuntimeError):
    pass


def com_ok(fn, default=None):
    """执行 COM 调用，失败返回 default。"""
    try:
        return fn()
    except Exception as exc:
        log.debug("COM 调用失败: %s", exc)
        return default


def _verify_bin(path: Path) -> bool:
    return bool(path) and path.is_dir() and all((path / name).exists() for name in REQUIRED_TLB)


def _parse_server_path(value: str) -> Path | None:
    """把注册表里的 `"D:\\...\\CNEXT.exe" -env ...` 解析成 exe 路径。"""
    text = (value or "").strip()
    if text.startswith('"'):
        end = text.find('"', 1)
        if end > 1:
            candidate = Path(text[1:end])
            if candidate.exists():
                return candidate
    candidate = Path(text.split(" ", 1)[0].strip('"'))
    return candidate if candidate.exists() else None


def _registry_catia_bin() -> Path | None:
    """注册表定位（兼容 32/64 位视图与 HKCU）。"""
    import winreg

    views = [0, winreg.KEY_WOW64_32KEY, winreg.KEY_WOW64_64KEY]
    roots = (
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Classes"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Classes"),
        (winreg.HKEY_CURRENT_USER, r"Software\Classes"),
    )
    for hive, base in roots:
        for view in views:
            try:
                with winreg.OpenKey(
                    hive, base + r"\CATIA.Application\CLSID", 0, winreg.KEY_READ | view
                ) as key:
                    clsid = winreg.QueryValueEx(key, "")[0]
                with winreg.OpenKey(
                    hive, base + "\\CLSID\\" + clsid + r"\LocalServer32", 0, winreg.KEY_READ | view
                ) as key:
                    server = winreg.QueryValueEx(key, "")[0]
            except OSError:
                continue
            exe = _parse_server_path(server)
            if exe is not None and _verify_bin(exe.parent):
                return exe.parent
    return None


def _scan_catia_bin() -> Path | None:
    """最后手段：在常见位置按有限深度搜 win_b64\\code\\bin。"""
    roots = [
        Path("C:/Program Files"),
        Path("C:/Program Files (x86)"),
        Path("D:/Programs"),
        Path("D:/"),
        Path("E:/"),
        Path("F:/"),
    ]
    patterns = ("*/win_b64/code/bin", "*/*/win_b64/code/bin", "*/*/*/win_b64/code/bin")
    for root in roots:
        if not root.exists():
            continue
        for pattern in patterns:
            try:
                for cand in root.glob(pattern):
                    if _verify_bin(cand):
                        log.info("扫描到 CATIA 类型库目录: %s", cand)
                        return cand
            except Exception:
                continue
    return None


def find_catia_bin(config_bin: str | Path | None = None) -> Path:
    """定位 CATIA 的 code\\bin 目录（类型库所在位置）。

    顺序：配置/参数 → 环境变量 CATIA_BIN → 注册表 → 常见路径扫描。
    """
    for candidate in (config_bin, os.environ.get("CATIA_BIN")):
        if candidate:
            path = Path(str(candidate))
            if _verify_bin(path):
                return path
            log.warning("指定的 CATIA 目录不完整（缺 %s）: %s", "/".join(REQUIRED_TLB), path)
    registry = _registry_catia_bin()
    if registry is not None:
        return registry
    scanned = _scan_catia_bin()
    if scanned is not None:
        return scanned
    raise CatiaComError(
        "找不到 CATIA 类型库目录：请在 config.json 里设置 catia_bin"
        r'（例如 "D:\Programs\CATIA\win_b64\code\bin"），或设置环境变量 CATIA_BIN。'
    )


@dataclass
class SelectionItem:
    index: int
    type_name: str
    value_type: str
    display_name: str
    label: lbl.LabelInfo | None
    coordinates: tuple[float, float, float] | None
    raw: Any = None

    @property
    def is_edge(self) -> bool:
        if self.label is not None and self.label.is_edge:
            return True
        # 手动拾取的边线在 automation 里是 TriDimFeatEdge 之类的对象
        return "edge" in (self.value_type or "").lower()

    @property
    def is_face(self) -> bool:
        if self.label is not None and self.label.is_face:
            return True
        vt = (self.value_type or "").lower()
        return "face" in vt or "surface" in vt or vt.endswith("sur")

    def describe(self) -> str:
        kind = "边线" if self.is_edge else ("面" if self.is_face else "未知")
        return f"#{self.index} {kind} [{self.type_name}/{self.value_type}] {self.display_name[:64]}"


class CatiaSession:
    """当前 CATIA 会话的封装，支持自动重连。"""

    def __init__(self, catia_bin: str | Path | None = None) -> None:
        ensure_comtypes_gen_dir()
        self._bin: Path | None = Path(catia_bin) if catia_bin else None
        self._mods: dict[str, Any] = {}

    # -------------------------------------------------------------- 类型库
    @property
    def bin(self) -> Path:
        if self._bin is None:
            self._bin = find_catia_bin()
        return self._bin

    def mod(self, name: str) -> Any:
        if name not in self._mods:
            self._mods[name] = cc.GetModule(str(self.bin / _TLB[name]))
        return self._mods[name]

    # ---------------------------------------------------------------- 连接
    def connect(self) -> Any:
        try:
            return cc.GetActiveObject(PROG_ID)
        except Exception as exc:
            raise CatiaNotRunning("CATIA 没在运行，请先打开 CATIA V5") from exc

    def alive(self) -> bool:
        try:
            _ = self.connect().Documents.Count
            return True
        except Exception:
            return False

    @staticmethod
    def dyn(obj: Any) -> Any:
        """把具体接口对象包成动态派发对象（属性导航用）。"""
        return dynamic.Dispatch(obj)

    # ------------------------------------------------------------ 文档/部件
    def active_document(self) -> Any | None:
        return com_ok(lambda: self.connect().ActiveDocument)

    def active_part(self) -> Any | None:
        doc = self.active_document()
        if doc is None:
            return None
        pd = com_ok(lambda: doc.QueryInterface(self.mod("mec").PartDocument))
        if pd is None:
            return None
        return com_ok(lambda: pd.Part)

    def workbench(self) -> Any | None:
        doc = self.active_document()
        if doc is None:
            return None
        wb = com_ok(lambda: doc.GetWorkbench(BSTR(SPA_WORKBENCH)))
        if wb is None:
            return None
        # 通用 Workbench 接口上没有 GetMeasurable，需要转成 SPAWorkbench 扩展接口
        return com_ok(lambda: wb.QueryInterface(self.mod("spa").SPAWorkbench)) or wb

    def part_cog(self, part: Any) -> V3 | None:
        """零件重心，用来推断对称中心面。"""
        wb = self.workbench()
        if wb is None:
            return None
        ref = com_ok(lambda: part.CreateReferenceFromObject(part.MainBody))
        m = com_ok(lambda: wb.GetMeasurable(ref if ref is not None else part.MainBody))
        if m is None:
            return None
        cog = com_ok(lambda: m.GetCOG(self._buf(3)))
        if isinstance(cog, (tuple, list)) and len(cog) >= 3:
            return V3(float(cog[0]), float(cog[1]), float(cog[2]))
        return None

    def curve_length(self, reference: Any) -> float | None:
        wb = self.workbench()
        if wb is None:
            return None
        m = com_ok(lambda: wb.GetMeasurable(reference))
        if m is None:
            return None
        val = com_ok(lambda: m.Length)
        return float(val) if isinstance(val, (int, float)) else None

    def volume_of(self, part: Any) -> float | None:
        """零件（MainBody）体积，用来做"倒圆角是否真的生效"这类验收。"""
        wb = self.workbench()
        if wb is None:
            return None
        for target in (lambda: part.CreateReferenceFromObject(part.MainBody), lambda: part.MainBody):
            ref = com_ok(target)
            if ref is None:
                continue
            m = com_ok(lambda: wb.GetMeasurable(ref))
            if m is None:
                continue
            value = com_ok(lambda: m.Volume)
            if isinstance(value, (int, float)):
                return float(value)
        return None

    # ---------------------------------------------------------- 数组缓冲区
    @staticmethod
    def _buf(n: int):
        """构造 [in,out] SAFEARRAY(VARIANT) 缓冲区。"""
        lp = _midlSAFEARRAY(VARIANT)
        return lp.create([0.0] * n)

    # ---------------------------------------------------------------- 选择
    def _selection(self) -> Any | None:
        doc = self.active_document()
        if doc is None:
            return None
        return com_ok(lambda: doc.Selection)

    def selection(self) -> Any | None:
        """公开的选择集入口（形状级对称扫描边线要用；只读写选择集，不动模型）。"""
        return self._selection()

    def selection_count(self) -> int:
        sel = self._selection()
        if sel is None:
            return 0
        return int(com_ok(lambda: sel.Count2, 0) or 0)

    def selection_items(self) -> list[SelectionItem]:
        sel = self._selection()
        if sel is None:
            return []
        items: list[SelectionItem] = []
        for i in range(1, self.selection_count() + 1):
            element = com_ok(lambda i=i: sel.Item2(i))
            if element is None:
                continue
            value = com_ok(lambda: element.Value)
            ref = com_ok(lambda: element.Reference)
            display = com_ok(lambda: ref.DisplayName) if ref is not None else None
            if not display:
                display = com_ok(lambda: self.dyn(value).DisplayName, "") or ""
            coords = com_ok(lambda: element.GetCoordinates(self._buf(3)))
            items.append(
                SelectionItem(
                    index=i,
                    type_name=str(com_ok(lambda: element.Type, "?")),
                    value_type=self._type_name(value),
                    display_name=str(display or ""),
                    label=lbl.parse_label(str(display)) if display else None,
                    coordinates=tuple(float(c) for c in coords[:3])
                    if isinstance(coords, (tuple, list)) and len(coords) >= 3
                    else None,
                    raw=element,
                )
            )
        return items

    def selection_all_edges(self) -> bool:
        sel = self._selection()
        if sel is None:
            return False
        if self.selection_count() == 0:
            return False  # 空选择集时 FilterCorrespondence 会返回 True，别被它骗了
        return bool(com_ok(lambda: sel.FilterCorrespondence(["TriDimFeatEdge"])))

    def selection_references(self) -> list[Any]:
        """当前选择集里每个元素的 Reference（做倒圆角时要用）。"""
        sel = self._selection()
        if sel is None:
            return []
        refs: list[Any] = []
        for i in range(1, self.selection_count() + 1):
            element = com_ok(lambda i=i: sel.Item2(i))
            if element is None:
                continue
            ref = com_ok(lambda: element.Reference)
            if ref is None:
                ref = com_ok(lambda: element.Value)
            if ref is not None:
                refs.append(ref)
        return refs

    def clear_selection(self) -> None:
        sel = self._selection()
        if sel is not None:
            com_ok(lambda: sel.Clear())

    def remove_selection_index(self, index: int) -> None:
        sel = self._selection()
        if sel is None:
            return
        if com_ok(lambda: sel.Remove2(index)) is None:
            com_ok(lambda: sel.Remove(index))

    def add_to_selection(self, obj: Any) -> bool:
        sel = self._selection()
        if sel is None:
            return False
        candidates: list[Any] = []
        dyn_obj = com_ok(lambda: self.dyn(obj))
        if dyn_obj is not None:
            candidates.append(dyn_obj)
        candidates.append(obj)
        for cand in candidates:
            try:
                sel.Add(cand)
                return True
            except Exception as exc:
                log.debug("Selection.Add 失败(%s): %s", type(cand), exc)
        log.warning("加入选择集失败")
        return False

    # ------------------------------------------------------------ 标签→对象
    def object_by_name(self, part: Any, name: str) -> Any | None:
        obj = com_ok(lambda: part.FindObjectByName(BSTR(name)))
        if obj is not None:
            return obj
        dyn_part = com_ok(lambda: self.dyn(part))
        if dyn_part is None:
            return None
        return com_ok(lambda: dyn_part.FindObjectByName(name))

    def reference_from_label(self, part: Any, label: str, context: Any | None = None) -> Any | None:
        """GenericNaming 标签 → Reference。"""
        ctx = context if context is not None else com_ok(lambda: part.MainBody)
        if ctx is None:
            return None
        dyn_ctx = com_ok(lambda: self.dyn(ctx))
        for cand_ctx in (dyn_ctx, ctx):
            if cand_ctx is None:
                continue
            try:
                return part.CreateReferenceFromBRepName(BSTR(label), cand_ctx)
            except Exception as exc:
                log.debug("CreateReferenceFromBRepName 失败: %s", exc)
        return None

    # ---------------------------------------------------------------- 草图
    def read_sketch_frame(self, sketch: Any) -> SketchFrame | None:
        sk = com_ok(lambda: sketch.QueryInterface(self.mod("mec").Sketch))
        if sk is None:
            return None
        data = com_ok(lambda: sk.GetAbsoluteAxisData(self._buf(9)))
        if not isinstance(data, (tuple, list)) or len(data) < 9:
            return None
        origin = V3(float(data[0]), float(data[1]), float(data[2]))
        u = V3(float(data[3]), float(data[4]), float(data[5]))
        v = V3(float(data[6]), float(data[7]), float(data[8]))
        w = u.cross(v)
        if w.norm() <= 1e-12:
            w = V3(0.0, 0.0, 1.0)
        return SketchFrame(origin=origin, u=u.unit(), v=v.unit(), w=w.unit())

    def read_sketch_elements(self, sketch: Any, samples: int = 9) -> list[SketchElement]:
        """读取草图元素并采样。

        元素编号用**名字里的序号**（直线.4 → 4），这样与 BRep 标签 Sketch.1;4 对齐；
        草图里还有构造点/构造线（名字是 点.N），它们不参与候选，也不影响编号。
        """
        sk = com_ok(lambda: sketch.QueryInterface(self.mod("mec").Sketch))
        if sk is None:
            return []
        geoms = com_ok(lambda: sk.GeometricElements)
        if geoms is None:
            return []
        count = int(com_ok(lambda: geoms.Count, 0) or 0)
        out: list[SketchElement] = []
        sequential = 0
        for i in range(1, count + 1):
            el = com_ok(lambda i=i: geoms.Item(i))
            if el is None:
                continue
            geo_type = int(com_ok(lambda: el.GeometricType, 0) or 0)
            name = str(com_ok(lambda: el.Name, "") or "")
            if geo_type == 1 or name.startswith("绝对轴"):
                continue  # 绝对轴不参与元素编号
            sequential += 1
            number = re.search(r"\.(\d+)\s*$", name)
            element_index = int(number.group(1)) if number else sequential
            if geo_type == 2:
                continue  # 构造点不生成边，跳过（但已计入编号参考）
            pts = self._sample_element(el, geo_type, samples)
            if not pts:
                log.debug("草图元素 %s（类型 %d）采样失败，跳过", name, geo_type)
                continue
            kind = "other"
            center = None
            radius = None
            if geo_type == 3:
                kind = "line"
                pts = [pts[0], pts[-1]]
            elif geo_type == 5:
                kind = "circle"
                typed = com_ok(lambda: el.QueryInterface(self.mod("mec").Circle2D)) or el
                cp = com_ok(lambda: typed.CenterPoint)
                if cp is not None:
                    center = com_ok(lambda: cp.GetCoordinates(self._buf(2)))
                r = com_ok(lambda: typed.Radius)
                if isinstance(r, (int, float)):
                    radius = float(r)
            elif geo_type == 2:
                kind = "point"
                pts = pts[:1]
            out.append(
                SketchElement(
                    index=element_index,
                    kind=kind,
                    points=[(float(p[0]), float(p[1])) for p in pts],
                    center=(float(center[0]), float(center[1])) if center else None,
                    radius=radius,
                )
            )
        return out

    def _sample_element(self, el: Any, geo_type: int, samples: int) -> list[tuple[float, float]]:
        typed = el
        if geo_type == 3:
            typed = com_ok(lambda: el.QueryInterface(self.mod("mec").Line2D)) or el
        elif geo_type == 5:
            typed = com_ok(lambda: el.QueryInterface(self.mod("mec").Circle2D)) or el
        elif geo_type == 2:
            typed = com_ok(lambda: el.QueryInterface(self.mod("mec").Point2D)) or el
        ext = com_ok(lambda: typed.GetParamExtents(self._buf(2)))
        if isinstance(ext, (tuple, list)) and len(ext) >= 2:
            p0, p1 = float(ext[0]), float(ext[1])
            n = max(2, samples)
            pts: list[tuple[float, float]] = []
            for k in range(n):
                t = p0 + (p1 - p0) * (k / (n - 1))
                coord = com_ok(lambda t=t: typed.GetPointAtParam(t, self._buf(2)))
                if isinstance(coord, (tuple, list)) and len(coord) >= 2:
                    pts.append((float(coord[0]), float(coord[1])))
            if pts:
                return pts
        if geo_type == 3:
            sp = com_ok(lambda: typed.StartPoint.GetCoordinates(self._buf(2)))
            ep = com_ok(lambda: typed.EndPoint.GetCoordinates(self._buf(2)))
            if sp and ep:
                return [(float(sp[0]), float(sp[1])), (float(ep[0]), float(ep[1]))]
            # 再兜一层：有些元素走 typed 接口读不出来，用动态派发再试
            dyn = com_ok(lambda: self.dyn(el))
            if dyn is not None:
                sp2 = com_ok(lambda: dyn.StartPoint.GetCoordinates(self._buf(2)))
                ep2 = com_ok(lambda: dyn.EndPoint.GetCoordinates(self._buf(2)))
                if sp2 and ep2:
                    return [(float(sp2[0]), float(sp2[1])), (float(ep2[0]), float(ep2[1]))]
        return []

    @staticmethod
    def _type_name(obj: Any) -> str:
        if obj is None:
            return "None"
        try:
            return type(obj).__name__
        except Exception:
            return "?"


def iter_selection_labels(session: CatiaSession) -> Iterable[str]:
    for item in session.selection_items():
        yield item.display_name
