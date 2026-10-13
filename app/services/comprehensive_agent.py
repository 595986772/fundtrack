"""s20 产品级 Agent 应用服务。

这里是 HTTP 层真正调用的用例编排器：准备会话历史、运行核心 Agent、持久化一轮消息，
最后附带统一工作区快照。它不复制 Agent Loop，也不直接操作各领域数据库。
"""

from __future__ import annotations

import json
import asyncio
from contextlib import nullcontext
from dataclasses import replace
from datetime import datetime
from time import perf_counter
from typing import Any, Awaitable, Callable

from sqlalchemy import select

from app.config import settings
from app.services.tracing import traced
from app.models import AgentConversationMessageModel
from app.schemas import (
    AgentChatRequest,
    AgentConversationMessageInfo,
    AgentConversationSummary,
    ComprehensiveAgentChatResponse,
)
from app.services.agent_runtime_state import (
    AgentRuntimeStateService,
    agent_runtime_state_service,
)
from app.services import agent_authorization as task_auth
from app.services.agent_execution_context import (
    AgentExecutionContext, PORTFOLIO_READ, agent_execution_scope,
    current_agent_execution_context,
)
from app.services.ai_governance_context import current_ai_scope
from app.services.chat_execution_budget import chat_stage
from app.services.conversations import ConversationService, conversation_service
from app.services.observability import (
    AgentObservabilityService,
    agent_observability_service,
)
from app.services.tool_agent import ToolCallingAgentService, tool_calling_agent_service
from app.services.request_context import current_request_id
from app.services.portfolio_report_context import resolve_report_context, selected_chat_portfolio_ids
from app.services.fund_report_context import resolve_fund_report_context, report_reference_from_run
from app.services.investment_context import (
    InvestmentContextService, investment_context_service, investment_context_scope,
)


def requires_chat_task_authorization(context: AgentExecutionContext | None) -> bool:
    return bool(context is not None and context.mode == "product"
        and settings.agent_task_authorization_enabled)


def chat_task_authorization_metadata(context: AgentExecutionContext, portfolio_ids: list[str]) -> dict:
    """只公开本任务的授权范围；缓存交付也复查归属与策略，不枚举用户其他组合。"""
    service = task_auth.get_task_authorization_service()
    task_auth.check_active_task_authorization()
    detail = service.detail(user_id=context.user_id, grant_id=context.task_grant_id)
    if sorted(detail["portfolio_ids"]) != sorted(portfolio_ids):
        raise task_auth.AgentAuthorizationError("grant_scope_conflict", "本次选择与原任务授权范围不一致。")
    for portfolio_id in portfolio_ids:
        service.authorize(context, action=PORTFOLIO_READ, portfolio_id=portfolio_id, audit=False)
    task_auth.check_active_task_authorization()
    return {"task_grant_id": context.task_grant_id, "run_id": context.task_run_id,
        "agent_id": context.task_agent_id, "portfolio_ids": list(portfolio_ids),
        "capabilities": detail["capabilities"], "expires_at": detail["expires_at"]}


def chat_authorization_event(metadata: dict) -> dict:
    return {"type": "phase", "phase": "authorization", "label": "本次任务授权已确认",
        "authorization": {"id": metadata["task_grant_id"], "run_id": metadata["run_id"],
            "agent_id": metadata["agent_id"], "expires_at": metadata["expires_at"],
            "scope": {"portfolio_ids": list(metadata["portfolio_ids"]),
                "capabilities": list(metadata["capabilities"])}}}


