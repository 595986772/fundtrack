"""调用额度的只读入口和管理员对账入口，不执行模型、不暴露其他用户的账单。"""

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from app.config import settings
from app.services.auth import CurrentPrincipal, get_current_principal, require_admin
from app.services.ai_capacity import get_ai_capacity
from app.services.ai_usage import get_ai_usage_service


router = APIRouter(prefix="/api/v1/ai", tags=["ai-usage"])


@router.get("/usage")
async def get_usage(user_id: str = Query("guest", max_length=50),
                    principal: CurrentPrincipal = Depends(get_current_principal)):
    owner = principal.resolve_user_id(user_id)
    result = await run_in_threadpool(get_ai_usage_service().summary, user_id=owner)
    capacity = get_ai_capacity()
    result["control"] = {"enabled": settings.ai_governance_enabled, "backend": capacity.backend_name,
        "global_concurrency": settings.ai_global_concurrency, "user_concurrency": settings.ai_user_concurrency,
        "global_queue_size": settings.ai_global_queue_size, "user_queue_size": settings.ai_user_queue_size,
        "queue_wait_seconds": settings.ai_queue_wait_seconds,
        "global_pending_jobs": settings.ai_global_pending_jobs, "user_pending_jobs": settings.ai_user_pending_jobs,
        **await run_in_threadpool(capacity.snapshot, owner)}
    return result


class UsageReconciliation(BaseModel):
    """只能填写核对供应商账单后的真实计数；0 也需要管理员明确确认。"""
    user_id: str = Field(min_length=1, max_length=80)
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    total_tokens: int = Field(ge=0)
    note: str = Field(min_length=5, max_length=120)


@router.post("/usage/calls/{call_id}/reconcile")
async def reconcile_usage(call_id: str, payload: UsageReconciliation,
                          principal: CurrentPrincipal = Depends(require_admin)):
    # 管理操作需有真实登录身份，本地 legacy 管理员不能远程修改费用记录。
    if not principal.authenticated:
        raise HTTPException(401, "请用管理员账号登录后对账")
    try:
        await run_in_threadpool(get_ai_usage_service().reconcile, user_id=payload.user_id,
            call_id=call_id, usage=payload.model_dump(include={"input_tokens", "output_tokens", "total_tokens"}),
            note=f"{principal.user_id}: {payload.note}")
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"ok": True, "call_id": call_id}
