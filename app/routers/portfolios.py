"""FundScope 基金组合、真实持仓流水与组合研判 API。"""

import asyncio
from enum import IntEnum

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Response

from app.schemas import (
    BackgroundJobResponse,
    PortfolioAlertActionRequest,
    PortfolioAlertResponse,
    PortfolioAdjustmentResumeRequest,
    PortfolioAdjustmentStartRequest,
    PortfolioAssessmentResponse,
    PortfolioCreateRequest,
    PortfolioDecisionCreateRequest,
    PortfolioDecisionDeleteResponse,
    PortfolioDecisionListResponse,
    PortfolioDecisionResponse,
    PortfolioDecisionReviewRequest,
    PortfolioDeleteResponse,
    PortfolioEventImpactResponse,
    PortfolioExposureResponse,
    PortfolioListResponse,
    PortfolioMonitoringOverviewResponse,
    PortfolioMonitoringPolicyResponse,
    PortfolioMonitoringPolicyUpdateRequest,
    PortfolioMonitoringRunRequest,
    PortfolioPerformanceResponse,
    PortfolioResearchChangeAuditResponse,
    PortfolioResearchHistoryResponse,
    PortfolioResearchStartRequest,
    PortfolioRenameRequest,
    PortfolioResponse,
    PortfolioTransactionCreateRequest,
    PortfolioTransactionDeleteResponse,
    PortfolioTransactionListResponse,
    PortfolioTransactionResponse,
    PortfolioUpdateRequest,
    PortfolioStressRequest,
    PortfolioStressReport,
)
from app.services.portfolio_stress import portfolio_stress_service, PortfolioStressError, PortfolioStressNotFound
from app.services.portfolio_allocation import portfolio_allocation_service
from app.services.portfolio_allocation_contracts import AllocationDraftRequest, AllocationConfirmRequest
from app.services.portfolio_risk_engine import PortfolioRiskEngineError
from sqlalchemy.exc import SQLAlchemyError
from app.services.background_jobs import background_job_service
from app.services.auth import CurrentPrincipal, get_current_principal
from app.services.cron_scheduler import cron_scheduler_service
from app.services.portfolio_assessment import (
    PortfolioAssessmentError,
    portfolio_assessment_service,
)
from app.services.portfolio_exposure import (
    PortfolioExposureError,
    portfolio_exposure_service,
)
from app.services.portfolio_event_impacts import (
    PortfolioEventImpactError,
    portfolio_event_impact_service,
)
from app.services.portfolio_decisions import (
    PortfolioDecisionError,
    portfolio_decision_service,
)
from app.services.portfolio_performance import (
    PortfolioPerformanceError,
    portfolio_performance_service,
)
from app.services.portfolios import (
    PortfolioError,
    PortfolioIdempotencyConflictError,
    PortfolioNotFoundError,
    PortfolioValidationError,
    portfolio_service,
)
from app.services.portfolio_monitoring import portfolio_monitoring_service
from app.services.portfolio_review import portfolio_review_service
from app.services.portfolio_trades import PortfolioTradeService
from app.services.portfolio_trade_contracts import (
    TradeOrderCreateRequest, TradeOrderActionRequest, TransactionCorrectionRequest,
)
from app.services.portfolio_research_audit import (
    PortfolioResearchAuditNotFoundError,
    PortfolioResearchAuditValidationError,
    PortfolioResearchComparisonUnavailableError,
    portfolio_research_audit_service,
)


router = APIRouter(prefix="/api/v1/portfolios", tags=["fund-portfolios"])


class PortfolioAssessmentMonths(IntEnum):
    """组合中所有基金必须使用相同的观察区间。"""

    SIX_MONTHS = 6
    ONE_YEAR = 12
    THREE_YEARS = 36


async def _stress_call(function, **kwargs):
    """SQL 和 NumPy 在工作线程执行，避免阻塞 FastAPI 事件循环。"""
    try:
        return await asyncio.to_thread(function, **kwargs)
    except (PortfolioStressNotFound, PortfolioNotFoundError) as exc:
        raise HTTPException(404, str(exc)) from exc
    except (PortfolioStressError, PortfolioRiskEngineError, PortfolioValidationError) as exc:
        raise HTTPException(400, str(exc)) from exc
    except (SQLAlchemyError, PortfolioError) as exc:
        raise HTTPException(503, "压力测试存储或本地数据暂不可用") from exc


@router.post("/{portfolio_id}/allocation-drafts")
async def create_allocation_draft(
    request: AllocationDraftRequest,
    portfolio_id: str = Path(..., pattern=r"^portfolio_[a-f0-9]{16}$"),
    principal: CurrentPrincipal = Depends(get_current_principal),
):
    request = request.model_copy(update={"user_id": principal.resolve_user_id(request.user_id)})
    return await _stress_call(portfolio_allocation_service.create, portfolio_id=portfolio_id, request=request)


