"""每日简报 API：登录身份决定归属，客户端不能替其他人开关任务。"""

import asyncio

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from pydantic import BaseModel, ConfigDict

from app.services.auth import CurrentPrincipal, get_current_principal
from app.services.daily_briefings import daily_briefing_service
from app.services.portfolios import PortfolioNotFoundError

router = APIRouter(prefix="/api/v1/portfolios", tags=["daily-briefings"])


class BriefingPolicyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    user_id: str
    enabled: bool


async def _call(method, **kwargs):
    try:
        return await asyncio.to_thread(method, **kwargs)
    except PortfolioNotFoundError as exc:
        raise HTTPException(status_code=404, detail="组合不存在") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/{portfolio_id}/daily-briefing")
async def get_briefing(portfolio_id: str = Path(pattern=r"^portfolio_[a-f0-9]{16}$"),
                       user_id: str = Query(), principal: CurrentPrincipal = Depends(get_current_principal)):
    return await _call(daily_briefing_service.overview, user_id=principal.resolve_user_id(user_id), portfolio_id=portfolio_id)


@router.put("/{portfolio_id}/daily-briefing")
async def configure_briefing(request: BriefingPolicyRequest,
                             portfolio_id: str = Path(pattern=r"^portfolio_[a-f0-9]{16}$"),
                             principal: CurrentPrincipal = Depends(get_current_principal)):
    return await _call(daily_briefing_service.configure, user_id=principal.resolve_user_id(request.user_id),
                       portfolio_id=portfolio_id, enabled=request.enabled)


@router.post("/{portfolio_id}/daily-briefing/runs")
async def run_briefing(portfolio_id: str = Path(pattern=r"^portfolio_[a-f0-9]{16}$"),
                       user_id: str = Query(), principal: CurrentPrincipal = Depends(get_current_principal)):
    return await _call(daily_briefing_service.run_today, user_id=principal.resolve_user_id(user_id), portfolio_id=portfolio_id)
