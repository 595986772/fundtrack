"""Pydantic 数据模型。

FastAPI 会用这些模型做三件事：
1. 校验请求参数是否合法；
2. 约束接口返回的数据结构；
3. 自动生成 `/docs` 里的接口文档。
"""

from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Any, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field


class HealthResponse(BaseModel):
    """健康检查接口的响应结构。"""

    status: str
    app_name: str

    # `| None` 表示这个字段可以是字符串，也可以是 None。
    agent_model: str | None = None


class AuthRegisterRequest(BaseModel):
    """注册本地 FundScope 账号。"""

    user_id: str = Field(pattern=r"^[a-zA-Z0-9_-]{3,32}$")
    display_name: str = Field(min_length=1, max_length=80)
    password: str = Field(min_length=8, max_length=128)


class AuthLoginRequest(BaseModel):
    """使用账号和密码创建服务端会话。"""

    user_id: str = Field(min_length=1, max_length=50)
    password: str = Field(min_length=1, max_length=128)


class AuthUserInfo(BaseModel):
    """前端可见的账号资料，不包含密码哈希或 Session Token。"""

    user_id: str
    display_name: str
    role: Literal["user", "admin"]


class AuthStateResponse(BaseModel):
    """当前浏览器的认证状态。"""

    authenticated: bool
    auth_required: bool
    user: AuthUserInfo | None = None


class AuthTokenResponse(AuthStateResponse):
    """供小程序使用的会话；原始令牌只在登录成功时返回一次。"""

    access_token: str
    token_type: Literal["bearer"] = "bearer"
    expires_in: int


class FundNavPointResponse(BaseModel):
    """净值曲线上的一个日期点。"""

    nav_date: date
    unit_nav: float
    daily_growth_rate_pct: float | None = None


FundReturnBasis = Literal[
    "total_return_reinvested",
    "provider_reported_daily_return",
    "unit_nav_unadjusted",
]
FundReturnBasisStatus = Literal["verified", "provider_reported", "limited"]
FundBenchmarkStatus = Literal[
    "matched",
    "proxy",
    "unavailable",
    "insufficient",
]


class FundCorporateActionResponse(BaseModel):
    """影响单位净值可比性的一次现金分红或份额折算。"""

    fund_code: str
    action_date: date
    action_type: Literal["cash_dividend", "share_split"]
    cash_dividend_per_unit: float | None = None
    split_ratio: float | None = None
    description: str
    source: str | None = None
    source_url: str | None = None
    fetched_at: datetime | None = None


class FundCorporateActionCoverageIntervalResponse(BaseModel):
    """一段已经完成公司行为核对的连续日期区间。"""

    coverage_start: date
    coverage_end: date


class FundCorporateActionCoverageResponse(BaseModel):
    """空明细是否可以解释为“该区间确实没有分红”的审计依据。"""

    coverage_start: date | None = None
    coverage_end: date | None = None
    intervals: list[FundCorporateActionCoverageIntervalResponse] = Field(
        default_factory=list
    )
    source: str | None = None


class FundSummaryResponse(BaseModel):
    """基金列表中用于快速扫描的摘要信息。"""

    fund_code: str
    short_name: str
    fund_type: str
    sample_role: str
    latest_nav: float
    latest_nav_date: date
    daily_growth_rate_pct: float | None = None
    nav_record_count: int
    # 短周期收益与风险模块使用同一收益序列；缺失数据用 None，不显示为 0%。
    return_30d_pct: float | None = None
    return_3m_pct: float | None = None
    return_basis_label: str = ""
    chart_warnings: list[str] = Field(default_factory=list)


class FundProfileResponse(BaseModel):
    """基金详情页需要的基本档案。"""

    fund_code: str
    full_name: str | None = None
    short_name: str
    fund_type: str
    sample_role: str
    issue_date: str | None = None
    established: str | None = None
    net_asset_size: str | None = None
    share_size: str | None = None
    management_company: str | None = None
    custodian: str | None = None
    fund_managers: str | None = None
    management_fee: str | None = None
    custodian_fee: str | None = None
    benchmark: str | None = None
    tracking_target: str | None = None
    source_url: str | None = None


class FundListResponse(BaseModel):
    """基金观察页首次加载时返回的样本列表。"""

    count: int
    snapshot_start: date
    snapshot_end: date
    source: str
    # fixture 表示尚未执行在线同步；database 表示至少一只基金使用了 SQL 最新快照。
    data_mode: Literal["fixture", "database"] = "fixture"
    data_updated_at: datetime | None = None
    funds: list[FundSummaryResponse]


class FundDetailResponse(BaseModel):
    """单只基金的档案、最新值与完整净值曲线。"""

    profile: FundProfileResponse
    latest_nav: float
    latest_nav_date: date
    daily_growth_rate_pct: float | None = None
    nav_record_count: int
    snapshot_start: date
    snapshot_end: date
    source: str
    data_mode: Literal["fixture", "database"] = "fixture"
    data_updated_at: datetime | None = None
    nav_points: list[FundNavPointResponse]
    corporate_actions: list[FundCorporateActionResponse] = Field(default_factory=list)
    corporate_action_coverage: FundCorporateActionCoverageResponse


class FundChartPointResponse(BaseModel):
    """value 是收益指数；nav 只用于单位净值视图。"""

    date: date
    value: float
    nav: float | None = None


class FundChartResponse(BaseModel):
    """只读图表契约，携带真实来源和收益口径。"""

    fund_code: str
    short_name: str
    source: str
    data_mode: Literal["fixture", "database"]
    data_updated_at: datetime | None = None
    points: list[FundChartPointResponse]
    return_basis_label: str
    return_basis_status: str
    data_quality_warnings: list[str] = Field(default_factory=list)
    benchmark_name: str
    benchmark_status: str
    benchmark_points: list[FundChartPointResponse] = Field(default_factory=list)
    benchmark_warnings: list[str] = Field(default_factory=list)
    benchmark_source_urls: list[str] = Field(default_factory=list)


class FundDataSyncRequest(BaseModel):
    """手动触发基金净值同步时只需提供当前工作空间用户。"""

    user_id: str = Field(default="chen", min_length=1, max_length=50)


class FundDirectorySearchItemResponse(BaseModel):
    """基金目录搜索中的一条轻量结果。"""

    fund_code: str
    short_name: str
    fund_type: str
    source_url: str | None = None
    import_status: Literal["discovered", "importing", "ready", "failed"]
    is_imported: bool


class FundDirectorySearchResponse(BaseModel):
    """目录搜索结果及其缓存新鲜度。"""

    query: str
    count: int
    source: str
    data_mode: Literal["live", "memory_cache", "database_cache"]
    catalog_updated_at: datetime | None = None
    message: str = ""
    funds: list[FundDirectorySearchItemResponse]


class FundImportRequest(BaseModel):
    """把目录中的基金导入当前研究工作台。"""

    user_id: str = Field(default="chen", min_length=1, max_length=50)
    fund_code: str = Field(..., pattern=r"^\d{6}$")
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=100)


class FundDisclosureStatusResponse(BaseModel):
    """基金持仓披露数据在数据库或冻结样例中的覆盖情况。"""

    data_mode: Literal["fixture", "database", "unavailable"]
    record_count: int
    security_record_count: int
    industry_record_count: int
    fund_count: int
    latest_report_date: date | None = None
    last_successful_sync_at: datetime | None = None


class FundCorporateActionStatusResponse(BaseModel):
    """公司行为采集表当前覆盖多少基金和记录。"""

    action_count: int = Field(ge=0)
    fund_count: int = Field(ge=0)
    coverage_end: date | None = None
    last_successful_sync_at: datetime | None = None


class FundBenchmarkStatusResponse(BaseModel):
    """业绩基准日收益当前覆盖多少基金，以及采用何种质量口径。"""

    record_count: int = Field(ge=0)
    fund_count: int = Field(ge=0)
    matched_fund_count: int = Field(ge=0)
    proxy_fund_count: int = Field(ge=0)
    unavailable_fund_count: int = Field(ge=0)
    coverage_end: date | None = None
    last_successful_sync_at: datetime | None = None


class FundDataSyncStatusResponse(BaseModel):
    """基金净值与定期持仓披露采集管线的当前状态。"""

    ok: bool = True
    data_mode: Literal["fixture", "database"]
    source: str
    sync_enabled: bool
    sync_cron: str
    schedule_id: str
    record_count: int
    fund_count: int
    snapshot_start: date | None = None
    snapshot_end: date | None = None
    last_successful_sync_at: datetime | None = None
    disclosures: FundDisclosureStatusResponse | None = None
    corporate_actions: FundCorporateActionStatusResponse | None = None
    benchmarks: FundBenchmarkStatusResponse | None = None


class FundDisclosureReportDatesResponse(BaseModel):
    """股票、债券和行业可能来自不同报告期，因此分别返回日期。"""

    stock: date | None = None
    bond: date | None = None
    industry: date | None = None


class FundSecurityHoldingResponse(BaseModel):
    """一只基金披露的底层股票或债券。"""

    report_date: date
    asset_type: Literal["stock", "bond"]
    security_code: str
    security_name: str
    weight_pct: float
    market_value_10k: float | None = None
    rank: int
    source: str
    source_url: str


class FundIndustryAllocationResponse(BaseModel):
    """一只基金最新报告期的股票行业配置。"""

    report_date: date
    industry_name: str
    weight_pct: float
    market_value_10k: float | None = None
    rank: int
    source: str
    source_url: str


class FundDisclosureResponse(BaseModel):
    """单基金最新持仓披露及其来源、新鲜度和覆盖口径。"""

    fund_code: str
    data_mode: Literal["fixture", "database", "unavailable"]
    source: str
    source_url: str
    fetched_at: datetime | None = None
    report_dates: FundDisclosureReportDatesResponse
    latest_report_date: date | None = None
    oldest_report_date: date | None = None
    report_age_days: int | None = None
    freshness_status: Literal["fresh", "aging", "stale", "unavailable"]
    warnings: list[str]
    security_count: int
    industry_count: int
    disclosed_stock_weight_pct: float
    disclosed_bond_weight_pct: float
    industry_coverage_pct: float
    security_holdings: list[FundSecurityHoldingResponse]
    industry_allocations: list[FundIndustryAllocationResponse]


class FundAssetEvidenceResponse(BaseModel):
    """已披露资产小计，不冒充完整资产配置；未知值保留 null。"""

    kind: Literal["stock", "bond", "cash"]
    label: str
    report_date: date | None
    weight_pct: float | None


class FundHoldingsConcentrationResponse(BaseModel):
    """只给出可复算的集中度数值，不设置主观风险评分。"""

    top_stock_weight_pct: float | None
    top_five_disclosed_stocks_pct: float | None
    top_industry_name: str | None
    top_industry_weight_pct: float | None


class FundHoldingChangeResponse(BaseModel):
    """比较榜单里的比例；未再披露不代表清仓，变化单位是百分点。"""

    key: str
    name: str
    status: Literal["newly_disclosed", "not_disclosed", "comparable"]
    previous_weight_pct: float | None
    weight_pct: float | None
    change_pp: float | None
    source_url: str | None
    previous_source_url: str | None


class FundDisclosureComparisonResponse(BaseModel):
    kind: Literal["stock", "bond", "industry"]
    status: Literal["available", "unavailable"]
    report_date: date | None
    previous_report_date: date | None
    rows: list[FundHoldingChangeResponse]
    message: str


class FundHoldingsAnalysisResponse(BaseModel):
    """单基金持仓分析，保留披露原文来源和不可穿透的边界。"""

    fund_code: str
    short_name: str
    fund_type: str
    disclosure: FundDisclosureResponse
    asset_evidence: list[FundAssetEvidenceResponse]
    gross_disclosed_weight_pct: float | None
    security_coverage_pct: float | None
    unresolved_weight_pct: float | None
    concentration: FundHoldingsConcentrationResponse
    comparisons: list[FundDisclosureComparisonResponse]
    lookthrough_status: Literal["target_etf_unavailable", "direct_disclosure_only"]
    limitations: list[str]


class FundDrawdownResponse(BaseModel):
    """最大回撤对应的高点、低点和恢复过程。"""

    # drawdown_pct 使用正数表示跌幅大小，例如 18.5 表示从高点下跌 18.5%。
    drawdown_pct: float
    peak_date: date
    trough_date: date
    recovery_date: date | None = None
    recovery_days: int | None = None


class FundRiskAdjustedResponse(BaseModel):
    """在显式无风险利率假设下计算的风险调整后收益。"""

    annualized_return_pct: float
    risk_free_rate_annual_pct: float
    risk_free_rate_source: str
    sharpe_ratio: float | None = None
    sortino_ratio: float | None = None
    methodology: str
    warnings: list[str] = Field(default_factory=list)


class FundBenchmarkComparisonResponse(BaseModel):
    """基金和披露业绩基准在共同交易日上的区间比较。"""

    benchmark_name: str
    status: FundBenchmarkStatus
    period_return_pct: float | None = None
    fund_aligned_return_pct: float | None = None
    excess_return_pct: float | None = None
    observation_count: int = Field(ge=0)
    coverage_pct: float = Field(ge=0, le=100)
    period_start: date | None = None
    period_end: date | None = None
    source: str | None = None
    source_urls: list[str] = Field(default_factory=list)
    methodology: str
    warnings: list[str] = Field(default_factory=list)


class FundRiskResponse(BaseModel):
    """一只基金在指定历史区间内的确定性风险画像。"""

    fund_code: str
    short_name: str
    period_months: int
    period_start: date
    period_end: date
    observation_count: int
    period_return_pct: float
    annualized_volatility_pct: float
    downside_volatility_pct: float
    max_drawdown: FundDrawdownResponse
    current_drawdown_pct: float
    current_peak_date: date
    worst_daily_return_pct: float
    worst_daily_return_date: date
    longest_drawdown_days: int
    annualization_trading_days: int
    risk_adjusted: FundRiskAdjustedResponse
    benchmark_comparison: FundBenchmarkComparisonResponse
    source: str
    data_mode: Literal["fixture", "database"] = "fixture"
    data_updated_at: datetime | None = None
    snapshot_end: date
    return_basis: FundReturnBasis
    return_basis_label: str
    return_basis_status: FundReturnBasisStatus
    corporate_action_count: int = Field(ge=0)
    corporate_action_source: str | None = None
    corporate_action_coverage_start: date | None = None
    corporate_action_coverage_end: date | None = None
    data_quality_warnings: list[str] = Field(default_factory=list)
    methodology: str
    disclaimer: str


class MarketTrendResponse(BaseModel):
    """市场趋势使用哪个代理，以及多个观察周期发生了什么变化。"""

    proxy_code: str
    proxy_name: str
    tracking_target: str
    return_20d_pct: float
    return_30d_pct: float
    return_60d_pct: float
    return_3m_pct: float
    label: str


class MarketVolatilityResponse(BaseModel):
    """最近一段时间的年化波动率。"""

    window_days: int
    annualized_volatility_pct: float
    annualization_trading_days: int
    label: str


class MarketBreadthResponse(BaseModel):
    """全市场当日上涨、下跌和平盘家数。"""

    advancing_count: int
    declining_count: int
    flat_count: int
    suspended_count: int
    limit_up_count: int
    limit_down_count: int
    advance_ratio_pct: float
    market_activity_pct: float
    label: str


class MarketActivityResponse(BaseModel):
    """使用沪深300指数成交额衡量最近交易活跃程度。"""

    benchmark_code: str
    benchmark_name: str
    latest_amount_cny: float
    five_day_average_amount_cny: float
    latest_vs_five_day_average_pct: float
    label: str


class MarketSignalResponse(BaseModel):
    """Agent 可以逐条引用的一项市场状态证据。"""

    key: str
    label: str
    direction: Literal["positive", "neutral", "negative", "caution"]
    value_text: str
    evidence: str


class MarketDataSourceResponse(BaseModel):
    """一项市场数据的来源和截止日期。"""

    name: str
    source_level: Literal["A", "B", "C"]
    data_date: date
    source_url: str


class MarketIntradayQuoteResponse(BaseModel):
    """一条盘中累计行情；缺失值保持None，来源时钟与抓取时钟分开。"""

    index_code: str
    index_name: str
    source_url: str
    last: float | None = None
    previous_close: float | None = None
    change_pct: float | None = None
    volume_lots: float | None = None
    amount_cny: float | None = None
    quote_at: datetime | None = None
    fetched_at: datetime | None = None
    status: Literal["available", "not_synced", "error", "cached_error"]
    freshness: Literal["recent", "stale", "previous_date", "missing"]
    error: str = ""


class MarketIntradayOverviewResponse(BaseModel):
    """独立于日线研究的手动快照，不表示持续推送或逐笔实时。"""

    data_kind: Literal["market_intraday"]
    source_name: str
    checked_at: datetime
    attempted_at: datetime | None = None
    completed_at: datetime | None = None
    next_refresh_at: datetime | None = None
    refreshing: bool = False
    indices: list[MarketIntradayQuoteResponse]
    limitations: list[str]


class MarketExchangeTurnoverResponse(BaseModel):
    """证券集合互斥的交易所A股汇总。"""

    exchange: Literal["sh", "sz", "bj"]
    label: str
    amount_cny: float
    security_count: int


class MarketTurnoverSnapshotResponse(BaseModel):
    """整批汇总快照；缺页时不能更新任何合计。"""

    sh_sz_amount_cny: float
    sh_sz_bj_amount_cny: float
    exchanges: list[MarketExchangeTurnoverResponse]
    security_count: int
    expected_count: int
    reference_at: datetime
    started_at: datetime
    fetched_at: datetime


class MarketTurnoverOverviewResponse(BaseModel):
    data_kind: Literal["market_turnover"]
    source_name: str
    source_url: str
    status: Literal["available", "not_synced", "error", "cached_error"]
    snapshot: MarketTurnoverSnapshotResponse | None = None
    error: str = ""
    attempted_at: datetime | None = None
    next_refresh_at: datetime | None = None
    refreshing: bool = False
    freshness: Literal["recent", "stale", "previous_date", "missing"]
    scope: str
    time_basis: str
    limitation: str


class MarketTurnoverSeriesResponse(BaseModel):
    series_id: str
    label: str
    group: Literal["market", "indices"]


class MarketTurnoverPointResponse(BaseModel):
    """一个真实采样点：金额是当日累计值，并非净流入额。"""

    quote_at: datetime
    fetched_at: datetime
    amount_cny: float
    security_count: int | None = None


