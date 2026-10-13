"""Agent 可调用工具定义。

Tool Calling 里最重要的一点是：工具本质上还是普通 Python 函数。
区别只是我们额外告诉大模型：
- 工具叫什么；
- 工具能解决什么问题；
- 工具需要哪些参数；
- 工具执行后会返回什么。
"""

from __future__ import annotations

import ast
from fnmatch import fnmatch
import json
import operator
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field

from app.schemas import RagQueryRequest
from app.services.autonomous_agents import autonomous_agent_service
from app.services.agent_execution_context import (
    AgentExecutionContext,
    PRODUCT_TOOL_CAPABILITIES,
    current_agent_execution_context,
)
from app.services.agent_teams import MAIN_AGENT_NAME, agent_team_service
from app.services.background_jobs import background_job_service
from app.services.cron_scheduler import cron_scheduler_service
from app.services.financial_research_agent_tools import (
    get_financial_research_agent_tools,
)
from app.services.fund_agent_tools import get_fund_agent_tools
from app.services.market_agent_tools import get_market_agent_tools
from app.services.portfolio_agent_tools import get_portfolio_agent_tools
from app.services.portfolio_stress_tools import run_portfolio_stress_tool, get_portfolio_stress_tool
from app.services.portfolio_allocation_tools import draft_portfolio_allocation_tool, get_portfolio_allocation_tool
from app.services.memory import memory_service
from app.services.mcp_tools import mcp_tool_service
from app.services.skills import skill_library_service
from app.services.tasks import task_store_service
from app.services.team_protocols import team_protocol_service
from app.services.todos import todo_state
from app.services.vector_rag import vector_rag_service
from app.services.worktrees import worktree_service
from app.services.workspace_context import (
    current_workspace_root,
    current_worktree_name,
    is_isolated_workspace,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]

# 这些目录要么包含敏感信息，要么是缓存/二进制索引，不适合交给模型阅读。
EXCLUDED_DIR_NAMES = {
    ".agents",
    ".codex",
    ".git",
    ".idea",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "data/chroma",
    "node_modules",
    "venv",
}
SENSITIVE_FILE_NAMES = {".env"}
TEXT_FILE_EXTENSIONS = {
    ".css",
    ".env.example",
    ".html",
    ".ini",
    ".js",
    ".json",
    ".md",
    ".py",
    ".txt",
    ".toml",
    ".ts",
    ".tsx",
    ".yaml",
    ".yml",
}


class CalculatorInput(BaseModel):
    """calculator_tool 的参数结构。"""

    expression: str = Field(
        ...,
        description="要计算的数学表达式，例如 '2 + 3 * 4'。",
    )


class RagSearchInput(BaseModel):
    """rag_search_tool 的参数结构。"""

    question: str = Field(
        ...,
        description="用户想从项目知识库中查询的问题。",
    )
    top_k: int = Field(
        default=3,
        ge=1,
        le=8,
        description="最多从 Chroma 向量库召回几个相关 chunk。",
    )


class ListFilesInput(BaseModel):
    """list_files_tool 的参数结构。"""

    pattern: str = Field(
        default="**/*",
        description="文件匹配模式，例如 '**/*.py'、'app/**/*.py'、'data/docs/*.md'。",
    )
    max_results: int = Field(
        default=80,
        ge=1,
        le=200,
        description="最多返回多少个文件路径。",
    )


class ReadFileInput(BaseModel):
    """read_file_tool 的参数结构。"""

    path: str = Field(
        ...,
        description="要读取的项目内相对路径，例如 'app/services/tool_agent.py'。",
    )
    start_line: int = Field(
        default=1,
        ge=1,
        description="从第几行开始读取。",
    )
    max_lines: int = Field(
        default=120,
        ge=1,
        le=300,
        description="最多读取多少行，避免一次把大文件全部塞进上下文。",
    )


class SearchTextInput(BaseModel):
    """search_text_tool 的参数结构。"""

    query: str = Field(
        ...,
        min_length=1,
        max_length=120,
        description="要搜索的文本，例如 'bind_tools'、'VectorRagService'。",
    )
    pattern: str = Field(
        default="**/*",
        description="在哪些文件里搜索，例如 '**/*.py'、'app/**/*.py'、'*.md'。",
    )
    max_results: int = Field(
        default=30,
        ge=1,
        le=80,
        description="最多返回多少条匹配结果。",
    )


class WriteNoteInput(BaseModel):
    """write_note_tool 的参数结构。"""

    path: str = Field(
        ...,
        description="要写入的项目内路径，必须位于 data/agent_notes/，例如 data/agent_notes/tool-summary.md。",
    )
    content: str = Field(
        ...,
        min_length=1,
        max_length=20_000,
        description="要写入笔记文件的内容。",
    )
    overwrite: bool = Field(
        default=False,
        description="文件已存在时是否覆盖。默认 False，避免误覆盖。",
    )


class WorkspaceWriteFileInput(BaseModel):
    """workspace_write_file_tool 的参数结构。

    这个工具只会绑定给“正在执行已绑定 Worktree Task”的长期 teammate。模型不需要也
    不能传 workspace_root，根目录由 Harness 的 ContextVar 注入。
    """

    path: str = Field(
        ...,
        min_length=1,
        max_length=300,
        description="Worktree 内的相对路径，例如 app/services/example.py。",
    )
    content: str = Field(
        ...,
        max_length=50_000,
        description="要写入的完整 UTF-8 文本内容，最长 50000 字符。",
    )
    overwrite: bool = Field(
        default=False,
        description="已有文件是否允许覆盖；默认 false 防止误覆盖。",
    )


class TodoItemInput(BaseModel):
    """todo_write_tool 中单条 todo 的参数结构。"""

    id: str = Field(
        ...,
        description="todo 的稳定编号，例如 'inspect-tools'、'write-summary'。",
    )
    content: str = Field(
        ...,
        description="todo 的具体内容，用一句人能看懂的话描述。",
    )
    status: Literal["pending", "in_progress", "completed"] = Field(
        ...,
        description="todo 当前状态，只能是 pending、in_progress、completed。",
    )


class TodoWriteInput(BaseModel):
    """todo_write_tool 的参数结构。"""

    items: list[TodoItemInput] = Field(
        ...,
        description=(
            "完整 todo 列表。每次调用都会用这个列表替换当前计划；"
            "同一时间最多一条是 in_progress。"
        ),
    )
    user_id: str = Field(
        default="guest",
        description="当前用户 ID。后端会自动填入，模型通常不用设置。",
    )


class SubagentInput(BaseModel):
    """subagent_tool 的参数结构。"""

    task: str = Field(
        ...,
        min_length=1,
        max_length=1200,
        description="要交给临时 Subagent 的独立子任务。",
    )
    max_rounds: int = Field(
        default=3,
        ge=1,
        le=5,
        description="Subagent 自己最多允许几轮工具调用。教学版建议 1-5。",
    )
    top_k: int = Field(
        default=3,
        ge=1,
        le=8,
        description="Subagent 调用 rag_search_tool 时默认召回几个 chunk。",
    )


class ListSkillsInput(BaseModel):
    """list_skills_tool 的参数结构。"""

    max_results: int = Field(
        default=20,
        ge=1,
        le=50,
        description="最多返回多少个 skill catalog 项。",
    )


class LoadSkillInput(BaseModel):
    """load_skill_tool 的参数结构。"""

    name: str = Field(
        ...,
        min_length=1,
        max_length=64,
        description="要加载的 skill 名称，例如 agent-harness-learning-route。",
    )
    reference_path: str | None = Field(
        default=None,
        description=(
            "可选 reference 路径。为空时加载 SKILL.md；"
            "如果填写，只允许类似 references/route-map.md 的 .md 文件。"
        ),
    )


class RememberInput(BaseModel):
    """remember_tool 的参数结构。"""

    memory_type: Literal[
        "user_preference",
        "project_fact",
        "feedback",
        "reference_clue",
    ] = Field(
        ...,
        description="记忆类型：用户偏好、项目事实、用户反馈或引用线索。",
    )
    content: str = Field(
        ...,
        min_length=1,
        max_length=3000,
        description="要保存成长期记忆的具体内容。",
    )
    tags: list[str] = Field(
        default_factory=list,
        description="可选标签，例如 learning、style、project。",
    )
    importance: int = Field(
        default=3,
        ge=1,
        le=5,
        description="重要程度，1-5。越重要越应该在检索时靠前。",
    )
    source: str = Field(
        default="agent",
        description="记忆来源，默认 agent。",
    )
    user_id: str = Field(
        default="guest",
        description="当前用户 ID。后端会自动填入，模型通常不用设置。",
    )


class SearchMemoryInput(BaseModel):
    """search_memory_tool 的参数结构。"""

    query: str = Field(
        ...,
        min_length=1,
        max_length=300,
        description="要从长期记忆里检索的关键词或问题。",
    )
    memory_type: Literal[
        "user_preference",
        "project_fact",
        "feedback",
        "reference_clue",
    ] | None = Field(
        default=None,
        description="可选，限定只搜索某一类记忆。",
    )
    max_results: int = Field(
        default=5,
        ge=1,
        le=20,
        description="最多返回多少条记忆。",
    )
    user_id: str = Field(
        default="guest",
        description="当前用户 ID。后端会自动填入，模型通常不用设置。",
    )


