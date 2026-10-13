"""社区图片证据的浏览器回归：模拟数据与 Job，绝不采集平台或调用付费模型。"""

import asyncio
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from urllib.parse import urlparse, parse_qs

from PIL import Image, ImageDraw, ImageFont
from playwright.async_api import async_playwright, expect


async def main():
    output = Path(__file__).resolve().parents[1] / ".impeccable/review"
    output.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc).isoformat()
    picture = Image.new("RGB", (700, 300), "white")
    draw = ImageDraw.Draw(picture)
    draw.text((30, 35), "TEST EVIDENCE\nFUND 110020\nHOLDINGS 8000.00", fill="black", font=ImageFont.load_default(size=34), spacing=15)
    buffer = BytesIO()
    picture.save(buffer, format="PNG")
    posts = [{"post_id": f"test-{index}", "title": f"测试样本 {index + 1}：半导体持仓与观点记录",
        "author_name": f"测试作者 {index + 1}", "published_at": now, "matched_keyword": "半导体基金",
        "source_url": "https://www.xiaohongshu.com/explore/test", "content_excerpt": "这是用于页面回归的测试内容，持仓金额不代表今天买入。",
        "analysis_status": "abstained", "action_label": "未识别", "rationale": "图片是已有持仓记录，无法证明今天发生买入。",
        "exclusion_reasons": ["持仓或收益记录不能直接证明本次买卖"], "uncertainties": ["交易时间未提供"],
        "evidence_kind": "holding", "model_name": "测试模型", "analyzed_at": now, "like_count": 3, "comment_count": 0,
        "images": [{"media_id": "test-image", "image_index": 1, "image_url": "/api/v1/market/social-signals/images/test-image",
                    "recognition": {"status": "completed", "lines": [{"text": "HOLDINGS 8000.00", "confidence": .98}]}}]} for index in range(27)]
    report = {"status": "ready", "overview": "测试解读：讨论集中于半导体，但持仓图不能作为今日交易凭证。",
        "model_name": "测试模型", "generated_at": now, "sample_count": 27, "author_count": 27, "image_count": 27,
        "findings": [{"heading": "半导体关注度", "explanation": "样本提到半导体，操作细节仍需更多证据。", "post_ids": ["test-0"]}],
        "disagreements": [], "limitations": ["这是一组浏览器测试样本"]}
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page(viewport={"width": 1440, "height": 1000})
        errors, submissions, external = [], [], []
        page.on("pageerror", lambda error: errors.append(str(error)))

        async def deny(route):
            external.append(route.request.url)
            await route.abort()

        async def social(route):
            url = urlparse(route.request.url)
            if url.path.endswith("/images/test-image"):
                await route.fulfill(content_type="image/png", body=buffer.getvalue())
            elif url.path.endswith("/analysis"):
                submissions.append(route.request.url)
                assert route.request.headers.get("content-type") == "application/json"
                await route.fulfill(json={"job": {"id": "social-ui-test"}})
            elif "/posts/" in url.path:
                await route.fulfill(json=posts[int(url.path.split("test-")[-1])])
            elif url.path.endswith("/posts"):
                number = int(parse_qs(url.query).get("page", ["1"])[0])
                await route.fulfill(json={"posts": posts[(number-1)*10:number*10], "total": 27, "page": number, "page_size": 10})
            else:
                response = await route.fetch()
                data = await response.json()
                data["status"].update(verification_required=True, access_message="测试状态：平台采集暂停，已有数据仍可分析。")
                data.update(review=report)
                await route.fulfill(json=data)

        await page.route("**/*xiaohongshu.com/**", deny)
        await page.route("**/api/v1/market/social-signals**", social)
        await page.route("**/background-jobs/social-ui-test?**", lambda route: route.fulfill(json={"job": {
            "status": "completed", "result": {"status": "completed", "summary": "测试分析完成"}}}))
        await page.goto("http://127.0.0.1:8000/", wait_until="networkidle")
        await page.locator('#sidebar [data-product-view="market"]').click()
        await page.locator('[data-workspace-target=".social-signals-section"]').click()
        await expect(page.locator("#analyze-social-signals")).to_be_enabled()
        await expect(page.locator("#sync-social-signals")).to_be_disabled()
        await expect(page.locator("#social-posts .social-post-row")).to_have_count(10)
        await page.locator("#social-post-scope").select_option("all")
        await page.get_by_role("button", name="下一页", exact=True).click()
        await expect(page.locator("#social-posts")).to_contain_text("测试样本 11")
        await page.get_by_role("button", name="下一页", exact=True).click()
        await expect(page.locator("#social-posts .social-post-row")).to_have_count(7)
        await page.locator("#analyze-social-signals").click()
        await expect(page.locator("#analyze-social-signals")).to_be_enabled()
        await expect(page.locator("#social-review")).to_contain_text("AI 社区解读")
        assert len(submissions) == 1

        for label, width, height in [("desktop", 1440, 1000), ("mobile", 390, 844)]:
            await page.set_viewport_size({"width": width, "height": height})
            # 视口变化会触发侧栏收起过渡；截图需等待布局稳定，不能截在动画中间。
            await page.wait_for_timeout(400)
            await page.locator("#social-review").scroll_into_view_if_needed()
            await page.wait_for_timeout(200)
            await page.screenshot(path=str(output / f"social-evidence-{label}.png"))
            await page.locator("#social-review [data-social-post]").first.click()
            await expect(page.locator("#social-detail-body")).to_contain_text("已有持仓记录")
            await expect(page.locator("#social-post-detail img")).to_be_visible()
            assert await page.locator("#social-post-detail img").evaluate("img => img.complete && img.naturalWidth > 0")
            await page.locator("#social-post-detail summary").click()
            await expect(page.locator(".social-ocr-lines")).to_contain_text("8000.00")
            await page.screenshot(path=str(output / f"social-detail-{label}.png"))
            assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            await page.keyboard.press("Escape")
            await expect(page.locator("#social-post-detail")).not_to_be_visible()
        assert not errors, errors
        assert not external, external
        print("PASS: 27 posts paginated, image/OCR details, citations, analysis during access pause, desktop/mobile; no external collection or model calls.")
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
