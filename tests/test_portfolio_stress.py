"""用人工行情核对数学，再用临时数据库验证隔离、复算和 API；不调用模型或外网。"""
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
import json
import asyncio
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import numpy as np
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.database import Database
from app.models import PortfolioStressRunModel
from app.schemas import PortfolioStressRequest, AgentChatRequest
from app.services.portfolio_stress import (
    PortfolioStressService, PortfolioStressError, PortfolioStressNotFound,
    replay_graph, select_window,
)
from app.services.portfolio_stress_tools import summarize_report, run_portfolio_stress_tool, get_portfolio_stress_tool
from app.services.agent_routing import AgentRequestRouter
from app.services.agent_tool_executor import AgentToolExecutor
from app.services.agent_execution_context import AgentExecutionContext
from app.services.tool_permissions import check_tool_permission
from app.services.comprehensive_agent import ComprehensiveAgentService

PORTFOLIO = "portfolio_0123456789abcdef"
CODES = ["110020", "110007"]


def inputs():
    # 唯一的连续亏损区间：原配置60/40；债券每天小幅正收益，便于手算比较。
    dates = [(date(2026, 1, 1) + timedelta(days=i)).isoformat() for i in range(81)]
    return {
        "portfolio_id": PORTFOLIO, "portfolio_name": "确定性测试组合", "months": 12,
        "fund_codes": CODES, "short_names": dict(zip(CODES, ["权益样本", "债券样本"])),
        "baseline_weights": dict(zip(CODES, [60, 40])), "window_days": 20,
        "dates": dates[1:], "start_dates": dates[:-1], "eligible": [True] * 80,
        "daily_returns": [[-.02 if 25 <= i < 45 else .003, .001] for i in range(80)],
        "data_sources": [{"fund_code": code, "return_basis": "test"} for code in CODES],
        "calendar_note": "人工样本仅用于测试",
    }


class StressMathTests(unittest.TestCase):
    def test_select_same_window_and_compound_contributions(self):
        frozen = inputs()
        result = replay_graph().invoke({"frozen": frozen, "target_weights": dict(zip(CODES, [20, 80]))})
        self.assertEqual(result["window"]["start_index"], 25)
        baseline, candidate = result["scenarios"]
        self.assertAlmostEqual(baseline["period_return_pct"], ((1 - .02 * .6 + .001 * .4) ** 20 - 1) * 100, places=5)
        self.assertGreater(candidate["period_return_pct"], baseline["period_return_pct"])
        self.assertEqual(len(baseline["curve"]), 21)
        self.assertEqual(baseline["curve"][0]["index"], 100)
        for scenario in result["scenarios"]:
            self.assertAlmostEqual(sum(c["return_contribution_pp"] for c in scenario["contributions"]), scenario["period_return_pct"], places=5)
            self.assertAlmostEqual(scenario["max_drawdown_pct"], -scenario["period_return_pct"], places=5)

    def test_positive_samples_never_fabricate_losses(self):
        frozen = inputs(); frozen["daily_returns"] = [[.001, .001]] * 80
        result = replay_graph().invoke({"frozen": frozen})
        self.assertGreater(result["scenarios"][0]["period_return_pct"], 0)
        self.assertEqual(result["scenarios"][0]["max_drawdown_pct"], 0)
        self.assertEqual(result["window"]["start_index"], 0)

    def test_real_return_adapter_keeps_gap_mask_and_sources(self):
        from tests.test_portfolio_risk_engine import FakePortfolioService, FakeFundCatalog
        from app.services.portfolio_risk_engine import PortfolioRiskEngine
        engine = PortfolioRiskEngine(portfolios=FakePortfolioService(), funds=FakeFundCatalog())
        frozen = engine.capture_stress_inputs(user_id="chen", portfolio_id=PORTFOLIO, months=12)
        self.assertEqual(frozen["eligible"].count(False), 1)
        self.assertEqual(len(frozen["dates"]), len(frozen["daily_returns"]))
        self.assertEqual(len(frozen["data_sources"]), 2)
        frozen["window_days"] = 20
        self.assertEqual(select_window({"frozen": frozen})["window"]["excluded_windows"], 20)

    def test_zero_ties_choose_earliest(self):
        frozen = inputs(); frozen["daily_returns"] = [[0, 0]] * 80
        self.assertEqual(select_window({"frozen": frozen})["window"]["start_index"], 0)

    def test_gap_windows_excluded(self):
        frozen = inputs(); frozen["eligible"][30] = False
        w = select_window({"frozen": frozen})["window"]
        self.assertEqual(w["excluded_windows"], 20)
        self.assertFalse(w["start_index"] <= 30 < w["start_index"] + 20)
        frozen["eligible"] = [False] * 80
        with self.assertRaises(PortfolioStressError): select_window({"frozen": frozen})

    def test_invalid_weights_and_incomplete_samples(self):
        for weights in ({CODES[0]: 100}, dict(zip(CODES, [-1, 101])), dict(zip(CODES, [30, 40]))):
            with self.subTest(weights=weights), self.assertRaises(PortfolioStressError):
                replay_graph().invoke({"frozen": inputs(), "target_weights": weights})
        frozen = inputs(); frozen["daily_returns"] = frozen["daily_returns"][:10]
        with self.assertRaises(PortfolioStressError): select_window({"frozen": frozen})

    def test_request_bounds(self):
        for kwargs in ({"months": 1}, {"window_days": 999}, {"source_run_id": "other"}, {"target_weights": {"110020": float("nan")}}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValidationError): PortfolioStressRequest(**kwargs)


class StressStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Database(f"sqlite:///{Path(self.temp.name) / 'stress.db'}")
        self.db.create_schema()
        self.capture_count = 0
        def capture(**kwargs):
            self.capture_count += 1
            return inputs()
        self.service = PortfolioStressService(self.db, risk_engine=SimpleNamespace(capture_stress_inputs=capture))

    def tearDown(self):
        self.db.engine.dispose(); self.temp.cleanup()

    def run_report(self, **kwargs):
        return self.service.run(portfolio_id=PORTFOLIO, request=PortfolioStressRequest(user_id="chen", **kwargs))

    def test_frozen_compare_survives_data_change_and_restart(self):
        original = self.run_report()
        self.service.risk_engine.capture_stress_inputs = lambda **kwargs: self.fail("比较或复算不能重新读取行情")
        candidate = self.run_report(source_run_id=original["run_id"], target_weights=dict(zip(CODES, [20, 80])))
        self.assertEqual(original["window"], candidate["window"])
        self.assertEqual(original["input_hash"], candidate["input_hash"])
        fresh_service = PortfolioStressService(self.db)
        self.assertTrue(fresh_service.verify(user_id="chen", portfolio_id=PORTFOLIO, run_id=candidate["run_id"])["consistent"])
        self.assertEqual(len(fresh_service.list(user_id="chen", portfolio_id=PORTFOLIO)), 2)
        with self.db.session() as session:
            self.assertIsNone(session.get(PortfolioStressRunModel, ("chen", candidate["run_id"])).frozen_inputs)

    def test_idempotent_retry_and_conflict(self):
        first = self.run_report(idempotency_key="request-1")
        self.assertEqual(first, self.run_report(idempotency_key="request-1"))
        self.assertEqual(self.capture_count, 1)
        with self.assertRaises(PortfolioStressError): self.run_report(idempotency_key="request-1", months=6)

    def test_user_portfolio_isolation_and_integrity(self):
        first = self.run_report()
        for user, portfolio in (("other", PORTFOLIO), ("chen", "portfolio_1111111111111111")):
            with self.assertRaises(PortfolioStressNotFound): self.service.get(user_id=user, portfolio_id=portfolio, run_id=first["run_id"])
        with self.db.session() as session, session.begin():
            row = session.get(PortfolioStressRunModel, ("chen", first["run_id"]))
            value = deepcopy(row.frozen_inputs); value["daily_returns"][0][0] = .99
            row.frozen_inputs = value
        with self.assertRaises(PortfolioStressError): self.service.verify(user_id="chen", portfolio_id=PORTFOLIO, run_id=first["run_id"])

    def test_cannot_change_window_for_comparison(self):
        first = self.run_report()
        with self.assertRaises(PortfolioStressError): self.run_report(source_run_id=first["run_id"], window_days=40)

    def test_concurrent_duplicate_creates_one_report(self):
        from threading import Barrier
        barrier = Barrier(2)
        def capture(**kwargs):
            barrier.wait(timeout=10)
            return inputs()
        self.service.risk_engine.capture_stress_inputs = capture
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(self.run_report, idempotency_key="concurrent") for _ in range(2)]
            values = [future.result(timeout=15) for future in futures]
        self.assertEqual(values[0]["run_id"], values[1]["run_id"])
        self.assertEqual(len(self.service.list(user_id="chen", portfolio_id=PORTFOLIO)), 1)

    def test_structured_compaction_preserves_comparison(self):
        from app.services.context_compactor import ContextCompactorService
        report = self.run_report(target_weights=dict(zip(CODES, [20, 80])))
        payload = summarize_report(report)
        compact = ContextCompactorService()._build_compacted_content(
            original_content=json.dumps(payload), saved_path="test-only.json", reason="test",
            original_chars=9000, message=SimpleNamespace(tool_call_id="test"))
        parsed = json.loads(compact)
        self.assertEqual(parsed["facts"], report["facts"])
        self.assertEqual(parsed["scenarios"][1]["weights"], report["scenarios"][1]["weights"])

    def test_latest_alembic_migration_matches_orm(self):
        # 在独立空库跑完整迁移，绝不对用户数据库降级或清空。
        from alembic import command
        from alembic.config import Config
        from app.config import settings
        from sqlalchemy import create_engine, inspect
        url = f"sqlite:///{Path(self.temp.name) / 'migration.db'}"
        with patch.object(settings, "database_url", url):
            command.upgrade(Config("alembic.ini"), "head")
        engine = create_engine(url)
        try:
            inspector = inspect(engine)
            self.assertEqual({c["name"] for c in inspector.get_columns("portfolio_stress_runs")}, set(PortfolioStressRunModel.__table__.columns.keys()))
            self.assertIn("uq_portfolio_stress_idempotency", {c["name"] for c in inspector.get_unique_constraints("portfolio_stress_runs")})
        finally: engine.dispose()

    def test_tools_and_report_metadata(self):
        with patch("app.services.portfolio_stress_tools.portfolio_stress_service", self.service):
            payload = json.loads(run_portfolio_stress_tool.invoke({"user_id": "chen", "portfolio_id": PORTFOLIO}))
            read = json.loads(get_portfolio_stress_tool.invoke({"user_id": "chen", "portfolio_id": PORTFOLIO, "run_id": payload["run_id"]}))
        self.assertEqual(payload, read)
        self.assertNotIn("curve", payload["scenarios"][0])
        self.assertLess(len(json.dumps(payload, ensure_ascii=False)), 6000)
        run = SimpleNamespace(tool_calls=[SimpleNamespace(tool_name="run_portfolio_stress_tool", is_error=False, tool_result=json.dumps(payload))])
        self.assertEqual(ComprehensiveAgentService._portfolio_stress_runs(run)[0]["run_id"], payload["run_id"])

    def test_actual_agent_loop_executes_and_followup_reuses_frozen_run(self):
        from langchain_core.messages import AIMessage
        from app.services.tool_agent import ToolCallingAgentService
        class ScriptedModel:
            def __init__(self, args): self.args, self.calls = args, 0
            def bind_tools(self, tools): return self
            async def ainvoke(self, messages):
                self.calls += 1
                if self.calls == 1:
                    return AIMessage(content="", tool_calls=[{"name": "run_portfolio_stress_tool", "args": self.args, "id": "stress-call"}])
                payload = json.loads(messages[-1].content)
                return AIMessage(content=f"历史重演结果，不是未来预测。{payload['facts'][0]['evidence_id']}")
        request = AgentChatRequest(user_id="chen", portfolio_id=PORTFOLIO, message="重演过去一年最差连续20个观察日")
        model = ScriptedModel({"user_id": "wrong-user", "portfolio_id": PORTFOLIO})
        with patch("app.services.portfolio_stress_tools.portfolio_stress_service", self.service), patch("app.services.tool_agent.get_chat_model", return_value=model):
            first = asyncio.run(ToolCallingAgentService().chat(request))
        self.assertEqual(model.calls, 2)
        self.assertFalse(first.tool_calls[0].is_error)
        original = json.loads(first.tool_calls[0].tool_result)
        history = [{"role": "assistant", "content": f"压力测试记录：{original['run_id']}（组合{PORTFOLIO}）"}]
        model = ScriptedModel({"portfolio_id": PORTFOLIO, "source_run_id": original["run_id"], "target_weights": dict(zip(CODES, [20, 80]))})
        self.service.risk_engine.capture_stress_inputs = lambda **kwargs: self.fail("追问不能重取当前数据")
        with patch("app.services.portfolio_stress_tools.portfolio_stress_service", self.service), patch("app.services.tool_agent.get_chat_model", return_value=model):
            followup = asyncio.run(ToolCallingAgentService().chat(request.model_copy(update={"message": "如果权益20%，债券80%呢"}), history=history))
        self.assertFalse(followup.tool_calls[0].is_error)
        self.assertEqual(json.loads(followup.tool_calls[0].tool_result)["input_hash"], original["input_hash"])

    def test_rest_create_history_read_verify(self):
        from app.routers.portfolios import router
        from app.services.auth import get_current_principal
        application = FastAPI(); application.include_router(router)
        application.dependency_overrides[get_current_principal] = lambda: SimpleNamespace(resolve_user_id=lambda value: "chen")
        with patch("app.routers.portfolios.portfolio_stress_service", self.service), TestClient(application) as client:
            base = f"/api/v1/portfolios/{PORTFOLIO}/stress-runs"
            result = client.post(base, json={"user_id": "untrusted"})
            self.assertEqual(result.status_code, 200, result.text)
            report = result.json(); endpoint = f"{base}/{report['run_id']}"
            self.assertEqual(client.get(endpoint).json(), report)
            self.assertEqual(len(client.get(base).json()), 1)
            self.assertTrue(client.post(endpoint + "/verify").json()["consistent"])
            self.assertEqual(client.post(base, json={"window_days": 900}).status_code, 422)


