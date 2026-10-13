"""离线聊天验收驱动，不注册到产品 API，也不能在在线服务进程中运行。

真实执行 HTTP 路由、协调器、Loop、工具恢复、模型用量账本和会话保存。
替身只提供固定模型响应、固定业务来源、测试身份及空工作区快照；所有状态放临时库。
这样可以重复注入超时和取消，且不会调用付费模型或修改用户持仓。
"""

from __future__ import annotations

import asyncio
from collections import Counter
from contextlib import ExitStack, suppress
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import httpx
from fastapi import FastAPI
from langchain_core.tools import StructuredTool
from sqlalchemy import select

from app.config import settings
from app.database import Database
from app.models import AgentChatExecutionModel
from app.routers import workspace
from app.schemas import (AgentEvalCaseResult, AgentEvalCheck, AgentEvalRun,
    AgentWorkspaceRuntimeInfo, AgentWorkspaceSnapshot, ComprehensiveAgentChatResponse)
from app.services import governed_model, tool_agent
from app.services.agent_chat_runtime import AgentChatCoordinator, AgentChatExecutionStore
from app.services.agent_evals import AgentEvalService
from app.services.agent_routing import agent_request_router
from app.services.agent_streams import AgentStreamRegistry
from app.services.ai_capacity import MemoryAICapacity
from app.services.ai_usage import AIUsageService
from app.services.auth import CurrentPrincipal, get_current_principal
from app.services.comprehensive_agent import ComprehensiveAgentService
from app.services.conversations import ConversationService
from app.services.fund_agent_tools import FundRiskToolInput
from app.services.investment_context import InvestmentContextService
from app.services.observability import AgentObservabilityService, eval_case_set_id
from app.services.prompt_builder import PromptBuildResult
from app.services.tool_recovery import TOOL_RECOVERY_INSTRUCTION


ROOT = Path(__file__).resolve().parents[1]
SUITE_PATH = ROOT / "data/evals/agent_loop_acceptance_v1.json"
RISK = "get_fund_risk_tool"
REPORT = "get_fund_report_research_tool"


class ScriptedProvider:
    """模拟供应商 HTTP 协议，而不是跳过 ModelRecovery/治理器直接返回 Agent 答案。"""

    def __init__(self, script):
        self.script = script
        self.requests = []
        self.tool_intents = 0
        self.waiting = asyncio.Event()

    async def respond(self, request):
        self.requests.append(json.loads(request.content))
        index = len(self.requests) - 1
        if index >= len(self.script):
            raise AssertionError("模型调用超过固定脚本，可能发生无效重试")
        action = self.script[index]
        if isinstance(action, dict) and action.get("wait"):
            self.waiting.set()
            # 只允许产品的共享时限或取消来结束，不靠返回一个假超时响应掩盖问题。
            await asyncio.Event().wait()
        message = {"role": "assistant", "content": action if isinstance(action, str) else ""}
        if isinstance(action, dict):
            calls = [{"id": f"call_{index}_{i}", "type": "function", "function": {
                "name": REPORT if call.get("report") else RISK,
                "arguments": json.dumps({"fund_code": call["code"], "months": call.get("months", 12)}),
            }} for i, call in enumerate(action.get("calls", []))]
            message["tool_calls"] = calls
            self.tool_intents += len(calls)
        return httpx.Response(200, json={"id": f"offline-{index}", "object": "chat.completion",
            "created": 1, "model": "offline-scripted",
            "choices": [{"index": 0, "message": message,
                "finish_reason": "tool_calls" if message.get("tool_calls") else "stop"}],
            # 固定计数只验证累计/结算，绝不拿它声称真实 Token 优化比例。
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}})


