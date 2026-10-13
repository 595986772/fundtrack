"""跨请求、线程和 Worker 的 OpenTelemetry 追踪，不保存模型或持仓正文。

Span 是一个计时步骤，Trace 是这些步骤共同的编号。队列只传 Job ID，所以父步骤
必须随 Job 持久化，不能依赖 HTTP 进程内的变量。观测失败不改变业务或费用结算。
"""

from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
import base64
import inspect
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
from threading import Lock
from urllib.parse import urlsplit
from uuid import uuid4

from opentelemetry import trace
from opentelemetry.context import Context
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

from app.config import settings

logger = logging.getLogger(__name__)
_provider = None
_pid = None
_lock = Lock()
_metadata = ContextVar("trace_metadata", default={})
_propagator = TraceContextTextMapPropagator()
# 只有这些服务端业务编号可跨任务传播；绝不携带任意 baggage、用户问题或凭据。
_CARRIER_KEYS = {"job.id", "job.type", "research.mode", "research.graph_version", "prompt.version"}


def initialize_tracing():
    """每个进程拥有独立的有界导出队列，避免复用 fork 前的导出线程。"""
    global _provider, _pid, _lock
    if not settings.trace_enabled:
        return None
    if _pid != os.getpid():
        _provider, _pid, _lock = None, os.getpid(), Lock()
    with _lock:
        if _provider is not None:
            return _provider
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter, SpanExportResult
        from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased

        provider = TracerProvider(
            resource=Resource.create({"service.name": settings.trace_service_name,
                "service.version": settings.trace_release, "process.pid": os.getpid(),
                "deployment.environment.name": settings.deployment_mode}),
            sampler=ParentBased(TraceIdRatioBased(max(0.0, min(1.0, settings.trace_sample_rate)))),
        )

        class LocalExporter(SpanExporter):
            """每个进程单独轮转文件，避免 API 和多个 Worker 争写同一个文件。"""
            def __init__(self):
                directory = Path(settings.trace_local_dir)
                directory.mkdir(parents=True, exist_ok=True)
                # 容器里 PID 可能都为 1；启动标识避免共享卷上的文件相互覆盖。
                self.handler = RotatingFileHandler(directory / f"spans-{os.getpid()}-{uuid4().hex[:12]}.jsonl",
                    maxBytes=5 * 1024 * 1024, backupCount=2, encoding="utf-8")
                self.handler.setFormatter(logging.Formatter("%(message)s"))

            def export(self, spans):
                try:
                    for span in spans:
                        row = json.loads(span.to_json())
                        self.handler.emit(logging.LogRecord("trace", logging.INFO, "", 0,
                            json.dumps(row, ensure_ascii=False), (), None))
                    return SpanExportResult.SUCCESS
                except Exception:
                    return SpanExportResult.FAILURE

            def shutdown(self):
                self.handler.close()

        def add(exporter):
            # 写文件和上传均不在请求线程执行；满队列可以丢观测，不能堵住分析任务。
            provider.add_span_processor(BatchSpanProcessor(exporter, max_queue_size=2048,
                max_export_batch_size=128, schedule_delay_millis=1000))

        if settings.trace_local_enabled:
            try:
                add(LocalExporter())
            except Exception as exc:
                logger.warning("Local tracing unavailable: %s", type(exc).__name__)
        try:
            endpoint, headers = _remote_export_options()
            if endpoint:
                from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
                add(OTLPSpanExporter(endpoint=endpoint, headers=headers, timeout=3))
        except Exception as exc:
            # 只记异常类型，SDK 错误正文可能包含 URL 或认证信息。
            logger.warning("Remote tracing unavailable: %s", type(exc).__name__)
        _provider = provider
        return provider


def _remote_export_options():
    """直连标准 OTLP 或 Langfuse 的 HTTP/protobuf 入口，不需要第二套 SDK。"""
    endpoint = settings.trace_otlp_endpoint
    headers = {}
    if not endpoint and settings.langfuse_base_url:
        if not (settings.langfuse_public_key and settings.langfuse_secret_key):
            raise ValueError("Langfuse credentials missing")
        endpoint = settings.langfuse_base_url.rstrip("/") + "/api/public/otel/v1/traces"
        credential = base64.b64encode(
            f"{settings.langfuse_public_key}:{settings.langfuse_secret_key}".encode()).decode()
        headers = {"Authorization": f"Basic {credential}", "x-langfuse-ingestion-version": "4"}
    if endpoint:
        url = urlsplit(endpoint)
        if url.scheme not in {"http", "https"} or not url.hostname or url.username or url.query:
            raise ValueError("Invalid trace endpoint")
    return endpoint, headers


def shutdown_tracing():
    """正常退出时尽量送完当前进程的观测；强杀进程仍可能丢失尚未导出的 Span。"""
    global _provider
    provider, _provider = _provider, None
    if provider is not None and _pid == os.getpid():
        provider.shutdown()


def flush_tracing():
    return _provider.force_flush(timeout_millis=5000) if _provider is not None else True


