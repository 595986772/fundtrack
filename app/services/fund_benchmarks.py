"""同步并比较基金业绩基准的确定性服务。

基金档案通常把基准写成一条文字公式。这个模块负责三件事：
1. 把已经支持的指数名称解析成明确组件和权重；
2. 通过 AKShare 读取指数历史值，合成为基金专属的基准日收益并落库；
3. 在相同交易日上比较基金总回报和基准回报。

整个过程不调用大模型。无法识别的基准会明确返回 unavailable，不会拿沪深300等
宽基指数替代所有基金。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import math
import re
from typing import Any, Literal

from sqlalchemy import delete, func, select

from app.config import settings
from app.database import Database, runtime_database
from app.models import FundBenchmarkReturnModel, FundBenchmarkSyncModel


TRADING_DAYS_PER_YEAR = 252
MIN_BENCHMARK_COVERAGE_PCT = 80.0
BenchmarkSyncStatus = Literal["matched", "proxy", "unavailable"]
BenchmarkComparisonStatus = Literal[
    "matched",
    "proxy",
    "unavailable",
    "insufficient",
]
BenchmarkComponentKind = Literal[
    "stock_index",
    "cni_index",
    "bond_index",
    "cash",
]


class FundBenchmarkDataError(RuntimeError):
    """基准公式、外部指数数据或数据库记录无法组成合法序列。"""


@dataclass(frozen=True)
class BenchmarkComponent:
    """业绩基准公式中的一个指数或现金分量。"""

    key: str
    name: str
    kind: BenchmarkComponentKind
    weight: float
    symbol: str | None
    source: str
    source_url: str


@dataclass(frozen=True)
class FundBenchmarkPoint:
    """某个交易日的基金专属基准收益，使用小数而非百分数。"""

    benchmark_date: date
    daily_return: float


@dataclass(frozen=True)
class FundBenchmarkSeries:
    """一次基准解析和采集得到的完整可落库结果。"""

    fund_code: str
    benchmark_name: str
    benchmark_status: BenchmarkSyncStatus
    points: list[FundBenchmarkPoint]
    methodology: str
    source: str | None
    source_urls: list[str]
    warnings: list[str]


_COMPONENT_SPECS: dict[str, dict[str, str]] = {
    "hs300": {
        "name": "沪深300指数",
        "kind": "stock_index",
        "symbol": "sh000300",
        "source": "Sina Finance via AKShare",
        "source_url": "https://finance.sina.com.cn/realstock/company/sh000300/nc.shtml",
    },
    "csi_liquor": {
        "name": "中证白酒指数",
        "kind": "stock_index",
        "symbol": "sz399997",
        "source": "Sina Finance via AKShare",
        "source_url": "https://www.csindex.com.cn/zh-CN/indices/index-detail/399997",
    },
    "cni_chips": {
        "name": "国证半导体芯片指数",
        "kind": "cni_index",
        "symbol": "980017",
        "source": "CNIndex via AKShare",
        "source_url": "https://www.cnindex.com.cn/module/index-detail.html?act_menu=1&indexCode=980017",
    },
    "china_bond_new_composite_wealth": {
        "name": "中债新综合财富指数",
        "kind": "bond_index",
        "symbol": "new-composite-wealth-total",
        "source": "ChinaBond via AKShare",
        "source_url": "https://yield.chinabond.com.cn/cbweb-mn/indices/single_index_query",
    },
}


class AkshareFundBenchmarkProvider:
    """把常见公募基金业绩基准公式转换成每日组合收益。"""

    source = "AKShare benchmark components"

    def __init__(self, *, cash_rate_annual_pct: float | None = None) -> None:
        self.cash_rate_annual_pct = (
            settings.fund_benchmark_cash_rate_annual_pct
            if cash_rate_annual_pct is None
            else float(cash_rate_annual_pct)
        )
        if (
            not math.isfinite(self.cash_rate_annual_pct)
            or self.cash_rate_annual_pct <= -100
        ):
            raise ValueError("业绩基准现金利率必须是大于 -100% 的有限数值")
        # 同一次基金批量同步里，沪深300可能被多只基金复用。缓存只活在这一批次，
        # begin_batch 会在下次同步前清空，避免把昨天的数据永久当作最新数据。
        self._batch_cache: dict[
            tuple[str, date, date],
            dict[date, float],
        ] = {}

    def begin_batch(self) -> None:
        """开始新一批同步并清空短生命周期指数缓存。"""

        self._batch_cache.clear()

    def fetch_series(
        self,
        *,
        profile: dict[str, Any],
        date_from: date,
        date_to: date,
    ) -> FundBenchmarkSeries:
        """解析一只基金的基准公式，并合成指定日期范围内的日收益。"""

        fund_code = str(profile.get("fund_code") or "").strip()
        benchmark_name = str(profile.get("benchmark") or "").strip()
        if not fund_code:
            raise FundBenchmarkDataError("基金档案缺少 fund_code")
        if date_from >= date_to:
            raise FundBenchmarkDataError("业绩基准同步至少需要两个不同日期")

        components, resolution_error = self._resolve_components(benchmark_name)
        if resolution_error:
            return FundBenchmarkSeries(
                fund_code=fund_code,
                benchmark_name=benchmark_name or "未披露业绩比较基准",
                benchmark_status="unavailable",
                points=[],
                methodology="当前没有可计算的业绩基准公式。",
                source=None,
                source_urls=[],
                warnings=[resolution_error],
            )

        market_components = [
            component for component in components if component.kind != "cash"
        ]
        if not market_components:
            raise FundBenchmarkDataError("业绩基准至少需要一个可观察指数分量")

        component_returns: dict[str, dict[date, float]] = {}
        for component in market_components:
            values = self._load_component_values(
                component=component,
                date_from=date_from,
                date_to=date_to,
            )
            component_returns[component.key] = self._daily_returns(
                values=values,
                date_from=date_from,
                date_to=date_to,
            )

        # 组合基准只在所有市场分量都有值的共同交易日计算，避免给缺失分量偷偷补 0。
        common_dates = set.intersection(
            *(set(values) for values in component_returns.values())
        )
        if len(common_dates) < 2:
            raise FundBenchmarkDataError("业绩基准共同交易日不足")

        cash_daily_return = (
            (1 + self.cash_rate_annual_pct / 100)
            ** (1 / TRADING_DAYS_PER_YEAR)
            - 1
        )
        points: list[FundBenchmarkPoint] = []
        for benchmark_date in sorted(common_dates):
            daily_return = 0.0
            for component in components:
                component_return = (
                    cash_daily_return
                    if component.kind == "cash"
                    else component_returns[component.key][benchmark_date]
                )
                daily_return += component.weight * component_return
            if not math.isfinite(daily_return) or daily_return <= -1:
                raise FundBenchmarkDataError("合成后的基准日收益率非法")
            points.append(
                FundBenchmarkPoint(
                    benchmark_date=benchmark_date,
                    daily_return=daily_return,
                )
            )

        has_cash_proxy = any(item.kind == "cash" for item in components)
        status: BenchmarkSyncStatus = "proxy" if has_cash_proxy else "matched"
        warnings: list[str] = []
        if has_cash_proxy:
            warnings.append(
                "基准中的活期存款部分使用项目配置的固定年利率 "
                f"{self.cash_rate_annual_pct:.2f}%，不是实时银行利率。"
            )
        component_text = " + ".join(
            f"{component.name} {component.weight * 100:.2f}%"
            for component in components
        )
        sources = list(dict.fromkeys(item.source for item in components))
        source_urls = list(
            dict.fromkeys(
                item.source_url for item in components if item.source_url
            )
        )
        return FundBenchmarkSeries(
            fund_code=fund_code,
            benchmark_name=benchmark_name,
            benchmark_status=status,
            points=points,
            methodology=(
                f"按基金披露公式将 {component_text} 的每日收益固定权重合成；"
                "市场分量只使用共同交易日。"
            ),
            source=" · ".join(sources),
            source_urls=source_urls,
            warnings=warnings,
        )

    def _resolve_components(
        self,
        benchmark_name: str,
    ) -> tuple[list[BenchmarkComponent], str | None]:
        """把支持的中文基准名称解析成指数代码和权重。"""

        if not benchmark_name:
            return [], "基金档案没有披露业绩比较基准。"
        normalized = (
            benchmark_name.replace("×", "*")
            .replace("＊", "*")
            .replace("X", "*")
            .replace("x", "*")
            .replace("＋", "+")
        )
        normalized = re.sub(r"\s+", "", normalized)

        requested: list[tuple[str, str]] = []
        for key in (
            "china_bond_new_composite_wealth",
            "cni_chips",
            "csi_liquor",
            "hs300",
        ):
            name = _COMPONENT_SPECS[key]["name"]
            if name in normalized:
                requested.append((key, name))

        has_cash = "活期存款" in normalized
        if not requested:
            return [], f"暂不支持该业绩基准：{benchmark_name}"

        components: list[BenchmarkComponent] = []
        for key, name in requested:
            weight = self._extract_weight(normalized, name)
            if weight is None and len(requested) == 1 and not has_cash:
                weight = 1.0
            if weight is None:
                return [], f"无法解析 {name} 在业绩基准中的权重。"
            spec = _COMPONENT_SPECS[key]
            components.append(
                BenchmarkComponent(
                    key=key,
                    name=spec["name"],
                    kind=spec["kind"],  # type: ignore[arg-type]
                    weight=weight,
                    symbol=spec["symbol"],
                    source=spec["source"],
                    source_url=spec["source_url"],
                )
            )

        if has_cash:
            cash_weight = self._extract_weight(normalized, "活期存款")
            if cash_weight is None:
                return [], "无法解析活期存款在业绩基准中的权重。"
            components.append(
                BenchmarkComponent(
                    key="cash",
                    name="活期存款税后利率",
                    kind="cash",
                    weight=cash_weight,
                    symbol=None,
                    source="FundScope project configuration",
                    source_url="",
                )
            )

        weight_sum = sum(item.weight for item in components)
        if not math.isclose(weight_sum, 1.0, abs_tol=0.005):
            return [], (
                "已识别的业绩基准权重合计为 "
                f"{weight_sum * 100:.2f}%，无法安全合成完整基准。"
            )
        return components, None

    @staticmethod
    def _extract_weight(text: str, label: str) -> float | None:
        """从“指数收益率*95%”一类片段中提取 0.95。"""

        position = text.find(label)
        if position < 0:
            return None
        tail = text[position + len(label) : position + len(label) + 50]
        match = re.search(r"\*(\d+(?:\.\d+)?)%", tail)
        if match is None:
            return None
        return float(match.group(1)) / 100

    def _load_component_values(
        self,
        *,
        component: BenchmarkComponent,
        date_from: date,
        date_to: date,
    ) -> dict[date, float]:
        """调用与指数类型对应的 AKShare API，并统一成日期到收盘值。"""

        cache_key = (component.key, date_from, date_to)
        cached = self._batch_cache.get(cache_key)
        if cached is not None:
            return dict(cached)

        try:
            import akshare as ak
        except ModuleNotFoundError as exc:
            raise FundBenchmarkDataError(
                "请先安装 requirements.txt 中的 akshare"
            ) from exc

        try:
            if component.kind == "stock_index":
                # 新浪接口返回完整历史，当前环境比东财接口更稳定；后续只保留需要区间。
                frame = ak.stock_zh_index_daily(symbol=component.symbol)
                date_column, value_column = "date", "close"
            elif component.kind == "cni_index":
                frame = ak.index_hist_cni(
                    symbol=component.symbol,
                    start_date=(date_from - timedelta(days=14)).strftime("%Y%m%d"),
                    end_date=date_to.strftime("%Y%m%d"),
                )
                date_column, value_column = "日期", "收盘价"
            elif component.kind == "bond_index":
                frame = ak.bond_new_composite_index_cbond(
                    indicator="财富",
                    period="总值",
                )
                date_column, value_column = "date", "value"
            else:
                raise FundBenchmarkDataError(
                    f"不支持直接抓取的基准组件：{component.kind}"
                )
        except FundBenchmarkDataError:
            raise
        except Exception as exc:
            raise FundBenchmarkDataError(
                f"AKShare 获取 {component.name} 失败：{exc}"
            ) from exc

        if frame.empty or not {date_column, value_column}.issubset(frame.columns):
            raise FundBenchmarkDataError(f"{component.name} 返回字段不完整")

        values: dict[date, float] = {}
        for raw in frame.to_dict(orient="records"):
            try:
                raw_date = raw[date_column]
                if isinstance(raw_date, datetime):
                    point_date = raw_date.date()
                elif isinstance(raw_date, date):
                    point_date = raw_date
                else:
                    point_date = date.fromisoformat(str(raw_date)[:10])
                value = float(raw[value_column])
            except (KeyError, TypeError, ValueError) as exc:
                raise FundBenchmarkDataError(
                    f"{component.name} 存在无法解析的历史记录"
                ) from exc
            if math.isfinite(value) and value > 0:
                values[point_date] = value

        if len(values) < 2:
            raise FundBenchmarkDataError(f"{component.name} 历史点不足")
        self._batch_cache[cache_key] = dict(values)
        return values

    @staticmethod
    def _daily_returns(
        *,
        values: dict[date, float],
        date_from: date,
        date_to: date,
    ) -> dict[date, float]:
        """用相邻指数值计算日收益，并保留指定区间内的结束日期。"""

        sorted_values = sorted(values.items())
        returns: dict[date, float] = {}
        for (_previous_date, previous), (current_date, current) in zip(
            sorted_values,
            sorted_values[1:],
        ):
            if not date_from <= current_date <= date_to:
                continue
            daily_return = current / previous - 1
            if not math.isfinite(daily_return) or daily_return <= -1:
                raise FundBenchmarkDataError("指数日收益率非法")
            returns[current_date] = daily_return
        if not returns:
            raise FundBenchmarkDataError("指定区间没有基准日收益")
        return returns


class FundBenchmarkRepository:
    """业绩基准两张表的 SQLAlchemy Repository。"""

    def __init__(self, database: Database = runtime_database) -> None:
        self.database = database
        self.database.create_schema()

    def replace_series(
        self,
        *,
        series: FundBenchmarkSeries,
        fetched_at: datetime,
    ) -> dict[str, Any]:
        """原子替换一只基金的基准序列和元数据。"""

        dates = [point.benchmark_date for point in series.points]
        if len(dates) != len(set(dates)):
            raise FundBenchmarkDataError("业绩基准序列包含重复日期")
        coverage_start = min(dates) if dates else None
        coverage_end = max(dates) if dates else None

        with self.database.session() as session, session.begin():
            session.execute(
                delete(FundBenchmarkReturnModel).where(
                    FundBenchmarkReturnModel.fund_code == series.fund_code
                )
            )
            for point in series.points:
                session.add(
                    FundBenchmarkReturnModel(
                        fund_code=series.fund_code,
                        benchmark_date=point.benchmark_date,
                        daily_return_pct=point.daily_return * 100,
                        fetched_at=fetched_at,
                    )
                )

            sync = session.get(FundBenchmarkSyncModel, series.fund_code)
            if sync is None:
                sync = FundBenchmarkSyncModel(fund_code=series.fund_code)
                session.add(sync)
            sync.benchmark_name = series.benchmark_name
            sync.benchmark_status = series.benchmark_status
            sync.methodology = series.methodology
            sync.coverage_start = coverage_start
            sync.coverage_end = coverage_end
            sync.record_count = len(series.points)
            sync.source = series.source
            sync.source_urls = list(series.source_urls)
            sync.warnings = list(series.warnings)
            sync.fetched_at = fetched_at

        return {
            "benchmark_status": series.benchmark_status,
            "record_count": len(series.points),
            "coverage_start": coverage_start.isoformat() if coverage_start else None,
            "coverage_end": coverage_end.isoformat() if coverage_end else None,
            "warnings": list(series.warnings),
        }

    def get_context(self, *, fund_code: str) -> dict[str, Any] | None:
        """返回风险比较需要的日收益和同步元数据。"""

        point_statement = (
            select(FundBenchmarkReturnModel)
            .where(FundBenchmarkReturnModel.fund_code == fund_code)
            .order_by(FundBenchmarkReturnModel.benchmark_date.asc())
        )
        with self.database.session() as session:
            sync = session.get(FundBenchmarkSyncModel, fund_code)
            if sync is None:
                return None
            points = list(session.scalars(point_statement))
            return {
                "fund_code": fund_code,
                "benchmark_name": sync.benchmark_name,
                "benchmark_status": sync.benchmark_status,
                "methodology": sync.methodology,
                "coverage_start": (
                    sync.coverage_start.isoformat()
                    if sync.coverage_start
                    else None
                ),
                "coverage_end": (
                    sync.coverage_end.isoformat() if sync.coverage_end else None
                ),
                "record_count": sync.record_count,
                "source": sync.source,
                "source_urls": list(sync.source_urls or []),
                "warnings": list(sync.warnings or []),
                "fetched_at": self._as_utc(sync.fetched_at).isoformat(),
                "points": [
                    {
                        "benchmark_date": point.benchmark_date.isoformat(),
                        "daily_return": point.daily_return_pct / 100,
                    }
                    for point in points
                ],
            }

    def status(self) -> dict[str, Any]:
        """汇总基准记录数、基金数、质量状态和最近同步日期。"""

        totals = select(
            func.count(FundBenchmarkReturnModel.fund_code),
            func.count(func.distinct(FundBenchmarkReturnModel.fund_code)),
            func.max(FundBenchmarkReturnModel.benchmark_date),
        )
        grouped = select(
            FundBenchmarkSyncModel.benchmark_status,
            func.count(FundBenchmarkSyncModel.fund_code),
        ).group_by(FundBenchmarkSyncModel.benchmark_status)
        latest = select(func.max(FundBenchmarkSyncModel.fetched_at))
        with self.database.session() as session:
            record_count, fund_count, coverage_end = session.execute(totals).one()
            counts = {
                status: int(count)
                for status, count in session.execute(grouped).all()
            }
            fetched_at = session.scalar(latest)
        return {
            "record_count": int(record_count or 0),
            "fund_count": int(fund_count or 0),
            "matched_fund_count": counts.get("matched", 0),
            "proxy_fund_count": counts.get("proxy", 0),
            "unavailable_fund_count": counts.get("unavailable", 0),
            "coverage_end": coverage_end.isoformat() if coverage_end else None,
            "last_successful_sync_at": (
                self._as_utc(fetched_at).isoformat() if fetched_at else None
            ),
        }

    @staticmethod
    def _as_utc(value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)


class FundBenchmarkPerformanceService:
    """在共同交易日上计算基金相对业绩基准的超额收益。"""

    def __init__(
        self,
        repository: FundBenchmarkRepository,
        *,
        minimum_coverage_pct: float = MIN_BENCHMARK_COVERAGE_PCT,
    ) -> None:
        self.repository = repository
        self.minimum_coverage_pct = float(minimum_coverage_pct)

    def compare(
        self,
        *,
        fund_code: str,
        profile: dict[str, Any],
        fund_index_points: list[dict[str, Any]],
        fund_daily_returns: list[float],
    ) -> dict[str, Any]:
        """返回同日基金回报、基准回报及两者百分点差。"""

        declared_name = str(profile.get("benchmark") or "").strip()
        context = self.repository.get_context(fund_code=fund_code)
        if context is None:
            return self._unavailable(
                benchmark_name=declared_name or "未披露业绩比较基准",
                warning="该基金的业绩基准尚未同步。",
            )
        if context["benchmark_status"] == "unavailable":
            return self._unavailable(
                benchmark_name=context["benchmark_name"],
                warning=(
                    context["warnings"][0]
                    if context["warnings"]
                    else "当前业绩基准无法计算。"
                ),
                context=context,
            )

        fund_by_date = {
            point["nav_date"]: daily_return
            for point, daily_return in zip(
                fund_index_points[1:],
                fund_daily_returns,
            )
        }
        benchmark_by_date = {
            point["benchmark_date"]: float(point["daily_return"])
            for point in context["points"]
        }
        common_dates = sorted(set(fund_by_date) & set(benchmark_by_date))
        coverage_pct = (
            len(common_dates) / len(fund_by_date) * 100 if fund_by_date else 0.0
        )
        if len(common_dates) < 2 or coverage_pct < self.minimum_coverage_pct:
            result = self._unavailable(
                benchmark_name=context["benchmark_name"],
                warning=(
                    f"业绩基准只覆盖基金区间 {coverage_pct:.2f}% 的交易日，"
                    f"低于 {self.minimum_coverage_pct:.0f}% 比较门槛。"
                ),
                context=context,
                status="insufficient",
            )
            result["coverage_pct"] = round(coverage_pct, 2)
            result["observation_count"] = len(common_dates)
            return result

        fund_value = 1.0
        benchmark_value = 1.0
        for point_date in common_dates:
            fund_value *= 1 + fund_by_date[point_date]
            benchmark_value *= 1 + benchmark_by_date[point_date]
        fund_return_pct = (fund_value - 1) * 100
        benchmark_return_pct = (benchmark_value - 1) * 100

        return {
            "benchmark_name": context["benchmark_name"],
            "status": context["benchmark_status"],
            "period_return_pct": round(benchmark_return_pct, 2),
            "fund_aligned_return_pct": round(fund_return_pct, 2),
            # 这里是最直观的百分点差，不是“基金/基准-1”的相对倍率。
            "excess_return_pct": round(
                fund_return_pct - benchmark_return_pct,
                2,
            ),
            "observation_count": len(common_dates),
            "coverage_pct": round(coverage_pct, 2),
            "period_start": common_dates[0],
            "period_end": common_dates[-1],
            "source": context["source"],
            "source_urls": context["source_urls"],
            "methodology": (
                context["methodology"]
                + " 基金与基准只在共同交易日复利，超额收益为两者区间收益的百分点差。"
            ),
            "warnings": list(context["warnings"]),
        }

    @staticmethod
    def _unavailable(
        *,
        benchmark_name: str,
        warning: str,
        context: dict[str, Any] | None = None,
        status: BenchmarkComparisonStatus = "unavailable",
    ) -> dict[str, Any]:
        warnings = list(context.get("warnings") or []) if context else []
        if warning not in warnings:
            warnings.append(warning)
        return {
            "benchmark_name": benchmark_name,
            "status": status,
            "period_return_pct": None,
            "fund_aligned_return_pct": None,
            "excess_return_pct": None,
            "observation_count": 0,
            "coverage_pct": 0.0,
            "period_start": None,
            "period_end": None,
            "source": context.get("source") if context else None,
            "source_urls": list(context.get("source_urls") or []) if context else [],
            "methodology": (
                context.get("methodology")
                if context
                else "尚无可用于同区间比较的业绩基准序列。"
            ),
            "warnings": warnings,
        }


fund_benchmark_provider = AkshareFundBenchmarkProvider()
fund_benchmark_repository = FundBenchmarkRepository(runtime_database)
fund_benchmark_performance_service = FundBenchmarkPerformanceService(
    fund_benchmark_repository
)