class FixedSources:
    """在工具实际被执行时计数，用来独立核对 recovery_trace，避免自证正确。"""

    def __init__(self, fixtures, fault):
        self.fixtures, self.fault = fixtures, fault
        self.reads = Counter()

    def select(self, names, **kwargs):
        tools = []
        for name in names:
            if name not in {RISK, REPORT}:
                raise AssertionError(f"测试未提供这个来源：{name}")

            async def read(fund_code, months=12, _name=name):
                key = f"report:{fund_code}" if _name == REPORT else fund_code
                self.reads[key] += 1
                if _name == REPORT:
                    return json.dumps({"ok": True, "fund_code": fund_code, "documents": [], "evidence": []})
                if fund_code == "110007":
                    if self.fault == "timeout_always" or (self.fault == "timeout_once" and self.reads[key] == 1):
                        raise httpx.ReadTimeout("离线注入的来源超时")
                    if self.fault == "missing_data":
                        return json.dumps({"ok": False, "fund_code": fund_code,
                            "error_code": "data_unavailable", "error": "净值样本不足"})
                values = self.fixtures[fund_code]
                return json.dumps({"ok": True, "fund_code": fund_code, "period_months": months,
                    "period_start": "2025-09-01" if months == 12 else "2026-03-01",
                    "period_end": "2026-09-01", "annualized_volatility_pct": values["annualized_volatility_pct"],
                    "max_drawdown": {"drawdown_pct": values["drawdown_pct"]},
                    "evidence_id": f"risk:{fund_code}:{months}"})

            tools.append(StructuredTool.from_function(coroutine=read, name=name,
                description="离线固定数据来源", args_schema=FundRiskToolInput))
        return tools


class EmptyWorkspace:
    """侧栏不属于本轮验收，避免读取本机的真实任务、MCP 和长期记忆。"""

    def workspace_snapshot(self, *, user_id, tool_names):
        return AgentWorkspaceSnapshot(user_id=user_id, runtime=AgentWorkspaceRuntimeInfo(
            app_name="Offline acceptance", model="offline-scripted", execution_backend="local",
            database_backend="sqlite", tool_count=len(tool_names), mcp_tool_count=0))


class StreamProbe:
    """内存 ASGI 客户端，边收到 SSE 边解析，能在请求未结束时调用停止接口。

    httpx 的 ASGITransport 会缓冲完整响应，单靠它无法验收“输出还没结束就取消”。
    这里实现 ASGI 的 receive/send 边界，不直接调用 Router 函数或协调器。
    """

    def __init__(self):
        self.status = None
        self.events = []
        self.buffer = ""
        self.stream_id = None
        self.started = asyncio.Event()
        self.cancel_response = None

    async def request(self, app, payload, provider, *, cancel=False):
        body = json.dumps(payload).encode()
        incoming = asyncio.Queue()
        incoming.put_nowait({"type": "http.request", "body": body, "more_body": False})

        async def send(message):
            if message["type"] == "http.response.start":
                self.status = message["status"]
            if message["type"] != "http.response.body":
                return
            self.buffer += message.get("body", b"").decode()
            while "\n\n" in self.buffer:
                frame, self.buffer = self.buffer.split("\n\n", 1)
                fields = dict(line.split(": ", 1) for line in frame.splitlines() if ": " in line)
                if "data" not in fields:
                    continue
                event = (fields.get("event", "message"), json.loads(fields["data"]))
                self.events.append(event)
                if event[0] == "stream_started":
                    self.stream_id = event[1]["stream_id"]
                    self.started.set()

        async def stop():
            await self.started.wait()
            await provider.waiting.wait()
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://offline") as client:
                response = await client.delete(f"/api/v1/agent/chat/stream/{self.stream_id}")
                self.cancel_response = response.json()

        scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"},
            "method": "POST", "scheme": "http", "path": "/api/v1/agent/chat/stream",
            "raw_path": b"/api/v1/agent/chat/stream", "query_string": b"", "root_path": "",
            "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())],
            "server": ("offline", 80), "client": ("127.0.0.1", 12345), "http_version": "1.1"}
        tasks = [asyncio.create_task(app(scope, incoming.get, send))]
        if cancel:
            tasks.append(asyncio.create_task(stop()))
        try:
            # 验收自身的看门时限不是产品超时；到这里意味着生命周期测试失败。
            await asyncio.wait_for(asyncio.gather(*tasks), timeout=15)
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            for task in tasks:
                with suppress(asyncio.CancelledError):
                    await task


