"""Agent Runtime 的 SQLAlchemy 数据库连接。

这个文件只负责数据库基础设施：创建 Engine、Session 和表结构。Job 业务通过
``app/services/job_store.py`` 访问数据库；Agent Teams 业务则通过自己的 service
读写 teammate 和 message。这里不放任何状态流转规则。

本地学习模式使用 SQLite；Docker Compose 中 ``DATABASE_URL`` 会指向 PostgreSQL。
两种数据库使用同一套 ORM 模型，因此切换环境时不需要重写 service 层。
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import settings


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class Base(DeclarativeBase):
    """所有 ORM 表模型的共同基类。"""


class Database:
    """封装一个可复用的 SQLAlchemy Engine 和 Session 工厂。

    Engine 管理连接池，适合跟随进程长期存在；Session 表示一次短事务，应该用完即关。
    把两者分清楚，是理解 SQLAlchemy 工程代码的第一步。
    """

    def __init__(self, url: str) -> None:
        self.url = self._normalize_url(url)

        # SQLite 默认限制连接只能在创建它的线程里使用。
        # 当前项目的本地 Worker 会跨线程更新进度，所以要关闭该限制。
        connect_args = (
            {"check_same_thread": False}
            if self.url.startswith("sqlite")
            else {}
        )
        self.engine: Engine = create_engine(
            self.url,
            # 从连接池取连接前先做一次探活，避免数据库重启后拿到失效连接。
            pool_pre_ping=True,
            connect_args=connect_args,
        )
        self.session_factory = sessionmaker(
            bind=self.engine,
            class_=Session,
            # commit 后仍允许读取刚写入对象的字段，service 返回响应时无需再次查询。
            expire_on_commit=False,
        )

    def create_schema(self) -> None:
        """创建当前还不存在的表。

        本地首次启动可用 create_all 快速建表；已有数据库和正式部署由 Alembic
        revision 管理结构演进。
        """

        # Python 只有执行过模型类定义，表才会注册进 Base.metadata。
        # 所以即使这里不直接使用这些类，也必须先导入它们。
        from app.models import (  # noqa: F401
            AgentAutonomySessionModel,
            AgentChatExecutionModel,
            AgentConversationMessageModel,
            AgentConversationModel,
            AgentEvalRunModel,
            AgentResponseFeedbackModel,
            AgentRunObservationModel,
            AgentTeamProtocolEventModel,
            AgentTeamProtocolModel,
            AgentTeammateModel,
            AgentTeamMessageModel,
            AgentWorktreeEventModel,
            AgentWorktreeModel,
            BackgroundJobModel,
            AppUserModel,
            AuthSessionModel,
            FundCatalogEntryModel,
            FundBenchmarkReturnModel,
            FundBenchmarkSyncModel,
            FundCorporateActionModel,
            FundCorporateActionSyncModel,
            FundDisclosureSyncModel,
            FundIndustryAllocationModel,
            FundNavSnapshotModel,
            FundPortfolioHoldingModel,
            FundPortfolioModel,
            FundPortfolioTransactionModel,
            FundSecurityHoldingModel,
            InstitutionalTradeModel,
            EtfCapitalSnapshotModel,
            MarketEventModel,
            MarketEventClusterMemberModel,
            MarketEventClusterModel,
            MarketEventEmbeddingModel,
            MarketHotspotSnapshotModel,
            MarketTopicKeywordExpansionModel,
            MarketEventSemanticAnalysisModel,
            MarketIndexBarModel,
            MarketSnapshotModel,
            PortfolioAlertModel,
            PortfolioDecisionModel,
            PortfolioMonitoringPolicyModel,
            PortfolioResearchInputSnapshotModel,
            PortfolioRiskSnapshotModel,
            SocialInvestmentPostModel,
            SocialSignalAnalysisModel,
            SocialPostMediaModel,
            SocialSignalReportModel,
        )

        if not settings.database_auto_create_schema:
            return

        # create_all 只创建缺失表，不负责修改已有列；正式迁移应使用 Alembic。
        Base.metadata.create_all(self.engine)
        self._apply_additive_migrations()

    def _apply_additive_migrations(self) -> None:
        """为已有学习数据库补充向后兼容的新列。

        这里仅保留少量“只新增、不删除、不改类型”的启动兼容逻辑，让早期本地 SQLite
        文件能直接启动。正式环境仍以 Alembic revision 作为唯一迁移记录。
        """

        inspector = inspect(self.engine)
        table_names = set(inspector.get_table_names())
        statements: list[str] = []

        # 本地旧库只追加分析详情列，保留全部既有帖子和模型结果。
        if "social_signal_analyses" in table_names:
            columns = {item["name"] for item in inspector.get_columns("social_signal_analyses")}
            if "details" not in columns:
                statements.append("ALTER TABLE social_signal_analyses ADD COLUMN details JSON")

        risk_table = "portfolio_risk_snapshots"
        if risk_table in table_names:
            risk_columns = {
                column["name"] for column in inspector.get_columns(risk_table)
            }
            # 表名和列名来自源码常量，不接受用户输入；JSON 在 SQLite 和 PostgreSQL
            # 中都可作为新增列类型，历史行由 Service 把 NULL 规范化为空对象。
            for column_name in ("comparison", "generation", "event_impacts"):
                if column_name not in risk_columns:
                    statements.append(
                        f"ALTER TABLE {risk_table} ADD COLUMN {column_name} JSON"
                    )

        policy_table = "portfolio_monitoring_policies"
        if policy_table in table_names:
            policy_columns = {
                column["name"] for column in inspector.get_columns(policy_table)
            }
            if "event_impact_exposure_threshold_pct" not in policy_columns:
                statements.append(
                    f"ALTER TABLE {policy_table} ADD COLUMN "
                    "event_impact_exposure_threshold_pct "
                    "FLOAT DEFAULT 5.0 NOT NULL"
                )

        conversation_table = "agent_conversations"
        if conversation_table in table_names:
            conversation_columns = {
                column["name"]
                for column in inspector.get_columns(conversation_table)
            }
            # 这三个字段支撑跨请求滚动摘要。默认值让历史会话直接从“尚未摘要”开始，
            # 不需要一次性扫描并重写旧数据。
            conversation_ddl = {
                "rolling_summary": "TEXT DEFAULT '' NOT NULL",
                "summary_message_count": "INTEGER DEFAULT 0 NOT NULL",
                "summary_updated_at": "TIMESTAMP",
            }
            for column_name, column_type in conversation_ddl.items():
                if column_name not in conversation_columns:
                    statements.append(
                        f"ALTER TABLE {conversation_table} "
                        f"ADD COLUMN {column_name} {column_type}"
                    )

        holding_table = "fund_portfolio_holdings"
        if holding_table in table_names:
            holding_columns = {
                column["name"] for column in inspector.get_columns(holding_table)
            }
            # 本地旧组合继续作为 allocation 读取；新建真实持仓才会写入份额与成本。
            holding_ddl = {
                "position_mode": "VARCHAR(20) DEFAULT 'allocation' NOT NULL",
                "units": "FLOAT",
                "cost_amount": "FLOAT",
                "average_cost_nav": "FLOAT",
                "first_purchase_date": "DATE",
            }
            for column_name, column_type in holding_ddl.items():
                if column_name not in holding_columns:
                    statements.append(
                        f"ALTER TABLE {holding_table} "
                        f"ADD COLUMN {column_name} {column_type}"
                    )

        transaction_table = "fund_portfolio_transactions"
        if transaction_table in table_names:
            transaction_columns = {
                column["name"]
                for column in inspector.get_columns(transaction_table)
            }
            if "exact_values" not in transaction_columns:
                statements.append(f"ALTER TABLE {transaction_table} ADD COLUMN exact_values JSON")
            if "timing" not in transaction_columns:
                statements.append(f"ALTER TABLE {transaction_table} ADD COLUMN timing JSON")
            transaction_indexes = {
                index["name"]
                for index in inspector.get_indexes(transaction_table)
            }
            # 本地 SQLite 默认使用 create_all 启动，不会自动修改已有表。这里做一次
            # 向后兼容的纯新增迁移；Docker/正式环境仍由下方 Alembic revision 管理。
            if "idempotency_key" not in transaction_columns:
                statements.append(
                    f"ALTER TABLE {transaction_table} "
                    "ADD COLUMN idempotency_key VARCHAR(100)"
                )
            if (
                "uq_fund_portfolio_transactions_idempotency"
                not in transaction_indexes
            ):
                statements.append(
                    "CREATE UNIQUE INDEX IF NOT EXISTS "
                    "uq_fund_portfolio_transactions_idempotency "
                    f"ON {transaction_table} "
                    "(user_id, portfolio_id, idempotency_key)"
                )

        if not statements:
            return
        with self.engine.begin() as connection:
            for statement in statements:
                connection.execute(text(statement))

    def session(self) -> Session:
        """返回一个短生命周期 Session，由调用方使用 ``with`` 关闭。"""

        # 调用方通常写成：with database.session() as session, session.begin():
        # begin 成功时 commit，异常时 rollback，with 结束时关闭 Session。
        return self.session_factory()

    def _normalize_url(self, url: str) -> str:
        value = str(url or "").strip()
        if not value:
            raise ValueError("DATABASE_URL 不能为空。")

        # 相对 SQLite 文件统一相对于项目根目录解析，避免从不同 cwd 启动时
        # 意外生成多个数据库文件。
        prefix = "sqlite:///"
        if value.startswith(prefix) and not value.startswith("sqlite:////"):
            relative_path = value[len(prefix) :]
            absolute_path = (PROJECT_ROOT / relative_path).resolve()
            absolute_path.parent.mkdir(parents=True, exist_ok=True)
            return f"sqlite:///{absolute_path}"

        return value


# API 进程和 Celery Worker 都从相同环境变量创建数据库连接。
runtime_database = Database(settings.database_url)
