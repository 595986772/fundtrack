"""Tool Permission 检查层。

Permission 的核心思想是：模型可以“请求”调用工具，但后端不一定执行。
真正执行工具前，先经过这里的规则判断。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
from typing import Any

from app.services.agent_execution_context import (
    AgentExecutionContext,
    current_agent_execution_context,
)
from app.services.cron_scheduler import validate_cron
from app.services.mcp_tools import mcp_tool_service


PROJECT_ROOT = Path(__file__).resolve().parents[2]
AGENT_NOTES_DIR = PROJECT_ROOT / "data" / "agent_notes"

# 通过 ExecutionContext 第一层检查后，下列工具不再需要额外参数审批。
# 这里包含只读工具、本地计算工具、只修改内存状态的 todo_write_tool，
# 只启动受限子 Agent 的 subagent_tool、只读取项目 skills/ 的 skill loading 工具，
# 只读取 data/memory/ 的 search_memory_tool、只读任务列表工具和后台 Job 查询工具。
SAFE_ALLOWED_TOOLS = {
    "search_tools_tool",
    "calculator_tool",
    "list_files_tool",
    "read_file_tool",
    "search_text_tool",
    "rag_search_tool",
    "get_fund_research_brief_tool",
    "get_portfolio_research_brief_tool",
    "get_fund_profile_tool",
    "get_fund_holdings_tool",
    "get_fund_report_research_tool",
    "get_fund_nav_summary_tool",
    "get_fund_risk_tool",
    "get_fund_assessment_tool",
    "get_fund_semantic_review_tool",
    "get_portfolio_assessment_tool",
    "get_portfolio_performance_tool",
    "get_portfolio_exposure_tool",
    "get_portfolio_event_impacts_tool",
    "list_portfolio_decisions_tool",
    "get_portfolio_research_status_tool",
    "get_portfolio_stress_tool",
    "get_portfolio_allocation_tool",
    "get_market_sentiment_tool",
    "get_retail_indices_tool",
    "get_large_capital_signals_tool",
    "get_market_hotspots_tool",
    "search_market_events_tool",
    "todo_write_tool",
    "subagent_tool",
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
}

# 当前允许写入但需要额外规则检查的工具。
WRITE_NOTE_TOOL = "write_note_tool"
WORKSPACE_WRITE_TOOL = "workspace_write_file_tool"
REMEMBER_TOOL = "remember_tool"
TASK_WRITE_TOOLS = {
    "task_create_tool",
    "task_update_tool",
    "task_claim_tool",
}
BACKGROUND_JOB_WRITE_TOOLS = {
    "background_job_start_tool",
    "background_job_cancel_tool",
    "start_portfolio_research_tool",
}
CRON_WRITE_TOOLS = {
    "schedule_cron_tool",
    "cancel_cron_tool",
}
TEAM_WRITE_TOOLS = {
    "spawn_teammate_tool",
    "send_team_message_tool",
    "request_teammate_shutdown_tool",
    "review_team_plan_tool",
    "submit_team_plan_tool",
    "start_teammate_autonomy_tool",
    "stop_autonomy_session_tool",
}
WORKTREE_WRITE_TOOLS = {
    "create_task_worktree_tool",
    "keep_worktree_tool",
    "remove_worktree_tool",
}
CONNECT_MCP_TOOL = "connect_mcp_tool"
ALLOWED_MEMORY_TYPES = {
    "user_preference",
    "project_fact",
    "feedback",
    "reference_clue",
}


@dataclass(frozen=True)
class PermissionDecision:
    """一次工具调用的权限判断结果。"""

    allowed: bool
    reason: str


def check_tool_permission(
    tool_name: str,
    tool_args: dict[str, Any],
    *,
    execution_context: AgentExecutionContext | None = None,
) -> PermissionDecision:
    """判断某个工具调用是否允许执行。

    这是所有工具的中央分发器：只读工具快速放行，有副作用的工具进入专用检查函数，
    未登记工具默认拒绝。采用默认拒绝才能避免新增工具时忘记配置权限。
    """

    # 先检查“这个身份是否应该拥有该工具”，再检查工具自己的参数规则。缺少显式上下文
    # 时使用 ContextVar；完全孤立的调用会安全降级为最小产品权限，而不是默认管理员。
    context = execution_context or current_agent_execution_context()
    access = context.authorize_tool(tool_name=tool_name, tool_args=tool_args)
    if not access.allowed:
        return PermissionDecision(allowed=False, reason=access.reason)

    if tool_name in SAFE_ALLOWED_TOOLS:
        return PermissionDecision(allowed=True, reason="safe tool allowed")

    if tool_name == "draft_portfolio_allocation_tool":
        from app.services.portfolio_allocation_tools import AllocationToolInput
        from pydantic import ValidationError
        try:
            AllocationToolInput.model_validate(tool_args)
        except ValidationError:
            return PermissionDecision(False, "约束草案参数不正确")
        return PermissionDecision(True, "仅保存待确认草案，不执行求解或交易")

    if tool_name == "run_portfolio_stress_tool":
        # 它会保存研究记录，但不是交易工具。权限先由上面的能力白名单裁决。
        from app.services.portfolio_stress_tools import StressToolInput
        from pydantic import ValidationError
        try:
            StressToolInput.model_validate(tool_args)
        except ValidationError:
            return PermissionDecision(False, "压力测试参数格式或范围不正确")
        return PermissionDecision(True, "允许本地历史重演并保存报告，不修改持仓")

    if tool_name == WRITE_NOTE_TOOL:
        return _check_write_note_permission(tool_args)

    if tool_name == WORKSPACE_WRITE_TOOL:
        return _check_workspace_write_permission(tool_args)

    if tool_name == REMEMBER_TOOL:
        return _check_remember_permission(tool_args)

    if tool_name in TASK_WRITE_TOOLS:
        return _check_task_write_permission(tool_name, tool_args)

    if tool_name in BACKGROUND_JOB_WRITE_TOOLS:
        return _check_background_job_write_permission(tool_name, tool_args)

    if tool_name in CRON_WRITE_TOOLS:
        return _check_cron_write_permission(tool_name, tool_args)

    if tool_name in TEAM_WRITE_TOOLS:
        return _check_team_write_permission(tool_name, tool_args)

    if tool_name in WORKTREE_WRITE_TOOLS:
        return _check_worktree_write_permission(tool_name, tool_args)

    if tool_name == CONNECT_MCP_TOOL:
        server_name = str(tool_args.get("server_name") or "").strip()
        if server_name in mcp_tool_service.configured_server_names():
            return PermissionDecision(
                allowed=True,
                reason="只允许连接后端白名单中的 MCP Server",
            )
        return PermissionDecision(
            allowed=False,
            reason=f"未授权的 MCP Server：{server_name}",
        )

    # MCP 工具不是静态写在这个文件里的；权限来自 tools/list 返回的 annotations。
    # 未连接、未知或没有明确 readOnly 声明的 MCP 工具一律拒绝。
    if tool_name.startswith("mcp__"):
        allowed, reason = mcp_tool_service.permission_for_tool(tool_name)
        return PermissionDecision(allowed=allowed, reason=reason)

    return PermissionDecision(
        allowed=False,
        reason=f"未知工具或未授权工具：{tool_name}",
    )


def _check_write_note_permission(tool_args: dict[str, Any]) -> PermissionDecision:
    """只允许 write_note_tool 写入 data/agent_notes/ 下的文本文件。"""

    raw_path = str(tool_args.get("path") or "").strip()

    if not raw_path:
        return PermissionDecision(False, "write_note_tool 缺少 path 参数")

    if "\x00" in raw_path:
        return PermissionDecision(False, "path 包含非法字符")

    target_path = (PROJECT_ROOT / raw_path).resolve()

    try:
        target_path.relative_to(AGENT_NOTES_DIR.resolve())
    except ValueError:
        return PermissionDecision(False, "只允许写入 data/agent_notes/ 目录")

    if target_path.suffix.lower() not in {".md", ".txt"}:
        return PermissionDecision(False, "只允许写入 .md 或 .txt 文件")

    content = str(tool_args.get("content") or "")

    if len(content) > 20_000:
        return PermissionDecision(False, "笔记内容太长，当前 demo 限制为 20000 字符以内")

    return PermissionDecision(True, "write_note_tool allowed in data/agent_notes/")


def _check_workspace_write_permission(
    tool_args: dict[str, Any],
) -> PermissionDecision:
    """检查 Worktree 相对路径和文本大小；实际根目录由运行上下文二次校验。"""

    raw_path = str(tool_args.get("path") or "").strip()
    if not raw_path or "\x00" in raw_path:
        return PermissionDecision(False, "Worktree 写入 path 为空或包含非法字符")
    if Path(raw_path).is_absolute():
        return PermissionDecision(False, "Worktree 写入只接受相对路径")

    # Permission 不解析真实磁盘路径，因为可信 workspace root 只有执行时 ContextVar 才有。
    # 这里先从纯参数层拒绝父目录穿越、敏感文件和不支持的扩展名。
    parts = Path(raw_path).parts
    if ".." in parts or "." == raw_path:
        return PermissionDecision(False, "Worktree 写入路径不能包含父目录穿越")
    if ".git" in parts or Path(raw_path).name == ".env":
        return PermissionDecision(False, "Worktree 写入不能修改 .git 或 .env")

    allowed_extensions = {
        ".css", ".html", ".ini", ".js", ".json", ".md", ".py", ".txt",
        ".toml", ".ts", ".tsx", ".yaml", ".yml",
    }
    target = Path(raw_path)
    if target.name != ".env.example" and target.suffix.lower() not in allowed_extensions:
        return PermissionDecision(False, "Worktree 写入只允许常见源码和文档文本")

    content = str(tool_args.get("content") or "")
    if len(content) > 50_000:
        return PermissionDecision(False, "Worktree 写入内容不能超过 50000 字符")
    if not isinstance(tool_args.get("overwrite", False), bool):
        return PermissionDecision(False, "overwrite 必须是布尔值")
    return PermissionDecision(True, "bounded isolated workspace write allowed")


def _check_remember_permission(tool_args: dict[str, Any]) -> PermissionDecision:
    """只允许 remember_tool 写入受控的长期记忆。"""

    memory_type = str(tool_args.get("memory_type") or "").strip()

    if memory_type not in ALLOWED_MEMORY_TYPES:
        return PermissionDecision(False, "remember_tool 的 memory_type 不在允许范围内")

    content = str(tool_args.get("content") or "")

    if not content.strip():
        return PermissionDecision(False, "remember_tool 缺少 content 参数")

    if len(content) > 3_000:
        return PermissionDecision(False, "记忆内容太长，当前 demo 限制为 3000 字符以内")

    user_id = str(tool_args.get("user_id") or "guest").strip()

    if not user_id:
        return PermissionDecision(False, "remember_tool 缺少 user_id")

    return PermissionDecision(True, "remember_tool allowed in data/memory/")


def _check_task_write_permission(
    tool_name: str,
    tool_args: dict[str, Any],
) -> PermissionDecision:
    """只允许任务工具修改当前用户的 data/tasks/ JSON。"""

    simple_name_pattern = r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}"
    user_id = str(tool_args.get("user_id") or "").strip()

    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,49}", user_id):
        return PermissionDecision(False, "任务工具的 user_id 格式非法")

    task_id = str(tool_args.get("task_id") or "").strip()

    if tool_name in {"task_update_tool", "task_claim_tool"} and not task_id:
        return PermissionDecision(False, f"{tool_name} 缺少 task_id")

    if task_id and not re.fullmatch(simple_name_pattern, task_id):
        return PermissionDecision(False, "task_id 格式非法")

    blocked_by = tool_args.get("blocked_by")
    if blocked_by is not None:
        if not isinstance(blocked_by, list) or len(blocked_by) > 20:
            return PermissionDecision(False, "blocked_by 必须是最多 20 项的数组")

        for dependency_id in blocked_by:
            if not re.fullmatch(simple_name_pattern, str(dependency_id or "")):
                return PermissionDecision(False, "blocked_by 包含非法任务 ID")

    if tool_name == "task_create_tool":
        subject = str(tool_args.get("subject") or "").strip()
        if not subject or len(subject) > 200:
            return PermissionDecision(False, "task_create_tool 的 subject 非法")

    if tool_name == "task_claim_tool":
        owner = str(tool_args.get("owner") or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,49}", owner):
            return PermissionDecision(False, "task_claim_tool 的 owner 格式非法")

    return PermissionDecision(True, f"{tool_name} allowed in data/tasks/")


def _check_background_job_write_permission(
    tool_name: str,
    tool_args: dict[str, Any],
) -> PermissionDecision:
    """限制后台工具的用户、ID 和运行时长，避免模型创建无边界工作。"""

    # 第一层限制“替谁操作”和“操作哪个 Job”。user_id 已由后端注入，但仍再次校验。
    user_id = str(tool_args.get("user_id") or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,49}", user_id):
        return PermissionDecision(False, "后台 Job 工具的 user_id 格式非法")

    job_id = str(tool_args.get("job_id") or "").strip()
    if job_id and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", job_id):
        return PermissionDecision(False, "后台 Job 工具的 job_id 格式非法")

    if tool_name == "start_portfolio_research_tool":
        portfolio_id = str(tool_args.get("portfolio_id") or "").strip()
        if not re.fullmatch(r"portfolio_[a-f0-9]{16}", portfolio_id):
            return PermissionDecision(False, "组合研究工具的 portfolio_id 格式非法")
        try:
            months = int(tool_args.get("months", 12))
        except (TypeError, ValueError):
            return PermissionDecision(False, "组合研究工具的 months 不是整数")
        if months not in {6, 12, 36}:
            return PermissionDecision(False, "组合研究工具的 months 只能是 6、12 或 36")
        idempotency_key = str(tool_args.get("idempotency_key") or "").strip()
        if idempotency_key and not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,99}",
            idempotency_key,
        ):
            return PermissionDecision(False, "组合研究工具的幂等键非法")
        return PermissionDecision(True, "受限组合研究 Job allowed")

    # cancel 只需要合法 job_id；start 还要继续检查资源消耗边界。
    if tool_name == "background_job_cancel_tool":
        if not job_id:
            return PermissionDecision(False, "background_job_cancel_tool 缺少 job_id")
        return PermissionDecision(True, "background_job_cancel_tool allowed")

    title = str(tool_args.get("title") or "").strip()
    if not title or len(title) > 200:
        return PermissionDecision(False, "background_job_start_tool 的 title 非法")

    job_type = str(tool_args.get("job_type") or "demo_steps").strip()
    if job_type not in {"demo_steps", "agent_prompt"}:
        return PermissionDecision(False, "background_job_start_tool 的 job_type 非法")

    prompt = str(tool_args.get("prompt") or "").strip()
    if len(prompt) > 8000:
        return PermissionDecision(False, "background_job_start_tool 的 prompt 过长")

    idempotency_key = str(tool_args.get("idempotency_key") or "").strip()
    if idempotency_key and not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,99}",
        idempotency_key,
    ):
        return PermissionDecision(False, "background_job_start_tool 的幂等键非法")

    # 数字边界防止模型创建无限步骤、无限重试或超长 Agent Loop。
    try:
        total_steps = int(tool_args.get("total_steps", 8))
        step_delay_seconds = float(tool_args.get("step_delay_seconds", 0.4))
        max_retries = int(
            2 if tool_args.get("max_retries") is None else tool_args["max_retries"]
        )
        timeout_seconds = int(
            300
            if tool_args.get("timeout_seconds") is None
            else tool_args["timeout_seconds"]
        )
        max_tool_rounds = int(tool_args.get("max_tool_rounds", 10))
    except (TypeError, ValueError):
        return PermissionDecision(False, "后台 Job 的运行边界参数不是数字")

    if total_steps < 1 or total_steps > 20:
        return PermissionDecision(False, "后台 Job 的 total_steps 必须在 1-20 之间")
    if step_delay_seconds < 0.05 or step_delay_seconds > 5.0:
        return PermissionDecision(
            False,
            "后台 Job 的 step_delay_seconds 必须在 0.05-5.0 之间",
        )
    if max_retries < 0 or max_retries > 5:
        return PermissionDecision(False, "后台 Job 的 max_retries 必须在 0-5 之间")
    if timeout_seconds < 1 or timeout_seconds > 3600:
        return PermissionDecision(False, "后台 Job 的 timeout_seconds 必须在 1-3600 之间")
    if max_tool_rounds < 1 or max_tool_rounds > 10:
        return PermissionDecision(False, "后台 Job 的 max_tool_rounds 必须在 1-10 之间")

    return PermissionDecision(True, "bounded background job allowed")


def _check_cron_write_permission(
    tool_name: str,
    tool_args: dict[str, Any],
) -> PermissionDecision:
    """校验 Cron 写操作，阻止越权用户和非法表达式进入调度线程。"""

    user_id = str(tool_args.get("user_id") or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,49}", user_id):
        return PermissionDecision(False, "Cron 工具的 user_id 格式非法")

    schedule_id = str(tool_args.get("schedule_id") or "").strip()
    if schedule_id and not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}",
        schedule_id,
    ):
        return PermissionDecision(False, "Cron 工具的 schedule_id 格式非法")

    if tool_name == "cancel_cron_tool":
        if not schedule_id:
            return PermissionDecision(False, "cancel_cron_tool 缺少 schedule_id")
        return PermissionDecision(True, "cancel_cron_tool allowed")

    cron_expression = str(tool_args.get("cron") or "").strip()
    cron_error = validate_cron(cron_expression)
    if cron_error:
        return PermissionDecision(False, f"Cron 表达式非法：{cron_error}")

    prompt = str(tool_args.get("prompt") or "").strip()
    if not prompt or len(prompt) > 500:
        return PermissionDecision(False, "schedule_cron_tool 的 prompt 非法")

    return PermissionDecision(True, "validated cron schedule allowed")


def _check_team_write_permission(
    tool_name: str,
    tool_args: dict[str, Any],
) -> PermissionDecision:
    """限制 s15 消息与 s16 协议写操作，避免模型越权改变团队状态。"""

    # 读取列表/inbox/protocols 在安全集合中；所有状态变化都进入本函数逐类检查。
    identifier_pattern = r"[A-Za-z][A-Za-z0-9_-]{0,49}"
    user_id = str(tool_args.get("user_id") or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,49}", user_id):
        return PermissionDecision(False, "Agent Team 工具的 user_id 格式非法")

    if tool_name == "spawn_teammate_tool":
        # 注册检查身份和配置大小；能力名称的最终白名单由 AgentTeamService 再校验。
        name = str(tool_args.get("name") or "").strip()
        role = str(tool_args.get("role") or "").strip()
        instructions = str(tool_args.get("instructions") or "")
        capabilities = tool_args.get("capabilities") or []
        if not re.fullmatch(identifier_pattern, name) or name == "main-agent":
            return PermissionDecision(False, "spawn_teammate_tool 的 name 非法")
        if not role or len(role) > 100:
            return PermissionDecision(False, "spawn_teammate_tool 的 role 非法")
        if len(instructions) > 2_000:
            return PermissionDecision(False, "teammate instructions 过长")
        if not isinstance(capabilities, list) or len(capabilities) > 8:
            return PermissionDecision(False, "teammate capabilities 必须是最多 8 项的数组")
        return PermissionDecision(True, "bounded teammate registration allowed")

    if tool_name == "stop_autonomy_session_tool":
        session_id = str(tool_args.get("session_id") or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", session_id):
            return PermissionDecision(False, "自治会话 session_id 格式非法")
        return PermissionDecision(True, "autonomy stop allowed for current user")

    if tool_name == "start_teammate_autonomy_tool":
        teammate_name = str(tool_args.get("teammate_name") or "").strip()
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,49}", teammate_name):
            return PermissionDecision(False, "自治 teammate_name 格式非法")
        return PermissionDecision(True, "bounded teammate autonomy allowed")

    if tool_name == "request_teammate_shutdown_tool":
        teammate_name = str(tool_args.get("teammate_name") or "").strip()
        reason = str(tool_args.get("reason") or "")
        if not re.fullmatch(identifier_pattern, teammate_name):
            return PermissionDecision(False, "shutdown teammate_name 非法")
        if len(reason) > 4_000:
            return PermissionDecision(False, "shutdown reason 过长")
        return PermissionDecision(True, "bounded shutdown protocol allowed")

    if tool_name == "review_team_plan_tool":
        request_id = str(tool_args.get("request_id") or "").strip()
        feedback = str(tool_args.get("feedback") or "")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", request_id):
            return PermissionDecision(False, "plan review request_id 非法")
        if not isinstance(tool_args.get("approve"), bool):
            return PermissionDecision(False, "plan review approve 必须是布尔值")
        if len(feedback) > 2_000:
            return PermissionDecision(False, "plan review feedback 过长")
        return PermissionDecision(True, "bounded plan review allowed")

    if tool_name == "submit_team_plan_tool":
        # user_id 和 teammate_name 都由 SubagentService 从当前运行上下文注入。
        teammate_name = str(tool_args.get("teammate_name") or "").strip()
        plan = str(tool_args.get("plan") or "").strip()
        if not re.fullmatch(identifier_pattern, teammate_name):
            return PermissionDecision(False, "submit plan teammate_name 非法")
        if not plan or len(plan) > 4_000:
            return PermissionDecision(False, "submit plan 内容非法")
        return PermissionDecision(True, "bounded teammate plan submission allowed")

    # send_team_message_tool 不能直接指定 sender，主循环会固定为 main-agent。
    recipient = str(tool_args.get("recipient") or "").strip()
    content = str(tool_args.get("content") or "").strip()
    if not re.fullmatch(identifier_pattern, recipient):
        return PermissionDecision(False, "send_team_message_tool 的 recipient 非法")
    if not content or len(content) > 4_000:
        return PermissionDecision(False, "send_team_message_tool 的 content 非法")

    # 这些值最终进入 Background Job，Permission 先限制模型最多能消耗多少资源。
    try:
        max_rounds = int(tool_args.get("max_rounds", 3))
        max_retries = int(tool_args.get("max_retries", 1))
        timeout_seconds = int(tool_args.get("timeout_seconds", 300))
    except (TypeError, ValueError):
        return PermissionDecision(False, "teammate AgentRun 边界参数不是数字")

    if max_rounds < 1 or max_rounds > 5:
        return PermissionDecision(False, "max_rounds 必须在 1-5 之间")
    if max_retries < 0 or max_retries > 3:
        return PermissionDecision(False, "max_retries 必须在 0-3 之间")
    if timeout_seconds < 1 or timeout_seconds > 1800:
        return PermissionDecision(False, "timeout_seconds 必须在 1-1800 之间")
    return PermissionDecision(True, "bounded team message allowed")


def _check_worktree_write_permission(
    tool_name: str,
    tool_args: dict[str, Any],
) -> PermissionDecision:
    """限制主 Agent 只能管理当前用户下由安全名称推导的 Worktree。"""

    user_id = str(tool_args.get("user_id") or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,49}", user_id):
        return PermissionDecision(False, "Worktree 工具的 user_id 格式非法")

    name = str(tool_args.get("name") or "").strip()
    if (
        name in {".", ".."}
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", name)
    ):
        return PermissionDecision(False, "Worktree 名称格式非法")

    if tool_name == "create_task_worktree_tool":
        task_id = str(tool_args.get("task_id") or "").strip()
        if task_id and not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}",
            task_id,
        ):
            return PermissionDecision(False, "Worktree task_id 格式非法")
        base_ref = str(tool_args.get("base_ref") or "HEAD").strip()
        if (
            not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,199}", base_ref)
            or ".." in base_ref
            or "//" in base_ref
        ):
            return PermissionDecision(False, "Worktree base_ref 格式非法")
        return PermissionDecision(True, "bounded worktree creation allowed")

    if tool_name == "remove_worktree_tool" and not isinstance(
        tool_args.get("discard_changes", False),
        bool,
    ):
        return PermissionDecision(False, "discard_changes 必须是布尔值")

    return PermissionDecision(True, f"{tool_name} allowed for current user")
