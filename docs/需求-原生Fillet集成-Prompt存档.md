# 需求存档：原生 Edge Fillet 命令集成 + 形状级对称自动补边

> 本文是用户 2026-09-25 交办的完整开发 Prompt **原文存档**（未做任何删改）。
> 实测结论（POC-01 结果）见 [原生Fillet集成-实测结论.md](原生Fillet集成-实测结论.md)。

---

保存这个版本 CATIA V5 倒圆角助手

## 原生 Edge Fillet 命令集成 + 形状级对称自动补边

### 完整开发 Prompt

---

# 1. 项目目标

继续开发现有：
```text
CATIA V5 倒圆角助手
当前自动化版本：v0.5.1
技术栈：Python + comtypes/pywin32 + Win32 Hook
平台：Windows
```

项目核心目标：

> 用户只需要选择一条需要倒圆角的 Edge，右键触发助手；助手自动寻找该 Edge 关于零件轴对称/镜像平面的对应 Edge，然后将“源 Edge + 对称 Edge”加入 CATIA 原生的 Edge Fillet 命令，使用户继续使用 CATIA 原生倒圆角对话框自行调整参数并最终确认。

最终工作流必须是：
```text
用户选择一条 Edge
        ↓
右键 / Ctrl+Alt+R
        ↓
助手识别源 Edge
        ↓
助手寻找镜像/轴对称 Edge
        ↓
得到：
    Source Edge
    Target Symmetric Edge
        ↓
将两条 Edge 准备为 CATIA Fillet 命令的初始选择
        ↓
启动 CATIA 原生 Edge Fillet 命令
        ↓
CATIA 原生 Edge Fillet Definition 对话框
        ↓
对象列表中已有：
    Source Edge
    Target Edge
        ↓
用户自行调整：
    Radius
    Tangency / Propagation
    Extremity
    Trim
    Preview
    其他 CATIA 原生选项
        ↓
用户自己点击 OK
        ↓
CATIA 自己生成原生 Fillet 特征
```

**助手不直接调用&#x20;****`AddNewEdgeFilletWithConstantRadius()`****&#x20;作为主要工作流。**

**助手不替用户点 CATIA 的 OK。**

**最终模型修改由 CATIA 原生 Edge Fillet 命令以及用户自己的最终确认完成。**

---

# 2. 当前项目已知事实

以下事实已经通过现有 v0.4.8 实测或长期开发经验确认，不得重新尝试已经明确失败的路线。

## 2.1 用户点选对象在命令运行期间不会正常进入 Document.Selection

当 CATIA 的交互式 Fillet 命令已经运行：
```text
Document.Selection.Count2
```

可能仍为：
```text
0
```

因此不要用：
```text
Document.Selection
```

判断用户在 CATIA 原生 Fillet 对话框中点了什么。

当前正确的用户对象获取方式仍然可以沿用：
```text
对话框对象列表
    ↓
FindObjectByName
    ↓
可测量单元
    ↓
真实 BRep 标签
```

---

# 2.2 正在运行的 Fillet 命令不接受脚本 Selection.Add() 注入对象

已经验证：
```python
selection.Add(edge)
```

即使 Edge 已经被加入 Document.Selection，也不能保证正在运行的 CATIA Fillet 对话框中的：
```text
Object(s) to fillet
```

列表同步更新。

甚至尝试在对话框打开后：
```text
Selection.Add()
```

然后手动点 CATIA “确定”，也不会自动让该对象进入 Fillet 对象列表。

因此：

> **禁止继续采用“先启动 Fillet，再 Selection.Add() 向正在运行的命令中塞对象”的路线。**

---

# 2.3 新的首选路线：命令启动前预选

需要重点验证：
```text
Selection.Clear()
Selection.Add(SourceEdge)
Selection.Add(TargetEdge)
CATIA.StartCommand("Edge Fillet")
```

即：
```text
先建立 CATIA Selection
        ↓
再启动原生 Edge Fillet
```

