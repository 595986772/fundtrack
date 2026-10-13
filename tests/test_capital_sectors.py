"""验证板块统计的口径，不访问财经站点、不调用模型、不写用户数据库。"""

from dataclasses import replace
from datetime import date, datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import json
import unittest

from app.database import Database
from app.services.capital_sector_analysis import build_sector_analysis
from app.services.large_capital_flows import (
    CAPITAL_TOPICS, LargeCapitalAnalysisService, LargeCapitalBatch, LargeCapitalRepository,
)
from app.services.market_agent_tools import LargeCapitalToolInput, _bounded_large_capital_result
from app.services.evidence import extract_tool_evidence_ids
from tests.test_large_capital_flows import (
    FakeDisclosures, FakeFunds, FakePortfolioExposure, FakeSectors, build_batch,
)


class CapitalSectorTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.database = Database(f"sqlite:///{Path(self.directory.name) / 'sectors.db'}")
        self.repository = LargeCapitalRepository(self.database)
        self.analysis = LargeCapitalAnalysisService(
            repository=self.repository, funds=FakeFunds(), disclosures=FakeDisclosures(),
            portfolio_exposure=FakePortfolioExposure(), sectors=FakeSectors(),
        )

    def tearDown(self):
        self.database.engine.dispose()
        self.directory.cleanup()

    def save(self, snapshots):
        self.repository.save(
            batch=LargeCapitalBatch(tuple(snapshots), ()),
            fetched_at=datetime(2026, 8, 24, tzinfo=timezone.utc),
        )

    def test_sector_windows_exclude_broad_and_keep_context(self):
        self.save(build_batch().etf_snapshots)
        result = self.analysis.overview(fund_code="161725")
        sectors = result["sector_analysis"]
        self.assertEqual(sectors["sector_count"], 2)
        for key, amount in (("1d", -8_000_000), ("5d", -40_000_000), ("20d", -160_000_000)):
            window = sectors["windows"][key]
            rows = {item["topic_code"]: item for item in window["rows"]}
            self.assertNotIn("csi300", rows)
            self.assertEqual(rows["semiconductor"]["net_flow_cny"], amount)
            self.assertEqual(rows["semiconductor"]["exposure_links"][0]["known_exposure_pct"], 8)
            self.assertIn(key, rows["semiconductor"]["evidence_id"])
            # 零流量是持平，不应该冒充“数据不足”或“流入第一名”。
            self.assertEqual(rows["finance"]["status"], "available")
            self.assertEqual(window["neutral_count"], 1)
            self.assertIsNone(window["leaders"]["inflow"])
            self.assertEqual(window["leaders"]["outflow"]["topic_code"], "semiconductor")

    def test_only_common_etfs_contribute_and_intensity_uses_covered_scale(self):
        base = build_batch().etf_snapshots[2]
        old = replace(base, shares=100, estimated_scale_cny=100)
        new = replace(base, data_date=date(2026, 8, 4), shares=110, estimated_scale_cny=110)
        added = replace(new, etf_code="588000", shares=890, estimated_scale_cny=890)
        self.save([old, new, added])
        row = self.analysis.overview()["sector_analysis"]["windows"]["1d"]["rows"][0]
        self.assertEqual(row["net_flow_cny"], 10)
        self.assertEqual(row["coverage_pct"], 11)
        self.assertAlmostEqual(row["intensity_pct"], 10 / 110 * 100, places=4)
        self.assertEqual(row["comparable_etf_count"], 1)
        self.assertIn("未外推", row["interpretation"])

    def test_lack_of_history_never_falls_back_to_provider_order_flow(self):
        self.save([replace(build_batch().etf_snapshots[2], provider_main_net_inflow_cny=9e12)])
        for window in self.analysis.overview()["sector_analysis"]["windows"].values():
            self.assertEqual(window["available_count"], 0)
            self.assertIsNone(window["rows"][0]["net_flow_cny"])
            self.assertIsNone(window["rows"][0]["intensity_pct"])
            self.assertIsNone(window["rows"][0].get("rank"))

    def test_missing_start_date_does_not_silently_use_an_older_one(self):
        snapshots = [item for item in build_batch().etf_snapshots
                     if not (item.topic_code == "semiconductor" and item.data_date == date(2026, 8, 18))]
        self.save(snapshots)
        window = self.analysis.overview()["sector_analysis"]["windows"]["5d"]
        row = next(item for item in window["rows"] if item["topic_code"] == "semiconductor")
        self.assertEqual(row["start_date"], date(2026, 8, 18))
        self.assertIsNone(row["net_flow_cny"])

    def test_stale_topic_is_visible_but_not_ranked(self):
        snapshots = [item for item in build_batch().etf_snapshots
                     if not (item.topic_code == "semiconductor" and item.data_date == date(2026, 8, 23))]
        self.save(snapshots)
        window = self.analysis.overview()["sector_analysis"]["windows"]["5d"]
        row = window["rows"][-1]
        self.assertEqual(row["status"], "stale")
        self.assertIsNone(row.get("rank"))
        self.assertEqual(window["unavailable_count"], 1)

    def test_short_and_long_direction_difference_is_not_called_reversal(self):
        # 长窗口净赎回，最后一天小幅申购。只能说窗口分化，不能宣称趋势已反转。
        snapshots = [replace(item, shares=item.shares + 9_000_000,
                             estimated_scale_cny=item.estimated_scale_cny + 9_000_000)
                     if item.topic_code == "semiconductor" and item.data_date == date(2026, 8, 23)
                     else item for item in build_batch().etf_snapshots]
        self.save(snapshots)
        row = self.analysis.overview()["sector_analysis"]["windows"]["5d"]["rows"][-1]
        self.assertIn("短长窗口分化", row["interpretation"])
        self.assertNotIn("反转", row["interpretation"])

    def test_all_sectors_reach_agent_and_period_changes_the_numbers(self):
        # 用 9 个板块验证工具摘要没有再截断成仅剩前两个行业。
        base = [item for item in build_batch().etf_snapshots if item.topic_code == "semiconductor"]
        snapshots = [replace(item, etf_code=f"{index + 1:06d}", topic_code=topic.code,
                             topic_label=topic.label)
                     for index, topic in enumerate(t for t in CAPITAL_TOPICS if t.kind == "sector")
                     for item in base]
        self.save(snapshots)
        result = self.analysis.overview()
        one = _bounded_large_capital_result(result, 1)["sector_analysis"]
        five = _bounded_large_capital_result(result, 5)["sector_analysis"]
        self.assertEqual(len(five["rows"]), 9)
        self.assertEqual(five["period"], 5)
        five_row = dict(zip(five["columns"], five["rows"][0]["values"]))
        one_row = dict(zip(one["columns"], one["rows"][0]["values"]))
        self.assertEqual(five_row["net_flow_cny"], one_row["net_flow_cny"] * 5)
        encoded = json.dumps(_bounded_large_capital_result(result), ensure_ascii=False, separators=(",", ":"), default=str)
        self.assertLess(len(encoded), 4000)
        ids = extract_tool_evidence_ids([SimpleNamespace(tool_result=encoded)])
        self.assertIn(five["rows"][0]["evidence_id"], ids)
        self.assertNotIn("top_net_buys", _bounded_large_capital_result(result)["institutional_signal"])
        with self.assertRaises(ValueError):
            LargeCapitalToolInput(sector_period=30)

    def test_empty_structure_is_stable(self):
        result = self.analysis.overview()["sector_analysis"]
        self.assertEqual(result, build_sector_analysis([], []))
        self.assertEqual(set(result["windows"]), {"1d", "5d", "20d"})
        self.assertEqual(result["windows"]["5d"]["rows"], [])


if __name__ == "__main__":
    unittest.main()
