"""多角色投研 Web 验收：真实页面 + 浏览器接口夹具，不写用户库或调用真实模型。

先启动本地 FastAPI，再从项目根目录运行 python -m scripts.check_research_committee_ui。
报告来自真实 LangGraph 配合假模型，截图保存到 .impeccable/review。
"""

import asyncio
import argparse
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from playwright.async_api import async_playwright, expect

from app.services.portfolio_diagnosis import report_with_diagnosis
from app.services.portfolio_report_evidence import PortfolioReportEvidenceService
from app.services.portfolio_research_plan import build_research_plan
from tests.test_portfolio_report_evidence import reader_for, report_pair
from tests.test_portfolio_research_committee import CommitteeModel
from tests.test_portfolio_research_graph import build_graph_service, FakePortfolioService, PORTFOLIO_ID


def report_fixture(*, degraded=False):
    model = CommitteeModel(edits={"risk_analysis": RuntimeError("test-only")} if degraded else None)
    graph = build_graph_service()
    graph.report_evidence = PortfolioReportEvidenceService(reader_for(report_pair()), max_funds=1)
    plan = build_research_plan("深入比较两期季报的策略变化", "committee", investor_context={"horizon": "三年"})
    with patch("app.services.portfolio_research_committee.get_chat_model", side_effect=model.create):
        result = graph.run(run_id="job-preview-committee", user_id="chen",
            portfolio_id=PORTFOLIO_ID, research_mode="committee", research_plan=plan)
    return report_with_diagnosis(result)


