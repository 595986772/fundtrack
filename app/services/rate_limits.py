"""昂贵聊天接口的轻量滑动窗口限流。"""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
from threading import Lock
from time import monotonic

from fastapi import Depends, HTTPException, Request

from app.config import settings
from app.services.auth import CurrentPrincipal, get_current_principal
from app.services.ai_capacity import get_ai_capacity
from starlette.concurrency import run_in_threadpool


@dataclass(frozen=True)
class RateLimitDecision:
    allowed: bool
    remaining: int
    retry_after_seconds: int


class SlidingWindowRateLimiter:
    """当前单 API 进程共享的滑动窗口；接口可在多副本部署时替换为 Redis。"""

    def __init__(self) -> None:
        self._events: dict[str, deque[float]] = defaultdict(deque)
        self._lock = Lock()

    def consume(
        self,
        *,
        key: str,
        limit: int,
        window_seconds: float = 60.0,
    ) -> RateLimitDecision:
        now = monotonic()
        safe_limit = max(1, limit)
        with self._lock:
            events = self._events[key]
            boundary = now - window_seconds
            while events and events[0] <= boundary:
                events.popleft()
            if len(events) >= safe_limit:
                retry_after = max(1, int(window_seconds - (now - events[0])) + 1)
                return RateLimitDecision(False, 0, retry_after)
            events.append(now)
            return RateLimitDecision(True, safe_limit - len(events), 0)


chat_rate_limiter = SlidingWindowRateLimiter()


async def enforce_chat_rate_limit(
    request: Request,
    principal: CurrentPrincipal = Depends(get_current_principal),
) -> CurrentPrincipal:
    """登录用户按账号限流，迁移模式按来源 IP 限流。"""

    client_host = request.client.host if request.client else "local"
    identity = principal.user_id if principal.authenticated else client_host
    if settings.ai_governance_enabled:
        values = await run_in_threadpool(get_ai_capacity().consume_rate,
            f"chat:{identity}", settings.chat_rate_limit_per_minute)
        decision = RateLimitDecision(*values)
    else:
        decision = chat_rate_limiter.consume(key=f"chat:{identity}", limit=settings.chat_rate_limit_per_minute)
    if not decision.allowed:
        raise HTTPException(
            status_code=429,
            detail="请求过于频繁，请稍后再试",
            headers={"Retry-After": str(decision.retry_after_seconds)},
        )
    return principal


async def enforce_developer_chat_rate_limit(
    principal: CurrentPrincipal = Depends(enforce_chat_rate_limit),
) -> CurrentPrincipal:
    """开发聊天复用费用限流，并额外要求服务端 Session 中的管理员角色。"""

    if principal.role != "admin":
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return principal
