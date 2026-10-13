"""持仓级事件归因、REST、Agent Tool 和路由的确定性测试。"""

from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.main import app
from app.schemas import AgentChatRequest
from app.services.agent_routing import agent_request_router
from app.services.agent_tools import get_agent_tools
from app.services.portfolio_agent_tools import get_portfolio_event_impacts_tool
from app.services.portfolio_event_impacts import PortfolioEventImpactService
from app.services.security_sectors import SecuritySectorService
from app.services.tool_permissions import check_tool_permission


PORTFOLIO_ID = "portfolio_aaaaaaaaaaaaaaaa"
REPORT_DATE = "2026-06-30"


class FakeExposureReader:
    """省略基金披露抓取，只提供事件归因真正需要的穿透结果。"""

    def analyze(self, *, user_id: str, portfolio_id: str):
        securities = [
            self._security("600519", "贵州茅台", 8),
            self._security("600809", "山西汾酒", 5),
            self._security("300750", "宁德时代", 4),
        ]
        return {
            "data_kind": "deterministic_portfolio_lookthrough",
            "portfolio_id": portfolio_id,
            "user_id": user_id,
            "portfolio_name": "事件归因测试组合",
            "latest_report_date": REPORT_DATE,
            "data_quality": {
                "status": "partial",
                "security_coverage_pct": 55.0,
            },
            "security_exposures": securities,
            "industry_exposures": [
                {"industry_name": "制造业", "portfolio_exposure_pct": 40.0},
                {
                    "industry_name": "交通运输、仓储和邮政业",
                    "portfolio_exposure_pct": 3.0,
                },
            ],
            "fund_reports": [
                {
                    "fund_code": "000001",
                    "short_name": "权益基金",
                    "portfolio_weight_pct": 75.0,
                },
                {
                    "fund_code": "000002",
                    "short_name": "债券基金",
                    "portfolio_weight_pct": 25.0,
                },
            ],
        }

    @staticmethod
    def _security(code: str, name: str, exposure: float):
        return {
            "evidence_id": f"lookthrough:{code}",
            "asset_type": "stock",
            "security_code": code,
            "security_name": name,
            "portfolio_exposure_pct": exposure,
            "report_date": REPORT_DATE,
        }


class FakeEventReader:
    def search(self, *, limit: int = 20, **kwargs):
        return {
            "data_mode": "database",
            "data_cutoff": "2026-08-30",
            "events": [
                self._event(
                    "evt-direct",
                    "贵州茅台发布经营信息",
                    entities=["贵州茅台"],
                    themes=["白酒", "制造业"],
                    direction="caution",
                    severity=3,
                ),
                self._event(
                    "evt-sector-a",
                    "白酒行业需求出现改善",
                    themes=["白酒", "制造业"],
                    direction="supportive",
                    severity=2,
                    cluster_id="cluster-sector",
                    representative=True,
                ),
                # 同一 cluster 的转载不能形成第二条影响。
                self._event(
                    "evt-sector-b",
                    "食品饮料行业需求改善",
                    themes=["食品饮料"],
                    direction="supportive",
                    severity=2,
                    cluster_id="cluster-sector",
                    representative=False,
                ),
                self._event(
                    "evt-transport",
                    "交通运输主要指标增长",
                    themes=["交通运输"],
                    direction="supportive",
                    severity=1,
                ),
                self._event(
                    "evt-fund",
                    "债券基金发布季度报告",
                    event_type="fund_announcement",
                    direction="neutral",
                    severity=1,
                    affected_fund_codes=["000002"],
                ),
                # 已过期事件不会进入有效事件和匹配统计。
                self._event(
                    "evt-expired",
                    "贵州茅台历史消息",
                    entities=["贵州茅台"],
                    direction="caution",
                    severity=3,
                    freshness_status="expired",
                    decay_factor=0,
                ),
            ][:limit],
        }

    @staticmethod
    def _event(
        event_id: str,
        title: str,
        *,
        entities=None,
        themes=None,
        direction="neutral",
        severity=1,
        event_type="policy",
        affected_fund_codes=None,
        cluster_id=None,
        representative=False,
        freshness_status="fresh",
        decay_factor=1.0,
    ):
        semantic = {
            "status": "accepted",
            "entities": entities or [],
            "themes": themes or [],
            "direction": direction,
            "horizon": "short_term",
            "severity": severity,
            "relevance": "direct" if entities or affected_fund_codes else "related",
            "confidence": 0.9,
            "reason": "固定测试语义结果。",
            "affected_fund_codes": affected_fund_codes or [],
            "uncertainties": [],
        }
        cluster = None
        if cluster_id:
            cluster = {
                "cluster_id": cluster_id,
                "is_representative": representative,
            }
        return {
            "event_id": event_id,
            "event_date": "2026-08-29",
            "event_type": event_type,
            "title": title,
            "summary": title,
            "impact_direction": {
                "supportive": "positive",
                "caution": "caution",
                "neutral": "neutral",
            }[direction],
            "impact_horizon": "short_term",
            "impact_reason": "固定测试事件。",
            "affected_fund_codes": affected_fund_codes or [],
            "tags": themes or [],
            "source_name": "测试来源",
            "source_level": "A",
            "source_url": f"https://example.com/{event_id}",
            "semantic_analysis": semantic,
            "freshness_status": freshness_status,
            "decay_factor": decay_factor,
            "cluster": cluster,
        }


class PortfolioEventImpactTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = TemporaryDirectory()
        taxonomy_path = Path(self.temp_dir.name) / "sectors.json"
        taxonomy_path.write_text(
            json.dumps(
                {
                    "taxonomy_version": "test-v1",
                    "generated_at": "2026-08-30T00:00:00Z",
                    "source": "Test taxonomy",
                    "methodology": "Fixed test mapping",
                    "securities": [
                        {
                            "security_code": "600519",
                            "security_name": "贵州茅台",
                            "sector_name": "白酒",
                            "aliases": ["白酒", "食品饮料"],
                        },
                        {
                            "security_code": "600809",
                            "security_name": "山西汾酒",
                            "sector_name": "白酒",
                            "aliases": ["白酒", "食品饮料"],
                        },
                        {
                            "security_code": "300750",
                            "security_name": "宁德时代",
                            "sector_name": "电池",
                            "aliases": ["电池", "新能源车"],
                        },
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        self.service = PortfolioEventImpactService(
            exposures=FakeExposureReader(),
            events=FakeEventReader(),
            sectors=SecuritySectorService(taxonomy_path),
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_prefers_precise_match_and_deduplicates_event_clusters(self) -> None:
        result = self.service.analyze(
            user_id="chen",
            portfolio_id=PORTFOLIO_ID,
        )

        self.assertEqual(result["quality"]["active_event_count"], 4)
        self.assertEqual(result["quality"]["matched_event_count"], 4)
        self.assertEqual(result["caution_event_count"], 1)
        self.assertEqual(result["supportive_event_count"], 2)
        self.assertEqual(result["neutral_event_count"], 1)

        by_event = {item["event_id"]: item for item in result["impacts"]}
        direct = by_event["evt-direct"]
        self.assertEqual(direct["match_basis"], "direct_security")
        # 即使事件同时带“白酒、制造业”，直接点名茅台时只计算茅台 8%，不重复加行业。
        self.assertEqual(direct["affected_exposure_pct"], 8.0)

        sector = by_event["evt-sector-a"]
        self.assertEqual(sector["match_basis"], "fine_sector")
        self.assertEqual(sector["affected_exposure_pct"], 13.0)
        self.assertNotIn("evt-sector-b", by_event)

        transport = by_event["evt-transport"]
        self.assertEqual(transport["match_basis"], "broad_industry")
        self.assertEqual(transport["affected_exposure_pct"], 3.0)
        self.assertEqual(by_event["evt-fund"]["affected_exposure_pct"], 25.0)
        self.assertEqual(result["quality"]["classified_stock_exposure_pct"], 17.0)

    def test_api_tool_permission_and_route_share_the_same_capability(self) -> None:
        from app.routers import portfolios as portfolio_router
        from app.services import portfolio_agent_tools as tool_module

        with patch.object(
            portfolio_router,
            "portfolio_event_impact_service",
            self.service,
        ):
            response = TestClient(app).get(
                f"/api/v1/portfolios/{PORTFOLIO_ID}/event-impacts?user_id=chen"
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["quality"]["matched_event_count"], 4)

        with patch.object(tool_module, "portfolio_event_impact_service", self.service):
            raw = get_portfolio_event_impacts_tool.invoke(
                {"user_id": "chen", "portfolio_id": PORTFOLIO_ID, "limit": 4}
            )
        payload = json.loads(raw)
        self.assertTrue(payload["ok"])
        self.assertLess(len(raw), 10_000)
        self.assertIn(
            "get_portfolio_event_impacts_tool",
            {tool.name for tool in get_agent_tools()},
        )
        self.assertTrue(
            check_tool_permission("get_portfolio_event_impacts_tool", {}).allowed
        )

        route = agent_request_router.route(
            AgentChatRequest(
                user_id="chen",
                portfolio_id=PORTFOLIO_ID,
                message="最近有哪些新闻影响我的组合持仓？",
            )
        )
        self.assertEqual(route.name, "portfolio_event_impact")
        self.assertEqual(route.tool_names, ("get_portfolio_event_impacts_tool",))

        combined = agent_request_router.route(
            AgentChatRequest(
                user_id="chen",
                portfolio_id=PORTFOLIO_ID,
                message=(
                    "综合分析我的组合主要风险、重复持仓，以及哪些新闻影响我的持仓。"
                ),
            )
        )
        # 跨风险、穿透和事件的综合问题现在先进入研究计划与证据闸门，模型只看一个
        # 已经汇总好的 Brief，不再临场分别调用三个相互重叠的工具。
        self.assertEqual(combined.name, "portfolio_research_brief")
        self.assertEqual(
            combined.tool_names,
            ("get_portfolio_research_brief_tool",),
        )


if __name__ == "__main__":
    unittest.main()