@router.get("/{portfolio_id}/allocation-drafts")
async def list_allocation_drafts(
    portfolio_id: str = Path(..., pattern=r"^portfolio_[a-f0-9]{16}$"),
    user_id: str = Query("guest", min_length=1, max_length=50),
    principal: CurrentPrincipal = Depends(get_current_principal),
):
    return await _stress_call(portfolio_allocation_service.list, user_id=principal.resolve_user_id(user_id), portfolio_id=portfolio_id)


@router.get("/{portfolio_id}/allocation-drafts/{draft_id}")
async def get_allocation_draft(
    portfolio_id: str = Path(..., pattern=r"^portfolio_[a-f0-9]{16}$"),
    draft_id: str = Path(..., pattern=r"^alloc_[a-f0-9]{16}$"),
    user_id: str = Query("guest", min_length=1, max_length=50),
    principal: CurrentPrincipal = Depends(get_current_principal),
):
    return await _stress_call(portfolio_allocation_service.get, user_id=principal.resolve_user_id(user_id), portfolio_id=portfolio_id, draft_id=draft_id)


@router.post("/{portfolio_id}/allocation-drafts/{draft_id}/confirm")
async def confirm_allocation_draft(
    request: AllocationConfirmRequest,
    portfolio_id: str = Path(..., pattern=r"^portfolio_[a-f0-9]{16}$"),
    draft_id: str = Path(..., pattern=r"^alloc_[a-f0-9]{16}$"),
    principal: CurrentPrincipal = Depends(get_current_principal),
):
    """仅来自用户的显式确认。它不是 Agent Tool，也不产生交易或持仓写入。"""
    request = request.model_copy(update={"user_id": principal.resolve_user_id(request.user_id)})
    return await _stress_call(portfolio_allocation_service.confirm, portfolio_id=portfolio_id, draft_id=draft_id, request=request)


@router.post("/{portfolio_id}/stress-runs", response_model=PortfolioStressReport)
async def run_portfolio_stress(
    request: PortfolioStressRequest,
    portfolio_id: str = Path(..., pattern=r"^portfolio_[a-f0-9]{16}$"),
    principal: CurrentPrincipal = Depends(get_current_principal),
):
    """仅显式提交才计算；带 source_run_id 时只比较原冻结情景。"""
    request = request.model_copy(update={"user_id": principal.resolve_user_id(request.user_id)})
    return await _stress_call(portfolio_stress_service.run, portfolio_id=portfolio_id, request=request)


@router.get("/{portfolio_id}/stress-runs")
async def list_portfolio_stress(
    portfolio_id: str = Path(..., pattern=r"^portfolio_[a-f0-9]{16}$"),
    user_id: str = Query("guest", min_length=1, max_length=50),
    principal: CurrentPrincipal = Depends(get_current_principal),
):
    return await _stress_call(portfolio_stress_service.list, user_id=principal.resolve_user_id(user_id), portfolio_id=portfolio_id)


@router.get("/{portfolio_id}/stress-runs/{run_id}", response_model=PortfolioStressReport)
async def get_portfolio_stress(
    portfolio_id: str = Path(..., pattern=r"^portfolio_[a-f0-9]{16}$"),
    run_id: str = Path(..., pattern=r"^stress_[a-f0-9]{16}$"),
    user_id: str = Query("guest", min_length=1, max_length=50),
    principal: CurrentPrincipal = Depends(get_current_principal),
):
    return await _stress_call(portfolio_stress_service.get, user_id=principal.resolve_user_id(user_id), portfolio_id=portfolio_id, run_id=run_id)


@router.post("/{portfolio_id}/stress-runs/{run_id}/verify")
async def verify_portfolio_stress(
    portfolio_id: str = Path(..., pattern=r"^portfolio_[a-f0-9]{16}$"),
    run_id: str = Path(..., pattern=r"^stress_[a-f0-9]{16}$"),
    user_id: str = Query("guest", min_length=1, max_length=50),
    principal: CurrentPrincipal = Depends(get_current_principal),
):
    """用冻结输入复算，而不是更新行情；不新增报告。"""
    return await _stress_call(portfolio_stress_service.verify, user_id=principal.resolve_user_id(user_id), portfolio_id=portfolio_id, run_id=run_id)


@router.get("", response_model=PortfolioListResponse)
async def list_portfolios(
    user_id: str = Query(default="guest", min_length=1, max_length=50),
    principal: CurrentPrincipal = Depends(get_current_principal),
):
    """返回当前用户保存的全部基金组合。"""

    user_id = principal.resolve_user_id(user_id)
    try:
        portfolios = portfolio_service.list(user_id=user_id)
    except PortfolioError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {"user_id": user_id, "count": len(portfolios), "portfolios": portfolios}


@router.post("", response_model=PortfolioResponse, status_code=201)
async def create_portfolio(
    request: PortfolioCreateRequest,
    principal: CurrentPrincipal = Depends(get_current_principal),
):
    """创建真实持仓组合，或兼容旧版权重组合。"""

    try:
        return portfolio_service.create(
            user_id=principal.resolve_user_id(request.user_id),
            name=request.name,
            holdings=[item.model_dump() for item in request.holdings],
        )
    except PortfolioValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except PortfolioError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.get("/{portfolio_id}", response_model=PortfolioResponse)
