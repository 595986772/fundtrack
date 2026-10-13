"""HTTP Request ID、结构化访问日志和当前请求上下文。"""

from __future__ import annotations

from contextvars import ContextVar
import json
import logging
from time import perf_counter
from uuid import uuid4

from fastapi import Request
from app.services.ai_governance_context import ai_execution_scope
from app.services.tracing import current_trace_id


logger = logging.getLogger("fundscope.requests")
_request_id: ContextVar[str] = ContextVar("fundscope_request_id", default="")


def current_request_id() -> str:
    """业务和观测服务可读取同一 Request ID，无需层层传递参数。"""

    return _request_id.get()


async def request_observability_middleware(request: Request, call_next):
    """为每个 HTTP 请求生成关联 ID、记录耗时并写入响应头。"""

    incoming = str(request.headers.get("X-Request-ID") or "").strip()
    request_id = incoming[:96] or f"req_{uuid4().hex[:20]}"
    request.state.request_id = request_id
    token = _request_id.set(request_id)
    started = perf_counter()
    status_code = 500
    try:
        # 计费任务 ID 必须由服务端生成，不能直接信任客户端 X-Request-ID。
        # 鉴权依赖稍后绑定真正的用户；SSE 子任务继承自己的上下文副本。
        with ai_execution_scope("local:guest", f"http:{uuid4().hex}", "http"):
            response = await call_next(request)
        status_code = response.status_code
        response.headers["X-Request-ID"] = request_id
        response.headers["Server-Timing"] = (
            f"app;dur={(perf_counter() - started) * 1000:.2f}"
        )
        return response
    finally:
        duration_ms = round((perf_counter() - started) * 1000, 2)
        logger.info(
            json.dumps(
                {
                    "event": "http_request",
                    "request_id": request_id,
                    "trace_id": current_trace_id(),
                    "method": request.method,
                    "path": request.url.path,
                    "status_code": status_code,
                    "duration_ms": duration_ms,
                },
                ensure_ascii=False,
            )
        )
        _request_id.reset(token)
