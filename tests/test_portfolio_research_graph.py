"""LangGraph 组合研究、Background Job 和 FastAPI 接口的确定性测试。

运行方式：

    conda run --no-capture-output -n dl \
      python -m unittest tests.test_portfolio_research_graph -v

本文件全部使用 Fake Service，不会调用 DeepSeek、Embedding、AKShare 或网络数据源。
"""

from __future__ import annotations

import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

from app.database import Database
from app.main import app
from app.services.background_jobs import BackgroundJobService
from tests.task_authorization_fixture import install_research_authorization
from app.services.portfolio_research_graph import PortfolioResearchGraphService
from app.services.portfolio_research_checkpoints import (
    create_portfolio_research_checkpoint_runtime,
)
from app.services.portfolio_research_interpretation import (
    PortfolioResearchInterpretationError,
)
from app.services.portfolio_research_snapshots import (
    PortfolioResearchSnapshotService,
    PortfolioResearchSnapshotStore,
)


PORTFOLIO_ID = "portfolio_0123456789abcdef"


class FakePortfolioService:
    """只提供图和 Router 真正需要的一份组合。"""

    def get(self, *, user_id: str, portfolio_id: str) -> dict:
        if user_id != "chen" or portfolio_id != PORTFOLIO_ID:
            raise AssertionError("测试传入了错误的用户或组合 ID")
        return {
            "id": portfolio_id,
            "user_id": user_id,
            "name": "LangGraph 测试组合",
            "holding_count": 2,
            "holdings": [
                {
                    "fund_code": "110020",
                    "short_name": "沪深300联接",
                    "weight_pct": 60.0,
                },
                {
                    "fund_code": "110007",
                    "short_name": "稳健债券",
                    "weight_pct": 40.0,
                },
            ],
        }


class FakeAssessmentService:
    """返回足以验证报告汇总的固定组合研判。"""

    def assess(
        self,
        *,
        user_id: str,
        portfolio_id: str,
        months: int,
    ) -> dict:
        return {
            "portfolio_id": portfolio_id,
            "portfolio_name": "LangGraph 测试组合",
            "period_months": months,
            "assessment_date": "2026-08-28",
            "state_code": "cautious",
            "state_label": "谨慎观察",
            "confidence": "medium",
            "summary": "组合风险证据暂时高于支撑证据。",
            "score_breakdown": {
                "supportive_points": 1.2,
                "caution_points": 2.4,
                "net_points": -1.2,
                "method": "测试加权方法",
            },
            "concentration": {
                "hhi": 0.52,
                "effective_fund_count": 1.92,
                "max_weight_pct": 60.0,
                "state_code": "concentrated",
                "state_label": "偏集中",
            },
            "fund_contributions": [
                {
                    "fund_code": "110020",
                    "short_name": "沪深300联接",
                    "weight_pct": 60.0,
                    "weighted_caution_points": 1.8,
                    "weighted_supportive_points": 0.5,
                },
                {
                    "fund_code": "110007",
                    "short_name": "稳健债券",
                    "weight_pct": 40.0,
                    "weighted_caution_points": 0.6,
                    "weighted_supportive_points": 0.7,
                },
            ],
            "risk_evidence": [
                {
                    "evidence_id": "risk:test:1",
                    "title": "风险测试证据",
                    "explanation": "用于验证报告证据字段。",
                    "data_date": "2026-08-28",
                    "source_name": "测试来源",
                    "source_url": "https://example.com/risk",
                    "weighted_points": 1.8,
                }
            ],
            "supporting_evidence": [],
            "limitations": ["测试数据不代表真实市场。"],
            "disclaimer": "仅用于测试。",
        }


