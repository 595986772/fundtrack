"""将市场事件映射到用户组合的底层证券、细分行业和基金权重。

这层解决的是“消息和我的持仓有什么关系”，而不是预测涨跌。模型语义层已经提取
entities、themes、direction 和 confidence；这里由 Python 完成可重复的字符串匹配、
季报权重关联、事件簇去重和关注优先级计算。

受影响仓位按最精确的可用口径计算，优先级为：

1. 事件直接点名的底层证券；
2. 事件命中的细分行业，汇总该行业已披露重仓；
3. 基金季报给出的宽口径行业配置；
4. 事件直接关联的整只基金。

一条事件只使用其中最精确的一层，避免证券、行业和基金权重被重复相加。
"""

from __future__ import annotations

from datetime import date
import re
from typing import Any, Protocol

from app.services.market_events import MarketEventError, market_event_service
from app.services.portfolio_exposure import (
    PortfolioExposureError,
    portfolio_exposure_service,
)
from app.services.portfolios import PortfolioNotFoundError
from app.services.security_sectors import (
    SecuritySectorError,
    security_sector_service,
)


SOURCE_RELIABILITY = {"A": 1.0, "B": 0.85, "C": 0.7, "D": 0.35}
MATCH_CERTAINTY = {
    "direct_security": 1.0,
    "fine_sector": 0.85,
    "broad_industry": 0.65,
    "direct_fund": 0.75,
}
MATCH_LABELS = {
    "direct_security": "直接点名底层证券",
    "fine_sector": "匹配细分行业重仓",
    "broad_industry": "匹配基金季报行业",
    "direct_fund": "直接关联持仓基金",
}

# 基金披露行业是证监会大类。这里只维护常见简称，目的是让“交通运输”能匹配
# “交通运输、仓储和邮政业”；不把语义相近但边界不同的行业偷偷合并。
BROAD_INDUSTRY_ALIASES: dict[str, tuple[str, ...]] = {
    "制造业": ("制造业", "先进制造", "新型工业化"),
    "金融业": ("金融业", "金融行业"),
    "交通运输、仓储和邮政业": ("交通运输", "仓储", "邮政"),
    "信息传输、软件和信息技术服务业": ("信息技术", "软件", "数字经济"),
    "科学研究和技术服务业": ("科研服务", "技术服务"),
    "电力、热力、燃气及水生产和供应业": ("电力", "燃气", "公用事业"),
    "农、林、牧、渔业": ("农业", "林业", "牧业", "渔业"),
    "采矿业": ("采矿", "煤炭", "矿业"),
    "建筑业": ("建筑业", "建筑工程"),
}


class PortfolioEventImpactError(RuntimeError):
    """组合、持仓披露或事件数据无法生成归因结果。"""


class ExposureReader(Protocol):
    def analyze(self, *, user_id: str, portfolio_id: str) -> dict[str, Any]: ...


class EventReader(Protocol):
    def search(self, *, limit: int = 20, **kwargs: Any) -> dict[str, Any]: ...


class SectorReader(Protocol):
    def get(self, *, security_code: str, security_name: str = "") -> dict[str, Any] | None: ...

    def classify_exposures(
        self,
        security_exposures: list[dict[str, Any]],
    ) -> dict[str, Any]: ...