class MarketTurnoverHistoryResponse(MarketTurnoverSeriesResponse):
    series: list[MarketTurnoverSeriesResponse]
    data_date: date
    available_dates: list[date]
    is_today: bool
    points: list[MarketTurnoverPointResponse]
    warnings: list[str]
    sampling: str
    time_basis: str
    retention_days: int


class MarketDailyTurnoverPointResponse(BaseModel):
    date: date
    amount_cny: float | None = None


class MarketDailyTurnoverSeriesResponse(BaseModel):
    series_id: str
    label: str
    group: Literal["reference", "indices"]


class MarketDailyTurnoverResponse(MarketDailyTurnoverSeriesResponse):
    """日成交额专用协议，不接收分时采样时间或当日未确认累计额。"""

    series: list[MarketDailyTurnoverSeriesResponse]
    period: Literal["30d", "3m"]
    range_start: date
    range_end: date
    data_date: date | None = None
    points: list[MarketDailyTurnoverPointResponse]
    status: Literal["available", "cached_error", "error", "not_synced"]
    error: str = ""
    warnings: list[str]
    fetched_at: datetime | None = None
    attempted_at: datetime | None = None
    refreshing: bool = False
    source_name: str
    source_url: str
    scope: str
    methodology: str


class MarketIndexHistoryPointResponse(BaseModel):
    """指数图只绘制已存储的日线，不合成盘中价格或 OHLC。"""

    date: date
    close: float
    volume: float


class MarketIndexAnalysisResponse(BaseModel):
    """一个宽基指数基于自身日线行情得到的透明分析。"""

    index_code: str
    provider_symbol: str
    index_name: str
    market_segment: str
    data_date: date
    close: float
    daily_return_pct: float
    return_20d_pct: float
    return_30d_pct: float
    return_60d_pct: float
    return_3m_pct: float
    annualized_volatility_20d_pct: float
    max_drawdown_60d_pct: float
    volume_vs_five_day_average_pct: float
    trend_label: str
    volatility_label: str
    activity_label: str
    direction: Literal["positive", "neutral", "negative"]
    relative_strength_rank: int = Field(ge=1)
    relative_strength_rank_30d: int = Field(ge=1)
    relative_strength_rank_3m: int = Field(ge=1)
    summary: str
    evidence_id: str
    source: str
    source_url: str
    history: list[MarketIndexHistoryPointResponse] = Field(default_factory=list)


class MarketIndexOverviewResponse(BaseModel):
    """已同步宽基指数的相对比较，以及每组可展开的详细指标。"""

    data_kind: Literal["market_index_comparison"]
    data_mode: Literal["database", "unavailable"]
    data_updated_at: datetime | None = None
    data_date: date | None = None
    index_count: int = Field(ge=0)
    strongest_index_code: str | None = None
    strongest_index_name: str | None = None
    weakest_index_code: str | None = None
    weakest_index_name: str | None = None
    spread_20d_pct: float | None = None
    strongest_index_30d_code: str | None = None
    strongest_index_30d_name: str | None = None
    weakest_index_30d_code: str | None = None
    weakest_index_30d_name: str | None = None
    spread_30d_pct: float | None = None
    strongest_index_3m_code: str | None = None
    strongest_index_3m_name: str | None = None
    weakest_index_3m_code: str | None = None
    weakest_index_3m_name: str | None = None
    spread_3m_pct: float | None = None
    summary: str
    indices: list[MarketIndexAnalysisResponse] = Field(default_factory=list)
    methodology: str
    limitations: list[str] = Field(default_factory=list)


class MarketSentimentResponse(BaseModel):
    """FundScope 市场环境区和 Agent Tool 共用的结构化结果。"""

    data_kind: str
    data_mode: Literal["fixture", "database"] = "fixture"
    data_updated_at: datetime | None = None
    data_date: date
    state_code: str
    state_label: str
    confidence: Literal["high", "medium", "low"]
    summary: str
    trend: MarketTrendResponse
    volatility: MarketVolatilityResponse
    breadth: MarketBreadthResponse
    activity: MarketActivityResponse
    index_overview: MarketIndexOverviewResponse | None = None
    signals: list[MarketSignalResponse]
    evidence_ids: list[str]
    sources: list[MarketDataSourceResponse]
    limitations: list[str]
    methodology: str
    disclaimer: str


class MarketDataSyncRequest(BaseModel):
    """手动触发市场数据同步时使用当前工作空间用户。"""

    user_id: str = Field(default="chen", min_length=1, max_length=50)


class MarketDataSyncStatusResponse(BaseModel):
    """市场总览和宽基指数行情采集管线的持久化状态。"""

    ok: bool = True
    data_mode: Literal["fixture", "database"]
    source: str
    sync_enabled: bool
    sync_cron: str
    schedule_id: str
    record_count: int
    snapshot_start: date | None = None
    snapshot_end: date | None = None
    last_successful_sync_at: datetime | None = None
    index_bar_count: int = Field(default=0, ge=0)
    index_count: int = Field(default=0, ge=0)
    index_snapshot_start: date | None = None
    index_snapshot_end: date | None = None
    index_last_successful_sync_at: datetime | None = None


class LargeCapitalSyncRequest(BaseModel):
    """手动刷新 ETF 份额与龙虎榜机构席位时使用当前可信用户。"""

    user_id: str = Field(default="chen", min_length=1, max_length=50)
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=100)


class LargeCapitalStatusResponse(BaseModel):
    """大资金事实表的覆盖范围和最近一次成功同步时间。"""

    ok: bool = True
    data_mode: Literal["database", "unavailable"]
    source: str
    etf_snapshot_count: int = Field(ge=0)
    etf_history_days: int = Field(ge=0)
    etf_start: date | None = None
    etf_end: date | None = None
    institutional_trade_count: int = Field(ge=0)
    institutional_start: date | None = None
    institutional_end: date | None = None
    last_successful_sync_at: datetime | None = None


class LargeCapitalEtfItemResponse(BaseModel):
    """一个主题中规模最大的代表 ETF。"""

    etf_code: str
    etf_name: str
    estimated_scale_cny: float
    source_level: Literal["A", "B", "C"]
    source_url: str


class LargeCapitalPeriodFlowResponse(BaseModel):
    """一个统计窗口的可比样本。金额为 None 表示不能计算，不是零流入。"""

    start_date: date | None = None
    end_date: date | None = None
    net_flow_cny: float | None = None
    intensity_pct: float | None = None
    coverage_pct: float = Field(ge=0, le=100)
    comparable_etf_count: int = Field(ge=0)


class LargeCapitalSectorExposureResponse(BaseModel):
    """板块与已选基金/组合的关联，披露日说明它不是实时仓位。"""

    scope: Literal["fund", "portfolio"]
    scope_id: str
    scope_name: str
    disclosure_date: date | None = None
    known_exposure_pct: float = Field(ge=0)


class LargeCapitalSectorRowResponse(LargeCapitalPeriodFlowResponse):
    """板块表格的一行，同时带上计算结果、解读和证据编号。"""

    topic_code: str
    topic_label: str
    data_date: date
    etf_count: int = Field(ge=1)
    rank: int | None = None
    status: Literal["available", "insufficient", "stale"]
    direction: Literal["inflow", "outflow", "neutral", "insufficient"]
    interpretation: str
    evidence_id: str | None = None
    exposure_links: list[LargeCapitalSectorExposureResponse] = Field(default_factory=list)


class LargeCapitalSectorLeaderResponse(BaseModel):
    """净申购/净赎回样本金额领先的板块，不表示全行业资金排名。"""

    topic_code: str
    topic_label: str
    net_flow_cny: float
    coverage_pct: float
    evidence_id: str


class LargeCapitalSectorWindowResponse(BaseModel):
    """页面切换窗口时使用同一次返回的数据，不重新抓取。"""

    period: Literal[1, 5, 20]
    available_count: int
    inflow_count: int
    outflow_count: int
    neutral_count: int
    unavailable_count: int
    leaders: dict[str, LargeCapitalSectorLeaderResponse | None]
    rows: list[LargeCapitalSectorRowResponse]


class LargeCapitalSectorAnalysisResponse(BaseModel):
    """完整板块观察及口径，显式声明后 FastAPI 才会把它发给前端。"""

    basis: Literal["sector_etf_net_creation_estimate"]
    data_date: date | None = None
    sector_count: int
    windows: dict[str, LargeCapitalSectorWindowResponse]
    methodology: str
    boundary: str


class LargeCapitalTopicSignalResponse(BaseModel):
    """同一宽基或行业主题的 ETF 份额变化汇总。"""

    topic_code: str
    topic_label: str
    topic_kind: Literal["broad", "sector"]
    data_date: date
    etf_count: int = Field(ge=1)
    history_days: int = Field(ge=1)
    period_flows: dict[str, LargeCapitalPeriodFlowResponse] = Field(default_factory=dict)
    estimated_scale_cny: float
    flow_1d_cny: float | None = None
    flow_5d_cny: float | None = None
    flow_20d_cny: float | None = None
    coverage_1d_pct: float = Field(ge=0, le=100)
    coverage_5d_pct: float = Field(ge=0, le=100)
    coverage_20d_pct: float = Field(ge=0, le=100)
    provider_main_net_inflow_cny: float
    direction: Literal["inflow", "outflow", "neutral", "insufficient"]
    direction_label: str
    confidence: Literal["high", "medium", "low"]
    basis_value_cny: float
    basis_ratio_pct: float
    evidence_id: str
    top_etfs: list[LargeCapitalEtfItemResponse] = Field(default_factory=list)


class InstitutionalSecuritySignalResponse(BaseModel):
    """龙虎榜机构席位净买入或净卖出的代表证券。"""

    stock_code: str
    stock_name: str
    trade_date: date
    net_buy_cny: float
    buyer_count: int = Field(ge=0)
    seller_count: int = Field(ge=0)
    listing_reason: str
    evidence_id: str
    source_level: Literal["A", "B", "C"]
    source_url: str


class InstitutionalCapitalSignalResponse(BaseModel):
    """最近五个龙虎榜交易日的机构席位汇总。"""

    status: Literal["available", "unavailable"]
    direction: Literal["inflow", "outflow", "neutral", "insufficient"]
    direction_label: str
    confidence: Literal["high", "medium", "low"]
    trade_date_start: date | None = None
    trade_date_end: date | None = None
    trading_day_count: int = Field(ge=0)
    stock_count: int = Field(ge=0)
    buy_cny: float
    sell_cny: float
    net_buy_cny: float
    net_buy_ratio_pct: float | None = None
    evidence_id: str | None = None
    top_net_buys: list[InstitutionalSecuritySignalResponse] = Field(
        default_factory=list
    )
    top_net_sells: list[InstitutionalSecuritySignalResponse] = Field(
        default_factory=list
    )


class LargeCapitalMatchedTopicResponse(BaseModel):
    """资金主题与当前基金或组合披露持仓的一次关联。"""

    topic_code: str
    topic_label: str
    direction: Literal["inflow", "outflow", "neutral", "insufficient"]
    direction_label: str
    confidence: Literal["high", "medium", "low"]
    known_exposure_pct: float = Field(ge=0)
    evidence_id: str


class DirectInstitutionalExposureResponse(BaseModel):
    """基金披露证券与龙虎榜机构席位直接重合的记录。"""

    security_code: str
    security_name: str
    exposure_pct: float = Field(ge=0)
    net_buy_cny: float
    evidence_id: str


class LargeCapitalExposureLinkResponse(BaseModel):
    """大资金线索对当前基金或组合的可核验关联。"""

    scope: Literal["fund", "portfolio"]
    scope_id: str
    scope_name: str
    direction: Literal[
        "inflow",
        "outflow",
        "neutral",
        "insufficient",
        "mixed",
        "direct_institution",
    ]
    known_exposure_pct: float = Field(ge=0)
    disclosure_date: date | None = None
    matched_topics: list[LargeCapitalMatchedTopicResponse] = Field(default_factory=list)
    direct_institutional_securities: list[DirectInstitutionalExposureResponse] = Field(
        default_factory=list
    )
    evidence_ids: list[str] = Field(default_factory=list)
    summary: str


class LargeCapitalIdentityBoundaryResponse(BaseModel):
    """明确告诉前端哪些公开线索不能被解释为真实机构身份。"""

    confirmed_national_team_disclosure_count: int = Field(ge=0)
    label: str
    explanation: str


class LargeCapitalSourceResponse(BaseModel):
    """ETF 或机构席位数据来源。"""

    name: str
    source_level: Literal["A", "B", "C"]
    source_url: str


class RetailIndexReturnResponse(BaseModel):
    """百分比已在后端换算；日期说明每个窗口真正使用的起终点。"""

    label: str
    value_pct: float | None = None
    start_date: str | None = None
    end_date: str


class RetailIndexPointResponse(BaseModel):
    """公开指数的一个观测点，不插入周末或补零。"""

    date: str
    value: float


class RetailIndexResponse(BaseModel):
    """两个同名民间指标保留各自版本和单位，不合成为一个总分。"""

    indicator_id: Literal["mom", "bagholder50"]
    name: str
    version: str
    kind: Literal["sentiment_score", "theoretical_index"]
    status: Literal["available", "not_synced", "source_unavailable", "stale", "cached_error", "error"]
    unit: str
    value: float | None = None
    data_date: str | None = None
    returns: dict[str, RetailIndexReturnResponse] = Field(default_factory=dict)
    history: list[RetailIndexPointResponse] = Field(default_factory=list)
    source_name: str
    source_url: str
    data_url: str | None = None
    methodology_url: str
    source_hash: str | None = None
    evidence_id: str | None = None
    publication_date: str | None = None
    fetched_at: datetime | None = None
    attempted_at: datetime | None = None
    last_error: str = ""
    summary: str
    methodology: str
    limitations: list[str] = Field(default_factory=list)


class RetailIndexOverviewResponse(BaseModel):
    data_kind: Literal["retail_indices"] = "retail_indices"
    indicators: list[RetailIndexResponse]


class LargeCapitalOverviewResponse(BaseModel):
    """市场页与 Agent Tool 共用的大资金行为线索结果。"""

    data_kind: Literal["large_capital_signals"]
    data_mode: Literal["database", "unavailable"]
    generated_at: datetime
    data_updated_at: datetime | None = None
    data_date: date | None = None
    state_code: Literal[
        "accumulating",
        "diverging",
        "withdrawing",
        "neutral",
        "insufficient",
    ]
    state_label: str
    confidence: Literal["high", "medium", "low"]
    summary: str
    etf_history_days: int = Field(ge=0)
    topic_signals: list[LargeCapitalTopicSignalResponse] = Field(default_factory=list)
    sector_analysis: LargeCapitalSectorAnalysisResponse
    institutional_signal: InstitutionalCapitalSignalResponse
    exposure_links: list[LargeCapitalExposureLinkResponse] = Field(default_factory=list)
    identity_boundary: LargeCapitalIdentityBoundaryResponse
    evidence_legend: dict[str, str]
    sources: list[LargeCapitalSourceResponse] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    methodology: str
    disclaimer: str


MarketTopicValue = Annotated[str, Field(min_length=1, max_length=20)]


class MarketEventDataSyncRequest(BaseModel):
    """手动触发新闻与公告同步时使用当前工作空间用户。"""

    user_id: str = Field(default="chen", min_length=1, max_length=50)
    topics: list[MarketTopicValue] = Field(default_factory=list, max_length=3)
    use_model_topic_expansion: bool = True


class MarketTopicOptionResponse(BaseModel):
    """前端板块选择器展示的一项固定词典和可选模型缓存。"""

    topic: str
    dictionary_terms: list[str] = Field(default_factory=list)
    cached_model_terms: list[str] = Field(default_factory=list)
    cached_at: datetime | None = None


class MarketTopicCatalogResponse(BaseModel):
    """采集阶段允许选择的预设板块。"""

    data_kind: Literal["market_topic_catalog"]
    max_selected_topics: int
    model_expansion_default: bool
    topics: list[MarketTopicOptionResponse] = Field(default_factory=list)


class MarketEventDataSyncStatusResponse(BaseModel):
    """市场事件数据库、采集窗口和可选自动调度的运行状态。"""

    ok: bool = True
    data_mode: Literal["fixture", "database"]
    source: str
    sync_enabled: bool
    sync_cron: str
    schedule_id: str
    lookback_days: int
    record_count: int
    event_start: date | None = None
    event_end: date | None = None
    last_successful_sync_at: datetime | None = None
    pending_analysis_count: int = 0
    analyzed_count: int = 0
    accepted_analysis_count: int = 0
    abstained_analysis_count: int = 0
    embedding_count: int = 0
    cluster_count: int = 0
    clustered_event_count: int = 0
    pending_cluster_event_count: int = 0
    last_clustered_at: datetime | None = None


class MarketEventAnalysisRequest(BaseModel):
    """按需分析数据库中尚未拥有有效语义结果的事件。"""

    user_id: str = Field(default="chen", min_length=1, max_length=50)
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=100)


class MarketEventClusterRequest(BaseModel):
    """按需为当前数据库事件生成 Embedding 并重建事件簇。"""

    user_id: str = Field(default="chen", min_length=1, max_length=50)
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=100)


class MarketIntelligencePipelineRequest(BaseModel):
    """手动启动完整情报流水线；组合阶段仍严格限制在当前用户范围内。"""

    user_id: str = Field(default="chen", min_length=1, max_length=50)
    # 手机断网重试沿用同一请求编号，由现有 Job Store 返回已受理任务。
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=100)
    topics: list[MarketTopicValue] = Field(default_factory=list, max_length=3)
    use_model_topic_expansion: bool = True


class SocialSignalRefreshRequest(BaseModel):
    """单独刷新社交观点时仍使用当前可信会话中的用户。"""

    user_id: str = Field(default="chen", min_length=1, max_length=50)
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=100)


class SocialSignalOperationWindowResponse(BaseModel):
    """围绕中国交易日 15:00 截止时间计算出的有效帖子窗口。"""

    phase: Literal["before_cutoff", "after_cutoff", "market_closed"]
    timezone: Literal["Asia/Shanghai"]
    calendar: Literal["XSHG"]
    target_trade_date: date
    window_start: datetime
    window_end: datetime
    cutoff_at: datetime
    cutoff_time: str
    max_age_hours: int = Field(gt=0)
    effective_window_hours: float = Field(ge=0)
    minutes_until_cutoff: int = Field(ge=0)
    label: str


