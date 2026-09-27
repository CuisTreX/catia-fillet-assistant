// CAA RADE —— 形状级对称边查找器（核心算法，与本机 CATIA V5R20 对应）
//
// 作用：在当前实体（含之前所有倒圆角的结果）上枚举全部边，
//       对"源边"的几何做镜像/旋转后，在实体边集合里找几何完全重合的那条边。
//
// 依赖的 CAA 框架：
//   CATObjectModelerBase（CATBody / CATCell / CATEdge / CATVertex）
//   CATGeometricObjects（CATCurve / CATLine / CATCircle）
//   CATMathematics（CATMathTransformation / CATMathPlane / CATMathPoint）
//
// 注意：本文件是"可编译骨架"，需要 CAA RADE V5R20 + VC8/VC9 才能构建（见 caa/README.md）。

#ifndef SymmetryFinder_H
#define SymmetryFinder_H

#include "CATBoolean.h"
#include "CATMathTransformation.h"
#include "CATLISTP_CATCell.h"
#include "CATLISTP_CATMathPoint.h"

class CATBody;
class CATEdge;
class CATGeoFactory;
class CATTopData;
class CATCurve;
class CATMathPlane;

/** 对称变换类型 */
enum SymmetryKind
{
    SymmetryMirror = 0,   // 关于平面镜像（轴对称）
    SymmetryRotate = 1    // 绕轴旋转（中心/旋转对称，默认关闭）
};

/** 找到的一条对称边（含匹配误差，便于界面提示） */
struct SymmetryMatch
{
    CATEdge* Target;
    double   Error;       // 采样点最大偏差（mm）
    SymmetryMatch() : Target(NULL), Error(0.0) {}
};

class SymmetryFinder
{
public:
    SymmetryFinder(CATGeoFactory* iFactory, CATTopData* iTopData);
    ~SymmetryFinder();

    /** 枚举实体全部边（关键：CAA 才能做到，自动化做不到） */
    void EnumerateAllEdges(CATBody* iBody, CATLISTP(CATCell)& oEdges) const;

    /**
     * 在实体上找与 iSource 关于 iTransform 对称的边。
     * @param iBody       当前实体（含此前所有倒圆角结果）
     * @param iSource     用户选中的边
     * @param iTransform  对称变换（镜像/旋转）
     * @param iTolerance  几何容差（默认 1e-3 mm）
     * @param oMatch      输出：匹配到的对称边 + 误差
     * @return 找到返回 TRUE
     */
    CATBoolean FindSymmetricEdge(CATBody* iBody,
                                 CATEdge* iSource,
                                 const CATMathTransformation& iTransform,
                                 double iTolerance,
                                 SymmetryMatch& oMatch) const;

    /**
     * 批量：找所有与已选边集合对称的边（用于"一次把其余对称处都倒上"）。
     * @param iSources 已选中的边（通常 1 条）
     * @param oMatches 输出：与 iSources 一一对应的对称边
     */
    CATBoolean FindAllSymmetricEdges(CATBody* iBody,
                                     const CATLISTP(CATCell)& iSources,
                                     const CATMathTransformation& iTransform,
                                     double iTolerance,
                                     CATLISTP(CATCell)& oMatches) const;

    /** 由实体包围盒/主方向自动推断对称面（先用最简实现：三张中心面） */
    CATBoolean SuggestSymmetryPlanes(CATBody* iBody, CATLISTP(CATMathPlane)& oPlanes) const;

private:
    /** 取边的几何采样点（直线取端点+中点；圆取四等分点；样条按参数采样） */
    CATBoolean SampleEdge(CATEdge* iEdge, CATLISTP(CATMathPoint)& oPoints) const;

    /** 两条边的采样点集在容差内是否完全重合（双向） */
    CATBoolean PointsCoincide(const CATLISTP(CATMathPoint)& iA,
                              const CATLISTP(CATMathPoint)& iB,
                              double iTolerance,
                              double& oMaxError) const;

    CATGeoFactory* _Factory;
    CATTopData*    _TopData;
};

#endif
