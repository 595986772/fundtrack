"""将多只基金的单基金 Assessment 汇总成模拟组合研判。

这一层仍然完全由 Python 执行。它不把单基金波动率简单相加，也不伪造组合最大回撤；
第一版只计算权重集中度、单基金证据分的加权贡献，以及跨基金去重后的关键证据。
"""

from __future__ import annotations

from datetime import date
from typing import Any
from app.services.errors import ServiceError

from app.services.fund_assessment import (
    RELIABILITY_WEIGHTS,
    RELEVANCE_WEIGHTS,
    FundAssessmentError,
    EvidenceFusionService,
    evidence_fusion_service,
)
from app.services.portfolios import (
    PortfolioError,
    PortfolioNotFoundError,
    PortfolioService,
    portfolio_service,
)


ROLE_LABELS = {
    "broad_market_index": "宽基指数",
    "industry_theme": "行业主题",
    "bond_fund": "债券基金",
}
RELEVANCE_RANK = {"context": 0, "related": 1, "direct": 2}


class PortfolioAssessmentError(ServiceError):
    """组合或任一单基金无法完成确定性研判。"""


class PortfolioAssessmentService:
    """复用单基金研判，并按持仓权重汇总组合证据。"""

    def __init__(
        self,
        *,
        portfolios: PortfolioService = portfolio_service,
        fund_assessments: EvidenceFusionService = evidence_fusion_service,
    ) -> None:
        self.portfolios = portfolios
        self.fund_assessments = fund_assessments

    def assess(
        self,
        *,
        user_id: str,
        portfolio_id: str,
        months: int = 12,
    ) -> dict[str, Any]:
        """生成组合集中度、基金贡献和去重证据。"""

        try:
            portfolio = self.portfolios.get(
                user_id=user_id,
                portfolio_id=portfolio_id,
            )
            assessments = {
                holding["fund_code"]: self.fund_assessments.assess(
                    fund_code=holding["fund_code"],
                    months=months,
                )
                for holding in portfolio["holdings"]
            }
        except PortfolioNotFoundError:
            raise
        except (PortfolioError, FundAssessmentError, KeyError, TypeError) as exc:
            raise PortfolioAssessmentError(f"无法生成组合研判：{exc}") from exc

        contributions = self._fund_contributions(
            portfolio=portfolio,
            assessments=assessments,
        )
        caution_points = round(
            sum(item["weighted_caution_points"] for item in contributions),
            2,
        )
        supportive_points = round(
            sum(item["weighted_supportive_points"] for item in contributions),
            2,
        )
        concentration = self._concentration(portfolio=portfolio)
        allocations = self._allocations(portfolio=portfolio)
        evidence_groups = self._aggregate_evidence(
            portfolio=portfolio,
            assessments=assessments,
        )
        state_code, state_label = self._state(
            caution_points=caution_points,
            supportive_points=supportive_points,
            concentration_code=concentration["state_code"],
        )

        all_evidence = [
            *evidence_groups["risk_evidence"],
            *evidence_groups["supporting_evidence"],
            *evidence_groups["neutral_evidence"],
        ]
        event_ids = {
            item["evidence_id"]
            for item in all_evidence
            if item["kind"] == "market_event"
        }
        active_event_ids = {
            item["evidence_id"]
            for item in all_evidence
            if item["kind"] == "market_event"
            and float(item.get("decay_factor", 1.0)) > 0
        }
        assessment_dates = [
            date.fromisoformat(assessment["assessment_date"])
            for assessment in assessments.values()
        ]
        assessment_date = min(assessment_dates)
        period_label = {6: "近 6 个月", 12: "近 1 年", 36: "近 3 年"}[months]
        # 权重来自 PortfolioService：实盘模式按份额乘最新已保存净值计算，
        # allocation 模式才使用用户目标比例。描述必须与事实源一致，不能误导 Agent。
        actual = portfolio.get("position_mode") == "actual"
        weight_basis = "current_position_market_value" if actual else "target_allocation"
        weight_note = (
            "权重按已录入份额与各基金最新已保存净值计算；不等于业绩工具按总回报推进的模型期末权重。"
            if actual else "组合权重是用户模拟目标权重，不代表真实账户、成本或交易记录。"
        )

        summary = (
            f"组合包含 {portfolio['holding_count']} 只基金，最大单只权重 "
            f"{concentration['max_weight_pct']:.2f}%，集中度为“{concentration['state_label']}”。"
            f"按持仓权重汇总{period_label}单基金证据后，组合呈现“{state_label}”："
            f"风险证据 {caution_points:.2f} 分，支撑证据 {supportive_points:.2f} 分。"
        )

        return {
            "data_kind": "deterministic_portfolio_assessment",
            "portfolio_id": portfolio_id,
            "user_id": user_id,
            "portfolio_name": portfolio["name"],
            "position_mode": portfolio.get("position_mode", "allocation"),
            "weight_basis": weight_basis,
            "period_months": months,
            "assessment_date": assessment_date.isoformat(),
            "state_code": state_code,
            "state_label": state_label,
            "confidence": "medium",
            "confidence_reason": (
                "全部持仓均有风险、市场和事件证据；各来源有独立截止日，"
                "且尚未加入基金间相关性。"
            ),
            "summary": summary,
            "concentration": concentration,
            "allocations": allocations,
            "fund_contributions": contributions,
            "score_breakdown": {
                "supportive_points": supportive_points,
                "caution_points": caution_points,
                "net_points": round(supportive_points - caution_points, 2),
                "method": (
                    "组合分 = Σ(单基金证据分 × 持仓权重)；共享 evidence_id 在展示层去重，"
                    "同时保留覆盖基金和累计权重。"
                ),
            },
            **evidence_groups,
            "evidence_count": len(all_evidence),
            "event_count": len(event_ids),
            "event_cluster_count": len(event_ids),
            "active_event_count": len(active_event_ids),
            "expired_event_count": len(event_ids - active_event_ids),
            "missing_information": [
                "缺少基金收益相关性和协方差矩阵，当前不计算真实组合波动率。",
                "本接口的证据分不直接纳入季报持仓穿透；底层证券与行业暴露由独立 "
                "exposure 接口展示。",
                weight_note,
                "市场与事件有独立截止日；事件已按影响周期计算有效期并展示聚类热度。",
            ],
            "limitations": [
                "加权证据分用于比较组合风险来源，不是行业评级或收益预测。",
                "单基金最大回撤不能直接加权成真实组合最大回撤。",
                "当前只有三只教学样例基金，无法代表完整基金市场。",
            ],
            "methodology": (
                "先为每只持仓生成相同区间的单基金 Assessment，再按上述权重口径汇总两侧分值；"
                "HHI 为各持仓权重小数的平方和，有效基金数为 1/HHI；最后按 evidence_id "
                "合并共享市场和事件证据；相似报道按 cluster_id 去重，事件分值沿用"
                "单基金层的时间衰减系数，热度只用于观察而不直接放大分数。"
            ),
            "disclaimer": "手工录入的基金组合与当前数据快照，仅用于学习和研究，不构成投资建议。",
        }

    @staticmethod
    def _fund_contributions(
        *,
        portfolio: dict[str, Any],
        assessments: dict[str, dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """把每只基金的两侧分值乘以组合权重。"""

        contributions: list[dict[str, Any]] = []
        for holding in portfolio["holdings"]:
            assessment = assessments[holding["fund_code"]]
            score = assessment["score_breakdown"]
            weight = holding["weight_pct"] / 100
            contributions.append(
                {
                    "fund_code": holding["fund_code"],
                    "short_name": holding["short_name"],
                    "weight_pct": holding["weight_pct"],
                    "assessment_state_code": assessment["state_code"],
                    "assessment_state_label": assessment["state_label"],
                    "caution_points": score["caution_points"],
                    "supportive_points": score["supportive_points"],
                    "weighted_caution_points": round(
                        score["caution_points"] * weight,
                        2,
                    ),
                    "weighted_supportive_points": round(
                        score["supportive_points"] * weight,
                        2,
                    ),
                }
            )
        return sorted(
            contributions,
            key=lambda item: item["weight_pct"],
            reverse=True,
        )

    @staticmethod
    def _concentration(*, portfolio: dict[str, Any]) -> dict[str, Any]:
        """用 HHI 和最大单只权重描述组合是否过度集中。"""

        holdings = portfolio["holdings"]
        hhi = sum((holding["weight_pct"] / 100) ** 2 for holding in holdings)
        largest = max(holdings, key=lambda item: item["weight_pct"])
        max_weight = largest["weight_pct"]

        if max_weight >= 50 or hhi >= 0.42:
            state_code, state_label = "high", "高度集中"
        elif max_weight >= 40 or hhi >= 0.34:
            state_code, state_label = "moderate", "中度集中"
        else:
            state_code, state_label = "balanced", "相对均衡"

        return {
            "hhi": round(hhi, 4),
            "effective_fund_count": round(1 / hhi, 2),
            "max_weight_pct": round(max_weight, 2),
            "max_weight_fund_code": largest["fund_code"],
            "state_code": state_code,
            "state_label": state_label,
        }

    @staticmethod
    def _allocations(*, portfolio: dict[str, Any]) -> list[dict[str, Any]]:
        """把持仓按当前教学样例角色汇总，供前端快速展示。"""

        totals: dict[str, float] = {}
        for holding in portfolio["holdings"]:
            role = holding["sample_role"]
            totals[role] = totals.get(role, 0) + holding["weight_pct"]
        return [
            {
                "sample_role": role,
                "label": ROLE_LABELS.get(role, role),
                "weight_pct": round(weight, 2),
            }
            for role, weight in sorted(
                totals.items(),
                key=lambda item: item[1],
                reverse=True,
            )
        ]

    def _aggregate_evidence(
        self,
        *,
        portfolio: dict[str, Any],
        assessments: dict[str, dict[str, Any]],
    ) -> dict[str, list[dict[str, Any]]]:
        """跨基金按 evidence_id 去重，同时累计覆盖权重和加权分值。"""

        weights = {
            holding["fund_code"]: holding["weight_pct"]
            for holding in portfolio["holdings"]
        }
        grouped: dict[str, dict[str, dict[str, Any]]] = {
            "risk_evidence": {},
            "supporting_evidence": {},
            "neutral_evidence": {},
        }

        for fund_code, assessment in assessments.items():
            for group_name in grouped:
                for item in assessment[group_name]:
                    target = grouped[group_name].get(item["evidence_id"])
                    if target is None:
                        target = {
                            "evidence_id": item["evidence_id"],
                            "kind": item["kind"],
                            "direction": item["direction"],
                            "strength": item["strength"],
                            "reliability": item["reliability"],
                            "relevance": item["relevance"],
                            "title": item["title"],
                            "value_text": item["value_text"],
                            "explanation": item["explanation"],
                            "data_date": item["data_date"],
                            "source_name": item["source_name"],
                            "source_url": item["source_url"],
                            "event_id": item["event_id"],
                            "cluster_id": item.get("cluster_id"),
                            "cluster_member_count": item.get("cluster_member_count"),
                            "cluster_source_count": item.get("cluster_source_count"),
                            "heat_score": item.get("heat_score"),
                            "heat_level": item.get("heat_level"),
                            "decay_factor": float(item.get("decay_factor", 1.0)),
                            "freshness_status": item.get("freshness_status"),
                            "valid_until": item.get("valid_until"),
                            "age_days": item.get("age_days"),
                            "affected_fund_codes": [],
                            "portfolio_weight_coverage_pct": 0.0,
                            "weighted_points": 0.0,
                        }
                        grouped[group_name][item["evidence_id"]] = target

                    if RELEVANCE_RANK[item["relevance"]] > RELEVANCE_RANK[target["relevance"]]:
                        target["relevance"] = item["relevance"]
                    if fund_code not in target["affected_fund_codes"]:
                        target["affected_fund_codes"].append(fund_code)
                        target["portfolio_weight_coverage_pct"] += weights[fund_code]

                    if item["direction"] != "neutral":
                        raw_points = (
                            item["strength"]
                            * RELIABILITY_WEIGHTS[item["reliability"]]
                            * RELEVANCE_WEIGHTS[item["relevance"]]
                            * float(item.get("decay_factor", 1.0))
                        )
                        target["weighted_points"] += raw_points * weights[fund_code] / 100

        result: dict[str, list[dict[str, Any]]] = {}
        for group_name, group in grouped.items():
            items = list(group.values())
            for item in items:
                item["portfolio_weight_coverage_pct"] = round(
                    item["portfolio_weight_coverage_pct"],
                    2,
                )
                item["weighted_points"] = round(item["weighted_points"], 2)
                item["affected_fund_codes"].sort()
            result[group_name] = sorted(
                items,
                key=lambda item: (
                    item["weighted_points"],
                    item["portfolio_weight_coverage_pct"],
                    item["data_date"],
                ),
                reverse=True,
            )
        return result

    @staticmethod
    def _state(
        *,
        caution_points: float,
        supportive_points: float,
        concentration_code: str,
    ) -> tuple[str, str]:
        """根据两侧差值和集中度给出组合层标签。"""

        difference = caution_points - supportive_points
        if (difference >= 5) or (
            concentration_code == "high" and difference >= 0
        ):
            return "high_attention", "组合风险信号占优"
        if difference >= 2:
            return "cautious", "组合偏谨慎"
        if difference <= -1.5:
            return "supportive", "组合支撑信号占优"
        return "mixed", "组合证据分化"


portfolio_assessment_service = PortfolioAssessmentService()
