"""教学版 Subagent。

s06 Subagent 要学习的重点是“上下文隔离”：
- 主 Agent 不必亲自读所有文件、塞满自己的 messages；
- 它可以把一个独立子任务交给临时小 Agent；
- 小 Agent 用全新的 messages 工作，最后只把摘要返回主 Agent。

默认模式仍是同步教学版：主 Agent 会等待临时 Subagent 完成。
s15 Agent Teams 复用了这里的受限循环，但由 Background Job 在独立 Worker 中调用，
并传入稳定成员身份及该成员自己的 Message Bus 历史。
"""

from __future__ import annotations

import json
import asyncio
from pathlib import Path
from typing import Any
from uuid import uuid4

from app.config import settings
from app.services.agent_execution_context import (
    AgentExecutionContext,
    agent_execution_scope,
    current_agent_execution_context,
)
from app.services.errors import AgentConfigError, AgentRuntimeError
from app.services.llm import get_chat_model
from app.services.tool_hooks import (
    HookTrace,
    run_post_tool_use_hooks,
    run_pre_tool_use_hooks,
)
from app.services.workspace_context import PROJECT_ROOT, workspace_scope
from app.services.agent_authorization import AgentAuthorizationError, get_task_authorization_service
from app.services.ai_governance_context import AIControlError


