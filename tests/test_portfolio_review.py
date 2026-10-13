"""持仓到报告追问的隔离测试：临时数据库、假 Agent，不采集或调用付费模型。"""

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient
from app.main import app
from app.database import Database
from app.schemas import AgentChatRequest
from app.services.comprehensive_agent import ComprehensiveAgentService
from app.services.conversations import ConversationService
from app.services.observability import AgentObservabilityService
from app.services.portfolio_review import PortfolioReviewService
from app.services.portfolio_performance import PortfolioPerformanceError
from app.services.portfolios import PortfolioNotFoundError
from app.services.portfolio_report_context import resolve_report_context
from app.services.portfolio_research_audit import PortfolioResearchAuditNotFoundError
from tests.test_conversations import FakeAgent, FakeRuntimeState

PID = "portfolio_1234567890abcdef"
REPORT = {"run_id": "research-test", "portfolio_id": PID, "portfolio_name": "测试账本",
          "assessment_date": "2026-09-01", "headline": "需要注意集中风险",
          "research_snapshot": {"snapshot_id": "frozen-test", "immutable": True},
          "key_findings": [{"title": "集中风险", "evidence_ids": ["risk:concentration"],
                            "metric_references": [{"key": "concentration.max_weight_pct", "value_text": "40.00%", "data_date": "2026-09-01"}]}]}


class PortfolioReviewTests(unittest.TestCase):
    def make_service(self):
        service = PortfolioReviewService(Mock(), Mock(), Mock(), Mock())
        service.portfolios.get.return_value = {"id": PID}
        service.performance.analyze.return_value = {"twr_pct": 0, "daily_points": ["large matrix"]}
        service.assessment.assess.return_value = {"state_label": "测试状态", "risk_evidence": [{}] * 5}
        service.research.list_runs.return_value = {"runs": [{"job_id": "research-test"}]}
        return service

    def test_bounded_read_keeps_zero_and_separate_partial_failures(self):
        service = self.make_service()
        result = service.read(user_id="chen", portfolio_id=PID)
        self.assertEqual(result["performance"]["twr_pct"], 0)
        self.assertNotIn("daily_points", result["performance"])
        self.assertEqual(len(result["assessment"]["risk_evidence"]), 3)
        service.performance.analyze.side_effect = PortfolioPerformanceError("净值不足")
        result = service.read(user_id="chen", portfolio_id=PID, months=6)
        self.assertIsNone(result["performance"])
        self.assertEqual(result["assessment"]["state_label"], "测试状态")
        self.assertEqual(result["gaps"], [{"section": "performance", "message": "净值不足"}])
        service.research.list_runs.assert_called_with(user_id="chen", portfolio_id=PID, months=6, limit=1)

    def test_ownership_is_checked_before_any_calculation(self):
        service = self.make_service()
        service.portfolios.get.side_effect = PortfolioNotFoundError("不存在")
        with self.assertRaises(PortfolioNotFoundError):
            service.read(user_id="other", portfolio_id=PID)
        service.performance.analyze.assert_not_called()
        service.assessment.assess.assert_not_called()
        service.research.list_runs.assert_not_called()

    def test_summary_http_period_and_error_contract(self):
        with patch("app.routers.portfolios.portfolio_review_service", self.make_service()):
            client = TestClient(app)
            self.assertEqual(client.get(f"/api/v1/portfolios/{PID}/review?user_id=chen&months=6").status_code, 200)
            self.assertEqual(client.get(f"/api/v1/portfolios/{PID}/review?user_id=chen&months=99").status_code, 422)

    def test_report_explicit_inherited_and_cleared_references(self):
        reader = Mock(); reader.read_report.return_value = REPORT
        request = AgentChatRequest(user_id="chen", message="解释证据", portfolio_id=PID, research_job_id="research-test")
        ref, context = resolve_report_context(request, {}, reader)
        self.assertIn("risk:concentration", context["content"])
        self.assertIn("40.00%", context["content"])
        self.assertIn("历史", context["content"])
        previous = {"research_reference": ref}
        inherited, _ = resolve_report_context(request.model_copy(update={"research_job_id": None}), previous, reader)
        self.assertEqual(inherited["job_id"], "research-test")
        reader.reset_mock()
        for updates in ({"research_job_id": ""}, {"research_job_id": None, "portfolio_id": "portfolio_abcdef1234567890"},
                        {"research_job_id": None, "fund_code": "110020", "portfolio_id": None}):
            self.assertEqual(resolve_report_context(request.model_copy(update=updates), previous, reader), (None, None))
        reader.read_report.assert_not_called()


class ReportConversationTests(unittest.IsolatedAsyncioTestCase):
    async def test_two_turns_reload_verified_report_without_duplicate_visible_messages(self):
        with TemporaryDirectory() as directory:
            database = Database(f"sqlite:///{Path(directory) / 'chat.db'}")
            agent = FakeAgent()
            conversations = ConversationService(database)
            service = ComprehensiveAgentService(conversations=conversations, agent=agent,
                runtime_state=FakeRuntimeState(), observability=AgentObservabilityService(database))
            with patch("app.services.portfolio_report_context.portfolio_research_audit_service.read_report", return_value=REPORT) as reader:
                first = await service.chat(AgentChatRequest(user_id="chen", message="解释报告", portfolio_id=PID, research_job_id="research-test"))
                second = await service.chat(AgentChatRequest(user_id="chen", message="最强反对理由呢", conversation_id=first.conversation.id))
                self.assertEqual(reader.call_count, 2)
                self.assertEqual(second.assistant_message.run_metadata["research_reference"]["job_id"], "research-test")
                self.assertIn("risk:concentration", agent.histories[1][-1]["content"])
                messages = conversations.detail(user_id="chen", conversation_id=first.conversation.id)["messages"]
                self.assertEqual(len(messages), 4)
                self.assertEqual(messages[0]["content"], "解释报告")
                cleared = await service.chat(AgentChatRequest(user_id="chen", message="换个话题", conversation_id=first.conversation.id, research_job_id=""))
                self.assertIsNone(cleared.assistant_message.run_metadata["research_reference"])
                self.assertEqual(reader.call_count, 2)
                await service.chat(AgentChatRequest(user_id="chen", message="新对话"))
                self.assertEqual(agent.histories[-1], [])
                reader.side_effect = PortfolioResearchAuditNotFoundError("报告不存在")
                with self.assertRaises(PortfolioResearchAuditNotFoundError):
                    await service.chat(AgentChatRequest(user_id="chen", message="越权报告", portfolio_id=PID, research_job_id="other-job"))
                self.assertEqual(agent.call_count, 4)
            database.engine.dispose()
