"""ETF 申赎与龙虎榜机构席位组成的“大资金行为线索”模块。

这条链路刻意区分三件事：

1. Provider 只负责抓取并清洗公开事实；
2. Repository 只负责幂等保存 ETF 份额和机构席位买卖；
3. AnalysisService 才计算 1/5/20 日变化、方向和基金/组合关联。

“机构专用”不等于某一家机构，ETF 申购也不能自动等于“国家队入场”。因此本模块
始终返回 evidence_level、confidence 和 identity_boundary，不让大模型补猜身份。
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import math
from typing import Any, Protocol

from sqlalchemy import delete, func, select

from app.database import Database, runtime_database
from app.models import EtfCapitalSnapshotModel, InstitutionalTradeModel
from app.services.capital_sector_analysis import build_sector_analysis
from app.services.fund_disclosures import (
    FundDisclosureService,
    fund_disclosure_service,
)
from app.services.funds import FundCatalogService, fund_catalog_service
from app.services.portfolio_exposure import (
    PortfolioExposureService,
    portfolio_exposure_service,
)
from app.services.security_sectors import SecuritySectorService, security_sector_service


ETF_SPOT_SOURCE = "Eastmoney ETF spot via AKShare"
ETF_SPOT_URL = "https://quote.eastmoney.com/center/gridlist.html#fund_etf"
ETF_SCALE_SOURCE = "Shanghai Stock Exchange ETF scale via AKShare"
ETF_SCALE_URL = "https://www.sse.com.cn/assortment/fund/etf/list/scale/"
INSTITUTION_SOURCE = "Eastmoney institutional LHB via AKShare"
INSTITUTION_URL = "https://data.eastmoney.com/stock/jgmmtj.html"

# 本地只保留足够覆盖 20 个交易日分析和短期审计的窗口，表不会无限增长。
STORED_CALENDAR_DAYS = 70
INSTITUTION_LOOKBACK_DAYS = 30
INITIAL_ETF_HISTORY_CALENDAR_DAYS = 35


class LargeCapitalError(RuntimeError):
    """外部数据、持久化或关联分析无法形成可靠结果。"""


@dataclass(frozen=True)
class CapitalTopicDefinition:
    """ETF 名称与基金披露使用的透明主题词典。"""

    code: str
    label: str
    kind: str
    keywords: tuple[str, ...]


# 主题行业放在宽基之前。像“沪深300医药ETF”应归到医药，而不是重复计入宽基。
CAPITAL_TOPICS = (
    CapitalTopicDefinition("semiconductor", "半导体", "sector", ("半导体", "芯片", "集成电路")),
    CapitalTopicDefinition("ai", "人工智能", "sector", ("人工智能", "AIETF", "算力", "机器人")),
    CapitalTopicDefinition("new_energy", "新能源", "sector", ("新能源", "光伏", "电池", "储能", "新能源车")),
    CapitalTopicDefinition("medicine", "医药医疗", "sector", ("医药", "医疗", "创新药", "生物医药", "CXO")),
    CapitalTopicDefinition("consumption", "消费", "sector", ("消费", "白酒", "食品饮料", "家电")),
    CapitalTopicDefinition("finance", "大金融", "sector", ("金融", "银行", "证券", "券商", "保险")),
    CapitalTopicDefinition("defense", "军工", "sector", ("军工", "国防", "航天", "航空")),
    CapitalTopicDefinition("gold", "黄金", "sector", ("黄金",)),
    CapitalTopicDefinition("nonferrous", "有色金属", "sector", ("有色", "稀有金属", "稀土")),
    # 宽基只匹配完整指数名。像“科创 50ETF”“沪深300医药ETF”都带有数字，
    # 如果用“50ETF/300ETF”这种短词，会把行业产品错误算进宽基资金。
    CapitalTopicDefinition("csi300", "沪深300", "broad", ("沪深300",)),
    CapitalTopicDefinition("csi500", "中证500", "broad", ("中证500",)),
    CapitalTopicDefinition("csi1000", "中证1000", "broad", ("中证1000",)),
    CapitalTopicDefinition("sse50", "上证50", "broad", ("上证50",)),
    CapitalTopicDefinition("chinext", "创业板", "broad", ("创业板", "创成长")),
    CapitalTopicDefinition("star50", "科创50", "broad", ("科创50",)),
)
CAPITAL_TOPIC_BY_CODE = {item.code: item for item in CAPITAL_TOPICS}


@dataclass(frozen=True)
class EtfCapitalSnapshot:
    """Provider 清洗完成后交给数据库的一条 ETF 快照。"""

    etf_code: str
    etf_name: str
    exchange: str
    topic_code: str
    topic_label: str
    topic_kind: str
    data_date: date
    unit_price: float
    price_basis: str
    shares: float
    estimated_scale_cny: float
    turnover_cny: float | None
    provider_main_net_inflow_cny: float | None
    source_level: str
    source: str
    source_url: str


@dataclass(frozen=True)
class InstitutionalTrade:
    """一只证券某日的龙虎榜机构席位汇总。"""

    stock_code: str
    stock_name: str
    trade_date: date
    close: float | None
    daily_return_pct: float | None
    buyer_count: int
    seller_count: int
    buy_cny: float
    sell_cny: float
    net_buy_cny: float
    market_turnover_cny: float | None
    net_buy_ratio_pct: float | None
    turnover_rate_pct: float | None
    listing_reason: str
    source_level: str = "B"
    source: str = INSTITUTION_SOURCE
    source_url: str = INSTITUTION_URL


@dataclass(frozen=True)
class LargeCapitalBatch:
    """一次外部同步得到的 ETF、机构席位和非致命警告。"""

    etf_snapshots: tuple[EtfCapitalSnapshot, ...]
    institutional_trades: tuple[InstitutionalTrade, ...]
    warnings: tuple[str, ...] = ()


class LargeCapitalProvider(Protocol):
    """测试和生产 Provider 共享的最小协议。"""

    def fetch(
        self,
        *,
        as_of: date | None = None,
        include_history: bool = False,
    ) -> LargeCapitalBatch: ...


class AkshareLargeCapitalProvider:
    """使用 AKShare 读取 ETF 份额和龙虎榜机构每日统计。"""

    def fetch(
        self,
        *,
        as_of: date | None = None,
        include_history: bool = False,
    ) -> LargeCapitalBatch:
        try:
            import akshare as ak
        except ModuleNotFoundError as exc:
            raise LargeCapitalError("请先安装 requirements.txt 中的 akshare") from exc

        cutoff = as_of or date.today()
        fetched_at = datetime.now(timezone.utc)
        warnings: list[str] = []
        current: list[EtfCapitalSnapshot] = []
        trades: list[InstitutionalTrade] = []

        # 两个来源互相独立；一个暂时失败时仍保留另一个来源的可用事实。
        try:
            spot_frame = ak.fund_etf_spot_em()
            current = self._normalize_etf_spot(spot_frame)
        except Exception as exc:
            warnings.append(f"ETF 最新份额采集失败：{exc}")

        try:
            start = cutoff - timedelta(days=INSTITUTION_LOOKBACK_DAYS)
            trade_frame = ak.stock_lhb_jgmmtj_em(
                start_date=start.strftime("%Y%m%d"),
                end_date=cutoff.strftime("%Y%m%d"),
            )
            trades = self._normalize_institutional_trades(trade_frame)
        except Exception as exc:
            warnings.append(f"龙虎榜机构统计采集失败：{exc}")

        history: list[EtfCapitalSnapshot] = []
        if include_history and current:
            try:
                history = self._fetch_initial_sse_history(
                    ak=ak,
                    as_of=cutoff,
                    current=current,
                )
            except Exception as exc:
                # 历史补齐失败不应覆盖当天成功快照，下一次同步仍可以继续积累。
                warnings.append(f"上交所 ETF 历史份额补齐失败：{exc}")

        if not current and not trades:
            detail = "；".join(warnings) or "外部来源没有返回记录"
            raise LargeCapitalError(f"大资金公开数据采集失败：{detail}")

        # 历史在前、当天在后；Repository merge 时当天实时快照可覆盖同日历史代理值。
        return LargeCapitalBatch(
            etf_snapshots=tuple([*history, *current]),
            institutional_trades=tuple(trades),
            warnings=tuple(warnings),
        )

    def _normalize_etf_spot(self, frame: Any) -> list[EtfCapitalSnapshot]:
        """从 1500+ 只 ETF 中只保留 FundScope 当前关注的宽基和行业主题。"""

        required = {"代码", "名称", "最新价", "最新份额", "数据日期"}
        if frame is None or frame.empty or not required.issubset(frame.columns):
            raise LargeCapitalError("ETF 实时行情返回字段不完整")

        items: list[EtfCapitalSnapshot] = []
        for row in frame.to_dict(orient="records"):
            code = str(row.get("代码") or "").strip().zfill(6)
            name = str(row.get("名称") or "").strip()
            topic = classify_capital_topic(name)
            price = _finite_number(row.get("最新价"))
            shares = _finite_number(row.get("最新份额"))
            data_date = _as_date(row.get("数据日期"))
            if not topic or len(code) != 6 or not data_date or not price or not shares:
                continue
            if price <= 0 or shares <= 0:
                continue
            turnover = _finite_number(row.get("成交额"))
            main_flow = _finite_number(row.get("主力净流入-净额"))
            items.append(
                EtfCapitalSnapshot(
                    etf_code=code,
                    etf_name=name,
                    exchange="SSE" if code.startswith("5") else "SZSE",
                    topic_code=topic.code,
                    topic_label=topic.label,
                    topic_kind=topic.kind,
                    data_date=data_date,
                    unit_price=price,
                    price_basis="latest_spot",
                    shares=shares,
                    estimated_scale_cny=shares * price,
                    turnover_cny=turnover,
                    provider_main_net_inflow_cny=main_flow,
                    source_level="B",
                    source=ETF_SPOT_SOURCE,
                    source_url=ETF_SPOT_URL,
                )
            )
        if not items:
            raise LargeCapitalError("重点 ETF 没有可用份额记录")
        return items

    def _fetch_initial_sse_history(
        self,
        *,
        ak: Any,
        as_of: date,
        current: list[EtfCapitalSnapshot],
    ) -> list[EtfCapitalSnapshot]:
        """首次同步补齐上交所近一个月份额，让 5/20 日指标不必等二十天。

        上交所历史规模表没有同日价格，因此统一使用当天最新价估算份额变动金额，
        并把 ``price_basis`` 标成 latest_proxy。Service 会据此把置信度限制在中等。
        """

        current_by_code = {
            item.etf_code: item for item in current if item.exchange == "SSE"
        }
        if not current_by_code:
            return []

        start = as_of - timedelta(days=INITIAL_ETF_HISTORY_CALENDAR_DAYS)
        history: list[EtfCapitalSnapshot] = []
        cursor = start
        while cursor < as_of:
            if cursor.weekday() < 5:
                frame = ak.fund_etf_scale_sse(date=cursor.strftime("%Y%m%d"))
                if frame is not None and not frame.empty:
                    for row in frame.to_dict(orient="records"):
                        code = str(row.get("基金代码") or "").strip().zfill(6)
                        current_item = current_by_code.get(code)
                        shares = _finite_number(row.get("基金份额"))
                        data_date = _as_date(row.get("统计日期"))
                        if current_item is None or not shares or not data_date or shares <= 0:
                            continue
                        history.append(
                            EtfCapitalSnapshot(
                                etf_code=code,
                                etf_name=str(row.get("基金简称") or current_item.etf_name),
                                exchange="SSE",
                                topic_code=current_item.topic_code,
                                topic_label=current_item.topic_label,
                                topic_kind=current_item.topic_kind,
                                data_date=data_date,
                                unit_price=current_item.unit_price,
                                price_basis="latest_proxy",
                                shares=shares,
                                estimated_scale_cny=shares * current_item.unit_price,
                                turnover_cny=None,
                                provider_main_net_inflow_cny=None,
                                source_level="A",
                                source=ETF_SCALE_SOURCE,
                                source_url=ETF_SCALE_URL,
                            )
                        )
            cursor += timedelta(days=1)
        return history

    def _normalize_institutional_trades(self, frame: Any) -> list[InstitutionalTrade]:
        required = {
            "代码",
            "名称",
            "买方机构数",
            "卖方机构数",
            "机构买入总额",
            "机构卖出总额",
            "机构买入净额",
            "上榜日期",
        }
        if frame is None or frame.empty:
            return []
        if not required.issubset(frame.columns):
            raise LargeCapitalError("龙虎榜机构统计返回字段不完整")

        items: list[InstitutionalTrade] = []
        for row in frame.to_dict(orient="records"):
            code = str(row.get("代码") or "").strip().zfill(6)
            trade_date = _as_date(row.get("上榜日期"))
            buy = _finite_number(row.get("机构买入总额")) or 0.0
            sell = _finite_number(row.get("机构卖出总额")) or 0.0
            net = _finite_number(row.get("机构买入净额"))
            if len(code) != 6 or not trade_date:
                continue
            items.append(
                InstitutionalTrade(
                    stock_code=code,
                    stock_name=str(row.get("名称") or code).strip(),
                    trade_date=trade_date,
                    close=_finite_number(row.get("收盘价")),
                    daily_return_pct=_finite_number(row.get("涨跌幅")),
                    buyer_count=max(0, int(_finite_number(row.get("买方机构数")) or 0)),
                    seller_count=max(0, int(_finite_number(row.get("卖方机构数")) or 0)),
                    buy_cny=max(0.0, buy),
                    sell_cny=max(0.0, sell),
                    net_buy_cny=(buy - sell) if net is None else net,
                    market_turnover_cny=_finite_number(row.get("市场总成交额")),
                    net_buy_ratio_pct=_finite_number(row.get("机构净买额占总成交额比")),
                    turnover_rate_pct=_finite_number(row.get("换手率")),
                    listing_reason=str(row.get("上榜原因") or "交易公开信息").strip(),
                )
            )
        # 同一证券同日可能同时满足“单日偏离”和“连续三日累计偏离”等多个上榜条件。
        # 这些记录的统计区间互相重叠，求和会重复计算；联合主键直接 merge 又会由返回
        # 顺序随机决定保留哪条。因此每个证券/日期只保留市场成交额最小的一条，通常
        # 更接近单日口径；若成交额缺失，再以净买卖绝对值较大的记录稳定决胜。
        deduplicated: dict[tuple[str, date], InstitutionalTrade] = {}
        for item in items:
            key = (item.stock_code, item.trade_date)
            current = deduplicated.get(key)
            if current is None or self._institution_row_preference(
                item
            ) < self._institution_row_preference(current):
                deduplicated[key] = item
        return sorted(
            deduplicated.values(),
            key=lambda item: (item.trade_date, item.stock_code),
        )

    @staticmethod
    def _institution_row_preference(item: InstitutionalTrade) -> tuple[float, float, str]:
        turnover = item.market_turnover_cny
        return (
            turnover if turnover is not None and turnover > 0 else math.inf,
            -abs(item.net_buy_cny),
            item.listing_reason,
        )


class LargeCapitalRepository:
    """大资金原始事实表的唯一 SQLAlchemy 读写入口。"""

    def __init__(self, database: Database = runtime_database) -> None:
        self.database = database
        self.database.create_schema()

    def save(self, *, batch: LargeCapitalBatch, fetched_at: datetime) -> dict[str, Any]:
        """按业务主键 merge，同一天重复刷新不会产生重复记录。"""

        cutoff = fetched_at.date() - timedelta(days=STORED_CALENDAR_DAYS)
        with self.database.session() as session, session.begin():
            for item in batch.etf_snapshots:
                session.merge(
                    EtfCapitalSnapshotModel(
                        **item.__dict__,
                        fetched_at=fetched_at,
                    )
                )
            for item in batch.institutional_trades:
                session.merge(
                    InstitutionalTradeModel(
                        **item.__dict__,
                        fetched_at=fetched_at,
                    )
                )
            session.execute(
                delete(EtfCapitalSnapshotModel).where(
                    EtfCapitalSnapshotModel.data_date < cutoff
                )
            )
            session.execute(
                delete(InstitutionalTradeModel).where(
                    InstitutionalTradeModel.trade_date < cutoff
                )
            )
        return {
            "etf_snapshot_count": len(batch.etf_snapshots),
            "institutional_trade_count": len(batch.institutional_trades),
        }

    def etf_history_day_count(self) -> int:
        with self.database.session() as session:
            return int(
                session.scalar(
                    select(func.count(func.distinct(EtfCapitalSnapshotModel.data_date)))
                )
                or 0
            )

    def list_etf_snapshots(self) -> list[dict[str, Any]]:
        with self.database.session() as session:
            rows = list(
                session.scalars(
                    select(EtfCapitalSnapshotModel).order_by(
                        EtfCapitalSnapshotModel.data_date.asc(),
                        EtfCapitalSnapshotModel.topic_code.asc(),
                        EtfCapitalSnapshotModel.estimated_scale_cny.desc(),
                    )
                )
            )
        return [self._etf_dict(item) for item in rows]

    def list_institutional_trades(self) -> list[dict[str, Any]]:
        with self.database.session() as session:
            rows = list(
                session.scalars(
                    select(InstitutionalTradeModel).order_by(
                        InstitutionalTradeModel.trade_date.desc(),
                        InstitutionalTradeModel.net_buy_cny.desc(),
                    )
                )
            )
        return [self._trade_dict(item) for item in rows]

    def status(self) -> dict[str, Any]:
        with self.database.session() as session:
            etf_count, etf_days, etf_start, etf_end, etf_fetched = session.execute(
                select(
                    func.count(),
                    func.count(func.distinct(EtfCapitalSnapshotModel.data_date)),
                    func.min(EtfCapitalSnapshotModel.data_date),
                    func.max(EtfCapitalSnapshotModel.data_date),
                    func.max(EtfCapitalSnapshotModel.fetched_at),
                ).select_from(EtfCapitalSnapshotModel)
            ).one()
            trade_count, trade_start, trade_end, trade_fetched = session.execute(
                select(
                    func.count(),
                    func.min(InstitutionalTradeModel.trade_date),
                    func.max(InstitutionalTradeModel.trade_date),
                    func.max(InstitutionalTradeModel.fetched_at),
                ).select_from(InstitutionalTradeModel)
            ).one()
        fetched_values = [value for value in (etf_fetched, trade_fetched) if value]
        return {
            "data_mode": "database" if etf_count or trade_count else "unavailable",
            "etf_snapshot_count": int(etf_count or 0),
            "etf_history_days": int(etf_days or 0),
            "etf_start": etf_start.isoformat() if etf_start else None,
            "etf_end": etf_end.isoformat() if etf_end else None,
            "institutional_trade_count": int(trade_count or 0),
            "institutional_start": trade_start.isoformat() if trade_start else None,
            "institutional_end": trade_end.isoformat() if trade_end else None,
            "last_successful_sync_at": (
                _as_utc(max(fetched_values)).isoformat() if fetched_values else None
            ),
        }

    @staticmethod
    def _etf_dict(item: EtfCapitalSnapshotModel) -> dict[str, Any]:
        return {
            "etf_code": item.etf_code,
            "etf_name": item.etf_name,
            "exchange": item.exchange,
            "topic_code": item.topic_code,
            "topic_label": item.topic_label,
            "topic_kind": item.topic_kind,
            "data_date": item.data_date,
            "unit_price": item.unit_price,
            "price_basis": item.price_basis,
            "shares": item.shares,
            "estimated_scale_cny": item.estimated_scale_cny,
            "turnover_cny": item.turnover_cny,
            "provider_main_net_inflow_cny": item.provider_main_net_inflow_cny,
            "source_level": item.source_level,
            "source": item.source,
            "source_url": item.source_url,
            "fetched_at": _as_utc(item.fetched_at),
        }

    @staticmethod
    def _trade_dict(item: InstitutionalTradeModel) -> dict[str, Any]:
        return {
            "stock_code": item.stock_code,
            "stock_name": item.stock_name,
            "trade_date": item.trade_date,
            "close": item.close,
            "daily_return_pct": item.daily_return_pct,
            "buyer_count": item.buyer_count,
            "seller_count": item.seller_count,
            "buy_cny": item.buy_cny,
            "sell_cny": item.sell_cny,
            "net_buy_cny": item.net_buy_cny,
            "market_turnover_cny": item.market_turnover_cny,
            "net_buy_ratio_pct": item.net_buy_ratio_pct,
            "turnover_rate_pct": item.turnover_rate_pct,
            "listing_reason": item.listing_reason,
            "source_level": item.source_level,
            "source": item.source,
            "source_url": item.source_url,
            "fetched_at": _as_utc(item.fetched_at),
        }


class LargeCapitalAnalysisService:
    """把 ETF 与机构席位事实转换成可解释、可关联的资金线索。"""

    def __init__(
        self,
        *,
        repository: LargeCapitalRepository,
        funds: FundCatalogService = fund_catalog_service,
        disclosures: FundDisclosureService = fund_disclosure_service,
        portfolio_exposure: PortfolioExposureService = portfolio_exposure_service,
        sectors: SecuritySectorService = security_sector_service,
    ) -> None:
        self.repository = repository
        self.funds = funds
        self.disclosures = disclosures
        self.portfolio_exposure = portfolio_exposure
        self.sectors = sectors

    def overview(
        self,
        *,
        user_id: str | None = None,
        fund_code: str | None = None,
        portfolio_id: str | None = None,
    ) -> dict[str, Any]:
        """返回市场总览，并按需关联当前基金或当前组合。"""

        etf_rows = self.repository.list_etf_snapshots()
        trade_rows = self.repository.list_institutional_trades()
        status = self.repository.status()
        generated_at = datetime.now(timezone.utc)
        if not etf_rows and not trade_rows:
            return self._empty_overview(generated_at=generated_at, status=status)

        topic_signals = self._topic_signals(etf_rows)
        institution = self._institution_signal(trade_rows)
        links: list[dict[str, Any]] = []
        link_warnings: list[str] = []
        if fund_code:
            try:
                link = self._fund_link(
                    fund_code=fund_code,
                    topic_signals=topic_signals,
                    institution=institution,
                )
                if link:
                    links.append(link)
            except Exception as exc:
                link_warnings.append(f"当前基金关联失败：{exc}")
        if portfolio_id and user_id:
            try:
                link = self._portfolio_link(
                    user_id=user_id,
                    portfolio_id=portfolio_id,
                    topic_signals=topic_signals,
                    institution=institution,
                )
                if link:
                    links.append(link)
            except Exception as exc:
                link_warnings.append(f"当前组合关联失败：{exc}")

        latest_dates = [
            value
            for value in (
                max((item["data_date"] for item in etf_rows), default=None),
                max((item["trade_date"] for item in trade_rows), default=None),
            )
            if value
        ]
        state_code, state_label, summary = self._overall_state(
            topic_signals=topic_signals,
            institution=institution,
        )
        return {
            "data_kind": "large_capital_signals",
            "data_mode": "database",
            "generated_at": generated_at,
            "data_updated_at": status["last_successful_sync_at"],
            "data_date": max(latest_dates) if latest_dates else None,
            "state_code": state_code,
            "state_label": state_label,
            "confidence": self._overall_confidence(topic_signals, institution),
            "summary": summary,
            "etf_history_days": status["etf_history_days"],
            "topic_signals": topic_signals,
            "sector_analysis": build_sector_analysis(topic_signals, links),
            "institutional_signal": institution,
            "exposure_links": links,
            "identity_boundary": {
                "confirmed_national_team_disclosure_count": 0,
                "label": "当前为公开资金线索，不识别匿名账户",
                "explanation": (
                    "ETF 申购者身份不可见；龙虎榜“机构专用”也不会披露具体机构。"
                    "只有未来接入汇金、证金等定期股东披露后，才会标记为明确身份。"
                ),
            },
            "evidence_legend": {
                "A": "交易所或法定披露的直接事实",
                "B": "可追溯财经数据接口中的公开市场事实",
                "C": "成交结构算法或身份未知的间接线索",
            },
            "sources": self._sources(etf_rows, trade_rows),
            "limitations": [
                "ETF 份额增加能证明净申购，不能证明申购者是国家队或某家机构。",
                "历史 ETF 份额金额使用最新价格做代理估算，不等于精确成交金额。",
                "龙虎榜仅覆盖触发交易公开条件的证券，不代表全市场机构持仓。",
                "同一证券同日若有重叠上榜口径，仅保留市场成交额最小的一条避免重复计算。",
                "基金和组合关联使用定期披露持仓，不代表盘中实时仓位。",
                *link_warnings,
            ],
            "methodology": (
                "ETF 资金估算 = 同一 ETF 份额变化 × 最新可用价格；主题汇总只比较"
                "起止两端都存在的 ETF。机构席位为最近五个龙虎榜交易日买卖汇总。"
            ),
            "disclaimer": "大资金行为线索仅用于研究，不构成机构身份认定或投资建议。",
        }

    def _topic_signals(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            grouped[row["topic_code"]].append(row)

        # 统一比较日期；某个板块缺一天时不能偷偷换成更早的起点。
        comparison_dates = sorted({item["data_date"] for item in rows})
        signals = []
        for topic_code, topic_rows in grouped.items():
            definition = CAPITAL_TOPIC_BY_CODE.get(topic_code)
            dates = sorted({item["data_date"] for item in topic_rows})
            if not definition or not dates:
                continue
            latest_date = dates[-1]
            latest_rows = [item for item in topic_rows if item["data_date"] == latest_date]
            current_scale = sum(item["estimated_scale_cny"] for item in latest_rows)
            current_turnover = sum(float(item["turnover_cny"] or 0) for item in latest_rows)
            provider_flow = sum(
                float(item["provider_main_net_inflow_cny"] or 0)
                for item in latest_rows
            )

            periods = {
                f"{period}d": self._period_metrics(topic_rows, comparison_dates, period)
                for period in (1, 5, 20)
            }
            flow_1d, coverage_1d = periods["1d"]["net_flow_cny"], periods["1d"]["coverage_pct"] / 100
            flow_5d, coverage_5d = periods["5d"]["net_flow_cny"], periods["5d"]["coverage_pct"] / 100
            flow_20d, coverage_20d = periods["20d"]["net_flow_cny"], periods["20d"]["coverage_pct"] / 100
            direction, basis_value, basis_ratio = self._topic_direction(
                current_scale=current_scale,
                current_turnover=current_turnover,
                provider_flow=provider_flow,
                flow_1d=flow_1d,
                flow_5d=flow_5d,
            )
            confidence = (
                "medium"
                if flow_5d is not None and coverage_5d >= 0.6
                else "low"
            )

            top_etfs = sorted(
                latest_rows,
                key=lambda item: item["estimated_scale_cny"],
                reverse=True,
            )[:3]
            start_date = dates[max(0, len(dates) - 21)]
            signals.append(
                {
                    "topic_code": definition.code,
                    "topic_label": definition.label,
                    "topic_kind": definition.kind,
                    "data_date": latest_date,
                    "etf_count": len(latest_rows),
                    "history_days": len(dates),
                    "period_flows": periods,
                    "estimated_scale_cny": round(current_scale, 2),
                    "flow_1d_cny": _rounded_or_none(flow_1d),
                    "flow_5d_cny": _rounded_or_none(flow_5d),
                    "flow_20d_cny": _rounded_or_none(flow_20d),
                    "coverage_1d_pct": round(coverage_1d * 100, 2),
                    "coverage_5d_pct": round(coverage_5d * 100, 2),
                    "coverage_20d_pct": round(coverage_20d * 100, 2),
                    "provider_main_net_inflow_cny": round(provider_flow, 2),
                    "direction": direction,
                    "direction_label": {
                        "inflow": "资金流入",
                        "outflow": "资金流出",
                        "neutral": "方向中性",
                        "insufficient": "历史不足",
                    }[direction],
                    "confidence": confidence,
                    "basis_value_cny": round(basis_value, 2),
                    "basis_ratio_pct": round(basis_ratio, 4),
                    "evidence_id": (
                        f"capital:etf:{definition.code}:{start_date.isoformat()}:"
                        f"{latest_date.isoformat()}"
                    ),
                    "top_etfs": [
                        {
                            "etf_code": item["etf_code"],
                            "etf_name": item["etf_name"],
                            "estimated_scale_cny": round(item["estimated_scale_cny"], 2),
                            "source_level": item["source_level"],
                            "source_url": item["source_url"],
                        }
                        for item in top_etfs
                    ],
                }
            )

        # 先展示宽基，再展示行业；同组内按用于判断方向的绝对资金量排序。
        return sorted(
            signals,
            key=lambda item: (
                0 if item["topic_kind"] == "broad" else 1,
                -abs(item["basis_value_cny"]),
                item["topic_label"],
            ),
        )

    @staticmethod
    def _period_flow(
        rows: list[dict[str, Any]],
        dates: list[date],
        period: int,
    ) -> tuple[float | None, float]:
        """保留原来的内部接口；详细口径统一由 _period_metrics 计算。"""

        metrics = LargeCapitalAnalysisService._period_metrics(rows, dates, period)
        return metrics["net_flow_cny"], metrics["coverage_pct"] / 100

    @staticmethod
    def _period_metrics(
        rows: list[dict[str, Any]], dates: list[date], period: int,
    ) -> dict[str, Any]:
        """用同一批 ETF 计算金额、覆盖和强度，缺记录不补成零。

        强度的分母只取实际参与计算的 ETF 规模，不能用全部板块规模稀释它。
        这里只是份额变化的价格代理估算，不是股票交易净流入，也不是收益率。
        """

        result = {
            "start_date": dates[-(period + 1)] if len(dates) > period else None,
            "end_date": dates[-1] if dates else None,
            "net_flow_cny": None,
            "intensity_pct": None,
            "coverage_pct": 0.0,
            "comparable_etf_count": 0,
        }
        if len(dates) <= period:
            return result
        latest_date = dates[-1]
        previous_date = dates[-(period + 1)]
        latest = {
            item["etf_code"]: item for item in rows if item["data_date"] == latest_date
        }
        previous = {
            item["etf_code"]: item for item in rows if item["data_date"] == previous_date
        }
        common_codes = set(latest).intersection(previous)
        if not common_codes:
            return result
        latest_scale = sum(float(item["estimated_scale_cny"]) for item in latest.values())
        covered_scale = sum(
            float(latest[code]["estimated_scale_cny"]) for code in sorted(common_codes)
        )
        flow = sum(
            (float(latest[code]["shares"]) - float(previous[code]["shares"]))
            * float(latest[code]["unit_price"])
            for code in sorted(common_codes)
        )
        coverage = covered_scale / latest_scale if latest_scale > 0 else 0.0
        return {
            **result,
            "net_flow_cny": round(flow, 2),
            "intensity_pct": round(flow / covered_scale * 100, 4) if covered_scale > 0 else None,
            "coverage_pct": round(coverage * 100, 2),
            "comparable_etf_count": len(common_codes),
        }

    @staticmethod
    def _topic_direction(
        *,
        current_scale: float,
        current_turnover: float,
        provider_flow: float,
        flow_1d: float | None,
        flow_5d: float | None,
    ) -> tuple[str, float, float]:
        # 份额变化比成交“大单算法”更接近真实申赎，因此优先使用 5 日、再用 1 日。
        if flow_5d is not None or flow_1d is not None:
            value = flow_5d if flow_5d is not None else float(flow_1d or 0)
            ratio = value / current_scale * 100 if current_scale > 0 else 0.0
            threshold = 0.25
        elif current_turnover > 0:
            value = provider_flow
            ratio = value / current_turnover * 100
            threshold = 2.0
        else:
            return "insufficient", 0.0, 0.0
        if ratio >= threshold:
            return "inflow", value, ratio
        if ratio <= -threshold:
            return "outflow", value, ratio
        return "neutral", value, ratio

    def _institution_signal(self, rows: list[dict[str, Any]]) -> dict[str, Any]:
        if not rows:
            return {
                "status": "unavailable",
                "direction": "insufficient",
                "direction_label": "暂无机构席位记录",
                "confidence": "low",
                "trade_date_start": None,
                "trade_date_end": None,
                "trading_day_count": 0,
                "stock_count": 0,
                "buy_cny": 0.0,
                "sell_cny": 0.0,
                "net_buy_cny": 0.0,
                "top_net_buys": [],
                "top_net_sells": [],
            }
        dates = sorted({item["trade_date"] for item in rows})
        selected_dates = set(dates[-5:])
        selected = [item for item in rows if item["trade_date"] in selected_dates]
        buy = sum(float(item["buy_cny"]) for item in selected)
        sell = sum(float(item["sell_cny"]) for item in selected)
        net = buy - sell
        ratio = net / (buy + sell) * 100 if buy + sell > 0 else 0.0
        direction = "inflow" if ratio >= 5 else "outflow" if ratio <= -5 else "neutral"
        return {
            "status": "available",
            "direction": direction,
            "direction_label": {
                "inflow": "机构席位偏净买入",
                "outflow": "机构席位偏净卖出",
                "neutral": "机构席位买卖接近",
            }[direction],
            "confidence": "medium",
            "trade_date_start": min(selected_dates),
            "trade_date_end": max(selected_dates),
            "trading_day_count": len(selected_dates),
            "stock_count": len({item["stock_code"] for item in selected}),
            "buy_cny": round(buy, 2),
            "sell_cny": round(sell, 2),
            "net_buy_cny": round(net, 2),
            "net_buy_ratio_pct": round(ratio, 4),
            "evidence_id": (
                f"capital:institution:{min(selected_dates).isoformat()}:"
                f"{max(selected_dates).isoformat()}"
            ),
            "top_net_buys": self._top_institutional_rows(selected, reverse=True),
            "top_net_sells": self._top_institutional_rows(selected, reverse=False),
        }

    @staticmethod
    def _top_institutional_rows(
        rows: list[dict[str, Any]],
        *,
        reverse: bool,
    ) -> list[dict[str, Any]]:
        ordered = sorted(rows, key=lambda item: item["net_buy_cny"], reverse=reverse)
        filtered = [
            item
            for item in ordered
            if (item["net_buy_cny"] > 0 if reverse else item["net_buy_cny"] < 0)
        ][:6]
        return [
            {
                "stock_code": item["stock_code"],
                "stock_name": item["stock_name"],
                "trade_date": item["trade_date"],
                "net_buy_cny": round(item["net_buy_cny"], 2),
                "buyer_count": item["buyer_count"],
                "seller_count": item["seller_count"],
                "listing_reason": item["listing_reason"],
                "evidence_id": (
                    f"capital:institution:{item['stock_code']}:"
                    f"{item['trade_date'].isoformat()}"
                ),
                "source_level": item["source_level"],
                "source_url": item["source_url"],
            }
            for item in filtered
        ]

    def _fund_link(
        self,
        *,
        fund_code: str,
        topic_signals: list[dict[str, Any]],
        institution: dict[str, Any],
    ) -> dict[str, Any] | None:
        fund = self.funds.get_fund(fund_code)
        profile = fund["profile"]
        disclosure = self.disclosures.get_fund(fund_code=fund_code)
        securities = [
            item
            for item in disclosure.get("security_holdings", [])
            if item.get("asset_type") == "stock"
        ]
        return self._build_exposure_link(
            scope="fund",
            scope_id=fund_code,
            scope_name=profile["short_name"],
            # 托管人和基准公式常带“银行/金融机构”等词，却不代表基金持有金融股。
            # 名称匹配只使用基金名称和跟踪标的，证券暴露另按每只股票行业判断。
            profile_text=" ".join(
                str(profile.get(key) or "")
                for key in ("full_name", "short_name", "tracking_target")
            ).replace("证券投资基金", ""),
            securities=[
                {
                    "security_code": item["security_code"],
                    "security_name": item["security_name"],
                    "exposure_pct": float(item["weight_pct"]),
                    "evidence_id": (
                        f"holding:{fund_code}:{item['security_code']}:"
                        f"{item['report_date']}"
                    ),
                }
                for item in securities
            ],
            topic_signals=topic_signals,
            institution=institution,
            disclosure_date=disclosure.get("latest_report_date"),
        )

    def _portfolio_link(
        self,
        *,
        user_id: str,
        portfolio_id: str,
        topic_signals: list[dict[str, Any]],
        institution: dict[str, Any],
    ) -> dict[str, Any] | None:
        exposure = self.portfolio_exposure.analyze(
            user_id=user_id,
            portfolio_id=portfolio_id,
        )
        return self._build_exposure_link(
            scope="portfolio",
            scope_id=portfolio_id,
            scope_name=exposure["portfolio_name"],
            profile_text=" ".join(
                item.get("industry_name", "")
                for item in exposure.get("industry_exposures", [])
            ),
            securities=[
                {
                    "security_code": item["security_code"],
                    "security_name": item["security_name"],
                    "exposure_pct": float(item["portfolio_exposure_pct"]),
                    "evidence_id": item["evidence_id"],
                }
                for item in exposure.get("security_exposures", [])
                if item.get("asset_type") == "stock"
            ],
            topic_signals=topic_signals,
            institution=institution,
            disclosure_date=exposure.get("latest_report_date"),
        )

    def _build_exposure_link(
        self,
        *,
        scope: str,
        scope_id: str,
        scope_name: str,
        profile_text: str,
        securities: list[dict[str, Any]],
        topic_signals: list[dict[str, Any]],
        institution: dict[str, Any],
        disclosure_date: str | None,
    ) -> dict[str, Any] | None:
        topic_matches: dict[str, list[dict[str, Any]]] = defaultdict(list)
        matched_security_codes: set[str] = set()
        evidence_ids: list[str] = []

        for security in securities:
            sector = self.sectors.get(
                security_code=security["security_code"],
                security_name=security["security_name"],
            )
            sector_text = " ".join(
                [
                    security["security_name"],
                    str((sector or {}).get("sector_name") or ""),
                    *list((sector or {}).get("aliases") or []),
                ]
            )
            for signal in topic_signals:
                definition = CAPITAL_TOPIC_BY_CODE[signal["topic_code"]]
                # 这里只判断当前证券自身。基金或组合名称命中主题，不应把全部持仓
                # 都算成该行业暴露。
                if self._matches_topic(sector_text, definition):
                    topic_matches[definition.code].append(security)

        institution_rows = [
            *institution.get("top_net_buys", []),
            *institution.get("top_net_sells", []),
        ]
        institution_by_code = {item["stock_code"]: item for item in institution_rows}
        direct_institution = []
        for security in securities:
            matched = institution_by_code.get(security["security_code"])
            if matched:
                direct_institution.append(
                    {
                        "security_code": security["security_code"],
                        "security_name": security["security_name"],
                        "exposure_pct": round(security["exposure_pct"], 4),
                        "net_buy_cny": matched["net_buy_cny"],
                        "evidence_id": matched["evidence_id"],
                    }
                )
                matched_security_codes.add(security["security_code"])
                evidence_ids.extend([matched["evidence_id"], security["evidence_id"]])

        matched_topics = []
        for signal in topic_signals:
            matched = topic_matches.get(signal["topic_code"], [])
            # 基金名称直接包含主题时也保留关联，但暴露比例仍只计算可核验证券。
            definition = CAPITAL_TOPIC_BY_CODE[signal["topic_code"]]
            name_match = self._matches_topic(profile_text, definition)
            if not matched and not name_match:
                continue
            for security in matched:
                matched_security_codes.add(security["security_code"])
                evidence_ids.append(security["evidence_id"])
            evidence_ids.append(signal["evidence_id"])
            matched_topics.append(
                {
                    "topic_code": signal["topic_code"],
                    "topic_label": signal["topic_label"],
                    "direction": signal["direction"],
                    "direction_label": signal["direction_label"],
                    "confidence": signal["confidence"],
                    "known_exposure_pct": round(
                        sum(item["exposure_pct"] for item in matched),
                        4,
                    ),
                    "evidence_id": signal["evidence_id"],
                }
            )

        if not matched_topics and not direct_institution:
            return None
        known_exposure = sum(
            item["exposure_pct"]
            for item in securities
            if item["security_code"] in matched_security_codes
        )
        directions = {item["direction"] for item in matched_topics}
        direction = (
            next(iter(directions))
            if len(directions) == 1
            else "mixed"
            if directions
            else "direct_institution"
        )
        return {
            "scope": scope,
            "scope_id": scope_id,
            "scope_name": scope_name,
            "direction": direction,
            "known_exposure_pct": round(known_exposure, 4),
            "disclosure_date": disclosure_date,
            "matched_topics": matched_topics[:6],
            "direct_institutional_securities": sorted(
                direct_institution,
                key=lambda item: abs(item["net_buy_cny"]),
                reverse=True,
            )[:6],
            "evidence_ids": list(dict.fromkeys(evidence_ids))[:16],
            "summary": (
                f"{scope_name}已识别 {len(matched_topics)} 个 ETF 资金主题，"
                f"与龙虎榜机构席位直接重合 {len(direct_institution)} 只证券；"
                f"可核验关联暴露约 {known_exposure:.2f}%。"
            ),
        }

    @staticmethod
    def _matches_topic(text: str, definition: CapitalTopicDefinition) -> bool:
        normalized = str(text or "").lower()
        return any(keyword.lower() in normalized for keyword in definition.keywords)

    @staticmethod
    def _overall_state(
        *,
        topic_signals: list[dict[str, Any]],
        institution: dict[str, Any],
    ) -> tuple[str, str, str]:
        inflows = [item for item in topic_signals if item["direction"] == "inflow"]
        outflows = [item for item in topic_signals if item["direction"] == "outflow"]
        top_in = max(inflows, key=lambda item: item["basis_value_cny"], default=None)
        top_out = min(outflows, key=lambda item: item["basis_value_cny"], default=None)
        if inflows and outflows:
            code, label = "diverging", "资金方向分化"
        elif inflows:
            code, label = "accumulating", "重点 ETF 获得净申购"
        elif outflows:
            code, label = "withdrawing", "重点 ETF 出现净赎回"
        else:
            code, label = "neutral", "大资金线索暂时中性"
        parts = []
        if top_in:
            parts.append(f"{top_in['topic_label']}是当前较强流入线索")
        if top_out:
            parts.append(f"{top_out['topic_label']}是当前较强流出线索")
        if institution.get("status") == "available":
            parts.append(institution["direction_label"])
        return code, label, "；".join(parts) + "。" if parts else "当前公开证据尚未形成明确方向。"

    @staticmethod
    def _overall_confidence(
        topic_signals: list[dict[str, Any]],
        institution: dict[str, Any],
    ) -> str:
        if any(item["confidence"] == "medium" for item in topic_signals):
            return "medium"
        return "medium" if institution.get("status") == "available" else "low"

    @staticmethod
    def _sources(
        etf_rows: list[dict[str, Any]],
        trade_rows: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        sources: dict[tuple[str, str], dict[str, Any]] = {}
        for item in [*etf_rows, *trade_rows]:
            key = (item["source"], item["source_url"])
            sources[key] = {
                "name": item["source"],
                "source_level": item["source_level"],
                "source_url": item["source_url"],
            }
        return list(sources.values())

    @staticmethod
    def _empty_overview(*, generated_at: datetime, status: dict[str, Any]) -> dict[str, Any]:
        return {
            "data_kind": "large_capital_signals",
            "data_mode": "unavailable",
            "generated_at": generated_at,
            "data_updated_at": None,
            "data_date": None,
            "state_code": "insufficient",
            "state_label": "等待首次同步",
            "confidence": "low",
            "summary": "点击刷新后采集 ETF 份额和龙虎榜机构席位。",
            "etf_history_days": status["etf_history_days"],
            "topic_signals": [],
            "sector_analysis": build_sector_analysis([], []),
            "institutional_signal": {
                "status": "unavailable",
                "direction": "insufficient",
                "direction_label": "暂无机构席位记录",
                "confidence": "low",
                "trade_date_start": None,
                "trade_date_end": None,
                "trading_day_count": 0,
                "stock_count": 0,
                "buy_cny": 0.0,
                "sell_cny": 0.0,
                "net_buy_cny": 0.0,
                "top_net_buys": [],
                "top_net_sells": [],
            },
            "exposure_links": [],
            "identity_boundary": {
                "confirmed_national_team_disclosure_count": 0,
                "label": "尚无可用资金身份披露",
                "explanation": "首次同步后仍只展示公开资金线索，不猜测匿名账户。",
            },
            "evidence_legend": {
                "A": "交易所或法定披露的直接事实",
                "B": "可追溯财经数据接口中的公开市场事实",
                "C": "成交结构算法或身份未知的间接线索",
            },
            "sources": [],
            "limitations": ["尚未同步公开资金数据。"],
            "methodology": "等待首次同步后计算。",
            "disclaimer": "大资金行为线索仅用于研究，不构成机构身份认定或投资建议。",
        }


class LargeCapitalSyncService:
    """执行外部采集、SQL 写入，并返回同步后的确定性摘要。"""

    def __init__(
        self,
        *,
        repository: LargeCapitalRepository,
        provider: LargeCapitalProvider,
        analysis: LargeCapitalAnalysisService,
    ) -> None:
        self.repository = repository
        self.provider = provider
        self.analysis = analysis

    def sync(self) -> dict[str, Any]:
        # 第一次同步补历史；之后每天只保存当天快照，减少对交易所接口的访问。
        include_history = self.repository.etf_history_day_count() < 21
        batch = self.provider.fetch(include_history=include_history)
        fetched_at = datetime.now(timezone.utc)
        writes = self.repository.save(batch=batch, fetched_at=fetched_at)
        overview = self.analysis.overview()
        data_date = overview.get("data_date")
        return {
            "data_kind": "large_capital_sync_result",
            "workflow": "large_capital_sync",
            "status": "partial" if batch.warnings else "completed",
            "summary": (
                f"大资金线索已更新：{writes['etf_snapshot_count']} 条 ETF 快照，"
                f"{writes['institutional_trade_count']} 条机构席位记录。"
            ),
            "history_bootstrap_requested": include_history,
            "warnings": list(batch.warnings),
            "writes": writes,
            # Background Job 的 result 最终存入数据库 JSON 字段，原生 date 不能直接
            # JSON 序列化，所以在领域服务出口统一转成 ISO 日期字符串。
            "data_date": data_date.isoformat() if isinstance(data_date, date) else data_date,
            "state_label": overview.get("state_label"),
        }

    def status(self) -> dict[str, Any]:
        return {
            "ok": True,
            "source": f"{ETF_SPOT_SOURCE} + {INSTITUTION_SOURCE}",
            **self.repository.status(),
        }


def classify_capital_topic(name: str) -> CapitalTopicDefinition | None:
    """使用版本化固定词典归类 ETF，不让模型在采集阶段临时改口径。"""

    normalized = str(name or "").lower()
    for definition in CAPITAL_TOPICS:
        if any(keyword.lower() in normalized for keyword in definition.keywords):
            return definition
    return None


def _finite_number(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _as_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value or "").strip()[:10]
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _rounded_or_none(value: float | None) -> float | None:
    return round(value, 2) if value is not None else None


large_capital_repository = LargeCapitalRepository()
large_capital_analysis_service = LargeCapitalAnalysisService(
    repository=large_capital_repository,
)
large_capital_sync_service = LargeCapitalSyncService(
    repository=large_capital_repository,
    provider=AkshareLargeCapitalProvider(),
    analysis=large_capital_analysis_service,
)
