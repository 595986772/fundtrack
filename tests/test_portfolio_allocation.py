"""用可手算行情验证真实求解、确认隔离、幂等和API；不消耗任何外部模型额度。"""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import select

from app.database import Database
from app.models import PortfolioAllocationDraftModel, PortfolioStressRunModel
from app.schemas import AgentChatRequest
from app.services.portfolio_allocation import PortfolioAllocationService, classify_profiles, parse_question
from app.services.portfolio_allocation_contracts import AllocationConstraints, AllocationProposal, AllocationDraftRequest, AllocationConfirmRequest
from app.services.portfolio_allocation_solver import solve_allocation
from app.services.portfolio_stress import PortfolioStressService, PortfolioStressError, PortfolioStressNotFound
from app.services.portfolio_allocation_tools import draft_portfolio_allocation_tool, get_portfolio_allocation_tool
from tests.test_portfolio_stress import inputs, CODES, PORTFOLIO


TYPES = [{"fund_code": CODES[0], "fund_type": "指数型-股票", "is_bond": False},
         {"fund_code": CODES[1], "fund_type": "债券型-混合二级", "is_bond": True}]


def constraints(**changes):
    return AllocationConstraints(**{**dict(min_bond_weight_pct=40, max_single_fund_weight_pct=70, max_turnover_pct=20), **changes})


class AllocationMathTests(unittest.TestCase):
    def test_real_solver_finds_expected_weights_and_risk(self):
        r = solve_allocation(inputs(), TYPES, constraints())
        self.assertEqual(r["status"], "solved")
        self.assertAlmostEqual(r["target_weights"][CODES[0]], 40, places=5)
        self.assertAlmostEqual(r["target_weights"][CODES[1]], 60, places=5)
        self.assertAlmostEqual(r["candidate"]["cvar_95_daily_pct"], .74, places=6)
        self.assertAlmostEqual(r["turnover_pct"], 20, places=5)
        self.assertTrue(all(c["passed"] for c in r["checks"]))

    def test_two_funds_capped_40_is_infeasible(self):
        r = solve_allocation(inputs(), TYPES, constraints(max_single_fund_weight_pct=40))
        self.assertEqual(r["status"], "infeasible")
        self.assertIn("80%", r["reason"])
        self.assertEqual([c["field"] for c in r["conflicts"]], ["max_single_fund_weight_pct"])
        self.assertNotIn("target_weights", r)

    def test_bond_minimum_and_cap_conflict(self):
        r = solve_allocation(inputs(), TYPES, constraints(min_bond_weight_pct=80, max_single_fund_weight_pct=70, max_turnover_pct=100))
        self.assertEqual({c["field"] for c in r["conflicts"]}, {"min_bond_weight_pct", "max_single_fund_weight_pct"})

    def test_turnover_and_bond_min_conflict(self):
        r = solve_allocation(inputs(), TYPES, constraints(min_bond_weight_pct=70, max_turnover_pct=10))
        self.assertEqual({c["field"] for c in r["conflicts"]}, {"min_bond_weight_pct", "max_turnover_pct"})

    def test_zero_turnover_and_tied_risk_keep_baseline(self):
        r = solve_allocation(inputs(), TYPES, constraints(max_turnover_pct=0))
        self.assertFalse(r["risk_improved"])
        self.assertAlmostEqual(r["turnover_pct"], 0)
        frozen = inputs(); frozen["daily_returns"] = [[.001, .001]] * 80
        r = solve_allocation(frozen, TYPES, constraints())
        self.assertAlmostEqual(r["turnover_pct"], 0)
        self.assertLess(r["candidate"]["cvar_95_daily_pct"], 0)

    def test_missing_types_and_no_bond_are_different(self):
        types = deepcopy(TYPES); types[1]["is_bond"] = None
        with self.assertRaisesRegex(PortfolioStressError, "类型未知"):
            solve_allocation(inputs(), types, constraints())
        types[1]["is_bond"] = False
        r = solve_allocation(inputs(), types, constraints())
        self.assertEqual(r["status"], "infeasible")
        self.assertIn("没有", r["reason"])

    def test_missing_intervals_excluded_not_counted_as_daily(self):
        frozen = inputs(); frozen["eligible"][30] = False
        r = solve_allocation(frozen, TYPES, constraints())
        self.assertEqual(r["sample_count"], 79)
        self.assertEqual(r["excluded_count"], 1)
        frozen["eligible"] = [False] * 80
        with self.assertRaisesRegex(PortfolioStressError, "60"):
            solve_allocation(frozen, TYPES, constraints())

    def test_solver_failure_is_not_infeasibility(self):
        with patch("app.services.portfolio_allocation_solver.linprog", return_value=SimpleNamespace(status=1)):
            with self.assertRaisesRegex(PortfolioStressError, "不能据此判断"):
                solve_allocation(inputs(), TYPES, constraints())

    def test_schema_rejects_nan_range_and_unconfirmed(self):
        for value in (float("nan"), float("inf"), -1, 101):
            with self.assertRaises(ValidationError): constraints(max_turnover_pct=value)
        with self.assertRaises(ValidationError):
            AllocationConfirmRequest(draft_hash="a" * 64, constraints=constraints(), confirm=False)

    def test_classification_uses_type_not_fund_name(self):
        profiles = [{"fund_code": CODES[0], "short_name": "纯债名字", "fund_type": "股票型"}]
        result = classify_profiles(profiles, CODES)
        self.assertFalse(result[0]["is_bond"])
        self.assertIsNone(result[1]["is_bond"])


class AllocationServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Database(f"sqlite:///{Path(self.temp.name) / 'allocation.db'}")
        self.captures = 0
        def capture(**kwargs):
            self.captures += 1
            return inputs()
        self.stress = PortfolioStressService(self.db, risk_engine=SimpleNamespace(capture_stress_inputs=capture))
        self.parser_calls = 0
        def parser(question):
            self.parser_calls += 1
            return AllocationProposal(**constraints().model_dump())
        self.service = PortfolioAllocationService(self.db, stress=self.stress,
            funds=SimpleNamespace(list_profiles=lambda: deepcopy(TYPES)), parser=parser)

    def tearDown(self):
        self.db.engine.dispose(); self.temp.cleanup()

    def create(self, **changes):
        return self.service.create(portfolio_id=PORTFOLIO, request=AllocationDraftRequest(**{
            "user_id": "chen", "question": "债券至少40%，单只最高70%，换手最多20%", "idempotency_key": "draft-a", **changes}))

    def confirm(self, draft, **changes):
        return self.service.confirm(portfolio_id=PORTFOLIO, draft_id=draft["draft_id"], request=AllocationConfirmRequest(
            user_id="chen", draft_hash=draft["draft_hash"], constraints=constraints(**changes), confirm=True))

    def test_no_solve_before_confirmation_then_same_frozen_stress(self):
        d = self.create()
        self.assertEqual(d["status"], "awaiting_confirmation"); self.assertIsNone(d["result"])
        self.assertEqual(d["sample"]["period_start"], inputs()["start_dates"][0])
        self.assertEqual(d["sample"]["period_end"], inputs()["dates"][-1])
        self.assertEqual(d["data_sources"], inputs()["data_sources"])
        r = self.confirm(d)
        self.assertEqual(self.captures, 1)
        report = r["result"]["stress_report"]
        self.assertEqual(report["input_hash"], d["input_hash"])
        self.assertEqual(report["window"], d["window"])
        self.assertAlmostEqual(report["scenarios"][1]["weights"][CODES[0]], 40, places=5)
        self.assertEqual(self.confirm(d), r)
        with self.assertRaisesRegex(PortfolioStressError, "另一组"):
            self.confirm(d, max_turnover_pct=10)

    def test_idempotency_no_extra_parse_or_capture(self):
        d = self.create(); self.assertEqual(self.create(), d)
        self.assertEqual(self.parser_calls, 1); self.assertEqual(self.captures, 1)
        with self.assertRaisesRegex(PortfolioStressError, "编号"):
            self.create(question="换一份需求")

    def test_scope_hash_and_tamper_isolation(self):
        d = self.create()
        for user, portfolio in (("other", PORTFOLIO), ("chen", "portfolio_ffffffffffffffff")):
            with self.assertRaises(PortfolioStressNotFound):
                self.service.get(user_id=user, portfolio_id=portfolio, draft_id=d["draft_id"])
            self.assertEqual(self.service.list(user_id=user, portfolio_id=portfolio), [])
        with self.assertRaisesRegex(PortfolioStressError, "版本不匹配"):
            self.service.confirm(portfolio_id=PORTFOLIO, draft_id=d["draft_id"], request=AllocationConfirmRequest(
                user_id="chen", draft_hash="0" * 64, constraints=constraints(), confirm=True))
        with self.db.session() as session, session.begin():
            row = session.get(PortfolioAllocationDraftModel, ("chen", d["draft_id"]))
            row.draft = {**row.draft, "question": "篡改"}
        with self.assertRaisesRegex(PortfolioStressError, "完整性"):
            self.service.get(user_id="chen", portfolio_id=PORTFOLIO, draft_id=d["draft_id"])

    def test_restart_and_no_new_sources(self):
        d = self.create(); r = self.confirm(d)
        restarted = PortfolioAllocationService(self.db, stress=self.stress)
        self.assertEqual(restarted.get(user_id="chen", portfolio_id=PORTFOLIO, draft_id=d["draft_id"]), r)
        with patch.object(self.service.funds, "list_profiles", side_effect=AssertionError("should not read current profiles")):
            self.assertEqual(self.confirm(d), r)

    def test_infeasible_saved_without_candidate(self):
        d = self.create(); r = self.confirm(d, max_single_fund_weight_pct=40)
        self.assertEqual(r["status"], "infeasible")
        self.assertNotIn("stress_report", r["result"])
        with self.db.session() as session:
            self.assertEqual(len(session.scalars(select(PortfolioStressRunModel)).all()), 1)

    def test_unsupported_never_silently_ignored(self):
        d = self.service.create(portfolio_id=PORTFOLIO, request=AllocationDraftRequest(user_id="chen", question="收益至少20%", idempotency_key="u"),
            proposal=AllocationProposal(unsupported_constraints=["收益至少20%"]))
        with self.assertRaisesRegex(PortfolioStressError, "不支持"):
            self.confirm(d)

    def test_concurrent_same_confirmation_is_idempotent(self):
        d = self.create()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: self.confirm(d), range(2)))
        self.assertEqual(results[0], results[1])
        with self.db.session() as session:
            self.assertEqual(len(session.scalars(select(PortfolioStressRunModel)).all()), 2)

    def test_parse_uses_structured_model_and_failure_does_not_invent(self):
        fake = SimpleNamespace(with_structured_output=lambda *a, **k: SimpleNamespace(invoke=lambda messages: AllocationProposal(
            min_bond_weight_pct=40, max_single_fund_weight_pct=70, questions=["最多接受多少单边换手？"])))
        with patch("app.services.portfolio_allocation.get_chat_model", return_value=fake):
            self.assertIsNone(parse_question("不要大幅调仓").max_turnover_pct)
        with patch("app.services.portfolio_allocation.get_chat_model", side_effect=RuntimeError("secret")):
            with self.assertRaisesRegex(PortfolioStressError, "整理失败"):
                parse_question("不要大幅调仓")

    def test_rest_confirm_history_validation_and_owner(self):
        from app.routers import portfolios
        from app.services.auth import get_current_principal
        api = FastAPI(); api.include_router(portfolios.router)
        api.dependency_overrides[get_current_principal] = lambda: SimpleNamespace(resolve_user_id=lambda _: "chen")
        client = TestClient(api); base = f"/api/v1/portfolios/{PORTFOLIO}/allocation-drafts"
        with patch.object(portfolios, "portfolio_allocation_service", self.service):
            response = client.post(base, json={"user_id": "untrusted", "question": "风险下降", "idempotency_key": "api"})
            self.assertEqual(response.status_code, 200, response.text); d = response.json()
            self.assertEqual(client.get(base).json()[0]["draft_id"], d["draft_id"])
            url = base + "/" + d["draft_id"]
            self.assertEqual(client.get(url).json(), d)
            body = dict(draft_hash=d["draft_hash"], constraints=constraints().model_dump(), confirm=True)
            self.assertEqual(client.post(url + "/confirm", json={**body, "confirm": False}).status_code, 422)
            r = client.post(url + "/confirm", json=body)
            self.assertEqual(r.status_code, 200, r.text); self.assertEqual(r.json()["status"], "solved")

    def test_agent_tool_routing_permissions_and_owner_injection(self):
        from app.services.agent_routing import AgentRequestRouter
        from app.services.agent_tool_executor import AgentToolExecutor
        from app.services.agent_execution_context import AgentExecutionContext
        from app.services.tool_permissions import check_tool_permission
        request = AgentChatRequest(user_id="chen", message="债券至少40%，单只不超过70%，换手20%", portfolio_id=PORTFOLIO)
        route = AgentRequestRouter().route(request)
        self.assertIn("draft_portfolio_allocation_tool", route.tool_names)
        self.assertNotIn("run_portfolio_stress_tool", route.tool_names)
        args = dict(user_id="victim", portfolio_id="portfolio_ffffffffffffffff", question="model rewrite", idempotency_key="model key", proposal=constraints().model_dump())
        AgentToolExecutor()._inject_trusted_arguments(tool_name="draft_portfolio_allocation_tool", tool_args=args,
            request=request, execution_context=AgentExecutionContext.product(user_id="chen"))
        self.assertEqual(args["user_id"], "chen"); self.assertEqual(args["portfolio_id"], PORTFOLIO)
        self.assertEqual(args["question"], request.message); self.assertTrue(args["idempotency_key"].startswith("allocation:"))
        self.assertFalse(check_tool_permission("confirm_portfolio_allocation_tool", {}, execution_context=AgentExecutionContext.product(user_id="chen")).allowed)
        with patch("app.services.portfolio_allocation_tools.portfolio_allocation_service", self.service):
            d = json.loads(draft_portfolio_allocation_tool.invoke(dict(user_id="chen", portfolio_id=PORTFOLIO,
                question=request.message, idempotency_key="tool", proposal=constraints().model_dump())))
            self.assertEqual(d["status"], "awaiting_confirmation")
            r = self.confirm(d)
            result = json.loads(get_portfolio_allocation_tool.invoke(dict(user_id="chen", portfolio_id=PORTFOLIO, draft_id=d["draft_id"])))
            self.assertNotIn("curve", result["result"]["stress_report"]["scenarios"][0])
            self.assertTrue(result["result"]["facts"])

    def test_real_agent_loop_draft_then_read_confirmed_result(self):
        from langchain_core.messages import AIMessage
        from app.services.tool_agent import ToolCallingAgentService
        from app.services.comprehensive_agent import ComprehensiveAgentService
        from app.services.context_compactor import ContextCompactorService
        class ScriptedModel:
            def __init__(self, name, args): self.name, self.args, self.calls = name, args, 0
            def bind_tools(self, tools): return self
            async def ainvoke(self, messages):
                self.calls += 1
                if self.calls == 1:
                    return AIMessage(content="", tool_calls=[{"name": self.name, "args": self.args, "id": "allocation-call"}])
                payload = json.loads(messages[-1].content)
                return AIMessage(content=f"草案状态：{payload['status']}，不是未来预测。")
        request = AgentChatRequest(user_id="chen", message="债券至少40%，单只不超过70%，换手20%，降低组合历史风险", portfolio_id=PORTFOLIO)
        model = ScriptedModel("draft_portfolio_allocation_tool", dict(proposal=constraints().model_dump(), portfolio_id=PORTFOLIO))
        with patch("app.services.portfolio_allocation_tools.portfolio_allocation_service", self.service), patch("app.services.tool_agent.get_chat_model", return_value=model):
            first = asyncio.run(ToolCallingAgentService().chat(request))
        self.assertEqual(model.calls, 2)
        self.assertFalse(first.tool_calls[0].is_error, first.tool_calls[0].tool_result)
        draft = json.loads(first.tool_calls[0].tool_result)
        self.assertEqual(draft["status"], "awaiting_confirmation")
        refs = ComprehensiveAgentService._portfolio_allocation_refs(first)
        self.assertEqual(refs[0]["draft_id"], draft["draft_id"])
        confirmed = self.confirm(draft)
        self.stress.risk_engine.capture_stress_inputs = lambda **kwargs: self.fail("读结果不能抓取新数据")
        model = ScriptedModel("get_portfolio_allocation_tool", dict(draft_id=draft["draft_id"], portfolio_id=PORTFOLIO))
        history = [{"role": "assistant", "content": f"约束草案：{draft['draft_id']} 组合 {PORTFOLIO}"}]
        with patch("app.services.portfolio_allocation_tools.portfolio_allocation_service", self.service), patch("app.services.tool_agent.get_chat_model", return_value=model):
            second = asyncio.run(ToolCallingAgentService().chat(request.model_copy(update={"message": "解释一下权衡"}), history=history))
        self.assertFalse(second.tool_calls[0].is_error, second.tool_calls[0].tool_result)
        payload = json.loads(second.tool_calls[0].tool_result)
        self.assertEqual(payload["status"], "solved")
        compact = ContextCompactorService()._build_compacted_content(original_content=json.dumps(payload),
            saved_path="test-only.json", reason="test", original_chars=20000, message=SimpleNamespace(tool_call_id="test"))
        self.assertEqual(json.loads(compact)["result"]["facts"], confirmed["result"]["facts"])

    def test_alembic_table_matches_orm(self):
        from alembic import command
        from alembic.config import Config
        from app.config import settings
        from sqlalchemy import create_engine, inspect
        url = f"sqlite:///{Path(self.temp.name) / 'migration.db'}"
        with patch.object(settings, "database_url", url): command.upgrade(Config("alembic.ini"), "head")
        engine = create_engine(url)
        try:
            inspector = inspect(engine)
            self.assertEqual({c["name"] for c in inspector.get_columns("portfolio_allocation_drafts")}, set(PortfolioAllocationDraftModel.__table__.columns.keys()))
            self.assertIn("uq_allocation_idempotency", {c["name"] for c in inspector.get_unique_constraints("portfolio_allocation_drafts")})
        finally: engine.dispose()


if __name__ == "__main__":
    unittest.main()