class TaskCreateInput(BaseModel):
    """task_create_tool 的参数结构。

    这个类不是任务本身，而是“模型调用创建任务工具时必须传什么参数”。
    LangChain 会把这些字段转换成 tool schema 交给大模型看，
    大模型再按这个 schema 生成 tool_calls。
    """

    # subject 是最重要的必填字段，相当于任务列表里看到的一句话标题。
    subject: str = Field(
        ...,
        min_length=1,
        max_length=200,
        description="简短任务标题，例如‘实现 JWT 登录接口’。",
    )
    # description 用来补充验收目标和边界，空字符串表示暂时没有补充说明。
    description: str = Field(
        default="",
        max_length=2000,
        description="任务的验收目标、范围或补充说明。",
    )
    # blocked_by 表示当前任务被哪些已有任务阻塞。
    # 注意：这里填的是任务 ID，不是任务标题。
    blocked_by: list[str] = Field(
        default_factory=list,
        max_length=20,
        description=(
            "阻塞当前任务的已有任务 ID。依赖必须先创建且全部 completed，"
            "当前任务才能被认领。"
        ),
    )
    # task_id 可选。用户想要稳定可读 ID 时可以手动指定；
    # 不指定时后端会生成类似 task-xxxx 的 ID。
    task_id: str | None = Field(
        default=None,
        max_length=64,
        description="可选稳定 ID，例如 auth-design；为空时后端自动生成。",
    )
    # user_id 会由后端根据请求自动注入，避免模型替用户伪造别人的任务目录。
    user_id: str = Field(
        default="guest",
        description="当前用户 ID。后端会自动填入，模型通常不用设置。",
    )


class TaskListInput(BaseModel):
    """task_list_tool 的参数结构。

    这是一个只读工具的入参类。
    它不会修改任务，只是让模型按条件查询当前用户的任务列表。
    """

    status: Literal[
        "pending",
        "in_progress",
        "completed",
        "cancelled",
    ] | None = Field(default=None, description="可选，按任务状态过滤。")
    # owner 可以用来查看某个 Agent/子 Agent 当前认领了哪些任务。
    owner: str | None = Field(default=None, description="可选，按负责人过滤。")
    # only_blocked 常用于排查“为什么某个任务现在不能开始”。
    only_blocked: bool = Field(
        default=False,
        description="为 true 时只返回仍被依赖阻塞的任务。",
    )
    max_results: int = Field(
        default=50,
        ge=1,
        le=100,
        description="最多返回多少个任务。",
    )
    user_id: str = Field(
        default="guest",
        description="当前用户 ID。后端会自动填入，模型通常不用设置。",
    )


class TaskUpdateInput(BaseModel):
    """task_update_tool 的参数结构。

    这个工具负责更新已有任务。
    重点规则：它不能把任务直接改成 in_progress，
    因为进入 in_progress 必须走 task_claim_tool 做依赖检查和 owner 绑定。
    """

    # task_id 告诉后端要更新哪一个 JSON 任务文件。
    task_id: str = Field(..., min_length=1, max_length=64)
    # subject/description/status/blocked_by 都是可选字段。
    # 至少要传其中一个，否则后端会拒绝“空更新”。
    subject: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    status: Literal[
        "pending",
        "in_progress",
        "completed",
        "cancelled",
    ] | None = Field(
        default=None,
        description=(
            "可选新状态。进入 in_progress 必须改用 task_claim_tool；"
            "in_progress 完成后可以更新为 completed。"
        ),
    )
    blocked_by: list[str] | None = Field(
        default=None,
        max_length=20,
        description="可选，替换完整依赖列表；只有 pending 任务可以修改。",
    )
    user_id: str = Field(
        default="guest",
        description="当前用户 ID。后端会自动填入，模型通常不用设置。",
    )


class TaskClaimInput(BaseModel):
    """task_claim_tool 的参数结构。

    claim 的含义是“认领任务并准备开始做”。
    这个动作会把 pending 任务变成 in_progress，
    所以后端会先确认它没有未完成依赖。
    """

    task_id: str = Field(..., min_length=1, max_length=64)
    # owner 是认领者名称。当前教学版可以写 main-agent，
    # 后续 s15/s16 做多 Agent 团队时，这里会更像 teammate 名称。
    owner: str = Field(
        ...,
        min_length=1,
        max_length=50,
        description="认领者名称。当前主 Agent 建议使用 main-agent。",
    )
    user_id: str = Field(
        default="guest",
        description="当前用户 ID。后端会自动填入，模型通常不用设置。",
    )


class BackgroundJobStartInput(BaseModel):
    """background_job_start_tool 的参数结构。

    demo_steps 用于观察“立即返回 job_id”；agent_prompt 会把明确的 prompt 交给
    独立 Worker 中的现有 Agent Loop。两种类型都不会直接执行任意 shell 命令。
    """

    title: str = Field(
        ...,
        min_length=1,
        max_length=200,
        description="这次后台工作的可读标题。",
    )
    total_steps: int = Field(
        default=8,
        ge=1,
        le=20,
        description="教学任务分成多少步，用于观察进度。",
    )
    step_delay_seconds: float = Field(
        default=0.4,
        ge=0.05,
        le=5.0,
        description="每一步等待多少秒，用于模拟耗时 I/O。",
    )
    job_id: str | None = Field(
        default=None,
        max_length=64,
        description="可选稳定 Job ID；为空时由后端自动生成。",
    )
    job_type: Literal["demo_steps", "agent_prompt"] = Field(
        default="demo_steps",
        description="demo_steps 是确定性测试；agent_prompt 是真实异步 AgentRun。",
    )
    prompt: str | None = Field(
        default=None,
        max_length=8000,
        description="agent_prompt 的具体问题；为空时使用 title。",
    )
    max_tool_rounds: int = Field(default=10, ge=1, le=10)
    max_retries: int | None = Field(default=None, ge=0, le=5)
    timeout_seconds: int | None = Field(default=None, ge=1, le=3600)
    idempotency_key: str | None = Field(
        default=None,
        max_length=100,
        description="防止网络重试造成重复 Job 的稳定请求标识。",
    )
    user_id: str = Field(
        default="guest",
        description="当前用户 ID。后端会自动填入，模型通常不用设置。",
    )


class BackgroundJobStatusInput(BaseModel):
    """background_job_status_tool 的参数结构。"""

    job_id: str = Field(..., min_length=1, max_length=64)
    user_id: str = Field(
        default="guest",
        description="当前用户 ID。后端会自动填入，模型通常不用设置。",
    )


class BackgroundJobListInput(BaseModel):
    """background_job_list_tool 的参数结构。"""

    status: Literal[
        "queued",
        "running",
        "waiting_input",
        "completed",
        "failed",
        "cancelled",
    ] | None = Field(
        default=None,
        description="可选，只查看某一种运行状态。",
    )
    max_results: int = Field(default=30, ge=1, le=100)
    user_id: str = Field(
        default="guest",
        description="当前用户 ID。后端会自动填入，模型通常不用设置。",
    )


class BackgroundJobCancelInput(BaseModel):
    """background_job_cancel_tool 的参数结构。"""

    job_id: str = Field(..., min_length=1, max_length=64)
    user_id: str = Field(
        default="guest",
        description="当前用户 ID。后端会自动填入，模型通常不用设置。",
    )


# s15 Agent Teams 的四个 schema 会通过 bind_tools 暴露给模型。
# 它们描述“模型应该输出什么参数”，更严格的授权仍由 tool_permissions.py 负责。
class SpawnTeammateInput(BaseModel):
    """spawn_teammate_tool 的参数结构。"""

    name: str = Field(
        ...,
        min_length=1,
        max_length=50,
        description="稳定 teammate 名称，例如 researcher 或 reviewer。",
    )
    role: str = Field(
        ...,
        min_length=1,
        max_length=100,
        description="成员角色，例如代码研究员、审查员。",
    )
    instructions: str = Field(
        default="",
        max_length=2000,
        description="该成员每次处理消息都要遵守的专属要求。",
    )
    capabilities: list[
        Literal[
            "calculate",
            "rag_search",
            "read_project",
            "search_project",
            "write_workspace",
        ]
    ] = Field(
        default_factory=list,
        max_length=8,
        description="能力标签；为空时使用默认只读能力，Worktree 写入必须显式选择。",
    )
    user_id: str = Field(
        default="guest",
        description="当前用户 ID。后端会自动填入，模型通常不用设置。",
    )


class ListTeammatesInput(BaseModel):
    """list_teammates_tool 的参数结构。"""

    max_results: int = Field(default=30, ge=1, le=100)
    user_id: str = Field(default="guest")


