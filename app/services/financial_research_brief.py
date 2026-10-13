"""为主 Agent 生成一份有研究计划、有证据闸门的金融 Brief。

这个 Service 是“综合研究 Tool”的业务实现。它不会再次启动子 Agent，也不会调用
DeepSeek。基金和组合的风险、业绩、持仓、市场与事件仍由已有 Python Service 负责；
本层只做三件事：

* 先确认研究对象并生成证据清单；
* 并行读取彼此独立的业务结果，单路失败不会抹掉其他有效事实；
* 发现缺口后，在请求预算内重读失败证据；用户授权时检查过旧的公开披露；
* 重新检查充分性，压缩成主模型看得懂、又不会塞入整份日频序列的有界 JSON。

因此，模型看到的是“已经算好的事实 + 明确缺口”，而不是一堆任意工具让它临场拼接。
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from contextvars import copy_context
from time import perf_counter
from typing import Any, Callable, Protocol

from app.services.financial_research_planner import (
    FinancialEvidenceGate,
    FinancialResearchPlanner,
    financial_evidence_gate,
    financial_research_planner,
)
from app.services.fund_assessment import (
    EvidenceFusionService,
    evidence_fusion_service,
)
from app.services.fund_disclosures import (
    FundDisclosureService,
    fund_disclosure_service,
)
from app.services.fund_risk import FundRiskService, fund_risk_service
from app.services.funds import FundCatalogService, fund_catalog_service
from app.services.portfolio_assessment import (
    PortfolioAssessmentService,
    portfolio_assessment_service,
)
from app.services.portfolio_event_impacts import (
    PortfolioEventImpactService,
    portfolio_event_impact_service,
)
from app.services.portfolio_exposure import (
    PortfolioExposureService,
    portfolio_exposure_service,
)
from app.services.portfolio_performance import (
    PortfolioPerformanceService,
    portfolio_performance_service,
)
from app.services.portfolio_risk_engine import (
    PortfolioRiskEngine,
    portfolio_risk_engine,
)
from app.services.portfolios import PortfolioService, portfolio_service
from app.services.financial_research_repair import financial_research_repair_service


SUPPORTED_MONTHS = {6, 12, 36}
BRIEF_VERSION = "financial-research-brief-v1"


class FinancialResearchBriefError(RuntimeError):
    """研究对象不存在，或基础输入不足以建立任何研究计划。"""


class PlannerProtocol(Protocol):
    def plan(
        self,
        *,
        question: str,
        scope: str,
        profiles: list[dict[str, Any]],
    ) -> dict[str, Any]: ...


class GateProtocol(Protocol):
    def evaluate(
        self,
        *,
        plan: dict[str, Any],
        scope_data: dict[str, Any],
        task_results: dict[str, dict[str, Any]],
        question: str,
    ) -> dict[str, Any]: ...


class FinancialResearchBriefService:
    """复用现有服务，生成带有限补查与前后对照的综合研究输入。"""

    def __init__(
        self,
        *,
        planner: PlannerProtocol = financial_research_planner,
        gate: GateProtocol = financial_evidence_gate,
        funds: FundCatalogService = fund_catalog_service,
        fund_risk: FundRiskService = fund_risk_service,
        fund_assessment: EvidenceFusionService = evidence_fusion_service,
        fund_disclosures: FundDisclosureService = fund_disclosure_service,
        portfolios: PortfolioService = portfolio_service,
        portfolio_assessment: PortfolioAssessmentService = (
            portfolio_assessment_service
        ),
        portfolio_quant_risk: PortfolioRiskEngine = portfolio_risk_engine,
        portfolio_performance: PortfolioPerformanceService = (
            portfolio_performance_service
        ),
        portfolio_exposure: PortfolioExposureService = portfolio_exposure_service,
        portfolio_event_impacts: PortfolioEventImpactService = (
            portfolio_event_impact_service
        ),
        repair=financial_research_repair_service,
    ) -> None:
        # 构造函数注入让测试可以用小型 Fake 替换数据库和行情服务。
        self.planner = planner
        self.gate = gate
        self.funds = funds
        self.fund_risk = fund_risk
        self.fund_assessment = fund_assessment
        self.fund_disclosures = fund_disclosures
        self.portfolios = portfolios
        self.portfolio_assessment = portfolio_assessment
        self.portfolio_quant_risk = portfolio_quant_risk
        self.portfolio_performance = portfolio_performance
        self.portfolio_exposure = portfolio_exposure
        self.portfolio_event_impacts = portfolio_event_impacts
        self.repair = repair

    def build_fund(
        self,
        *,
        fund_code: str,
        question: str,
        months: int = 12,
    ) -> dict[str, Any]:
        """为单只基金构造研究计划、并行证据和充分性判断。"""

        self._validate_months(months)
        try:
            detail = self.funds.get_fund(fund_code)
            profile = {
                "fund_code": fund_code,
                **dict(detail.get("profile") or {}),
            }
        except Exception as exc:
            raise FinancialResearchBriefError(
                f"无法确认基金研究对象：{exc}"
            ) from exc

        plan = self.planner.plan(
            question=question,
            scope="fund",
            profiles=[profile],
        )
        # 风险、综合证据和季报披露互不修改数据，可以并行读取。综合证据内部仍复用
        # 原有风险规则；单独的 risk 分支用于给 Brief 保留更完整的数值事实。
        tasks = {
            "fund_risk": lambda: self.fund_risk.analyze(fund_code=fund_code, months=months),
            "fund_assessment": lambda: self.fund_assessment.assess(fund_code=fund_code, months=months),
            "fund_disclosure": lambda: self._fund_disclosure(fund_code),
        }
        task_results = self._run_parallel(tasks)

        # 补查前后使用完全相同的计划与闸门，更新的是证据，不是人为修改评分。
        def evaluate() -> dict[str, Any]:
            return self.gate.evaluate(
                plan=plan,
                scope_data={"profile": profile},
                task_results=task_results,
                question=question,
            )

        gate, repair = self.repair.run(
            gate=evaluate(), task_results=task_results, tasks=tasks,
            evaluate=evaluate, run_tasks=self._run_parallel, fund_codes=[fund_code],
            disclosure_tasks=("fund_disclosure", "fund_assessment"), scope_key=f"fund:{fund_code}",
        )
        # 事实编号在工具返回前生成，模型和校验器读同一份结果，不另查一遍数据。
        from app.services.fund_answer import build_answer_facts

        summary = self._fund_summary(profile=profile, task_results=task_results)
        return {
            "ok": True,
            "data_kind": "deterministic_financial_research_brief",
            "brief_version": BRIEF_VERSION,
            "scope": {
                "kind": "fund",
                "fund_code": fund_code,
                "name": profile.get("short_name") or fund_code,
                "fund_type": profile.get("fund_type"),
            },
            "question": str(question)[:800],
            "period_months": months,
            "research_plan": plan,
            "evidence_gate": gate,
            "evidence_repair": repair,
            "execution": self._public_execution(task_results),
            "evidence_summary": summary,
            "answer_facts": build_answer_facts(fund_code, summary),
            "answer_contract": self._answer_contract(gate),
            "disclaimer": (
                "本 Brief 只组织历史风险、披露持仓、市场和事件证据；"
                "大模型只负责解释，不代表未来收益，也不会自动执行交易。"
            ),
        }

    def build_portfolio(
        self,
        *,
        user_id: str,
        portfolio_id: str,
        question: str,
        months: int = 12,
    ) -> dict[str, Any]:
        """为当前用户的一份组合构造完整但有界的研究 Brief。"""

        self._validate_months(months)
        try:
            portfolio = self.portfolios.get(
                user_id=user_id,
                portfolio_id=portfolio_id,
            )
        except Exception as exc:
            raise FinancialResearchBriefError(
                f"无法确认组合研究对象：{exc}"
            ) from exc

        profiles = [
            {
                "fund_code": item.get("fund_code"),
                "short_name": item.get("short_name"),
                "fund_type": item.get("fund_type"),
                "sample_role": item.get("sample_role"),
            }
            for item in list(portfolio.get("holdings") or [])
        ]
        plan = self.planner.plan(
            question=question,
            scope="portfolio",
            profiles=profiles,
        )

        # 五个分支全部只读。某一个外部数据源失败时，其他结果仍会进入证据闸门，
        # 用户会看到“哪一路失败”，而不是得到一个没有原因的 500。
        tasks = {
            "portfolio_assessment": lambda: self.portfolio_assessment.assess(
                user_id=user_id, portfolio_id=portfolio_id, months=months,
            ),
            "portfolio_quant_risk": lambda: self.portfolio_quant_risk.analyze(
                user_id=user_id, portfolio_id=portfolio_id, months=months,
            ),
            "portfolio_performance": lambda: self.portfolio_performance.analyze(
                user_id=user_id, portfolio_id=portfolio_id, months=months,
            ),
            "portfolio_exposure": lambda: self.portfolio_exposure.analyze(
                user_id=user_id, portfolio_id=portfolio_id,
            ),
            "portfolio_event_impacts": lambda: self.portfolio_event_impacts.analyze(
                user_id=user_id, portfolio_id=portfolio_id, limit=8,
            ),
        }
        task_results = self._run_parallel(tasks)

        def evaluate() -> dict[str, Any]:
            return self.gate.evaluate(
                plan=plan,
                scope_data=portfolio,
                task_results=task_results,
                question=question,
            )

        # 持仓来自上面的归属校验结果，不接受模型自填“待补查基金列表”。
        codes = [str(item["fund_code"]) for item in sorted(
            portfolio.get("holdings") or [], key=lambda item: float(item.get("weight_pct") or 0), reverse=True,
        )]
        gate, repair = self.repair.run(
            gate=evaluate(), task_results=task_results, tasks=tasks,
            evaluate=evaluate, run_tasks=self._run_parallel, fund_codes=codes,
            disclosure_tasks=("portfolio_exposure", "portfolio_event_impacts", "portfolio_assessment"),
            scope_key=f"portfolio:{user_id}:{portfolio_id}",
        )
        return {
            "ok": True,
            "data_kind": "deterministic_financial_research_brief",
            "brief_version": BRIEF_VERSION,
            "scope": {
                "kind": "portfolio",
                "portfolio_id": portfolio_id,
                "name": portfolio.get("name") or portfolio_id,
                "position_mode": portfolio.get("position_mode"),
                "holding_count": len(portfolio.get("holdings") or []),
            },
            "question": str(question)[:800],
            "period_months": months,
            "research_plan": plan,
            "evidence_gate": gate,
            "evidence_repair": repair,
            "execution": self._public_execution(task_results),
            "evidence_summary": self._portfolio_summary(
                portfolio=portfolio,
                task_results=task_results,
            ),
            "answer_contract": self._answer_contract(gate),
            "disclaimer": (
                "组合 Brief 使用历史净值、基金专属基准、定期披露持仓和有效事件；"
                "关联仓位不是预计涨跌，历史风险不代表未来，也不会自动执行交易。"
            ),
        }

    def _run_parallel(
        self,
        tasks: dict[str, Callable[[], dict[str, Any]]],
    ) -> dict[str, dict[str, Any]]:
        """并行执行独立只读任务，并把失败保留为可检查的数据。"""

        if not tasks:
            return {}
        started_at: dict[str, float] = {}
        completed: dict[str, dict[str, Any]] = {}

        # ContextVar 默认不会自动复制到新线程。每个任务使用独立 copy，确保固定 Eval
        # 数据模式等可信上下文不会在并行采集时意外丢失。
        with ThreadPoolExecutor(
            max_workers=min(5, len(tasks)),
            thread_name_prefix="fundscope-research",
        ) as executor:
            futures = {}
            for name, callback in tasks.items():
                started_at[name] = perf_counter()
                context = copy_context()
                futures[executor.submit(context.run, callback)] = name

            for future in as_completed(futures):
                name = futures[future]
                duration_ms = round((perf_counter() - started_at[name]) * 1000, 2)
                try:
                    payload = future.result()
                    if not isinstance(payload, dict):
                        raise TypeError("业务服务没有返回 JSON 对象")
                    completed[name] = {
                        "status": "completed",
                        "duration_ms": duration_ms,
                        "data": payload,
                        "error": None,
                    }
                except Exception as exc:
                    completed[name] = {
                        "status": "failed",
                        "duration_ms": duration_ms,
                        "data": None,
                        "error": str(exc)[:300],
                    }

        # future 完成顺序不稳定；按计划中的任务顺序重建字典，方便测试和前端阅读。
        return {name: completed[name] for name in tasks}

    def _fund_disclosure(self, fund_code: str) -> dict[str, Any]:
        payload = self.fund_disclosures.list_for_funds([fund_code])
        result = payload.get(fund_code)
        if not isinstance(result, dict):
            raise FinancialResearchBriefError("基金披露服务没有返回当前基金")
        return result

    @staticmethod
    def _fund_summary(
        *,
        profile: dict[str, Any],
        task_results: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        risk = FinancialResearchBriefService._task_data(task_results, "fund_risk")
        assessment = FinancialResearchBriefService._task_data(
            task_results, "fund_assessment"
        )
        disclosure = FinancialResearchBriefService._task_data(
            task_results, "fund_disclosure"
        )
        benchmark = (risk or {}).get("benchmark_comparison") or {}
        return {
            "profile": {
                key: profile.get(key)
                for key in (
                    "fund_code",
                    "short_name",
                    "fund_type",
                    "sample_role",
                    "management_company",
                    "fund_managers",
                    "management_fee",
                    "custodian_fee",
                    "benchmark",
                    "tracking_target",
                    "source_url",
                )
            },
            "historical_risk": {
                key: (risk or {}).get(key)
                for key in (
                    "period_start",
                    "period_end",
                    "observation_count",
                    "period_return_pct",
                    "annualized_volatility_pct",
                    "downside_volatility_pct",
                    "current_drawdown_pct",
                    "worst_daily_return_pct",
                    "longest_drawdown_days",
                    "return_basis_label",
                    "return_basis_status",
                    "data_quality_warnings",
                )
            }
            | {
                "max_drawdown": (risk or {}).get("max_drawdown"),
                "risk_adjusted": (risk or {}).get("risk_adjusted"),
                "benchmark": {
                    key: benchmark.get(key)
                    for key in (
                        "benchmark_name",
                        "status",
                        "period_return_pct",
                        "fund_aligned_return_pct",
                        "excess_return_pct",
                        "coverage_pct",
                        "warnings",
                    )
                },
            },
            "rule_assessment": {
                "state_code": (assessment or {}).get("state_code"),
                "state_label": (assessment or {}).get("state_label"),
                "summary": (assessment or {}).get("summary"),
                "score_breakdown": (assessment or {}).get("score_breakdown"),
                "component_dates": (assessment or {}).get("component_dates"),
                "top_risk_evidence": FinancialResearchBriefService._compact_evidence(
                    (assessment or {}).get("risk_evidence"), 4
                ),
                "top_supporting_evidence": FinancialResearchBriefService._compact_evidence(
                    (assessment or {}).get("supporting_evidence"), 3
                ),
                "active_event_count": (assessment or {}).get("active_event_count"),
            },
            "disclosure": FinancialResearchBriefService._compact_fund_disclosure(
                fund_code=str(profile.get("fund_code") or ""),
                disclosure=disclosure,
            ),
        }

    @staticmethod
    def _portfolio_summary(
        *,
        portfolio: dict[str, Any],
        task_results: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        assessment = FinancialResearchBriefService._task_data(
            task_results, "portfolio_assessment"
        )
        quant = FinancialResearchBriefService._task_data(
            task_results, "portfolio_quant_risk"
        )
        performance = FinancialResearchBriefService._task_data(
            task_results, "portfolio_performance"
        )
        exposure = FinancialResearchBriefService._task_data(
            task_results, "portfolio_exposure"
        )
        impacts = FinancialResearchBriefService._task_data(
            task_results, "portfolio_event_impacts"
        )
        benchmark = (performance or {}).get("benchmark") or {}
        return {
            "holdings": [
                {
                    key: item.get(key)
                    for key in (
                        "fund_code",
                        "short_name",
                        "fund_type",
                        "sample_role",
                        "weight_pct",
                        "latest_nav_date",
                    )
                }
                for item in list(portfolio.get("holdings") or [])[:12]
            ],
            "rule_assessment": {
                "state_code": (assessment or {}).get("state_code"),
                "state_label": (assessment or {}).get("state_label"),
                "summary": (assessment or {}).get("summary"),
                "concentration": (assessment or {}).get("concentration"),
                "score_breakdown": (assessment or {}).get("score_breakdown"),
                "top_risk_evidence": FinancialResearchBriefService._compact_evidence(
                    (assessment or {}).get("risk_evidence"), 4
                ),
                "top_supporting_evidence": FinancialResearchBriefService._compact_evidence(
                    (assessment or {}).get("supporting_evidence"), 3
                ),
            },
            "quantitative_risk": {
                key: (quant or {}).get(key)
                for key in (
                    "period_start",
                    "period_end",
                    "observation_count",
                    "period_return_pct",
                    "annualized_volatility_pct",
                    "daily_cvar_95_pct",
                    "max_drawdown",
                    "top_risk_contributor",
                    "risk_contributions",
                    "correlation",
                    "return_basis_label",
                    "data_quality_warnings",
                )
            },
            "performance": {
                key: (performance or {}).get(key)
                for key in (
                    "data_kind",
                    "performance_label",
                    "position_mode",
                    "period_start",
                    "period_end",
                    "twr_pct",
                    "xirr_pct",
                    "annualized_return_pct",
                    "annualized_volatility_pct",
                    "sharpe_ratio",
                    "sortino_ratio",
                    "max_drawdown",
                    "warnings",
                )
            }
            | {
                "benchmark": {
                    key: benchmark.get(key)
                    for key in (
                        "benchmark_name",
                        "status",
                        "period_return_pct",
                        "portfolio_aligned_return_pct",
                        "excess_return_pct",
                        "coverage_pct",
                    )
                }
            },
            "lookthrough": {
                "summary": (exposure or {}).get("summary"),
                "oldest_report_date": (exposure or {}).get("oldest_report_date"),
                "latest_report_date": (exposure or {}).get("latest_report_date"),
                "data_quality": (exposure or {}).get("data_quality"),
                "concentration": (exposure or {}).get("concentration"),
                "top_securities": [
                    FinancialResearchBriefService._compact_exposure(item)
                    for item in list((exposure or {}).get("security_exposures") or [])[:6]
                ],
                "top_industries": [
                    FinancialResearchBriefService._compact_exposure(item)
                    for item in list((exposure or {}).get("industry_exposures") or [])[:5]
                ],
                "fund_overlaps": list((exposure or {}).get("fund_overlaps") or [])[:4],
            },
            "event_impacts": {
                "summary": (impacts or {}).get("summary"),
                "event_data_cutoff": (impacts or {}).get("event_data_cutoff"),
                "latest_holding_report_date": (impacts or {}).get(
                    "latest_holding_report_date"
                ),
                "quality": (impacts or {}).get("quality"),
                "top_impacts": [
                    {
                        key: item.get(key)
                        for key in (
                            "impact_evidence_id",
                            "event_id",
                            "title",
                            "event_date",
                            "direction",
                            "affected_exposure_pct",
                            "match_label",
                            "source_name",
                            "source_url",
                            "reason",
                            "uncertainties",
                        )
                    }
                    for item in list((impacts or {}).get("impacts") or [])[:5]
                ],
            },
        }

    @staticmethod
    def _compact_evidence(items: Any, limit: int) -> list[dict[str, Any]]:
        result = []
        for item in list(items or [])[:limit]:
            result.append(
                {
                    key: item.get(key)
                    for key in (
                        "evidence_id",
                        "kind",
                        "direction",
                        "strength",
                        "title",
                        "value_text",
                        "explanation",
                        "data_date",
                        "source_name",
                        "source_url",
                        "affected_fund_codes",
                        "portfolio_weight_coverage_pct",
                    )
                }
            )
        return result

    @staticmethod
    def _compact_fund_disclosure(
        *,
        fund_code: str,
        disclosure: dict[str, Any] | None,
    ) -> dict[str, Any]:
        payload = disclosure or {}

        def security(item: dict[str, Any]) -> dict[str, Any]:
            report_date = item.get("report_date")
            code = item.get("security_code")
            return {
                "evidence_id": f"disclosure:{fund_code}:{code}:{report_date}",
                **{
                    key: item.get(key)
                    for key in (
                        "asset_type",
                        "security_code",
                        "security_name",
                        "weight_pct",
                        "report_date",
                        "source_url",
                    )
                },
            }

        return {
            "data_mode": payload.get("data_mode"),
            "latest_report_date": payload.get("latest_report_date"),
            "freshness_status": payload.get("freshness_status"),
            "security_count": payload.get("security_count"),
            "industry_count": payload.get("industry_count"),
            "disclosed_stock_weight_pct": payload.get("disclosed_stock_weight_pct"),
            "disclosed_bond_weight_pct": payload.get("disclosed_bond_weight_pct"),
            "industry_coverage_pct": payload.get("industry_coverage_pct"),
            "top_securities": [
                security(item)
                for item in list(payload.get("security_holdings") or [])[:8]
            ],
            "top_industries": [
                {
                    "evidence_id": (
                        f"disclosure:{fund_code}:industry:"
                        f"{item.get('industry_name')}:{item.get('report_date')}"
                    ),
                    **{
                        key: item.get(key)
                        for key in (
                            "industry_name",
                            "weight_pct",
                            "report_date",
                            "source_url",
                        )
                    },
                }
                for item in list(payload.get("industry_allocations") or [])[:6]
            ],
            "warnings": list(payload.get("warnings") or [])[:6],
        }

    @staticmethod
    def _compact_exposure(item: dict[str, Any]) -> dict[str, Any]:
        return {
            key: item.get(key)
            for key in (
                "evidence_id",
                "asset_type",
                "security_code",
                "security_name",
                "industry_name",
                "portfolio_exposure_pct",
                "source_fund_count",
                "report_date",
                "source_url",
            )
        }

    @staticmethod
    def _public_execution(
        task_results: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        tasks = [
            {
                "name": name,
                "status": result["status"],
                "duration_ms": result["duration_ms"],
                "error": result.get("error"),
            }
            for name, result in task_results.items()
        ]
        return {
            "mode": "parallel_read_only",
            "task_count": len(tasks),
            "completed_count": sum(item["status"] == "completed" for item in tasks),
            "failed_count": sum(item["status"] == "failed" for item in tasks),
            "tasks": tasks,
        }

    @staticmethod
    def _answer_contract(gate: dict[str, Any]) -> list[str]:
        rules = [
            "先说明 evidence_gate 的状态和数据边界，再给金融解释。",
            "说明 evidence_repair 中实际补查了什么、前后变化及停止原因；checked_no_change 不代表有更新。",
            "区分用户信息缺口、公开数据缺口与尚未支持的能力；只向用户补问缺少的个人条件。",
            "补查授权只对本轮有效。未授权不声称联网，失败时保留原证据日期；不得靠模型猜测补齐缺口。",
            "evidence_repair.warnings 若说明重读失败，相关结论必须注明仍依赖补查前的旧证据。",
            "数值只能来自 evidence_summary，证据编号必须逐字符引用。",
            "风险状态、市场事件和用户适当性是不同层次，不得互相替代。",
            "披露持仓不是实时仓位，关联仓位不是预计涨跌幅。",
            "不要声称已经执行申购、赎回或调仓。",
        ]
        if gate["conclusion_policy"] == "conditional_only":
            rules.append("当前只能给条件式观点，并列出 clarification_questions。")
        elif gate["conclusion_policy"] == "facts_and_gaps_only":
            rules.append("当前只陈述已确认事实和缺口，不生成强方向性结论。")
        return rules

    @staticmethod
    def _task_data(
        task_results: dict[str, dict[str, Any]],
        name: str,
    ) -> dict[str, Any] | None:
        result = task_results.get(name) or {}
        data = result.get("data")
        return data if result.get("status") == "completed" and isinstance(data, dict) else None

    @staticmethod
    def _validate_months(months: int) -> None:
        if int(months) not in SUPPORTED_MONTHS:
            raise FinancialResearchBriefError("研究区间只支持 6、12 或 36 个月")


financial_research_brief_service = FinancialResearchBriefService()
