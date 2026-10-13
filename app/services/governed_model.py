"""给现有 ChatOpenAI 加资源治理，保留 LangChain 的工具、LCEL 和结构化输出接口。

控制放在原始生成层：先拿执行槽位、预留 Token，再发送请求；解析结构化结果之前
就结算原始 usage。因此 JSON 解析失败不会把已经付费的调用误当成零消耗。
"""

import asyncio
from contextlib import asynccontextmanager, contextmanager
import json
import logging
from threading import Event, Thread
from time import monotonic, sleep
from uuid import uuid4

from langchain_openai import ChatOpenAI

from app.config import settings
from app.services.ai_capacity import get_ai_capacity
from app.services.ai_governance_context import AIControlError, current_ai_scope
from app.services.ai_usage import get_ai_usage_service
from app.services.tracing import trace_span
from app.services.chat_execution_budget import check_chat_active


logger = logging.getLogger(__name__)


def extract_provider_usage(value):
    """只接受完整且非负的供应商计数；缺字段不是 0。"""
    raw = getattr(value, "usage_metadata", None)
    if raw is None:
        raw = (getattr(value, "llm_output", None) or {}).get("token_usage")
    if raw is None and getattr(value, "generations", None):
        return extract_provider_usage(value.generations[0].message)
    if raw is None:
        raw = (getattr(value, "response_metadata", None) or {}).get("token_usage")
    if not isinstance(raw, dict):
        return None
    items = {"input_tokens": raw.get("input_tokens", raw.get("prompt_tokens")),
             "output_tokens": raw.get("output_tokens", raw.get("completion_tokens")),
             "total_tokens": raw.get("total_tokens")}
    if any(type(v) is not int or v < 0 for v in items.values()):
        return None
    return items if items["total_tokens"] >= items["input_tokens"] + items["output_tokens"] else None


async def _critical(function, *args, **kwargs):
    """数据库/Redis 不阻塞事件循环；取消时先等短事务完成，再由 finally 做补偿。

    单纯取消 to_thread 不会终止线程。若不等待，可能出现“刚退款，预留线程又写入”的竞态。
    """
    task = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        await task
        raise