async def get_portfolio(
    portfolio_id: str = Path(..., pattern=r"^portfolio_[a-f0-9]{16}$"),
    user_id: str = Query(default="guest", min_length=1, max_length=50),
    principal: CurrentPrincipal = Depends(get_current_principal),
):
    """读取一份属于当前用户的基金组合。"""

    user_id = principal.resolve_user_id(user_id)
    try:
        return portfolio_service.get(user_id=user_id, portfolio_id=portfolio_id)
    except PortfolioNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PortfolioError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.put("/{portfolio_id}", response_model=PortfolioResponse)
async def update_portfolio(
    request: PortfolioUpdateRequest,
    portfolio_id: str = Path(..., pattern=r"^portfolio_[a-f0-9]{16}$"),
    principal: CurrentPrincipal = Depends(get_current_principal),
):
    """整体替换组合名称和目标持仓权重。"""

    user_id = principal.resolve_user_id(request.user_id)
    try:
        return portfolio_service.update(
            user_id=user_id,
            portfolio_id=portfolio_id,
            name=request.name,
            holdings=[item.model_dump() for item in request.holdings],
        )
    except PortfolioNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PortfolioValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except PortfolioError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.patch("/{portfolio_id}", response_model=PortfolioResponse)
async def rename_portfolio(
    request: PortfolioRenameRequest,
    portfolio_id: str = Path(..., pattern=r"^portfolio_[a-f0-9]{16}$"),
    principal: CurrentPrincipal = Depends(get_current_principal),
):
    """只修改组合名称，真实买卖流水与持仓计算保持不变。"""

    try:
        return portfolio_service.rename(
            user_id=principal.resolve_user_id(request.user_id),
            portfolio_id=portfolio_id,
            name=request.name,
        )
    except PortfolioNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PortfolioValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except PortfolioError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.get(
    "/{portfolio_id}/transactions",
    response_model=PortfolioTransactionListResponse,
)
async def list_portfolio_transactions(
    portfolio_id: str = Path(..., pattern=r"^portfolio_[a-f0-9]{16}$"),
    user_id: str = Query(default="guest", min_length=1, max_length=50),
    principal: CurrentPrincipal = Depends(get_current_principal),
):
    """读取当前用户组合的买卖流水和每笔卖出的已实现盈亏。"""

    trusted_user_id = principal.resolve_user_id(user_id)
    try:
        transactions = portfolio_service.list_transactions(
            user_id=trusted_user_id,
            portfolio_id=portfolio_id,
        )
        return {
            "portfolio_id": portfolio_id,
            "count": len(transactions),
            "transactions": transactions,
        }
    except PortfolioNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PortfolioError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.post(
    "/{portfolio_id}/transactions",
    response_model=PortfolioTransactionResponse,
    status_code=201,
)
async def create_portfolio_transaction(
    request: PortfolioTransactionCreateRequest,
    response: Response,
    portfolio_id: str = Path(..., pattern=r"^portfolio_[a-f0-9]{16}$"),
    principal: CurrentPrincipal = Depends(get_current_principal),
):
    """新增买入或卖出，并立即按移动平均成本重建持仓。"""

    try:
        result = portfolio_service.add_transaction(
            user_id=principal.resolve_user_id(request.user_id),
            portfolio_id=portfolio_id,
            idempotency_key=request.idempotency_key,
            fund_code=request.fund_code,
            transaction_type=request.transaction_type,
            trade_date=request.trade_date,
            units=request.units,
            unit_price=request.unit_price,
            fee=request.fee,
            note=request.note,
            submitted_at=request.submitted_at,
            channel=request.channel,
        )
        # 状态码保持 201，使首次请求与丢失响应后的重试拥有同一 HTTP 语义；调用方
        # 可通过响应头或响应体判断本次是否直接回放了首次结果。
        response.headers["Idempotency-Replayed"] = str(
            result["idempotency_replayed"]
        ).lower()
        return result
    except PortfolioIdempotencyConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except PortfolioNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PortfolioValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except PortfolioError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.delete(
    "/{portfolio_id}/transactions/{transaction_id}",
    response_model=PortfolioTransactionDeleteResponse,
)
async def delete_portfolio_transaction(
    portfolio_id: str = Path(..., pattern=r"^portfolio_[a-f0-9]{16}$"),
    transaction_id: str = Path(..., pattern=r"^txn_[a-f0-9]{16}$"),
    user_id: str = Query(default="guest", min_length=1, max_length=50),
    principal: CurrentPrincipal = Depends(get_current_principal),
):
    """保留旧客户端入口但拒绝物理删除，提示使用带原因的更正接口。"""

    try:
        portfolio_service.delete_transaction(
            user_id=principal.resolve_user_id(user_id),
            portfolio_id=portfolio_id,
            transaction_id=transaction_id,
        )
    except PortfolioNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PortfolioValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except PortfolioError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {
        "ok": True,
        "portfolio_id": portfolio_id,
        "transaction_id": transaction_id,
    }