class PortfolioEventImpactService:
    """生成组合当前持仓与有效市场事件之间的可追溯关联。"""

    def __init__(
        self,
        *,
        exposures: ExposureReader = portfolio_exposure_service,
        events: EventReader = market_event_service,
        sectors: SectorReader = security_sector_service,
    ) -> None:
        self.exposures = exposures
        self.events = events
        self.sectors = sectors

    def analyze(
        self,
        *,
        user_id: str,
        portfolio_id: str,
        limit: int = 12,
    ) -> dict[str, Any]:
        """计算有效去重事件影响；limit 只限制返回明细，不改变统计总数。"""

        if not 1 <= int(limit) <= 30:
            raise PortfolioEventImpactError("limit 必须在 1 到 30 之间")
        try:
            exposure = self.exposures.analyze(
                user_id=user_id,
                portfolio_id=portfolio_id,
            )
            event_payload = self.events.search(limit=50)
            sector_payload = self.sectors.classify_exposures(
                exposure["security_exposures"]
            )
        except PortfolioNotFoundError:
            raise
        except (
            PortfolioExposureError,
            MarketEventError,
            SecuritySectorError,
            KeyError,
            TypeError,
        ) as exc:
            raise PortfolioEventImpactError(f"无法生成持仓事件归因：{exc}") from exc

        events = self._deduplicate_events(list(event_payload.get("events") or []))
        active_events = [
            event for event in events if float(event.get("decay_factor", 1.0)) > 0
        ]
        semantic_accepted_count = sum(
            event.get("semantic_analysis", {}).get("status") == "accepted"
            for event in active_events
            if event.get("semantic_analysis")
        )

        impacts: list[dict[str, Any]] = []
        skipped_unreviewed = 0
        fixture_links_allowed = event_payload.get("data_mode") == "fixture"
        for event in active_events:
            # 数据库自动采集事件只有在 AI 语义结果被 Python 接受后才参与主题匹配。
            # 基金公告等确定性 fund_code 关联即使未语义复核，也允许作为中性直接事件。
            semantic = event.get("semantic_analysis")
            has_deterministic_fund_link = bool(event.get("affected_fund_codes")) and (
                fixture_links_allowed or event.get("event_type") == "fund_announcement"
            )
            if (
                semantic
                and semantic.get("status") != "accepted"
                and not has_deterministic_fund_link
            ):
                skipped_unreviewed += 1
                continue
            if (
                semantic is None
                and not fixture_links_allowed
                and not has_deterministic_fund_link
            ):
                skipped_unreviewed += 1
                continue

            impact = self._match_event(
                event=event,
                exposure=exposure,
                sector_payload=sector_payload,
                allow_collector_fund_codes=fixture_links_allowed,
            )
            if impact is not None:
                impacts.append(impact)

        # priority_score 是“值得先看”的排序分，不是损益预测。相同分数时优先较新事件。
        impacts.sort(
            key=lambda item: (
                item["priority_score"],
                item["event_date"],
                item["affected_exposure_pct"],
            ),
            reverse=True,
        )
        directional = [item for item in impacts if item["direction"] != "neutral"]
        caution_count = sum(item["direction"] == "caution" for item in impacts)
        supportive_count = sum(item["direction"] == "supportive" for item in impacts)
        neutral_count = len(impacts) - caution_count - supportive_count
        top_impact = impacts[0] if impacts else None

        summary = (
            f"{len(active_events)} 个有效去重事件中，{len(impacts)} 个能与当前持仓形成"
            f"可验证关联，其中风险关注 {caution_count} 个、支撑 {supportive_count} 个。"
        )
        if top_impact:
            summary += (
                f"当前优先查看“{top_impact['title']}”，按{top_impact['match_label']}"
                f"关联组合 {top_impact['affected_exposure_pct']:.2f}%。"
            )

        return {
            "data_kind": "deterministic_portfolio_event_impacts",
            "portfolio_id": portfolio_id,
            "user_id": user_id,
            "portfolio_name": exposure["portfolio_name"],
            "calculated_at": date.today().isoformat(),
            "event_data_cutoff": event_payload["data_cutoff"],
            "latest_holding_report_date": exposure.get("latest_report_date"),
            "summary": summary,
            "quality": {
                "status": self._quality_status(
                    exposure_status=exposure["data_quality"]["status"],
                    classified_exposure=float(
                        sector_payload["classified_stock_exposure_pct"]
                    ),
                    matched_count=len(impacts),
                ),
                "active_event_count": len(active_events),
                "matched_event_count": len(impacts),
                "directional_event_count": len(directional),
                "unmatched_event_count": max(0, len(active_events) - len(impacts)),
                "semantic_accepted_count": semantic_accepted_count,
                "skipped_unreviewed_count": skipped_unreviewed,
                "security_coverage_pct": exposure["data_quality"][
                    "security_coverage_pct"
                ],
                "classified_stock_exposure_pct": sector_payload[
                    "classified_stock_exposure_pct"
                ],
                "unclassified_stock_exposure_pct": sector_payload[
                    "unclassified_stock_exposure_pct"
                ],
                "taxonomy_version": sector_payload["taxonomy_version"],
                "taxonomy_source": sector_payload["source"],
            },
            "caution_event_count": caution_count,
            "supportive_event_count": supportive_count,
            "neutral_event_count": neutral_count,
            "sector_exposures": sector_payload["sector_exposures"],
            "impacts": impacts[: int(limit)],
            "total_matched": len(impacts),
            "omitted_impact_count": max(0, len(impacts) - int(limit)),
            "methodology": (
                "事件先按 cluster_id 去重并剔除过期结果；Python 按直接证券、细分行业、"
                "季报大类行业、直接基金的顺序选择最精确的一层。priority_score = 受影响"
                "仓位 × 严重度/3 × 模型置信度 × 时间衰减 × 来源可靠度 × 匹配确定度。"
                "中性事件再乘 0.35，只降低关注排序，不修改事件原始方向。"
            ),
            "limitations": [
                "受影响仓位表示事件可关联的组合范围，不是预计涨跌幅或可能亏损比例。",
                "细分行业只覆盖版本化词典中的已披露重仓，未分类和未穿透仓位不会由模型补猜。",
                "基金季报持仓有披露滞后；报告期与事件数据截止日必须分别理解。",
                "宏观背景若无法匹配具体证券、行业或基金，会保留在事件库但不进入本列表。",
            ],
            "disclaimer": "事件影响归因仅用于风险研究和信息整理，不构成投资建议。",
        }

    def _match_event(
        self,
        *,
        event: dict[str, Any],
        exposure: dict[str, Any],
        sector_payload: dict[str, Any],
        allow_collector_fund_codes: bool,
    ) -> dict[str, Any] | None:
        """按最精确口径匹配单个事件，并计算受影响仓位和排序分。"""

        semantic = event.get("semantic_analysis") or {}
        text = self._event_text(event)
        matched_keywords: set[str] = set()

        direct_securities = []
        security_profiles: dict[str, dict[str, Any]] = {}
        for item in exposure["security_exposures"]:
            code = str(item["security_code"])
            name = str(item["security_name"])
            profile = self.sectors.get(security_code=code, security_name=name)
            if profile:
                security_profiles[code] = profile
            direct_terms = [code, name]
            hits = [term for term in direct_terms if self._contains_term(text, term)]
            if hits:
                direct_securities.append(self._security_match(item, profile))
                matched_keywords.update(hits)

        matched_sector_names: set[str] = set()
        matched_sector_keywords: dict[str, list[str]] = {}
        for sector in sector_payload["sector_exposures"]:
            terms = [sector["sector_name"], *sector.get("aliases", [])]
            hits = [term for term in terms if self._contains_term(text, term)]
            if hits:
                matched_sector_names.add(sector["sector_name"])
                matched_sector_keywords[sector["sector_name"]] = sorted(set(hits))
                matched_keywords.update(hits)

        sector_securities = [
            self._security_match(item, security_profiles.get(str(item["security_code"])))
            for item in exposure["security_exposures"]
            if security_profiles.get(str(item["security_code"]), {}).get("sector_name")
            in matched_sector_names
        ]
        matched_sectors = [
            {
                "sector_name": item["sector_name"],
                "portfolio_exposure_pct": item["portfolio_exposure_pct"],
                "security_count": item["security_count"],
                "matched_keywords": matched_sector_keywords[item["sector_name"]],
            }
            for item in sector_payload["sector_exposures"]
            if item["sector_name"] in matched_sector_names
        ]

        matched_industries = []
        for item in exposure["industry_exposures"]:
            aliases = BROAD_INDUSTRY_ALIASES.get(
                item["industry_name"],
                (item["industry_name"],),
            )
            hits = [term for term in aliases if self._contains_term(text, term)]
            if hits:
                matched_industries.append(
                    {
                        "industry_name": item["industry_name"],
                        "portfolio_exposure_pct": item["portfolio_exposure_pct"],
                        "matched_keywords": sorted(set(hits)),
                    }
                )
                matched_keywords.update(hits)

        # 自动采集器的 fund_codes 只是粗召回，不能直接当作整只基金受影响。数据库事件
        # 只采用已通过语义校验的代码；基金公告和人工冻结 fixture 才可使用采集层代码。
        if semantic.get("status") == "accepted":
            affected_codes = set(semantic.get("affected_fund_codes") or [])
        elif allow_collector_fund_codes or event.get("event_type") == "fund_announcement":
            affected_codes = set(event.get("affected_fund_codes") or [])
        else:
            affected_codes = set()
        matched_funds = [
            {
                "fund_code": item["fund_code"],
                "short_name": item["short_name"],
                "portfolio_weight_pct": item["portfolio_weight_pct"],
            }
            for item in exposure["fund_reports"]
            if item["fund_code"] in affected_codes
        ]
        matched_keywords.update(item["fund_code"] for item in matched_funds)

        # 只选最精确的一层计算仓位，下一层仍保留在结果中作为解释上下文。
        if direct_securities:
            match_basis = "direct_security"
            affected_exposure = sum(
                item["portfolio_exposure_pct"] for item in direct_securities
            )
        elif sector_securities:
            match_basis = "fine_sector"
            affected_exposure = sum(
                item["portfolio_exposure_pct"] for item in sector_securities
            )
        elif matched_industries:
            match_basis = "broad_industry"
            affected_exposure = sum(
                item["portfolio_exposure_pct"] for item in matched_industries
            )
        elif matched_funds:
            match_basis = "direct_fund"
            affected_exposure = sum(
                item["portfolio_weight_pct"] for item in matched_funds
            )
        else:
            return None

        direction = self._direction(event=event, semantic=semantic)
        severity = int(semantic.get("severity") or self._default_severity(event))
        confidence = float(
            semantic.get("confidence")
            if semantic.get("status") == "accepted"
            else (0.85 if matched_funds else 0.7)
        )
        decay_factor = float(event.get("decay_factor", 1.0))
        source_weight = SOURCE_RELIABILITY.get(str(event.get("source_level")), 0.6)
        priority_score = (
            min(100.0, affected_exposure)
            * severity
            / 3
            * confidence
            * decay_factor
            * source_weight
            * MATCH_CERTAINTY[match_basis]
            * (0.35 if direction == "neutral" else 1.0)
        )
        cluster = event.get("cluster") or {}
        impact_id = cluster.get("cluster_id") or event["event_id"]

        return {
            "impact_evidence_id": f"portfolio-impact:{exposure['portfolio_id']}:{impact_id}",
            "event_id": event["event_id"],
            "cluster_id": cluster.get("cluster_id"),
            "title": event["title"],
            "event_date": event["event_date"],
            "event_type": event["event_type"],
            "direction": direction,
            "horizon": event["impact_horizon"],
            "severity": severity,
            "confidence": round(confidence, 4),
            "relevance": semantic.get("relevance") or (
                "direct" if match_basis in {"direct_security", "direct_fund"} else "related"
            ),
            "freshness_status": event["freshness_status"],
            "decay_factor": round(decay_factor, 4),
            "source_name": event["source_name"],
            "source_level": event["source_level"],
            "source_url": event["source_url"],
            "affected_exposure_pct": round(min(100.0, affected_exposure), 4),
            "priority_score": round(priority_score, 4),
            "match_basis": match_basis,
            "match_label": MATCH_LABELS[match_basis],
            "matched_keywords": sorted(matched_keywords),
            "matched_securities": sorted(
                direct_securities or sector_securities,
                key=lambda item: item["portfolio_exposure_pct"],
                reverse=True,
            )[:8],
            "matched_sectors": sorted(
                matched_sectors,
                key=lambda item: item["portfolio_exposure_pct"],
                reverse=True,
            ),
            "matched_industries": sorted(
                matched_industries,
                key=lambda item: item["portfolio_exposure_pct"],
                reverse=True,
            ),
            "matched_funds": sorted(
                matched_funds,
                key=lambda item: item["portfolio_weight_pct"],
                reverse=True,
            ),
            "reason": str(semantic.get("reason") or event["impact_reason"]),
            "uncertainties": list(semantic.get("uncertainties") or []),
        }

    @staticmethod
    def _security_match(
        item: dict[str, Any],
        profile: dict[str, Any] | None,
    ) -> dict[str, Any]:
        return {
            "asset_type": item["asset_type"],
            "security_code": item["security_code"],
            "security_name": item["security_name"],
            "sector_name": profile.get("sector_name") if profile else None,
            "portfolio_exposure_pct": item["portfolio_exposure_pct"],
            "evidence_id": item["evidence_id"],
            "report_date": item["report_date"],
        }

    @classmethod
    def _event_text(cls, event: dict[str, Any]) -> str:
        semantic = event.get("semantic_analysis") or {}
        values = [
            event.get("title"),
            event.get("summary"),
            event.get("impact_reason"),
            *(event.get("tags") or []),
            *(semantic.get("entities") or []),
            *(semantic.get("themes") or []),
        ]
        return cls._normalize(" ".join(str(value or "") for value in values))

    @classmethod
    def _contains_term(cls, normalized_text: str, term: str) -> bool:
        normalized_term = cls._normalize(term)
        return len(normalized_term) >= 2 and normalized_term in normalized_text

    @staticmethod
    def _normalize(value: str) -> str:
        return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", str(value).casefold())

    @staticmethod
    def _direction(*, event: dict[str, Any], semantic: dict[str, Any]) -> str:
        if semantic.get("status") == "accepted":
            return {
                "supportive": "supportive",
                "caution": "caution",
                "neutral": "neutral",
                "uncertain": "neutral",
            }.get(str(semantic.get("direction")), "neutral")
        return {
            "positive": "supportive",
            "negative": "caution",
            "caution": "caution",
            "neutral": "neutral",
            "mixed": "neutral",
        }.get(str(event.get("impact_direction")), "neutral")

    @staticmethod
    def _default_severity(event: dict[str, Any]) -> int:
        if event.get("event_type") == "fund_announcement":
            return 1
        return 2 if event.get("impact_direction") in {"negative", "caution"} else 1

    @staticmethod
    def _deduplicate_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """每个事件簇只保留代表报道，避免多家转载重复放大关注度。"""

        selected: dict[str, dict[str, Any]] = {}
        source_rank = {"A": 4, "B": 3, "C": 2, "D": 1}
        for event in events:
            cluster = event.get("cluster") or {}
            key = str(cluster.get("cluster_id") or event["event_id"])
            current = selected.get(key)
            rank = (
                bool(cluster.get("is_representative")),
                (event.get("semantic_analysis") or {}).get("status") == "accepted",
                source_rank.get(str(event.get("source_level")), 0),
                str(event.get("event_date") or ""),
            )
            if current is None:
                selected[key] = event
                continue
            current_cluster = current.get("cluster") or {}
            current_rank = (
                bool(current_cluster.get("is_representative")),
                (current.get("semantic_analysis") or {}).get("status") == "accepted",
                source_rank.get(str(current.get("source_level")), 0),
                str(current.get("event_date") or ""),
            )
            if rank > current_rank:
                selected[key] = event
        return list(selected.values())

    @staticmethod
    def _quality_status(
        *,
        exposure_status: str,
        classified_exposure: float,
        matched_count: int,
    ) -> str:
        if matched_count == 0:
            return "no_matches"
        if exposure_status in {"unavailable", "limited"} or classified_exposure <= 0:
            return "limited"
        if exposure_status == "complete":
            return "good"
        return "partial"


portfolio_event_impact_service = PortfolioEventImpactService()
