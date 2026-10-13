"""Tool Hooks 扩展点。

Hooks 的核心思想是：主循环不直接堆很多扩展逻辑，而是在关键时机触发事件。

当前实现两个事件：
- PreToolUse：工具执行前，例如权限检查、日志记录；
- PostToolUse：工具执行后，例如记录输出长度、后处理提醒。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.services.agent_execution_context import AgentExecutionContext
from app.services.tool_permissions import check_tool_permission


@dataclass(frozen=True)
class HookTrace:
    """一次 hook 运行轨迹。"""

    event: str
    hook_name: str
    status: str
    message: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PreToolUseResult:
    """PreToolUse hook 的结果。

    allowed=False 表示这个 hook 阻止工具继续执行。
    """

    allowed: bool
    trace: HookTrace


def run_pre_tool_use_hooks(
    *,
    tool_name: str,
    tool_args: dict[str, Any],
    execution_context: AgentExecutionContext | None = None,
) -> tuple[bool, str, list[HookTrace]]:
    """运行所有工具执行前 hooks。"""

    traces: list[HookTrace] = []

    for hook in PRE_TOOL_USE_HOOKS:
        result = hook(
            tool_name=tool_name,
            tool_args=tool_args,
            execution_context=execution_context,
        )
        traces.append(result.trace)

        if not result.allowed:
            return False, result.trace.message, traces

    return True, "all pre hooks passed", traces


def run_post_tool_use_hooks(
    *,
    tool_name: str,
    tool_args: dict[str, Any],
    tool_result: str,
    is_error: bool,
) -> list[HookTrace]:
    """运行所有工具执行后 hooks。"""

    return [
        hook(
            tool_name=tool_name,
            tool_args=tool_args,
            tool_result=tool_result,
            is_error=is_error,
        )
        for hook in POST_TOOL_USE_HOOKS
    ]


def log_pre_tool_use_hook(
    *,
    tool_name: str,
    tool_args: dict[str, Any],
    execution_context: AgentExecutionContext | None = None,
) -> PreToolUseResult:
    """记录模型准备调用哪个工具。"""

    return PreToolUseResult(
        allowed=True,
        trace=HookTrace(
            event="PreToolUse",
            hook_name="log_pre_tool_use",
            status="passed",
            message=f"tool request observed: {tool_name}",
            metadata={
                "tool_name": tool_name,
                "arg_names": sorted(tool_args.keys()),
                "agent_mode": (
                    execution_context.mode if execution_context is not None else "inherited"
                ),
            },
        ),
    )


def permission_pre_tool_use_hook(
    *,
    tool_name: str,
    tool_args: dict[str, Any],
    execution_context: AgentExecutionContext | None = None,
) -> PreToolUseResult:
    """把 Permission 检查挂到 PreToolUse 事件上。"""

    decision = check_tool_permission(
        tool_name,
        tool_args,
        execution_context=execution_context,
    )

    return PreToolUseResult(
        allowed=decision.allowed,
        trace=HookTrace(
            event="PreToolUse",
            hook_name="permission_pre_tool_use",
            status="passed" if decision.allowed else "blocked",
            message=decision.reason,
            metadata={
                "tool_name": tool_name,
                "agent_mode": (
                    execution_context.mode if execution_context is not None else "inherited"
                ),
            },
        ),
    )


def output_size_post_tool_use_hook(
    *,
    tool_name: str,
    tool_args: dict[str, Any],
    tool_result: str,
    is_error: bool,
) -> HookTrace:
    """记录工具输出长度，帮助观察工具结果是否过大。"""

    output_length = len(tool_result)
    status = "warning" if output_length > 8_000 else "passed"

    return HookTrace(
        event="PostToolUse",
        hook_name="output_size_post_tool_use",
        status=status,
        message=f"tool output length={output_length}",
        metadata={
            "tool_name": tool_name,
            "output_length": output_length,
            "is_error": is_error,
        },
    )


PRE_TOOL_USE_HOOKS = [
    log_pre_tool_use_hook,
    permission_pre_tool_use_hook,
]

POST_TOOL_USE_HOOKS = [
    output_size_post_tool_use_hook,
]
