"""FundScope 基金净值的数据采集与数据库快照服务。

这条链路没有大模型参与：AKShare 负责访问东方财富数据，Python 负责字段清洗，
SQLAlchemy 负责持久化。Agent 之后只通过基金 Tool 读取这里已经落库的事实。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import json
import math
from pathlib import Path
import re
from typing import Any, Callable, Protocol

from sqlalchemy import func, select

from app.config import settings
from app.database import Database, runtime_database
from app.models import FundNavSnapshotModel
from app.services.fund_corporate_actions import (
    AkshareFundCorporateActionProvider,
    FundCorporateActionRepository,
    fund_corporate_action_provider,
    fund_corporate_action_repository,
)
from app.services.fund_benchmarks import (
    AkshareFundBenchmarkProvider,
    FundBenchmarkRepository,
    fund_benchmark_provider,
    fund_benchmark_repository,
)
from app.services.fund_catalog import (
    FundCatalogRepository,
    fund_catalog_repository,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_FIXTURE_DIR = PROJECT_ROOT / "data" / "fund_fixtures"
FUND_CODE_PATTERN = re.compile(r"^\d{6}$")

SYNC_SYSTEM_USER_ID = "fundscope-system"
SYNC_SCHEDULE_ID = "fund-nav-daily-sync"
SYNC_SOURCE = "AKShare / Eastmoney Fund"


class FundDataSyncError(RuntimeError):
    """外部采集、字段清洗或数据库写入无法完成。"""


@dataclass(frozen=True)
class FundNavPoint:
    """采集层和数据库层之间使用的稳定净值结构。"""

    fund_code: str
    nav_date: date
    unit_nav: float
    daily_growth_rate_pct: float | None


class FundNavProvider(Protocol):
    """规定外部数据提供者至少要实现什么方法。"""

    source: str

    def fetch_nav(
        self,
        *,
        fund_code: str,
        date_from: date,
    ) -> list[FundNavPoint]: ...


class AkshareFundNavProvider:
    """通过 AKShare 读取东方财富开放式基金历史净值。"""

    source = SYNC_SOURCE

    def fetch_nav(
        self,
        *,
        fund_code: str,
        date_from: date,
    ) -> list[FundNavPoint]:
        """抓取并清洗一只基金从指定日期开始的日净值。"""

        # 延迟导入很重要：普通 API 查询不需要加载 AKShare；只有真正执行同步 Job 时
        # 才承担依赖初始化和网络访问成本。
        try:
            import akshare as ak
            import pandas as pd
        except ModuleNotFoundError as exc:
            raise FundDataSyncError("请先安装 requirements.txt 中的 akshare") from exc

        try:
            frame = ak.fund_open_fund_info_em(
                symbol=fund_code,
                indicator="单位净值走势",
            )
        except Exception as exc:
            raise FundDataSyncError(
                f"AKShare 获取基金 {fund_code} 净值失败：{exc}"
            ) from exc

        required_columns = {"净值日期", "单位净值", "日增长率"}
        if frame.empty or not required_columns.issubset(frame.columns):
            raise FundDataSyncError(f"基金 {fund_code} 返回的净值字段不完整")

        points_by_date: dict[date, FundNavPoint] = {}
        for raw in frame.to_dict(orient="records"):
            try:
                nav_date = pd.to_datetime(raw["净值日期"]).date()
                unit_nav = float(raw["单位净值"])
            except (TypeError, ValueError) as exc:
                raise FundDataSyncError(
                    f"基金 {fund_code} 存在无法解析的净值记录"
                ) from exc

            if nav_date < date_from:
                continue
            if not math.isfinite(unit_nav) or unit_nav <= 0:
                raise FundDataSyncError(
                    f"基金 {fund_code} 在 {nav_date.isoformat()} 的单位净值非法"
                )

            growth_raw = raw.get("日增长率")
            growth = None
            if growth_raw is not None and not pd.isna(growth_raw):
                growth = float(growth_raw)
                if not math.isfinite(growth):
                    growth = None

            # 上游偶尔可能返回重复日期；同一天只保留最后一条规范化记录。
            points_by_date[nav_date] = FundNavPoint(
                fund_code=fund_code,
                nav_date=nav_date,
                unit_nav=unit_nav,
                daily_growth_rate_pct=growth,
            )

        points = sorted(points_by_date.values(), key=lambda item: item.nav_date)
        if not points:
            raise FundDataSyncError(
                f"基金 {fund_code} 在 {date_from.isoformat()} 之后没有可用净值"
            )
        return points


class FundNavSnapshotRepository:
    """基金净值表的 SQLAlchemy 访问层。"""

    def __init__(self, database: Database = runtime_database) -> None:
        self.database = database
        self.database.create_schema()

    def upsert_points(
        self,
        *,
        fund_code: str,
        points: list[FundNavPoint],
        source: str,
        source_url: str,
        fetched_at: datetime,
    ) -> dict[str, int]:
        """按基金代码和日期插入或更新，重复同步不会制造重复行。"""

        dates = [point.nav_date for point in points]
        with self.database.session() as session, session.begin():
            existing_rows = list(
                session.scalars(
                    select(FundNavSnapshotModel).where(
                        FundNavSnapshotModel.fund_code == fund_code,
                        FundNavSnapshotModel.nav_date.in_(dates),
                    )
                )
            )
            existing_by_date = {row.nav_date: row for row in existing_rows}
            inserted = 0
            updated = 0

            for point in points:
                model = existing_by_date.get(point.nav_date)
                if model is None:
                    session.add(
                        FundNavSnapshotModel(
                            fund_code=fund_code,
                            nav_date=point.nav_date,
                            unit_nav=point.unit_nav,
                            daily_growth_rate_pct=point.daily_growth_rate_pct,
                            source=source,
                            source_url=source_url,
                            fetched_at=fetched_at,
                        )
                    )
                    inserted += 1
                    continue

                model.unit_nav = point.unit_nav
                model.daily_growth_rate_pct = point.daily_growth_rate_pct
                model.source = source
                model.source_url = source_url
                model.fetched_at = fetched_at
                updated += 1

        return {"inserted": inserted, "updated": updated}

    def list_points(self, *, fund_code: str) -> list[dict[str, Any]]:
        """返回一只基金的数据库快照，供 Catalog 与本地 fixture 合并。"""

        statement = (
            select(FundNavSnapshotModel)
            .where(FundNavSnapshotModel.fund_code == fund_code)
            .order_by(FundNavSnapshotModel.nav_date.asc())
        )
        with self.database.session() as session:
            rows = list(session.scalars(statement))
            return [
                {
                    "nav_date": row.nav_date.isoformat(),
                    "unit_nav": row.unit_nav,
                    "daily_growth_rate_pct": row.daily_growth_rate_pct,
                    "source": row.source,
                    "source_url": row.source_url,
                    "fetched_at": self._as_utc(row.fetched_at).isoformat(),
                }
                for row in rows
            ]

    def status(self) -> dict[str, Any]:
        """汇总最近一次成功入库时间和当前数据库覆盖范围。"""

        statement = select(
            func.count(FundNavSnapshotModel.fund_code),
            func.count(func.distinct(FundNavSnapshotModel.fund_code)),
            func.min(FundNavSnapshotModel.nav_date),
            func.max(FundNavSnapshotModel.nav_date),
            func.max(FundNavSnapshotModel.fetched_at),
        )
        with self.database.session() as session:
            record_count, fund_count, snapshot_start, snapshot_end, fetched_at = (
                session.execute(statement).one()
            )

        return {
            "record_count": int(record_count or 0),
            "fund_count": int(fund_count or 0),
            "snapshot_start": snapshot_start.isoformat() if snapshot_start else None,
            "snapshot_end": snapshot_end.isoformat() if snapshot_end else None,
            "last_successful_sync_at": (
                self._as_utc(fetched_at).isoformat() if fetched_at else None
            ),
        }

    @staticmethod
    def _as_utc(value: datetime) -> datetime:
        # SQLite 可能丢失 timezone；API 层统一补成 UTC ISO 时间。
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)


class FundNavSyncService:
    """编排已关注基金的抓取、清洗和数据库写入。"""

    def __init__(
        self,
        *,
        repository: FundNavSnapshotRepository,
        provider: FundNavProvider | None = None,
        fixture_dir: Path = DEFAULT_FIXTURE_DIR,
        catalog_repository: FundCatalogRepository | None = None,
        disclosure_sync: Any | None = None,
        corporate_action_repository: FundCorporateActionRepository | None = None,
        corporate_action_provider: AkshareFundCorporateActionProvider | None = None,
        benchmark_repository: FundBenchmarkRepository | None = None,
        benchmark_provider: AkshareFundBenchmarkProvider | None = None,
    ) -> None:
        self.repository = repository
        self.provider = provider or AkshareFundNavProvider()
        self.fixture_dir = Path(fixture_dir)
        self.catalog_repository = catalog_repository
        # 公司行为与净值使用同一批次抓取。测试不传 Provider 时只验证原有净值链路，
        # 应用全局实例则显式传入 AKShare Provider，避免单元测试意外访问网络。
        self.corporate_action_repository = corporate_action_repository
        self.corporate_action_provider = corporate_action_provider
        # 业绩基准也是可选的同步子链路。测试不传时不会访问指数网站；全局实例会把
        # 已支持的基准公式同步成日收益，用于夏普之外的相对业绩比较。
        self.benchmark_repository = benchmark_repository
        self.benchmark_provider = benchmark_provider
        # 披露同步通过可注入依赖接入。单元测试不传时只测试净值；应用全局实例会同时
        # 更新底层持仓，避免 FundNavSyncService 反向导入具体实现形成循环依赖。
        self.disclosure_sync = disclosure_sync

    def sync(
        self,
        *,
        fund_codes: list[str] | None = None,
        progress_callback: Callable[[int, int, str], None] | None = None,
    ) -> dict[str, Any]:
        """同步指定基金；未指定时同步样例和用户已导入的全部基金。"""

        config = self._fixture_config()
        fixture_codes = [item["fund_code"] for item in config["funds"]]
        imported_codes = (
            self.catalog_repository.list_ready_codes()
            if self.catalog_repository is not None
            else []
        )
        selected_codes = fund_codes or [*fixture_codes, *imported_codes]
        normalized_codes = list(dict.fromkeys(str(code).strip() for code in selected_codes))
        unsupported = [
            code
            for code in normalized_codes
            if not FUND_CODE_PATTERN.fullmatch(code)
        ]
        if unsupported:
            raise FundDataSyncError(
                f"当前同步管线不支持基金：{', '.join(unsupported)}"
            )

        fetched_at = datetime.now(timezone.utc)
        results: list[dict[str, Any]] = []
        failures: list[dict[str, str]] = []
        profile_by_code = {
            str(profile["fund_code"]): dict(profile)
            for profile in config["funds"]
        }
        if self.catalog_repository is not None:
            for profile in self.catalog_repository.list_ready_profiles():
                if profile and profile.get("fund_code"):
                    profile_by_code[str(profile["fund_code"])] = dict(profile)
        if self.benchmark_provider is not None:
            self.benchmark_provider.begin_batch()

        for index, fund_code in enumerate(normalized_codes, start=1):
            if progress_callback:
                progress_callback(index, len(normalized_codes), fund_code)
            try:
                # 三只 fixture 继续补齐原有冻结区间；新导入基金默认保留近三年历史。
                date_from = (
                    date.fromisoformat(config["snapshot_start"])
                    if fund_code in fixture_codes
                    else date.today() - timedelta(days=366 * 3)
                )
                points = self.provider.fetch_nav(
                    fund_code=fund_code,
                    date_from=date_from,
                )
                source_url = f"https://fund.eastmoney.com/{fund_code}.html"

                # 先把净值和公司行为都抓取成功，再开始写库。这样公司行为接口失败时，
                # 不会出现“新净值已入库但复权依据仍停留在旧日期”的半完成状态。
                actions = None
                if (
                    self.corporate_action_repository is not None
                    and self.corporate_action_provider is not None
                ):
                    actions = self.corporate_action_provider.fetch_actions(
                        fund_code=fund_code,
                        date_from=date_from,
                        date_to=points[-1].nav_date,
                    )
                write_result = self.repository.upsert_points(
                    fund_code=fund_code,
                    points=points,
                    source=self.provider.source,
                    source_url=source_url,
                    fetched_at=fetched_at,
                )
                action_result: dict[str, Any] | None = None
                if actions is not None:
                    action_result = self.corporate_action_repository.replace_coverage(
                        fund_code=fund_code,
                        actions=actions,
                        coverage_start=date_from,
                        coverage_end=points[-1].nav_date,
                        source=self.corporate_action_provider.source,
                        source_url=source_url,
                        fetched_at=fetched_at,
                    )
                benchmark_result: dict[str, Any] | None = None
                if (
                    self.benchmark_repository is not None
                    and self.benchmark_provider is not None
                    and fund_code in profile_by_code
                ):
                    try:
                        benchmark_series = self.benchmark_provider.fetch_series(
                            profile=profile_by_code[fund_code],
                            date_from=date_from,
                            date_to=points[-1].nav_date,
                        )
                        benchmark_result = self.benchmark_repository.replace_series(
                            series=benchmark_series,
                            fetched_at=fetched_at,
                        )
                    except Exception as exc:
                        # 基准是风险分析的增强数据，不应让已经成功的基金净值整批回滚。
                        # 旧基准数据也不覆盖，下一次手动刷新可以继续重试。
                        benchmark_result = {
                            "benchmark_status": "failed",
                            "record_count": 0,
                            "error": str(exc),
                        }
                results.append(
                    {
                        "fund_code": fund_code,
                        "record_count": len(points),
                        "snapshot_start": points[0].nav_date.isoformat(),
                        "snapshot_end": points[-1].nav_date.isoformat(),
                        "corporate_actions": action_result,
                        "benchmark": benchmark_result,
                        **write_result,
                    }
                )
                # 每日同步只刷新已经 ready 的目录项；首次导入由 ImportService 在档案和
                # 净值都成功后统一从 importing 切换到 ready。
                if self.catalog_repository is not None:
                    cached = self.catalog_repository.get_many([fund_code]).get(
                        fund_code
                    )
                    if cached and cached["import_status"] == "ready":
                        self.catalog_repository.set_status(
                            fund_code=fund_code,
                            status="ready",
                            nav_fetched_at=fetched_at,
                        )
            except Exception as exc:
                failures.append({"fund_code": fund_code, "error": str(exc)})

        if not results:
            details = "; ".join(
                f"{item['fund_code']}: {item['error']}" for item in failures
            )
            raise FundDataSyncError(f"所有基金同步均失败：{details}")

        latest_date = max(item["snapshot_end"] for item in results)
        disclosure_result: dict[str, Any] | None = None
        if self.disclosure_sync is not None:
            try:
                # 季报接口和日净值共享同一次“刷新基金数据”操作，但仍保留独立结果，
                # 这样净值成功、披露暂时失败时不会把已经入库的净值回滚或隐藏。
                disclosure_result = self.disclosure_sync.sync(
                    fund_codes=normalized_codes,
                    progress_callback=progress_callback,
                )
            except Exception as exc:
                disclosure_result = {
                    "ok": False,
                    "workflow": "fund_disclosure_sync",
                    "error": str(exc),
                }

        summary = (
            f"已同步 {len(results)}/{len(normalized_codes)} 只基金，"
            f"数据库净值更新至 {latest_date}。"
        )
        if disclosure_result and disclosure_result.get("successful_funds"):
            summary += (
                f" 最新持仓披露同步成功 "
                f"{disclosure_result['successful_funds']} 只。"
            )
        adjusted_funds = sum(
            item.get("corporate_actions") is not None for item in results
        )
        if adjusted_funds:
            summary += f" 公司行为口径已核对 {adjusted_funds} 只。"
        benchmark_funds = sum(
            (item.get("benchmark") or {}).get("benchmark_status")
            in {"matched", "proxy"}
            for item in results
        )
        if benchmark_funds:
            summary += f" 业绩基准已匹配 {benchmark_funds} 只。"
        return {
            "summary": summary,
            "workflow": "fund_nav_sync",
            "source": self.provider.source,
            "fetched_at": fetched_at.isoformat(),
            "successful_funds": len(results),
            "failed_funds": len(failures),
            "results": results,
            "failures": failures,
            "disclosure_sync": disclosure_result,
        }

    def status(self) -> dict[str, Any]:
        """返回前端和运维接口需要的轻量数据管线状态。"""

        database_status = self.repository.status()
        return {
            "ok": True,
            "data_mode": (
                "database" if database_status["record_count"] else "fixture"
            ),
            "source": self.provider.source,
            "sync_enabled": settings.fund_nav_sync_enabled,
            "sync_cron": settings.fund_nav_sync_cron,
            "schedule_id": SYNC_SCHEDULE_ID,
            "disclosures": (
                self.disclosure_sync.status()
                if self.disclosure_sync is not None
                else None
            ),
            "corporate_actions": (
                self.corporate_action_repository.status()
                if self.corporate_action_repository is not None
                else None
            ),
            "benchmarks": (
                self.benchmark_repository.status()
                if self.benchmark_repository is not None
                else None
            ),
            **database_status,
        }

    def ensure_daily_schedule(self, scheduler: Any) -> dict[str, Any]:
        """启动时确保系统级每日同步 Cron 存在，重复启动不会重复创建。"""

        current = scheduler.list_schedules(user_id=SYNC_SYSTEM_USER_ID)
        existing = next(
            (
                item
                for item in current["schedules"]
                if item["id"] == SYNC_SCHEDULE_ID
            ),
            None,
        )
        if existing is not None:
            return {"ok": True, "created": False, "schedule": existing}

        result = scheduler.create_schedule(
            user_id=SYNC_SYSTEM_USER_ID,
            schedule_id=SYNC_SCHEDULE_ID,
            cron=settings.fund_nav_sync_cron,
            prompt="同步 FundScope 已关注基金每日单位净值",
            job_type="fund_nav_sync",
            recurring=True,
            durable=True,
        )
        return {"ok": True, "created": True, "schedule": result["schedule"]}

    def _fixture_config(self) -> dict[str, Any]:
        """读取样例基金代码和历史起点，避免在同步层重复硬编码。"""

        path = self.fixture_dir / "fund_profiles.json"
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if not payload.get("funds") or not payload.get("snapshot_start"):
                raise ValueError("缺少 funds 或 snapshot_start")
            return payload
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise FundDataSyncError("无法读取 FundScope 样例基金配置") from exc


# 放在模块末尾导入，使 fund_disclosures 可以复用 Catalog Repository，且不会在类定义
# 阶段互相初始化。两个同步服务仍然是独立对象，只由这里完成应用层组合。
from app.services.fund_disclosures import fund_disclosure_sync_service


fund_nav_snapshot_repository = FundNavSnapshotRepository(runtime_database)
fund_nav_sync_service = FundNavSyncService(
    repository=fund_nav_snapshot_repository,
    catalog_repository=fund_catalog_repository,
    disclosure_sync=fund_disclosure_sync_service,
    corporate_action_repository=fund_corporate_action_repository,
    corporate_action_provider=fund_corporate_action_provider,
    benchmark_repository=fund_benchmark_repository,
    benchmark_provider=fund_benchmark_provider,
)
