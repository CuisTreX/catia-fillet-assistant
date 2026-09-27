"""主程序：把监视、提示、热键、对称配对串起来。"""

from __future__ import annotations

import argparse
import ctypes
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from functools import partial
from pathlib import Path

from . import pairing, paths, shape_engine, shape_symmetry, win32util
from .cellgeom import CellIndex
from .edge_scan import EdgeScanner
from .catia import (
    OPTIONAL_TLB,
    REQUIRED_TLB,
    CatiaNotRunning,
    CatiaSession,
    ensure_comtypes_gen_dir,
    find_catia_bin,
)
from . import __version__
from .config import Config
from .geometry import V3
from .hotkeys import InputListener
from .toast import ToastUI
from .watcher import FilletWatcher, WatchState

log = logging.getLogger(__name__)

_SINGLE_INSTANCE_HANDLE = None
MUTEX_NAME = "CatiaFilletAssistant_SingleInstance"


def single_instance_guard(name: str = MUTEX_NAME) -> bool:
    """防止重复启动（双份热键/双份鼠标钩子会把对称边线补两次）。"""
    global _SINGLE_INSTANCE_HANDLE
    try:
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.CreateMutexW(None, False, name)
        if not handle:
            return True  # 拿不到句柄就放行，别因为保护逻辑挡住使用
        if kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
            return False
        _SINGLE_INSTANCE_HANDLE = handle
    except Exception as exc:  # pragma: no cover
        log.debug("单实例保护失败（忽略）: %s", exc)
    return True


def other_instance_running(name: str = MUTEX_NAME) -> bool:
    """探测是否已经有助手在运行（用完立刻关掉自己创建的句柄，不留副作用）。"""
    try:
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.CreateMutexW(None, False, name)
        if not handle:
            return False
        already = kernel32.GetLastError() == 183
        kernel32.CloseHandle(handle)
        return bool(already)
    except Exception:
        return False


def quit_signal_path() -> Path:
    return paths.data_dir() / "quit.signal"


