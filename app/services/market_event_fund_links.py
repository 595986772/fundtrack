"""根据基金档案与披露，把市场事件关联到当前工作台基金。

这个模块只回答“事件提到了哪些基金主题或重仓证券”，不回答利好、利空和未来涨跌。
关联规则全部来自基金名称、基金类型、季报披露和版本化行业词典，因此结果可以复现，
也不会把基金代码固定在三只教学样例上。
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Callable

from app.services.fund_disclosures import fund_disclosure_service
from app.services.funds import fund_catalog_service
from app.services.security_sectors import SecuritySectorService, security_sector_service


# 这些词单独出现时范围过宽，不能据此把一条新闻关联到某只基金。
GENERIC_LINK_TERMS = {
    "行业",
    "产业",
    "市场",
    "基金",
    "股票",
    "指数",
    "投资",
    "经济",
    "增长",
    "数据",
    "科技",
    "金融",
    "证券",
    "金融业",
    "制造业",
}

# 债券基金和宽基指数还需要一组可解释的资产类别规则。行业主题基金主要依靠
# 名称、重仓证券和细分行业词典，不在这里逐只硬编码基金代码。
BOND_EVENT_TERMS = (
    "债券",
    "债市",
    "国债",
    "利率",
    "降息",
    "加息",
    "收益率曲线",
    "信用利差",
    "央行",
    "人民银行",
    "货币政策",
)
BROAD_MARKET_NAME_TERMS = (
    "沪深300",
    "中证500",
    "中证800",
    "中证1000",
    "上证50",
    "创业板",
    "科创50",
    "全指",
    "A股",
)
BROAD_MARKET_EVENT_TERMS = (
    "资本市场",
    "A股",
    "沪深",
    "股票市场",
    "证券市场",
    "证监会",
    "指数",
    "成交额",
    "北向资金",
    "降准",
    "财政政策",
    "货币政策",
    "GDP",
    "CPI",
    "PPI",
    "PMI",
)

# 极小的尾部持仓不应让白酒基金因为持有 0.12% 半导体股票就被归为半导体基金。
# 基金名称中的主题词不受此阈值影响；阈值只约束季报持仓推导出的关联。
MIN_DIRECT_HOLDING_WEIGHT_PCT = 0.5
MIN_SECTOR_WEIGHT_PCT = 1.0
MIN_INDUSTRY_WEIGHT_PCT = 1.0

FundProfileLoader = Callable[[], list[dict[str, Any]]]
DisclosureLoader = Callable[[list[str]], dict[str, dict[str, Any]]]


@dataclass(frozen=True)
class FundEventLinkProfile:
    """一只基金用于事件匹配的精简研究画像。"""

    fund_code: str
    short_name: str
    fund_type: str
    direct_terms: tuple[str, ...]
    theme_terms: tuple[str, ...]
    is_bond_fund: bool
    is_broad_market_fund: bool


class MarketEventFundLinkService:
    """从当前基金目录动态建立基金画像，并为事件补充关联基金代码。"""

    def __init__(
        self,
        *,
        fund_profile_loader: FundProfileLoader | None = None,
        disclosure_loader: DisclosureLoader | None = None,
        sector_service: SecuritySectorService = security_sector_service,
    ) -> None:
        # Loader 可以在单元测试中替换成固定数据，正式运行时读取当前工作台。
        self.fund_profile_loader = (
            fund_profile_loader or fund_catalog_service.list_profiles
        )
        self.disclosure_loader = (
            disclosure_loader or fund_disclosure_service.list_for_funds
        )
        self.sector_service = sector_service

    def available_fund_codes(self) -> list[str]:
        """返回当前已导入基金代码，供公告采集和模型白名单共同使用。"""

        return list(
            dict.fromkeys(
                str(profile.get("fund_code") or "").strip()
                for profile in self.fund_profile_loader()
                if str(profile.get("fund_code") or "").strip()
            )
        )

    def model_fund_contexts(self) -> list[dict[str, Any]]:
        """给语义模型提供代码、名称和少量主题词，而不发送整份季报。"""

        return [
            {
                "fund_code": profile.fund_code,
                "short_name": profile.short_name,
                "fund_type": profile.fund_type,
                "theme_keywords": list(profile.theme_terms[:12]),
            }
            for profile in self._build_link_profiles()
        ]

    def link_events(self, events: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """为一批事件补充基金代码；一次构建画像后复用，避免逐条重复查库。"""

        profiles = self._build_link_profiles()
        return [self._link_event(event=event, profiles=profiles) for event in events]

    def _build_link_profiles(self) -> list[FundEventLinkProfile]:
        """把基金档案、季报重仓和行业词典合成可匹配的精简画像。"""

        raw_profiles = self.fund_profile_loader()
        fund_codes = [
            str(profile.get("fund_code") or "").strip()
            for profile in raw_profiles
            if str(profile.get("fund_code") or "").strip()
        ]
        disclosures = self.disclosure_loader(fund_codes)
        taxonomy_groups = [
            self._clean_terms(group)
            for group in self.sector_service.keyword_groups()
        ]
        taxonomy_term_counts: dict[str, int] = {}
        for group in taxonomy_groups:
            for term in set(group):
                taxonomy_term_counts[term] = taxonomy_term_counts.get(term, 0) + 1
        link_profiles: list[FundEventLinkProfile] = []

        for raw_profile in raw_profiles:
            fund_code = str(raw_profile.get("fund_code") or "").strip()
            if not fund_code:
                continue

            short_name = str(raw_profile.get("short_name") or "").strip()
            full_name = str(raw_profile.get("full_name") or "").strip()
            tracking_target = str(raw_profile.get("tracking_target") or "").strip()
            benchmark = str(raw_profile.get("benchmark") or "").strip()
            fund_type = str(raw_profile.get("fund_type") or "").strip()
            name_context = self._normalize_text(
                " ".join([short_name, full_name, tracking_target, benchmark])
            )

            # 代码、基金名、跟踪标的是强证据；新闻直接出现它们即可关联。
            direct_terms = self._clean_terms(
                [fund_code, short_name, full_name, tracking_target]
            )
            # 先从基金名称和业绩基准中识别版本化行业词，再加入季报证据。
            theme_terms: list[str] = []
            for group in taxonomy_groups:
                matched_terms = [
                    term for term in group if term.casefold() in name_context
                ]
                if not matched_terms:
                    continue
                if any(taxonomy_term_counts[term] == 1 for term in matched_terms):
                    # 名称明确写“AI芯片”或“半导体设备”时，可以使用对应细分组。
                    theme_terms.extend(group)
                else:
                    # 名称只写宽泛的“半导体/芯片”时，仅扩展多个细分组共有的
                    # “集成电路”等同义词，不能顺便推导出算力、材料或设备主题。
                    theme_terms.extend(
                        term
                        for term in group
                        if taxonomy_term_counts[term] > 1
                    )
            disclosure = disclosures.get(fund_code, {})
            weighted_sector_terms: dict[str, float] = {}
            for holding in disclosure.get("security_holdings", []):
                security_code = str(holding.get("security_code") or "").strip()
                security_name = str(holding.get("security_name") or "").strip()
                holding_weight = float(holding.get("weight_pct") or 0)
                if holding_weight >= MIN_DIRECT_HOLDING_WEIGHT_PCT:
                    direct_terms.extend(
                        self._clean_terms([security_code, security_name])
                    )

                sector = self.sector_service.get(
                    security_code=security_code,
                    security_name=security_name,
                )
                if sector is not None:
                    for term in [sector["sector_name"], *sector.get("aliases", [])]:
                        weighted_sector_terms[str(term)] = (
                            weighted_sector_terms.get(str(term), 0.0) + holding_weight
                        )

            theme_terms.extend(
                term
                for term, weight in weighted_sector_terms.items()
                if weight >= MIN_SECTOR_WEIGHT_PCT
            )

            for allocation in disclosure.get("industry_allocations", []):
                if float(allocation.get("weight_pct") or 0) >= MIN_INDUSTRY_WEIGHT_PCT:
                    theme_terms.append(str(allocation.get("industry_name") or ""))

            is_bond_fund = "债券" in fund_type or "债券" in name_context
            is_broad_market_fund = (
                raw_profile.get("sample_role") == "broad_market_index"
                or any(term.casefold() in name_context for term in BROAD_MARKET_NAME_TERMS)
            )
            if is_bond_fund:
                theme_terms.extend(BOND_EVENT_TERMS)

            link_profiles.append(
                FundEventLinkProfile(
                    fund_code=fund_code,
                    short_name=short_name,
                    fund_type=fund_type,
                    direct_terms=tuple(self._clean_terms(direct_terms)),
                    theme_terms=tuple(self._clean_terms(theme_terms)),
                    is_bond_fund=is_bond_fund,
                    is_broad_market_fund=is_broad_market_fund,
                )
            )
        return link_profiles

    def _link_event(
        self,
        *,
        event: dict[str, Any],
        profiles: list[FundEventLinkProfile],
    ) -> dict[str, Any]:
        """使用直接提及、主题命中和资产类别规则给单条事件追加基金。"""

        semantic = event.get("semantic_analysis") or {}
        event_text = self._normalize_text(
            " ".join(
                [
                    str(event.get("title") or ""),
                    str(event.get("summary") or ""),
                    " ".join(str(tag) for tag in event.get("tags", [])),
                    " ".join(str(item) for item in semantic.get("entities", [])),
                    " ".join(str(item) for item in semantic.get("themes", [])),
                ]
            )
        )
        existing_codes = [
            str(code).strip()
            for code in event.get("affected_fund_codes", [])
            if str(code).strip()
        ]

        linked_codes: list[str] = []
        for profile in profiles:
            direct_match = any(
                self._normalize_text(term) in event_text
                for term in profile.direct_terms
            )
            theme_match = any(
                self._normalize_text(term) in event_text
                for term in profile.theme_terms
            )
            broad_market_match = profile.is_broad_market_fund and any(
                self._normalize_text(term) in event_text
                for term in BROAD_MARKET_EVENT_TERMS
            )
            if direct_match or theme_match or broad_market_match:
                linked_codes.append(profile.fund_code)

        return {
            **event,
            "affected_fund_codes": list(
                dict.fromkeys([*existing_codes, *linked_codes])
            ),
        }

    @classmethod
    def _clean_terms(cls, values: list[Any]) -> list[str]:
        """去掉空值、宽泛词和过短词，降低“市场、行业”造成的误关联。"""

        cleaned: list[str] = []
        for value in values:
            term = re.sub(r"\s+", " ", str(value or "")).strip()
            if not term or term in GENERIC_LINK_TERMS:
                continue
            if not term.isdigit() and len(term) < 2:
                continue
            cleaned.append(term)
        return list(dict.fromkeys(cleaned))

    @staticmethod
    def _normalize_text(value: Any) -> str:
        return re.sub(r"\s+", " ", str(value)).strip().casefold()


market_event_fund_link_service = MarketEventFundLinkService()