class FakePortfolioRiskEngine:
    """模拟 Riskfolio 结果，让图测试只验证编排而不重复测试数学库。"""

    def analyze(
        self,
        *,
        user_id: str,
        portfolio_id: str,
        months: int,
    ) -> dict:
        return {
            "data_kind": "quantitative_portfolio_risk",
            "portfolio_id": portfolio_id,
            "portfolio_name": "LangGraph 测试组合",
            "period_months": months,
            "period_start": "2025-08-28",
            "period_end": "2026-08-28",
            "return_observation_count": 240,
            "period_return_pct": 4.2,
            "annualized_volatility_pct": 8.5,
            "daily_cvar_95_pct": 1.15,
            "max_drawdown": {
                "drawdown_pct": 6.4,
                "peak_date": "2026-03-01",
                "trough_date": "2026-04-01",
                "recovery_date": "2026-05-01",
            },
            "risk_contributions": [
                {
                    "fund_code": "110020",
                    "short_name": "沪深300联接",
                    "weight_pct": 60.0,
                    "annualized_risk_contribution_pct_points": 7.0,
                    "risk_share_pct": 82.35,
                    "absolute_risk_share_pct": 82.35,
                },
                {
                    "fund_code": "110007",
                    "short_name": "稳健债券",
                    "weight_pct": 40.0,
                    "annualized_risk_contribution_pct_points": 1.5,
                    "risk_share_pct": 17.65,
                    "absolute_risk_share_pct": 17.65,
                },
            ],
            "top_risk_contributor": {
                "fund_code": "110020",
                "short_name": "沪深300联接",
                "weight_pct": 60.0,
                "annualized_risk_contribution_pct_points": 7.0,
                "risk_share_pct": 82.35,
                "absolute_risk_share_pct": 82.35,
            },
            "correlation": {
                "fund_codes": ["110020", "110007"],
                "values": [[1.0, 0.2], [0.2, 1.0]],
                "strongest_pair": {
                    "left_fund_code": "110020",
                    "right_fund_code": "110007",
                    "correlation": 0.2,
                },
            },
            "engine": {"name": "Riskfolio-Lib", "version": "test"},
            "limitations": ["量化测试限制。"],
        }

    def analyze_for_research_snapshot(
        self,
        *,
        user_id: str,
        portfolio_id: str,
        months: int,
    ) -> dict:
        result = self.analyze(
            user_id=user_id,
            portfolio_id=portfolio_id,
            months=months,
        )
        result["_scenario_validation_inputs"] = {
            "schema_version": "portfolio-scenario-input-v1",
            "fund_codes": ["110020", "110007"],
            "short_names": {
                "110020": "沪深300联接",
                "110007": "稳健债券",
            },
            "dates": [f"2026-07-{day:02d}" for day in range(1, 26)],
            "daily_returns": [
                [0.004 if day % 2 else -0.003, 0.001]
                for day in range(1, 26)
            ],
            "period_start": "2026-07-01",
            "period_end": "2026-07-25",
            "return_observation_count": 25,
            "return_basis": "total_return_reinvested",
            "return_basis_label": "分红再投资总回报",
            "return_basis_status": "verified",
            "data_quality_warnings": [],
            "engine": {"name": "Riskfolio-Lib", "version": "test"},
        }
        return result