class SocialSignalStatusResponse(BaseModel):
    """社交观点浏览器、登录会话和持久化覆盖状态。"""

    ok: bool = True
    enabled: bool
    configured: bool
    provider: str
    browser_available: bool = False
    session_ready: bool = False
    login_required: bool = False
    # 登录凭据仍在，不代表平台允许继续读取。验证暂停与本地限频单独报告。
    verification_required: bool = False
    collection_allowed: bool = False
    next_collection_at: datetime | None = None
    access_message: str = ""
    tracked_author_count: int = Field(default=0, ge=0)
    keyword_count: int = Field(default=0, ge=0)
    lookback_hours: int = Field(gt=0)
    minimum_authors: int = Field(gt=0)
    operation_window: SocialSignalOperationWindowResponse
    post_count: int = Field(ge=0)
    analyzed_count: int = Field(ge=0)
    accepted_count: int = Field(ge=0)
    pending_analysis_count: int = Field(ge=0)
    last_fetched_at: datetime | None = None
    latest_post_at: datetime | None = None


SocialSignalAction = Literal[
    "buy",
    "add",
    "reduce",
    "sell",
    "hold",
    "watch",
    "unknown",
]


class SocialSignalEvidencePostResponse(BaseModel):
    """聚合结果中可点击追溯的一条作者自述。"""

    post_id: str
    author_name: str
    published_at: datetime
    title: str
    content_excerpt: str
    source_url: str
    matched_keyword: str
    analysis_status: Literal["accepted", "abstained", "pending"]
    action: SocialSignalAction
    action_label: str
    position_text: str
    rationale: str
    confidence: float = Field(ge=0, le=1)
    images: list[dict[str, Any]] = Field(default_factory=list)
    themes: list[str] = Field(default_factory=list)
    exclusion_reasons: list[str] = Field(default_factory=list)
    uncertainties: list[str] = Field(default_factory=list)
    evidence_kind: str = "unclear"
    evidence_ids: list[str] = Field(default_factory=list)
    model_name: str = ""
    analyzed_at: datetime | None = None
    like_count: int = 0
    comment_count: int = 0


class SocialSignalAnalysisRequest(BaseModel):
    user_id: str = Field(default="guest", min_length=1, max_length=50)
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=100)
    scope: Literal["window", "all"] = "window"


class SocialSignalAggregateResponse(BaseModel):
    """同一基金或主题在聚合窗口内的作者多样化仓位观点。"""

    topic_key: str
    topic_type: Literal["fund", "fund_name", "theme"]
    topic_value: str
    topic_label: str
    sample_count: int = Field(ge=1)
    author_count: int = Field(ge=1)
    action_counts: dict[str, int]
    positioning_score: float = Field(ge=-100, le=100)
    positioning_label: str
    disagreement_ratio: float = Field(ge=0, le=1)
    confidence: float = Field(ge=0, le=1)
    excluded_commercial_count: int = Field(ge=0)
    window_start: datetime
    window_end: datetime
    evidence_posts: list[SocialSignalEvidencePostResponse]


class SocialSignalOverviewResponse(BaseModel):
    """市场页展示的社交观点聚合与最近证据。"""

    data_kind: str
    generated_at: datetime
    provider: str
    enabled: bool
    configured: bool
    lookback_hours: int = Field(gt=0)
    minimum_authors: int = Field(gt=0)
    operation_window: SocialSignalOperationWindowResponse
    sample_count: int = Field(ge=0)
    accepted_sample_count: int = Field(ge=0)
    aggregate_count: int = Field(ge=0)
    aggregates: list[SocialSignalAggregateResponse]
    recent_posts: list[SocialSignalEvidencePostResponse]
    review: dict[str, Any] = Field(default_factory=dict)
    status: SocialSignalStatusResponse
    disclaimer: str


# 事件类型和来源等级同时给 Schema、Router 与编辑器使用，避免三处各写一套字符串。
MarketEventType = Literal[
    "policy",
    "economic_data",
    "regulation",
    "fund_announcement",
    "market_notice",
    "social_positioning",
]
MarketSourceLevel = Literal["A", "B", "C", "D"]
MarketImpactDirection = Literal[
    "positive",
    "neutral",
    "negative",
    "caution",
    "mixed",
]
MarketImpactHorizon = Literal["short_term", "medium_term", "long_term"]


class MarketEventSemanticAnalysisResponse(BaseModel):
    """经过 Python 校验后保存的 DeepSeek 事件语义结果。"""

    status: Literal["accepted", "abstained"]
    model_name: str
    prompt_version: str
    entities: list[str]
    themes: list[str]
    direction: Literal["supportive", "caution", "neutral", "uncertain"]
    horizon: MarketImpactHorizon
    severity: int = Field(ge=1, le=3)
    relevance: Literal["direct", "related", "context", "uncertain"]
    confidence: float = Field(ge=0, le=1)
    reason: str
    affected_fund_codes: list[str]
    uncertainties: list[str]
    analyzed_at: datetime


class MarketEventClusterResponse(BaseModel):
    """一条事件所在逻辑事件簇的公开元数据。"""

    cluster_id: str
    representative_event_id: str
    is_representative: bool
    member_count: int = Field(ge=1)
    source_count: int = Field(ge=1)
    source_names: list[str]
    source_levels: list[MarketSourceLevel]
    event_start: date
    event_end: date
    heat_score: float = Field(ge=0, le=100)
    heat_level: Literal["low", "medium", "high"]
    similarity: float = Field(ge=-1, le=1)
    algorithm_version: str
    embedding_model: str
    generated_at: datetime


class MarketEventResponse(BaseModel):
    """时间线中的一条政策、数据、监管或基金公告。"""

    event_id: str
    event_date: date
    published_date: date
    event_type: MarketEventType
    title: str
    summary: str
    impact_direction: MarketImpactDirection
    impact_horizon: MarketImpactHorizon
    impact_reason: str
    affected_fund_codes: list[str]
    tags: list[str]
    source_name: str
    source_level: MarketSourceLevel
    source_grade_reason: str
    source_url: str
    # 哈希让调用方能判断两条记录是否来自同一份规范化来源文本。
    content_hash: str
    # 原始事件始终保留；只有存在有效独立结果时才附加这一层。
    semantic_analysis: MarketEventSemanticAnalysisResponse | None = None
    # 时效性由 Python 根据事件日期和影响周期动态计算，不回写原始事件表。
    freshness_status: Literal["fresh", "decaying", "expired"]
    freshness_as_of: date
    age_days: int = Field(ge=0)
    fresh_days: int = Field(gt=0)
    valid_days: int = Field(gt=0)
    valid_until: date
    decay_factor: float = Field(ge=0, le=1)
    freshness_score: float = Field(ge=0, le=100)
    cluster: MarketEventClusterResponse | None = None


class MarketEventListResponse(BaseModel):
    """市场事件查询结果，以及当前数据库或离线快照的来源口径。"""

    data_kind: str
    data_mode: Literal["fixture", "database"] = "fixture"
    data_updated_at: datetime | None = None
    generated_at: datetime
    data_cutoff: date
    count: int
    total_matched: int
    semantic_accepted_count: int = 0
    semantic_abstained_count: int = 0
    semantic_pending_count: int = 0
    fresh_event_count: int = 0
    decaying_event_count: int = 0
    expired_event_count: int = 0
    cluster_count: int = 0
    clustered_event_count: int = 0
    source_count: int = 0
    source_names: list[str] = Field(default_factory=list)
    events: list[MarketEventResponse]
    source_grade_legend: dict[str, str]
    disclaimer: str


MarketHotspotLifecycle = Literal[
    "emerging",
    "rising",
    "peaking",
    "steady",
    "cooling",
    "faded",
]


class MarketHotspotEvidenceResponse(BaseModel):
    """热点背后的一篇原始报道，供页面和 Agent 回到真实来源。"""

    event_id: str
    event_date: date
    source_name: str
    source_level: MarketSourceLevel
    source_url: str


class MarketHotspotTimelinePointResponse(BaseModel):
    """一次手动刷新留下的热度时间点。"""

    observed_at: datetime
    data_cutoff: date
    heat_score: float = Field(ge=0, le=100)
    member_count: int = Field(ge=1)
    source_count: int = Field(ge=1)


class MarketHotspotResponse(BaseModel):
    """一个逻辑市场热点及其当前阶段、变化量和证据。"""

    rank: int = Field(ge=0)
    cluster_id: str
    representative_event_id: str
    title: str
    summary: str
    event_start: date
    event_end: date
    observed_at: datetime
    impact_direction: MarketImpactDirection
    impact_horizon: MarketImpactHorizon
    affected_fund_codes: list[str]
    themes: list[str]
    source_count: int = Field(ge=1)
    source_names: list[str]
    source_levels: list[MarketSourceLevel]
    member_count: int = Field(ge=1)
    heat_score: float = Field(ge=0, le=100)
    peak_heat_score: float = Field(ge=0, le=100)
    heat_level: Literal["low", "medium", "high"]
    freshness_status: Literal["fresh", "decaying", "expired"]
    freshness_score: float = Field(ge=0, le=100)
    lifecycle: MarketHotspotLifecycle
    lifecycle_label: str
    trend_direction: Literal[
        "rising",
        "flat",
        "falling",
        "insufficient_history",
    ]
    change_since_previous: float | None = None
    heat_change_24h: float | None = None
    heat_change_3d: float | None = None
    member_delta: int
    source_delta: int
    snapshot_count: int = Field(ge=1)
    trend_confidence: Literal["low", "medium", "high"]
    tracking_reason: str
    evidence: list[MarketHotspotEvidenceResponse]
    timeline: list[MarketHotspotTimelinePointResponse]
    algorithm_version: str


class MarketHotspotListResponse(BaseModel):
    """热点追踪列表；没有快照时仍返回稳定空结构。"""

    data_kind: str
    data_mode: Literal["database", "unavailable"]
    generated_at: datetime | None = None
    data_cutoff: date | None = None
    history_days: int = Field(ge=7, le=90)
    count: int = Field(ge=0)
    total_matched: int = Field(ge=0)
    total_hotspot_count: int = Field(ge=0)
    history_point_count: int = Field(ge=0)
    lifecycle_counts: dict[MarketHotspotLifecycle, int]
    hotspots: list[MarketHotspotResponse]
    disclaimer: str


AssessmentDirection = Literal["supportive", "caution", "neutral"]
AssessmentEvidenceKind = Literal["fund_risk", "market_state", "market_event"]
AssessmentReliability = Literal["high", "medium", "low"]
AssessmentRelevance = Literal["direct", "related", "context"]
AssessmentHorizon = Literal[
    "historical",
    "current",
    "short_term",
    "medium_term",
    "long_term",
]


class FundAssessmentEvidenceResponse(BaseModel):
    """综合研判中的一条可追溯证据，而不是模型生成的自由文本。"""

    evidence_id: str
    kind: AssessmentEvidenceKind
    direction: AssessmentDirection
    strength: int = Field(ge=1, le=3)
    reliability: AssessmentReliability
    relevance: AssessmentRelevance
    horizon: AssessmentHorizon
    title: str
    value_text: str
    explanation: str
    data_date: date
    source_name: str
    source_level: MarketSourceLevel | None = None
    source_url: str | None = None
    event_id: str | None = None
    # 聚类字段只对 market_event 证据有值；event_id 仍指向实际展示的原始报道。
    cluster_id: str | None = None
    cluster_member_count: int | None = Field(default=None, ge=1)
    cluster_source_count: int | None = Field(default=None, ge=1)
    heat_score: float | None = Field(default=None, ge=0, le=100)
    heat_level: Literal["low", "medium", "high"] | None = None
    # 非事件证据保持默认 1；事件证据会随时间衰减，过期时为 0。
    decay_factor: float = Field(default=1.0, ge=0, le=1)
    freshness_status: Literal["fresh", "decaying", "expired"] | None = None
    valid_until: date | None = None
    age_days: int | None = Field(default=None, ge=0)


class FundAssessmentScoreResponse(BaseModel):
    """透明的规则分解，前端和 Agent 都能解释数字从哪里来。"""

    supportive_points: float
    caution_points: float
    net_points: float
    neutral_evidence_count: int
    expired_event_count: int = 0
    reliability_weights: dict[str, float]
    relevance_weights: dict[str, float]
    method: str


class FundAssessmentResponse(BaseModel):
    """单只基金风险、市场状态和关联事件的确定性融合结果。"""

    data_kind: str
    fund_code: str
    short_name: str
    sample_role: str
    period_months: int
    period_start: date
    period_end: date
    assessment_date: date
    state_code: str
    state_label: str
    confidence: Literal["high", "medium", "low"]
    confidence_reason: str
    summary: str
    score_breakdown: FundAssessmentScoreResponse
    supporting_evidence: list[FundAssessmentEvidenceResponse]
    risk_evidence: list[FundAssessmentEvidenceResponse]
    neutral_evidence: list[FundAssessmentEvidenceResponse]
    evidence_count: int
    event_count: int
    event_cluster_count: int = 0
    active_event_count: int = 0
    expired_event_count: int = 0
    available_dimensions: list[AssessmentEvidenceKind]
    coverage_pct: float = Field(ge=0, le=100)
    risk_return_basis: Literal[
        "total_return_reinvested",
        "provider_reported_daily_return",
        "unit_nav_unadjusted",
    ]
    risk_return_basis_label: str
    risk_return_basis_status: Literal["verified", "provider_reported", "limited"]
    risk_data_quality_warnings: list[str] = Field(default_factory=list)
    risk_adjusted: FundRiskAdjustedResponse
    benchmark_comparison: FundBenchmarkComparisonResponse
    component_dates: dict[str, date]
    missing_information: list[str]
    limitations: list[str]
    methodology: str
    disclaimer: str


SemanticDirection = Literal["supportive", "caution", "neutral", "uncertain"]
SemanticRelevance = Literal["direct", "related", "context", "uncertain"]
SemanticHorizon = Literal[
    "short_term",
    "medium_term",
    "long_term",
    "uncertain",
]


class FundSemanticEventJudgmentResponse(BaseModel):
    """模型对一条事件的语义判断，以及 Python 校验后的实际状态。"""

    # Pydantic 默认把 model_ 当作内部命名空间；这里的 model_direction 是业务字段。
    model_config = ConfigDict(protected_namespaces=())

    event_id: str
    title: str
    event_date: date
    source_name: str
    source_url: str | None = None
    model_direction: SemanticDirection
    effective_direction: SemanticDirection
    relevance: SemanticRelevance
    horizon: SemanticHorizon
    confidence: float = Field(ge=0, le=1)
    review_status: Literal["accepted", "abstained"]
    reason: str
    baseline_direction: AssessmentDirection | None = None
    differs_from_rule_baseline: bool | None = None


class FundSemanticReviewRequest(BaseModel):
    """可选的用户原问题；Agent 会用它告诉研判模型用户真正关心什么。"""

    question: str | None = Field(default=None, min_length=1, max_length=500)


class FundSemanticInterpretationResponse(BaseModel):
    """受约束的大模型语义层；它不会覆盖确定性指标和规则分。"""

    status: Literal["completed", "partial", "abstained", "no_events"]
    model: str
    prompt_version: str
    generated_at: datetime
    is_predictive: bool
    allows_conditional_opinion: bool
    calibration_status: Literal["uncalibrated"]
    user_question: str | None = None
    proposed_overall_view: Literal[
        "supportive", "caution", "mixed", "insufficient"
    ]
    effective_overall_view: Literal[
        "supportive", "caution", "mixed", "insufficient"
    ]
    overall_confidence: float = Field(ge=0, le=1)
    summary: str
    event_judgments: list[FundSemanticEventJudgmentResponse]
    reviewed_event_count: int
    omitted_event_count: int
    accepted_count: int
    abstained_count: int
    conflict_count: int
    conflicts: list[str]
    uncertainties: list[str]
    guardrails: list[str]


class FundHybridAssessmentResponse(BaseModel):
    """历史事实、规则信号和模型语义判断组成的三层研判响应。"""

    model_config = ConfigDict(protected_namespaces=())

    data_kind: Literal["hybrid_fund_assessment"]
    fund_code: str
    short_name: str
    period_months: int
    assessment_date: date
    layer_contract: dict[str, str]
    baseline_assessment: FundAssessmentResponse
    model_interpretation: FundSemanticInterpretationResponse
    disclaimer: str


class PortfolioHoldingInput(BaseModel):
    """组合初始持仓；旧版权重与真实金额模式二选一。"""

    fund_code: str = Field(pattern=r"^\d{6}$")
    weight_pct: float | None = Field(default=None, gt=0, le=100)
    units: Decimal | None = Field(default=None, gt=0, le=10**12, allow_inf_nan=False)
    cost_amount: Decimal | None = Field(default=None, gt=0, le=10**12, allow_inf_nan=False)
    purchase_date: date | None = None


class PortfolioCreateRequest(BaseModel):
    """创建基金组合；可提交真实份额成本或旧版目标权重。"""

    user_id: str = Field(
        default="guest",
        min_length=1,
        max_length=50,
        pattern=r"^[\w-]+$",
    )
    name: str = Field(min_length=1, max_length=80)
    holdings: list[PortfolioHoldingInput] = Field(min_length=1, max_length=10)


class PortfolioUpdateRequest(PortfolioCreateRequest):
    """整体替换组合名称和持仓，避免部分更新产生权重不完整状态。"""


class PortfolioRenameRequest(BaseModel):
    """真实持仓组合只改名称，不覆盖交易流水。"""

    user_id: str = Field(default="guest", min_length=1, max_length=50)
    name: str = Field(min_length=1, max_length=80)


class PortfolioHoldingResponse(BaseModel):
    """持仓快照、最新市值和移动平均成本盈亏。"""

    fund_code: str
    short_name: str
    fund_type: str
    sample_role: str
    weight_pct: float
    position_mode: Literal["allocation", "actual"] = "allocation"
    units: float | None = None
    reserved_units: float = 0
    available_units: float | None = None
    cost_amount: float | None = None
    average_cost_nav: float | None = None
    first_purchase_date: date | None = None
    latest_nav: float
    latest_nav_date: date
    current_value: float | None = None
    unrealized_pnl_amount: float | None = None
    unrealized_pnl_pct: float | None = None
    realized_pnl_amount: float = 0.0
    total_pnl_amount: float | None = None


class PortfolioResponse(BaseModel):
    """一份持久化组合；真实持仓会额外返回资金与盈亏汇总。"""

    id: str
    user_id: str
    name: str
    holding_count: int
    total_weight_pct: float
    position_mode: Literal["allocation", "actual", "empty"] = "allocation"
    total_cost_amount: float | None = None
    total_market_value: float | None = None
    unrealized_pnl_amount: float | None = None
    realized_pnl_amount: float = 0.0
    total_pnl_amount: float | None = None
    total_return_pct: float | None = None
    pending_order_count: int = 0
    pending_buy_amount: str = "0.00"
    unsettled_redemption_amount: str = "0.00"
    holdings: list[PortfolioHoldingResponse]
    created_at: datetime
    updated_at: datetime


