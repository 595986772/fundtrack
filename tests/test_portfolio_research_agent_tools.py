"""产品 Agent 调度组合深度研究的确定性测试。

这些测试使用 Fake Portfolio/Job 服务，不会调用 DeepSeek、行情网络或真实 Worker。
"""

from __future__ import annotations

import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from app.schemas import AgentChatRequest, AgentToolCall
from app.services.agent_execution_context import AgentExecutionContext
from app.services.agent_routing import agent_request_router
from app.services.agent_tool_executor import agent_tool_executor
from app.services.agent_tools import get_product_agent_tools
from app.services.comprehensive_agent import ComprehensiveAgentService
from app.services.portfolio_agent_tools import (
    get_portfolio_research_status_tool,
    start_portfolio_research_tool,
)
from app.services.tool_permissions import check_tool_permission


PORTFOLIO_ID = "portfolio_0123456789abcdef"
JOB_ID = "job-research-agent"


def fake_report() -> dict:
    """构造完整报告外形，用来验证 Tool 只返回页面真正需要的摘要。"""

    return {
        "data_kind": "langgraph_portfolio_research",
        "run_id": JOB_ID,
        "portfolio_id": PORTFOLIO_ID,
        "portfolio_name": "长期组合",
        "period_months": 12,
        "assessment_date": "2026-08-31",
        "headline": "组合风险与支撑暂时均衡",
        "summary": "八阶段研究已经完成。",
        "research_snapshot": {
            "snapshot_id": "research_snapshot_0123456789abcdef",
            "schema_version": "portfolio-research-input-v1",
            "graph_version": "portfolio-research-v6",
            "content_hash": "a" * 64,
            "capture_started_at": "2026-09-01T00:00:00+00:00",
            "captured_at": "2026-09-01T00:00:01+00:00",
            "immutable": True,
            "source_manifest": {
                "source_dates": {"market_data_date": "2026-09-01"},
                "record_counts": {"relevant_events": 2},
                "versions": {"graph": "portfolio-research-v6"},
                "section_hashes": {"portfolio": "b" * 64},
            },
        },
        "assessment_snapshot": {
            "state_code": "balanced",
            "state_label": "风险收益均衡",
            "confidence": "medium",
        },
        "quant_risk_snapshot": {
            "period_start": "2025-08-31",
            "period_end": "2026-08-31",
            "annualized_volatility_pct": 9.8,
            "daily_cvar_95_pct": 1.2,
            "max_drawdown": {"drawdown_pct": -5.4},
            "top_risk_contributor": {"fund_code": "110020"},
            "large_internal_array": list(range(100)),
        },
        "performance_snapshot": {
            "data_kind": "actual_portfolio_performance",
            "performance_label": "真实持仓业绩",
            "position_mode": "actual",
            "period_start": "2025-08-31",
            "period_end": "2026-08-31",
            "twr_pct": 6.2,
            "xirr_pct": 5.9,
            "annualized_return_pct": 6.4,
            "benchmark": {"excess_return_pct": 1.3},
            "max_drawdown": {"drawdown_pct": -5.4},
        },
        "market_snapshot": {
            "data_date": "2026-08-31",
            "state_code": "neutral",
            "state_label": "中性",
            "summary": "市场风险偏好保持中性。",
            "relevant_event_count": 2,
            "events": [{"body": "不应进入 Tool 摘要"}],
        },
        "decision_history": {
            "decision_count": 1,
            "has_decisions": True,
            "note": "有一条可复盘记录。",
            "decisions": [{"decision_id": "decision_test", "action": "hold"}],
        },
        "key_findings": [
            {
                "tone": "caution",
                "title": "注意波动",
                "detail": "权益仓位贡献主要波动。",
                "evidence_ids": ["risk:test"],
                "metric_references": [{"large": "omitted"}],
            }
        ],
        "generation": {"model_used": True, "model": "fake-model"},
        "graph_trace": {"completed_nodes": ["compose_report"]},
        "model_interpretation": {"large": "not copied"},
        "disclaimer": "仅用于研究。",
    }


