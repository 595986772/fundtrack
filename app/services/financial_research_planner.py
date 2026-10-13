"""FundScope 金融研究计划与证据充分性闸门。

过去的 Agent 路由解决的是“这句话该绑定哪个工具”，但它没有回答另一个更重要的
问题：完成一份综合风险判断，哪些证据必须出现？本模块把这个研究流程固定下来：

1. 根据问题识别研究意图，但不直接生成金融结论；
2. 根据基金/组合类型列出必须检查的证据维度；
3. 在数据读取完成后，由 Python 判断每个维度是充分、部分充分还是缺失；
4. 告诉下游模型当前最多可以给出多强的结论。

这里不调用大模型，也不计算未来收益。模型之后只能解释已经通过本闸门的事实。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal


ResearchScope = Literal["fund", "portfolio"]
ResearchIntent = Literal[
    "risk_overview",
    "decision_support",
    "event_impact",
    "exposure_review",
    "performance_review",
]

PLANNER_VERSION = "financial-research-plan-v1"
GATE_VERSION = "financial-evidence-gate-v1"


ASSET_CLASS_LABELS = {
    "equity_index": "权益指数基金",
    "equity_theme": "行业主题基金",
    "fixed_income": "固定收益基金",
    "money_market": "货币基金",
    "qdii": "QDII / 海外基金",
    "mixed_active": "主动权益或混合基金",
}

INTENT_LABELS = {
    "risk_overview": "综合风险概览",
    "decision_support": "条件式操作研判",
    "event_impact": "事件影响研究",
    "exposure_review": "持仓与集中度研究",
    "performance_review": "业绩与基准研究",
}


@dataclass(frozen=True)
class EvidenceRequirement:
    """一项研究证据要求。

    weight 用于计算“证据覆盖分”，不是金融风险分；critical 表示该项缺失时不能给出
    强结论。把二者写成数据后，测试可以验证规则，而不用猜 Prompt 中的一句话。
    """

    key: str
    label: str
    reason: str
    weight: int
    critical: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "reason": self.reason,
            "weight": self.weight,
            "critical": self.critical,
        }


class FinancialResearchPlanner:
    """用确定性规则生成一份有限、可测试的金融研究计划。"""

    def plan(
        self,
        *,
        question: str,
        scope: ResearchScope,
        profiles: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """返回研究意图、资产类型、证据清单和执行阶段。"""

        normalized_question = str(question or "").strip()
        if not normalized_question:
            raise ValueError("研究问题不能为空")
        if scope not in {"fund", "portfolio"}:
            raise ValueError("研究范围只能是 fund 或 portfolio")

        intent = self._intent(normalized_question)
        asset_classes = sorted(
            {
                self._asset_class(profile)
                for profile in profiles
                if isinstance(profile, dict)
            }
        ) or ["mixed_active"]
        requirements = (
            self._fund_requirements(intent)
            if scope == "fund"
            else self._portfolio_requirements(intent)
        )

        task_names = (
            ["fund_risk", "fund_assessment", "fund_disclosure"]
            if scope == "fund"
            else [
                "portfolio_assessment",
                "portfolio_quant_risk",
                "portfolio_performance",
                "portfolio_exposure",
                "portfolio_event_impacts",
            ]
        )
        return {
            "planner_version": PLANNER_VERSION,
            "scope": scope,
            "intent": intent,
            "intent_label": INTENT_LABELS[intent],
            "asset_classes": [
                {
                    "code": code,
                    "label": ASSET_CLASS_LABELS[code],
                }
                for code in asset_classes
            ],
            "requirements": [item.to_dict() for item in requirements],
            "execution_stages": [
                {
                    "stage": "resolve_scope",
                    "mode": "sequential",
                    "purpose": "先确认基金或组合及其资产类型",
                },
                {
                    "stage": "collect_evidence",
                    "mode": "parallel",
                    "tasks": task_names,
                    "purpose": "并行读取彼此独立的确定性业务服务",
                },
                {
                    "stage": "evidence_gate",
                    "mode": "sequential",
                    "purpose": "检查覆盖率、时效、缺失项和结论边界",
                },
                {
                    "stage": "repair_gaps",
                    "mode": "bounded_after_gate",
                    "purpose": "区分用户条件与公开数据缺口；预算内补读，用户授权后补查披露，再重新检查",
                },
                {
                    "stage": "model_explanation",
                    "mode": "after_gate",
                    "purpose": "模型只解释通过闸门的事实，不补猜缺失数据",
                },
            ],
            "asset_specific_checks": self._asset_specific_checks(asset_classes),
            "answer_sections": [
                "研究范围与数据充分度",
                "当前风险状态",
                "风险如何从市场或事件传导到持仓",
                "支持与反方证据",
                "不确定性与仍需补充的信息",
            ],
        }

    @staticmethod
    def _intent(question: str) -> ResearchIntent:
        text = question.casefold()
        if any(
            marker in text
            for marker in (
                "买入",
                "卖出",
                "申购",
                "赎回",
                "加仓",
                "减仓",
                "持有",
                "仓位",
                "调仓",
                "操作",
                "适合我", "适不适合我", "根据我的情况", "结合我的情况",
            )
        ):
            return "decision_support"
        if any(marker in text for marker in ("新闻", "政策", "事件", "消息面", "舆情")):
            return "event_impact"
        if any(
            marker in text
            for marker in ("底层", "穿透", "行业暴露", "重复持仓", "集中度")
        ):
            return "exposure_review"
        if any(
            marker in text
            for marker in ("业绩", "收益", "基准", "跑赢", "跑输", "归因")
        ):
            return "performance_review"
        return "risk_overview"

    @staticmethod
    def _asset_class(profile: dict[str, Any]) -> str:
        """把基金目录中的自由文本归并为研究流程需要的少量资产类别。"""

        text = " ".join(
            str(profile.get(key) or "")
            for key in (
                "fund_type",
                "short_name",
                "full_name",
                "sample_role",
                "tracking_target",
            )
        ).casefold()
        if "qdii" in text or "海外" in text:
            return "qdii"
        if "货币" in text:
            return "money_market"
        if "债券" in text or "bond_fund" in text:
            return "fixed_income"
        if "industry_theme" in text or any(
            marker in text for marker in ("行业", "主题", "白酒", "半导体", "芯片")
        ):
            return "equity_theme"
        if "指数" in text or "broad_market_index" in text or "etf" in text:
            return "equity_index"
        return "mixed_active"

    @staticmethod
    def _fund_requirements(intent: ResearchIntent) -> tuple[EvidenceRequirement, ...]:
        decision = intent == "decision_support"
        return (
            EvidenceRequirement(
                "scope_profile",
                "基金身份与策略",
                "确认研究对象、基金类型和业绩比较基准",
                10,
                True,
            ),
            EvidenceRequirement(
                "historical_risk",
                "历史收益与风险",
                "检查回撤、波动、下行风险和风险调整后收益",
                20,
                True,
            ),
            EvidenceRequirement(
                "benchmark_alignment",
                "专属业绩基准",
                "避免用同一个宽基比较所有基金",
                10,
            ),
            EvidenceRequirement(
                "holdings_exposure",
                "披露持仓与集中度",
                "识别基金名称背后的证券、行业和披露时滞",
                20,
            ),
            EvidenceRequirement(
                "market_regime",
                "市场环境",
                "把基金自身风险和当前宽基环境分开理解",
                10,
            ),
            EvidenceRequirement(
                "event_context",
                "近期事件与情绪",
                "检查是否存在仍在有效期内的相关事件",
                10,
            ),
            EvidenceRequirement(
                "data_quality",
                "数据质量",
                "检查收益口径、来源日期、覆盖率和告警",
                10,
            ),
            EvidenceRequirement(
                "investor_context",
                "用户期限与承受能力",
                "操作观点需要结合投资期限、可承受回撤和资金流动性",
                10,
                decision,
            ),
        )

    @staticmethod
    def _portfolio_requirements(
        intent: ResearchIntent,
    ) -> tuple[EvidenceRequirement, ...]:
        decision = intent == "decision_support"
        return (
            EvidenceRequirement(
                "portfolio_holdings",
                "组合与当前权重",
                "确认组合归属、持仓基金和当前权重",
                10,
                True,
            ),
            EvidenceRequirement(
                "deterministic_assessment",
                "规则证据汇总",
                "按当前权重汇总风险、支撑和集中度证据",
                10,
            ),
            EvidenceRequirement(
                "quantitative_risk",
                "组合量化风险",
                "使用共同日期计算波动、CVaR、回撤和风险贡献",
                15,
                True,
            ),
            EvidenceRequirement(
                "historical_performance",
                "组合历史业绩",
                "区分真实流水业绩和目标权重历史模拟",
                10,
            ),
            EvidenceRequirement(
                "benchmark_alignment",
                "组合专属基准",
                "按各基金业绩基准形成同口径组合比较",
                10,
            ),
            EvidenceRequirement(
                "lookthrough_exposure",
                "底层穿透与重合",
                "识别表面分散背后的证券和行业集中风险",
                20,
            ),
            EvidenceRequirement(
                "event_context",
                "事件到持仓的传导",
                "把有效事件映射到具体证券、行业或基金权重",
                10,
            ),
            EvidenceRequirement(
                "data_quality",
                "跨来源数据质量",
                "检查净值、基准、季报和事件各自的截止日",
                5,
            ),
            EvidenceRequirement(
                "investor_context",
                "用户期限与承受能力",
                "操作观点需要结合投资期限、可承受回撤和资金流动性",
                10,
                decision,
            ),
        )

    @staticmethod
    def _asset_specific_checks(asset_classes: list[str]) -> list[dict[str, str]]:
        """列出资产类别真正关心、但当前系统可能尚未完整覆盖的风险。"""

        catalog = {
            "equity_index": (
                "tracking_and_concentration",
                "跟踪误差、基准偏离与成分集中度",
                "partial",
            ),
            "equity_theme": (
                "theme_concentration",
                "主题集中度、景气变化和政策敏感度",
                "partial",
            ),
            "fixed_income": (
                "duration_credit_liquidity",
                "久期、信用利差、发行人和流动性压力",
                "not_yet_supported",
            ),
            "money_market": (
                "liquidity_and_maturity",
                "流动性、剩余期限和集中赎回压力",
                "not_yet_supported",
            ),
            "qdii": (
                "currency_and_valuation_lag",
                "汇率、海外基准、时区和估值滞后",
                "not_yet_supported",
            ),
            "mixed_active": (
                "allocation_and_style_drift",
                "股债配置变化、基金经理风格漂移和换手",
                "not_yet_supported",
            ),
        }
        return [
            {"key": catalog[code][0], "label": catalog[code][1], "status": catalog[code][2]}
            for code in asset_classes
        ]


class FinancialEvidenceGate:
    """把各业务服务返回的数据转换成明确的证据充分度。"""

    STATUS_FACTOR = {
        "satisfied": 1.0,
        "partial": 0.5,
        "missing": 0.0,
        "not_applicable": 0.0,
    }

    def evaluate(
        self,
        *,
        plan: dict[str, Any],
        scope_data: dict[str, Any],
        task_results: dict[str, dict[str, Any]],
        question: str,
    ) -> dict[str, Any]:
        """检查每项要求，并决定模型应正常解释、条件式解释还是只说明缺口。"""

        scope = str(plan["scope"])
        observations = (
            self._fund_observations(scope_data, task_results, question, plan)
            if scope == "fund"
            else self._portfolio_observations(scope_data, task_results, question, plan)
        )
        # 聊天入口始终提供结构化状态，工具不再靠“当前这句话里有没有亏损一词”
        # 猜用户背景。独立 REST/旧教学调用没有会话状态时仍保留原有兼容路径。
        from app.services.investment_context import current_investment_context, FIELD_LABELS

        background = current_investment_context()
        if background is not None and plan["intent"] == "decision_support":
            missing_fields = background.missing_fields()
            known_count = 3 - len(missing_fields)
            observations["investor_context"] = {
                **self._observation(
                    "satisfied" if not missing_fields else "partial" if known_count else "missing",
                    "投资背景待重新确认" if background.needs_confirmation else (
                        "已读取本会话用户自述的期限、亏损承受与近期用款条件"
                        if not missing_fields else "仍缺少：" + "、".join(FIELD_LABELS[key] for key in missing_fields)
                    ),
                ),
                "source": "conversation_investment_context",
                "missing_fields": missing_fields,
                "facts": {} if background.needs_confirmation else {
                    key: fact.model_dump() for key, fact in background.facts.items()
                },
            }
        checked: list[dict[str, Any]] = []
        earned = 0.0
        denominator = 0.0

        for requirement in plan["requirements"]:
            observation = observations.get(
                requirement["key"],
                self._observation("missing", "该证据维度没有生成检查结果"),
            )
            item = {**requirement, **observation}
            checked.append(item)
            if item["status"] == "not_applicable":
                continue
            weight = float(item["weight"])
            denominator += weight
            earned += weight * self.STATUS_FACTOR[item["status"]]

        score = round(earned / denominator * 100, 2) if denominator else 0.0
        critical_missing = [
            item for item in checked if item["critical"] and item["status"] == "missing"
        ]
        failed_task_count = sum(
            result.get("status") != "completed" for result in task_results.values()
        )
        partial = [item for item in checked if item["status"] == "partial"]
        missing = [item for item in checked if item["status"] == "missing"]
        # 通用指标齐全，不代表所有资产类别的核心风险都齐全。例如债券基金若没有
        # 久期和信用利差，不能因为净值波动、基准和季报都存在就标成“证据充分”。
        unsupported_asset_checks = [
            item
            for item in list(plan.get("asset_specific_checks") or [])
            if item.get("status") == "not_yet_supported"
        ]

        if (
            score >= 80
            and not critical_missing
            and failed_task_count == 0
            and not unsupported_asset_checks
        ):
            status = "sufficient"
            conclusion_policy = "normal_with_uncertainty"
            status_label = "证据较充分"
        elif score >= 55:
            status = "limited"
            conclusion_policy = "conditional_only"
            status_label = "证据有限，只能条件式判断"
        else:
            status = "insufficient"
            conclusion_policy = "facts_and_gaps_only"
            status_label = "证据不足"

        questions = self._clarification_questions(
            intent=str(plan["intent"]),
            checked=checked,
        )
        return {
            "gate_version": GATE_VERSION,
            "status": status,
            "status_label": status_label,
            "coverage_score_pct": score,
            "score_meaning": "只表示本次研究证据覆盖度，不是收益概率或风险评级",
            "conclusion_policy": conclusion_policy,
            "requirements": checked,
            "satisfied_count": sum(item["status"] == "satisfied" for item in checked),
            "partial_count": len(partial),
            "missing_count": len(missing),
            "failed_task_count": failed_task_count,
            "critical_missing_keys": [item["key"] for item in critical_missing],
            "asset_specific_gaps": unsupported_asset_checks,
            "missing_or_partial": [
                {
                    "key": item["key"],
                    "label": item["label"],
                    "status": item["status"],
                    "detail": item["detail"],
                }
                for item in [*missing, *partial]
            ],
            "clarification_questions": questions,
        }

    def _fund_observations(
        self,
        scope_data: dict[str, Any],
        tasks: dict[str, dict[str, Any]],
        question: str,
        plan: dict[str, Any],
    ) -> dict[str, dict[str, Any]]:
        profile = scope_data.get("profile") or {}
        risk = self._task_data(tasks, "fund_risk")
        assessment = self._task_data(tasks, "fund_assessment")
        disclosure = self._task_data(tasks, "fund_disclosure")

        benchmark = (risk or {}).get("benchmark_comparison") or {}
        benchmark_status = str(benchmark.get("status") or "unavailable")
        benchmark_coverage = float(benchmark.get("coverage_pct") or 0)
        if benchmark_status == "unavailable":
            benchmark_observation = self._observation(
                "missing", "当前没有可用的基金专属业绩基准序列"
            )
        elif benchmark_coverage >= 80:
            benchmark_observation = self._observation(
                "satisfied",
                f"基准状态为 {benchmark_status}，共同日期覆盖 {benchmark_coverage:.2f}%",
                as_of_date=benchmark.get("period_end"),
                coverage_pct=benchmark_coverage,
            )
        else:
            benchmark_observation = self._observation(
                "partial",
                f"基准可以使用，但共同日期覆盖仅 {benchmark_coverage:.2f}%",
                as_of_date=benchmark.get("period_end"),
                coverage_pct=benchmark_coverage,
            )

        disclosure_observation = self._fund_disclosure_observation(disclosure)
        market_available = "market_state" in ((assessment or {}).get("available_dimensions") or [])
        event_count = int((assessment or {}).get("active_event_count") or 0)
        risk_warnings = list((risk or {}).get("data_quality_warnings") or [])
        disclosure_warnings = list((disclosure or {}).get("warnings") or [])
        quality_status = (
            "missing"
            if risk is None
            else "partial"
            if risk_warnings
            or disclosure_warnings
            or (disclosure or {}).get("freshness_status") in {"stale", "unavailable"}
            else "satisfied"
        )

        return {
            "scope_profile": self._observation(
                "satisfied" if profile.get("fund_code") or profile.get("short_name") else "missing",
                "已确认基金类型、名称和目录来源" if profile else "基金档案不可用",
            ),
            "historical_risk": self._observation(
                "satisfied" if risk else "missing",
                (
                    f"已计算 {risk.get('observation_count')} 个净值点的历史风险"
                    if risk
                    else self._task_error(tasks, "fund_risk")
                ),
                as_of_date=(risk or {}).get("period_end"),
                evidence_ids=self._evidence_ids(assessment, kind="fund_risk"),
            ),
            "benchmark_alignment": benchmark_observation,
            "holdings_exposure": disclosure_observation,
            "market_regime": self._observation(
                "satisfied" if market_available else "missing",
                "已取得独立市场状态截面" if market_available else self._task_error(tasks, "fund_assessment"),
                as_of_date=((assessment or {}).get("component_dates") or {}).get("market_state"),
            ),
            # 没有活跃事件是一条合法结果，不等于事件数据读取失败。
            "event_context": self._observation(
                "satisfied" if assessment else "missing",
                (
                    f"当前有 {event_count} 个有效关联事件"
                    if event_count
                    else "事件检索成功，当前没有仍在有效期内的关联事件"
                ) if assessment else self._task_error(tasks, "fund_assessment"),
                as_of_date=((assessment or {}).get("component_dates") or {}).get("market_events"),
                evidence_ids=self._evidence_ids(assessment, kind="market_event"),
            ),
            "data_quality": self._observation(
                quality_status,
                "；".join([*risk_warnings, *disclosure_warnings])[:300]
                or "风险收益口径和披露时效检查通过",
            ),
            "investor_context": self._investor_context_observation(question, plan),
        }

    def _portfolio_observations(
        self,
        scope_data: dict[str, Any],
        tasks: dict[str, dict[str, Any]],
        question: str,
        plan: dict[str, Any],
    ) -> dict[str, dict[str, Any]]:
        assessment = self._task_data(tasks, "portfolio_assessment")
        quant = self._task_data(tasks, "portfolio_quant_risk")
        performance = self._task_data(tasks, "portfolio_performance")
        exposure = self._task_data(tasks, "portfolio_exposure")
        impacts = self._task_data(tasks, "portfolio_event_impacts")
        holdings = list(scope_data.get("holdings") or [])

        benchmark = (performance or {}).get("benchmark") or {}
        benchmark_status = str(benchmark.get("status") or "unavailable")
        benchmark_coverage = float(benchmark.get("coverage_pct") or 0)
        if benchmark_status == "unavailable":
            benchmark_observation = self._observation(
                "missing", "当前组合没有可用的同区间混合基准"
            )
        elif benchmark_coverage >= 80:
            benchmark_observation = self._observation(
                "satisfied",
                f"组合基准共同日期覆盖 {benchmark_coverage:.2f}%",
                coverage_pct=benchmark_coverage,
                as_of_date=(performance or {}).get("period_end"),
            )
        else:
            benchmark_observation = self._observation(
                "partial",
                f"组合基准共同日期覆盖仅 {benchmark_coverage:.2f}%",
                coverage_pct=benchmark_coverage,
                as_of_date=(performance or {}).get("period_end"),
            )

        exposure_quality = (exposure or {}).get("data_quality") or {}
        exposure_status = str(exposure_quality.get("status") or "unavailable")
        if exposure is None or exposure_status == "unavailable":
            exposure_observation = self._observation(
                "missing", self._task_error(tasks, "portfolio_exposure")
            )
        elif exposure_status in {"complete", "partial"} and exposure_quality.get(
            "freshness_status"
        ) != "stale":
            exposure_observation = self._observation(
                "satisfied" if exposure_status == "complete" else "partial",
                (
                    "证券穿透覆盖 "
                    f"{float(exposure_quality.get('security_coverage_pct') or 0):.2f}%"
                ),
                as_of_date=exposure.get("latest_report_date"),
                coverage_pct=exposure_quality.get("security_coverage_pct"),
                evidence_ids=self._exposure_ids(exposure),
            )
        else:
            exposure_observation = self._observation(
                "partial",
                f"持仓穿透状态为 {exposure_status}，披露时效为 "
                f"{exposure_quality.get('freshness_status') or 'unknown'}",
                as_of_date=exposure.get("latest_report_date"),
                coverage_pct=exposure_quality.get("security_coverage_pct"),
                evidence_ids=self._exposure_ids(exposure),
            )

        failed_tasks = [name for name, result in tasks.items() if result.get("status") != "completed"]
        quality_warnings = [
            *list((performance or {}).get("warnings") or []),
            *list(exposure_quality.get("warnings") or []),
            *list((quant or {}).get("data_quality_warnings") or []),
        ]
        quality_status = "missing" if len(failed_tasks) >= 3 else "partial" if failed_tasks or quality_warnings else "satisfied"

        return {
            "portfolio_holdings": self._observation(
                "satisfied" if holdings else "missing",
                f"已确认 {len(holdings)} 只持仓基金及当前权重" if holdings else "组合没有可研究持仓",
            ),
            "deterministic_assessment": self._observation(
                "satisfied" if assessment else "missing",
                (assessment or {}).get("summary") or self._task_error(tasks, "portfolio_assessment"),
                as_of_date=(assessment or {}).get("assessment_date"),
                evidence_ids=self._evidence_ids(assessment),
            ),
            "quantitative_risk": self._observation(
                "satisfied" if quant else "missing",
                (
                    f"已使用 {quant.get('observation_count')} 个共同日期计算组合风险"
                    if quant
                    else self._task_error(tasks, "portfolio_quant_risk")
                ),
                as_of_date=(quant or {}).get("period_end"),
            ),
            "historical_performance": self._observation(
                "satisfied" if performance else "missing",
                (performance or {}).get("performance_label") or self._task_error(tasks, "portfolio_performance"),
                as_of_date=(performance or {}).get("period_end"),
            ),
            "benchmark_alignment": benchmark_observation,
            "lookthrough_exposure": exposure_observation,
            "event_context": self._observation(
                "satisfied" if impacts else "missing",
                (
                    (impacts or {}).get("summary")
                    or "事件归因成功，当前没有可映射到持仓的有效事件"
                ) if impacts else self._task_error(tasks, "portfolio_event_impacts"),
                as_of_date=(impacts or {}).get("event_data_cutoff"),
                evidence_ids=[
                    str(item.get("impact_evidence_id"))
                    for item in list((impacts or {}).get("impacts") or [])[:6]
                    if item.get("impact_evidence_id")
                ],
            ),
            "data_quality": self._observation(
                quality_status,
                (
                    "失败任务：" + "、".join(failed_tasks)
                    if failed_tasks
                    else "；".join(str(item) for item in quality_warnings)[:300]
                    or "净值、基准、披露和事件均返回可检查的独立日期"
                ),
            ),
            "investor_context": self._investor_context_observation(question, plan),
        }

    def _fund_disclosure_observation(
        self,
        disclosure: dict[str, Any] | None,
    ) -> dict[str, Any]:
        if not disclosure or disclosure.get("data_mode") == "unavailable":
            return self._observation("missing", "当前没有可用的基金定期持仓披露")
        freshness = str(disclosure.get("freshness_status") or "unavailable")
        count = int(disclosure.get("security_count") or 0) + int(
            disclosure.get("industry_count") or 0
        )
        status = "satisfied" if freshness in {"fresh", "aging"} and count else "partial"
        evidence_ids = []
        fund_code = str(disclosure.get("fund_code") or "unknown")
        for item in list(disclosure.get("security_holdings") or [])[:5]:
            code = item.get("security_code")
            report_date = item.get("report_date")
            if code and report_date:
                # 与 Brief 摘要中的证据编号保持同一格式，模型才能从闸门缺口
                # 一路追溯到具体基金、证券和披露日期。
                evidence_ids.append(
                    f"disclosure:{fund_code}:{code}:{report_date}"
                )
        return self._observation(
            status,
            f"持仓披露时效为 {freshness}，包含 {count} 条证券或行业记录",
            as_of_date=disclosure.get("latest_report_date"),
            coverage_pct=max(
                float(disclosure.get("industry_coverage_pct") or 0),
                float(disclosure.get("disclosed_stock_weight_pct") or 0)
                + float(disclosure.get("disclosed_bond_weight_pct") or 0),
            ),
            evidence_ids=evidence_ids,
        )

    @staticmethod
    def _investor_context_observation(
        question: str,
        plan: dict[str, Any],
    ) -> dict[str, Any]:
        if plan["intent"] != "decision_support":
            return FinancialEvidenceGate._observation(
                "not_applicable", "本次问题不是操作研判，不强制要求用户适当性信息"
            )
        text = question.casefold()
        horizon = any(
            marker in text
            for marker in ("半年", "一年", "三年", "长期", "短期", "持有期", "投资期限")
        )
        tolerance = any(
            marker in text
            for marker in (
                "能承受",
                "可承受",
                "最大回撤",
                "亏损",
                "风险偏好",
                "保守",
                "稳健",
                "激进",
                "急用",
                "流动性",
            )
        )
        if horizon and tolerance:
            return FinancialEvidenceGate._observation(
                "satisfied", "问题中同时给出了投资期限和风险承受线索"
            )
        if horizon or tolerance:
            return FinancialEvidenceGate._observation(
                "partial", "只识别到投资期限或风险承受能力中的一项"
            )
        return FinancialEvidenceGate._observation(
            "missing", "操作观点缺少投资期限、可承受回撤和资金流动性背景"
        )

    @staticmethod
    def _clarification_questions(
        *,
        intent: str,
        checked: list[dict[str, Any]],
    ) -> list[str]:
        questions: list[str] = []
        by_key = {item["key"]: item for item in checked}
        investor = by_key.get("investor_context") or {}
        if intent == "decision_support" and investor.get("status") != "satisfied":
            if "missing_fields" in investor:
                from app.services.investment_context import FIELD_QUESTIONS

                questions.extend(FIELD_QUESTIONS[key] for key in investor["missing_fields"])
            else:
                questions.extend(
                    [
                        "这笔资金预计至少持有多久？",
                        "你能接受的阶段性最大亏损或回撤大约是多少？",
                        "近期是否可能需要赎回这笔资金？",
                    ]
                )
        # 旧季报是公开数据限制，不是“用户少回答了一个问题”。补查控制器负责检查来源；
        # 最终仍过旧时应说明日期和结论边界，不混入个人背景追问。
        return questions[:4]

    @staticmethod
    def _task_data(
        tasks: dict[str, dict[str, Any]],
        name: str,
    ) -> dict[str, Any] | None:
        result = tasks.get(name) or {}
        data = result.get("data")
        return data if result.get("status") == "completed" and isinstance(data, dict) else None

    @staticmethod
    def _task_error(tasks: dict[str, dict[str, Any]], name: str) -> str:
        result = tasks.get(name) or {}
        return str(result.get("error") or f"{name} 没有返回数据")[:240]

    @staticmethod
    def _evidence_ids(
        payload: dict[str, Any] | None,
        *,
        kind: str | None = None,
    ) -> list[str]:
        items = [
            *list((payload or {}).get("risk_evidence") or []),
            *list((payload or {}).get("supporting_evidence") or []),
            *list((payload or {}).get("neutral_evidence") or []),
        ]
        return [
            str(item["evidence_id"])
            for item in items
            if item.get("evidence_id") and (kind is None or item.get("kind") == kind)
        ][:8]

    @staticmethod
    def _exposure_ids(exposure: dict[str, Any] | None) -> list[str]:
        return [
            str(item["evidence_id"])
            for item in [
                *list((exposure or {}).get("security_exposures") or [])[:4],
                *list((exposure or {}).get("industry_exposures") or [])[:4],
            ]
            if item.get("evidence_id")
        ]

    @staticmethod
    def _observation(
        status: str,
        detail: str,
        *,
        as_of_date: Any = None,
        coverage_pct: Any = None,
        evidence_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        return {
            "status": status,
            "detail": str(detail or "")[:400],
            "as_of_date": str(as_of_date) if as_of_date else None,
            "coverage_pct": round(float(coverage_pct), 2) if coverage_pct is not None else None,
            "evidence_ids": list(evidence_ids or [])[:8],
        }


financial_research_planner = FinancialResearchPlanner()
financial_evidence_gate = FinancialEvidenceGate()