class PortfolioListResponse(BaseModel):
    """当前用户的全部基金组合。"""

    user_id: str
    count: int
    portfolios: list[PortfolioResponse]


class PortfolioDeleteResponse(BaseModel):
    """删除组合后的最小确认结构。"""

    ok: bool
    portfolio_id: str


class FundTransactionTimingRequest(BaseModel):
    """下单时间必须带时区；场外申赎与证券账户的场内买卖不能共用规则。"""

    submitted_at: AwareDatetime
    transaction_type: Literal["buy", "sell"]
    channel: Literal["off_exchange", "exchange"] = "off_exchange"


class FundTransactionTimingResponse(FundTransactionTimingRequest):
    """日历推算不是交易平台回执，所有日期都属于有条件的估算。"""

    fund_code: str
    rule_version: str
    status: Literal["estimated", "manual_required", "calendar_unavailable"]
    reason: str
    nav_date: date | None = None
    confirmation_date: date | None = None
    first_return_nav_date: date | None = None
    last_return_nav_date: date | None = None
    visibility: str
    warnings: list[str] = Field(default_factory=list)
    sources: list[str] = Field(default_factory=list)
    calendar_through: date | None = None
    # 保存流水时补上实际回单日期。允许回单覆盖通用估算，但不会悄悄改写回单。
    recorded_nav_date: date | None = None


class PortfolioTransactionCreateRequest(BaseModel):
    """新增一笔买入或卖出；金额由份额乘成交净值确定。"""

    user_id: str = Field(default="guest", min_length=1, max_length=50)
    # 浏览器在首次提交时生成，网络失败后用同一个 key 重试。后端不会把 key 当作
    # transaction_id，而是用它判断“这是不是同一笔业务请求”。
    idempotency_key: str = Field(
        min_length=8,
        max_length=100,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{7,99}$",
    )
    fund_code: str = Field(pattern=r"^\d{6}$")
    transaction_type: Literal["buy", "sell"]
    trade_date: date
    # 接收字符串或 JSON 数字；前端提交字符串可避免 JS 先丢失小数精度。
    units: Decimal = Field(gt=0, le=10**12, allow_inf_nan=False)
    unit_price: Decimal = Field(gt=0, le=10**12, allow_inf_nan=False)
    fee: Decimal = Field(default=Decimal("0"), ge=0, le=10**12, allow_inf_nan=False)
    note: str = Field(default="", max_length=200)
    submitted_at: AwareDatetime | None = None
    channel: Literal["off_exchange", "exchange"] = "off_exchange"


class PortfolioTransactionResponse(BaseModel):
    """交易流水及该笔卖出按移动平均成本计算的已实现盈亏。"""

    id: str
    idempotency_key: str | None = None
    # 仅创建接口重放旧结果时为 true；流水列表中的普通读取保持 false。
    idempotency_replayed: bool = False
    portfolio_id: str
    fund_code: str
    short_name: str
    transaction_type: Literal["buy", "sell"]
    trade_date: date
    units: float
    unit_price: float
    gross_amount: float
    fee: float
    cash_flow: float
    realized_pnl_amount: float | None = None
    running_units: float
    note: str = ""
    created_at: datetime
    timing: FundTransactionTimingResponse | None = None
    exact_values: dict[str, str] = Field(default_factory=dict)
    effective: bool = True
    status: Literal["posted", "corrected", "reversed"] = "posted"
    correction: dict[str, Any] | None = None


class PortfolioTransactionListResponse(BaseModel):
    """一份组合的完整交易流水。"""

    portfolio_id: str
    count: int
    transactions: list[PortfolioTransactionResponse]


class PortfolioTransactionDeleteResponse(BaseModel):
    ok: bool
    portfolio_id: str
    transaction_id: str


class PortfolioPerformancePointResponse(BaseModel):
    """收益与回撤图中的一个日频点。"""

    date: date
    portfolio_index: float
    benchmark_index: float | None = None
    drawdown_pct: float
    market_value: float
    net_flow: float
    daily_return_pct: float


class PortfolioPerformanceBenchmarkResponse(BaseModel):
    """持仓基金专属业绩基准混合后的同区间比较。"""

    benchmark_name: str
    status: Literal["matched", "proxy", "unavailable", "insufficient"]
    period_return_pct: float | None = None
    portfolio_aligned_return_pct: float | None = None
    excess_return_pct: float | None = None
    observation_count: int
    coverage_pct: float
    period_start: date | None = None
    period_end: date | None = None
    source: str | None = None
    source_urls: list[str]
    methodology: str
    warnings: list[str]


class PortfolioPerformanceCashFlowResponse(BaseModel):
    """所选区间的期初资产、外部资金和模型期末价值。"""

    opening_value: float | None = None
    contributions: float | None = None
    withdrawals: float | None = None
    fees: float | None = None
    net_invested: float | None = None
    ending_value: float | None = None


class PortfolioPerformanceAttributionResponse(BaseModel):
    """单只基金对组合金额盈亏和 TWR 的贡献。"""

    fund_code: str
    short_name: str
    ending_weight_pct: float
    pnl_contribution_amount: float | None = None
    return_contribution_pct_points: float
    return_basis: str
    return_basis_label: str


class PortfolioPerformanceResponse(BaseModel):
    """交易调整后的真实业绩，或旧目标配置的明确标注模拟业绩。"""

    data_kind: Literal[
        "actual_portfolio_performance",
        "simulated_allocation_performance",
    ]
    performance_label: str
    portfolio_id: str
    portfolio_name: str
    position_mode: Literal["actual", "allocation"]
    period_months: int
    period_start: date
    period_end: date
    observation_count: int
    twr_pct: float
    xirr_pct: float | None = None
    annualized_return_pct: float
    annualized_volatility_pct: float
    sharpe_ratio: float | None = None
    sortino_ratio: float | None = None
    risk_free_rate_annual_pct: float
    risk_free_rate_source: str
    max_drawdown: dict[str, Any]
    benchmark: PortfolioPerformanceBenchmarkResponse
    cash_flows: PortfolioPerformanceCashFlowResponse
    attribution: list[PortfolioPerformanceAttributionResponse]
    series: list[PortfolioPerformancePointResponse]
    data_sources: list[dict[str, Any]]
    assumptions: list[str]
    warnings: list[str]
    methodology: str
    disclaimer: str


PortfolioDecisionAction = Literal[
    "watch",
    "hold",
    "add",
    "reduce",
    "rebalance",
]


class PortfolioDecisionCreateRequest(BaseModel):
    """保存一条决策，并在服务端冻结当时的指标和研究证据。"""

    user_id: str = Field(default="guest", min_length=1, max_length=50)
    action: PortfolioDecisionAction
    thesis: str = Field(min_length=5, max_length=2000)
    horizon_days: Literal[30, 90, 180] = 90
    research_job_id: str | None = Field(default=None, max_length=64)


class PortfolioDecisionReviewRequest(BaseModel):
    """到观察期后记录复盘结论，不修改原始决策快照。"""

    user_id: str = Field(default="guest", min_length=1, max_length=50)
    review_note: str = Field(min_length=3, max_length=2000)


class PortfolioDecisionOutcomeResponse(BaseModel):
    """用决策后真实组合曲线计算的阶段表现。"""

    status: Literal["tracking", "review_ready", "reviewed", "unavailable"]
    as_of_date: date | None = None
    elapsed_days: int
    target_days: int
    portfolio_return_pct: float | None = None
    benchmark_return_pct: float | None = None
    excess_return_pct: float | None = None
    note: str


class PortfolioDecisionResponse(BaseModel):
    """决策事实、不可变快照和动态后续表现。"""

    id: str
    portfolio_id: str
    decision_date: date
    target_date: date
    action: PortfolioDecisionAction
    thesis: str
    horizon_days: int
    research_job_id: str | None = None
    snapshot: dict[str, Any]
    outcome: PortfolioDecisionOutcomeResponse
    review_note: str
    reviewed_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class PortfolioDecisionListResponse(BaseModel):
    portfolio_id: str
    count: int
    decisions: list[PortfolioDecisionResponse]


class PortfolioDecisionDeleteResponse(BaseModel):
    ok: bool
    portfolio_id: str
    decision_id: str


class PortfolioConcentrationResponse(BaseModel):
    """只基于持仓权重计算的集中度，不依赖大模型。"""

    hhi: float
    effective_fund_count: float
    max_weight_pct: float
    max_weight_fund_code: str
    state_code: str
    state_label: str


class PortfolioAllocationResponse(BaseModel):
    """宽基、行业主题和债券等样例角色的权重汇总。"""

    sample_role: str
    label: str
    weight_pct: float


class PortfolioFundContributionResponse(BaseModel):
    """一只基金对组合支撑分和风险分的加权贡献。"""

    fund_code: str
    short_name: str
    weight_pct: float
    assessment_state_code: str
    assessment_state_label: str
    caution_points: float
    supportive_points: float
    weighted_caution_points: float
    weighted_supportive_points: float


class PortfolioAssessmentEvidenceResponse(BaseModel):
    """去重后的组合证据，以及它覆盖了哪些持仓。"""

    evidence_id: str
    kind: AssessmentEvidenceKind
    direction: AssessmentDirection
    strength: int = Field(ge=1, le=3)
    reliability: AssessmentReliability
    relevance: AssessmentRelevance
    title: str
    value_text: str
    explanation: str
    data_date: date
    source_name: str
    source_url: str | None = None
    event_id: str | None = None
    cluster_id: str | None = None
    cluster_member_count: int | None = Field(default=None, ge=1)
    cluster_source_count: int | None = Field(default=None, ge=1)
    heat_score: float | None = Field(default=None, ge=0, le=100)
    heat_level: Literal["low", "medium", "high"] | None = None
    affected_fund_codes: list[str]
    portfolio_weight_coverage_pct: float
    weighted_points: float
    decay_factor: float = Field(default=1.0, ge=0, le=1)
    freshness_status: Literal["fresh", "decaying", "expired"] | None = None
    valid_until: date | None = None
    age_days: int | None = Field(default=None, ge=0)


class PortfolioAssessmentScoreResponse(BaseModel):
    """组合层将单基金证据分按持仓权重汇总后的结果。"""

    supportive_points: float
    caution_points: float
    net_points: float
    method: str


class PortfolioAssessmentResponse(BaseModel):
    """实际持仓或目标配置的集中度与证据研判，明确两种权重来源。"""

    data_kind: str
    portfolio_id: str
    user_id: str
    portfolio_name: str
    position_mode: str | None = None
    weight_basis: str | None = None
    period_months: int
    assessment_date: date
    state_code: str
    state_label: str
    confidence: Literal["high", "medium", "low"]
    confidence_reason: str
    summary: str
    concentration: PortfolioConcentrationResponse
    allocations: list[PortfolioAllocationResponse]
    fund_contributions: list[PortfolioFundContributionResponse]
    score_breakdown: PortfolioAssessmentScoreResponse
    risk_evidence: list[PortfolioAssessmentEvidenceResponse]
    supporting_evidence: list[PortfolioAssessmentEvidenceResponse]
    neutral_evidence: list[PortfolioAssessmentEvidenceResponse]
    evidence_count: int
    event_count: int
    event_cluster_count: int = 0
    active_event_count: int = 0
    expired_event_count: int = 0
    missing_information: list[str]
    limitations: list[str]
    methodology: str
    disclaimer: str


class PortfolioExposureSourceFundResponse(BaseModel):
    """一项底层暴露由哪只基金贡献，以及两层权重如何相乘。"""

    fund_code: str
    short_name: str
    fund_weight_pct: float
    within_fund_weight_pct: float
    portfolio_exposure_pct: float
    report_date: date


class PortfolioSecurityExposureResponse(BaseModel):
    """同一股票或债券跨基金合并后的组合暴露。"""

    evidence_id: str
    asset_type: Literal["stock", "bond"]
    security_code: str
    security_name: str
    portfolio_exposure_pct: float
    source_fund_count: int
    report_date: date
    report_dates: list[date]
    source_url: str
    source_funds: list[PortfolioExposureSourceFundResponse]


class PortfolioIndustryExposureResponse(BaseModel):
    """基金股票行业配置按组合市值权重汇总后的结果。"""

    evidence_id: str
    industry_name: str
    portfolio_exposure_pct: float
    source_fund_count: int
    report_date: date
    report_dates: list[date]
    source_url: str
    source_funds: list[PortfolioExposureSourceFundResponse]


class PortfolioAssetExposureResponse(BaseModel):
    """股票行业、固定收益和未穿透资产三类概览。"""

    code: Literal["equity_industry", "fixed_income", "unpenetrated"]
    label: str
    portfolio_exposure_pct: float


class PortfolioCommonSecurityResponse(BaseModel):
    """两只基金共同持有的一只底层证券。"""

    asset_type: Literal["stock", "bond"]
    security_code: str
    security_name: str
    combined_portfolio_exposure_pct: float


class PortfolioFundOverlapResponse(BaseModel):
    """两只基金的证券重合度和它在当前组合中的实际暴露。"""

    left_fund_code: str
    left_fund_name: str
    right_fund_code: str
    right_fund_name: str
    common_security_count: int
    fund_overlap_pct: float
    combined_portfolio_exposure_pct: float
    top_common_securities: list[PortfolioCommonSecurityResponse]


class PortfolioExposureFundReportResponse(BaseModel):
    """组合中一只基金采用的报告期、来源和穿透覆盖情况。"""

    fund_code: str
    short_name: str
    portfolio_weight_pct: float
    data_mode: Literal["fixture", "database", "unavailable"]
    report_dates: FundDisclosureReportDatesResponse
    latest_report_date: date | None = None
    report_age_days: int | None = None
    freshness_status: Literal["fresh", "aging", "stale", "unavailable"]
    security_disclosure_pct: float
    security_coverage_pct: float
    industry_asset_disclosure_pct: float
    industry_asset_coverage_pct: float
    unpenetrated_pct: float
    source: str
    source_url: str
    warnings: list[str]


class PortfolioExposureQualityResponse(BaseModel):
    """覆盖率与新鲜度共同决定结果应被怎样理解。"""

    status: Literal["complete", "partial", "limited", "unavailable"]
    confidence: Literal["high", "medium", "low"]
    security_coverage_pct: float
    industry_asset_coverage_pct: float
    unpenetrated_security_pct: float
    freshness_status: Literal["fresh", "aging", "stale", "unavailable"]
    unavailable_fund_codes: list[str]
    warnings: list[str]


class PortfolioExposureConcentrationResponse(BaseModel):
    """底层证券和股票行业的集中度摘要。"""

    known_security_hhi: float
    known_security_count: int
    top_security_exposure_pct: float
    top_security_code: str | None = None
    top_security_name: str | None = None
    security_state: Literal["high", "moderate", "diversified", "insufficient"]
    top_industry_exposure_pct: float
    top_industry_name: str | None = None
    industry_state: Literal["high", "moderate", "diversified", "insufficient"]
    overall_state: Literal[
        "high", "moderate", "diversified", "insufficient", "partial"
    ]


class PortfolioExposureResponse(BaseModel):
    """组合最新季报下的确定性底层持仓穿透。"""

    data_kind: Literal["deterministic_portfolio_lookthrough"]
    portfolio_id: str
    user_id: str
    portfolio_name: str
    calculated_at: date
    oldest_report_date: date | None = None
    latest_report_date: date | None = None
    summary: str
    data_quality: PortfolioExposureQualityResponse
    concentration: PortfolioExposureConcentrationResponse
    asset_exposures: list[PortfolioAssetExposureResponse]
    security_exposures: list[PortfolioSecurityExposureResponse]
    industry_exposures: list[PortfolioIndustryExposureResponse]
    fund_overlaps: list[PortfolioFundOverlapResponse]
    fund_reports: list[PortfolioExposureFundReportResponse]
    gross_security_exposure_pct: float
    gross_industry_asset_exposure_pct: float
    methodology: str
    limitations: list[str]
    disclaimer: str


class PortfolioEventSectorExposureResponse(BaseModel):
    """版本化细分行业词典覆盖到的已披露股票重仓。"""

    sector_name: str
    portfolio_exposure_pct: float
    security_count: int = Field(ge=1)
    security_codes: list[str]
    security_names: list[str]
    aliases: list[str]


class PortfolioEventMatchedSecurityResponse(BaseModel):
    """一条事件直接或通过细分行业匹配到的底层证券。"""

    asset_type: Literal["stock", "bond"]
    security_code: str
    security_name: str
    sector_name: str | None = None
    portfolio_exposure_pct: float
    evidence_id: str
    report_date: date


class PortfolioEventMatchedSectorResponse(BaseModel):
    sector_name: str
    portfolio_exposure_pct: float
    security_count: int = Field(ge=1)
    matched_keywords: list[str]


class PortfolioEventMatchedIndustryResponse(BaseModel):
    industry_name: str
    portfolio_exposure_pct: float
    matched_keywords: list[str]


class PortfolioEventMatchedFundResponse(BaseModel):
    fund_code: str
    short_name: str
    portfolio_weight_pct: float


class PortfolioEventImpactItemResponse(BaseModel):
    """一条市场事件与当前组合之间的确定性关联结果。"""

    impact_evidence_id: str
    event_id: str
    cluster_id: str | None = None
    title: str
    event_date: date
    event_type: MarketEventType
    direction: Literal["supportive", "caution", "neutral"]
    horizon: MarketImpactHorizon
    severity: int = Field(ge=1, le=3)
    confidence: float = Field(ge=0, le=1)
    relevance: Literal["direct", "related", "context", "uncertain"]
    freshness_status: Literal["fresh", "decaying", "expired"]
    decay_factor: float = Field(ge=0, le=1)
    source_name: str
    source_level: MarketSourceLevel
    source_url: str
    affected_exposure_pct: float = Field(ge=0, le=100)
    # priority_score 只是排序关注度，不使用百分比上限，也不表示收益概率。
    priority_score: float = Field(ge=0)
    match_basis: Literal[
        "direct_security",
        "fine_sector",
        "broad_industry",
        "direct_fund",
    ]
    match_label: str
    matched_keywords: list[str]
    matched_securities: list[PortfolioEventMatchedSecurityResponse]
    matched_sectors: list[PortfolioEventMatchedSectorResponse]
    matched_industries: list[PortfolioEventMatchedIndustryResponse]
    matched_funds: list[PortfolioEventMatchedFundResponse]
    reason: str
    uncertainties: list[str]


