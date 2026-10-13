"""补查闭环回归：用 Fake 公开源和临时 SQLite，不访问真实模型/数据源。"""

from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import asdict, replace
from datetime import date
import json
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from app.database import Database
from app.schemas import AgentChatRequest, AgentToolCall
from app.services.agent_routing import AgentRequestRouter
from app.services.agent_chat_runtime import AgentChatCoordinator, AgentChatExecutionStore
from app.services.comprehensive_agent import ComprehensiveAgentService
from app.services.conversations import ConversationService
from app.services.eval_context import use_frozen_eval_data
from app.services.financial_research_agent_tools import get_fund_research_brief_tool
from app.services.financial_research_repair import (
    FinancialResearchRepairService, ResearchRepairBudget, research_repair_scope,
    current_repair_budget, _refresh_disclosure,
)
from app.services.fund_disclosure_fetch import fetch_disclosure_bounded
from app.services.fund_disclosures import (
    FundDisclosureBatch, FundSecurityHolding, FundDisclosureRepository,
)
from app.services.investment_context import InvestmentContext, investment_context_scope
from app.services.tool_agent import ToolCallingAgentService
from tests.test_financial_research_brief import (
    build_service, FakeDisclosures, FakeAnalyze, exposure, PORTFOLIO_ID,
)


class MutableDisclosures:
    """公开抓取前是旧季报；测试中的 refresh 才把它变成新季报。"""

    def __init__(self):
        self.fresh = set()

    def list_for_funds(self, fund_codes):
        result = {}
        for code in fund_codes:
            data = FakeDisclosures().list_for_funds([code])[code]
            if code not in self.fresh:
                data.update(freshness_status="stale", latest_report_date="2024-12-31")
                for row in data["security_holdings"]:
                    row["report_date"] = "2024-12-31"
            result[code] = data
        return result


def setup_service():
    service = build_service()
    disclosures = MutableDisclosures()
    calls = []

    def refresh(code, budget):
        calls.append(code)
        disclosures.fresh.add(code)
        return {"status": "completed", "detail": "Fake 源已更新"}

    service.fund_disclosures = disclosures
    service.repair = FinancialResearchRepairService(refresh)
    return service, disclosures, calls


