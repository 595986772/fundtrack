"""隔离浏览器验收：正式UI/接口/求解器 + 临时数据库；解析器替身不访问真实模型。"""
from fastapi.responses import HTMLResponse
import uvicorn

from tests.preview_portfolio_stress import app, database, temp, portfolios, page, PORTFOLIO
from tests.test_portfolio_allocation import TYPES
from app.services.portfolio_allocation import PortfolioAllocationService
from app.services.portfolio_allocation_contracts import AllocationProposal
from types import SimpleNamespace


def parser(question):
    # 只在隔离验收进程使用，用于走通模型成功、模糊需求、失败等前端状态。
    if "失败" in question:
        from app.services.portfolio_stress import PortfolioStressError
        raise PortfolioStressError("需求整理失败，请重试")
    return AllocationProposal(min_bond_weight_pct=40, max_single_fund_weight_pct=70,
        max_turnover_pct=None if "不大幅" in question else 20,
        questions=["不大幅调仓具体是多少单边换手？"] if "不大幅" in question else [])


portfolios.portfolio_allocation_service = PortfolioAllocationService(database, stress=portfolios.portfolio_stress_service,
    funds=SimpleNamespace(list_profiles=lambda: TYPES), parser=parser)


@app.get("/allocation", response_class=HTMLResponse)
def allocation_page():
    html = page().replace("压力测试 · 离线验收", "约束求解 · 离线验收")
    html = html.replace('<style>', '<link rel="stylesheet" href="/static/portfolio-allocation.css"><style>')
    html = html.replace('<script>\nconst controller', '<script src="/static/portfolio-allocation.js"></script><script>\nconst controller')
    html = html.replace('FundScopePortfolioStress.create', 'FundScopePortfolioAllocation.create')
    html = html.replace("report.run_id", "report.draft_id")
    return html


if __name__ == "__main__":
    try: uvicorn.run(app, host="127.0.0.1", port=8768)
    finally: database.engine.dispose(); temp.cleanup()