class FakePortfolioService:
    def get(self, *, user_id: str, portfolio_id: str) -> dict:
        assert user_id == "chen"
        assert portfolio_id == PORTFOLIO_ID
        return {"id": portfolio_id, "name": "长期组合"}


class FakeBackgroundJobService:
    def __init__(self) -> None:
        self.start_arguments = None

    def start_job(self, **kwargs) -> dict:
        self.start_arguments = kwargs
        return {
            "ok": True,
            "accepted": True,
            "message": "已提交",
            "job": self._job(status="queued", result=None),
        }

    def get_job(self, *, user_id: str, job_id: str) -> dict:
        assert user_id == "chen"
        assert job_id == JOB_ID
        return {"ok": True, "job": self._job(status="completed", result=fake_report())}

    @staticmethod
    def _job(*, status: str, result: dict | None) -> dict:
        return {
            "id": JOB_ID,
            "job_type": "portfolio_research",
            "title": "长期组合组合研究",
            "status": status,
            "current_step": 8 if status == "completed" else 0,
            "total_steps": 8,
            "progress_percent": 100.0 if status == "completed" else 0.0,
            "progress_message": "研究完成" if status == "completed" else "等待 Worker",
            "result": result,
            "error": None,
            "created_at": "2026-09-01T00:00:00+00:00",
            "updated_at": "2026-09-01T00:00:01+00:00",
            "completed_at": (
                "2026-09-01T00:00:01+00:00" if status == "completed" else None
            ),
        }


class PortfolioResearchAgentToolTests(unittest.TestCase):
    def test_start_and_status_tools_reuse_existing_background_workflow(self) -> None:
        from app.services import portfolio_agent_tools as tool_module

        jobs = FakeBackgroundJobService()
        with (
            patch.object(tool_module, "portfolio_service", FakePortfolioService()),
            patch.object(tool_module, "background_job_service", jobs),
        ):
            started = json.loads(
                start_portfolio_research_tool.invoke(
                    {
                        "user_id": "chen",
                        "portfolio_id": PORTFOLIO_ID,
                        "months": 12,
                        "idempotency_key": "portfolio-research:chat-key-123",
                    }
                )
            )
            completed = json.loads(
                get_portfolio_research_status_tool.invoke(
                    {
                        "user_id": "chen",
                        "portfolio_id": PORTFOLIO_ID,
                        "job_id": JOB_ID,
                    }
                )
            )

        self.assertEqual(jobs.start_arguments["job_type"], "portfolio_research")
        self.assertEqual(jobs.start_arguments["portfolio_months"], 12)
        self.assertEqual(started["job"]["id"], JOB_ID)
        self.assertEqual(started["events_endpoint"], f"/background-jobs/{JOB_ID}/events")
        self.assertEqual(completed["report"]["performance_snapshot"]["twr_pct"], 6.2)
        self.assertTrue(completed["report"]["research_snapshot"]["immutable"])
        self.assertNotIn(
            "section_hashes",
            completed["report"]["research_snapshot"]["source_manifest"],
        )
        self.assertNotIn(
            "large_internal_array",
            completed["report"]["quant_risk_snapshot"],
        )
        self.assertNotIn("events", completed["report"]["market_snapshot"])

    def test_product_permission_and_router_expose_only_specialized_job_tool(self) -> None:
        product_names = {tool.name for tool in get_product_agent_tools()}
        self.assertIn("start_portfolio_research_tool", product_names)
        self.assertIn("get_portfolio_research_status_tool", product_names)
        self.assertNotIn("background_job_start_tool", product_names)

        context = AgentExecutionContext.product(user_id="chen")
        permission = check_tool_permission(
            "start_portfolio_research_tool",
            {
                "user_id": "chen",
                "portfolio_id": PORTFOLIO_ID,
                "months": 12,
                "idempotency_key": "portfolio-research:chat-key-123",
            },
            execution_context=context,
        )
        self.assertTrue(permission.allowed)

        start_route = agent_request_router.route(
            AgentChatRequest(
                user_id="chen",
                portfolio_id=PORTFOLIO_ID,
                message="请运行组合深度研究",
            )
        )
        status_route = agent_request_router.route(
            AgentChatRequest(
                user_id="chen",
                portfolio_id=PORTFOLIO_ID,
                message=f"查询组合研究进度，job_id 是 {JOB_ID}",
            )
        )
        missing_context = agent_request_router.route(
            AgentChatRequest(user_id="chen", message="请运行组合深度研究")
        )

        self.assertEqual(start_route.tool_names, ("start_portfolio_research_tool",))
        self.assertEqual(
            status_route.tool_names,
            ("get_portfolio_research_status_tool",),
        )
        self.assertEqual(missing_context.name, "clarify")

    def test_message_metadata_keeps_job_reference_and_bounded_report(self) -> None:
        tool_payload = {
            "ok": True,
            "data_kind": "portfolio_research_job",
            "portfolio_id": PORTFOLIO_ID,
            "portfolio_name": "长期组合",
            "period_months": 12,
            "events_endpoint": f"/background-jobs/{JOB_ID}/events",
            "job": FakeBackgroundJobService._job(
                status="completed",
                result=fake_report(),
            ),
            "report": {"headline": "组合风险与支撑暂时均衡"},
        }
        run = SimpleNamespace(
            tool_calls=[
                AgentToolCall(
                    tool_name="get_portfolio_research_status_tool",
                    tool_args={"job_id": JOB_ID},
                    tool_result=json.dumps(tool_payload, ensure_ascii=False),
                    is_error=False,
                )
            ]
        )

        jobs = ComprehensiveAgentService._portfolio_research_jobs(run)

        self.assertEqual(jobs[0]["id"], JOB_ID)
        self.assertEqual(jobs[0]["portfolio_id"], PORTFOLIO_ID)
        self.assertEqual(jobs[0]["report"]["headline"], "组合风险与支撑暂时均衡")
        self.assertNotIn("result", jobs[0])