class ResearchRepairTests(unittest.TestCase):
    def test_default_never_refreshes_and_separates_user_gaps(self):
        service, _, calls = setup_service()
        with investment_context_scope(InvestmentContext()):
            result = service.build_fund(fund_code="012345", question="这只基金适合我吗")
        report = result["evidence_repair"]
        self.assertEqual(calls, [])
        self.assertEqual(report["used_rounds"], 0)
        self.assertEqual(report["stop_reason"], "manual_refresh_required")
        kinds = {item["key"]: item["kind"] for item in report["remaining_gaps"]}
        self.assertEqual(kinds["investor_context"], "user_information")
        self.assertEqual(kinds["holdings_exposure"], "public_data")

    def test_authorized_refresh_rechecks_gate_and_retains_other_facts(self):
        service, _, calls = setup_service()
        service.fund_risk.analyze = Mock(wraps=service.fund_risk.analyze)
        with research_repair_scope(allow_public_refresh=True):
            result = service.build_fund(fund_code="012345", question="全面分析这只基金")
        report = result["evidence_repair"]
        self.assertEqual(calls, ["012345"])
        self.assertEqual(report["attempts"][0]["status"], "evidence_changed")
        self.assertGreater(report["final_gate"]["coverage_score_pct"], report["initial_gate"]["coverage_score_pct"])
        self.assertEqual(result["evidence_summary"]["disclosure"]["latest_report_date"], "2026-06-30")
        service.fund_risk.analyze.assert_called_once()

    def test_same_stale_report_is_not_claimed_updated_or_repeated(self):
        service, _, _ = setup_service()
        refresh = Mock(return_value={"status": "completed"})
        service.repair = FinancialResearchRepairService(refresh)
        with research_repair_scope(allow_public_refresh=True):
            first = service.build_fund(fund_code="012345", question="全面分析")
            second = service.build_fund(fund_code="012345", question="全面分析")
        refresh.assert_called_once()
        self.assertEqual(first["evidence_repair"]["attempts"][0]["status"], "checked_no_change")
        self.assertEqual(second["evidence_repair"]["attempts"], [])
        self.assertTrue(first["evidence_repair"]["remaining_gaps"])

    def test_network_failure_preserves_original_evidence(self):
        service, _, _ = setup_service()
        service.repair = FinancialResearchRepairService(Mock(side_effect=TimeoutError("测试超时")))
        with research_repair_scope(allow_public_refresh=True):
            result = service.build_fund(fund_code="012345", question="全面分析")
        self.assertEqual(result["evidence_repair"]["attempts"][0]["status"], "failed")
        self.assertEqual(result["evidence_summary"]["disclosure"]["latest_report_date"], "2024-12-31")
        self.assertEqual(result["evidence_summary"]["historical_risk"]["annualized_volatility_pct"], 22.1)

    def test_local_retry_only_reruns_failed_branch(self):
        service = build_service()
        analyze = service.fund_risk.analyze
        service.fund_risk.analyze = Mock(side_effect=[RuntimeError("暂时失败"), analyze()])
        service.fund_assessment.assess = Mock(wraps=service.fund_assessment.assess)
        result = service.build_fund(fund_code="012345", question="全面分析")
        self.assertEqual(service.fund_risk.analyze.call_count, 2)
        service.fund_assessment.assess.assert_called_once()
        self.assertEqual(result["execution"]["failed_count"], 0)
        self.assertEqual(result["evidence_repair"]["attempts"][0]["action"], "local_reread")

    def test_zero_events_and_user_only_gap_do_not_cause_refresh(self):
        service, disclosures, calls = setup_service()
        disclosures.fresh.add("012345")
        service.fund_assessment.payload["active_event_count"] = 0
        with research_repair_scope(allow_public_refresh=True), investment_context_scope(InvestmentContext()):
            result = service.build_fund(fund_code="012345", question="适合我吗")
        self.assertEqual(calls, [])
        self.assertEqual([gap["kind"] for gap in result["evidence_repair"]["remaining_gaps"]], ["user_information"])

    def test_candidate_selection_does_not_read_failed_disclosure_again(self):
        service, _, _ = setup_service()
        service.fund_disclosures.list_for_funds = Mock(side_effect=RuntimeError("披露数据库异常"))
        service.repair = FinancialResearchRepairService(Mock(side_effect=TimeoutError("公开源超时")))
        with research_repair_scope(allow_public_refresh=True):
            result = service.build_fund(fund_code="012345", question="全面分析")
        self.assertEqual(service.fund_disclosures.list_for_funds.call_count, 2)  # 初读 + 一次有记录重读
        self.assertEqual(result["evidence_repair"]["used_rounds"], 2)
        self.assertEqual(len(result["evidence_repair"]["attempts"]), 2)

    def test_portfolio_budget_prioritizes_high_weight_and_checks_ownership(self):
        service, _, calls = setup_service()
        original = service.portfolios.get(user_id="chen", portfolio_id=PORTFOLIO_ID)
        original["holdings"] = [
            {**original["holdings"][0], "fund_code": code, "weight_pct": weight}
            for code, weight in [("012345", 10), ("023456", 60), ("034567", 30)]
        ]
        original_get = service.portfolios.get
        service.portfolios.get = lambda **kw: original if kw["user_id"] == "chen" else original_get(**kw)
        data = exposure()
        data["data_quality"]["freshness_status"] = "stale"
        service.portfolio_exposure = FakeAnalyze(data)
        with research_repair_scope(allow_public_refresh=True):
            result = service.build_portfolio(user_id="chen", portfolio_id=PORTFOLIO_ID, question="全面分析")
        self.assertEqual(calls, ["023456", "034567"])
        self.assertEqual(result["evidence_repair"]["used_rounds"], 2)
        self.assertEqual(result["evidence_repair"]["stop_reason"], "budget_exhausted")
        self.assertEqual(result["evidence_repair"]["unattempted_fund_codes"], ["012345"])
        with research_repair_scope(allow_public_refresh=True), self.assertRaises(RuntimeError):
            service.build_portfolio(user_id="other", portfolio_id=PORTFOLIO_ID, question="全面分析")
        self.assertEqual(len(calls), 2)

    def test_eval_data_overrides_refresh_authorization(self):
        service, _, calls = setup_service()
        with research_repair_scope(allow_public_refresh=True), use_frozen_eval_data():
            result = service.build_fund(fund_code="012345", question="全面分析")
        self.assertEqual(calls, [])
        self.assertFalse(result["evidence_repair"]["public_refresh_enabled"])

    def test_post_refresh_read_failure_keeps_prior_facts_and_warning(self):
        service, _, _ = setup_service()
        payload = deepcopy(service.fund_assessment.payload)
        service.fund_assessment.assess = Mock(side_effect=[payload, RuntimeError("重读失败")])
        with research_repair_scope(allow_public_refresh=True):
            result = service.build_fund(fund_code="012345", question="全面分析")
        self.assertTrue(result["evidence_repair"]["warnings"])
        self.assertEqual(result["evidence_summary"]["rule_assessment"]["summary"], payload["summary"])

    def test_report_persists_as_message_metadata(self):
        service, _, _ = setup_service()
        result = service.build_fund(fund_code="012345", question="全面分析")
        calls = [AgentToolCall(tool_name="get_fund_research_brief_tool", tool_args={}, tool_result=json.dumps(result))]
        reports = ComprehensiveAgentService._research_repairs(SimpleNamespace(tool_calls=calls))
        with TemporaryDirectory() as directory:
            database = Database(f"sqlite:///{Path(directory) / 'test.db'}")
            conversations = ConversationService(database)
            conversation = conversations.create(user_id="chen")
            conversations.append_exchange(
                user_id="chen", conversation_id=conversation["id"], user_content="全面分析",
                assistant_content="持仓缺口待补查", run_id="repair-test", run_metadata={"research_repairs": reports},
            )
            saved = ConversationService(database).detail(user_id="chen", conversation_id=conversation["id"])
            self.assertEqual(saved["messages"][-1]["run_metadata"]["research_repairs"], reports)

    def test_authorization_is_not_a_model_argument(self):
        self.assertNotIn("research_refresh", get_fund_research_brief_tool.args)
        self.assertFalse(AgentChatRequest(message="请联网更新").research_refresh)
        router = AgentRequestRouter()
        request = AgentChatRequest(message="补查后分析", fund_code="012345", research_refresh=True)
        self.assertEqual(router.route(request).tool_names, ("get_fund_research_brief_tool",))
        denied = router.route(request.model_copy(update={"message": "读取项目源码"}))
        self.assertEqual(denied.name, "access_denied")


class ResearchBudgetAsyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_failed_request_retry_restores_budget_after_coordinator_restart(self):
        service, _, calls = setup_service()

        class FailingSummaryAgent:
            async def chat(self, request, **kwargs):
                # 两只基金都已尝试，但最终总结失败，尚未产生助手消息。
                service.build_fund(fund_code="012345", question="全面分析")
                service.build_fund(fund_code="023456", question="全面分析")
                raise RuntimeError("模型总结失败")

        with TemporaryDirectory() as directory:
            url = f"sqlite:///{Path(directory) / 'retry.db'}"
            request = AgentChatRequest(message="全面分析", research_refresh=True, idempotency_key="same-request-123")
            for _ in range(2):
                # 重建数据库连接和协调器，证明预算不是仅保存在 Python 内存。
                coordinator = AgentChatCoordinator(agent=FailingSummaryAgent(), store=AgentChatExecutionStore(Database(url)))
                with self.assertRaisesRegex(RuntimeError, "模型总结失败"):
                    await coordinator.chat(request)
                service.fund_disclosures.fresh.clear()
            self.assertEqual(calls, ["012345", "023456"])
            saved = coordinator.store.research_repair_state(user_id="guest", idempotency_key=request.idempotency_key)
            self.assertEqual(saved["used_rounds"], 2)

    async def test_core_agent_owns_request_scoped_budget(self):
        service = ToolCallingAgentService()

        async def inspect(request, **kwargs):
            budget = current_repair_budget()
            await asyncio.sleep(0)
            return budget.allow_public_refresh

        with patch.object(service, "_chat", side_effect=inspect):
            result = await asyncio.gather(
                service.chat(AgentChatRequest(message="全面分析", research_refresh=True)),
                service.chat(AgentChatRequest(message="全面分析")),
            )
        self.assertEqual(result, [True, False])
        self.assertIsNone(current_repair_budget())

    async def test_sync_langchain_tool_inherits_authorization(self):
        service, _, calls = setup_service()
        with patch("app.services.financial_research_agent_tools.financial_research_brief_service", service):
            with research_repair_scope(allow_public_refresh=True):
                await get_fund_research_brief_tool.ainvoke({"fund_code": "012345", "question": "全面分析"})
        self.assertEqual(calls, ["012345"])

    async def test_parallel_budget_reservation_and_cancel(self):
        with research_repair_scope(allow_public_refresh=True) as budget:
            reservations = await asyncio.gather(*(asyncio.to_thread(budget.reserve, f"disclosure:{i}") for i in range(8)))
            self.assertEqual(sum(reservations), 2)
        self.assertTrue(budget.cancelled)
        self.assertFalse(budget.reserve("new"))


