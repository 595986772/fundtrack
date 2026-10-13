"""ETF 份额和龙虎榜机构席位资金线索的确定性测试。

测试使用临时 SQLite 和 Fake Provider，不访问真实财经网站，也不调用大模型。这样可以
精确检查 1/5/20 日计算、重复同步和基金持仓关联，而不会被盘中数据变化干扰。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import time
import unittest
from unittest.mock import patch

import pandas as pd

from app.database import Database
from app.services.background_jobs import BackgroundJobService
from app.services.large_capital_flows import (
    AkshareLargeCapitalProvider,
    EtfCapitalSnapshot,
    InstitutionalTrade,
    LargeCapitalAnalysisService,
    LargeCapitalBatch,
    LargeCapitalRepository,
    LargeCapitalSyncService,
    classify_capital_topic,
)


class FakeLargeCapitalProvider:
    """记录 Service 是否只在首次同步请求历史数据。"""

    def __init__(self, batch: LargeCapitalBatch) -> None:
        self.batch = batch
        self.include_history_calls: list[bool] = []

    def fetch(self, *, as_of=None, include_history: bool = False) -> LargeCapitalBatch:
        self.include_history_calls.append(include_history)
        return self.batch


class FakeFunds:
    def get_fund(self, fund_code: str) -> dict:
        return {
            "profile": {
                "fund_code": fund_code,
                "full_name": "半导体主题测试证券投资基金",
                "short_name": "半导体主题测试基金",
                "fund_type": "股票型",
                "tracking_target": "半导体产业指数",
                # 这些字段故意包含“银行”，验证它们不会污染实际行业暴露。
                "custodian": "测试银行",
                "benchmark": "半导体指数×95%+银行存款利率×5%",
            }
        }


class FakeDisclosures:
    def get_fund(self, *, fund_code: str) -> dict:
        return {
            "latest_report_date": "2026-06-30",
            "security_holdings": [
                {
                    "asset_type": "stock",
                    "security_code": "688981",
                    "security_name": "中芯国际",
                    "weight_pct": 8.0,
                    "report_date": "2026-06-30",
                }
            ],
        }


class FakePortfolioExposure:
    def analyze(self, *, user_id: str, portfolio_id: str) -> dict:
        return {
            "portfolio_name": "科技成长组合",
            "latest_report_date": "2026-06-30",
            "industry_exposures": [{"industry_name": "半导体"}],
            "security_exposures": [
                {
                    "asset_type": "stock",
                    "security_code": "688981",
                    "security_name": "中芯国际",
                    "portfolio_exposure_pct": 4.0,
                    "evidence_id": "portfolio-holding:688981",
                }
            ],
        }


class FakeSectors:
    def get(self, *, security_code: str, security_name: str) -> dict:
        return {
            "sector_name": "半导体",
            "aliases": ["芯片", "集成电路"],
        }


def build_batch() -> LargeCapitalBatch:
    """构造 21 个日期，使 1/5/20 日三个窗口都有可比的起止点。"""

    start = date(2026, 8, 3)
    snapshots: list[EtfCapitalSnapshot] = []
    for offset in range(21):
        data_date = start + timedelta(days=offset)
        snapshots.extend(
            [
                EtfCapitalSnapshot(
                    etf_code="510300",
                    etf_name="沪深300ETF",
                    exchange="SSE",
                    topic_code="csi300",
                    topic_label="沪深300",
                    topic_kind="broad",
                    data_date=data_date,
                    unit_price=4.0,
                    price_basis="latest_proxy" if offset < 20 else "latest_spot",
                    shares=1_000_000_000 + offset * 10_000_000,
                    estimated_scale_cny=(1_000_000_000 + offset * 10_000_000) * 4.0,
                    turnover_cny=500_000_000,
                    provider_main_net_inflow_cny=20_000_000,
                    source_level="A" if offset < 20 else "B",
                    source="Fake ETF source",
                    source_url="https://example.com/etf",
                ),
                EtfCapitalSnapshot(
                    etf_code="512800",
                    etf_name="银行ETF",
                    exchange="SSE",
                    topic_code="finance",
                    topic_label="大金融",
                    topic_kind="sector",
                    data_date=data_date,
                    unit_price=1.2,
                    price_basis="latest_proxy" if offset < 20 else "latest_spot",
                    shares=500_000_000,
                    estimated_scale_cny=600_000_000,
                    turnover_cny=100_000_000,
                    provider_main_net_inflow_cny=0,
                    source_level="A" if offset < 20 else "B",
                    source="Fake ETF source",
                    source_url="https://example.com/etf",
                ),
                EtfCapitalSnapshot(
                    etf_code="512480",
                    etf_name="半导体ETF",
                    exchange="SSE",
                    topic_code="semiconductor",
                    topic_label="半导体",
                    topic_kind="sector",
                    data_date=data_date,
                    unit_price=1.0,
                    price_basis="latest_proxy" if offset < 20 else "latest_spot",
                    shares=2_000_000_000 - offset * 8_000_000,
                    estimated_scale_cny=2_000_000_000 - offset * 8_000_000,
                    turnover_cny=300_000_000,
                    provider_main_net_inflow_cny=-10_000_000,
                    source_level="A" if offset < 20 else "B",
                    source="Fake ETF source",
                    source_url="https://example.com/etf",
                ),
            ]
        )

    trades = (
        InstitutionalTrade(
            stock_code="688981",
            stock_name="中芯国际",
            trade_date=date(2026, 8, 21),
            close=100.0,
            daily_return_pct=5.0,
            buyer_count=4,
            seller_count=1,
            buy_cny=180_000_000,
            sell_cny=30_000_000,
            net_buy_cny=150_000_000,
            market_turnover_cny=2_000_000_000,
            net_buy_ratio_pct=7.5,
            turnover_rate_pct=2.0,
            listing_reason="日涨幅偏离值达到公开条件",
        ),
        InstitutionalTrade(
            stock_code="600519",
            stock_name="贵州茅台",
            trade_date=date(2026, 8, 22),
            close=1500.0,
            daily_return_pct=-3.0,
            buyer_count=1,
            seller_count=3,
            buy_cny=20_000_000,
            sell_cny=70_000_000,
            net_buy_cny=-50_000_000,
            market_turnover_cny=3_000_000_000,
            net_buy_ratio_pct=-1.67,
            turnover_rate_pct=1.0,
            listing_reason="日跌幅偏离值达到公开条件",
        ),
    )
    return LargeCapitalBatch(
        etf_snapshots=tuple(snapshots),
        institutional_trades=trades,
    )


class LargeCapitalFlowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = TemporaryDirectory()
        path = Path(self.temporary_directory.name) / "large-capital.db"
        self.database = Database(f"sqlite:///{path}")
        self.repository = LargeCapitalRepository(self.database)
        self.analysis = LargeCapitalAnalysisService(
            repository=self.repository,
            funds=FakeFunds(),
            disclosures=FakeDisclosures(),
            portfolio_exposure=FakePortfolioExposure(),
            sectors=FakeSectors(),
        )

    def tearDown(self) -> None:
        self.database.engine.dispose()
        self.temporary_directory.cleanup()

    def test_sync_is_idempotent_and_bootstraps_history_once(self) -> None:
        provider = FakeLargeCapitalProvider(build_batch())
        sync = LargeCapitalSyncService(
            repository=self.repository,
            provider=provider,
            analysis=self.analysis,
        )

        first = sync.sync()
        second = sync.sync()
        status = sync.status()

        self.assertEqual(provider.include_history_calls, [True, False])
        self.assertEqual(first["status"], "completed")
        self.assertEqual(second["status"], "completed")
        self.assertRegex(first["data_date"], r"^\d{4}-\d{2}-\d{2}$")
        # 同步结果必须能原样写入 BackgroundJob.result 的 JSON 列。
        self.assertIsInstance(json.dumps(first), str)
        self.assertEqual(status["etf_history_days"], 21)
        # 两次 merge 后仍是 3 只 ETF × 21 天，不会重复膨胀。
        self.assertEqual(status["etf_snapshot_count"], 63)
        self.assertEqual(status["institutional_trade_count"], 2)

    def test_analysis_returns_window_flows_and_exposure_links(self) -> None:
        self.repository.save(
            batch=build_batch(),
            fetched_at=datetime(2026, 8, 24, tzinfo=timezone.utc),
        )

        result = self.analysis.overview(
            user_id="chen",
            fund_code="161725",
            portfolio_id="portfolio-test",
        )
        by_topic = {item["topic_code"]: item for item in result["topic_signals"]}

        self.assertEqual(by_topic["csi300"]["direction"], "inflow")
        self.assertGreater(by_topic["csi300"]["flow_20d_cny"], 0)
        self.assertEqual(by_topic["semiconductor"]["direction"], "outflow")
        self.assertLess(by_topic["semiconductor"]["flow_5d_cny"], 0)
        self.assertEqual(result["institutional_signal"]["direction"], "inflow")
        self.assertEqual(len(result["exposure_links"]), 2)
        fund_topics = {
            item["topic_code"]
            for item in result["exposure_links"][0]["matched_topics"]
        }
        self.assertIn("semiconductor", fund_topics)
        self.assertNotIn("finance", fund_topics)
        self.assertEqual(
            result["exposure_links"][0]["direct_institutional_securities"][0][
                "security_code"
            ],
            "688981",
        )
        self.assertIn("身份不可见", result["identity_boundary"]["explanation"])

    def test_topic_classification_does_not_confuse_numeric_etf_names(self) -> None:
        self.assertEqual(classify_capital_topic("科创50ETF华夏").code, "star50")
        self.assertEqual(classify_capital_topic("沪深300医药ETF").code, "medicine")
        self.assertEqual(classify_capital_topic("上证50ETF").code, "sse50")

    def test_overlapping_lhb_reasons_keep_single_day_like_record(self) -> None:
        """同一股票同日多种上榜原因不能被机构汇总重复计算。"""

        frame = pd.DataFrame(
            [
                {
                    "代码": "688981",
                    "名称": "中芯国际",
                    "买方机构数": 2,
                    "卖方机构数": 1,
                    "机构买入总额": 120.0,
                    "机构卖出总额": 20.0,
                    "机构买入净额": 100.0,
                    "市场总成交额": 1_000.0,
                    "上榜日期": "2026-08-24",
                    "上榜原因": "日涨幅偏离值达到公开条件",
                },
                {
                    "代码": "688981",
                    "名称": "中芯国际",
                    "买方机构数": 4,
                    "卖方机构数": 3,
                    "机构买入总额": 300.0,
                    "机构卖出总额": 100.0,
                    "机构买入净额": 200.0,
                    "市场总成交额": 3_000.0,
                    "上榜日期": "2026-08-24",
                    "上榜原因": "连续三日涨幅偏离值达到公开条件",
                },
            ]
        )

        rows = AkshareLargeCapitalProvider()._normalize_institutional_trades(frame)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].net_buy_cny, 100.0)
        self.assertIn("日涨幅", rows[0].listing_reason)

    def test_background_job_runs_large_capital_sync(self) -> None:
        """前端同步按钮提交的 Job 会真正分发到资金同步 Service。"""

        jobs = BackgroundJobService()
        fake_result = {
            "workflow": "large_capital_sync",
            "summary": "大资金线索已更新。",
            "data_date": "2026-08-24",
        }
        try:
            with patch(
                "app.services.large_capital_flows.large_capital_sync_service.sync",
                return_value=fake_result,
            ) as sync_mock:
                jobs.start_job(
                    user_id="large-capital-test-user",
                    job_id="large-capital-sync-test",
                    title="同步大资金线索",
                    job_type="large_capital_sync",
                )
                deadline = time.monotonic() + 2.0
                while time.monotonic() < deadline:
                    job = jobs.get_job(
                        user_id="large-capital-test-user",
                        job_id="large-capital-sync-test",
                    )["job"]
                    if job["status"] in {"completed", "failed", "cancelled"}:
                        break
                    time.sleep(0.01)

            self.assertEqual(job["status"], "completed")
            self.assertEqual(job["result"]["workflow"], "large_capital_sync")
            sync_mock.assert_called_once_with()
        finally:
            jobs.shutdown(wait=True)


if __name__ == "__main__":
    unittest.main()