而不是：
```text
先 StartCommand("Edge Fillet")
        ↓
再 Selection.Add()
```

这是本项目当前最重要的新技术路线。

CATIA 的 `StartCommand` 存在“命令启动之前准备 Selection，然后由命令消费该 Selection”的工作方式，但是否支持取决于具体命令，因此必须针对当前 CATIA 版本实际验证。

---

# 3. 最终工作流定义

## R2：自动补对称边

用户：
```text
选中 Edge A
```

然后：
```text
右键
```

或者：
```text
Ctrl+Alt+R
```

助手：
```text
识别 Edge A
        ↓
计算对称 Edge B
        ↓
准备 Edge A + Edge B
        ↓
启动 CATIA 原生 Edge Fillet
```

CATIA：
```text
Edge Fillet Definition
```

并且理想情况下：
```text
Object(s) to fillet:
    Edge A
    Edge B
```

用户继续：
```text
修改半径
修改传播方式
Preview
OK / Cancel
```

助手不再创建 Fillet。

---

# 4. R3：特征树要求

助手不得自己创建：
```text
Fillet Feature
```

而应由：
```text
CATIA 原生 Edge Fillet Command
```

完成。

最终用户点击 OK 后：
```text
PartBody
    ...
    Fillet.xxx
```

只增加一个 CATIA 原生 Fillet 特征。

该 Fillet 中应包含：
```text
Source Edge
Target Symmetric Edge
```

---

# 5. 为什么采用原生 Fillet 命令

这是设计原则，不是可选项。

使用原生 Edge Fillet 可以让 CATIA 自己管理：
```text
Radius
Propagation
Tangency
Minimal
Extremity
Trim
Keep Edges
Corner treatment
Preview
Update
Topology regeneration
```

助手无需重新实现 CATIA Fillet 参数逻辑。

因此：
```text
助手职责：
    找对称 Edge
    准备选择
    启动原生命令
```

而：
```text
CATIA 职责：
    管理 Fillet 参数
    Preview
    建模
    更新拓扑
    创建特征
```

---

# 6. 第一验证任务：POC-01

在正式修改现有程序之前，先创建一个极简 POC。

文件：
```text
tests/poc_native_fillet_preselection.py
```

仅完成：
```python
sel.Clear()

sel.Add(edge1)
sel.Add(edge2)

catia.StartCommand("Edge Fillet")
```

然后人工观察 CATIA：
```text
Edge Fillet Definition
```

重点确认：
```text
Object(s) to fillet
```

中是否已经存在：
```text
Edge 1
Edge 2
```

---

# 7. POC-01 验收标准

## PASS

如果出现：
```text
Object(s) to fillet:
    Edge A
    Edge B
```

则：
```text
首选方案成立
```

后续正式开发全部优先采用：
```text
Preselection
    ↓
StartCommand
```

---

## FAIL-1

如果只有：
```text
Edge A
```

则继续研究 CATIA 对多对象预选的处理方式。

不得直接判断整个方案失败。

---

## FAIL-2

如果：
```text
Selection.Add(A)
Selection.Add(B)
StartCommand("Edge Fillet")
```

完全不被原生 Fillet 命令识别：

则停止尝试“命令运行期间 Selection.Add”。

转入：
```text
B 方案
```

---

# 8. B 方案：原生 Fillet + 真正鼠标点击

如果 POC-01 不成立，则使用：
```text
StartCommand("Edge Fillet")
```

启动原生命令后：
```text
助手计算 Source Edge
助手计算 Target Edge
        ↓
启动原生 Fillet
        ↓
通过 Win32 模拟真实鼠标移动
        ↓
移动到 Target Edge 的可见屏幕位置
        ↓
执行真实 Left Click
        ↓
由 CATIA 自己把 Edge 加入 Object(s) to fillet
```

关键原则：

> 不再使用 `Selection.Add()` 向正在运行的 Fillet 命令注入对象。