def _trade_result(operation):
    """状态机接口统一映射错误，失败时不暴露数据库连接或内部堆栈。"""
    try:
        return operation()
    except PortfolioIdempotencyConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except PortfolioNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PortfolioValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except (PortfolioError, SQLAlchemyError) as exc:
        raise HTTPException(status_code=503, detail="账本暂时不可用，请保留原请求重试") from exc


@router.get("/{portfolio_id}/trade-orders")
def list_trade_orders(
    portfolio_id: str = Path(..., pattern=r"^portfolio_[a-f0-9]{16}$"),
    user_id: str = Query(default="guest", min_length=1, max_length=50),
    principal: CurrentPrincipal = Depends(get_current_principal),
):
    def load():
        orders = PortfolioTradeService(portfolio_service).list_orders(
            user_id=principal.resolve_user_id(user_id), portfolio_id=portfolio_id)
        return {"portfolio_id": portfolio_id, "count": len(orders), "orders": orders}
    return _trade_result(load)


@router.post("/{portfolio_id}/trade-orders", status_code=201)
def create_trade_order(
    request: TradeOrderCreateRequest,
    portfolio_id: str = Path(..., pattern=r"^portfolio_[a-f0-9]{16}$"),
    principal: CurrentPrincipal = Depends(get_current_principal),
):
    """登记外部申请，不实际下单，不自动判定成交或可撤单。"""
    return _trade_result(lambda: PortfolioTradeService(portfolio_service).create_order(
        portfolio_id=portfolio_id, **{**request.model_dump(), "user_id": principal.resolve_user_id(request.user_id)}))


@router.post("/{portfolio_id}/trade-orders/{order_id}/actions")
def record_trade_order_result(
    request: TradeOrderActionRequest,
    portfolio_id: str = Path(..., pattern=r"^portfolio_[a-f0-9]{16}$"),
    order_id: str = Path(..., pattern=r"^ord_[a-f0-9]{16}$"),
    principal: CurrentPrincipal = Depends(get_current_principal),
):
    """登记确认、撤单结果或赎回到账；每个动作都有幂等键和依据。"""
    return _trade_result(lambda: PortfolioTradeService(portfolio_service).apply_action(
        portfolio_id=portfolio_id, order_id=order_id,
        **{**request.model_dump(exclude_none=True), "user_id": principal.resolve_user_id(request.user_id)}))


@router.post("/{portfolio_id}/transactions/{transaction_id}/corrections", status_code=201)
def correct_portfolio_transaction(
    request: TransactionCorrectionRequest,
    portfolio_id: str = Path(..., pattern=r"^portfolio_[a-f0-9]{16}$"),
    transaction_id: str = Path(..., pattern=r"^txn_[a-f0-9]{16}$"),
    principal: CurrentPrincipal = Depends(get_current_principal),
):
    """冲正错误记录或原子更正，不删除原交易，也不撤销外部真实成交。"""
    return _trade_result(lambda: PortfolioTradeService(portfolio_service).correct_transaction(
        portfolio_id=portfolio_id, transaction_id=transaction_id,
        **{**request.model_dump(), "user_id": principal.resolve_user_id(request.user_id)}))


@router.delete("/{portfolio_id}", response_model=PortfolioDeleteResponse)
async def delete_portfolio(
    portfolio_id: str = Path(..., pattern=r"^portfolio_[a-f0-9]{16}$"),
    user_id: str = Query(default="guest", min_length=1, max_length=50),
    principal: CurrentPrincipal = Depends(get_current_principal),
):
    """只删除无记账历史的组合；账本必须保留以供核对。"""

    user_id = principal.resolve_user_id(user_id)
    try:
        # 先由锁内校验决定能否删除，避免拒绝删除账本时却已清空监控/决策记录。
        portfolio_service.delete(user_id=user_id, portfolio_id=portfolio_id)
        portfolio_monitoring_service.delete_monitoring_data(
            user_id=user_id,
            portfolio_id=portfolio_id,
            scheduler=cron_scheduler_service,
        )
        portfolio_decision_service.delete_for_portfolio(
            user_id=user_id,
            portfolio_id=portfolio_id,
        )
    except PortfolioNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PortfolioValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except PortfolioError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {"ok": True, "portfolio_id": portfolio_id}