def add_check(result, name, passed, detail, dimension="execution"):
    result.checks.append(AgentEvalCheck(name=name, passed=bool(passed), detail=str(detail), dimension=dimension))


def finish_grade(result, expected_outcome):
    """必须先通过内容、协议和持久化检查，才可以称为‘正确处理限制’。"""
    result.passed = all(check.passed for check in result.checks if check.required)
    total = sum(check.weight for check in result.checks)
    result.score = round(sum(check.weight for check in result.checks if check.passed) / total * 100, 1) if total else 0
    result.metric_scores = AgentEvalService._metric_scores(result.checks)
    result.outcome = expected_outcome if result.passed else "system_error"
    return result


async def run_case(suite, scenario, database, fingerprint):
    """一个场景独立用户、独立槽位；使用同一临时数据库保存可核对的最终状态。"""
    started = perf_counter()
    user_id = f"accept-{scenario['id']}"
    fault = scenario.get("fault")
    provider, sources = ScriptedProvider(scenario["script"]), FixedSources(suite["fixtures"], fault)
    capacity, ledger = MemoryAICapacity(), AIUsageService(database)
    conversations, observations = ConversationService(database), AgentObservabilityService(database)
    investment_context = InvestmentContextService()
    service = ComprehensiveAgentService(agent=tool_agent.tool_calling_agent_service,
        conversations=conversations, observability=observations, runtime_state=EmptyWorkspace(),
        investment_context=investment_context)
    coordinator = AgentChatCoordinator(agent=service, store=AgentChatExecutionStore(database))
    registry = AgentStreamRegistry()
    app = FastAPI()
    app.include_router(workspace.router)
    principal = CurrentPrincipal(user_id=user_id, display_name="离线验收", role="user", authenticated=True)
    app.dependency_overrides[get_current_principal] = lambda: principal

    # 复用规则路由；不让语义路由、画像提取、工作区附件额外访问本机状态或收费服务。
    async def route(request, **kwargs):
        return agent_request_router.route(request, execution_context=kwargs.get("execution_context"))

    async with httpx.AsyncClient(transport=httpx.MockTransport(provider.respond)) as provider_client:
        model = governed_model.GovernedChatOpenAI(model="offline-scripted", api_key="offline-placeholder",
            base_url="https://model.invalid/v1", http_async_client=provider_client,
            max_tokens=128, max_retries=0)
        with ExitStack() as stack:
            for name, value in {"agent_task_authorization_enabled": False,
                "agent_tool_discovery_enabled": False, "agent_loop_guard_enabled": True,
                "agent_chat_budget_enabled": True, "agent_chat_timeout_seconds": 2 if fault == "deadline" else 10,
                "agent_chat_finalize_reserve_seconds": .4 if fault == "deadline" else 1,
                "agent_model_max_attempts": 2, "ai_governance_enabled": True,
                "ai_daily_token_limit": 1 if fault == "quota" else 2000000,
                "ai_run_token_limit": 500000, "deepseek_model": "offline-scripted"}.items():
                stack.enter_context(patch.object(settings, name, value))
            for target, value in [
                ("app.routers.workspace.agent_chat_coordinator", coordinator),
                ("app.routers.workspace.conversation_service", conversations),
                ("app.routers.workspace.agent_stream_registry", registry),
            ]:
                stack.enter_context(patch(target, value))
            stack.enter_context(patch("app.services.rate_limits.get_ai_capacity", return_value=capacity))
            stack.enter_context(patch.object(governed_model, "get_ai_capacity", return_value=capacity))
            stack.enter_context(patch.object(governed_model, "get_ai_usage_service", return_value=ledger))
            stack.enter_context(patch.object(tool_agent, "get_chat_model", return_value=model))
            stack.enter_context(patch.object(tool_agent, "select_agent_tools", side_effect=sources.select))
            stack.enter_context(patch.object(agent_request_router, "route_hybrid", side_effect=route))
            stack.enter_context(patch.object(tool_agent.prompt_builder_service, "build",
                return_value=PromptBuildResult("离线验收。" + TOOL_RECOVERY_INSTRUCTION, (), len(TOOL_RECOVERY_INSTRUCTION))))
            stack.enter_context(patch.object(tool_agent.agent_runtime_state_service, "collect_response_fields", return_value={}))
            stack.enter_context(patch.object(investment_context, "_extract", new=AsyncMock(return_value={})))
            # 未声明的外部 HTTP 不能意外访问真实 API。供应商 MockTransport 不经过这些方法。
            stack.enter_context(patch.object(httpx.AsyncHTTPTransport, "handle_async_request",
                side_effect=AssertionError("离线验收禁止外部 HTTP")))
            stack.enter_context(patch.object(httpx.HTTPTransport, "handle_request",
                side_effect=AssertionError("离线验收禁止外部 HTTP")))

            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://offline") as client:
                created = await client.post("/api/v1/conversations", json={"user_id": user_id})
                created.raise_for_status()
                conversation_id = created.json()["id"]
                payload = {"message": scenario["message"], "user_id": "forged-client-user",
                    "conversation_id": conversation_id, "idempotency_key": f"accept:{scenario['id']}", "max_tool_rounds": 10}
                probe = StreamProbe()
                await probe.request(app, payload, provider, cancel=fault == "cancel")
                duration_ms = (perf_counter() - started) * 1000
                details = (await client.get(f"/api/v1/conversations/{conversation_id}")).json()
                result = grade_observation(suite, scenario, probe, sources, provider, details,
                    coordinator, ledger, capacity, database, fingerprint, duration_ms)
                if scenario.get("replay"):
                    before = (len(provider.requests), dict(sources.reads))
                    replay = await client.post("/api/v1/agent/chat", json=payload)
                    after_detail = (await client.get(f"/api/v1/conversations/{conversation_id}")).json()
                    add_check(result, "同键重放不重复执行或保存", replay.status_code == 200
                        and replay.headers.get("Idempotency-Replayed") == "true"
                        and before == (len(provider.requests), dict(sources.reads))
                        and len(after_detail["messages"]) == 2,
                        f"HTTP {replay.status_code}，消息数 {len(after_detail['messages'])}", "conversation")
                # 再次停止必须返回 false，证明结束后注册表不残留任务。
                stopped = await client.delete(f"/api/v1/agent/chat/stream/{probe.stream_id}")
                add_check(result, "流注册已回收", stopped.json().get("ok") is False, stopped.json())
                return finish_grade(result, scenario["expected_outcome"])