class FakePortfolioPerformanceService:
    """返回完整业绩对象，图节点应删掉 series 后再写入 checkpoint。"""

    def analyze(
        self,
        *,
        user_id: str,
        portfolio_id: str,
        months: int,
    ) -> dict:
        return {
            "data_kind": "simulated_allocation_performance",
            "performance_label": "当前目标权重的历史模拟业绩",
            "portfolio_id": portfolio_id,
            "portfolio_name": "LangGraph 测试组合",
            "position_mode": "allocation",
            "period_months": months,
            "period_start": "2025-08-28",
            "period_end": "2026-08-28",
            "observation_count": 240,
            "twr_pct": 4.2,
            "xirr_pct": None,
            "annualized_return_pct": 4.3,
            "annualized_volatility_pct": 8.5,
            "sharpe_ratio": 0.42,
            "sortino_ratio": 0.61,
            "max_drawdown": {
                "drawdown_pct": -6.4,
                "trough_date": "2026-04-01",
            },
            "benchmark": {
                "benchmark_name": "测试混合基准",
                "status": "matched",
                "period_return_pct": 3.0,
                "portfolio_aligned_return_pct": 4.2,
                "excess_return_pct": 1.2,
                "coverage_pct": 100.0,
                "source": "测试基准",
            },
            "cash_flows": {
                "opening_value": None,
                "contributions": None,
                "withdrawals": None,
                "fees": None,
                "net_invested": None,
                "ending_value": None,
            },
            "attribution": [
                {
                    "fund_code": "110020",
                    "short_name": "沪深300联接",
                    "ending_weight_pct": 60.0,
                    "pnl_contribution_amount": None,
                    "return_contribution_pct_points": 3.5,
                    "return_basis_label": "分红再投资总回报",
                },
                {
                    "fund_code": "110007",
                    "short_name": "稳健债券",
                    "ending_weight_pct": 40.0,
                    "pnl_contribution_amount": None,
                    "return_contribution_pct_points": 0.7,
                    "return_basis_label": "分红再投资总回报",
                },
            ],
            "series": [
                {"date": "2026-08-27", "portfolio_index": 103.9},
                {"date": "2026-08-28", "portfolio_index": 104.2},
            ],
            "assumptions": ["当前权重每日再平衡。"],
            "warnings": ["目标配置没有真实现金流水，因此不计算 XIRR。"],
            "methodology": "测试业绩方法。",
        }


class FakePortfolioDecisionService:
    """默认返回一条可复盘记录，也可模拟尚未写过决策的组合。"""

    def __init__(self, *, empty: bool = False) -> None:
        self.empty = empty

    def list(self, *, user_id: str, portfolio_id: str) -> list[dict]:
        if self.empty:
            return []
        return [
            {
                "id": "decision_1111111111111111",
                "decision_date": "2026-06-01",
                "target_date": "2026-08-30",
                "action": "hold",
                "thesis": "回撤仍在承受范围，等待市场方向更明确。",
                "horizon_days": 90,
                "snapshot": {
                    "performance": {
                        "period_end": "2026-06-01",
                        "twr_pct": 2.0,
                        "excess_return_pct": 0.5,
                    },
                    "assessment": {
                        "assessment_date": "2026-06-01",
                        "state_code": "balanced",
                        "state_label": "风险收益均衡",
                        "summary": "当时风险与支撑证据均衡。",
                        "risk_evidence_ids": ["risk:test:1"],
                        "supporting_evidence_ids": [],
                    },
                    "research": None,
                },
                "outcome": {
                    "status": "review_ready",
                    "as_of_date": "2026-08-28",
                    "elapsed_days": 88,
                    "target_days": 90,
                    "portfolio_return_pct": 3.0,
                    "benchmark_return_pct": 2.0,
                    "excess_return_pct": 1.0,
                    "note": "观察期接近结束，可以准备复盘。",
                },
                "review_note": "",
                "reviewed_at": None,
            }
        ]


class FakeMarketSentimentService:
    """固定市场环境让测试不依赖运行当天行情。"""

    def analyze(self) -> dict:
        return {
            "data_date": "2026-08-28",
            "state_code": "balanced",
            "state_label": "震荡均衡",
            "confidence": "medium",
            "summary": "趋势和市场宽度暂未形成一致方向。",
            "signals": [
                {
                    "key": "trend",
                    "label": "趋势",
                    "direction": "neutral",
                    "value_text": "震荡",
                    "evidence": "20 日与 60 日方向不同。",
                }
            ],
            "evidence_ids": ["market:test:1"],
        }


class FakeMarketEventService:
    """同时返回相关和无关事件，验证节点会按持仓过滤。"""

    def search(self, *, limit: int = 20, **_: object) -> dict:
        return {
            "data_cutoff": "2026-08-28",
            "events": [
                {
                    "event_id": "event-related",
                    "event_date": "2026-08-27",
                    "title": "宽基相关事件",
                    "summary": "影响当前组合中的宽基基金。",
                    "impact_direction": "neutral",
                    "impact_horizon": "short_term",
                    "affected_fund_codes": ["110020"],
                    "source_name": "测试来源",
                    "source_url": "https://example.com/event-related",
                    "freshness_status": "fresh",
                    "decay_factor": 1.0,
                },
                {
                    "event_id": "event-unrelated",
                    "event_date": "2026-08-27",
                    "title": "无关基金事件",
                    "summary": "不应进入当前组合上下文。",
                    "impact_direction": "caution",
                    "impact_horizon": "short_term",
                    "affected_fund_codes": ["999999"],
                    "source_name": "测试来源",
                    "source_url": "https://example.com/event-unrelated",
                },
            ][:limit],
        }


