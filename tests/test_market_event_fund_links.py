"""市场事件与动态基金目录关联的确定性测试。

这些测试不访问网络、不调用大模型，重点验证半导体同义词可以命中，同时极小尾部
持仓不会把其他主题基金错误关联到半导体事件。
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from app.database import Database
from app.services.market_event_fund_links import MarketEventFundLinkService
from app.services.market_events import MarketEventRepository, MarketEventService


class FakeSectorService:
    """提供一组最小半导体同义词，替代正式版本化行业文件。"""

    def keyword_groups(self) -> list[list[str]]:
        return [["半导体", "芯片", "集成电路", "晶圆制造"]]

    def vocabulary(self) -> list[str]:
        return ["晶圆制造", "集成电路", "半导体", "芯片"]

    def get(self, *, security_code: str, security_name: str = ""):
        if security_code not in {"688981", "603986"}:
            return None
        return {
            "security_code": security_code,
            "security_name": security_name,
            "sector_name": "半导体",
            "aliases": ["半导体", "芯片", "集成电路"],
        }


def semiconductor_event() -> dict:
    return {
        "event_id": "evt-semiconductor",
        "event_date": "2026-08-28",
        "published_date": "2026-08-28",
        "event_type": "policy",
        "title": "集成电路关键核心技术攻关取得进展",
        "summary": "主要晶圆代工企业产能利用率保持较高水平。",
        "impact_direction": "neutral",
        "impact_horizon": "short_term",
        "impact_reason": "等待独立语义分析。",
        "affected_fund_codes": [],
        "tags": ["集成电路"],
        "source_name": "测试官方来源",
        "source_level": "A",
        "source_grade_reason": "发布主体官方来源",
        "source_url": "https://example.com/semiconductor",
        "content_hash": "sha256:" + "8" * 64,
    }


class MarketEventFundLinkServiceTests(unittest.TestCase):
    def test_fund_name_theme_expands_to_synonyms(self) -> None:
        """基金名写“半导体”，事件只写“集成电路”时仍应建立关联。"""

        service = MarketEventFundLinkService(
            fund_profile_loader=lambda: [
                {
                    "fund_code": "008888",
                    "short_name": "华夏国证半导体芯片ETF联接C",
                    "full_name": "华夏国证半导体芯片指数基金",
                    "fund_type": "股票型-标准指数",
                }
            ],
            disclosure_loader=lambda _codes: {"008888": {}},
            sector_service=FakeSectorService(),
        )

        linked = service.link_events([semiconductor_event()])[0]

        self.assertEqual(linked["affected_fund_codes"], ["008888"])
        self.assertIn(
            "集成电路",
            service.model_fund_contexts()[0]["theme_keywords"],
        )

    def test_tiny_tail_holding_does_not_change_fund_theme(self) -> None:
        """白酒基金只有 0.12% 半导体尾部持仓时，不应被当成半导体基金。"""

        service = MarketEventFundLinkService(
            fund_profile_loader=lambda: [
                {
                    "fund_code": "161725",
                    "short_name": "招商中证白酒指数A",
                    "fund_type": "指数型-股票",
                }
            ],
            disclosure_loader=lambda _codes: {
                "161725": {
                    "security_holdings": [
                        {
                            "security_code": "688981",
                            "security_name": "中芯国际",
                            "weight_pct": 0.12,
                        }
                    ],
                    "industry_allocations": [],
                }
            },
            sector_service=FakeSectorService(),
        )

        linked = service.link_events([semiconductor_event()])[0]

        self.assertEqual(linked["affected_fund_codes"], [])

    def test_aggregated_meaningful_holdings_can_create_theme_link(self) -> None:
        """主动基金没有主题名时，两只合计超过阈值的半导体重仓也能形成关联。"""

        service = MarketEventFundLinkService(
            fund_profile_loader=lambda: [
                {
                    "fund_code": "000001",
                    "short_name": "示例成长混合",
                    "fund_type": "混合型",
                }
            ],
            disclosure_loader=lambda _codes: {
                "000001": {
                    "security_holdings": [
                        {
                            "security_code": "688981",
                            "security_name": "中芯国际",
                            "weight_pct": 0.6,
                        },
                        {
                            "security_code": "603986",
                            "security_name": "兆易创新",
                            "weight_pct": 0.6,
                        },
                    ],
                    "industry_allocations": [],
                }
            },
            sector_service=FakeSectorService(),
        )

        linked = service.link_events([semiconductor_event()])[0]

        self.assertEqual(linked["affected_fund_codes"], ["000001"])

    def test_market_event_search_filters_with_dynamic_link(self) -> None:
        """HTTP Service 应先补动态关联，再执行 fund_code 筛选。"""

        linker = MarketEventFundLinkService(
            fund_profile_loader=lambda: [
                {
                    "fund_code": "008888",
                    "short_name": "华夏国证半导体芯片ETF联接C",
                    "fund_type": "股票型-标准指数",
                }
            ],
            disclosure_loader=lambda _codes: {"008888": {}},
            sector_service=FakeSectorService(),
        )
        with TemporaryDirectory() as directory:
            database = Database(f"sqlite:///{Path(directory) / 'events.db'}")
            repository = MarketEventRepository(database)
            repository.upsert(
                events=[semiconductor_event()],
                fetched_at=datetime(2026, 8, 28, tzinfo=timezone.utc),
            )
            try:
                result = MarketEventService(
                    fixture_path=Path("/fixture/not-used.json"),
                    repository=repository,
                    fund_link_service=linker,
                ).search(fund_code="008888")
            finally:
                database.engine.dispose()

        self.assertEqual(result["total_matched"], 1)
        self.assertEqual(result["events"][0]["affected_fund_codes"], ["008888"])


if __name__ == "__main__":
    unittest.main()
