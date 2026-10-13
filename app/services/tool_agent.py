"""综合 Agent 的核心 Tool Calling Loop。

这个文件演示的是 Agent 的核心雏形：

用户问题
-> 模型判断是否需要工具
-> 后端执行工具
-> 工具结果交回模型
-> 模型继续判断是否还要工具
-> 直到模型不再调用工具，生成最终回答

主循环保持“单 Agent、多轮工具调用”的稳定结构；Permission、Hooks、Context、
Recovery 和动态工具作为外围能力接入。产品会话与工作区快照由
`ComprehensiveAgentService` 负责，避免 HTTP/数据库职责进入核心循环。
"""

from __future__ import annotations

import asyncio
import json
from contextlib import nullcontext
from dataclasses import replace
from time import monotonic
from typing import Any, Awaitable, Callable
from uuid import uuid4

from app.config import settings
from app.services.tracing import traced
from app.schemas import (
    AgentChatRequest,
    AgentChatResponse,
    AgentContextCompactTrace,
    AgentHookTrace,
    AgentLoopRound,
    AgentLoopControl,
    AgentPromptSection,
    AgentPromptTrace,
    AgentRecoveryTrace,
    AgentRouteInfo,
    AgentTokenUsage,
    AgentToolCall,
    AgentToolDiscoveryTrace,
    FundAnswerReport,
    FundReportAnswerReport,
)
from app.services.agent_routing import AgentRequestRoute, agent_request_router
from app.services.agent_tools import (
    get_agent_tools,
    get_product_agent_tools,
    select_agent_tools,
)
from app.services.agent_execution_context import AgentExecutionContext
from app.services.agent_runtime_state import agent_runtime_state_service
from app.services.agent_tool_executor import agent_tool_executor
from app.services.context_compactor import CompactTrace, context_compactor_service
from app.services.errors import AgentConfigError, AgentRuntimeError
from app.services.evidence import (
    ensure_answer_evidence_index,
    extract_tool_evidence_records,
)
from app.services.llm import get_chat_model
from app.services.ai_governance_context import AIControlError
from app.services.fund_answer import FundAnswerCheck, render_answer_text
from app.services.fund_report_answer import FundReportAnswerCheck, render_report_answer
from app.services.fund_report_query import report_query_profile, select_report_context, supports_evidence_synthesis
from app.services.fund_report_runtime import ReportExecution, ReportExecutionError
from app.services.investment_context import current_investment_context
from app.services.mcp_tools import mcp_tool_service
from app.services.prompt_builder import PromptBuildResult, prompt_builder_service
from app.services.recovery import (
    ModelCallResult,
    ModelRecoveryError,
    RecoveryTrace,
    model_recovery_service,
)
from app.services.tool_hooks import HookTrace
from app.services.tool_discovery import ToolDiscoveryRun
from app.services.agent_loop_guard import LoopGuard, loop_guard_scope, current_loop_guard
from app.services.tool_recovery import tool_recovery_scope
from app.services.chat_execution_budget import (
    ChatTimeBudgetExceeded, chat_stage, current_chat_budget, current_loop_progress,
    loop_progress_scope, partial_loop_response,
)


# 季报修正只核对格式和引用，不再展开投研推理；整体限时也覆盖供应商故障切换。
_REPORT_REPAIR_TIMEOUT_SECONDS = 20
# 不是额外加15秒：从已有整理轮截止时间中预留，最多使用原先剩余的一次尝试。
_REPORT_SYNTHESIS_FALLBACK_SECONDS = 15
_REPORT_MIN_RETRY_SECONDS = 3


# 这些工具一次就返回当前请求所需的完整只读快照。成功后下一轮只做答案整理，继续
# 暴露同一个工具只会增加重复查询、Token 和外部数据源压力。RAG、文件搜索等探索型
# 工具不在这里，因为它们可能确实需要多轮改变查询条件。
FINALIZE_AFTER_SUCCESS_TOOLS = frozenset(
    {
        "get_retail_indices_tool",
        "get_fund_profile_tool",
        "get_fund_report_research_tool",
        "get_fund_nav_summary_tool",
        "get_fund_risk_tool",
        "get_fund_assessment_tool",
        "get_fund_semantic_review_tool",
        "get_fund_research_brief_tool",
        "get_market_sentiment_tool",
        "search_market_events_tool",
        "get_portfolio_assessment_tool",
        "get_portfolio_performance_tool",
        "get_portfolio_exposure_tool",
        "get_portfolio_event_impacts_tool",
        "list_portfolio_decisions_tool",
        "start_portfolio_research_tool",
        "get_portfolio_research_status_tool",
        "get_portfolio_research_brief_tool",
    }
)

# 只有明确无副作用的读取工具才能在同一模型轮次中并发。写文件、更新任务、启动 Job、
# 连接 MCP 等操作仍严格按模型给出的顺序执行，避免先后关系和权限审计变得不可预测。
PARALLEL_SAFE_READ_TOOLS = frozenset(
    {
        "get_retail_indices_tool",
        "calculator_tool",
        "list_files_tool",
        "read_file_tool",
        "search_text_tool",
        "rag_search_tool",
        "list_skills_tool",
        "load_skill_tool",
        "search_memory_tool",
        "task_list_tool",
        "background_job_status_tool",
        "background_job_list_tool",
        "list_teammates_tool",
        "check_team_inbox_tool",
        "list_team_protocols_tool",
        "list_autonomy_sessions_tool",
        "list_worktrees_tool",
        "list_crons_tool",
        "get_fund_profile_tool",
        "get_fund_nav_summary_tool",
        "get_fund_risk_tool",
        "get_fund_assessment_tool",
        "get_fund_semantic_review_tool",
        "get_market_sentiment_tool",
        "search_market_events_tool",
        "get_portfolio_assessment_tool",
        "get_portfolio_performance_tool",
        "get_portfolio_exposure_tool",
        "get_portfolio_event_impacts_tool",
        "list_portfolio_decisions_tool",
        "get_portfolio_research_status_tool",
    }
)


