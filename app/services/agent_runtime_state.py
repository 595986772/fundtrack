"""聚合主 Agent 周围的运行态资源。

过去 `tool_agent.py` 在两个 return 分支里分别读取 Todo、Task、Job、Team、MCP 等状态，
容易遗漏字段或产生重复代码。这个只读 collector 让 Agent 响应和工作台 API 使用同一份
状态定义，但不把任何业务写入逻辑搬到这里。
"""

from __future__ import annotations

from typing import Any

from app.config import settings
from app.schemas import AgentWorkspaceRuntimeInfo, AgentWorkspaceSnapshot
from app.services.agent_teams import agent_team_service
from app.services.autonomous_agents import autonomous_agent_service
from app.services.background_jobs import background_job_service
from app.services.cron_scheduler import cron_scheduler_service
from app.services.memory import memory_service
from app.services.mcp_tools import mcp_tool_service
from app.services.tasks import task_store_service
from app.services.team_protocols import team_protocol_service
from app.services.todos import todo_state
from app.services.worktrees import worktree_service


class AgentRuntimeStateService:
    """从各领域 service 读取当前用户的统一快照。"""

    def collect_response_fields(
        self,
        *,
        user_id: str,
        mcp_service: Any = mcp_tool_service,
    ) -> dict[str, Any]:
        """返回兼容 `AgentChatResponse` 的完整运行态字段。"""

        return {
            "current_todos": todo_state.list(user_id=user_id),
            "current_tasks": task_store_service.recent_index(user_id=user_id),
            "current_background_jobs": background_job_service.recent_index(
                user_id=user_id
            ),
            "current_cron_schedules": cron_scheduler_service.recent_index(
                user_id=user_id
            ),
            "recent_cron_deliveries": cron_scheduler_service.recent_deliveries(
                user_id=user_id
            ),
            "current_teammates": agent_team_service.list_teammates(
                user_id=user_id
            )["teammates"],
            "recent_team_messages": agent_team_service.recent_messages(
                user_id=user_id
            ),
            "current_team_protocols": team_protocol_service.recent_index(
                user_id=user_id
            ),
            "recent_team_protocol_events": team_protocol_service.recent_events(
                user_id=user_id
            ),
            "current_autonomy_sessions": autonomous_agent_service.recent_index(
                user_id=user_id
            ),
            "current_worktrees": worktree_service.recent_index(user_id=user_id),
            "current_mcp_servers": mcp_service.list_servers(),
            "current_mcp_tools": mcp_service.list_tools(),
            "recent_mcp_calls": mcp_service.recent_calls(),
            "current_memories": memory_service.recent_index(user_id=user_id),
        }

    def workspace_snapshot(
        self,
        *,
        user_id: str,
        tool_names: list[str] | None = None,
        mcp_service: Any = mcp_tool_service,
    ) -> AgentWorkspaceSnapshot:
        """把内部字段整理成前端稳定使用的产品结构。"""

        if tool_names is None:
            # 延迟导入避免 agent_tools -> 多个领域 service -> state 的循环依赖。
            from app.services.agent_tools import get_agent_tools

            tools = mcp_service.assemble_tool_pool(get_agent_tools())
            tool_names = [tool.name for tool in tools]

        fields = self.collect_response_fields(
            user_id=user_id,
            mcp_service=mcp_service,
        )
        database_backend = (
            "postgresql"
            if settings.database_url.startswith("postgresql")
            else "sqlite"
        )

        return AgentWorkspaceSnapshot(
            user_id=user_id,
            runtime=AgentWorkspaceRuntimeInfo(
                app_name=settings.app_name,
                model=settings.deepseek_model,
                execution_backend=settings.job_execution_mode,
                database_backend=database_backend,
                tool_count=len(tool_names),
                mcp_tool_count=len(fields["current_mcp_tools"]),
            ),
            todos=fields["current_todos"],
            tasks=fields["current_tasks"],
            jobs=fields["current_background_jobs"],
            schedules=fields["current_cron_schedules"],
            teammates=fields["current_teammates"],
            protocols=fields["current_team_protocols"],
            autonomy_sessions=fields["current_autonomy_sessions"],
            worktrees=fields["current_worktrees"],
            mcp_servers=fields["current_mcp_servers"],
            memories=fields["current_memories"],
        )


agent_runtime_state_service = AgentRuntimeStateService()
