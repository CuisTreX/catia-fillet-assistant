"""启动入口（供 .bat / pythonw 调用）。"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from catia_fillet_assistant.app import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
