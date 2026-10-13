"""FundScope 每日市场状态原始数据的采集与持久化。

这条链路不调用大模型：AKShare 负责获取市场宽度和指数成交额，Python 负责清洗，
SQLAlchemy 负责保存。MarketSentimentService 再根据这些事实执行固定公式。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import math
from typing import Any, Protocol

from sqlalchemy import func, select

from app.config import settings
from app.database import Database, runtime_database
from app.models import MarketSnapshotModel
from app.services.market_indices import (
    MarketIndexError,
    MarketIndexSyncService,
    market_index_sync_service,
)


MARKET_SYNC_SYSTEM_USER_ID = "fundscope-system"
MARKET_SYNC_SCHEDULE_ID = "market-snapshot-daily-sync"
MARKET_SYNC_SOURCE = "AKShare / LeGuLeGu + Eastmoney/CSI Index"
EASTMONEY_TURNOVER_SOURCE = "Eastmoney Index via AKShare"
EASTMONEY_TURNOVER_URL = "https://quote.eastmoney.com/zs000300.html"
CSINDEX_TURNOVER_SOURCE = "CSI Index via AKShare"
CSINDEX_TURNOVER_URL = "https://www.csindex.com.cn/#/indices/family/detail?indexCode=000300"


class MarketDataSyncError(RuntimeError):
    """外部市场数据无法获取、字段非法或数据库写入失败。"""


@dataclass(frozen=True)
class MarketAmountPoint:
    """沪深300某个交易日的成交额。"""

    data_date: date
    amount_cny: float


@dataclass(frozen=True)
class MarketSnapshot:
    """Provider 清洗完成后交给 Repository 的稳定市场结构。"""

    data_date: date
    observed_at: datetime
    advancing_count: int
    declining_count: int
    flat_count: int
    suspended_count: int
    limit_up_count: int
    limit_down_count: int
    market_activity_pct: float
    daily_amounts: tuple[MarketAmountPoint, ...]
    # 成交额存在两个等口径的数据源。把实际命中的来源随快照一起传给 Repository，
    # 页面展示的数据来源才不会永远写死成东方财富。
    turnover_source: str = EASTMONEY_TURNOVER_SOURCE
    turnover_source_url: str = EASTMONEY_TURNOVER_URL


class MarketSnapshotProvider(Protocol):
    """外部数据适配器必须提供来源名称和一次快照抓取方法。"""

    source: str

    def fetch_snapshot(self) -> MarketSnapshot: ...


class AkshareMarketSnapshotProvider:
    """通过 AKShare 获取市场宽度，以及沪深300最近 5 日成交额。"""

    source = MARKET_SYNC_SOURCE

    def fetch_snapshot(self) -> MarketSnapshot:
        """抓取同一交易日的两类原始市场数据并完成字段校验。"""

        # 只有真正执行同步 Job 时才导入大型财经依赖，普通接口启动更轻。
        try:
            import akshare as ak
            import pandas as pd
        except ModuleNotFoundError as exc:
            raise MarketDataSyncError("请先安装 requirements.txt 中的 akshare") from exc

        try:
            activity_frame = ak.stock_market_activity_legu()
            activity = dict(zip(activity_frame["item"], activity_frame["value"]))
        except Exception as exc:
            raise MarketDataSyncError(f"AKShare 获取市场宽度失败：{exc}") from exc

        required_items = {
            "统计日期",
            "上涨",
            "下跌",
            "平盘",
            "停牌",
            "涨停",
            "跌停",
            "活跃度",
        }
        if not required_items.issubset(activity):
            raise MarketDataSyncError("市场宽度接口返回字段不完整")

        observed_timestamp = pd.Timestamp(str(activity["统计日期"]))
        if observed_timestamp.tzinfo is None:
            observed_timestamp = observed_timestamp.tz_localize("Asia/Shanghai")
        observed_at = observed_timestamp.to_pydatetime()
        data_date = observed_at.date()

        # 东方财富偶尔会被网络代理中断。这里保留它为首选，同时使用中证指数官网
        # 作为同口径备用源；这属于数据源容错，不改变后面的市场活跃度算法。
        amounts, turnover_source, turnover_source_url = self._fetch_daily_amounts(
            ak=ak,
            pd=pd,
            data_date=data_date,
        )

        return MarketSnapshot(
            data_date=data_date,
            observed_at=observed_at,
            advancing_count=self._integer(activity["上涨"], "上涨"),
            declining_count=self._integer(activity["下跌"], "下跌"),
            flat_count=self._integer(activity["平盘"], "平盘"),
            suspended_count=self._integer(activity["停牌"], "停牌"),
            limit_up_count=self._integer(activity["涨停"], "涨停"),
            limit_down_count=self._integer(activity["跌停"], "跌停"),
            market_activity_pct=self._percentage(activity["活跃度"]),
            daily_amounts=amounts,
            turnover_source=turnover_source,
            turnover_source_url=turnover_source_url,
        )

    def _fetch_daily_amounts(
        self,
        *,
        ak: Any,
        pd: Any,
        data_date: date,
    ) -> tuple[tuple[MarketAmountPoint, ...], str, str]:
        """按优先级尝试两个沪深300成交额接口，并统一成“人民币元”。"""

        start_date = (data_date - timedelta(days=20)).strftime("%Y%m%d")
        end_date = data_date.strftime("%Y%m%d")
        errors: list[str] = []

        try:
            eastmoney_frame = ak.stock_zh_index_daily_em(
                symbol="sh000300",
                start_date=start_date,
                end_date=end_date,
            )
            amounts = self._normalize_amounts(
                frame=eastmoney_frame,
                pd=pd,
                date_column="date",
                amount_column="amount",
                amount_multiplier=1.0,
                expected_date=data_date,
            )
            return amounts, EASTMONEY_TURNOVER_SOURCE, EASTMONEY_TURNOVER_URL
        except Exception as exc:
            errors.append(f"东方财富：{exc}")

        try:
            csindex_frame = ak.stock_zh_index_hist_csindex(
                symbol="000300",
                start_date=start_date,
                end_date=end_date,
            )
            amounts = self._normalize_amounts(
                frame=csindex_frame,
                pd=pd,
                date_column="日期",
                amount_column="成交金额",
                # 中证接口的成交金额单位是亿元，数据库统一保存为人民币元。
                amount_multiplier=100_000_000.0,
                expected_date=data_date,
            )
            return amounts, CSINDEX_TURNOVER_SOURCE, CSINDEX_TURNOVER_URL
        except Exception as exc:
            errors.append(f"中证指数：{exc}")

        raise MarketDataSyncError(
            "AKShare 获取沪深300成交额失败；" + "；".join(errors)
        )

    @staticmethod
    def _normalize_amounts(
        *,
        frame: Any,
        pd: Any,
        date_column: str,
        amount_column: str,
        amount_multiplier: float,
        expected_date: date,
    ) -> tuple[MarketAmountPoint, ...]:
        """校验第三方 DataFrame，并转换成应用内部稳定结构。"""

        if not {date_column, amount_column}.issubset(frame.columns):
            raise ValueError("指数接口返回字段不完整")
        amount_frame = frame.tail(5)
        if len(amount_frame) < 5:
            raise ValueError("沪深300成交额不足 5 个交易日")

        amounts = tuple(
            MarketAmountPoint(
                data_date=pd.Timestamp(row[date_column]).date(),
                amount_cny=float(row[amount_column]) * amount_multiplier,
            )
            for _, row in amount_frame.iterrows()
        )
        # 市场宽度和成交额必须属于同一交易日，否则综合标签会混合两个截面。
        if amounts[-1].data_date != expected_date:
            raise ValueError(
                "市场宽度日期与沪深300最新成交额日期不一致："
                f"{expected_date} / {amounts[-1].data_date}"
            )
        if any(
            not math.isfinite(item.amount_cny) or item.amount_cny <= 0
            for item in amounts
        ):
            raise ValueError("沪深300成交额包含非法值")
        return amounts

    @staticmethod
    def _integer(value: object, field_name: str) -> int:
        try:
            result = int(float(str(value).replace(",", "")))
        except ValueError as exc:
            raise MarketDataSyncError(f"市场宽度字段 {field_name} 不是数字") from exc
        if result < 0:
            raise MarketDataSyncError(f"市场宽度字段 {field_name} 不能小于 0")
        return result

    @staticmethod
    def _percentage(value: object) -> float:
        try:
            result = float(str(value).replace("%", "").strip())
        except ValueError as exc:
            raise MarketDataSyncError("市场活跃度不是合法百分比") from exc
        if not math.isfinite(result) or result < 0:
            raise MarketDataSyncError("市场活跃度包含非法值")
        return result


class MarketSnapshotRepository:
    """market_snapshots 表的 SQLAlchemy Repository。"""

    def __init__(self, database: Database = runtime_database) -> None:
        self.database = database
        self.database.create_schema()

    def upsert(self, *, snapshot: MarketSnapshot, fetched_at: datetime) -> str:
        """插入或更新同一交易日快照，返回 inserted/updated。"""

        with self.database.session() as session, session.begin():
            model = session.get(MarketSnapshotModel, snapshot.data_date)
            action = "updated" if model is not None else "inserted"
            if model is None:
                model = MarketSnapshotModel(data_date=snapshot.data_date)
                session.add(model)

            model.advancing_count = snapshot.advancing_count
            model.declining_count = snapshot.declining_count
            model.flat_count = snapshot.flat_count
            model.suspended_count = snapshot.suspended_count
            model.limit_up_count = snapshot.limit_up_count
            model.limit_down_count = snapshot.limit_down_count
            model.market_activity_pct = snapshot.market_activity_pct
            model.observed_at = snapshot.observed_at
            model.breadth_source = "LeGuLeGu via AKShare"
            model.breadth_source_url = "https://legulegu.com/stockdata/market-activity"
            model.benchmark_code = "000300"
            model.benchmark_name = "沪深300指数"
            model.daily_amounts = [
                {
                    "date": point.data_date.isoformat(),
                    "amount_cny": round(point.amount_cny, 2),
                }
                for point in snapshot.daily_amounts
            ]
            model.turnover_source = snapshot.turnover_source
            model.turnover_source_url = snapshot.turnover_source_url
            model.fetched_at = fetched_at
        return action

    def latest(self) -> dict[str, Any] | None:
        """返回最新数据库快照，并转换成原有 MarketSentiment 输入结构。"""

        statement = select(MarketSnapshotModel).order_by(
            MarketSnapshotModel.data_date.desc()
        ).limit(1)
        with self.database.session() as session:
            model = session.scalar(statement)
            if model is None:
                return None
            return {
                "data_date": model.data_date.isoformat(),
                "broad_market_proxy": {
                    "fund_code": "110020",
                    "name": "易方达沪深300ETF联接A",
                    "tracking_target": "沪深300指数",
                    "source": "AKShare / Eastmoney Fund",
                    "source_url": "https://fundf10.eastmoney.com/jbgk_110020.html",
                },
                "breadth": {
                    "advancing_count": model.advancing_count,
                    "declining_count": model.declining_count,
                    "flat_count": model.flat_count,
                    "suspended_count": model.suspended_count,
                    "limit_up_count": model.limit_up_count,
                    "limit_down_count": model.limit_down_count,
                    "market_activity_pct": model.market_activity_pct,
                    "observed_at": self._as_utc(model.observed_at).isoformat(),
                    "source": model.breadth_source,
                    "source_url": model.breadth_source_url,
                },
                "turnover_proxy": {
                    "benchmark_code": model.benchmark_code,
                    "benchmark_name": model.benchmark_name,
                    "source": model.turnover_source,
                    "source_url": model.turnover_source_url,
                    "daily_amounts": list(model.daily_amounts or []),
                },
                "_data_mode": "database",
                "_data_updated_at": self._as_utc(model.fetched_at).isoformat(),
            }

    def status(self) -> dict[str, Any]:
        """返回数据库市场快照数量、覆盖日期和最近同步时间。"""

        statement = select(
            func.count(MarketSnapshotModel.data_date),
            func.min(MarketSnapshotModel.data_date),
            func.max(MarketSnapshotModel.data_date),
            func.max(MarketSnapshotModel.fetched_at),
        )
        with self.database.session() as session:
            record_count, snapshot_start, snapshot_end, fetched_at = session.execute(
                statement
            ).one()
        return {
            "record_count": int(record_count or 0),
            "snapshot_start": snapshot_start.isoformat() if snapshot_start else None,
            "snapshot_end": snapshot_end.isoformat() if snapshot_end else None,
            "last_successful_sync_at": (
                self._as_utc(fetched_at).isoformat() if fetched_at else None
            ),
        }

    @staticmethod
    def _as_utc(value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)


class MarketSnapshotSyncService:
    """编排一次市场抓取、落库和每日 Cron 注册。"""

    def __init__(
        self,
        *,
        repository: MarketSnapshotRepository,
        provider: MarketSnapshotProvider | None = None,
        index_sync_service: MarketIndexSyncService | None = None,
    ) -> None:
        self.repository = repository
        self.provider = provider or AkshareMarketSnapshotProvider()
        # 测试实例默认不注入指数服务；产品全局实例在文件末尾把两条采集链路组合起来。
        self.index_sync_service = index_sync_service

    def sync(self) -> dict[str, Any]:
        """分别同步总览与宽基指数；单一外部源失败时保留另一条成功结果。"""

        fetched_at = datetime.now(timezone.utc)
        snapshot: MarketSnapshot | None = None
        action: str | None = None
        index_result: dict[str, Any] | None = None
        warnings: list[str] = []

        # 两类数据来自独立接口。某个来源临时延迟时，不能丢掉另一类已经获取成功的事实。
        try:
            snapshot = self.provider.fetch_snapshot()
            action = self.repository.upsert(
                snapshot=snapshot,
                fetched_at=fetched_at,
            )
        except MarketDataSyncError as exc:
            warnings.append(f"全市场总览沿用上一份快照：{exc}")

        if self.index_sync_service is not None:
            try:
                index_result = self.index_sync_service.sync()
            except MarketIndexError as exc:
                warnings.append(f"宽基指数分区沿用上一批行情：{exc}")

        # 两条链路都没有写入新数据时才让 Background Job 失败，避免伪造“成功”。
        if snapshot is None and index_result is None:
            raise MarketDataSyncError("；".join(warnings) or "市场数据同步失败")

        summary_parts: list[str] = []
        if snapshot is not None:
            summary_parts.append(
                f"市场总览已同步至 {snapshot.data_date.isoformat()}"
            )
        if index_result is not None:
            summary_parts.append(
                f"{index_result['index_count']} 个宽基指数已同步至 "
                f"{index_result['data_date']}"
            )
        summary = "；".join(summary_parts) + "。"
        if warnings:
            summary += "部分来源暂不可用，未覆盖旧数据。"

        return {
            "summary": summary,
            "workflow": "market_snapshot_sync",
            "status": "partial" if warnings else "complete",
            "source": (
                f"LeGuLeGu via AKShare + {snapshot.turnover_source}"
                if snapshot is not None
                else index_result["source"]
            ),
            "data_date": (
                snapshot.data_date.isoformat()
                if snapshot is not None
                else index_result["data_date"]
            ),
            "write_action": action,
            "fetched_at": fetched_at.isoformat(),
            "index_sync": index_result,
            "warnings": warnings,
        }

    def status(self) -> dict[str, Any]:
        database_status = self.repository.status()
        index_status = (
            self.index_sync_service.status()
            if self.index_sync_service is not None
            else {
                "index_bar_count": 0,
                "index_count": 0,
                "index_snapshot_start": None,
                "index_snapshot_end": None,
                "index_last_successful_sync_at": None,
            }
        )
        return {
            "ok": True,
            "data_mode": "database" if database_status["record_count"] else "fixture",
            "source": self.provider.source,
            "sync_enabled": settings.market_snapshot_sync_enabled,
            "sync_cron": settings.market_snapshot_sync_cron,
            "schedule_id": MARKET_SYNC_SCHEDULE_ID,
            **database_status,
            **index_status,
        }

    def ensure_daily_schedule(self, scheduler: Any) -> dict[str, Any]:
        """启动时确保系统用户只有一条市场同步 Cron。"""

        current = scheduler.list_schedules(user_id=MARKET_SYNC_SYSTEM_USER_ID)
        existing = next(
            (
                item
                for item in current["schedules"]
                if item["id"] == MARKET_SYNC_SCHEDULE_ID
            ),
            None,
        )
        if existing is not None:
            return {"ok": True, "created": False, "schedule": existing}

        result = scheduler.create_schedule(
            user_id=MARKET_SYNC_SYSTEM_USER_ID,
            schedule_id=MARKET_SYNC_SCHEDULE_ID,
            cron=settings.market_snapshot_sync_cron,
            prompt="同步 FundScope 每日市场宽度、成交活跃度和配置的宽基指数",
            job_type="market_snapshot_sync",
            recurring=True,
            durable=True,
        )
        return {"ok": True, "created": True, "schedule": result["schedule"]}


market_snapshot_repository = MarketSnapshotRepository(runtime_database)
market_snapshot_sync_service = MarketSnapshotSyncService(
    repository=market_snapshot_repository,
    index_sync_service=market_index_sync_service,
)
