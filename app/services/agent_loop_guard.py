"""一次聊天内的进展检查与有限完成清单，不另开模型、不代替业务事实校验。

缓存只复用本次请求已经授权读取的结果；进展判断看实际工具回执而不是模型自述。
完成清单只覆盖明确场景，未识别的问题标记 not_applicable，不能冒充通用答案评分。
"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
from hashlib import sha256
import json
import math
import re

from app.schemas import AgentLoopControl


# 有副作用、实时状态、模型分析、可补查来源的工具不缓存。不能直接复用并行白名单。
CACHEABLE_READS = frozenset({
    "get_fund_profile_tool", "get_fund_nav_summary_tool", "get_fund_risk_tool",
    "get_portfolio_assessment_tool", "get_portfolio_performance_tool",
    "get_portfolio_exposure_tool",
})
MONITORED_READS = CACHEABLE_READS | frozenset({
    "get_fund_holdings_tool", "get_fund_assessment_tool", "get_fund_report_research_tool",
    "get_fund_research_brief_tool", "get_portfolio_research_brief_tool",
    "search_market_events_tool", "get_market_sentiment_tool", "get_market_hotspots_tool",
    "get_large_capital_signals_tool", "get_retail_indices_tool", "rag_search_tool",
    "calculator_tool", "search_tools_tool",
})
# 这些业务已有专用完成协议，第一轮不重复解释其状态，也不干预后台 Job 的轮询。
DELEGATED_TOOLS = frozenset({
    "get_fund_research_brief_tool", "get_portfolio_research_brief_tool",
    "start_portfolio_research_tool", "get_portfolio_research_status_tool",
    "run_portfolio_stress_tool", "get_portfolio_stress_tool",
    "draft_portfolio_allocation_tool", "get_portfolio_allocation_tool",
})
_current_guard: ContextVar[LoopGuard | None] = ContextVar("agent_loop_guard", default=None)


def current_loop_guard():
    return _current_guard.get()


@contextmanager
def loop_guard_scope(guard):
    """每次主/子 Agent 调用都新建状态，退出后恢复父作用域，不跨用户共享缓存。"""
    token = _current_guard.set(guard)
    try:
        yield guard
    finally:
        _current_guard.reset(token)


def object_payload(content):
    try:
        value = json.loads(content)
        return value if isinstance(value, dict) else None
    except (ValueError, TypeError):
        return None


def successful(payload):
    # 工具可能把业务失败作为正常返回值，不能只检查 Python 是否抛异常。
    return bool(payload) and payload.get("ok") is not False and not payload.get("error")


def _stable(value):
    """只剔除观测噪声；数据日期、文档版本、数值、错误与原文都参与比较。"""
    if isinstance(value, dict):
        return {k: _stable(v) for k, v in value.items()
                if k not in {"request_id", "duration_ms", "elapsed_ms", "retrieved_at", "generated_at"}}
    if isinstance(value, list):
        return [_stable(v) for v in value]
    return value


def _digest(value):
    return sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _number(value):
    # 0 是有效指标；True、NaN、错误提示等不能作为已取得的数字。
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _drawdown(payload):
    value = payload.get("max_drawdown")
    return isinstance(value, dict) and _number(value.get("drawdown_pct"))


class LoopGuard:
    """记录缓存、无进展轮次和最小取证要求；实例生命周期等于一次 chat。"""

    def __init__(self, request, *, enabled=True):
        self.request = request
        self.trace = AgentLoopControl(enabled=enabled)
        self.cache = {}
        self.locks = {}
        self.seen_results = set()
        self.stalled_rounds = 0
        self.required = []
        self.boundary_issue = ""
        self.corrected = False

    def configure(self, route):
        """显式代码比较与组合解释可由规则核验；不从模糊名称猜测基金身份。"""
        if not self.trace.enabled or route.name in {"direct", "clarify", "access_denied"}:
            return
        if DELEGATED_TOOLS.intersection(route.tool_names):
            return
        text = self.request.message
        codes = list(dict.fromkeys(re.findall(r"(?<!\d)\d{6}(?!\d)", text)))
        risk = any(word in text for word in ("风险", "回撤", "波动"))
        strategy = any(word in text for word in ("策略", "经理观点", "季报"))
        if any(word in text for word in ("对比", "比较")) and 2 <= len(codes) <= 4:
            for code in codes:
                if risk:
                    self.required.append(("fund_risk", code, f"{code} 的历史风险指标"))
                if strategy:
                    self.required.append(("fund_strategy", code, f"{code} 两个不同季度的经理原文"))
            if self.required and (self.request.fund_code or self.request.fund_report_ids):
                self.boundary_issue = "当前页面绑定单只基金，请先取消单基金选择，再提交明确代码的比较问题。"
        elif self.request.portfolio_id and risk and any(word in text for word in ("收益", "业绩")):
            if any(word in text for word in ("解释", "分析", "怎么看")):
                self.required = [
                    ("portfolio_performance", self.request.portfolio_id, "当前组合的历史收益指标"),
                    ("portfolio_risk", self.request.portfolio_id, "当前组合的风险或集中度依据"),
                ]
        self.trace.required = [row[2] for row in self.required]
        self.trace.completion_status = "insufficient_evidence" if self.required else "not_applicable"

    def instruction(self):
        if not self.required:
            return ""
        return ("本轮最低取证清单：" + "；".join(self.trace.required)
                + "。只使用当前获准工具；工具返回成功不代表已覆盖全部对象。"
                "资料缺失必须明确说明，不得凭模型记忆补造指标或原文。" + self.boundary_issue)

    async def read(self, name, args, selected_tool, context, invoke):
        """在执行器完成权限检查后调用；锁合并同轮重复读取，仍逐个回填 ToolMessage。"""
        if not self.trace.enabled or name not in CACHEABLE_READS or self.request.research_refresh:
            # 非缓存工具可能改变持仓/任务；宁可清空，也不沿用写入前的读取结果。
            if name not in MONITORED_READS:
                self.cache.clear()
            return await invoke(), False
        if context.user_id != self.request.user_id:
            raise PermissionError("工具缓存不能跨用户复用")
        schema = selected_tool.args_schema
        if not isinstance(schema, type) or not hasattr(schema, "model_validate"):
            return await invoke(), False
        # 模型省略默认月份和显式传 12 应视为同一查询。非法参数仍交给原工具校验。
        try:
            normalized = {**args, **schema.model_validate(args).model_dump(mode="json")}
        except ValueError:
            return await invoke(), False
        key = _digest([context.user_id, context.task_grant_id, context.task_run_id,
                       context.task_agent_id, name, normalized])
        # 有界缓存，不把整段工具原文写进观测记录或长期历史。
        if key not in self.locks and len(self.locks) >= 32:
            return await invoke(), False
        async with self.locks.setdefault(key, asyncio.Lock()):
            if key in self.cache:
                self.trace.cache_hits += 1
                return self.cache[key], True
            result = await invoke()
            if isinstance(result, str) and len(result) <= 64_000 and successful(object_payload(result)):
                self.cache[key] = result
            return result, False

    def observe_round(self, calls, round_index):
        """成功新结果算进展；重复成功或失败不会因更换 tool_call_id 变成新证据。"""
        if not self.trace.enabled:
            return None
        if any(call.tool_name not in MONITORED_READS for call in calls):
            self.stalled_rounds = 0
            return None
        progress = False
        for call in calls:
            payload = object_payload(call.tool_result)
            if call.is_error or not successful(payload):
                continue
            # 发现工具不算金融证据，但真正加载新能力是一次有效的流程推进。
            value = payload.get("loaded_tools", []) if call.tool_name == "search_tools_tool" else _stable(payload)
            if not value:
                continue
            fingerprint = _digest([call.tool_name, value])
            if fingerprint not in self.seen_results:
                progress = True
                self.seen_results.add(fingerprint)
        self.stalled_rounds = 0 if progress else self.stalled_rounds + 1
        action = "stop" if self.stalled_rounds >= 3 else "redirect" if self.stalled_rounds == 2 else "continue"
        self.trace.rounds.append({"round_index": round_index, "new_result": progress,
                                  "stalled_rounds": self.stalled_rounds, "action": action})
        return action

    def evaluate(self, messages, calls):
        """只检查当前仍可见的 ToolMessage，不能用已压缩掉的审计原文补齐清单。"""
        if not self.required:
            return []
        tool_messages = [m for m in messages if getattr(m, "type", "") == "tool"]
        covered = set()
        risk_windows = {}
        for message, call in zip(tool_messages, calls):
            payload = object_payload(message.content)
            if call.is_error or not successful(payload):
                continue
            for kind, target, label in self.required:
                if self._covers(kind, target, call.tool_name, payload):
                    covered.add(label)
                    if kind == "fund_risk":
                        risk_windows[target] = (payload.get("period_months"), payload.get("period_start"), payload.get("period_end"))
        self.trace.covered = [label for label in self.trace.required if label in covered]
        self.trace.missing = [label for label in self.trace.required if label not in covered]
        if len(risk_windows) > 1 and len(set(risk_windows.values())) > 1:
            self.trace.missing.append("可比的风险统计区间（当前各基金区间不一致）")
        self.trace.completion_status = ("needs_clarification" if self.boundary_issue
            else "insufficient_evidence" if self.trace.missing else "evidence_ready")
        return self.trace.missing

    @staticmethod
    def _covers(kind, target, name, payload):
        """按现有工具协议检查身份和实质字段；ok:true、空字典、错误提示不算证据。"""
        if kind == "fund_risk":
            return (name == "get_fund_risk_tool" and payload.get("fund_code") == target
                and _number(payload.get("annualized_volatility_pct")) and _drawdown(payload)
                and isinstance(payload.get("period_start"), str) and bool(payload["period_start"])
                and isinstance(payload.get("period_end"), str) and bool(payload["period_end"])
                and _number(payload.get("period_months")))
        if kind == "fund_strategy":
            if name != "get_fund_report_research_tool" or payload.get("fund_code") != target:
                return False
            documents, evidence = payload.get("documents"), payload.get("evidence")
            if not isinstance(documents, list) or not isinstance(evidence, list):
                return False
            documents = {d["id"]: d for d in documents if isinstance(d, dict)
                and d.get("fund_code") == target and isinstance(d.get("id"), str)}
            periods = set()
            for row in evidence:
                if not isinstance(row, dict) or not isinstance(row.get("document_id"), str):
                    continue
                doc = documents.get(row["document_id"], {})
                if (row.get("section") == "manager_review" and isinstance(row.get("text"), str)
                    and row["text"].strip() and isinstance(row.get("evidence_id"), str)
                    and row["evidence_id"] and _number(row.get("page")) and row["page"] >= 1
                    and isinstance(doc.get("period"), str) and doc["period"]):
                    periods.add(doc["period"])
            return len(periods) >= 2
        if payload.get("portfolio_id") != target:
            return False
        if kind == "portfolio_performance":
            returns = payload.get("returns") or {}
            return (name == "get_portfolio_performance_tool" and bool(payload.get("period_end"))
                    and isinstance(returns, dict)
                    and any(_number(returns.get(k)) for k in ("twr_pct", "xirr_pct", "annualized_return_pct")))
        concentration = payload.get("concentration")
        return (name == "get_portfolio_assessment_tool" and isinstance(concentration, dict)
                and _number(concentration.get("max_weight_pct"))) or (
            name == "get_portfolio_performance_tool" and _drawdown(payload))

    def request_correction(self, messages, calls, *, can_continue):
        missing = self.evaluate(messages, calls)
        if missing and can_continue and not self.corrected and not self.boundary_issue:
            self.corrected = True
            self.trace.completion_retries = 1
            return "当前回答还没有满足取证清单，仍缺：" + "；".join(missing) + "。请只补查缺项；无法取得就明确说明，不要重复已成功查询。"
        return None

    def finalize(self, answer, messages, calls, *, reason):
        """不合格草稿不展示为完整比较；输出程序确定的覆盖范围，不替模型编造结论。"""
        if not self.trace.enabled:
            return answer, reason
        self.evaluate(messages, calls)
        if self.required and (self.trace.missing or self.boundary_issue):
            status = self.trace.completion_status
            answer = "本轮尚不能给出完整结论。"
            if self.trace.covered:
                answer += "\n\n已取得：" + "；".join(self.trace.covered) + "。"
            if self.trace.missing:
                answer += "\n\n仍缺少：" + "；".join(self.trace.missing) + "。这不代表相关事实不存在。"
            answer += "\n\n" + (self.boundary_issue or "本轮已停止补查，请补齐资料或缩小比较范围后再分析。")
            reason = status
        elif reason == "no_progress":
            answer = "本轮连续查询未取得新增信息，已停止重复调用。以下仅依据已取得的资料：\n\n" + answer
        self.trace.stop_reason = reason
        return answer, reason
