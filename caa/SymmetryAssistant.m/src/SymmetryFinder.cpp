// CAA RADE —— 形状级对称边查找器（实现骨架）
//
// 算法：几何匹配（不依赖任何命名/编号）：
//   1) 枚举实体全部边（CATBody::GetAllCells(CATEdge, ...)）
//   2) 对源边采样（直线：端点+中点；圆/样条：参数采样）
//   3) 采样点按对称变换（镜像/旋转）变换
//   4) 遍历实体所有边，比较采样点是否重合（双向容差）
//   5) 返回误差最小者
//
// 该算法天然支持"第一刀圆角之后的新形状"：因为枚举的是当前实体的边。

#include "SymmetryFinder.h"

#include "CATBody.h"
#include "CATEdge.h"
#include "CATVertex.h"
#include "CATGeoFactory.h"
#include "CATTopData.h"
#include "CATCurve.h"
#include "CATLine.h"
#include "CATCircle.h"
#include "CATMathPoint.h"
#include "CATMathPlane.h"
#include "CATMathTransformation.h"
#include "CATMathVector.h"
#include "CATSoftwareConfiguration.h"

#include "CATLISTP_CATCell.h"
#include "CATLISTP_CATMathPoint.h"

#include <math.h>

// ------------------------------------------------------------------ 生命周期
SymmetryFinder::SymmetryFinder(CATGeoFactory* iFactory, CATTopData* iTopData)
    : _Factory(iFactory), _TopData(iTopData)
{
}

SymmetryFinder::~SymmetryFinder() {}

// ------------------------------------------------------------------ 枚举全部边
// CAA 的 CATBody::GetAllCells 可以按类型取到实体的全部单元 —— 这正是自动化拿不到的能力。
void SymmetryFinder::EnumerateAllEdges(CATBody* iBody, CATLISTP(CATCell)& oEdges) const
{
    oEdges.RemoveAll();
    if (iBody == NULL) return;

    // 说明：不同 CAA 版本该 API 的签名略有差异（部分版本为 GetAllCells(CATCell*, CATLISTP(CATCell)&)）
    // 这里按 R20 的用法编写；若编译报签名不符，按 RADE 自带 CAADoc 示例调整即可。
    CATLISTP(CATCell) allCells;
#ifdef CAA_HAS_BODY_GETALLCELLS_WITH_TYPE
    iBody->GetAllCells(CATEdge::ClassName(), allCells);
#else
    iBody->GetAllCells(allCells);
#endif

    for (int i = 1; i <= allCells.Size(); ++i)
    {
        CATCell* cell = allCells[i];
        if (cell == NULL) continue;
        if (cell->IsAKindOf(CATEdge::ClassName()))
            oEdges.Append(cell);
    }
}

// ------------------------------------------------------------------ 边的采样
CATBoolean SymmetryFinder::SampleEdge(CATEdge* iEdge, CATLISTP(CATMathPoint)& oPoints) const
{
    oPoints.RemoveAll();
    if (iEdge == NULL) return FALSE;

    CATCurve* curve = iEdge->GetCurve();   // 边上的几何曲线
    if (curve == NULL) return FALSE;

    // 直线：端点 + 中点
    if (curve->IsAKindOf(CATLine::ClassName()))
    {
        CATMathPoint start, end;
        if (!iEdge->GetVerticesPoints(start, end)) return FALSE;
        oPoints.Append(new CATMathPoint(start));
        oPoints.Append(new CATMathPoint((start + end) * 0.5));
        oPoints.Append(new CATMathPoint(end));
        return TRUE;
    }

    // 圆/圆弧：取参数区间 5 等分
    if (curve->IsAKindOf(CATCircle::ClassName()))
    {
        double first = 0.0, last = 0.0;
        curve->GetParamExtents(first, last);
        const int n = 5;
        for (int k = 0; k <= n; ++k)
        {
            double t = first + (last - first) * (double)k / (double)n;
            CATMathPoint p;
            curve->Eval(t, p);
            oPoints.Append(new CATMathPoint(p));
        }
        return TRUE;
    }

    // 其他曲线（样条等）：按参数均匀采样
    double first = 0.0, last = 0.0;
    curve->GetParamExtents(first, last);
    const int n = 12;
    for (int k = 0; k <= n; ++k)
    {
        double t = first + (last - first) * (double)k / (double)n;
        CATMathPoint p;
        curve->Eval(t, p);
        oPoints.Append(new CATMathPoint(p));
    }
    return oPoints.Size() > 0;
}

