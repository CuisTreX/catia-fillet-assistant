# CAA RADE 版「形状级对称倒圆角助手」——开发说明

> 目标：不依赖草图编号、不依赖"点两条边"，而是直接在当前**实体几何**上枚举所有边，
> 自动找到与所选边关于指定对称面/轴互相对称的那条边，并把两条边一次性倒圆角。
> 这是自动化（VBScript/COM）做不到的部分，必须用 CAA。

## 1. 为什么必须 CAA（实测结论）

CATIA V5R20 的自动化接口**没有暴露"枚举实体全部边"的能力**，本机逐条验证过：

| 方式 | 结果 |
| --- | --- |
| `Selection.Search("Type=Edge,all")` 等按类型搜索 | 语法不成立 |
| `Shapes.GetBoundary(标签)` | 调用失败 |
| `Measurable` 量 BRep 边 | 对引用不可用 |
| 命名单元 `边线.N` | 只对手动点选的那条有效，命令关闭即消失 |

而 CAA 里有 `CATBody::GetAllCells(CATEdge, ...)`，可以拿到实体**所有边**，
再用 `CATMathTransformation`（镜像/旋转）+ `CATEdge` 的曲线几何做匹配 —— 这条路是通的。

## 2. 本机环境检查结果（2026-09-25）

| 项目 | 状态 |
| --- | --- |
| CATIA V5R20（5.20.0.9268，win_b64） | ✅ 已装：`D:\Programs\CATIA` |
| CAA RADE 开发环境 | ❌ **未安装**（无 CAA/RADE 目录，无 `CAA_*` 环境变量） |
| C/C++ 编译器（MSVC，R20 对应 VC8/VC9） | ❌ **未安装**（只有 Windows SDK 8.1，无 `cl.exe`/`nmake`） |
| CATIA 运行库接口 DLL（`CATTopologicalObjects.dll` 等） | ✅ 存在（但这是**运行时**，不含 CAA 头文件与导入库） |

## 3. 需要准备的三样东西（缺一不可）

1. **CAA RADE V5R20（win_b64）**：与已装 CATIA 同版本、同位数。来源：Dassault 客户账号的
   DSx Client / 介质，或代理商（**需要 CAA 相关许可**）。
2. **编译器**：R20 时代 CAA 官方支持 **Visual Studio 2005 (VC8)** / **2008 (VC9)**；
   64 位需对应的 x64 工具链。装完后 CAA 才能 `mkGetPreq` + `nmake`。
3. **运行端许可**：目标 CATIA 必须能加载自定义 CAA 工作台（Development/CAA 相关装配）。
   纯商业配置的 CATIA **不能**加载自定义 workbench —— 这一点必须提前和 IT/代理商确认，
   否则代码能编译、却无法在客户 CATIA 里运行。

## 4. 本目录结构与编译步骤（RADE 装好后执行）

```
caa\
  SymmetryAssistant.edu\        # CAA "education"（工作空间）目录，含 .edu 简介文件
  SymmetryAssistant.m\          # 模块（一个 CAA 命令 + 算法）
      src\SymmetryFinder.cpp    # 核心算法：枚举边 → 镜像匹配 → 返回对称边
      src\SymmetryFinder.h
      src\SymmetryAssistantCmd.cpp   # 命令骨架（选中一条边 → 调算法 → 用 ShapeFactory 倒圆角）
      LocalInterfaces\           # 本模块对外头文件
  SymmetryAssistantFrm\         # 框架（工具栏/图标资源，可选；先用菜单项）
  docs\                         # 设计说明、部署说明
```

编译（RADE 环境变量就绪后）：

```bat
call "%CAA_V5_RADE_LEVEL%\intel_a\code\bin\mkGetPreq.bat" %*
cd caa\SymmetryAssistant.edu
nmake -f SymmetryAssistant.mk        :: 或用 CAA 的 mkmk / CATIA 的命令模板
```

> 说明：具体命令模板以 RADE 安装自带示例（`CAADoc`）为准；本文件只固定**目录与代码结构**，
> 避免写成 RADE 版本相关的错误命令。

## 5. 核心算法（已写进代码骨架）

```
输入：当前 PartDocument + 选中的一条边 + 对称面（或自动从实体包围盒/主方向推断）
1) body  = 选中边所属的 CATBody（当前实体，含之前所有倒圆角的结果）
2) edges = body->GetAllCells(CATEdge, ...)              // 枚举"当前形状"的全部边
3) 对每条边取几何：CATCurve（直线/圆/样条）+ 端点
4) 用 CATMathTransformation::CreateReflection(平面) 把源边几何镜像
5) 在 edges 中找几何完全重合的一条（容差 1e-3）→ 即对称边
6) 循环/递归：可一次对多对边批量生成倒圆角（形状级对称，含圆角之后的新形状）
输出：对称边 + 生成 EdgeFillet 特征（可配置"只选中"或"直接倒圆角"）
```

关键点：算法只依赖**几何本身**，因此：

* 第一刀圆角之后产生的新边、新形状，照样能被枚举与匹配（这正是你要的能力）；
* 不再需要 `Sketch.1;N` 编号推断；
* 不写入任何试探特征。

## 6. 与现有自动化助手的关系

| | 自动化助手（已交付 v0.4.8） | CAA 版（本项目） |
| --- | --- | --- |
| 覆盖边 | 草图拉伸生成的边 | **当前实体全部边**（含圆角/阵列/布尔之后的新边） |
| 对称依据 | 草图编号 + 几何 | **纯几何**（平面镜像/轴旋转） |
| 依赖 | 无（便携版 exe） | 需要 RADE 编译 + 目标 CATIA 具备加载自定义工作台的许可 |
| 用途 | 日常快速倒圆角 | 形状级对称的"专业版" |

两者共存：日常用助手，遇到"圆角之后的新形状对称"用 CAA 版命令。