def request_quit(timeout: float = 8.0, force: bool = True) -> tuple[bool, str]:
    """请求正在运行的助手退出：先写"退出信号"文件，超时再 taskkill 兜底。"""
    if not other_instance_running():
        return True, "助手当前没有在运行"
    try:
        quit_signal_path().write_text("quit\n", encoding="utf-8")
    except Exception as exc:
        return False, f"写退出信号失败：{exc}"
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not other_instance_running():
            return True, "已退出"
        time.sleep(0.4)
    if force:
        try:
            subprocess.run(
                ["taskkill", "/F", "/IM", "CatiaFilletAssistant.exe"],
                capture_output=True,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            time.sleep(1.0)
            if not other_instance_running():
                return True, "助手没有响应退出信号，已强制结束进程"
        except Exception as exc:
            return False, f"强制结束失败：{exc}"
    return False, "助手仍在运行（可手动结束进程）"


def with_com(func):
    """热键/气泡回调都在新线程里执行，需要先初始化 COM。"""

    def wrapper(*args, **kwargs):
        try:
            import comtypes

            comtypes.CoInitialize()
        except Exception:
            pass
        return func(*args, **kwargs)

    wrapper.__name__ = getattr(func, "__name__", "action")
    wrapper.__doc__ = func.__doc__
    return wrapper


class FilletAssistant:
    def __init__(self, base_dir: Path, config: Config) -> None:
        self.base_dir = base_dir
        self.config = config
        self.session = CatiaSession(catia_bin=config.catia_bin or None)
        self.cells = CellIndex(self.session)
        # R5 形状级对称：边线扫描器（只读，带 TTL 缓存；生成倒圆角后立刻失效）
        self.shape_scanner = EdgeScanner(
            self.session,
            ttl=float(getattr(config, "shape_cache_seconds", 6.0) or 6.0),
            max_edges=int(getattr(config, "shape_max_edges", 600) or 600),
            restore_selection=bool(getattr(config, "shape_scan_restore_selection", True)),
        )
        self.toast = ToastUI()
        self.stop_event = threading.Event()
        self._last_pair_time = 0.0
        self.watcher = FilletWatcher(
            self.session,
            self.config,
            on_wrong_pick=self.on_wrong_pick,
            on_error=lambda msg: self.toast.show("CATIA 连接异常", msg, seconds=6, level="warn"),
            stop_event=self.stop_event,
            cells=self.cells,
        )
        self.listener = InputListener(
            hotkeys={
                self.config.hotkey_pair: with_com(self.action_pair),
                self.config.hotkey_remove_wrong: with_com(self.action_remove_wrong),
                self.config.hotkey_start_fillet: with_com(self.action_start_fillet),
                self.config.hotkey_quit: self.stop,
            },
            rmb_predicate=self._rmb_predicate,
            rmb_action=with_com(self.action_pair),
            stop_event=self.stop_event,
        )

    # ------------------------------------------------------------- 生命周期
    def start(self) -> None:
        self.toast.start()
        self.toast.wait_ready(2.0)
        self.watcher.start()
        self.listener.start()
        log.info(
            "助手已启动：热键 配对=%s 移除误选=%s 启动倒圆角=%s 退出=%s；右键补对称=%s",
            self.config.hotkey_pair,
            self.config.hotkey_remove_wrong,
            self.config.hotkey_start_fillet,
            self.config.hotkey_quit,
            "开" if self.config.right_click_pairing else "关",
        )
        self.toast.show(
            "CATIA 倒圆角助手已启动",
            f"选到面会立刻提醒；在倒圆角对话框里右击 = 补对称边线；"
            f"热键 {self.config.hotkey_pair.upper()} 也可手动补。",
            seconds=6,
            level="ok",
        )

    def stop(self) -> None:
        if self.stop_event.is_set():
            return
        log.info("正在退出 ...")
        self.stop_event.set()
        try:
            self.listener.stop()
        except Exception:
            pass
        try:
            self.toast.shutdown()
        except Exception:
            pass

    def run(self) -> int:
        self.start()
        try:
            while not self.stop_event.is_set():
                # "退出助手.bat" 会写这个信号文件
                if quit_signal_path().exists():
                    log.info("收到退出信号，正在退出")
                    try:
                        quit_signal_path().unlink()
                    except Exception:
                        pass
                    self.stop()
                    break
                self.stop_event.wait(0.5)
        except KeyboardInterrupt:
            self.stop()
        return 0

    # ----------------------------------------------------------------- 动作
    def on_wrong_pick(self, state: WatchState) -> None:
        if state.dialog_non_edge and not state.bad_items:
            self.toast.show(
                "⚠ 倒圆角里选到了面/其它元素",
                "对话框里当前是：\n" + "\n".join(state.dialog_entries)
                + "\n\n倒圆角需要【边线】。请重新点一条边线；如果想连同对称边一起倒圆角，"
                "按鼠标右键或 Ctrl+Alt+R。",
                level="warn",
            )
            return
        names = "；".join(it.describe() for it in state.bad_items[:3])
        extra = "" if len(state.bad_items) <= 3 else f"（共 {len(state.bad_items)} 个）"
        message = (
            f"倒圆角需要【边线】，你选中了【面/其它元素】：\n{names}{extra}\n"
            "建议：移除误选后重新点边线，或用右键/热键补对称边线。"
        )
        if self.config.auto_remove_wrong_pick:
            self._remove_items(state.bad_items)
            self.toast.show("已自动移除误选", message, seconds=5, level="warn")
            return
        self.toast.show(
            "⚠ 选到面了，不是边线",
            message,
            buttons=[("移除误选", partial(self._remove_items, state.bad_items))],
            level="warn",
        )

    def action_pair(self) -> None:
        """补对称边线：优先用「倒圆角对话框里点选的那条边」，其次用文档选择集里的边线。"""
        # 防抖：右键/热键连点时不重复执行（避免刷屏、避免把上一条提示顶掉）
        now = time.time()
        if now - self._last_pair_time < 1.5:
            log.debug("忽略重复触发（距上次 %.2f 秒）", now - self._last_pair_time)
            return
        self._last_pair_time = now
        log.info("动作：补对称边线（右键或 Ctrl+Alt+R）")
        part = self.session.active_part()
        if part is None:
            log.info("动作结果：当前文档不是 CATPart")
            self.toast.show("没有零件文档", "请先打开 CATPart（倒圆角只能在零件里做）。", seconds=5, level="warn")
            return

        dialog = win32util.find_fillet_dialog(self.config.extra_dialog_titles)
        entries = win32util.fillet_dialog_entries(dialog.hwnd) if dialog is not None else []
        log.info(
            "动作上下文：对话框=%s 对话框列表=%s 文档选择集=%s",
            dialog.title if dialog else "未打开",
            entries,
            [it.describe() for it in self.session.selection_items()],
        )
        # 最可靠、也最省特征的做法：你在模型里选好**两条边**（原来的 + 对称的），
        # 直接做成**一个**倒圆角。完全不依赖编号推算，绝不会落到别的边上。
        selected = self.session.selection_items()
        edge_items = [it for it in selected if it.is_edge]
        max_manual = max(2, int(getattr(self.config, "shape_manual_fillet_max_edges", 4) or 4))
        if len(edge_items) >= 2 and len(edge_items) <= max_manual:
            references = []
            for item in edge_items:
                for attr in ("Value", "Reference"):
                    candidate_ref = getattr(item.raw, attr, None)
                    if candidate_ref is not None:
                        references.append(candidate_ref)
                        break
            if references:
                radius = float(self.config.fillet_radius)
                if dialog is not None:
                    from_dialog = win32util.fillet_dialog_radius(dialog.hwnd)
                    if from_dialog:
                        radius = from_dialog
                    if self.config.close_dialog_before_fillet:
                        import win32con
                        import win32gui

                        win32gui.PostMessage(dialog.hwnd, win32con.WM_CLOSE, 0, 0)
                        time.sleep(0.8)
                ok, message = pairing.create_edge_fillet(
                    self.session,
                    part,
                    references,
                    radius=radius,
                    propagation=self.config.fillet_propagation,
                )
                log.info(
                    "两边点选 → 生成结果：ok=%s 边数=%d 半径=%s 说明=%s",
                    ok, len(references), radius, message,
                )
                self.toast.show(
                    "已生成倒圆角" if ok else "生成失败",
                    f"用你选中的 {len(references)} 条边生成一个倒圆角（半径 {radius:g}）。\n{message}"
                    if ok
                    else message,
                    seconds=8,
                    level="ok" if ok else "warn",
                )
                return
        if entries:
            try:
                self._pair_from_dialog(part, entries[0])
            except Exception as exc:
                log.exception("对话框配对失败")
                self.toast.show(
                    "配对出错",
                    f"处理「{entries[0]}」时出错：{exc}\n请把 logs\\assistant.log 发给开发者。",
                    seconds=8,
                    level="warn",
                )
            return

        try:
            outcome = pairing.pair_selection(self.session, self.config)
        except CatiaNotRunning as exc:
            self.toast.show("CATIA 没连上", str(exc), seconds=5, level="warn")
            return
        except Exception as exc:  # pragma: no cover
            log.exception("配对失败")
            self.toast.show("配对失败", str(exc), seconds=6, level="warn")
            return

        if outcome.needs_choice:
            buttons = [
                (label, partial(self._pair_with, label)) for label in list(outcome.groups.keys())[:4]
            ]
            self.toast.show(
                "选择对称方式",
                "这条边线有不止一种对称解释，点一下用哪一种：",
                buttons=buttons,
                level="info",
            )
            return
        log.info("动作结果：%s", outcome.message)
        # 草图引擎没结果时，交给形状级对称引擎（R5/R14）
        if not outcome.added and self.config.shape_engine_enabled and len(edge_items) == 1:
            self._shape_pair_from_selection(part, edge_items[0])
            return
        if outcome.added and str(self.config.symmetric_action).lower() != "select":
            ok, message = self._create_fillet_from_selection(part)
            self.toast.show(
                "已生成对称倒圆角" if ok else "生成倒圆角失败",
                message,
                seconds=4 if ok else 8,
                level="ok" if ok else "warn",
            )
            return
        self.toast.show(
            "对称边线",
            outcome.message,
            seconds=4 if outcome.added else 6,
            level="ok" if outcome.added else "warn",
        )

    def _create_fillet_from_selection(self, part) -> tuple[bool, str]:
        """把当前选择集里的边线直接做成倒圆角（半径取对话框或配置）。"""
        references = self.session.selection_references()
        if not references:
            return False, "当前选择集里没有可用的边线"
        radius = float(self.config.fillet_radius)
        dialog = win32util.find_fillet_dialog(self.config.extra_dialog_titles)
        if dialog is not None:
            from_dialog = win32util.fillet_dialog_radius(dialog.hwnd)
            if from_dialog:
                radius = from_dialog
            if self.config.close_dialog_before_fillet:
                import time as _time
                import win32con
                import win32gui

                win32gui.PostMessage(dialog.hwnd, win32con.WM_CLOSE, 0, 0)
                _time.sleep(0.6)
        ok, message = pairing.create_edge_fillet(
            self.session,
            part,
            references,
            radius=radius,
            propagation=self.config.fillet_propagation,
        )
        # 特征已生成：把选择集清干净，避免残留高亮让 CATIA 越用越卡
        self.session.clear_selection()
        if ok:
            message = f"半径 {radius:g}，共 {len(references)} 条边。{message}"
        return ok, message

    def _pair_from_dialog(self, part, entry: str) -> None:
        """命令运行中：从对话框列表读到被点选的对象 → 按几何推算对称边线。"""
        cell = self.cells.find_by_name(part, entry)
        feature_local = entry.split("\\", 1)[0].strip() if "\\" in entry else ""
        if cell is None:
            self.toast.show(
                "读到对象但解析不了",
                f"对话框里是「{entry}」，但取不到它的几何。\n"
                "请把 logs\\assistant.log 与 诊断报告.txt 发给开发者排查。",
                seconds=8,
                level="warn",
            )
            return
        if cell.kind != "edge":
            self.toast.show(
                "选到的是面，不是边线",
                f"对话框里是「{entry}」（{cell.describe()}）。\n"
                "对称补线目前只对【边线】生效：请重新点一条边线（或先在倒圆角里取消它）。",
                seconds=8,
                level="warn",
            )
            return
        outcome = pairing.pair_from_source_cell(self.session, part, cell, entry, self.config, self.cells)
        log.info(
            "配对结果：源=%s 对称选项=%s 说明=%s（单元=%s）",
            outcome.source_desc or "(未识别)",
            list(outcome.groups),
            outcome.message or "(无)",
            cell.describe(),
        )
        if outcome.needs_choice:
            options = []
            for label, pairs in outcome.groups.items():
                if not pairs:
                    continue
                target = pairs[0].target
                # 用"位置"描述，比"元素 a-b"好判断：给目标边中点与长度
                try:
                    mid = (
                        sum(p.x for p in target.samples) / len(target.samples),
                        sum(p.y for p in target.samples) / len(target.samples),
                        sum(p.z for p in target.samples) / len(target.samples),
                    )
                    text = f"→ 中点({mid[0]:.0f},{mid[1]:.0f},{mid[2]:.0f}) 长{target.length:.0f}"
                except Exception:
                    text = f"→ {target.describe()}"
                options.append((text, partial(self._pair_from_dialog_with, part, entry, label)))
            self.toast.show(
                "选择对称方式",
                f"这条边（{outcome.source_desc}）有 {len(options)} 种对称解释，点一下用哪一种：",
                buttons=options[:4],
                level="info",
            )
            return
        group = next(iter(outcome.groups.values()), [])
        if not group:
            if self.config.shape_engine_enabled:
                # 草图级找不到（例如圆角/阵列/布尔之后的新边）→ 形状级对称
                self._pair_from_dialog_shape(part, entry, cell)
                return
            self.toast.show("没找到对称边线", outcome.message or "对称方式不匹配", seconds=7, level="warn")
            return
        self._finish_pairing(part, feature_local, group[0].source, group[0].target, group[0].transform.label)

    def _pair_from_dialog_with(self, part, entry: str, transform_label: str) -> None:
        # 这是气泡按钮的回调线程：不要复用别线程的 COM 指针，重新取一次
        part = self.session.active_part() or part
        cell = self.cells.find_by_name(part, entry)
        if cell is None:
            self.toast.show("读不到那条边", f"「{entry}」解析失败，请把日志发给开发者。", seconds=8, level="warn")
            return
        try:
            outcome = pairing.pair_from_source_cell(self.session, part, cell, entry, self.config, self.cells)
            log.info("选择了对称方式「%s」：源=%s 可用=%s", transform_label, outcome.source_desc, list(outcome.groups))
            feature_local = entry.split("\\", 1)[0].strip() if "\\" in entry else ""
            items = outcome.groups.get(transform_label, [])
            if not items:
                self.toast.show("没补上", "该对称方式下没有匹配的边线", seconds=6, level="warn")
                return
            self._finish_pairing(part, feature_local, items[0].source, items[0].target, transform_label)
        except Exception as exc:
            log.exception("生成对称倒圆角失败")
            self.toast.show(
                "生成失败",
                f"出错：{exc}\n请把 logs\\assistant.log 发给开发者。",
                seconds=8,
                level="warn",
            )

    # ----------------------------------------------- R5 形状级对称（新引擎）
    def _shape_pair_from_selection(self, part, item) -> None:
        """用户只选中一条边线（没有倒圆角对话框）时的形状级配对。"""
        reference = None
        for attr in ("Reference", "Value"):
            try:
                reference = getattr(item.raw, attr)
            except Exception:
                reference = None
            if reference is not None:
                break
        if reference is None:
            self.toast.show(
                "读不到那条边",
                "请在模型里重新点一下这条边线，再按热键/右键。",
                seconds=7,
                level="warn",
            )
            return
        try:
            outcome = shape_engine.pair_from_reference(
                self.session, part, reference, self.config, scanner=self.shape_scanner
            )
        except Exception as exc:
            log.exception("形状级配对失败")
            self.toast.show(
                "配对出错",
                f"形状级配对失败：{exc}\n请把 logs\\assistant.log 发给开发者。",
                seconds=8,
                level="warn",
            )
            return
        self._handle_shape_outcome(part, outcome, source=None)

    def _pair_from_dialog_shape(self, part, entry: str, cell) -> None:
        """倒圆角对话框里点了一条边（草图级找不到对称边）→ 形状级配对。"""
        try:
            outcome = shape_engine.pair_from_cell(
                self.session, part, cell, self.config, scanner=self.shape_scanner
            )
        except Exception as exc:
            log.exception("形状级配对失败（对话框）")
            self.toast.show(
                "配对出错",
                f"形状级配对失败：{exc}\n请把 logs\\assistant.log 发给开发者。",
                seconds=8,
                level="warn",
            )
            return
        self._handle_shape_outcome(part, outcome, source=entry)

    def _handle_shape_outcome(self, part, outcome, source=None) -> None:
        """按状态给用户反馈：唯一→直接生成，多解→让人选，找不到→明确说不找。"""
        for line in outcome.log_lines:
            log.info("形状引擎 | %s", line)
        log.info(
            "形状引擎状态：%s（边数=%d 候选=%d）%s",
            outcome.state,
            outcome.edge_count,
            len(outcome.candidates),
            outcome.message,
        )
        if outcome.state == shape_engine.STATE_OK and outcome.candidates:
            if not bool(getattr(self.config, "shape_engine_autocreate", True)):
                self.toast.show("对称边位置（请你点它一下）", outcome.message, seconds=12, level="info")
                return
            self._shape_create_fillet(part, outcome, outcome.candidates[0])
            return
        if outcome.state == shape_engine.STATE_AMBIGUOUS and outcome.candidates:
            if source:
                buttons = [
                    (text, partial(self._shape_choose, source, index))
                    for text, index in shape_engine.describe_candidates(outcome.candidates)
                ]
                self.toast.show(
                    "选择对称边",
                    f"这条边有 {len(outcome.candidates)} 条同样合理的镜像候选，点一下用哪条：",
                    buttons=buttons,
                    level="info",
                )
                return
            lines = "\n".join("· " + m.describe() for m in outcome.candidates[:4])
            self.toast.show(
                "有多条候选（请手动确认）",
                f"这条边有多个镜像候选，请点选其中一条再看：\n{lines}",
                seconds=12,
                level="info",
            )
            return
        if outcome.state == shape_engine.STATE_SELF:
            self.toast.show("这条边在对称面上", outcome.message, seconds=8, level="info")
            return
        self.toast.show(
            "未找到对称边",
            f"{outcome.message}\n（形状级对称：已扫描 {outcome.edge_count} 条边；"
            "如果这个零件本来就是不对称的，这是正常结果）",
            seconds=8,
            level="warn",
        )

    def _shape_choose(self, entry: str, index: int) -> None:
        """气泡按钮回调线程：重新取 COM 对象、重新跑一次配对，不用跨线程的旧引用。"""
        part = self.session.active_part()
        if part is None:
            self.toast.show("没有零件文档", "请先打开 CATPart。", seconds=5, level="warn")
            return
        cell = self.cells.find_by_name(part, entry)
        if cell is None:
            self.toast.show("读不到那条边", f"「{entry}」解析失败。", seconds=7, level="warn")
            return
        outcome = shape_engine.pair_from_cell(
            self.session, part, cell, self.config, scanner=self.shape_scanner
        )
        if index >= len(outcome.candidates):
            self.toast.show("候选已变化", "模型可能已经变了，请重新点一次这条边。", seconds=7, level="warn")
            return
        self._shape_create_fillet(part, outcome, outcome.candidates[index])

    def _shape_create_fillet(self, part, outcome, match) -> None:
        """写模型阶段：先读半径、先关命令，再用扫描到的两条边生成**一个**倒圆角。"""
        outcome.target = match.target
        radius = float(self.config.fillet_radius)
        dialog = win32util.find_fillet_dialog(self.config.extra_dialog_titles)
        if dialog is not None:
            from_dialog = win32util.fillet_dialog_radius(dialog.hwnd)
            if from_dialog:
                radius = from_dialog
            if self.config.close_dialog_before_fillet:
                import win32con
                import win32gui

                win32gui.PostMessage(dialog.hwnd, win32con.WM_CLOSE, 0, 0)
                time.sleep(0.8)
        part = self.session.active_part() or part
        try:
            ok, message = shape_engine.create_fillet(
                self.session,
                part,
                outcome,
                radius=radius,
                propagation=self.config.fillet_propagation,
            )
        finally:
            self.shape_scanner.invalidate()  # 模型已变，边线缓存必须作废
            self.session.clear_selection()   # 扫过的整圈边线不要留在选择集里（高亮会让 CATIA 变卡）
        self.toast.show(
            "已生成对称倒圆角" if ok else "生成倒圆角失败",
            (
                f"{outcome.source.describe()}\n+ {match.describe()}\n"
                f"（形状级对称，半径 {radius:g}）\n{message}"
            )
            if ok
            else message,
            seconds=8,
            level="ok" if ok else "warn",
        )

    def _finish_pairing(self, part, feature_local: str, source, target, transform_label: str) -> None:
        """把"源边线 + 对称边线"落地：默认直接生成倒圆角，也可只加入选择集。"""
        # 这个方法可能跑在气泡按钮的回调线程里：重新取一次文档/部件，别复用别线程的 COM 指针
        part = self.session.active_part() or part
        want_select = str(self.config.symmetric_action).lower() == "select"
        radius = float(self.config.fillet_radius)

        if not want_select and not bool(getattr(self.config, "label_autocreate", False)):
            # 默认不再"按标签推算"直接建特征（某些零件编号不规律，可能落到别的边上）。
            # 只告诉你对称边在哪，然后请你自己点一下它，再"选中两条边 + 右键"生成。
            try:
                mid = (
                    sum(p.x for p in target.samples) / len(target.samples),
                    sum(p.y for p in target.samples) / len(target.samples),
                    sum(p.z for p in target.samples) / len(target.samples),
                )
                where = f"中点({mid[0]:.0f}, {mid[1]:.0f}, {mid[2]:.0f})，长 {target.length:.0f}"
            except Exception:
                where = target.describe()
            self.toast.show(
                "对称边位置（请你点它一下）",
                f"你要的对称边在：{where}\n\n"
                "更稳的做法：在模型里点中【你原来那条边】和【这条对称边】（两条都选中）→ 右键/ Ctrl+Alt+R，"
                "助手会把这两条边做成一个倒圆角（不再依赖编号推算，绝不会落到别的边上）。\n"
                "若要恢复「助手直接生成」，把 config.json 的 label_autocreate 设为 true。",
                seconds=12,
                level="info",
            )
            log.info("默认模式：只提示对称边位置（%s），不自动建特征", where)
            return

        # 重要：命令运行中创建的 BRep 引用绑在命令的临时体上，命令一关就失效。
        # 所以先生成圆角的话：**先读半径、先关命令，再造引用**。
        if not want_select:
            dialog = win32util.find_fillet_dialog(self.config.extra_dialog_titles)
            if dialog is not None:
                from_dialog = win32util.fillet_dialog_radius(dialog.hwnd)
                if from_dialog:
                    radius = from_dialog
                if self.config.close_dialog_before_fillet:
                    import time as _time
                    import win32con
                    import win32gui

                    win32gui.PostMessage(dialog.hwnd, win32con.WM_CLOSE, 0, 0)
                    _time.sleep(0.8)

        feature = self.session.object_by_name(part, feature_local) if feature_local else None
        ref_source = (
            self.session.reference_from_label(part, source.label, feature) if feature is not None else None
        )
        ref_target = (
            self.session.reference_from_label(part, target.label, feature) if feature is not None else None
        )
        if ref_source is None or ref_target is None:
            log.warning("引用创建失败：源=%s 目标=%s（feature=%s）", ref_source is not None, ref_target is not None, feature_local)
            self.toast.show("引用创建失败", "无法把算出的边线变成 CATIA 对象，请把日志发给开发者。", seconds=8, level="warn")
            return

        if want_select:
            session_ok = self.session.add_to_selection(ref_target)
            self.toast.show(
                "已补对称边线",
                f"{source.describe()} → {target.describe()}\n（按「{transform_label}」，只加入选择集）",
                seconds=4 if session_ok else 6,
                level="ok" if session_ok else "warn",
            )
            return
        ok, message = pairing.create_edge_fillet(
            self.session,
            part,
            [ref_source, ref_target],
            radius=radius,
            propagation=self.config.fillet_propagation,
            expected_lengths=[source.length, target.length],
        )
        log.info("生成结果：ok=%s 半径=%s 源=%s 目标=%s 说明=%s", ok, radius, source.describe(), target.describe(), message)
        self.toast.show(
            "已生成对称倒圆角" if ok else "生成倒圆角失败",
            f"{source.describe()} + {target.describe()}\n（按「{transform_label}」，半径 {radius:g}）\n{message}\n"
            "想看到这两条边：双击特征树里的『倒圆角』特征，框里就是它俩。"
            if ok
            else message,
            seconds=8,
            level="ok" if ok else "warn",
        )

    def _pair_with(self, transform_label: str) -> None:
        outcome = pairing.pair_selection(self.session, self.config, transform_label=transform_label)
        part = self.session.active_part()
        if outcome.added and part is not None and str(self.config.symmetric_action).lower() != "select":
            ok, message = self._create_fillet_from_selection(part)
            self.toast.show(
                "已生成对称倒圆角" if ok else "生成倒圆角失败",
                message,
                seconds=4 if ok else 8,
                level="ok" if ok else "warn",
            )
            return
        self.toast.show(
            "对称边线",
            outcome.message or transform_label,
            seconds=4 if outcome.added else 6,
            level="ok" if outcome.added else "warn",
        )

    def action_remove_wrong(self) -> None:
        log.info("动作：移除误选（Ctrl+Alt+X）")
        state = self.watcher.state_snapshot()
        if not state.bad_items:
            self.toast.show("没有误选", "当前选择里没有面/其它元素。", seconds=3, level="info")
            return
        self._remove_items(state.bad_items)

    def _remove_items(self, bad_items) -> None:
        """按标签移除误选项（按下标倒序删，避免下标漂移）。"""
        names = {it.display_name for it in bad_items}
        current = self.session.selection_items()
        targets = [it for it in current if it.display_name in names]
        for item in sorted(targets, key=lambda i: i.index, reverse=True):
            self.session.remove_selection_index(item.index)
        self.watcher.reset_alert()
        self.toast.show("已移除误选", f"移除了 {len(targets)} 个非边线元素，请重新点选边线。", seconds=4, level="info")

    def action_start_fillet(self) -> None:
        try:
            app = self.session.connect()
        except CatiaNotRunning as exc:
            self.toast.show("CATIA 没连上", str(exc), seconds=5, level="warn")
            return
        if win32util.start_edge_fillet(app):
            self.toast.show("边线倒圆角", "已打开「倒圆角定义」，选边线即可（可按右击补对称边线）。", seconds=4, level="ok")
        else:
            self.toast.show("启动失败", "没能打开「边线倒圆角」命令，请检查当前是否为 Part 文档。", seconds=6, level="warn")

    def _rmb_predicate(self) -> bool:
        """右键钩子的快速判断：CATIA 在前台，而且"有得可配"时才接管右键。"""
        if not self.config.right_click_pairing:
            return False
        if not win32util.is_catia_foreground():
            return False
        mode = (self.config.rmb_mode or "any").lower()
        if mode == "off":
            return False
        dialog = win32util.find_fillet_dialog(self.config.extra_dialog_titles)
        if dialog is not None and win32util.fillet_dialog_entries(dialog.hwnd):
            # 命令运行中点选的对象会被命令收走，文档选择集是空的，只能看对话框列表
            return True
        if mode == "dialog":
            return False
        state = self.watcher.state_snapshot()
        if state.all_edges:
            return True
        # 没有对话框时：只要选中的都是边线，也接管右键（直接做对称倒圆角）
        return bool(state.items) and all(item.is_edge for item in state.items)


# ------------------------------------------------------------------ 命令行


def setup_logging(level: str) -> Path:
    """日志写到可写目录（exe 同目录优先，只读时自动换地方）。"""
    log_file = paths.log_dir() / "assistant.log"
    handlers: list[logging.Handler] = []
    if sys.stdout is not None:
        handlers.append(logging.StreamHandler(sys.stdout))
    try:
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))
    except Exception:
        pass
    logging.basicConfig(
        level=getattr(logging, (level or "INFO").upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        handlers=handlers or [logging.NullHandler()],
    )
    return log_file


def _diagnose_shape_engine(session: CatiaSession, part, config: Config, rep) -> None:
    """R5 形状引擎自检：只读（枚举边线 + 量几何 + 数学自检），绝不建特征。"""
    rep("")
    rep("R5 形状引擎诊断（只读）")
    rep(
        f"  配置：启用={config.shape_engine_enabled} 优先={config.shape_engine_first} "
        f"自动生成={config.shape_engine_autocreate} 位置容差={config.shape_tolerance}mm "
        f"分数下限={config.shape_min_score} 平面匹配率下限={config.shape_plane_min_ratio}"
    )
    try:
        plane = shape_symmetry.Plane(normal=V3(1.0, 0.0, 0.0), origin=V3(5.0, 0.0, 0.0))
        mirrored = plane.mirror(V3(10.0, 20.0, 30.0))
        ok = abs(mirrored.x) < 1e-9 and abs(mirrored.y - 20.0) < 1e-9 and abs(mirrored.z - 30.0) < 1e-9
        rep(
            f"  Test7 镜像数学            : {'PASS' if ok else 'FAIL'}"
            f"（(10,20,30) 关于 x=5 → ({mirrored.x:.3f},{mirrored.y:.3f},{mirrored.z:.3f})）"
        )
    except Exception as exc:
        rep(f"  Test7 镜像数学            : FAIL —— {exc}")

    if part is None:
        rep("  Test1/4/5 边线枚举        : SKIP（当前文档不是 CATPart）")
        return
    try:
        scanner = EdgeScanner(session, ttl=0.0)
        edges = scanner.scan(part, force=True)
    except Exception as exc:
        rep(f"  Test5 边线枚举            : FAIL —— {exc}")
        return
    rep("  Test1 CATIA COM 连接      : PASS")
    rep("  Test4 当前实体(MainBody)  : PASS")
    rep(
        f"  Test5 边线枚举            : {'PASS' if edges else 'NOT_SUPPORTED'}"
        f"（{len(edges)} 条三维边，语法 {list(scanner.queries)}）"
    )
    for note in scanner.notes:
        rep(f"      · {note}")
    if not edges:
        rep(
            "      提示：若这里为 0，可能是该 CATIA 版本的搜索语法不同 —— "
            "可在 config.json 的 shape_edge_queries 里换成 \"Topology.CGMEdge,all\" 等再试。"
        )
        return
    measured = [edge for edge in edges if edge.length is not None]
    rep(
        f"  Test6 Measurable          : {'PASS' if measured else 'FAIL'}"
        f"（{len(measured)}/{len(edges)} 条能取到长度）"
    )
    complete = [edge for edge in edges if edge.sample_complete]
    rep(f"      采样完整性            : {len(complete)}/{len(edges)}（残缺的已用 中点+方向 兜底）")
    try:
        scored = shape_symmetry.rank_planes(
            edges,
            tolerance=config.shape_tolerance,
            min_ratio=config.shape_plane_min_ratio,
            max_planes=3,
        )
        if scored:
            rep(f"  Test8 镜像面候选          : PASS（最佳：{scored[0].describe()}）")
        else:
            rep("  Test8 镜像面候选          : NOT_FOUND（该零件没有轴对称面，属正常）")
    except Exception as exc:
        rep(f"  Test8 镜像面候选          : FAIL —— {exc}")
    try:
        wb = session.workbench()
        measurable = wb.GetMeasurable(edges[0].reference) if wb is not None else None
        rep(
            "  Test9 Fillet 输入引用     : "
            f"{'PASS' if measurable is not None else 'FAIL'}"
            "（扫描得到的 Reference 能再次量测＝可当 Fillet 输入）"
        )
    except Exception as exc:
        rep(f"  Test9 Fillet 输入引用     : FAIL —— {exc}")


def _diagnose(session: CatiaSession, config: Config) -> tuple[int, str]:
    """输出诊断报告（冻结 exe 无控制台时也安全）。"""
    rep = paths.Reporter()
    rep("=" * 72)
    rep(f"CATIA 倒圆角助手 v{__version__} 诊断报告")
    rep("=" * 72)
    rep(f"程序位置      : {paths.exe_dir()}")
    rep(f"冻结 exe      : {'是' if paths.is_frozen() else '否（源码运行）'}")
    rep(f"可写数据目录  : {paths.data_dir()}")
    rep(f"配置文件      : {paths.config_path()}")
    rep(f"comtypes 缓存 : {ensure_comtypes_gen_dir()}")
    rep(f"Python        : {sys.version.split()[0]} ({'64' if sys.maxsize > 2**32 else '32'} 位)")
    try:
        import comtypes

        rep(f"comtypes      : {comtypes.__version__}")
    except Exception as exc:
        rep(f"comtypes      : 不可用（{exc}）")
    try:
        import win32api  # noqa: F401

        rep("pywin32       : OK")
    except Exception as exc:
        rep(f"pywin32       : 不可用（{exc}）")
    try:
        import tkinter  # noqa: F401

        rep("Tkinter 气泡  : OK")
    except Exception as exc:
        rep(f"Tkinter 气泡  : 不可用（{exc}）")
    rep("")

    rep(f"配置: 对称方式={config.symmetry_mode} 镜像轴={config.mirror_axis} "
        f"右击补线={config.right_click_pairing} 轮询={config.poll_interval}s")
    rep(f"      catia_bin 配置值 = {config.catia_bin or '（空，自动探测）'}")
    catia_bin_path = None
    try:
        catia_bin_path = session.bin if session._bin else find_catia_bin(config.catia_bin)
        rep(f"CATIA 类型库目录: {catia_bin_path}")
        for name in REQUIRED_TLB:
            rep(f"    [必需] {name}: {'找到' if (catia_bin_path / name).exists() else '缺失！'}")
        for name in OPTIONAL_TLB:
            rep(
                f"    [可选] {name}: "
                f"{'找到' if (catia_bin_path / name).exists() else '缺失（重心退回草图中心）'}"
            )
    except Exception as exc:
        rep(f"CATIA 类型库目录: 定位失败 —— {exc}")
    # 类型库加载测试：不需要 CATIA 在运行，专门验证 comtypes 在冻结 exe 里能不能生成/加载接口
    if catia_bin_path is not None:
        try:
            import comtypes.client as _cc

            for name in REQUIRED_TLB + OPTIONAL_TLB:
                tlb = catia_bin_path / name
                if not tlb.exists():
                    rep(f"类型库加载 {name}: 跳过（文件缺失）")
                    continue
                try:
                    mod = _cc.GetModule(str(tlb))
                    rep(f"类型库加载 {name}: OK（{getattr(mod, '__name__', '?')}）")
                except Exception as exc:
                    rep(f"类型库加载 {name}: 失败 —— {exc}")
        except Exception as exc:
            rep(f"类型库加载: comtypes 不可用 —— {exc}")
    rep("")

    try:
        app = session.connect()
        version = "未知"
        try:
            cfg_obj = app.SystemConfiguration
            for attr in ("Version", "Name", "FullName"):
                value = getattr(cfg_obj, attr, None)
                if value:
                    version = str(value)
                    break
        except Exception:
            pass
        rep(f"CATIA 连接    : OK（{app.FullName}）")
        rep(f"CATIA 版本    : {version}")
        doc = session.active_document()
        part = session.active_part()
        rep(f"活动文档      : {doc.Name if doc else '（无）'}")
        rep(f"是否 CATPart  : {'是' if part else '否'}")
        dialog = win32util.find_fillet_dialog(config.extra_dialog_titles)
        rep(f"倒圆角对话框  : {dialog.title if dialog else '未打开'}")
        rep(f"官方边线判定  : {session.selection_all_edges()}")
        items = session.selection_items()
        if not items:
            rep("当前选择集    : 空")
        for item in items:
            rep(f"  - {item.describe()}")
            if item.label:
                rep(f"      {item.label.describe()}")
            if item.coordinates:
                rep(f"      拾取点={tuple(round(c, 2) for c in item.coordinates)}")
        _diagnose_shape_engine(session, part, config, rep)
        status = 0
    except CatiaNotRunning as exc:
        rep(f"CATIA 连接    : 失败 —— {exc}")
        rep("提示：请先启动 CATIA V5（助手不会自己启动 CATIA）。")
        status = 2
    except Exception as exc:
        rep(f"CATIA 连接    : 异常 —— {exc}")
        status = 3

    rep("")
    rep(f"诊断报告文件  : {paths.report_path()}")
    return status, rep.text()


def _print_state(session: CatiaSession, config: Config) -> int:
    status, _text = _diagnose_and_save(session, config)
    return status


def _diagnose_and_save(session: CatiaSession, config: Config) -> tuple[int, str]:
    status, text = _diagnose(session, config)
    report = paths.Reporter()
    report.lines = text.splitlines()
    saved = report.write()
    if sys.stdout is not None:
        print(f"\n（诊断报告已保存到 {saved}）")
    else:
        report.show_dialog(extra=f"报告文件：{saved}")
    return status, text


# ---------------------------------------------------------------- 退出/卸载


def _confirm(message: str, title: str, ok_text: str) -> bool:
    """二次确认对话框（Tk）。没有桌面会话时返回 False。"""
    try:
        import tkinter as tk

        root = tk.Tk()
        root.title(title)
        root.attributes("-topmost", True)
        root.resizable(False, False)
        result = {"ok": False}
        tk.Label(
            root, text=message, justify="left", wraplength=460, font=("Microsoft YaHei UI", 10), padx=16, pady=14
        ).pack()
        row = tk.Frame(root)
        row.pack(pady=(0, 14))

        def on_ok() -> None:
            result["ok"] = True
            root.destroy()

        tk.Button(row, text=ok_text, width=14, command=on_ok).pack(side="left", padx=8)
        tk.Button(row, text="取消", width=10, command=root.destroy).pack(side="left", padx=8)
        # 回车=确定，Esc=取消（也方便自动化验收）
        root.bind("<Return>", lambda _event: on_ok())
        root.bind("<Escape>", lambda _event: root.destroy())
        root.focus_force()
        root.mainloop()
        return result["ok"]
    except Exception as exc:
        log.warning("确认对话框不可用：%s", exc)
        return False


def _uninstall_targets() -> list[Path]:
    """要删除的目录：程序目录本身 + %LOCALAPPDATA% 下的数据目录。"""
    targets = [paths.exe_dir()]
    local = os.environ.get("LOCALAPPDATA")
    if local:
        data = Path(local) / paths.APP_NAME
        if data.exists() and data not in targets:
            targets.append(data)
    return targets


def _looks_like_our_folder(path: Path) -> bool:
    """安全校验：只删看起来确实是我们包装出来的目录。"""
    marks = ("CatiaFilletAssistant.exe", "run_assistant.py", "portable_entry.py", "CatiaFilletAssistant")
    try:
        if any((path / mark).exists() for mark in marks):
            return True
        if path.name.startswith("CATIA倒圆角助手"):
            return True
        if (path / "_internal").exists():
            return True
    except Exception:
        return False
    return False


def _schedule_delete(path: Path) -> None:
    """用一个临时 bat 在几秒后删除文件（用于清理"被挪走的自己"）。"""
    bat = Path(tempfile.gettempdir()) / f"catia_assistant_cleanup_{os.getpid()}.bat"
    try:
        bat.write_text(
            "@echo off\r\n"
            "ping -n 6 127.0.0.1 >nul\r\n"
            f'del /f /q "{path}"\r\n'
            f'if exist "{path}" (ping -n 6 127.0.0.1 >nul & del /f /q "{path}")\r\n'
            f'if exist "{path}" (ping -n 8 127.0.0.1 >nul & del /f /q "{path}")\r\n'
            f'del /f /q "{bat}"\r\n',
            encoding="ascii",
        )
        subprocess.Popen(
            ["cmd", "/c", str(bat)],
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception as exc:
        log.warning("安排延迟删除失败：%s", exc)


def _schedule_dir_delete(directory: Path) -> None:
    """兜底：写个临时 bat，等本进程退出后再删目录（用于 exe 挪不走的情况）。"""
    bat = Path(tempfile.gettempdir()) / f"catia_assistant_rmdir_{os.getpid()}.bat"
    try:
        bat.write_text(
            "@echo off\r\n"
            "ping -n 6 127.0.0.1 >nul\r\n"
            f'rmdir /s /q "{directory}"\r\n'
            f'if exist "{directory}" (ping -n 6 127.0.0.1 >nul & rmdir /s /q "{directory}")\r\n'
            'del /f /q "%~f0"\r\n',
            encoding="ascii",
        )
        subprocess.Popen(
            ["cmd", "/c", f'"{bat}"'],
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception as exc:
        log.warning("安排目录延迟删除失败：%s", exc)


def run_quit() -> int:
    ok, message = request_quit()
    print(message)
    if sys.stdout is None:
        _show_message("退出助手", message)
    return 0 if ok else 1


def _show_message(title: str, message: str) -> None:
    try:
        import tkinter as tk

        root = tk.Tk()
        root.title(title)
        root.attributes("-topmost", True)
        root.geometry("520x220")
        tk.Label(root, text=message, justify="left", wraplength=480, font=("Microsoft YaHei UI", 10), padx=16, pady=16).pack()
        tk.Button(root, text="关闭", command=root.destroy, height=2).pack(fill="x")
        root.mainloop()
    except Exception:
        pass


def _show_message_auto(title: str, message: str, seconds: float = 4.0) -> None:
    """自动关闭的提示框（卸载时用，避免把 exe 一直占住导致目录删不掉）。"""
    try:
        import tkinter as tk

        root = tk.Tk()
        root.title(title)
        root.attributes("-topmost", True)
        root.geometry("520x240")
        tk.Label(
            root, text=message, justify="left", wraplength=480, font=("Microsoft YaHei UI", 10), padx=16, pady=16
        ).pack()
        tk.Label(root, text=f"（{seconds:g} 秒后自动关闭）", fg="#555555").pack()
        root.after(int(seconds * 1000), root.destroy)
        root.mainloop()
    except Exception:
        pass


def run_uninstall(skip_confirm: bool = False) -> int:
    """卸载：二次确认 → 退出助手 → 删除程序目录与数据目录。"""
    targets = _uninstall_targets()
    target_text = "\n".join(f"  · {t}" for t in targets)
    if not skip_confirm:
        if not _confirm(
            "确定要卸载「CATIA 倒圆角助手」吗？\n\n"
            "卸载会退出正在运行的助手，并删除下面这些目录（含日志与配置）：\n"
            f"{target_text}\n\n"
            "注意：不会动 CATIA 本身（不写注册表、不改 CATIA 设置）。",
            "卸载 CATIA 倒圆角助手（第 1 / 2 步）",
            "继续",
        ):
            return 0
        if not _confirm(
            "再次确认：删除后无法恢复。\n\n"
            f"{target_text}\n\n"
            "如果只是想让助手退出、保留程序，请改用「退出助手」。",
            "卸载 CATIA 倒圆角助手（第 2 / 2 步）",
            "确认卸载",
        ):
            return 0

    ok, message = request_quit()
    log.info("卸载：退出助手 → %s", message)
    removed: list[str] = []
    failed: list[str] = []
    program_dir = paths.exe_dir()
    for target in targets:
        # 先删数据目录，程序目录最后用延迟命令删（自己正在里面运行）
        if target == program_dir:
            continue
        if not _looks_like_our_folder(target) and target.name != paths.APP_NAME:
            failed.append(f"{target}（未通过安全校验，跳过）")
            continue
        try:
            shutil.rmtree(target)
            removed.append(str(target))
        except Exception as exc:
            failed.append(f"{target}（{exc}）")
    if not paths.is_frozen():
        failed.append(f"{program_dir}（源码模式：为安全起见不删除项目目录，请手动删）")
    elif _looks_like_our_folder(program_dir):
        # 运行中的 exe 不能删，但可以**同盘改名**：先挪出程序目录，再删目录
        moved_exe: Path | None = None
        try:
            exe_path = Path(sys.executable)
            if exe_path.exists() and exe_path.parent == program_dir:
                target = program_dir.parent / f"CatiaFilletAssistant_removed_{os.getpid()}.exe"
                os.replace(exe_path, target)  # 同卷改名，运行中也可以
                moved_exe = target
        except Exception as exc:
            log.warning("改名自身 exe 失败，改用延迟删除：%s", exc)
            moved_exe = None
        try:
            shutil.rmtree(program_dir)
            removed.append(str(program_dir))
        except Exception as exc:
            failed.append(f"{program_dir}（{exc}）")
            if moved_exe is None:
                _schedule_dir_delete(program_dir)
                removed.append(f"{program_dir}（已安排稍后自动删除）")
        if moved_exe is not None:
            _schedule_delete(moved_exe)
            removed.append(f"{moved_exe}（几秒后自动清理）")
    else:
        failed.append(f"{program_dir}（未通过安全校验，跳过）")

    text = "卸载完成。\n\n已删除：\n" + ("\n".join(f"  · {r}" for r in removed) or "（无）")
    if failed:
        text += "\n\n未删除/需要手动处理：\n" + "\n".join(f"  · {f}" for f in failed)
    text += "\n\n助手已退出；CATIA 本身未被改动。"
    log.info("卸载结果：%s", text.replace("\n", " "))
    print(text)
    if sys.stdout is None:
        _show_message_auto("卸载完成", text, seconds=4.0)
    return 0


def main(argv: list[str] | None = None) -> int:
    base_dir = paths.exe_dir()
    parser = argparse.ArgumentParser(prog="catia-fillet-assistant", description="CATIA V5 倒圆角助手")
    parser.add_argument("--config", type=Path, default=None, help="指定配置文件（默认取程序目录下 config.json）")
    parser.add_argument("--check", action="store_true", help="生成诊断报告（含 CATIA/类型库/选择集信息）后退出")
    parser.add_argument("--pair", action="store_true", help="只执行一次「补对称边线」后退出")
    parser.add_argument("--start-fillet", action="store_true", help="打开边线倒圆角命令后退出")
    parser.add_argument("--debug", action="store_true", help="调试日志")
    parser.add_argument("--version", action="store_true", help="打印版本号后退出")
    parser.add_argument("--quit", action="store_true", help="让正在运行的助手退出")
    parser.add_argument("--uninstall", action="store_true", help="卸载（退出助手并删除程序/数据目录）")
    parser.add_argument("--yes", action="store_true", help="配合 --uninstall：跳过二次确认（脚本用）")
    parser.add_argument("--report-pick", action="store_true", help="只读取证：导出点选边与候选边的几何对照报告")
    args = parser.parse_args(argv)

    if args.version:
        print(f"CATIA 倒圆角助手 v{__version__}")
        return 0
    if args.quit:
        return run_quit()
    if args.uninstall:
        return run_uninstall(skip_confirm=args.yes)
    if args.report_pick:
        # 只读取证：把"点选边实测几何"与"每条候选标签推算几何"写成报告文件，供定位编号对应关系
        config = Config.load(paths.config_path(args.config), create_if_missing=False)
        report = paths.Reporter()
        try:
            from .catia import CatiaSession as _Session
            from .cellgeom import CellIndex as _CellIndex
            from . import featuregeom as _fg

            session = _Session(catia_bin=config.catia_bin or None)
            cells = _CellIndex(session)
            part = session.active_part()
            dialog = win32util.find_fillet_dialog(config.extra_dialog_titles)
            entries = win32util.fillet_dialog_entries(dialog.hwnd) if dialog else []
            report(f"对话框：{dialog.title if dialog else '未打开'}  列表={entries}")
            if entries and part is not None:
                cell = cells.find_by_name(part, entries[0])
                if cell is None:
                    report("点选单元解析失败（拿不到几何）")
                else:
                    mid = (
                        sum(p.x for p in cell.samples) / len(cell.samples) if cell.samples else 0,
                        sum(p.y for p in cell.samples) / len(cell.samples) if cell.samples else 0,
                        sum(p.z for p in cell.samples) / len(cell.samples) if cell.samples else 0,
                    )
                    report(f"点选边：{cell.name} 类型={cell.kind} 长度={cell.length} 中点=({mid[0]:.1f},{mid[1]:.1f},{mid[2]:.1f})")
                    report(f"点选边采样点：{[tuple(round(c, 1) for c in p.as_tuple()) for p in cell.samples]}")
                    if cell.obj is not None:
                        try:
                            ref = part.CreateReferenceFromObject(cell.obj)
                            session.clear_selection()
                            session.add_to_selection(ref)
                            items = session.selection_items()
                            real = items[0].display_name if items else ""
                            session.clear_selection()
                            report(f"点选边真实 BRep 标签：{real}")
                        except Exception as exc:
                            report(f"读取真实 BRep 标签失败：{exc}")
                    else:
                        report("点选单元没有对象句柄，无法读真实 BRep 标签")
                    feature_local = entries[0].split("\\", 1)[0].strip() if "\\" in entries[0] else ""
                    feature = session.object_by_name(part, feature_local)
                    names = _fg.name_candidates(session, part, feature, feature_local)
                    report(f"特征内部名候选：{names[:3]}")
                    sketch = _fg.feature_attr(session, feature, "Sketch")
                    frame = session.read_sketch_frame(sketch)
                    elements = session.read_sketch_elements(sketch)
                    report(f"草图可读元素：{[(el.index, el.kind) for el in elements]}")
                    first, second, symmetric = _fg.read_limits(session, feature)
                    candidates = _fg.prismatic_candidates(
                        session, part, feature, names[0][0], names[0][1], frame, elements,
                        _fg._z_ranges(first, second, symmetric),
                    )
                    report("候选标签（长度 / 中点 / 与点选边的距离）：")
                    seen = set()
                    for cand in candidates:
                        key = (cand.kind, cand.element_index, cand.partner_index)
                        if key in seen:
                            continue
                        seen.add(key)
                        _match, dist = _fg.best_match(cell.samples, [cand], tolerance=1e9)
                        cmid = (
                            sum(p.x for p in cand.samples) / len(cand.samples),
                            sum(p.y for p in cand.samples) / len(cand.samples),
                            sum(p.z for p in cand.samples) / len(cand.samples),
                        )
                        report(
                            f"  {cand.describe():<22} 长={cand.length:7.2f} "
                            f"中点=({cmid[0]:.1f},{cmid[1]:.1f},{cmid[2]:.1f}) 距离={dist:8.3f}"
                        )
            else:
                report("请先在倒圆角对话框里点一条边，再运行本功能")
        except Exception as exc:
            report(f"取证失败：{exc}")
        saved = report.write(paths.data_dir() / "取证报告.txt")
        report.show_dialog(extra=f"报告文件：{saved}")
        print(f"报告已保存：{saved}")
        return 0

    config_path = paths.config_path(args.config)
    config = Config.load(config_path, create_if_missing=True)
    log_file = setup_logging("DEBUG" if args.debug else config.log_level)
    log.info("启动：程序目录=%s 配置=%s 日志=%s", base_dir, config_path, log_file)

    session = CatiaSession(catia_bin=config.catia_bin or None)
    if args.check:
        return _diagnose_and_save(session, config)[0]
    if args.pair:
        outcome = pairing.pair_selection(session, config)
        print(outcome.message)
        for label, cands in outcome.groups.items():
            print(f"  可选对称方式: {label}（{len(cands)} 条）")
        return 0
    if args.start_fillet:
        try:
            app = session.connect()
        except CatiaNotRunning as exc:
            print(exc)
            return 2
        ok = win32util.start_edge_fillet(app)
        print("已打开倒圆角定义" if ok else "没能打开边线倒圆角命令")
        return 0 if ok else 1

    if not single_instance_guard():
        log.warning("助手已在运行，本次启动退出（避免重复热键/钩子）")
        print("CATIA 倒圆角助手已经在运行了（如需重启：先按 Ctrl+Alt+Q 退出）。")
        if sys.stdout is None:
            try:
                toast = ToastUI()
                toast.start()
                toast.wait_ready(2.0)
                toast.show(
                    "助手已在运行",
                    "CATIA 倒圆角助手已经在后台运行。\n如需重启：在 CATIA 里按 Ctrl+Alt+Q 退出后再启动。",
                    seconds=6,
                    level="info",
                )
                import time as _time

                _time.sleep(6.5)
            except Exception:
                pass
        return 0

    assistant = FilletAssistant(config_path.parent, config)
    return assistant.run()


if __name__ == "__main__":
    raise SystemExit(main())
