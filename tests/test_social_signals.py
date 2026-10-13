"""小红书社交观点采集、模型结果校验和作者多样化聚合测试。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from app.config import settings
from app.database import Database
from app.services.market_events import MarketEventRepository
from app.services.social_signal_freshness import SocialSignalFreshnessService
from app.services.social_signal_provider import XiaohongshuBrowserProvider
from app.services.social_signals import (
    ModelSocialSignalBatch,
    SocialSignalAnalysisService,
    SocialSignalAggregationService,
    SocialSignalRepository,
    SOCIAL_SIGNAL_PROMPT_VERSION,
)


class FakeLocator:
    def __init__(self, text: str = "公开搜索结果") -> None:
        self.text = text

    def inner_text(self, *, timeout: int) -> str:
        del timeout
        return self.text


class FakeBrowserPage:
    """模拟已登录浏览器渲染出的 DOM，不访问小红书或内部接口。"""

    def __init__(self, published_at: datetime) -> None:
        self.published_at = published_at
        self.urls: list[str] = []

    def goto(self, url: str, **_kwargs) -> None:
        self.urls.append(url)

    def wait_for_timeout(self, _milliseconds: int) -> None:
        return None

    def locator(self, _selector: str) -> FakeLocator:
        return FakeLocator()

    def evaluate(self, script: str):
        if script.startswith("window.scrollBy"):
            return None
        if "const anchors" in script:
            return [
                {
                    "href": "https://www.xiaohongshu.com/explore/note-001",
                    "title": "今天加仓半导体基金",
                    "author_name": "测试作者",
                    "author_url": (
                        "https://www.xiaohongshu.com/user/profile/author-001"
                    ),
                    "like_count": "1.2万",
                    "card_text": "今天加仓半导体基金 测试作者 1.2万",
                }
            ]
        return {
            "title": "今天加仓半导体基金",
            "content": "这是作者自述的测试正文。",
            "author_name": "测试作者",
            "author_url": "https://www.xiaohongshu.com/user/profile/author-001",
            "published_value": int(self.published_at.timestamp() * 1000),
            "like_count": "1.2万",
            "comment_count": "36",
            "collect_count": "8",
            "login_required": False,
        }


class FakeBrowserContext:
    def __init__(self, page: FakeBrowserPage) -> None:
        self.pages = [page]

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def cookies(self, *_args) -> list[dict]:
        return [{"name": "web_session", "value": "test-session"}]


class SocialSignalFreshnessTests(unittest.TestCase):
    """验证 15:00 截止、下一交易日和最大年龄三层时效规则。"""

    def setUp(self) -> None:
        self.service = SocialSignalFreshnessService()

    def test_default_collection_budget_is_halved(self) -> None:
        self.assertEqual(settings.xhs_social_signal_max_keywords, 4)
        self.assertEqual(settings.xhs_social_signal_max_authors, 50)
        self.assertEqual(settings.xhs_social_signal_max_posts_per_refresh, 24)
        self.assertEqual(settings.xhs_social_signal_max_detail_requests, 8)
        self.assertEqual(settings.xhs_social_signal_analysis_batches, 4)
        self.assertEqual(settings.xhs_social_signal_lookback_hours, 18)

    def test_before_cutoff_targets_today_and_caps_window_at_18_hours(self) -> None:
        # 北京时间 2026-08-31（周一）10:00，距离 15:00 还有 5 小时。
        result = self.service.resolve(
            as_of=datetime(2026, 8, 31, 2, tzinfo=timezone.utc)
        )

        self.assertEqual(result.phase, "before_cutoff")
        self.assertEqual(result.target_trade_date.isoformat(), "2026-08-31")
        self.assertEqual(result.window_start.isoformat(), "2026-08-30T08:00:00+00:00")
        self.assertEqual(result.as_dict()["minutes_until_cutoff"], 300)

    def test_after_cutoff_targets_next_exchange_session(self) -> None:
        # 北京时间周一 16:00，窗口从当日 15:00 开始并服务于周二。
        result = self.service.resolve(
            as_of=datetime(2026, 8, 31, 8, tzinfo=timezone.utc)
        )

        self.assertEqual(result.phase, "after_cutoff")
        self.assertEqual(result.target_trade_date.isoformat(), "2026-09-01")
        self.assertEqual(result.window_start.isoformat(), "2026-08-31T07:00:00+00:00")

    def test_exchange_holiday_targets_next_real_session(self) -> None:
        # 2026 国庆休市，XSHG 日历给出的下一交易日为 10 月 8 日。
        result = self.service.resolve(
            as_of=datetime(2026, 10, 1, 2, tzinfo=timezone.utc)
        )

        self.assertEqual(result.phase, "market_closed")
        self.assertEqual(result.target_trade_date.isoformat(), "2026-10-08")
        self.assertEqual(result.window_start.isoformat(), "2026-09-30T08:00:00+00:00")


class XiaohongshuBrowserProviderTests(unittest.TestCase):
    def test_prepare_session_writes_only_a_local_marker(self) -> None:
        page = FakeBrowserPage(datetime(2026, 8, 31, 1, tzinfo=timezone.utc))
        context = FakeBrowserContext(page)
        with TemporaryDirectory() as profile_dir:
            provider = XiaohongshuBrowserProvider(
                keyword_loader=lambda: [],
                context_factory=lambda _headless: context,
                profile_dir=profile_dir,
            )
            with patch.object(settings, "xhs_social_signal_enabled", True):
                result = provider.prepare_session()

            self.assertEqual(result["status"], "connected")
            self.assertTrue(provider.session_ready)
            marker = Path(profile_dir) / "fundscope-session.json"
            self.assertIn("connected_at", marker.read_text(encoding="utf-8"))
            self.assertNotIn("test-session", marker.read_text(encoding="utf-8"))

    def test_login_cookie_does_not_override_visible_login_prompt(self) -> None:
        """匿名或过期 Cookie 存在时，登录弹层仍应阻止会话被判定为成功。"""

        page = FakeBrowserPage(datetime(2026, 8, 31, 1, tzinfo=timezone.utc))
        page.locator = lambda _selector: FakeLocator("请扫码登录")
        context = FakeBrowserContext(page)
        with TemporaryDirectory() as profile_dir:
            provider = XiaohongshuBrowserProvider(
                keyword_loader=lambda: [],
                context_factory=lambda _headless: context,
                profile_dir=profile_dir,
            )

            self.assertFalse(provider._has_authenticated_session(context, page))

    def test_normalizes_rendered_search_and_detail_without_network(self) -> None:
        now = datetime(2026, 8, 31, 1, tzinfo=timezone.utc)
        page = FakeBrowserPage(now - timedelta(hours=1))
        context = FakeBrowserContext(page)
        with TemporaryDirectory() as profile_dir:
            provider = XiaohongshuBrowserProvider(
                keyword_loader=lambda: [],
                context_factory=lambda _headless: context,
                profile_dir=profile_dir,
                session_ready=True,
            )
            with (
                patch.object(settings, "xhs_social_signal_enabled", True),
                patch.object(settings, "xhs_social_signal_keywords", ("基金实盘",)),
                patch.object(settings, "xhs_social_signal_author_urls", ()),
                patch.object(settings, "xhs_social_signal_max_keywords", 1),
                patch.object(settings, "xhs_social_signal_max_posts_per_refresh", 10),
                patch.object(settings, "xhs_social_signal_max_detail_requests", 10),
                patch.object(settings, "xhs_browser_scroll_rounds", 0),
                patch.object(settings, "xhs_browser_settle_seconds", 0),
            ):
                batch = provider.fetch_posts(
                    since=now - timedelta(hours=24),
                    until=now,
                )

        self.assertEqual(len(batch.posts), 1)
        post = batch.posts[0]
        self.assertEqual(post["post_id"], "note-001")
        self.assertEqual(post["author_id"], "author-001")
        self.assertEqual(post["like_count"], 12_000)
        self.assertIn("作者自述", post["content_excerpt"])
        self.assertTrue(post["content_hash"].startswith("sha256:"))
        self.assertEqual(len(page.urls), 2)
        self.assertIn("keyword=%E5%9F%BA%E9%87%91%E5%AE%9E%E7%9B%98", page.urls[0])

    def test_parses_relative_and_millisecond_publish_times(self) -> None:
        now = datetime(2026, 8, 31, 2, tzinfo=timezone.utc)

        relative = XiaohongshuBrowserProvider._parse_published_at(
            "2小时前",
            reference_time=now,
        )
        milliseconds = XiaohongshuBrowserProvider._parse_published_at(
            int((now - timedelta(hours=1)).timestamp() * 1000),
            reference_time=now,
        )

        self.assertEqual(relative, now - timedelta(hours=2))
        self.assertEqual(milliseconds, now - timedelta(hours=1))

    def test_uses_hex_note_id_time_when_detail_time_is_missing(self) -> None:
        """页面改版拿不到日期时，24 位公开笔记 ID 仍可提供受限的时间兜底。"""

        now = datetime(2026, 9, 3, 8, tzinfo=timezone.utc)
        published_at = now - timedelta(hours=1)
        post_id = f"{int(published_at.timestamp()):08x}" + "0" * 16

        parsed = XiaohongshuBrowserProvider._published_at_from_post_id(
            post_id,
            reference_time=now,
        )

        self.assertEqual(parsed, published_at)

    def test_rejects_outdated_candidate_before_opening_detail(self) -> None:
        now = datetime(2026, 9, 3, 8, tzinfo=timezone.utc)
        recent_id = f"{int((now - timedelta(hours=1)).timestamp()):08x}" + "1" * 16
        old_id = f"{int((now - timedelta(days=2)).timestamp()):08x}" + "2" * 16

        self.assertTrue(
            XiaohongshuBrowserProvider._candidate_may_be_in_window(
                {"post_id": recent_id},
                since=now - timedelta(hours=18),
                until=now,
            )
        )
        self.assertFalse(
            XiaohongshuBrowserProvider._candidate_may_be_in_window(
                {"post_id": old_id},
                since=now - timedelta(hours=18),
                until=now,
            )
        )

    def test_ignores_non_xiaohongshu_author_urls(self) -> None:
        provider = XiaohongshuBrowserProvider(
            keyword_loader=lambda: [],
            context_factory=lambda _headless: object(),
            session_ready=True,
        )
        with (
            patch.object(
                settings,
                "xhs_social_signal_author_urls",
                (
                    "https://www.xiaohongshu.com/user/profile/author-001",
                    "https://example.com/user/profile/not-allowed",
                ),
            ),
            patch.object(settings, "xhs_social_signal_max_authors", 50),
        ):
            urls = provider._author_urls()

        self.assertEqual(
            urls,
            ["https://www.xiaohongshu.com/user/profile/author-001"],
        )

    def test_merge_prefers_detail_url_with_search_access_context(self) -> None:
        provider = XiaohongshuBrowserProvider(
            keyword_loader=lambda: [],
            context_factory=lambda _headless: object(),
            session_ready=True,
        )
        candidates: dict[str, dict] = {}
        short = {
            "post_id": "note-001",
            "source_url": "https://www.xiaohongshu.com/explore/note-001",
        }
        contextual = {
            "post_id": "note-001",
            "source_url": (
                "https://www.xiaohongshu.com/explore/note-001"
                "?xsec_token=test-token&xsec_source=pc_search"
            ),
        }

        provider._merge_candidates(candidates, [short, contextual])

        self.assertIn("xsec_token=", candidates["note-001"]["source_url"])


class SocialSignalAnalysisValidationTests(unittest.TestCase):
    """模型只能抽取原文中出现、或由明确基金代码关联出的主题。"""

    @staticmethod
    def _post() -> dict:
        return {
            "post_id": "note-grounding",
            "published_at": datetime(2026, 8, 31, 1, tzinfo=timezone.utc),
            "title": "今天加仓半导体基金",
            "content_excerpt": "这是我今天自己的操作。",
            "matched_keyword": "基金实盘",
            "content_hash": "sha256:" + "1" * 64,
        }

    def test_abstains_when_model_invents_the_fund_topic(self) -> None:
        service = SocialSignalAnalysisService(
            repository=object(),  # 校验方法不访问 Repository。
            fund_context_loader=lambda: [],
        )
        result = ModelSocialSignalBatch.model_validate(
            {
                "judgments": [
                    {
                        "post_id": "note-grounding",
                        "fund_codes": ["999999"],
                        "fund_names": ["不存在基金"],
                        "themes": ["白酒"],
                        "action": "add",
                        "action_scope": "today",
                        "position_text": "加仓",
                        "rationale": "看好板块",
                        "is_self_reported": True,
                        "commercial_likelihood": 0.1,
                        "confidence": 0.95,
                        "uncertainties": [],
                    }
                ]
            }
        )

        analyses, _ = service._validate_model_result(
            model_result=result,
            posts=[self._post()],
            fund_contexts=[],
        )

        self.assertEqual(analyses[0]["status"], "abstained")
        self.assertEqual(analyses[0]["fund_codes"], [])
        self.assertEqual(analyses[0]["fund_names"], [])
        self.assertEqual(analyses[0]["themes"], [])

    def test_accepts_a_theme_that_is_visible_in_the_post(self) -> None:
        service = SocialSignalAnalysisService(
            repository=object(),
            fund_context_loader=lambda: [],
        )
        result = ModelSocialSignalBatch.model_validate(
            {
                "judgments": [
                    {
                        "post_id": "note-grounding",
                        "fund_codes": [],
                        "fund_names": [],
                        "themes": ["半导体"],
                        "action": "add",
                        "action_scope": "today",
                        "position_text": "加仓",
                        "rationale": "作者自述",
                        "is_self_reported": True,
                        "commercial_likelihood": 0.1,
                        "confidence": 0.9,
                        "uncertainties": [],
                    }
                ]
            }
        )

        analyses, _ = service._validate_model_result(
            model_result=result,
            posts=[self._post()],
            fund_contexts=[],
        )

        self.assertEqual(analyses[0]["status"], "accepted")
        self.assertEqual(analyses[0]["themes"], ["半导体"])


class SocialSignalAggregationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = TemporaryDirectory()
        path = Path(self.directory.name) / "social-signals.db"
        self.database = Database(f"sqlite:///{path}")
        self.repository = SocialSignalRepository(self.database)
        self.event_repository = MarketEventRepository(self.database)
        self.aggregator = SocialSignalAggregationService(
            repository=self.repository,
            event_repository=self.event_repository,
            fund_context_loader=lambda: [
                {
                    "fund_code": "110020",
                    "short_name": "沪深300联接",
                    "fund_type": "指数型",
                    "theme_keywords": ["沪深300"],
                }
            ],
        )

    def tearDown(self) -> None:
        self.database.engine.dispose()
        self.directory.cleanup()

    def test_pending_analysis_only_uses_current_operation_window(self) -> None:
        now = datetime(2026, 8, 31, 2, tzinfo=timezone.utc)
        posts = []
        for post_id, age_hours in (("fresh-post", 1), ("expired-post", 20)):
            posts.append(
                {
                    "post_id": post_id,
                    "platform": "xiaohongshu",
                    "author_id": f"author-{post_id}",
                    "author_name": post_id,
                    "published_at": now - timedelta(hours=age_hours),
                    "title": "今天加仓半导体基金",
                    "content_excerpt": "作者自述操作。",
                    "source_url": f"https://example.com/{post_id}",
                    "matched_keyword": "基金实盘",
                    "like_count": 0,
                    "comment_count": 0,
                    "collect_count": 0,
                    "content_hash": "sha256:" + post_id.encode().hex().ljust(64, "0")[:64],
                }
            )
        self.repository.upsert_posts(posts=posts, fetched_at=now)

        pending = self.repository.pending_for_analysis(
            limit=10,
            since=now - timedelta(hours=18),
        )

        self.assertEqual([item["post_id"] for item in pending], ["fresh-post"])

    def test_deduplicates_author_excludes_commercial_and_projects_level_d_event(self) -> None:
        now = datetime(2026, 8, 31, 1, tzinfo=timezone.utc)
        posts = []
        actions = {
            "post-a-old": ("author-a", "add", 4, 0.1),
            "post-a-new": ("author-a", "add", 1, 0.1),
            "post-b": ("author-b", "buy", 2, 0.1),
            "post-c": ("author-c", "hold", 3, 0.1),
            "post-ad": ("author-ad", "add", 1, 0.95),
        }
        for index, (post_id, (author_id, _action, age_hours, _commercial)) in enumerate(
            actions.items()
        ):
            posts.append(
                {
                    "post_id": post_id,
                    "platform": "xiaohongshu",
                    "author_id": author_id,
                    "author_name": author_id,
                    "published_at": now - timedelta(hours=age_hours),
                    "title": "作者自述基金操作",
                    "content_excerpt": "今天操作了110020。",
                    "source_url": f"https://example.com/{post_id}",
                    "matched_keyword": "基金实盘",
                    "like_count": index,
                    "comment_count": 0,
                    "collect_count": 0,
                    "content_hash": f"sha256:{index:064x}",
                }
            )
        self.repository.upsert_posts(posts=posts, fetched_at=now)

        analyses = []
        for post in posts:
            action = actions[post["post_id"]][1]
            commercial = actions[post["post_id"]][3]
            analyses.append(
                {
                    "post_id": post["post_id"],
                    "input_hash": post["content_hash"],
                    "status": "accepted",
                    "model_name": "test-model",
                    "prompt_version": SOCIAL_SIGNAL_PROMPT_VERSION,
                    "fund_codes": ["110020"],
                    "fund_names": ["沪深300联接"],
                    "themes": ["宽基"],
                    "action": action,
                    "action_scope": "today",
                    "position_text": "测试仓位",
                    "rationale": "测试理由",
                    "is_self_reported": True,
                    "commercial_likelihood": commercial,
                    "confidence": 0.9,
                    "uncertainties": [],
                }
            )
        self.repository.save_analyses(analyses=analyses, analyzed_at=now)

        with (
            patch.object(settings, "xhs_social_signal_lookback_hours", 36),
            patch.object(settings, "xhs_social_signal_min_authors", 3),
        ):
            result = self.aggregator.build(as_of=now)

        aggregate = result["aggregates"][0]
        self.assertEqual(aggregate["author_count"], 3)
        self.assertEqual(aggregate["sample_count"], 3)
        self.assertEqual(
            aggregate["evidence_posts"][0]["analysis_status"],
            "accepted",
        )
        self.assertEqual(
            aggregate["evidence_posts"][0]["matched_keyword"],
            "基金实盘",
        )
        self.assertEqual(aggregate["excluded_commercial_count"], 1)
        self.assertEqual(result["projected_event_count"], 1)
        self.assertEqual(result["operation_window"]["cutoff_time"], "15:00")
        self.assertEqual(
            result["operation_window"]["target_trade_date"],
            "2026-08-31",
        )

        stored = self.event_repository.load()["events"][0]
        self.assertEqual(stored["event_type"], "social_positioning")
        self.assertEqual(stored["source_level"], "D")
        self.assertEqual(stored["affected_fund_codes"], ["110020"])
        self.assertIn("未验证真实成交", stored["impact_reason"])
        self.assertIn("15:00 前", stored["title"])
        self.assertIn("目标交易日:2026-08-31", stored["tags"])


if __name__ == "__main__":
    unittest.main()