class SubagentService:
    """运行一个临时、受限、上下文干净的子 Agent。"""

    async def run(
        self,
        *,
        task: str,
        max_rounds: int = 3,
        top_k: int = 3,
        agent_name: str | None = None,
        role: str | None = None,
        instructions: str | None = None,
        conversation_context: str | None = None,
        capabilities: list[str] | None = None,
        user_id: str = "guest",
        workspace_root: str | None = None,
        worktree_name: str | None = None,
    ) -> dict[str, Any]:
        """执行一个子任务，并返回精简结果。

        注意：这里不会复用主 Agent 的 messages。
        这就是 Subagent 和普通多轮工具调用最大的区别。

        s15 Agent Teams 复用同一个受限循环，但会额外传入稳定 teammate 身份和它自己的
        Message Bus 历史。默认参数为空时，原来的 s06 临时 Subagent 行为完全不变。
        """

        try:
            from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
        except ModuleNotFoundError as exc:
            raise AgentConfigError("请先安装依赖：pip install -r requirements.txt") from exc

        from app.services.agent_tools import get_subagent_tools, get_teammate_tools

        teammate_mode = bool(str(agent_name or "").strip())
        inherited_context = current_agent_execution_context()
        # 主 Agent 调用时会通过 ContextVar 继承管理员开发模式；Worker 直接启动长期
        # teammate 时没有 HTTP 上下文，因此由后端调度器创建 system 开发上下文。
        execution_context = inherited_context
        if inherited_context.task_grant_id:
            # 子 Agent 拿独立身份，但不能因为切换到 teammate 模式就升级成 system。
            # 子票不延长父票期限；父任务撤销后，这张票的后续使用也会被拒绝。
            authorizer = get_task_authorization_service()
            parent = await asyncio.to_thread(authorizer.detail,
                user_id=inherited_context.user_id, grant_id=inherited_context.task_grant_id)
            execution_context = await asyncio.to_thread(authorizer.create, inherited_context,
                run_id=f"subtask:{uuid4().hex}", agent_id=f"subagent:{uuid4().hex}",
                portfolio_ids=parent["portfolio_ids"], parent_context=inherited_context,
                capabilities=sorted(inherited_context.capabilities & {
                    "portfolio:read", "fund:read", "market:read", "knowledge:read", "calculator:use"}))
        elif teammate_mode or inherited_context.source == "safe-default":
            execution_context = AgentExecutionContext.internal_developer(
                user_id=user_id,
                source="teammate-worker" if teammate_mode else "subagent-service",
            )
        # 临时 Subagent 只有 s06 只读工具；长期 teammate 额外得到 s16 提交计划工具。
        isolated_workspace = bool(
            teammate_mode
            and str(workspace_root or "").strip()
            and str(worktree_name or "").strip()
        )
        tools = (
            get_teammate_tools(allow_workspace_write=isolated_workspace)
            if teammate_mode
            else get_subagent_tools()
        )
        # 无论工具清单从哪个工厂产生，最后都与可信任务能力取交集。
        tools = [tool for tool in tools if execution_context.can_expose_tool(tool.name)]
        if capabilities is not None:
            # s15 的 capabilities 不只是展示标签，也会缩小 teammate 实际绑定的工具集合。
            tool_capability = {
                "calculator_tool": "calculate",
                "list_files_tool": "read_project",
                "read_file_tool": "read_project",
                "search_text_tool": "search_project",
                "rag_search_tool": "rag_search",
                "workspace_write_file_tool": "write_workspace",
            }
            allowed_capabilities = {str(item).strip() for item in capabilities}
            tools = [
                tool
                for tool in tools
                if tool.name == "submit_team_plan_tool"
                or tool_capability.get(tool.name) in allowed_capabilities
            ]
        # 执行映射也从过滤后的 tools 构建。即使模型意外输出未绑定工具名，后端也不会
        # 绕过 teammate capabilities 执行它。
        tool_map = {tool.name: tool for tool in tools}
        model = get_chat_model()
        # bind_tools 把“本次实际可用”的 schema 告诉模型，不是执行这些工具。
        model_with_tools = model.bind_tools(tools)

        # 每次 run 都创建全新的 messages；这就是上下文隔离发生的具体位置。
        messages = [
            SystemMessage(
                content=self._system_prompt(
                    agent_name=agent_name,
                    role=role,
                    instructions=instructions,
                    available_tool_names=[tool.name for tool in tools],
                    worktree_name=worktree_name if isolated_workspace else None,
                )
            ),
        ]
        if conversation_context:
            # Message Bus 历史属于 teammate 自己的上下文，不会进入主 Agent messages。
            messages.append(HumanMessage(content=conversation_context))
        messages.append(HumanMessage(content=task))

        result_note = (
            (
                "Teammate 使用独立 messages，并在隔离 Worktree "
                f"{worktree_name} 中读写；主项目目录不被直接修改。"
            )
            if isolated_workspace
            else
            "Teammate 使用独立 messages 和持久化 Message Bus，只把回复写回 main-agent inbox。"
            if teammate_mode
            else "Subagent 使用独立 messages，主 Agent 只收到这个精简结果。"
        )

        tool_calls_trace: list[dict[str, Any]] = []
        hook_traces: list[dict[str, Any]] = []
        rounds: list[dict[str, Any]] = []
        pipeline_steps = [
            "Create fresh subagent messages",
            (
                f"Bind tools to isolated worktree={worktree_name}"
                if isolated_workspace
                else "Bind limited read-only/retrieval tools"
            ),
            f"Start subagent loop, max_rounds={max_rounds}",
        ]

        # 小循环与主 Agent Loop 结构相同：模型决策 -> 后端执行工具 -> ToolMessage 回填。
        for round_index in range(1, max_rounds + 1):
            try:
                with agent_execution_scope(execution_context):
                    if execution_context.task_grant_id:
                        await asyncio.to_thread(get_task_authorization_service().check_context, execution_context)
                    response = await model_with_tools.ainvoke(messages)
                    if execution_context.task_grant_id:
                        await asyncio.to_thread(get_task_authorization_service().check_context, execution_context)
            except AIControlError:
                raise
            except Exception as exc:
                raise AgentRuntimeError(f"Subagent 第 {round_index} 轮调用大模型失败：{exc}") from exc

            messages.append(response)
            tool_calls = getattr(response, "tool_calls", None) or []

            # 没有 tool_calls 表示模型认为信息已足够，当前子循环立即结束。
            if not tool_calls:
                answer = self._content_to_text(response.content)
                rounds.append(
                    {
                        "round_index": round_index,
                        "model_action": "final_answer",
                        "assistant_message": answer,
                        "tool_calls": [],
                    }
                )
                pipeline_steps.append(
                    f"Subagent round {round_index}: answered without tools"
                )

                return {
                    "ok": True,
                    "task": task,
                    "agent_name": agent_name or "temporary-subagent",
                    "role": role or "temporary-subtask",
                    "answer": answer,
                    "used_model": settings.deepseek_model,
                    "max_rounds": max_rounds,
                    "stop_reason": "model_finished",
                    "tools_available": [tool.name for tool in tools],
                    "tool_calls": tool_calls_trace,
                    "hooks_trace": hook_traces,
                    "rounds": rounds,
                    "pipeline_steps": pipeline_steps,
                    "note": result_note,
                }

            round_tool_calls: list[dict[str, Any]] = []
            pipeline_steps.append(
                f"Subagent round {round_index}: requested {len(tool_calls)} tool call(s)"
            )

            # 一轮可能包含多个工具请求，全部执行后再进入下一次模型决策。
            for tool_index, tool_call in enumerate(tool_calls, start=1):
                executed_call, tool_message, traces = await self._execute_tool_call(
                    tool_call=tool_call,
                    tool_map=tool_map,
                    tool_index=tool_index,
                    top_k=top_k,
                    user_id=user_id,
                    execution_context=execution_context,
                    teammate_name=str(agent_name or ""),
                    workspace_root=workspace_root,
                    worktree_name=worktree_name,
                    ToolMessage=ToolMessage,
                )
                tool_calls_trace.append(executed_call)
                round_tool_calls.append(executed_call)
                hook_traces.extend(self._hook_traces_to_dicts(traces))
                messages.append(tool_message)

            rounds.append(
                {
                    "round_index": round_index,
                    "model_action": "tool_calls",
                    "assistant_message": self._content_to_text(response.content),
                    "tool_calls": round_tool_calls,
                }
            )
            pipeline_steps.append(
                f"Subagent round {round_index}: tool results appended to subagent messages"
            )

        # 达到上限后切换回未绑定工具的原模型，强制只做一次总结，防止无限循环。
        messages.append(
            HumanMessage(
                content=(
                    "你已经达到 Subagent 工具调用轮次上限。"
                    "请不要再调用工具，直接基于已有工具结果给出简洁结论；"
                    "如果信息仍不足，请说明还缺什么。"
                )
            )
        )

        try:
            with agent_execution_scope(execution_context):
                if execution_context.task_grant_id:
                    await asyncio.to_thread(get_task_authorization_service().check_context, execution_context)
                final_response = await model.ainvoke(messages)
                if execution_context.task_grant_id:
                    await asyncio.to_thread(get_task_authorization_service().check_context, execution_context)
        except AIControlError:
            raise
        except Exception as exc:
            raise AgentRuntimeError(f"Subagent 达到轮次上限后总结失败：{exc}") from exc

        answer = self._content_to_text(final_response.content)
        rounds.append(
            {
                "round_index": max_rounds + 1,
                "model_action": "forced_final_answer",
                "assistant_message": answer,
                "tool_calls": [],
            }
        )
        pipeline_steps.append("Subagent reached max_rounds, forced final answer")

        return {
            "ok": True,
            "task": task,
            "agent_name": agent_name or "temporary-subagent",
            "role": role or "temporary-subtask",
            "answer": answer,
            "used_model": settings.deepseek_model,
            "max_rounds": max_rounds,
            "stop_reason": "max_rounds_reached",
            "tools_available": [tool.name for tool in tools],
            "tool_calls": tool_calls_trace,
            "hooks_trace": hook_traces,
            "rounds": rounds,
            "pipeline_steps": pipeline_steps,
            "note": result_note,
        }

    async def _execute_tool_call(
        self,
        *,
        tool_call: dict[str, Any],
        tool_map: dict[str, Any],
        tool_index: int,
        top_k: int,
        user_id: str,
        execution_context: AgentExecutionContext,
        teammate_name: str,
        workspace_root: str | None,
        worktree_name: str | None,
        ToolMessage: type,
    ) -> tuple[dict[str, Any], Any, list[HookTrace]]:
        """执行 Subagent 请求的受限工具调用。"""

        tool_name = str(tool_call.get("name") or "")
        tool_args = dict(tool_call.get("args") or {})
        tool_call_id = str(tool_call.get("id") or f"subagent_tool_call_{tool_index}")

        if tool_name == "rag_search_tool" and "top_k" not in tool_args:
            tool_args["top_k"] = top_k

        if tool_name == "submit_team_plan_tool":
            # 这两个身份字段来自 Worker 正在执行的 teammate，不信任模型自行填写。
            tool_args["user_id"] = user_id
            tool_args["teammate_name"] = teammate_name

        pre_allowed, pre_block_reason, hook_traces = run_pre_tool_use_hooks(
            tool_name=tool_name,
            tool_args=tool_args,
            execution_context=execution_context,
        )
        if pre_allowed and execution_context.task_grant_id:
            try:
                await asyncio.to_thread(get_task_authorization_service().authorize_tool,
                    execution_context, tool_name, tool_args)
            except AgentAuthorizationError as exc:
                pre_allowed, pre_block_reason = False, str(exc)
                hook_traces.append(HookTrace(event="PreToolUse", hook_name="task_authorization",
                    status="blocked", message=str(exc), metadata={"code": exc.code}))

        # Permission/PreToolUse 即使在小 Agent 中也不能省略；模型无权自行放行工具。
        if not pre_allowed:
            tool_result = json.dumps(
                {
                    "ok": False,
                    "permission": "denied",
                    "reason": pre_block_reason,
                    "tool_name": tool_name,
                    "tool_args": tool_args,
                },
                ensure_ascii=False,
            )
            executed_call = {
                "tool_name": tool_name,
                "tool_args": tool_args,
                "tool_result": tool_result,
                "is_error": True,
            }
            return (
                executed_call,
                ToolMessage(content=tool_result, tool_call_id=tool_call_id),
                hook_traces,
            )

        # tool_map 已按 capabilities 过滤，是 prompt 之外的后端硬边界。
        selected_tool = tool_map.get(tool_name)

        if selected_tool is None:
            tool_result = json.dumps(
                {
                    "ok": False,
                    "error": f"Subagent 未授权工具：{tool_name}",
                },
                ensure_ascii=False,
            )
            is_error = True
        else:
            try:
                # 文件工具不接收 root 参数。可信路径由外层自治 Harness 查表后注入
                # ContextVar，因此模型只能填写 Worktree 内的相对路径。
                with agent_execution_scope(execution_context):
                    with workspace_scope(
                        Path(workspace_root).resolve()
                        if workspace_root
                        else PROJECT_ROOT,
                        isolated=bool(workspace_root and worktree_name),
                        worktree_name=worktree_name,
                    ):
                        tool_result = await selected_tool.ainvoke(tool_args)
                        if execution_context.task_grant_id:
                            await asyncio.to_thread(get_task_authorization_service().check_context, execution_context)
                is_error = False
            except Exception as exc:
                tool_result = json.dumps(
                    {
                        "ok": False,
                        "error": str(exc),
                    },
                    ensure_ascii=False,
                )
                is_error = True

        hook_traces.extend(
            # PostToolUse 记录结果与错误，保持主 Agent/Subagent 的可观察方式一致。
            run_post_tool_use_hooks(
                tool_name=tool_name,
                tool_args=tool_args,
                tool_result=str(tool_result),
                is_error=is_error,
            )
        )

        executed_call = {
            "tool_name": tool_name,
            "tool_args": tool_args,
            "tool_result": str(tool_result),
            "is_error": is_error,
        }
        return (
            executed_call,
            ToolMessage(content=str(tool_result), tool_call_id=tool_call_id),
            hook_traces,
        )

    def _system_prompt(
        self,
        *,
        agent_name: str | None = None,
        role: str | None = None,
        instructions: str | None = None,
        available_tool_names: list[str] | None = None,
        worktree_name: str | None = None,
    ) -> str:
        """根据运行模式构建临时 Subagent 或长期 Teammate 的身份提示词。"""

        normalized_name = str(agent_name or "").strip()
        tools_text = ", ".join(available_tool_names or []) or "当前没有可用工具"
        if normalized_name:
            normalized_role = str(role or "general teammate").strip()
            normalized_instructions = str(instructions or "").strip()
            workspace_rules = (
                (
                    f"当前 Harness 已把你的文件工具绑定到隔离 Git Worktree："
                    f"{worktree_name}。所有相对路径都以该 Worktree 为根目录。"
                    "你可以使用 workspace_write_file_tool 落地任务要求的代码或文档；"
                    "写入后应再次 read/search 验证结果。不要尝试访问主项目或其他 Worktree。"
                )
                if worktree_name
                else (
                    "当前没有为任务绑定 Worktree，因此你只能读取和分析，"
                    "不要声称已经修改项目文件。"
                )
            )
            return (
                f"你是 Agent Team 中的长期 teammate：{normalized_name}。"
                f"你的角色是：{normalized_role}。"
                f"你的专属工作要求是：{normalized_instructions or '根据角色准确完成收到的工作'}。"
                "你有自己的独立上下文，只能看到 Message Bus 提供的团队历史和当前消息，"
                "不能假装知道 main-agent 的完整对话。"
                f"你只能使用当前能力绑定的受限工具：{tools_text}。"
                f"{workspace_rules}"
                "不要创建任务，不要派生 Subagent 或 teammate。"
                "需要查看项目代码时先搜索或列文件，再读取关键片段。"
                "如果当前工作在执行前需要 main-agent 正式审批计划，调用 "
                "submit_team_plan_tool；提交后只能说明计划处于 pending，不能假装已经批准。"
                "后续消息上下文出现匹配 request_id 的 plan_approval_response 后，"
                "再根据 approved/rejected 结果继续或调整。"
                "最终回复要直接回答当前消息，并突出结论与依据。"
            )

        return (
            "你是主 Agent 派出的临时 Subagent。"
            "你的任务是完成一个独立子问题，并把结论简洁返回。"
            "你有自己的全新上下文，不知道主 Agent 的完整对话历史。"
            "你只能使用受限工具：calculator_tool、list_files_tool、"
            "search_text_tool、read_file_tool、rag_search_tool。"
            "不要写文件，不要创建 todo，不要再派生其他 Subagent。"
            "如果需要查看项目代码，先搜索或列文件，再读取关键片段。"
            "最终回答要突出：结论、依据、还缺什么信息。"
        )

    def _content_to_text(self, content: Any) -> str:
        """把 LangChain 消息内容统一转成字符串。"""

        if isinstance(content, str):
            return content

        return json.dumps(content, ensure_ascii=False)

    def _hook_traces_to_dicts(self, traces: list[HookTrace]) -> list[dict[str, Any]]:
        """把 hook trace 转成工具结果里的普通 dict。"""

        return [
            {
                "event": trace.event,
                "hook_name": trace.hook_name,
                "status": trace.status,
                "message": trace.message,
                "metadata": trace.metadata,
            }
            for trace in traces
        ]


subagent_service = SubagentService()
