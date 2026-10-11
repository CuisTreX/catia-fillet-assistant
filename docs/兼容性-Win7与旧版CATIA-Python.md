# 兼容性：Windows 7 / 旧版 CATIA / 旧版 Python

本文是"老平台也要能跑"的操作手册；每条结论都对应到具体做法。

## 1. Windows 7

| 项目 | 要求 | 说明 |
| --- | --- | --- |
| 系统 | Windows 7 **SP1**（32/64 位） | SP1 是 Python 3.8 的最低要求 |
| Python | **3.8.x**（推荐 3.8.10） | Python **3.9 起官方不再支持 Win7**，所以 Win7 机器只能用 3.8 分支 |
| 依赖 | `comtypes==1.1.14`、`pywin32==228` | 见 `requirements-win7.txt` |
| 打包 | **PyInstaller 4.10** | 5.x/6.x 打出的 exe 在 Win7 上起不来；`打包便携版-Win7.bat` 已固定这个版本 |

一键流程（在**打包机**上）：

```text
1) 双击「安装环境-Win7-Python38.bat」  ← 用 Python 3.8 建独立 venv
                                         （F:\CodeX\04-Codex工作区\tools\catia-fillet-assistant-win7）
2) 双击「打包便携版-Win7.bat」         ← PyInstaller 4.10 出包：
                                         dist\CATIA倒圆角助手-便携版-v*\
                                         dist\CATIA倒圆角助手-文件夹模式-v*\
3) 整个文件夹拷到目标 Win7 机器 → 双击「诊断.bat」→ 双击「CatiaFilletAssistant.exe」
```

Win7 目标机上的注意点：

* **不需要**装 Python、不需要联网、不需要管理员权限（便携版自带解释器）；
* 程序目录只读时（共享盘/策略保护），配置与日志自动退到 `%LOCALAPPDATA%\CatiaFilletAssistant\`；
* 封闭平台禁止 exe 从临时目录运行时，用**文件夹模式**那个包；
* 本助手没有任何网络调用（打包脚本也不会给 exe 加联网组件）。

## 2. 旧版本 CATIA（V5R19 / R20 / R21 …）

代码里没有写死版本号；版本相关的只有四件事，全部可配置：

| 配置项（config.json） | 作用 | 旧版本怎么调 |
| --- | --- | --- |
| `shape_edge_queries` | 枚举边线的搜索语法 | 默认 `["Topology.CGMEdge,sel", "Topology.CGMEdge,all"]`；某版本不认时换成候选语法，诊断报告会打印实际用了哪条、命中几条 |
| `catia_bin` | `win_b64\code\bin` 类型库目录 | 注册表定位失败（绿色版/多版本共存）时手工填 |
| `extra_dialog_titles` | 倒圆角对话框标题 | 换语言/换版本后标题变了，在这里补 |
| 命令 ID | 打开"边线倒圆角"用的命令 | 见 `docs/技术方案与兼容性.md`，可配置 |

验证办法：双击「诊断.bat」，看 `R5 形状引擎诊断` 一段：

```text
Test4 当前实体(MainBody)  : PASS
Test5 边线枚举            : PASS（12 条三维边，语法 ['Topology.CGMEdge,sel']）
Test6 Measurable          : PASS（12/12 条能取到长度）
Test7 镜像数学            : PASS
Test8 镜像面候选          : PASS（最佳：Y = 30.00 的中面…）
Test9 Fillet 输入引用     : PASS
```

任何一项 `FAIL / NOT_SUPPORTED`，把诊断报告发给开发者即可定位，不必在每个版本上重新试错。

## 3. 旧版本 Python

* **3.8 是目标下限**（也是 Win7 的天花板）：源码用 `tools/check_compat.py` 做静态校验
  （`ast.parse(feature_version=(3,8))` + 3.9+/3.10+ 接口黑名单），当前 **56 个文件 / 12184 行，0 问题**；
* 3.9 / 3.10 / 3.11 / 3.12 / 3.13 同样可跑（本机开发环境是 3.13，单测与联机验收全过）；
* 3.7 未做保证（不建议）；
* 代码约定：
  * 所有文件都带 `from __future__ import annotations`（`X | Y` 注解只作为字符串存在，3.8 不会炸）；
  * 不使用 `str.removeprefix/removesuffix`、`functools.cache`、`zoneinfo`、`graphlib`、`dataclass(slots=True)` 等新接口；
  * 不用 `match` 语句、不用 3.10 的联合类型判别。

## 4. 每次发版前的固定检查（约 3 分钟）

```bat
:: 用 Win7 venv 的 python 执行（源码环境同样适用）
python tools\check_compat.py          :: 语法/接口是否仍在 3.8 范围内
python -m pytest tests -q             :: 纯数学单测（不需要 CATIA）
python run_assistant.py --check       :: 环境诊断（需要 CATIA 在运行）
python tools\test_r5_shape_engine_live.py   :: 临时件端到端验收（Case1/3/4/5 + 负测试）
```