class FakePortfolioInterpreter:
    """模拟通过校验的 DeepSeek 结果，也可以主动触发回退路径。"""

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail

    def interpret(
        self,
        *,
        portfolio: dict,
        assessment: dict,
        quant_risk: dict,
        market_context: dict,
        performance_snapshot: dict,
        decision_history: dict,
    ) -> dict:
        if self.fail:
            raise PortfolioResearchInterpretationError("模型输出未通过校验。")
        return self._result(
            portfolio=portfolio,
            assessment=assessment,
            quant_risk=quant_risk,
            market_context=market_context,
            performance_snapshot=performance_snapshot,
            decision_history=decision_history,
            model_used=True,
        )

    def fallback(
        self,
        *,
        portfolio: dict,
        assessment: dict,
        quant_risk: dict,
        market_context: dict,
        performance_snapshot: dict,
        decision_history: dict,
        reason: str,
    ) -> dict:
        result = self._result(
            portfolio=portfolio,
            assessment=assessment,
            quant_risk=quant_risk,
            market_context=market_context,
            performance_snapshot=performance_snapshot,
            decision_history=decision_history,
            model_used=False,
        )
        result["fallback_reason"] = "模型输出未通过校验，已确定性回退。"
        return result

    @staticmethod
    def _result(
        *,
        portfolio: dict,
        assessment: dict,
        quant_risk: dict,
        market_context: dict,
        performance_snapshot: dict,
        decision_history: dict,
        model_used: bool,
    ) -> dict:
        return {
            "status": "accepted" if model_used else "fallback",
            "model_used": model_used,
            "model": "fake-deepseek",
            "prompt_version": "test-v1",
            "attempt_count": 1 if model_used else 2,
            "generated_at": "2026-08-28T00:00:00+00:00",
            "current_state": "cautious",
            "confidence": 0.78 if model_used else 0.6,
            "headline": f"{portfolio['name']}需要关注风险来源",
            "summary": (
                f"{assessment['summary']} {market_context['summary']} "
                f"{performance_snapshot['performance_label']}，"
                f"历史决策 {decision_history['decision_count']} 条。"
            ),
            "key_findings": [
                {
                    "tone": "caution",
                    "title": "量化风险",
                    "detail": "主要风险来源需要优先检查。",
                    "evidence_ids": ["risk:test:1"],
                    "evidence_references": [],
                    "metric_keys": ["risk.annualized_volatility_pct"],
                    "metric_references": [
                        {
                            "key": "risk.annualized_volatility_pct",
                            "label": "年化波动率",
                            "value": quant_risk["annualized_volatility_pct"],
                            "value_text": "8.50%",
                            "data_date": quant_risk["period_end"],
                        }
                    ],
                    "fund_codes": [
                        quant_risk["top_risk_contributor"]["fund_code"]
                    ],
                },
                {
                    "tone": "neutral",
                    "title": "市场环境",
                    "detail": market_context["summary"],
                    "evidence_ids": ["market:test:1"],
                    "evidence_references": [],
                    "metric_keys": [],
                    "metric_references": [],
                    "fund_codes": [],
                },
            ],
            "conflicts": [],
            "uncertainties": ["测试不确定性。"],
            "conditional_view": "应结合个人承受能力理解当前风险。",
            "follow_up_items": ["继续观察市场方向。"],
            "fallback_reason": None,
        }


