"""轮询监视：倒圆角命令是否打开、当前选择里有没有「误选成面」的元素。"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

from . import win32util
from .catia import CatiaSession, SelectionItem
from .cellgeom import CellIndex
from .config import Config

log = logging.getLogger(__name__)


@dataclass
class WatchState:
    dialog_active: bool = False
    dialog_title: str = ""
    dialog_entries: list[str] = field(default_factory=list)
    dialog_non_edge: list[str] = field(default_factory=list)
    items: list[SelectionItem] = field(default_factory=list)
    bad_items: list[SelectionItem] = field(default_factory=list)
    updated_at: float = 0.0

    @property
    def count(self) -> int:
        return len(self.items)

    @property
    def all_edges(self) -> bool:
        return bool(self.items) and all(it.is_edge for it in self.items)

    def summary(self) -> str:
        if not self.dialog_active:
            return "倒圆角命令未打开"
        if self.dialog_entries:
            text = "；".join(self.dialog_entries)
            if self.dialog_non_edge:
                return f"倒圆角对话框里选的是：{text}（含非边线：{'、'.join(self.dialog_non_edge)}）"
            return f"倒圆角对话框里选的是：{text}"
        if not self.items:
            return "倒圆角命令已打开，当前没有选中任何元素"
        return "；".join(it.describe() for it in self.items)


class FilletWatcher(threading.Thread):
    """后台轮询线程；通过 state_snapshot() 读取最新状态（线程安全）。"""

    def __init__(
        self,
        session: CatiaSession,
        config: Config,
        on_wrong_pick: Callable[[WatchState], None] | None = None,
        on_error: Callable[[str], None] | None = None,
        stop_event: threading.Event | None = None,
        cells: CellIndex | None = None,
    ) -> None:
        super().__init__(name="fillet-watcher", daemon=True)
        self.session = session
        self.config = config
        self.on_wrong_pick = on_wrong_pick
        self.on_error = on_error
        self.stop_event = stop_event or threading.Event()
        self.cells = cells or CellIndex(session)
        self._state = WatchState()
        self._lock = threading.Lock()
        self._last_signature: tuple[str, ...] | None = None
        self._last_entries: list[str] = []
        self._cells_cache: dict[str, object] = {}
        self._errors = 0

    # ------------------------------------------------------------------ API
    def state_snapshot(self) -> WatchState:
        with self._lock:
            state = WatchState(
                dialog_active=self._state.dialog_active,
                dialog_title=self._state.dialog_title,
                dialog_entries=list(self._state.dialog_entries),
                dialog_non_edge=list(self._state.dialog_non_edge),
                items=list(self._state.items),
                bad_items=list(self._state.bad_items),
                updated_at=self._state.updated_at,
            )
        return state

    def reset_alert(self) -> None:
        self._last_signature = None

    # ---------------------------------------------------------------- 主循环
    def run(self) -> None:
        # comtypes 在每个线程里都要先初始化 COM，否则跨线程调用会静默失败
        try:
            import comtypes

            comtypes.CoInitialize()
        except Exception as exc:  # pragma: no cover
            log.debug("CoInitialize 失败: %s", exc)
        while not self.stop_event.is_set():
            try:
                self._tick()
                self._errors = 0
            except Exception as exc:  # pragma: no cover - CATIA 状态异常
                self._errors += 1
                log.warning("监视循环异常(%s): %s", self._errors, exc)
                if self.on_error and self._errors == 3:
                    self.on_error(f"与 CATIA 的连接异常: {exc}")
            self.stop_event.wait(max(0.05, float(self.config.poll_interval)))

    def _tick(self) -> None:
        dialog = win32util.find_fillet_dialog(self.config.extra_dialog_titles)
        if dialog is None:
            with self._lock:
                self._state = WatchState(updated_at=time.time())
            self._last_signature = None
            return

        items = self.session.selection_items()
        bad = [it for it in items if not it.is_edge]
        # 命令里点选的对象不在文档选择集里，只能从对话框列表判断
        entries = win32util.fillet_dialog_entries(dialog.hwnd)
        non_edge: list[str] = []
        part = self.session.active_part()
        # 只有"对话框内容变了"才重新解析单元：解析要走 COM，每轮都做会把日志刷爆、还会拖慢动作
        if entries != self._last_entries:
            self._last_entries = list(entries)
            self._cells_cache.clear()
            for entry in entries:
                cell = self.cells.find_by_name(part, entry) if part is not None else None
                self._cells_cache[entry] = cell
        for entry, cell in self._cells_cache.items():
            if cell is not None:
                if cell.kind != "edge":
                    non_edge.append(f"{entry}（{cell.kind}）")
                continue
            text = entry.lower()
            if any(k in entry for k in ("面", "平面", "圆柱", "球", "锥")) or any(
                k in text for k in ("face", "plane", "cylinder", "sphere", "surface")
            ):
                non_edge.append(f"{entry}（疑似面）")
        signature = tuple([it.display_name for it in items] + entries)
        with self._lock:
            self._state = WatchState(
                dialog_active=True,
                dialog_title=dialog.title,
                dialog_entries=entries,
                dialog_non_edge=non_edge,
                items=items,
                bad_items=bad,
                updated_at=time.time(),
            )
        if (bad or non_edge) and signature != self._last_signature:
            self._last_signature = signature
            log.info(
                "检测到误选: %s",
                "; ".join([it.describe() for it in bad] + non_edge),
            )
            if self.on_wrong_pick:
                self.on_wrong_pick(self.state_snapshot())
        elif not bad:
            self._last_signature = signature

    def stop(self) -> None:
        self.stop_event.set()
