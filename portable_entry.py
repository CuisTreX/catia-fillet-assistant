"""PyInstaller 打包入口（源码直接运行也走这里）。

冻结成 exe 后：
    * 所有依赖（Python、pywin32、comtypes、Tk）都在 exe 内部，拷过去即可运行；
    * 配置与日志写到 exe 同目录（只读目录时自动退到 %LOCALAPPDATA%）。
"""

from __future__ import annotations

import sys
from pathlib import Path

if not getattr(sys, "frozen", False):
    sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from catia_fillet_assistant.app import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
