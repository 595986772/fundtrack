"""市场事件增量同步管线的确定性测试。

测试使用临时 SQLite、Fake Provider 和模拟 AKShare DataFrame，不访问真实网站，也不调用模型。
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import time
import unittest
from unittest.mock import patch

import pandas as pd

from app.database import Database
from app.services.background_jobs import BackgroundJobService
from app.services.cron_scheduler import CronSchedulerService
from app.services.market_event_sync import (
    AkshareMarketEventProvider,
    CompositeMarketEventProvider,
    MarketEventBatch,
    MarketEventSyncError,
    MarketEventSyncService,
    OfficialWebsiteMarketEventProvider,
)
from app.services.market_events import MarketEventRepository, MarketEventService


def sample_event(
    *,
    event_id: str = "evt-live-one",
    content_hash: str = "sha256:" + "1" * 64,
) -> dict:
    """生成 Repository 和 Service 测试共用的一条规范化事件。"""

    return {
        "event_id": event_id,
        "event_date": "2026-08-27",
        "published_date": "2026-08-27",
        "event_type": "policy",
        "title": "财政部门发布测试政策",
        "summary": "这是一条用于测试数据库事件管线的摘要。",
        "impact_direction": "neutral",
        "impact_horizon": "short_term",
        "impact_reason": "尚未进行模型语义复核。",
        "affected_fund_codes": ["110020"],
        "tags": ["财政", "政策"],
        "source_name": "测试官方来源",
        "source_level": "A",
        "source_grade_reason": "发布主体官方来源",
        "source_url": "https://example.com/event",
        "content_hash": content_hash,
    }


class FakeMarketEventProvider:
    """返回一条固定事件，让同步 Service 测试不依赖网络。"""

    source = "Fake Event Provider"

    def fetch_events(self, *, since: date, until: date) -> MarketEventBatch:
        return MarketEventBatch(
            events=(sample_event(),),
            successful_sources=(self.source,),
            source_errors=(),
        )


class FakeFundLinkService:
    """模拟动态关联层，验证 Sync 会在入库前使用它。"""

    def available_fund_codes(self) -> list[str]:
        return ["008888"]

    def link_events(self, events: list[dict]) -> list[dict]:
        return [
            {**event, "affected_fund_codes": ["008888"]}
            for event in events
        ]


class MarketEventRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = TemporaryDirectory()
        path = Path(self.temporary_directory.name) / "events.db"
        self.database = Database(f"sqlite:///{path}")
        self.repository = MarketEventRepository(self.database)

    def tearDown(self) -> None:
        self.database.engine.dispose()
        self.temporary_directory.cleanup()

    def test_content_hash_deduplicates_different_event_ids(self) -> None:
        duplicate = sample_event(event_id="evt-live-duplicate")
        result = self.repository.upsert(
            events=[sample_event(), duplicate],
            fetched_at=datetime(2026, 8, 27, tzinfo=timezone.utc),
        )

        self.assertEqual(result["inserted_count"], 1)
        self.assertEqual(result["deduplicated_count"], 1)
        self.assertEqual(self.repository.status()["record_count"], 1)

    def test_service_prefers_database_and_sync_is_idempotent(self) -> None:
        service = MarketEventSyncService(
            repository=self.repository,
            provider=FakeMarketEventProvider(),
        )
        first = service.sync()
        second = service.sync()
        result = MarketEventService(
            fixture_path=Path("/fixture/should/not/be/read.json"),
            repository=self.repository,
        ).search()

        self.assertEqual(first["inserted_count"], 1)
        self.assertEqual(second["updated_count"], 1)
        self.assertEqual(result["data_mode"], "database")
        self.assertEqual(result["data_kind"], "database_market_events")
        self.assertEqual(result["events"][0]["event_id"], "evt-live-one")

    def test_sync_persists_dynamic_fund_links_before_repository_write(self) -> None:
        service = MarketEventSyncService(
            repository=self.repository,
            provider=FakeMarketEventProvider(),
            fund_link_service=FakeFundLinkService(),
        )

        result = service.sync()
        stored = self.repository.load()["events"][0]

        self.assertEqual(stored["affected_fund_codes"], ["008888"])
        self.assertEqual(result["linked_event_count"], 1)
        self.assertEqual(result["linked_fund_count"], 1)

    def test_daily_schedule_is_durable_and_idempotent(self) -> None:
        background_jobs = BackgroundJobService()
        scheduler = CronSchedulerService(
            root=Path(self.temporary_directory.name) / "schedules",
            background_jobs=background_jobs,
        )
        service = MarketEventSyncService(
            repository=self.repository,
            provider=FakeMarketEventProvider(),
        )
        try:
            first = service.ensure_daily_schedule(scheduler)
            second = service.ensure_daily_schedule(scheduler)
            schedules = scheduler.list_schedules(user_id="fundscope-system")

            self.assertTrue(first["created"])
            self.assertFalse(second["created"])
            self.assertEqual(schedules["count"], 1)
            self.assertEqual(schedules["schedules"][0]["job_type"], "market_event_sync")
        finally:
            scheduler.stop(wait=True)
            background_jobs.shutdown(wait=True)


class AkshareMarketEventProviderTests(unittest.TestCase):
    def test_uses_current_imported_fund_codes_when_sync_starts(self) -> None:
        provider = AkshareMarketEventProvider(
            fund_code_loader=lambda: ["008888", "008888", "110020"]
        )

        self.assertEqual(provider._current_fund_codes(), ("008888", "110020"))

    def test_normalizes_fund_announcement_and_cctv_news(self) -> None:
        """真实适配器只能归类和映射，不应自行制造利好利空。"""

        columns = ["基金代码", "公告标题", "基金名称", "公告日期", "报告ID"]
        report_frame = pd.DataFrame(
            [[
                "110020",
                "易方达沪深300ETF联接基金2026年第2季度报告",
                "易方达沪深300ETF联接A",
                date(2026, 7, 21),
                "AN202607211827165072",
            ]],
            columns=columns,
        )
        empty_announcement_frame = pd.DataFrame(columns=columns)
        empty_eastmoney_frame = pd.DataFrame(
            columns=["标题", "摘要", "发布时间", "链接"]
        )
        empty_cls_frame = pd.DataFrame(
            columns=["标题", "内容", "发布日期", "发布时间"]
        )

        def fake_cctv(*, date: str):
            if date != "20260827":
                return pd.DataFrame(columns=["date", "title", "content"])
            return pd.DataFrame(
                [[date, "前7个月消费数据同比增长", "有关部门介绍财政投资和经济增长情况。"]],
                columns=["date", "title", "content"],
            )

        with (
            patch("akshare.fund_announcement_report_em", return_value=report_frame),
            patch(
                "akshare.fund_announcement_dividend_em",
                return_value=empty_announcement_frame,
            ),
            patch(
                "akshare.fund_announcement_personnel_em",
                return_value=empty_announcement_frame,
            ),
            patch("akshare.news_cctv", side_effect=fake_cctv),
            patch("akshare.stock_info_global_em", return_value=empty_eastmoney_frame),
            patch("akshare.stock_info_global_cls", return_value=empty_cls_frame),
        ):
            batch = AkshareMarketEventProvider(fund_codes=("110020",)).fetch_events(
                since=date(2026, 5, 1),
                until=date(2026, 8, 27),
            )

        self.assertEqual(len(batch.events), 2)
        self.assertEqual(
            {event["event_type"] for event in batch.events},
            {"fund_announcement", "economic_data"},
        )
        self.assertTrue(all(event["impact_direction"] == "neutral" for event in batch.events))
        self.assertTrue(all(event["content_hash"].startswith("sha256:") for event in batch.events))

    def test_normalizes_eastmoney_and_cls_as_separate_media_reports(self) -> None:
        """同一消息的两家媒体记录必须都保留，后续由语义聚类而非哈希去重处理。"""

        empty_fund = pd.DataFrame(
            columns=["基金代码", "公告标题", "基金名称", "公告日期", "报告ID"]
        )
        empty_cctv = pd.DataFrame(columns=["date", "title", "content"])
        eastmoney = pd.DataFrame(
            [
                [
                    "央行开展逆回购操作",
                    "【央行开展逆回购操作】央行今日开展公开市场逆回购操作。",
                    "2026-08-27 09:30:00",
                    "https://finance.eastmoney.com/example.html",
                ],
                [
                    "德方纳米：预计产品保持高速增长",
                    "这是一条普通上市公司经营快讯，不属于基金市场宏观事件。",
                    "2026-08-27 09:35:00",
                    "https://finance.eastmoney.com/company-example.html",
                ],
                [
                    "世纪华通蝉联A股游戏利润王",
                    "标题虽然出现 A 股，但内容仍然只描述一家公司的经营情况。",
                    "2026-08-27 09:40:00",
                    "https://finance.eastmoney.com/company-a-share.html",
                ],
            ],
            columns=["标题", "摘要", "发布时间", "链接"],
        )
        cls = pd.DataFrame(
            [[
                "央行今日开展逆回购操作",
                "财联社电，央行今日开展公开市场逆回购操作。",
                "2026-08-27",
                "09:31:00",
            ]],
            columns=["标题", "内容", "发布日期", "发布时间"],
        )

        with (
            patch("akshare.fund_announcement_report_em", return_value=empty_fund),
            patch("akshare.fund_announcement_dividend_em", return_value=empty_fund),
            patch("akshare.fund_announcement_personnel_em", return_value=empty_fund),
            patch("akshare.news_cctv", return_value=empty_cctv),
            patch("akshare.stock_info_global_em", return_value=eastmoney),
            patch("akshare.stock_info_global_cls", return_value=cls),
        ):
            batch = AkshareMarketEventProvider(fund_codes=("110020",)).fetch_events(
                since=date(2026, 8, 27),
                until=date(2026, 8, 27),
            )

        self.assertEqual(len(batch.events), 2)
        self.assertEqual(
            {item["source_name"] for item in batch.events},
            {"东方财富财经", "财联社电报"},
        )
        self.assertTrue(all(item["source_level"] == "C" for item in batch.events))
        self.assertEqual(len({item["content_hash"] for item in batch.events}), 2)
        self.assertNotIn("德方纳米", " ".join(item["title"] for item in batch.events))
        self.assertNotIn("世纪华通", " ".join(item["title"] for item in batch.events))

    def test_topic_keywords_include_sector_news_before_database_write(self) -> None:
        """板块词应绕过宽泛宏观门槛，但仍排除没有命中板块的普通公司新闻。"""

        empty_fund = pd.DataFrame(
            columns=["基金代码", "公告标题", "基金名称", "公告日期", "报告ID"]
        )
        empty_cctv = pd.DataFrame(columns=["date", "title", "content"])
        eastmoney = pd.DataFrame(
            [
                [
                    "先进制程扩产带动晶圆设备需求",
                    "国内半导体设备产业链发布新的产能规划。",
                    "2026-08-27 09:30:00",
                    "https://finance.eastmoney.com/semiconductor.html",
                ],
                [
                    "某公司公布门店装修计划",
                    "普通经营信息，与目标板块无关。",
                    "2026-08-27 09:35:00",
                    "https://finance.eastmoney.com/unrelated.html",
                ],
            ],
            columns=["标题", "摘要", "发布时间", "链接"],
        )
        empty_cls = pd.DataFrame(
            columns=["标题", "内容", "发布日期", "发布时间"]
        )

        with (
            patch("akshare.fund_announcement_report_em", return_value=empty_fund),
            patch("akshare.fund_announcement_dividend_em", return_value=empty_fund),
            patch("akshare.fund_announcement_personnel_em", return_value=empty_fund),
            patch("akshare.news_cctv", return_value=empty_cctv),
            patch("akshare.stock_info_global_em", return_value=eastmoney),
            patch("akshare.stock_info_global_cls", return_value=empty_cls),
        ):
            batch = AkshareMarketEventProvider(fund_codes=("110020",)).fetch_events(
                since=date(2026, 8, 27),
                until=date(2026, 8, 27),
                keywords=("半导体", "晶圆", "先进制程"),
            )

        self.assertEqual(len(batch.events), 1)
        self.assertIn("晶圆", batch.events[0]["tags"])
        self.assertEqual(batch.events[0]["source_name"], "东方财富财经")


class FakeHttpResponse:
    def __init__(self, text: str) -> None:
        self.text = text

    def raise_for_status(self) -> None:
        return None


class FakeHttpClient:
    """按 URL 返回固定 HTML，让官方网站解析测试完全离线。"""

    def __init__(self, pages: dict[str, str]) -> None:
        self.pages = pages

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def get(self, url: str) -> FakeHttpResponse:
        if url not in self.pages:
            raise RuntimeError(f"unexpected url: {url}")
        return FakeHttpResponse(self.pages[url])


class OfficialWebsiteMarketEventProviderTests(unittest.TestCase):
    def test_parses_ndrc_and_csrc_as_level_a_sources(self) -> None:
        ndrc_article_url = (
            "https://www.ndrc.gov.cn/xwdt/xwfb/202608/t20260827_1.html"
        )
        csrc_article_url = (
            "https://www.csrc.gov.cn/csrc/c100028/c123/content.shtml"
        )
        pages = {
            OfficialWebsiteMarketEventProvider.ndrc_list_url: f"""
                <ul class="u-list"><li>
                  <a href="./202608/t20260827_1.html" title="发改委部署扩大消费政策">政策</a>
                  <span>2026/08/27</span>
                </li></ul>
            """,
            ndrc_article_url: """
                <meta name="ArticleTitle" content="发改委部署扩大消费政策">
                <div class="TRS_Editor"><p>有关部门将进一步扩大消费并促进投资。</p></div>
            """,
            OfficialWebsiteMarketEventProvider.csrc_list_url: """
                <div class="szyw-lists"><a href="/csrc/c100028/c123/content.shtml">证监会发布监管规则</a></div>
            """,
            csrc_article_url: """
                <meta name="ArticleTitle" content="证监会发布监管规则">
                <meta name="PubDate" content="2026-08-27 10:00:00">
                <meta name="Description" content="规则用于完善资本市场监管。">
            """,
        }
        provider = OfficialWebsiteMarketEventProvider(
            client_factory=lambda: FakeHttpClient(pages)
        )

        batch = provider.fetch_events(
            since=date(2026, 8, 27),
            until=date(2026, 8, 27),
        )

        self.assertEqual(len(batch.events), 2)
        self.assertEqual(
            {item["source_name"] for item in batch.events},
            {"国家发展改革委", "中国证监会"},
        )
        self.assertTrue(all(item["source_level"] == "A" for item in batch.events))
        self.assertTrue(all(item["impact_direction"] == "neutral" for item in batch.events))


class FailingMarketEventProvider:
    source = "Failing Provider"

    def fetch_events(self, *, since: date, until: date) -> MarketEventBatch:
        raise MarketEventSyncError("temporary unavailable")


class CompositeMarketEventProviderTests(unittest.TestCase):
    def test_partial_source_failure_keeps_successful_provider_result(self) -> None:
        provider = CompositeMarketEventProvider(
            (FailingMarketEventProvider(), FakeMarketEventProvider())
        )

        batch = provider.fetch_events(
            since=date(2026, 8, 1),
            until=date(2026, 8, 27),
        )

        self.assertEqual(len(batch.events), 1)
        self.assertIn("Fake Event Provider", batch.successful_sources)
        self.assertTrue(any("temporary unavailable" in error for error in batch.source_errors))


class MarketEventSyncJobTests(unittest.TestCase):
    def test_background_job_runs_sync_without_agent_loop(self) -> None:
        service = BackgroundJobService()
        fake_result = {
            "summary": "市场事件同步完成：采集 2 条，新增 2 条。",
            "workflow": "market_event_sync",
        }
        try:
            with (
                patch(
                    "app.services.market_event_sync.market_event_sync_service.sync",
                    return_value=fake_result,
                ) as sync_mock,
                patch(
                    "app.services.market_event_clusters.market_event_cluster_service.build",
                    return_value={
                        "summary": "2 条报道已归并为 1 个事件簇。",
                        "workflow": "market_event_clustering",
                    },
                ) as cluster_mock,
            ):
                service.start_job(
                    user_id="chen",
                    job_id="market-event-sync-test",
                    title="同步市场事件",
                    job_type="market_event_sync",
                )
                deadline = time.monotonic() + 2.0
                while time.monotonic() < deadline:
                    job = service.get_job(
                        user_id="chen",
                        job_id="market-event-sync-test",
                    )["job"]
                    if job["status"] in {"completed", "failed", "cancelled"}:
                        break
                    time.sleep(0.01)

            self.assertEqual(job["status"], "completed")
            self.assertEqual(job["result"]["workflow"], "market_event_sync")
            self.assertEqual(
                job["result"]["clustering"]["workflow"],
                "market_event_clustering",
            )
            sync_mock.assert_called_once_with()
            cluster_mock.assert_called_once_with()
        finally:
            service.shutdown(wait=True)


if __name__ == "__main__":
    unittest.main()