class FailOncePortfolioInterpreter(FakePortfolioInterpreter):
    """第一次模拟进程中断，第二次返回正常结果，用来验证真正的断点续跑。"""

    def __init__(self) -> None:
        super().__init__()
        self.attempt_count = 0

    def interpret(
        self,
        *,
        portfolio: dict,
        assessment: dict,
        quant_risk: dict,
        market_context: dict,
        performance_snapshot: dict,
        decision_history: dict,
    ) -> dict:
        self.attempt_count += 1
        if self.attempt_count == 1:
            # 这里故意不用 PortfolioResearchInterpretationError，因为后者会在节点内部
            # 正常降级；RuntimeError 才能模拟 Worker 在节点执行中意外退出。
            raise RuntimeError("模拟 interpret_with_llm 节点中断")
        return super().interpret(
            portfolio=portfolio,
            assessment=assessment,
            quant_risk=quant_risk,
            market_context=market_context,
            performance_snapshot=performance_snapshot,
            decision_history=decision_history,
        )


def build_graph_service(
    *,
    interpreter: FakePortfolioInterpreter | None = None,
    decisions: FakePortfolioDecisionService | None = None,
    snapshots: PortfolioResearchSnapshotStore | None = None,
) -> PortfolioResearchGraphService:
    """每条测试创建独立图和内存 checkpoint，避免 thread_id 相互污染。"""

    return PortfolioResearchGraphService(
        portfolios=FakePortfolioService(),
        assessments=FakeAssessmentService(),
        quant_risk=FakePortfolioRiskEngine(),
        performance=FakePortfolioPerformanceService(),
        decisions=decisions or FakePortfolioDecisionService(),
        market_sentiment=FakeMarketSentimentService(),
        market_events=FakeMarketEventService(),
        interpreter=interpreter or FakePortfolioInterpreter(),
        snapshots=snapshots,
    )