def sample_batch():
    return FundDisclosureBatch(
        fund_code="012345", security_holdings=(FundSecurityHolding(
            fund_code="012345", report_date=date(2026, 6, 30), asset_type="stock",
            security_code="688981", security_name="测试证券", weight_pct=8,
            market_value_10k=None, rank=1, source_url="https://example.test/disclosure",
        ),), industry_allocations=(), stock_report_date=date(2026, 6, 30),
        bond_report_date=None, industry_report_date=None,
    )


class BoundedDisclosureFetchTests(unittest.TestCase):
    def test_partially_empty_asset_does_not_erase_existing_rows(self):
        with TemporaryDirectory() as directory:
            repository = FundDisclosureRepository(Database(f"sqlite:///{Path(directory) / 'partial.db'}"))
            with patch("app.services.fund_disclosures.fund_disclosure_repository", repository), patch(
                "app.services.fund_disclosure_fetch.fetch_disclosure_bounded", return_value=sample_batch(),
            ) as fetch:
                _refresh_disclosure("012345", ResearchRepairBudget())
                # 股票有日期却没有有效明细，不能因为债券有明细就允许覆盖整批数据。
                invalid = replace(sample_batch(), security_holdings=(replace(
                    sample_batch().security_holdings[0], asset_type="bond", security_code="123456",
                ),), bond_report_date=date(2026, 6, 30))
                fetch.return_value = invalid
                self.assertEqual(_refresh_disclosure("012345", ResearchRepairBudget())["status"], "no_update")
                rows = repository.latest(fund_code="012345")["security_holdings"]
                self.assertEqual([row["security_code"] for row in rows], ["688981"])

    def test_timeout_is_explicit_and_command_is_fixed(self):
        with patch("app.services.fund_disclosure_fetch.subprocess.run", side_effect=subprocess.TimeoutExpired("fetch", 25)) as run:
            with self.assertRaisesRegex(TimeoutError, "已停止"):
                fetch_disclosure_bounded("012345", timeout_seconds=25)
        self.assertEqual(run.call_args.kwargs["timeout"], 25)
        self.assertFalse(run.call_args.kwargs.get("shell", False))

    def test_json_roundtrip_and_bad_code(self):
        payload = json.dumps(asdict(sample_batch()), default=lambda item: item.isoformat())
        with patch("app.services.fund_disclosure_fetch.subprocess.run", return_value=SimpleNamespace(returncode=0, stdout=payload)):
            self.assertEqual(fetch_disclosure_bounded("012345", timeout_seconds=25), sample_batch())
            with self.assertRaises(ValueError):
                fetch_disclosure_bounded("023456", timeout_seconds=25)
            with self.assertRaises(ValueError):
                fetch_disclosure_bounded("012345;rm", timeout_seconds=25)

    def test_success_writes_sql_and_cancel_never_writes(self):
        with TemporaryDirectory() as directory:
            repository = FundDisclosureRepository(Database(f"sqlite:///{Path(directory) / 'disclosures.db'}"))
            with patch("app.services.fund_disclosures.fund_disclosure_repository", repository), patch(
                "app.services.fund_disclosure_fetch.fetch_disclosure_bounded", return_value=sample_batch(),
            ):
                cancelled = ResearchRepairBudget(cancelled=True)
                self.assertEqual(_refresh_disclosure("012345", cancelled)["status"], "cancelled")
                self.assertIsNone(repository.latest(fund_code="012345"))
                self.assertEqual(_refresh_disclosure("012345", ResearchRepairBudget())["status"], "completed")
                self.assertEqual(repository.latest(fund_code="012345")["report_dates"]["stock"], "2026-06-30")
