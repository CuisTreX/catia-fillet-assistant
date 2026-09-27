"""把 CATIA V5 自动化类型库（*.tlb）的成员清单转储成文本，便于做接口兼容性核对。

用途：
    1. 确认某个 V5 版本到底暴露了哪些接口/方法（不同 Release 差异较大）。
    2. 插件里用 try/except + hasattr 做特性探测时，先在这里查清正确拼写。

用法：
    python dump_typelib.py <tlb 路径> [<tlb 路径> ...] -o <输出目录>
    python dump_typelib.py --dir "D:\\Programs\\CATIA\\win_b64\\code\\bin" -o .\\out

只读操作：不做任何注册表写入，不启动 CATIA。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pythoncom

TKIND_NAMES = {
    0: "ENUM",
    1: "RECORD",
    2: "MODULE",
    3: "INTERFACE",
    4: "DISPATCH",
    5: "COCLASS",
    6: "ALIAS",
    7: "UNION",
}

INVOKE_NAMES = {
    1: "FUNC",
    2: "PROP_GET",
    4: "PROP_PUT",
    8: "PROP_PUTREF",
}

IMPLTYPE_FLAGS = {
    1: "DEFAULT",
    2: "SOURCE",  # 事件接口
    4: "RESTRICTED",
    8: "DEFAULTVTABLE",
}


def _doc(ti) -> str:
    try:
        name, doc, _ctx, _help = ti.GetDocumentation(-1)
        parts = [p for p in (name, doc) if p]
        return " | ".join(parts)
    except Exception as exc:  # pragma: no cover - 某些类型无文档
        return f"<no doc: {exc}>"


def _getattr_any(obj, *names, default="?"):
    for name in names:
        try:
            return getattr(obj, name)
        except Exception:
            continue
    return default


def _member_line(ti, desc, kind: str) -> str:
    names = []
    try:
        names = list(ti.GetNames(desc.memid))
    except Exception:
        pass
    head = names[0] if names else f"memid={desc.memid}"
    args = ", ".join(names[1:]) if len(names) > 1 else ""
    invkind = INVOKE_NAMES.get(desc.invkind, str(desc.invkind))
    return f"    [{desc.memid:>6}] {invkind:<11} {head}({args})"


def dump_typeinfo(ti, out: list[str], index: int) -> None:
    try:
        attr = ti.GetTypeAttr()
    except Exception as exc:
        out.append(f"  <#{index} 无法读取类型属性: {exc}>")
        return

    kind = TKIND_NAMES.get(attr.typekind, str(attr.typekind))
    out.append(f"\n== #{index} {kind}: {_doc(ti)}")
    out.append(
        f"    iid={_getattr_any(attr, 'iid', 'guid')}  funcs={attr.cFuncs}  vars={attr.cVars}  impltypes={attr.cImplTypes}"
    )

    if attr.cImplTypes:
        for k in range(attr.cImplTypes):
            try:
                href = ti.GetRefTypeOfImplType(k)
                flags = ti.GetImplTypeFlags(k)
                ref = ti.GetRefTypeInfo(href)
                ref_name, ref_doc, _c, _h = ref.GetDocumentation(-1)
                flag_txt = "|".join(n for bit, n in IMPLTYPE_FLAGS.items() if flags & bit) or str(flags)
                out.append(f"    implements[{k}] {ref_name} ({flag_txt}) {ref_doc or ''}".rstrip())
            except Exception as exc:
                out.append(f"    implements[{k}] <{exc}>")

    for j in range(attr.cFuncs):
        try:
            desc = ti.GetFuncDesc(j)
        except Exception as exc:
            out.append(f"    <func#{j} 读取失败: {exc}>")
            continue
        out.append(_member_line(ti, desc, kind))

    for j in range(attr.cVars):
        try:
            desc = ti.GetVarDesc(j)
        except Exception as exc:
            out.append(f"    <var#{j} 读取失败: {exc}>")
            continue
        names = []
        try:
            names = list(ti.GetNames(desc.memid))
        except Exception:
            pass
        out.append(f"    [var {desc.memid:>6}] {names[0] if names else '?'}  {names[1] if len(names) > 1 else ''}".rstrip())

    # 枚举类型把取值也打出来，方便写代码时挑常量
    if attr.typekind == 0:  # TKIND_ENUM
        for j in range(attr.cVars):
            try:
                desc = ti.GetVarDesc(j)
                names = list(ti.GetNames(desc.memid))
                out.append(f"        {names[0]} = {desc.value}")
            except Exception:
                pass


def dump_library(tlb_path: Path, out_dir: Path) -> tuple[Path, int]:
    tlb = pythoncom.LoadTypeLib(str(tlb_path))
    try:
        name, doc, _ctx, _help = tlb.GetDocumentation(-1)
    except Exception:
        name, doc = tlb_path.name, ""
    lines = [f"# {tlb_path}", f"# 库名: {name}", f"# 说明: {doc}"]
    try:
        attr = tlb.GetLibAttr()
        lines.append(f"# 属性: {attr}")
    except Exception:
        pass

    count = tlb.GetTypeInfoCount()
    for i in range(count):
        try:
            ti = tlb.GetTypeInfo(i)
        except Exception as exc:
            lines.append(f"\n== #{i} <读取失败: {exc}>")
            continue
        dump_typeinfo(ti, lines, i)

    out_path = out_dir / f"{tlb_path.stem}.txt"
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out_path, count


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="转储 CATIA V5 类型库成员清单")
    parser.add_argument("tlb", nargs="*", type=Path, help="一个或多个 .tlb 文件")
    parser.add_argument("--dir", type=Path, help="批量扫描该目录下的 *.tlb")
    parser.add_argument("-o", "--out", type=Path, default=Path("out"), help="输出目录")
    args = parser.parse_args(argv)

    targets: list[Path] = list(args.tlb)
    if args.dir:
        targets.extend(sorted(args.dir.glob("*.tlb")))
    if not targets:
        parser.error("请至少给出一个 .tlb 文件，或用 --dir 指定目录")

    args.out.mkdir(parents=True, exist_ok=True)
    for path in targets:
        if not path.exists():
            print(f"[跳过] 不存在: {path}", file=sys.stderr)
            continue
        try:
            out_path, count = dump_library(path, args.out)
        except Exception as exc:
            print(f"[失败] {path.name}: {exc}", file=sys.stderr)
            continue
        print(f"[完成] {path.name} -> {out_path}  类型数={count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
