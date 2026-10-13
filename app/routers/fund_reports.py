"""季报只读查询与显式同步接口；长期下载复用现有 Job，不占住手机请求。"""
from fastapi import APIRouter, Depends, HTTPException, Path, Query
from pydantic import BaseModel, Field

from app.schemas import BackgroundJobResponse
from app.services.auth import CurrentPrincipal, get_current_principal
from app.services.background_jobs import background_job_service
from app.services.fund_reports import fund_report_service

router = APIRouter(prefix="/api/v1/funds", tags=["fund-reports"])


class ReportSyncRequest(BaseModel):
    user_id: str = Field(default="guest", min_length=1, max_length=50)
    idempotency_key: str = Field(min_length=8, max_length=64, pattern=r"^[a-zA-Z0-9:_-]+$")


@router.get("/{fund_code}/reports")
def list_reports(fund_code: str = Path(pattern=r"^\d{6}$"),
                 document_ids: list[str] | None = Query(default=None, max_length=2)):
    try:
        return fund_report_service.list_reports(fund_code, document_ids)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/{fund_code}/reports/sync", response_model=BackgroundJobResponse)
def sync_reports(body: ReportSyncRequest, fund_code: str = Path(pattern=r"^\d{6}$"),
                 principal: CurrentPrincipal = Depends(get_current_principal)):
    try:
        return background_job_service.start_job(user_id=principal.resolve_user_id(body.user_id),
            title=f"同步 {fund_code} 最近两期季报", job_type="fund_report_sync", fund_code=fund_code,
            timeout_seconds=300, max_retries=0,
            idempotency_key=f"fund-reports:{fund_code}:{body.idempotency_key}")
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(503, "同步任务暂时无法启动，请稍后重试。") from exc


@router.get("/{fund_code}/reports/compare")
def compare_reports(fund_code: str = Path(pattern=r"^\d{6}$"),
                    document_ids: list[str] | None = Query(default=None, max_length=2)):
    try:
        return fund_report_service.compare(fund_code, document_ids)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get("/{fund_code}/reports/{document_id}/pages/{page}")
def report_page(fund_code: str = Path(pattern=r"^\d{6}$"),
                document_id: str = Path(pattern=r"^fr_[a-f0-9]{32}$"),
                page: int = Path(ge=1, le=80)):
    try:
        return fund_report_service.page(fund_code, document_id, page)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