@router.get(
    "/{portfolio_id}/monitoring",
    response_model=PortfolioMonitoringOverviewResponse,
)
async def get_portfolio_monitoring(
    portfolio_id: str = Path(..., pattern=r"^portfolio_[a-f0-9]{16}$"),
    user_id: str = Query(default="guest", min_length=1, max_length=50),
    principal: CurrentPrincipal = Depends(get_current_principal),
):
    """加载监控策略、最近两次风险快照和预警列表。"""

    user_id = principal.resolve_user_id(user_id)
    try:
        return portfolio_monitoring_service.get_overview(
            user_id=user_id,
            portfolio_id=portfolio_id,
        )
    except PortfolioNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except PortfolioError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.put(
    "/{portfolio_id}/monitoring/policy",
    response_model=PortfolioMonitoringPolicyResponse,
)
async def update_portfolio_monitoring_policy(
    request: PortfolioMonitoringPolicyUpdateRequest,
    portfolio_id: str = Path(..., pattern=r"^portfolio_[a-f0-9]{16}$"),
    principal: CurrentPrincipal = Depends(get_current_principal),
):
    """保存策略，并创建、更新或取消该组合对应的持久化 Cron。"""

    user_id = principal.resolve_user_id(request.user_id)
    try:
        policy = portfolio_monitoring_service.upsert_policy(
            user_id=user_id,
            portfolio_id=portfolio_id,
            enabled=request.enabled,
            risk_preference=request.risk_preference,
            investment_horizon=request.investment_horizon,
            monitor_months=request.monitor_months,
            max_single_adjustment_pct=request.max_single_adjustment_pct,
            drawdown_alert_pct_points=request.drawdown_alert_pct_points,
            concentration_alert_pct_points=(
                request.concentration_alert_pct_points
            ),
            major_event_heat_threshold=request.major_event_heat_threshold,
            event_impact_exposure_threshold_pct=(
                request.event_impact_exposure_threshold_pct
            ),
            alert_on_risk_state_worsening=(
                request.alert_on_risk_state_worsening
            ),
            schedule_cron=request.schedule_cron,
        )
        portfolio_monitoring_service.sync_schedule(
            policy=policy,
            scheduler=cron_scheduler_service,
        )
        return policy
    except PortfolioNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except (PortfolioError, RuntimeError) as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.post(
    "/{portfolio_id}/monitoring/runs",
    response_model=BackgroundJobResponse,
    status_code=202,
)
async def start_portfolio_monitoring_run(
    request: PortfolioMonitoringRunRequest,
    portfolio_id: str = Path(..., pattern=r"^portfolio_[a-f0-9]{16}$"),
    principal: CurrentPrincipal = Depends(get_current_principal),
):
    """立即创建一次监控 Job；请求返回后由 Worker 更新快照与预警。"""

    user_id = principal.resolve_user_id(request.user_id)
    try:
        portfolio = portfolio_service.get(
            user_id=user_id,
            portfolio_id=portfolio_id,
        )
        policy = portfolio_monitoring_service.get_policy(
            user_id=user_id,
            portfolio_id=portfolio_id,
        )
        # 授权关系写入是同步网络请求，交给线程池，不占住整个 API 事件循环。
        return await asyncio.to_thread(background_job_service.start_job,
            user_id=user_id,
            title=f"{portfolio['name']}风险监控",
            job_type="portfolio_monitor",
            idempotency_key=request.idempotency_key,
            portfolio_id=portfolio_id,
            portfolio_months=policy["monitor_months"],
        )
    except PortfolioNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except (PortfolioError, RuntimeError) as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.post(
    "/{portfolio_id}/monitoring/alerts/{alert_id}/acknowledge",
    response_model=PortfolioAlertResponse,
)
async def acknowledge_portfolio_alert(
    request: PortfolioAlertActionRequest,
    portfolio_id: str = Path(..., pattern=r"^portfolio_[a-f0-9]{16}$"),
    alert_id: str = Path(..., pattern=r"^alert_[a-f0-9]{16}$"),
    principal: CurrentPrincipal = Depends(get_current_principal),
):
    """把一条预警标记为已读；不会修改历史快照或删除预警。"""

    try:
        return portfolio_monitoring_service.acknowledge_alert(
            user_id=principal.resolve_user_id(request.user_id),
            portfolio_id=portfolio_id,
            alert_id=alert_id,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=exc.args[0]) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get(
    "/{portfolio_id}/assessment",
    response_model=PortfolioAssessmentResponse,
)
async def get_portfolio_assessment(
    portfolio_id: str = Path(..., pattern=r"^portfolio_[a-f0-9]{16}$"),
    user_id: str = Query(default="guest", min_length=1, max_length=50),
    months: PortfolioAssessmentMonths = Query(
        default=PortfolioAssessmentMonths.ONE_YEAR
    ),
    principal: CurrentPrincipal = Depends(get_current_principal),
):
    """返回组合集中度、基金风险贡献和跨基金去重证据。"""

    user_id = principal.resolve_user_id(user_id)
    try:
        return portfolio_assessment_service.assess(
            user_id=user_id,
            portfolio_id=portfolio_id,
            months=int(months),
        )
    except PortfolioNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (PortfolioError, PortfolioAssessmentError) as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.get(
    "/{portfolio_id}/performance",
    response_model=PortfolioPerformanceResponse,
)
async def get_portfolio_performance(
    portfolio_id: str = Path(..., pattern=r"^portfolio_[a-f0-9]{16}$"),
    user_id: str = Query(default="guest", min_length=1, max_length=50),
    months: PortfolioAssessmentMonths = Query(
        default=PortfolioAssessmentMonths.ONE_YEAR
    ),
    principal: CurrentPrincipal = Depends(get_current_principal),
):
    """返回交易调整后的 TWR/XIRR、基准、回撤曲线和基金收益归因。"""

    user_id = principal.resolve_user_id(user_id)
    try:
        return portfolio_performance_service.analyze(
            user_id=user_id,
            portfolio_id=portfolio_id,
            months=int(months),
        )
    except PortfolioNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (PortfolioError, PortfolioPerformanceError) as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.get(
    "/{portfolio_id}/decisions",
    response_model=PortfolioDecisionListResponse,
)
async def list_portfolio_decisions(
    portfolio_id: str = Path(..., pattern=r"^portfolio_[a-f0-9]{16}$"),
    user_id: str = Query(default="guest", min_length=1, max_length=50),
    principal: CurrentPrincipal = Depends(get_current_principal),
):
    """读取历史决策快照，并用最新业绩曲线更新每条决策的复盘结果。"""

    user_id = principal.resolve_user_id(user_id)
    try:
        decisions = portfolio_decision_service.list(
            user_id=user_id,
            portfolio_id=portfolio_id,
        )
        return {
            "portfolio_id": portfolio_id,
            "count": len(decisions),
            "decisions": decisions,
        }
    except PortfolioNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (PortfolioError, PortfolioPerformanceError) as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.post(
    "/{portfolio_id}/decisions",
    response_model=PortfolioDecisionResponse,
    status_code=201,
)
async def create_portfolio_decision(
    request: PortfolioDecisionCreateRequest,
    portfolio_id: str = Path(..., pattern=r"^portfolio_[a-f0-9]{16}$"),
    principal: CurrentPrincipal = Depends(get_current_principal),
):
    """保存决策，并冻结当前指标、规则证据和可选研究报告摘要。"""

    user_id = principal.resolve_user_id(request.user_id)
    research_report = None
    try:
        if request.research_job_id:
            source_job = background_job_service.get_job(
                user_id=user_id,
                job_id=request.research_job_id,
            )["job"]
            if source_job["job_type"] != "portfolio_research":
                raise PortfolioDecisionError("引用的 Job 不是组合研究报告")
            if source_job["status"] != "completed" or not source_job.get("result"):
                raise PortfolioDecisionError("引用的组合研究报告尚未完成")
            if source_job["result"].get("portfolio_id") != portfolio_id:
                raise PortfolioDecisionError("研究报告与当前组合不匹配")
            research_report = source_job["result"]

        return portfolio_decision_service.create(
            user_id=user_id,
            portfolio_id=portfolio_id,
            action=request.action,
            thesis=request.thesis,
            horizon_days=request.horizon_days,
            research_job_id=request.research_job_id,
            research_report=research_report,
        )
    except PortfolioNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=exc.args[0]) from exc
    except PortfolioDecisionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except (
        PortfolioError,
        PortfolioPerformanceError,
        PortfolioAssessmentError,
    ) as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.patch(
    "/{portfolio_id}/decisions/{decision_id}",
    response_model=PortfolioDecisionResponse,
)
# 微信 wx.request 不支持 PATCH；别名复用同一函数、鉴权和不可变决策快照规则。
@router.post(
    "/{portfolio_id}/decisions/{decision_id}/review",
    response_model=PortfolioDecisionResponse,
)
async def review_portfolio_decision(
    request: PortfolioDecisionReviewRequest,
    portfolio_id: str = Path(..., pattern=r"^portfolio_[a-f0-9]{16}$"),
    decision_id: str = Path(..., pattern=r"^decision_[a-f0-9]{16}$"),
    principal: CurrentPrincipal = Depends(get_current_principal),
):
    """写入人工复盘结论，原始理由和决策快照保持不变。"""

    user_id = principal.resolve_user_id(request.user_id)
    try:
        return portfolio_decision_service.review(
            user_id=user_id,
            portfolio_id=portfolio_id,
            decision_id=decision_id,
            review_note=request.review_note,
        )
    except PortfolioDecisionError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (PortfolioError, PortfolioPerformanceError) as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.delete(
    "/{portfolio_id}/decisions/{decision_id}",
    response_model=PortfolioDecisionDeleteResponse,
)
async def delete_portfolio_decision(
    portfolio_id: str = Path(..., pattern=r"^portfolio_[a-f0-9]{16}$"),
    decision_id: str = Path(..., pattern=r"^decision_[a-f0-9]{16}$"),
    user_id: str = Query(default="guest", min_length=1, max_length=50),
    principal: CurrentPrincipal = Depends(get_current_principal),
):
    """删除一条误录决策，不删除组合研究报告。"""

    user_id = principal.resolve_user_id(user_id)
    try:
        portfolio_decision_service.delete(
            user_id=user_id,
            portfolio_id=portfolio_id,
            decision_id=decision_id,
        )
    except PortfolioDecisionError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {
        "ok": True,
        "portfolio_id": portfolio_id,
        "decision_id": decision_id,
    }