// ------------------------------------------------------------------ 点集重合判定
CATBoolean SymmetryFinder::PointsCoincide(const CATLISTP(CATMathPoint)& iA,
                                          const CATLISTP(CATMathPoint)& iB,
                                          double iTolerance,
                                          double& oMaxError) const
{
    oMaxError = 0.0;
    if (iA.Size() == 0 || iB.Size() == 0) return FALSE;

    // 双向最近距离（对采样密度不敏感）
    for (int i = 1; i <= iA.Size(); ++i)
    {
        double best = 1e30;
        for (int j = 1; j <= iB.Size(); ++j)
        {
            double d = iA[i]->DistanceTo(*iB[j]);
            if (d < best) best = d;
        }
        if (best > oMaxError) oMaxError = best;
    }
    for (int j = 1; j <= iB.Size(); ++j)
    {
        double best = 1e30;
        for (int i = 1; i <= iA.Size(); ++i)
        {
            double d = iB[j]->DistanceTo(*iA[i]);
            if (d < best) best = d;
        }
        if (best > oMaxError) oMaxError = best;
    }
    return oMaxError <= iTolerance;
}

// ------------------------------------------------------------------ 找一条对称边
CATBoolean SymmetryFinder::FindSymmetricEdge(CATBody* iBody,
                                             CATEdge* iSource,
                                             const CATMathTransformation& iTransform,
                                             double iTolerance,
                                             SymmetryMatch& oMatch) const
{
    if (iBody == NULL || iSource == NULL) return FALSE;

    // 1) 源边采样并变换
    CATLISTP(CATMathPoint) srcPts;
    if (!SampleEdge(iSource, srcPts)) return FALSE;
    CATLISTP(CATMathPoint) movedPts;
    for (int i = 1; i <= srcPts.Size(); ++i)
        movedPts.Append(new CATMathPoint(iTransform * (*srcPts[i])));

    // 2) 枚举当前实体的全部边（含此前倒圆角产生的新边）
    CATLISTP(CATCell) edges;
    EnumerateAllEdges(iBody, edges);

    // 3) 逐条比较，取误差最小者
    CATBoolean found = FALSE;
    double bestError = 1e30;
    for (int e = 1; e <= edges.Size(); ++e)
    {
        CATEdge* cand = (CATEdge*)edges[e];
        if (cand == iSource) continue;
        CATLISTP(CATMathPoint) candPts;
        if (!SampleEdge(cand, candPts)) continue;
        double err = 0.0;
        if (PointsCoincide(movedPts, candPts, iTolerance, err) && err < bestError)
        {
            bestError = err;
            oMatch.Target = cand;
            oMatch.Error = err;
            found = TRUE;
        }
        for (int k = 1; k <= candPts.Size(); ++k) delete candPts[k];
        candPts.RemoveAll();
    }

    for (int k = 1; k <= srcPts.Size(); ++k) delete srcPts[k];
    for (int k = 1; k <= movedPts.Size(); ++k) delete movedPts[k];
    return found;
}

// ------------------------------------------------------------------ 批量
CATBoolean SymmetryFinder::FindAllSymmetricEdges(CATBody* iBody,
                                                 const CATLISTP(CATCell)& iSources,
                                                 const CATMathTransformation& iTransform,
                                                 double iTolerance,
                                                 CATLISTP(CATCell)& oMatches) const
{
    oMatches.RemoveAll();
    for (int i = 1; i <= iSources.Size(); ++i)
    {
        SymmetryMatch match;
        if (FindSymmetricEdge(iBody, (CATEdge*)iSources[i], iTransform, iTolerance, match))
            oMatches.Append(match.Target);
    }
    return oMatches.Size() > 0;
}

// ------------------------------------------------------------------ 自动建议对称面
CATBoolean SymmetryFinder::SuggestSymmetryPlanes(CATBody* iBody, CATLISTP(CATMathPlane)& oPlanes) const
{
    oPlanes.RemoveAll();
    if (iBody == NULL) return FALSE;

    // 最简实现：用实体包围盒中心 + 三个主方向构造三张中心面；
    // 后续可升级为"用质量特性/主惯性轴"推断（CATBody 的 GetBoundingBox / 惯性接口）。
    CATMathBox box;
    if (!iBody->GetBoundingBox(box)) return FALSE;

    CATMathPoint center;
    box.GetCenter(center);
    oPlanes.Append(new CATMathPlane(center, CATMathVector(1, 0, 0)));
    oPlanes.Append(new CATMathPlane(center, CATMathVector(0, 1, 0)));
    oPlanes.Append(new CATMathPlane(center, CATMathVector(0, 0, 1)));
    return TRUE;
}