class PortfolioEventImpactQualityResponse(BaseModel):
    """说明归因使用了多少有效事件和多少已分类底层仓位。"""

    status: Literal["good", "partial", "limited", "no_matches"]
    active_event_count: int = Field(ge=0)
    matched_event_count: int = Field(ge=0)
    directional_event_count: int = Field(ge=0)
    unmatched_event_count: int = Field(ge=0)
    semantic_accepted_count: int = Field(ge=0)
    skipped_unreviewed_count: int = Field(ge=0)
    security_coverage_pct: float = Field(ge=0, le=100)
    classified_stock_exposure_pct: float = Field(ge=0)
    unclassified_stock_exposure_pct: float = Field(ge=0)
    taxonomy_version: str
    taxonomy_source: str


class PortfolioEventImpactResponse(BaseModel):
    """当前有效事件对一份基金组合的持仓级影响归因。"""

    data_kind: Literal["deterministic_portfolio_event_impacts"]
    portfolio_id: str
    user_id: str
    portfolio_name: str
    calculated_at: date
    event_data_cutoff: date
    latest_holding_report_date: date | None = None
    summary: str
    quality: PortfolioEventImpactQualityResponse
    caution_event_count: int = Field(ge=0)
    supportive_event_count: int = Field(ge=0)
    neutral_event_count: int = Field(ge=0)
    sector_exposures: list[PortfolioEventSectorExposureResponse]
    impacts: list[PortfolioEventImpactItemResponse]
    total_matched: int = Field(ge=0)
    omitted_impact_count: int = Field(ge=0)
    methodology: str
    limitations: list[str]
    disclaimer: str


class PortfolioStressRequest(BaseModel):
    """新建情景或在旧情景上比较配置。候选权重不会写入真实持仓。"""

    model_config = ConfigDict(extra="forbid")
    user_id: str = Field(default="guest", min_length=1, max_length=50)
    months: Literal[6, 12, 36] | None = None
    window_days: Literal[20, 40, 60] | None = None
    source_run_id: str | None = Field(default=None, pattern=r"^stress_[a-f0-9]{16}$")
    target_weights: dict[
        Annotated[str, Field(pattern=r"^\d{6}$")],
        Annotated[float, Field(ge=0, le=100, allow_inf_nan=False)],
    ] | None = Field(default=None, max_length=20)
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=100)


class PortfolioStressReport(BaseModel):
    """确定性压力测试报告，曲线与贡献由后端计算，不接受模型改写。"""

    version: Literal["portfolio-stress-v1"] = "portfolio-stress-v1"
    run_id: str
    root_run_id: str
    portfolio_id: str
    portfolio_name: str
    created_at: str
    captured_at: str
    input_hash: str
    engine_version: str
    months: int
    window: dict[str, Any]
    holdings: list[dict[str, Any]]
    scenarios: list[dict[str, Any]]
    facts: list[dict[str, Any]]
    data_sources: list[dict[str, Any]]
    limitations: list[str]
    stages: list[str]


class PortfolioResearchStartRequest(BaseModel):
    """启动一次 LangGraph 组合研究时需要的最小输入。"""

    user_id: str = Field(default="guest", min_length=1, max_length=50)
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=100)
    months: Literal[6, 12, 36] = 12
    research_mode: Literal["auto", "explain", "strategy", "standard", "committee"] = "standard"
    question: str = Field(default="", max_length=800)
    allow_public_refresh: bool = False
    investor_context: dict[Literal["horizon", "loss_tolerance", "liquidity"], str] = Field(default_factory=dict)


class PortfolioResearchRunSummaryResponse(BaseModel):
    """历史研究列表中的轻量条目，不重复携带整份报告。"""

    job_id: str
    portfolio_id: str
    portfolio_name: str
    period_months: Literal[6, 12, 36]
    completed_at: str
    assessment_date: str | None = None
    headline: str
    rule_state_code: str | None = None
    rule_state_label: str | None = None
    model_state_code: str | None = None
    snapshot_id: str
    content_hash: str
    graph_version: str


class PortfolioResearchHistoryResponse(BaseModel):
    """同组合、同观察区间的已完成研究，以及页面要恢复的最近报告。"""

    data_kind: Literal["portfolio_research_history"]
    user_id: str
    portfolio_id: str
    period_months: Literal[6, 12, 36] | None = None
    count: int = Field(ge=0)
    omitted_count: int = Field(default=0, ge=0)
    message: str = ""
    runs: list[PortfolioResearchRunSummaryResponse] = Field(default_factory=list)
    latest_report: dict[str, Any] | None = None


class PortfolioResearchChangeSectionResponse(BaseModel):
    """一条快照分区哈希对比；哈希相同表示该路结构化输入完全相同。"""

    section: str
    label: str
    changed: bool
    before_hash: str
    after_hash: str


class PortfolioResearchChangeFactResponse(BaseModel):
    """由 Python 确定的前后变化事实，模型只能引用 fact_key。"""

    fact_key: str
    section: str
    section_label: str
    kind: str
    label: str
    before_value: Any = None
    after_value: Any = None
    before_text: str
    after_text: str
    delta: float | None = None
    delta_text: str = ""
    tone: Literal["supportive", "caution", "neutral"] = "neutral"


class PortfolioResearchChangeDriverResponse(BaseModel):
    """模型对一组真实变化事实的通俗解释。"""

    tone: Literal["supportive", "caution", "neutral"]
    title: str
    detail: str
    fact_keys: list[str]
    references: list[PortfolioResearchChangeFactResponse]


class PortfolioResearchChangeInterpretationResponse(BaseModel):
    """受约束模型解释；模型失败时 status=fallback，差异事实仍然有效。"""

    status: Literal["accepted", "fallback"]
    model_used: bool
    model: str
    prompt_version: str
    attempt_count: int = Field(ge=0)
    generated_at: str
    headline: str
    summary: str
    drivers: list[PortfolioResearchChangeDriverResponse] = Field(default_factory=list)
    uncertainties: list[str] = Field(default_factory=list)
    follow_up_items: list[str] = Field(default_factory=list)
    fallback_reason: str | None = None


class PortfolioResearchChangeAuditResponse(BaseModel):
    """同一组合两次研究的完整、可追溯对比结果。"""

    data_kind: Literal["portfolio_research_change_audit"]
    audit_version: str
    comparison_id: str
    generated_at: str
    user_id: str
    portfolio_id: str
    period_months: Literal[6, 12, 36]
    base_run: PortfolioResearchRunSummaryResponse
    target_run: PortfolioResearchRunSummaryResponse
    input_changed: bool
    conclusion_changed: bool
    change_scope: Literal[
        "none",
        "wording_only",
        "limited",
        "multi_source",
        "broad",
    ]
    changed_section_count: int = Field(ge=0, le=6)
    changed_sections: list[PortfolioResearchChangeSectionResponse]
    facts: list[PortfolioResearchChangeFactResponse]
    interpretation: PortfolioResearchChangeInterpretationResponse
    limitations: list[str]
    methodology: str


class PortfolioMonitoringPolicyUpdateRequest(BaseModel):
    """用户在组合工作台保存的监控频率、偏好和确定性触发阈值。"""

    user_id: str = Field(
        default="guest",
        min_length=1,
        max_length=50,
        pattern=r"^[\w-]+$",
    )
    enabled: bool = False
    risk_preference: Literal["conservative", "balanced", "growth"] = "balanced"
    investment_horizon: Literal[
        "under_1y",
        "one_to_three_years",
        "over_3y",
    ] = "one_to_three_years"
    monitor_months: Literal[6, 12, 36] = 12
    max_single_adjustment_pct: float = Field(default=15, ge=1, le=50)
    drawdown_alert_pct_points: float = Field(default=3, ge=0.5, le=30)
    concentration_alert_pct_points: float = Field(default=5, ge=1, le=50)
    major_event_heat_threshold: float = Field(default=70, ge=0, le=100)
    event_impact_exposure_threshold_pct: float = Field(
        default=5,
        ge=1,
        le=100,
    )
    alert_on_risk_state_worsening: bool = True
    schedule_cron: str = Field(default="0 21 * * 1-5", min_length=9, max_length=100)


class PortfolioMonitoringPolicyResponse(BaseModel):
    """数据库策略及其对应的稳定 Cron ID。"""

    user_id: str
    portfolio_id: str
    enabled: bool
    risk_preference: str
    investment_horizon: str
    monitor_months: Literal[6, 12, 36]
    max_single_adjustment_pct: float
    drawdown_alert_pct_points: float
    concentration_alert_pct_points: float
    major_event_heat_threshold: float
    event_impact_exposure_threshold_pct: float
    alert_on_risk_state_worsening: bool
    schedule_cron: str
    schedule_id: str
    persisted: bool
    created_at: str | None = None
    updated_at: str | None = None


class PortfolioRiskSnapshotResponse(BaseModel):
    """一次监控运行留下的风险事实，下一次运行会与它比较。"""

    id: str
    run_id: str
    portfolio_id: str
    period_months: Literal[6, 12, 36]
    assessment_date: date
    state_code: str
    state_label: str
    caution_points: float
    supportive_points: float
    annualized_volatility_pct: float
    max_drawdown_pct: float
    daily_cvar_95_pct: float
    concentration_hhi: float
    max_weight_pct: float
    major_events: list[dict[str, Any]] = Field(default_factory=list)
    event_impacts: list[dict[str, Any]] = Field(default_factory=list)
    source_summary: dict[str, Any] = Field(default_factory=dict)
    comparison: dict[str, Any] = Field(default_factory=dict)
    generation: dict[str, Any] = Field(default_factory=dict)
    observed_at: datetime


class PortfolioAlertResponse(BaseModel):
    """一条由 Python 阈值命中的预警，以及模型可选的解释。"""

    id: str
    portfolio_id: str
    snapshot_id: str
    alert_type: str
    severity: Literal["medium", "high"]
    title: str
    detail: str
    metric_data: dict[str, Any] = Field(default_factory=dict)
    explanation: dict[str, Any] | None = None
    status: Literal["open", "acknowledged"]
    created_at: datetime
    acknowledged_at: datetime | None = None


class PortfolioMonitoringOverviewResponse(BaseModel):
    """组合工作台一次加载所需的完整监控视图。"""

    portfolio_id: str
    policy: PortfolioMonitoringPolicyResponse
    latest_snapshot: PortfolioRiskSnapshotResponse | None = None
    previous_snapshot: PortfolioRiskSnapshotResponse | None = None
    alerts: list[PortfolioAlertResponse] = Field(default_factory=list)
    open_alert_count: int = 0


class PortfolioMonitoringRunRequest(BaseModel):
    """手动立即运行监控时只需标识当前用户。"""

    user_id: str = Field(default="guest", min_length=1, max_length=50)
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=100)


class PortfolioAlertActionRequest(BaseModel):
    """确认已阅读某条预警。"""

    user_id: str = Field(default="guest", min_length=1, max_length=50)


class PortfolioAdjustmentStartRequest(BaseModel):
    """基于一份已完成组合研究报告启动 HITL 调整方案。"""

    user_id: str = Field(default="guest", min_length=1, max_length=50)
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=100)
    source_research_job_id: str = Field(
        ...,
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$",
    )


class PortfolioAdjustmentResumeRequest(BaseModel):
    """用户在 LangGraph interrupt 页面提交的真实约束。"""

    user_id: str = Field(default="guest", min_length=1, max_length=50)
    decision: Literal["continue", "cancel"] = "continue"
    risk_preference: Literal["conservative", "balanced", "growth"] = "balanced"
    investment_horizon: Literal[
        "under_1y",
        "one_to_three_years",
        "over_3y",
    ] = "one_to_three_years"
    max_single_adjustment_pct: float = Field(default=15, ge=1, le=50)
    max_single_fund_weight_pct: float = Field(default=70, ge=10, le=100)
    max_turnover_pct: float = Field(default=20, ge=0, le=100)
    additional_constraints: str = Field(default="", max_length=1000)


class UserResponse(BaseModel):
    """用户相关接口的响应结构。"""

    user_id: str
    name: str
    role: str

    # 给字段默认值后，返回数据里可以不显式传 active。
    active: bool = True


class ChatRequest(BaseModel):
    """聊天接口的请求体结构。

    客户端 POST JSON 到 `/chat` 或 `/chat/stream` 时，需要符合这个结构。
    """

    # Field 可以增加校验规则。这里要求 message 长度在 1 到 500 之间。
    message: str = Field(..., min_length=1, max_length=500)

    # 如果请求里不传 user_id，就默认当作 guest 用户。
    user_id: str = Field(default="guest", min_length=1, max_length=50)


class ChatResponse(BaseModel):
    """普通聊天接口的响应结构。"""

    user_id: str
    answer: str
    used_model: str


class LcelChatResponse(BaseModel):
    """LCEL 学习接口的响应结构。

    比普通 ChatResponse 多了 chain_steps，方便你从接口返回里直接看到
    这次请求经历了哪些 LangChain 组件。
    """

    user_id: str
    answer: str
    used_model: str
    chain_steps: list[str]


class MessageAnalysis(BaseModel):
    """结构化输出的核心结果。

    这个模型不是给用户直接提交的请求体，而是约束大模型必须返回的字段。
    LangChain 会让模型尽量按这个 Pydantic schema 生成结果。
    """

    intent: str = Field(..., description="用户输入的主要意图，例如 learning_plan、debug、chat。")
    difficulty: Literal["easy", "medium", "hard"] = Field(
        ...,
        description="完成这个请求的难度等级。",
    )
    next_action: str = Field(..., description="建议用户下一步做什么。")
    reason: str = Field(..., description="为什么给出这个判断和下一步建议。")
    key_concepts: list[str] = Field(..., description="这个请求涉及的关键概念。")
    confidence: float = Field(..., ge=0, le=1, description="模型对分析结果的置信度。")


class LcelAnalyzeResponse(BaseModel):
    """LCEL 结构化输出接口的响应结构。"""

    user_id: str
    analysis: MessageAnalysis
    used_model: str
    chain_steps: list[str]


class RunnableDemoResponse(BaseModel):
    """Runnable Playground 的通用响应结构。"""

    ability: str
    title: str
    input: Any
    output: Any
    steps: list[str]
    explanation: str


class RagQueryRequest(BaseModel):
    """RAG 问答接口的请求体结构。

    RAG 查询比普通聊天多了 top_k，用来控制最多召回几个文档片段。
    """

    # 用户真正想问的问题。
    # 比普通聊天放宽到 800 字，方便你问稍微复杂一点的文档问题。
    message: str = Field(..., min_length=1, max_length=800)

    # user_id 目前主要用于学习和调试，方便知道是谁发起的请求。
    # 还没有接入真正的用户系统。
    user_id: str = Field(default="guest", min_length=1, max_length=50)

    # top_k 表示最多召回几个 chunk。
    # 值越大，模型能看到的资料越多，但无关内容也可能变多。
    top_k: int = Field(default=3, ge=1, le=8)


class RagChunk(BaseModel):
    """一个被检索出来的文档片段。

    真实 RAG 系统里，这类对象通常还会包含向量分数、文档来源、页码等信息。
    """

    # 片段唯一编号，例如 rag-basics-001。
    chunk_id: str

    # 片段来自哪个文件，例如 data/docs/rag_basics.md。
    source: str

    # 片段的正文内容，也就是可能会被塞进 prompt 的资料。
    content: str

    # 当前 demo 里的相关性分数。
    # /rag/chunks 只是查看所有 chunk，所以 score 是 None；
    # /rag/query 经过检索排序，所以会有具体分数。
    score: float | None = None

    # 额外信息，例如 chunk_index、start、end。
    # 它不是模型必须看的内容，主要给开发者调试切块效果。
    metadata: dict[str, Any] = Field(default_factory=dict)


class RagReloadResponse(BaseModel):
    """重新加载本地知识库后的响应结构。"""

    # 成功读取了几个文档文件。
    document_count: int

    # 所有文档一共切出了几个 chunk。
    chunk_count: int

    # 这次加载了哪些来源文件。
    sources: list[str]

    # 给前端看的简短提示。
    message: str


class RagChunksResponse(BaseModel):
    """查看当前内存中所有 RAG 文档片段的响应结构。"""

    # 当前内存里 chunk 的总数。
    chunk_count: int

    # chunk 详情列表。
    chunks: list[RagChunk]


class RagQueryResponse(BaseModel):
    """RAG 问答接口的响应结构。"""

    # 发起请求的用户 ID。
    user_id: str

    # 原始问题，原样返回方便前端展示和调试。
    question: str

    # 大模型基于检索资料生成的最终回答。
    answer: str

    # 本次回答参考的资料片段。
    # 这是 RAG 和普通聊天最大的区别之一：回答要能追溯来源。
    sources: list[RagChunk]

    # 实际使用的模型名称。
    used_model: str

    # 这次请求经过的步骤，专门为了学习 LCEL/RAG 流程而返回。
    chain_steps: list[str]


class RagFrameworkDocument(BaseModel):
    """LlamaIndex 版 RAG 中的 Document 信息。

    Document 是“原始资料容器”，通常对应一个文件、一条数据库记录或一个网页。
    """

    doc_id: str
    source: str
    file_name: str | None = None
    file_type: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    excluded_llm_metadata_keys: list[str] = Field(default_factory=list)
    excluded_embed_metadata_keys: list[str] = Field(default_factory=list)


class RagFrameworkNode(BaseModel):
    """LlamaIndex 版 RAG 中的 Node 信息。

    Node 是从 Document 切出来、真正参与 embedding、索引和检索的 chunk。
    """

    node_id: str
    ref_doc_id: str | None = None
    source: str
    text: str
    score: float | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    relationships: dict[str, str | None] = Field(default_factory=dict)


class RagFrameworkReloadResponse(BaseModel):
    """LlamaIndex 框架版 RAG 重新构建索引后的响应结构。"""

    document_count: int
    node_count: int
    index_ready: bool
    documents: list[RagFrameworkDocument]
    parser_summary: dict[str, int]
    pipeline_steps: list[str]
    message: str


class RagFrameworkNodesResponse(BaseModel):
    """查看 LlamaIndex 框架版 Document / Node 的响应结构。"""

    document_count: int
    node_count: int
    documents: list[RagFrameworkDocument]
    nodes: list[RagFrameworkNode]


class RagFrameworkQueryResponse(BaseModel):
    """LlamaIndex 框架版 RAG 问答响应结构。"""

    user_id: str
    question: str
    answer: str
    retrieved_nodes: list[RagFrameworkNode]
    used_model: str
    pipeline_steps: list[str]