class FakeToolMessage:
    def __init__(self, *, content: str, tool_call_id: str) -> None:
        self.content = content
        self.tool_call_id = tool_call_id


class RecordingResearchTool:
    def __init__(self) -> None:
        self.arguments = None

    async def ainvoke(self, arguments):
        self.arguments = arguments
        return json.dumps({"ok": True, "job": {"id": JOB_ID}})


class PortfolioResearchTrustedArgumentTests(unittest.IsolatedAsyncioTestCase):
    async def test_executor_overrides_model_identity_portfolio_and_idempotency(self) -> None:
        tool = RecordingResearchTool()
        request = AgentChatRequest(
            user_id="chen",
            portfolio_id=PORTFOLIO_ID,
            idempotency_key="chat-key-123",
            message="运行组合深度研究",
        )

        executed, _, _ = await agent_tool_executor.execute(
            tool_call={
                "id": "research-start-1",
                "name": "start_portfolio_research_tool",
                "args": {
                    "user_id": "other-user",
                    "portfolio_id": "portfolio_ffffffffffffffff",
                    "months": 12,
                    "idempotency_key": "model-selected-key",
                },
            },
            tool_map={"start_portfolio_research_tool": tool},
            request=request,
            execution_context=AgentExecutionContext.product(user_id="chen"),
            tool_index=1,
            ToolMessage=FakeToolMessage,
        )

        self.assertFalse(executed.is_error)
        self.assertEqual(tool.arguments["user_id"], "chen")
        self.assertEqual(tool.arguments["portfolio_id"], PORTFOLIO_ID)
        self.assertEqual(
            tool.arguments["idempotency_key"],
            "portfolio-research:chat-key-123",
        )


if __name__ == "__main__":
    unittest.main()
