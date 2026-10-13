"""用临时库和本地图片验证证据管线，不访问小红书，不调用真实付费模型。"""

from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from PIL import Image, ImageDraw, ImageFont

from app.config import settings
from app.database import Database
from app.services.social_signal_media import cache_image, allowed_image_url, media_path, recognize_image
from app.services.social_signal_provider import XiaohongshuBrowserProvider, SocialSignalAccessPausedError
from app.services.social_signal_report import SocialSignalReportService, CommunityReview
from app.services.social_signals import (
    SocialSignalRepository, SocialSignalService, SocialSignalAnalysisService,
    ModelSocialSignalBatch, SOCIAL_SIGNAL_PROMPT_VERSION,
)


class SocialImageEvidenceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.database = Database(f"sqlite:///{self.directory.name}/test.db")
        self.addCleanup(self.database.engine.dispose)
        self.repo = SocialSignalRepository(self.database)
        self.now = datetime.now(timezone.utc)
        context = patch.object(settings, "social_media_dir", str(Path(self.directory.name) / "images"))
        context.start()
        self.addCleanup(context.stop)

    def post(self, index=0, **kwargs):
        return {"post_id": f"post-{index}", "author_id": f"author-{index}", "author_name": "测试作者",
            "published_at": self.now - timedelta(minutes=index), "title": "今天加仓半导体",
            "content_excerpt": "仓位图片中的信息需要识别", "content_hash": f"hash-{index}",
            "source_url": "https://www.xiaohongshu.com/explore/test", "matched_keyword": "基金", **kwargs}

    def image(self):
        output = BytesIO()
        Image.new("RGB", (200, 100), "white").save(output, format="PNG")
        return cache_image(output.getvalue(), post_id="post-0", index=1, source_url="https://sns-webpic-qc.xhscdn.com/test")

    def test_media_paths_and_cdn_allowlist(self):
        for url in ("http://xhscdn.com/a", "https://127.0.0.1/a", "https://xhscdn.com.evil.org/a", "https://xhscdn.com:8443/a", "https://user@xhscdn.com/a"):
            self.assertFalse(allowed_image_url(url))
        self.assertTrue(allowed_image_url("https://sns-webpic-qc.xhscdn.com/a"))
        with self.assertRaises(ValueError):
            media_path("../../.env")
        with self.assertRaises(Exception):
            cache_image(b"not an image", post_id="p", index=1, source_url="")

    def test_all_posts_are_paginated_even_without_analysis(self):
        self.repo.upsert_posts(posts=[self.post(index) for index in range(27)], fetched_at=self.now)
        pages = [self.repo.list_posts(page=page, page_size=10) for page in (1, 2, 3)]
        ids = [post["post_id"] for page in pages for post in page["posts"]]
        self.assertEqual(len(set(ids)), 27)
        self.assertTrue(all(page["total"] == 27 for page in pages))
        self.assertEqual(pages[0]["posts"][0]["analysis_status"], "pending")
        self.assertEqual(len(self.repo.recent_analyzed(since=self.now - timedelta(days=1))), 27)
        self.assertEqual(self.repo.status()["pending_analysis_count"], 27)

    def test_image_cache_and_ocr_are_reused(self):
        image = self.image()
        self.repo.upsert_posts(posts=[self.post(images=[image])], fetched_at=self.now)
        fake = {"status": "completed", "text": "基金110020 买入100元", "lines": [{"text": "基金110020 买入100元", "confidence": .99}]}
        with patch("app.services.social_signal_media.recognize_image", return_value=fake) as ocr:
            self.repo.media.recognize_posts(["post-0"])
            self.repo.media.recognize_posts(["post-0"])
            self.assertEqual(ocr.call_count, 1)
        detail = self.repo.get_post("post-0")
        self.assertEqual(detail["images"][0]["recognition"]["text"], fake["text"])
        self.assertNotIn(self.directory.name, str(detail))

    def test_ocr_text_enters_model_and_grounded_validation(self):
        post = self.post(title="今日操作", images=[{"media_id": "image-1", "recognition": {
            "lines": [{"text": "110020 今日买入100元", "confidence": .98}]}}])
        analyzer = SocialSignalAnalysisService(repository=self.repo, fund_context_loader=lambda: [])
        self.assertIn("image-1", str(analyzer._model_post(post)))
        batch = ModelSocialSignalBatch.model_validate({"judgments": [{"post_id": "post-0", "fund_codes": ["110020"],
            "action": "buy", "action_scope": "today", "is_self_reported": True, "commercial_likelihood": 0,
            "confidence": .9, "evidence_kind": "trade", "evidence_ids": ["image:image-1"], "rationale": "作者自述的交易记录"}]})
        result, _ = analyzer._validate_model_result(model_result=batch, posts=[post], fund_contexts=[])
        self.assertEqual(result[0]["status"], "accepted")
        batch.judgments[0].evidence_kind = "holding"
        result, _ = analyzer._validate_model_result(model_result=batch, posts=[post], fund_contexts=[])
        self.assertEqual(result[0]["status"], "abstained")
        self.assertIn("不能直接证明", result[0]["details"]["exclusion_reasons"][0])
        self.assertEqual(result[0]["details"]["raw_judgment"]["action"], "buy")

    def test_unavailable_local_file_is_not_reported_as_empty_image(self):
        self.assertEqual(recognize_image(Path(self.directory.name) / "missing.png")["status"], "error")

    def test_real_local_ocr_recognizes_generated_fixture(self):
        # 可移植的本地测试图片，不是用户资产，也不会上传到任何模型服务。
        image = Image.new("RGB", (700, 180), "white")
        draw = ImageDraw.Draw(image)
        font = ImageFont.load_default(size=38)
        draw.text((30, 25), "FUND 110020", font=font, fill="black")
        draw.text((30, 95), "BUY 100.00", font=font, fill="black")
        path = Path(self.directory.name) / "ocr-fixture.png"
        image.save(path)
        result = recognize_image(path)
        if result["status"] == "unavailable":
            self.skipTest("未安装可选本地OCR运行时")
        self.assertEqual(result["status"], "completed")
        self.assertIn("110020", result["text"])

    def test_api_pagination_detail_image_and_query_validation(self):
        from fastapi.testclient import TestClient
        from app.main import app
        self.repo.upsert_posts(posts=[self.post(images=[self.image()])], fetched_at=self.now)
        service = SimpleNamespace(repository=self.repo, freshness_service=SimpleNamespace(
            resolve=lambda: SimpleNamespace(window_start=self.now-timedelta(days=1))))
        with patch("app.routers.market.social_signal_service", service):
            client = TestClient(app)
            self.assertEqual(client.get("/api/v1/market/social-signals/posts?scope=all").json()["total"], 1)
            detail = client.get("/api/v1/market/social-signals/posts/post-0")
            self.assertEqual(detail.status_code, 200)
            self.assertEqual(detail.json()["analysis_status"], "pending")
            media = client.get(detail.json()["images"][0]["image_url"])
            self.assertEqual(media.headers["content-type"], "image/png")
            self.assertEqual(client.get("/api/v1/market/social-signals/posts?scope=invalid").status_code, 422)
            self.assertEqual(client.get("/api/v1/market/social-signals/posts?page_size=100").status_code, 422)
            self.assertEqual(client.get("/api/v1/market/social-signals/images/missing").status_code, 404)

    def test_cdn_restriction_stops_before_next_image(self):
        provider = XiaohongshuBrowserProvider(profile_dir=self.directory.name, session_ready=True)
        provider._images_remaining = 3
        response = Mock(status=429)
        page = Mock(url="https://www.xiaohongshu.com/explore/a")
        page.context.request.get.return_value = response
        with patch.object(provider, "_ensure_page_access"), self.assertRaises(SocialSignalAccessPausedError):
            provider._capture_images(page, ["https://xhscdn.com/a", "https://xhscdn.com/b"], "p")
        self.assertEqual(page.context.request.get.call_count, 1)
        self.assertTrue(provider.status_details()["verification_required"])

    async def test_saved_analysis_never_opens_provider_when_paused(self):
        self.repo.upsert_posts(posts=[self.post()], fetched_at=self.now)
        provider = Mock()
        provider.status_details.return_value = {"verification_required": True}
        analyzer = AsyncMock()
        analyzer.analyze_pending.side_effect = [{"requested_count": 1, "accepted_count": 0}, {"requested_count": 0}]
        aggregator = Mock()
        aggregator.build.return_value = {"aggregate_count": 0}
        service = SocialSignalService(provider=provider, repository=self.repo, analyzer=analyzer, aggregator=aggregator,
            freshness_service=SimpleNamespace(resolve=lambda **_: SimpleNamespace(window_start=self.now-timedelta(hours=18), target_trade_date=self.now.date(), as_dict=lambda: {})))
        with patch.object(SocialSignalReportService, "generate", new=AsyncMock(return_value={"status": "ready"})):
            result = await service.analyze_saved(as_of=self.now)
        self.assertEqual(result["status"], "completed")
        provider.fetch_posts.assert_not_called()
        provider.prepare_session.assert_not_called()
        analyzer.analyze_pending.assert_awaited()

    async def test_report_citations_and_read_only_cache(self):
        self.repo.upsert_posts(posts=[self.post()], fetched_at=self.now)
        rows = self.repo.recent_analyzed(since=self.now-timedelta(days=1))
        service = SocialSignalReportService(self.repo)
        review = CommunityReview(overview="样本讨论半导体，未核验成交", findings=[{
            "heading": "半导体", "explanation": "作者表示加仓", "post_ids": ["post-0"]}],
            disagreements=[], limitations=["图片未采集，不能判断金额"])
        chain = Mock()
        chain.with_structured_output.return_value.ainvoke = AsyncMock(return_value=review)
        with patch("app.services.social_signal_report.get_chat_model", return_value=chain):
            result = await service.generate(rows, self.now.date())
            self.assertEqual(result["status"], "ready")
            await service.generate(rows, self.now.date())
            self.assertEqual(chain.with_structured_output.return_value.ainvoke.await_count, 1)
            rows[0]["content_hash"] = "new-content"
            self.assertEqual(service.latest(rows, self.now.date())["status"], "stale")
            review.findings[0].post_ids = ["invented"]
            with self.assertRaises(ValueError):
                await service.generate(rows, self.now.date())


if __name__ == "__main__":
    unittest.main()
