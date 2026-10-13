"""任务授权的领域资源回归：真实 SQLite + 注入策略，不调用模型或外部服务。"""

from copy import deepcopy
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

from sqlalchemy import select

from app.database import Database
from app.models import (
    AgentAuthorizationAuditModel, AgentTaskGrantModel, BackgroundJobModel,
    FundPortfolioModel, PortfolioAllocationDraftModel, PortfolioDecisionModel,
    PortfolioStressRunModel, PortfolioTradeOrderModel,
)
from app.schemas import PortfolioStressRequest
from app.services.agent_authorization import AgentAuthorizationError, TaskAuthorizationService
from app.services.agent_execution_context import AgentExecutionContext, agent_execution_scope
from app.services.portfolio_allocation import PortfolioAllocationService
from app.services.portfolio_allocation_contracts import (
    AllocationConfirmRequest, AllocationConstraints, AllocationDraftRequest, AllocationProposal,
)
from app.services.portfolio_decisions import PortfolioDecisionError, PortfolioDecisionService
from app.services.portfolio_research_audit import PortfolioResearchAuditService
from app.services.portfolio_research_snapshots import (
    InMemoryPortfolioResearchSnapshotStore, PortfolioResearchSnapshotService,
    REQUIRED_SNAPSHOT_SECTIONS, public_snapshot_metadata,
)
from app.services.portfolio_stress import PortfolioStressNotFound, PortfolioStressService, digest
from app.services.portfolio_trades import PortfolioTradeService
from app.services.portfolios import PortfolioNotFoundError, PortfolioService


USER = "resource-owner"
READ = "portfolio:read"
RESEARCH = "portfolio:research-run"
CODES = ["110020", "110007"]


class FakePolicy:
    """只模拟 OpenFGA 关系，任务有效期、归属和父子链仍由真实 core 检查。"""

    store_id = "resource-test-store"
    model_id = "resource-test-model"

    def __init__(self):
        self.tuples = set()
        self.checks = []
        self.unavailable = False

    def grant(self, grant_id, portfolio_ids, capabilities):
        self.tuples.update((grant_id, action, portfolio_id)
                           for action in capabilities for portfolio_id in portfolio_ids)

    def check(self, grant_id, action, portfolio_id):
        self.checks.append((grant_id, action, portfolio_id))
        if self.unavailable:
            raise RuntimeError("测试策略服务不可用")
        return (grant_id, action, portfolio_id) in self.tuples


class FakeFunds:
    """固定净值和基金档案，组合和交易测试不依赖本地行情更新。"""

    def get_fund(self, code):
        return {"profile": {"fund_code": code, "short_name": code, "sample_role": "test",
                            "fund_type": "债券型" if code == CODES[1] else "股票型"},
                "latest_nav": 1.0, "latest_nav_date": date(2025, 2, 3)}

    def list_profiles(self):
        return [self.get_fund(code)["profile"] for code in CODES]


def stress_inputs(*, user_id, portfolio_id, months):
    dates = [(date(2025, 1, 1) + timedelta(days=i)).isoformat() for i in range(81)]
    return {
        "portfolio_id": portfolio_id, "portfolio_name": portfolio_id, "months": months,
        "fund_codes": CODES, "short_names": dict(zip(CODES, CODES)),
        "baseline_weights": dict(zip(CODES, [60, 40])), "dates": dates[1:],
        "start_dates": dates[:-1], "eligible": [True] * 80,
        "daily_returns": [[-.02 if 25 <= i < 45 else .003, .001] for i in range(80)],
        "data_sources": [{"fund_code": code, "return_basis": "test"} for code in CODES],
        "calendar_note": "人工行情，仅用于权限和计算流程测试",
    }


class TaskAuthorizationResourceTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(agent_execution_scope(AgentExecutionContext.product(user_id=USER)))
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.db = Database(f"sqlite:///{Path(temporary.name) / 'resources.db'}")
        self.addCleanup(self.db.engine.dispose)
        self.funds = FakeFunds()
        self.enterContext(patch("app.services.portfolios.fund_catalog_service", self.funds))
        # 若业务路径意外调用真实模型，立即失败，不等待供应商或消耗额度。
        self.enterContext(patch("app.services.portfolio_allocation.get_chat_model",
                                side_effect=AssertionError("资源测试不能启动模型")))
        self.portfolios = PortfolioService(self.db)
        self.trades = PortfolioTradeService(self.portfolios)
        holdings = [{"fund_code": code, "units": units, "cost_amount": units,
                     "purchase_date": date(2025, 1, 2)} for code, units in zip(CODES, [600, 400])]
        self.a = self.portfolios.create(user_id=USER, name="授权组合", holdings=holdings)["id"]
        self.b = self.portfolios.create(user_id=USER, name="未授权组合", holdings=holdings)["id"]
        self.foreign = self.portfolios.create(user_id="other", name="其他用户组合", holdings=holdings)["id"]
        self.capture = Mock(side_effect=stress_inputs)
        self.stress = PortfolioStressService(self.db, risk_engine=SimpleNamespace(capture_stress_inputs=self.capture))
        self.proposal = AllocationProposal(min_bond_weight_pct=40, max_single_fund_weight_pct=70,
                                           max_turnover_pct=20)
        self.parser = Mock(return_value=self.proposal)
        self.allocation = PortfolioAllocationService(self.db, stress=self.stress,
                                                     funds=self.funds, parser=self.parser)
        self.snapshots = PortfolioResearchSnapshotService(self.db)
        self.explainer = SimpleNamespace(interpret=Mock(return_value={"model_used": False}))
        self.audit = PortfolioResearchAuditService(database=self.db, snapshots=self.snapshots,
                                                   explainer=self.explainer)
        self.performance = SimpleNamespace(analyze=Mock(return_value=None))
        self.decisions = PortfolioDecisionService(self.db, portfolios=self.portfolios,
                                                  performance=self.performance)
        self.reports, self.drafts, self.frozen, self.orders, self.decision_ids = {}, {}, {}, {}, {}
        for index, portfolio in enumerate((self.a, self.b)):
            report = self.stress.run(portfolio_id=portfolio, request=self.stress_request(portfolio))
            self.reports[portfolio] = report
            self.drafts[portfolio] = self.allocation.create(
                portfolio_id=portfolio, request=self.draft_request(portfolio), proposal=self.proposal)
            self.frozen[portfolio] = self.make_research(portfolio, job_id=f"research-{index}")
            self.orders[portfolio] = self.trades.create_order(**self.order_args(portfolio))
            now = datetime.now(timezone.utc)
            decision = PortfolioDecisionModel(
                user_id=USER, id=f"decision-{index}", portfolio_id=portfolio,
                decision_date=now.date(), target_date=now.date() + timedelta(days=30),
                action="hold", thesis="人工决策原始依据", horizon_days=30,
                snapshot={"private_marker": portfolio}, review_note="", created_at=now, updated_at=now)
            with self.db.session() as session, session.begin():
                session.add(decision)
            self.decision_ids[portfolio] = decision.id

        self.now = datetime.now(timezone.utc)
        self.policy = FakePolicy()
        self.authorization = TaskAuthorizationService(self.db, policy=self.policy, clock=lambda: self.now)
        self.enterContext(patch("app.services.agent_authorization.get_task_authorization_service",
                                return_value=self.authorization))
        self.context = self.new_grant([self.a])
        self.policy.checks.clear()

    def new_grant(self, portfolios, capabilities=None):
        return self.authorization.create(AgentExecutionContext.product(user_id=USER),
            run_id=f"run-{uuid4().hex}", agent_id="main", portfolio_ids=portfolios,
            capabilities=[READ, RESEARCH] if capabilities is None else capabilities)

    def stress_request(self, portfolio, **changes):
        return PortfolioStressRequest(**{"user_id": USER, "idempotency_key": f"stress:{portfolio}", **changes})

    def draft_request(self, portfolio, **changes):
        return AllocationDraftRequest(**{
            "user_id": USER, "question": "债券至少40%，单只最多70%，换手不超过20%",
            "source_run_id": self.reports[portfolio]["run_id"],
            "idempotency_key": f"draft:{portfolio}", **changes})

    def confirm_request(self, portfolio):
        return AllocationConfirmRequest(user_id=USER, draft_hash=self.drafts[portfolio]["draft_hash"],
            constraints=AllocationConstraints(**self.proposal.model_dump(
                exclude={"questions", "unsupported_constraints"})), confirm=True)

    def order_args(self, portfolio):
        return dict(user_id=USER, portfolio_id=portfolio, idempotency_key=f"order:{portfolio}",
                    fund_code=CODES[0], transaction_type="buy", requested_amount="100.00",
                    submitted_at=datetime(2025, 2, 3, 6, tzinfo=timezone.utc))

    def snapshot_args(self, portfolio, run_id):
        payload = {key: {} for key in REQUIRED_SNAPSHOT_SECTIONS}
        payload["portfolio"] = {"id": portfolio, "name": portfolio, "holdings": []}
        return dict(user_id=USER, portfolio_id=portfolio, run_id=run_id, period_months=12,
                    graph_version="test-research-v1", capture_started_at=datetime.now(timezone.utc),
                    source_manifest={}, payload=payload)

    def make_research(self, portfolio, *, job_id):
        snapshot = self.snapshots.create_once(**self.snapshot_args(portfolio, job_id))
        now = datetime.now(timezone.utc)
        report = {"workflow": "portfolio_research", "run_id": job_id, "portfolio_id": portfolio,
                  "period_months": 12, "headline": "冻结研究报告", "research_snapshot": public_snapshot_metadata(snapshot)}
        with self.db.session() as session, session.begin():
            session.add(BackgroundJobModel(user_id=USER, id=job_id, title="冻结研究报告",
                job_type="portfolio_research", status="completed", payload={"portfolio_id": portfolio},
                result=report, created_at=now, updated_at=now, completed_at=now))
        return snapshot

    def readers(self, portfolio):
        scope = dict(user_id=USER, portfolio_id=portfolio)
        run_id = self.reports[portfolio]["run_id"]
        draft_id = self.drafts[portfolio]["draft_id"]
        snapshot = self.frozen[portfolio]
        return {
            "portfolio": lambda: self.portfolios.get(**scope),
            "transactions": lambda: self.portfolios.list_transactions(**scope),
            "stress": lambda: self.stress.get(**scope, run_id=run_id),
            "stress_inputs": lambda: self.stress.frozen_inputs(**scope, run_id=run_id),
            "stress_list": lambda: self.stress.list(**scope),
            "stress_verify": lambda: self.stress.verify(**scope, run_id=run_id),
            "allocation": lambda: self.allocation.get(**scope, draft_id=draft_id),
            "allocation_list": lambda: self.allocation.list(**scope),
            "decision": lambda: self.decisions.get(**scope, decision_id=self.decision_ids[portfolio]),
            "decision_list": lambda: self.decisions.list(**scope),
            "snapshot": lambda: self.snapshots.get(user_id=USER, snapshot_id=snapshot["id"]),
            "snapshot_run": lambda: self.snapshots.get_for_run(user_id=USER, run_id=snapshot["run_id"]),
            "report": lambda: self.audit.read_report(**scope, job_id=snapshot["run_id"]),
            "report_list": lambda: self.audit.list_runs(**scope),
            "order": lambda: self.trades.get_order(**scope, order_id=self.orders[portfolio]["id"]),
            "order_list": lambda: self.trades.list_orders(**scope),
        }

    def test_reads_filter_same_owner_portfolios_and_leave_human_requests_unchanged(self):
        with agent_execution_scope(self.context):
            self.assertEqual([row["id"] for row in self.portfolios.list(user_id=USER)], [self.a])
            for name, read in self.readers(self.a).items():
                with self.subTest(allowed=name):
                    self.assertIsNotNone(read())
            for name, read in self.readers(self.b).items():
                with self.subTest(denied=name), self.assertRaises(AgentAuthorizationError):
                    read()
            for read in (
                lambda: self.portfolios.list(user_id="other"),
                lambda: self.portfolios.get(user_id="other", portfolio_id=self.foreign),
            ):
                with self.assertRaises(AgentAuthorizationError):
                    read()
        # 没有 grant 的 Context 与没有 Context 一样，不能误伤普通 REST。
        with patch("app.services.agent_authorization.get_task_authorization_service",
                   side_effect=AssertionError("人工请求不应调用授权后端")):
            self.assertEqual({row["id"] for row in self.portfolios.list(user_id=USER)}, {self.a, self.b})
            for read in self.readers(self.b).values():
                self.assertIsNotNone(read())

    def test_empty_task_scope_is_not_all_portfolios(self):
        with agent_execution_scope(self.new_grant([])):
            self.assertEqual(self.portfolios.list(user_id=USER), [])
            with self.assertRaises(AgentAuthorizationError):
                self.snapshots.get(user_id=USER, snapshot_id=self.frozen[self.a]["id"])
            with self.assertRaises(AgentAuthorizationError):
                self.portfolios.create(user_id=USER, name="不能创建", holdings=[])

    def test_lists_recheck_stored_rows_before_returning_content(self):
        # 模拟入口预检后策略关系被撤销，列表不能直接交付先前查询到的正文。
        with agent_execution_scope(self.context):
            for entry in ("stress_list", "allocation_list", "decision_list", "order_list"):
                with self.subTest(entry=entry), patch.object(self.policy, "check", side_effect=[True, False]):
                    with self.assertRaises(AgentAuthorizationError):
                        self.readers(self.a)[entry]()

    def test_expired_revoked_and_forged_contexts_cannot_read_stored_results(self):
        for state in ("expired", "revoked", "wrong-run", "wrong-agent"):
            context = self.new_grant([self.a])
            if state in {"expired", "revoked"}:
                with self.db.session() as session, session.begin():
                    grant = session.get(AgentTaskGrantModel, context.task_grant_id)
                    if state == "expired":
                        grant.expires_at = self.now - timedelta(seconds=1)
                    else:
                        grant.status = "revoked"
            else:
                context = replace(context, **{"task_run_id" if state == "wrong-run" else "task_agent_id": "forged"})
            with agent_execution_scope(context):
                for name, read in self.readers(self.a).items():
                    with self.subTest(state=state, entry=name), self.assertRaises(AgentAuthorizationError):
                        read()
                with self.assertRaises(AgentAuthorizationError):
                    self.portfolios.list(user_id=USER)

    def test_policy_denial_and_outage_fail_closed_on_direct_service_reads(self):
        for unavailable in (False, True):
            self.policy.unavailable = unavailable
            self.policy.tuples.discard((self.context.task_grant_id, READ, self.a))
            with agent_execution_scope(self.context):
                for name, read in self.readers(self.a).items():
                    with self.subTest(unavailable=unavailable, entry=name), self.assertRaises(AgentAuthorizationError):
                        read()

    def test_orphaned_history_is_not_authorized_by_old_grant_membership(self):
        with self.db.session() as session, session.begin():
            session.delete(session.get(FundPortfolioModel, (USER, self.a)))
        with agent_execution_scope(self.context):
            for name in ("stress", "allocation", "snapshot", "report", "report_list"):
                with self.subTest(entry=name), self.assertRaises(AgentAuthorizationError):
                    self.readers(self.a)[name]()

    def test_foreign_record_ids_cannot_be_laundered_through_allowed_parameter(self):
        scope = dict(user_id=USER, portfolio_id=self.a)
        calls = [
            (PortfolioStressNotFound, lambda: self.stress.get(**scope, run_id=self.reports[self.b]["run_id"])),
            (PortfolioStressNotFound, lambda: self.allocation.get(**scope, draft_id=self.drafts[self.b]["draft_id"])),
            (PortfolioDecisionError, lambda: self.decisions.get(**scope, decision_id=self.decision_ids[self.b])),
            (PortfolioNotFoundError, lambda: self.trades.get_order(**scope, order_id=self.orders[self.b]["id"])),
            (AgentAuthorizationError, lambda: self.audit.read_report(**scope, job_id=self.frozen[self.b]["run_id"])),
        ]
        with agent_execution_scope(self.context):
            for error, call in calls:
                with self.subTest(error=error.__name__), self.assertRaises(error):
                    call()

    def test_report_json_cannot_hide_the_jobs_actual_portfolio(self):
        job_id = self.frozen[self.b]["run_id"]
        with self.db.session() as session, session.begin():
            row = session.get(BackgroundJobModel, (USER, job_id))
            row.result = {**row.result, "portfolio_id": self.a}
        with agent_execution_scope(self.context):
            with self.assertRaises(AgentAuthorizationError):
                self.audit.read_report(user_id=USER, portfolio_id=self.a, job_id=job_id)
            history = self.audit.list_runs(user_id=USER, portfolio_id=self.a)
            self.assertEqual([run["job_id"] for run in history["runs"]], [self.frozen[self.a]["run_id"]])

    def test_inmemory_snapshots_follow_the_same_scope_rules(self):
        store = InMemoryPortfolioResearchSnapshotStore()
        allowed = store.create_once(**self.snapshot_args(self.a, "memory-a"))
        hidden = store.create_once(**self.snapshot_args(self.b, "memory-b"))
        with agent_execution_scope(self.context):
            self.assertEqual(store.get_for_run(user_id=USER, run_id="memory-a"), allowed)
            for call in (
                lambda: store.get(user_id=USER, snapshot_id=hidden["id"]),
                lambda: store.get_for_run(user_id=USER, run_id="memory-b"),
                lambda: store.create_once(**self.snapshot_args(self.a, "memory-b")),
            ):
                with self.assertRaises(AgentAuthorizationError):
                    call()

    def test_research_creation_needs_research_action_but_does_not_change_assets(self):
        before = self.portfolios.get(user_id=USER, portfolio_id=self.a)
        with agent_execution_scope(self.context):
            report = self.stress.run(portfolio_id=self.a,
                                    request=self.stress_request(self.a, idempotency_key="new-stress"))
            draft = self.allocation.create(portfolio_id=self.a,
                request=self.draft_request(self.a, idempotency_key="new-draft"), proposal=self.proposal)
            self.assertEqual(draft["status"], "awaiting_confirmation")
            self.assertEqual(report["portfolio_id"], self.a)
            self.snapshots.create_once(**self.snapshot_args(self.a, "new-snapshot"))
        self.assertEqual(before, self.portfolios.get(user_id=USER, portfolio_id=self.a))
        self.assertIn((self.context.task_grant_id, RESEARCH, self.a), self.policy.checks)
        read_only = self.new_grant([self.a], [READ])
        with agent_execution_scope(read_only), patch.object(self.capture, "side_effect", AssertionError("拒绝前不能计算")):
            for call in (
                lambda: self.stress.run(portfolio_id=self.a, request=self.stress_request(self.a)),
                lambda: self.allocation.create(portfolio_id=self.a, request=self.draft_request(self.a)),
                lambda: self.snapshots.create_once(**self.snapshot_args(self.a, "new-snapshot")),
            ):
                with self.assertRaises(AgentAuthorizationError) as denied:
                    call()
                self.assertEqual(denied.exception.code, "action_denied")
        self.parser.assert_not_called()

    def test_idempotent_results_check_actual_row_even_with_matching_hash(self):
        stress_request = self.stress_request(self.b)
        draft_request = self.draft_request(self.a, idempotency_key=f"draft:{self.b}")
        # 故意让指纹匹配请求；若实现只检查哈希或参数，就会把 B 的正文泄给 A。
        with self.db.session() as session, session.begin():
            stress = session.get(PortfolioStressRunModel, (USER, self.reports[self.b]["run_id"]))
            stress.request_hash = digest({"portfolio_id": self.a,
                **stress_request.model_dump(exclude={"idempotency_key"}, mode="json")})
            draft = session.get(PortfolioAllocationDraftModel, (USER, self.drafts[self.b]["draft_id"]))
            draft.request_hash = digest({"portfolio_id": self.a,
                **draft_request.model_dump(exclude={"idempotency_key"})})
        with agent_execution_scope(self.context):
            for call in (
                lambda: self.stress.run(portfolio_id=self.a, request=stress_request),
                lambda: self.allocation.create(portfolio_id=self.a, request=draft_request),
                lambda: self.snapshots.create_once(**self.snapshot_args(self.a, self.frozen[self.b]["run_id"])),
            ):
                with self.assertRaises(AgentAuthorizationError):
                    call()

    def test_stress_unique_conflict_rechecks_the_winning_rows_portfolio(self):
        request = self.stress_request(self.a, idempotency_key="race-stress")

        def capture(**kwargs):
            # 在计算期间插入并发赢家，确定性走到 IntegrityError 恢复分支。
            with self.db.session() as session, session.begin():
                row = session.get(PortfolioStressRunModel, (USER, self.reports[self.b]["run_id"]))
                session.add(PortfolioStressRunModel(user_id=USER, id="race-winner", portfolio_id=self.b,
                    root_id=row.root_id, idempotency_key=request.idempotency_key,
                    request_hash=digest({"portfolio_id": self.a,
                        **request.model_dump(exclude={"idempotency_key"}, mode="json")}),
                    input_hash=row.input_hash, report=deepcopy(row.report), created_at=self.now))
            return stress_inputs(**kwargs)

        with agent_execution_scope(self.context), patch.object(self.capture, "side_effect", capture):
            with self.assertRaises(AgentAuthorizationError):
                self.stress.run(portfolio_id=self.a, request=request)

    def test_draft_unique_conflict_rechecks_the_winning_rows_portfolio(self):
        request = self.draft_request(self.a, idempotency_key="race-draft")

        def parser(question):
            with self.db.session() as session, session.begin():
                row = session.get(PortfolioAllocationDraftModel, (USER, self.drafts[self.b]["draft_id"]))
                session.add(PortfolioAllocationDraftModel(user_id=USER, id="race-winner", portfolio_id=self.b,
                    idempotency_key=request.idempotency_key,
                    request_hash=digest({"portfolio_id": self.a, **request.model_dump(exclude={"idempotency_key"})}),
                    draft_hash=row.draft_hash, draft=deepcopy(row.draft), created_at=self.now))
            return self.proposal

        with agent_execution_scope(self.context), patch.object(self.parser, "side_effect", parser):
            with self.assertRaises(AgentAuthorizationError):
                self.allocation.create(portfolio_id=self.a, request=request)

    def test_all_mutating_entries_refuse_agent_before_opening_transactions(self):
        scope = dict(user_id=USER, portfolio_id=self.a)
        transaction_id = self.portfolios.list_transactions(**scope)[0]["id"]
        calls = {
            "create": lambda: self.portfolios.create(user_id=USER, name="禁止新增", holdings=[]),
            "replace": lambda: self.portfolios.update(**scope, name="禁止改仓", holdings=[]),
            "rename": lambda: self.portfolios.rename(**scope, name="禁止改名"),
            "delete": lambda: self.portfolios.delete(**scope),
            "transaction": lambda: self.portfolios.add_transaction(**scope, fund_code=CODES[0],
                transaction_type="buy", trade_date=date(2025, 2, 3), units=1, unit_price=1, idempotency_key="agent-buy"),
            "delete_transaction": lambda: self.portfolios.delete_transaction(**scope, transaction_id=transaction_id),
            "order": lambda: self.trades.create_order(**self.order_args(self.a)),
            "confirm_trade": lambda: self.trades.apply_action(**scope, order_id=self.orders[self.a]["id"],
                idempotency_key="agent-confirm", action="confirm", reason="禁止代为确认"),
            "correction": lambda: self.trades.correct_transaction(**scope, transaction_id=transaction_id,
                idempotency_key="agent-correct", reason="禁止改账"),
            "confirm_draft": lambda: self.allocation.confirm(portfolio_id=self.a,
                draft_id=self.drafts[self.a]["draft_id"], request=self.confirm_request(self.a)),
            "decision_create": lambda: self.decisions.create(**scope, action="hold", thesis="不能替用户决策", horizon_days=30),
            "decision_review": lambda: self.decisions.review(**scope, decision_id=self.decision_ids[self.a], review_note="不能代确认"),
            "decision_delete": lambda: self.decisions.delete(**scope, decision_id=self.decision_ids[self.a]),
            "decision_cleanup": lambda: self.decisions.delete_for_portfolio(**scope),
        }
        with agent_execution_scope(self.context), patch.object(self.db, "session",
                side_effect=AssertionError("拒绝写入应发生在开事务之前")):
            for name, call in calls.items():
                with self.subTest(entry=name), self.assertRaises(AgentAuthorizationError) as denied:
                    call()
                self.assertEqual(denied.exception.code, "write_not_granted")
        with self.db.session() as session, session.begin(), agent_execution_scope(self.context):
            with patch.object(session, "connection", side_effect=AssertionError("不能先获取 SQLite 写锁")):
                with self.assertRaises(AgentAuthorizationError):
                    self.portfolios._get_model(session, **scope, for_update=True)

    def test_human_confirm_and_bookkeeping_still_work_without_task_scope(self):
        scope = dict(user_id=USER, portfolio_id=self.a)
        timing = {"nav_date": "2025-02-03", "warnings": [],
                  "submitted_at": "2025-02-03T06:00:00+00:00", "channel": "off_exchange"}
        with patch("app.services.portfolios.preview_transaction_timing", return_value=timing):
            order = self.trades.apply_action(**scope, order_id=self.orders[self.a]["id"],
                idempotency_key="human-confirm", action="confirm", reason="已核对真实回单",
                trade_date=date(2025, 2, 3), confirmation_date=date(2025, 2, 4), units=100, unit_price=1)
        self.assertEqual(order["status"], "confirmed")
        self.assertEqual(len(self.portfolios.list_transactions(**scope)), 3)
        self.trades.correct_transaction(**scope, transaction_id=order["transaction_id"],
                                       idempotency_key="human-reverse", reason="人工冲正误录流水")
        draft = self.allocation.confirm(portfolio_id=self.a, draft_id=self.drafts[self.a]["draft_id"],
                                        request=self.confirm_request(self.a))
        self.assertEqual(draft["status"], "solved")
        self.portfolios.rename(**scope, name="人工重命名")
        allocation = self.portfolios.create(user_id=USER, name="人工权重组合",
                                             holdings=[{"fund_code": CODES[0], "weight_pct": 100}])
        self.portfolios.update(user_id=USER, portfolio_id=allocation["id"], name="人工改仓",
                               holdings=[{"fund_code": CODES[1], "weight_pct": 100}])
        self.portfolios.delete(user_id=USER, portfolio_id=allocation["id"])
        self.assertEqual(self.policy.checks, [])

    def test_services_do_not_write_audit_inside_sqlite_write_lock(self):
        with self.db.session() as session:
            audit_ids = set(session.scalars(select(AgentAuthorizationAuditModel.id)))
        with agent_execution_scope(self.context), patch.object(self.authorization, "_audit",
                side_effect=AssertionError("服务层不能另开审计写事务")):
            with self.db.session() as session, session.begin():
                session.connection().exec_driver_sql("BEGIN IMMEDIATE")
                self.snapshots.get(user_id=USER, snapshot_id=self.frozen[self.a]["id"])
                with self.assertRaises(AgentAuthorizationError):
                    self.snapshots.get(user_id=USER, snapshot_id=self.frozen[self.b]["id"])
            self.stress.run(portfolio_id=self.a, request=self.stress_request(self.a, idempotency_key="without-audit"))
        with self.db.session() as session:
            self.assertEqual(audit_ids, set(session.scalars(select(AgentAuthorizationAuditModel.id))))

    def test_report_comparison_validates_explicit_base_and_target(self):
        target = self.make_research(self.a, job_id="research-a-new")
        scope = dict(user_id=USER, portfolio_id=self.a, target_job_id=target["run_id"])
        with agent_execution_scope(self.context):
            with self.assertRaises(AgentAuthorizationError):
                self.audit.compare(**scope, base_job_id=self.frozen[self.b]["run_id"])
            self.explainer.interpret.assert_not_called()
            result = self.audit.compare(**scope)
            self.assertEqual(result["base_run"]["job_id"], self.frozen[self.a]["run_id"])
            self.assertEqual(result["portfolio_id"], self.a)

    def test_linked_trade_transaction_requires_its_own_portfolio_authorization(self):
        hidden_transaction = self.portfolios.list_transactions(user_id=USER, portfolio_id=self.b)[0]
        with self.db.session() as session, session.begin():
            row = session.get(PortfolioTradeOrderModel, (USER, self.orders[self.a]["id"]))
            row.transaction_id = hidden_transaction["id"]
            row.status = "confirmed"
        with agent_execution_scope(self.context):
            with self.assertRaises(AgentAuthorizationError):
                self.trades.get_order(user_id=USER, portfolio_id=self.a, order_id=self.orders[self.a]["id"])


if __name__ == "__main__":
    unittest.main()
