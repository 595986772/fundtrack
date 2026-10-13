"""验证真实 Web 入口和授权弹窗；所有写请求在浏览器边界拦截，不调用模型或撤销真实授权。

服务启动后运行 python scripts/check_agent_authorization_ui.py。
先检查真实只读入口，再用浏览器夹具覆盖授权接口，检查审计、确认撤销、错误和空状态。
桌面与窄屏截图保存在 .impeccable/review，不更改用户数据库。
"""

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

from playwright.async_api import async_playwright, expect


async def main():
    output = Path(__file__).resolve().parents[1] / ".impeccable/review"
    output.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        page = await browser.new_page(viewport={"width": 1440, "height": 980})
        errors, writes = [], []
        page.on("pageerror", lambda error: errors.append(str(error)))

        async def block_writes(route):
            if route.request.method not in {"GET", "HEAD", "OPTIONS"}:
                writes.append(route.request.url)
                await route.fulfill(status=409, json={"detail": "浏览器测试不执行真实写入"})
            else:
                await route.continue_()

        await page.route("**/*", block_writes)
        await page.goto("http://127.0.0.1:8000/#research/chat", wait_until="networkidle")
        trigger = page.locator("#open-agent-authorizations")
        dialog = page.locator("#agent-authorizations-dialog")
        await trigger.click()
        await expect(dialog).to_be_visible()
        await expect(dialog.locator("[data-auth-content]")).to_have_attribute("aria-busy", "false")
        await expect(dialog).not_to_contain_text("读取失败")
        await page.keyboard.press("Escape")
        await expect(trigger).to_be_focused()

        now = datetime.now(timezone.utc)
        grant = {"id": "grant_browser_preview", "run_id": "chat:browser-preview", "agent_id": "main",
            "source": "product-api", "status": "active", "effective": True,
            "portfolio_ids": ["portfolio_browser_preview"],
            "capabilities": ["portfolio:read", "portfolio:research-run", "market:read"],
            "created_at": now.isoformat(), "expires_at": (now + timedelta(minutes=30)).isoformat()}
        mode, revocations = "normal", []

        async def mock_authorizations(route):
            if mode == "error":
                await route.fulfill(status=503, json={"detail": "测试：授权服务暂不可用"})
            elif route.request.method == "POST":
                revocations.append(route.request.post_data_json)
                grant.update(status="revoked", effective=False, revoked_at=now.isoformat())
                await route.fulfill(json=grant)
            elif "/grant_browser_preview" in route.request.url:
                await route.fulfill(json={**grant, "audit": [{"action": "portfolio:read", "allowed": True,
                    "resource_id": grant["portfolio_ids"][0], "reason": "allowed", "created_at": now.isoformat()}]})
            else:
                await route.fulfill(json={"enabled": True, "strict_identity": False,
                    "grants": [] if mode == "empty" else [grant]})

        await page.route("**/api/v1/agent/authorizations**", mock_authorizations)
        await trigger.click()
        await expect(dialog).to_contain_text("读取组合")
        await dialog.get_by_role("button", name="只读审计").click()
        await expect(dialog).to_contain_text("校验通过")
        await page.screenshot(path=str(output / "agent-authorizations-desktop.png"))
        await dialog.get_by_role("button", name="撤销组合", exact=False).click()
        await expect(dialog).to_contain_text("不会撤回既有回答或交易")
        await dialog.get_by_role("button", name="确认撤销", exact=True).click()
        await expect(dialog).to_contain_text("已撤销")
        assert len(revocations) == 1

        # 同一组件在窄屏保持可读，不依赖 hover 或水平滚动才能找到撤销入口。
        await page.set_viewport_size({"width": 390, "height": 844})
        await page.screenshot(path=str(output / "agent-authorizations-narrow.png"))
        assert await dialog.evaluate("node => node.scrollWidth <= node.clientWidth + 1")
        mode = "error"
        await dialog.locator("[data-auth-refresh]").click()
        await expect(dialog).to_contain_text("授权读取失败")
        mode = "empty"
        await dialog.get_by_role("button", name="重试读取").click()
        await expect(dialog).to_contain_text("暂无任务授权")
        await page.keyboard.press("Escape")
        await expect(dialog).not_to_be_visible()
        assert not errors, errors
        assert not writes, f"未预期的写请求（已拦截）: {writes}"
        await browser.close()
        print("PASS: live entry, audit, revoke confirmation, errors, empty state, desktop/narrow layouts; no real writes")


if __name__ == "__main__":
    asyncio.run(main())