def grade_observation(suite, scenario, probe, sources, provider, details, coordinator,
                      ledger, capacity, database, fingerprint, duration_ms):
    """复用既有内容评分，再增加产品入口的协议、账本及资源清理断言。"""
    user_id = f"accept-{scenario['id']}"
    grader = AgentEvalService(observability=AgentObservabilityService(database))
    terminals = [(name, data) for name, data in probe.events if name in {"result", "error", "cancelled"}]
    responses = [ComprehensiveAgentChatResponse.model_validate(data) for name, data in terminals if name == "result"]
    run = responses[0].run if responses else None
    if run is not None:
        result = grader.grade_case(scenario=scenario, run=run, duration_ms=duration_ms,
            data_profile=suite["data_profile"], data_fingerprint=fingerprint)
    else:
        result = AgentEvalCaseResult(case_id=scenario["id"], name=scenario["name"],
            category=scenario["category"], passed=False, score=0, duration_ms=duration_ms,
            stop_reason=terminals[0][0] if terminals else "missing_terminal",
            tool_call_count=provider.tool_intents, data_profile=suite["data_profile"], data_fingerprint=fingerprint)
    expected_terminal = scenario.get("expected_terminal", "result")
    add_check(result, "SSE 只有一个预期终态", [name for name, _ in terminals] == [expected_terminal],
        f"预期 {expected_terminal}，实际 {[name for name, _ in terminals]}")
    add_check(result, "SSE 正常打开与结束", probe.status == 200 and
        sum(name == "done" for name, _ in probe.events) == 1 and probe.events[-1][0] == "done",
        f"HTTP {probe.status}；事件 {len(probe.events)} 条")
    error = next((data for name, data in terminals if name == "error"), {})
    if expected_terminal == "error":
        add_check(result, "控制错误码正确", error.get("code") == scenario["expected_error_code"], error)
    if expected_terminal == "cancelled":
        add_check(result, "停止接口已接受请求", (probe.cancel_response or {}).get("ok"), probe.cancel_response)
    if run:
        deltas = "".join(data["delta"] for name, data in probe.events if name == "answer_delta")
        add_check(result, "流文本与保存答案一致", deltas == run.answer == details["messages"][-1]["content"],
            f"输出字符数 {len(deltas)}", "conversation")
        add_check(result, "服务端身份覆盖伪造用户", run.user_id == user_id and
            details["conversation"]["user_id"] == user_id, f"实际用户 {run.user_id}", "safety")
        if scenario.get("fault") == "deadline":
            metadata = details["messages"][-1]["run_metadata"]
            add_check(result, "超时诊断进入会话", bool(run.execution_budget.get("expired_phase"))
                and metadata.get("execution_budget") == run.execution_budget,
                f"超时阶段 {run.execution_budget.get('expired_phase')}", "conversation")
    else:
        add_check(result, "未泄露半成品回答", not any(name == "answer_delta" for name, _ in probe.events),
            "拒绝/取消时不发送答案增量", "safety")

    expected_messages = 2 if expected_terminal == "result" else 0
    add_check(result, "会话整轮原子保存", len(details["messages"]) == expected_messages,
        f"预期 {expected_messages}，实际 {len(details['messages'])}", "conversation")
    with database.session() as session:
        executions = list(session.scalars(select(AgentChatExecutionModel).where(AgentChatExecutionModel.user_id == user_id)))
        states = [entry.status for entry in executions]
    expected_state = "completed" if expected_terminal == "result" else "failed"
    add_check(result, "幂等执行状态收尾", states == [expected_state], states)
    add_check(result, "会话锁已释放", not coordinator.locks._entries, list(coordinator.locks._entries))
    add_check(result, "来源执行次数符合契约", dict(sources.reads) == scenario["expected_reads"],
        f"预期 {scenario['expected_reads']}，实际 {dict(sources.reads)}", "efficiency")
    add_check(result, "模型工具意图次数符合契约", provider.tool_intents == scenario["expected_calls"],
        f"预期 {scenario['expected_calls']}，实际 {provider.tool_intents}", "efficiency")
    if run:
        add_check(result, "工具轨迹与来源计数一致", result.physical_tool_attempts == sum(sources.reads.values())
            and result.tool_call_count == provider.tool_intents,
            f"轨迹 {result.physical_tool_attempts}，来源 {sum(sources.reads.values())}", "efficiency")

    usage = ledger.summary(user_id=user_id)
    statuses = Counter(item["status"] for item in usage["calls"])
    expected_unknown = int(scenario.get("fault") in {"deadline", "cancel"})
    expected_settled = len(provider.requests) - expected_unknown
    add_check(result, "费用账本无悬挂预留", usage["reserved_tokens"] == 0
        and statuses == Counter(settled=expected_settled, unknown=expected_unknown)
        and bool(usage["uncertain_tokens"]) == bool(expected_unknown)
        and usage["used_tokens"] == expected_settled * 15,
        f"调用状态 {dict(statuses)}，预留 {usage['reserved_tokens']}，待确认 {usage['uncertain_tokens']}")
    add_check(result, "模型请求未额外重试", len(provider.requests) == len(scenario["script"]),
        f"预期 {len(scenario['script'])}，实际 {len(provider.requests)}", "efficiency")
    snapshot = capacity.snapshot(user_id)
    add_check(result, "执行槽位和排队票已释放", snapshot["running"] == 0 and snapshot["waiting"] == 0, snapshot)
    if run:
        add_check(result, "已知用量与账本一致", run.usage.total_tokens == usage["used_tokens"],
            f"回答 {run.usage.total_tokens}，账本 {usage['used_tokens']}")
    else:
        result.usage.total_tokens = usage["used_tokens"]
        result.usage.input_tokens = sum(item.get("input_tokens") or 0 for item in usage["calls"])
        result.usage.output_tokens = sum(item.get("output_tokens") or 0 for item in usage["calls"])
    result.physical_tool_attempts = sum(sources.reads.values())
    result.execution_diagnostics = {"mode": "offline_scripted", "usage_origin": "synthetic_provider",
        "transport": "in_memory_asgi", "model_requests": len(provider.requests),
        "source_attempts": dict(sources.reads), "ledger_statuses": dict(statuses),
        "uncertain_tokens": usage["uncertain_tokens"], "event_types": [name for name, _ in probe.events],
        "terminal_error": error, "execution_budget": run.execution_budget if run else {},
        "tool_traces": [{"tool": call.tool_name, "args": call.tool_args,
            "is_error": call.is_error, "recovery_trace": call.recovery_trace} for call in run.tool_calls] if run else []}
    return result


