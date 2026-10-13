"""请求内的只读工具发现：找能力，不抓数据，也不替模型执行找到的工具。

路由器仍负责首轮选工具；这里只为复杂产品问题补充遗漏的能力。目录从现有注册表
读取，模型只看到少量命中项。每个请求创建自己的实例，搜索次数和已加载集合不共享。
"""

from __future__ import annotations

import json
import re
from time import perf_counter
from typing import Any, Callable

from langchain_core.tools import StructuredTool
from langchain_core.utils.function_calling import convert_to_openai_tool
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.config import settings
from app.schemas import AgentChatRequest, AgentToolDiscoveryTrace
from app.services.agent_execution_context import (
    AgentExecutionContext,
    current_agent_execution_context,
)
from app.services.agent_routing import AgentRequestRoute, TOOL_GROUPS


SEARCH_TOOL_NAME = "search_tools_tool"

# 这里只维护审核过的只读范围和检索别名，不复制函数、参数 Schema 或工具描述。
# 季报、综合研究 Brief、嵌套模型、Job、交易写入、MCP 均不允许经此入口补入。
READ_TOOL_KEYWORDS: dict[str, tuple[str, ...]] = {
    "get_fund_profile_tool": ("档案", "类型", "经理", "规模", "费率", "profile"),
    "get_fund_holdings_tool": ("持仓", "重仓", "穿透", "行业分布", "披露", "holdings"),
    "get_fund_nav_summary_tool": ("净值", "涨跌", "收益", "回报", "走势", "nav", "return"),
    "get_fund_risk_tool": ("风险", "回撤", "波动", "夏普", "risk", "drawdown"),
    "get_fund_assessment_tool": ("综合评估", "整体状态", "评估", "assessment"),
    "get_market_sentiment_tool": ("市场环境", "指数", "宽度", "成交", "情绪", "sentiment"),
    "get_large_capital_signals_tool": ("资金", "机构", "国家队", "申购", "龙虎榜", "capital"),
    "get_market_hotspots_tool": ("热点", "热度", "升温", "降温", "发酵", "hotspot"),
    "search_market_events_tool": ("新闻", "消息", "政策", "事件", "公告", "监管", "news", "event"),
    "get_retail_indices_tool": ("韭菜", "宝妈", "散户", "retail"),
    "get_portfolio_assessment_tool": ("组合评估", "整体状态", "配置风险", "assessment"),
    "get_portfolio_performance_tool": ("组合收益", "收益", "盈亏", "成本", "业绩", "performance"),
    "get_portfolio_exposure_tool": ("穿透", "重叠", "集中度", "行业分布", "暴露", "exposure"),
    "get_portfolio_event_impacts_tool": ("事件影响", "新闻影响", "消息影响", "关联事件", "impact"),
    "list_portfolio_decisions_tool": ("历史决策", "决策记录", "历史报告", "decision"),
}
EXPLORATORY_WORDS = (
    "结合", "综合", "对比", "比较", "关联", "原因", "为什么", "为何", "同时",
    "分别", "联动", "补查", "缺口", "多角度", "交叉", "compare", "why",
)


class ToolSearchInput(BaseModel):
    """模型只能描述需要的能力，不能指定用户身份、加载数量或权限范围。"""

    model_config = ConfigDict(extra="forbid")
    query: str = Field(min_length=2, max_length=160, strict=True,
                       description="缺少的只读能力，例如：查询半导体新闻政策或基金持仓披露。")

    @field_validator("query", mode="before")
    @classmethod
    def strip_query(cls, value: Any) -> Any:
        return value.strip() if isinstance(value, str) else value


def tool_schema_chars(tools: list[Any]) -> int:
    """统计标准化工具定义的字符数，不把字符估算冒充供应商实际 Token。"""

    return len(json.dumps([convert_to_openai_tool(tool) for tool in tools],
                          ensure_ascii=False, separators=(",", ":"))) if tools else 0


