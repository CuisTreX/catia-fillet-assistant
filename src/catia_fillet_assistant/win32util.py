"""Win32 层：枚举 CATIA 窗口、识别倒圆角对话框、调用命令、判断前台窗口。"""

from __future__ import annotations

import ctypes
import logging
import time
from dataclasses import dataclass
from typing import Iterable

import win32api
import win32con
import win32gui
import win32process

log = logging.getLogger(__name__)

# 各语言版本的「边线倒圆角定义」对话框标题（config 可追加）
FILLET_DIALOG_TITLES = (
    "倒圆角定义",
    "边线倒圆角定义",
    "Edge Fillet Definition",
    "Definition de l'arête arrondie",
    "Definition de l'arete arrondie",
    "Verrundung definieren",
    "Definizione raccordo",
    "Definición de redondeo",
    "Definição de arredondamento",
    "コーナーラウンド定義",
    "フィレット定義",
)

FILLET_COMMAND_IDS = ("Edge Fillet", "EdgeFillet", "Fillet")


@dataclass
class FilletDialog:
    hwnd: int
    title: str
    class_name: str


def _cnext_pid_of_window(hwnd: int) -> int | None:
    try:
        pid = win32process.GetWindowThreadProcessId(hwnd)[1]
        handle = win32api.OpenProcess(
            win32con.PROCESS_QUERY_INFORMATION | win32con.PROCESS_VM_READ, False, pid
        )
        exe = win32process.GetModuleFileNameEx(handle, 0)
        win32api.CloseHandle(handle)
    except Exception:
        return None
    return pid if exe.lower().endswith("cnext.exe") else None


def catia_windows(visible_only: bool = True) -> list[tuple[int, str, str]]:
    """CATIA 进程（CNEXT.exe）的顶层窗口：(hwnd, 类名, 标题)。"""
    result: list[tuple[int, str, str]] = []

    def cb(hwnd, _param):
        if _cnext_pid_of_window(hwnd) is None:
            return True
        if visible_only and not win32gui.IsWindowVisible(hwnd):
            return True
        try:
            result.append((hwnd, win32gui.GetClassName(hwnd), win32gui.GetWindowText(hwnd)))
        except Exception:
            pass
        return True

    try:
        win32gui.EnumWindows(cb, None)
    except Exception:
        pass
    return result


def catia_main_window() -> int | None:
    """CATIA 主窗口（标题形如 CATIA V5 - [xxx]）。"""
    best = None
    for hwnd, cls, title in catia_windows():
        if title.startswith("CATIA V5") and " - [" in title:
            return hwnd
        if title == "CATIA V5":
            best = hwnd
    return best


def is_catia_foreground() -> bool:
    hwnd = win32gui.GetForegroundWindow()
    return _cnext_pid_of_window(hwnd) is not None


def children(hwnd: int) -> list[int]:
    found: list[int] = []

    def cb(child, _param):
        found.append(child)
        return True

    try:
        win32gui.EnumChildWindows(hwnd, cb, None)
    except Exception:
        pass
    return found


def child_texts(hwnd: int) -> list[str]:
    texts: list[str] = []
    for child in children(hwnd):
        try:
            texts.append(win32gui.GetWindowText(child))
        except Exception:
            continue
    return [t for t in texts if t]


def fillet_dialog_entries(hwnd: int) -> list[str]:
    """读「要圆角化的对象」列表里的条目（形如 `凸台.1\\目标边线.1`）。

    命令运行中用户点选的对象不在 Document.Selection 里，只能从这里读。
    """
    LB_GETCOUNT, LB_GETTEXT, LB_GETTEXTLEN = 0x018B, 0x0189, 0x018A
    entries: list[str] = []
    for child in children(hwnd):
        try:
            if win32gui.GetClassName(child).lower() != "listbox":
                continue
            if win32gui.GetWindowText(child) != "Edt":
                continue
            count = int(win32gui.SendMessage(child, LB_GETCOUNT, 0, 0))
        except Exception:
            continue
        for i in range(min(count, 32)):
            try:
                length = int(win32gui.SendMessage(child, LB_GETTEXTLEN, i, 0))
                buffer = ctypes.create_unicode_buffer(max(64, length + 4))
                win32gui.SendMessage(child, LB_GETTEXT, i, buffer)
            except Exception:
                continue
            text = buffer.value.strip()
            # 真实的条目形如 “特征\\单元.序号”，占位文本（无选择）会被过滤掉
            if text and ("\\" in text or "." in text):
                if text not in entries:
                    entries.append(text)
    return entries


