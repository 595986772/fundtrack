"""FundScope 基金列表、详情与风险分析 API。"""

from enum import IntEnum

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from starlette.concurrency import run_in_threadpool

from app.schemas import (
    BackgroundJobResponse,
    FundAssessmentResponse,
    FundDataSyncRequest,
    FundDataSyncStatusResponse,
    FundDetailResponse,
    FundChartResponse,
    FundDisclosureResponse,
    FundHoldingsAnalysisResponse,
    FundDirectorySearchResponse,
    FundHybridAssessmentResponse,
    FundImportRequest,
    FundListResponse,
    FundRiskResponse,
    FundSemanticReviewRequest,
    FundTransactionTimingRequest,
    FundTransactionTimingResponse,
)
from app.services.background_jobs import background_job_service
from app.services.auth import CurrentPrincipal, get_current_principal
from app.services.fund_data_sync import fund_nav_sync_service
from app.services.fund_disclosures import fund_disclosure_service
from app.services.fund_holdings import fund_holdings_service
from app.services.fund_catalog import (
    FundCatalogProviderError,
    fund_directory_service,
)
from app.services.fund_assessment import (
    FundAssessmentError,
    evidence_fusion_service,
)
from app.services.funds import (
    FundCatalogError,
    FundNotFoundError,
    fund_catalog_service,
)
from app.services.fund_risk import FundRiskError, fund_risk_service
from app.services.fund_chart_data import build_fund_chart_data
from app.services.fund_transaction_timing import preview_transaction_timing
from app.services.fund_returns import FundReturnSeriesError
from app.services.fund_benchmarks import fund_benchmark_repository
from app.services.eval_context import frozen_eval_data_enabled
from app.services.fund_semantic_review import (
    FundSemanticReviewError,
    fund_semantic_review_service,
)


router = APIRouter(prefix="/api/v1/funds", tags=["funds"])


class FundRiskMonths(IntEnum):
    """URL 查询参数支持的三个风险观察区间。"""

    SIX_MONTHS = 6
    ONE_YEAR = 12
    THREE_YEARS = 36


