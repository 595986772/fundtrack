"""工程化 Background Job / AgentRun 服务。

s13 最初版本把 Job 和线程都保存在一个字典里。本版保留相同的公开接口，但拆成：

- JobStore：状态存在哪里（内存、SQLite、PostgreSQL）；
- JobExecutor：任务在哪里执行（本地线程、Celery Worker）；
- JobRunner：具体执行 demo_steps 或真实 agent_prompt 工作流；
- BackgroundJobService：校验参数、创建记录、查询、取消和 Prompt 索引。

因此 REST、Agent Tools 和 Cron 不需要跟着基础设施变化。
"""

from __future__ import annotations

from datetime import datetime
import re
from typing import Any, Literal
from uuid import uuid4

from app.config import settings
from app.services.tracing import capture_trace_context, context_trace_id, set_trace_attributes, traced
from app.services.ai_governance_context import AIControlError
from app.database import runtime_database
from app.services.agent_execution_context import AgentExecutionContext, current_agent_execution_context
from app.services.job_executor import CeleryJobExecutor, JobExecutor, LocalJobExecutor
from app.services.job_runtime import (
    PORTFOLIO_JOB_TYPES, TASK_GRANT_FIELDS, job_authorization_identity,
    job_portfolio_ids, restore_job_execution_context,
)
from app.services.job_store import (
    BackgroundJobRecord,
    BackgroundJobStatus,
    InMemoryJobStore,
    JobStore,
    SqlJobStore,
    TERMINAL_JOB_STATUSES,
    utc_now,
)


# Python 不支持在 import 列表里声明普通常量；这里单独维护允许的状态集合。
JOB_STATUSES = {
    "queued",
    "running",
    "waiting_input",
    "completed",
    "failed",
    "cancelled",
}
# Team/Autonomy 类型是内部工作流，不直接把内部引用交给公共 Background Job REST/Tool。
JOB_TYPES = {
    "demo_steps",
    "agent_prompt",
    "fund_catalog_import",
    "fund_report_sync",
    "fund_report_eval",
    "fund_nav_sync",
    "market_snapshot_sync",
    "large_capital_sync",
    "market_event_sync",
    "market_event_analysis",
    "market_event_clustering",
    "social_signal_browser_login",
    "social_signal_sync",
    "social_signal_analysis",
    "market_intelligence_pipeline",
    "portfolio_research",
    "portfolio_adjustment",
    "portfolio_monitor",
    "teammate_message",
    "teammate_protocol",
    "autonomous_teammate",
}
JOB_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
USER_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,49}$")
IDEMPOTENCY_KEY_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,99}$")
PORTFOLIO_ID_PATTERN = re.compile(r"^portfolio_[a-f0-9]{16}$")
FUND_CODE_PATTERN = re.compile(r"^\d{6}$")
PORTFOLIO_RISK_PREFERENCES = {"conservative", "balanced", "growth"}
PORTFOLIO_INVESTMENT_HORIZONS = {
    "under_1y",
    "one_to_three_years",
    "over_3y",
}

MAX_JOBS_PER_USER = 100
MAX_TITLE_CHARS = 200
MAX_PROMPT_CHARS = 8000
MAX_TOTAL_STEPS = 20
MAX_STEP_DELAY_SECONDS = 5.0
MAX_JOB_RETRIES = 5
MAX_TIMEOUT_SECONDS = 3600