class RagSplitterComparisonItem(BaseModel):
    """一个分块器的对比结果。"""

    parser: str
    target_documents: str
    node_count: int
    average_chars: float
    max_chars: int
    sample_text: str
    note: str


class RagSplitterComparisonResponse(BaseModel):
    """多个 NodeParser / TextSplitter 的对比结果。"""

    document_count: int
    comparisons: list[RagSplitterComparisonItem]
    conclusion: str


class VectorRagHit(BaseModel):
    """向量库检索命中的一个 chunk。"""

    chunk_id: str
    document_id: str | None = None
    source: str
    text: str
    distance: float | None = None
    similarity: float | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class VectorRagRebuildResponse(BaseModel):
    """真实 embedding + Chroma 向量库重建后的响应结构。"""

    document_count: int
    chunk_count: int
    collection_name: str
    persist_dir: str
    embedding_provider: str
    embedding_model: str
    embedding_dimensions: int
    sample_chunk: VectorRagHit | None = None
    pipeline_steps: list[str]
    message: str


class VectorRagStatsResponse(BaseModel):
    """Chroma 向量库状态。"""

    collection_name: str
    persist_dir: str
    chunk_count: int
    embedding_provider: str
    embedding_model: str
    embedding_dimensions: int
    is_ready: bool


class VectorRagSearchResponse(BaseModel):
    """只做向量检索、不调用大模型的响应结构。"""

    question: str
    top_k: int
    hits: list[VectorRagHit]
    embedding_model: str
    pipeline_steps: list[str]


class VectorRagQueryResponse(BaseModel):
    """真实向量库 RAG 问答响应结构。"""

    user_id: str
    question: str
    answer: str
    hits: list[VectorRagHit]
    used_model: str
    embedding_model: str
    pipeline_steps: list[str]


class AgentToolInfo(BaseModel):
    """一个可被 Agent 调用的工具说明。

    这个结构对应“告诉模型工具能干什么”的那部分信息。
    """

    name: str
    description: str
    args_schema: dict[str, Any] = Field(default_factory=dict)


class AgentToolsResponse(BaseModel):
    """查看当前后端注册了哪些工具。"""

    tools: list[AgentToolInfo]


class AgentChatRequest(BaseModel):
    """Agent 工具调用接口的请求体。

    它和普通聊天很像，但多了 top_k，因为 rag_search_tool 需要知道最多召回几个 chunk。
    """

    message: str = Field(..., min_length=1, max_length=800)
    user_id: str = Field(default="guest", min_length=1, max_length=50)
    # conversation_id 由后端创建。为空时仍可把核心 Agent 当作无状态接口使用，
    # Background Job 等内部调用不会被迫创建一条聊天记录。
    conversation_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=64,
    )
    # 浏览器每次“发送”生成一个 key，网络失败后的重试继续复用它。后端据此返回首次
    # 已完成结果，不会重复调用模型或重复保存一轮会话。
    idempotency_key: str | None = Field(
        default=None,
        pattern=r"^[a-zA-Z0-9:_-]{8,64}$",
    )
    # 基金页面会传入当前选中的基金。它只是一段可信的业务上下文，不会替代工具查询；
    # 模型仍需调用基金 Tool 才能拿到净值和档案数据。
    fund_code: str | None = Field(
        default=None,
        pattern=r"^\d{6}$",
        description="FundScope 当前选中的六位基金代码。",
    )
    # 组合页面会传入当前选中的持久化组合，模型仍需调用组合 Tool 获取权重和研判。
    portfolio_id: str | None = Field(
        default=None,
        pattern=r"^portfolio_[a-f0-9]{16}$",
        description="FundScope 当前选中的模拟组合 ID。",
    )
    top_k: int = Field(default=3, ge=1, le=8)
    max_tool_rounds: int = Field(
        default=10,
        ge=1,
        le=10,
        description="最多允许 Agent 连续进行几轮工具调用，防止死循环。",
    )
    # 仅作用于这一次发送，不存成偏好，也不让模型通过 tool_args 开启。
    research_refresh: bool = Field(
        default=False,
        description="本轮允许综合研究补查缺失/过旧的公开持仓披露；默认只读已保存数据。",
    )
    # None 沿用同一会话的报告；空串解除关联；具体编号必须由服务端校验。
    research_job_id: str | None = Field(default=None, max_length=64,
        pattern=r"^[a-zA-Z0-9_-]*$", description="继续追问所引用的已完成组合研究报告。")
    # None 沿用上轮，[] 解除引用；两个 ID 冻结两期原文，后续同步不会偷换证据。
    fund_report_ids: list[str] | None = Field(default=None, max_length=2)


class AgentToolCall(BaseModel):
    """记录一次模型决定调用工具的过程。

    这个不是给模型看的，而是返回给前端和学习者看的调试信息。
    """

    tool_name: str
    tool_args: dict[str, Any] = Field(default_factory=dict)
    tool_result: str
    is_error: bool = False
    # 缓存复用仍对应一次模型调用意图，但不冒充重新访问了数据源。
    execution_kind: Literal["executed", "cached"] = "executed"
    # 区分模型发起一次调用与后端实际尝试次数，不把内部重试伪装成新模型轮次。
    recovery_trace: list[dict[str, Any]] = Field(default_factory=list)


class AgentTodoItem(BaseModel):
    """Agent 当前短期计划中的一条 todo。

    这是 s05 TodoWrite 的学习重点：
    它只表示“当前这次复杂任务准备怎么做”，不是长期任务数据库。
    """

    id: str
    content: str
    status: Literal["pending", "in_progress", "completed"]


class AgentTaskInfo(BaseModel):
    """s12 Task System 中一条持久化任务的展示结构。

    这个类描述 `/agent/chat` 响应里的 current_tasks 字段。
    它不是写入 JSON 的原始 TaskRecord，而是给前端/测试/用户看的公开结构，
    所以会额外带上 unresolved_dependencies、is_blocked、can_claim 这些派生字段。
    """

    # id/user_id/subject/description/status/owner/blocked_by 来自持久化任务本身。
    id: str
    user_id: str
    subject: str
    description: str = ""
    status: Literal["pending", "in_progress", "completed", "cancelled"]
    owner: str | None = None
    blocked_by: list[str] = Field(default_factory=list)
    # s18 只公开安全名称；绝对路径由 Worktree 状态接口单独返回。
    worktree_name: str | None = None
    # unresolved_dependencies 是 blocked_by 里还没 completed 的那部分依赖。
    unresolved_dependencies: list[str] = Field(default_factory=list)
    # is_blocked=True 表示当前任务还不能被认领。
    is_blocked: bool = False
    # can_claim=True 表示它是 pending，并且所有依赖都已完成。
    can_claim: bool = False
    # created_at/updated_at/path 用来帮助你在本地定位和观察任务 JSON 文件。
    created_at: str
    updated_at: str
    path: str


class JobRecoveryInfo(BaseModel):
    """任务公开的恢复状态；执行权 Token 和内部投递凭据不进入响应。"""

    enabled: bool
    execution_version: int
    recovery_count: int
    failure_retry_count: int
    max_recoveries: int
    last_recovery_reason: str | None = None
    heartbeat_at: str | None = None
    deadline_at: str | None = None


class JobResourceScopeInfo(BaseModel):
    """只公开组合范围和授权引用，不包含任务 Prompt 或内部执行输入。"""

    portfolio_id: str | None = None
    portfolio_ids: list[str] | None = None
    task_grant_id: str | None = None
    task_run_id: str | None = None
    task_agent_id: str | None = None


class AgentBackgroundJobInfo(BaseModel):
    """s13 Background Tasks 中一条后台运行记录的公开结构。

    它和 ``AgentTaskInfo`` 的区别是：Task 描述“要做什么”，Job 描述“一次实际运行
    现在进行到哪一步”。工程化版本会把它保存到 SQLite/PostgreSQL。
    """

    # id 是业务 Job ID；celery_task_id 是底层队列 ID，两者不要混用。
    id: str
    user_id: str
    title: str
    # job_type 决定 JobRunner 执行 demo、主 Agent 还是 teammate 工作流。
    job_type: Literal[
        "demo_steps",
        "agent_prompt",
        "fund_catalog_import",
        "fund_report_sync",
        "fund_report_eval",
        "fund_nav_sync",
        "market_snapshot_sync",
        "large_capital_sync",
        "market_event_sync",
        "market_event_analysis",
        "market_event_clustering",
        "social_signal_browser_login",
        "social_signal_analysis",
        "social_signal_sync",
        "market_intelligence_pipeline",
        "portfolio_research",
        "portfolio_adjustment",
        "portfolio_monitor",
        "teammate_message",
        "teammate_protocol",
        "autonomous_teammate",
    ] = "demo_steps"
    # queued -> running -> completed/failed/cancelled 是主要状态流转。
    status: Literal[
        "queued",
        "running",
        "waiting_input",
        "completed",
        "failed",
        "cancelled",
    ]
    execution_backend: Literal["local", "celery"] = "local"
    # current_step/total_steps 用于计算进度，不表示模型 token 数量。
    current_step: int
    total_steps: int
    progress_percent: float
    progress_message: str
    # 旧任务可能没有范围信息；新任务也只通过类型化白名单公开这些字段。
    payload: JobResourceScopeInfo | None = None
    # 成功看 result，失败看 error；运行中二者通常都为空。
    result: dict[str, Any] | None = None
    error: str | None = None
    # cancel_requested 是协作式取消信号，Worker 在 Harness 检查点读取它。
    cancel_requested: bool = False
    attempt_count: int = 0
    max_retries: int = 0
    timeout_seconds: int = 300
    # 重复业务请求使用相同幂等键，会返回原 Job 而不是重复建单。
    idempotency_key: str | None = None
    celery_task_id: str | None = None
    # 排障关联号，不携带凭据，也不允许据此绕过 Job 的用户归属检查。
    trace_id: str | None = None
    recovery: JobRecoveryInfo | None = None
    created_at: str
    updated_at: str
    started_at: str | None = None
    completed_at: str | None = None


class BackgroundJobStartRequest(BaseModel):
    """通过 REST 接口提交持久化 Background Job / AgentRun。"""

    user_id: str = Field(default="guest", min_length=1, max_length=50)
    title: str = Field(..., min_length=1, max_length=200)
    total_steps: int = Field(default=8, ge=1, le=20)
    step_delay_seconds: float = Field(default=0.4, ge=0.05, le=5.0)
    job_id: str | None = Field(default=None, min_length=1, max_length=64)
    # teammate_message 是内部类型，必须通过 Message Bus 创建，故不暴露在公共 REST 请求中。
    job_type: Literal[
        "demo_steps",
        "agent_prompt",
        "fund_nav_sync",
        "market_snapshot_sync",
        "large_capital_sync",
        "market_event_sync",
        "market_event_analysis",
        "market_event_clustering",
        "social_signal_sync",
        "market_intelligence_pipeline",
    ] = "demo_steps"
    # agent_prompt 会把 prompt 交给现有 Tool Calling Agent；为空时复用 title。
    prompt: str | None = Field(default=None, min_length=1, max_length=8000)
    # 这里只表达用户选择，实际归属与父任务范围由授权服务验证。
    portfolio_id: str | None = Field(default=None, pattern=r"^portfolio_[a-f0-9]{16}$")
    max_tool_rounds: int = Field(default=10, ge=1, le=10)
    max_retries: int | None = Field(default=None, ge=0, le=5)
    timeout_seconds: int | None = Field(default=None, ge=1, le=3600)
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=100)


class BackgroundJobActionRequest(BaseModel):
    """查询路径已包含 job_id 时，用请求体传递当前用户。"""

    user_id: str = Field(default="guest", min_length=1, max_length=50)


class BackgroundJobResponse(BaseModel):
    """启动、查询或取消单个后台 Job 的统一响应。"""

    ok: bool
    job: AgentBackgroundJobInfo
    accepted: bool | None = None
    message: str = ""


class BackgroundJobListResponse(BaseModel):
    """后台 Job 列表接口响应。"""

    ok: bool
    user_id: str
    count: int
    total_count: int
    jobs: list[AgentBackgroundJobInfo] = Field(default_factory=list)


class AgentTeammateInfo(BaseModel):
    """s15 Agent Teams 中一个长期成员的公开状态。

    这是 API/Tool 的展示模型，不是数据库 ORM；它刻意不暴露 SQLAlchemy 内部对象。
    """

    # id 用于数据库唯一定位，name 用于 Message Bus 寻址。
    id: str
    user_id: str
    name: str
    # 角色和专属指令进入该 teammate 的 system prompt。
    role: str
    instructions: str = ""
    # 能力标签会映射到实际绑定的受限工具。
    capabilities: list[str] = Field(default_factory=list)
    # current_job_id 只在 queued/working 等运行状态下通常有值。
    status: Literal["idle", "queued", "working", "failed", "offline"]
    current_job_id: str | None = None
    last_result: str | None = None
    last_error: str | None = None
    created_at: str
    updated_at: str


class AgentTeamMessageInfo(BaseModel):
    """Message Bus 中一条普通文本消息；正式协议会在 s16 再加入。"""

    # sender/recipient 保存稳定成员名；main-agent 是允许出现的保留逻辑身份。
    id: str
    user_id: str
    sender: str
    recipient: str
    content: str
    # job_id 指向处理这条消息的后台运行；dispatch=False 时可以为空。
    job_id: str | None = None
    # teammate 回复通过 in_reply_to 关联原消息，也用于 Worker 重投去重。
    in_reply_to: str | None = None
    # is_read 是由 read_at 推导出的前端友好字段。
    is_read: bool = False
    read_at: str | None = None
    created_at: str


class TeammateCreateRequest(BaseModel):
    """通过 REST 注册一个有稳定身份的 teammate。"""

    user_id: str = Field(default="guest", min_length=1, max_length=50)
    # name 创建后作为长期寻址名称；Service 还会限制字符集合并拒绝 main-agent。
    name: str = Field(..., min_length=1, max_length=50)
    role: str = Field(..., min_length=1, max_length=100)
    instructions: str = Field(default="", max_length=2000)
    # 空列表表示默认四种只读能力；write_workspace 必须显式选择。
    capabilities: list[
        Literal[
            "calculate",
            "rag_search",
            "read_project",
            "search_project",
            "write_workspace",
        ]
    ] = Field(default_factory=list, max_length=8)


class TeammateResponse(BaseModel):
    """创建或查询单个 teammate 的响应。"""

    ok: bool
    teammate: AgentTeammateInfo
    message: str = ""


class TeammateListResponse(BaseModel):
    """当前用户的 Agent Team 成员列表。"""

    ok: bool
    user_id: str
    count: int
    teammates: list[AgentTeammateInfo] = Field(default_factory=list)


class TeamMessageSendRequest(BaseModel):
    """main-agent 给 teammate 发送消息并选择是否立即交给 Worker。"""

    user_id: str = Field(default="guest", min_length=1, max_length=50)
    recipient: str = Field(..., min_length=1, max_length=50)
    content: str = Field(..., min_length=1, max_length=4000)
    # false 只写 inbox；true 还会创建异步 teammate_message Job。
    dispatch: bool = True
    # 三个运行参数限制子 Agent 循环、失败重试和总时间。
    max_rounds: int = Field(default=3, ge=1, le=5)
    max_retries: int = Field(default=1, ge=0, le=3)
    timeout_seconds: int = Field(default=300, ge=1, le=1800)


class TeamMessageSendResponse(BaseModel):
    """消息写入结果，以及可选的后台 teammate AgentRun。"""

    ok: bool
    # message 总会存在；只有 dispatch 成功时 job 才存在。
    message: AgentTeamMessageInfo
    job: AgentBackgroundJobInfo | None = None
    dispatched: bool = False
    # s17 中已有自治 Worker 时不会重复派 Job，而是由现有循环读取这条 inbox 消息。
    delivery_mode: Literal["message_only", "background_job", "autonomy_wakeup"] = (
        "message_only"
    )
    autonomy_session_id: str | None = None


class TeamInboxResponse(BaseModel):
    """一个团队成员的 inbox。"""

    ok: bool
    user_id: str
    recipient: str
    # count 是本次返回条数；unread_count 是该收件箱当前未读总数。
    count: int
    unread_count: int
    messages: list[AgentTeamMessageInfo] = Field(default_factory=list)


class AgentTeamProtocolInfo(BaseModel):
    """s16 中一条持久化 ProtocolState 的公开结构。"""

    request_id: str
    user_id: str
    protocol_type: Literal["shutdown", "plan_approval"]
    sender: str
    target: str
    status: Literal["pending", "approved", "rejected"]
    payload: str
    feedback: str = ""
    request_event_id: str
    response_event_id: str | None = None
    job_id: str | None = None
    created_at: str
    updated_at: str
    resolved_at: str | None = None


class AgentTeamProtocolEventInfo(BaseModel):
    """协议 Message Bus 中一条带 request_id 和 message_type 的 typed event。"""

    id: str
    user_id: str
    request_id: str
    message_type: Literal[
        "shutdown_request",
        "shutdown_response",
        "plan_approval_request",
        "plan_approval_response",
    ]
    sender: str
    recipient: str
    content: str
    approve: bool | None = None
    created_at: str


class TeamPlanSubmitRequest(BaseModel):
    """确定性 REST 测试：模拟 teammate 向 main-agent 提交计划。"""

    user_id: str = Field(default="guest", min_length=1, max_length=50)
    teammate_name: str = Field(..., min_length=1, max_length=50)
    plan: str = Field(..., min_length=1, max_length=4000)


class TeamShutdownRequest(BaseModel):
    """main-agent 向指定 teammate 发起关闭握手。"""

    user_id: str = Field(default="guest", min_length=1, max_length=50)
    teammate_name: str = Field(..., min_length=1, max_length=50)
    reason: str = Field(default="", max_length=4000)
    # true 进入 Worker 自动分发；false 只保存 pending 请求，主要用于观察协议状态。
    dispatch: bool = True


class TeamPlanReviewRequest(BaseModel):
    """main-agent 对 pending plan_approval 请求做一次明确决策。"""

    user_id: str = Field(default="guest", min_length=1, max_length=50)
    approve: bool
    feedback: str = Field(default="", max_length=2000)


class TeamProtocolResponse(BaseModel):
    """创建、查询或响应单个 Team Protocol 的统一响应。"""

    ok: bool
    request: AgentTeamProtocolInfo
    request_event: AgentTeamProtocolEventInfo | None = None
    response_event: AgentTeamProtocolEventInfo | None = None
    events: list[AgentTeamProtocolEventInfo] = Field(default_factory=list)
    job: AgentBackgroundJobInfo | None = None
    dispatched: bool = False
    deduplicated: bool = False
    message: str = ""