而是：

> 模拟一次真正的用户鼠标点击。

---

# 9. 鼠标点击目标 Edge

Shape Engine 必须能够获得目标 Edge 的三维采样点：
```text
P0
P1
P2
```

默认优先：
```text
P1 = Edge 中点
```

作为鼠标点击目标。

流程：
```text
3D Edge Point
        ↓
CATIA Viewpoint
        ↓
3D → Screen Projection
        ↓
Desktop Screen Coordinates
        ↓
SetCursorPos
        ↓
MouseDown
        ↓
MouseUp
```

必须考虑：
```text
CATIA 窗口位置
DPI
多显示器
缩放
视角
平移
旋转
投影模式
```

不能直接把 CATIA 世界坐标当成屏幕像素坐标。

---

# 10. 3D→2D 投影

使用 CATIA Viewpoint 信息，尽可能获得：
```text
Origin
Eye
SightDirection
UpDirection
FocusDistance
FieldOfView
ProjectionMode
```

建立：
```text
World → View → Screen
```

变换。

在无法直接获得屏幕矩阵时，可自行实现：
```text
View coordinate transformation
Perspective / Orthographic projection
```

必须对：
```text
Orthographic
Perspective
```

做区分。

---

# 11. 点击点选择策略

不能永远只点击中点。

候选点击点：
```text
P1
P0 * 0.25 + P1 * 0.75
P0 * 0.50 + P1 * 0.50
P0 * 0.75 + P1 * 0.25
P2 * 0.25 + P1 * 0.75
```

如果中点被：
```text
Face
Other Edge
Feature UI
遮挡
```

则尝试其他采样点。

---

# 12. 最终 B 方案安全机制

如果助手无法证明鼠标点击一定落在：
```text
Target Edge
```

必须：
```text
停止自动点击
```

显示：
```text
无法安全确定目标边的屏幕位置。
请手动点击目标边。
```

绝不盲点。

---

# 13. C 方案：CAA

只有：
```text
A：预选 + StartCommand
```

和：
```text
B：启动原生命令 + 真实鼠标点击
```

都无法稳定实现时，再研究：
```text
CAA Command / Selection Agent
```

相关内部交互控制。

CAA 不是第一路线。

---

# 14. 形状级对称——R5/R14

核心目标：

> 用户选择的 Edge 即使不是 Sketch 直接生成的边，也能够找到其镜像对应 Edge。

例如：
```text
Pad
 ↓
Fillet
 ↓
Pattern
 ↓
Boolean
 ↓
当前最终实体
```

用户选择：
```text
新的 BRep Edge
```

Shape Engine 仍能找到：
```text
Mirror Edge
```

---

# 15. Shape Engine 总体架构
```text
用户选择 Edge
        ↓
Source Edge Resolver
        ↓
Current Tip
        ↓
Current BRep Edge Set
        ↓
Edge Fingerprint
        ↓
Symmetry Plane Detection
        ↓
Mirror Source Edge
        ↓
Candidate Edge Matching
        ↓
Safety Validation
        ↓
Source + Target
        ↓
Native Fillet Integration
```

---

# 16. 当前 Tip 的处理

不要扫描整个历史树。

必须优先找到：
```text
Current Tip
```

然后：
```text
Current Tip
    ↓
Current Shape
    ↓
Current Edge Set
```

避免：
```text
已删除 Edge
旧 Feature Edge
历史拓扑
```

污染候选。

---

# 17. Edge 枚举首选方案

首先验证：
```python
Selection.Search("Topology.CGMEdge,sel")
```

以及：
```python
Selection.Search("Topology.CGMEdge,all")
```

优先：
```text
CurrentTip
    ↓
Selection.Add(CurrentTip)
    ↓
Topology.CGMEdge,sel
```

这样尽量限制在：
```text
当前实体
```

范围内。

---

# 18. 不再依赖：