class PortfolioResearchGraphTests(unittest.TestCase):
    def test_graph_runs_parallel_branches_and_saves_checkpoint(self) -> None:
        progress_events: list[tuple[str, str]] = []
        service = build_graph_service()

        report = service.run(
            run_id="graph-run-1",
            user_id="chen",
            portfolio_id=PORTFOLIO_ID,
            months=12,
            progress_callback=lambda node, message: progress_events.append(
                (node, message)
            ),
        )

        self.assertEqual(report["workflow"], "portfolio_research")
        self.assertEqual(report["graph_version"], PortfolioResearchGraphService.GRAPH_VERSION)
        self.assertTrue(report["research_snapshot"]["immutable"])
        self.assertEqual(
            report["research_snapshot"]["schema_version"],
            "portfolio-research-input-v1",
        )
        self.assertEqual(len(report["research_snapshot"]["content_hash"]), 64)
        self.assertEqual(
            report["graph_trace"]["snapshot_id"],
            report["research_snapshot"]["snapshot_id"],
        )
        self.assertTrue(report["generation"]["model_used"])
        self.assertEqual(report["generation"]["mode"], "structured_model_synthesis")
        self.assertEqual(
            report["quant_risk_snapshot"]["annualized_volatility_pct"],
            8.5,
        )
        self.assertNotIn(
            "_scenario_validation_inputs",
            report["quant_risk_snapshot"],
        )
        stored_snapshot = service.snapshots.get(
            user_id="chen",
            snapshot_id=report["research_snapshot"]["snapshot_id"],
        )
        self.assertIn(
            "_scenario_validation_inputs",
            stored_snapshot["payload"]["quant_risk"],
        )
        self.assertEqual(report["market_snapshot"]["relevant_event_count"], 1)
        self.assertEqual(report["performance_snapshot"]["twr_pct"], 4.2)
        self.assertNotIn("series", report["performance_snapshot"])
        self.assertEqual(report["decision_history"]["decision_count"], 1)
        self.assertEqual(
            report["market_snapshot"]["events"][0]["event_id"],
            "event-related",
        )
        self.assertEqual(len(progress_events), 8)
        self.assertEqual(progress_events[0][0], "load_portfolio")
        self.assertEqual(progress_events[-1][0], "compose_report")
        self.assertEqual(
            {node for node, _ in progress_events[1:6]},
            {
                "calculate_assessment",
                "calculate_quant_risk",
                "calculate_performance",
                "load_market_context",
                "load_decision_history",
            },
        )
        self.assertEqual(progress_events[-2][0], "interpret_with_llm")
        self.assertEqual(report["graph_trace"]["checkpoint_backend"], "memory")
        self.assertFalse(report["graph_trace"]["checkpoint_durable"])
        self.assertEqual(report["graph_trace"]["checkpoint_status"], "completed")

        # checkpoint_state 证明最终报告不仅是普通函数返回值，也进入了 LangGraph State。
        checkpoint = service.checkpoint_state(run_id="graph-run-1")
        self.assertEqual(checkpoint["report"]["run_id"], "graph-run-1")
        self.assertEqual(
            checkpoint["input_snapshot"]["content_hash"],
            report["research_snapshot"]["content_hash"],
        )
        self.assertEqual(
            set(checkpoint["completed_nodes"]),
            {
                "load_portfolio",
                "calculate_assessment",
                "calculate_quant_risk",
                "calculate_performance",
                "load_market_context",
                "load_decision_history",
                "interpret_with_llm",
                "compose_report",
            },
        )

    def test_empty_decision_history_does_not_block_report(self) -> None:
        report = build_graph_service(
            decisions=FakePortfolioDecisionService(empty=True)
        ).run(
            run_id="graph-run-no-decisions",
            user_id="chen",
            portfolio_id=PORTFOLIO_ID,
            months=12,
        )

        self.assertFalse(report["decision_history"]["has_decisions"])
        self.assertEqual(report["decision_history"]["decision_count"], 0)
        self.assertIn("暂无可复盘决策", report["decision_history"]["note"])

    def test_model_failure_uses_deterministic_fallback(self) -> None:
        service = build_graph_service(
            interpreter=FakePortfolioInterpreter(fail=True)
        )

        report = service.run(
            run_id="graph-run-fallback",
            user_id="chen",
            portfolio_id=PORTFOLIO_ID,
            months=12,
        )

        self.assertFalse(report["generation"]["model_used"])
        self.assertEqual(report["generation"]["mode"], "deterministic_fallback")
        self.assertEqual(report["model_interpretation"]["status"], "fallback")
        self.assertIn("确定性回退", report["generation"]["fallback_reason"])

    def test_graph_rejects_unsupported_period(self) -> None:
        with self.assertRaisesRegex(ValueError, "6、12 或 36"):
            build_graph_service().run(
                run_id="graph-run-invalid",
                user_id="chen",
                portfolio_id=PORTFOLIO_ID,
                months=24,
            )

    def test_sqlite_checkpoint_resumes_after_service_restart(self) -> None:
        """关闭旧连接并新建 Graph，证明恢复不依赖原 Python 进程的内存。"""

        portfolios = FakePortfolioService()
        assessments = FakeAssessmentService()
        quant_risk = FakePortfolioRiskEngine()
        performance = FakePortfolioPerformanceService()
        decisions = FakePortfolioDecisionService()
        sentiment = FakeMarketSentimentService()
        events = FakeMarketEventService()
        interpreter = FailOncePortfolioInterpreter()

        # Mock 只负责计数，真正返回值仍来自上面的 Fake Service。
        portfolios.get = Mock(wraps=portfolios.get)
        assessments.assess = Mock(wraps=assessments.assess)
        quant_risk.analyze = Mock(wraps=quant_risk.analyze)
        performance.analyze = Mock(wraps=performance.analyze)
        decisions.list = Mock(wraps=decisions.list)
        sentiment.analyze = Mock(wraps=sentiment.analyze)
        events.search = Mock(wraps=events.search)

        with TemporaryDirectory() as temp_dir:
            sqlite_url = f"sqlite:///{Path(temp_dir) / 'checkpoints.db'}"
            snapshot_url = f"sqlite:///{Path(temp_dir) / 'snapshots.db'}"
            snapshot_database_1 = Database(snapshot_url)
            snapshot_store_1 = PortfolioResearchSnapshotService(
                snapshot_database_1
            )
            runtime_1 = create_portfolio_research_checkpoint_runtime(
                backend="sqlite",
                database_url=sqlite_url,
            )
            service_1 = PortfolioResearchGraphService(
                portfolios=portfolios,
                assessments=assessments,
                quant_risk=quant_risk,
                performance=performance,
                decisions=decisions,
                market_sentiment=sentiment,
                market_events=events,
                interpreter=interpreter,
                snapshots=snapshot_store_1,
                checkpointer=runtime_1.checkpointer,
                checkpoint_backend=runtime_1.backend,
                checkpoint_durable=runtime_1.durable,
            )

            with self.assertRaisesRegex(RuntimeError, "模拟.*中断"):
                service_1.run(
                    run_id="durable-run-1",
                    user_id="chen",
                    portfolio_id=PORTFOLIO_ID,
                    months=12,
                )
            interrupted = service_1.checkpoint_info(run_id="durable-run-1")
            self.assertEqual(interrupted["status"], "resumable")
            self.assertEqual(interrupted["next_nodes"], ["interpret_with_llm"])
            frozen = snapshot_store_1.get_for_run(
                user_id="chen",
                run_id="durable-run-1",
            )
            self.assertIsNotNone(frozen)
            assert frozen is not None
            frozen_hash = frozen["content_hash"]
            runtime_1.close()
            snapshot_database_1.engine.dispose()

            # 模拟恢复前行情、组合或事件已经变化。只要快照机制有效，下列 Source
            # 就不应再次调用；否则测试会立刻抛错，而不是生成混合时点报告。
            portfolios.get.side_effect = AssertionError("恢复时不应重读组合")
            assessments.assess.side_effect = AssertionError("恢复时不应重算研判")
            quant_risk.analyze.side_effect = AssertionError("恢复时不应重算风险")
            performance.analyze.side_effect = AssertionError("恢复时不应重算业绩")
            decisions.list.side_effect = AssertionError("恢复时不应重读决策")
            sentiment.analyze.side_effect = AssertionError("恢复时不应重读市场")
            events.search.side_effect = AssertionError("恢复时不应重读事件")

            # 用同一文件创建全新的连接和 Graph Service，相当于 Worker 重启后重新加载。
            runtime_2 = create_portfolio_research_checkpoint_runtime(
                backend="sqlite",
                database_url=sqlite_url,
            )
            snapshot_database_2 = Database(snapshot_url)
            snapshot_store_2 = PortfolioResearchSnapshotService(
                snapshot_database_2
            )
            try:
                service_2 = PortfolioResearchGraphService(
                    portfolios=portfolios,
                    assessments=assessments,
                    quant_risk=quant_risk,
                    performance=performance,
                    decisions=decisions,
                    market_sentiment=sentiment,
                    market_events=events,
                    interpreter=interpreter,
                    snapshots=snapshot_store_2,
                    checkpointer=runtime_2.checkpointer,
                    checkpoint_backend=runtime_2.backend,
                    checkpoint_durable=runtime_2.durable,
                )
                resumed_events: list[str] = []
                report = service_2.run(
                    run_id="durable-run-1",
                    user_id="chen",
                    portfolio_id=PORTFOLIO_ID,
                    months=12,
                    progress_callback=lambda node, _: resumed_events.append(node),
                )

                self.assertEqual(
                    resumed_events,
                    ["interpret_with_llm", "compose_report"],
                )
                self.assertEqual(report["graph_trace"]["checkpoint_backend"], "sqlite")
                self.assertTrue(report["graph_trace"]["checkpoint_durable"])
                self.assertTrue(
                    report["graph_trace"]["resumed_from_checkpoint"]
                )
                self.assertEqual(
                    report["graph_trace"]["checkpoint_status"],
                    "completed",
                )
                self.assertTrue(report["graph_trace"]["checkpoint_id"])
                self.assertEqual(
                    report["research_snapshot"]["content_hash"],
                    frozen_hash,
                )
                self.assertTrue(report["research_snapshot"]["immutable"])

                # 前六个只读节点都来自旧 checkpoint，没有再次查询数据库或重算风险。
                self.assertEqual(portfolios.get.call_count, 1)
                self.assertEqual(assessments.assess.call_count, 1)
                self.assertEqual(quant_risk.analyze.call_count, 1)
                self.assertEqual(performance.analyze.call_count, 1)
                self.assertEqual(decisions.list.call_count, 1)
                self.assertEqual(sentiment.analyze.call_count, 1)
                self.assertEqual(events.search.call_count, 1)
                self.assertEqual(interpreter.attempt_count, 2)

                # 已完成 Job 再次投递时直接读报告，也不会重复调用模型。
                cached = service_2.run(
                    run_id="durable-run-1",
                    user_id="chen",
                    portfolio_id=PORTFOLIO_ID,
                    months=12,
                )
                self.assertEqual(cached["run_id"], report["run_id"])
                self.assertEqual(interpreter.attempt_count, 2)
            finally:
                runtime_2.close()
                snapshot_database_2.engine.dispose()


