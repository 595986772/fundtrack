"""宽基指数行情采集、持久化与确定性分析。

这一模块把职责分成三段：Provider 从 AKShare 读取原始行情，Repository 保存每日
收盘价和成交量，AnalysisService 再计算趋势、波动、回撤和活跃度。整个过程不调用
大模型，因此 REST、前端和 Agent Tool 看到的是同一套可复算结果。
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timezone
import math
import statistics
from typing import Any, Protocol

from sqlalchemy import delete, func, select

from app.database import Database, runtime_database
from app.models import MarketIndexBarModel


TRADING_DAYS_PER_YEAR = 252
MINIMUM_ANALYSIS_POINTS = 61
STORED_HISTORY_POINTS = 90
THREE_MONTH_TRADING_DAYS = 60
MARKET_INDEX_SOURCE = "Sina Index via AKShare"


class MarketIndexError(RuntimeError):
    """指数行情缺失、第三方字段非法或数据库写入失败。"""


@dataclass(frozen=True)
class MarketIndexDefinition:
    """一个需要跟踪的指数，以及外部 Provider 识别它所需的固定信息。"""

    index_code: str
    index_name: str
    market_segment: str
    provider_symbol: str
    source_url: str


# 顺序就是前端默认展示顺序：先看两市全貌，再比较大中小盘和成长风格。
MARKET_INDEX_DEFINITIONS = (
    MarketIndexDefinition(
        index_code="000001",
        index_name="上证指数",
        market_segment="沪市综合",
        provider_symbol="sh000001",
        source_url="https://finance.sina.com.cn/realstock/company/sh000001/nc.shtml",
    ),
    MarketIndexDefinition(
        index_code="399001",
        index_name="深证成指",
        market_segment="深市核心",
        provider_symbol="sz399001",
        source_url="https://finance.sina.com.cn/realstock/company/sz399001/nc.shtml",
    ),
    MarketIndexDefinition(
        index_code="000300",
        index_name="沪深300",
        market_segment="大盘核心",
        provider_symbol="sh000300",
        source_url="https://finance.sina.com.cn/realstock/company/sh000300/nc.shtml",
    ),
    MarketIndexDefinition(
        index_code="000905",
        index_name="中证500",
        market_segment="中盘风格",
        provider_symbol="sh000905",
        source_url="https://finance.sina.com.cn/realstock/company/sh000905/nc.shtml",
    ),
    MarketIndexDefinition(
        index_code="000852",
        index_name="中证1000",
        market_segment="小盘风格",
        provider_symbol="sh000852",
        source_url="https://finance.sina.com.cn/realstock/company/sh000852/nc.shtml",
    ),
    MarketIndexDefinition(
        index_code="399006",
        index_name="创业板指",
        market_segment="成长风格",
        provider_symbol="sz399006",
        source_url="https://finance.sina.com.cn/realstock/company/sz399006/nc.shtml",
    ),
    # 科创50与创业板分开观察；日线采集和盘中行情共用这份配置。
    MarketIndexDefinition(
        index_code="000688",
        index_name="科创50",
        market_segment="科创核心",
        provider_symbol="sh000688",
        source_url="https://finance.sina.com.cn/realstock/company/sh000688/nc.shtml",
    ),
)


@dataclass(frozen=True)
class MarketIndexBar:
    """Provider 清洗完成后的一天指数事实数据。"""

    data_date: date
    close: float
    volume: float


@dataclass(frozen=True)
class MarketIndexSeries:
    """一个指数最近一段时间的有序行情。"""

    definition: MarketIndexDefinition
    bars: tuple[MarketIndexBar, ...]


class MarketIndexProvider(Protocol):
    """外部指数 Provider 的最小接口，测试可注入 Fake 实现。"""

    source: str

    def fetch_all(self) -> tuple[MarketIndexSeries, ...]: ...


class AkshareMarketIndexProvider:
    """通过 AKShare 的新浪指数接口抓取配置中的宽基指数。"""

    source = MARKET_INDEX_SOURCE

    def __init__(self, history_points: int = STORED_HISTORY_POINTS) -> None:
        self.history_points = max(history_points, MINIMUM_ANALYSIS_POINTS)

    def fetch_all(self) -> tuple[MarketIndexSeries, ...]:
        """逐个抓取指数并统一成日期、收盘价和成交量。"""

        try:
            import akshare as ak
        except ModuleNotFoundError as exc:
            raise MarketIndexError("请先安装 requirements.txt 中的 akshare") from exc

        series: list[MarketIndexSeries] = []
        for definition in MARKET_INDEX_DEFINITIONS:
            try:
                frame = ak.stock_zh_index_daily(
                    symbol=definition.provider_symbol,
                )
            except Exception as exc:
                raise MarketIndexError(
                    f"AKShare 获取{definition.index_name}失败：{exc}"
                ) from exc
            series.append(self._normalize(definition=definition, frame=frame))
        return tuple(series)

    def _normalize(
        self,
        *,
        definition: MarketIndexDefinition,
        frame: Any,
    ) -> MarketIndexSeries:
        """丢弃非法第三方记录，并只保留分析所需的最近 90 个交易日。"""

        required_columns = {"date", "close", "volume"}
        if frame.empty or not required_columns.issubset(frame.columns):
            raise MarketIndexError(f"{definition.index_name}返回字段不完整")

        # 用字典按日期去重，再排序，避免第三方偶发重复行破坏 20/60 日窗口。
        normalized: dict[date, MarketIndexBar] = {}
        for raw in frame.tail(self.history_points + 10).to_dict(orient="records"):
            try:
                raw_date = raw["date"]
                point_date = (
                    raw_date.date()
                    if isinstance(raw_date, datetime)
                    else raw_date
                    if isinstance(raw_date, date)
                    else date.fromisoformat(str(raw_date)[:10])
                )
                close = float(raw["close"])
                volume = float(raw["volume"])
            except (KeyError, TypeError, ValueError) as exc:
                raise MarketIndexError(
                    f"{definition.index_name}存在无法解析的历史记录"
                ) from exc
            if (
                math.isfinite(close)
                and close > 0
                and math.isfinite(volume)
                and volume > 0
            ):
                normalized[point_date] = MarketIndexBar(
                    data_date=point_date,
                    close=close,
                    volume=volume,
                )

        bars = tuple(
            sorted(normalized.values(), key=lambda item: item.data_date)[
                -self.history_points :
            ]
        )
        if len(bars) < MINIMUM_ANALYSIS_POINTS:
            raise MarketIndexError(
                f"{definition.index_name}至少需要 {MINIMUM_ANALYSIS_POINTS} 个交易日"
            )
        return MarketIndexSeries(definition=definition, bars=bars)


class MarketIndexRepository:
    """`market_index_bars` 表的唯一读写入口。"""

    def __init__(self, database: Database = runtime_database) -> None:
        self.database = database
        self.database.create_schema()

    def replace_all(
        self,
        *,
        series: tuple[MarketIndexSeries, ...],
        fetched_at: datetime,
        source: str,
    ) -> dict[str, Any]:
        """在一个事务中替换本批指数的短历史，未传入的指数保持不变。"""

        if not series:
            raise MarketIndexError("指数同步结果不能为空")

        with self.database.session() as session, session.begin():
            existing_codes = set(
                session.scalars(select(MarketIndexBarModel.index_code).distinct())
            )
            for item in series:
                definition = item.definition
                dates = [bar.data_date for bar in item.bars]
                if len(dates) < MINIMUM_ANALYSIS_POINTS or len(dates) != len(set(dates)):
                    raise MarketIndexError(
                        f"{definition.index_name}历史点不足或存在重复日期"
                    )

                # 每次只保留最近 90 个交易日，表规模稳定，也不会混入旧来源口径。
                session.execute(
                    delete(MarketIndexBarModel).where(
                        MarketIndexBarModel.index_code == definition.index_code
                    )
                )
                session.add_all(
                    [
                        MarketIndexBarModel(
                            index_code=definition.index_code,
                            data_date=bar.data_date,
                            index_name=definition.index_name,
                            market_segment=definition.market_segment,
                            provider_symbol=definition.provider_symbol,
                            close=bar.close,
                            volume=bar.volume,
                            source=source,
                            source_url=definition.source_url,
                            fetched_at=fetched_at,
                        )
                        for bar in item.bars
                    ]
                )

        latest_date = max(item.bars[-1].data_date for item in series)
        return {
            "write_action": "updated" if existing_codes else "inserted",
            "index_count": len(series),
            "bar_count": sum(len(item.bars) for item in series),
            "data_date": latest_date.isoformat(),
        }

    def list_series(self) -> list[dict[str, Any]]:
        """按配置顺序返回各指数历史，供分析层统一计算。"""

        statement = select(MarketIndexBarModel).order_by(
            MarketIndexBarModel.index_code,
            MarketIndexBarModel.data_date,
        )
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        metadata: dict[str, dict[str, Any]] = {}
        with self.database.session() as session:
            for model in session.scalars(statement):
                grouped[model.index_code].append(
                    {
                        "date": model.data_date,
                        "close": model.close,
                        "volume": model.volume,
                    }
                )
                metadata[model.index_code] = {
                    "index_code": model.index_code,
                    "index_name": model.index_name,
                    "market_segment": model.market_segment,
                    "provider_symbol": model.provider_symbol,
                    "source": model.source,
                    "source_url": model.source_url,
                    "fetched_at": self._as_utc(model.fetched_at),
                }

        order = {item.index_code: position for position, item in enumerate(MARKET_INDEX_DEFINITIONS)}
        return [
            {**metadata[code], "bars": grouped[code]}
            for code in sorted(grouped, key=lambda value: order.get(value, 999))
        ]

    def status(self) -> dict[str, Any]:
        """返回指数数量、行情行数和数据日期覆盖范围。"""

        statement = select(
            func.count(MarketIndexBarModel.index_code),
            func.count(func.distinct(MarketIndexBarModel.index_code)),
            func.min(MarketIndexBarModel.data_date),
            func.max(MarketIndexBarModel.data_date),
            func.max(MarketIndexBarModel.fetched_at),
        )
        with self.database.session() as session:
            bar_count, index_count, start, end, fetched_at = session.execute(
                statement
            ).one()
        return {
            "index_bar_count": int(bar_count or 0),
            "index_count": int(index_count or 0),
            "index_snapshot_start": start.isoformat() if start else None,
            "index_snapshot_end": end.isoformat() if end else None,
            "index_last_successful_sync_at": (
                self._as_utc(fetched_at).isoformat() if fetched_at else None
            ),
        }

    @staticmethod
    def _as_utc(value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)


class MarketIndexSyncService:
    """编排一次配置指数的抓取和原子落库。"""

    def __init__(
        self,
        *,
        repository: MarketIndexRepository,
        provider: MarketIndexProvider | None = None,
    ) -> None:
        self.repository = repository
        self.provider = provider or AkshareMarketIndexProvider()

    def sync(self) -> dict[str, Any]:
        series = self.provider.fetch_all()
        fetched_at = datetime.now(timezone.utc)
        result = self.repository.replace_all(
            series=series,
            fetched_at=fetched_at,
            source=self.provider.source,
        )
        return {
            **result,
            "source": self.provider.source,
            "fetched_at": fetched_at.isoformat(),
            "summary": f"已同步 {result['index_count']} 个宽基指数至 {result['data_date']}。",
        }

    def status(self) -> dict[str, Any]:
        return self.repository.status()


class MarketIndexAnalysisService:
    """把数据库里的各组原始行情转换成可解释的指数对比。"""

    def __init__(self, repository: MarketIndexRepository) -> None:
        self.repository = repository

    def analyze(self) -> dict[str, Any]:
        raw_series = self.repository.list_series()
        usable = [
            item for item in raw_series if len(item["bars"]) >= MINIMUM_ANALYSIS_POINTS
        ]
        if not usable:
            return self._empty_result()

        analyses = [self._analyze_one(item) for item in usable]
        # 同时保留原有 20 日排名，并新增用户更关心的 30 日和近 3 个月排名。
        # 近 3 个月按 60 个交易日近似，这也是原有 60 日窗口的产品化名称。
        ranking_20d = sorted(
            analyses,
            key=lambda item: item["return_20d_pct"],
            reverse=True,
        )
        ranking_30d = sorted(
            analyses,
            key=lambda item: item["return_30d_pct"],
            reverse=True,
        )
        ranking_3m = sorted(
            analyses,
            key=lambda item: item["return_3m_pct"],
            reverse=True,
        )
        ranks_20d = {
            item["index_code"]: position
            for position, item in enumerate(ranking_20d, start=1)
        }
        ranks_30d = {
            item["index_code"]: position
            for position, item in enumerate(ranking_30d, start=1)
        }
        ranks_3m = {
            item["index_code"]: position
            for position, item in enumerate(ranking_3m, start=1)
        }
        for item in analyses:
            item["relative_strength_rank"] = ranks_20d[item["index_code"]]
            item["relative_strength_rank_30d"] = ranks_30d[item["index_code"]]
            item["relative_strength_rank_3m"] = ranks_3m[item["index_code"]]

        strongest_20d = ranking_20d[0]
        weakest_20d = ranking_20d[-1]
        strongest_30d = ranking_30d[0]
        weakest_30d = ranking_30d[-1]
        strongest_3m = ranking_3m[0]
        weakest_3m = ranking_3m[-1]
        spread_20d = (
            strongest_20d["return_20d_pct"] - weakest_20d["return_20d_pct"]
        )
        spread_30d = (
            strongest_30d["return_30d_pct"] - weakest_30d["return_30d_pct"]
        )
        spread_3m = strongest_3m["return_3m_pct"] - weakest_3m["return_3m_pct"]
        latest_date = max(item["data_date"] for item in analyses)
        latest_updated_at = max(item["_fetched_at"] for item in analyses)
        for item in analyses:
            item.pop("_fetched_at", None)

        if strongest_30d["index_code"] == strongest_3m["index_code"]:
            leaders_summary = (
                f"近 30 日和近 3 个月相对较强的均为{strongest_30d['index_name']}"
                f"（分别为{strongest_30d['return_30d_pct']:+.2f}%、"
                f"{strongest_3m['return_3m_pct']:+.2f}%）"
            )
        else:
            leaders_summary = (
                f"近 30 日相对较强的是{strongest_30d['index_name']}"
                f"（{strongest_30d['return_30d_pct']:+.2f}%），近 3 个月相对较强的"
                f"是{strongest_3m['index_name']}（{strongest_3m['return_3m_pct']:+.2f}%）"
            )
        if weakest_30d["index_code"] == weakest_3m["index_code"]:
            laggards_summary = (
                f"两个周期相对较弱的均为{weakest_30d['index_name']}"
            )
        else:
            laggards_summary = (
                f"同期较弱的分别是{weakest_30d['index_name']}和"
                f"{weakest_3m['index_name']}"
            )

        return {
            "data_kind": "market_index_comparison",
            "data_mode": "database",
            "data_updated_at": latest_updated_at.isoformat(),
            "data_date": latest_date,
            "index_count": len(analyses),
            # 无后缀字段继续表示原有 20 日排名，保证已有调用方兼容。
            "strongest_index_code": strongest_20d["index_code"],
            "strongest_index_name": strongest_20d["index_name"],
            "weakest_index_code": weakest_20d["index_code"],
            "weakest_index_name": weakest_20d["index_name"],
            "spread_20d_pct": round(spread_20d, 2),
            "strongest_index_30d_code": strongest_30d["index_code"],
            "strongest_index_30d_name": strongest_30d["index_name"],
            "weakest_index_30d_code": weakest_30d["index_code"],
            "weakest_index_30d_name": weakest_30d["index_name"],
            "spread_30d_pct": round(spread_30d, 2),
            "strongest_index_3m_code": strongest_3m["index_code"],
            "strongest_index_3m_name": strongest_3m["index_name"],
            "weakest_index_3m_code": weakest_3m["index_code"],
            "weakest_index_3m_name": weakest_3m["index_name"],
            "spread_3m_pct": round(spread_3m, 2),
            "summary": f"{len(analyses)} 个宽基中，{leaders_summary}；{laggards_summary}。",
            "indices": analyses,
            "methodology": (
                "每个指数使用自身收盘价计算 1/20/30/60 日收益，其中近 3 个月按"
                "60 个交易日近似；20 日年化波动率为最近 20 个日收益样本标准差乘"
                "sqrt(252)；最大回撤取最近 60 个交易日从阶段高点到后续低点的最大"
                "跌幅；成交活跃度比较最新成交量与最近 5 日均量。"
            ),
            "limitations": [
                "指数行情为日线收盘数据，不是盘中实时行情。",
                "成交量只在同一指数内部做时间比较，不能在不同指数之间直接比较绝对值。",
                "当前指数层不抓取全部成分股，因此不提供各指数成分股上涨家数。",
                "历史收益和相对强弱只描述已经发生的市场状态，不代表未来表现。",
            ],
        }

    def _analyze_one(self, series: dict[str, Any]) -> dict[str, Any]:
        """对一个指数执行全部透明公式。"""

        bars = series["bars"]
        closes = [float(item["close"]) for item in bars]
        volumes = [float(item["volume"]) for item in bars]
        daily_returns = [
            current / previous - 1
            for previous, current in zip(closes, closes[1:])
        ]
        return_1d = (closes[-1] / closes[-2] - 1) * 100
        return_20d = (closes[-1] / closes[-21] - 1) * 100
        return_30d = (closes[-1] / closes[-31] - 1) * 100
        return_60d = (
            closes[-1] / closes[-(THREE_MONTH_TRADING_DAYS + 1)] - 1
        ) * 100
        # 3 个月没有固定交易日数量，产品里统一按 60 个交易日近似。
        return_3m = return_60d
        volatility = (
            statistics.stdev(daily_returns[-20:])
            * math.sqrt(TRADING_DAYS_PER_YEAR)
            * 100
        )
        max_drawdown = self._max_drawdown_pct(closes[-61:])
        volume_vs_average = volumes[-1] / statistics.fmean(volumes[-5:]) * 100

        trend_label = self._trend_label(return_20d, return_60d)
        volatility_label = self._volatility_label(volatility)
        activity_label = self._activity_label(volume_vs_average)
        direction = (
            "positive"
            if trend_label in {"上行", "中期偏强"}
            else "negative"
            if trend_label in {"下行", "中期偏弱"}
            else "neutral"
        )
        data_date = bars[-1]["date"].isoformat()

        return {
            "index_code": series["index_code"],
            "provider_symbol": series["provider_symbol"],
            "index_name": series["index_name"],
            "market_segment": series["market_segment"],
            "data_date": data_date,
            "close": round(closes[-1], 3),
            "daily_return_pct": round(return_1d, 2),
            "return_20d_pct": round(return_20d, 2),
            "return_30d_pct": round(return_30d, 2),
            "return_60d_pct": round(return_60d, 2),
            "return_3m_pct": round(return_3m, 2),
            "annualized_volatility_20d_pct": round(volatility, 2),
            "max_drawdown_60d_pct": round(max_drawdown, 2),
            "volume_vs_five_day_average_pct": round(volume_vs_average, 2),
            "trend_label": trend_label,
            "volatility_label": volatility_label,
            "activity_label": activity_label,
            "direction": direction,
            "relative_strength_rank": 0,
            "relative_strength_rank_30d": 0,
            "relative_strength_rank_3m": 0,
            "summary": (
                f"{series['index_name']}近 20 日{return_20d:+.2f}%，近 30 日"
                f"{return_30d:+.2f}%，近 3 个月{return_3m:+.2f}%，趋势为"
                f"{trend_label}；20 日年化波动{volatility:.2f}%，近 3 个月最大"
                f"回撤{max_drawdown:.2f}%，成交量处于{activity_label}状态。"
            ),
            "evidence_id": f"market_index_{series['index_code']}_{data_date}",
            "source": series["source"],
            "source_url": series["source_url"],
            "history": [
                {"date": bar["date"].isoformat(), "close": bar["close"], "volume": bar["volume"]}
                for bar in bars
            ],
            "_fetched_at": series["fetched_at"],
        }

    @staticmethod
    def _max_drawdown_pct(closes: list[float]) -> float:
        peak = closes[0]
        maximum_drawdown = 0.0
        for close in closes:
            peak = max(peak, close)
            maximum_drawdown = min(maximum_drawdown, (close / peak - 1) * 100)
        return maximum_drawdown

    @staticmethod
    def _trend_label(return_20d: float, return_60d: float) -> str:
        if return_20d >= 3 and return_60d >= 3:
            return "上行"
        if return_20d <= -3 and return_60d <= -3:
            return "下行"
        if return_60d >= 3 and return_20d > -3:
            return "中期偏强"
        if return_60d <= -3 and return_20d < 3:
            return "中期偏弱"
        if abs(return_20d) >= 3:
            return "短中期分化"
        return "震荡"

    @staticmethod
    def _volatility_label(volatility_pct: float) -> str:
        if volatility_pct <= 15:
            return "偏低"
        if volatility_pct <= 25:
            return "中等"
        return "偏高"

    @staticmethod
    def _activity_label(volume_vs_average_pct: float) -> str:
        if volume_vs_average_pct >= 110:
            return "放量"
        if volume_vs_average_pct <= 90:
            return "缩量"
        return "常态"

    @staticmethod
    def _empty_result() -> dict[str, Any]:
        return {
            "data_kind": "market_index_comparison",
            "data_mode": "unavailable",
            "data_updated_at": None,
            "data_date": None,
            "index_count": 0,
            "strongest_index_code": None,
            "strongest_index_name": None,
            "weakest_index_code": None,
            "weakest_index_name": None,
            "spread_20d_pct": None,
            "strongest_index_30d_code": None,
            "strongest_index_30d_name": None,
            "weakest_index_30d_code": None,
            "weakest_index_30d_name": None,
            "spread_30d_pct": None,
            "strongest_index_3m_code": None,
            "strongest_index_3m_name": None,
            "weakest_index_3m_code": None,
            "weakest_index_3m_name": None,
            "spread_3m_pct": None,
            "summary": "尚未同步宽基指数行情，点击市场环境右上角的同步按钮后显示。",
            "indices": [],
            "methodology": (
                "同步至少 61 个交易日后计算 20 日、30 日、近 3 个月收益，以及"
                "指数趋势、波动、回撤和成交活跃度。"
            ),
            "limitations": ["当前数据库没有足够的宽基指数历史行情。"],
        }


market_index_repository = MarketIndexRepository(runtime_database)
market_index_sync_service = MarketIndexSyncService(
    repository=market_index_repository,
)
market_index_analysis_service = MarketIndexAnalysisService(
    repository=market_index_repository,
)