class SendTeamMessageInput(BaseModel):
    """send_team_message_tool 的参数结构。"""

    recipient: str = Field(..., min_length=1, max_length=50)
    content: str = Field(..., min_length=1, max_length=4000)
    dispatch: bool = Field(
        default=True,
        description="true 会立即创建 teammate AgentRun；false 只写入 inbox。",
    )
    max_rounds: int = Field(default=3, ge=1, le=5)
    max_retries: int = Field(default=1, ge=0, le=3)
    timeout_seconds: int = Field(default=300, ge=1, le=1800)
    user_id: str = Field(default="guest")


class CheckTeamInboxInput(BaseModel):
    """check_team_inbox_tool 的参数结构。"""

    recipient: str = Field(
        default=MAIN_AGENT_NAME,
        max_length=50,
        description="默认查看 main-agent inbox，也可以查看指定 teammate inbox。",
    )
    unread_only: bool = False
    mark_read: bool = False
    max_results: int = Field(default=30, ge=1, le=100)
    user_id: str = Field(default="guest")


class RequestTeammateShutdownInput(BaseModel):
    """request_teammate_shutdown_tool 的参数结构。"""

    teammate_name: str = Field(..., min_length=1, max_length=50)
    reason: str = Field(default="", max_length=4000)
    user_id: str = Field(default="guest")


class ListTeamProtocolsInput(BaseModel):
    """list_team_protocols_tool 的参数结构。"""

    status: Literal["pending", "approved", "rejected"] | None = None
    max_results: int = Field(default=30, ge=1, le=100)
    user_id: str = Field(default="guest")


class ReviewTeamPlanInput(BaseModel):
    """review_team_plan_tool 的参数结构。"""

    request_id: str = Field(..., min_length=1, max_length=64)
    approve: bool
    feedback: str = Field(default="", max_length=2000)
    user_id: str = Field(default="guest")


class StartTeammateAutonomyInput(BaseModel):
    """start_teammate_autonomy_tool 的有界运行参数。"""

    teammate_name: str = Field(..., min_length=1, max_length=50)
    poll_interval_seconds: float = Field(default=2.0, ge=0.05, le=30)
    max_idle_polls: int = Field(default=5, ge=1, le=60)
    max_tasks: int = Field(default=5, ge=1, le=20)
    max_rounds_per_task: int = Field(default=3, ge=1, le=5)
    timeout_seconds: int = Field(default=300, ge=5, le=3600)
    user_id: str = Field(default="guest")


class ListAutonomySessionsInput(BaseModel):
    """list_autonomy_sessions_tool 的筛选参数。"""

    status: Literal[
        "queued", "running", "idle", "working", "stopping",
        "completed", "failed", "cancelled",
    ] | None = None
    max_results: int = Field(default=30, ge=1, le=100)
    user_id: str = Field(default="guest")


class StopAutonomySessionInput(BaseModel):
    """stop_autonomy_session_tool 的参数结构。"""

    session_id: str = Field(..., min_length=1, max_length=64)
    user_id: str = Field(default="guest")


class CreateTaskWorktreeInput(BaseModel):
    """create_task_worktree_tool 的参数结构。"""

    name: str = Field(
        ...,
        min_length=1,
        max_length=64,
        description="安全 Worktree 名称，例如 wt-auto-task-1。",
    )
    task_id: str | None = Field(
        default=None,
        max_length=64,
        description="可选：绑定已经存在且尚未认领的 pending Task。",
    )
    base_ref: str = Field(
        default="HEAD",
        min_length=1,
        max_length=200,
        description="创建分支的 Git 基点，默认当前 HEAD。",
    )
    user_id: str = Field(default="guest")


class ListWorktreesInput(BaseModel):
    """list_worktrees_tool 的筛选参数。"""

    status: Literal["active", "kept", "removed", "failed"] | None = None
    max_results: int = Field(default=30, ge=1, le=100)
    user_id: str = Field(default="guest")


class KeepWorktreeInput(BaseModel):
    """keep_worktree_tool 只需要 Worktree 名称和后端注入用户。"""

    name: str = Field(..., min_length=1, max_length=64)
    user_id: str = Field(default="guest")


class RemoveWorktreeInput(BaseModel):
    """remove_worktree_tool 默认保护尚未提交或尚未合并的工作。"""

    name: str = Field(..., min_length=1, max_length=64)
    discard_changes: bool = Field(
        default=False,
        description="只有用户明确确认丢弃修改时才能设为 true。",
    )
    user_id: str = Field(default="guest")


class SubmitTeamPlanInput(BaseModel):
    """submit_team_plan_tool 的参数结构；身份字段由 Teammate Harness 注入。"""

    plan: str = Field(..., min_length=1, max_length=4000)
    teammate_name: str = Field(default="", max_length=50)
    user_id: str = Field(default="guest")


class ScheduleCronInput(BaseModel):
    """schedule_cron_tool 的参数结构。"""

    cron: str = Field(
        ...,
        min_length=9,
        max_length=100,
        description=(
            "标准五段式 Cron：分钟 小时 日期 月份 星期，例如 '*/5 * * * *'。"
        ),
    )
    prompt: str = Field(
        ...,
        min_length=1,
        max_length=500,
        description="时间命中后交给后台 Consumer 的工作描述。",
    )
    job_type: Literal[
        "demo_steps",
        "agent_prompt",
        "fund_nav_sync",
        "market_snapshot_sync",
        "market_event_sync",
    ] = Field(
        default="demo_steps",
        description=(
            "受控后台工作流：教学步骤、真实 Agent 请求、基金净值、市场快照或事件同步。"
        ),
    )
    recurring: bool = Field(
        default=True,
        description="true 表示每次命中都触发；false 表示命中一次后删除定义。",
    )
    durable: bool = Field(
        default=True,
        description="true 会写入 data/schedules，FastAPI 重启后恢复。",
    )
    schedule_id: str | None = Field(
        default=None,
        max_length=64,
        description="可选稳定 ID；为空时由后端生成。",
    )
    user_id: str = Field(
        default="guest",
        description="当前用户 ID。后端会自动填入，模型通常不用设置。",
    )


class ListCronsInput(BaseModel):
    """list_crons_tool 的参数结构。"""

    max_results: int = Field(default=50, ge=1, le=100)
    user_id: str = Field(
        default="guest",
        description="当前用户 ID。后端会自动填入，模型通常不用设置。",
    )


class CancelCronInput(BaseModel):
    """cancel_cron_tool 的参数结构。"""

    schedule_id: str = Field(..., min_length=1, max_length=64)
    user_id: str = Field(
        default="guest",
        description="当前用户 ID。后端会自动填入，模型通常不用设置。",
    )


class ConnectMcpInput(BaseModel):
    """connect_mcp_tool 的参数结构。

    模型只能选择 Server 名称。真正的 command、模块和工作目录来自后端白名单，
    不会把“启动任意进程”的能力交给模型。
    """

    server_name: str = Field(
        default="agent-learning",
        min_length=1,
        max_length=80,
        description="要连接的 MCP Server 名称，当前可用 agent-learning。",
    )


def _load_tool_decorator():
    """延迟导入 LangChain 的 tool 装饰器。

    这样如果依赖没安装，错误会在真正使用工具调用时暴露，
    不会影响 FastAPI 项目里其他不依赖 LangChain Tool 的接口。
    """

    from langchain_core.tools import tool

    return tool


def _json_dumps(data: dict[str, Any]) -> str:
    """统一把工具结果转成中文友好的 JSON 字符串。"""

    return json.dumps(data, ensure_ascii=False)


def _relative_path(path: Path, *, root: Path | None = None) -> str:
    """把绝对路径转成当前 Harness 工作区内的相对路径。"""

    workspace_root = (root or current_workspace_root()).resolve()
    return path.resolve().relative_to(workspace_root).as_posix()


def _is_excluded_path(path: Path, *, root: Path | None = None) -> bool:
    """判断路径是否应该对 Agent 隐藏。"""

    workspace_root = (root or current_workspace_root()).resolve()
    relative = _relative_path(path, root=workspace_root)
    parts = set(Path(relative).parts)

    if path.name in SENSITIVE_FILE_NAMES:
        return True

    if path.name.startswith(".env.") and path.name != ".env.example":
        return True

    protected_data_dirs = (
        "data/agent_context",
        "data/chroma",
        "data/memory",
        "data/tasks",
    )
    if any(
        relative == directory or relative.startswith(f"{directory}/")
        for directory in protected_data_dirs
    ):
        return True

    return bool(parts & EXCLUDED_DIR_NAMES)


def _safe_project_path(path_value: str, *, root: Path | None = None) -> Path:
    """把工具路径限制在 Harness 分配的当前工作目录内。"""

    if not path_value or "\x00" in path_value:
        raise ValueError("路径不能为空，也不能包含非法字符。")

    workspace_root = (root or current_workspace_root()).resolve()
    path = (workspace_root / path_value).resolve()

    try:
        path.relative_to(workspace_root)
    except ValueError as exc:
        raise ValueError("只能访问当前 Harness 工作目录内的文件。") from exc

    if _is_excluded_path(path, root=workspace_root):
        raise ValueError("这个路径属于敏感文件或缓存目录，当前工具不允许读取。")

    return path


