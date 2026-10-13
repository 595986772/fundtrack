"""验证采集暂停和跨进程限频。只用假浏览器，不访问小红书或调用模型。"""

from datetime import datetime, timedelta, timezone
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from app.config import settings
from app.services.social_signal_provider import (
    SocialPostBatch,
    SocialSignalAccessPausedError,
    SocialSignalCooldownError,
    XiaohongshuBrowserProvider,
)
from app.services.social_signals import SocialSignalService
from tests.test_social_signals import FakeBrowserContext, FakeBrowserPage, FakeLocator


class AccessPage(FakeBrowserPage):
    """控制哪一页出现门禁，记录是否还发生了后续导航和滚动。"""

    def __init__(self, now, *, blocked_at=None, status=None, block_on_scroll=False):
        super().__init__(now - timedelta(hours=1))
        self.url = ""
        self.body = "公开搜索结果"
        self.blocked_at = blocked_at
        self.status = status
        self.block_on_scroll = block_on_scroll
        self.scroll_count = 0

    def goto(self, url, **kwargs):
        super().goto(url, **kwargs)
        self.url = url
        if len(self.urls) == self.blocked_at:
            self.body = "访问过于频繁，请完成安全验证"
        return SimpleNamespace(status=self.status)

    def locator(self, _selector):
        return FakeLocator(self.body)

    def evaluate(self, script):
        if script.startswith("window.scrollBy"):
            self.scroll_count += 1
            if self.block_on_scroll:
                self.body = "请完成安全验证"
        return super().evaluate(script)


class SocialSignalAccessTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.now = datetime.now(timezone.utc)
        # 参数全部限制在本测试，真实服务的配置与登录资料不会被改变。
        for name, value in {
            "xhs_social_signal_enabled": True,
            "xhs_social_signal_keywords": ("基金实盘", "基金操作"),
            "xhs_social_signal_max_keywords": 2,
            "xhs_social_signal_author_urls": (),
            "xhs_browser_settle_seconds": 0,
            "xhs_browser_scroll_rounds": 0,
            "xhs_browser_min_refresh_seconds": 900,
        }.items():
            context = patch.object(settings, name, value)
            context.start()
            self.addCleanup(context.stop)

    def provider(self, page):
        return XiaohongshuBrowserProvider(
            context_factory=Mock(return_value=FakeBrowserContext(page)),
            keyword_loader=lambda: [],
            profile_dir=self.directory.name,
            session_ready=True,
        )

    def fetch(self, provider):
        return provider.fetch_posts(since=self.now - timedelta(hours=18), until=self.now)

    def test_challenge_stops_before_other_keywords_or_scrolling(self):
        page = AccessPage(self.now, blocked_at=1)
        provider = self.provider(page)
        batch = self.fetch(provider)
        self.assertEqual(batch.status, "verification_required")
        self.assertEqual(len(page.urls), 1)
        self.assertEqual(page.scroll_count, 0)
        self.assertTrue(provider.status_details()["verification_required"])
        self.assertFalse(provider.status_details()["collection_allowed"])
        self.assertNotIn("test-session", provider.access_marker.read_text())

    def test_pause_survives_provider_restart_and_prevents_browser_launch(self):
        self.fetch(self.provider(AccessPage(self.now, blocked_at=1)))
        another = self.provider(AccessPage(self.now))
        with self.assertRaises(SocialSignalAccessPausedError):
            self.fetch(another)
        another.context_factory.assert_not_called()

    def test_refresh_cooldown_is_shared_by_provider_instances(self):
        self.assertEqual(self.fetch(self.provider(AccessPage(self.now))).status, "completed")
        another = self.provider(AccessPage(self.now))
        with self.assertRaises(SocialSignalCooldownError):
            self.fetch(another)
        another.context_factory.assert_not_called()

    def test_http_access_denial_stops_even_when_page_looks_normal(self):
        for status in (403, 429):
            with self.subTest(status=status), TemporaryDirectory() as directory:
                page = AccessPage(self.now, status=status)
                provider = XiaohongshuBrowserProvider(
                    context_factory=lambda _: FakeBrowserContext(page),
                    profile_dir=directory, session_ready=True,
                )
                self.assertEqual(self.fetch(provider).status, "verification_required")
                self.assertEqual(len(page.urls), 1)

    def test_challenge_during_scroll_stops_subsequent_scrolls(self):
        page = AccessPage(self.now, block_on_scroll=True)
        with patch.object(settings, "xhs_browser_scroll_rounds", 5):
            batch = self.fetch(self.provider(page))
        self.assertEqual(batch.status, "verification_required")
        self.assertEqual(page.scroll_count, 1)

    def test_visible_challenge_cannot_be_overridden_by_a_login_cookie(self):
        page = AccessPage(self.now)
        provider = self.provider(page)
        for text in ("请扫码验证", "网络环境异常", "请拖动滑块", "请扫码登录"):
            page.body = text
            with self.subTest(text=text):
                self.assertFalse(provider._has_authenticated_session(FakeBrowserContext(page), page))
        page.body = "投资交流：不要把验证码给别人，风控也值得重视。"
        self.assertTrue(provider._has_authenticated_session(FakeBrowserContext(page), page))

    def test_login_prompt_stops_and_clears_ready_marker(self):
        page = AccessPage(self.now)
        page.body = "请扫码登录"
        provider = self.provider(page)
        batch = self.fetch(provider)
        self.assertEqual(batch.status, "login_required")
        self.assertFalse(provider.session_ready)
        self.assertEqual(len(page.urls), 1)

    def test_unreadable_page_stops_instead_of_continuing_keywords(self):
        page = AccessPage(self.now)
        page.locator = Mock(side_effect=RuntimeError("page unavailable"))
        provider = self.provider(page)
        batch = self.fetch(provider)
        self.assertEqual(batch.status, "verification_required")
        self.assertEqual(len(page.urls), 1)
        self.assertIn("无法确认", provider.status_details()["access_message"])

    def test_partial_posts_are_returned_when_later_detail_is_blocked(self):
        provider = self.provider(AccessPage(self.now))
        post = {"post_id": "first", "author_id": "author", "title": "基金操作", "content_excerpt": "公开正文", "published_at": self.now}
        with patch.object(provider, "_search_keyword", return_value=[{"post_id": "first"}, {"post_id": "second"}]), patch.object(
            provider, "_read_detail", side_effect=[post, SocialSignalAccessPausedError("请人工验证")],
        ):
            batch = self.fetch(provider)
        self.assertEqual(batch.status, "verification_required")
        self.assertEqual(len(batch.posts), 1)
        self.assertEqual(batch.posts[0]["post_id"], "first")

    def test_manual_reconnection_clears_pause_but_not_refresh_interval(self):
        provider = self.provider(AccessPage(self.now, blocked_at=1))
        self.fetch(provider)
        before = provider.status_details()["next_collection_at"]
        recovered = self.provider(AccessPage(self.now))
        self.assertEqual(recovered.prepare_session()["status"], "connected")
        status = recovered.status_details()
        self.assertFalse(status["verification_required"])
        self.assertEqual(status["next_collection_at"], before)
        self.assertFalse(status["collection_allowed"])


class SocialSignalPausedServiceTests(unittest.IsolatedAsyncioTestCase):
    def service(self, provider):
        return SocialSignalService(provider=provider, repository=Mock(), analyzer=AsyncMock(), aggregator=Mock())

    async def test_cooldown_is_result_not_job_retry_and_does_not_call_model(self):
        provider = Mock(configured=True, session_ready=True)
        provider.status_details.return_value = {}
        provider.fetch_posts.side_effect = SocialSignalCooldownError("请稍后再试")
        service = self.service(provider)
        result = await service.refresh()
        self.assertEqual(result["status"], "cooldown")
        service.analyzer.analyze_pending.assert_not_called()
        service.repository.upsert_posts.assert_not_called()

    async def test_partial_batch_is_saved_without_further_analysis(self):
        provider = Mock(configured=True, session_ready=True)
        provider.status_details.return_value = {}
        post = {"post_id": "complete-post-before-block"}
        provider.fetch_posts.return_value = SocialPostBatch((post,), ("基金实盘",), ("请人工验证",), "verification_required")
        service = self.service(provider)
        service.repository.upsert_posts.return_value = {"inserted_count": 1}
        result = await service.refresh()
        self.assertEqual(result["status"], "verification_required")
        self.assertEqual(service.repository.upsert_posts.call_args.kwargs["posts"], [post])
        service.analyzer.analyze_pending.assert_not_called()
        service.aggregator.build.assert_not_called()
