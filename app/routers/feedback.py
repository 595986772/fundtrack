"""真实用户反馈与评测候选 API。"""

from fastapi import APIRouter, Depends, HTTPException, Query

from app.schemas import (
    AgentFeedbackCandidate,
    AgentFeedbackDashboard,
    AgentFeedbackReviewRequest,
    AgentFeedbackSubmitRequest,
)
from app.services.agent_feedback import (
    AgentFeedbackNotFoundError,
    AgentFeedbackValidationError,
    agent_feedback_service,
)
from app.services.auth import CurrentPrincipal, get_current_principal, require_admin


router = APIRouter(prefix="/api/v1", tags=["agent-feedback"])


@router.put(
    "/conversations/{conversation_id}/messages/{message_id}/feedback",
    response_model=AgentFeedbackCandidate,
)
async def submit_response_feedback(
    conversation_id: str,
    message_id: str,
    request: AgentFeedbackSubmitRequest,
    principal: CurrentPrincipal = Depends(get_current_principal),
):
    """创建或修改一条回答反馈；重复评价同一回答不会产生重复记录。"""

    try:
        return agent_feedback_service.submit(
            user_id=principal.resolve_user_id(request.user_id),
            conversation_id=conversation_id,
            assistant_message_id=message_id,
            rating=request.rating,
            reason_code=request.reason_code,
            comment=request.comment,
            correction=request.correction,
        )
    except AgentFeedbackNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except AgentFeedbackValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/feedback/dashboard", response_model=AgentFeedbackDashboard)
async def get_feedback_dashboard(
    user_id: str = Query(default="guest", min_length=1, max_length=50),
    principal: CurrentPrincipal = Depends(require_admin),
):
    """读取反馈统计和待人工审核的负向回答。"""

    return agent_feedback_service.dashboard(
        user_id=principal.resolve_user_id(user_id)
    )


@router.patch(
    "/feedback/{feedback_id}/review",
    response_model=AgentFeedbackCandidate,
)
async def review_feedback_candidate(
    feedback_id: str,
    request: AgentFeedbackReviewRequest,
    principal: CurrentPrincipal = Depends(require_admin),
):
    """保留或忽略候选；保留也不会自动写入正式评测集。"""

    try:
        return agent_feedback_service.review(
            user_id=principal.resolve_user_id(request.user_id),
            feedback_id=feedback_id,
            status=request.status,
            review_note=request.review_note,
        )
    except AgentFeedbackNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except AgentFeedbackValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
