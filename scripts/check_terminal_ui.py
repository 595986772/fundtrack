"""研究终端的真实浏览器冒烟测试。

先启动 FastAPI，再运行 python scripts/check_terminal_ui.py。
读取本地已有快照；所有 Agent POST 都在浏览器边界拦截，不产生模型费用或研究记录。
截图保存在 .impeccable/review，用于检查桌面、当前窄窗口与手机布局。
"""

import asyncio
import re
from pathlib import Path

from playwright.async_api import async_playwright, expect


async def main():
    output = Path(__file__).resolve().parents[1] / ".impeccable/review"
    output.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        page = await browser.new_page(viewport={"width": 1440, "height": 1000}, device_scale_factor=1)
        errors, model_requests = [], []
        page.on("pageerror", lambda error: errors.append(str(error)))

        async def mock_agent(route):
            model_requests.append(route.request.post_data_json)
            await route.fulfill(status=503, content_type="application/json", body='{"detail":"浏览器测试：模拟模型暂不可用"}')

        await page.route("**/api/v1/agent/chat/stream", mock_agent)
        # 历史会话来自浏览器夹具，不向用户数据库写测试消息。
        history = {"id": "browser-history-fund-b", "title": "另一只基金的历史研究", "message_count": 1, "updated_at": "2026-09-01T08:00:00Z"}
        async def mock_history_list(route):
            await route.fulfill(json={"conversations": [history]})

        async def mock_history_detail(route):
            await route.fulfill(json={"conversation": history, "messages": [{"id": "history-message-b", "role": "assistant", "content": "历史标的 B 的独立结论", "created_at": history["updated_at"], "run_metadata": {}}]})

        await page.route("**/api/v1/conversations?*", mock_history_list)
        await page.route("**/api/v1/conversations/browser-history-fund-b?*", mock_history_detail)
        await page.goto("http://127.0.0.1:8000/", wait_until="networkidle")
        await expect(page.locator(".terminal-tick")).to_have_count(6)
        await expect(page.locator("#terminal-chart canvas")).to_be_visible()
        assert not model_requests, "打开终端不应调用模型"

        # 不同周期都必须改变选中状态并保留可见图表。
        for period in ["30d", "3m", "6m", "12m", "36m"]:
            await page.locator(f'[data-chart-period="{period}"]').click()
            await expect(page.locator(f'[data-chart-period="{period}"]')).to_have_attribute("aria-pressed", "true")
            await expect(page.locator("#terminal-chart canvas")).to_be_visible()
        await page.locator('[data-chart-period="3m"]').click()
        await page.locator("#terminal-comparison").select_option("161725")
        await expect(page.locator("#terminal-chart-caption")).to_contain_text("对比 招商")
        await page.locator('[data-chart-mode="nav"]').click()
        await expect(page.locator("#terminal-comparison")).to_be_disabled()
        await expect(page.locator("#terminal-chart-caption")).to_contain_text("单位净值未复权")
        await page.locator('[data-chart-mode="return"]').click()
        await page.locator("#terminal-comparison").select_option("")

        await page.locator('[data-index-code="000300"]').click()
        await expect(page.locator("#terminal-subject h2")).to_have_text("沪深300")
        await page.locator('[data-chart-period="36m"]').click()
        await expect(page.locator("#terminal-chart-warning")).to_contain_text("历史不足")
        await page.locator('[data-side-tab="ai"]').click()
        await page.locator("#terminal-ai-input").fill("说明当前市场状态")
        await page.locator("#terminal-ai-send").click()
        await expect(page.locator("#terminal-ai-error")).to_contain_text("模拟模型暂不可用")
        assert model_requests[-1]["fund_code"] is None, "指数研究不能夹带旧基金代码"
        assert "000300" in model_requests[-1]["message"]

        # 在完整助手打开其他历史后，终端既不能展示它，也不能复用它的 conversation_id。
        await page.locator('#sidebar [data-product-view="agent"]').click()
        await page.locator('[data-conversation-id="browser-history-fund-b"]').click()
        await expect(page.get_by_role("region", name="聊天消息")).to_contain_text("历史标的 B 的独立结论")
        await page.locator('#sidebar [data-product-view="overview"]').click()
        await expect(page.locator("#terminal-ai-content")).not_to_contain_text("历史标的 B 的独立结论")
        await page.locator("#terminal-ai-input").fill("继续研究当前指数")
        await page.locator("#terminal-ai-send").click()
        await expect(page.locator("#terminal-ai-error")).to_contain_text("模拟模型暂不可用")
        assert model_requests[-1]["conversation_id"] is None, "历史会话不得串入终端当前标的"
        await page.locator('[data-terminal-action="new-research"]').click()
        await page.locator('[data-side-tab="news"]').click()

        # 用真实只读关键词过滤将市场页列表清空，终端全市场列表必须保持不变。
        headlines = await page.locator("#terminal-news-list h3").all_text_contents()
        assert headlines, "新闻隔离测试需要本地至少一条已保存的事件"
        await page.locator('#sidebar [data-product-view="market"]').click()
        await page.locator("#market-event-query").fill("terminal-no-match-202609")
        async with page.expect_response(lambda response: "/market/events?" in response.url and "q=terminal-no-match" in response.url):
            await page.locator("#market-event-query").press("Enter")
        await page.locator('#sidebar [data-product-view="overview"]').click()
        assert await page.locator("#terminal-news-list h3").all_text_contents() == headlines
        # 恢复筛选状态，后面的视觉截图继续使用用户已有的实际资讯。
        await page.locator('#sidebar [data-product-view="market"]').click()
        await page.locator("#market-event-query").fill("")
        async with page.expect_response(lambda response: "/market/events?limit=20" in response.url):
            await page.locator("#market-event-query").press("Enter")
        await page.locator('#sidebar [data-product-view="overview"]').click()

        # 人为延迟旧基金响应，随后快速换到另一只基金，模拟用户连续点击。
        async def slow_chart(route):
            response = await route.fetch()
            await asyncio.sleep(0.6)
            await route.fulfill(response=response)

        await page.route("**/api/v1/funds/161725/chart", slow_chart)
        await page.locator('#fund-nav-list [data-fund-code="161725"]').click()
        await page.locator('#fund-nav-list [data-fund-code="110007"]').click()
        await expect(page.locator("#terminal-chart-empty")).to_be_hidden()
        await asyncio.sleep(0.8)
        await expect(page.locator("#terminal-subject h2")).to_contain_text("稳健收益")
        await expect(page.locator("#terminal-chart")).to_have_attribute("aria-label", re.compile(".*稳健收益.*"))
        await page.unroute("**/api/v1/funds/161725/chart", slow_chart)
        for tab in ["capital", "hotspots", "community", "risk"]:
            await page.locator(f'[data-research-tab="{tab}"]').click()
            await expect(page.locator(f'[data-research-tab="{tab}"]')).to_have_attribute("aria-selected", "true")
            assert await page.locator("#terminal-research-body").inner_text()
        assert len(model_requests) == 2, "图表、标的和研究页签切换不应调用模型"

        await page.locator('#fund-nav-list [data-fund-code="110020"]').click()
        await page.locator('[data-chart-period="3m"]').click()
        await expect(page.locator("#terminal-chart-empty")).to_be_hidden()
        # 检查真正的 canvas 像素，而不仅检查画布节点存在。
        colored_pixels = await page.locator("#terminal-chart canvas").evaluate("""canvas => {
            const pixels = canvas.getContext('2d').getImageData(0, 0, canvas.width, canvas.height).data;
            let yellow = 0;
            for (let i = 0; i < pixels.length; i += 4) if (pixels[i] > 170 && pixels[i+1] > 130 && pixels[i+2] < 140 && pixels[i+3] > 100) yellow++;
            return yellow;
        }""")
        assert colored_pixels > 100, "主曲线不能为空"
        axis_colors = await page.locator("#terminal-chart").evaluate("el => { const option = echarts.getInstanceByDom(el).getOption(); return [option.xAxis[0].axisLabel.color, option.yAxis[0].axisLabel.color]; }")
        def luminance(hex_color):
            channels = [int(hex_color[i:i + 2], 16) / 255 for i in (1, 3, 5)]
            linear = [value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4 for value in channels]
            return sum(value * weight for value, weight in zip(linear, (0.2126, 0.7152, 0.0722)))

        contrast = min((luminance(color) + 0.05) / (luminance("#191a1e") + 0.05) for color in axis_colors)
        assert contrast >= 4.5, f"图表轴文字对比度不足: {contrast:.2f}"

        # 行为检查结束后撤销历史夹具，视觉证据展示用户的实际会话列表。
        await page.unroute("**/api/v1/conversations?*", mock_history_list)
        await page.unroute("**/api/v1/conversations/browser-history-fund-b?*", mock_history_detail)
        await page.locator('#sidebar [data-product-view="agent"]').click()
        await page.get_by_role("button", name="刷新会话", exact=True).click()
        await page.locator('#sidebar [data-product-view="overview"]').click()

        # 同一轮同时捕获全部视口，避免只看宽屏就宣布适配完成。
        for name, width, height in [("desktop", 1440, 1000), ("user-686", 686, 984), ("mobile", 390, 844)]:
            await page.set_viewport_size({"width": width, "height": height})
            await page.locator(".fund-content").evaluate("el => el.scrollTop = 0")
            await page.wait_for_timeout(180)
            assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth"), f"{name} 页面横向溢出"
            assert await page.locator(".fund-content").evaluate("el => el.scrollWidth <= el.clientWidth + 1"), f"{name} 内容横向溢出"
            await page.screenshot(path=str(output / f"{name}.png"), full_page=True)
            if name == "mobile":
                # 滚到面板顶部，而非仅让 tab 进入 viewport 底部（那里被手机导航覆盖）。
                await page.locator(".terminal-research").evaluate("el => { const scroller = el.closest('.fund-content'); scroller.scrollTop += el.getBoundingClientRect().top - scroller.getBoundingClientRect().top - 24; }")
                await page.wait_for_timeout(180)
                await page.screenshot(path=str(output / "mobile-research.png"), full_page=True)
                await page.locator("#open-fund-sidebar").click()
                await expect(page.locator("#sidebar")).to_be_in_viewport()
                await page.locator("#close-sidebar").click()

        await page.set_viewport_size({"width": 1440, "height": 1000})
        # 原有的组合、市场、AI 页保留导航能力，没有整页白底或不可读的主按钮。
        for view in ["portfolio", "market", "agent"]:
            await page.locator(f'#sidebar [data-product-view="{view}"]').click()
            if view != "agent":
                await page.locator(".fund-content").evaluate("el => el.scrollTop = 0")
            await page.wait_for_timeout(200)
            if view == "portfolio":
                await expect(page.locator(".portfolio-chart-frame")).to_have_count(2)
                await expect(page.locator(".portfolio-chart-ticks span").first).to_have_css("font-size", "11px")
                # 收益贡献的柱子与其数值同色，正负号仍保留，风险警示色不受影响。
                for row in await page.locator(".portfolio-return-attribution article").all():
                    bar_color = await row.locator(".portfolio-attribution-track i").evaluate("el => getComputedStyle(el).backgroundColor")
                    value_color = await row.locator(".portfolio-attribution-value strong").evaluate("el => getComputedStyle(el).color")
                    assert bar_color == value_color, "贡献条与数值颜色不一致"
            await page.screenshot(path=str(output / f"{view}.png"))
        assert not errors, f"浏览器异常: {errors}"
        print(f"PASS: 3 viewports, chart pixels={colored_pixels}, axis contrast={contrast:.2f}; periods/comparison/tabs/race/model-context/news isolation/portfolio labels verified; no page errors.")
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
