"""把基金风险、市场状态和关联事件融合成统一的确定性研判。

这一层仍然不调用大模型。它只复用已经验证过的三个 Service，把不同来源的事实
转换成统一 Evidence，再按公开权重分组和汇总。REST、前端和 Agent Tool 因此会
看到完全相同的结论；模型只负责把这份结构化结果讲得更通俗。
"""

from __future__ import annotations

from datetime import date
from typing import Any

from app.services.fund_risk import FundRiskError, FundRiskService, fund_risk_service
from app.services.funds import (
    FundCatalogError,
    FundCatalogService,
    FundNotFoundError,
    fund_catalog_service,
)
from app.services.market_events import (
    MarketEventError,
    MarketEventService,
    market_event_service,
)
from app.services.market_sentiment import (
    MarketSentimentError,
    MarketSentimentService,
    market_sentiment_service,
)


# 权重公开写在代码和响应里，避免出现用户无法解释的“AI 风险分”。
RELIABILITY_WEIGHTS = {"high": 1.0, "medium": 0.75, "low": 0.5}
RELEVANCE_WEIGHTS = {"direct": 1.0, "related": 0.75, "context": 0.0}


class FundAssessmentError(RuntimeError):
    """任一基础数据层无法完成单基金综合研判。"""


class EvidenceFusionService:
    """将三类事实转换成可追溯证据，并执行透明的规则汇总。"""

    def __init__(
        self,
        *,
        catalog: FundCatalogService = fund_catalog_service,
        risk_service: FundRiskService = fund_risk_service,
        sentiment_service: MarketSentimentService = market_sentiment_service,
        event_service: MarketEventService = market_event_service,
    ) -> None:
        # 依赖从构造函数注入，单元测试可以分别替换为人工数据服务。
        self.catalog = catalog
        self.risk_service = risk_service
        self.sentiment_service = sentiment_service
        self.event_service = event_service

    def assess(self, *, fund_code: str, months: int = 12) -> dict[str, Any]:
        """生成指定基金近 6、12 或 36 个月的统一证据研判。"""

        try:
            detail = self.catalog.get_fund(fund_code)
            risk = self.risk_service.analyze(fund_code=fund_code, months=months)
            market = self.sentiment_service.analyze()
            period_start = date.fromisoformat(risk["period_start"])
            period_end = date.fromisoformat(risk["period_end"])
            event_result = self.event_service.search(
                fund_code=fund_code,
                date_from=period_start,
                date_to=period_end,
                limit=50,
            )
        except FundNotFoundError:
            # “基金不存在”是正常的 404 业务结果，交给 Router 保留原始语义。
            raise
        except (
            FundCatalogError,
            FundRiskError,
            MarketSentimentError,
            MarketEventError,
            KeyError,
            TypeError,
            ValueError,
        ) as exc:
            raise FundAssessmentError(f"无法生成基金综合研判：{exc}") from exc

        profile = detail["profile"]
        sample_role = profile["sample_role"]
        source_url = profile.get("source_url")

        risk_evidence = self._risk_evidence(
            risk=risk,
            sample_role=sample_role,
            source_url=source_url,
        )
        market_evidence = self._market_evidence(
            market=market,
            sample_role=sample_role,
        )
        raw_event_evidence = [
            self._event_to_evidence(event) for event in event_result["events"]
        ]
        # 同一个事件簇只保留一条最可靠的代表证据。原始报道数仍由 event_count 返回，
        # 因此“审计多少篇”和“实际计几次分”两个口径不会混在一起。
        event_evidence = self._deduplicate_event_clusters(raw_event_evidence)
        active_event_count = sum(
            item["decay_factor"] > 0 for item in event_evidence
        )
        expired_event_count = len(event_evidence) - active_event_count
        all_evidence = [*risk_evidence, *market_evidence, *event_evidence]

        supporting = [
            evidence
            for evidence in all_evidence
            if evidence["direction"] == "supportive"
        ]
        caution = [
            evidence
            for evidence in all_evidence
            if evidence["direction"] == "caution"
        ]
        neutral = [
            evidence
            for evidence in all_evidence
            if evidence["direction"] == "neutral"
        ]
        score = self._score(evidence=all_evidence)
        state_code, state_label = self._state(
            score=score,
            caution_evidence=caution,
        )

        available_dimensions = ["fund_risk", "market_state"]
        if event_result["events"]:
            available_dimensions.append("market_event")
        coverage_pct = round(len(available_dimensions) / 3 * 100, 2)
        confidence = "medium" if len(available_dimensions) == 3 else "low"
        fund_data_note = (
            "基金净值已使用每日同步数据库快照"
            if detail.get("data_mode") == "database"
            else "基金净值仍使用本地固定快照"
        )
        return_basis_note = f"风险收益口径为{risk['return_basis_label']}"
        semantic_accepted = int(event_result.get("semantic_accepted_count", 0))
        semantic_abstained = int(event_result.get("semantic_abstained_count", 0))
        semantic_pending = int(event_result.get("semantic_pending_count", 0))
        if event_result.get("data_mode") == "database" and semantic_accepted:
            event_data_note = (
                f"事件来自每日同步数据库，其中 {semantic_accepted} 条通过 AI 语义校验、"
                f"{semantic_abstained} 条弃权、{semantic_pending} 条待分析"
            )
        elif event_result.get("data_mode") == "database":
            event_data_note = "事件来自每日同步数据库，当前没有通过校验的 AI 语义结果"
        else:
            event_data_note = "事件来自本地固定快照，影响方向为人工研究标注"
        confidence_reason = (
            f"风险、市场和事件三类证据齐全；{fund_data_note}，{return_basis_note}，"
            f"市场与事件仍有各自截止日；{event_data_note}。"
            if confidence == "medium"
            else (
                f"{fund_data_note}，{return_basis_note}；缺少当前统计区间内的关联事件，"
                "只能依据风险和市场背景研判。"
            )
        )

        period_label = {6: "近 6 个月", 12: "近 1 年", 36: "近 3 年"}[months]
        market_description = (
            f"权益市场状态为{market['state_label']}"
            if sample_role != "bond_fund"
            else f"权益市场状态为{market['state_label']}，对债券基金仅作背景"
        )
        summary = (
            f"{period_label}历史风险、{market_description}，统计区间内检索到"
            f" {event_result['count']} 篇关联报道，归并为 {len(event_evidence)} 个逻辑事件，"
            f"其中 {active_event_count} 个仍在有效期内。"
            "按公开权重与时间衰减汇总后，当前呈现"
            f"“{state_label}”：风险证据 {score['caution_points']:.2f} 分，"
            f"支撑证据 {score['supportive_points']:.2f} 分。"
        )

        return {
            "data_kind": "deterministic_fund_assessment",
            "fund_code": fund_code,
            "short_name": profile["short_name"],
            "sample_role": sample_role,
            "period_months": months,
            "period_start": risk["period_start"],
            "period_end": risk["period_end"],
            "assessment_date": risk["period_end"],
            "state_code": state_code,
            "state_label": state_label,
            "confidence": confidence,
            "confidence_reason": confidence_reason,
            "summary": summary,
            "score_breakdown": score,
            "supporting_evidence": self._sort_evidence(supporting),
            "risk_evidence": self._sort_evidence(caution),
            "neutral_evidence": self._sort_evidence(neutral),
            "evidence_count": len(all_evidence),
            "event_count": event_result["count"],
            "event_cluster_count": len(event_evidence),
            "active_event_count": active_event_count,
            "expired_event_count": expired_event_count,
            "available_dimensions": available_dimensions,
            "coverage_pct": coverage_pct,
            "risk_return_basis": risk["return_basis"],
            "risk_return_basis_label": risk["return_basis_label"],
            "risk_return_basis_status": risk["return_basis_status"],
            "risk_data_quality_warnings": risk["data_quality_warnings"],
            # 风险调整指标和基准比较属于可引用事实，但第一版不直接改变证据分数，
            # 避免同一段历史收益同时通过波动率和夏普被重复计分。
            "risk_adjusted": risk["risk_adjusted"],
            "benchmark_comparison": risk["benchmark_comparison"],
            "component_dates": {
                "fund_risk": risk["period_end"],
                "market_state": market["data_date"],
                "market_events": event_result["data_cutoff"],
            },
            "missing_information": self._missing_information(
                sample_role=sample_role,
                has_events=bool(event_result["events"]),
                event_data_mode=event_result.get("data_mode", "fixture"),
                semantic_accepted_count=semantic_accepted,
                semantic_pending_count=semantic_pending,
            ),
            "limitations": [
                "分值只用于本项目内排序证据，不是行业评级、买卖信号或收益预测。",
                "基金风险来自历史净值，无法代表未来波动和回撤。",
                "市场状态使用宽基和成交代理；事件关联依赖模型语义判断与样例基金白名单。",
                "事件有效期是透明教学规则，不等同于专业机构对消息影响持续时间的估计。",
                *risk["data_quality_warnings"],
                *risk["risk_adjusted"]["warnings"],
                *risk["benchmark_comparison"]["warnings"],
            ],
            "methodology": (
                "先把基金风险、市场状态和关联事件统一为 Evidence；单项得分为 "
                "strength × reliability_weight × relevance_weight。支撑证据计入正向分，"
                "风险证据计入谨慎分；事件证据再乘 decay_factor，中性和过期证据不计分；"
                "最终标签由两侧差值和是否存在高强度基金风险共同决定。"
            ),
            "disclaimer": (
                "基金净值为每日数据库快照，市场与事件有独立截止日；"
                "仅用于学习和研究，不构成投资建议。"
                if detail.get("data_mode") == "database"
                else "本地固定历史快照，仅用于学习和研究，不构成投资建议。"
            ),
        }

    def _risk_evidence(
        self,
        *,
        risk: dict[str, Any],
        sample_role: str,
        source_url: str | None,
    ) -> list[dict[str, Any]]:
        """把四个主要历史风险指标转换成统一 Evidence。"""

        common = {
            "kind": "fund_risk",
            "reliability": "medium",
            "relevance": "direct",
            "horizon": "historical",
            "data_date": risk["period_end"],
            "source_name": risk["source"],
            "source_level": "B",
            "source_url": source_url,
            "event_id": None,
        }
        fund_code = risk["fund_code"]
        period_key = f"{risk['period_start']}-{risk['period_end']}"
        basis_note = f"收益口径：{risk['return_basis_label']}。"

        drawdown = float(risk["max_drawdown"]["drawdown_pct"])
        drawdown_direction, drawdown_strength = self._threshold_direction(
            value=drawdown,
            medium=20,
            high=35,
        )
        current_drawdown = float(risk["current_drawdown_pct"])
        current_direction, current_strength = self._threshold_direction(
            value=current_drawdown,
            medium=15,
            high=25,
        )

        volatility = float(risk["annualized_volatility_pct"])
        volatility_medium, volatility_high = (
            (6, 10) if sample_role == "bond_fund" else (15, 25)
        )
        volatility_direction, volatility_strength = self._threshold_direction(
            value=volatility,
            medium=volatility_medium,
            high=volatility_high,
        )

        period_return = float(risk["period_return_pct"])
        if period_return <= -20:
            return_direction, return_strength = "caution", 3
        elif period_return <= -8:
            return_direction, return_strength = "caution", 2
        elif period_return >= 15:
            return_direction, return_strength = "supportive", 1
        else:
            return_direction, return_strength = "neutral", 1

        return [
            {
                **common,
                "evidence_id": f"risk:max_drawdown:{fund_code}:{period_key}",
                "direction": drawdown_direction,
                "strength": drawdown_strength,
                "title": "历史最大回撤",
                "value_text": f"-{drawdown:.2f}%",
                "explanation": (
                    f"从 {risk['max_drawdown']['peak_date']} 高点到 "
                    f"{risk['max_drawdown']['trough_date']} 低点的最大跌幅。{basis_note}"
                ),
            },
            {
                **common,
                "evidence_id": f"risk:current_drawdown:{fund_code}:{period_key}",
                "direction": current_direction,
                "strength": current_strength,
                "title": "当前回撤",
                "value_text": f"-{current_drawdown:.2f}%",
                "explanation": (
                    f"统计截止日相对区间内此前高点仍然回撤的幅度。{basis_note}"
                ),
            },
            {
                **common,
                "evidence_id": f"risk:volatility:{fund_code}:{period_key}",
                "direction": volatility_direction,
                "strength": volatility_strength,
                "title": "历史年化波动",
                "value_text": f"{volatility:.2f}%",
                "explanation": (
                    f"使用 {risk['observation_count']} 个净值点的日收益样本标准差年化。"
                    f"{basis_note}"
                ),
            },
            {
                **common,
                "evidence_id": f"risk:period_return:{fund_code}:{period_key}",
                "direction": return_direction,
                "strength": return_strength,
                "title": "区间历史收益",
                "value_text": f"{period_return:+.2f}%",
                "explanation": (
                    f"只描述所选历史区间的复利变化，不代表未来收益。{basis_note}"
                ),
            },
        ]

    def _market_evidence(
        self,
        *,
        market: dict[str, Any],
        sample_role: str,
    ) -> list[dict[str, Any]]:
        """把四项市场信号映射到权益基金；债券基金只保留中性背景。"""

        sources = market["sources"]
        if sample_role == "bond_fund":
            return [
                {
                    "evidence_id": f"market:equity_context:{market['data_date']}",
                    "kind": "market_state",
                    "direction": "neutral",
                    "strength": 1,
                    "reliability": "medium",
                    "relevance": "context",
                    "horizon": "current",
                    "title": "权益市场背景",
                    "value_text": market["state_label"],
                    "explanation": (
                        "当前市场状态主要使用沪深300和全市场涨跌家数，对债券基金"
                        "不直接计入支撑或风险分。"
                    ),
                    "data_date": market["data_date"],
                    "source_name": sources[0]["name"],
                    "source_level": sources[0]["source_level"],
                    "source_url": sources[0]["source_url"],
                    "event_id": None,
                }
            ]

        title_by_key = {
            "trend": "宽基趋势",
            "volatility": "市场波动",
            "breadth": "市场宽度",
            "activity": "成交活跃度",
        }
        source_index_by_key = {
            "trend": 0,
            "volatility": 0,
            "breadth": 1,
            "activity": 2,
        }
        relevance = "direct" if sample_role == "broad_market_index" else "related"

        evidence: list[dict[str, Any]] = []
        for signal in market["signals"]:
            source = sources[source_index_by_key[signal["key"]]]
            direction = {
                "positive": "supportive",
                "negative": "caution",
                "caution": "caution",
                "neutral": "neutral",
            }[signal["direction"]]
            evidence.append(
                {
                    "evidence_id": f"market:{signal['key']}:{market['data_date']}",
                    "kind": "market_state",
                    "direction": direction,
                    "strength": 1,
                    "reliability": "medium",
                    "relevance": relevance,
                    "horizon": "current",
                    "title": title_by_key[signal["key"]],
                    "value_text": signal["value_text"],
                    "explanation": signal["evidence"],
                    "data_date": market["data_date"],
                    "source_name": source["name"],
                    "source_level": source["source_level"],
                    "source_url": source["source_url"],
                    "event_id": None,
                }
            )
        return evidence

    @staticmethod
    def _event_to_evidence(event: dict[str, Any]) -> dict[str, Any]:
        """保留追溯字段，并优先使用通过 Python 校验的模型语义结果。"""

        semantic = event.get("semantic_analysis")
        if semantic and semantic.get("status") == "accepted":
            relevance = semantic["relevance"]
            strength = int(semantic["severity"])
        else:
            relevance = (
                "direct" if len(event["affected_fund_codes"]) == 1 else "related"
            )
            if event["event_type"] == "fund_announcement":
                strength = 3
            elif relevance == "direct":
                strength = 2
            else:
                strength = 1

        direction = {
            "positive": "supportive",
            "negative": "caution",
            "caution": "caution",
            "neutral": "neutral",
            "mixed": "neutral",
        }[event["impact_direction"]]
        reliability = {"A": "high", "B": "medium", "C": "low", "D": "low"}[
            event["source_level"]
        ]
        cluster = event.get("cluster")
        cluster_label = (
            f" · 热度 {cluster['heat_score']:.0f} · {cluster['member_count']}篇报道"
            if cluster
            else ""
        )

        return {
            # 聚类后用 cluster_id 做证据主键，使相似转载在计分层天然去重。
            "evidence_id": cluster["cluster_id"] if cluster else event["event_id"],
            "kind": "market_event",
            "direction": direction,
            "strength": strength,
            "reliability": reliability,
            "relevance": relevance,
            "horizon": event["impact_horizon"],
            "title": event["title"],
            "value_text": (
                f"{event['event_type']} · AI复核 {semantic['confidence']:.0%}{cluster_label}"
                if semantic and semantic.get("status") == "accepted"
                else f"{event['event_type']} · 来源 {event['source_level']}{cluster_label}"
            ),
            "explanation": event["impact_reason"],
            "data_date": event["event_date"],
            "source_name": event["source_name"],
            "source_level": event["source_level"],
            "source_url": event["source_url"],
            "event_id": event["event_id"],
            "cluster_id": cluster["cluster_id"] if cluster else None,
            "cluster_member_count": cluster["member_count"] if cluster else None,
            "cluster_source_count": cluster["source_count"] if cluster else None,
            "heat_score": cluster["heat_score"] if cluster else None,
            "heat_level": cluster["heat_level"] if cluster else None,
            "cluster_is_representative": (
                cluster["is_representative"] if cluster else False
            ),
            "decay_factor": event["decay_factor"],
            "freshness_status": event["freshness_status"],
            "valid_until": event["valid_until"],
            "age_days": event["age_days"],
        }

    @staticmethod
    def _deduplicate_event_clusters(
        evidence: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """每个逻辑事件只留一条证据，并优先选择簇指定的代表报道。"""

        selected: dict[str, dict[str, Any]] = {}
        reliability_rank = {"high": 3, "medium": 2, "low": 1}
        relevance_rank = {"direct": 3, "related": 2, "context": 1}
        for item in evidence:
            current = selected.get(item["evidence_id"])
            if current is None:
                selected[item["evidence_id"]] = item
                continue
            candidate_rank = (
                bool(item.get("cluster_is_representative")),
                reliability_rank[item["reliability"]],
                relevance_rank[item["relevance"]],
                item["data_date"],
            )
            current_rank = (
                bool(current.get("cluster_is_representative")),
                reliability_rank[current["reliability"]],
                relevance_rank[current["relevance"]],
                current["data_date"],
            )
            if candidate_rank > current_rank:
                selected[item["evidence_id"]] = item
        return list(selected.values())

    @staticmethod
    def _threshold_direction(
        *,
        value: float,
        medium: float,
        high: float,
    ) -> tuple[str, int]:
        """将越高越需要警惕的指标映射成 1-3 级强度。"""

        if value >= high:
            return "caution", 3
        if value >= medium:
            return "caution", 2
        if value >= medium / 2:
            return "caution", 1
        return "neutral", 1

    @staticmethod
    def _score(*, evidence: list[dict[str, Any]]) -> dict[str, Any]:
        """按公开因子累计两侧分值，只有事件证据额外乘时间衰减。"""

        supportive_points = 0.0
        caution_points = 0.0
        for item in evidence:
            points = (
                item["strength"]
                * RELIABILITY_WEIGHTS[item["reliability"]]
                * RELEVANCE_WEIGHTS[item["relevance"]]
                * float(item.get("decay_factor", 1.0))
            )
            if item["direction"] == "supportive":
                supportive_points += points
            elif item["direction"] == "caution":
                caution_points += points

        supportive_points = round(supportive_points, 2)
        caution_points = round(caution_points, 2)
        return {
            "supportive_points": supportive_points,
            "caution_points": caution_points,
            "net_points": round(supportive_points - caution_points, 2),
            "neutral_evidence_count": sum(
                item["direction"] == "neutral" for item in evidence
            ),
            "expired_event_count": sum(
                item["kind"] == "market_event"
                and float(item.get("decay_factor", 1.0)) == 0
                for item in evidence
            ),
            "reliability_weights": RELIABILITY_WEIGHTS,
            "relevance_weights": RELEVANCE_WEIGHTS,
            "method": (
                "单项分 = strength(1-3) × reliability_weight × relevance_weight；"
                "市场事件再乘 decay_factor，中性和过期证据不计分。"
            ),
        }

    @staticmethod
    def _state(
        *,
        score: dict[str, Any],
        caution_evidence: list[dict[str, Any]],
    ) -> tuple[str, str]:
        """使用两侧差值，并防止重大历史风险被普通支持事件完全抵消。"""

        difference = score["caution_points"] - score["supportive_points"]
        has_high_fund_risk = any(
            item["kind"] == "fund_risk" and item["strength"] == 3
            for item in caution_evidence
        )
        if has_high_fund_risk and difference >= 0:
            return "high_attention", "风险信号占优"
        if difference >= 3:
            return "cautious", "偏谨慎"
        if difference <= -2:
            return "supportive", "支撑信号占优"
        return "mixed", "证据分化"

    @staticmethod
    def _sort_evidence(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """强证据优先；同强度下先展示日期更新的证据。"""

        return sorted(
            items,
            key=lambda item: (
                float(item.get("decay_factor", 1.0)) > 0,
                float(item.get("decay_factor", 1.0)),
                item["strength"],
                item["data_date"],
            ),
            reverse=True,
        )

    @staticmethod
    def _missing_information(
        *,
        sample_role: str,
        has_events: bool,
        event_data_mode: str,
        semantic_accepted_count: int = 0,
        semantic_pending_count: int = 0,
    ) -> list[str]:
        """明确告诉调用方当前结论还缺什么，而不是伪装成完整研究报告。"""

        missing = [
            "缺少基金实时持仓，模型只能结合事件主题与样例基金角色判断关联。",
            (
                f"仍有 {semantic_pending_count} 条自动采集事件待语义分析；有效期和衰减"
                "已由 Python 计算，但尚未接入跨来源热度。"
                if event_data_mode == "database" and semantic_pending_count
                else (
                    f"已有 {semantic_accepted_count} 条事件通过模型语义校验并计算时间衰减，"
                    "但尚未接入跨来源热度。"
                    if event_data_mode == "database" and semantic_accepted_count
                    else "事件尚未获得有效模型语义结果；已计算有效期，但尚未接入热度。"
                    if event_data_mode == "database"
                    else "事件为固定快照，已计算有效期和时间衰减，但尚未接入实时热度。"
                )
            ),
        ]
        role_missing = {
            "broad_market_index": "尚未拆解沪深300成分行业对基金表现的贡献。",
            "industry_theme": "尚未穿透到基金当前成分股和行业权重。",
            "bond_fund": "缺少完整收益率曲线、久期、信用利差和券种持仓。",
        }
        missing.append(role_missing.get(sample_role, "缺少基金底层持仓暴露。"))
        if not has_events:
            missing.append("当前统计区间没有与该基金直接关联的事件。")
        return missing


evidence_fusion_service = EvidenceFusionService()
