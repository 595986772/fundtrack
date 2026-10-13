"""FundScope 市场环境与可追溯事件 API。"""

from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse

from app.schemas import (
    BackgroundJobResponse,
    LargeCapitalOverviewResponse,
    LargeCapitalStatusResponse,
    LargeCapitalSyncRequest,
    MarketDataSyncRequest,
    MarketDataSyncStatusResponse,
    MarketEventDataSyncRequest,
    MarketEventDataSyncStatusResponse,
    MarketEventAnalysisRequest,
    MarketEventClusterRequest,
    MarketHotspotLifecycle,
    MarketHotspotListResponse,
    MarketHotspotResponse,
    MarketIntelligencePipelineRequest,
    MarketIndexOverviewResponse,
    MarketIntradayOverviewResponse,
    MarketTurnoverOverviewResponse,
    MarketTurnoverHistoryResponse,
    MarketDailyTurnoverResponse,
    MarketEventListResponse,
    MarketTopicCatalogResponse,
    MarketEventType,
    MarketSentimentResponse,
    RetailIndexOverviewResponse,
    MarketSourceLevel,
    SocialSignalOverviewResponse,
    SocialSignalRefreshRequest,
    SocialSignalStatusResponse,
    SocialSignalEvidencePostResponse,
    SocialSignalAnalysisRequest,
)
from app.services.background_jobs import background_job_service
from app.services.auth import CurrentPrincipal, get_current_principal
from app.services.market_data_sync import market_snapshot_sync_service
from app.services.large_capital_flows import (
    LargeCapitalError,
    large_capital_analysis_service,
    large_capital_sync_service,
)
from app.services.market_event_sync import market_event_sync_service
from app.services.market_events import MarketEventError, market_event_service
from app.services.market_hotspots import (
    MarketHotspotError,
    market_hotspot_tracking_service,
)
from app.services.market_indices import (
    MarketIndexError,
    market_index_analysis_service,
)
from app.services.market_topic_keywords import market_topic_keyword_service
from app.services.market_sentiment import (
    MarketSentimentError,
    market_sentiment_service,
)
from app.services.social_signals import SocialSignalError, social_signal_service
from app.services.retail_indices import retail_index_service
from app.services.market_intraday import market_intraday_service
from app.services.market_turnover import market_turnover_service
from app.services.market_turnover_history import market_turnover_history_service
from app.services.market_daily_turnover import market_daily_turnover_service


router = APIRouter(prefix="/api/v1/market", tags=["market"])


@router.get("/turnover", response_model=MarketTurnoverOverviewResponse)
def get_market_turnover():
    """只读取全市场汇总快照，打开页面不触发全量采集。"""
    return market_turnover_service.overview()


@router.get("/turnover/history", response_model=MarketTurnoverHistoryResponse)
def get_market_turnover_history(series_id: str = "market:sh_sz", data_date: date | None = None):
    """切换曲线和日期只查库，不隐式触发市场采集。"""
    try:
        return market_turnover_history_service.history(series_id, data_date)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/turnover/daily", response_model=MarketDailyTurnoverResponse)
def get_market_daily_turnover(series_id: str = "reference:sh_sz", period: str = "3m"):
    """切换日线市场/周期只读取缓存，不调用行情Provider。"""
    try:
        return market_daily_turnover_service.overview(series_id, period)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/turnover/daily/refresh", response_model=MarketDailyTurnoverResponse)
def refresh_market_daily_turnover(series_id: str = "reference:sh_sz", period: str = "3m",
                                  principal: CurrentPrincipal = Depends(get_current_principal)):
    """仅手动刷新选中序列的历史日线，独立于上方盘中成交快照。"""
    try:
        return market_daily_turnover_service.refresh(series_id, period)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/turnover/refresh", response_model=MarketTurnoverOverviewResponse)
def refresh_market_turnover(principal: CurrentPrincipal = Depends(get_current_principal)):
    """手动全量采集，独立节流；失败状态随旧快照一起返回。"""
    return market_turnover_service.refresh()


@router.get("/intraday", response_model=MarketIntradayOverviewResponse)
def get_market_intraday():
    """只读已保存的盘中快照，导航和刷新网页不发起外部采集。"""
    return market_intraday_service.overview()


@router.post("/intraday/refresh", response_model=MarketIntradayOverviewResponse)
def refresh_market_intraday(principal: CurrentPrincipal = Depends(get_current_principal)):
    """显式点击才请求配置指数；同步函数在线程池执行，不阻塞Agent流式输出。"""
    return market_intraday_service.refresh()