class PortfolioResearchJobTests(unittest.TestCase):
    def setUp(self) -> None:
        install_research_authorization(self, PORTFOLIO_ID)
        self.jobs = BackgroundJobService()

    def tearDown(self) -> None:
        self.jobs.shutdown(wait=True)

    def wait_for_terminal(self, job_id: str) -> dict:
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            job = self.jobs.get_job(user_id="chen", job_id=job_id)["job"]
            if job["status"] in {"completed", "failed", "cancelled"}:
                return job
            time.sleep(0.01)
        self.fail("组合研究 Job 未在测试时限内结束")

    def test_background_job_executes_graph_as_eight_steps(self) -> None:
        graph = build_graph_service()
        from app.services import portfolio_research_graph as graph_module

        with patch.object(graph_module, "portfolio_research_graph_service", graph):
            started = self.jobs.start_job(
                user_id="chen",
                job_id="portfolio-research-job",
                title="组合研究测试",
                job_type="portfolio_research",
                portfolio_id=PORTFOLIO_ID,
                portfolio_months=12,
            )
            completed = self.wait_for_terminal(started["job"]["id"])

        self.assertEqual(started["job"]["total_steps"], 8)
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(completed["current_step"], 8)
        self.assertEqual(completed["result"]["thread_id"], "portfolio-research-job")