def _is_probably_text_file(path: Path) -> bool:
    """只允许读取常见源码和文档文件。"""

    if path.name == ".env.example":
        return True

    return path.suffix.lower() in TEXT_FILE_EXTENSIONS


def _iter_project_files(
    pattern: str,
    max_results: int,
    *,
    root: Path | None = None,
) -> tuple[list[Path], bool]:
    """按 pattern 遍历当前项目或当前 Worktree 里的可读文本文件。"""

    normalized_pattern = pattern.strip() or "**/*"
    workspace_root = (root or current_workspace_root()).resolve()
    candidates = workspace_root.rglob("*")
    files: list[Path] = []

    for path in candidates:
        if len(files) >= max_results:
            return files, True

        if not path.is_file():
            continue

        try:
            relative = _relative_path(path, root=workspace_root)
        except ValueError:
            continue

        if _is_excluded_path(path, root=workspace_root):
            continue

        if not _is_probably_text_file(path):
            continue

        if fnmatch(relative, normalized_pattern) or fnmatch(path.name, normalized_pattern):
            files.append(path)

    return files, False


def _safe_calculate(expression: str) -> float | int:
    """只允许计算安全的数学表达式。

    这里不用 Python 的 eval，因为 eval 可以执行任意代码。
    我们把表达式解析成 AST，只放行加减乘除等数学节点。
    """

    if len(expression) > 120:
        raise ValueError("表达式太长，请控制在 120 个字符以内。")

    binary_ops = {
        ast.Add: operator.add,
        ast.Sub: operator.sub,
        ast.Mult: operator.mul,
        ast.Div: operator.truediv,
        ast.FloorDiv: operator.floordiv,
        ast.Mod: operator.mod,
        ast.Pow: operator.pow,
    }
    unary_ops = {
        ast.UAdd: operator.pos,
        ast.USub: operator.neg,
    }

    def eval_node(node: ast.AST) -> float | int:
        if isinstance(node, ast.Expression):
            return eval_node(node.body)

        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return node.value

        if isinstance(node, ast.BinOp) and type(node.op) in binary_ops:
            left = eval_node(node.left)
            right = eval_node(node.right)

            if isinstance(node.op, ast.Pow) and abs(right) > 10:
                raise ValueError("指数太大，当前 demo 不计算这类表达式。")

            return binary_ops[type(node.op)](left, right)

        if isinstance(node, ast.UnaryOp) and type(node.op) in unary_ops:
            return unary_ops[type(node.op)](eval_node(node.operand))

        raise ValueError("只支持数字和 +、-、*、/、//、%、**、括号。")

    tree = ast.parse(expression, mode="eval")
    return eval_node(tree)


def _read_text_lines(path: Path) -> list[str]:
    """读取文本文件，统一处理编码和大小限制。"""

    if path.stat().st_size > 300_000:
        raise ValueError("文件太大，当前工具只读取 300KB 以内的文本文件。")

    try:
        return path.read_text(encoding="utf-8").splitlines()
    except UnicodeDecodeError as exc:
        raise ValueError("这个文件不是 UTF-8 文本文件，当前工具不读取。") from exc


def _safe_note_path(path_value: str) -> Path:
    """write_note_tool 的二次安全检查。

    Permission 层会先检查一次；工具内部再检查一次，避免绕过 Permission 时误写。
    """

    notes_dir = PROJECT_ROOT / "data" / "agent_notes"
    target_path = (PROJECT_ROOT / path_value).resolve()

    try:
        target_path.relative_to(notes_dir.resolve())
    except ValueError as exc:
        raise ValueError("write_note_tool 只允许写入 data/agent_notes/ 目录") from exc

    if target_path.suffix.lower() not in {".md", ".txt"}:
        raise ValueError("write_note_tool 只允许写入 .md 或 .txt 文件")

    return target_path


tool = _load_tool_decorator()


@tool(
    "calculator_tool",
    args_schema=CalculatorInput,
    description="用于计算确定性的数学表达式。适合加减乘除、括号、指数等简单计算问题。",
)
def calculator_tool(expression: str) -> str:
    """计算简单数学表达式，并把结果返回给模型。"""

    try:
        result = _safe_calculate(expression)
    except Exception as exc:
        return _json_dumps(
            {
                "ok": False,
                "error": str(exc),
                "expression": expression,
            },
        )

    return _json_dumps(
        {
            "ok": True,
            "expression": expression,
            "result": result,
        },
    )


@tool(
    "list_files_tool",
    args_schema=ListFilesInput,
    description=(
        "列出 Harness 当前分配工作区中的源码和文档文件。"
        "当你需要先了解项目结构、查找有哪些文件时使用。"
        "不会返回 .env、.git、缓存目录或 Chroma 二进制索引。"
    ),
)
def list_files_tool(pattern: str = "**/*", max_results: int = 80) -> str:
    """列出项目内可读的源码/文档文件。"""

    try:
        files, truncated = _iter_project_files(pattern=pattern, max_results=max_results)
    except Exception as exc:
        return _json_dumps({"ok": False, "error": str(exc)})

    return _json_dumps(
        {
            "ok": True,
            "pattern": pattern,
            "count": len(files),
            "truncated": truncated,
            "files": [_relative_path(path) for path in files],
        }
    )


@tool(
    "read_file_tool",
    args_schema=ReadFileInput,
    description=(
        "读取 Harness 当前分配工作区内某个源码或文档文件的指定行。"
        "适合在 list_files_tool 或 search_text_tool 找到文件后，进一步查看关键代码。"
        "只能读取普通文本文件，不读取 .env、缓存目录或二进制文件。"
    ),
)
def read_file_tool(path: str, start_line: int = 1, max_lines: int = 120) -> str:
    """读取项目内文件的部分内容。"""

    try:
        safe_path = _safe_project_path(path)

        if not safe_path.is_file():
            raise ValueError("路径不是文件。")

        if not _is_probably_text_file(safe_path):
            raise ValueError("当前工具只读取常见源码和文档文本文件。")

        lines = _read_text_lines(safe_path)
        start_index = max(start_line - 1, 0)
        end_index = min(start_index + max_lines, len(lines))
        selected_lines = [
            f"{line_number}: {line}"
            for line_number, line in enumerate(
                lines[start_index:end_index],
                start=start_index + 1,
            )
        ]
    except Exception as exc:
        return _json_dumps({"ok": False, "path": path, "error": str(exc)})

    return _json_dumps(
        {
            "ok": True,
            "path": _relative_path(safe_path),
            "start_line": start_line,
            "end_line": end_index,
            "total_lines": len(lines),
            "truncated": end_index < len(lines),
            "content": "\n".join(selected_lines),
        }
    )


@tool(
    "search_text_tool",
    args_schema=SearchTextInput,
    description=(
        "在 Harness 当前分配工作区的源码和文档中搜索关键词。"
        "适合查找函数名、类名、接口路径、LangChain 方法名，例如 bind_tools、rag_search_tool。"
    ),
)
def search_text_tool(query: str, pattern: str = "**/*", max_results: int = 30) -> str:
    """在项目文本文件中搜索关键词。"""

    results: list[dict[str, Any]] = []
    scanned_files = 0

    try:
        files, files_truncated = _iter_project_files(pattern=pattern, max_results=500)

        for path in files:
            if len(results) >= max_results:
                break

            scanned_files += 1

            try:
                lines = _read_text_lines(path)
            except ValueError:
                continue

            for line_number, line in enumerate(lines, start=1):
                if query in line:
                    results.append(
                        {
                            "path": _relative_path(path),
                            "line_number": line_number,
                            "line": line.strip(),
                        }
                    )

                    if len(results) >= max_results:
                        break
    except Exception as exc:
        return _json_dumps({"ok": False, "query": query, "error": str(exc)})

    return _json_dumps(
        {
            "ok": True,
            "query": query,
            "pattern": pattern,
            "scanned_files": scanned_files,
            "count": len(results),
            "truncated": len(results) >= max_results or files_truncated,
            "results": results,
        }
    )


@tool(
    "workspace_write_file_tool",
    args_schema=WorkspaceWriteFileInput,
    description=(
        "只在当前自治 Task 已绑定隔离 Git Worktree 时，写入一个工作区内文本文件。"
        "路径必须是当前 Worktree 内的相对路径，不能访问主项目、.git、.env 或运行时数据。"
        "适合 teammate 落地代码或文档修改；已有文件默认不会覆盖。"
    ),
)
def workspace_write_file_tool(
    path: str,
    content: str,
    overwrite: bool = False,
) -> str:
    """把文本安全写入当前 Worktree；普通主项目上下文始终拒绝。"""

    try:
        if not is_isolated_workspace():
            raise ValueError(
                "workspace_write_file_tool 只能在后端分配的隔离 Worktree 中使用。"
            )

        workspace_root = current_workspace_root()
        target_path = _safe_project_path(path, root=workspace_root)
        if not _is_probably_text_file(target_path):
            raise ValueError("只允许写入常见源码或文档文本文件。")
        if target_path.exists() and target_path.is_dir():
            raise ValueError("目标路径是目录，不能作为文本文件写入。")
        if target_path.exists() and not overwrite:
            raise ValueError("文件已存在。如需覆盖，请显式传 overwrite=true。")

        # 先写同目录临时文件，再原子替换。进程中途退出时不会留下半截正式文件。
        target_path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = target_path.with_name(
            f".{target_path.name}.{uuid4().hex[:8]}.tmp"
        )
        try:
            temporary_path.write_text(content, encoding="utf-8")
            temporary_path.replace(target_path)
        finally:
            if temporary_path.exists():
                temporary_path.unlink()
    except Exception as exc:
        return _json_dumps(
            {
                "ok": False,
                "path": path,
                "worktree_name": current_worktree_name(),
                "error": str(exc),
            }
        )

    return _json_dumps(
        {
            "ok": True,
            "path": _relative_path(target_path, root=workspace_root),
            "worktree_name": current_worktree_name(),
            "bytes": target_path.stat().st_size,
            "message": "文件已写入隔离 Worktree，主项目目录未被修改。",
        }
    )


