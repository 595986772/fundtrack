"""社区采集异常的页面回归。读取本地样本、模拟访问限制，绝不访问小红书。"""

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

from playwright.async_api import async_playwright, expect


async def main():
    output = Path(__file__).resolve().parents[1] / ".impeccable/review"
    output.mkdir(parents=True, exist_ok=True)
    mode = "verification"
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        page = await browser.new_page(viewport={"width": 1440, "height": 1000})
        errors, posts, external = [], [], []
        page.on("pageerror", lambda error: errors.append(str(error)))

        async def deny_external(route):
            external.append(route.request.url)
            await route.abort()

        async def blocked_login(route):
            posts.append(route.request.url)
            await route.fulfill(status=503, json={"detail": "UI 测试：未打开真实小红书"})

        async def mock_status(route):
            response = await route.fetch()
            data = await response.json()
            status = data["status"]
            status.update(
                session_ready=mode != "login",
                verification_required=mode == "verification",
                next_collection_at=(datetime.now(timezone.utc) + timedelta(minutes=15 if mode == "cooldown" else -1)).isoformat(),
                access_message="小红书要求安全验证，采集已暂停；已保存样本仍可查看。" if mode == "verification" else "",
            )
            await route.fulfill(json=data)

        await page.route("**/*xiaohongshu.com/**", deny_external)
        await page.route("**/api/v1/market/social-signals/browser-session", blocked_login)
        await page.route("**/api/v1/market/social-signals/data-sync", blocked_login)
        await page.route("**/api/v1/market/social-signals", mock_status)
        await page.goto("http://127.0.0.1:8000/", wait_until="networkidle")
        await page.locator('#sidebar [data-product-view="market"]').click()
        await page.locator('[data-workspace-target=".social-signals-section"]').click()
        await expect(page.locator("#social-signals")).to_contain_text("采集已暂停")
        await expect(page.locator("#social-posts")).to_contain_text("已保存帖子")
        await expect(page.locator("#sync-social-signals")).to_be_disabled()
        await expect(page.locator("#connect-xhs-session")).to_have_attribute("title", "处理小红书验证")

        # 两个视口同轮截图，检查长暂停提示没有压住按钮，也没有隐藏历史样本。
        for name, width, height in [("desktop", 1440, 1000), ("mobile", 390, 844)]:
            await page.set_viewport_size({"width": width, "height": height})
            await page.locator(".social-signals-section").evaluate("el => { const scroller = el.closest('.fund-content'); scroller.scrollTop += el.getBoundingClientRect().top - scroller.getBoundingClientRect().top - 100; }")
            await page.wait_for_timeout(150)
            assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            await page.screenshot(path=str(output / f"social-access-{name}.png"))

        # Cookie 标记仍在时，验证按钮必须能发起人工连接，而不是被旧的“已连接”判断挡住。
        await page.locator("#connect-xhs-session").click()
        await expect(page.locator("#connect-xhs-session")).to_be_enabled()
        assert len(posts) == 1
        for next_mode in ["cooldown", "ready", "login"]:
            mode = next_mode
            await page.reload(wait_until="networkidle")
            await expect(page.locator("#social-posts")).to_contain_text("已保存帖子")
            if mode == "ready":
                await expect(page.locator("#sync-social-signals")).to_be_enabled()
            else:
                await expect(page.locator("#sync-social-signals")).to_be_disabled()
            if mode == "cooldown":
                await expect(page.locator("#social-signals")).to_contain_text("不会到点自动采集")
        assert len(posts) == 1, "切换状态和到期不得自动提交采集"
        assert not external, "测试不应请求小红书"
        assert not errors, errors
        print("PASS: verification/cooldown/login/ready, retained samples, desktop/mobile; no XHS requests.")
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