class TeamProtocolListResponse(BaseModel):
    """当前用户的 ProtocolState 和最近协议事件。"""

    ok: bool
    user_id: str
    count: int
    protocols: list[AgentTeamProtocolInfo] = Field(default_factory=list)
    recent_events: list[AgentTeamProtocolEventInfo] = Field(default_factory=list)


class AgentAutonomySessionInfo(BaseModel):
    """s17 自治 teammate 的持久化生命周期快照。"""

    id: str
    user_id: str
    teammate_name: str
    job_id: str
    status: Literal[
        "queued", "running", "idle", "working", "stopping",
        "completed", "failed", "cancelled",
    ]
    current_task_id: str | None = None
    completed_task_ids: list[str] = Field(default_factory=list)
    poll_interval_seconds: float
    max_idle_polls: int
    idle_poll_count: int
    max_tasks: int
    tasks_completed: int
    max_rounds_per_task: int
    stop_requested: bool
    stop_reason: str | None = None
    last_event: str
    last_error: str | None = None
    created_at: str
    updated_at: str
    started_at: str | None = None
    completed_at: str | None = None


class AutonomyTaskCreateRequest(BaseModel):
    """给 s17 Task Board 准备一条可由空闲 teammate 主动领取的任务。"""

    user_id: str = Field(default="guest", min_length=1, max_length=50)
    task_id: str | None = Field(default=None, max_length=64)
    subject: str = Field(..., min_length=1, max_length=200)
    description: str = Field(default="", max_length=4000)
    blocked_by: list[str] = Field(default_factory=list, max_length=20)


class AutonomySessionStartRequest(BaseModel):
    """启动受限自治循环；每个限制都由 Harness 强制执行。"""

    user_id: str = Field(default="guest", min_length=1, max_length=50)
    teammate_name: str = Field(..., min_length=1, max_length=50)
    poll_interval_seconds: float = Field(default=2.0, ge=0.05, le=30)
    max_idle_polls: int = Field(default=5, ge=1, le=60)
    max_tasks: int = Field(default=5, ge=1, le=20)
    max_rounds_per_task: int = Field(default=3, ge=1, le=5)
    timeout_seconds: int = Field(default=300, ge=5, le=3600)


class AutonomySessionActionRequest(BaseModel):
    """停止自治会话时只需当前用户身份。"""

    user_id: str = Field(default="guest", min_length=1, max_length=50)


class AutonomySessionResponse(BaseModel):
    """创建、查询或停止单个自治会话的统一响应。"""

    ok: bool
    session: AgentAutonomySessionInfo
    job: AgentBackgroundJobInfo | None = None
    message: str = ""


class AutonomySessionListResponse(BaseModel):
    """当前用户可观察到的自治会话列表。"""

    ok: bool
    user_id: str
    count: int
    sessions: list[AgentAutonomySessionInfo] = Field(default_factory=list)


class WorktreeReadinessResponse(BaseModel):
    """当前运行环境是否满足真实 Git Worktree 前置条件。"""

    ready: bool
    git_available: bool
    repository_root: str
    worktrees_root: str
    head_commit: str | None = None
    current_branch: str | None = None
    reason: str


class AgentWorktreeInfo(BaseModel):
    """s18 中一条 Worktree 的持久化状态和最新 Git 指标。"""

    user_id: str
    name: str
    task_id: str | None = None
    branch_name: str
    path: str
    repository_root: str
    base_ref: str
    base_commit: str
    status: Literal["active", "kept", "removed", "failed"]
    # dirty 表示尚未提交的文件数；ahead 表示相对创建基点新增的 commit 数。
    dirty_file_count: int = 0
    ahead_commit_count: int = 0
    last_error: str | None = None
    created_at: str
    updated_at: str
    removed_at: str | None = None


class AgentWorktreeEventInfo(BaseModel):
    """创建、保留、删除等不可变 Worktree 生命周期事件。"""

    id: str
    user_id: str
    worktree_name: str
    task_id: str | None = None
    event_type: Literal["created", "kept", "removed"]
    detail: str
    created_at: str


class WorktreeCreateRequest(BaseModel):
    """从一个 Git ref 创建 Worktree，并可绑定现有 pending Task。"""

    user_id: str = Field(default="guest", min_length=1, max_length=50)
    name: str = Field(..., min_length=1, max_length=64)
    task_id: str | None = Field(default=None, max_length=64)
    base_ref: str = Field(default="HEAD", min_length=1, max_length=200)


class WorktreeActionRequest(BaseModel):
    """保留或删除 Worktree 时使用的控制参数。"""

    user_id: str = Field(default="guest", min_length=1, max_length=50)
    # remove 默认不允许丢数据；只有用户明确勾选时才传 true。
    discard_changes: bool = False


class WorktreeResponse(BaseModel):
    """创建、查询、保留或删除单个 Worktree 的统一响应。"""

    ok: bool = True
    worktree: AgentWorktreeInfo
    events: list[AgentWorktreeEventInfo] = Field(default_factory=list)
    message: str = ""


class WorktreeListResponse(BaseModel):
    """Worktree 列表以及当前 Git 环境就绪状态。"""

    ok: bool
    user_id: str
    count: int
    worktrees: list[AgentWorktreeInfo] = Field(default_factory=list)
    readiness: WorktreeReadinessResponse


class AgentMcpServerInfo(BaseModel):
    """s19 中一个 MCP Server 的公开连接状态。"""

    name: str
    description: str
    transport: str
    status: str
    connected: bool
    protocol_version: str | None = None
    server_info: dict[str, Any] = Field(default_factory=dict)
    tool_count: int = 0
    tool_names: list[str] = Field(default_factory=list)
    connected_at: str | None = None
    error: str | None = None


class AgentMcpToolInfo(BaseModel):
    """通过 MCP tools/list 动态发现的一条外部工具定义。"""

    server_name: str
    original_name: str
    name: str
    description: str
    input_schema: dict[str, Any] = Field(default_factory=dict)
    annotations: dict[str, Any] = Field(default_factory=dict)
    read_only: bool


class AgentMcpCallTrace(BaseModel):
    """一次 MCP tools/call 的轻量运行轨迹。"""

    server_name: str
    tool_name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    ok: bool
    error: str | None = None
    started_at: str
    duration_ms: float


class McpStateResponse(BaseModel):
    """MCP REST 控制面的完整状态。"""

    ok: bool = True
    servers: list[AgentMcpServerInfo] = Field(default_factory=list)
    tools: list[AgentMcpToolInfo] = Field(default_factory=list)
    recent_calls: list[AgentMcpCallTrace] = Field(default_factory=list)


class McpServerActionResponse(BaseModel):
    """连接、刷新或断开一个 Server 后的结果。"""

    ok: bool = True
    action: Literal["connect", "refresh", "disconnect"]
    server: AgentMcpServerInfo
    tools: list[AgentMcpToolInfo] = Field(default_factory=list)


class McpCallRequest(BaseModel):
    """不经过大模型，直接测试 MCP tools/call 的请求。"""

    tool_name: str = Field(
        ...,
        min_length=1,
        max_length=160,
        description="完整名称，例如 mcp__agent-learning__search_course_notes。",
    )
    arguments: dict[str, Any] = Field(default_factory=dict)


class McpCallResponse(BaseModel):
    """直接调用 MCP 工具后的结构化结果。"""

    ok: bool = True
    result: dict[str, Any]
    recent_calls: list[AgentMcpCallTrace] = Field(default_factory=list)


class AgentCronScheduleInfo(BaseModel):
    """s14 Cron Scheduler 中一条定时定义的公开结构。"""

    id: str
    user_id: str
    cron: str
    prompt: str
    job_type: Literal[
        "demo_steps",
        "agent_prompt",
        "fund_nav_sync",
        "market_snapshot_sync",
        "market_event_sync",
        "market_intelligence_pipeline",
        "portfolio_monitor",
    ] = "demo_steps"
    job_payload: dict[str, Any] = Field(default_factory=dict)
    recurring: bool
    durable: bool
    created_at: str
    last_fired_at: str | None = None
    last_fire_marker: str | None = None
    trigger_count: int = 0
    last_background_job_id: str | None = None
    last_error: str | None = None
    path: str | None = None


class AgentCronDeliveryInfo(BaseModel):
    """一次 Cron 事件从 Queue 交付给 Background Job 的记录。"""

    event_id: str
    schedule_id: str
    user_id: str
    prompt: str
    job_type: Literal[
        "demo_steps",
        "agent_prompt",
        "fund_nav_sync",
        "market_snapshot_sync",
        "market_event_sync",
        "market_intelligence_pipeline",
        "portfolio_monitor",
    ] = "demo_steps"
    source: Literal["scheduler", "manual"]
    fired_at: str
    delivered_at: str | None = None
    status: Literal["queued", "delivered", "failed"]
    background_job_id: str | None = None
    error: str | None = None


class CronScheduleCreateRequest(BaseModel):
    """通过 REST 接口注册五段式 Cron 的请求体。"""

    user_id: str = Field(default="guest", min_length=1, max_length=50)
    cron: str = Field(..., min_length=9, max_length=100)
    prompt: str = Field(..., min_length=1, max_length=500)
    job_type: Literal[
        "demo_steps",
        "agent_prompt",
        "fund_nav_sync",
        "market_snapshot_sync",
        "market_event_sync",
        "market_intelligence_pipeline",
        "portfolio_monitor",
    ] = "demo_steps"
    job_payload: dict[str, Any] = Field(default_factory=dict)
    recurring: bool = True
    durable: bool = True
    schedule_id: str | None = Field(default=None, min_length=1, max_length=64)


class CronScheduleActionRequest(BaseModel):
    """取消或手动触发路径已指定的 Schedule。"""

    user_id: str = Field(default="guest", min_length=1, max_length=50)


class CronScheduleResponse(BaseModel):
    """创建或取消一个 Cron Schedule 的响应。"""

    ok: bool
    schedule: AgentCronScheduleInfo
    message: str = ""


class CronFireEventInfo(BaseModel):
    """时间命中或手动触发时写入 Queue 的事件。"""

    id: str
    schedule_id: str
    user_id: str
    prompt: str
    job_type: Literal[
        "demo_steps",
        "agent_prompt",
        "fund_nav_sync",
        "market_snapshot_sync",
        "market_event_sync",
        "market_intelligence_pipeline",
        "portfolio_monitor",
    ] = "demo_steps"
    job_payload: dict[str, Any] = Field(default_factory=dict)
    fired_at: str
    source: Literal["scheduler", "manual"]


class CronTriggerResponse(BaseModel):
    """手动触发 Schedule 的响应，用来快速测试 Queue。"""

    ok: bool
    event: CronFireEventInfo
    queue_depth: int
    message: str = ""


class CronScheduleListResponse(BaseModel):
    """当前用户 Cron、Queue 和最近交付状态。"""

    ok: bool
    user_id: str
    count: int
    schedules: list[AgentCronScheduleInfo] = Field(default_factory=list)
    queue_depth: int
    recent_deliveries: list[AgentCronDeliveryInfo] = Field(default_factory=list)
    scheduler_running: bool


class AgentHookTrace(BaseModel):
    """工具执行前后 hook 的运行轨迹。"""

    event: str
    hook_name: str
    status: str
    message: str
    metadata: dict[str, Any] = Field(default_factory=dict)


class AgentLoopRound(BaseModel):
    """Agent Loop 中的一轮模型决策记录。

    一轮的意思是：模型看完当前 messages 后，决定“继续调用工具”或“直接回答”。
    """

    round_index: int
    model_action: str
    assistant_message: str = ""
    tool_calls: list[AgentToolCall] = Field(default_factory=list)


class AgentContextCompactTrace(BaseModel):
    """Context Compact 的运行轨迹。

    这是 s08 的学习重点：
    它告诉你这次 Agent Loop 有没有把过长工具结果压缩掉。
    """

    event: str
    round_index: int
    before_chars: int
    after_chars: int
    compacted_messages: int
    reason: str
    saved_paths: list[str] = Field(default_factory=list)
    details: list[dict[str, Any]] = Field(default_factory=list)


class AgentMemoryInfo(BaseModel):
    """长期记忆的轻量展示结构。

    这是 s09 Memory 的学习重点：
    它不是当前任务计划，而是跨请求保存下来的用户偏好、项目事实和反馈。
    """

    id: str
    memory_type: str
    user_id: str
    content_preview: str
    tags: list[str] = Field(default_factory=list)
    source: str = ""
    importance: int = 3
    created_at: str = ""
    path: str
    score: int = 0


class AgentPromptSection(BaseModel):
    """s10 System Prompt 中一个可观察的区段。"""

    name: str
    title: str
    content: str
    char_count: int


class AgentPromptTrace(BaseModel):
    """本次请求实际组装出的系统提示词调试信息。

    模型最终收到的是一个完整字符串；这里额外保留分段结果，
    方便学习每一段规则和动态上下文来自哪里。
    """

    section_count: int
    total_chars: int
    sections: list[AgentPromptSection] = Field(default_factory=list)


class AgentRecoveryTrace(BaseModel):
    """s11 Error Recovery 的一次恢复动作。

    正常模型调用不会产生记录；只有重试、压缩后重试、续写或恢复失败时，
    才会出现在 `/agent/chat` 的 recovery_trace 中。
    """

    event: str
    phase: str
    round_index: int
    attempt: int
    max_attempts: int
    action: str
    error_type: str
    message: str
    delay_seconds: float = 0.0
    metadata: dict[str, Any] = Field(default_factory=dict)


class AgentTokenUsage(BaseModel):
    """一次 Agent 运行中能够从模型响应读取到的 token 用量。

    LangChain 会把模型供应商返回的 usage_metadata 放在 AIMessage 上。这里把不同
    供应商的字段名统一，方便前端、评测和数据库使用同一种结构。
    """

    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0


class AgentRouteInfo(BaseModel):
    """动态工具池在本次请求中选择出的能力范围。"""

    name: str = "full"
    domains: list[str] = Field(default_factory=list)
    reason: str = ""
    strategy: Literal["rule", "model", "fallback", "clarification"] = "rule"
    confidence: float = Field(default=1.0, ge=0, le=1)
    needs_clarification: bool = False


class AgentEvidenceReference(BaseModel):
    """从本轮真实 Tool 结果提取的一条可展示证据引用。"""

    evidence_id: str
    title: str
    source_name: str = ""
    source_url: str | None = None
    data_date: str | None = None
    direction: str | None = None
    tool_name: str


class FundAnswerFact(BaseModel):
    """后端从真实研究工具中登记的事实；标题和来源不由模型自由编写。"""

    evidence_id: str
    fund_code: str
    label: str
    value: str
    unit: str
    period: str
    source_path: str
    source_name: str
    source_url: str | None = None
    meaning: str = ""


class FundAnswerEvidenceRelation(BaseModel):
    """引用由后端核对，为什么支持或反对则是模型给用户的简短解释。"""

    evidence_id: str
    explanation: str = Field(min_length=1, max_length=600)


class FundAnswerCondition(BaseModel):
    """条件必须标清来源，不能把模型的假设写成用户已经说过的话。"""

    basis: Literal["user_context", "assumption", "missing_information"]
    text: str = Field(min_length=1, max_length=600)
    background_fields: list[Literal["horizon", "loss_tolerance", "liquidity", "purpose"]] = Field(max_length=4)


class FundAnswerBackground(BaseModel):
    """后端复制本轮真实用户背景，历史回看时不读取其他会话或最新状态。"""

    label: str
    text: str
    source_turn: str
    updated_at: str


class FundAnswerJudgment(BaseModel):
    """模型的解释或条件式观点；引用存在不等于推理正确。"""

    text: str
    # evidence_ids 保留给旧消费者；v2 由后端合并正反两类引用，不让模型重复维护。
    evidence_ids: list[str] = Field(default_factory=list)
    supporting_evidence: list[FundAnswerEvidenceRelation] = Field(default_factory=list)
    counter_evidence: list[FundAnswerEvidenceRelation] = Field(default_factory=list)
    supporting_evidence_gap: str = ""
    counter_evidence_gap: str = ""
    conditions: list[FundAnswerCondition] = Field(default_factory=list)
    change_triggers: list[str] = Field(default_factory=list)


class FundAnswerReport(BaseModel):
    """单基金回答的核对结果，随会话保存，REST 和 SSE 共用。"""

    # 默认值保留 v1，旧消息反序列化不会被伪装成具有新证据解释的报告。
    version: Literal["fund-answer-v1", "fund-answer-v2"] = "fund-answer-v1"
    fund_code: str
    fund_name: str
    status: Literal["consistent", "corrected", "limited"]
    repair_attempts: int = 0
    facts: list[FundAnswerFact] = Field(default_factory=list)
    judgments: list[FundAnswerJudgment] = Field(default_factory=list)
    investment_background: dict[str, FundAnswerBackground] = Field(default_factory=dict)
    uncertainties: list[str] = Field(default_factory=list)
    # 保存诊断而不是错误草稿，避免历史回看又把被删除的错数字当作答案。
    issues: list[str] = Field(default_factory=list)
    verification_scope: str = (
        "仅核对关键事实与本轮工具数据是否一致；未验证数据源真实性、"
        "模型解释或未来走势。"
    )


class FundReportAnswerReport(BaseModel):
    """季报回答的引用核对记录；保存实际引用与范围，不冒充语义正确性评分。

    资产变化、章节范围沿用季报工具的数据结构；statements由专用校验器验证后
    补齐原文、文档和页码。错误草稿不进入此对象，也不会在历史消息中重新出现。
    """

    # 新回答用编号原句协议；旧历史记录仍可读取，不强制迁移已保存的正文。
    version: Literal["fund-report-answer-v1", "fund-report-answer-v2", "fund-report-answer-v3"] = "fund-report-answer-v1"
    fund_code: str
    status: Literal["consistent", "corrected", "limited"]
    repair_attempts: int = Field(default=0, ge=0, le=1)
    # 记录本题实际选择的上下文与长度，不把数字题未读取经理章节说成“原文没有”。
    query_profile: dict[str, Any] = Field(default_factory=dict)
    # 与引用一起保存阶段耗时和实际整理模式，旧消息无此字段时默认空对象。
    execution: dict[str, Any] = Field(default_factory=dict)
    reading_scope: list[dict[str, Any]] = Field(default_factory=list)
    allocations: list[dict[str, Any]] = Field(default_factory=list)
    statements: list[dict[str, Any]] = Field(default_factory=list)
    unanswered: bool = False
    issues: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    verification_scope: str = "仅核对引用归属、原文匹配和阅读范围；未判定模型解释、引用支持性、主题是否遗漏或数据源真实性。"