@tool(
    "write_note_tool",
    args_schema=WriteNoteInput,
    description=(
        "把学习笔记写入当前项目的 data/agent_notes/ 目录。"
        "适合保存 Agent 学习总结、工具调用观察结果、临时复盘。"
        "只能写 .md 或 .txt，不能写源码目录或 .env。"
    ),
)
def write_note_tool(path: str, content: str, overwrite: bool = False) -> str:
    """把模型整理出的学习笔记写入安全目录。"""

    try:
        target_path = _safe_note_path(path)
        target_path.parent.mkdir(parents=True, exist_ok=True)

        if target_path.exists() and not overwrite:
            raise ValueError("文件已存在。如需覆盖，请显式传 overwrite=true。")

        target_path.write_text(content, encoding="utf-8")
    except Exception as exc:
        return _json_dumps(
            {
                "ok": False,
                "path": path,
                "error": str(exc),
            }
        )

    return _json_dumps(
        {
            "ok": True,
            "path": _relative_path(target_path),
            "bytes": target_path.stat().st_size,
            "message": "学习笔记已写入安全目录",
        }
    )


@tool(
    "todo_write_tool",
    args_schema=TodoWriteInput,
    description=(
        "维护当前复杂任务的短期 todo 计划。"
        "当用户请求需要多个步骤，例如分析项目、阅读多个文件、写总结、修改代码时，"
        "先用它列计划；完成关键步骤后再调用它更新 pending/in_progress/completed 状态。"
        "这不是长期任务数据库，服务重启后会清空。"
    ),
)
def todo_write_tool(items: list[dict[str, str]], user_id: str = "guest") -> str:
    """保存或更新当前用户的短期计划。"""

    try:
        current_todos = todo_state.replace(user_id=user_id, raw_items=items)
    except Exception as exc:
        return _json_dumps(
            {
                "ok": False,
                "user_id": user_id,
                "error": str(exc),
            }
        )

    return _json_dumps(
        {
            "ok": True,
            "user_id": user_id,
            "current_todos": current_todos,
            "message": "TodoWrite 已更新当前短期计划",
        }
    )


@tool(
    "subagent_tool",
    args_schema=SubagentInput,
    description=(
        "派出一个临时 Subagent 完成独立子任务，并只返回精简结论。"
        "适合把代码阅读、模块分析、资料检索这类会污染主上下文的工作交出去。"
        "Subagent 使用全新的 messages，且只能使用受限的只读/检索工具。"
        "不要用它执行写文件、创建 todo 或递归派生子 Agent。"
    ),
)
async def subagent_tool(task: str, max_rounds: int = 3, top_k: int = 3) -> str:
    """运行一个临时 Subagent，并把它的最终摘要返回给主 Agent。"""

    try:
        from app.services.subagent import subagent_service

        result = await subagent_service.run(
            task=task,
            max_rounds=max_rounds,
            top_k=top_k,
        )
    except Exception as exc:
        return _json_dumps(
            {
                "ok": False,
                "task": task,
                "error": str(exc),
            }
        )

    return _json_dumps(result)


@tool(
    "list_skills_tool",
    args_schema=ListSkillsInput,
    description=(
        "列出当前项目内可按需加载的 skills catalog。"
        "它只返回 name、description、路径和 references 列表，不会读取完整 SKILL.md。"
        "适合在用户问题涉及学习路线、项目规则、专门流程时先查看有哪些 skill。"
    ),
)
def list_skills_tool(max_results: int = 20) -> str:
    """列出项目内可用 skill 的轻量目录。"""

    try:
        skills = skill_library_service.list_skills(max_results=max_results)
    except Exception as exc:
        return _json_dumps(
            {
                "ok": False,
                "error": str(exc),
            }
        )

    return _json_dumps(
        {
            "ok": True,
            "count": len(skills),
            "skills": skills,
            "message": "这里只是 skill catalog，完整内容需要再调用 load_skill_tool。",
        }
    )


@tool(
    "load_skill_tool",
    args_schema=LoadSkillInput,
    description=(
        "按需加载当前项目内某个 skill 的完整 SKILL.md 或 references/*.md。"
        "适合在 list_skills_tool 找到相关 skill 后，再读取具体说明。"
        "只能读取 skills/<name>/SKILL.md 或该 skill 的 references/*.md。"
    ),
)
def load_skill_tool(name: str, reference_path: str | None = None) -> str:
    """读取某个 skill 的完整内容或受限 reference。"""

    try:
        result = skill_library_service.load_skill(
            name=name,
            reference_path=reference_path,
        )
    except Exception as exc:
        return _json_dumps(
            {
                "ok": False,
                "name": name,
                "reference_path": reference_path,
                "error": str(exc),
            }
        )

    return _json_dumps(result)


@tool(
    "remember_tool",
    args_schema=RememberInput,
    description=(
        "把重要信息保存为长期记忆。"
        "适合保存用户明确偏好、项目事实、学习进度、用户反馈或以后可能复用的引用线索。"
        "不要保存 API Key、密码、身份证号等敏感信息。"
    ),
)
def remember_tool(
    memory_type: str,
    content: str,
    tags: list[str] | None = None,
    importance: int = 3,
    source: str = "agent",
    user_id: str = "guest",
) -> str:
    """把一条长期记忆写入 data/memory/。"""

    try:
        result = memory_service.remember(
            memory_type=memory_type,
            content=content,
            user_id=user_id,
            tags=tags or [],
            source=source,
            importance=importance,
        )
    except Exception as exc:
        return _json_dumps(
            {
                "ok": False,
                "memory_type": memory_type,
                "error": str(exc),
            }
        )

    return _json_dumps(result)


@tool(
    "search_memory_tool",
    args_schema=SearchMemoryInput,
    description=(
        "检索当前用户的长期记忆。"
        "适合在回答用户偏好、项目进度、过去反馈、长期事实相关问题前使用。"
        "只读取 data/memory/ 下当前用户和 global 的记忆。"
    ),
)
def search_memory_tool(
    query: str,
    memory_type: str | None = None,
    max_results: int = 5,
    user_id: str = "guest",
) -> str:
    """从 data/memory/ 中搜索相关长期记忆。"""

    try:
        result = memory_service.search(
            query=query,
            user_id=user_id,
            memory_type=memory_type,
            max_results=max_results,
        )
    except Exception as exc:
        return _json_dumps(
            {
                "ok": False,
                "query": query,
                "error": str(exc),
            }
        )

    return _json_dumps(result)


@tool(
    "task_create_tool",
    args_schema=TaskCreateInput,
    description=(
        "创建一个跨请求、服务重启后仍存在的正式任务。"
        "适合长期、多阶段或需要依赖关系的工作，不适合替代当前请求里的 TodoWrite。"
        "blocked_by 只能引用已经存在的任务 ID。"
    ),
)
def task_create_tool(
    subject: str,
    description: str = "",
    blocked_by: list[str] | None = None,
    task_id: str | None = None,
    user_id: str = "guest",
) -> str:
    """把一个 pending 任务写入 data/tasks/。"""

    try:
        result = task_store_service.create_task(
            user_id=user_id,
            subject=subject,
            description=description,
            blocked_by=blocked_by or [],
            task_id=task_id,
        )
    except Exception as exc:
        return _json_dumps(
            {
                "ok": False,
                "task_id": task_id,
                "error": str(exc),
            }
        )

    return _json_dumps(result)


@tool(
    "task_list_tool",
    args_schema=TaskListInput,
    description=(
        "查看当前用户的持久化任务，可以按 status、owner 或阻塞状态过滤。"
        "在认领、更新或创建依赖任务前，优先用它确认现有任务状态和 ID。"
    ),
)
def task_list_tool(
    status: str | None = None,
    owner: str | None = None,
    only_blocked: bool = False,
    max_results: int = 50,
    user_id: str = "guest",
) -> str:
    """列出 data/tasks/ 中当前用户的任务。"""

    try:
        result = task_store_service.list_tasks(
            user_id=user_id,
            status=status,
            owner=owner,
            only_blocked=only_blocked,
            max_results=max_results,
        )
    except Exception as exc:
        return _json_dumps(
            {
                "ok": False,
                "error": str(exc),
            }
        )

    return _json_dumps(result)