class ToolDiscoveryRun:
    """一次请求的目录、搜索预算和动态工具池；不存在跨用户的全局可变状态。"""

    def __init__(
        self, *, request: AgentChatRequest, route: AgentRequestRoute,
        context: AgentExecutionContext, initial_tools: list[Any],
        registry: Callable[[], list[Any]],
    ) -> None:
        self.context = context
        self.active_tools = list(initial_tools)
        self.catalog: dict[str, Any] = {}
        self.search_count = 0
        self.seen_queries: set[str] = set()
        self.closed = False
        # 配置可以降低上限，但不能把一次发现变成无边界的全量工具加载。
        self.max_calls = max(0, min(settings.agent_tool_discovery_max_calls, 4))
        self.top_k = max(1, min(settings.agent_tool_discovery_top_k, 3))
        self.max_schema_chars = max(0, settings.agent_tool_discovery_max_schema_chars)
        self.trace = AgentToolDiscoveryTrace(initial_tools=[tool.name for tool in initial_tools])
        self.tool = StructuredTool.from_function(
            coroutine=self.search, name=SEARCH_TOOL_NAME, args_schema=ToolSearchInput,
            description=("仅当当前工具不足以回答复杂基金/组合/市场问题时，搜索并加载少量已授权只读工具。"
                         "不查询业务数据；返回工具在下一轮才可调用。已有证据足够时直接回答，不要为搜索而搜索。"),
        )

        eligible = (
            settings.agent_tool_discovery_enabled and self.max_calls > 0
            and context.mode == "product" and context.can_expose_tool(SEARCH_TOOL_NAME)
            and request.max_tool_rounds >= 2 and not route.needs_clarification
            and not request.fund_report_ids and not request.research_job_id
            and bool(route.tool_names) and set(route.tool_names).issubset(READ_TOOL_KEYWORDS)
            and (len(route.domains) > 1 or any(word in request.message.lower() for word in EXPLORATORY_WORDS))
        )
        if eligible:
            # 没有明确研究对象时，不靠工具发现猜测组合或基金；继续沿用原有澄清流程。
            has_fund = bool(request.fund_code or re.search(r"(?<!\d)\d{6}(?!\d)", request.message))
            for tool in registry():
                name = tool.name
                if name not in READ_TOOL_KEYWORDS or not context.can_expose_tool(name):
                    continue
                if name.startswith("get_fund_") and not has_fund:
                    continue
                if "portfolio" in name and not request.portfolio_id:
                    continue
                self.catalog[name] = tool

        candidates = set(self.catalog).difference(self.trace.initial_tools)
        self.trace.enabled = bool(candidates) and tool_schema_chars([*initial_tools, self.tool]) <= self.max_schema_chars
        self.trace.catalog_size = len(self.catalog)
        self.record_binding(round_index=1)

    @property
    def search_available(self) -> bool:
        return self.trace.enabled and not self.closed and self.search_count < self.max_calls

    def tools(self) -> list[Any]:
        """只有这一份列表进入 bind_tools 和执行映射，搜索不能先于下一轮生效。"""

        return [*self.active_tools, self.tool] if self.search_available else list(self.active_tools)

    def domains(self, initial_domains: tuple[str, ...]) -> tuple[str, ...]:
        """新工具的业务规则也必须进入 Prompt，不能只换 Schema 而遗漏来源与时效规则。"""

        names = {tool.name for tool in self.active_tools}
        added = [domain for domain, members in TOOL_GROUPS.items() if names.intersection(members)]
        return tuple(dict.fromkeys([*initial_domains, *added]))

    def record_binding(self, *, round_index: int) -> None:
        self.trace.bindings.append({
            "round_index": round_index, "tools": [tool.name for tool in self.tools()],
            "schema_chars": tool_schema_chars(self.tools()),
        })

    def finish_search(self) -> None:
        """最后一个工具轮次仍可读数据，但不再发现下一轮才可用的新工具。"""

        self.closed = True

    async def search(self, query: str) -> str:
        """本地检索并登记候选；不访问网络、不启动任务、不执行任何命中的业务工具。"""

        started = perf_counter()
        active_context = current_agent_execution_context()
        if active_context.user_id != self.context.user_id or active_context.mode != self.context.mode:
            raise PermissionError("工具发现请求与当前执行身份不一致")
        if not active_context.can_expose_tool(SEARCH_TOOL_NAME):
            raise PermissionError("当前身份没有工具发现权限")
        if not self.search_available:
            return self._result(query, [], "search_budget_exhausted", started)

        self.search_count += 1
        normalized = re.sub(r"\s+", "", query.lower())
        if normalized in self.seen_queries:
            return self._result(query, [], "duplicate_query", started)
        self.seen_queries.add(normalized)

        ranked = sorted(
            ((self._score(query, tool), name) for name, tool in self.catalog.items()
             if active_context.can_expose_tool(name)),
            key=lambda item: (-item[0], item[1]),
        )
        # 只保留明确命中的能力，不能在零相关时把所有工具当作搜索结果。
        threshold = max(3, ranked[0][0] * 0.5) if ranked else 3
        matches: list[dict[str, Any]] = []
        budget_blocked = False
        for score, name in ranked:
            if score < threshold or len(matches) >= self.top_k:
                break
            tool = self.catalog[name]
            loaded = name in {item.name for item in self.active_tools}
            if not loaded:
                candidate = [*self.active_tools, tool, *([self.tool] if self.search_available else [])]
                if tool_schema_chars(candidate) > self.max_schema_chars:
                    budget_blocked = True
                    continue
                self.active_tools.append(tool)
                self.trace.loaded_tools.append(name)
            matches.append({"name": name, "description": (tool.description or "")[:180],
                            "already_loaded": loaded})

        status = "matched" if matches else ("schema_budget_exhausted" if budget_blocked else "no_matches")
        return self._result(query, matches, status, started)

    def _result(self, query: str, matches: list[dict], status: str, started: float) -> str:
        """记录检索事实，失败也计入预算；搜索结果不是金融证据。"""

        loaded = [item["name"] for item in matches if not item["already_loaded"]]
        self.trace.searches.append({
            "query": query, "status": status, "loaded_tools": loaded,
            "matched_tools": [item["name"] for item in matches],
            "duration_ms": round((perf_counter() - started) * 1000, 2),
        })
        return json.dumps({
            "ok": bool(matches), "data_kind": "tool_discovery", "status": status,
            "matches": matches, "loaded_tools": loaded,
            "remaining_searches": max(0, self.max_calls - self.search_count),
            "hint": ("这里只发现能力，尚未取得业务数据。新增工具下一轮可用，请按其参数定义调用。"
                     if matches else "未加载新能力。不要猜测工具或反复搜索；使用已有证据，说明缺口或向用户澄清。"),
        }, ensure_ascii=False)

    @staticmethod
    def _score(query: str, tool: Any) -> int:
        """优先使用人工审核别名，描述只作辅助，避免“不要用于新闻”等反向描述抢占结果。"""

        text = query.lower()
        name = tool.name.lower()
        if name in text:
            return 100
        keywords = READ_TOOL_KEYWORDS[tool.name]
        score = sum(6 for word in keywords if word in text)
        words = set(re.findall(r"[a-z]{3,}", text)).difference({"get", "tool", "search", "query"})
        score += 3 * len(words.intersection(name.split("_")))
        description = (tool.description or "").lower()
        if 2 <= len(text) <= 24 and text in description:
            score += 2
        return score
