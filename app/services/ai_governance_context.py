"""调用治理的身份和任务上下文，不把额度控制参数暴露给大模型。

ContextVar 随异步任务和框架线程池传播。HTTP 从登录身份绑定用户，Worker 则从
数据库中的 Job 恢复身份；模型不能通过修改工具参数替别人消费额度。
"""

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from hashlib import sha256
from typing import Callable
from uuid import uuid4


class AIControlError(Exception):
    """资源不足是业务拒绝，不是供应商故障；禁止重试或换模型绕过额度。"""

    def __init__(self, code: str, message: str, *, status_code: int = 429, retry_after: int = 5):
        super().__init__(message)
        self.code = code
        self.status_code = status_code
        self.retry_after = retry_after


@dataclass(frozen=True)
class AIExecutionScope:
    user_id: str
    run_id: str
    source: str
    check_cancelled: Callable[[], None] | None = None


_scope: ContextVar[AIExecutionScope | None] = ContextVar("ai_execution_scope", default=None)


def current_ai_scope() -> AIExecutionScope:
    # 脚本没有 HTTP 登录态时归入明确的系统账户，不冒充某个真实用户。
    scope = _scope.get()
    if scope is None:
        scope = AIExecutionScope("system:local", f"script:{uuid4().hex}", "script")
        _scope.set(scope)
    return scope


def bind_ai_user(user_id: str) -> None:
    """只由服务端鉴权依赖调用；HTTP 的随机 run_id 不随用户输入变化。"""
    scope = _scope.get()
    if scope is not None:
        _scope.set(AIExecutionScope(user_id, scope.run_id, scope.source, scope.check_cancelled))


def stable_run_id(kind: str, value: str) -> str:
    # 不保存原始幂等键，也不允许过长的客户端字符串进入账本索引。
    return f"{kind}:{sha256(value.encode()).hexdigest()}"


@contextmanager
def ai_execution_scope(user_id: str, run_id: str, source: str, check_cancelled=None):
    token = _scope.set(AIExecutionScope(user_id, run_id, source, check_cancelled))
    try:
        yield _scope.get()
    finally:
        _scope.reset(token)
