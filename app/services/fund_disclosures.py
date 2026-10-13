"""基金底层证券持仓和行业配置的采集、持久化与查询服务。

这条数据链路没有大模型参与：

1. Provider 通过 AKShare 读取东方财富定期报告；
2. Repository 按基金、报告期和证券/行业写入 SQL；
3. Query Service 优先读取数据库，数据库没有数据时使用冻结 fixture；
4. 组合穿透服务只消费这里已经规范化的百分比，不直接依赖网页字段。

基金持仓是定期披露数据，不是盘中实时仓位。所有公开结果都会携带报告日期、抓取时间
和新鲜度，避免 Agent 把旧季报描述成“当前实时持仓”。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
import json
import math
from pathlib import Path
import re
from typing import Any, Callable, Protocol

from sqlalchemy import delete, func, select

from app.database import Database, runtime_database
from app.models import (
    FundDisclosureSyncModel,
    FundIndustryAllocationModel,
    FundSecurityHoldingModel,
)
from app.services.fund_catalog import (
    FundCatalogRepository,
    fund_catalog_repository,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DISCLOSURE_FIXTURE = (
    PROJECT_ROOT / "data" / "fund_fixtures" / "fund_disclosures.json"
)
DISCLOSURE_SOURCE = "AKShare / Eastmoney Fund Disclosures"
FUND_CODE_PATTERN = re.compile(r"^\d{6}$")
QUARTER_PATTERN = re.compile(r"(?P<year>\d{4})年(?P<quarter>[1-4])季度")

# 季报通常不会像净值一样每日更新。150 天内视为正常，超过 240 天才明确标记过期。
FRESH_REPORT_DAYS = 150
AGING_REPORT_DAYS = 240


class FundDisclosureError(RuntimeError):
    """外部披露抓取、字段清洗或 SQL 写入失败。"""


@dataclass(frozen=True)
class FundSecurityHolding:
    """Provider 与 Repository 之间使用的一条规范化证券持仓。"""

    fund_code: str
    report_date: date
    asset_type: str
    security_code: str
    security_name: str
    weight_pct: float
    market_value_10k: float | None
    rank: int
    source_url: str


@dataclass(frozen=True)
class FundIndustryAllocation:
    """一条规范化股票行业配置。"""

    fund_code: str
    report_date: date
    industry_name: str
    weight_pct: float
    market_value_10k: float | None
    rank: int
    source_url: str


@dataclass(frozen=True)
class FundDisclosureBatch:
    """一次基金披露抓取的完整输出，也记录合法的空数据和上游提示。"""

    fund_code: str
    security_holdings: tuple[FundSecurityHolding, ...]
    industry_allocations: tuple[FundIndustryAllocation, ...]
    stock_report_date: date | None
    bond_report_date: date | None
    industry_report_date: date | None
    warnings: tuple[str, ...] = ()


class FundDisclosureProvider(Protocol):
    """同步层依赖的最小 Provider 接口，单元测试可注入 Fake。"""

    source: str

    def fetch(
        self,
        *,
        fund_code: str,
        as_of: date | None = None,
    ) -> FundDisclosureBatch: ...


class AkshareFundDisclosureProvider:
    """读取东方财富最新可用的股票、债券和行业披露。"""

    source = DISCLOSURE_SOURCE

    def fetch(
        self,
        *,
        fund_code: str,
        as_of: date | None = None,
    ) -> FundDisclosureBatch:
        """分别请求三类披露，并只保留各自最新报告期。

        股票基金可能没有债券，债券基金也可能没有行业配置，所以单个空表不是错误。
        只有三个上游接口全部调用失败时，才把本次同步判为失败。
        """

        try:
            import akshare as ak
        except ModuleNotFoundError as exc:
            raise FundDisclosureError(
                "请先安装 requirements.txt 中的 akshare"
            ) from exc

        code = self._normalize_code(fund_code)
        cutoff = as_of or date.today()
        warnings: list[str] = []
        successful_requests = 0

        stock_frame, stock_warning = self._fetch_latest_year(
            loader=lambda year: ak.fund_portfolio_hold_em(
                symbol=code,
                date=year,
            ),
            as_of=cutoff,
            label="股票持仓",
        )
        if stock_frame is not None:
            successful_requests += 1
        if stock_warning:
            warnings.append(stock_warning)

        bond_frame, bond_warning = self._fetch_latest_year(
            loader=lambda year: ak.fund_portfolio_bond_hold_em(
                symbol=code,
                date=year,
            ),
            as_of=cutoff,
            label="债券持仓",
        )
        if bond_frame is not None:
            successful_requests += 1
        if bond_warning:
            warnings.append(bond_warning)

        industry_frame, industry_warning = self._fetch_latest_year(
            loader=lambda year: ak.fund_portfolio_industry_allocation_em(
                symbol=code,
                date=year,
            ),
            as_of=cutoff,
            label="行业配置",
        )
        if industry_frame is not None:
            successful_requests += 1
        if industry_warning:
            warnings.append(industry_warning)

        if successful_requests == 0:
            raise FundDisclosureError(
                f"基金 {code} 的股票、债券和行业披露接口均不可用"
            )

        stocks, stock_date = self._security_rows(
            frame=stock_frame,
            fund_code=code,
            asset_type="stock",
            code_column="股票代码",
            name_column="股票名称",
            market_value_column="持仓市值",
            source_url=f"https://fundf10.eastmoney.com/ccmx_{code}.html",
        )
        bonds, bond_date = self._security_rows(
            frame=bond_frame,
            fund_code=code,
            asset_type="bond",
            code_column="债券代码",
            name_column="债券名称",
            market_value_column="持仓市值",
            source_url=f"https://fundf10.eastmoney.com/ccmx1_{code}.html",
        )
        industries, industry_date = self._industry_rows(
            frame=industry_frame,
            fund_code=code,
            source_url=f"https://fundf10.eastmoney.com/hytz_{code}.html",
        )

        return FundDisclosureBatch(
            fund_code=code,
            security_holdings=tuple([*stocks, *bonds]),
            industry_allocations=tuple(industries),
            stock_report_date=stock_date,
            bond_report_date=bond_date,
            industry_report_date=industry_date,
            warnings=tuple(warnings),
        )

    @staticmethod
    def _fetch_latest_year(
        *,
        loader: Callable[[str], Any],
        as_of: date,
        label: str,
    ) -> tuple[Any | None, str | None]:
        """先查当前年份，再查上一年；空表允许回退，网络错误留作提示。"""

        errors: list[str] = []
        for year in (as_of.year, as_of.year - 1):
            try:
                frame = loader(str(year))
            except Exception as exc:  # AKShare 会包装多种 requests/pandas 异常。
                errors.append(str(exc))
                continue
            if frame is not None and not frame.empty:
                return frame, None
            # 请求成功但没有该资产，仍然继续看上一年，便于识别刚转型的基金。
            if frame is not None and year == as_of.year - 1:
                return frame, f"{label}最近两个年份均未返回明细"

        if errors:
            return None, f"{label}读取失败：{errors[-1][:160]}"
        return None, f"{label}未返回可用数据"

    @classmethod
    def _security_rows(
        cls,
        *,
        frame: Any | None,
        fund_code: str,
        asset_type: str,
        code_column: str,
        name_column: str,
        market_value_column: str,
        source_url: str,
    ) -> tuple[list[FundSecurityHolding], date | None]:
        """把 AKShare 的季度中文标签转换为稳定日期和百分比字段。"""

        if frame is None or frame.empty:
            return [], None
        required = {code_column, name_column, "占净值比例", "季度"}
        if not required.issubset(frame.columns):
            raise FundDisclosureError(
                f"基金 {fund_code} 的{asset_type}持仓字段不完整"
            )

        parsed: list[tuple[date, dict[str, Any]]] = []
        for raw in frame.to_dict(orient="records"):
            report_date = cls._quarter_end(raw.get("季度"))
            if report_date is not None:
                parsed.append((report_date, raw))
        if not parsed:
            return [], None

        latest_date = max(item[0] for item in parsed)
        latest_rows = [raw for report_date, raw in parsed if report_date == latest_date]
        results: list[FundSecurityHolding] = []
        seen_codes: set[str] = set()
        for raw in latest_rows:
            security_code = str(raw.get(code_column) or "").strip()
            security_name = str(raw.get(name_column) or "").strip()
            weight = cls._finite_number(raw.get("占净值比例"))
            if not security_code or not security_name or weight is None or weight <= 0:
                continue
            if security_code in seen_codes:
                continue
            seen_codes.add(security_code)
            results.append(
                FundSecurityHolding(
                    fund_code=fund_code,
                    report_date=latest_date,
                    asset_type=asset_type,
                    security_code=security_code,
                    security_name=security_name,
                    weight_pct=round(weight, 6),
                    market_value_10k=cls._finite_number(
                        raw.get(market_value_column)
                    ),
                    rank=len(results) + 1,
                    source_url=source_url,
                )
            )
        return results, latest_date

    @classmethod
    def _industry_rows(
        cls,
        *,
        frame: Any | None,
        fund_code: str,
        source_url: str,
    ) -> tuple[list[FundIndustryAllocation], date | None]:
        """行业接口直接给截止日期；零权重行不进入数据库和后续计算。"""

        if frame is None or frame.empty:
            return [], None
        required = {"行业类别", "占净值比例", "截止时间"}
        if not required.issubset(frame.columns):
            raise FundDisclosureError(f"基金 {fund_code} 的行业配置字段不完整")

        parsed: list[tuple[date, dict[str, Any]]] = []
        for raw in frame.to_dict(orient="records"):
            report_date = cls._date_value(raw.get("截止时间"))
            if report_date is not None:
                parsed.append((report_date, raw))
        if not parsed:
            return [], None

        latest_date = max(item[0] for item in parsed)
        latest_rows = [raw for report_date, raw in parsed if report_date == latest_date]
        results: list[FundIndustryAllocation] = []
        seen_names: set[str] = set()
        for raw in latest_rows:
            industry_name = str(raw.get("行业类别") or "").strip()
            weight = cls._finite_number(raw.get("占净值比例"))
            if not industry_name or weight is None or weight <= 0:
                continue
            if industry_name in seen_names:
                continue
            seen_names.add(industry_name)
            results.append(
                FundIndustryAllocation(
                    fund_code=fund_code,
                    report_date=latest_date,
                    industry_name=industry_name,
                    weight_pct=round(weight, 6),
                    market_value_10k=cls._finite_number(raw.get("市值")),
                    rank=len(results) + 1,
                    source_url=source_url,
                )
            )
        return results, latest_date

    @staticmethod
    def _quarter_end(value: Any) -> date | None:
        match = QUARTER_PATTERN.search(str(value or ""))
        if not match:
            return None
        year = int(match.group("year"))
        quarter = int(match.group("quarter"))
        month_day = {1: (3, 31), 2: (6, 30), 3: (9, 30), 4: (12, 31)}
        month, day = month_day[quarter]
        return date(year, month, day)

    @staticmethod
    def _date_value(value: Any) -> date | None:
        if isinstance(value, datetime):
            return value.date()
        if isinstance(value, date):
            return value
        text = str(value or "").strip()[:10]
        try:
            return date.fromisoformat(text)
        except ValueError:
            return None

    @staticmethod
    def _finite_number(value: Any) -> float | None:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        return number if math.isfinite(number) else None

    @staticmethod
    def _normalize_code(value: str) -> str:
        code = str(value or "").strip()
        if not FUND_CODE_PATTERN.fullmatch(code):
            raise FundDisclosureError("fund_code 必须是 6 位数字")
        return code


class FundDisclosureRepository:
    """证券持仓、行业配置和同步元数据的 SQLAlchemy Repository。"""

    def __init__(self, database: Database = runtime_database) -> None:
        self.database = database
        self.database.create_schema()

    def replace_latest(
        self,
        *,
        batch: FundDisclosureBatch,
        source: str,
        fetched_at: datetime,
    ) -> dict[str, int]:
        """替换本次报告期的明细，同时保留更早报告期用于未来比较。"""

        with self.database.session() as session, session.begin():
            # 同一期重新同步时先删后写；若上游删除了一条持仓，数据库也不会留下旧行。
            for asset_type, report_date in (
                ("stock", batch.stock_report_date),
                ("bond", batch.bond_report_date),
            ):
                if report_date is not None:
                    session.execute(
                        delete(FundSecurityHoldingModel).where(
                            FundSecurityHoldingModel.fund_code == batch.fund_code,
                            FundSecurityHoldingModel.asset_type == asset_type,
                            FundSecurityHoldingModel.report_date == report_date,
                        )
                    )
            if batch.industry_report_date is not None:
                session.execute(
                    delete(FundIndustryAllocationModel).where(
                        FundIndustryAllocationModel.fund_code == batch.fund_code,
                        FundIndustryAllocationModel.report_date
                        == batch.industry_report_date,
                    )
                )

            session.add_all(
                [
                    FundSecurityHoldingModel(
                        fund_code=item.fund_code,
                        report_date=item.report_date,
                        asset_type=item.asset_type,
                        security_code=item.security_code,
                        security_name=item.security_name,
                        weight_pct=item.weight_pct,
                        market_value_10k=item.market_value_10k,
                        rank=item.rank,
                        source=source,
                        source_url=item.source_url,
                        fetched_at=fetched_at,
                    )
                    for item in batch.security_holdings
                ]
            )
            session.add_all(
                [
                    FundIndustryAllocationModel(
                        fund_code=item.fund_code,
                        report_date=item.report_date,
                        industry_name=item.industry_name,
                        weight_pct=item.weight_pct,
                        market_value_10k=item.market_value_10k,
                        rank=item.rank,
                        source=source,
                        source_url=item.source_url,
                        fetched_at=fetched_at,
                    )
                    for item in batch.industry_allocations
                ]
            )

            metadata = session.get(FundDisclosureSyncModel, batch.fund_code)
            if metadata is None:
                metadata = FundDisclosureSyncModel(
                    fund_code=batch.fund_code,
                    stock_report_date=batch.stock_report_date,
                    bond_report_date=batch.bond_report_date,
                    industry_report_date=batch.industry_report_date,
                    source=source,
                    source_url=(
                        f"https://fundf10.eastmoney.com/ccmx_{batch.fund_code}.html"
                    ),
                    warnings=list(batch.warnings),
                    fetched_at=fetched_at,
                )
                session.add(metadata)
            else:
                metadata.stock_report_date = batch.stock_report_date
                metadata.bond_report_date = batch.bond_report_date
                metadata.industry_report_date = batch.industry_report_date
                metadata.source = source
                metadata.source_url = (
                    f"https://fundf10.eastmoney.com/ccmx_{batch.fund_code}.html"
                )
                metadata.warnings = list(batch.warnings)
                metadata.fetched_at = fetched_at

        return {
            "security_count": len(batch.security_holdings),
            "industry_count": len(batch.industry_allocations),
        }

    def latest(self, *, fund_code: str) -> dict[str, Any] | None:
        """读取元数据指定的最新报告期，合法空资产不会回退到旧季度。"""

        with self.database.session() as session:
            metadata = session.get(FundDisclosureSyncModel, fund_code)
            if metadata is None:
                return None

            securities: list[FundSecurityHoldingModel] = []
            for asset_type, report_date in (
                ("stock", metadata.stock_report_date),
                ("bond", metadata.bond_report_date),
            ):
                if report_date is None:
                    continue
                securities.extend(
                    session.scalars(
                        select(FundSecurityHoldingModel)
                        .where(
                            FundSecurityHoldingModel.fund_code == fund_code,
                            FundSecurityHoldingModel.asset_type == asset_type,
                            FundSecurityHoldingModel.report_date == report_date,
                        )
                        .order_by(FundSecurityHoldingModel.rank.asc())
                    ).all()
                )

            industries: list[FundIndustryAllocationModel] = []
            if metadata.industry_report_date is not None:
                industries = list(
                    session.scalars(
                        select(FundIndustryAllocationModel)
                        .where(
                            FundIndustryAllocationModel.fund_code == fund_code,
                            FundIndustryAllocationModel.report_date
                            == metadata.industry_report_date,
                        )
                        .order_by(FundIndustryAllocationModel.rank.asc())
                    )
                )

            return {
                "fund_code": fund_code,
                "data_mode": "database",
                "source": metadata.source,
                "source_url": metadata.source_url,
                "fetched_at": self._as_utc(metadata.fetched_at).isoformat(),
                "report_dates": {
                    "stock": self._date_text(metadata.stock_report_date),
                    "bond": self._date_text(metadata.bond_report_date),
                    "industry": self._date_text(metadata.industry_report_date),
                },
                "warnings": list(metadata.warnings or []),
                "security_holdings": [
                    {
                        "report_date": item.report_date.isoformat(),
                        "asset_type": item.asset_type,
                        "security_code": item.security_code,
                        "security_name": item.security_name,
                        "weight_pct": item.weight_pct,
                        "market_value_10k": item.market_value_10k,
                        "rank": item.rank,
                        "source": item.source,
                        "source_url": item.source_url,
                    }
                    for item in securities
                ],
                "industry_allocations": [
                    {
                        "report_date": item.report_date.isoformat(),
                        "industry_name": item.industry_name,
                        "weight_pct": item.weight_pct,
                        "market_value_10k": item.market_value_10k,
                        "rank": item.rank,
                        "source": item.source,
                        "source_url": item.source_url,
                    }
                    for item in industries
                ],
            }

    def previous_report(
        self, *, fund_code: str, kind: str, before: str, source: str,
    ) -> dict[str, Any] | None:
        """查找同来源、同资产类别的上一份已保存披露，不联网补历史。

        数据库不保证每个季度都有记录，所以返回真实日期，不把它称为“上季度”。
        空报告没有独立历史元数据，因此这里仅能比较已保存的非空明细。
        """
        if kind not in {"stock", "bond", "industry"}:
            raise ValueError("不支持的披露类别")
        model = FundIndustryAllocationModel if kind == "industry" else FundSecurityHoldingModel
        conditions = [model.fund_code == fund_code, model.report_date < date.fromisoformat(before), model.source == source]
        if kind != "industry":
            conditions.append(model.asset_type == kind)
        with self.database.session() as session:
            previous_date = session.scalar(select(func.max(model.report_date)).where(*conditions))
            if previous_date is None:
                return None
            rows = session.scalars(select(model).where(*conditions, model.report_date == previous_date).order_by(model.rank)).all()
            return {
                "report_date": previous_date.isoformat(),
                "rows": [{
                    "key": item.industry_name if kind == "industry" else item.security_code,
                    "name": item.industry_name if kind == "industry" else item.security_name,
                    "weight_pct": item.weight_pct,
                    "source_url": item.source_url,
                } for item in rows],
            }

    def status(self) -> dict[str, Any]:
        """给运维和同步状态接口返回数据库覆盖范围。"""

        with self.database.session() as session:
            security_count = session.scalar(
                select(func.count()).select_from(FundSecurityHoldingModel)
            )
            industry_count = session.scalar(
                select(func.count()).select_from(FundIndustryAllocationModel)
            )
            fund_count, last_sync = session.execute(
                select(
                    func.count(FundDisclosureSyncModel.fund_code),
                    func.max(FundDisclosureSyncModel.fetched_at),
                )
            ).one()
            stock_latest, bond_latest, industry_latest = session.execute(
                select(
                    func.max(FundDisclosureSyncModel.stock_report_date),
                    func.max(FundDisclosureSyncModel.bond_report_date),
                    func.max(FundDisclosureSyncModel.industry_report_date),
                )
            ).one()

        dates = [
            item for item in (stock_latest, bond_latest, industry_latest) if item
        ]
        return {
            "record_count": int(security_count or 0) + int(industry_count or 0),
            "security_record_count": int(security_count or 0),
            "industry_record_count": int(industry_count or 0),
            "fund_count": int(fund_count or 0),
            "latest_report_date": max(dates).isoformat() if dates else None,
            "last_successful_sync_at": (
                self._as_utc(last_sync).isoformat() if last_sync else None
            ),
        }

    @staticmethod
    def _date_text(value: date | None) -> str | None:
        return value.isoformat() if value else None

    @staticmethod
    def _as_utc(value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)


class FundDisclosureService:
    """面向 API、组合计算和 Agent Tool 的统一只读入口。"""

    def __init__(
        self,
        *,
        repository: FundDisclosureRepository,
        fixture_path: Path = DEFAULT_DISCLOSURE_FIXTURE,
        today: Callable[[], date] = date.today,
    ) -> None:
        self.repository = repository
        self.fixture_path = Path(fixture_path)
        self.today = today
        self._fixture_cache: dict[str, dict[str, Any]] | None = None

    def get_fund(self, *, fund_code: str) -> dict[str, Any]:
        """数据库优先，未同步的样例基金再读取冻结披露。"""

        code = AkshareFundDisclosureProvider._normalize_code(fund_code)
        raw = self.repository.latest(fund_code=code)
        if raw is None:
            raw = self._fixtures().get(code)
        if raw is None:
            raw = {
                "fund_code": code,
                "data_mode": "unavailable",
                "source": DISCLOSURE_SOURCE,
                "source_url": f"https://fundf10.eastmoney.com/ccmx_{code}.html",
                "fetched_at": None,
                "report_dates": {"stock": None, "bond": None, "industry": None},
                "warnings": ["该基金尚未同步持仓披露"],
                "security_holdings": [],
                "industry_allocations": [],
            }
        return self._decorate(raw)

    def list_for_funds(self, fund_codes: list[str]) -> dict[str, dict[str, Any]]:
        """组合最多十只基金，逐只读取可保持接口清楚且足够轻量。"""

        return {
            code: self.get_fund(fund_code=code)
            for code in dict.fromkeys(fund_codes)
        }

    def status(self) -> dict[str, Any]:
        """数据库无数据时也显示 fixture 覆盖，便于离线演示。"""

        database_status = self.repository.status()
        fixtures = self._fixtures()
        if database_status["record_count"]:
            return {"data_mode": "database", **database_status}
        fixture_records = sum(
            len(item.get("security_holdings", []))
            + len(item.get("industry_allocations", []))
            for item in fixtures.values()
        )
        report_dates = [
            value
            for item in fixtures.values()
            for value in item.get("report_dates", {}).values()
            if value
        ]
        return {
            "data_mode": "fixture" if fixtures else "unavailable",
            "record_count": fixture_records,
            "security_record_count": sum(
                len(item.get("security_holdings", [])) for item in fixtures.values()
            ),
            "industry_record_count": sum(
                len(item.get("industry_allocations", []))
                for item in fixtures.values()
            ),
            "fund_count": len(fixtures),
            "latest_report_date": max(report_dates) if report_dates else None,
            "last_successful_sync_at": None,
        }

    def _decorate(self, raw: dict[str, Any]) -> dict[str, Any]:
        report_dates = raw.get("report_dates") or {}
        parsed_dates = [
            date.fromisoformat(value)
            for value in report_dates.values()
            if value
        ]
        age_days = (
            max(max(0, (self.today() - value).days) for value in parsed_dates)
            if parsed_dates
            else None
        )
        if age_days is None:
            freshness = "unavailable"
        elif age_days <= FRESH_REPORT_DAYS:
            freshness = "fresh"
        elif age_days <= AGING_REPORT_DAYS:
            freshness = "aging"
        else:
            freshness = "stale"

        securities = list(raw.get("security_holdings") or [])
        industries = list(raw.get("industry_allocations") or [])
        stock_weight = sum(
            float(item["weight_pct"])
            for item in securities
            if item["asset_type"] == "stock"
        )
        bond_weight = sum(
            float(item["weight_pct"])
            for item in securities
            if item["asset_type"] == "bond"
        )
        industry_weight = sum(float(item["weight_pct"]) for item in industries)
        return {
            **raw,
            "latest_report_date": max(parsed_dates).isoformat() if parsed_dates else None,
            "oldest_report_date": min(parsed_dates).isoformat() if parsed_dates else None,
            "report_age_days": age_days,
            "freshness_status": freshness,
            "security_count": len(securities),
            "industry_count": len(industries),
            "disclosed_stock_weight_pct": round(stock_weight, 4),
            "disclosed_bond_weight_pct": round(bond_weight, 4),
            "industry_coverage_pct": round(industry_weight, 4),
        }

    def _fixtures(self) -> dict[str, dict[str, Any]]:
        if self._fixture_cache is not None:
            return self._fixture_cache
        try:
            payload = json.loads(self.fixture_path.read_text(encoding="utf-8"))
            generated_at = payload.get("generated_at")
            source = payload.get("source", DISCLOSURE_SOURCE)
            self._fixture_cache = {
                str(item["fund_code"]): {
                    **item,
                    "data_mode": "fixture",
                    "source": source,
                    "fetched_at": generated_at,
                }
                for item in payload.get("funds", [])
            }
        except (OSError, KeyError, TypeError, json.JSONDecodeError):
            # 任意基金仍可通过在线同步补齐；缺 fixture 不应阻止应用启动。
            self._fixture_cache = {}
        return self._fixture_cache


class FundDisclosureSyncService:
    """编排多只已关注基金的披露抓取和 SQL 写入。"""

    def __init__(
        self,
        *,
        repository: FundDisclosureRepository,
        provider: FundDisclosureProvider | None = None,
        query_service: FundDisclosureService | None = None,
        catalog_repository: FundCatalogRepository | None = None,
    ) -> None:
        self.repository = repository
        self.provider = provider or AkshareFundDisclosureProvider()
        self.query_service = query_service
        self.catalog_repository = catalog_repository

    def sync(
        self,
        *,
        fund_codes: list[str],
        progress_callback: Callable[[int, int, str], None] | None = None,
    ) -> dict[str, Any]:
        """逐只同步；单只失败会记录，但至少一只成功即可返回部分结果。"""

        normalized_codes = list(
            dict.fromkeys(str(code or "").strip() for code in fund_codes)
        )
        invalid = [
            code for code in normalized_codes if not FUND_CODE_PATTERN.fullmatch(code)
        ]
        if invalid:
            raise FundDisclosureError(f"基金代码非法：{', '.join(invalid)}")

        fetched_at = datetime.now(timezone.utc)
        results: list[dict[str, Any]] = []
        failures: list[dict[str, str]] = []
        for index, fund_code in enumerate(normalized_codes, start=1):
            if progress_callback:
                progress_callback(index, len(normalized_codes), fund_code)
            try:
                batch = self.provider.fetch(fund_code=fund_code)
                write_result = self.repository.replace_latest(
                    batch=batch,
                    source=self.provider.source,
                    fetched_at=fetched_at,
                )
                results.append(
                    {
                        "fund_code": fund_code,
                        "report_dates": {
                            "stock": self._date_text(batch.stock_report_date),
                            "bond": self._date_text(batch.bond_report_date),
                            "industry": self._date_text(batch.industry_report_date),
                        },
                        "warnings": list(batch.warnings),
                        **write_result,
                    }
                )
            except Exception as exc:
                failures.append({"fund_code": fund_code, "error": str(exc)})

        if not results:
            details = "; ".join(
                f"{item['fund_code']}: {item['error']}" for item in failures
            )
            raise FundDisclosureError(f"所有基金披露同步均失败：{details}")
        return {
            "summary": (
                f"已同步 {len(results)}/{len(normalized_codes)} 只基金的最新持仓披露。"
            ),
            "workflow": "fund_disclosure_sync",
            "source": self.provider.source,
            "fetched_at": fetched_at.isoformat(),
            "successful_funds": len(results),
            "failed_funds": len(failures),
            "results": results,
            "failures": failures,
        }

    def status(self) -> dict[str, Any]:
        service = self.query_service or FundDisclosureService(
            repository=self.repository
        )
        return service.status()

    @staticmethod
    def _date_text(value: date | None) -> str | None:
        return value.isoformat() if value else None


fund_disclosure_repository = FundDisclosureRepository(runtime_database)
fund_disclosure_service = FundDisclosureService(
    repository=fund_disclosure_repository,
)
fund_disclosure_sync_service = FundDisclosureSyncService(
    repository=fund_disclosure_repository,
    query_service=fund_disclosure_service,
    catalog_repository=fund_catalog_repository,
)
