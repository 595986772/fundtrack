"""用户查看和撤销自己的任务授权。不给前端提供任意创建/扩大授权的接口。"""

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from app.config import settings
from app.services.auth import CurrentPrincipal, get_current_principal
from app.services.agent_authorization import get_task_authorization_service

router = APIRouter(prefix="/api/v1/agent/authorizations", tags=["agent-authorizations"])


class RevokeAuthorizationRequest(BaseModel):
    user_id: str = Field(default="guest", min_length=1, max_length=50)
    reason: str = Field(default="用户撤销", max_length=200)


@router.get("")
def list_authorizations(user_id: str = Query(default="guest", min_length=1, max_length=50),
                        limit: int = Query(default=30, ge=1, le=100),
                        principal: CurrentPrincipal = Depends(get_current_principal)):
    # resolve_user_id 在正式登录模式忽略浏览器自报身份；本地兼容模式仍明确显示其限制。
    owner = principal.resolve_user_id(user_id)
    return {"enabled": settings.agent_task_authorization_enabled, "strict_identity": settings.auth_required,
        "grants": get_task_authorization_service().list(user_id=owner, limit=limit)}


@router.get("/{grant_id}")
def read_authorization(grant_id: str, user_id: str = Query(default="guest", min_length=1, max_length=50),
                       principal: CurrentPrincipal = Depends(get_current_principal)):
    try:
        return get_task_authorization_service().detail(
            user_id=principal.resolve_user_id(user_id), grant_id=grant_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/{grant_id}/revoke")
def revoke_authorization(grant_id: str, request: RevokeAuthorizationRequest,
                         principal: CurrentPrincipal = Depends(get_current_principal)):
    try:
        return get_task_authorization_service().revoke(
            user_id=principal.resolve_user_id(request.user_id), grant_id=grant_id, reason=request.reason)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
