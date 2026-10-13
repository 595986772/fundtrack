"""Agent Evals & Observability 产品 API。"""

from fastapi import APIRouter, Depends, HTTPException, Query
import hashlib
import json

from app.schemas import (
    AgentEvalDraftInfo,
    AgentEvalDraftListResponse,
    AgentEvalDraftUpsertRequest,
    AgentEvalDashboard,
    AgentEvalRun,
    AgentEvalRunRequest,
    AgentObservabilitySummary,
    BackgroundJobResponse, FundReportEvalJobRequest, FundReportEvalReviewRequest,
)
from app.services.agent_evals import AgentEvalError, agent_eval_service
from app.services.agent_feedback import AgentFeedbackNotFoundError
from app.services.eval_drafts import (
    AgentEvalDraftError,
    AgentEvalDraftValidationError,
    agent_eval_draft_service,
)
from app.services.observability import agent_observability_service
from app.services.auth import CurrentPrincipal, require_admin
from app.services.agent_execution_context import AgentExecutionContext
from app.services.background_jobs import background_job_service


router = APIRouter(prefix="/api/v1", tags=["agent-evals"])


def evaluation_service(suite_id):
    """套件白名单，不接受前端传本地文件路径或任意Python类。"""
    if suite_id == "fund_agent_v2":
        return agent_eval_service
    if suite_id == "fund_report_rag_v1":
        from app.services.fund_report_evals import fund_report_eval_service
        return fund_report_eval_service
    raise HTTPException(400, "不支持的评测套件。")


@router.get("/evals/dashboard", response_model=AgentEvalDashboard)
async def get_eval_dashboard(
    user_id: str = Query(default="guest", min_length=1, max_length=50),
    principal: CurrentPrincipal = Depends(require_admin),
    suite_id: str = Query(default="fund_agent_v2"),
    retrieval_mode: str = Query(default="hybrid", pattern="^(keyword|hybrid)$"),
):
    """读取固定评测集、最近成绩和运行观测摘要。"""

    try:
        return evaluation_service(suite_id).dashboard(
            user_id=principal.resolve_user_id(user_id),
            **({"retrieval_mode": retrieval_mode} if suite_id == "fund_report_rag_v1" else {}),
        )
    except AgentEvalError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/evals/runs", response_model=AgentEvalRun)
async def run_agent_eval(
    request: AgentEvalRunRequest,
    principal: CurrentPrincipal = Depends(require_admin),
):
    """真实执行全部或指定评测场景；请求会等待本轮评测完成。"""

    try:
        return await evaluation_service(request.suite_id).run_suite(
            user_id=principal.resolve_user_id(request.user_id),
            case_ids=request.case_ids,
        )
    except AgentEvalError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/evals/fund-reports/jobs", response_model=BackgroundJobResponse)
def start_report_eval(request: FundReportEvalJobRequest, principal: CurrentPrincipal = Depends(require_admin)):
    """仅显式点击运行才创建模型任务；相同场景与幂等键不会重复计费执行。"""
    service = evaluation_service("fund_report_rag_v1")
    try:
        scenarios = service._select_scenarios(service._load_suite()["scenarios"], request.case_ids)
        service.validate_fixture()
        user_id = principal.resolve_user_id(request.user_id)
        case_ids = [case["id"] for case in scenarios]
        signature = hashlib.sha256(json.dumps([sorted(case_ids), request.retrieval_mode]).encode()).hexdigest()[:12]
        return background_job_service.start_job(user_id=user_id, title=f"季报RAG评测 · {len(case_ids)}题",
            job_type="fund_report_eval", total_steps=len(case_ids), timeout_seconds=3600, max_retries=0,
            idempotency_key=f"report-eval:{signature}:{request.idempotency_key}",
            eval_case_ids=case_ids, eval_retrieval_mode=request.retrieval_mode,
            execution_context=AgentExecutionContext.developer(user_id=user_id,
                role=principal.role, authenticated=principal.authenticated, source="report-eval-api"))
    except (AgentEvalError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(503, "评测任务暂时无法启动，请检查后台执行服务。") from exc


@router.get("/evals/runs/{run_id}", response_model=AgentEvalRun)
def get_eval_run(run_id: str, user_id: str = Query(default="guest", min_length=1, max_length=50),
                 principal: CurrentPrincipal = Depends(require_admin)):
    try:
        return agent_observability_service.get_eval_run(user_id=principal.resolve_user_id(user_id), run_id=run_id)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.put("/evals/runs/{run_id}/cases/{case_id}/review", response_model=AgentEvalRun)
def review_report_case(run_id: str, case_id: str, request: FundReportEvalReviewRequest,
                       principal: CurrentPrincipal = Depends(require_admin)):
    try:
        return agent_observability_service.review_report_case(user_id=principal.resolve_user_id(request.user_id),
            run_id=run_id, case_id=case_id, status=request.status, note=request.note)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get("/evals/drafts", response_model=AgentEvalDraftListResponse)
async def list_agent_eval_drafts(
    user_id: str = Query(default="guest", min_length=1, max_length=50),
    principal: CurrentPrincipal = Depends(require_admin),
):
    """读取人工草稿；这些文件不会被正式评测运行器自动加载。"""

    try:
        return agent_eval_draft_service.list(
            user_id=principal.resolve_user_id(user_id)
        )
    except AgentEvalDraftError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.put(
    "/evals/drafts/{feedback_id}",
    response_model=AgentEvalDraftInfo,
)
async def upsert_agent_eval_draft(
    feedback_id: str,
    request: AgentEvalDraftUpsertRequest,
    principal: CurrentPrincipal = Depends(require_admin),
):
    """把已保留反馈保存为本地草稿，不修改正式版本化评测集。"""

    request = request.model_copy(
        update={"user_id": principal.resolve_user_id(request.user_id)}
    )
    try:
        return agent_eval_draft_service.upsert(
            feedback_id=feedback_id,
            request=request,
        )
    except AgentFeedbackNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except AgentEvalDraftValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except AgentEvalDraftError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get("/observability", response_model=AgentObservabilitySummary)
async def get_observability_summary(
    user_id: str = Query(default="guest", min_length=1, max_length=50),
    principal: CurrentPrincipal = Depends(require_admin),
):
    """提供不依赖前端的运行指标查询接口。"""

    return agent_observability_service.summary(
        user_id=principal.resolve_user_id(user_id)
    )