class PortfolioResearchApiTests(unittest.TestCase):
    def setUp(self):
        install_research_authorization(self, PORTFOLIO_ID)

    def test_start_endpoint_returns_trackable_background_job(self) -> None:
        jobs = BackgroundJobService()
        graph = build_graph_service()
        client = TestClient(app)
        from app.routers import portfolios as portfolio_router
        from app.services import portfolio_research_graph as graph_module

        try:
            with (
                patch.object(
                    portfolio_router,
                    "portfolio_service",
                    FakePortfolioService(),
                ),
                patch.object(portfolio_router, "background_job_service", jobs),
                patch.object(
                    graph_module,
                    "portfolio_research_graph_service",
                    graph,
                ),
            ):
                response = client.post(
                    f"/api/v1/portfolios/{PORTFOLIO_ID}/research-runs",
                    json={"user_id": "chen", "months": 12},
                )
                self.assertEqual(response.status_code, 202)
                payload = response.json()
                completed = self._wait_for_terminal(jobs, payload["job"]["id"])
        finally:
            jobs.shutdown(wait=True)

        self.assertEqual(payload["job"]["job_type"], "portfolio_research")
        self.assertEqual(completed["result"]["workflow"], "portfolio_research")

    @staticmethod
    def _wait_for_terminal(jobs: BackgroundJobService, job_id: str) -> dict:
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            job = jobs.get_job(user_id="chen", job_id=job_id)["job"]
            if job["status"] in {"completed", "failed", "cancelled"}:
                return job
            time.sleep(0.01)
        raise AssertionError("API 创建的组合研究 Job 未在测试时限内结束")


if __name__ == "__main__":
    unittest.main()
