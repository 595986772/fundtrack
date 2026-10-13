"""s13 Background Tasks 的直接测试接口。

Agent 可以通过四个 tools 操作后台 Job；这里再提供 REST 接口，让学习者不依赖模型是否
选对工具，也能稳定观察启动、查询、列表和取消过程。
"""

from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from app.schemas import (
    BackgroundJobActionRequest,
    BackgroundJobListResponse,
    BackgroundJobResponse,
    BackgroundJobStartRequest,
)
from app.services.background_jobs import background_job_service
from app.services.agent_execution_context import AgentExecutionContext
from app.services.auth import CurrentPrincipal, get_current_principal, require_admin


router = APIRouter(prefix="/background-jobs", tags=["background-jobs"])


@router.post("", response_model=BackgroundJobResponse)
async def start_background_job(
    request: BackgroundJobStartRequest,
    principal: CurrentPrincipal = Depends(get_current_principal),
):
    """提交后台工作；接口会快速返回，不等待所有步骤完成。"""

    # Router 只做 HTTP/Pydantic 到 Service 参数的转换，可靠性规则都留在 service 层复用。
    user_id = principal.resolve_user_id(request.user_id)
    try:
        # 建单包含 SQL 与 OpenFGA 网络访问；线程池保留 ContextVar 且不阻塞事件循环。
        return await asyncio.to_thread(background_job_service.start_job,
            user_id=user_id,
            title=request.title,
            total_steps=request.total_steps,
            step_delay_seconds=request.step_delay_seconds,
            job_id=request.job_id,
            job_type=request.job_type,
            prompt=request.prompt,
            portfolio_id=request.portfolio_id,
            max_tool_rounds=request.max_tool_rounds,
            max_retries=request.max_retries,
            timeout_seconds=request.timeout_seconds,
            idempotency_key=request.idempotency_key,
            # 通用 Background Job REST 是产品入口；请求体不能把它升级成开发者任务。
            execution_context=AgentExecutionContext.product(
                user_id=user_id,
                role=principal.role,
                authenticated=principal.authenticated,
                source="background-job-api",
            ),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        # Redis/Celery 暂时不可用属于基础设施故障，不是请求格式错误。
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.get("/runtime", dependencies=[Depends(require_admin)])
async def get_background_job_runtime():
    """查看当前使用 local/SQLite 还是 Celery/PostgreSQL。"""

    return {"ok": True, "runtime": background_job_service.runtime_info()}


@router.get("", response_model=BackgroundJobListResponse)
async def list_background_jobs(
    user_id: str = Query(default="guest", min_length=1, max_length=50),
    status: str | None = Query(default=None),
    max_results: int = Query(default=30, ge=1, le=100),
    principal: CurrentPrincipal = Depends(get_current_principal),
):
    """查看当前用户的后台 Job；列表按最近更新时间倒序。"""

    user_id = principal.resolve_user_id(user_id)
    try:
        return background_job_service.list_jobs(
            user_id=user_id,
            status=status,
            max_results=max_results,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/{job_id}", response_model=BackgroundJobResponse)
async def get_background_job(
    job_id: str,
    user_id: str = Query(default="guest", min_length=1, max_length=50),
    principal: CurrentPrincipal = Depends(get_current_principal),
):
    """按 job_id 查询单次后台运行状态。"""

    user_id = principal.resolve_user_id(user_id)
    try:
        return background_job_service.get_job(user_id=user_id, job_id=job_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=exc.args[0]) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/{job_id}/events")
async def stream_background_job_events(
    request: Request,
    job_id: str,
    user_id: str = Query(default="guest", min_length=1, max_length=50),
    principal: CurrentPrincipal = Depends(get_current_principal),
):
    """用 SSE 推送 Job 状态，直到终态、等待人工输入或浏览器断开。"""

    user_id = principal.resolve_user_id(user_id)
    try:
        # 在返回 StreamingResponse 前先校验一次，404/400 才能使用正常 HTTP 状态返回。
        await asyncio.to_thread(
            background_job_service.get_job,
            user_id=user_id,
            job_id=job_id,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=exc.args[0]) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    async def event_generator():
        # last_version 是轻量变更指纹；没有变化就不重复把同一快照推给浏览器。
        last_version: tuple | None = None

        while not await request.is_disconnected():
            # SQLAlchemy 是同步 API，用 to_thread 避免数据库轮询阻塞 FastAPI 事件循环。
            response = await asyncio.to_thread(
                background_job_service.get_job,
                user_id=user_id,
                job_id=job_id,
            )
            job = response["job"]
            version = (
                job["updated_at"],
                job["status"],
                job["current_step"],
                job.get("error"),
            )

            # 只有状态真正变化时才发 progress，减少重复网络数据。
            if version != last_version:
                yield (
                    "event: progress\n"
                    f"data: {json.dumps(job, ensure_ascii=False)}\n\n"
                )
                last_version = version

            if job["status"] in {"completed", "failed", "cancelled"}:
                # done 是一次明确终态事件，前端收到后可以关闭 EventSource。
                yield (
                    "event: done\n"
                    f"data: {json.dumps(job, ensure_ascii=False)}\n\n"
                )
                return

            if job["status"] == "waiting_input":
                # waiting_input 会释放当前 Worker 和 SSE 连接；前端提交人工输入后会
                # 重新连接同一个 Job，而不是让一个 HTTP 请求无限等待。
                yield (
                    "event: input_required\n"
                    f"data: {json.dumps(job, ensure_ascii=False)}\n\n"
                )
                return

            await asyncio.sleep(background_job_service.runtime_info()["sse_poll_seconds"])

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            # 禁止浏览器/代理缓存，并提示 Nginx 不要缓冲 SSE 小数据块。
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/{job_id}/cancel", response_model=BackgroundJobResponse)
async def cancel_background_job(
    job_id: str,
    request: BackgroundJobActionRequest,
    principal: CurrentPrincipal = Depends(get_current_principal),
):
    """向运行中的 Job 发送协作式取消信号。"""

    try:
        return background_job_service.cancel_job(
            user_id=principal.resolve_user_id(request.user_id),
            job_id=job_id,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=exc.args[0]) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
