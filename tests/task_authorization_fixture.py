"""任务授权测试的独立数据库与策略替身；不读取真实账户，也不调用付费模型。"""

from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from app.database import Database
from app.models import FundPortfolioModel
from app.services.agent_authorization import TaskAuthorizationService
from app.services.agent_execution_context import AgentExecutionContext

A = "portfolio_aaaaaaaaaaaaaaaa"
B = "portfolio_bbbbbbbbbbbbbbbb"
C = "portfolio_cccccccccccccccc"


class FakePolicy:
    """只替代 HTTP 关系判定；真实生命周期、身份、SQL 和工具执行器照常运行。"""
    store_id, model_id = "test-store", "test-model"

    def __init__(self):
        self.relations = set()
        self.down = False

    def grant(self, grant_id, portfolio_ids, capabilities):
        if self.down:
            raise RuntimeError("policy unavailable")
        for portfolio in portfolio_ids:
            for action in capabilities:
                self.relations.add((grant_id, action, portfolio))

    def check(self, grant_id, action, portfolio_id):
        if self.down:
            raise RuntimeError("policy unavailable")
        return (grant_id, action, portfolio_id) in self.relations


class AuthorizationFixture:
    def __init__(self):
        self.directory = TemporaryDirectory()
        self.database = Database(f"sqlite:///{Path(self.directory.name) / 'authz.db'}")
        self.policy = FakePolicy()
        self.now = datetime(2026, 9, 21, 6, 30, tzinfo=timezone.utc)
        self.service = TaskAuthorizationService(self.database, policy=self.policy, clock=lambda: self.now)
        with self.database.session() as session, session.begin():
            for portfolio_id, owner in ((A, "chen"), (B, "chen"), (C, "alice")):
                session.add(FundPortfolioModel(id=portfolio_id, user_id=owner, name=portfolio_id,
                    created_at=self.now, updated_at=self.now))
        self.context = AgentExecutionContext.product(user_id="chen", source="test-api", authenticated=True)

    def grant(self, portfolios=None, *, run_id="test-run", **options):
        return self.service.create(self.context, run_id=run_id, agent_id="main",
            portfolio_ids=[A] if portfolios is None else portfolios, **options)

    def close(self):
        self.database.engine.dispose()
        self.directory.cleanup()


def install_research_authorization(test_case, portfolio_id):
    """给早期研究测试补齐真实授权生命周期，不能靠关闭产品权限来跑绿测试。"""
    fixture = AuthorizationFixture()
    test_case.addCleanup(fixture.close)
    with fixture.database.session() as session, session.begin():
        session.add(FundPortfolioModel(id=portfolio_id, user_id="chen", name="研究测试组合",
            created_at=fixture.now, updated_at=fixture.now))
    test_case.enterContext(patch("app.services.agent_authorization.get_task_authorization_service", return_value=fixture.service))
    return fixture