class ModelPermit:
    """一次物理模型请求的票据。重试和备用供应商都必须重新取票、重新记账。"""

    def __init__(self, *, provider, model, payload, timeout, attempt=1):
        self.scope = current_ai_scope()
        self.capacity = get_ai_capacity()
        self.ledger = get_ai_usage_service()
        self.id = uuid4().hex
        self.provider, self.model = provider, model
        self.attempt = attempt
        # UTF-8 字节数加协议余量作保守预留，包含工具/schema；不冒充准确 tokenizer。
        # 文本 ChatModel 和显式 governed 的 Embedding 共用账本；后者 max_tokens=0。
        def json_default(value):
            # 原生 JSON Schema 可能是 Pydantic 类，不能只数类名而漏掉整份 schema。
            return value.model_json_schema() if isinstance(value, type) and hasattr(value, "model_json_schema") else str(value)
        self.tokens = len(json.dumps(payload, ensure_ascii=False, default=json_default).encode("utf-8")) + 1024
        self.tokens += int(payload.get("max_completion_tokens", payload.get("max_tokens", settings.ai_max_output_tokens)))
        self.ttl = max(300, float(timeout or 90) + settings.ai_slot_lease_seconds * 2)
        self.usage = None
        self.reason = None
        self.stop = Event()
        self.lost = Event()
        self.heartbeat = None

    @contextmanager
    def generation_trace(self):
        """只观察真正发送给供应商的阶段；未知用量留空，不能伪装成免费调用。"""
        with trace_span("ai.generate", attributes={
            "langfuse.observation.type": "generation",
            "gen_ai.provider.name": self.provider,
            "gen_ai.request.model": self.model,
            "ai.call_id": self.id,
            "ai.attempt": self.attempt,
        }) as span:
            try:
                yield
            finally:
                span.set_attribute("ai.usage_known", self.usage is not None)
                if self.usage is not None:
                    span.set_attribute("gen_ai.usage.input_tokens", self.usage["input_tokens"])
                    span.set_attribute("gen_ai.usage.output_tokens", self.usage["output_tokens"])
                    span.set_attribute("langfuse.observation.usage_details", json.dumps(self.usage))

    def check(self):
        check_chat_active()
        # 包括路由、主 Agent、研究角色和重试在内，每次真正请求供应商前检查任务。
        # 此时已取到的上下文不是永久授权，用户可以在排队期间撤销。
        from app.services.agent_authorization import check_active_task_authorization
        check_active_task_authorization()
        if self.scope.check_cancelled:
            self.scope.check_cancelled()
        if self.lost.is_set():
            raise AIControlError("ai_lease_lost", "AI 执行槽位续租失败，请稍后重试", status_code=503)

    def rate(self):
        self.check()
        allowed, _, retry = self.capacity.consume_rate(
            f"model:{self.scope.user_id}", settings.ai_model_requests_per_minute)
        if not allowed:
            raise AIControlError("model_rate_limited", "当前账号的模型调用过于频繁，请稍后再试", retry_after=retry)

    def poll(self, deadline):
        self.check()
        result = self.capacity.acquire(self.scope.user_id, self.id)
        if result == "full":
            raise AIControlError("ai_queue_full", "AI 等待队列已满，请稍后再试")
        if result != "acquired" and monotonic() >= deadline:
            raise AIControlError("ai_queue_timeout", "等待 AI 执行资源超时，请稍后再试")
        return result == "acquired"

    def reserve(self):
        self.ledger.reserve(call_id=self.id, scope=self.scope, provider=self.provider,
            model=self.model, tokens=self.tokens, ttl_seconds=self.ttl)

    def start(self):
        self.check()
        self.ledger.mark_started(user_id=self.scope.user_id, call_id=self.id)
        self.heartbeat = Thread(target=self._renew, name=f"ai-lease-{self.id[:8]}", daemon=True)
        self.heartbeat.start()

    def _renew(self):
        # 槽位只在真实调用返回/取消后释放。同步线程仍在联网时，不能提前释放。
        while not self.stop.wait(max(0.1, settings.ai_slot_lease_seconds / 3)):
            try:
                if (not self.capacity.renew(self.scope.user_id, self.id)
                        or not self.ledger.renew(user_id=self.scope.user_id, call_id=self.id, ttl_seconds=self.ttl)):
                    self.lost.set()
                    return
            except AIControlError:
                self.lost.set()
                return

    def finish(self):
        # 先停并等待续租线程，再删票，防止旧线程把已经释放的票续活。
        self.stop.set()
        if self.heartbeat:
            self.heartbeat.join(timeout=2)
        try:
            self.ledger.settle(user_id=self.scope.user_id, call_id=self.id,
                usage=self.usage, reason=self.reason or (None if self.usage else "usage_missing"))
        except LookupError:
            # 被限流/队列拒绝的请求没有预留记录，也从未发送给模型。
            pass
        finally:
            try:
                self.capacity.release(self.scope.user_id, self.id)
            except AIControlError:
                # Redis 临时断线时槽位由 TTL 回收；SQL 结算不能被清理异常覆盖。
                logger.warning("AI slot cleanup deferred to lease expiry", extra={"call_id": self.id})


@contextmanager
def model_permit(**kwargs):
    permit = ModelPermit(**kwargs)
    with trace_span("ai.call", attributes={"ai.call_id": permit.id, "ai.attempt": permit.attempt}):
        try:
            with trace_span("ai.admission"):
                permit.rate()
                deadline = monotonic() + settings.ai_queue_wait_seconds
                while not permit.poll(deadline):
                    sleep(0.1)
                permit.reserve()
                permit.start()
            with permit.generation_trace():
                yield permit
        except BaseException as exc:
            permit.reason = type(exc).__name__
            raise
        finally:
            # 观测与 SQL 费用账本分开，结算始终执行原有逻辑。
            with trace_span("ai.settle"):
                permit.finish()


@asynccontextmanager
async def async_model_permit(**kwargs):
    permit = ModelPermit(**kwargs)
    with trace_span("ai.call", attributes={"ai.call_id": permit.id, "ai.attempt": permit.attempt}):
        try:
            with trace_span("ai.admission"):
                await _critical(permit.rate)
                deadline = monotonic() + settings.ai_queue_wait_seconds
                while not await _critical(permit.poll, deadline):
                    await asyncio.sleep(0.1)
                await _critical(permit.reserve)
                await _critical(permit.start)
            with permit.generation_trace():
                yield permit
        except BaseException as exc:
            permit.reason = type(exc).__name__
            raise
        finally:
            with trace_span("ai.settle"):
                await _critical(permit.finish)


