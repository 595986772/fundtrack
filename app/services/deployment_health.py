"""进程就绪和管理员诊断：只检查依赖与队列，不触发模型或行情调用。"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import func, select, text

from app.config import settings
from app.database import runtime_database
from app.models import BackgroundJobModel


def responding_workers() -> int:
    """向 Celery 控制面发一次有界 ping；零个响应表示后台执行不可用。"""

    try:
        from app.celery_app import celery_app

        return len(celery_app.control.inspect(timeout=1.0).ping() or {})
    except Exception:
        return 0


def dependency_status() -> dict[str, str]:
    """返回依赖状态码，不把数据库地址、异常正文或凭据交给公网。"""

    def database() -> None:
        with runtime_database.engine.connect() as connection:
            connection.execute(text("SELECT 1"))

    def redis_connection(url: str) -> None:
        import redis

        client = redis.Redis.from_url(url, socket_connect_timeout=1, socket_timeout=1)
        try:
            if client.ping() is not True:
                raise RuntimeError("Redis 未响应 PING")
        finally:
            client.close()

    def openfga() -> None:
        from app.services.agent_authorization import _configured_policy

        _configured_policy().check_available()

    probes = {"database": database}
    if settings.job_execution_mode == "celery":
        probes["broker"] = lambda: redis_connection(settings.celery_broker_url)
        def worker() -> None:
            if responding_workers() < 1:
                raise RuntimeError("Worker 未响应")
        probes["worker"] = worker
    if settings.ai_control_backend == "redis":
        probes["ai_control"] = lambda: redis_connection(settings.ai_redis_url)
    if settings.agent_task_authorization_enabled:
        probes["authorization"] = openfga

    status = {}
    for name, probe in probes.items():
        try:
            probe()
            status[name] = "ok"
        except Exception:
            status[name] = "unavailable"
    return status


def runtime_diagnostics() -> dict:
    """读 SQL 中的待执行量；Worker 在线数通过 Celery 控制面有界查询。"""

    with runtime_database.session() as session:
        queued, running = (
            int(session.scalar(select(func.count()).select_from(BackgroundJobModel).where(
                BackgroundJobModel.status == status)) or 0)
            for status in ("queued", "running")
        )
        oldest = session.scalar(select(func.min(BackgroundJobModel.created_at)).where(
            BackgroundJobModel.status == "queued"))
    if oldest is not None and oldest.tzinfo is None:
        oldest = oldest.replace(tzinfo=timezone.utc)

    workers = None
    if settings.job_execution_mode == "celery":
        workers = responding_workers()

    return {
        "queued_jobs": queued,
        "running_jobs": running,
        "oldest_queued_seconds": max(0, int((datetime.now(timezone.utc) - oldest).total_seconds())) if oldest else None,
        "responding_workers": workers,
    }
