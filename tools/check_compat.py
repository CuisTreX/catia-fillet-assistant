"""兼容性自检：确保源码能在 **Python 3.8 + Windows 7 + 旧版 CATIA** 上跑。

检查内容：
    1. 语法：用 `ast.parse(feature_version=(3, 8))` 逐个文件解析（超出 3.8 的语法会报错）；
    2. 注解：出现 `X | Y` 这类 3.10 写法时，文件必须有 `from __future__ import annotations`；
    3. 3.9+/3.10+/3.11+ 才有的标准库接口（removeprefix / functools.cache / zoneinfo / graphlib /
       tomllib / dataclass(slots=True) / itertools.pairwise / asyncio.to_thread 等）；
    4. 报告统计信息（文件数、行数）。

用法：
    python tools/check_compat.py            # 默认检查 src/ tools/ tests/
    python tools/check_compat.py src        # 只查某个目录
退出码 0 = 通过。
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path
from typing import List, Tuple

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DIRS = ("src", "tools", "tests", "run_assistant.py", "portable_entry.py")

FUTURE_ANNOTATIONS = "from __future__ import annotations"

# 3.9+ / 3.10+ / 3.11+ 才有的属性式接口
NEW_ATTRS = {
    "removeprefix": "3.9+ str.removeprefix",
    "removesuffix": "3.9+ str.removesuffix",
    "pairwise": "3.10+ itertools.pairwise",
    "to_thread": "3.9+ asyncio.to_thread",
    "lcm": "3.9+ math.lcm",
}

# 3.9+ 才有的模块
NEW_MODULES = {
    "zoneinfo": "3.9+ zoneinfo",
    "graphlib": "3.9+ graphlib",
    "tomllib": "3.11+ tomllib",
}


def iter_files(target: Path) -> List[Path]:
    if target.is_file() and target.suffix == ".py":
        return [target]
    if target.is_dir():
        return sorted(p for p in target.rglob("*.py") if "__pycache__" not in p.parts)
    return []


def check_file(path: Path) -> Tuple[List[str], int]:
    issues: List[str] = []
    text = path.read_text(encoding="utf-8", errors="replace")
    lines = text.count("\n") + 1

    # 1) 语法是否属于 3.8
    try:
        ast.parse(text, filename=str(path), feature_version=(3, 8))
    except SyntaxError as exc:
        issues.append(f"{path}: 语法超出 Python 3.8（第 {exc.lineno} 行）：{exc.msg}")
        return issues, lines

    tree = ast.parse(text, filename=str(path))
    has_future = FUTURE_ANNOTATIONS in text

    for node in ast.walk(tree):
        # 2) 注解里的 X | Y（PEP 604）需要 future import
        if isinstance(node, (ast.AnnAssign, ast.arg)) or isinstance(
            node, (ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            annotation = None
            if isinstance(node, ast.AnnAssign):
                annotation = node.annotation
            elif isinstance(node, ast.arg):
                annotation = node.annotation
            elif getattr(node, "returns", None) is not None:
                annotation = node.returns
            if isinstance(annotation, ast.BinOp) and isinstance(annotation.op, ast.BitOr):
                if not has_future:
                    issues.append(
                        f"{path}:{getattr(node, 'lineno', '?')} 注解用了 `X | Y`，"
                        "但文件没有 from __future__ import annotations（3.8 上会炸）"
                    )
        # 3) 新接口
        if isinstance(node, ast.Attribute) and node.attr in NEW_ATTRS:
            issues.append(
                f"{path}:{node.lineno} 用了 {NEW_ATTRS[node.attr]}"
            )
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute):
                if isinstance(func.value, ast.Name) and func.value.id == "functools" and func.attr == "cache":
                    issues.append(f"{path}:{node.lineno} 用了 3.9+ 的 functools.cache（改用 lru_cache）")
                if isinstance(func.value, ast.Name) and func.value.id == "dataclasses":
                    if any(kw.arg == "slots" for kw in node.keywords):
                        issues.append(f"{path}:{node.lineno} 用了 3.10+ 的 dataclass(slots=...)")
                if func.attr == "dataclass":
                    if any(kw.arg == "slots" for kw in node.keywords):
                        issues.append(f"{path}:{node.lineno} 用了 3.10+ 的 dataclass(slots=...)")
            if isinstance(func, ast.Name) and func.id == "dataclass":
                if any(kw.arg == "slots" for kw in node.keywords):
                    issues.append(f"{path}:{node.lineno} 用了 3.10+ 的 dataclass(slots=...)")
        # 4) 新模块
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".", 1)[0]
                if root in NEW_MODULES:
                    issues.append(f"{path}:{node.lineno} 导入了 {NEW_MODULES[root]}")
        if isinstance(node, ast.ImportFrom) and node.module:
            root = node.module.split(".", 1)[0]
            if root in NEW_MODULES:
                issues.append(f"{path}:{node.lineno} 导入了 {NEW_MODULES[root]}")
    return issues, lines


def main(argv: List[str]) -> int:
    targets = argv[1:] or list(DEFAULT_DIRS)
    files: List[Path] = []
    for item in targets:
        files.extend(iter_files(ROOT / item if not Path(item).is_absolute() else Path(item)))
    if not files:
        print("没有找到要检查的 .py 文件")
        return 2

    print(f"兼容性检查：目标 Python 3.8 / Windows 7（共 {len(files)} 个文件）")
    all_issues: List[str] = []
    total_lines = 0
    for path in files:
        issues, lines = check_file(path)
        total_lines += lines
        try:
            shown = path.relative_to(ROOT)
        except ValueError:
            shown = path
        if issues:
            print(f"  FAIL  {shown}")
            for issue in issues:
                print(f"        {issue}")
            all_issues.extend(issues)
        else:
            print(f"  PASS  {shown}")
    print(f"\n合计 {len(files)} 个文件 / {total_lines} 行；问题 {len(all_issues)} 处")
    if all_issues:
        print("结论：有 3.8 兼容问题，逐条修掉后重新运行。")
        return 1
    print("结论：源码语法与标准库用法都在 Python 3.8 范围内（Win7 可跑）。")
    print("提示：Win7 上请用 `安装环境-Win7-Python38.bat` 建独立 3.8 venv，")
    print("      并用 `打包便携版-Win7.bat`（PyInstaller 4.10）出包。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