@tool(
    "task_update_tool",
    args_schema=TaskUpdateInput,
    description=(
        "更新持久化任务的标题、说明、依赖或状态。"
        "已认领的 in_progress 任务完成后，用它设置 completed；"
        "不要用它直接进入 in_progress，应调用 task_claim_tool。"
    ),
)
def task_update_tool(
    task_id: str,
    subject: str | None = None,
    description: str | None = None,
    status: str | None = None,
    blocked_by: list[str] | None = None,
    user_id: str = "guest",
) -> str:
    """更新一个已有任务。"""

    try:
        result = task_store_service.update_task(
            user_id=user_id,
            task_id=task_id,
            subject=subject,
            description=description,
            status=status,
            blocked_by=blocked_by,
        )
    except Exception as exc:
        return _json_dumps(
            {
                "ok": False,
                "task_id": task_id,
                "error": str(exc),
            }
        )

    return _json_dumps(result)


@tool(
    "task_claim_tool",
    args_schema=TaskClaimInput,
    description=(
        "认领一个未被依赖阻塞的 pending 任务，并把状态改为 in_progress。"
        "如果 blocked_by 中仍有任务未完成，后端会拒绝认领。"
    ),
)
def task_claim_tool(
    task_id: str,
    owner: str,
    user_id: str = "guest",
) -> str:
    """经过依赖检查后认领任务。"""

    try:
        result = task_store_service.claim_task(
            user_id=user_id,
            task_id=task_id,
            owner=owner,
        )
    except Exception as exc:
        return _json_dumps(
            {
                "ok": False,
                "task_id": task_id,
                "owner": owner,
                "error": str(exc),
            }
        )

    return _json_dumps(result)


@tool(
    "background_job_start_tool",
    args_schema=BackgroundJobStartInput,
    description=(
        "启动一个不会阻塞当前 Agent Loop 的受控后台 Job，并立即返回 job_id。"
        "适合演示耗时工作先提交、稍后再查询的流程；不要在同一轮里持续轮询等待完成。"
    ),
)
def background_job_start_tool(
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
    user_id: str = "guest",
) -> str:
    """持久化并提交到当前执行器，不等待 Worker 完成。"""

    try:
        result = background_job_service.start_job(
            user_id=user_id,
            title=title,
            total_steps=total_steps,
            step_delay_seconds=step_delay_seconds,
            job_id=job_id,
            job_type=job_type,
            prompt=prompt,
            max_tool_rounds=max_tool_rounds,
            max_retries=max_retries,
            timeout_seconds=timeout_seconds,
            idempotency_key=idempotency_key,
            # 该值来自 AgentToolExecutor 设置的 ContextVar，不来自模型参数。
            execution_context=current_agent_execution_context(),
        )
    except Exception as exc:
        return _json_dumps({"ok": False, "job_id": job_id, "error": str(exc)})

    return _json_dumps(result)


@tool(
    "background_job_status_tool",
    args_schema=BackgroundJobStatusInput,
    description=(
        "根据 job_id 查询一个后台 Job 的进度、结果或错误。"
        "只在用户追问状态或后续请求需要结果时调用，不要在同一次 Agent Loop 中频繁轮询。"
    ),
)
def background_job_status_tool(job_id: str, user_id: str = "guest") -> str:
    """读取当前用户指定 Job 的最新状态。"""

    try:
        result = background_job_service.get_job(user_id=user_id, job_id=job_id)
    except Exception as exc:
        return _json_dumps({"ok": False, "job_id": job_id, "error": str(exc)})

    return _json_dumps(result)


@tool(
    "background_job_list_tool",
    args_schema=BackgroundJobListInput,
    description=(
        "列出当前用户最近的后台 Job，可按状态过滤。"
        "适合用户忘记 job_id，或下一轮对话需要发现哪些工作已完成时使用。"
    ),
)
def background_job_list_tool(
    status: str | None = None,
    max_results: int = 30,
    user_id: str = "guest",
) -> str:
    """列出当前用户持久化到数据库的后台 Job。"""

    try:
        result = background_job_service.list_jobs(
            user_id=user_id,
            status=status,
            max_results=max_results,
        )
    except Exception as exc:
        return _json_dumps({"ok": False, "error": str(exc)})

    return _json_dumps(result)


@tool(
    "background_job_cancel_tool",
    args_schema=BackgroundJobCancelInput,
    description="向尚未结束的后台 Job 发送协作式取消信号。",
)
def background_job_cancel_tool(job_id: str, user_id: str = "guest") -> str:
    """取消 queued/running Job；已完成的 Job 不会被回滚。"""

    try:
        result = background_job_service.cancel_job(user_id=user_id, job_id=job_id)
    except Exception as exc:
        return _json_dumps({"ok": False, "job_id": job_id, "error": str(exc)})

    return _json_dumps(result)


@tool(
    "spawn_teammate_tool",
    args_schema=SpawnTeammateInput,
    description=(
        "注册一个长期 Agent Team 成员，保存稳定名称、角色、指令和状态。"
        "创建成员不会自动执行工作；需要再用 send_team_message_tool 发送消息。"
    ),
)
def spawn_teammate_tool(
    name: str,
    role: str,
    instructions: str = "",
    capabilities: list[str] | None = None,
    user_id: str = "guest",
) -> str:
    """把 LangChain tool 调用适配到 AgentTeamService 的成员注册方法。

    Tool 函数不自己操作 SQL，REST 和模型调用才能共享同一套业务规则。
    """

    try:
        result = agent_team_service.spawn_teammate(
            user_id=user_id,
            name=name,
            role=role,
            instructions=instructions,
            capabilities=capabilities,
        )
    except Exception as exc:
        return _json_dumps({"ok": False, "name": name, "error": str(exc)})
    return _json_dumps(result)


@tool(
    "list_teammates_tool",
    args_schema=ListTeammatesInput,
    description="列出当前用户的长期 teammate、角色、状态和当前后台 Job。",
)
def list_teammates_tool(max_results: int = 30, user_id: str = "guest") -> str:
    """读取持久化 Teammate Registry，不触发任何成员运行。"""

    try:
        result = agent_team_service.list_teammates(
            user_id=user_id,
            max_results=max_results,
        )
    except Exception as exc:
        return _json_dumps({"ok": False, "error": str(exc)})
    return _json_dumps(result)


@tool(
    "send_team_message_tool",
    args_schema=SendTeamMessageInput,
    description=(
        "以 main-agent 身份给一个已注册 teammate 发送消息。dispatch=true 时消息会进入"
        "现有 Background Job/Celery Runtime，由 teammate 独立 Agent Loop 处理；"
        "工具会立即返回 job_id，不要在同一 Agent Loop 中持续轮询。"
    ),
)
def send_team_message_tool(
    recipient: str,
    content: str,
    dispatch: bool = True,
    max_rounds: int = 3,
    max_retries: int = 1,
    timeout_seconds: int = 300,
    user_id: str = "guest",
) -> str:
    """消息先持久化，再按需提交 teammate AgentRun。

    返回值中的 job_id 是异步执行凭据；工具本身不会等待 teammate 最终回答。
    """

    try:
        result = agent_team_service.send_message(
            user_id=user_id,
            sender=MAIN_AGENT_NAME,
            recipient=recipient,
            content=content,
            dispatch=dispatch,
            max_rounds=max_rounds,
            max_retries=max_retries,
            timeout_seconds=timeout_seconds,
        )
    except Exception as exc:
        return _json_dumps(
            {"ok": False, "recipient": recipient, "error": str(exc)}
        )
    return _json_dumps(result)


@tool(
    "check_team_inbox_tool",
    args_schema=CheckTeamInboxInput,
    description=(
        "查看 main-agent 或指定 teammate 的 Message Bus inbox。"
        "后续请求需要收取 teammate 回复时，默认查看 main-agent inbox。"
    ),
)
def check_team_inbox_tool(
    recipient: str = MAIN_AGENT_NAME,
    unread_only: bool = False,
    mark_read: bool = False,
    max_results: int = 30,
    user_id: str = "guest",
) -> str:
    """读取团队消息；默认不改变已读状态，便于重复观察。

    主 Agent 通常在后续用户请求中读取自己的 inbox，而不是发送后原地轮询。
    """

    try:
        result = agent_team_service.check_inbox(
            user_id=user_id,
            recipient=recipient,
            unread_only=unread_only,
            mark_read=mark_read,
            max_results=max_results,
        )
    except Exception as exc:
        return _json_dumps({"ok": False, "recipient": recipient, "error": str(exc)})
    return _json_dumps(result)