@router.get("/retail-indices", response_model=RetailIndexOverviewResponse)
def get_retail_indices():
    """只读本地快照，打开页面不会自动抓取平台或调用付费模型。"""
    return retail_index_service.overview()


@router.post("/retail-indices/refresh", response_model=RetailIndexOverviewResponse)
def refresh_retail_indices(principal: CurrentPrincipal = Depends(get_current_principal)):
    """显式更新一个公开指数文件；同步函数在线程池执行，不阻塞异步聊天。"""
    return retail_index_service.refresh()


@router.get("/sentiment", response_model=MarketSentimentResponse)
async def get_market_sentiment():
    """返回最新可用快照对应的趋势、波动、宽度和成交活跃度。"""

    try:
        return market_sentiment_service.analyze()
    except MarketSentimentError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.get("/indices", response_model=MarketIndexOverviewResponse)
async def get_market_indices():
    """返回已同步宽基指数的趋势、波动、回撤和相对强弱。"""

    try:
        return market_index_analysis_service.analyze()
    except MarketIndexError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.get("/data-status", response_model=MarketDataSyncStatusResponse)
async def get_market_data_status():
    """查看市场快照覆盖日期、最后同步时间和每日 Cron 配置。"""

    return market_snapshot_sync_service.status()


@router.post("/data-sync", response_model=BackgroundJobResponse)
async def start_market_data_sync(
    request: MarketDataSyncRequest,
    principal: CurrentPrincipal = Depends(get_current_principal),
):
    """创建市场同步 Background Job，HTTP 请求不等待外部抓取。"""

    try:
        return background_job_service.start_job(
            user_id=principal.resolve_user_id(request.user_id),
            title="同步 FundScope 每日市场状态",
            job_type="market_snapshot_sync",
            max_retries=1,
            timeout_seconds=180,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.get("/large-capital/status", response_model=LargeCapitalStatusResponse)
async def get_large_capital_status():
    """返回 ETF 份额和龙虎榜机构席位的数据库覆盖情况。"""

    return large_capital_sync_service.status()


@router.get("/large-capital", response_model=LargeCapitalOverviewResponse)
async def get_large_capital_overview(
    fund_code: Annotated[
        str | None,
        Query(pattern=r"^\d{6}$", description="关联当前选中基金的披露持仓"),
    ] = None,
    portfolio_id: Annotated[
        str | None,
        Query(
            pattern=r"^portfolio_[a-f0-9]{16}$",
            description="关联当前用户选中的基金组合",
        ),
    ] = None,
    user_id: Annotated[str, Query(min_length=1, max_length=50)] = "chen",
    principal: CurrentPrincipal = Depends(get_current_principal),
):
    """读取已保存资金线索，并按需关联当前基金或组合。"""

    try:
        return large_capital_analysis_service.overview(
            user_id=principal.resolve_user_id(user_id),
            fund_code=fund_code,
            portfolio_id=portfolio_id,
        )
    except LargeCapitalError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.post("/large-capital/data-sync", response_model=BackgroundJobResponse)
async def start_large_capital_sync(
    request: LargeCapitalSyncRequest,
    principal: CurrentPrincipal = Depends(get_current_principal),
):
    """提交大资金同步 Job；首次运行会在后台补齐上交所历史份额。"""

    try:
        return background_job_service.start_job(
            user_id=principal.resolve_user_id(request.user_id),
            title="更新 FundScope 大资金行为线索",
            job_type="large_capital_sync",
            idempotency_key=request.idempotency_key,
            max_retries=1,
            timeout_seconds=420,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.get(
    "/events/data-status",
    response_model=MarketEventDataSyncStatusResponse,
)
async def get_market_event_data_status():
    """查看事件数量、日期覆盖范围和当前手动/自动刷新配置。"""

    return market_event_sync_service.status()


@router.get(
    "/events/topics",
    response_model=MarketTopicCatalogResponse,
)
async def get_market_event_topics():
    """返回采集阶段可选的板块词典，以及当前有效的模型扩展缓存。"""

    return market_topic_keyword_service.catalog()


@router.post("/events/data-sync", response_model=BackgroundJobResponse)
async def start_market_event_data_sync(
    request: MarketEventDataSyncRequest,
    principal: CurrentPrincipal = Depends(get_current_principal),
):
    """创建事件采集 Background Job，HTTP 请求立即返回 Job ID。"""

    try:
        topic_suffix = f"（{'、'.join(request.topics)}）" if request.topics else ""
        return background_job_service.start_job(
            user_id=principal.resolve_user_id(request.user_id),
            title=f"同步 FundScope 每日市场事件{topic_suffix}",
            job_type="market_event_sync",
            max_retries=1,
            timeout_seconds=300,
            market_topics=request.topics,
            use_model_topic_expansion=request.use_model_topic_expansion,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.post("/events/analyze", response_model=BackgroundJobResponse)
async def start_market_event_analysis(
    request: MarketEventAnalysisRequest,
    principal: CurrentPrincipal = Depends(get_current_principal),
):
    """创建 DeepSeek 事件语义分析 Job；只处理当前待分析批次。"""

    try:
        return background_job_service.start_job(
            user_id=principal.resolve_user_id(request.user_id),
            title="分析 FundScope 待处理市场事件",
            idempotency_key=request.idempotency_key,
            job_type="market_event_analysis",
            max_retries=1,
            timeout_seconds=180,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.post("/events/cluster", response_model=BackgroundJobResponse)
async def start_market_event_clustering(
    request: MarketEventClusterRequest,
    principal: CurrentPrincipal = Depends(get_current_principal),
):
    """创建语义聚类 Job；Embedding 有缓存时只计算新增或变化的报道。"""

    try:
        return background_job_service.start_job(
            user_id=principal.resolve_user_id(request.user_id),
            title="聚合 FundScope 多来源市场报道",
            idempotency_key=request.idempotency_key,
            job_type="market_event_clustering",
            max_retries=1,
            timeout_seconds=180,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.post("/events/pipeline", response_model=BackgroundJobResponse)
async def start_market_intelligence_pipeline(
    request: MarketIntelligencePipelineRequest,
    principal: CurrentPrincipal = Depends(get_current_principal),
):
    """提交完整情报流水线；采集、模型处理和组合监控都在 Worker 中执行。"""

    try:
        topic_suffix = f"（{'、'.join(request.topics)}）" if request.topics else ""
        return background_job_service.start_job(
            user_id=principal.resolve_user_id(request.user_id),
            title=f"更新 FundScope 市场情报并检查组合{topic_suffix}",
            job_type="market_intelligence_pipeline",
            idempotency_key=request.idempotency_key,
            max_retries=2,
            timeout_seconds=900,
            market_topics=request.topics,
            use_model_topic_expansion=request.use_model_topic_expansion,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.get("/hotspots", response_model=MarketHotspotListResponse)
async def get_market_hotspots(
    q: Annotated[str | None, Query(max_length=100)] = None,
    fund_code: Annotated[
        str | None,
        Query(pattern=r"^\d{6}$", description="只看与当前基金相关的热点"),
    ] = None,
    lifecycle: MarketHotspotLifecycle | None = None,
    history_days: Annotated[int, Query(ge=7, le=90)] = 30,
    limit: Annotated[int, Query(ge=1, le=30)] = 12,
):
    """读取最近一次快照，并比较历史热度得到热点生命周期。"""

    try:
        return market_hotspot_tracking_service.overview(
            query=q,
            fund_code=fund_code,
            lifecycle=lifecycle,
            history_days=history_days,
            limit=limit,
        )
    except MarketHotspotError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/hotspots/{cluster_id}", response_model=MarketHotspotResponse)
async def get_market_hotspot_detail(
    cluster_id: str,
    history_days: Annotated[int, Query(ge=7, le=90)] = 30,
):
    """读取一个当前热点及其最近 7 到 90 天的快照时间线。"""

    try:
        return market_hotspot_tracking_service.detail(
            cluster_id=cluster_id,
            history_days=history_days,
        )
    except MarketHotspotError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get(
    "/social-signals/status",
    response_model=SocialSignalStatusResponse,
)
async def get_social_signal_status():
    """查看社交数据 Provider 是否就绪，以及当前数据库覆盖范围。"""

    return social_signal_service.status()


@router.get(
    "/social-signals",
    response_model=SocialSignalOverviewResponse,
)
async def get_social_signal_overview():
    """只读已保存帖子，返回作者去重后的基金/主题仓位观点。"""

    try:
        return social_signal_service.overview()
    except SocialSignalError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.get("/social-signals/posts")
async def list_social_posts(
    scope: Annotated[str, Query(pattern="^(window|all)$")] = "window",
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=30)] = 10,
):
    """分页读取全部已保存样本；不会触发采集或模型调用。"""
    since = social_signal_service.freshness_service.resolve().window_start if scope == "window" else None
    from app.services.social_signal_report import SocialSignalReportService
    from datetime import datetime, timezone
    result = social_signal_service.repository.list_posts(since=since, page=page, page_size=page_size)
    rows = social_signal_service.repository.recent_analyzed(since=since or datetime.min.replace(tzinfo=timezone.utc), limit=200)
    target = social_signal_service.freshness_service.resolve().target_trade_date if scope == "window" else "历史样本"
    result["review"] = SocialSignalReportService(social_signal_service.repository, scope=scope).latest(rows, target)
    return result


@router.get("/social-signals/posts/{post_id}", response_model=SocialSignalEvidencePostResponse)
async def get_social_post(post_id: str):
    post = social_signal_service.repository.get_post(post_id)
    if post is None:
        raise HTTPException(status_code=404, detail="帖子不存在")
    return post


@router.get("/social-signals/images/{media_id}")
async def get_social_image(media_id: str):
    """只返回数据库登记过的本地图片，不接受 URL 或文件路径。"""
    path = social_signal_service.repository.media.path_for_id(media_id)
    if path is None or not path.is_file():
        raise HTTPException(status_code=404, detail="本地图片不存在")
    return FileResponse(path, media_type="image/png", headers={"Cache-Control": "private, max-age=3600"})


@router.post("/social-signals/analysis", response_model=BackgroundJobResponse)
async def start_social_analysis(request: SocialSignalAnalysisRequest,
                                principal: CurrentPrincipal = Depends(get_current_principal)):
    """沿用持久化 Job 和现有登录鉴权，只处理数据库与本地图片。"""
    try:
        return background_job_service.start_job(user_id=principal.resolve_user_id(request.user_id),
            title="分析已保存的社区观点", job_type="social_signal_analysis", social_scope=request.scope,
            max_retries=0, timeout_seconds=600, idempotency_key=request.idempotency_key)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.post(
    "/social-signals/browser-session",
    response_model=BackgroundJobResponse,
)
async def start_social_signal_browser_login(
    request: SocialSignalRefreshRequest,
    principal: CurrentPrincipal = Depends(get_current_principal),
):
    """打开本机独立 Chrome，让用户手动建立小红书登录会话。"""

    try:
        return background_job_service.start_job(
            user_id=principal.resolve_user_id(request.user_id),
            title="连接 FundScope 小红书公开页面",
            job_type="social_signal_browser_login",
            idempotency_key=request.idempotency_key,
            # 用户交互任务失败后不能自动重试，否则会再次弹出浏览器窗口。
            max_retries=0,
            timeout_seconds=360,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.post(
    "/social-signals/data-sync",
    response_model=BackgroundJobResponse,
)
async def start_social_signal_refresh(
    request: SocialSignalRefreshRequest,
    principal: CurrentPrincipal = Depends(get_current_principal),
):
    """单独刷新小红书观点；完整市场情报按钮也会自动执行同一服务。"""

    try:
        return background_job_service.start_job(
            user_id=principal.resolve_user_id(request.user_id),
            title="刷新 FundScope 社交投资观点",
            job_type="social_signal_sync",
            idempotency_key=request.idempotency_key,
            # 第三方要求验证时不能自动重试浏览器采集。
            max_retries=0,
            timeout_seconds=600,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.get("/events", response_model=MarketEventListResponse)
async def search_market_events(
    q: Annotated[str | None, Query(max_length=100)] = None,
    fund_code: Annotated[
        str | None,
        Query(pattern=r"^\d{6}$", description="只看与某只样例基金相关的事件"),
    ] = None,
    event_type: MarketEventType | None = None,
    source_level: MarketSourceLevel | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    limit: Annotated[int, Query(ge=1, le=50)] = 20,
):
    """按关键词、基金、类型、来源等级或日期范围查询最新可用事件。"""

    if date_from and date_to and date_from > date_to:
        raise HTTPException(status_code=400, detail="date_from 不能晚于 date_to")

    try:
        return market_event_service.search(
            query=q,
            fund_code=fund_code,
            event_type=event_type,
            source_level=source_level,
            date_from=date_from,
            date_to=date_to,
            limit=limit,
        )
    except MarketEventError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