def retryable(exc):
    # 沿用 SDK 对暂时性供应商错误的分类；额度拒绝绝不重试。
    from openai import APIConnectionError, APIStatusError
    return isinstance(exc, APIConnectionError) or (
        isinstance(exc, APIStatusError) and (exc.status_code in {408, 409, 429} or exc.status_code >= 500))


class GovernedChatOpenAI(ChatOpenAI):
    """只扩展生成边界，不重写 LangChain 的 Tool Calling 或解析器。"""

    governance_provider: str = "primary"
    governance_retries: int = 0
    max_retries: int | None = 0

    def _get_request_payload(self, input_, *, stop=None, **kwargs):
        payload = super()._get_request_payload(input_, stop=stop, **kwargs)
        # DeepSeek 的 OpenAI 兼容接口使用 max_tokens；其余供应商保留 SDK 的原协议。
        if self.governance_provider.startswith("deepseek") and "max_completion_tokens" in payload:
            payload["max_tokens"] = payload.pop("max_completion_tokens")
        return payload

    def _options(self, messages, stop, kwargs):
        options = dict(kwargs)
        # 每次调用都封顶，bind(max_tokens=...) 也不能扩大服务端上限。
        requested = options.pop("max_tokens", options.get("max_completion_tokens", self.max_tokens))
        cap = settings.ai_max_output_tokens
        if cap <= 0:
            raise AIControlError("invalid_output_limit", "AI 输出额度配置无效", status_code=503)
        if requested is not None and int(requested) <= 0:
            raise AIControlError("invalid_output_limit", "AI 输出额度必须大于零", status_code=503)
        options["max_completion_tokens"] = min(int(requested or cap), cap)
        payload = self._get_request_payload(messages, stop=stop, **options)
        timeout = self.request_timeout
        if hasattr(timeout, "read"):
            timeout = timeout.read
        if isinstance(timeout, tuple):
            timeout = max(timeout)
        return options, dict(provider=self.governance_provider, model=self.model_name,
            payload=payload, timeout=timeout)

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        options, params = self._options(messages, stop, kwargs)
        for attempt in range(self.governance_retries + 1):
            try:
                with model_permit(**params, attempt=attempt + 1) as permit:
                    result = super()._generate(messages, stop=stop, run_manager=run_manager, **options)
                    permit.usage = extract_provider_usage(result)
                    permit.check()
                    return result
            except Exception as exc:
                if attempt >= self.governance_retries or not retryable(exc):
                    raise
                sleep(min(2 ** attempt, 2))

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        options, params = self._options(messages, stop, kwargs)
        for attempt in range(self.governance_retries + 1):
            try:
                async with async_model_permit(**params, attempt=attempt + 1) as permit:
                    result = await super()._agenerate(messages, stop=stop, run_manager=run_manager, **options)
                    permit.usage = extract_provider_usage(result)
                    permit.check()
                    return result
            except Exception as exc:
                if attempt >= self.governance_retries or not retryable(exc):
                    raise
                await asyncio.sleep(min(2 ** attempt, 2))

    def _stream(self, messages, stop=None, run_manager=None, **kwargs):
        options, params = self._options(messages, stop, {**kwargs, "stream_options": {"include_usage": True}})
        # 流一旦输出就不自动重放，以免用户收到重复内容；断流没有最终 usage 则待确认。
        with model_permit(**params) as permit:
            for chunk in super()._stream(messages, stop=stop, run_manager=run_manager, **options):
                permit.usage = extract_provider_usage(chunk.message) or permit.usage
                permit.check()
                yield chunk

    async def _astream(self, messages, stop=None, run_manager=None, **kwargs):
        options, params = self._options(messages, stop, {**kwargs, "stream_options": {"include_usage": True}})
        async with async_model_permit(**params) as permit:
            async for chunk in super()._astream(messages, stop=stop, run_manager=run_manager, **options):
                permit.usage = extract_provider_usage(chunk.message) or permit.usage
                permit.check()
                yield chunk