class StressAgentTests(unittest.TestCase):
    def test_route_followup_and_missing_portfolio(self):
        router = AgentRequestRouter()
        request = AgentChatRequest(user_id="chen", message="重演过去一年最差连续20天", portfolio_id=PORTFOLIO)
        self.assertIn("portfolio_stress", router.route(request).domains)
        history = [{"role": "assistant", "content": f"压力测试记录 stress_0123456789abcdef 组合 {PORTFOLIO}"}]
        self.assertIn("portfolio_stress", router.route(request.model_copy(update={"message": "如果权益降到20%呢"}), history=history).domains)
        self.assertTrue(router.route(request.model_copy(update={"portfolio_id": None})).needs_clarification)
        self.assertNotIn("portfolio_stress", router.route(request.model_copy(update={"message": "最近市场新闻"})).domains)

    def test_trusted_owner_and_distinct_idempotency(self):
        request = AgentChatRequest(user_id="chen", message="压力测试", portfolio_id=PORTFOLIO, idempotency_key="same-request")
        context = AgentExecutionContext.product(user_id="chen")
        keys = []
        for days in (20, 20, 40):
            args = {"user_id": "victim", "portfolio_id": "portfolio_1111111111111111", "window_days": days, "idempotency_key": "model-key"}
            AgentToolExecutor()._inject_trusted_arguments(tool_name="run_portfolio_stress_tool", tool_args=args, request=request, execution_context=context)
            self.assertEqual(args["user_id"], "chen"); self.assertEqual(args["portfolio_id"], PORTFOLIO)
            self.assertTrue(check_tool_permission(tool_name="run_portfolio_stress_tool", tool_args=args, execution_context=context).allowed)
            keys.append(args["idempotency_key"])
        self.assertEqual(keys[0], keys[1]); self.assertNotEqual(keys[1], keys[2])


if __name__ == "__main__": unittest.main()
