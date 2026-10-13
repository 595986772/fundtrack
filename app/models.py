"""Agent Runtime 的 SQLAlchemy ORM 表模型。"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    Float,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class AgentTaskGrantModel(Base):
    """一次任务的授权单。保存范围与生命周期，不保存 Prompt 或金融数据正文。"""

    __tablename__ = "agent_task_grants"
    __table_args__ = (
        UniqueConstraint("user_id", "run_id", "agent_id", name="uq_agent_task_grant_identity"),
        Index("ix_agent_task_grant_user_time", "user_id", "created_at"),
    )
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(50))
    run_id: Mapped[str] = mapped_column(String(100))
    agent_id: Mapped[str] = mapped_column(String(100))
    parent_grant_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    source: Mapped[str] = mapped_column(String(80))
    status: Mapped[str] = mapped_column(String(20))
    portfolio_ids: Mapped[list[str]] = mapped_column(JSON)
    capabilities: Mapped[list[str]] = mapped_column(JSON)
    # 固定策略版本，防止部署时切换 model/store 悄悄改变已有任务的权限含义。
    policy_store_id: Mapped[str] = mapped_column(String(64))
    policy_model_id: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoke_reason: Mapped[str | None] = mapped_column(String(200), nullable=True)


class AgentAuthorizationAuditModel(Base):
    """授权判定日志，只记录资源编号和结果，不把工具参数、持仓或密钥抄进日志。"""

    __tablename__ = "agent_authorization_audits"
    __table_args__ = (Index("ix_agent_authz_audit_grant_time", "grant_id", "created_at"),)
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(50))
    grant_id: Mapped[str] = mapped_column(String(64))
    action: Mapped[str] = mapped_column(String(80))
    resource_id: Mapped[str] = mapped_column(String(100))
    allowed: Mapped[bool] = mapped_column(Boolean)
    reason: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ReliableJobStateModel(Base):
    """组合投研的执行权。版本号递增后，旧 Worker 即使活过来也不能写回结果。"""

    __tablename__ = "reliable_job_states"
    user_id: Mapped[str] = mapped_column(String(50), primary_key=True)
    job_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    execution_version: Mapped[int] = mapped_column(Integer, default=0)
    owner_token: Mapped[str | None] = mapped_column(String(32), nullable=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    # 总执行期限从第一次领取开始计算；接管不能不断获得新的完整超时时间。
    deadline_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    recovery_count: Mapped[int] = mapped_column(Integer, default=0)
    failure_retry_count: Mapped[int] = mapped_column(Integer, default=0)
    max_recoveries: Mapped[int] = mapped_column(Integer)
    checkpoint_threads: Mapped[list] = mapped_column(JSON, default=list)
    last_recovery_reason: Mapped[str | None] = mapped_column(String(200), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class JobOutboxModel(Base):
    """待投递清单，与 Job 在同一事务保存。消息可以重复，领取必须幂等。"""

    __tablename__ = "job_outbox"
    __table_args__ = (
        UniqueConstraint("user_id", "job_id", "execution_version", name="uq_job_outbox_version"),
        Index("ix_job_outbox_due", "status", "available_at"),
    )
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(50))
    job_id: Mapped[str] = mapped_column(String(64))
    execution_version: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16), default="pending")
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # 发布者也有短租约：发出消息后宕机，其他进程可以补投同一个事件。
    sender_token: Mapped[str | None] = mapped_column(String(32), nullable=True)
    dispatch_attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class AIUsageBucketModel(Base):
    """每日/单任务的 Token 账本。预留、已知消耗、待确认消耗分别计数。"""

    __tablename__ = "ai_usage_buckets"
    user_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    scope_key: Mapped[str] = mapped_column(String(100), primary_key=True)
    used_tokens: Mapped[int] = mapped_column(Integer, default=0)
    reserved_tokens: Mapped[int] = mapped_column(Integer, default=0)
    uncertain_tokens: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class AIModelCallModel(Base):
    """一行对应一次供应商请求，不是一次聊天；重试必须创建新的调用记录。"""

    __tablename__ = "ai_model_calls"
    __table_args__ = (Index("ix_ai_calls_user_time", "user_id", "created_at"),
                     Index("ix_ai_calls_pending", "user_id", "status", "expires_at"))
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(80))
    run_id: Mapped[str] = mapped_column(String(100))
    day_key: Mapped[str] = mapped_column(String(20))
    source: Mapped[str] = mapped_column(String(60))
    provider: Mapped[str] = mapped_column(String(60))
    model: Mapped[str] = mapped_column(String(120))
    status: Mapped[str] = mapped_column(String(20))
    reserved_tokens: Mapped[int] = mapped_column(Integer)
    input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    total_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # 只记录错误类型/管理备注，不保存 Prompt、回答、密钥或持仓正文。
    reason: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    settled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class FundReportDocumentModel(Base):
    """公开季报的不可变版本；页码原文与数字保存在 SQL，检索向量保存在 Chroma。"""

    __tablename__ = "fund_report_documents"
    __table_args__ = (Index("ix_fund_report_period", "fund_code", "period"),)
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    fund_code: Mapped[str] = mapped_column(String(6))
    period: Mapped[str] = mapped_column(String(7))
    title: Mapped[str] = mapped_column(String(250))
    published_at: Mapped[str] = mapped_column(String(10))
    source_url: Mapped[str] = mapped_column(Text)
    content_hash: Mapped[str] = mapped_column(String(64))
    # pages 保留 PDF 的物理页码；不把印刷页脚的页号当成 PDF 页码。
    pages: Mapped[list] = mapped_column(JSON)
    facts: Mapped[list] = mapped_column(JSON)
    index_version: Mapped[str | None] = mapped_column(String(80), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class PortfolioAllocationDraftModel(Base):
    """约束草案及一次确认结果；冻结输入复用压力沙盘，不复制行情或改写持仓。"""
    __tablename__ = "portfolio_allocation_drafts"
    __table_args__ = (
        UniqueConstraint("user_id", "idempotency_key", name="uq_allocation_idempotency"),
        Index("ix_allocation_history", "user_id", "portfolio_id", "created_at"),
    )
    user_id: Mapped[str] = mapped_column(String(50), primary_key=True)
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    portfolio_id: Mapped[str] = mapped_column(String(40))
    idempotency_key: Mapped[str] = mapped_column(String(100))
    request_hash: Mapped[str] = mapped_column(String(64))
    draft_hash: Mapped[str] = mapped_column(String(64))
    draft: Mapped[dict[str, Any]] = mapped_column(JSON)
    # NULL代表尚未确认；一次确认通过条件更新写入，重复请求只能读相同结果。
    confirmation_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class PortfolioStressRunModel(Base):
    """压力测试记录。原始运行冻结输入，后续方案引用它，不覆盖旧结果。"""

    __tablename__ = "portfolio_stress_runs"
    __table_args__ = (
        UniqueConstraint("user_id", "idempotency_key", name="uq_portfolio_stress_idempotency"),
        Index("ix_portfolio_stress_history", "user_id", "portfolio_id", "created_at"),
    )
    user_id: Mapped[str] = mapped_column(String(50), primary_key=True)
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    portfolio_id: Mapped[str] = mapped_column(String(40))
    # root_id 指向冻结行情的首次运行；比较记录的 frozen_inputs 为 NULL。
    root_id: Mapped[str] = mapped_column(String(40))
    idempotency_key: Mapped[str | None] = mapped_column(String(100), nullable=True)
    request_hash: Mapped[str] = mapped_column(String(64))
    input_hash: Mapped[str] = mapped_column(String(64))
    frozen_inputs: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    report: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class AppUserModel(Base):
    """FundScope 登录账号；业务表继续使用同一个稳定 user_id 做数据隔离。"""

    __tablename__ = "app_users"

    id: Mapped[str] = mapped_column(String(50), primary_key=True)
    display_name: Mapped[str] = mapped_column(String(80))
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(20), default="user")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class AuthSessionModel(Base):
    """服务端登录会话；浏览器只保存随机原始 Token，数据库只保存 SHA-256。"""

    __tablename__ = "auth_sessions"
    __table_args__ = (
        Index("ix_auth_sessions_user", "user_id", "expires_at"),
        Index("ix_auth_sessions_expires", "expires_at"),
    )

    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(50))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class AgentChatExecutionModel(Base):
    """聊天幂等记录；相同用户和 key 只会保存一份最终响应。"""

    __tablename__ = "agent_chat_executions"
    __table_args__ = (
        Index("ix_agent_chat_executions_status", "status", "updated_at"),
    )

    user_id: Mapped[str] = mapped_column(String(50), primary_key=True)
    idempotency_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    conversation_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="running")
    response_payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class BackgroundJobModel(Base):
    """一条真实后台运行记录。

    ``user_id + id`` 是联合主键，因此不同用户可以使用相同的可读 Job ID。
    进度、结果、错误和取消标记全部进入数据库，FastAPI 重启后仍然可以查询。
    """

    __tablename__ = "background_jobs"
    __table_args__ = (
        # 幂等键只要求在同一个用户范围内唯一，不同用户可使用相同业务键。
        UniqueConstraint(
            "user_id",
            "idempotency_key",
            name="uq_background_jobs_user_idempotency_key",
        ),
        # 列表页经常按用户查询最近 Job；Worker/运维则常按 status 查询。
        Index("ix_background_jobs_user_updated", "user_id", "updated_at"),
        Index("ix_background_jobs_status", "status"),
    )

    # 身份字段：联合主键同时完成用户隔离和 Job 唯一定位。
    user_id: Mapped[str] = mapped_column(String(50), primary_key=True)
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    title: Mapped[str] = mapped_column(String(200))

    # 工作流与状态字段：job_type 决定 JobRunner 走哪个分支。
    # demo_steps 用于确定性测试；其余类型执行 Agent 或 Team Harness 工作流。
    job_type: Mapped[str] = mapped_column(String(32), default="demo_steps")
    status: Mapped[str] = mapped_column(String(20))
    execution_backend: Mapped[str] = mapped_column(String(20), default="local")

    # 进度字段既给 REST 查询，也给 SSE 轮询和 Prompt Builder 使用。
    current_step: Mapped[int] = mapped_column(Integer, default=0)
    total_steps: Mapped[int] = mapped_column(Integer, default=1)
    progress_message: Mapped[str] = mapped_column(Text, default="")

    # payload 是 Worker 的输入；result 是 Worker 的结构化输出。
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    # 可靠性字段：取消、重试和超时都属于 Harness 控制面，而不是模型自行决定。
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    max_retries: Mapped[int] = mapped_column(Integer, default=0)
    timeout_seconds: Mapped[int] = mapped_column(Integer, default=300)

    # idempotency_key 防止浏览器重试或网络抖动造成重复提交。
    idempotency_key: Mapped[str | None] = mapped_column(String(100), nullable=True)
    # celery_task_id 是队列系统的任务 ID；id 才是本项目自己的业务 Job ID。
    celery_task_id: Mapped[str | None] = mapped_column(String(100), nullable=True)

    # 四个时间分别回答：何时创建、最近何时变化、首次何时运行、何时进入终态。
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class FundNavSnapshotModel(Base):
    """从外部数据源同步下来的一条基金日净值。

    一只基金每天只有一个正式单位净值，因此使用 ``fund_code + nav_date`` 做联合主键。
    定时任务重复抓取同一天时会更新原记录，而不会越积越多形成重复数据。
    """

    __tablename__ = "fund_nav_snapshots"
    __table_args__ = (
        # 基金详情和风险计算都会按基金代码、日期顺序读取历史净值。
        Index("ix_fund_nav_snapshots_fund_date", "fund_code", "nav_date"),
        # 运维状态页通过 fetched_at 判断最近一次成功同步发生在什么时候。
        Index("ix_fund_nav_snapshots_fetched", "fetched_at"),
    )

    fund_code: Mapped[str] = mapped_column(String(6), primary_key=True)
    nav_date: Mapped[date] = mapped_column(Date, primary_key=True)
    unit_nav: Mapped[float] = mapped_column(Float)
    daily_growth_rate_pct: Mapped[float | None] = mapped_column(Float, nullable=True)

    # source/source_url 让每一个数值都能追溯到采集口径，而不是只剩一个裸数字。
    source: Mapped[str] = mapped_column(String(120))
    source_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class FundCorporateActionModel(Base):
    """一只基金发生的一次现金分红或份额折算。

    单位净值会在除息或折算日机械下降，仅比较前后单位净值会把这类变化误判为亏损。
    因此这里把影响收益口径的公司行为单独保存，风险计算再据此重建总回报序列。
    """

    __tablename__ = "fund_corporate_actions"
    __table_args__ = (
        Index(
            "ix_fund_corporate_actions_fund_date",
            "fund_code",
            "action_date",
        ),
        Index("ix_fund_corporate_actions_fetched", "fetched_at"),
    )

    # 同一天可能既有分红又有折算，因此 action_type 也是联合主键的一部分。
    fund_code: Mapped[str] = mapped_column(String(6), primary_key=True)
    action_date: Mapped[date] = mapped_column(Date, primary_key=True)
    action_type: Mapped[str] = mapped_column(String(20), primary_key=True)
    cash_dividend_per_unit: Mapped[float | None] = mapped_column(
        Float,
        nullable=True,
    )
    # 2.0 表示每 1 份旧份额折算成 2 份新份额；无折算时保持 NULL。
    split_ratio: Mapped[float | None] = mapped_column(Float, nullable=True)
    description: Mapped[str] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(160))
    source_url: Mapped[str] = mapped_column(String(500))
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class FundCorporateActionSyncModel(Base):
    """记录一只基金的公司行为明细已经核对到哪个日期。

    明细表为空可能表示“确实没有分红”，也可能表示“从未同步”。只有本表覆盖统计区间
    时，计算层才可以把空明细解释成该区间没有公司行为。
    """

    __tablename__ = "fund_corporate_action_syncs"
    __table_args__ = (
        Index("ix_fund_corporate_action_syncs_fetched", "fetched_at"),
    )

    fund_code: Mapped[str] = mapped_column(String(6), primary_key=True)
    coverage_start: Mapped[date] = mapped_column(Date)
    coverage_end: Mapped[date] = mapped_column(Date)
    action_count: Mapped[int] = mapped_column(Integer, default=0)
    source: Mapped[str] = mapped_column(String(160))
    source_url: Mapped[str] = mapped_column(String(500))
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class FundBenchmarkReturnModel(Base):
    """一只基金业绩比较基准在某个交易日的收益率。

    基金档案中的 benchmark 只是文字公式，例如“沪深300×95%+活期存款×5%”。
    风险计算不能直接拿这段文字做数学，因此同步层先把各指数分量组合成日收益，
    再按 ``fund_code + benchmark_date`` 保存成可复现的事实序列。
    """

    __tablename__ = "fund_benchmark_returns"
    __table_args__ = (
        Index(
            "ix_fund_benchmark_returns_fund_date",
            "fund_code",
            "benchmark_date",
        ),
        Index("ix_fund_benchmark_returns_fetched", "fetched_at"),
    )

    fund_code: Mapped[str] = mapped_column(String(6), primary_key=True)
    benchmark_date: Mapped[date] = mapped_column(Date, primary_key=True)
    # 这里保存百分数：0.25 表示当日上涨 0.25%，与基金净值表的字段单位一致。
    daily_return_pct: Mapped[float] = mapped_column(Float)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class FundBenchmarkSyncModel(Base):
    """记录业绩基准的公式、来源、质量等级和连续覆盖范围。

    明细为空既可能是“不支持这个基准”，也可能是“从未同步”。独立元数据表让 API
    可以明确展示 unavailable，而不是把缺失数据伪装成 0% 基准收益。
    """

    __tablename__ = "fund_benchmark_syncs"
    __table_args__ = (
        Index("ix_fund_benchmark_syncs_fetched", "fetched_at"),
        Index("ix_fund_benchmark_syncs_status", "benchmark_status"),
    )

    fund_code: Mapped[str] = mapped_column(String(6), primary_key=True)
    benchmark_name: Mapped[str] = mapped_column(String(500))
    # matched 表示公式中的指数分量均已匹配；proxy 表示含固定存款利率等透明近似。
    benchmark_status: Mapped[str] = mapped_column(String(20))
    methodology: Mapped[str] = mapped_column(Text)
    coverage_start: Mapped[date | None] = mapped_column(Date, nullable=True)
    coverage_end: Mapped[date | None] = mapped_column(Date, nullable=True)
    record_count: Mapped[int] = mapped_column(Integer, default=0)
    source: Mapped[str | None] = mapped_column(String(500), nullable=True)
    source_urls: Mapped[list[str]] = mapped_column(JSON, default=list)
    warnings: Mapped[list[str]] = mapped_column(JSON, default=list)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class FundSecurityHoldingModel(Base):
    """基金定期报告披露的一只底层股票或债券。

    ``weight_pct`` 表示这只证券占基金净值的百分比，例如 6.5 表示 6.5%。
    同一只基金会保留多个报告期，查询层只取最新一期；这样以后仍能比较持仓变化。
    """

    __tablename__ = "fund_security_holdings"
    __table_args__ = (
        Index(
            "ix_fund_security_holdings_fund_report",
            "fund_code",
            "report_date",
        ),
        Index("ix_fund_security_holdings_fetched", "fetched_at"),
    )

    # 四列联合主键允许同一证券出现在不同基金、报告期和资产类型中。
    fund_code: Mapped[str] = mapped_column(String(6), primary_key=True)
    report_date: Mapped[date] = mapped_column(Date, primary_key=True)
    asset_type: Mapped[str] = mapped_column(String(10), primary_key=True)
    security_code: Mapped[str] = mapped_column(String(30), primary_key=True)
    security_name: Mapped[str] = mapped_column(String(160))
    weight_pct: Mapped[float] = mapped_column(Float)
    market_value_10k: Mapped[float | None] = mapped_column(Float, nullable=True)
    rank: Mapped[int] = mapped_column(Integer)

    # 每条披露都保留来源和抓取时间，Agent 引用时可以回到原始页面核验。
    source: Mapped[str] = mapped_column(String(160))
    source_url: Mapped[str] = mapped_column(String(500))
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class FundIndustryAllocationModel(Base):
    """基金定期报告中的股票行业配置。

    行业配置通常比“前十大股票”覆盖更完整，因此组合行业暴露使用本表计算；
    底层证券集中度则继续使用 ``fund_security_holdings``，两种口径不会混在一起。
    """

    __tablename__ = "fund_industry_allocations"
    __table_args__ = (
        Index(
            "ix_fund_industry_allocations_fund_report",
            "fund_code",
            "report_date",
        ),
        Index("ix_fund_industry_allocations_fetched", "fetched_at"),
    )

    fund_code: Mapped[str] = mapped_column(String(6), primary_key=True)
    report_date: Mapped[date] = mapped_column(Date, primary_key=True)
    industry_name: Mapped[str] = mapped_column(String(160), primary_key=True)
    weight_pct: Mapped[float] = mapped_column(Float)
    market_value_10k: Mapped[float | None] = mapped_column(Float, nullable=True)
    rank: Mapped[int] = mapped_column(Integer)
    source: Mapped[str] = mapped_column(String(160))
    source_url: Mapped[str] = mapped_column(String(500))
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class FundDisclosureSyncModel(Base):
    """一只基金最近一次披露同步的元数据。

    某些纯股票基金没有债券行，某些联接基金只有很少直接股票。单靠明细表为空无法
    判断“尚未同步”还是“确实没有披露”，因此单独用这张表记录每类数据的最新报告期。
    """

    __tablename__ = "fund_disclosure_syncs"
    __table_args__ = (
        Index("ix_fund_disclosure_syncs_fetched", "fetched_at"),
    )

    fund_code: Mapped[str] = mapped_column(String(6), primary_key=True)
    stock_report_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    bond_report_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    industry_report_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    source: Mapped[str] = mapped_column(String(160))
    source_url: Mapped[str] = mapped_column(String(500))
    warnings: Mapped[list[str]] = mapped_column(JSON, default=list)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class FundCatalogEntryModel(Base):
    """基金目录中的一条主数据记录。

    净值表回答“这只基金每天值多少”，本表回答“这只基金是谁、是否已经导入”。
    搜索只需保存轻量名称和类型；真正导入后，profile 会补齐基金公司、经理、
    业绩基准等详情。把两类数据分开后，搜索 2 万多只基金不会复制历史净值。
    """

    __tablename__ = "fund_catalog_entries"
    __table_args__ = (
        Index("ix_fund_catalog_short_name", "short_name"),
        Index("ix_fund_catalog_fund_type", "fund_type"),
        Index("ix_fund_catalog_import_status", "import_status"),
        Index("ix_fund_catalog_updated", "updated_at"),
    )

    fund_code: Mapped[str] = mapped_column(String(6), primary_key=True)
    short_name: Mapped[str] = mapped_column(String(160))
    full_name: Mapped[str | None] = mapped_column(String(500), nullable=True)
    fund_type: Mapped[str] = mapped_column(String(100))
    pinyin_abbreviation: Mapped[str] = mapped_column(String(100), default="")
    pinyin_name: Mapped[str] = mapped_column(String(500), default="")

    # discovered 表示只在目录中见过；ready 表示详情和历史净值都已落库。
    import_status: Mapped[str] = mapped_column(String(20), default="discovered")
    profile: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    source: Mapped[str] = mapped_column(String(160))
    source_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    discovered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    profile_fetched_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    nav_fetched_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class RetailIndexCacheModel(Base):
    """外部民间指数的最近一次有效快照，不保存作者或个股名单。

    payload 保留带日期的指数曲线和来源；失败只更新 error，不覆盖可追溯旧值。
    抓取时间与指数数据日期分开，避免把今天下载的旧数据说成今日行情。
    """

    __tablename__ = "retail_index_cache"

    indicator_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    attempted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    error: Mapped[str] = mapped_column(Text, default="")


class MarketIntradayCacheModel(Base):
    """手动获取的指数盘中快照，与日线研究证据分开保存。

    payload 保留每个指数的行情时间、抓取时间和错误；部分失败不会清空成功记录。
    attempted_at 同时用于数据库级刷新节流，让多个 API 进程共享请求间隔。
    """

    __tablename__ = "market_intraday_cache"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    attempted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class MarketTurnoverPointModel(Base):
    """一次成功采样的成交额，用于回看当天的累计成交曲线。

    同一序列、同一来源时间只存一个点；抓取时间单独保存，不能冒充行情时间。
    trade_date 按北京时间归档，避免把昨天下午与今天上午连接成一条线。
    """

    __tablename__ = "market_turnover_points"

    series_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    quote_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    trade_date: Mapped[date] = mapped_column(Date, index=True)
    amount_cny: Mapped[float] = mapped_column(Float)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    security_count: Mapped[int | None] = mapped_column(Integer, nullable=True)


class MarketSnapshotModel(Base):
    """某个交易日收盘后的市场宽度和成交活跃度原始快照。

    这张表服务全市场总览；各宽基指数的日线行情保存在 market_index_bars，
    两类数据可以独立刷新，也不会把单个指数的变化误当成全市场宽度。
    """

    __tablename__ = "market_snapshots"
    __table_args__ = (
        Index("ix_market_snapshots_fetched", "fetched_at"),
    )

    # 一天只保留一份规范化市场快照，重复执行 Cron 会更新同一行。
    data_date: Mapped[date] = mapped_column(Date, primary_key=True)
    advancing_count: Mapped[int] = mapped_column(Integer)
    declining_count: Mapped[int] = mapped_column(Integer)
    flat_count: Mapped[int] = mapped_column(Integer)
    suspended_count: Mapped[int] = mapped_column(Integer)
    limit_up_count: Mapped[int] = mapped_column(Integer)
    limit_down_count: Mapped[int] = mapped_column(Integer)
    market_activity_pct: Mapped[float] = mapped_column(Float)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    breadth_source: Mapped[str] = mapped_column(String(120))
    breadth_source_url: Mapped[str] = mapped_column(String(500))
    benchmark_code: Mapped[str] = mapped_column(String(20))
    benchmark_name: Mapped[str] = mapped_column(String(80))
    # 最近 5 个交易日成交额天然是一个小型有序数组，JSON 比拆成额外表更直观。
    daily_amounts: Mapped[list[dict[str, Any]]] = mapped_column(JSON)
    turnover_source: Mapped[str] = mapped_column(String(120))
    turnover_source_url: Mapped[str] = mapped_column(String(500))
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class MarketIndexBarModel(Base):
    """一个宽基指数在某个交易日的原始行情。

    这里保存事实数据，不保存“偏强、偏弱”等分析标签。分析标签由 Service 根据
    最近 20/60 个交易日动态计算，后续调整阈值时不需要重新采集历史行情。
    """

    __tablename__ = "market_index_bars"
    __table_args__ = (
        Index("ix_market_index_bars_date", "data_date"),
        Index("ix_market_index_bars_fetched", "fetched_at"),
    )

    # 复合主键保证同一个指数在同一个交易日只保留一条规范化记录。
    index_code: Mapped[str] = mapped_column(String(20), primary_key=True)
    data_date: Mapped[date] = mapped_column(Date, primary_key=True)
    index_name: Mapped[str] = mapped_column(String(80))
    market_segment: Mapped[str] = mapped_column(String(40))
    provider_symbol: Mapped[str] = mapped_column(String(20))
    close: Mapped[float] = mapped_column(Float)
    # 不同指数的成交量单位不适合横向比较，只用于该指数自身与 5 日均量比较。
    volume: Mapped[float] = mapped_column(Float)
    source: Mapped[str] = mapped_column(String(120))
    source_url: Mapped[str] = mapped_column(String(500))
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class EtfCapitalSnapshotModel(Base):
    """一只重点 ETF 在某个交易日的份额与资金结构快照。

    ETF 份额变化是公开可复算的资金行为线索，但它不能证明申购者身份。这里保存
    Provider 返回的原始事实和价格口径；1/5/20 日估算流入由 Service 动态计算，
    避免以后调整窗口或阈值时重写历史数据。
    """

    __tablename__ = "etf_capital_snapshots"
    __table_args__ = (
        Index("ix_etf_capital_snapshots_date", "data_date"),
        Index("ix_etf_capital_snapshots_topic", "topic_code", "data_date"),
        Index("ix_etf_capital_snapshots_fetched", "fetched_at"),
    )

    # 同一 ETF 每个交易日只保留一份规范化快照，重复同步会更新原记录。
    etf_code: Mapped[str] = mapped_column(String(6), primary_key=True)
    data_date: Mapped[date] = mapped_column(Date, primary_key=True)
    etf_name: Mapped[str] = mapped_column(String(120))
    exchange: Mapped[str] = mapped_column(String(10))
    topic_code: Mapped[str] = mapped_column(String(40))
    topic_label: Mapped[str] = mapped_column(String(60))
    topic_kind: Mapped[str] = mapped_column(String(20))

    # 历史交易所份额缺少同日价格时，会用最新价格做代理并显式记录 price_basis。
    unit_price: Mapped[float] = mapped_column(Float)
    price_basis: Mapped[str] = mapped_column(String(30))
    shares: Mapped[float] = mapped_column(Float)
    estimated_scale_cny: Mapped[float] = mapped_column(Float)
    turnover_cny: Mapped[float | None] = mapped_column(Float, nullable=True)
    provider_main_net_inflow_cny: Mapped[float | None] = mapped_column(
        Float,
        nullable=True,
    )

    # 来源等级只评价数据可追溯性，不代表资金方向判断一定正确。
    source_level: Mapped[str] = mapped_column(String(1))
    source: Mapped[str] = mapped_column(String(160))
    source_url: Mapped[str] = mapped_column(String(1000))
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class InstitutionalTradeModel(Base):
    """龙虎榜中一只证券在某日的机构席位买卖汇总。

    交易所公开信息只统一显示“机构专用”，不能据此识别具体基金、保险或国家队。
    本表因此只保存机构席位事实，不保存任何由模型猜测出的机构身份。
    """

    __tablename__ = "institutional_trades"
    __table_args__ = (
        Index("ix_institutional_trades_date", "trade_date"),
        Index("ix_institutional_trades_fetched", "fetched_at"),
    )

    stock_code: Mapped[str] = mapped_column(String(10), primary_key=True)
    trade_date: Mapped[date] = mapped_column(Date, primary_key=True)
    stock_name: Mapped[str] = mapped_column(String(120))
    close: Mapped[float | None] = mapped_column(Float, nullable=True)
    daily_return_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    buyer_count: Mapped[int] = mapped_column(Integer)
    seller_count: Mapped[int] = mapped_column(Integer)
    buy_cny: Mapped[float] = mapped_column(Float)
    sell_cny: Mapped[float] = mapped_column(Float)
    net_buy_cny: Mapped[float] = mapped_column(Float)
    market_turnover_cny: Mapped[float | None] = mapped_column(Float, nullable=True)
    net_buy_ratio_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    turnover_rate_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    listing_reason: Mapped[str] = mapped_column(Text)
    source_level: Mapped[str] = mapped_column(String(1))
    source: Mapped[str] = mapped_column(String(160))
    source_url: Mapped[str] = mapped_column(String(1000))
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class MarketEventModel(Base):
    """清洗完成后的一条市场事件或基金公告。

    第三方网页字段只在 Provider 内出现；进入数据库后统一使用这套结构。这样 REST、
    Agent Tool 和前端不需要知道事件最初来自 CCTV 还是基金公告接口。
    """

    __tablename__ = "market_events"
    __table_args__ = (
        # 同一份来源内容即使换了采集入口，也只能保存一次。
        UniqueConstraint("content_hash", name="uq_market_events_content_hash"),
        Index("ix_market_events_event_date", "event_date"),
        Index("ix_market_events_fetched", "fetched_at"),
    )

    # Provider 生成稳定 event_id；重复执行每日同步会更新原记录而不是无限追加。
    event_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    event_date: Mapped[date] = mapped_column(Date)
    published_date: Mapped[date] = mapped_column(Date)
    event_type: Mapped[str] = mapped_column(String(30))
    title: Mapped[str] = mapped_column(String(500))
    summary: Mapped[str] = mapped_column(Text)

    # 当前真实采集阶段不让规则猜利好利空，因此新增记录默认是 neutral。
    impact_direction: Mapped[str] = mapped_column(String(20), default="neutral")
    impact_horizon: Mapped[str] = mapped_column(String(20), default="short_term")
    impact_reason: Mapped[str] = mapped_column(Text)
    affected_fund_codes: Mapped[list[str]] = mapped_column(JSON, default=list)
    tags: Mapped[list[str]] = mapped_column(JSON, default=list)

    # 来源、URL 和稳定哈希构成事件证据的最小可追溯信息。
    source_name: Mapped[str] = mapped_column(String(160))
    source_level: Mapped[str] = mapped_column(String(1))
    source_grade_reason: Mapped[str] = mapped_column(String(300))
    source_url: Mapped[str] = mapped_column(String(1000))
    content_hash: Mapped[str] = mapped_column(String(71))
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class MarketTopicKeywordExpansionModel(Base):
    """一个关注板块对应的可复用采集关键词缓存。

    固定词典仍由代码版本控制；DeepSeek 只补充候选词。两部分和最终采用词表一起保存，
    方便后台任务复用，也方便前端解释本轮采集到底使用了哪些关键词。
    """

    __tablename__ = "market_topic_keyword_expansions"
    __table_args__ = (
        Index("ix_market_topic_expansions_expanded", "expanded_at"),
    )

    # topic 是规范化后的用户输入，例如“半导体”。同一主题只保留当前有效版本。
    topic: Mapped[str] = mapped_column(String(40), primary_key=True)
    # input_hash 同时覆盖固定词典和 Prompt 版本；任一变化都会让旧缓存自动失效。
    input_hash: Mapped[str] = mapped_column(String(71))
    dictionary_terms: Mapped[list[str]] = mapped_column(JSON, default=list)
    model_terms: Mapped[list[str]] = mapped_column(JSON, default=list)
    final_terms: Mapped[list[str]] = mapped_column(JSON, default=list)
    model_name: Mapped[str] = mapped_column(String(120))
    prompt_version: Mapped[str] = mapped_column(String(40))
    expanded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class SocialInvestmentPostModel(Base):
    """从社交平台采集的一条公开投资观点原文。

    原始帖子只保存结构化分析所需的必要字段，图片证据单独保存，视频暂不处理。作者的自述操作仍然
    是未经验证的用户内容；后续模型分析和群体聚合都不能修改这张原始证据表。
    """

    __tablename__ = "social_investment_posts"
    __table_args__ = (
        UniqueConstraint(
            "content_hash",
            name="uq_social_investment_posts_content_hash",
        ),
        Index("ix_social_investment_posts_published", "published_at"),
        Index("ix_social_investment_posts_author", "author_id", "published_at"),
        Index("ix_social_investment_posts_fetched", "fetched_at"),
    )

    # 平台笔记 ID 在同一来源内稳定，可用于增量更新而不是每天重复插入。
    post_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    platform: Mapped[str] = mapped_column(String(30), default="xiaohongshu")
    author_id: Mapped[str] = mapped_column(String(120))
    author_name: Mapped[str] = mapped_column(String(160))
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    title: Mapped[str] = mapped_column(String(500))
    # 最长文本由 Provider 在入库前裁剪；不镜像整篇页面。
    content_excerpt: Mapped[str] = mapped_column(Text)
    source_url: Mapped[str] = mapped_column(String(1200))
    matched_keyword: Mapped[str] = mapped_column(String(160))
    like_count: Mapped[int] = mapped_column(Integer, default=0)
    comment_count: Mapped[int] = mapped_column(Integer, default=0)
    collect_count: Mapped[int] = mapped_column(Integer, default=0)
    content_hash: Mapped[str] = mapped_column(String(71))
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class SocialSignalAnalysisModel(Base):
    """DeepSeek 对一条社交帖子做出的结构化“作者自述”抽取。

    这里记录的是作者声称做了什么，不验证其是否真实成交，也不把文本转换成交易建议。
    `input_hash` 不匹配时表示原帖内容发生变化，需要重新分析。
    """

    __tablename__ = "social_signal_analyses"
    __table_args__ = (
        Index("ix_social_signal_analyses_status", "status"),
        Index("ix_social_signal_analyses_analyzed", "analyzed_at"),
    )

    post_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    input_hash: Mapped[str] = mapped_column(String(71))
    status: Mapped[str] = mapped_column(String(20))
    model_name: Mapped[str] = mapped_column(String(120))
    prompt_version: Mapped[str] = mapped_column(String(40))
    fund_codes: Mapped[list[str]] = mapped_column(JSON, default=list)
    fund_names: Mapped[list[str]] = mapped_column(JSON, default=list)
    themes: Mapped[list[str]] = mapped_column(JSON, default=list)
    action: Mapped[str] = mapped_column(String(20))
    action_scope: Mapped[str] = mapped_column(String(20))
    position_text: Mapped[str] = mapped_column(Text, default="")
    rationale: Mapped[str] = mapped_column(Text, default="")
    is_self_reported: Mapped[bool] = mapped_column(Boolean, default=False)
    commercial_likelihood: Mapped[float] = mapped_column(Float, default=0.0)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    uncertainties: Mapped[list[str]] = mapped_column(JSON, default=list)
    # 保留模型原始判断和未纳入统计的原因，不再将“没通过校验”等同于“没有信息”。
    details: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    analyzed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class SocialPostMediaModel(Base):
    """一张公开帖子图片的本地证据及 OCR 结果。图片文件不存进数据库。"""

    __tablename__ = "social_post_media"
    media_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    post_id: Mapped[str] = mapped_column(String(100), index=True)
    image_index: Mapped[int] = mapped_column(Integer)
    source_url: Mapped[str] = mapped_column(Text)
    storage_name: Mapped[str] = mapped_column(String(80))
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # 包含识别状态、逐行文字/置信度、失败原因；失败不伪装成无文字。
    recognition: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class SocialSignalReportModel(Base):
    """已生成的社区综合解读，GET 页面仅读取，不隐式调用付费模型。"""

    __tablename__ = "social_signal_reports"
    report_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    input_hash: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class MarketEventSemanticAnalysisModel(Base):
    """一条市场事件对应的独立模型语义分析。

    原始事件和模型观点分表保存：模型可以被重新运行、替换或弃权，但不能修改原始证据。
    `input_hash` 用来判断原事件内容是否变化，变化后旧分析不再被视为有效。
    """

    __tablename__ = "market_event_semantic_analyses"
    __table_args__ = (
        Index("ix_market_event_analyses_status", "status"),
        Index("ix_market_event_analyses_analyzed", "analyzed_at"),
    )

    event_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    input_hash: Mapped[str] = mapped_column(String(71))
    status: Mapped[str] = mapped_column(String(20))
    model_name: Mapped[str] = mapped_column(String(120))
    prompt_version: Mapped[str] = mapped_column(String(40))

    # 下面是 DeepSeek 的结构化输出，先经 Pydantic 和业务规则校验再保存。
    entities: Mapped[list[str]] = mapped_column(JSON, default=list)
    themes: Mapped[list[str]] = mapped_column(JSON, default=list)
    direction: Mapped[str] = mapped_column(String(20))
    horizon: Mapped[str] = mapped_column(String(20))
    severity: Mapped[int] = mapped_column(Integer)
    relevance: Mapped[str] = mapped_column(String(20))
    confidence: Mapped[float] = mapped_column(Float)
    reason: Mapped[str] = mapped_column(Text)
    affected_fund_codes: Mapped[list[str]] = mapped_column(JSON, default=list)
    uncertainties: Mapped[list[str]] = mapped_column(JSON, default=list)
    analyzed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class MarketEventEmbeddingModel(Base):
    """一条市场事件对应的可复用向量缓存。

    `input_hash + embedding_model` 都匹配时可以直接复用向量；原文变化或切换模型时，
    聚类服务只重新计算受影响的事件，不必每次为全部历史新闻支付 Embedding 成本。
    """

    __tablename__ = "market_event_embeddings"
    __table_args__ = (
        Index("ix_market_event_embeddings_model", "embedding_model"),
        Index("ix_market_event_embeddings_embedded", "embedded_at"),
    )

    event_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    input_hash: Mapped[str] = mapped_column(String(71))
    embedding_model: Mapped[str] = mapped_column(String(120))
    dimensions: Mapped[int] = mapped_column(Integer)
    # 教学项目事件量很小，JSON 可以直观看到向量缓存；大规模系统通常改用向量数据库。
    vector: Mapped[list[float]] = mapped_column(JSON)
    embedded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class MarketEventClusterModel(Base):
    """语义相似事件组成的一个逻辑事件簇。"""

    __tablename__ = "market_event_clusters"
    __table_args__ = (
        Index("ix_market_event_clusters_heat", "heat_score"),
        Index("ix_market_event_clusters_generated", "generated_at"),
    )

    cluster_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    # anchor 只用于生成稳定 cluster_id；representative 是当前最适合展示和计分的成员。
    anchor_event_id: Mapped[str] = mapped_column(String(80))
    representative_event_id: Mapped[str] = mapped_column(String(80))
    member_count: Mapped[int] = mapped_column(Integer)
    source_count: Mapped[int] = mapped_column(Integer)
    source_names: Mapped[list[str]] = mapped_column(JSON, default=list)
    source_levels: Mapped[list[str]] = mapped_column(JSON, default=list)
    event_start: Mapped[date] = mapped_column(Date)
    event_end: Mapped[date] = mapped_column(Date)
    heat_score: Mapped[float] = mapped_column(Float)
    heat_level: Mapped[str] = mapped_column(String(20))
    algorithm_version: Mapped[str] = mapped_column(String(40))
    embedding_model: Mapped[str] = mapped_column(String(120))
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class MarketHotspotSnapshotModel(Base):
    """一次市场情报刷新时保存的热点状态。

    ``market_event_clusters`` 只保存最新聚类，会在重建时整体替换；这张表按
    ``cluster_id + observed_at`` 保留历史，因此可以回答热点是在升温还是降温。
    快照只复制展示和趋势计算需要的字段，原始新闻正文仍以 market_events 为准。
    """

    __tablename__ = "market_hotspot_snapshots"
    __table_args__ = (
        Index("ix_market_hotspot_snapshots_observed", "observed_at"),
        Index(
            "ix_market_hotspot_snapshots_cluster_observed",
            "cluster_id",
            "observed_at",
        ),
        Index("ix_market_hotspot_snapshots_heat", "heat_score"),
    )

    # 复合主键让同一次聚类重试不会为同一事件簇写出两条完全相同的快照。
    cluster_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        primary_key=True,
    )
    data_cutoff: Mapped[date] = mapped_column(Date)
    representative_event_id: Mapped[str] = mapped_column(String(80))
    title: Mapped[str] = mapped_column(String(500))
    summary: Mapped[str] = mapped_column(Text)
    event_start: Mapped[date] = mapped_column(Date)
    event_end: Mapped[date] = mapped_column(Date)
    impact_direction: Mapped[str] = mapped_column(String(20))
    impact_horizon: Mapped[str] = mapped_column(String(20))
    member_count: Mapped[int] = mapped_column(Integer)
    source_count: Mapped[int] = mapped_column(Integer)
    source_names: Mapped[list[str]] = mapped_column(JSON, default=list)
    source_levels: Mapped[list[str]] = mapped_column(JSON, default=list)
    affected_fund_codes: Mapped[list[str]] = mapped_column(JSON, default=list)
    themes: Mapped[list[str]] = mapped_column(JSON, default=list)
    # evidence_items 保存事件 ID、来源和链接，历史趋势仍能回到真实证据。
    evidence_items: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    heat_score: Mapped[float] = mapped_column(Float)
    heat_level: Mapped[str] = mapped_column(String(20))
    freshness_status: Mapped[str] = mapped_column(String(20))
    freshness_score: Mapped[float] = mapped_column(Float)
    algorithm_version: Mapped[str] = mapped_column(String(40))


class MarketEventClusterMemberModel(Base):
    """记录一条原始事件属于哪个事件簇，以及它与代表事件的相似度。"""

    __tablename__ = "market_event_cluster_members"
    __table_args__ = (
        Index("ix_market_event_cluster_members_cluster", "cluster_id"),
    )

    event_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    cluster_id: Mapped[str] = mapped_column(String(80))
    input_hash: Mapped[str] = mapped_column(String(71))
    similarity: Mapped[float] = mapped_column(Float)
    is_representative: Mapped[bool] = mapped_column(Boolean, default=False)


class AgentTeammateModel(Base):
    """s15 Agent Teams 中一个长期存在的队友。

    Subagent 只在一次调用中临时存在；Teammate 则拥有稳定名称、角色和状态。
    API 与 Celery Worker 都通过数据库读取这张表，因此它不依赖某个 Python 进程的内存。
    """

    __tablename__ = "agent_teammates"
    __table_args__ = (
        # name 是消息寻址使用的稳定名称，因此同一用户下必须唯一。
        UniqueConstraint(
            "user_id",
            "name",
            name="uq_agent_teammates_user_name",
        ),
        Index("ix_agent_teammates_user_updated", "user_id", "updated_at"),
    )

    # id 是数据库身份，name 是 Agent 互相发送消息时使用的逻辑身份。
    user_id: Mapped[str] = mapped_column(String(50), primary_key=True)
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(50))
    # role/instructions 进入 teammate system prompt；capabilities 决定实际绑定哪些工具。
    role: Mapped[str] = mapped_column(String(100))
    instructions: Mapped[str] = mapped_column(Text, default="")
    capabilities: Mapped[list[str]] = mapped_column(JSON, default=list)

    # 运行快照：idle 可接收工作；queued 已入队；working 正在运行；failed 保留错误。
    # current_job_id 把成员状态与 Background Job 的一次真实执行关联起来。
    status: Mapped[str] = mapped_column(String(20), default="idle")
    current_job_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_result: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class AgentTeamMessageModel(Base):
    """Agent Team Message Bus 中的一条普通文本消息。

    s15 只学习成员与消息传递，因此这里还没有 s16 的 approval、request_id、
    accepted/rejected 等协议字段。sender/recipient 可以是 teammate 名称，也可以是
    保留名称 ``main-agent``。
    """

    __tablename__ = "agent_team_messages"
    __table_args__ = (
        # inbox 索引服务于“某个接收者最近收到什么”；conversation 索引服务于
        # “main-agent 与某个 teammate 最近聊了什么”。
        Index(
            "ix_agent_team_messages_inbox",
            "user_id",
            "recipient",
            "created_at",
        ),
        Index(
            "ix_agent_team_messages_conversation",
            "user_id",
            "sender",
            "recipient",
        ),
    )

    # 这五个字段构成最小消息信封：租户、ID、发送者、接收者和正文。
    user_id: Mapped[str] = mapped_column(String(50), primary_key=True)
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    sender: Mapped[str] = mapped_column(String(50))
    recipient: Mapped[str] = mapped_column(String(50))
    content: Mapped[str] = mapped_column(Text)

    # job_id 把“消息”与“处理这条消息的后台 AgentRun”关联起来。
    # 回复消息使用 in_reply_to 指向原消息，既方便观察，也避免 Worker 重投后重复回复。
    job_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    in_reply_to: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # read_at=None 就是未读；用时间而非布尔值还能知道何时被消费。
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class AgentTeamProtocolModel(Base):
    """s16 Team Protocols 中一条持久化 ProtocolState。

    普通聊天只记录“谁说了什么”；协议状态还要回答：这是哪类请求、当前是否待处理、
    最终被批准还是拒绝。request_id 是请求和响应贯穿全链路的关联键。
    """

    __tablename__ = "agent_team_protocols"
    __table_args__ = (
        # 主 Agent 最常查看当前用户有哪些 pending 协议。
        Index(
            "ix_agent_team_protocols_user_status",
            "user_id",
            "status",
            "updated_at",
        ),
        # target 索引用于构造某个成员的结构化协议 inbox。
        Index(
            "ix_agent_team_protocols_target_status",
            "user_id",
            "target",
            "status",
        ),
    )

    user_id: Mapped[str] = mapped_column(String(50), primary_key=True)
    request_id: Mapped[str] = mapped_column(String(64), primary_key=True)

    # 当前教学版支持 shutdown 和 plan_approval 两种协议，共用同一状态机。
    protocol_type: Mapped[str] = mapped_column(String(32))
    sender: Mapped[str] = mapped_column(String(50))
    target: Mapped[str] = mapped_column(String(50))
    status: Mapped[str] = mapped_column(String(20), default="pending")
    payload: Mapped[str] = mapped_column(Text, default="")
    feedback: Mapped[str] = mapped_column(Text, default="")

    # 两个事件 ID 让 ProtocolState 能快速追溯原 request 和最终 response。
    request_event_id: Mapped[str] = mapped_column(String(64))
    response_event_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # shutdown 由内部 Background Job 分发；plan approval 通常不需要 Job。
    job_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    resolved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )


class AgentTeamProtocolEventModel(Base):
    """结构化协议 Message Bus 中的一条 request/response 事件。

    State 表保存“当前结论”，Event 表保存“发生过什么”。两者分开后，既能快速查询
    pending 请求，也能完整观察 request_id 是否在请求和响应之间保持一致。
    """

    __tablename__ = "agent_team_protocol_events"
    __table_args__ = (
        # 一个 request_id 对同一种 message_type 只能写一次，防止 Worker 重投重复响应。
        UniqueConstraint(
            "user_id",
            "request_id",
            "message_type",
            name="uq_agent_team_protocol_event_type",
        ),
        Index(
            "ix_agent_team_protocol_events_inbox",
            "user_id",
            "recipient",
            "created_at",
        ),
        Index(
            "ix_agent_team_protocol_events_request",
            "user_id",
            "request_id",
            "created_at",
        ),
    )

    user_id: Mapped[str] = mapped_column(String(50), primary_key=True)
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    request_id: Mapped[str] = mapped_column(String(64))
    message_type: Mapped[str] = mapped_column(String(40))
    sender: Mapped[str] = mapped_column(String(50))
    recipient: Mapped[str] = mapped_column(String(50))
    content: Mapped[str] = mapped_column(Text, default="")
    # request 事件没有 approve；response 事件使用 true/false 表示批准或拒绝。
    approve: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class AgentAutonomySessionModel(Base):
    """s17 中一个 teammate 的受控自治生命周期。

    Background Job 记录“一次 Worker 运行”；这张表记录自治业务状态，例如成员当前正在
    轮询还是执行哪条 Task。API 与 Worker 共享它，因此前端不需要读取 Worker 内存。
    """

    __tablename__ = "agent_autonomy_sessions"
    __table_args__ = (
        Index(
            "ix_agent_autonomy_sessions_user_status",
            "user_id",
            "status",
            "updated_at",
        ),
        Index(
            "ix_agent_autonomy_sessions_teammate",
            "user_id",
            "teammate_name",
            "updated_at",
        ),
    )

    user_id: Mapped[str] = mapped_column(String(50), primary_key=True)
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    teammate_name: Mapped[str] = mapped_column(String(50))
    job_id: Mapped[str] = mapped_column(String(64))

    # queued -> running -> idle/working 可反复切换，最后进入 completed/failed/cancelled。
    status: Mapped[str] = mapped_column(String(20), default="queued")
    current_task_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    completed_task_ids: Mapped[list[str]] = mapped_column(JSON, default=list)

    # 这些值构成自治预算，防止 Worker 永久轮询或无限领取任务。
    poll_interval_seconds: Mapped[float] = mapped_column(Float, default=2.0)
    max_idle_polls: Mapped[int] = mapped_column(Integer, default=5)
    idle_poll_count: Mapped[int] = mapped_column(Integer, default=0)
    max_tasks: Mapped[int] = mapped_column(Integer, default=5)
    tasks_completed: Mapped[int] = mapped_column(Integer, default=0)
    max_rounds_per_task: Mapped[int] = mapped_column(Integer, default=3)

    # stop_requested 是优雅停止信号；stop_reason 解释为什么退出。
    stop_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    stop_reason: Mapped[str | None] = mapped_column(String(40), nullable=True)
    last_event: Mapped[str] = mapped_column(Text, default="")
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AgentWorktreeModel(Base):
    """s18 中一个受控 Git worktree 的权威状态。

    Git 保存真正的分支和文件；这张表保存 Agent Harness 需要查询的业务信息，例如它绑定
    哪条 Task、当前保留还是已删除。路径永远由后端根据安全名称生成，不接受模型提供的
    任意绝对路径。
    """

    __tablename__ = "agent_worktrees"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "task_id",
            name="uq_agent_worktrees_user_task",
        ),
        Index("ix_agent_worktrees_user_status", "user_id", "status", "updated_at"),
    )

    user_id: Mapped[str] = mapped_column(String(50), primary_key=True)
    name: Mapped[str] = mapped_column(String(64), primary_key=True)
    task_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    branch_name: Mapped[str] = mapped_column(String(200))
    path: Mapped[str] = mapped_column(Text)
    repository_root: Mapped[str] = mapped_column(Text)
    base_ref: Mapped[str] = mapped_column(String(200), default="HEAD")
    base_commit: Mapped[str] = mapped_column(String(64))

    # active 可继续执行；kept 表示人工保留待 review；removed 表示目录和分支已清理。
    status: Mapped[str] = mapped_column(String(20), default="active")
    dirty_file_count: Mapped[int] = mapped_column(Integer, default=0)
    ahead_commit_count: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    removed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )


class AgentWorktreeEventModel(Base):
    """Worktree 创建、保留和删除事件，供前端与故障排查查看。"""

    __tablename__ = "agent_worktree_events"
    __table_args__ = (
        Index(
            "ix_agent_worktree_events_user_created",
            "user_id",
            "created_at",
        ),
        Index(
            "ix_agent_worktree_events_worktree",
            "user_id",
            "worktree_name",
            "created_at",
        ),
    )

    user_id: Mapped[str] = mapped_column(String(50), primary_key=True)
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    worktree_name: Mapped[str] = mapped_column(String(64))
    task_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    event_type: Mapped[str] = mapped_column(String(30))
    detail: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class AgentConversationModel(Base):
    """用户与主 Agent 的一个持久化会话。

    s09 Memory 保存的是可跨会话复用的事实；Conversation 保存的是一次聊天中的
    上下文。把两者分开后，用户可以切换会话，同时长期偏好仍然能够继续生效。
    """

    __tablename__ = "agent_conversations"
    __table_args__ = (
        Index(
            "ix_agent_conversations_user_updated",
            "user_id",
            "updated_at",
        ),
    )

    # 联合主键保证 conversation_id 只在当前用户空间内解析，避免跨用户读取。
    user_id: Mapped[str] = mapped_column(String(50), primary_key=True)
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    title: Mapped[str] = mapped_column(String(120), default="新对话")
    # 较早消息会按原意提取成滚动摘要；summary_message_count 记录已有多少条原始消息
    # 被摘要覆盖，避免下一次重复压缩同一批内容。
    rolling_summary: Mapped[str] = mapped_column(Text, default="")
    summary_message_count: Mapped[int] = mapped_column(Integer, default=0)
    summary_updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class AgentConversationMessageModel(Base):
    """会话中的一条用户消息或最终 Agent 回答。

    工具的完整中间输出仍由 Agent Trace 和 Context Compact 管理；会话表只保存用户真正
    需要回看的内容，以及一份轻量 run_metadata，避免数据库被大段工具结果撑大。
    """

    __tablename__ = "agent_conversation_messages"
    __table_args__ = (
        Index(
            "ix_agent_messages_conversation_created",
            "user_id",
            "conversation_id",
            "created_at",
        ),
    )

    user_id: Mapped[str] = mapped_column(String(50), primary_key=True)
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    conversation_id: Mapped[str] = mapped_column(String(64))
    role: Mapped[str] = mapped_column(String(20))
    content: Mapped[str] = mapped_column(Text)

    # assistant 消息通过 run_id 关联一次 Agent 执行，metadata 只保留可展示摘要。
    run_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    run_metadata: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class AgentResponseFeedbackModel(Base):
    """用户对一条 Agent 最终回答给出的持久化反馈。

    反馈表保存问题和回答快照，候选审核不必反查相邻消息；run_id 则关联
    AgentRunObservation，工具轨迹、Token 和耗时不在这里重复存储。用户删除会话时，
    ConversationService 会同步删除这些反馈快照。
    """

    __tablename__ = "agent_response_feedback"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "assistant_message_id",
            name="uq_agent_feedback_user_message",
        ),
        Index(
            "ix_agent_feedback_user_review",
            "user_id",
            "review_status",
            "updated_at",
        ),
    )

    user_id: Mapped[str] = mapped_column(String(50), primary_key=True)
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    conversation_id: Mapped[str] = mapped_column(String(64))
    assistant_message_id: Mapped[str] = mapped_column(String(64))
    run_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # helpful 只参与反馈统计；unhelpful 会进入人工审核候选队列。
    rating: Mapped[str] = mapped_column(String(20))
    reason_code: Mapped[str | None] = mapped_column(String(32), nullable=True)
    comment: Mapped[str] = mapped_column(Text, default="")
    correction: Mapped[str] = mapped_column(Text, default="")
    review_status: Mapped[str] = mapped_column(String(20), default="recorded")
    review_note: Mapped[str] = mapped_column(Text, default="")

    # 快照让候选案例不依赖会话表的生命周期，也便于人工判断是否值得进入评测集。
    question_snapshot: Mapped[str] = mapped_column(Text)
    answer_snapshot: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class AgentRunObservationModel(Base):
    """一次聊天或评测的结构化运行观测。

    Conversation 负责保存用户可回看的消息；这张表负责回答“用了多久、调用了什么、
    是否出错、消耗多少 token”。两者职责不同，因此不把观测字段继续塞进消息表。
    """

    __tablename__ = "agent_run_observations"
    __table_args__ = (
        Index("ix_agent_run_observations_user_created", "user_id", "created_at"),
        Index("ix_agent_run_observations_source", "source", "created_at"),
    )

    # run_id 是一次 Agent Loop 的唯一标识；user_id 继续承担数据隔离职责。
    user_id: Mapped[str] = mapped_column(String(50), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    conversation_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # source 区分真实聊天与自动评测；评测运行还会带上套件运行和场景 ID。
    source: Mapped[str] = mapped_column(String(20))
    eval_run_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    eval_case_id: Mapped[str | None] = mapped_column(String(80), nullable=True)

    # 问题和回答只保留受限长度的排障文本，完整回答仍由会话或评测结果管理。
    question: Mapped[str] = mapped_column(Text)
    answer_preview: Mapped[str] = mapped_column(Text, default="")
    model: Mapped[str] = mapped_column(String(100))
    stop_reason: Mapped[str] = mapped_column(String(50))

    # 这些字段支持延迟、成本、工具错误和证据引用等基础质量指标。
    duration_ms: Mapped[float] = mapped_column(Float, default=0.0)
    round_count: Mapped[int] = mapped_column(Integer, default=0)
    tool_calls: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    evidence_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    total_tokens: Mapped[int] = mapped_column(Integer, default=0)
    metrics: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class AgentEvalRunModel(Base):
    """一次版本化评测集的汇总结果。

    results 使用 JSON 保存每个场景的检查明细，便于教学项目直接查看；以后场景量很大时
    可以再拆成独立 case_results 表，不影响当前接口结构。
    """

    __tablename__ = "agent_eval_runs"
    __table_args__ = (
        Index("ix_agent_eval_runs_user_created", "user_id", "created_at"),
    )

    user_id: Mapped[str] = mapped_column(String(50), primary_key=True)
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    suite_id: Mapped[str] = mapped_column(String(80))
    suite_name: Mapped[str] = mapped_column(String(120))
    status: Mapped[str] = mapped_column(String(20))
    total_cases: Mapped[int] = mapped_column(Integer, default=0)
    passed_cases: Mapped[int] = mapped_column(Integer, default=0)
    failed_cases: Mapped[int] = mapped_column(Integer, default=0)
    score: Mapped[float] = mapped_column(Float, default=0.0)
    duration_ms: Mapped[float] = mapped_column(Float, default=0.0)
    total_tokens: Mapped[int] = mapped_column(Integer, default=0)
    results: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class FundPortfolioModel(Base):
    """FundScope 用户保存的一份模拟基金组合。"""

    __tablename__ = "fund_portfolios"
    __table_args__ = (
        Index("ix_fund_portfolios_user_updated", "user_id", "updated_at"),
    )

    # 组合 ID 只在当前用户空间内解析，和会话、Job 使用相同的用户隔离方式。
    user_id: Mapped[str] = mapped_column(String(50), primary_key=True)
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(80))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class FundPortfolioHoldingModel(Base):
    """组合当前持仓快照；真实持仓由交易流水重放后更新。"""

    __tablename__ = "fund_portfolio_holdings"
    __table_args__ = (
        Index(
            "ix_fund_portfolio_holdings_portfolio",
            "user_id",
            "portfolio_id",
        ),
    )

    # 三列联合主键天然阻止同一组合重复保存相同基金。
    user_id: Mapped[str] = mapped_column(String(50), primary_key=True)
    portfolio_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    fund_code: Mapped[str] = mapped_column(String(6), primary_key=True)
    weight_pct: Mapped[float] = mapped_column(Float)
    # allocation 兼容旧版目标权重；actual 表示份额与成本来自真实交易流水。
    position_mode: Mapped[str] = mapped_column(String(20), default="allocation")
    units: Mapped[float | None] = mapped_column(Float, nullable=True)
    cost_amount: Mapped[float | None] = mapped_column(Float, nullable=True)
    average_cost_nav: Mapped[float | None] = mapped_column(Float, nullable=True)
    first_purchase_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class FundPortfolioTransactionModel(Base):
    """真实持仓的一笔买入或卖出，是成本和份额计算的权威事实源。"""

    __tablename__ = "fund_portfolio_transactions"
    __table_args__ = (
        Index(
            "ix_fund_portfolio_transactions_portfolio_date",
            "user_id",
            "portfolio_id",
            "trade_date",
        ),
        Index(
            "ix_fund_portfolio_transactions_fund",
            "user_id",
            "portfolio_id",
            "fund_code",
            "trade_date",
        ),
        # txn_id 每次提交都会随机生成，不能识别 HTTP 重试；该业务唯一索引才是
        # 多 API 进程同时收到同一笔请求时的最终仲裁者。历史初始持仓使用 NULL，
        # SQLite/PostgreSQL 都允许唯一索引中存在多行 NULL。
        Index(
            "uq_fund_portfolio_transactions_idempotency",
            "user_id",
            "portfolio_id",
            "idempotency_key",
            unique=True,
        ),
    )

    user_id: Mapped[str] = mapped_column(String(50), primary_key=True)
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    portfolio_id: Mapped[str] = mapped_column(String(64))
    fund_code: Mapped[str] = mapped_column(String(6))
    transaction_type: Mapped[str] = mapped_column(String(10))
    trade_date: Mapped[date] = mapped_column(Date)
    units: Mapped[float] = mapped_column(Float)
    unit_price: Mapped[float] = mapped_column(Float)
    fee: Mapped[float] = mapped_column(Float, default=0.0)
    note: Mapped[str] = mapped_column(String(200), default="")
    idempotency_key: Mapped[str | None] = mapped_column(
        String(100),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # 保存原始下单时间和当时的推算口径，历史流水留空，不拿记账时间冒充下单时间。
    timing: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # 新流水的十进制事实源；原 FLOAT 列只保留为旧接口兼容投影。
    exact_values: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class PortfolioTradeOrderModel(Base):
    """外部申赎的本地登记，不会向任何基金销售平台发送订单。

    申请字段始终保留，确认结果通过 transaction_id 指向已入账流水。
    待赎回占用由 pending/cancel_requested 申请计算，撤单请求本身不会解除占用。
    """

    __tablename__ = "portfolio_trade_orders"
    __table_args__ = (
        Index("uq_portfolio_trade_order_key", "user_id", "portfolio_id", "idempotency_key", unique=True),
        Index("ix_portfolio_trade_order_state", "user_id", "portfolio_id", "status"),
    )
    user_id: Mapped[str] = mapped_column(String(50), primary_key=True)
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    portfolio_id: Mapped[str] = mapped_column(String(64))
    idempotency_key: Mapped[str] = mapped_column(String(100))
    fund_code: Mapped[str] = mapped_column(String(6))
    transaction_type: Mapped[str] = mapped_column(String(10))
    # 字符串保留申请金额/份额的十进制精度；空字段根据买入/卖出方向校验。
    requested_amount: Mapped[str | None] = mapped_column(String(40), nullable=True)
    requested_units: Mapped[str | None] = mapped_column(String(40), nullable=True)
    submitted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    channel: Mapped[str] = mapped_column(String(20), default="off_exchange")
    note: Mapped[str] = mapped_column(String(200), default="")
    status: Mapped[str] = mapped_column(String(24), default="pending")
    transaction_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    confirmation_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    settlement_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class PortfolioTradeOrderEventModel(Base):
    """只追加的状态事件，同时承担动作幂等记录，保留外部结果的人工确认依据。"""

    __tablename__ = "portfolio_trade_order_events"
    __table_args__ = (
        Index("uq_portfolio_trade_event_key", "user_id", "portfolio_id", "idempotency_key", unique=True),
        Index("ix_portfolio_trade_event_order", "user_id", "order_id", "created_at"),
    )
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(50))
    portfolio_id: Mapped[str] = mapped_column(String(64))
    order_id: Mapped[str] = mapped_column(String(64))
    idempotency_key: Mapped[str] = mapped_column(String(100))
    action: Mapped[str] = mapped_column(String(24))
    from_status: Mapped[str] = mapped_column(String(24))
    to_status: Mapped[str] = mapped_column(String(24))
    reason: Mapped[str] = mapped_column(String(300))
    payload: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class PortfolioTransactionCorrectionModel(Base):
    """原流水不可删除；冲正/更正以独立记录关联原流水和可选的新流水。

    成本投影只重放尚未被更正的流水。审计列表保留全部版本，不把账务更正冒充外部撤单。
    """

    __tablename__ = "portfolio_transaction_corrections"
    __table_args__ = (
        Index("uq_portfolio_correction_original", "user_id", "transaction_id", unique=True),
        Index("uq_portfolio_correction_key", "user_id", "portfolio_id", "idempotency_key", unique=True),
    )
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(50))
    portfolio_id: Mapped[str] = mapped_column(String(64))
    transaction_id: Mapped[str] = mapped_column(String(64))
    replacement_transaction_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    idempotency_key: Mapped[str] = mapped_column(String(100))
    reason: Mapped[str] = mapped_column(String(300))
    payload: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class PortfolioDecisionModel(Base):
    """用户基于一份组合研究做出的投资决策及当时证据快照。

    snapshot 保存“做决定那一刻”看到的指标、事件证据和 Agent 报告摘要。后续复盘
    必须读这份历史快照，不能拿今天已经变化的数据冒充当时的依据。
    """

    __tablename__ = "portfolio_decisions"
    __table_args__ = (
        Index(
            "ix_portfolio_decisions_portfolio_date",
            "user_id",
            "portfolio_id",
            "decision_date",
        ),
        Index(
            "ix_portfolio_decisions_target_date",
            "user_id",
            "target_date",
        ),
    )

    user_id: Mapped[str] = mapped_column(String(50), primary_key=True)
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    portfolio_id: Mapped[str] = mapped_column(String(64))
    decision_date: Mapped[date] = mapped_column(Date)
    target_date: Mapped[date] = mapped_column(Date)
    action: Mapped[str] = mapped_column(String(20))
    thesis: Mapped[str] = mapped_column(Text)
    horizon_days: Mapped[int] = mapped_column(Integer)
    research_job_id: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
    )
    snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    review_note: Mapped[str] = mapped_column(Text, default="")
    reviewed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class PortfolioResearchInputSnapshotModel(Base):
    """一次组合研究实际消费的不可变输入包。

    Background Job 和 LangGraph checkpoint 负责保存“执行到了哪里”；本表保存
    “这次研究到底使用了哪批数据”。同一个 run_id 只能对应一行，Service 也不提供
    update/delete，因此 Worker 重试时只能读取原快照，不能悄悄换成最新行情。
    """

    __tablename__ = "portfolio_research_input_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "run_id",
            name="uq_portfolio_research_snapshots_user_run",
        ),
        Index(
            "ix_portfolio_research_snapshots_portfolio_captured",
            "user_id",
            "portfolio_id",
            "captured_at",
        ),
        Index(
            "ix_portfolio_research_snapshots_content_hash",
            "content_hash",
        ),
    )

    user_id: Mapped[str] = mapped_column(String(50), primary_key=True)
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(64))
    portfolio_id: Mapped[str] = mapped_column(String(64))
    period_months: Mapped[int] = mapped_column(Integer)

    # schema_version 描述快照 JSON 契约；graph_version 描述消费它的工作流版本。
    schema_version: Mapped[str] = mapped_column(String(50))
    graph_version: Mapped[str] = mapped_column(String(50))
    content_hash: Mapped[str] = mapped_column(String(64))

    # capture_started_at/captured_at 公开采集窗口，避免把多源读取伪装成一个原子时刻。
    capture_started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    # source_manifest 是可公开的日期与版本摘要；payload 才是节点消费的完整冻结输入。
    source_manifest: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class PortfolioDailyBriefingPolicyModel(Base):
    """每份组合的自动简报开关。运行和报告复用 BackgroundJob，不另建一套任务表。"""

    __tablename__ = "portfolio_daily_briefing_policies"
    user_id: Mapped[str] = mapped_column(String(50), primary_key=True)
    portfolio_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    last_error: Mapped[str | None] = mapped_column(String(200), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class PortfolioMonitoringPolicyModel(Base):
    """用户为一份模拟组合保存的持续监控策略。

    风险偏好和投资期限交给模型解释变化时参考；真正决定是否触发预警的阈值仍由
    Python 读取下面这些明确字段，模型不能修改它们。
    """

    __tablename__ = "portfolio_monitoring_policies"
    __table_args__ = (
        Index(
            "ix_portfolio_monitoring_policies_enabled",
            "enabled",
            "updated_at",
        ),
    )

    user_id: Mapped[str] = mapped_column(String(50), primary_key=True)
    portfolio_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    risk_preference: Mapped[str] = mapped_column(String(20), default="balanced")
    investment_horizon: Mapped[str] = mapped_column(
        String(30),
        default="one_to_three_years",
    )
    monitor_months: Mapped[int] = mapped_column(Integer, default=12)
    max_single_adjustment_pct: Mapped[float] = mapped_column(Float, default=15.0)

    # 三个数值阈值与一个布尔开关构成第一版确定性触发规则。
    drawdown_alert_pct_points: Mapped[float] = mapped_column(Float, default=3.0)
    concentration_alert_pct_points: Mapped[float] = mapped_column(
        Float,
        default=5.0,
    )
    major_event_heat_threshold: Mapped[float] = mapped_column(Float, default=70.0)
    # 一条风险事件至少影响组合多少仓位，才值得生成持仓级事件预警。
    event_impact_exposure_threshold_pct: Mapped[float] = mapped_column(
        Float,
        default=5.0,
    )
    alert_on_risk_state_worsening: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
    )

    # Cron 定义保存在原有 Scheduler 中；表里保存稳定 ID 和用户设置的执行时间。
    schedule_cron: Mapped[str] = mapped_column(String(100), default="0 21 * * 1-5")
    schedule_id: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class PortfolioRiskSnapshotModel(Base):
    """一次组合监控运行保存的可比较风险快照。"""

    __tablename__ = "portfolio_risk_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "run_id",
            name="uq_portfolio_risk_snapshots_user_run",
        ),
        Index(
            "ix_portfolio_risk_snapshots_portfolio_observed",
            "user_id",
            "portfolio_id",
            "observed_at",
        ),
    )

    user_id: Mapped[str] = mapped_column(String(50), primary_key=True)
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    portfolio_id: Mapped[str] = mapped_column(String(64))
    run_id: Mapped[str] = mapped_column(String(64))
    period_months: Mapped[int] = mapped_column(Integer)
    assessment_date: Mapped[date] = mapped_column(Date)
    state_code: Mapped[str] = mapped_column(String(30))
    state_label: Mapped[str] = mapped_column(String(80))
    caution_points: Mapped[float] = mapped_column(Float)
    supportive_points: Mapped[float] = mapped_column(Float)
    annualized_volatility_pct: Mapped[float] = mapped_column(Float)
    max_drawdown_pct: Mapped[float] = mapped_column(Float)
    daily_cvar_95_pct: Mapped[float] = mapped_column(Float)
    concentration_hhi: Mapped[float] = mapped_column(Float)
    max_weight_pct: Mapped[float] = mapped_column(Float)

    # 只保留达到当前策略阈值的有效风险事件，供下一次运行识别“新事件”。
    major_events: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    # 保存事件与真实持仓的可验证关联，下一次运行可比较新增事件和仓位变化。
    event_impacts: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    source_summary: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    comparison: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    generation: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class PortfolioAlertModel(Base):
    """Python 规则触发的一条组合预警，以及模型可选的通俗解释。"""

    __tablename__ = "portfolio_alerts"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "snapshot_id",
            "alert_type",
            name="uq_portfolio_alert_snapshot_type",
        ),
        Index(
            "ix_portfolio_alerts_portfolio_status",
            "user_id",
            "portfolio_id",
            "status",
            "created_at",
        ),
    )

    user_id: Mapped[str] = mapped_column(String(50), primary_key=True)
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    portfolio_id: Mapped[str] = mapped_column(String(64))
    snapshot_id: Mapped[str] = mapped_column(String(64))
    alert_type: Mapped[str] = mapped_column(String(40))
    severity: Mapped[str] = mapped_column(String(20))
    title: Mapped[str] = mapped_column(String(160))
    detail: Mapped[str] = mapped_column(Text)
    metric_data: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    # explanation 只改善表达，不参与 alert_type/severity/title/detail 的规则判断。
    explanation: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="open")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    acknowledged_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
