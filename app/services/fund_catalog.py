"""可搜索基金目录、主数据缓存和按需导入服务。

这一层把“发现基金”和“研究基金”分成两个阶段：

1. 搜索阶段只读取基金代码、名称和类型，响应快，结果会缓存到 SQL；
2. 导入阶段由 Background Job 补齐档案和近三年净值，成功后才进入工作台。

外部数据访问集中在 Provider，SQL 读写集中在 Repository，业务状态流转集中在
Service。以后替换数据供应商时，Router、前端和风险算法都不需要重写。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import re
from threading import Lock
import time
from typing import Any, Callable

from sqlalchemy import func, or_, select

from app.database import Database, runtime_database
from app.models import FundCatalogEntryModel


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_FIXTURE_DIR = PROJECT_ROOT / "data" / "fund_fixtures"
FUND_CODE_PATTERN = re.compile(r"^\d{6}$")
CATALOG_SOURCE = "AKShare / Eastmoney Fund Directory"
PROFILE_SOURCE = "AKShare / Snowball Fund Profile"
CATALOG_CACHE_SECONDS = 6 * 60 * 60


class FundCatalogProviderError(RuntimeError):
    """在线基金目录无法读取或返回了不符合约定的数据。"""


class FundCatalogImportError(RuntimeError):
    """一只基金的档案或净值没有完整导入。"""


@dataclass(frozen=True)
class FundDirectoryEntry:
    """Provider 对外返回的稳定基金目录结构。"""

    fund_code: str
    short_name: str
    fund_type: str
    pinyin_abbreviation: str = ""
    pinyin_name: str = ""

    @property
    def source_url(self) -> str:
        return f"https://fund.eastmoney.com/{self.fund_code}.html"


@dataclass(frozen=True)
class FundDirectorySearchBatch:
    """一次目录检索结果，并说明是否复用了进程内缓存。"""

    entries: list[FundDirectoryEntry]
    fetched_at: datetime
    cache_hit: bool


class AkshareFundCatalogProvider:
    """使用 AKShare 访问东方财富目录和雪球基金详情。"""

    source = CATALOG_SOURCE

    def __init__(self, cache_seconds: int = CATALOG_CACHE_SECONDS) -> None:
        self.cache_seconds = cache_seconds
        self._catalog: list[FundDirectoryEntry] | None = None
        self._catalog_fetched_at: datetime | None = None
        self._loaded_at = 0.0
        self._lock = Lock()

    def search(
        self,
        *,
        query: str,
        limit: int,
        refresh: bool = False,
    ) -> FundDirectorySearchBatch:
        """按代码、中文名或拼音搜索，并把完全匹配项排在最前。"""

        catalog, fetched_at, cache_hit = self._load_catalog(refresh=refresh)
        keyword = query.strip()
        keyword_lower = keyword.casefold()
        keyword_upper = keyword.upper()

        def rank(entry: FundDirectoryEntry) -> tuple[int, str]:
            code = entry.fund_code
            name = entry.short_name.casefold()
            abbreviation = entry.pinyin_abbreviation.upper()
            full_pinyin = entry.pinyin_name.upper()
            if keyword == code or keyword_lower == name:
                priority = 0
            elif code.startswith(keyword):
                priority = 1
            elif name.startswith(keyword_lower):
                priority = 2
            elif abbreviation.startswith(keyword_upper):
                priority = 3
            elif keyword_lower in name:
                priority = 4
            elif keyword_upper in abbreviation or keyword_upper in full_pinyin:
                priority = 5
            else:
                priority = 99
            return priority, code

        matches = [entry for entry in catalog if rank(entry)[0] < 99]
        matches.sort(key=rank)
        return FundDirectorySearchBatch(
            entries=matches[:limit],
            fetched_at=fetched_at,
            cache_hit=cache_hit,
        )

    def get_entry(self, fund_code: str) -> FundDirectoryEntry:
        """从同一份目录缓存中精确定位一只基金。"""

        batch = self.search(query=fund_code, limit=5)
        entry = next(
            (item for item in batch.entries if item.fund_code == fund_code),
            None,
        )
        if entry is None:
            raise FundCatalogProviderError(f"在线基金目录中没有代码 {fund_code}")
        return entry

    def fetch_profile(self, entry: FundDirectoryEntry) -> dict[str, Any]:
        """读取单只基金详情，并映射为当前项目通用档案字段。"""

        try:
            import akshare as ak
        except ModuleNotFoundError as exc:
            raise FundCatalogProviderError(
                "请先安装 requirements.txt 中的 akshare"
            ) from exc

        try:
            frame = ak.fund_individual_basic_info_xq(symbol=entry.fund_code)
        except Exception as exc:
            raise FundCatalogProviderError(
                f"AKShare 获取基金 {entry.fund_code} 档案失败：{exc}"
            ) from exc

        if frame.empty or not {"item", "value"}.issubset(frame.columns):
            raise FundCatalogProviderError(
                f"基金 {entry.fund_code} 返回的档案字段不完整"
            )
        values = {
            str(row["item"]).strip(): self._clean_value(row.get("value"))
            for row in frame.to_dict(orient="records")
        }
        return {
            "fund_code": entry.fund_code,
            "full_name": values.get("基金全称") or values.get("基金名称"),
            "short_name": values.get("基金名称") or entry.short_name,
            "fund_type": values.get("基金类型") or entry.fund_type,
            "sample_role": self._classify_role(
                fund_type=values.get("基金类型") or entry.fund_type,
                fund_name=values.get("基金名称") or entry.short_name,
            ),
            "issue_date": None,
            "established": values.get("成立时间"),
            "net_asset_size": values.get("最新规模"),
            "share_size": None,
            "management_company": values.get("基金公司"),
            "custodian": values.get("托管银行"),
            "fund_managers": values.get("基金经理"),
            "management_fee": None,
            "custodian_fee": None,
            "benchmark": values.get("业绩比较基准"),
            "tracking_target": None,
            "source_url": entry.source_url,
        }

    def _load_catalog(
        self,
        *,
        refresh: bool,
    ) -> tuple[list[FundDirectoryEntry], datetime, bool]:
        """读取完整目录并在当前进程缓存六小时，避免每次输入都请求上游。"""

        with self._lock:
            cache_valid = (
                self._catalog is not None
                and self._catalog_fetched_at is not None
                and time.monotonic() - self._loaded_at < self.cache_seconds
            )
            if cache_valid and not refresh:
                return self._catalog, self._catalog_fetched_at, True

            try:
                import akshare as ak
            except ModuleNotFoundError as exc:
                raise FundCatalogProviderError(
                    "请先安装 requirements.txt 中的 akshare"
                ) from exc

            try:
                frame = ak.fund_name_em()
            except Exception as exc:
                raise FundCatalogProviderError(
                    f"AKShare 获取基金目录失败：{exc}"
                ) from exc

            required = {"基金代码", "基金简称", "基金类型"}
            if frame.empty or not required.issubset(frame.columns):
                raise FundCatalogProviderError("在线基金目录字段不完整")

            catalog: list[FundDirectoryEntry] = []
            for row in frame.to_dict(orient="records"):
                code = str(row.get("基金代码") or "").strip()
                name = str(row.get("基金简称") or "").strip()
                fund_type = str(row.get("基金类型") or "").strip()
                if not FUND_CODE_PATTERN.fullmatch(code) or not name or not fund_type:
                    continue
                catalog.append(
                    FundDirectoryEntry(
                        fund_code=code,
                        short_name=name,
                        fund_type=fund_type,
                        pinyin_abbreviation=str(row.get("拼音缩写") or "").strip(),
                        pinyin_name=str(row.get("拼音全称") or "").strip(),
                    )
                )

            if not catalog:
                raise FundCatalogProviderError("在线基金目录没有可用记录")
            self._catalog = catalog
            self._catalog_fetched_at = datetime.now(timezone.utc)
            self._loaded_at = time.monotonic()
            return catalog, self._catalog_fetched_at, False

    @staticmethod
    def _clean_value(value: Any) -> str | None:
        if value is None:
            return None
        text = str(value).strip()
        return text if text and text.lower() != "nan" else None

    @staticmethod
    def _classify_role(*, fund_type: str, fund_name: str) -> str:
        """只做可靠的大类识别，不根据名称臆测具体行业。"""

        if "债券" in fund_type:
            return "bond_fund"
        if "指数" in fund_type and any(
            keyword in fund_name
            for keyword in ("沪深300", "中证500", "中证1000", "上证50", "科创50")
        ):
            return "broad_market_index"
        return "general_fund"


class FundCatalogRepository:
    """基金主数据表的 SQLAlchemy Repository。"""

    def __init__(self, database: Database = runtime_database) -> None:
        self.database = database
        self.database.create_schema()

    def upsert_summaries(
        self,
        *,
        entries: list[FundDirectoryEntry],
        fetched_at: datetime,
        source: str,
    ) -> None:
        """缓存搜索命中的轻量目录项，并保留已经完成的导入状态。"""

        with self.database.session() as session, session.begin():
            for entry in entries:
                model = session.get(FundCatalogEntryModel, entry.fund_code)
                if model is None:
                    session.add(
                        FundCatalogEntryModel(
                            fund_code=entry.fund_code,
                            short_name=entry.short_name,
                            full_name=None,
                            fund_type=entry.fund_type,
                            pinyin_abbreviation=entry.pinyin_abbreviation,
                            pinyin_name=entry.pinyin_name,
                            import_status="discovered",
                            profile={},
                            source=source,
                            source_url=entry.source_url,
                            last_error=None,
                            discovered_at=fetched_at,
                            profile_fetched_at=None,
                            nav_fetched_at=None,
                            updated_at=fetched_at,
                        )
                    )
                    continue
                model.short_name = entry.short_name
                model.fund_type = entry.fund_type
                model.pinyin_abbreviation = entry.pinyin_abbreviation
                model.pinyin_name = entry.pinyin_name
                model.source = source
                model.source_url = entry.source_url
                model.updated_at = fetched_at

    def upsert_profile(
        self,
        *,
        entry: FundDirectoryEntry,
        profile: dict[str, Any],
        fetched_at: datetime,
    ) -> None:
        """保存完整基金档案，并进入 importing 状态等待净值完成。"""

        with self.database.session() as session, session.begin():
            model = session.get(FundCatalogEntryModel, entry.fund_code)
            if model is None:
                model = FundCatalogEntryModel(
                    fund_code=entry.fund_code,
                    discovered_at=fetched_at,
                    nav_fetched_at=None,
                )
                session.add(model)
            model.short_name = str(profile["short_name"])
            model.full_name = profile.get("full_name")
            model.fund_type = str(profile["fund_type"])
            model.pinyin_abbreviation = entry.pinyin_abbreviation
            model.pinyin_name = entry.pinyin_name
            model.import_status = "importing"
            model.profile = profile
            model.source = PROFILE_SOURCE
            model.source_url = entry.source_url
            model.last_error = None
            model.profile_fetched_at = fetched_at
            model.updated_at = fetched_at

    def set_status(
        self,
        *,
        fund_code: str,
        status: str,
        error: str | None = None,
        nav_fetched_at: datetime | None = None,
    ) -> None:
        """更新导入状态；调用方必须先通过目录 upsert 建立记录。"""

        now = datetime.now(timezone.utc)
        with self.database.session() as session, session.begin():
            model = session.get(FundCatalogEntryModel, fund_code)
            if model is None:
                raise FundCatalogImportError(f"基金 {fund_code} 尚未进入目录缓存")
            model.import_status = status
            model.last_error = error
            if nav_fetched_at is not None:
                model.nav_fetched_at = nav_fetched_at
            model.updated_at = now

    def list_ready_codes(self) -> list[str]:
        """返回每日净值同步需要覆盖的已导入基金代码。"""

        statement = (
            select(FundCatalogEntryModel.fund_code)
            .where(FundCatalogEntryModel.import_status == "ready")
            .order_by(FundCatalogEntryModel.fund_code.asc())
        )
        with self.database.session() as session:
            return list(session.scalars(statement))

    def list_ready_profiles(self) -> list[dict[str, Any]]:
        """返回工作台可用的完整档案，不把只搜索过的基金混入分析。"""

        statement = (
            select(FundCatalogEntryModel)
            .where(FundCatalogEntryModel.import_status == "ready")
            .order_by(FundCatalogEntryModel.updated_at.desc())
        )
        with self.database.session() as session:
            return [dict(row.profile or {}) for row in session.scalars(statement)]

    def get_many(self, fund_codes: list[str]) -> dict[str, dict[str, Any]]:
        """一次读取搜索结果的状态，避免为每一行单独发 SQL。"""

        if not fund_codes:
            return {}
        statement = select(FundCatalogEntryModel).where(
            FundCatalogEntryModel.fund_code.in_(fund_codes)
        )
        with self.database.session() as session:
            return {
                row.fund_code: self._to_dict(row)
                for row in session.scalars(statement)
            }

    def search_cached(self, *, query: str, limit: int) -> list[dict[str, Any]]:
        """上游不可用时，在已经发现过的 SQL 目录中继续提供搜索。"""

        keyword = query.strip()
        pattern = f"%{keyword}%"
        upper_pattern = f"%{keyword.upper()}%"
        statement = (
            select(FundCatalogEntryModel)
            .where(
                or_(
                    FundCatalogEntryModel.fund_code.like(pattern),
                    FundCatalogEntryModel.short_name.like(pattern),
                    func.upper(FundCatalogEntryModel.pinyin_abbreviation).like(
                        upper_pattern
                    ),
                    func.upper(FundCatalogEntryModel.pinyin_name).like(upper_pattern),
                )
            )
            .limit(limit)
        )
        with self.database.session() as session:
            return [self._to_dict(row) for row in session.scalars(statement)]

    @staticmethod
    def _to_dict(row: FundCatalogEntryModel) -> dict[str, Any]:
        updated_at = row.updated_at
        if updated_at.tzinfo is None:
            updated_at = updated_at.replace(tzinfo=timezone.utc)
        return {
            "fund_code": row.fund_code,
            "short_name": row.short_name,
            "fund_type": row.fund_type,
            "source_url": row.source_url,
            "import_status": row.import_status,
            "is_imported": row.import_status == "ready",
            "updated_at": updated_at.isoformat(),
        }


class FundDirectoryService:
    """组合在线 Provider、SQL 缓存和 fixture 基线的目录搜索入口。"""

    def __init__(
        self,
        *,
        repository: FundCatalogRepository,
        provider: AkshareFundCatalogProvider | Any | None = None,
        fixture_dir: Path = DEFAULT_FIXTURE_DIR,
    ) -> None:
        self.repository = repository
        self.provider = provider or AkshareFundCatalogProvider()
        self.fixture_codes = self._load_fixture_codes(Path(fixture_dir))

    def search(
        self,
        *,
        query: str,
        limit: int = 12,
        refresh: bool = False,
    ) -> dict[str, Any]:
        """优先返回在线目录；上游失败时仅在存在缓存时降级。"""

        keyword = str(query or "").strip()
        if not keyword:
            raise ValueError("基金搜索词不能为空。")
        safe_limit = max(1, min(int(limit), 30))

        try:
            batch = self.provider.search(
                query=keyword,
                limit=safe_limit,
                refresh=refresh,
            )
            self.repository.upsert_summaries(
                entries=batch.entries,
                fetched_at=batch.fetched_at,
                source=self.provider.source,
            )
            statuses = self.repository.get_many(
                [entry.fund_code for entry in batch.entries]
            )
            funds = [
                self._public_entry(
                    entry=entry,
                    cached=statuses.get(entry.fund_code),
                )
                for entry in batch.entries
            ]
            return {
                "query": keyword,
                "count": len(funds),
                "source": self.provider.source,
                "data_mode": "memory_cache" if batch.cache_hit else "live",
                "catalog_updated_at": batch.fetched_at.isoformat(),
                "message": "",
                "funds": funds,
            }
        except FundCatalogProviderError:
            cached = self.repository.search_cached(query=keyword, limit=safe_limit)
            if not cached:
                raise
            latest = max(item["updated_at"] for item in cached)
            for item in cached:
                if item["fund_code"] in self.fixture_codes:
                    item["import_status"] = "ready"
                    item["is_imported"] = True
            return {
                "query": keyword,
                "count": len(cached),
                "source": CATALOG_SOURCE,
                "data_mode": "database_cache",
                "catalog_updated_at": latest,
                "message": "在线目录暂时不可用，当前显示最近缓存。",
                "funds": cached,
            }

    def _public_entry(
        self,
        *,
        entry: FundDirectoryEntry,
        cached: dict[str, Any] | None,
    ) -> dict[str, Any]:
        fixture_ready = entry.fund_code in self.fixture_codes
        status = "ready" if fixture_ready else (cached or {}).get(
            "import_status",
            "discovered",
        )
        return {
            "fund_code": entry.fund_code,
            "short_name": entry.short_name,
            "fund_type": entry.fund_type,
            "source_url": entry.source_url,
            "import_status": status,
            "is_imported": status == "ready",
        }

    @staticmethod
    def _load_fixture_codes(fixture_dir: Path) -> set[str]:
        path = fixture_dir / "fund_profiles.json"
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            return {str(item["fund_code"]) for item in payload["funds"]}
        except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
            raise FundCatalogProviderError("无法读取基金 fixture 目录") from exc


class FundCatalogImportService:
    """把一只目录基金变成可供风险分析和组合使用的完整基金。"""

    def __init__(
        self,
        *,
        repository: FundCatalogRepository,
        provider: AkshareFundCatalogProvider | Any,
    ) -> None:
        self.repository = repository
        self.provider = provider

    def import_fund(
        self,
        *,
        fund_code: str,
        progress_callback: Callable[[int, str], None] | None = None,
    ) -> dict[str, Any]:
        """依次补齐目录、档案和三年净值，任一步失败都会留下可重试状态。"""

        code = str(fund_code or "").strip()
        if not FUND_CODE_PATTERN.fullmatch(code):
            raise ValueError("fund_code 必须是 6 位数字。")

        entry = self.provider.get_entry(code)
        now = datetime.now(timezone.utc)
        self.repository.upsert_summaries(
            entries=[entry],
            fetched_at=now,
            source=self.provider.source,
        )
        self.repository.set_status(fund_code=code, status="importing")

        try:
            if progress_callback:
                progress_callback(1, f"正在读取 {code} 的基金档案。")
            profile = self.provider.fetch_profile(entry)
            self.repository.upsert_profile(
                entry=entry,
                profile=profile,
                fetched_at=datetime.now(timezone.utc),
            )

            if progress_callback:
                progress_callback(2, f"正在同步 {code} 的近三年净值。")
            # 延迟导入避免 fund_data_sync 与本模块形成初始化循环。
            from app.services.fund_data_sync import fund_nav_sync_service

            nav_result = fund_nav_sync_service.sync(fund_codes=[code])
            completed_at = datetime.now(timezone.utc)
            self.repository.set_status(
                fund_code=code,
                status="ready",
                nav_fetched_at=completed_at,
            )
            if progress_callback:
                progress_callback(3, f"{code} 已进入基金研究工作台。")
            return {
                "summary": f"{profile['short_name']}（{code}）已导入并可开始分析。",
                "workflow": "fund_catalog_import",
                "fund_code": code,
                "profile": profile,
                "nav_sync": nav_result,
                "imported_at": completed_at.isoformat(),
            }
        except Exception as exc:
            self.repository.set_status(
                fund_code=code,
                status="failed",
                error=str(exc)[:1000],
            )
            if isinstance(exc, (FundCatalogProviderError, ValueError)):
                raise
            raise FundCatalogImportError(f"基金 {code} 导入失败：{exc}") from exc


fund_catalog_repository = FundCatalogRepository(runtime_database)
fund_catalog_provider = AkshareFundCatalogProvider()
fund_directory_service = FundDirectoryService(
    repository=fund_catalog_repository,
    provider=fund_catalog_provider,
)
fund_catalog_import_service = FundCatalogImportService(
    repository=fund_catalog_repository,
    provider=fund_catalog_provider,
)