def fillet_dialog_radius(hwnd: int) -> float | None:
    """从倒圆角对话框里读「半径」输入框的值（读不到就用配置里的默认值）。"""
    numeric: list[float] = []
    for child in children(hwnd):
        try:
            if win32gui.GetClassName(child).lower() != "edit":
                continue
            text = win32gui.GetWindowText(child).strip().replace(",", ".")
        except Exception:
            continue
        if not text:
            continue
        try:
            numeric.append(float(text))
        except ValueError:
            continue
    for value in numeric:
        if value > 0:
            return value
    return None


def find_fillet_dialog(extra_titles: Iterable[str] = ()) -> FilletDialog | None:
    """识别「边线倒圆角定义」对话框。

    1) 标题命中已知各语言标题（config 可追加）；
    2) 兜底：标题含 圆角/Fillet 关键字，且窗口里有「半径」文本。
    """
    patterns = [t.lower() for t in list(FILLET_DIALOG_TITLES) + list(extra_titles) if t]
    windows = catia_windows()
    for hwnd, cls, title in windows:
        low = title.lower()
        if any(p in low for p in patterns):
            return FilletDialog(hwnd=hwnd, title=title, class_name=cls)
    for hwnd, cls, title in windows:
        low = title.lower()
        if not any(k in low for k in ("圆角", "fillet", "arrondi", "verrundung", "raccordo")):
            continue
        kids = child_texts(hwnd)
        if any(("半径" in t) or ("radius" in t.lower()) for t in kids):
            return FilletDialog(hwnd=hwnd, title=title, class_name=cls)
    return None


def dismiss_unknown_command_dialogs() -> int:
    """关闭「未知命令」提示框（命令 ID 写错时会弹，并卡住调用线程）。"""
    closed = 0
    for hwnd, cls, _title in catia_windows():
        if cls != "#32770":
            continue
        texts = child_texts(hwnd)
        if not any(("未知命令" in t) or ("Unknown command" in t) for t in texts):
            continue
        for child in children(hwnd):
            try:
                if win32gui.GetClassName(child).lower() == "button":
                    win32gui.SendMessage(child, win32con.BM_CLICK, 0, 0)
                    closed += 1
                    break
            except Exception:
                continue
    return closed


def click_button(dialog_hwnd: int, texts: Iterable[str]) -> bool:
    wanted = {t.strip() for t in texts}
    for child in children(dialog_hwnd):
        try:
            if win32gui.GetWindowText(child).strip() in wanted and "button" in win32gui.GetClassName(child).lower():
                win32gui.SendMessage(child, win32con.BM_CLICK, 0, 0)
                return True
        except Exception:
            continue
    return False


def start_edge_fillet(app, timeout: float = 3.0) -> bool:
    """启动「边线倒圆角」命令（app 为 comtypes 的 CATIA.Application）。"""
    from comtypes.automation import BSTR

    for cmd in FILLET_COMMAND_IDS:
        try:
            app.StartCommand(BSTR(cmd))
        except Exception as exc:
            log.debug("StartCommand(%s) 异常: %s", cmd, exc)
            continue
        time.sleep(0.4)
        if dismiss_unknown_command_dialogs():
            log.info("命令 ID '%s' 无效，已关闭提示框", cmd)
            continue
        deadline = time.time() + timeout
        while time.time() < deadline:
            if find_fillet_dialog() is not None:
                return True
            time.sleep(0.2)
    return False