@router.get(
    "/{portfolio_id}/exposure",
    response_model=PortfolioExposureResponse,
)
async def get_portfolio_exposure(
    portfolio_id: str = Path(..., pattern=r"^portfolio_[a-f0-9]{16}$"),
    user_id: str = Query(default="guest", min_length=1, max_length=50),
    principal: CurrentPrincipal = Depends(get_current_principal),
):
    """按当前市值权重计算底层证券、行业/固收暴露和基金重合度。"""

    user_id = principal.resolve_user_id(user_id)
    try:
        return portfolio_exposure_service.analyze(
            user_id=user_id,
            portfolio_id=portfolio_id,
        )
    except PortfolioNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (PortfolioError, PortfolioExposureError) as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.get(
    "/{portfolio_id}/event-impacts",
    response_model=PortfolioEventImpactResponse,
)
async def get_portfolio_event_impacts(
    portfolio_id: str = Path(..., pattern=r"^portfolio_[a-f0-9]{16}$"),
    user_id: str = Query(default="guest", min_length=1, max_length=50),
    limit: int = Query(default=12, ge=1, le=30),
    principal: CurrentPrincipal = Depends(get_current_principal),
):
    """把有效市场事件映射到底层证券、细分行业或直接关联基金。"""

    user_id = principal.resolve_user_id(user_id)
    try:
        return portfolio_event_impact_service.analyze(
            user_id=user_id,
            portfolio_id=portfolio_id,
            limit=limit,
        )
    except PortfolioNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (PortfolioError, PortfolioEventImpactError) as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.post(
    "/{portfolio_id}/research-runs",
    response_model=BackgroundJobResponse,
    status_code=202,
)
async def start_portfolio_research(
    request: PortfolioResearchStartRequest,
    portfolio_id: str = Path(..., pattern=r"^portfolio_[a-f0-9]{16}$"),
    principal: CurrentPrincipal = Depends(get_current_principal),
):
    """创建后台 Job，由 Worker 按研究模式执行 LangGraph 组合研究图。"""

    user_id = principal.resolve_user_id(request.user_id)
    try:
        # 在 HTTP 请求阶段先确认组合存在且属于当前用户，避免返回一个注定失败的 Job。
        portfolio = portfolio_service.get(
            user_id=user_id,
            portfolio_id=portfolio_id,
        )
        return await asyncio.to_thread(background_job_service.start_job,
            user_id=user_id,
            title=f"{portfolio['name']}组合研究",
            job_type="portfolio_research",
            idempotency_key=request.idempotency_key,
            portfolio_id=portfolio_id,
            portfolio_months=request.months,
            research_mode=request.research_mode,
            research_question=request.question,
            research_allow_public_refresh=request.allow_public_refresh,
            research_investor_context=request.investor_context,
        )
    except PortfolioNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except (PortfolioError, RuntimeError) as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.get(
    "/{portfolio_id}/research-runs",
    response_model=PortfolioResearchHistoryResponse,
)
async def list_portfolio_research_runs(
    portfolio_id: str = Path(..., pattern=r"^portfolio_[a-f0-9]{16}$"),
    user_id: str = Query(default="guest", min_length=1, max_length=50),
    months: PortfolioAssessmentMonths | None = Query(default=None),
    limit: int = Query(default=10, ge=1, le=20),
    principal: CurrentPrincipal = Depends(get_current_principal),
):
    """列出已完成研究；最近报告可让页面刷新后恢复，而不是只存在浏览器内存。"""

    user_id = principal.resolve_user_id(user_id)
    try:
        # 先验证组合归属，避免一个空历史结果掩盖“组合不存在”或跨用户访问。
        portfolio_service.get(user_id=user_id, portfolio_id=portfolio_id)
        return await asyncio.to_thread(
            portfolio_research_audit_service.list_runs,
            user_id=user_id,
            portfolio_id=portfolio_id,
            months=int(months) if months is not None else None,
            limit=limit,
        )
    except PortfolioNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PortfolioResearchAuditValidationError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except PortfolioError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.get("/{portfolio_id}/review")