@router.post("/{fund_code}/transaction-timing", response_model=FundTransactionTimingResponse)
async def fund_transaction_timing(
    request: FundTransactionTimingRequest,
    fund_code: str = Path(..., pattern=r"^\d{6}$"),
):
    """公开基金的只读时间估算，不读取用户资产，也不提交真实交易。"""
    try:
        return await run_in_threadpool(preview_transaction_timing, fund_code=fund_code,
                                      **request.model_dump())
    except FundNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    except FundCatalogError as exc:
        raise HTTPException(503, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get("", response_model=FundListResponse)
async def list_available_funds():
    """返回三只冻结基线和用户已经导入的基金。"""
    try:
        return fund_catalog_service.list_funds()
    except FundCatalogError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.get("/search", response_model=FundDirectorySearchResponse)
async def search_fund_directory(
    q: str = Query(..., min_length=1, max_length=80),
    limit: int = Query(default=12, ge=1, le=30),
    refresh: bool = Query(default=False),
):
    """搜索在线公募基金目录；AKShare 的同步调用放到线程池中执行。"""

    try:
        return await run_in_threadpool(
            fund_directory_service.search,
            query=q,
            limit=limit,
            refresh=refresh,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except FundCatalogProviderError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.post("/import", response_model=BackgroundJobResponse)
async def import_fund_to_workspace(
    request: FundImportRequest,
    principal: CurrentPrincipal = Depends(get_current_principal),
):
    """创建异步导入 Job，HTTP 请求本身不等待档案和三年净值下载。"""

    try:
        return background_job_service.start_job(
            user_id=principal.resolve_user_id(request.user_id),
            title=f"导入基金 {request.fund_code}",
            job_type="fund_catalog_import",
            fund_code=request.fund_code,
            max_retries=1,
            timeout_seconds=360,
            idempotency_key=request.idempotency_key,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.get("/data-status", response_model=FundDataSyncStatusResponse)
async def get_fund_data_status():
    """查看净值、持仓披露、最后同步时间和每日 Cron 配置。"""

    return fund_nav_sync_service.status()


@router.post("/data-sync", response_model=BackgroundJobResponse)
async def start_fund_data_sync(
    request: FundDataSyncRequest,
    principal: CurrentPrincipal = Depends(get_current_principal),
):
    """把净值与定期持仓披露同步交给 Background Job，HTTP 不等待网络抓取。"""

    try:
        return background_job_service.start_job(
            user_id=principal.resolve_user_id(request.user_id),
            title="同步 FundScope 基金净值与持仓披露",
            job_type="fund_nav_sync",
            max_retries=1,
            timeout_seconds=300,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.get("/{fund_code}", response_model=FundDetailResponse)
async def get_sample_fund(
    fund_code: str = Path(..., pattern=r"^\d{6}$"),
):
    """返回单只基金的档案和三年净值曲线。"""
    try:
        return fund_catalog_service.get_fund(fund_code)
    except FundNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except FundCatalogError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.get(
    "/{fund_code}/disclosures",
    response_model=FundDisclosureResponse,
)
async def get_fund_disclosures(
    fund_code: str = Path(..., pattern=r"^\d{6}$"),
):
    """返回最新股票、债券和行业披露；该数据来自季报而非盘中实时仓位。"""

    try:
        return fund_disclosure_service.get_fund(fund_code=fund_code)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.get("/{fund_code}/holdings-analysis", response_model=FundHoldingsAnalysisResponse)
async def get_fund_holdings_analysis(
    fund_code: str = Path(..., pattern=r"^\d{6}$"),
):
    """按单基金读取已保存披露与历史对比，不创建组合、不启动同步。"""
    try:
        return await run_in_threadpool(fund_holdings_service.analyze, fund_code=fund_code)
    except FundNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.get("/{fund_code}/chart", response_model=FundChartResponse)
async def get_fund_chart(fund_code: str = Path(..., pattern=r"^\d{6}$")):
    """只读现有快照；冻结测试不把运行数据库的基准混入固定样本。"""
    def read_chart():
        detail = fund_catalog_service.get_fund(fund_code)
        benchmark = None if frozen_eval_data_enabled() else fund_benchmark_repository.get_context(fund_code=fund_code)
        return build_fund_chart_data(detail, benchmark)

    try:
        return await run_in_threadpool(read_chart)
    except FundNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (FundCatalogError, FundReturnSeriesError) as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.get("/{fund_code}/risk", response_model=FundRiskResponse)
async def get_sample_fund_risk(
    fund_code: str = Path(..., pattern=r"^\d{6}$"),
    months: FundRiskMonths = Query(default=FundRiskMonths.ONE_YEAR),
):
    """返回指定基金近 6、12 或 36 个月的确定性风险指标。"""

    try:
        return fund_risk_service.analyze(fund_code=fund_code, months=int(months))
    except FundNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (FundCatalogError, FundRiskError) as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.get("/{fund_code}/assessment", response_model=FundAssessmentResponse)
async def get_sample_fund_assessment(
    fund_code: str = Path(..., pattern=r"^\d{6}$"),
    months: FundRiskMonths = Query(default=FundRiskMonths.ONE_YEAR),
):
    """融合历史风险、市场状态和关联事件，返回可追溯的单基金研判。"""

    try:
        return evidence_fusion_service.assess(
            fund_code=fund_code,
            months=int(months),
        )
    except FundNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except FundAssessmentError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.post(
    "/{fund_code}/semantic-review",
    response_model=FundHybridAssessmentResponse,
)
async def create_fund_semantic_review(
    fund_code: str = Path(..., pattern=r"^\d{6}$"),
    months: FundRiskMonths = Query(default=FundRiskMonths.ONE_YEAR),
    review_request: FundSemanticReviewRequest | None = None,
):
    """按需调用 DeepSeek，为事件证据添加受约束的语义判断。"""

    try:
        return await fund_semantic_review_service.review(
            fund_code=fund_code,
            months=int(months),
            question=review_request.question if review_request else None,
        )
    except FundNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except FundSemanticReviewError as exc:
        # 模型配置和上游调用失败属于当前服务暂不可用，不伪装成空研判。
        raise HTTPException(status_code=503, detail=str(exc)) from exc
