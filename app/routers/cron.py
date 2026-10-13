"""s14 Cron Scheduler 的直接测试接口。

这些接口让学习者不用等待模型选择工具，也能观察 Schedule 注册、Queue 入队、自动交付和
取消。`trigger` 只用于快速实验，真正的自动触发仍由 Scheduler 根据 Cron 时间完成。
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from app.schemas import (
    CronScheduleActionRequest,
    CronScheduleCreateRequest,
    CronScheduleListResponse,
    CronScheduleResponse,
    CronTriggerResponse,
)
from app.services.cron_scheduler import cron_scheduler_service


router = APIRouter(prefix="/cron-schedules", tags=["cron-scheduler"])


@router.post("", response_model=CronScheduleResponse)
async def create_cron_schedule(request: CronScheduleCreateRequest):
    """创建 recurring 或 one-shot Cron，可选择是否持久化。"""

    try:
        return cron_scheduler_service.create_schedule(
            user_id=request.user_id,
            cron=request.cron,
            prompt=request.prompt,
            job_type=request.job_type,
            recurring=request.recurring,
            durable=request.durable,
            schedule_id=request.schedule_id,
            job_payload=request.job_payload,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("", response_model=CronScheduleListResponse)
async def list_cron_schedules(
    user_id: str = Query(default="guest", min_length=1, max_length=50),
    max_results: int = Query(default=50, ge=1, le=100),
):
    """查看 Schedule、Queue 深度、最近交付记录和调度线程状态。"""

    try:
        return cron_scheduler_service.list_schedules(
            user_id=user_id,
            max_results=max_results,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/{schedule_id}/trigger", response_model=CronTriggerResponse)
async def trigger_cron_schedule(
    schedule_id: str,
    request: CronScheduleActionRequest,
):
    """不等待 Cron 时间，手动产生一个 Queue 事件用于测试。"""

    try:
        return cron_scheduler_service.trigger_now(
            user_id=request.user_id,
            schedule_id=schedule_id,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=exc.args[0]) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/{schedule_id}/cancel", response_model=CronScheduleResponse)
async def cancel_cron_schedule(
    schedule_id: str,
    request: CronScheduleActionRequest,
):
    """删除 Schedule，阻止未来再次触发。"""

    try:
        return cron_scheduler_service.cancel_schedule(
            user_id=request.user_id,
            schedule_id=schedule_id,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=exc.args[0]) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
