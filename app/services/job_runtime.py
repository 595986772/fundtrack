"""Background Job 的真实工作流执行器。

``JobRunner`` 不知道任务来自 HTTP、Cron、本地线程还是 Celery。它只做三件事：

1. 原子领取 queued Job；
2. 根据 job_type 执行具体工作流并持续写入进度；
3. 成功时把结构化结果保存到 Store。

异常重试由外层 Executor 管理，避免这里把“做什么”和“在哪里重试”混在一起。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from threading import Lock
import time
from typing import Any

from app.schemas import AgentChatRequest
from app.config import settings
from app.services.agent_execution_context import (
    AgentExecutionContext,
    agent_execution_scope,
    current_agent_execution_context,
)
from app.services.job_store import BackgroundJobRecord, JobStore, utc_now
from app.services.tracing import trace_span


PORTFOLIO_JOB_TYPES = frozenset({
    "portfolio_research", "portfolio_adjustment", "portfolio_monitor",
})
TASK_GRANT_FIELDS = ("task_grant_id", "task_run_id", "task_agent_id")


def job_authorization_identity(job_id: str, job_type: str) -> tuple[str, str]:
    from app.services.ai_governance_context import stable_run_id

    return stable_run_id("job", job_id), f"background:{job_type}"


def job_portfolio_ids(record: BackgroundJobRecord) -> list[str]:
    """只认建单时保存的组合，不从模型生成的 result 推断权限。"""

    values = list(record.payload.get("portfolio_ids") or [])
    if record.payload.get("portfolio_id"):
        values.append(record.payload["portfolio_id"])
    return sorted(set(values))


def restore_job_execution_context(record: BackgroundJobRecord) -> AgentExecutionContext:
    """Worker 只能恢复旧授权；缺少授权的历史任务必须由用户重新建单。"""

    has_grant = any(record.payload.get(key) for key in TASK_GRANT_FIELDS)
    protected = record.job_type in PORTFOLIO_JOB_TYPES or (
        record.job_type == "agent_prompt" and record.payload.get("agent_mode") != "developer"
    )
    if has_grant or (settings.agent_task_authorization_enabled and protected):
        from app.services.agent_authorization import (
            AgentAuthorizationError, get_task_authorization_service,
        )

        run_id, agent_id = job_authorization_identity(record.id, record.job_type)
        if not all(record.payload.get(key) for key in TASK_GRANT_FIELDS):
            raise AgentAuthorizationError(
                "task_grant_missing", "旧后台任务缺少任务授权，请重新创建任务；Worker 不会自动补发授权。",
            )
        if (record.payload["task_run_id"], record.payload["task_agent_id"]) != (run_id, agent_id):
            raise AgentAuthorizationError("task_grant_identity_mismatch", "任务授权与后台 Job 身份不一致。")
        service = get_task_authorization_service()
        context = service.restore(
            user_id=record.user_id,
            grant_id=record.payload["task_grant_id"],
            run_id=run_id,
            agent_id=agent_id,
        )
        service.check_context(context)
        action = "portfolio:research-run" if record.job_type in PORTFOLIO_JOB_TYPES else "portfolio:read"
        for portfolio_id in job_portfolio_ids(record):
            service.authorize(context, action=action, portfolio_id=portfolio_id)
        return context
    if record.payload.get("agent_mode") == "developer":
        return AgentExecutionContext.internal_developer(user_id=record.user_id, source="background-agent-worker")
    return AgentExecutionContext.product(user_id=record.user_id, source="background-agent-worker")


class JobCancelledError(RuntimeError):
    """Worker 发现数据库中的取消标记时主动结束。"""


@dataclass(frozen=True)
class JobRunPause:
    """业务工作流主动暂停，等待外部输入，而不是执行失败。

    result 会保存 LangGraph interrupt 的公开内容；current_step 表示暂停前真正完成的
    节点数。JobRunner 识别这个对象后写入 waiting_input，并正常结束当前 Worker。
    """

    result: dict[str, Any]
    current_step: int
    progress_message: str


class JobRunner:
    """执行当前项目支持的后台工作流。"""

    def __init__(self, store: JobStore) -> None:
        self.store = store

    def run(self, *, user_id: str, job_id: str) -> BackgroundJobRecord | None:
        from app.services.ai_governance_context import ai_execution_scope, stable_run_id
        # Worker 从服务端保存的 Job 身份执行；Celery 重投和人工恢复共用原任务额度。
        with ai_execution_scope(user_id, stable_run_id("job", job_id), "background_job",
                                lambda: self._check_cancelled(user_id, job_id)):
            return self._run_scoped(user_id=user_id, job_id=job_id)

    def _run_scoped(self, *, user_id: str, job_id: str) -> BackgroundJobRecord | None:
        """领取并执行一条 Job。

        claim 返回 None 说明任务已经被另一个 Worker 领取，或者已经进入终态。
        这使重复投递不会导致同一 Job 被执行两次。
        """

        # 第一步一定是 claim，而不是先执行再改状态；这是防重复执行的核心入口。
        record = self.store.claim(user_id=user_id, job_id=job_id)
        if record is None:
            return None

        return self._execute_claimed(record)

    def _execute_claimed(self, record: BackgroundJobRecord) -> BackgroundJobRecord:
        """执行已领取的任务；可靠投研入口会注入每次写入都校验租约的 Store。"""

        age_ms = max(0, (utc_now() - record.created_at).total_seconds() * 1000)
        lease = getattr(self.store, "lease", None)
        # 每次领取是新 Span，但父上下文来自 SQL，因此重启和重投仍归属同一 Trace。
        attrs = {"job.id": record.id, "job.type": record.job_type,
            "job.attempt": record.attempt_count, "job.backend": record.execution_backend,
            "job.execution_version": lease.version if lease else record.attempt_count,
            "job.age_ms": age_ms}
        if record.attempt_count == 1:
            attrs["job.initial_queue_ms"] = age_ms
        with trace_span("job.execute", carrier=record.payload.get("_trace_context") or {}, attributes=attrs) as span:
            result = self._execute_in_context(record)
            span.set_attribute("job.status", result.status)
            return result

    def _execute_in_context(self, record: BackgroundJobRecord) -> BackgroundJobRecord:
        """身份和预算检查仍是业务执行前置条件，追踪本身不赋予任何权限。"""

        from app.services.ai_governance_context import ai_execution_scope, stable_run_id

        with trace_span("job.authorize"):
            context = restore_job_execution_context(record)
        # 可靠任务也直接调用本入口，所以断点迁移和模型取消回调都在恢复后的范围内。
        with agent_execution_scope(context), ai_execution_scope(
            record.user_id, stable_run_id("job", record.id), "background_job",
            lambda: self._check_cancelled(record.user_id, record.id),
        ):
            self._check_cancelled(record.user_id, record.id)
            return self._execute_authorized(record)

    def _execute_authorized(self, record: BackgroundJobRecord) -> BackgroundJobRecord:
        # monotonic 不受系统时钟校准影响，适合计算“已经运行多久”。
        started_at = time.monotonic()
        # job_type 是持久化的工作流路由键。Executor 不关心业务，只负责调用 run。
        if record.job_type == "demo_steps":
            result = self._run_demo_steps(record=record, started_at=started_at)
        elif record.job_type == "agent_prompt":
            result = self._run_agent_prompt(record=record, started_at=started_at)
        elif record.job_type == "fund_catalog_import":
            result = self._run_fund_catalog_import(
                record=record,
                started_at=started_at,
            )
        elif record.job_type == "fund_nav_sync":
            result = self._run_fund_nav_sync(record=record, started_at=started_at)
        elif record.job_type == "fund_report_sync":
            result = self._run_fund_reports(record=record, started_at=started_at)
        elif record.job_type == "fund_report_eval":
            result = self._run_fund_report_eval(record=record, started_at=started_at)
        elif record.job_type == "market_snapshot_sync":
            result = self._run_market_snapshot_sync(
                record=record,
                started_at=started_at,
            )
        elif record.job_type == "large_capital_sync":
            result = self._run_large_capital_sync(
                record=record,
                started_at=started_at,
            )
        elif record.job_type == "market_event_sync":
            result = self._run_market_event_sync(
                record=record,
                started_at=started_at,
            )
        elif record.job_type == "market_event_analysis":
            result = self._run_market_event_analysis(
                record=record,
                started_at=started_at,
            )
        elif record.job_type == "market_event_clustering":
            result = self._run_market_event_clustering(
                record=record,
                started_at=started_at,
            )
        elif record.job_type == "social_signal_browser_login":
            result = self._run_social_signal_browser_login(
                record=record,
                started_at=started_at,
            )
        elif record.job_type == "social_signal_analysis":
            from app.services.social_signals import social_signal_service

            def progress(message):
                # 每个阶段检查取消/超时，长分析复用 Job 生命周期，不启动浏览器。
                self._check_runtime_limit(record=record, started_at=started_at)
                self._check_cancelled(record.user_id, record.id)
                self.store.update(user_id=record.user_id, job_id=record.id,
                    allowed_statuses={"running"}, changes={"current_step": 1, "progress_message": message})

            result = asyncio.run(social_signal_service.analyze_saved(progress=progress, scope=record.payload.get("social_scope", "window")))
        elif record.job_type == "social_signal_sync":
            result = self._run_social_signal_sync(
                record=record,
                started_at=started_at,
            )
        elif record.job_type == "market_intelligence_pipeline":
            result = self._run_market_intelligence_pipeline(
                record=record,
                started_at=started_at,
            )
        elif record.job_type == "portfolio_research":
            result = self._run_portfolio_research(
                record=record,
                started_at=started_at,
            )
        elif record.job_type == "portfolio_adjustment":
            result = self._run_portfolio_adjustment(
                record=record,
                started_at=started_at,
            )
        elif record.job_type == "portfolio_monitor":
            result = self._run_portfolio_monitor(
                record=record,
                started_at=started_at,
            )
        elif record.job_type == "teammate_message":
            result = self._run_teammate_message(record=record, started_at=started_at)
        elif record.job_type == "teammate_protocol":
            result = self._run_teammate_protocol(record=record, started_at=started_at)
        elif record.job_type == "autonomous_teammate":
            result = self._run_autonomous_teammate(record=record, started_at=started_at)
        else:
            raise ValueError(f"不支持的 job_type：{record.job_type}")

        # HITL interrupt 是一种正常控制流：保存待确认内容后释放 Worker，不能把它
        # 误判为 completed，也不能抛给 Executor 触发失败重试。
        if isinstance(result, JobRunPause):
            self._check_cancelled(record.user_id, record.id)
            return self.store.update(
                user_id=record.user_id,
                job_id=record.id,
                allowed_statuses={"running"},
                changes={
                    "status": "waiting_input",
                    "current_step": min(record.total_steps, result.current_step),
                    "progress_message": result.progress_message,
                    "result": result.result,
                    "error": None,
                },
            )

        # 业务函数返回后再检查一次取消，避免“用户刚取消，迟到结果却写成 completed”。
        self._check_cancelled(record.user_id, record.id)
        lease = getattr(self.store, "lease", None)
        completion_message = (
            f"后台工作已完成，恢复后第 {lease.version} 次执行成功。"
            if lease is not None and lease.version > 1 else "后台工作已完成。"
        )
        # allowed_statuses 相当于最后一道 compare-and-set：只有仍为 running 才能完成。
        with trace_span("job.save_result"):
            return self.store.update(
                user_id=record.user_id,
                job_id=record.id,
                allowed_statuses={"running"},
                changes={
                    "status": "completed",
                    "current_step": record.total_steps,
                    "progress_message": completion_message,
                    "result": result,
                    "error": None,
                    "completed_at": utc_now(),
                },
            )

    def _run_demo_steps(
        self,
        *,
        record: BackgroundJobRecord,
        started_at: float,
    ) -> dict[str, Any]:
        """保留 s13 的确定性分步任务，便于无 API Key 时测试基础设施。"""

        delay = float(record.payload.get("step_delay_seconds", 0.4))
        for step in range(1, record.total_steps + 1):
            # 把一次长 sleep 切成短片段，取消和超时就能更快生效。
            self._cooperative_wait(
                user_id=record.user_id,
                job_id=record.id,
                seconds=delay,
                started_at=started_at,
                timeout_seconds=record.timeout_seconds,
            )
            self.store.update(
                user_id=record.user_id,
                job_id=record.id,
                allowed_statuses={"running"},
                changes={
                    "current_step": step,
                    "progress_message": f"已完成第 {step}/{record.total_steps} 步。",
                },
            )

        return {
            "summary": f"{record.title} 已完成 {record.total_steps} 个步骤。",
            "completed_steps": record.total_steps,
            "workflow": "demo_steps",
        }

    def _run_agent_prompt(
        self,
        *,
        record: BackgroundJobRecord,
        started_at: float,
    ) -> dict[str, Any]:
        """在 Worker 中执行现有 Tool Calling Agent，而不是模拟工作。"""

        prompt = str(record.payload.get("prompt") or record.title).strip()
        max_tool_rounds = int(record.payload.get("max_tool_rounds", 10))

        self._check_runtime_limit(record=record, started_at=started_at)
        self.store.update(
            user_id=record.user_id,
            job_id=record.id,
            allowed_statuses={"running"},
            changes={
                "current_step": 1,
                "progress_message": "正在组装上下文并调用 Agent Loop。",
            },
        )

        # 放在函数内部导入，避免 tool_agent -> background_jobs -> job_runtime 的循环导入。
        from app.services.tool_agent import tool_calling_agent_service

        request = AgentChatRequest(
            user_id=record.user_id,
            message=prompt,
            max_tool_rounds=max_tool_rounds,
        )
        # 前面的组装和数据库操作也消耗总超时，因此只把剩余预算交给 Agent Loop。
        remaining = max(1.0, record.timeout_seconds - (time.monotonic() - started_at))

        async def invoke_agent():
            # JobRunner 由同步线程/Celery task 调用；asyncio.run 在这里创建临时事件循环，
            # wait_for 则给异步 Agent 调用设置当前 Job 的剩余超时。
            # 使用 Worker 已恢复的授权，不能重新构造 product 丢掉组合范围。
            execution_context = current_agent_execution_context()
            return await asyncio.wait_for(
                tool_calling_agent_service.chat(
                    request,
                    execution_context=execution_context,
                ),
                timeout=remaining,
            )

        response = asyncio.run(invoke_agent())
        # 模型调用期间不能强杀协程；返回到 Harness 边界后立即检查共享取消标记。
        self._check_cancelled(record.user_id, record.id)
        self.store.update(
            user_id=record.user_id,
            job_id=record.id,
            allowed_statuses={"running"},
            changes={
                "current_step": 2,
                "progress_message": "Agent 已返回，正在持久化结构化结果。",
            },
        )

        # 保存完整结构化响应，后续 API/前端不必重新调用模型就能查看工具轨迹。
        response_data = response.model_dump(mode="json")
        return {
            "summary": response.answer,
            "workflow": "agent_prompt",
            "stop_reason": response.stop_reason,
            "tool_call_count": len(response.tool_calls),
            "agent_response": response_data,
        }

    def _run_fund_reports(self, *, record, started_at):
        """把季报同步接入已有进度、取消、超时与持久化结果的生命周期。"""
        from app.services.fund_reports import fund_report_service

        def progress(step, message):
            self._check_runtime_limit(record=record, started_at=started_at)
            self.store.update(user_id=record.user_id, job_id=record.id, allowed_statuses={"running"},
                changes={"current_step": step - 1, "progress_message": message})

        result = fund_report_service.sync(record.payload["fund_code"], progress_callback=progress)
        self._check_runtime_limit(record=record, started_at=started_at)
        return result

    def _run_fund_report_eval(self, *, record, started_at):
        """复用Job持久化与取消，默认不重试整套付费模型请求。"""
        from app.services.fund_report_evals import fund_report_eval_service

        def progress(completed, total, message):
            self._check_runtime_limit(record=record, started_at=started_at)
            changes = {"progress_message": message}
            if completed is not None:
                changes["current_step"] = min(completed, record.total_steps)
            self.store.update(user_id=record.user_id, job_id=record.id,
                allowed_statuses={"running"}, changes=changes)

        async def evaluate():
            task = asyncio.create_task(fund_report_eval_service.run_suite(user_id=record.user_id,
                case_ids=record.payload.get("eval_case_ids"), progress_callback=progress,
                retrieval_mode=record.payload.get("eval_retrieval_mode", "hybrid")))
            try:
                while not task.done():
                    self._check_runtime_limit(record=record, started_at=started_at)
                    await asyncio.wait({task}, timeout=0.5)
                return await task
            finally:
                if not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)

        run = asyncio.run(evaluate())
        self._check_runtime_limit(record=record, started_at=started_at)
        # 完整回答在评测表，Job只留定位信息，避免复制大段原文和答案。
        return {"ok": True, "eval_run_id": run.id, "suite_id": run.suite_id,
            "passed_cases": run.passed_cases, "total_cases": run.total_cases, "score": run.score}

    def _run_fund_nav_sync(
        self,
        *,
        record: BackgroundJobRecord,
        started_at: float,
    ) -> dict[str, Any]:
        """执行不含大模型的 AKShare -> SQL 基金净值同步。"""

        self._check_runtime_limit(record=record, started_at=started_at)
        self.store.update(
            user_id=record.user_id,
            job_id=record.id,
            allowed_statuses={"running"},
            changes={
                "current_step": 1,
                "progress_message": "正在从 AKShare / 东方财富读取基金净值。",
            },
        )

        # 延迟导入避免普通 Job 模块初始化时就加载基金数据服务。
        from app.services.fund_data_sync import fund_nav_sync_service

        def report_progress(index: int, total: int, fund_code: str) -> None:
            # 每只基金开始前检查取消与超时；网络请求返回后还会经过 run() 的最终检查。
            self._check_runtime_limit(record=record, started_at=started_at)
            self.store.update(
                user_id=record.user_id,
                job_id=record.id,
                allowed_statuses={"running"},
                changes={
                    "current_step": 1,
                    "progress_message": (
                        f"正在同步基金 {fund_code}（{index}/{total}）。"
                    ),
                },
            )

        result = fund_nav_sync_service.sync(progress_callback=report_progress)
        self._check_cancelled(record.user_id, record.id)
        self.store.update(
            user_id=record.user_id,
            job_id=record.id,
            allowed_statuses={"running"},
            changes={
                "current_step": 2,
                "progress_message": "净值已写入数据库，正在生成同步摘要。",
            },
        )
        return result

    def _run_fund_catalog_import(
        self,
        *,
        record: BackgroundJobRecord,
        started_at: float,
    ) -> dict[str, Any]:
        """读取基金档案和近三年净值，再把它加入可分析目录。"""

        fund_code = str(record.payload.get("fund_code") or "").strip()
        if not fund_code:
            raise ValueError("fund_catalog_import Job 缺少 fund_code。")

        from app.services.fund_catalog import fund_catalog_import_service

        def report_progress(step: int, message: str) -> None:
            # Provider 网络调用无法在中途强杀；每个边界都会重新检查取消和总超时。
            self._check_runtime_limit(record=record, started_at=started_at)
            self.store.update(
                user_id=record.user_id,
                job_id=record.id,
                allowed_statuses={"running"},
                changes={
                    "current_step": min(record.total_steps, max(0, step - 1)),
                    "progress_message": message,
                },
            )

        result = fund_catalog_import_service.import_fund(
            fund_code=fund_code,
            progress_callback=report_progress,
        )
        self._check_runtime_limit(record=record, started_at=started_at)
        return result

    def _run_market_snapshot_sync(
        self,
        *,
        record: BackgroundJobRecord,
        started_at: float,
    ) -> dict[str, Any]:
        """执行不含大模型的市场宽度、指数成交额同步。"""

        self._check_runtime_limit(record=record, started_at=started_at)
        self.store.update(
            user_id=record.user_id,
            job_id=record.id,
            allowed_statuses={"running"},
            changes={
                "current_step": 1,
                "progress_message": "正在从 AKShare 读取市场宽度和沪深300成交额。",
            },
        )

        # 延迟导入保持 Job Runtime 与具体财经 Provider 解耦。
        from app.services.market_data_sync import market_snapshot_sync_service

        result = market_snapshot_sync_service.sync()
        self._check_cancelled(record.user_id, record.id)
        self.store.update(
            user_id=record.user_id,
            job_id=record.id,
            allowed_statuses={"running"},
            changes={
                "current_step": 2,
                "progress_message": "市场快照已写入数据库，正在生成同步摘要。",
            },
        )
        return result

    def _run_market_event_sync(
        self,
        *,
        record: BackgroundJobRecord,
        started_at: float,
    ) -> dict[str, Any]:
        """同步多来源事件，并在写库后自动重建语义事件簇。"""

        self._check_runtime_limit(record=record, started_at=started_at)
        topics = list(record.payload.get("market_topics") or [])
        self.store.update(
            user_id=record.user_id,
            job_id=record.id,
            allowed_statuses={"running"},
            changes={
                "current_step": 1,
                "progress_message": (
                    "正在扩展关注板块的采集关键词。"
                    if topics
                    else "正在读取官方网站、基金公告和财经媒体事件。"
                ),
            },
        )

        # 延迟导入避免普通 Background Job 启动时加载财经采集依赖。
        from app.services.market_event_sync import market_event_sync_service

        keyword_expansion = None
        if topics:
            from app.services.market_topic_keywords import market_topic_keyword_service

            keyword_expansion = asyncio.run(
                market_topic_keyword_service.expand(
                    topics=topics,
                    use_model=bool(
                        record.payload.get("use_model_topic_expansion", True)
                    ),
                )
            )
            self._check_runtime_limit(record=record, started_at=started_at)
            self.store.update(
                user_id=record.user_id,
                job_id=record.id,
                allowed_statuses={"running"},
                changes={
                    "progress_message": (
                        f"已生成 {len(keyword_expansion['keywords'])} 个采集词，"
                        "正在筛选多来源事件。"
                    ),
                },
            )

        result = (
            market_event_sync_service.sync(keyword_expansion=keyword_expansion)
            if keyword_expansion is not None
            else market_event_sync_service.sync()
        )
        self._check_cancelled(record.user_id, record.id)
        self.store.update(
            user_id=record.user_id,
            job_id=record.id,
            allowed_statuses={"running"},
            changes={
                "current_step": 2,
                "progress_message": "事件已写入数据库，正在自动聚合多来源相似报道。",
            },
        )
        # 同步与聚类属于同一条可观测 Job，但仍由两个独立 Service 各管一件事。
        # 聚类使用 Qwen Embedding，不会调用负责事件方向判断的 DeepSeek。
        from app.services.market_event_clusters import market_event_cluster_service

        clustering = market_event_cluster_service.build()
        result["clustering"] = clustering
        result["summary"] = f"{result['summary']} {clustering['summary']}"
        return result

    def _run_large_capital_sync(
        self,
        *,
        record: BackgroundJobRecord,
        started_at: float,
    ) -> dict[str, Any]:
        """采集 ETF 份额和龙虎榜机构席位，不经过主 Agent 或大模型。"""

        self._check_runtime_limit(record=record, started_at=started_at)
        self.store.update(
            user_id=record.user_id,
            job_id=record.id,
            allowed_statuses={"running"},
            changes={
                "current_step": 1,
                "progress_message": "正在读取 ETF 份额和龙虎榜机构买卖。",
            },
        )

        # 领域模块延迟导入，普通 Job 不需要在进程启动时加载财经 Provider。
        from app.services.large_capital_flows import large_capital_sync_service

        result = large_capital_sync_service.sync()
        self._check_cancelled(record.user_id, record.id)
        self._check_runtime_limit(record=record, started_at=started_at)
        self.store.update(
            user_id=record.user_id,
            job_id=record.id,
            allowed_statuses={"running"},
            changes={
                "current_step": 2,
                "progress_message": "公开资金事实已写入数据库，正在生成关联摘要。",
            },
        )
        return result

    def _run_market_event_analysis(
        self,
        *,
        record: BackgroundJobRecord,
        started_at: float,
    ) -> dict[str, Any]:
        """让 DeepSeek 批量分析待处理事件，并保存经过 Python 校验的结果。"""

        self._check_runtime_limit(record=record, started_at=started_at)
        self.store.update(
            user_id=record.user_id,
            job_id=record.id,
            allowed_statuses={"running"},
            changes={
                "current_step": 1,
                "progress_message": "正在读取待分析事件并调用 DeepSeek 结构化输出。",
            },
        )

        from app.services.market_event_analysis import market_event_analysis_service

        result = asyncio.run(market_event_analysis_service.analyze_pending())
        self._check_cancelled(record.user_id, record.id)
        self.store.update(
            user_id=record.user_id,
            job_id=record.id,
            allowed_statuses={"running"},
            changes={
                "current_step": 2,
                "progress_message": "语义结果已校验并独立持久化，正在生成摘要。",
            },
        )
        return result

    def _run_market_event_clustering(
        self,
        *,
        record: BackgroundJobRecord,
        started_at: float,
    ) -> dict[str, Any]:
        """调用 Qwen Embedding，并用 Python 重建跨来源事件簇。"""

        self._check_runtime_limit(record=record, started_at=started_at)
        self.store.update(
            user_id=record.user_id,
            job_id=record.id,
            allowed_statuses={"running"},
            changes={
                "current_step": 1,
                "progress_message": "正在生成或复用事件 Embedding。",
            },
        )

        # Service 内部负责分批调用、缓存复用和聚类；JobRunner 只管理运行边界。
        from app.services.market_event_clusters import market_event_cluster_service

        result = market_event_cluster_service.build()
        # 开发者单独运行聚类时也记录热点。完整市场情报流水线会在自己的聚类阶段
        # 执行相同动作，因此两种入口都不会留下“有新簇、无热点历史”的状态。
        if int(result.get("cluster_count", 0)) > 0:
            from app.services.market_hotspots import market_hotspot_tracking_service

            result = {
                **result,
                "hotspot_tracking": market_hotspot_tracking_service.capture(),
            }
        self._check_cancelled(record.user_id, record.id)
        self.store.update(
            user_id=record.user_id,
            job_id=record.id,
            allowed_statuses={"running"},
            changes={
                "current_step": 2,
                "progress_message": "事件簇和热点快照已持久化，正在生成摘要。",
            },
        )
        return result

    def _run_social_signal_sync(
        self,
        *,
        record: BackgroundJobRecord,
        started_at: float,
    ) -> dict[str, Any]:
        """采集公开社交帖子、调用 DeepSeek 抽取并生成群体观点。"""

        self._check_runtime_limit(record=record, started_at=started_at)
        self.store.update(
            user_id=record.user_id,
            job_id=record.id,
            allowed_statuses={"running"},
            changes={
                "current_step": 1,
                "progress_message": "正在按基金与实盘关键词采集小红书公开观点。",
            },
        )

        # SocialSignalService 内部保持 Provider、模型抽取和聚合职责分离；JobRunner 只
        # 提供取消、超时和进度边界。
        from app.services.social_signals import social_signal_service

        result = asyncio.run(social_signal_service.refresh())
        self._check_cancelled(record.user_id, record.id)
        self.store.update(
            user_id=record.user_id,
            job_id=record.id,
            allowed_statuses={"running"},
            changes={
                "current_step": 2,
                "progress_message": result.get("summary") or "社交观点处理结束，正在生成摘要。",
            },
        )
        return result

    def _run_social_signal_browser_login(
        self,
        *,
        record: BackgroundJobRecord,
        started_at: float,
    ) -> dict[str, Any]:
        """打开本机独立 Chrome，等待用户自行完成小红书登录。"""

        self._check_runtime_limit(record=record, started_at=started_at)
        self.store.update(
            user_id=record.user_id,
            job_id=record.id,
            allowed_statuses={"running"},
            changes={
                "current_step": 1,
                "progress_message": "已打开独立 Chrome，正在等待用户完成小红书登录。",
            },
        )

        # Provider 只检测登录 Cookie 是否存在，不读取密码、验证码或 Cookie 值。
        from app.services.social_signals import social_signal_service

        result = social_signal_service.prepare_browser_session()
        self._check_cancelled(record.user_id, record.id)
        self.store.update(
            user_id=record.user_id,
            job_id=record.id,
            allowed_statuses={"running"},
            changes={
                "current_step": 2,
                "progress_message": "小红书登录会话已保存在本机浏览器 Profile。",
            },
        )
        return result

    def _run_market_intelligence_pipeline(
        self,
        *,
        record: BackgroundJobRecord,
        started_at: float,
    ) -> dict[str, Any]:
        """运行五阶段市场情报流水线，并把每个成功阶段保存为恢复检查点。"""

        from app.services.market_intelligence_pipeline import (
            market_intelligence_pipeline_service,
        )

        partial_result = (
            dict(record.result)
            if isinstance(record.result, dict)
            and record.result.get("workflow") == "market_intelligence_pipeline"
            else {}
        )
        stage_results = dict(partial_result.get("stages") or {})

        def check_runtime() -> None:
            self._check_runtime_limit(record=record, started_at=started_at)

        def save_stage(
            step: int,
            key: str,
            message: str,
            stage_result: dict[str, Any],
        ) -> None:
            """进度和阶段结果一起提交，避免只前进数字却丢失恢复上下文。"""

            check_runtime()
            stage_results[key] = stage_result
            self.store.update(
                user_id=record.user_id,
                job_id=record.id,
                allowed_statuses={"running"},
                changes={
                    "current_step": step,
                    "progress_message": f"{message}（{key}）。",
                    "result": {
                        "data_kind": "market_intelligence_pipeline_checkpoint",
                        "workflow": "market_intelligence_pipeline",
                        "status": "running",
                        "run_id": record.id,
                        "stages": dict(stage_results),
                    },
                    "error": None,
                },
            )

        check_runtime()
        return asyncio.run(
            market_intelligence_pipeline_service.run(
                run_id=record.id,
                requesting_user_id=record.user_id,
                market_topics=list(record.payload.get("market_topics") or []),
                use_model_topic_expansion=bool(
                    record.payload.get("use_model_topic_expansion", True)
                ),
                resume_from_step=record.current_step,
                previous_stage_results=stage_results,
                stage_callback=save_stage,
                cancellation_check=check_runtime,
            )
        )

    def _run_portfolio_research(
        self,
        *,
        record: BackgroundJobRecord,
        started_at: float,
    ) -> dict[str, Any]:
        """在 Worker 中执行 LangGraph 组合研究图。

        Background Job 是外层运行容器，负责排队、取消和超时；LangGraph 是内层业务
        编排器，负责节点状态、并行分支和 checkpoint。两层通过 progress_callback
        连接，但互不复制职责。
        """

        portfolio_id = str(record.payload.get("portfolio_id") or "").strip()
        months = int(record.payload.get("portfolio_months") or 12)
        research_mode = record.payload.get("research_mode", "standard")
        if not portfolio_id:
            raise ValueError("portfolio_research Job 缺少 portfolio_id。")
        if months not in {6, 12, 36}:
            raise ValueError("portfolio_research Job 的观察区间非法。")
        if research_mode not in ("explain", "strategy", "standard", "committee"):
            raise ValueError("不支持的组合研究模式。")
        if research_mode != "standard" and record.payload.get("daily_briefing") is not None:
            raise ValueError("每日简报只能使用 standard 模式。")

        # 延迟导入避免应用启动时让所有普通 Job 都立刻构建 LangGraph。
        from app.services.portfolio_research_graph import (
            portfolio_research_graph_service,
        )

        graph_options = {}
        lease = getattr(self.store, "lease", None)
        if lease is not None:
            # 复制旧断点后在新分区恢复。旧 Worker 后续写旧分区，不会覆盖本轮状态。
            self._check_runtime_limit(record=record, started_at=started_at)
            portfolio_research_graph_service.prepare_attempt_checkpoint(
                target_thread_id=lease.checkpoint_thread_id,
                source_thread_ids=lease.previous_threads,
            )
            graph_options["checkpoint_thread_id"] = lease.checkpoint_thread_id
        checkpoint = portfolio_research_graph_service.checkpoint_info(run_id=record.id, **graph_options)
        if record.payload.get("daily_briefing"):
            from app.services.daily_briefing_data import prepare_daily_briefing
            # 主快照已经冻结时无需重做刷新。尚未冻结则恢复 Job 内保存的刷新阶段。
            frozen = portfolio_research_graph_service.snapshots.get_for_run(user_id=record.user_id, run_id=record.id)
            if frozen is None:
                graph_options["daily_briefing"] = prepare_daily_briefing(
                    record=record, store=self.store,
                    check=lambda: self._check_runtime_limit(record=record, started_at=started_at),
                )
        # Job 进度和 LangGraph checkpoint 来自不同持久化层。重试时取两者较大值，
        # 让前端进度保持单调，同时从图状态得知哪些节点无需重跑。
        completed_step = min(
            record.total_steps,
            max(record.current_step, checkpoint["completed_node_count"]),
        )
        progress_lock = Lock()

        if checkpoint["status"] == "empty":
            # 快照采集不额外占用一个 LangGraph 节点，因此进度仍为零；这条消息让
            # 用户知道 Worker 正在并行冻结五路输入，而不是停在队列里。
            self.store.update(
                user_id=record.user_id,
                job_id=record.id,
                allowed_statuses={"running"},
                changes={
                    "current_step": 0,
                    "progress_message": (
                        "正在冻结组合、风险、业绩、市场和决策输入。"
                    ),
                },
            )

        if checkpoint["status"] in {"resumable", "completed"}:
            backend_label = (
                "PostgreSQL"
                if checkpoint["backend"] == "postgres"
                else "SQLite"
                if checkpoint["backend"] == "sqlite"
                else "内存"
            )
            message = (
                f"检测到已完成的 {backend_label} checkpoint，正在直接读取报告。"
                if checkpoint["status"] == "completed"
                else (
                    f"正在从 {backend_label} checkpoint 恢复，"
                    f"已保留 {completed_step}/{record.total_steps} 个节点进度。"
                )
            )
            self.store.update(
                user_id=record.user_id,
                job_id=record.id,
                allowed_statuses={"running"},
                changes={
                    "current_step": completed_step,
                    "progress_message": message,
                },
            )

        def report_progress(node_name: str, message: str) -> None:
            """把并行节点的完成事件安全地转换成单调递增的 Job 步数。"""

            nonlocal completed_step
            # 风险节点和市场节点可能在不同线程同时完成，用 Lock 防止两个回调都写 2。
            with progress_lock:
                self._check_runtime_limit(record=record, started_at=started_at)
                completed_step = min(record.total_steps, completed_step + 1)
                self.store.update(
                    user_id=record.user_id,
                    job_id=record.id,
                    allowed_statuses={"running"},
                    changes={
                        "current_step": completed_step,
                        "progress_message": f"{message}（{node_name}）。",
                    },
                )

        self._check_runtime_limit(record=record, started_at=started_at)
        # 轻量和旧任务不传新参数，兼容原有图适配器及测试 FakeGraph 的签名。
        if research_mode != "standard":
            graph_options["research_mode"] = research_mode
        if record.payload.get("research_plan"):
            graph_options["research_plan"] = record.payload["research_plan"]
            latest = self.store.get(user_id=record.user_id, job_id=record.id)
            graph_options["repair_previous"] = latest.payload.get("research_repair_budget") or {}

            def save_repair_budget(value):
                # 先保存已使用的来源名额。被取消/旧租约的 Worker 不得开始下一次外部抓取。
                self._check_runtime_limit(record=record, started_at=started_at)
                current = self.store.get(user_id=record.user_id, job_id=record.id)
                updated = self.store.update(user_id=record.user_id, job_id=record.id, allowed_statuses={"running"},
                    changes={"payload": {**current.payload, "research_repair_budget": value}})
                if updated.status != "running":
                    raise JobCancelledError("任务已停止，未开始补查")

            graph_options["repair_checkpoint"] = save_repair_budget
        result = portfolio_research_graph_service.run(
            run_id=record.id,
            user_id=record.user_id,
            portfolio_id=portfolio_id,
            months=months,
            progress_callback=report_progress,
            **graph_options,
        )
        # graph.invoke 返回时所选模式的节点都已结束；再检查一次用户是否刚刚取消。
        self._check_runtime_limit(record=record, started_at=started_at)
        return result

    def _run_portfolio_adjustment(
        self,
        *,
        record: BackgroundJobRecord,
        started_at: float,
    ) -> dict[str, Any] | JobRunPause:
        """执行可暂停、可恢复的组合调整 LangGraph。

        首次执行只会整理研究证据并停在 ``interrupt``；用户从前端提交约束后，
        BackgroundJobService 把同一 Job 重新置为 queued，Worker 再用相同 Job ID
        恢复 checkpoint。整个过程不会占着线程等待用户操作。
        """

        portfolio_id = str(record.payload.get("portfolio_id") or "").strip()
        source_job_id = str(
            record.payload.get("source_research_job_id") or ""
        ).strip()
        human_review = record.payload.get("human_review")
        if not portfolio_id:
            raise ValueError("portfolio_adjustment Job 缺少 portfolio_id。")
        if not source_job_id:
            raise ValueError(
                "portfolio_adjustment Job 缺少 source_research_job_id。"
            )
        if human_review is not None and not isinstance(human_review, dict):
            raise ValueError("portfolio_adjustment Job 的 human_review 非法。")

        # 调整方案必须引用同一用户、同一组合的一份已完成研究报告。Worker 再校验一次，
        # 即使绕过 HTTP Router 直接写入 Job，也不能把别人的报告当作模型上下文。
        source_job = self.store.get(
            user_id=record.user_id,
            job_id=source_job_id,
        )
        source_report = source_job.result
        if source_job.job_type != "portfolio_research":
            raise ValueError("组合调整的数据来源不是 portfolio_research Job。")
        if source_job.status != "completed" or not source_report:
            raise ValueError("组合调整需要一份已完成且包含结果的研究 Job。")
        if source_job.payload.get("portfolio_id") != portfolio_id or source_report.get("portfolio_id") != portfolio_id:
            from app.services.agent_authorization import AgentAuthorizationError

            raise AgentAuthorizationError("task_grant_scope_mismatch", "研究报告与当前组合不匹配。")

        from app.services.portfolio_adjustment_graph import (
            portfolio_adjustment_graph_service,
        )

        checkpoint = portfolio_adjustment_graph_service.checkpoint_info(
            run_id=record.id
        )
        completed_step = min(
            record.total_steps,
            max(record.current_step, checkpoint["completed_node_count"]),
        )
        progress_lock = Lock()

        if checkpoint["status"] in {
            "waiting_input",
            "resumable",
            "completed",
        }:
            self.store.update(
                user_id=record.user_id,
                job_id=record.id,
                allowed_statuses={"running"},
                changes={
                    "current_step": completed_step,
                    "progress_message": (
                        "正在从持久化 checkpoint 恢复组合调整流程。"
                    ),
                },
            )

        def report_progress(node_name: str, message: str) -> None:
            """把 LangGraph 节点完成事件同步成 Background Job 进度。"""

            nonlocal completed_step
            with progress_lock:
                self._check_runtime_limit(
                    record=record,
                    started_at=started_at,
                )
                completed_step = min(record.total_steps, completed_step + 1)
                self.store.update(
                    user_id=record.user_id,
                    job_id=record.id,
                    allowed_statuses={"running"},
                    changes={
                        "current_step": completed_step,
                        "progress_message": f"{message}（{node_name}）。",
                    },
                )

        self._check_runtime_limit(record=record, started_at=started_at)
        outcome = portfolio_adjustment_graph_service.run(
            run_id=record.id,
            user_id=record.user_id,
            portfolio_id=portfolio_id,
            source_research_job_id=source_job_id,
            source_report=source_report,
            resume_payload=human_review,
            progress_callback=report_progress,
        )
        self._check_runtime_limit(record=record, started_at=started_at)

        if outcome.status == "waiting_input":
            return JobRunPause(
                result=outcome.result,
                current_step=outcome.completed_node_count,
                progress_message="等待用户确认风险偏好与投资约束。",
            )
        return outcome.result

    def _run_portfolio_monitor(
        self,
        *,
        record: BackgroundJobRecord,
        started_at: float,
    ) -> dict[str, Any]:
        """更新组合风险快照，并保存 Python 阈值触发的预警。

        这个 Worker 不会自动研究或调仓。它只运行确定性监控；如果规则命中，模型可在
        Service 内把已经发生的变化解释成通俗文字。
        """

        portfolio_id = str(record.payload.get("portfolio_id") or "").strip()
        months = int(record.payload.get("portfolio_months") or 12)
        if not portfolio_id:
            raise ValueError("portfolio_monitor Job 缺少 portfolio_id。")
        if months not in {6, 12, 36}:
            raise ValueError("portfolio_monitor Job 的观察区间非法。")

        from app.services.portfolio_monitoring import portfolio_monitoring_service

        completed_step = max(0, record.current_step)

        def report_progress(stage: str, message: str) -> None:
            """把规则、量化、事件归因和持久化四个阶段映射为 Job 进度。"""

            nonlocal completed_step
            self._check_runtime_limit(record=record, started_at=started_at)
            completed_step = min(record.total_steps, completed_step + 1)
            self.store.update(
                user_id=record.user_id,
                job_id=record.id,
                allowed_statuses={"running"},
                changes={
                    "current_step": completed_step,
                    "progress_message": f"{message}（{stage}）。",
                },
            )

        self._check_runtime_limit(record=record, started_at=started_at)
        result = portfolio_monitoring_service.run_monitor(
            user_id=record.user_id,
            portfolio_id=portfolio_id,
            run_id=record.id,
            progress_callback=report_progress,
        )
        self._check_runtime_limit(record=record, started_at=started_at)
        return result

    def _run_teammate_message(
        self,
        *,
        record: BackgroundJobRecord,
        started_at: float,
    ) -> dict[str, Any]:
        """让一个长期 teammate 处理自己的 inbox 消息并回复 main-agent。"""

        # payload 只保存引用；成员配置和消息正文仍以 Agent Team 数据表为准。
        teammate_name = str(record.payload.get("teammate_name") or "").strip()
        message_id = str(record.payload.get("team_message_id") or "").strip()
        max_rounds = int(record.payload.get("max_tool_rounds", 3))
        if not teammate_name or not message_id:
            raise ValueError("teammate_message Job 缺少 teammate_name 或 team_message_id。")

        self._check_runtime_limit(record=record, started_at=started_at)
        self.store.update(
            user_id=record.user_id,
            job_id=record.id,
            allowed_statuses={"running"},
            changes={
                "current_step": 1,
                "progress_message": f"正在把 Message Bus 消息交给 {teammate_name}。",
            },
        )

        # 延迟导入避免 agent_teams -> background_jobs -> job_runtime 的循环依赖。
        from app.services.agent_teams import agent_team_service

        remaining = max(1.0, record.timeout_seconds - (time.monotonic() - started_at))

        async def invoke_teammate():
            # AgentTeamService 负责身份、上下文和消息落库；JobRunner 只给它执行预算。
            return await asyncio.wait_for(
                agent_team_service.execute_teammate_message(
                    user_id=record.user_id,
                    teammate_name=teammate_name,
                    message_id=message_id,
                    max_rounds=max_rounds,
                ),
                timeout=remaining,
            )

        result = asyncio.run(invoke_teammate())
        self._check_cancelled(record.user_id, record.id)
        self.store.update(
            user_id=record.user_id,
            job_id=record.id,
            allowed_statuses={"running"},
            changes={
                "current_step": 2,
                "progress_message": f"{teammate_name} 已回复，正在保存团队运行结果。",
            },
        )
        return result

    def _run_teammate_protocol(
        self,
        *,
        record: BackgroundJobRecord,
        started_at: float,
    ) -> dict[str, Any]:
        """分发一条 s16 typed request，并通过 request_id 写回匹配响应。

        当前内部 Job 只自动处理 shutdown_request。计划审批由 main-agent 显式调用
        review_team_plan_tool，Worker 不能替负责人做审批决定。
        """

        request_id = str(
            record.payload.get("team_protocol_request_id") or ""
        ).strip()
        teammate_name = str(record.payload.get("teammate_name") or "").strip()
        if not request_id or not teammate_name:
            raise ValueError(
                "teammate_protocol Job 缺少 request_id 或 teammate_name。"
            )

        self._check_runtime_limit(record=record, started_at=started_at)
        self.store.update(
            user_id=record.user_id,
            job_id=record.id,
            allowed_statuses={"running"},
            changes={
                "current_step": 1,
                "progress_message": (
                    f"正在向 {teammate_name} 分发结构化协议 {request_id}。"
                ),
            },
        )

        # 延迟导入避免 team_protocols -> background_jobs -> job_runtime 循环依赖。
        from app.services.team_protocols import team_protocol_service

        result = team_protocol_service.dispatch_protocol_request(
            user_id=record.user_id,
            request_id=request_id,
        )
        self._check_cancelled(record.user_id, record.id)
        self.store.update(
            user_id=record.user_id,
            job_id=record.id,
            allowed_statuses={"running"},
            changes={
                "current_step": 2,
                "progress_message": (
                    f"协议响应已通过 request_id={request_id} 完成匹配。"
                ),
            },
        )
        return {
            "summary": result["message"],
            "workflow": "teammate_protocol",
            "request_id": request_id,
            "protocol_response": result,
        }

    def _run_autonomous_teammate(
        self,
        *,
        record: BackgroundJobRecord,
        started_at: float,
    ) -> dict[str, Any]:
        """运行 s17 的 IDLE -> claim -> WORK 外层循环。

        Autonomy Service 管理业务状态；JobRunner 继续提供取消、总超时和进度写入。
        """

        session_id = str(record.payload.get("autonomy_session_id") or "").strip()
        teammate_name = str(record.payload.get("teammate_name") or "").strip()
        if not session_id or not teammate_name:
            raise ValueError(
                "autonomous_teammate Job 缺少 autonomy_session_id 或 teammate_name。"
            )

        from app.services.autonomous_agents import autonomous_agent_service

        def check_runtime() -> None:
            self._check_runtime_limit(record=record, started_at=started_at)

        def update_progress(message: str) -> None:
            self.store.update(
                user_id=record.user_id,
                job_id=record.id,
                allowed_statuses={"running"},
                changes={"current_step": 2, "progress_message": message},
            )

        def cooperative_wait(seconds: float) -> None:
            self._cooperative_wait(
                user_id=record.user_id,
                job_id=record.id,
                seconds=seconds,
                started_at=started_at,
                timeout_seconds=record.timeout_seconds,
            )

        self.store.update(
            user_id=record.user_id,
            job_id=record.id,
            allowed_statuses={"running"},
            changes={
                "current_step": 1,
                "progress_message": f"正在启动 {teammate_name} 的自治外层循环。",
            },
        )
        try:
            return autonomous_agent_service.run_session(
                user_id=record.user_id,
                session_id=session_id,
                check_runtime=check_runtime,
                update_progress=update_progress,
                wait=cooperative_wait,
            )
        except JobCancelledError:
            autonomous_agent_service.mark_cancelled(
                user_id=record.user_id,
                session_id=session_id,
            )
            raise
        except Exception as exc:
            # claim 后 attempt_count 已递增。只要后面还有一次重试，就保留 current_task_id；
            # 最后一次失败才把自治业务状态写成 terminal failed。
            if record.attempt_count <= record.max_retries:
                autonomous_agent_service.mark_retrying(
                    user_id=record.user_id,
                    session_id=session_id,
                    error=str(exc),
                )
            else:
                autonomous_agent_service.mark_failed(
                    user_id=record.user_id,
                    session_id=session_id,
                    error=str(exc),
                )
            raise

    def _cooperative_wait(
        self,
        *,
        user_id: str,
        job_id: str,
        seconds: float,
        started_at: float,
        timeout_seconds: int,
    ) -> None:
        """把等待拆成小片段，在没有可中断 sleep 的线程里实现协作式取消。"""

        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            self._check_cancelled(user_id, job_id)
            if time.monotonic() - started_at > timeout_seconds:
                raise TimeoutError(f"Job 执行超过 {timeout_seconds} 秒。")
            time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))

    def _check_runtime_limit(
        self,
        *,
        record: BackgroundJobRecord,
        started_at: float,
    ) -> None:
        """在进入昂贵工作流前统一检查取消和总运行时间。"""

        self._check_cancelled(record.user_id, record.id)
        if time.monotonic() - started_at > record.timeout_seconds:
            raise TimeoutError(f"Job 执行超过 {record.timeout_seconds} 秒。")

    def _check_cancelled(self, user_id: str, job_id: str) -> None:
        """每次从共享 Store 读最新标记，让 API 进程的取消能被 Worker 看见。"""

        current = self.store.get(user_id=user_id, job_id=job_id)
        # 回调可能由模型层或线程池调用，每次都从数据库复核生命周期和任务身份。
        context = restore_job_execution_context(current)
        active = current_agent_execution_context()
        if active.task_grant_id:
            from app.services.agent_authorization import AgentAuthorizationError, get_task_authorization_service

            get_task_authorization_service().check_context(active)
            if (active.user_id, active.task_grant_id, active.task_run_id, active.task_agent_id) != (
                context.user_id, context.task_grant_id, context.task_run_id, context.task_agent_id,
            ):
                raise AgentAuthorizationError("task_grant_identity_mismatch", "运行中的任务授权与持久化 Job 不一致。")
        if current.cancel_requested or current.status == "cancelled":
            raise JobCancelledError(f"Job 已取消：{job_id}")