def current_trace_id():
    ctx = trace.get_current_span().get_span_context()
    return f"{ctx.trace_id:032x}" if settings.trace_enabled and ctx.is_valid else None


def capture_trace_context():
    """只生成 W3C traceparent 和受限元数据，可安全保存到内部 Job payload。"""
    if not current_trace_id():
        return {}
    carrier = {}
    _propagator.inject(carrier)
    return {"traceparent": carrier["traceparent"],
        "attributes": {key: value for key, value in _metadata.get().items() if key in _CARRIER_KEYS}}


def context_trace_id(carrier):
    context = _propagator.extract({"traceparent": (carrier or {}).get("traceparent", "")}, context=Context())
    value = trace.get_current_span(context).get_span_context()
    return f"{value.trace_id:032x}" if value.is_valid else None


def mark_error(error):
    """只记错误类型/受控错误码；不记录可能包含正文的 exception.message/stacktrace。"""
    if not settings.trace_enabled:
        return
    span = trace.get_current_span()
    code = error if isinstance(error, str) else type(error).__name__
    span.set_attribute("error.type", code)
    span.set_status(trace.Status(trace.StatusCode.ERROR))


def set_trace_attributes(**attributes):
    """业务显式提供受控属性，关闭追踪时不修改其他库创建的 Span。"""
    if settings.trace_enabled:
        trace.get_current_span().set_attributes(attributes)


@contextmanager
def trace_span(name, *, attributes=None, carrier=None, root=False, kind=trace.SpanKind.INTERNAL):
    """在同一入口处理脱敏、采样、父子关系和异常，业务代码仅声明步骤。"""
    provider = initialize_tracing()
    if provider is None:
        yield trace.INVALID_SPAN
        return
    parent = None
    metadata = dict(_metadata.get())
    if carrier is not None:
        parent = _propagator.extract({"traceparent": carrier.get("traceparent", "")}, context=Context())
        metadata = {key: value for key, value in carrier.get("attributes", {}).items()
                    if key in _CARRIER_KEYS and isinstance(value, (str, int, bool))}
    elif root:
        parent, metadata = Context(), {}
    metadata.update(attributes or {})
    # 子步骤只继承业务关联字段，不继承父步骤的工具名、错误或 generation 类型。
    token = _metadata.set({key: value for key, value in metadata.items() if key in _CARRIER_KEYS})
    attrs = {**metadata, "langfuse.release": settings.trace_release,
        "langfuse.environment": settings.deployment_mode}
    if metadata.get("job.id"):
        attrs["langfuse.trace.metadata.job_id"] = metadata["job.id"]
    if metadata.get("prompt.version"):
        attrs["langfuse.version"] = metadata["prompt.version"]
    try:
        with provider.get_tracer("fundscope").start_as_current_span(name, context=parent,
                kind=kind, attributes=attrs, record_exception=False, set_status_on_exception=False) as span:
            try:
                yield span
            except BaseException as exc:
                if type(exc).__name__ in {"CancelledError", "JobCancelledError", "GeneratorExit"}:
                    span.set_attribute("execution.cancelled", True)
                else:
                    mark_error(exc)
                raise
    finally:
        _metadata.reset(token)


def traced(name, **attributes):
    """供普通同步/异步函数使用，不捕获其参数或返回正文。"""
    def decorate(function):
        if inspect.iscoroutinefunction(function):
            @wraps(function)
            async def async_call(*args, **kwargs):
                with trace_span(name, attributes=attributes):
                    return await function(*args, **kwargs)
            return async_call
        @wraps(function)
        def sync_call(*args, **kwargs):
            with trace_span(name, attributes=attributes):
                return function(*args, **kwargs)
        return sync_call
    return decorate


class TraceHTTPMiddleware:
    """纯 ASGI 包装整个响应流；SSE 最后一段发送后才结束 HTTP Span。"""
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "")
        if scope["type"] != "http" or path in {"/health", "/livez", "/readyz"} or path.startswith(("/static/", "/mini")):
            return await self.app(scope, receive, send)
        # 公网传入的 traceparent/baggage 不决定采样或跨用户关联；内部队列从 SQL 恢复。
        with trace_span("http.request", root=True, kind=trace.SpanKind.SERVER,
                attributes={"http.request.method": scope["method"]}) as span:
            async def traced_send(message):
                if message["type"] == "http.response.start":
                    span.set_attribute("http.response.status_code", message["status"])
                    if message["status"] >= 500:
                        mark_error("http_server_error")
                    trace_id = current_trace_id()
                    if trace_id:
                        message = {**message, "headers": [*message.get("headers", []),
                            (b"x-trace-id", trace_id.encode())]}
                await send(message)
            try:
                await self.app(scope, receive, traced_send)
            finally:
                # 路由模板无用户 ID、查询参数，且只有路由匹配之后才能读取。
                route = getattr(scope.get("route"), "path", "unmatched")
                span.update_name(f"{scope['method']} {route}")
                span.set_attribute("http.route", route)
