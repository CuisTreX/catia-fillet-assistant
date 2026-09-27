# 原生 Edge Fillet 集成 — 实测结论（2026-09-25）

> 对应 [需求-原生Fillet集成-Prompt存档.md](需求-原生Fillet集成-Prompt存档.md) 的 §6–§13、§40、§60。
> 所有结论都在本机 CATIA V5R20（中文版）用**临时件**实测，模型零改动。

## 1. 结论速览

| 路线 | 做法 | 结果 |
| --- | --- | --- |
| **A（Prompt 首选）** | 两条边放进 Selection → `StartCommand("Edge Fillet")` | **FAIL-2**：对话框能开，但「要圆角化的对象」列表是**「无选择」**（一条都没认） |
| **B（Prompt 备选）** | 先建原生特征 → 选中该特征 → `StartCommand("Edge Fillet")` | 打开的是**新建**对话框，不是编辑已有特征（对象列表同样「无选择」、半径读不到） |
| **C（命令 ID 探测）** | 试 `StartCommand("Definition")` 等 6 个"打开定义"命令 | **危险且失败**：CATIA 弹出模态框「超级输入消息」并把 COM 调用**卡死**（详见 §3） |
| **D（现状 v0.5.1）** | 助手用官方 API 一次调用建出原生 `倒圆角.x`（含两条边），用户再双击特征改参数 | 已验证可用（体积 240000→239570.8 mm³，特征树只多 1 个原生特征） |

## 2. 证据（可直接核对）

**POC-01（A 路线）**

```text
===== NATIVE FILLET POC =====
Selection.Count2 before StartCommand = 2
1. Selection_REdge:(Edge:(Face:(Brp:(Pad.1;0:(Brp:(Sketch.1;4)));None:();Cf11:());Face:(Brp:(Pad.1;0:(B ...
2. Selection_REdge:(Edge:(Face:(Brp:(Pad.1;0:(Brp:(Sketch.1;3)));None:();Cf11:());Face:(Brp:(Pad.1;0:(B ...
StartCommand = 'Edge Fillet'
Dialog Found = True（倒圆角定义 / #32770）
Object List（0 项）：
Radius 输入框读出 = None
结论：FAIL-2
```

**控件树 dump（排除"读错控件"）**：对话框里 21 个 `ListBox`（窗口文本 `Edt`，即 CATIA 的对象列表控件）
**全部只有占位文本「无选择」**——所以不是我们读错，而是原生命令确实没消费 Selection。

日志：`docs/API探测/poc_native_fillet_preselection.log`、`poc_native_fillet_*.json`、
`probe_native_fillet_variants.log`、`native_fillet_variants_*.json`。

## 3. 踩到并记下的坑（重要，别再试）

1. **`StartCommand` 传未验证的命令 ID 会把助手卡死**：`StartCommand("Definition")` 让 CATIA 弹出模态框
   「超级输入消息」，COM 调用不返回（本轮实测卡了 3 分钟，只能杀进程 + `WM_CLOSE` 关模态框）。
   **规矩**：任何 `StartCommand` 都必须带看门狗（超时后关闭模态框并放弃），并且只用已验证的命令 ID。
   项目里已有 `win32util.dismiss_unknown_command_dialogs()`，接入时要用上。
2. **原生命令不消费自动化 Selection**（A 路线 FAIL-2）：`Selection.Count2 = 2` 也没用。
3. **选中特征再启动原生命令 ≠ 编辑该特征**：`StartCommand("Edge Fillet")` 永远开新命令；
   要编辑已有特征，CATIA 的常规做法是**在特征树上双击该特征**（软件原生行为，非自动化）。

## 4. 现在就能用的工作流（v0.5.1，零风险）

```text
点一条边 → 右键 / Ctrl+Alt+R
        ↓
助手：形状级对称找镜像边（R5/R14 已实现，实测连续三刀、0 误配）
        ↓
助手：一次官方 API 调用建出 1 个原生 EdgeFillet（含两条边，半径/传播方式可配）
        ↓
你在特征树上双击这个『倒圆角』→ CATIA 原生「倒圆角定义」对话框（两条边、半径都在里面）
        ↓
你自己改半径 / 传播 / Preview → 点 OK → CATIA 原生更新
```

* 助手**不点 OK**、不做鼠标模拟、不在识别阶段写模型；
* 与 Prompt §58 的差别只有一处：**进入原生对话框的方式是"双击特征"而不是"预选自动带入"**，
  这一段 Prompt 要求的自动化路径已被实测判定为当前 CATIA 版本不支持。

## 5. 若要继续推进 Prompt 的原始设想（B 方案：真实鼠标点击）

需要按 Prompt §9–§12 实现，并满足这些安全条件（缺一不可）：

```text
1. 用 Viewpoint（Eye / SightDirection / UpDirection / FOV / ProjectionMode）算 World→Screen
2. 区分正交/透视；处理窗口位置、DPI、多显示器、缩放
3. 点击点优先取边中点，被遮挡时换 P0/P2 附近的采样点
4. 无法证明"落点就在目标边上"→ 一律不点，提示用户手动点
5. 绝不允许自动点 OK
```

这是本项目里唯一还没验证过的重活；建议先确认"双击特征改参数"这条路你用得顺不顺，
再决定要不要投入 B 方案。
