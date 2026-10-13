"""正式运行配置和浏览器 Cookie 的同源写请求边界。"""

from __future__ import annotations

from urllib.parse import urlsplit
from pathlib import Path

from fastapi import Request
from fastapi.responses import JSONResponse

from app.config import settings


def ensure_recovery_released():
    """恢复卷默认禁止启动业务：核对报告并人工解除隔离后才能恢复服务。"""
    marker = Path(__file__).resolve().parents[2] / "data" / "RECOVERY_HOLD.json"
    if marker.exists():
        raise RuntimeError("恢复副本仍处于隔离状态：请先核对恢复报告、旧任务和费用，再移除 RECOVERY_HOLD.json")


def validate_production_settings() -> None:
    """启动前失败，避免以本地宽松默认值对外提供服务。"""

    ensure_recovery_released()
    if settings.deployment_mode not in {"local", "production"}:
        raise RuntimeError("DEPLOYMENT_MODE 只能是 local 或 production")
    if settings.deployment_mode != "production":
        return
    origin = urlsplit(settings.public_origin)
    checks = {
        "PUBLIC_ORIGIN 必须是 HTTPS 根地址": (
            origin.scheme == "https"
            and bool(origin.hostname)
            and not (origin.path or origin.query or origin.fragment or origin.username or origin.password)
        ),
        "PUBLIC_HOST 必须与 PUBLIC_ORIGIN 一致": (
            bool(settings.public_host) and settings.public_host == origin.hostname
        ),
        "AUTH_REQUIRED 必须为 true": settings.auth_required,
        "AUTH_COOKIE_SECURE 必须为 true": settings.auth_cookie_secure,
        "DATABASE_AUTO_CREATE_SCHEMA 必须为 false": not settings.database_auto_create_schema,
        "DATABASE_URL 必须使用 PostgreSQL": settings.database_url.startswith("postgresql"),
        "JOB_EXECUTION_MODE 必须为 celery": settings.job_execution_mode == "celery",
        "AI_CONTROL_BACKEND 必须为 redis": settings.ai_control_backend == "redis",
        "AGENT_TASK_AUTHORIZATION_ENABLED 必须为 true": settings.agent_task_authorization_enabled,
        "OPENFGA_TOKEN 不能为空": bool(settings.openfga_token),
        "DEEPSEEK_API_KEY 不能为空": bool(settings.deepseek_api_key),
    }
    failures = [message for message, passed in checks.items() if not passed]
    if failures:
        raise RuntimeError("正式运行配置无效：" + "；".join(failures))


async def production_request_guard(request: Request, call_next):
    """有 Cookie 的浏览器写请求必须来自本站；Bearer 客户端不依赖 Cookie。"""

    if (settings.deployment_mode == "production"
            and request.method in {"POST", "PUT", "PATCH", "DELETE"}
            and settings.auth_cookie_name in request.cookies
            and "authorization" not in request.headers):
        source = request.headers.get("origin")
        if source is None:
            # 某些同源表单只带 Referer；缺两者时不能确认浏览器来源。
            referer = urlsplit(request.headers.get("referer", ""))
            source = f"{referer.scheme}://{referer.netloc}" if referer.scheme and referer.netloc else ""
        if source.rstrip("/") != settings.public_origin:
            return JSONResponse(status_code=403, content={"detail": "请求来源与当前站点不一致"})
    return await call_next(request)
