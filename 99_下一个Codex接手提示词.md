# 99_下一个Codex接手提示词

## 项目是什么

CATIA V5 倒圆角助手：误选面提醒 + 右键/热键补对称边线。不依赖 CAA RADE，走自动化 + Win32。

## 已完成

* v0.1 可用：监视/提醒/移除误选/对称配对/右键钩子/热键/气泡/配置/日志/单元测试/联机回归/文档全部就位。
* 关键技术结论见 `docs/技术方案与兼容性.md`、`docs/实测记录.md`（含"别再试"的失败路线）。

## 当前卡点

无硬卡点。待用户实测反馈：手动单击边线的识别、复杂对称件的配对成功率。

## 下一步具体做什么

1. 若识别有偏差：调 `SelectionItem.is_edge/is_face` 的判定（`src/catia_fillet_assistant/catia.py` + `labels.py`）。
2. 若配对找不到：扩展 `pairing.py` 的候选生成（跨特征/镜像体的对称映射）。
3. 若要打包：PyInstaller 打包 `run_assistant.py`（注意把 comtypes gen 缓存一起带上）。

## 先读哪些文件

1. `docs/实测记录.md`（事实清单）
2. `docs/技术方案与兼容性.md`（接口与兼容性）
3. `src/catia_fillet_assistant/pairing.py` + `catia.py`（核心算法/接口层）

## 不要动什么

* 不要改 `docs/API探测/` 下的类型库 dump（那是证据基线）。
* 不要在没有实证的情况下改分类/配对阈值；先用 `tools/probe_*.py` 复现再改。

## 本轮产出应该是什么

* 小步修改 + 用 `tools/test_pairing_live.py`、`tools/test_watcher_live.py`、`tests/` 回归；
* 更新 `docs/实测记录.md` 与 `02_问题与报错日志.md`。