class ComprehensiveAgentService:
    """把一次用户聊天请求组织成完整的 s20 工作流。"""

    def __init__(
        self,
        *,
        conversations: ConversationService = conversation_service,
        agent: ToolCallingAgentService = tool_calling_agent_service,
        runtime_state: AgentRuntimeStateService = agent_runtime_state_service,
        observability: AgentObservabilityService = agent_observability_service,
        investment_context: InvestmentContextService = investment_context_service,
    ) -> None:
        self.conversations = conversations
        self.agent = agent
        self.runtime_state = runtime_state
        self.observability = observability
        self.investment_context = investment_context

    @traced("agent.chat", **{"langfuse.observation.type": "agent"})
    async def chat(
        self,
        request: AgentChatRequest,
        *,
        execution_context: AgentExecutionContext | None = None,
        progress_callback: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    ) -> ComprehensiveAgentChatResponse:
        """运行一次带持久化上下文的综合 Agent 对话。"""

        # Worker 已恢复的授权必须继续生效；None 的纯服务旧调用不自动签发授权。
        task_auth.check_active_task_authorization()
        inherited = current_agent_execution_context()
        if inherited.task_grant_id:
            if execution_context is not None and (
                execution_context.user_id != inherited.user_id
                or execution_context.task_grant_id not in (None, inherited.task_grant_id)
            ):
                raise task_auth.AgentAuthorizationError("grant_identity_mismatch", "不能替换当前任务授权。")
            execution_context = inherited
        if execution_context is not None and execution_context.user_id != request.user_id:
            raise task_auth.AgentAuthorizationError("grant_identity_mismatch", "请求用户与可信执行身份不一致。")

        with agent_execution_scope(execution_context) if execution_context is not None else nullcontext():
            task_auth.check_active_task_authorization()
            conversation = self.conversations.ensure(
                user_id=request.user_id, conversation_id=request.conversation_id)
            # 授权前只读经过会话归属验证的引用，不读取历史全文或报告证据。
            previous = self.conversations.latest_run_metadata(
                user_id=request.user_id, conversation_id=conversation["id"])
            portfolio_ids = selected_chat_portfolio_ids(request, previous)
            if requires_chat_task_authorization(execution_context) and not execution_context.task_grant_id:
                task_auth.check_active_task_authorization()
                granted = await chat_stage("authorization", lambda: asyncio.to_thread(task_auth.get_task_authorization_service().create,
                    execution_context, run_id=current_ai_scope().run_id,
                    agent_id="main", portfolio_ids=portfolio_ids))
                execution_context = replace(execution_context, capabilities=granted.capabilities,
                    task_grant_id=granted.task_grant_id, task_run_id=granted.task_run_id,
                    task_agent_id=granted.task_agent_id)

            # 根 scope 包住全部预加载、辅助模型、Agent Loop、持久化与返回。
            with agent_execution_scope(execution_context) if execution_context is not None else nullcontext():
                task_auth.check_active_task_authorization()
                authorization = (await chat_stage("authorization_metadata", lambda: asyncio.to_thread(
                    chat_task_authorization_metadata, execution_context, portfolio_ids))
                    if execution_context is not None and execution_context.task_grant_id else None)

                async def checked_progress(event):
                    task_auth.check_active_task_authorization()
                    await progress_callback(event)
                    task_auth.check_active_task_authorization()

                callback = checked_progress if progress_callback is not None else None
                if authorization and callback is not None:
                    await callback(chat_authorization_event(authorization))
                if authorization and portfolio_ids:
                    request = request.model_copy(update={"portfolio_id": portfolio_ids[0]})
                response = await self._chat_in_scope(request, conversation=conversation,
                    previous=previous, authorization=authorization,
                    execution_context=execution_context, progress_callback=callback)
                task_auth.check_active_task_authorization()
                return response

    async def _chat_in_scope(
        self, request: AgentChatRequest, *, conversation: dict, previous: dict,
        authorization: dict | None, execution_context: AgentExecutionContext | None,
        progress_callback: Callable[[dict[str, Any]], Awaitable[None]] | None,
    ) -> ComprehensiveAgentChatResponse:
        await self._emit_progress(
            progress_callback,
            phase="prepare",
            label="正在准备会话与业务上下文",
        )
        task_auth.check_active_task_authorization()
        history = await chat_stage("conversation_context", lambda: asyncio.to_thread(
            self._history_for_authorization, request, conversation, previous, authorization))
        # 报告编号由服务端读取并验证；每一轮只保存引用，不重复把整份报告写进聊天。
        task_auth.check_active_task_authorization()
        fund_report_reference, fund_report_context = await chat_stage("fund_report_context",
            lambda: asyncio.to_thread(resolve_fund_report_context, request, previous))
        task_auth.check_active_task_authorization()
        if fund_report_context:
            history = [*history, fund_report_context]
            request = request.model_copy(update={"fund_code": fund_report_reference["fund_code"],
                "fund_report_ids": fund_report_reference["document_ids"], "research_job_id": ""})
        research_reference, report_context = await chat_stage("portfolio_report_context",
            lambda: asyncio.to_thread(resolve_report_context, request, previous))
        task_auth.check_active_task_authorization()
        if report_context:
            history = [*history, report_context]
            request = request.model_copy(update={"portfolio_id": research_reference["portfolio_id"]})
        await self._emit_progress(
            progress_callback,
            phase="context",
            label=f"已载入 {len(history)} 条近期对话",
        )

        # 核心 Agent 只接收准备好的历史消息，不知道 SQLAlchemy 和 HTTP 的存在。
        run_request = request.model_copy(
            update={"conversation_id": conversation["id"]}
        )
        # 背景属于经过归属检查的会话，而不是浏览器可伪造的请求字段或模型工具参数。
        background = await chat_stage("investment_context", lambda: self.investment_context.prepare(
            message=request.message,
            previous=previous.get("investment_context") or {},
            fund_code=request.fund_code,
            portfolio_id=request.portfolio_id,
        ))
        task_auth.check_active_task_authorization()
        if authorization and background.portfolio_id != request.portfolio_id:
            # 投资条件的抽取结果不是授权凭据，不能借“继续上次研究”扩大组合范围。
            background.portfolio_id = request.portfolio_id
            background.resume_research = False
            background.research_question, background.pending_fields = "", []
        if background.extraction_attempted or background.update_status == "cleared":
            await self._emit_progress(
                progress_callback, phase="investment_context",
                label=("投资背景需重新确认" if background.needs_confirmation else "已整理本轮投资背景"),
            )
        # 仅在用户补答同一研究追问时恢复研究对象；不改变普通新问题的选中对象规则。
        if background.resume_research and not (request.fund_code or request.portfolio_id):
            run_request = run_request.model_copy(update={
                "fund_code": background.fund_code,
                "portfolio_id": request.portfolio_id if authorization else background.portfolio_id,
            })
        # 计时只覆盖 Agent 执行本身，不把前后数据库读写混入模型延迟指标。
        run_started = perf_counter()
        agent_kwargs: dict[str, Any] = {"history": history}
        # HTTP 产品/开发入口总会传入可信上下文；保留 None 兼容纯服务测试和旧 Worker，
        # 底层 ToolCallingAgentService 会将它安全降级为产品模式。
        if execution_context is not None:
            agent_kwargs["execution_context"] = execution_context
        if progress_callback is not None:
            agent_kwargs["progress_callback"] = progress_callback
        task_auth.check_active_task_authorization()
        with investment_context_scope(background):
            run = await self.agent.chat(run_request, **agent_kwargs)
        task_auth.check_active_task_authorization()
        self.investment_context.finish(background, run.tool_calls)
        fund_report_reference = report_reference_from_run(run, fund_report_reference)
        duration_ms = (perf_counter() - run_started) * 1000

        # 只在 Agent 成功拿到最终答案后保存一整个 exchange，避免失败请求留下一条
        # 没有助手回复的孤立用户消息。
        allocation_refs = self._portfolio_allocation_refs(run)
        if allocation_refs:
            ref = allocation_refs[-1]
            run.answer += f"\n\n约束草案：`{ref['draft_id']}`（组合 `{ref['portfolio_id']}`）。在[组合约束求解](#portfolio/optimizer)读取该记录。"
        stress_refs = self._portfolio_stress_runs(run)
        if stress_refs:
            # 由实际工具结果生成，而不是要求模型记住输出编号。写入助手原文使现有
            # 跨轮历史能保留情景；上下文已丢失时 Prompt 要求追问，不默认偷换最新记录。
            ref = stress_refs[-1]
            run.answer += f"\n\n压力测试记录：`{ref['run_id']}`（组合 `{ref['portfolio_id']}`）。"
        await self._emit_progress(
            progress_callback,
            phase="persist",
            label="回答已生成，正在保存会话",
        )
        task_auth.check_active_task_authorization()
        conversation, user_message, assistant_message = (
            self.conversations.append_exchange(
                user_id=request.user_id,
                conversation_id=conversation["id"],
                user_content=request.message,
                assistant_content=run.answer,
                run_id=run.run_id,
                expected_updated_at=conversation["updated_at"],
                # 与本轮两条消息在同一事务提交，失败/取消请求不覆盖上次成功状态。
                run_metadata={
                    **self._compact_run_metadata(run, duration_ms=duration_ms),
                    "investment_context": background.model_dump(mode="json"),
                    "research_reference": research_reference,
                    "fund_report_reference": fund_report_reference,
                    **({"task_authorization": authorization} if authorization else {}),
                },
            )
        )

        # 聊天记录面向用户回看；Observation 面向质量统计和故障排查。
        task_auth.check_active_task_authorization()
        self.observability.record_run(
            run=run,
            user_id=request.user_id,
            duration_ms=duration_ms,
            source="chat",
            conversation_id=conversation["id"],
            metrics={"request_id": current_request_id()},
        )

        workspace = self.runtime_state.workspace_snapshot(
            user_id=request.user_id,
            tool_names=run.tools_available,
        )
        response = ComprehensiveAgentChatResponse(
            conversation=AgentConversationSummary(**conversation),
            user_message=AgentConversationMessageInfo(**user_message),
            assistant_message=AgentConversationMessageInfo(**assistant_message),
            run=run,
            workspace=workspace,
        )
        await self._emit_progress(
            progress_callback,
            phase="complete",
            label=("本轮已停止，已保存已完成范围与缺口" if run.stop_reason == "time_budget_exhausted"
                   else "本轮研究已完成"),
        )
        return response

    def _history_for_authorization(self, request, conversation, previous, authorization):
        if authorization is None:
            return self.conversations.context_messages(
                user_id=request.user_id, conversation_id=conversation["id"])
        old = previous.get("task_authorization") or {}
        same_scope = old.get("portfolio_ids") == authorization["portfolio_ids"]
        start = old.get("history_start_message_count") if same_scope else None
        if not isinstance(start, int) or not 0 <= start <= conversation["message_count"]:
            start = conversation["message_count"]
        authorization["history_start_message_count"] = start
        if not same_scope:
            previous.pop("investment_context", None)
        count = conversation["message_count"] - start
        if not count:
            return []
        # 滚动摘要混合了不同授权，不能先读全文再过滤。只查询当前授权段的有限尾部，
        # 截止时间固定在本轮开始时，其他进程后来写入的不同组合也不能混入预加载。
        with self.conversations.database.session() as session:
            rows = session.execute(select(AgentConversationMessageModel.role, AgentConversationMessageModel.content)
                .where(AgentConversationMessageModel.user_id == request.user_id,
                    AgentConversationMessageModel.conversation_id == conversation["id"],
                    AgentConversationMessageModel.created_at <= datetime.fromisoformat(conversation["updated_at"]))
                .order_by(AgentConversationMessageModel.created_at.desc())
                .limit(min(count, self.conversations.context_message_limit))).all()
        remaining = self.conversations.context_char_limit
        history = []
        for role, content in rows:
            if remaining <= 0:
                break
            history.append({"role": role, "content": content[:remaining]})
            remaining -= len(history[-1]["content"])
        return list(reversed(history))

    @staticmethod
    async def _emit_progress(
        callback: Callable[[dict[str, Any]], Awaitable[None]] | None,
        *,
        phase: str,
        label: str,
        **details: Any,
    ) -> None:
        if callback is not None:
            await callback(
                {
                    "type": "phase",
                    "phase": phase,
                    "label": label,
                    **details,
                }
            )

    def _compact_run_metadata(
        self,
        run: Any,
        *,
        duration_ms: float,
    ) -> dict[str, Any]:
        """保留界面回看需要的运行摘要，不把完整工具结果重复写入消息表。"""

        return {
            "stop_reason": run.stop_reason,
            "model": run.used_model,
            "duration_ms": round(duration_ms, 2),
            "request_id": current_request_id(),
            "total_tokens": run.usage.total_tokens,
            # 动态路由信息用于历史消息回看，帮助判断某次回答为什么只拿到这些工具。
            "route": run.route.model_dump(),
            # 保存本轮目录搜索和 Schema 变化，历史回看不重新搜索，也不复活旧工具池。
            "tool_discovery": run.tool_discovery.model_dump(mode="json"),
            "loop_control": run.loop_control.model_dump(mode="json"),
            "execution_budget": run.execution_budget,
            "selected_tool_count": len(run.tools_available),
            "prompt_chars": run.prompt_trace.total_chars,
            "round_count": len(run.rounds),
            "tool_calls": [
                {
                    "name": call.tool_name,
                    "is_error": call.is_error,
                    "recovery_trace": call.recovery_trace,
                }
                for call in run.tool_calls
            ],
            # 专用组合研究 Tool 只在这里保存轻量 Job 引用。完整报告留在 Job Store；
            # 浏览器刷新历史会话后可用 job_id 重新连接 SSE，而不会复制整份报告到消息表。
            "portfolio_research_jobs": self._portfolio_research_jobs(run),
            "portfolio_stress_runs": self._portfolio_stress_runs(run),
            "portfolio_allocation_drafts": self._portfolio_allocation_refs(run),
            "research_repairs": self._research_repairs(run),
            # 与正文一起持久化；历史回看无需重新调用模型或重新查库生成证据。
            "answer_check": run.answer_check.model_dump(mode="json") if run.answer_check else None,
            "report_answer_check": run.report_answer_check.model_dump(mode="json") if run.report_answer_check else None,
            # 来源卡片随消息保存，刷新历史会话后仍可追踪本轮使用过的事实。
            "evidence": [item.model_dump(mode="json") for item in run.evidence],
            "pipeline_steps": list(run.pipeline_steps),
            "hook_count": len(run.hooks_trace),
            "recovery_count": len(run.recovery_trace),
            "compaction_count": len(run.context_trace),
        }

    @staticmethod
    def _portfolio_allocation_refs(run: Any) -> list[dict[str, str]]:
        """草案引用由实际工具回执写进历史，不依赖模型自报确认状态。"""
        refs = {}
        for call in run.tool_calls:
            if call.is_error or call.tool_name not in {"draft_portfolio_allocation_tool", "get_portfolio_allocation_tool"}:
                continue
            try:
                payload = json.loads(call.tool_result)
            except (ValueError, TypeError):
                continue
            if isinstance(payload, dict) and payload.get("ok") and payload.get("draft_id") and payload.get("portfolio_id"):
                refs[payload["draft_id"]] = {key: payload[key] for key in ("draft_id", "portfolio_id")}
        return list(refs.values())

    @staticmethod
    def _portfolio_stress_runs(run: Any) -> list[dict[str, str]]:
        """只从成功工具回执提取情景引用，不从模型文字猜测记录。"""
        refs = {}
        for call in run.tool_calls:
            if call.is_error or call.tool_name not in {"run_portfolio_stress_tool", "get_portfolio_stress_tool"}:
                continue
            try:
                payload = json.loads(call.tool_result)
            except (ValueError, TypeError):
                continue
            if isinstance(payload, dict) and payload.get("ok") and payload.get("run_id"):
                refs[payload["run_id"]] = {key: payload[key] for key in ("run_id", "root_run_id", "portfolio_id")}
        return list(refs.values())

    @staticmethod
    def _research_repairs(run: Any) -> list[dict[str, Any]]:
        """随助手消息保存轻量补查记录，刷新页面后仍能看到查过什么和剩下什么。"""

        from app.services.financial_research_repair import BRIEF_TOOLS

        reports = []
        for call in run.tool_calls:
            if call.tool_name not in BRIEF_TOOLS or call.is_error:
                continue
            try:
                payload = json.loads(call.tool_result)
            except (TypeError, ValueError):
                continue
            if isinstance(payload, dict) and payload.get("ok") and payload.get("evidence_repair"):
                reports.append({"scope": payload.get("scope", {}), **payload["evidence_repair"]})
        return reports

    @staticmethod
    def _portfolio_research_jobs(run: Any) -> list[dict[str, Any]]:
        """从工具审计结果中提取前端恢复研究进度所需的最小字段。"""

        jobs: dict[str, dict[str, Any]] = {}
        research_tools = {
            "start_portfolio_research_tool",
            "get_portfolio_research_status_tool",
        }
        for call in run.tool_calls:
            if call.tool_name not in research_tools or call.is_error:
                continue
            try:
                payload = json.loads(call.tool_result)
            except (json.JSONDecodeError, TypeError):
                continue
            if not isinstance(payload, dict) or payload.get("ok") is not True:
                continue
            job = payload.get("job")
            if not isinstance(job, dict) or not job.get("id"):
                continue
            job_id = str(job["id"])
            jobs[job_id] = {
                "id": job_id,
                "job_type": "portfolio_research",
                "title": job.get("title"),
                "status": job.get("status"),
                "current_step": job.get("current_step", 0),
                "total_steps": job.get("total_steps", 8),
                "progress_percent": job.get("progress_percent", 0),
                "progress_message": job.get("progress_message"),
                "error": job.get("error"),
                "portfolio_id": payload.get("portfolio_id"),
                "portfolio_name": payload.get("portfolio_name"),
                "period_months": payload.get("period_months"),
                "events_endpoint": payload.get("events_endpoint"),
                # status 工具若读到终态，只保存已经裁剪过的报告摘要。
                "report": payload.get("report"),
            }
        return list(jobs.values())


comprehensive_agent_service = ComprehensiveAgentService()