class AgentToolDiscoveryTrace(BaseModel):
    """按需加载的可核对记录；Schema 字符数和真实模型 Token 用量分别统计。"""

    enabled: bool = False
    catalog_size: int = 0
    initial_tools: list[str] = Field(default_factory=list)
    loaded_tools: list[str] = Field(default_factory=list)
    searches: list[dict[str, Any]] = Field(default_factory=list)
    bindings: list[dict[str, Any]] = Field(default_factory=list)


class AgentLoopControl(BaseModel):
    """Loop 的可观察控制结果；evidence_ready 只指取证覆盖，不代表答案语义正确。"""

    version: str = "loop-control-v1"
    enabled: bool = False
    cache_hits: int = 0
    rounds: list[dict[str, Any]] = Field(default_factory=list)
    required: list[str] = Field(default_factory=list)
    covered: list[str] = Field(default_factory=list)
    missing: list[str] = Field(default_factory=list)
    completion_status: Literal["not_applicable", "evidence_ready", "insufficient_evidence", "needs_clarification"] = "not_applicable"
    completion_retries: int = 0
    stop_reason: str | None = None
    verification_scope: str = "仅检查支持场景的工具证据覆盖与循环进展；不验证语义正确性、未来收益或任意任务完整性。"


class AgentChatResponse(BaseModel):
    """Agent 工具调用接口的响应结构。"""

    # 每次请求都有独立 run_id，前端可以据此区分“会话”和“某一次执行”。
    run_id: str
    user_id: str
    question: str
    answer: str
    used_model: str
    max_tool_rounds: int
    stop_reason: str
    tools_available: list[str]
    tool_calls: list[AgentToolCall]
    evidence: list[AgentEvidenceReference] = Field(default_factory=list)
    answer_check: FundAnswerReport | None = None
    report_answer_check: FundReportAnswerReport | None = None
    current_todos: list[AgentTodoItem] = Field(default_factory=list)
    current_tasks: list[AgentTaskInfo] = Field(default_factory=list)
    current_background_jobs: list[AgentBackgroundJobInfo] = Field(default_factory=list)
    current_cron_schedules: list[AgentCronScheduleInfo] = Field(default_factory=list)
    recent_cron_deliveries: list[AgentCronDeliveryInfo] = Field(default_factory=list)
    # s15 把团队状态也放进调试响应，便于不查数据库就观察成员和消息变化。
    current_teammates: list[AgentTeammateInfo] = Field(default_factory=list)
    recent_team_messages: list[AgentTeamMessageInfo] = Field(default_factory=list)
    current_team_protocols: list[AgentTeamProtocolInfo] = Field(default_factory=list)
    recent_team_protocol_events: list[AgentTeamProtocolEventInfo] = Field(
        default_factory=list
    )
    current_autonomy_sessions: list[AgentAutonomySessionInfo] = Field(
        default_factory=list
    )
    current_worktrees: list[AgentWorktreeInfo] = Field(default_factory=list)
    # s19 把连接、动态工具和 tools/call 轨迹放进响应，便于观察工具池变化。
    current_mcp_servers: list[AgentMcpServerInfo] = Field(default_factory=list)
    current_mcp_tools: list[AgentMcpToolInfo] = Field(default_factory=list)
    recent_mcp_calls: list[AgentMcpCallTrace] = Field(default_factory=list)
    current_memories: list[AgentMemoryInfo] = Field(default_factory=list)
    prompt_trace: AgentPromptTrace
    recovery_trace: list[AgentRecoveryTrace] = Field(default_factory=list)
    hooks_trace: list[AgentHookTrace] = Field(default_factory=list)
    context_trace: list[AgentContextCompactTrace] = Field(default_factory=list)
    rounds: list[AgentLoopRound]
    pipeline_steps: list[str]
    usage: AgentTokenUsage = Field(default_factory=AgentTokenUsage)
    route: AgentRouteInfo = Field(default_factory=AgentRouteInfo)
    tool_discovery: AgentToolDiscoveryTrace = Field(default_factory=AgentToolDiscoveryTrace)
    loop_control: AgentLoopControl = Field(default_factory=AgentLoopControl)
    # 共享时限的轻量诊断，不含原始 Prompt，也不代替真实用量账本。
    execution_budget: dict[str, Any] = Field(default_factory=dict)


class AgentConversationCreateRequest(BaseModel):
    """创建一个新的聊天会话。"""

    user_id: str = Field(default="guest", min_length=1, max_length=50)
    title: str | None = Field(default=None, max_length=120)


class AgentConversationSummary(BaseModel):
    """会话侧边栏需要的轻量信息。"""

    id: str
    user_id: str
    title: str
    message_count: int
    created_at: str
    updated_at: str


class AgentResponseFeedbackInfo(BaseModel):
    """聊天消息旁边展示的一条回答反馈。"""

    id: str
    rating: Literal["helpful", "unhelpful"]
    reason_code: Literal[
        "inaccurate",
        "incomplete",
        "wrong_tool",
        "unclear",
        "outdated",
        "other",
    ] | None = None
    comment: str = ""
    correction: str = ""
    review_status: Literal["recorded", "pending", "approved", "dismissed"]
    review_note: str = ""
    created_at: str
    updated_at: str


class AgentConversationMessageInfo(BaseModel):
    """前端可以直接渲染的一条持久化聊天消息。"""

    id: str
    conversation_id: str
    role: Literal["user", "assistant"]
    content: str
    run_id: str | None = None
    run_metadata: dict[str, Any] = Field(default_factory=dict)
    feedback: AgentResponseFeedbackInfo | None = None
    created_at: str


class AgentConversationListResponse(BaseModel):
    """当前用户的会话列表。"""

    user_id: str
    conversations: list[AgentConversationSummary] = Field(default_factory=list)


class AgentConversationDetailResponse(BaseModel):
    """一个会话及其消息历史。"""

    conversation: AgentConversationSummary
    messages: list[AgentConversationMessageInfo] = Field(default_factory=list)


class AgentConversationDeleteResponse(BaseModel):
    """删除会话后的确认信息。"""

    ok: bool
    conversation_id: str


class AgentFeedbackSubmitRequest(BaseModel):
    """创建或更新一条回答反馈；同一回答始终只有一条当前反馈。"""

    user_id: str = Field(default="guest", min_length=1, max_length=50)
    rating: Literal["helpful", "unhelpful"]
    reason_code: Literal[
        "inaccurate",
        "incomplete",
        "wrong_tool",
        "unclear",
        "outdated",
        "other",
    ] | None = None
    comment: str = Field(default="", max_length=2000)
    correction: str = Field(default="", max_length=4000)


class AgentFeedbackReviewRequest(BaseModel):
    """人工审核一个负向反馈候选，不会自动改写正式评测集。"""

    user_id: str = Field(default="guest", min_length=1, max_length=50)
    status: Literal["approved", "dismissed"]
    review_note: str = Field(default="", max_length=2000)


class AgentFeedbackRunSummary(BaseModel):
    """候选反馈关联的运行摘要，数据来自 AgentRunObservation。"""

    model: str
    stop_reason: str
    duration_ms: float
    round_count: int
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    usage: AgentTokenUsage = Field(default_factory=AgentTokenUsage)
    metrics: dict[str, Any] = Field(default_factory=dict)


class AgentFeedbackCandidate(AgentResponseFeedbackInfo):
    """评测面板用于人工判断的一条真实失败案例。"""

    user_id: str
    conversation_id: str
    assistant_message_id: str
    run_id: str | None = None
    question: str
    answer: str
    run: AgentFeedbackRunSummary | None = None


class AgentFeedbackDashboard(BaseModel):
    """真实用户反馈统计和待审核候选。"""

    user_id: str
    total_count: int
    helpful_count: int
    unhelpful_count: int
    pending_count: int
    approved_count: int
    dismissed_count: int
    candidates: list[AgentFeedbackCandidate] = Field(default_factory=list)
    approved_candidates: list[AgentFeedbackCandidate] = Field(default_factory=list)


class AgentEvalDraftUpsertRequest(BaseModel):
    """把一个已保留反馈整理成兼容正式评测集的人工草稿。"""

    user_id: str = Field(default="guest", min_length=1, max_length=50)
    case_id: str = Field(
        min_length=3,
        max_length=80,
        pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$",
    )
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(min_length=1, max_length=500)
    category: str = Field(
        min_length=1,
        max_length=50,
        pattern=r"^[a-z][a-z0-9_]*$",
    )
    fund_code: str = Field(default="", max_length=20)
    expected_route: Literal[
        "direct",
        "fund_profile",
        "fund_nav",
        "fund_risk",
        "fund_assessment",
        "market",
        "portfolio",
    ]
    expected_tools: list[str] = Field(default_factory=list, max_length=8)
    required_answer_terms: list[str] = Field(min_length=1, max_length=12)
    answer_match: Literal["all", "any"] = "all"
    require_evidence_reference: bool = False


class AgentEvalDraftInfo(BaseModel):
    """一个尚未进入正式版本化评测集的本地 JSON 草稿。"""

    source_feedback_id: str
    user_id: str
    case_id: str
    name: str
    description: str
    category: str
    fund_code: str = ""
    expected_route: str
    expected_tools: list[str] = Field(default_factory=list)
    required_answer_terms: list[str] = Field(default_factory=list)
    answer_match: Literal["all", "any"] = "all"
    require_evidence_reference: bool = False
    question: str
    original_answer: str
    correction: str = ""
    file_path: str
    target_suite: str
    scenario: dict[str, Any] = Field(default_factory=dict)
    created_at: str
    updated_at: str


class AgentEvalDraftListResponse(BaseModel):
    """当前用户可人工检查的全部评测草稿。"""

    user_id: str
    drafts: list[AgentEvalDraftInfo] = Field(default_factory=list)


class AgentWorkspaceRuntimeInfo(BaseModel):
    """工作台顶部展示的后端运行环境摘要。"""

    app_name: str
    model: str
    execution_backend: str
    database_backend: str
    tool_count: int
    mcp_tool_count: int


class AgentWorkspaceSnapshot(BaseModel):
    """s20 统一工作区状态。

    之前每个章节各自返回一块调试 JSON；现在由一个只读快照把核心资源组织起来，
    Agent 响应和前端状态栏都从同一批 service 读取数据。
    """

    user_id: str
    runtime: AgentWorkspaceRuntimeInfo
    todos: list[AgentTodoItem] = Field(default_factory=list)
    tasks: list[AgentTaskInfo] = Field(default_factory=list)
    jobs: list[AgentBackgroundJobInfo] = Field(default_factory=list)
    schedules: list[AgentCronScheduleInfo] = Field(default_factory=list)
    teammates: list[AgentTeammateInfo] = Field(default_factory=list)
    protocols: list[AgentTeamProtocolInfo] = Field(default_factory=list)
    autonomy_sessions: list[AgentAutonomySessionInfo] = Field(default_factory=list)
    worktrees: list[AgentWorktreeInfo] = Field(default_factory=list)
    mcp_servers: list[AgentMcpServerInfo] = Field(default_factory=list)
    memories: list[AgentMemoryInfo] = Field(default_factory=list)


class ComprehensiveAgentChatResponse(BaseModel):
    """产品聊天接口的一次完整返回。

    conversation/message 面向聊天界面，run 面向运行轨迹，workspace 面向右侧资源栏。
    三类数据被明确分组，前端不需要再从一大坨调试 JSON 中猜字段用途。
    """

    conversation: AgentConversationSummary
    user_message: AgentConversationMessageInfo
    assistant_message: AgentConversationMessageInfo
    run: AgentChatResponse
    workspace: AgentWorkspaceSnapshot


class AgentRunObservation(BaseModel):
    """一条可查询的 Agent 运行观测记录。

    它比 Conversation 的 run_metadata 更完整，但仍然只保存排障和统计需要的摘要，
    不复制每个工具返回的整段原始 JSON。
    """

    run_id: str
    user_id: str
    conversation_id: str | None = None
    source: Literal["chat", "eval"]
    eval_run_id: str | None = None
    eval_case_id: str | None = None
    question: str
    answer_preview: str
    model: str
    stop_reason: str
    duration_ms: float
    round_count: int
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    usage: AgentTokenUsage = Field(default_factory=AgentTokenUsage)
    metrics: dict[str, Any] = Field(default_factory=dict)
    created_at: str


class AgentObservabilitySummary(BaseModel):
    """评测面板顶部使用的运行质量摘要。"""

    user_id: str
    run_count: int
    chat_run_count: int
    eval_run_count: int
    average_duration_ms: float
    total_tokens: int
    tool_error_count: int
    recent_runs: list[AgentRunObservation] = Field(default_factory=list)


class AgentEvalScenarioInfo(BaseModel):
    """一个固定评测场景的公开说明。"""

    id: str
    name: str
    description: str
    fund_code: str
    message: str
    category: str = "general"
    tags: list[str] = Field(default_factory=list)
    expected_tools: list[str] = Field(default_factory=list)


class AgentEvalSuiteInfo(BaseModel):
    """版本化评测集的基础信息。"""

    id: str
    name: str
    description: str
    version: str = "1"
    data_profile: str = ""
    data_fingerprint: str = ""
    category_counts: dict[str, int] = Field(default_factory=dict)
    scenarios: list[AgentEvalScenarioInfo] = Field(default_factory=list)


class AgentEvalCheck(BaseModel):
    """确定性评分器执行的一条检查。"""

    name: str
    passed: bool
    detail: str
    # dimension 让报告能区分“模型没选对工具”和“答案没讲清楚”。
    dimension: Literal[
        "routing",
        "execution",
        "grounding",
        "answer_quality",
        "safety",
        "efficiency",
        "retrieval",
        "numeric",
        "citations",
        "conversation",
    ] = "answer_quality"
    weight: float = Field(default=1.0, ge=0)
    # required=False 的检查会影响质量分，但不会单独让整个 case 判失败。
    required: bool = True


class AgentEvalCaseResult(BaseModel):
    """一个评测问题的运行和判分结果。"""

    case_id: str
    name: str
    passed: bool
    score: float
    run_id: str | None = None
    duration_ms: float
    stop_reason: str
    tools_called: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    available_evidence_ids: list[str] = Field(default_factory=list)
    usage: AgentTokenUsage = Field(default_factory=AgentTokenUsage)
    checks: list[AgentEvalCheck] = Field(default_factory=list)
    category: str = "general"
    data_profile: str = ""
    data_fingerprint: str = ""
    metric_scores: dict[str, float] = Field(default_factory=dict)
    tool_call_count: int = 0
    answer_preview: str = ""
    error: str | None = None
    # 执行失败也保留诊断；不能因为没有答案就丢失超时发生的阶段。
    execution_diagnostics: dict[str, Any] = Field(default_factory=dict)
    # 季报专项附加原文、分子/分母、完整回答和人工复核；旧记录默认空对象，免迁移。
    report_quality: dict[str, Any] = Field(default_factory=dict)
    # 完成答案和正确拒绝/说明缺口不是一回事。旧评测没有入口验收，保留 None，
    # 不能把历史记录自动标成“完整完成”。新值保存在已有 results JSON 内。
    outcome: Literal["completed", "handled_limitation", "system_error"] | None = None
    # 与 tool_call_count（模型发出的调用意图）分开，缓存命中不算物理执行。
    # 旧工具没有逐次 trace 时使用 None，避免把未知执行次数误写为零。
    physical_tool_attempts: int | None = Field(default=None, ge=0)


class AgentEvalRunRequest(BaseModel):
    """手动执行评测集的请求。case_ids 为空表示执行全部场景。"""

    user_id: str = Field(default="guest", min_length=1, max_length=50)
    case_ids: list[str] = Field(default_factory=list, max_length=20)
    suite_id: Literal["fund_agent_v2", "fund_report_rag_v1"] = "fund_agent_v2"


class FundReportEvalJobRequest(BaseModel):
    """付费季报评测必须显式启动；任务编号用于防止断网重提导致重复扣费。"""
    user_id: str = Field(default="guest", min_length=1, max_length=50)
    case_ids: list[str] = Field(default_factory=list, max_length=20)
    idempotency_key: str = Field(min_length=8, max_length=64, pattern=r"^[a-zA-Z0-9:_-]+$")
    retrieval_mode: Literal["hybrid", "keyword"] = "hybrid"


class FundReportEvalReviewRequest(BaseModel):
    user_id: str = Field(default="guest", min_length=1, max_length=50)
    status: Literal["accepted", "needs_fix", "pending"]
    note: str = Field(default="", max_length=2000)


class AgentEvalRun(BaseModel):
    """一次完整评测运行。"""

    id: str
    user_id: str
    suite_id: str
    suite_name: str
    status: Literal["completed", "failed"]
    total_cases: int
    passed_cases: int
    failed_cases: int
    score: float
    duration_ms: float
    total_tokens: int
    total_tool_calls: int = 0
    average_duration_ms: float = 0.0
    average_tokens: float = 0.0
    data_profile: str = ""
    data_fingerprint: str = ""
    # 只有 case_set_id 相同的两次运行才有资格计算 score_delta。
    case_ids: list[str] = Field(default_factory=list)
    case_set_id: str = ""
    metric_scores: dict[str, float] = Field(default_factory=dict)
    report_metrics: dict[str, Any] = Field(default_factory=dict)
    score_delta: float | None = None
    results: list[AgentEvalCaseResult] = Field(default_factory=list)
    created_at: str


class AgentEvalDashboard(BaseModel):
    """前端评测标签页一次读取所需的数据。"""

    suite: AgentEvalSuiteInfo
    latest_run: AgentEvalRun | None = None
    previous_run: AgentEvalRun | None = None
    recent_eval_runs: list[AgentEvalRun] = Field(default_factory=list)
    observability: AgentObservabilitySummary


class KnowledgeDocumentInfo(BaseModel):
    """知识库源目录中的一个用户文档。"""

    name: str
    size_bytes: int
    modified_at: str


class KnowledgeWorkspaceResponse(BaseModel):
    """知识库文档和向量索引的当前状态。"""

    documents: list[KnowledgeDocumentInfo] = Field(default_factory=list)
    document_count: int
    vector_ready: bool
    chunk_count: int
    embedding_model: str
    error: str | None = None


class KnowledgeUploadResponse(BaseModel):
    """上传一个知识文档后的响应。"""

    ok: bool
    document: KnowledgeDocumentInfo
    message: str


class KnowledgeDeleteResponse(BaseModel):
    """删除知识文档后的响应。"""

    ok: bool
    document_name: str
    message: str