class ToolCallingAgentService:
    """把 LangChain tools、模型和执行流程组织起来的 service。"""

    def list_tools(
        self,
        *,
        execution_context: AgentExecutionContext | None = None,
    ) -> list[dict[str, Any]]:
        """返回当前模式可见的工具；产品清单永远不包含开发与 MCP 工具。"""

        context = execution_context or AgentExecutionContext.product(
            user_id="guest",
            source="tool-list-safe-default",
        )
        built_in_tools = (
            get_product_agent_tools()
            if context.mode == "product"
            else get_agent_tools()
        )
        tools = (
            mcp_tool_service.assemble_tool_pool(built_in_tools)
            if context.mode == "developer"
            else built_in_tools
        )
        return [
            {
                "name": tool.name,
                "description": tool.description or "",
                "args_schema": getattr(tool, "args", {}) or {},
            }
            for tool in tools
        ]

    async def chat(
        self,
        request: AgentChatRequest,
        *,
        history: list[dict[str, str]] | None = None,
        execution_context: AgentExecutionContext | None = None,
        progress_callback: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    ) -> AgentChatResponse:
        """所有工具轮次共享任务授权与预算；退出时解绑上下文，不延长或撤销授权。"""

        from contextlib import nullcontext
        from app.services.financial_research_repair import research_repair_scope, current_repair_budget
        from app.services.agent_execution_context import agent_execution_scope, current_agent_execution_context
        from app.services.agent_authorization import AgentAuthorizationError, check_active_task_authorization

        # 作用域覆盖路由、提示词准备、模型和所有工具；不能只包住最后一次 Python 调用。
        # 后台入口已经恢复 grant 时也应继承它，不因为省略参数就退回全量产品权限。
        inherited = current_agent_execution_context()
        context = execution_context
        if inherited.task_grant_id:
            # 旧调用方可能显式传入一个未绑定 grant 的 product context；它不能清除
            # 上层授权。另一个身份或 grant 则直接拒绝，不在嵌套调用中暗中切换任务。
            if context and (context.user_id != inherited.user_id or (
                    context.task_grant_id and (
                        context.task_grant_id, context.task_run_id, context.task_agent_id
                    ) != (inherited.task_grant_id, inherited.task_run_id, inherited.task_agent_id))):
                raise AgentAuthorizationError("grant_identity_mismatch", "嵌套 Agent 不能替换当前任务授权。")
            context = inherited
        if context and context.task_grant_id and request.user_id != context.user_id:
            raise AgentAuthorizationError("grant_identity_mismatch", "请求用户与当前任务授权不一致。")
        scope = agent_execution_scope(context) if context else nullcontext()
        repair = (nullcontext() if current_repair_budget() is not None
            else research_repair_scope(allow_public_refresh=request.research_refresh))
        # 防空转状态只属于这一次调用；子 Agent 新建自己的状态，不能读父缓存。
        guard = LoopGuard(request, enabled=settings.agent_loop_guard_enabled
                          and (context is None or context.mode == "product"))
        with scope, repair, loop_guard_scope(guard), tool_recovery_scope(), loop_progress_scope() as progress:
            await asyncio.to_thread(check_active_task_authorization)
            try:
                result = await chat_stage("agent_loop", lambda: self._chat(request,
                    history=history, execution_context=context, progress_callback=progress_callback), closing=True)
            except ChatTimeBudgetExceeded:
                # 仅捕获自己的截止信号。用户取消、权限撤销仍向上传播，不能输出旧证据。
                await asyncio.to_thread(check_active_task_authorization)
                result = partial_loop_response(request, progress, guard)
                await self._emit_progress(progress_callback, phase="time_budget",
                    label="已到执行时限，正在保存已完成的范围与缺口")
            budget = current_chat_budget()
            if budget:
                result.execution_budget = budget.snapshot()
            await asyncio.to_thread(check_active_task_authorization)
            return result

    async def _chat(
        self, request: AgentChatRequest, *, history: list[dict[str, str]] | None = None,
        execution_context: AgentExecutionContext | None = None,
        progress_callback: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    ) -> AgentChatResponse:
        """执行一次多轮 Agent Loop 对话。

        history 是产品会话层传入的最近用户/助手消息。后台 Job 等内部调用不传 history，
        仍然可以复用同一个无状态 Agent 内核。
        """

        started_at = monotonic()
        try:
            from langchain_core.messages import (
                AIMessage,
                HumanMessage,
                SystemMessage,
                ToolMessage,
            )
        except ModuleNotFoundError as exc:
            raise AgentConfigError("请先安装依赖：pip install -r requirements.txt") from exc

        history = history or []
        context = execution_context or AgentExecutionContext.product(
            user_id=request.user_id,
            source="agent-safe-default",
        )
        if context.user_id != request.user_id:
            raise AgentRuntimeError("Agent 请求用户与可信执行上下文不一致")

        # 明确意图先由确定性规则把总工具池缩小；只有带业务上下文、规则又无法判断的
        # 模糊问题才调用受限语义分类器。分类器只能选领域，不能直接执行或发明工具。
        background = current_investment_context()
        routing_request = request.model_copy(update={
            "message": background.effective_question(request.message),
        }) if background is not None else request
        route = AgentRequestRoute(
            name="direct", domains=(), tool_names=(), include_mcp_tools=False,
            reason="用户只更新本会话投资背景，无需查询行情或调用开发者记忆工具",
        ) if background is not None and background.background_only and not request.research_refresh else await chat_stage("routing", lambda: agent_request_router.route_hybrid(
            routing_request,
            history=history,
            execution_context=context,
        ))
        base_tools = select_agent_tools(
            route.tool_names,
            execution_context=context,
        )
        guard = current_loop_guard()
        if guard is not None:
            guard.configure(route)
        tools = self._assemble_routed_tools(
            base_tools=base_tools,
            route=route,
            execution_context=context,
        )
        # 首轮仍由路由器选择。复杂只读问题额外绑定一个请求内的发现入口，目录留在
        # 后端，不会把全部产品工具 Schema 一次塞进模型；简单/固定流程原样执行。
        discovery = ToolDiscoveryRun(
            request=request, route=route, context=context,
            initial_tools=tools, registry=get_product_agent_tools,
        )
        tools = discovery.tools()
        tool_map = {tool.name: tool for tool in tools}
        tool_names = [tool.name for tool in tools]
        await self._emit_progress(
            progress_callback,
            phase="route",
            label=(
                f"已选择 {len(tool_names)} 个相关工具"
                if tool_names
                else "当前问题可直接回答"
            ),
            route=route.name,
            tool_count=len(tool_names),
        )

        # s10 System Prompt Assembly：
        # 每次请求都根据当前用户、工具、skills、memory 和 todos 动态组装提示词。
        # prompt_result 既包含最终字符串，也保留各 section 供接口调试展示。
        prompt_result = prompt_builder_service.build(
            user_id=request.user_id,
            tool_names=tool_names,
            active_fund_code=request.fund_code,
            active_portfolio_id=request.portfolio_id,
            domains=route.domains,
        )
        prompt_trace = self._prompt_result_to_schema(prompt_result)

        run_id = f"run_{uuid4().hex[:16]}"
        if route.name == "access_denied":
            # 权限拒绝不需要消耗模型 Token。更重要的是，它不会让模型凭已有知识猜测
            # 项目源码内容，产品用户会得到稳定且可审计的模式说明。
            return self._build_response(
                run_id=run_id,
                request=request,
                answer=(
                    "当前是基金产品模式，只能使用基金、组合、市场、知识库和计算能力。"
                    "文件读取、代码搜索、Worktree 与 MCP 管理仅在管理员开发者入口开放。"
                ),
                stop_reason="permission_denied",
                tool_names=[],
                executed_tool_calls=[],
                prompt_trace=prompt_trace,
                recovery_trace=[],
                hooks_trace=[],
                context_trace=[],
                rounds=[],
                pipeline_steps=[
                    f"Create agent run {run_id}",
                    "Receive user question",
                    f"Reject unauthorized product route: {route.reason}",
                ],
                usage=AgentTokenUsage(),
                route=route,
                tool_discovery=discovery.trace,
            )

        # bind_tools 的意思是：把工具清单告诉模型。
        # 模型看到用户问题后，可以选择直接回答，也可以返回 tool_calls。
        # 问候、改写等 direct 路由可能没有工具；此时直接使用模型，避免向供应商发送
        # 空 tools 定义导致兼容性问题。
        # s11 让 Agent 自己的 recovery service 负责可观察重试，因此关闭模型 SDK 内部
        # 不可见的自动重试。权限拒绝已经在上面返回，不会创建模型客户端。
        report_profile = (report_query_profile(request.message, request.fund_code or "")
            if route.tool_names == ("get_fund_report_research_tool",) and not (guard and guard.required) else None)
        # 只对明确的数字题关闭深度思考；混合/未知问题和普通Agent保持原模型设置。
        model_options = {"thinking": False} if report_profile and report_profile["mode"] == "allocation" else {}
        model = get_chat_model(
            max_retries=0,
            timeout_seconds=settings.agent_model_timeout_seconds,
            **model_options,
        )
        model_with_tools = model.bind_tools(tools) if tools else model
        # 准备耗时也应包含模型客户端初始化，不能把第一次导入SDK的时间漏掉。
        report_execution = (ReportExecution(run_id=run_id, started_at=started_at,
            query_mode=report_profile["mode"]) if report_profile else None)

        messages: list[Any] = [SystemMessage(content=prompt_result.system_prompt)]

        # 持久化层只把最终用户/助手消息放回上下文，不回放旧 run 的完整工具轨迹。
        # 这样既能实现多轮对话，也不会把历史工具结果重复塞进模型输入。
        for item in history:
            content = str(item.get("content") or "")
            if item.get("role") == "system":
                # ConversationService 把较早原文压成滚动摘要，以额外 SystemMessage
                # 提供背景；它不会替换本次请求真正的主系统提示词。
                messages.append(SystemMessage(content=content))
            elif item.get("role") == "assistant":
                messages.append(AIMessage(content=content))
            elif item.get("role") == "user":
                messages.append(HumanMessage(content=content))
        messages.append(HumanMessage(content=request.message))
        if guard is not None and guard.instruction():
            messages.append(SystemMessage(content=guard.instruction()))

        executed_tool_calls: list[AgentToolCall] = []
        answer_check_context: FundAnswerCheck | None = None
        answer_check_instruction: Any | None = None
        # 季报是另一种窄业务协议，不影响普通聊天或单基金综合研究的既有回答结构。
        report_check_context = (FundReportAnswerCheck(None, fund_code=request.fund_code or "")
            if report_profile else None)
        report_message = report_check_instruction = None
        hooks_trace: list[AgentHookTrace] = []
        context_trace: list[AgentContextCompactTrace] = []
        recovery_trace: list[AgentRecoveryTrace] = []
        rounds: list[AgentLoopRound] = []
        # 每一轮模型调用都会把供应商返回的 token usage 累加到这里。
        # 工具本身不消耗模型 token，因此不参与统计。
        usage = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
        progress = current_loop_progress()
        if progress:
            progress.run_id, progress.messages = run_id, messages
            progress.visible_calls, progress.usage = executed_tool_calls, usage
        pipeline_steps = [
            f"Create agent run {run_id}",
            "Receive user question",
            (
                f"Route request as {route.name}: selected {len(tools)} tool(s), "
                f"reason={route.reason}"
            ),
            f"Load {len(history)} persisted conversation message(s)",
            (
                "Assemble system prompt from "
                f"{prompt_trace.section_count} sections, "
                f"total_chars={prompt_trace.total_chars}"
            ),
            "Bind tools to chat model",
            f"Tool pool contains {len(mcp_tool_service.list_tools())} MCP tool(s)",
            f"Start agent loop, max_tool_rounds={request.max_tool_rounds}",
        ]
        if progress:
            progress.rounds, progress.pipeline_steps = rounds, pipeline_steps
            progress.tool_names, progress.prompt_trace = tool_names, prompt_trace
        if report_profile:
            pipeline_steps.append(f"Report query profile: {report_profile['mode']}; detailed={report_profile['detailed']}")
        if discovery.trace.enabled:
            pipeline_steps.append(
                f"Tool discovery enabled: max_searches={discovery.max_calls}, top_k={discovery.top_k}, "
                f"initial_schema_chars={discovery.trace.bindings[0]['schema_chars']}"
            )

        forced_reason = "max_tool_rounds_reached"
        for round_index in range(1, request.max_tool_rounds + 1):
            budget = current_chat_budget()
            if budget and monotonic() >= budget.work_deadline:
                # 停止扩展查询，剩余的预留时间只允许无工具整理及结果保存。
                forced_reason = "time_budget_exhausted"
                budget.expired_phase = "work"
                if not executed_tool_calls:
                    raise ChatTimeBudgetExceeded("work")
                round_index -= 1  # 这一轮尚未调用模型，收尾应使用下一个真实轮号。
                break
            if progress:
                progress.messages = messages
            if discovery.trace.enabled:
                # 新工具只在上一轮全部 ToolMessage 已回填后生效。同轮模型猜测调用的
                # 未加载工具仍会被旧 tool_map 拒绝，不能用搜索结果绕过执行顺序。
                if round_index == request.max_tool_rounds:
                    discovery.finish_search()
                next_tools = discovery.tools()
                next_names = [tool.name for tool in next_tools]
                if next_names != tool_names:
                    tools, tool_names = next_tools, next_names
                    tool_map = {tool.name: tool for tool in tools}
                    prompt_result = prompt_builder_service.build(
                        user_id=request.user_id, tool_names=tool_names,
                        active_fund_code=request.fund_code,
                        active_portfolio_id=request.portfolio_id,
                        domains=discovery.domains(route.domains),
                    )
                    prompt_trace = self._prompt_result_to_schema(prompt_result)
                    messages[0] = SystemMessage(content=prompt_result.system_prompt)
                    model_with_tools = model.bind_tools(tools) if tools else model
                    discovery.record_binding(round_index=round_index)
                    pipeline_steps.append(
                        f"Round {round_index}: discovery rebuilt tool pool; "
                        f"loaded={','.join(discovery.trace.loaded_tools)}, "
                        f"schema_chars={discovery.trace.bindings[-1]['schema_chars']}"
                    )
                    await self._emit_progress(
                        progress_callback, phase="tool_discovery",
                        label="已更新本轮可用的只读工具", round_index=round_index,
                        loaded_tools=list(discovery.trace.loaded_tools),
                    )
            await self._emit_progress(
                progress_callback,
                phase="model",
                label=("正在根据原文整理季报分析" if report_execution and report_message is not None
                    else f"模型正在进行第 {round_index} 轮判断"),
                round_index=round_index,
            )
            try:
                model_call = await self._invoke_model(
                    report_execution=report_execution,
                    report_phase="synthesis" if report_message is not None else "model_decision",
                    runnable=model_with_tools,
                    messages=messages,
                    phase="agent_round",
                    round_index=round_index,
                    max_attempts=settings.agent_model_max_attempts,
                    # 续写时使用未绑定工具的模型，避免截断回答又触发工具。
                    continuation_runnable=model,
                )
            except (AgentConfigError, ReportExecutionError, AIControlError):
                raise
            except ModelRecoveryError as exc:
                if exc.is_timeout:
                    raise AgentRuntimeError(
                        f"模型第 {round_index} 轮响应超时，有限重试后仍未完成。"
                        "可能是模型服务繁忙或服务器到模型的网络不稳定，请稍后重试；"
                        "原问题已保留。这不是工具调用轮数用完。"
                    ) from exc
                raise AgentRuntimeError(
                    f"Agent 第 {round_index} 轮经过错误恢复后仍失败：{exc}"
                ) from exc
            except Exception as exc:
                raise AgentRuntimeError(f"Agent 第 {round_index} 轮调用大模型失败：{exc}") from exc

            self._record_model_recovery(
                result=model_call,
                recovery_trace=recovery_trace,
                context_trace=context_trace,
                pipeline_steps=pipeline_steps,
            )
            self._accumulate_model_usage(usage, model_call)
            response = model_call.response
            messages.append(response)
            tool_calls = getattr(response, "tool_calls", None) or []

            if not tool_calls:
                answer = self._model_call_text(model_call)
                # 模型停止不代表证据齐全。仅支持明确业务清单，至多纠正一次且仍占原轮数。
                correction = guard.request_correction(messages, executed_tool_calls,
                    can_continue=round_index < request.max_tool_rounds) if guard else None
                if correction:
                    messages.pop()  # 未验证的最终草稿不继续污染下一轮上下文。
                    messages.append(SystemMessage(content=correction))
                    model_with_tools = model.bind_tools(tools) if tools else model
                    rounds.append(AgentLoopRound(round_index=round_index, model_action="completion_check",
                        assistant_message="取证清单未完成，继续一次有限补查。", tool_calls=[]))
                    pipeline_steps.append(f"Round {round_index}: completion check requested missing evidence")
                    await self._emit_progress(progress_callback, phase="completion_check",
                        label="发现尚未覆盖的对象或指标，正在补查缺项", round_index=round_index)
                    continue
                completion_limited = bool(guard and (guard.trace.missing or guard.boundary_issue))
                answer, answer_check = (answer, None) if completion_limited else await self._check_fund_answer(
                    check=answer_check_context, answer=answer, model=model, messages=messages,
                    round_index=round_index, usage=usage, recovery_trace=recovery_trace,
                    context_trace=context_trace, pipeline_steps=pipeline_steps,
                    progress_callback=progress_callback,
                )
                answer, report_answer_check = (answer, None) if completion_limited else await self._check_report_answer(
                    check=report_check_context, message=report_message, request=request, answer=answer, messages=messages,
                    round_index=round_index, usage=usage, recovery_trace=recovery_trace,
                    context_trace=context_trace, pipeline_steps=pipeline_steps,
                    progress_callback=progress_callback,
                    execution=report_execution,
                )
                # 结构化回答已把引用绑定在事实旁边；其他业务保留原来的来源索引。
                answer, appended_evidence = (answer, []) if answer_check or report_answer_check else ensure_answer_evidence_index(
                    answer=answer, tool_calls=executed_tool_calls,
                )
                if appended_evidence:
                    pipeline_steps.append(
                        "Final answer grounding guard appended "
                        f"{len(appended_evidence)} tool evidence ID(s)"
                    )
                stop_reason = "model_finished"
                if guard:
                    answer, stop_reason = guard.finalize(answer, messages, executed_tool_calls, reason=stop_reason)
                rounds.append(
                    AgentLoopRound(
                        round_index=round_index,
                        model_action="final_answer",
                        assistant_message=answer,
                        tool_calls=[],
                    )
                )
                pipeline_steps.append(f"Round {round_index}: model answered without tools")
                await self._emit_progress(
                    progress_callback,
                    phase="answer",
                    label=("资料或条件尚不完整，正在整理缺口" if completion_limited else "模型已完成回答，正在整理输出"),
                    round_index=round_index,
                )

                return self._build_response(
                    run_id=run_id,
                    request=request,
                    answer=answer,
                    answer_check=answer_check,
                    report_answer_check=report_answer_check,
                    stop_reason=stop_reason,
                    tool_names=tool_names,
                    executed_tool_calls=executed_tool_calls,
                    prompt_trace=prompt_trace,
                    recovery_trace=recovery_trace,
                    hooks_trace=hooks_trace,
                    context_trace=context_trace,
                    rounds=rounds,
                    pipeline_steps=pipeline_steps,
                    usage=AgentTokenUsage(**usage),
                    route=route,
                    tool_discovery=discovery.trace,
                )

            round_tool_calls: list[AgentToolCall] = []
            pipeline_steps.append(
                f"Round {round_index}: model requested {len(tool_calls)} tool call(s)"
            )

            parallel_read_round = self._can_execute_round_in_parallel(tool_calls)
            async def read_tools(_deadline=None):
                from app.services.fund_report_retrieval import report_retrieval_scope
                # ContextVar 会传播到工具线程。外层已超时的请求不能在后台继续启动精排。
                with report_retrieval_scope(_deadline):
                    return await self._execute_tool_round(
                        tool_calls=tool_calls,
                        tool_map=tool_map,
                        request=request,
                        execution_context=context,
                        ToolMessage=ToolMessage,
                        progress_callback=progress_callback,
                        round_index=round_index,
                    )
            round_results = (await report_execution.run("retrieval", read_tools,
                reserve_seconds=_REPORT_REPAIR_TIMEOUT_SECONDS, round_index=round_index)
                if report_execution else await read_tools())
            if parallel_read_round:
                pipeline_steps.append(
                    f"Round {round_index}: executed {len(tool_calls)} read-only tools concurrently"
                )

            # asyncio.gather 保留输入顺序，因此 ToolMessage 仍与模型原始 tool_calls
            # 一一对应；并发只改变等待方式，不改变回填顺序和审计顺序。
            for tool_call, executed_call, tool_message, hook_traces in round_results:
                executed_tool_calls.append(executed_call)
                round_tool_calls.append(executed_call)
                hooks_trace.extend(self._hook_traces_to_schema(hook_traces))
                messages.append(tool_message)
                if report_check_context is not None and executed_call.tool_name == "get_fund_report_research_tool":
                    # 保存消息对象，稍后压缩会原地更新content。校验器只能接受模型实际
                    # 看到的那份证据，不能从未压缩的审计日志中补回已经删掉的片段。
                    report_message = tool_message
                    select_report_context(report_message, report_profile)

                # connect_mcp_tool 会改变可用工具集合。连接完成后必须在同一次请求里
                # 重新组装 tools、tool_map、bind_tools 和 System Prompt；否则下一轮模型
                # 仍然只看到连接前的旧 schema，无法调用刚发现的 MCP 工具。
                if str(tool_call.get("name") or "") == "connect_mcp_tool":
                    base_tools = select_agent_tools(
                        route.tool_names,
                        execution_context=context,
                    )
                    tools = self._assemble_routed_tools(
                        base_tools=base_tools,
                        route=route,
                        execution_context=context,
                    )
                    tool_map = {tool.name: tool for tool in tools}
                    tool_names = [tool.name for tool in tools]
                    prompt_result = prompt_builder_service.build(
                        user_id=request.user_id,
                        tool_names=tool_names,
                        active_fund_code=request.fund_code,
                        active_portfolio_id=request.portfolio_id,
                        domains=route.domains,
                    )
                    prompt_trace = self._prompt_result_to_schema(prompt_result)
                    messages[0] = SystemMessage(
                        content=prompt_result.system_prompt
                    )
                    model_with_tools = model.bind_tools(tools) if tools else model
                    pipeline_steps.append(
                        "MCP connection changed: rebuilt tool pool with "
                        f"{len(mcp_tool_service.list_tools())} MCP tool(s)"
                    )

            # s08 Context Compact：
            # 工具结果已经进入 messages 后，检查是否过长。
            # 如果过长，就把 ToolMessage content 换成短摘要，并把完整结果保存到本地。
            compact_traces = context_compactor_service.compact_messages(
                messages,
                round_index=round_index,
            )
            if compact_traces:
                compacted_count = sum(
                    trace.compacted_messages for trace in compact_traces
                )
                context_trace.extend(
                    self._compact_traces_to_schema(compact_traces)
                )
                pipeline_steps.append(
                    f"Round {round_index}: context compacted {compacted_count} tool message(s)"
                )

            if not discovery.trace.enabled and self._should_finalize_single_read_route(
                route=route,
                round_tool_calls=round_tool_calls,
                compact_traces=compact_traces,
            ) and not (guard and guard.evaluate(messages, executed_tool_calls)):
                # 下一轮仍会收到完整 ToolMessage，但使用未绑定工具的模型，只生成答案。
                model_with_tools = model
                pipeline_steps.append(
                    f"Round {round_index}: single-read route completed; "
                    "disabled tools for final synthesis"
                )

            # 本轮只覆盖单基金综合研究窄路由，不改变普通聊天或组合研究协议。
            if route.tool_names == ("get_fund_research_brief_tool",):
                # 一轮可能请求多次读取，模型也可能随后换统计区间。每轮都从最新
                # 成功结果重建事实表，不能把新数字“纠正”回第一次读取的旧数字。
                answer_check_context = FundAnswerCheck.from_tool_calls(executed_tool_calls)
                if answer_check_instruction is not None:
                    messages = [message for message in messages if message is not answer_check_instruction]
                if answer_check_context is not None:
                    answer_check_instruction = SystemMessage(content=answer_check_context.instruction())
                    messages.append(answer_check_instruction)
                    pipeline_steps.append("Single-fund final synthesis uses the fact-bound JSON contract")

            if report_check_context is not None:
                report_check_context = FundReportAnswerCheck.from_message(report_message,
                    fund_code=request.fund_code or "", document_ids=request.fund_report_ids, prepare=True)
                if report_check_instruction is not None:
                    messages = [message for message in messages if message is not report_check_instruction]
                report_check_instruction = SystemMessage(content=report_check_context.instruction())
                messages.append(report_check_instruction)
                pipeline_steps.append("Report synthesis uses evidence-bound statements and server-owned reading scope")
                report_execution.evidence_ready = not report_check_context.input_issues
                # 模型先通过既有Permission/工具管线取得证据，才允许切换整理策略。
                # 固定数字题沿用上轮优化；观点对照仅在实验开关启用且问题边界明确时切换。
                light = report_profile["mode"] == "allocation" or (
                    report_execution.mode in {"evidence", "balanced"} and supports_evidence_synthesis(request.message))
                if report_execution.evidence_ready and light:
                    if report_profile["mode"] != "allocation":
                        balanced = report_execution.mode == "balanced"
                        model = get_chat_model(max_retries=0, thinking=balanced,
                            timeout_seconds=settings.agent_model_timeout_seconds,
                            **({"reasoning_effort": "low"} if balanced else {}))
                    model_with_tools = model
                    report_execution.synthesis_thinking = ("enabled_low" if report_profile["mode"] != "allocation"
                        and report_execution.mode == "balanced" else "disabled")
                pipeline_steps.append(f"Report synthesis: {report_execution.mode}; thinking={report_execution.synthesis_thinking}")

            rounds.append(
                AgentLoopRound(
                    round_index=round_index,
                    model_action="tool_calls",
                    assistant_message=self._content_to_text(response.content),
                    tool_calls=round_tool_calls,
                )
            )
            pipeline_steps.append(f"Round {round_index}: tool results appended to messages")
            action = guard.observe_round(round_tool_calls, round_index) if guard else None
            if action == "redirect":
                messages.append(SystemMessage(content=
                    "连续两轮工具未取得新增结果。不要重复相同查询；只能在已有权限和刷新授权内改查缺项。"
                    "如果缺少用户条件请追问，来源不可用则说明缺口，不要编造完整结论。"))
                pipeline_steps.append(f"Round {round_index}: no progress, requested a different query or explicit limitation")
                await self._emit_progress(progress_callback, phase="loop_guard",
                    label="查询没有新增依据，正在检查是否需要调整或结束", round_index=round_index)
            elif action == "stop":
                forced_reason = "no_progress"
                pipeline_steps.append(f"Round {round_index}: no progress, disabled tools for final synthesis")
                break

        # 走到这里说明每一轮模型都继续要工具。为了防止死循环，强制停止工具调用，
        # 让模型基于已有工具结果给出当前最好答案。
        messages.append(
            HumanMessage(
                content=(
                    ("本轮查询时间已用完，已停止工具调用。" if forced_reason == "time_budget_exhausted" else
                     "连续查询没有新增结果，已停止工具调用。" if forced_reason == "no_progress"
                     else "已经达到工具调用轮次上限。") +
                    "请不要再调用工具，直接基于已有工具结果给出最终答案；"
                    "如果信息仍不足，请明确说明还缺什么。"
                )
            )
        )
        try:
            await self._emit_progress(
                progress_callback,
                phase="answer",
                label=("查询时间已用完，正在整理已有依据和缺口" if forced_reason == "time_budget_exhausted" else
                       "重复查询已停止，正在整理已有依据和缺口" if forced_reason == "no_progress"
                       else "已达到工具轮次上限，正在生成最终回答"),
                round_index=round_index + 1,
            )
            final_model_call = await self._invoke_model(
                report_execution=report_execution,
                report_phase="synthesis" if report_message is not None else "model_decision",
                runnable=model,
                messages=messages,
                phase="forced_final_answer",
                round_index=round_index + 1,
                max_attempts=settings.agent_model_max_attempts,
                continuation_runnable=model,
            )
        except (AgentConfigError, ReportExecutionError, AIControlError):
            raise
        except ModelRecoveryError as exc:
            raise AgentRuntimeError(
                f"Agent 达到轮次上限后经过错误恢复仍总结失败：{exc}"
            ) from exc
        except Exception as exc:
            raise AgentRuntimeError(f"Agent 达到轮次上限后总结失败：{exc}") from exc

        self._record_model_recovery(
            result=final_model_call,
            recovery_trace=recovery_trace,
            context_trace=context_trace,
            pipeline_steps=pipeline_steps,
        )
        self._accumulate_model_usage(usage, final_model_call)
        answer = self._model_call_text(final_model_call)
        if guard:
            guard.evaluate(messages, executed_tool_calls)
        completion_limited = bool(guard and (guard.trace.missing or guard.boundary_issue))
        # 达到工具轮数上限也不能跳过事实核对。
        answer, answer_check = (answer, None) if completion_limited else await self._check_fund_answer(
            check=answer_check_context, answer=answer, model=model, messages=messages,
            round_index=round_index + 1, usage=usage, recovery_trace=recovery_trace,
            context_trace=context_trace, pipeline_steps=pipeline_steps,
            progress_callback=progress_callback,
        )
        answer, report_answer_check = (answer, None) if completion_limited else await self._check_report_answer(
            check=report_check_context, message=report_message, request=request, answer=answer, messages=messages,
            round_index=round_index + 1, usage=usage, recovery_trace=recovery_trace,
            context_trace=context_trace, pipeline_steps=pipeline_steps,
            progress_callback=progress_callback,
            execution=report_execution,
        )
        answer, appended_evidence = (answer, []) if answer_check or report_answer_check else ensure_answer_evidence_index(
            answer=answer, tool_calls=executed_tool_calls,
        )
        if appended_evidence:
            pipeline_steps.append(
                "Forced final answer grounding guard appended "
                f"{len(appended_evidence)} tool evidence ID(s)"
            )
        timed_close = forced_reason == "time_budget_exhausted"
        if guard:
            answer, forced_reason = guard.finalize(answer, messages, executed_tool_calls, reason=forced_reason)
        if timed_close:
            forced_reason = "time_budget_exhausted"
            answer = "本轮查询时间已用完，以下仅整理已取得的依据与缺口。\n\n" + answer
            if guard:
                guard.trace.stop_reason = forced_reason
        rounds.append(
            AgentLoopRound(
                round_index=round_index + 1,
                model_action="forced_final_answer",
                assistant_message=answer,
                tool_calls=[],
            )
        )
        pipeline_steps.append(f"Stopped ({forced_reason}), final answer without tools")

        return self._build_response(
            run_id=run_id,
            request=request,
            answer=answer,
            answer_check=answer_check,
            report_answer_check=report_answer_check,
            stop_reason=forced_reason,
            tool_names=tool_names,
            executed_tool_calls=executed_tool_calls,
            prompt_trace=prompt_trace,
            recovery_trace=recovery_trace,
            hooks_trace=hooks_trace,
            context_trace=context_trace,
            rounds=rounds,
            pipeline_steps=pipeline_steps,
            usage=AgentTokenUsage(**usage),
            route=route,
            tool_discovery=discovery.trace,
        )

    @traced("agent.validate_fund_answer", **{"langfuse.observation.type": "guardrail"})
    async def _check_fund_answer(
        self, *, check: FundAnswerCheck | None, answer: str, model: Any,
        messages: list[Any], round_index: int, usage: dict[str, int],
        recovery_trace: list[AgentRecoveryTrace], context_trace: list[AgentContextCompactTrace],
        pipeline_steps: list[str], progress_callback: Any,
    ) -> tuple[str, FundAnswerReport | None]:
        """复用原总结轮，只有不一致时才做一次无工具修正；不形成第二个循环。"""

        if check is None:
            return answer, None
        from langchain_core.messages import HumanMessage

        await self._emit_progress(progress_callback, phase="answer_check", label="正在核对关键事实与引用")
        _, issues = check.check(answer)
        attempts, failed = 0, False
        if issues:
            attempts = 1
            await self._emit_progress(progress_callback, phase="answer_check", label="发现事实格式或引用冲突，正在修正一次")
            # 修正请求携带原草稿（上限防止异常大输出）、明确错误和真实事实表。
            # 不提供任何工具，也不触发公开数据刷新。
            correction = HumanMessage(content=json.dumps({
                "instruction": check.instruction(), "issues": issues,
                "draft_to_correct": answer[:24000],
                "answer_facts": [fact.model_dump(mode="json") for fact in check.facts.values()],
            }, ensure_ascii=False))
            try:
                result = await model_recovery_service.invoke(
                    runnable=model, messages=[*messages, correction], phase="fund_answer_repair",
                    round_index=round_index, max_attempts=1, allow_continuation=False,
                )
                self._record_model_recovery(result=result, recovery_trace=recovery_trace,
                                            context_trace=context_trace, pipeline_steps=pipeline_steps)
                self._accumulate_model_usage(usage, result)
                answer = self._model_call_text(result)
                # 无工具模型异常返回 tool_calls 时，也不能把它当作成功的最终回答。
                failed = bool(getattr(result.response, "tool_calls", None))
            except ChatTimeBudgetExceeded:
                raise
            except Exception:
                failed = True
                pipeline_steps.append("Single-fund answer repair failed; discarded the unverified draft")
        report = check.finish(answer, attempts=attempts, initial_issues=issues, repair_failed=failed)
        pipeline_steps.append(f"Single-fund fact check: {report.status}; repair_attempts={attempts}")
        return render_answer_text(report), report

    @traced("agent.validate_report_answer", **{"langfuse.observation.type": "guardrail"})
    async def _check_report_answer(
        self, *, check: FundReportAnswerCheck | None, message: Any, request: AgentChatRequest, answer: str,
        messages: list[Any], round_index: int, usage: dict[str, int],
        recovery_trace: list[AgentRecoveryTrace], context_trace: list[AgentContextCompactTrace],
        pipeline_steps: list[str], progress_callback: Any, execution: ReportExecution | None = None,
    ) -> tuple[str, FundReportAnswerReport | None]:
        """原总结轮先核对，必要时只做一次无工具修正，不再建一条分析循环。"""
        if check is None:
            return answer, None
        from langchain_core.messages import HumanMessage, SystemMessage

        # 模型错误恢复也可能压缩上下文；检查前重新读取同一个ToolMessage对象，
        # 不把修复前的较大引用清单当成最终可见范围。
        check = FundReportAnswerCheck.from_message(message, fund_code=request.fund_code or "", document_ids=request.fund_report_ids)
        await self._emit_progress(progress_callback, phase="answer_check", label="正在核对季报原文引用与阅读范围")
        with execution.measure("validation") if execution else nullcontext():
            _, issues = check.check(answer)
        if execution:
            execution.record_validation(checkpoint="initial", issues=issues, round_index=round_index)
        attempts, failed = 0, False
        # 数据本身不存在/版本不匹配时，重问模型不能修好数据，直接给出有限结果。
        if issues and not check.input_issues:
            # 预算不足就明确保留有限结果；不能为了修正格式再领取一份20秒。
            if execution and execution.remaining() < 3:
                issues.append("本轮剩余时间不足，未发起引用修正。")
                failed = True
            else:
                attempts = 1
        if attempts:
            await self._emit_progress(progress_callback, phase="answer_check", label="季报引用不一致，正在修正一次")
            correction = HumanMessage(content=json.dumps({
                "repair_scope": "仅修正列出的协议和引用问题，不扩写分析。选择当前ToolMessage中的sentence_id，不抄写或编造引文。",
                "issues": issues, "citation_hints": check.repair_hints(answer),
                "draft_to_correct": answer[:24000]}, ensure_ascii=False, separators=(",", ":")))
            # 保留历史问题和实际工具对象，但不再发送两份协议、两份失败草稿。
            # 末次工具之后仅剩本轮整理指令和失败回答；修正用新的协议与单份草稿替代。
            last_tool = max(index for index, item in enumerate(messages) if getattr(item, "type", None) == "tool")
            repair_messages = [*messages[:last_tool + 1], SystemMessage(content=check.instruction()), correction]
            try:
                # 沿用项目模型工厂和同一模型配置，只有这次格式修正关闭深度思考。
                # 不绑定工具、不延长主分析时限，也不把一次修正变成多轮重试。
                repair_model = get_chat_model(max_retries=0, temperature=0, thinking=False,
                    timeout_seconds=_REPORT_REPAIR_TIMEOUT_SECONDS)
                async def repair(deadline=None):
                    result = await model_recovery_service.invoke(runnable=repair_model, messages=repair_messages,
                        phase="fund_report_answer_repair", round_index=round_index, max_attempts=1, allow_continuation=False,
                        **({"deadline": deadline, "attempt_callback": execution.record_attempt} if execution else {}))
                    return result
                if execution:
                    result = await execution.run("repair", repair, limit_seconds=_REPORT_REPAIR_TIMEOUT_SECONDS, round_index=round_index)
                else:
                    async with asyncio.timeout(_REPORT_REPAIR_TIMEOUT_SECONDS):
                        result = await repair()
                self._record_model_recovery(result=result, recovery_trace=recovery_trace,
                    context_trace=context_trace, pipeline_steps=pipeline_steps)
                self._accumulate_model_usage(usage, result)
                answer = self._model_call_text(result)
                failed = bool(getattr(result.response, "tool_calls", None))
            except ChatTimeBudgetExceeded:
                raise
            except Exception:
                failed = True
                pipeline_steps.append("Report answer repair failed; discarded the unverified draft")
        check = FundReportAnswerCheck.from_message(message, fund_code=request.fund_code or "", document_ids=request.fund_report_ids)
        with execution.measure("validation") if execution else nullcontext():
            report = check.finish(answer, attempts=attempts, initial_issues=issues, repair_failed=failed)
            # finish 保留前后错误合集供兼容显示；诊断另外保存复检当下的错误。
            if execution:
                _, final_issues = check.check(answer)
                execution.record_validation(checkpoint="final", issues=final_issues, round_index=round_index,
                    repair_status="failed" if failed else "rejected" if attempts and final_issues else "corrected" if attempts else "not_needed")
        if execution:
            report.execution = execution.snapshot(status="limited" if report.status == "limited" else "completed")
        pipeline_steps.append(f"Report reference check: {report.status}; repair_attempts={attempts}")
        return render_report_answer(report), report

    async def _invoke_model(self, *, report_execution=None, report_phase="synthesis", **kwargs):
        return await chat_stage(kwargs.get("phase", "model"), lambda: self._invoke_model_in_budget(
            report_execution=report_execution, report_phase=report_phase, **kwargs),
            closing=kwargs.get("phase") == "forced_final_answer")

    async def _invoke_model_in_budget(self, *, report_execution=None, report_phase="synthesis", **kwargs):
        """只为季报套共享预算；其他业务原封不动使用现有Recovery和续写策略。"""
        if report_execution is None:
            return await model_recovery_service.invoke(**kwargs)
        # JSON草稿不能靠拼接半截JSON续写；交由原有一次引用/格式修正处理。
        kwargs["allow_continuation"] = False
        async def call(deadline):
            # 只对已经取得证据的经理文字题预留兜底。数字题、普通Agent、未取证、
            # 已关闭思考的实验以及所剩预算太少的请求，都保持现有调用行为。
            fallback_seconds = _REPORT_SYNTHESIS_FALLBACK_SECONDS
            max_attempts = kwargs.get("max_attempts", settings.agent_model_max_attempts)
            eligible = (report_phase == "synthesis" and report_execution.query_mode == "manager"
                and report_execution.evidence_ready and report_execution.synthesis_thinking != "disabled"
                and max_attempts > 1 and deadline - monotonic() > fallback_seconds + _REPORT_MIN_RETRY_SECONDS)
            if not eligible:
                return await model_recovery_service.invoke(**kwargs, deadline=deadline,
                    attempt_callback=report_execution.record_attempt)

            started_attempts = len(report_execution.attempts)
            def record_primary(row):
                report_execution.record_attempt({**row, "strategy": "primary"})
            try:
                return await model_recovery_service.invoke(**kwargs, deadline=deadline - fallback_seconds,
                    attempt_callback=record_primary)
            except ModelRecoveryError as first_error:
                used = len(report_execution.attempts) - started_attempts
                # 取消/认证/余额错误不走这里；原恢复层已经用完两次尝试时也不追加第三次。
                if not first_error.is_timeout or not 0 < used < max_attempts or deadline - monotonic() < _REPORT_MIN_RETRY_SECONDS:
                    raise
                previous_thinking = report_execution.synthesis_thinking
                report_execution.synthesis_thinking = previous_thinking + "_then_disabled"
                fallback_trace = RecoveryTrace(event="ModelRecovery", phase=kwargs["phase"],
                    round_index=kwargs["round_index"], attempt=used + 1, max_attempts=max_attempts,
                    action="report_synthesis_without_thinking", error_type="timeout",
                    message="经理文字整理超时，在原截止时间内用同一证据关闭思考重试；引用仍须校验。",
                    metadata={"previous_thinking": previous_thinking,
                        "remaining_ms": round((deadline - monotonic()) * 1000, 2)})
                # 不复制或重新检索ToolMessage：若恢复层再次压缩，最终核对也必须看到
                # 实际被压缩后的同一份消息。这里没有自动替换引用或放宽输出协议。
                fallback_kwargs = {**kwargs, "runnable": get_chat_model(max_retries=0, thinking=False,
                    timeout_seconds=max(.01, deadline - monotonic())),
                    "continuation_runnable": None, "max_attempts": max_attempts - used}
                def record_fallback(row):
                    report_execution.record_attempt({**row, "attempt": row["attempt"] + used,
                        "strategy": "timeout_non_thinking"})
                try:
                    result = await model_recovery_service.invoke(**fallback_kwargs, deadline=deadline,
                        attempt_callback=record_fallback)
                except ModelRecoveryError as second_error:
                    raise ModelRecoveryError(phase=second_error.phase, round_index=second_error.round_index,
                        original_error=second_error.original_error,
                        traces=[*first_error.traces, fallback_trace,
                            *(replace(trace, attempt=trace.attempt + used, max_attempts=max_attempts) for trace in second_error.traces)],
                        compact_traces=[*first_error.compact_traces, *second_error.compact_traces]) from second_error
                return replace(result, traces=(*first_error.traces, fallback_trace,
                    *(replace(trace, attempt=trace.attempt + used, max_attempts=max_attempts) for trace in result.traces)),
                    compact_traces=(*first_error.compact_traces, *result.compact_traces))
        return await report_execution.run(report_phase, call, reserve_seconds=_REPORT_REPAIR_TIMEOUT_SECONDS,
            limit_seconds=settings.agent_model_timeout_seconds, round_index=kwargs["round_index"])

    @staticmethod
    async def _emit_progress(
        callback: Callable[[dict[str, Any]], Awaitable[None]] | None,
        *,
        phase: str,
        label: str,
        **details: Any,
    ) -> None:
        """把可选运行事件送给产品 SSE；普通 REST 调用完全不受影响。"""

        if callback is not None:
            await callback(
                {
                    "type": "phase",
                    "phase": phase,
                    "label": label,
                    **details,
                }
            )

    async def _execute_tool_call(
        self,
        *,
        tool_call: dict[str, Any],
        tool_map: dict[str, Any],
        request: AgentChatRequest,
        tool_index: int,
        ToolMessage: type,
        execution_context: AgentExecutionContext | None = None,
    ) -> tuple[AgentToolCall, Any, list[HookTrace]]:
        """保留稳定入口，把具体管线委托给独立 executor。"""

        context = execution_context or AgentExecutionContext.product(
            user_id=request.user_id,
            source="tool-call-safe-default",
        )
        return await agent_tool_executor.execute(
            tool_call=tool_call,
            tool_map=tool_map,
            request=request,
            execution_context=context,
            tool_index=tool_index,
            ToolMessage=ToolMessage,
        )

    async def _execute_tool_round(
        self,
        *,
        tool_calls: list[dict[str, Any]],
        tool_map: dict[str, Any],
        request: AgentChatRequest,
        ToolMessage: type,
        progress_callback: Callable[[dict[str, Any]], Awaitable[None]] | None,
        round_index: int,
        execution_context: AgentExecutionContext | None = None,
    ) -> list[tuple[dict[str, Any], AgentToolCall, Any, list[HookTrace]]]:
        """执行同一轮工具；纯读取可并发，其余调用保持严格顺序。"""

        context = execution_context or AgentExecutionContext.product(
            user_id=request.user_id,
            source="tool-round-safe-default",
        )
        indexed_calls = list(enumerate(tool_calls, start=1))

        async def execute_one(
            tool_index: int,
            tool_call: dict[str, Any],
        ) -> tuple[dict[str, Any], AgentToolCall, Any, list[HookTrace]]:
            tool_name = str(tool_call.get("name") or "unknown_tool")
            stage_name = tool_name if tool_name in tool_map else "unknown_tool"
            executed_call, tool_message, hook_traces = await chat_stage("tool:" + stage_name, lambda: self._execute_tool_call(
                tool_call=tool_call,
                tool_map=tool_map,
                request=request,
                execution_context=context,
                tool_index=tool_index,
                ToolMessage=ToolMessage,
            ))
            progress = current_loop_progress()
            if progress:
                # 并发中先返回的结果立即登记，不能等整个 gather 成功才保存。
                progress.calls.append(executed_call)
            return tool_call, executed_call, tool_message, hook_traces

        if self._can_execute_round_in_parallel(tool_calls):
            # 先一次性发布 running，让前端能真实显示多个数据源同时查询。
            for tool_index, tool_call in indexed_calls:
                await self._emit_tool_progress(
                    progress_callback,
                    tool_call=tool_call,
                    tool_index=tool_index,
                    round_index=round_index,
                    executed_call=None,
                )
            tasks = [asyncio.create_task(execute_one(i, call)) for i, call in indexed_calls]
            try:
                results = list(await asyncio.gather(*tasks))
            except BaseException:
                # 一个来源截止后不能留下其他协程继续下一步；同步线程无法被强杀。
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                raise
            for tool_index, (tool_call, executed_call, _, _) in zip(
                (index for index, _ in indexed_calls),
                results,
            ):
                await self._emit_tool_progress(
                    progress_callback,
                    tool_call=tool_call,
                    tool_index=tool_index,
                    round_index=round_index,
                    executed_call=executed_call,
                )
            return results

        results = []
        for tool_index, tool_call in indexed_calls:
            await self._emit_tool_progress(
                progress_callback,
                tool_call=tool_call,
                tool_index=tool_index,
                round_index=round_index,
                executed_call=None,
            )
            result = await execute_one(tool_index, tool_call)
            results.append(result)
            await self._emit_tool_progress(
                progress_callback,
                tool_call=tool_call,
                tool_index=tool_index,
                round_index=round_index,
                executed_call=result[1],
            )
        return results

    @staticmethod
    def _can_execute_round_in_parallel(tool_calls: list[dict[str, Any]]) -> bool:
        """至少两个调用且全部在只读白名单中，才允许并发。"""

        return len(tool_calls) > 1 and all(
            str(tool_call.get("name") or "") in PARALLEL_SAFE_READ_TOOLS
            for tool_call in tool_calls
        )

    async def _emit_tool_progress(
        self,
        callback: Callable[[dict[str, Any]], Awaitable[None]] | None,
        *,
        tool_call: dict[str, Any],
        tool_index: int,
        round_index: int,
        executed_call: AgentToolCall | None,
    ) -> None:
        """统一发送工具开始/结束事件，避免并发和串行分支出现两套文案。"""

        tool_name = str(tool_call.get("name") or "unknown_tool")
        if executed_call is None:
            label = f"正在调用 {tool_name}"
            status = "running"
        else:
            label = (
                f"{tool_name} 调用失败"
                if executed_call.is_error
                else f"{tool_name} 已返回证据"
            )
            status = "failed" if executed_call.is_error else "completed"
        await self._emit_progress(
            callback,
            phase="tool",
            label=label,
            tool_name=tool_name,
            tool_index=tool_index,
            round_index=round_index,
            status=status,
        )

    def _content_to_text(self, content: Any) -> str:
        """把 LangChain 消息内容统一转成字符串。"""

        if isinstance(content, str):
            return content

        return json.dumps(content, ensure_ascii=False)

    @staticmethod
    def _should_finalize_single_read_route(
        *,
        route: AgentRequestRoute,
        round_tool_calls: list[AgentToolCall],
        compact_traces: list[CompactTrace],
    ) -> bool:
        """判断一个完整只读结果是否已经满足窄路由，不再让模型重复调用。"""

        if len(route.tool_names) != 1 or route.tool_names[0] not in FINALIZE_AFTER_SUCCESS_TOOLS:
            return False
        structured_brief_tools = {
            "get_fund_research_brief_tool",
            "get_portfolio_research_brief_tool",
            "get_fund_report_research_tool",
        }
        # 普通长工具被压缩成预览后，模型可能仍需要重新查询；金融 Brief 使用字段级
        # 压缩，核心事实已经保留，所以仍可安全关闭工具并进入最终解释轮。
        if (
            compact_traces
            and route.tool_names[0] not in structured_brief_tools
        ) or len(round_tool_calls) != 1:
            return False
        call = round_tool_calls[0]
        if call.is_error:
            return False
        try:
            payload = json.loads(call.tool_result)
        except (json.JSONDecodeError, TypeError):
            return True
        return not (isinstance(payload, dict) and payload.get("ok") is False)

    def _build_response(
        self,
        *,
        run_id: str,
        request: AgentChatRequest,
        answer: str,
        stop_reason: str,
        tool_names: list[str],
        executed_tool_calls: list[AgentToolCall],
        prompt_trace: AgentPromptTrace,
        recovery_trace: list[AgentRecoveryTrace],
        hooks_trace: list[AgentHookTrace],
        context_trace: list[AgentContextCompactTrace],
        rounds: list[AgentLoopRound],
        pipeline_steps: list[str],
        usage: AgentTokenUsage,
        route: AgentRequestRoute,
        answer_check: FundAnswerReport | None = None,
        report_answer_check: FundReportAnswerReport | None = None,
        tool_discovery: AgentToolDiscoveryTrace | None = None,
    ) -> AgentChatResponse:
        """从统一运行态快照构造最终响应，避免两个停止分支重复几十个字段。"""

        state_fields = agent_runtime_state_service.collect_response_fields(
            user_id=request.user_id,
            # 测试或运行时可能替换当前模块的 MCP service，因此显式传入。
            mcp_service=mcp_tool_service,
        )
        return AgentChatResponse(
            run_id=run_id,
            user_id=request.user_id,
            question=request.message,
            answer=answer,
            answer_check=answer_check,
            report_answer_check=report_answer_check,
            used_model=settings.deepseek_model,
            max_tool_rounds=request.max_tool_rounds,
            stop_reason=stop_reason,
            tools_available=tool_names,
            tool_calls=executed_tool_calls,
            evidence=extract_tool_evidence_records(executed_tool_calls),
            prompt_trace=prompt_trace,
            recovery_trace=recovery_trace,
            hooks_trace=hooks_trace,
            context_trace=context_trace,
            rounds=rounds,
            pipeline_steps=pipeline_steps,
            usage=usage,
            tool_discovery=tool_discovery or AgentToolDiscoveryTrace(),
            loop_control=(current_loop_guard().trace if current_loop_guard() else AgentLoopControl()),
            execution_budget=current_chat_budget().snapshot() if current_chat_budget() else {},
            route=AgentRouteInfo(
                name=route.name,
                domains=list(route.domains),
                reason=route.reason,
                strategy=route.strategy,
                confidence=route.confidence,
                needs_clarification=route.needs_clarification,
            ),
            **state_fields,
        )

    def _assemble_routed_tools(
        self,
        *,
        base_tools: list[Any],
        route: AgentRequestRoute,
        execution_context: AgentExecutionContext,
    ) -> list[Any]:
        """仅为 MCP 路由合并动态工具，其他请求保持最小本地工具池。"""

        if route.include_mcp_tools and execution_context.mode == "developer":
            return mcp_tool_service.assemble_tool_pool(base_tools)
        return base_tools

    def _accumulate_model_usage(
        self,
        total: dict[str, int],
        result: ModelCallResult,
    ) -> None:
        """兼容 LangChain 常见字段并累计主调用与一次续写的 token。"""

        responses = [result.response]
        if result.continuation_response is not None:
            responses.append(result.continuation_response)

        for response in responses:
            raw = getattr(response, "usage_metadata", None) or {}
            if not raw:
                metadata = getattr(response, "response_metadata", None) or {}
                raw = metadata.get("token_usage") or metadata.get("usage") or {}

            input_tokens = int(
                raw.get("input_tokens") or raw.get("prompt_tokens") or 0
            )
            output_tokens = int(
                raw.get("output_tokens") or raw.get("completion_tokens") or 0
            )
            reported_total = int(raw.get("total_tokens") or 0)

            total["input_tokens"] += input_tokens
            total["output_tokens"] += output_tokens
            total["total_tokens"] += reported_total or (input_tokens + output_tokens)

    def _model_call_text(self, result: ModelCallResult) -> str:
        """合并模型第一次回答和可能存在的一次续写。"""

        primary_text = self._content_to_text(result.response.content)
        return f"{primary_text}{result.continuation_text}"

    def _record_model_recovery(
        self,
        *,
        result: ModelCallResult,
        recovery_trace: list[AgentRecoveryTrace],
        context_trace: list[AgentContextCompactTrace],
        pipeline_steps: list[str],
    ) -> None:
        """把恢复层结果合并进 `/agent/chat` 的调试字段。"""

        recovery_trace.extend(self._recovery_traces_to_schema(list(result.traces)))
        context_trace.extend(
            self._compact_traces_to_schema(list(result.compact_traces))
        )

        for trace in result.traces:
            pipeline_steps.append(
                f"Round {trace.round_index}: recovery action={trace.action}, "
                f"phase={trace.phase}"
            )

    def _prompt_result_to_schema(
        self,
        result: PromptBuildResult,
    ) -> AgentPromptTrace:
        """把 prompt builder 的 dataclass 转成接口响应模型。"""

        sections = [
            AgentPromptSection(
                name=section.name,
                title=section.title,
                content=section.content,
                char_count=section.char_count,
            )
            for section in result.sections
        ]

        return AgentPromptTrace(
            section_count=len(sections),
            total_chars=result.total_chars,
            sections=sections,
        )

    def _hook_traces_to_schema(self, traces: list[HookTrace]) -> list[AgentHookTrace]:
        """把 dataclass hook trace 转成 Pydantic 响应模型。"""

        return [
            AgentHookTrace(
                event=trace.event,
                hook_name=trace.hook_name,
                status=trace.status,
                message=trace.message,
                metadata=trace.metadata,
            )
            for trace in traces
        ]

    def _recovery_traces_to_schema(
        self,
        traces: list[RecoveryTrace],
    ) -> list[AgentRecoveryTrace]:
        """把 Error Recovery dataclass 转成 Pydantic 响应模型。"""

        return [
            AgentRecoveryTrace(
                event=trace.event,
                phase=trace.phase,
                round_index=trace.round_index,
                attempt=trace.attempt,
                max_attempts=trace.max_attempts,
                action=trace.action,
                error_type=trace.error_type,
                message=trace.message,
                delay_seconds=trace.delay_seconds,
                metadata=trace.metadata,
            )
            for trace in traces
        ]

    def _compact_traces_to_schema(
        self,
        traces: list[CompactTrace],
    ) -> list[AgentContextCompactTrace]:
        """把 Context Compact trace 转成 Pydantic 响应模型。"""

        return [
            AgentContextCompactTrace(
                event=trace.event,
                round_index=trace.round_index,
                before_chars=trace.before_chars,
                after_chars=trace.after_chars,
                compacted_messages=trace.compacted_messages,
                reason=trace.reason,
                saved_paths=trace.saved_paths,
                details=trace.details,
            )
            for trace in traces
        ]


tool_calling_agent_service = ToolCallingAgentService()