class BackgroundJobService:
    """Background Job 的应用服务，对外保持 s13 的调用方式。

    它位于 Router/Agent Tool 与 Store/Executor 中间：上层只表达“启动、查询、取消”，
    这里负责业务边界；至于 SQL 怎么写、任务在哪个进程跑，则交给注入的底层对象。
    """

    def __init__(
        self,
        *,
        store: JobStore | None = None,
        executor: JobExecutor | None = None,
    ) -> None:
        # 构造函数注入让测试可以使用内存 Store，也让全局服务可以使用 SQL Store。
        # 不传依赖时使用内存 + 本地线程，让旧单元测试继续完全隔离。
        self.store: JobStore = store or InMemoryJobStore()
        self.executor: JobExecutor = executor or LocalJobExecutor(self.store)
        self.delivery = None
        if isinstance(self.store, SqlJobStore):
            from app.services.job_delivery import JobDeliveryService
            self.delivery = JobDeliveryService(self.store, self.executor)

    @property
    def execution_backend(self) -> str:
        return self.executor.backend_name

    @traced("job.create")
    def start_job(
        self,
        *,
        user_id: str,
        title: str,
        total_steps: int = 8,
        step_delay_seconds: float = 0.4,
        job_id: str | None = None,
        job_type: str = "demo_steps",
        prompt: str | None = None,
        max_tool_rounds: int = 10,
        max_retries: int | None = None,
        timeout_seconds: int | None = None,
        idempotency_key: str | None = None,
        teammate_name: str | None = None,
        team_message_id: str | None = None,
        team_protocol_request_id: str | None = None,
        autonomy_session_id: str | None = None,
        portfolio_id: str | None = None,
        portfolio_months: int = 12,
        research_mode: str = "standard",
        research_question: str = "",
        research_allow_public_refresh: bool = False,
        research_investor_context: dict | None = None,
        source_research_job_id: str | None = None,
        fund_code: str | None = None,
        market_topics: list[str] | tuple[str, ...] | None = None,
        use_model_topic_expansion: bool = True,
        social_scope: str = "window",
        eval_case_ids: list[str] | None = None,
        eval_retrieval_mode: str = "hybrid",
        execution_context: AgentExecutionContext | None = None,
        daily_briefing: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """持久化 Job 后交给执行器，并立即返回 job_id。

        关键顺序是：校验 -> 幂等查询 -> 写 queued 记录 -> 提交执行器 -> 保存 task_id。
        HTTP 请求只走完“建单和投递”，不会等待实际工作完成。
        """

        # 第一阶段：把所有外部输入规范化。后续 Store/Worker 只接触可信边界内的数据。
        normalized_user_id = self._normalize_user_id(user_id)
        # 普通 REST 和旧内部调用默认是产品模式。只有已经通过管理员入口创建的
        # AgentExecutionContext 才能把开发者能力带入异步 Worker。
        trusted_context = execution_context or AgentExecutionContext.product(
            user_id=normalized_user_id,
            source="background-job-default",
        )
        if trusted_context.user_id != normalized_user_id:
            raise ValueError("后台任务用户与可信执行身份不一致")
        active = current_agent_execution_context()
        parent_context = active if active.task_grant_id else (
            trusted_context if trusted_context.task_grant_id else None
        )
        if parent_context is not None:
            from app.services.agent_authorization import AgentAuthorizationError, get_task_authorization_service

            if parent_context.user_id != normalized_user_id or (
                trusted_context.task_grant_id and trusted_context.task_grant_id != parent_context.task_grant_id
            ):
                raise AgentAuthorizationError("task_grant_identity_mismatch", "不能切换父任务的授权身份。")
            get_task_authorization_service().check_context(parent_context)
            # 即使调用方传入普通 product，也不能抹掉当前父授权。
            trusted_context = parent_context
        normalized_title = self._normalize_title(title)
        normalized_job_id = (
            self._normalize_job_id(job_id)
            if job_id
            else f"job-{uuid4().hex[:10]}"
        )
        normalized_job_type = self._normalize_job_type(job_type)
        from app.services.portfolio_research_plan import build_research_plan
        research_plan = build_research_plan(research_question, research_mode,
            allow_public_refresh=research_allow_public_refresh, investor_context=research_investor_context)
        if normalized_job_type != "portfolio_research" and (research_mode != "standard" or research_question
                or research_allow_public_refresh or research_investor_context):
            raise ValueError("只有 portfolio_research 任务可以指定研究计划。")
        research_mode = research_plan["mode"]
        # 该字段只由后端的简报服务构造，通用 REST/Agent 参数不暴露它。
        if daily_briefing is not None and normalized_job_type != "portfolio_research":
            raise ValueError("每日简报必须使用组合研究任务。")
        if daily_briefing is not None and research_mode != "standard":
            raise ValueError("每日简报只能使用 standard 模式。")
        # 专项评测只能由管理员API或可信服务端发起，普通产品工具不能创建付费评测。
        if normalized_job_type == "fund_report_eval":
            if trusted_context.role not in {"admin", "system"}:
                raise ValueError("季报评测需要管理员身份。")
            if eval_retrieval_mode not in {"hybrid", "keyword"}:
                raise ValueError("不支持的评测检索模式。")
        if social_scope not in {"window", "all"}:
            raise ValueError("不支持的社区分析范围")
        normalized_idempotency_key = self._normalize_idempotency_key(idempotency_key)

        committee = research_mode == "committee"
        # 委员会默认不做普通失败重试；失联接管仍由可靠任务的租约和 checkpoint 处理。
        safe_retries = self._normalize_max_retries(
            0 if committee and max_retries is None else max_retries
        )
        safe_timeout = self._normalize_timeout(
            900 if committee and timeout_seconds is None else timeout_seconds
        )
        # 组合研究按所选模式的业务节点展示进度。
        if normalized_job_type == "portfolio_research":
            safe_total_steps = research_plan["total_steps"]
        elif normalized_job_type == "portfolio_adjustment":
            # 整理、人工确认、候选生成、确定性场景验证和报告装配共五个节点。
            safe_total_steps = 5
        elif normalized_job_type == "portfolio_monitor":
            # 规则证据、量化风险、持仓事件归因和持久化共四个阶段。
            safe_total_steps = 4
        elif normalized_job_type == "market_intelligence_pipeline":
            # 事件采集、语义复核、聚类、持仓归因和监控预警共五个阶段。
            safe_total_steps = 5
        elif normalized_job_type == "social_signal_browser_login":
            # 打开独立浏览器、等待用户登录并保存本机会话，共两个可观察阶段。
            safe_total_steps = 2
        elif normalized_job_type in {
            "agent_prompt",
            "fund_catalog_import",
            "fund_report_sync",
            "fund_nav_sync",
            "market_snapshot_sync",
            "large_capital_sync",
            "market_event_sync",
            "market_event_analysis",
            "market_event_clustering",
            "social_signal_sync",
            "social_signal_analysis",
            "teammate_message",
            "teammate_protocol",
            "autonomous_teammate",
        }:
            safe_total_steps = 3
        else:
            safe_total_steps = self._normalize_total_steps(total_steps)
        safe_delay = self._normalize_step_delay(step_delay_seconds)
        safe_prompt = self._normalize_prompt(prompt or normalized_title)
        safe_tool_rounds = max(1, min(int(max_tool_rounds), 10))
        safe_teammate_name = None
        safe_team_message_id = None
        safe_team_protocol_request_id = None
        safe_autonomy_session_id = None
        safe_portfolio_id = None
        safe_portfolio_months = None
        safe_source_research_job_id = None
        safe_fund_code = None
        safe_market_topics: list[str] = []
        # teammate_message 只能由 AgentTeamService 内部创建，需要额外的成员和消息引用。
        if normalized_job_type in {
            "teammate_message",
            "teammate_protocol",
            "autonomous_teammate",
        }:
            safe_teammate_name = self._normalize_team_identifier(
                teammate_name,
                field_name="teammate_name",
                max_chars=50,
            )
        if normalized_job_type == "teammate_message":
            safe_team_message_id = self._normalize_team_identifier(
                team_message_id,
                field_name="team_message_id",
                max_chars=64,
            )
        if normalized_job_type == "teammate_protocol":
            safe_team_protocol_request_id = self._normalize_team_identifier(
                team_protocol_request_id,
                field_name="team_protocol_request_id",
                max_chars=64,
            )
        if normalized_job_type == "autonomous_teammate":
            safe_autonomy_session_id = self._normalize_team_identifier(
                autonomy_session_id,
                field_name="autonomy_session_id",
                max_chars=64,
            )
        if normalized_job_type in {
            "portfolio_research",
            "portfolio_adjustment",
            "portfolio_monitor",
        }:
            # 组合研究由领域 Router 创建；Worker 仍重新校验 payload，不能只信任 HTTP 层。
            safe_portfolio_id = self._normalize_portfolio_id(portfolio_id)
        elif normalized_job_type == "agent_prompt" and portfolio_id is not None:
            safe_portfolio_id = self._normalize_portfolio_id(portfolio_id)
        if normalized_job_type in {"portfolio_research", "portfolio_monitor"}:
            safe_portfolio_months = self._normalize_portfolio_months(
                portfolio_months
            )
        if normalized_job_type == "portfolio_adjustment":
            safe_source_research_job_id = self._normalize_job_id(
                source_research_job_id
            )
        if normalized_job_type in {"fund_catalog_import", "fund_report_sync"}:
            safe_fund_code = self._normalize_fund_code(fund_code)
        if normalized_job_type in {"market_event_sync", "market_intelligence_pipeline"}:
            safe_market_topics = self._normalize_market_topics(market_topics or [])
        elif market_topics:
            raise ValueError("只有市场事件同步任务可以携带 market_topics。")

        # 幂等命中也要检查原记录的组合与授权，不能把请求 A 当成读取 B 的通行证。
        if normalized_idempotency_key:
            existing = self.store.find_by_idempotency_key(
                user_id=normalized_user_id, idempotency_key=normalized_idempotency_key,
            )
            if existing is not None:
                self._check_idempotent_job(existing, normalized_job_type, safe_portfolio_id, trusted_context)
                self._check_research_plan(existing, research_plan, safe_portfolio_months)
                return {
                    "ok": True, "accepted": False,
                    "job": self._record_to_public_dict(existing),
                    "message": "检测到相同 idempotency_key，已返回原 Job。",
                }
        if self.store.count(user_id=normalized_user_id) >= MAX_JOBS_PER_USER:
            raise ValueError(f"每个用户最多保留 {MAX_JOBS_PER_USER} 条后台 Job。")

        selected_portfolios = [safe_portfolio_id] if safe_portfolio_id else []
        protected = normalized_job_type in PORTFOLIO_JOB_TYPES or (
            normalized_job_type == "agent_prompt" and trusted_context.mode == "product"
        )
        if parent_context is not None or (settings.agent_task_authorization_enabled and protected):
            from app.services.agent_authorization import get_task_authorization_service

            run_id, agent_id = job_authorization_identity(normalized_job_id, normalized_job_type)
            # create 同时验证用户归属和父范围，成功后才能保存、投递 Job。
            authorization = get_task_authorization_service()
            trusted_context = authorization.create(
                trusted_context, run_id=run_id, agent_id=agent_id,
                portfolio_ids=selected_portfolios, parent_context=parent_context,
            )
            action = "portfolio:research-run" if normalized_job_type in PORTFOLIO_JOB_TYPES else "portfolio:read"
            for selected in selected_portfolios:
                authorization.authorize(trusted_context, action=action, portfolio_id=selected)
        now = utc_now()

        # 由服务端构造并与 Job/Outbox 同事务保存。公开请求不接受这个内部字段，
        # 幂等重放返回旧任务，不会用新请求的追踪上下文覆盖原任务。
        trace_context = capture_trace_context()
        if trace_context:
            trace_context["attributes"].update({"job.id": normalized_job_id,
                "job.type": normalized_job_type, "research.mode": research_mode})
            set_trace_attributes(**{"job.id": normalized_job_id, "job.type": normalized_job_type,
                "langfuse.trace.metadata.job_id": normalized_job_id})

        # 第二阶段：构造执行契约。payload 是 Worker 输入，其他字段是控制面和观察面。
        record = BackgroundJobRecord(
            id=normalized_job_id,
            user_id=normalized_user_id,
            title=normalized_title,
            job_type=normalized_job_type,
            status="queued",
            execution_backend=self.execution_backend,
            current_step=0,
            total_steps=safe_total_steps,
            progress_message="已持久化，等待 Worker 领取。",
            payload={
                "_trace_context": trace_context,
                "eval_case_ids": eval_case_ids or [],
                "eval_retrieval_mode": eval_retrieval_mode,
                "social_scope": social_scope,
                "step_delay_seconds": safe_delay,
                "prompt": safe_prompt,
                "max_tool_rounds": safe_tool_rounds,
                # Worker 进程没有原 HTTP Session，只恢复服务端在建单时写入的模式。
                # 老 Job 没有该字段时会在 JobRunner 中安全降级为 product。
                "agent_mode": trusted_context.mode,
                **{key: getattr(trusted_context, key) for key in TASK_GRANT_FIELDS},
                "teammate_name": safe_teammate_name,
                "team_message_id": safe_team_message_id,
                "team_protocol_request_id": safe_team_protocol_request_id,
                "autonomy_session_id": safe_autonomy_session_id,
                "portfolio_id": safe_portfolio_id,
                "portfolio_ids": selected_portfolios,
                "portfolio_months": safe_portfolio_months,
                **({"research_mode": research_mode, "research_plan": research_plan}
                   if normalized_job_type == "portfolio_research" else {}),
                "source_research_job_id": safe_source_research_job_id,
                "fund_code": safe_fund_code,
                # 关注板块必须进入持久化 payload；Celery Worker 不会保留原 HTTP 请求。
                "market_topics": safe_market_topics,
                "use_model_topic_expansion": bool(use_model_topic_expansion),
                # 首次运行没有人工输入；resume_portfolio_adjustment 会填入该字段。
                "human_review": None,
                **({"daily_briefing": dict(daily_briefing)} if daily_briefing is not None else {}),
            },
            result=None,
            error=None,
            cancel_requested=False,
            attempt_count=0,
            max_retries=safe_retries,
            timeout_seconds=safe_timeout,
            idempotency_key=normalized_idempotency_key,
            celery_task_id=None,
            created_at=now,
            updated_at=now,
            started_at=None,
            completed_at=None,
        )
        # 第三阶段：必须先保存 queued 记录，再把 ID 发给执行器。
        # 这样 Worker 即使立刻启动，也一定能从共享数据库查到 payload。
        try:
            self.store.create(record)
        except (ValueError, AIControlError):
            # 两个相同请求可能在“查询幂等键”和“插入”之间并发到达。
            # 数据库唯一约束负责最终兜底；冲突后再查询一次并返回胜出的原 Job。
            if normalized_idempotency_key:
                existing = self.store.find_by_idempotency_key(
                    user_id=normalized_user_id,
                    idempotency_key=normalized_idempotency_key,
                )
                if existing is not None:
                    self._check_idempotent_job(existing, normalized_job_type, safe_portfolio_id, trusted_context)
                    self._check_research_plan(existing, research_plan, safe_portfolio_months)
                    return {
                        "ok": True,
                        "accepted": False,
                        "job": self._record_to_public_dict(existing),
                        "message": "并发幂等提交已合并，返回原 Job。",
                    }
            raise

        if (record.job_type == "portfolio_research" and self.delivery
                and self.delivery.repository.is_managed(normalized_user_id, normalized_job_id)):
            # 接受请求以 SQL 提交为准。Redis 不可用不丢任务，也不要求用户重新建单。
            self.delivery.wake()
            return {
                "ok": True, "accepted": True,
                "job": self._record_to_public_dict(record),
                "message": "任务与投递清单已保存，可通过原任务持续查看执行和恢复进度。",
            }

        # 第四阶段：其他工作流保留原提交方式，本轮不扩大自动恢复范围。
        try:
            task_id = self.executor.submit(
                user_id=normalized_user_id,
                job_id=normalized_job_id,
                timeout_seconds=safe_timeout,
            )
            # task_id 用于撤销队列消息和排障，不替代业务 job_id。
            submitted_record = self.store.update(
                user_id=normalized_user_id,
                job_id=normalized_job_id,
                changes={"celery_task_id": task_id},
            )
        except Exception as exc:
            # 队列不可用也必须形成可查询的失败记录，而不是只在 HTTP 日志里消失。
            self.store.update(
                user_id=normalized_user_id,
                job_id=normalized_job_id,
                allowed_statuses={"queued"},
                changes={
                    "status": "failed",
                    "error": str(exc),
                    "progress_message": "任务提交到执行器失败。",
                    "completed_at": utc_now(),
                },
            )
            raise RuntimeError(f"任务提交到 {self.execution_backend} 失败：{exc}") from exc

        # 返回刚提交后的快照，其中已经包含 executor task_id；它仍然不会等待工作完成。
        return {
            "ok": True,
            "accepted": True,
            "job": self._record_to_public_dict(submitted_record),
            "message": (
                f"Job 已提交到 {self.execution_backend} 执行器；"
                "主 Agent 无需等待，可通过状态接口或 SSE 跟踪。"
            ),
        }

    def get_job(self, *, user_id: str, job_id: str) -> dict[str, Any]:
        """读取一条权威 Job 状态，并转换成 API/Tool 共用的公开结构。"""

        key = self._normalize_key(user_id=user_id, job_id=job_id)
        record = self.store.get(user_id=key[0], job_id=key[1])
        self._authorize_record_access(record, current_agent_execution_context())
        return {"ok": True, "job": self._record_to_public_dict(record)}

    def list_jobs(
        self,
        *,
        user_id: str,
        status: str | None = None,
        max_results: int = 30,
    ) -> dict[str, Any]:
        """按用户和可选状态过滤，返回当前页数量与数据库总数量。"""

        normalized_user_id = self._normalize_user_id(user_id)
        normalized_status = self._normalize_status(status) if status else None
        safe_limit = max(1, min(int(max_results), 100))
        context = current_agent_execution_context()
        if context.task_grant_id:
            from app.services.agent_authorization import AgentAuthorizationError, get_task_authorization_service

            authorization = get_task_authorization_service()
            authorization.check_context(context)
            if context.user_id != normalized_user_id:
                raise AgentAuthorizationError("task_grant_identity_mismatch", "任务不能查询其他用户的 Job。")
        records, total = self.store.list(
            user_id=normalized_user_id,
            status=normalized_status,
            # 先过滤再分页，总数不能泄露其他组合的任务数量。
            limit=max(1, self.store.count(user_id=normalized_user_id)) if context.task_grant_id else safe_limit,
        )
        if context.task_grant_id:
            visible = []
            for record in records:
                try:
                    self._authorize_record_access(record, context)
                except AgentAuthorizationError as exc:
                    if exc.status_code != 403:
                        raise
                    continue
                visible.append(record)
            authorization.check_context(context)
            records, total = visible[:safe_limit], len(visible)
        return {
            "ok": True,
            "user_id": normalized_user_id,
            "count": len(records),
            "total_count": total,
            "jobs": [self._record_to_public_dict(record) for record in records],
        }

    def cancel_job(self, *, user_id: str, job_id: str) -> dict[str, Any]:
        """写入协作式取消标记，并撤销尚未领取的 Celery 消息。"""

        normalized_user_id, normalized_job_id = self._normalize_key(
            user_id=user_id,
            job_id=job_id,
        )
        current = self.store.get(user_id=normalized_user_id, job_id=normalized_job_id)
        self._authorize_record_access(current, current_agent_execution_context())
        # completed/failed/cancelled 都是终态，取消不能把历史结果倒退回 cancelled。
        if current.status in TERMINAL_JOB_STATUSES:
            return {
                "ok": False,
                "job": self._record_to_public_dict(current),
                "message": f"Job 已处于终态 {current.status}，不能再次取消。",
            }

        # 先把共享数据库设为 cancelled，再尝试 revoke。即使消息已经被 Worker 领取，
        # JobRunner 也会在下一检查点看到 cancel_requested 并停止。
        cancelled = self.store.update(
            user_id=normalized_user_id,
            job_id=normalized_job_id,
            allowed_statuses={"queued", "running", "waiting_input"},
            changes={
                "cancel_requested": True,
                "status": "cancelled",
                "progress_message": "已收到取消请求，Worker 将协作式停止。",
                "completed_at": utc_now(),
            },
        )
        self.executor.cancel(task_id=cancelled.celery_task_id)
        return {
            "ok": True,
            "job": self._record_to_public_dict(cancelled),
            "message": "取消信号已写入共享数据库。",
        }

    def resume_portfolio_adjustment(
        self,
        *,
        user_id: str,
        job_id: str,
        decision: str,
        risk_preference: str,
        investment_horizon: str,
        max_single_adjustment_pct: float,
        max_single_fund_weight_pct: float = 70,
        max_turnover_pct: float = 20,
        additional_constraints: str = "",
    ) -> dict[str, Any]:
        """保存人工约束，并把暂停的组合调整 Job 重新投递给 Worker。"""

        normalized_user_id, normalized_job_id = self._normalize_key(
            user_id=user_id,
            job_id=job_id,
        )
        current = self.store.get(
            user_id=normalized_user_id,
            job_id=normalized_job_id,
        )
        if current.job_type != "portfolio_adjustment":
            raise ValueError("只有组合调整 Job 可以提交人工确认。")
        if current.status != "waiting_input":
            raise ValueError("当前 Job 不在等待人工输入状态。")
        self._authorize_record_access(current, current_agent_execution_context())
        restore_job_execution_context(current)

        normalized_decision = str(decision or "").strip().lower()
        if normalized_decision not in {"continue", "cancel"}:
            raise ValueError("decision 只能是 continue 或 cancel。")
        normalized_risk = str(risk_preference or "").strip().lower()
        if normalized_risk not in PORTFOLIO_RISK_PREFERENCES:
            raise ValueError("risk_preference 格式非法。")
        normalized_horizon = str(investment_horizon or "").strip().lower()
        if normalized_horizon not in PORTFOLIO_INVESTMENT_HORIZONS:
            raise ValueError("investment_horizon 格式非法。")
        max_adjustment = float(max_single_adjustment_pct)
        if max_adjustment < 1 or max_adjustment > 50:
            raise ValueError("max_single_adjustment_pct 必须在 1-50 之间。")
        max_weight = float(max_single_fund_weight_pct)
        if max_weight < 10 or max_weight > 100:
            raise ValueError("max_single_fund_weight_pct 必须在 10-100 之间。")
        max_turnover = float(max_turnover_pct)
        if max_turnover < 0 or max_turnover > 100:
            raise ValueError("max_turnover_pct 必须在 0-100 之间。")
        constraints = str(additional_constraints or "").strip()
        if len(constraints) > 1000:
            raise ValueError("additional_constraints 最多 1000 个字符。")

        payload = dict(current.payload)
        payload["human_review"] = {
            "decision": normalized_decision,
            "risk_preference": normalized_risk,
            "investment_horizon": normalized_horizon,
            "max_single_adjustment_pct": round(max_adjustment, 2),
            "max_single_fund_weight_pct": round(max_weight, 2),
            "max_turnover_pct": round(max_turnover, 2),
            "additional_constraints": constraints,
        }
        queued = self.store.update(
            user_id=normalized_user_id,
            job_id=normalized_job_id,
            allowed_statuses={"waiting_input"},
            changes={
                "status": "queued",
                "payload": payload,
                "error": None,
                "progress_message": "人工约束已保存，等待 Worker 恢复 LangGraph。",
            },
        )

        try:
            task_id = self.executor.submit(
                user_id=normalized_user_id,
                job_id=normalized_job_id,
                timeout_seconds=queued.timeout_seconds,
            )
            submitted = self.store.update(
                user_id=normalized_user_id,
                job_id=normalized_job_id,
                allowed_statuses={"queued", "running"},
                changes={"celery_task_id": task_id},
            )
        except Exception as exc:
            self.store.update(
                user_id=normalized_user_id,
                job_id=normalized_job_id,
                allowed_statuses={"queued"},
                changes={
                    "status": "failed",
                    "error": str(exc),
                    "progress_message": "恢复任务提交到执行器失败。",
                    "completed_at": utc_now(),
                },
            )
            raise RuntimeError(
                f"组合调整恢复提交到 {self.execution_backend} 失败：{exc}"
            ) from exc

        return {
            "ok": True,
            "accepted": True,
            "job": self._record_to_public_dict(submitted),
            "message": "人工输入已保存，LangGraph 将从 interrupt 节点继续。",
        }

    def recent_index(self, *, user_id: str, max_items: int = 20) -> list[dict[str, Any]]:
        result = self.list_jobs(
            user_id=user_id,
            max_results=max(1, min(max_items, 50)),
        )
        return list(result["jobs"])

    def index_for_prompt(self, *, user_id: str, max_items: int = 10) -> str:
        """把完整 Job 记录压成少量文本，避免把大 result 全塞进 system prompt。"""

        jobs = self.recent_index(user_id=user_id, max_items=max_items)
        if not jobs:
            return "当前用户暂无后台 Job。"

        lines = ["当前用户后台 Job（最近更新优先）："]
        for job in jobs:
            result_hint = ""
            if job["status"] == "completed" and job.get("result"):
                result_hint = f", result={job['result'].get('summary', '')}"
            elif job["status"] == "failed":
                result_hint = f", error={job.get('error') or 'unknown'}"
            lines.append(
                "- "
                f"[{job['status']}] {job['id']}: {job['title']} "
                f"({job['current_step']}/{job['total_steps']}, "
                f"{job['progress_percent']}%{result_hint})"
            )
        return "\n".join(lines)

    def runtime_info(self) -> dict[str, Any]:
        """给前端展示当前是否真的使用了 Celery/PostgreSQL。"""

        database_kind = "sqlite" if runtime_database.url.startswith("sqlite") else "postgresql"
        return {
            "execution_backend": self.execution_backend,
            "database": database_kind,
            "database_url_masked": self._mask_database_url(runtime_database.url),
            "supported_job_types": sorted(JOB_TYPES),
            "sse_poll_seconds": settings.job_sse_poll_seconds,
            "reliable_research": {
                "enroll_new_jobs": settings.reliable_research_jobs_enabled,
                "delivery_loop_running": bool(self.delivery and self.delivery.running),
                "lease_seconds": settings.job_lease_seconds,
                "max_recoveries": settings.job_recovery_max_attempts,
            },
        }

    def start_reliable_delivery(self) -> None:
        if self.delivery:
            self.delivery.start()

    def shutdown(self, *, wait: bool = True, timeout: float = 2.0) -> None:
        if self.delivery:
            self.delivery.stop(timeout=timeout)
        self.executor.shutdown(wait=wait, timeout=timeout)

    def _record_to_public_dict(self, record: BackgroundJobRecord) -> dict[str, Any]:
        """集中定义对外字段，避免 Router、Tool 和 SSE 各自拼出不同结构。"""

        return {
            "id": record.id,
            "user_id": record.user_id,
            "title": record.title,
            "job_type": record.job_type,
            # 只公开资源与授权引用，不把 prompt、人工约束等内部输入整包交给 UI。
            "payload": {key: record.payload.get(key) for key in (
                "portfolio_id", "portfolio_ids", *TASK_GRANT_FIELDS,
            )},
            "status": record.status,
            "execution_backend": record.execution_backend,
            "current_step": record.current_step,
            "total_steps": record.total_steps,
            "progress_percent": round(
                record.current_step / max(1, record.total_steps) * 100,
                1,
            ),
            "progress_message": record.progress_message,
            "result": record.result,
            "error": record.error,
            "cancel_requested": record.cancel_requested,
            "attempt_count": record.attempt_count,
            "max_retries": record.max_retries,
            "timeout_seconds": record.timeout_seconds,
            "idempotency_key": record.idempotency_key,
            "celery_task_id": record.celery_task_id,
            "trace_id": context_trace_id(record.payload.get("_trace_context")),
            "created_at": self._iso(record.created_at),
            "updated_at": self._iso(record.updated_at),
            "started_at": self._iso(record.started_at),
            "completed_at": self._iso(record.completed_at),
            "recovery": (self.delivery.repository.summary(record.user_id, record.id)
                         if self.delivery and record.job_type == "portfolio_research" else None),
        }

    @staticmethod
    def _authorize_record_access(record: BackgroundJobRecord, context: AgentExecutionContext) -> None:
        if not context.task_grant_id:
            return
        from app.services.agent_authorization import AgentAuthorizationError, get_task_authorization_service

        service = get_task_authorization_service()
        service.check_context(context)
        if context.user_id != record.user_id:
            raise AgentAuthorizationError("task_grant_identity_mismatch", "任务不能访问其他用户的 Job。")
        portfolio_ids = job_portfolio_ids(record)
        if not portfolio_ids and not record.payload.get("task_grant_id"):
            raise AgentAuthorizationError("task_grant_missing", "该历史 Job 没有可确认的组合授权范围。")
        for portfolio_id in portfolio_ids:
            service.authorize(context, action="portfolio:read", portfolio_id=portfolio_id)
        restore_job_execution_context(record)
        service.check_context(context)

    def _check_idempotent_job(
        self, record: BackgroundJobRecord, job_type: str, portfolio_id: str | None,
        context: AgentExecutionContext,
    ) -> None:
        if record.job_type != job_type or record.payload.get("portfolio_id") != portfolio_id:
            from app.services.agent_authorization import AgentAuthorizationError

            raise AgentAuthorizationError("task_grant_scope_mismatch", "该幂等键已绑定另一种任务或组合，请使用新的幂等键。")
        self._authorize_record_access(record, context)
        restore_job_execution_context(record)

    def _normalize_key(self, *, user_id: str, job_id: str) -> tuple[str, str]:
        return self._normalize_user_id(user_id), self._normalize_job_id(job_id)

    @staticmethod
    def _check_research_plan(record, plan, months):
        if record.job_type != "portfolio_research":
            return
        from app.services.portfolio_research_plan import build_research_plan
        saved = record.payload.get("research_plan") or build_research_plan(mode=record.payload.get("research_mode", "standard"))
        if saved != plan or record.payload.get("portfolio_months") != months:
            raise ValueError("该幂等键已绑定不同研究问题、区间或刷新授权，请使用新请求。")

    def _normalize_user_id(self, user_id: str) -> str:
        value = str(user_id or "").strip()
        if not USER_ID_PATTERN.fullmatch(value):
            raise ValueError("user_id 只能包含字母、数字、下划线和连字符，最长 50 位。")
        return value

    def _normalize_job_id(self, job_id: str | None) -> str:
        value = str(job_id or "").strip()
        if not JOB_ID_PATTERN.fullmatch(value):
            raise ValueError("job_id 只能包含字母、数字、下划线和连字符，最长 64 位。")
        return value

    def _normalize_title(self, title: str) -> str:
        value = str(title or "").strip()
        if not value:
            raise ValueError("后台 Job 的 title 不能为空。")
        if len(value) > MAX_TITLE_CHARS:
            raise ValueError(f"后台 Job 的 title 最多 {MAX_TITLE_CHARS} 个字符。")
        return value

    def _normalize_prompt(self, prompt: str) -> str:
        value = str(prompt or "").strip()
        if not value:
            raise ValueError("agent_prompt 工作流的 prompt 不能为空。")
        if len(value) > MAX_PROMPT_CHARS:
            raise ValueError(f"prompt 最多 {MAX_PROMPT_CHARS} 个字符。")
        return value

    def _normalize_job_type(self, job_type: str) -> str:
        value = str(job_type or "").strip().lower()
        if value not in JOB_TYPES:
            raise ValueError(f"job_type 只能是：{', '.join(sorted(JOB_TYPES))}")
        return value

    def _normalize_fund_code(self, fund_code: str | None) -> str:
        """基金导入 Job 只接受标准 6 位公募基金代码。"""

        value = str(fund_code or "").strip()
        if not FUND_CODE_PATTERN.fullmatch(value):
            raise ValueError("fund_code 必须是 6 位数字。")
        return value

    def _normalize_status(self, status: str) -> BackgroundJobStatus:
        value = str(status or "").strip()
        if value not in JOB_STATUSES:
            raise ValueError(f"不支持的后台 Job 状态：{value}")
        return value  # type: ignore[return-value]

    def _normalize_total_steps(self, total_steps: int) -> int:
        value = int(total_steps)
        if value < 1 or value > MAX_TOTAL_STEPS:
            raise ValueError(f"total_steps 必须在 1-{MAX_TOTAL_STEPS} 之间。")
        return value

    def _normalize_step_delay(self, delay: float) -> float:
        value = float(delay)
        if value < 0.05 or value > MAX_STEP_DELAY_SECONDS:
            raise ValueError(
                f"step_delay_seconds 必须在 0.05-{MAX_STEP_DELAY_SECONDS} 之间。"
            )
        return value

    def _normalize_max_retries(self, value: int | None) -> int:
        retries = settings.job_default_max_retries if value is None else int(value)
        if retries < 0 or retries > MAX_JOB_RETRIES:
            raise ValueError(f"max_retries 必须在 0-{MAX_JOB_RETRIES} 之间。")
        return retries

    def _normalize_timeout(self, value: int | None) -> int:
        timeout = settings.job_default_timeout_seconds if value is None else int(value)
        if timeout < 1 or timeout > MAX_TIMEOUT_SECONDS:
            raise ValueError(f"timeout_seconds 必须在 1-{MAX_TIMEOUT_SECONDS} 之间。")
        return timeout

    def _normalize_idempotency_key(self, value: str | None) -> str | None:
        if value is None or not str(value).strip():
            return None
        normalized = str(value).strip()
        if not IDEMPOTENCY_KEY_PATTERN.fullmatch(normalized):
            raise ValueError(
                "idempotency_key 只能包含字母、数字、点、冒号、下划线和连字符，最长 100 位。"
            )
        return normalized

    def _normalize_team_identifier(
        self,
        value: str | None,
        *,
        field_name: str,
        max_chars: int,
    ) -> str:
        """校验 s15 内部 Job 用到的 teammate/message 引用。"""

        normalized = str(value or "").strip()
        pattern = rf"[A-Za-z0-9][A-Za-z0-9_-]{{0,{max_chars - 1}}}"
        if not re.fullmatch(pattern, normalized):
            raise ValueError(f"{field_name} 格式非法。")
        return normalized

    def _normalize_portfolio_id(self, value: str | None) -> str:
        """组合研究 Job 只接受 PortfolioService 生成的稳定 ID。"""

        normalized = str(value or "").strip()
        if not PORTFOLIO_ID_PATTERN.fullmatch(normalized):
            raise ValueError("portfolio_id 格式非法。")
        return normalized

    def _normalize_portfolio_months(self, value: int) -> int:
        """与组合研判 API 保持同一组观察区间。"""

        months = int(value)
        if months not in {6, 12, 36}:
            raise ValueError("portfolio_months 只能是 6、12 或 36。")
        return months

    @staticmethod
    def _normalize_market_topics(
        topics: list[str] | tuple[str, ...],
    ) -> list[str]:
        """复用领域服务的边界规则，但延迟导入以免 Job 基础设施反向加载模型。"""

        from app.services.market_topic_keywords import MarketTopicKeywordService

        try:
            return MarketTopicKeywordService.normalize_topics(topics)
        except RuntimeError as exc:
            raise ValueError(str(exc)) from exc

    def _iso(self, value: datetime | None) -> str | None:
        return value.isoformat(timespec="microseconds") if value is not None else None

    def _mask_database_url(self, value: str) -> str:
        # 只用于可观察接口；即使 URL 中带密码，也绝不能原样返回浏览器。
        if value.startswith("sqlite"):
            return "sqlite:///data/agent_runtime.db"
        if "@" not in value or "://" not in value:
            return value
        scheme, remainder = value.split("://", 1)
        host_part = remainder.split("@", 1)[1]
        return f"{scheme}://***:***@{host_part}"


def _build_global_service() -> BackgroundJobService:
    """根据环境变量组装 API 与 Worker 共用的工程化 Service。"""

    # 无论 local 还是 celery，全局运行服务都使用 SQL Store；差别只在执行位置。
    store = SqlJobStore(runtime_database)
    if settings.job_execution_mode == "celery":
        executor: JobExecutor = CeleryJobExecutor()
    elif settings.job_execution_mode == "local":
        executor = LocalJobExecutor(store)
    else:
        raise ValueError("JOB_EXECUTION_MODE 只能是 local 或 celery。")
    return BackgroundJobService(store=store, executor=executor)


# Router、Agent Tools、Cron 和 Agent Teams 共用这个应用级实例。
# Celery Worker 不复用进程内对象，而是按任务重新构造同数据库上的 Store/Runner。
background_job_service = _build_global_service()
