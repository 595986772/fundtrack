"""健康检查路由。

部署平台、监控系统和前端顶部状态都会读取这个稳定接口。
"""

import asyncio

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from app.config import settings
from app.schemas import HealthResponse
from app.services.auth import require_admin
from app.services.deployment_health import dependency_status, runtime_diagnostics

# APIRouter 用来把一组接口组织在一起。
# tags 会显示在 `/docs` 页面里，方便按模块查看接口。
router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthResponse)
async def health_check():
    """返回前端和监控都能直接使用的基础运行信息。"""

    return {
        "status": "ok",
        "app_name": settings.app_name,
        "agent_model": settings.agent_model,
    }


@router.get("/livez")
async def liveness():
    """只说明 Web 进程能处理请求，外部依赖故障不触发无意义重启。"""

    return {"status": "alive"}


@router.get("/readyz")
async def readiness():
    """有限等待地检查启用的关键依赖，故障时返回 503。"""

    try:
        checks = await asyncio.wait_for(asyncio.to_thread(dependency_status), timeout=6)
    except TimeoutError:
        return JSONResponse(status_code=503, content={"status": "unavailable", "checks": {"timeout": "unavailable"}})
    ready = all(value == "ok" for value in checks.values())
    return JSONResponse(status_code=200 if ready else 503,
                        content={"status": "ready" if ready else "unavailable", "checks": checks})


@router.get("/api/v1/ops/runtime", dependencies=[Depends(require_admin)])
async def operations_runtime():
    """排队和 Worker 状态只供管理员查看，避免公开运营细节。"""

    try:
        result = await asyncio.wait_for(asyncio.to_thread(runtime_diagnostics), timeout=5)
    except Exception:
        return JSONResponse(status_code=503, content={"status": "unavailable"})
    return {"status": "ok", **result}
