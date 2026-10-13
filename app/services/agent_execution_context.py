"""Agent 工具执行所使用的可信身份与能力上下文。

浏览器请求体和模型生成的 tool arguments 都属于不可信输入。真正的模式、角色和资源
所有者必须由 FastAPI 登录 Session 或后端 Worker 构造，再沿 Agent Loop 传到工具执行层。
本模块只描述授权事实，不负责具体工具参数是否合法；参数级规则仍由
``tool_permissions.py`` 处理。
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Iterator, Literal


AgentMode = Literal["product", "developer"]

FUND_READ = "fund:read"
PORTFOLIO_READ = "portfolio:read"
# 运行组合研究会创建后台 Job，因此和普通只读组合查询分开授权。
PORTFOLIO_RESEARCH_RUN = "portfolio:research-run"
MARKET_READ = "market:read"
KNOWLEDGE_READ = "knowledge:read"
CALCULATOR_USE = "calculator:use"
TOOL_DISCOVERY = "tools:discover"
DEVELOPER_TOOLS = "developer:tools"

# 产品 Agent 只看得到面向基金研究的能力。新增工具不会因为忘记分类而自动进入产品池；
# 它必须先在这里被明确登记，符合最小权限和默认拒绝原则。
PRODUCT_TOOL_CAPABILITIES: dict[str, str] = {
    # 运行时按请求创建该工具；它不能让产品用户看见开发者工具或授予新权限。
    "search_tools_tool": TOOL_DISCOVERY,
    "calculator_tool": CALCULATOR_USE,
    "get_fund_research_brief_tool": FUND_READ,
    "get_portfolio_research_brief_tool": PORTFOLIO_READ,
    "get_fund_profile_tool": FUND_READ,
    "get_fund_holdings_tool": FUND_READ,
    "get_fund_report_research_tool": FUND_READ,
    "get_fund_nav_summary_tool": FUND_READ,
    "get_fund_risk_tool": FUND_READ,
    "get_fund_assessment_tool": FUND_READ,
    "get_fund_semantic_review_tool": FUND_READ,
    "get_portfolio_assessment_tool": PORTFOLIO_READ,
    "get_portfolio_performance_tool": PORTFOLIO_READ,
    "get_portfolio_exposure_tool": PORTFOLIO_READ,
    "get_portfolio_event_impacts_tool": PORTFOLIO_READ,
    "list_portfolio_decisions_tool": PORTFOLIO_READ,
    "start_portfolio_research_tool": PORTFOLIO_RESEARCH_RUN,
    "run_portfolio_stress_tool": PORTFOLIO_RESEARCH_RUN,
    "get_portfolio_stress_tool": PORTFOLIO_READ,
    "draft_portfolio_allocation_tool": PORTFOLIO_RESEARCH_RUN,
    "get_portfolio_allocation_tool": PORTFOLIO_READ,
    "get_portfolio_research_status_tool": PORTFOLIO_READ,
    "get_market_sentiment_tool": MARKET_READ,
    "get_retail_indices_tool": MARKET_READ,
    "get_large_capital_signals_tool": MARKET_READ,
    "get_market_hotspots_tool": MARKET_READ,
    "search_market_events_tool": MARKET_READ,
    "rag_search_tool": KNOWLEDGE_READ,
}

PRODUCT_CAPABILITIES = frozenset(PRODUCT_TOOL_CAPABILITIES.values())


@dataclass(frozen=True)
class AgentAccessDecision:
    """执行上下文对一个工具给出的第一层授权结果。"""

    allowed: bool
    reason: str


@dataclass(frozen=True)
class AgentExecutionContext:
    """后端构造的一次 Agent 执行身份，模型和前端不能自行修改。"""

    user_id: str
    role: str
    mode: AgentMode
    capabilities: frozenset[str]
    authenticated: bool = False
    source: str = "backend"
    # 三个字段只由后端授权服务绑定，不能放进模型工具参数或从请求体直接反序列化。
    # run_id 区分两次工作，agent_id 区分主/子 Agent，grant_id 指向可撤销的授权记录。
    task_grant_id: str | None = None
    task_run_id: str | None = None
    task_agent_id: str | None = None

    @classmethod
    def product(
        cls,
        *,
        user_id: str,
        role: str = "user",
        authenticated: bool = False,
        source: str = "product-api",
    ) -> "AgentExecutionContext":
        """构造基金产品模式；管理员从产品入口进入时仍只有产品能力。"""

        return cls(
            user_id=cls._normalize_user_id(user_id),
            role=str(role or "user"),
            mode="product",
            capabilities=PRODUCT_CAPABILITIES,
            authenticated=authenticated,
            source=source,
        )

    @classmethod
    def developer(
        cls,
        *,
        user_id: str,
        role: str,
        authenticated: bool = False,
        source: str = "developer-api",
    ) -> "AgentExecutionContext":
        """构造开发者模式；只有服务端确认的管理员或内部 system 才能创建。"""

        normalized_role = str(role or "").strip().lower()
        if normalized_role not in {"admin", "system"}:
            raise ValueError("开发者模式只允许服务端确认的管理员身份")
        return cls(
            user_id=cls._normalize_user_id(user_id),
            role=normalized_role,
            mode="developer",
            capabilities=frozenset({*PRODUCT_CAPABILITIES, DEVELOPER_TOOLS}),
            authenticated=authenticated,
            source=source,
        )

    @classmethod
    def internal_developer(
        cls,
        *,
        user_id: str,
        source: str,
    ) -> "AgentExecutionContext":
        """供经过后端调度的 Subagent/Worker 使用，不接受 HTTP 请求字段。"""

        return cls.developer(
            user_id=user_id,
            role="system",
            authenticated=True,
            source=source,
        )

    def can_expose_tool(self, tool_name: str) -> bool:
        """决定工具 schema 能否出现在本次模型上下文中。"""

        required = PRODUCT_TOOL_CAPABILITIES.get(tool_name)
        if required is not None:
            return required in self.capabilities
        # MCP 动态工具、文件工具和 Harness 控制工具全部属于开发者能力。
        return self.mode == "developer" and DEVELOPER_TOOLS in self.capabilities

    def authorize_tool(
        self,
        *,
        tool_name: str,
        tool_args: dict[str, Any],
    ) -> AgentAccessDecision:
        """执行前复核模式和资源所有权，防止伪造或过期 tool_call 绕过工具池。"""

        if not self.can_expose_tool(tool_name):
            return AgentAccessDecision(
                False,
                f"{self.mode} 模式未授权工具：{tool_name}",
            )

        # 所有带 user_id 的工具都只能操作当前可信身份自己的资源。主执行器还会覆盖
        # 该参数；这里是独立的第二层检查，保护其他直接调用 Permission 的执行路径。
        requested_user_id = str(tool_args.get("user_id") or "").strip()
        if requested_user_id and requested_user_id != self.user_id:
            return AgentAccessDecision(
                False,
                "工具参数中的 user_id 与当前可信身份不一致",
            )

        return AgentAccessDecision(True, "execution context allowed")

    @staticmethod
    def _normalize_user_id(value: str) -> str:
        normalized = str(value or "guest").strip()[:50]
        return normalized or "guest"


_active_execution_context: ContextVar[AgentExecutionContext | None] = ContextVar(
    "fundscope_agent_execution_context",
    default=None,
)


def current_agent_execution_context() -> AgentExecutionContext:
    """读取当前异步任务的上下文；孤立调用默认降级为最小产品权限。"""

    return _active_execution_context.get() or AgentExecutionContext.product(
        user_id="guest",
        source="safe-default",
    )


@contextmanager
def agent_execution_scope(
    execution_context: AgentExecutionContext,
) -> Iterator[None]:
    """让嵌套 Subagent 在当前 asyncio Task 中继承同一可信权限边界。"""

    token = _active_execution_context.set(execution_context)
    try:
        yield
    finally:
        _active_execution_context.reset(token)