async def read_portfolio_review(
    portfolio_id: str = Path(..., pattern=r"^portfolio_[a-f0-9]{16}$"),
    user_id: str = Query(default="guest", min_length=1, max_length=50),
    months: PortfolioAssessmentMonths = Query(default=PortfolioAssessmentMonths.ONE_YEAR),
    principal: CurrentPrincipal = Depends(get_current_principal),
):
    """两个前端共享的收益与风险汇总。读取不会启动后台任务。"""
    try:
        return await asyncio.to_thread(portfolio_review_service.read,
            user_id=principal.resolve_user_id(user_id), portfolio_id=portfolio_id, months=int(months))
    except PortfolioNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (PortfolioError, RuntimeError) as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.get("/{portfolio_id}/research-runs/{job_id}")
async def read_portfolio_research_report(
    portfolio_id: str = Path(..., pattern=r"^portfolio_[a-f0-9]{16}$"),
    job_id: str = Path(..., min_length=1, max_length=64),
    user_id: str = Query(default="guest", min_length=1, max_length=50),
    principal: CurrentPrincipal = Depends(get_current_principal),
):
    """按明确报告编号读取，不把用户选择的旧报告替换为最新一份。"""
    user_id = principal.resolve_user_id(user_id)
    try:
        portfolio_service.get(user_id=user_id, portfolio_id=portfolio_id)
        return await asyncio.to_thread(portfolio_research_audit_service.read_report,
            user_id=user_id, portfolio_id=portfolio_id, job_id=job_id)
    except (PortfolioNotFoundError, PortfolioResearchAuditNotFoundError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PortfolioResearchAuditValidationError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get(
    "/{portfolio_id}/research-runs/{job_id}/comparison",
    response_model=PortfolioResearchChangeAuditResponse,
)
async def compare_portfolio_research_runs(
    portfolio_id: str = Path(..., pattern=r"^portfolio_[a-f0-9]{16}$"),
    job_id: str = Path(..., min_length=1, max_length=64),
    user_id: str = Query(default="guest", min_length=1, max_length=50),
    base_job_id: str | None = Query(default=None, min_length=1, max_length=64),
    principal: CurrentPrincipal = Depends(get_current_principal),
):
    """把目标报告与较早同区间报告比较；未指定基线时自动使用最近一份。"""

    user_id = principal.resolve_user_id(user_id)
    try:
        portfolio_service.get(user_id=user_id, portfolio_id=portfolio_id)
        # 模型解释是同步 SDK 调用，放进线程避免阻塞 FastAPI 的事件循环。
        return await asyncio.to_thread(
            portfolio_research_audit_service.compare,
            user_id=user_id,
            portfolio_id=portfolio_id,
            target_job_id=job_id,
            base_job_id=base_job_id,
        )
    except (PortfolioNotFoundError, PortfolioResearchAuditNotFoundError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PortfolioResearchComparisonUnavailableError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except PortfolioResearchAuditValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except PortfolioError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.post(
    "/{portfolio_id}/adjustment-runs",
    response_model=BackgroundJobResponse,
    status_code=202,
)
async def start_portfolio_adjustment(
    request: PortfolioAdjustmentStartRequest,
    portfolio_id: str = Path(..., pattern=r"^portfolio_[a-f0-9]{16}$"),
    principal: CurrentPrincipal = Depends(get_current_principal),
):
    """基于已完成研究报告，启动会停在人工确认节点的调整流程。"""

    user_id = principal.resolve_user_id(request.user_id)
    try:
        portfolio = portfolio_service.get(
            user_id=user_id,
            portfolio_id=portfolio_id,
        )
        source_job = background_job_service.get_job(
            user_id=user_id,
            job_id=request.source_research_job_id,
        )["job"]
        if source_job["job_type"] != "portfolio_research":
            raise ValueError("source_research_job_id 不是组合研究 Job。")
        if source_job["status"] != "completed" or not source_job.get("result"):
            raise ValueError("请先完成组合研究，再生成调整方案。")
        if source_job["result"].get("portfolio_id") != portfolio_id:
            raise ValueError("研究报告与当前组合不匹配。")

        return await asyncio.to_thread(background_job_service.start_job,
            user_id=user_id,
            title=f"{portfolio['name']}组合调整方案",
            job_type="portfolio_adjustment",
            idempotency_key=request.idempotency_key,
            portfolio_id=portfolio_id,
            source_research_job_id=request.source_research_job_id,
        )
    except PortfolioNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=exc.args[0]) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except (PortfolioError, RuntimeError) as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.post(
    "/{portfolio_id}/adjustment-runs/{job_id}/resume",
    response_model=BackgroundJobResponse,
    status_code=202,
)
async def resume_portfolio_adjustment(
    request: PortfolioAdjustmentResumeRequest,
    portfolio_id: str = Path(..., pattern=r"^portfolio_[a-f0-9]{16}$"),
    job_id: str = Path(..., pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$"),
    principal: CurrentPrincipal = Depends(get_current_principal),
):
    """保存人工约束，并用 Command(resume=...) 恢复同一个 LangGraph。"""

    user_id = principal.resolve_user_id(request.user_id)
    try:
        portfolio_service.get(
            user_id=user_id,
            portfolio_id=portfolio_id,
        )
        current_job = background_job_service.get_job(
            user_id=user_id,
            job_id=job_id,
        )["job"]
        if current_job["job_type"] != "portfolio_adjustment":
            raise ValueError("当前 Job 不是组合调整流程。")
        waiting_result = current_job.get("result") or {}
        if waiting_result.get("portfolio_id") != portfolio_id:
            raise ValueError("组合调整 Job 与当前组合不匹配。")

        return background_job_service.resume_portfolio_adjustment(
            user_id=user_id,
            job_id=job_id,
            decision=request.decision,
            risk_preference=request.risk_preference,
            investment_horizon=request.investment_horizon,
            max_single_adjustment_pct=request.max_single_adjustment_pct,
            max_single_fund_weight_pct=request.max_single_fund_weight_pct,
            max_turnover_pct=request.max_turnover_pct,
            additional_constraints=request.additional_constraints,
        )
    except PortfolioNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=exc.args[0]) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except (PortfolioError, RuntimeError) as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