禁止继续把以下方式作为主要 Edge 枚举方法：
```text
Selection.Search("Type=Edge,all")
Shapes.GetBoundary(...)
```

以及：
```text
固定 BRep 编号偏移
```

---

# 19. Edge Fingerprint

为每条 Edge 建立：
```text
EdgeFingerprint
```

推荐字段：
```text
Reference
DisplayName
GeometryType

Length

P0
P1
P2

Direction
Radius
Center
Axis
Angle

BoundingBox

AdjacentFaces
```

无法取得的字段允许：
```text
null
```

不得让整条 Edge 因单字段失败而丢失。

---

# 20. Geometry Match 数学模型

定义镜像平面：

Π:n⋅(x−p)=0\Pi: \mathbf n\cdot(\mathbf x-\mathbf p)=0

其中：
```text
n = 单位法向量
p = 平面上一点
```

点：

xx

关于镜像平面的结果：

x′=x−2n[n⋅(x−p)]x' = x - 2n[n\cdot(x-p)]

实现：
```python
mirror_point(point, plane_origin, plane_normal)
```

---

# 21. Edge 镜像

源 Edge：
```text
P0
P1
P2
```

得到：

P0′=Mirror(P0)P\_0'=Mirror(P\_0) P1′=Mirror(P1)P\_1'=Mirror(P\_1) P2′=Mirror(P2)P\_2'=Mirror(P\_2)

然后在当前 Edge 集合中寻找：
```text
Candidate P0 ≈ P0'
Candidate P1 ≈ P1'
Candidate P2 ≈ P2'
```

同时考虑：
```text
顺向匹配
```

和：
```text
反向匹配
```

因为 Edge 拓扑方向可能相反。

---

# 22. Edge Position Error

定义：