@tool(
    "request_teammate_shutdown_tool",
    args_schema=RequestTeammateShutdownInput,
    description=(
        "向一个空闲 teammate 发起 s16 结构化关闭握手。"
        "后端会创建 shutdown_request、request_id 和内部协议 Job；"
        "工具立即返回，不要在同一 Agent Loop 中忙等响应。"
    ),
)
def request_teammate_shutdown_tool(
    teammate_name: str,
    reason: str = "",
    user_id: str = "guest",
) -> str:
    """创建 shutdown ProtocolState，并通过 Worker 分发 typed request。"""

    try:
        result = team_protocol_service.request_shutdown(
            user_id=user_id,
            teammate_name=teammate_name,
            reason=reason,
            dispatch=True,
        )
    except Exception as exc:
        return _json_dumps(
            {"ok": False, "teammate_name": teammate_name, "error": str(exc)}
        )
    return _json_dumps(result)


@tool(
    "list_team_protocols_tool",
    args_schema=ListTeamProtocolsInput,
    description=(
        "列出当前用户的 Team Protocol 请求及 typed events。"
        "用于发现 pending 计划审批，或确认 shutdown 是否已 approved/rejected。"
    ),
)
def list_team_protocols_tool(
    status: str | None = None,
    max_results: int = 30,
    user_id: str = "guest",
) -> str:
    """读取持久化 ProtocolState；这是无副作用查询工具。"""

    try:
        result = team_protocol_service.list_protocols(
            user_id=user_id,
            status=status,
            max_results=max_results,
        )
    except Exception as exc:
        return _json_dumps({"ok": False, "error": str(exc)})
    return _json_dumps(result)


@tool(
    "review_team_plan_tool",
    args_schema=ReviewTeamPlanInput,
    description=(
        "批准或拒绝一个 pending plan_approval 请求。"
        "必须使用 list_team_protocols_tool 得到真实 request_id；"
        "同一请求只能响应一次。"
    ),
)
def review_team_plan_tool(
    request_id: str,
    approve: bool,
    feedback: str = "",
    user_id: str = "guest",
) -> str:
    """让 main-agent 写入与原 request_id 匹配的 plan_approval_response。"""

    try:
        result = team_protocol_service.review_plan(
            user_id=user_id,
            request_id=request_id,
            approve=approve,
            feedback=feedback,
        )
    except Exception as exc:
        return _json_dumps(
            {"ok": False, "request_id": request_id, "error": str(exc)}
        )
    return _json_dumps(result)


@tool(
    "start_teammate_autonomy_tool",
    args_schema=StartTeammateAutonomyInput,
    description=(
        "为已有 teammate 启动 s17 受控自治循环。它会优先检查 shutdown/inbox，"
        "再主动扫描并原子认领无 owner、依赖已完成的 Task；达到边界后自动停止。"
    ),
)
def start_teammate_autonomy_tool(
    teammate_name: str,
    poll_interval_seconds: float = 2.0,
    max_idle_polls: int = 5,
    max_tasks: int = 5,
    max_rounds_per_task: int = 3,
    timeout_seconds: int = 300,
    user_id: str = "guest",
) -> str:
    """启动后台自治 Job；当前主 Agent 不在同一轮等待它完成。"""

    try:
        result = autonomous_agent_service.start_session(
            user_id=user_id,
            teammate_name=teammate_name,
            poll_interval_seconds=poll_interval_seconds,
            max_idle_polls=max_idle_polls,
            max_tasks=max_tasks,
            max_rounds_per_task=max_rounds_per_task,
            timeout_seconds=timeout_seconds,
        )
    except Exception as exc:
        return _json_dumps({"ok": False, "teammate_name": teammate_name, "error": str(exc)})
    return _json_dumps(result)


@tool(
    "list_autonomy_sessions_tool",
    args_schema=ListAutonomySessionsInput,
    description="列出自治会话的 phase、当前任务、完成数、空闲轮询数和停止原因。",
)
def list_autonomy_sessions_tool(
    status: str | None = None,
    max_results: int = 30,
    user_id: str = "guest",
) -> str:
    """无副作用读取自治会话。"""

    try:
        result = autonomous_agent_service.list_sessions(
            user_id=user_id, status=status, max_results=max_results
        )
    except Exception as exc:
        return _json_dumps({"ok": False, "error": str(exc)})
    return _json_dumps(result)


@tool(
    "stop_autonomy_session_tool",
    args_schema=StopAutonomySessionInput,
    description="给指定自治会话写入优雅停止信号，Worker 会在下一个检查点退出。",
)
def stop_autonomy_session_tool(
    session_id: str,
    user_id: str = "guest",
) -> str:
    """请求停止而不是直接杀线程，保留一致的会话和 teammate 状态。"""

    try:
        result = autonomous_agent_service.request_stop(
            user_id=user_id, session_id=session_id
        )
    except Exception as exc:
        return _json_dumps({"ok": False, "session_id": session_id, "error": str(exc)})
    return _json_dumps(result)


@tool(
    "create_task_worktree_tool",
    args_schema=CreateTaskWorktreeInput,
    description=(
        "创建真实 Git Worktree，并可绑定现有 pending Task。"
        "先用 task_list_tool 确认 task_id；当前项目必须已经 git init 且存在首个 commit。"
        "它只创建隔离分支和目录，不会自动启动 teammate、合并或提交代码。"
    ),
)
def create_task_worktree_tool(
    name: str,
    task_id: str | None = None,
    base_ref: str = "HEAD",
    user_id: str = "guest",
) -> str:
    """把主 Agent 的工具请求交给确定性的 WorktreeService。"""

    try:
        result = worktree_service.create_worktree(
            user_id=user_id,
            name=name,
            task_id=task_id,
            base_ref=base_ref,
        )
    except Exception as exc:
        return _json_dumps(
            {
                "ok": False,
                "name": name,
                "task_id": task_id,
                "error": str(exc),
                "readiness": worktree_service.readiness(),
            }
        )
    return _json_dumps(result)


@tool(
    "list_worktrees_tool",
    args_schema=ListWorktreesInput,
    description=(
        "查看当前用户 Worktree、Task 绑定、状态、未提交文件数和领先提交数。"
        "也会返回 Git readiness，适合创建前检查或自治任务完成后 review。"
    ),
)
def list_worktrees_tool(
    status: str | None = None,
    max_results: int = 30,
    user_id: str = "guest",
) -> str:
    """读取 Worktree 状态，不修改文件或 Git 分支。"""

    try:
        result = worktree_service.list_worktrees(
            user_id=user_id,
            status=status,
            max_results=max_results,
        )
    except Exception as exc:
        return _json_dumps({"ok": False, "error": str(exc)})
    return _json_dumps(result)


@tool(
    "keep_worktree_tool",
    args_schema=KeepWorktreeInput,
    description=(
        "把 active Worktree 标记为 kept，保留分支和文件供人工 review。"
        "这个动作不会提交、合并或删除任何代码。"
    ),
)
def keep_worktree_tool(name: str, user_id: str = "guest") -> str:
    """显式保留 Worktree，适合任务完成后交给用户检查。"""

    try:
        result = worktree_service.keep_worktree(user_id=user_id, name=name)
    except Exception as exc:
        return _json_dumps({"ok": False, "name": name, "error": str(exc)})
    return _json_dumps(result)


@tool(
    "remove_worktree_tool",
    args_schema=RemoveWorktreeInput,
    description=(
        "删除 Worktree 目录和临时分支。默认在 dirty 或存在独立 commit 时拒绝；"
        "只有用户明确要求丢弃这些工作时，才能设置 discard_changes=true。"
    ),
)
def remove_worktree_tool(
    name: str,
    discard_changes: bool = False,
    user_id: str = "guest",
) -> str:
    """执行有数据保护的 Worktree 清理。"""

    try:
        result = worktree_service.remove_worktree(
            user_id=user_id,
            name=name,
            discard_changes=discard_changes,
        )
    except Exception as exc:
        return _json_dumps({"ok": False, "name": name, "error": str(exc)})
    return _json_dumps(result)


@tool(
    "submit_team_plan_tool",
    args_schema=SubmitTeamPlanInput,
    description=(
        "Teammate 专用：把计划作为 plan_approval_request 提交给 main-agent。"
        "teammate_name 和 user_id 由后端注入，不要自行伪造。"
    ),
)
def submit_team_plan_tool(
    plan: str,
    teammate_name: str = "",
    user_id: str = "guest",
) -> str:
    """Teammate 创建 pending 计划审批请求，不代表计划已经获准。"""

    try:
        result = team_protocol_service.submit_plan(
            user_id=user_id,
            teammate_name=teammate_name,
            plan=plan,
        )
    except Exception as exc:
        return _json_dumps({"ok": False, "error": str(exc)})
    return _json_dumps(result)


@tool(
    "schedule_cron_tool",
    args_schema=ScheduleCronInput,
    description=(
        "注册一个标准五段式 Cron Schedule，让工作在未来按时间自动进入 Queue。"
        "durable=true 时定义会保存到本地，recurring=false 时命中一次后自动删除。"
    ),
)
def schedule_cron_tool(
    cron: str,
    prompt: str,
    job_type: str = "demo_steps",
    recurring: bool = True,
    durable: bool = True,
    schedule_id: str | None = None,
    user_id: str = "guest",
) -> str:
    """创建 Cron 定义；实际工作会在未来命中时由 Queue Processor 交付。"""

    try:
        result = cron_scheduler_service.create_schedule(
            user_id=user_id,
            cron=cron,
            prompt=prompt,
            job_type=job_type,
            recurring=recurring,
            durable=durable,
            schedule_id=schedule_id,
        )
    except Exception as exc:
        return _json_dumps(
            {"ok": False, "schedule_id": schedule_id, "error": str(exc)}
        )

    return _json_dumps(result)