async def run_acceptance_suite(*, case_ids=None, progress_callback=None):
    """返回原有 AgentEvalRun 格式；数据库仅用于验收，结果另由 CLI 导出。"""
    raw = SUITE_PATH.read_bytes()
    suite = json.loads(raw)
    scenarios = suite["scenarios"]
    if case_ids:
        unknown = set(case_ids) - {row["id"] for row in scenarios}
        if unknown:
            raise ValueError(f"未知验收场景：{sorted(unknown)}")
        scenarios = [row for row in scenarios if row["id"] in case_ids]
    fingerprint = hashlib.sha256(raw).hexdigest()
    started = perf_counter()
    results = []
    with TemporaryDirectory(prefix="fundscope-loop-") as directory:
        database = Database(f"sqlite:///{Path(directory) / 'acceptance.db'}")
        try:
            for scenario in scenarios:
                try:
                    result = await run_case(suite, scenario, database, fingerprint)
                except Exception as exc:
                    # 看门超时、脚本不匹配等必须显示失败，并继续完成其他场景，不能静默跳过。
                    result = AgentEvalCaseResult(case_id=scenario["id"], name=scenario["name"], passed=False,
                        score=0, duration_ms=0, stop_reason="acceptance_error", outcome="system_error",
                        data_profile=suite["data_profile"], data_fingerprint=fingerprint,
                        error=f"{type(exc).__name__}: {exc}", checks=[AgentEvalCheck(
                            name="验收可执行", passed=False, detail=f"{type(exc).__name__}: {exc}", dimension="execution")])
                results.append(result)
                if progress_callback:
                    progress_callback(result)
            passed = sum(row.passed for row in results)
            ids = [row.case_id for row in results]
            report = AgentEvalRun(id=f"loop_accept_{uuid4().hex[:12]}", user_id="offline-acceptance",
                suite_id=suite["id"], suite_name=suite["name"], status="completed" if passed == len(results) else "failed",
                total_cases=len(results), passed_cases=passed, failed_cases=len(results) - passed,
                score=round(sum(row.score for row in results) / len(results), 1),
                duration_ms=round((perf_counter() - started) * 1000, 2),
                total_tokens=sum(row.usage.total_tokens for row in results),
                total_tool_calls=sum(row.tool_call_count for row in results), case_ids=ids,
                case_set_id=eval_case_set_id(ids), data_profile=suite["data_profile"], data_fingerprint=fingerprint,
                results=results, created_at=datetime.now(timezone.utc).isoformat())
            observations = AgentObservabilityService(database)
            observations.save_eval_run(report)
            # 用原有读回接口返回报告，顺便验证新增字段能跨序列化保存，而非只活在内存。
            return observations.recent_eval_runs(user_id=report.user_id, suite_id=report.suite_id, limit=1)[0]
        finally:
            database.engine.dispose()