Dp=d(P0′,C0)+d(P1′,C1)+d(P2′,C2)3D\_p= \frac{ d(P\_0',C\_0)+ d(P\_1',C\_1)+ d(P\_2',C\_2) }{3}

---

# 23. Length Error

定义：

Dl=∣LC−LE∣max⁡(LE,ϵ)D\_l= \frac{ |L\_C-L\_E| }{ \max(L\_E,\epsilon) }

---

# 24. Direction Error

定义：

Dd=1−∣v^E⋅v^C∣D\_d= 1- |\hat v\_E\cdot\hat v\_C|

因此：
```text
同方向
反方向
```

都能够得到合理判断。

---

# 25. 圆弧匹配

对于：
```text
Circle / Arc
```

比较：
```text
Radius
Center
Axis
Angle
```

镜像后的圆心：

C′=Mirror(C)C'=Mirror(C)

半径应基本一致：

∣R1−R2∣R1\<Tolerance\frac{|R\_1-R\_2|}{R\_1}\<Tolerance

---

# 26. Subsegment Matching

必须支持：
```text
原 Edge
↓
Fillet / Pocket
↓
相邻 Edge 被削短
```

因此：
```text
Length 不必完全相同
```

对于直线候选，重点比较：
```text
共线
方向一致
空间位置一致
投影区间存在足够重叠
```

点到直线距离：

d(P,L)=∥(P−A)−((P−A)⋅v^)v^∥d(P,L) = \left\\| (P-A)-((P-A)\cdot\hat v)\hat v \right\\|

重叠比例：

Roverlap=Loverlapmin⁡(L1,L2)R\_{overlap} = \frac{L\_{overlap}} {\min(L\_1,L\_2)}

推荐初值：
```text
R_overlap >= 0.70
```

全部阈值必须配置化。

---

# 27. 镜像平面发现

不能假设：
```text
X=0
Y=0
Z=0
```

必须建立：
```text
SymmetryPlaneDetector
```

---

# 28. Level 1：Bounding Box 中心面

获取：
```text
xmin
xmax
ymin
ymax
zmin
zmax
```

生成：

x=xmin+xmax2x=\frac{x\_{min}+x\_{max}}2 y=ymin+ymax2y=\frac{y\_{min}+y\_{max}}2 z=zmin+zmax2z=\frac{z\_{min}+z\_{max}}2

分别测试。

---

# 29. Level 2：主方向

如果模型不是按 XYZ 对齐：

建立点集：
```text
Edge endpoints
Edge midpoint
```

计算中心：

μ=1n∑ixi\mu= \frac1n\sum\_i x\_i

协方差矩阵：

C=1n∑i(xi−μ)(xi−μ)TC= \frac1n \sum\_i (x\_i-\mu)(x\_i-\mu)^T

求特征向量：
```text
v1
v2
v3
```

生成：
```text
normal=v1
normal=v2
normal=v3
```

对应的候选镜像面。

---

# 30. Level 3：Edge Pair 反推镜像平面

对于可能对称的一对 Edge：
```text
E1
E2
```

其中点：
```text
P
Q
```

镜像平面通过：

M=P+Q2M=\frac{P+Q}{2}

其法向量：

n∥(Q−P)n\parallel(Q-P)

生成：

Π:n⋅(x−M)=0\Pi:n\cdot(x-M)=0

然后用整个 Edge 集合进行全局验证。

---

# 31. Symmetry Score

对于镜像平面：
```text
Pi
```

每条 Edge：
```text
Ei
```

计算：
```text
Mirror(Ei, Pi)
```

再在当前 Edge 集合里找最近候选。

定义：

RΠ=NmatchedNvalidR\_\Pi= \frac{N\_{matched}}{N\_{valid}}

同时：

DˉΠ=1N∑iDi\bar D\_\Pi= \frac1N\sum\_iD\_i

优先选择：
```text
高 matched_ratio
低 mean_error
```

的镜像平面。

---

# 32. 初始接受标准

默认：
```text
matched_ratio >= 0.70
```

并且：
```text
median_position_error < PointTolerance
```

源 Edge 必须存在：
```text
唯一高置信度候选
```

否则：
```text
AMBIGUOUS
```

让用户选择。

---

# 33. Candidate Ranking

综合评分：

Score=0.40Sposition+0.20Slength+0.20Sgeometry+0.10Stopology+0.10SsubsegmentScore = 0.40S\_{position} + 0.20S\_{length} + 0.20S\_{geometry} + 0.10S\_{topology} + 0.10S\_{subsegment}

初始值：
```text
Position = 0.40
Length = 0.20
Geometry = 0.20
Topology = 0.10
Subsegment = 0.10
```

全部配置化。

---

# 34. 候选 UI

不得显示：
```text
元素 a-b
Sketch.1;17
BRep #31
```

应该显示：
```text
候选 1
中点：X=80.0 Y=35.0 Z=20.0 mm
长度：32.50 mm
类型：直线
匹配度：98.2%

候选 2
中点：X=82.0 Y=35.0 Z=20.0 mm
长度：31.80 mm
类型：直线
匹配度：91.6%
```

---

# 35. 安全阈值

保留：
```text
中心距离误差：
    > 5%
或者
    > 1 mm
```

则拒绝。

Shape Engine 不得在没有充分证据的情况下自动配对。

---

# 36. 不对称零件

如果：
```text
没有可信镜像平面
```

必须：
```text
NO_CONFIDENT_SYMMETRY
```

而不是：
```text
“找一个最近的 Edge 就算了”
```

---

# 37. 多解

如果：
```text
Best Score
和
Second Score
```

差距过小：
```text
AMBIGUOUS
```

弹出候选选择。

不得盲选。

---

# 38. 误选面

如果用户实际选择：
```text
Face
Plane
Datum
```

立即显示：
```text
你选的是面，不是边线。
```

支持一键移除错误选择。

---

# 39. 原生 Fillet 命令不能被助手接管

助手不得自动修改：
```text
Radius
Propagation
Extremity
Trim
Preview
```

用户看到的是标准 CATIA 原生 Fillet Dialog。

---

# 40. “右键”菜单功能

增加：
```text
对称倒圆角
```

行为：
```text
1. 获取用户当前 Edge
2. 运行 Shape / Sketch Engine
3. 找到对称 Edge
4. 准备 Source + Target
5. 启动 CATIA Edge Fillet
```

不是：
```text
直接创建 Fillet
```

---

# 41. 命令失败时的处理

如果：
```text
无法获得源 Edge
```

提示：
```text
无法识别当前选择的边。
```

如果：
```text
无法找到镜像 Edge
```

提示：
```text
未找到高置信度的对称边。
```

如果：
```text
存在多个候选
```

提示候选窗口。

如果：
```text
原生 Fillet 无法消费预选
```

记录：
```text
NATIVE_FILLET_PRESELECTION_UNSUPPORTED
```

然后进入 B 方案。

---

# 42. COM 生命周期

命令关闭前创建的：
```text
WithTemporaryBody
```

Reference 不能跨命令生命周期使用。

所以：
```text
命令运行期间产生的临时 Reference
```

不得保存到全局。

如果必须在命令关闭后重新构造稳定 Reference：
```text
先关闭命令
↓
重新获取 ActivePart
↓
重新解析 BRep
↓
创建稳定 Reference
```

---

# 43. COM 多线程

Win32 回调线程必须：
```python
comtypes.CoInitialize()
```

结束时：
```python
comtypes.CoUninitialize()
```

不能跨线程复用 COM 对象。

每次都重新获取：
```text
CATIA Application
ActiveDocument
ActivePart
```

---

# 44. 最重要的模型安全规则

识别阶段：
```text
100% Read-Only
```

禁止：
```text
临时 Pad
临时 Pocket
临时 Fillet
临时 Chamfer
Probe Feature
创建后删除
体积试探
```

只有 CATIA 原生 Fillet 命令、且由用户最终点击 OK，才允许真正修改模型。

---

# 45. 日志

完整记录：
```text
CATIA Version
CATIA Service Pack / HotFix
OS
Python Version
Python Bitness
CATIA Bitness

Document
Part
Body
Tip

Source Edge
Source Fingerprint

Symmetry Plane Candidates

Selected Symmetry Plane

Target Edge
Target Fingerprint

Candidate Scores

Native Fillet Integration Method:
    PRESELECT
    or
    MOUSE_CLICK

StartCommand Result

Fillet Dialog Detected
Object List Detection Result

最终状态
```

---

# 46. Native Fillet POC 日志

必须专门记录：
```text
===== NATIVE FILLET POC =====

Selection.Count2 before StartCommand = ...

Selected:
1. ...
2. ...

StartCommand = Edge Fillet

Dialog Found = True / False

Object List:
1. ...
2. ...

Preselection Accepted = True / False
```

---

# 47. Shape Scanner 第一阶段

在完成原生 Fillet POC 之前，不修改整个工程。

先做：
```text
R5 Edge Scanner
```

只读：
```text
Current Tip
↓
Topology.CGMEdge
↓
Edge Reference
↓
Measurable
↓
P0/P1/P2
```

生成：
```text
r5_edge_scan_YYYYMMDD_HHMMSS.json
```

不得修改 CATIA 模型。

---

# 48. 单元测试

至少：
```text
test_mirror_point()
test_plane_distance()
test_edge_distance()
test_line_subsegment()
test_arc_match()
test_edge_score()
test_candidate_ranking()
test_ambiguity()
test_safety_threshold()
```

---

# 49. CATIA 测试件

至少准备：

## Test A
```text
100 × 60 × 40 mm
```

两侧对称 Edge。

---

## Test B

外圈 + 内圈：
```text
┌──────────────┐
│  ┌────────┐  │
│  │        │  │
│  └────────┘  │
└──────────────┘
```

验证：
```text
外圈不会匹配到内圈
```

---

## Test C
```text
Pad
↓
Fillet
```

然后选择：
```text
Fillet 生成的新 Edge
```

仍然必须找到对应 Edge。

---

## Test D
```text
连续 Fillet 1
↓
Fillet 2
↓
Fillet 3
↓
Fillet 4
```

每次都通过原生 Fillet Dialog。

---

## Test E
```text
Pattern
```

测试新生成 Edge。

---

## Test F
```text
Boolean
```

测试新生成 Edge。

---

## Test G

明显不对称零件：

必须拒绝。

---

# 50. Windows 7 / 老 CATIA 兼容性

必须支持：
```text
Windows 7
```

Legacy Build 默认：
```text
Python 3.8
```

因为 Python 3.9+ 不再支持 Windows 7。

---

# 51. 位数兼容

建议：
```text
CATIA 32-bit
    ↓
Python 3.8 x86

CATIA 64-bit
    ↓
Python 3.8 x64
```

默认保证：
```text
CATIA Bitness == Python Bitness
```

---

# 52. CATIA 多版本兼容

不能硬编码：
```text
V5R20
```

必须采用：
```text
Capability Probe
```

启动时检测：
```text
CATIA.Application
ActiveDocument
ActivePart
CurrentTip

Topology.CGMEdge,sel
Topology.CGMEdge,all

Measurable
GetPointsOnCurve

Reference reconstruction
Native Fillet preselection
StartCommand
```

结果：
```text
PASS
FAIL
NOT_SUPPORTED
```

---

# 53. Legacy Backend

推荐架构：
```text
Shape Engine
    │
    ├── Modern/Standard Automation Backend
    │
    └── Legacy Automation Backend
```

上层统一使用：
```text
EdgeFingerprint
SymmetryCandidate
CandidateScore
```

不暴露底层 API 差异。

---

# 54. CAA 作为第三后端

只有 Automation 路线失败时再研究：
```text
CAA
```

可能涉及：
```text
CATBody
CATCell
CATEdge
CATIBRepAccess
CATBRepDecode
CATIPathAccess
CATIBuildPath
```

CAA 不允许未经真实编译和 CATIA 实测就宣称完成。

---

# 55. 不得做的事情

禁止：
```text
1. 直接 AddNewEdgeFilletWithConstantRadius 代替用户原生 Fillet 工作流
2. 命令运行后 Selection.Add() 强行往 Fillet 列表塞对象
3. 固定 BRep 编号偏移
4. 固定 Sketch 元素编号
5. 试探性建模
6. 在用户模型中测试探针特征
7. 自动点击 CATIA 的 OK
8. 未确认目标 Edge 屏幕位置就模拟鼠标点击
9. 多解情况下自动盲选
10. 未验证就声称兼容某个 CATIA 版本
```

---

# 56. 最终产品架构
```text
CATIA
 │
 ▼
User Edge Selection
 │
 ▼
Right Click / Ctrl+Alt+R
 │
 ▼
Source Edge Resolver
 │
 ├── Sketch Engine
 │
 └── Shape Engine
       │
       ├── Edge Scanner
       ├── Fingerprint
       ├── Symmetry Detector
       ├── Mirror Transform
       ├── Edge Matcher
       └── Safety Validator
 │
 ▼
Source Edge + Target Edge
 │
 ├───────────────┐
 ▼               ▼
A. Preselection   B. Native Dialog + Real Mouse Click
 ▼               ▼
CATIA StartCommand("Edge Fillet")
 │               │
 └───────┬───────┘
         ▼
CATIA 原生 Edge Fillet Definition
         │
         ▼
用户修改 Radius / Propagation / Preview
         │
         ▼
用户点击 OK
         │
         ▼
CATIA 生成原生 Fillet Feature
```

---

# 57. 开发顺序

必须严格按照以下顺序实施。

## STEP 1

检查现有 v0.4.8 源码。

不要重写。

---

## STEP 2

实现：
```text
POC-01
Preselection + StartCommand
```

---

## STEP 3

实测：
```text
Object(s) to fillet
```

能否出现两个 Edge。

---

## STEP 4

如果成功：

直接以该机制作为主方案。

---

## STEP 5

如果失败：

实现：
```text
POC-02
StartCommand + Win32 real mouse click
```

---

## STEP 6

确认 CATIA 原生 Fillet 能由真实鼠标点击添加 Target Edge。

---

## STEP 7

开发：
```text
R5 Edge Scanner
```

---

## STEP 8

实现：
```text
Edge Fingerprint
```

---

## STEP 9

实现：
```text
Mirror Point / Segment / Arc
```

---

## STEP 10

实现：
```text
Bounding Box Symmetry
```

---

## STEP 11

实现：
```text
Global Symmetry Score
```

---

## STEP 12

实现：
```text
Candidate Edge Matcher
```

---

## STEP 13

实现：
```text
Subsegment Matcher
```

---

## STEP 14

实现：
```text
Topology Context
```

能拿到就增强，拿不到不能阻塞。

---

## STEP 15

接入：
```text
Sketch Engine
+
Shape Engine
```

---

## STEP 16

测试：
```text
Pad
Fillet
Pattern
Boolean
连续多次 Fillet
```

---

## STEP 17

最后再做：
```text
CAA Backend
```

---

# 58. 成功标准

最终必须实现：
```text
用户：
点 Edge A
↓
右键“对称倒圆角”
```

助手：
```text
识别 Edge A
找到 Edge B
```

然后：
```text
CATIA 原生 Edge Fillet
```

对象列表中：
```text
Edge A
Edge B
```

用户：
```text
自行输入/修改 Radius
自行选择 Propagation
自行 Preview
自行点击 OK
```

最终：
```text
CATIA 原生生成 1 个 Fillet Feature
```

助手：
```text
没有直接建模
没有自动点击 OK
没有试探性修改模型
```

---

# 59. 最核心的数学问题

整个 R5/R14 可以定义为：

Etarget=arg⁡min⁡Ei∈SD(Mirror(Esource,Π),Ei)E\_{target} = \arg\min\_{E\_i\in S} D( Mirror(E\_{source},\Pi), E\_i )

其中：
```text
S = 当前 Tip 的全部 Edge
Π = 检测出来的镜像平面
D = 综合几何/拓扑距离
```

完整过程：

Current Tip→Edge Set→Find Π→Mirror(Esource)→Search(Etarget)→Validate→Native Fillet\boxed{ Current\ Tip \rightarrow Edge\ Set \rightarrow Find\ \Pi \rightarrow Mirror(E\_{source}) \rightarrow Search(E\_{target}) \rightarrow Validate \rightarrow Native\ Fillet }

---

# 60. 第一件必须实际完成的事情

开始开发时，不要马上改完整助手。

只完成：
```text
POC-01
```

即：
```python
sel.Clear()
sel.Add(edge1)
sel.Add(edge2)

catia.StartCommand("Edge Fillet")
```

然后用真实 CATIA 测试回答：
```text
1. Fillet Dialog 是否成功启动？
2. Object(s) to fillet 是否已有两个 Edge？
3. 两个 Edge 是否可被 CATIA 正常 Preview？
4. 用户是否仍然可以修改 Radius？
5. 用户是否仍然可以修改 Propagation？
6. 用户点击 OK 后是否只生成一个原生 Fillet？
```

只有这 6 项确认后，才继续整合 Shape Engine。

不要提前实现大量复杂代码。

---

# 61. 最终原则

本项目最终不是：
```text
“自动替用户完成一个圆角建模”
```

而是：
```text
“自动替用户识别第二条应该一起倒圆角的对称 Edge，
然后把两条 Edge 交给 CATIA 原生倒圆角命令，
用户仍然拥有完整的 CATIA 原生命令控制权。”
```

这是整个项目的最终产品定义。
