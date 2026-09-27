"""全局热键 + 低级鼠标钩子（右键补对称边线）。

全部在同一个后台线程里跑：注册热键（RegisterHotKey）+ 装鼠标钩子（WH_MOUSE_LL），
然后用 GetMessage 抽消息。钩子回调里只做「查缓存状态」这种极快的判断，
真正的 CATIA 操作交给工作线程，避免阻塞系统输入链。
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import logging
import threading
from typing import Callable

log = logging.getLogger(__name__)

MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000

WM_HOTKEY = 0x0312
WM_RBUTTONDOWN = 0x0204
WH_MOUSE_LL = 14

_MODS = {"ctrl": MOD_CONTROL, "control": MOD_CONTROL, "alt": MOD_ALT, "shift": MOD_SHIFT, "win": MOD_WIN}

_VK = {
    "f1": 0x70, "f2": 0x71, "f3": 0x72, "f4": 0x73, "f5": 0x74, "f6": 0x75,
    "f7": 0x76, "f8": 0x77, "f9": 0x78, "f10": 0x79, "f11": 0x7A, "f12": 0x7B,
    "space": 0x20, "enter": 0x0D, "esc": 0x1B, "tab": 0x09,
}


class _POINT(ctypes.Structure):
    _fields_ = [("x", wt.LONG), ("y", wt.LONG)]


class _MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("pt", _POINT),
        ("mouseData", wt.DWORD),
        ("flags", wt.DWORD),
        ("time", wt.DWORD),
        ("dwExtraInfo", ctypes.c_void_p),
    ]


def parse_hotkey(text: str) -> tuple[int, int] | None:
    """把 'ctrl+alt+r' 解析成 (modifiers, vk)。"""
    parts = [p.strip().lower() for p in (text or "").split("+") if p.strip()]
    if not parts:
        return None
    mods = 0
    key = None
    for p in parts:
        if p in _MODS:
            mods |= _MODS[p]
        elif p in _VK:
            key = _VK[p]
        elif len(p) == 1:
            key = ord(p.upper())
        else:
            log.warning("无法识别的按键: %s", p)
            return None
    if key is None:
        return None
    return (mods | MOD_NOREPEAT, key)


class InputListener(threading.Thread):
    """全局热键 + 右键钩子监听线程。

    hotkeys: {"ctrl+alt+r": callback}
    rmb_predicate: 返回 True 时吞掉这次右击并调用 rmb_action
    """

    def __init__(
        self,
        hotkeys: dict[str, Callable[[], None]],
        rmb_predicate: Callable[[], bool] | None = None,
        rmb_action: Callable[[], None] | None = None,
        stop_event: threading.Event | None = None,
    ) -> None:
        super().__init__(name="input-listener", daemon=True)
        self.hotkeys = dict(hotkeys)
        self.rmb_predicate = rmb_predicate
        self.rmb_action = rmb_action
        self.stop_event = stop_event or threading.Event()
        self._ids: dict[int, Callable[[], None]] = {}
        self._hook = None
        self._hook_proc = None
        self.available = False
        self.error: str | None = None

    def run(self) -> None:
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        for idx, (combo, callback) in enumerate(self.hotkeys.items(), start=1):
            parsed = parse_hotkey(combo)
            if parsed is None:
                continue
            mods, vk = parsed
            if not user32.RegisterHotKey(None, idx, mods, vk):
                self.error = f"热键 {combo} 注册失败（可能已被占用）"
                log.warning(self.error)
                continue
            self._ids[idx] = callback
        if self.rmb_predicate is not None and self.rmb_action is not None:
            self._install_mouse_hook(user32)
        self.available = bool(self._ids) or self._hook is not None

        msg = wt.MSG()
        while not self.stop_event.is_set():
            ret = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if ret <= 0:
                break
            if msg.message == WM_HOTKEY:
                callback = self._ids.get(int(msg.wParam))
                if callback is not None:
                    threading.Thread(target=callback, name="hotkey-job", daemon=True).start()
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

        for idx in self._ids:
            user32.UnregisterHotKey(None, idx)
        if self._hook:
            user32.UnhookWindowsHookEx(self._hook)
            self._hook = None

    def _install_mouse_hook(self, user32) -> None:
        # 注意：LPARAM 必须用 c_void_p，否则 64 位下会 OverflowError
        HOOKPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_int, ctypes.c_size_t, ctypes.c_void_p)
        user32.CallNextHookEx.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_size_t, ctypes.c_void_p]
        user32.CallNextHookEx.restype = ctypes.c_ssize_t
        user32.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC, ctypes.c_void_p, ctypes.c_uint32]
        user32.SetWindowsHookExW.restype = ctypes.c_void_p

        def _proc(n_code, w_param, l_param):
            try:
                if n_code >= 0 and w_param == WM_RBUTTONDOWN and self.rmb_predicate is not None:
                    if self.rmb_predicate():
                        threading.Thread(target=self.rmb_action, name="rmb-job", daemon=True).start()
                        return 1  # 吞掉这次右击，CATIA 不会弹右键菜单
            except Exception as exc:  # pragma: no cover
                log.debug("鼠标钩子异常: %s", exc)
            return user32.CallNextHookEx(None, n_code, w_param, l_param)

        self._hook_proc = HOOKPROC(_proc)
        self._hook = user32.SetWindowsHookExW(WH_MOUSE_LL, self._hook_proc, None, 0)
        if not self._hook:
            log.warning("鼠标钩子安装失败，右键补对称边线不可用")

    def stop(self) -> None:
        self.stop_event.set()
        try:
            ctypes.windll.user32.PostThreadMessageW(self.native_id, 0x0012, 0, 0)  # WM_QUIT
        except Exception:
            pass