@tool(
    "list_crons_tool",
    args_schema=ListCronsInput,
    description=(
        "列出当前用户的 Cron Schedule、队列深度和最近交付记录。"
        "创建或取消前可先用它确认 ID 和持久化状态。"
    ),
)
def list_crons_tool(max_results: int = 50, user_id: str = "guest") -> str:
    """读取当前用户 Cron 状态，不修改调度器。"""

    try:
        result = cron_scheduler_service.list_schedules(
            user_id=user_id,
            max_results=max_results,
        )
    except Exception as exc:
        return _json_dumps({"ok": False, "error": str(exc)})

    return _json_dumps(result)


@tool(
    "cancel_cron_tool",
    args_schema=CancelCronInput,
    description=(
        "按 schedule_id 取消未来的 Cron 触发。"
        "已经进入 Queue 或已经启动的 Background Job 不会被回滚。"
    ),
)
def cancel_cron_tool(schedule_id: str, user_id: str = "guest") -> str:
    """删除当前用户的 Cron 定义和对应 durable JSON。"""

    try:
        result = cron_scheduler_service.cancel_schedule(
            user_id=user_id,
            schedule_id=schedule_id,
        )
    except Exception as exc:
        return _json_dumps(
            {"ok": False, "schedule_id": schedule_id, "error": str(exc)}
        )

    return _json_dumps(result)


@tool(
    "connect_mcp_tool",
    args_schema=ConnectMcpInput,
    description=(
        "连接后端白名单中的 MCP Server，并通过 tools/list 动态发现外部工具。"
        "当前可连接 agent-learning；连接后下一轮会出现 mcp__agent-learning__* 工具。"
    ),
)
async def connect_mcp_tool(server_name: str = "agent-learning") -> str:
    """启动 MCP 子进程并完成 initialize + tools/list。"""

    try:
        server = await mcp_tool_service.connect(server_name)
    except Exception as exc:
        return _json_dumps(
            {
                "ok": False,
                "server_name": server_name,
                "error": str(exc),
                "available_servers": mcp_tool_service.configured_server_names(),
            }
        )

    return _json_dumps(
        {
            "ok": True,
            "message": "MCP Server 已连接，主 Agent 将重建动态工具池。",
            "server": server,
        }
    )


@tool(
    "rag_search_tool",
    args_schema=RagSearchInput,
    description=(
        "用于查询当前 FastAPI Agent 学习项目的本地知识库。"
        "当用户询问项目里的 RAG、LCEL、Runnable、embedding、Chroma、代码改动等内容时使用。"
    ),
)
def rag_search_tool(question: str, top_k: int = 3) -> str:
    """调用现有的真实向量库 RAG 检索能力，只返回检索结果，不直接生成最终答案。"""

    try:
        response = vector_rag_service.search(
            RagQueryRequest(message=question, user_id="tool", top_k=top_k)
        )
    except Exception as exc:
        return _json_dumps(
            {
                "ok": False,
                "error": str(exc),
                "hint": "如果 Chroma 为空，请先调用 /rag/vector/rebuild；如果缺少 key，请检查 .env。",
            },
        )

    return _json_dumps(
        {
            "ok": True,
            "question": response.question,
            "top_k": response.top_k,
            "hits": [
                {
                    "chunk_id": hit.chunk_id,
                    "source": hit.source,
                    "similarity": hit.similarity,
                    "text": hit.text,
                }
                for hit in response.hits
            ],
        },
    )


def get_agent_tools() -> list[Any]:
    """返回完整开发者工具注册表。

    这个函数保留给 Harness、Worker 和已有教学测试使用。产品聊天不能直接绑定该列表，
    必须经过 ``get_product_agent_tools`` 或 ``select_agent_tools`` 的权限过滤。
    """

    # 只有放进这个列表的工具才会同时出现在 bind_tools schema 和主 Agent tool_map 中。
    # 注册顺序不决定模型调用顺序，真正选择仍由模型根据 description 和 prompt 完成。
    return [
        calculator_tool,
        # 综合金融研究先规划证据、并行采集并执行充分性闸门；它不替代窄查询工具。
        *get_financial_research_agent_tools(),
        # FundScope 业务工具只读同一份基金服务数据，因此页面与 Agent 不会出现两套口径。
        *get_fund_agent_tools(),
        # 市场模块同时提供量化状态和可追溯事件检索，两者仍由不同 Tool 保持边界。
        *get_market_agent_tools(),
        # 组合工具读取持久化目标权重，并复用单基金 Assessment 做确定性汇总。
        *get_portfolio_agent_tools(),
        run_portfolio_stress_tool,
        get_portfolio_stress_tool,
        draft_portfolio_allocation_tool,
        get_portfolio_allocation_tool,
        list_files_tool,
        read_file_tool,
        search_text_tool,
        todo_write_tool,
        subagent_tool,
        list_skills_tool,
        load_skill_tool,
        remember_tool,
        search_memory_tool,
        task_create_tool,
        task_list_tool,
        task_update_tool,
        task_claim_tool,
        background_job_start_tool,
        background_job_status_tool,
        background_job_list_tool,
        background_job_cancel_tool,
        spawn_teammate_tool,
        list_teammates_tool,
        send_team_message_tool,
        check_team_inbox_tool,
        request_teammate_shutdown_tool,
        list_team_protocols_tool,
        review_team_plan_tool,
        start_teammate_autonomy_tool,
        list_autonomy_sessions_tool,
        stop_autonomy_session_tool,
        create_task_worktree_tool,
        list_worktrees_tool,
        keep_worktree_tool,
        remove_worktree_tool,
        schedule_cron_tool,
        list_crons_tool,
        cancel_cron_tool,
        connect_mcp_tool,
        write_note_tool,
        rag_search_tool,
    ]


def get_product_agent_tools() -> list[Any]:
    """返回基金产品模式可以向模型公开的最小工具池。"""

    allowed_names = set(PRODUCT_TOOL_CAPABILITIES)
    return [tool for tool in get_agent_tools() if tool.name in allowed_names]


def select_agent_tools(
    tool_names: tuple[str, ...] | list[str],
    *,
    execution_context: AgentExecutionContext | None = None,
) -> list[Any]:
    """按路由结果选择工具，同时保留主注册表中的稳定顺序。

    路由器只返回工具名称，不直接导入工具对象。这里统一完成名称到 LangChain Tool 的
    映射，避免业务路由与数十个工具实现产生循环依赖。即使路由器以后出现缺陷，
    这里仍会根据可信执行上下文做一次 schema 过滤。
    """

    context = execution_context or current_agent_execution_context()
    requested = set(tool_names)
    source_tools = (
        get_product_agent_tools()
        if context.mode == "product"
        else get_agent_tools()
    )
    return [
        tool
        for tool in source_tools
        if tool.name in requested and context.can_expose_tool(tool.name)
    ]


def get_subagent_tools() -> list[Any]:
    """返回 Subagent 可以使用的受限工具列表。

    Subagent 的重点是“干净上下文 + 受控能力”。
    所以这里故意不给它写入、规划、派生 Agent、后台 Job 和团队管理工具。
    s15 teammate 会在这个列表之上再按 capabilities 过滤一次。
    """

    return [
        calculator_tool,
        list_files_tool,
        read_file_tool,
        search_text_tool,
        rag_search_tool,
    ]


def get_teammate_tools(*, allow_workspace_write: bool = False) -> list[Any]:
    """返回长期 Teammate 工具集，并按运行上下文决定是否加入 Worktree 写工具。

    submit_team_plan_tool 不出现在主 Agent 或临时 Subagent 工具列表中。调用时身份由
    SubagentService 根据当前 teammate 上下文注入，模型不能伪造 sender。
    ``allow_workspace_write`` 也由自治 Harness 根据 Task 绑定注入，不由模型决定。
    """

    tools = [*get_subagent_tools(), submit_team_plan_tool]
    if allow_workspace_write:
        tools.append(workspace_write_file_tool)
    return tools


def get_agent_tool_map() -> dict[str, Any]:
    """按工具名建立索引，方便根据模型返回的 tool_call 找到对应函数。"""

    return {tool.name: tool for tool in get_agent_tools()}


def get_subagent_tool_map() -> dict[str, Any]:
    """按工具名建立 Subagent 受限工具索引。"""

    return {tool.name: tool for tool in get_subagent_tools()}


def list_agent_tools() -> list[dict[str, Any]]:
    """生成给 `/tools` 接口展示的工具说明。"""

    tools = []

    for tool_item in get_agent_tools():
        tools.append(
            {
                "name": tool_item.name,
                "description": tool_item.description or "",
                "args_schema": getattr(tool_item, "args", {}) or {},
            }
        )

    return tools