async def main(base_url="http://127.0.0.1:8000"):
    report = report_fixture()
    degraded = report_fixture(degraded=True)
    portfolio = FakePortfolioService().get(user_id="chen", portfolio_id=PORTFOLIO_ID)
    output = Path(__file__).resolve().parents[1] / ".impeccable" / "review"
    output.mkdir(parents=True, exist_ok=True)
    requests, errors = [], []
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        page = await browser.new_page(viewport={"width": 1440, "height": 1000})
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.on("dialog", lambda dialog: dialog.accept())

        async def intercept(route):
            request = route.request
            path = request.url.split("?")[0]
            # 所有写操作都在浏览器截断，保证验收不能产生付费任务或修改持仓。
            if request.method not in {"GET", "HEAD", "OPTIONS"}:
                if path.endswith(f"/{PORTFOLIO_ID}/research-runs"):
                    requests.append(request.post_data_json)
                    await route.fulfill(json={"ok": True, "accepted": True, "job": {
                        "id": report["run_id"], "status": "completed", "current_step": 13,
                        "total_steps": 13, "progress_percent": 100,
                    }})
                else:
                    await route.fulfill(status=409, json={"detail": "浏览器验收禁止写入"})
                return
            if f"/{PORTFOLIO_ID}/research-runs" in path:
                if path.endswith("research-runs"):
                    await route.fulfill(json={"runs": [], "count": 0, "latest_report": None})
                else:
                    await route.fulfill(json=report)
                return
            await route.continue_()

        await page.route("**/api/**", intercept)
        await page.goto(base_url + "/#research/reports", wait_until="networkidle")
        # 仅替换当前浏览器的展示状态，不落到 localStorage 或服务端。
        await page.evaluate("""portfolio => {
            state.portfolios = [portfolio]; state.selectedPortfolioId = portfolio.id;
            state.portfolioResearchHistory = []; state.portfolioResearchHistoryLoading = false;
            state.portfolioResearchResult = null; state.portfolioResearchError = '';
            state.portfolioResearchRunning = false; state.portfolioReportLoading = false;
            state.portfolioReportReadJobId = ''; state.portfolioResearchMode = 'standard';
            activateProductView('portfolio', {page:'research'});
            renderPortfolioWorkbench({pages:['research']});
        }""", portfolio)
        await expect(page.locator("[data-portfolio-research-mode]:visible")).to_have_value("standard")
        await page.locator("[data-portfolio-research-mode]:visible").select_option("committee")
        await page.locator("[data-research-question]:visible").fill("深入比较两期季报的策略变化")
        await page.locator('.research-personal-fields > summary:visible').click()
        await page.locator('[data-research-personal="horizon"]:visible').fill("三年")
        await expect(page.locator('[data-research-refresh]:visible')).not_to_be_checked()
        assert requests == [], "切换模式不能启动研究"
        await page.screenshot(path=str(output / "research-plan-desktop.png"), full_page=True)
        await page.set_viewport_size({"width": 390, "height": 844})
        await page.screenshot(path=str(output / "research-plan-mobile.png"), full_page=True)
        assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth"), "输入页面溢出"
        launch = page.locator("[data-run-portfolio-research]:visible").first
        await launch.scroll_into_view_if_needed()
        # 页面使用内部滚动容器；验证按钮滚入后能点击，而不是被固定底栏盖住。
        assert await launch.evaluate("""el => { const r=el.getBoundingClientRect();
            return el.contains(document.elementFromPoint(r.x+r.width/2,r.y+r.height/2)); }"""), "启动按钮被遮挡"
        await page.screenshot(path=str(output / "research-plan-mobile-action.png"), full_page=True)
        await launch.click()
        await expect(page.locator(".research-committee")).to_be_visible()
        assert requests and requests[0]["research_mode"] == "committee"
        assert requests[0]["question"] == report["research_plan"]["question"]
        assert requests[0]["investor_context"] == {"horizon": "三年"}
        assert requests[0]["allow_public_refresh"] is False
        await page.locator('.research-coverage > summary').click()
        await expect(page.locator('.research-coverage')).to_contain_text("doc-110020-2026-Q1")
        await page.locator('.research-coverage details > summary').first.click()
        await expect(page.locator('.research-coverage blockquote').first).to_contain_text("关注估值风险")
        await page.locator('.research-coverage').scroll_into_view_if_needed()
        await page.screenshot(path=str(output / "research-evidence-mobile.png"), full_page=True)
        await expect(page.locator('[data-committee-role="opportunity"]')).not_to_be_visible()
        await page.locator('.committee-analysis > summary').click()
        await expect(page.locator('[data-committee-role="opportunity"]')).to_contain_text("机会研究员")
        await expect(page.locator('[data-committee-role="risk"]')).to_contain_text("风险研究员")
        # 等操作提示消失再截图，不把短暂 Toast 遮挡当作页面的稳定布局。
        await expect(page.locator(".toast")).to_have_count(0, timeout=6000)

        for name, width, height in [("desktop", 1440, 1000), ("mobile", 390, 844)]:
            await page.set_viewport_size({"width": width, "height": height})
            await page.locator(".research-committee").scroll_into_view_if_needed()
            await page.screenshot(path=str(output / f"committee-{name}.png"), full_page=True)
            assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth"), f"{name} 页面横向溢出"
            assert await page.locator(".research-committee").evaluate("el => el.scrollWidth <= el.clientWidth + 1"), f"{name} 角色区溢出"

        await page.set_viewport_size({"width": 1440, "height": 1000})
        await page.evaluate("""report => { state.portfolioResearchResult=report;
            renderPortfolioWorkbench({pages:['research']}); }""", degraded)
        await expect(page.locator(".research-committee")).to_contain_text("失败")
        await page.locator(".research-committee").scroll_into_view_if_needed()
        await page.screenshot(path=str(output / "committee-degraded.png"), full_page=True)

        # 旧报告没有 committee 字段仍可展示，不能冒充多角色研判。
        legacy = deepcopy(report)
        del legacy["committee"]
        legacy.pop("research_plan", None)
        legacy["market_snapshot"].pop("fund_reports", None)
        legacy["market_snapshot"].pop("research_repair", None)
        legacy["generation"]["mode"] = "structured_model_synthesis"
        await page.evaluate("""report => { state.portfolioResearchResult=report;
            renderPortfolioWorkbench({pages:['research']}); }""", legacy)
        await expect(page.locator(".research-committee")).to_have_count(0)
        assert len(requests) == 1, "查看已有报告不应再次启动模型"
        assert not errors, errors
        await browser.close()
    print("PASS: mode selection, explicit launch, valid/degraded/legacy reports, desktop/mobile layout; no real writes or model calls")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    asyncio.run(main(parser.parse_args().base_url.rstrip("/")))
