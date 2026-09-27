"""置顶提示气泡（Tkinter 实现，跑在独立线程）。"""

from __future__ import annotations

import logging
import queue
import threading
import tkinter as tk
from dataclasses import dataclass, field
from typing import Callable

log = logging.getLogger(__name__)

_COLORS = {
    "warn": "#7f1d1d",
    "info": "#0f172a",
    "ok": "#064e3b",
}


def _run_with_com(callback: Callable[[], None]) -> Callable[[], None]:
    """气泡按钮的回调在新线程里跑，必须先初始化 COM，否则 CATIA 调用会静默失败。"""

    def wrapper() -> None:
        try:
            import comtypes

            comtypes.CoInitialize()
        except Exception:
            pass
        try:
            callback()
        except Exception as exc:  # pragma: no cover - 出错要留痕，不能静默
            log.exception("气泡按钮回调出错: %s", exc)

    return wrapper


@dataclass
class ToastRequest:
    title: str
    message: str
    buttons: list[tuple[str, Callable[[], None]]] = field(default_factory=list)
    seconds: float = 0.0
    level: str = "info"


class ToastUI(threading.Thread):
    """线程安全的置顶气泡：其它线程调用 show()，界面在自己的线程里刷新。"""

    def __init__(self) -> None:
        super().__init__(name="toast-ui", daemon=True)
        self._queue: "queue.Queue[ToastRequest | None]" = queue.Queue()
        self._root: tk.Tk | None = None
        self._windows: list[tk.Toplevel] = []
        self._ready = threading.Event()

    def show(
        self,
        title: str,
        message: str,
        buttons: list[tuple[str, Callable[[], None]]] | None = None,
        seconds: float = 0.0,
        level: str = "info",
    ) -> None:
        self._queue.put(ToastRequest(title, message, list(buttons or []), seconds, level))

    def shutdown(self) -> None:
        self._queue.put(None)

    def wait_ready(self, timeout: float = 3.0) -> bool:
        return self._ready.wait(timeout)

    def run(self) -> None:  # pragma: no cover - 依赖桌面环境
        try:
            self._root = tk.Tk()
        except Exception as exc:
            log.warning("无法创建提示窗口（可能没有桌面会话）: %s", exc)
            return
        self._root.withdraw()
        self._root.title("CATIA 倒圆角助手")
        self._ready.set()
        self._root.after(120, self._pump)
        self._root.mainloop()

    def _pump(self) -> None:  # pragma: no cover
        assert self._root is not None
        try:
            while True:
                req = self._queue.get_nowait()
                if req is None:
                    for win in list(self._windows):
                        win.destroy()
                    self._windows.clear()
                    self._root.quit()
                    return
                self._render(req)
        except queue.Empty:
            pass
        self._root.after(120, self._pump)

    def _render(self, req: ToastRequest) -> None:  # pragma: no cover
        assert self._root is not None
        for win in list(self._windows):
            # 只保留最新一条，避免刷屏
            win.destroy()
        self._windows.clear()

        bg = _COLORS.get(req.level, _COLORS["info"])
        win = tk.Toplevel(self._root)
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        frame = tk.Frame(win, bg=bg, padx=14, pady=10, highlightthickness=2, highlightbackground="#111827")
        frame.pack(fill="both", expand=True)
        tk.Label(
            frame, text=req.title, bg=bg, fg="#f8fafc", font=("Microsoft YaHei UI", 11, "bold")
        ).pack(anchor="w")
        tk.Label(
            frame,
            text=req.message,
            bg=bg,
            fg="#e2e8f0",
            justify="left",
            wraplength=380,
            font=("Microsoft YaHei UI", 9),
        ).pack(anchor="w", pady=(4, 8))

        def close() -> None:
            try:
                win.destroy()
            except Exception:
                pass
            if win in self._windows:
                self._windows.remove(win)

        row = tk.Frame(frame, bg=bg)
        row.pack(anchor="e")

        def make_cmd(callback: Callable[[], None] | None):
            def _cmd():
                close()
                if callback is not None:
                    threading.Thread(target=_run_with_com(callback), name="toast-action", daemon=True).start()

            return _cmd

        for text, callback in req.buttons:
            tk.Button(
                row,
                text=text,
                command=make_cmd(callback),
                font=("Microsoft YaHei UI", 9),
                padx=10,
                relief="flat",
                bg="#f8fafc",
                fg="#0f172a",
                activebackground="#e2e8f0",
            ).pack(side="left", padx=(6, 0))
        tk.Button(
            row,
            text="关闭",
            command=close,
            font=("Microsoft YaHei UI", 9),
            padx=10,
            relief="flat",
            bg="#334155",
            fg="#f8fafc",
            activebackground="#475569",
        ).pack(side="left", padx=(6, 0))

        win.update_idletasks()
        width = max(320, win.winfo_reqwidth())
        height = win.winfo_reqheight()
        screen_w = win.winfo_screenwidth()
        win.geometry(f"{width}x{height}+{screen_w - width - 40}+60")
        self._windows.append(win)
        if req.seconds and req.seconds > 0:
            win.after(int(req.seconds * 1000), close)
