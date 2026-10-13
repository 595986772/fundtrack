"""离线浏览器验收：正式组件 + 正式 REST + 临时数据库 + 人工行情，无真实用户写入。"""
from pathlib import Path
import tempfile
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
import uvicorn

from app.database import Database
from app.routers import portfolios
from app.services.auth import get_current_principal
from app.services.portfolio_stress import PortfolioStressService
from tests.test_portfolio_stress import inputs, PORTFOLIO

temp = tempfile.TemporaryDirectory()
database = Database(f"sqlite:///{Path(temp.name) / 'preview.db'}")
portfolios.portfolio_stress_service = PortfolioStressService(database, risk_engine=SimpleNamespace(capture_stress_inputs=lambda **kwargs: inputs()))
app = FastAPI()
app.include_router(portfolios.router)
app.dependency_overrides[get_current_principal] = lambda: SimpleNamespace(resolve_user_id=lambda user: "preview")
app.mount("/static", StaticFiles(directory=Path(__file__).resolve().parents[1] / "app/static"), name="static")


@app.get("/", response_class=HTMLResponse)
def page():
    return '''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>压力测试 · 离线验收</title><link rel="stylesheet" href="/static/styles.css"><link rel="stylesheet" href="/static/design-system.css"><link rel="stylesheet" href="/static/terminal.css"><link rel="stylesheet" href="/static/portfolio-stress.css">
<style>body{overflow:auto;background:#111215}main{max-width:1080px;margin:auto;padding:24px 16px;box-sizing:border-box}.fixture-label{color:#b0b1bb;font-size:12px;margin-bottom:20px}</style>
<body class="terminal-experience"><main><p class="fixture-label">离线人工行情，仅用于验证页面和计算流程</p><div id="sandbox"></div><p id="agent-prompt" class="fixture-label"></p></main>
<script src="https://unpkg.com/lucide@0.468.0/dist/umd/lucide.min.js"></script><script src="/static/vendor/echarts-6.0.0.min.js"></script><script src="/static/portfolio-stress.js"></script><script>
const controller=FundScopePortfolioStress.create({
 request: async (url, options) => { const response=await fetch(url,options);const result=await response.json();if(!response.ok)throw Error(result.detail);return result; },
 getContext:()=>({userId:'preview',portfolioId:'PORTFOLIO_ID'}),refreshIcons:()=>window.lucide?.createIcons(),
 askAgent: report=>{document.getElementById('agent-prompt').textContent='待发送AI的报告编号：'+report.run_id;}
});controller.mount(document.getElementById('sandbox'));
</script></body></html>'''.replace('PORTFOLIO_ID', PORTFOLIO)


if __name__ == "__main__":
    try: uvicorn.run(app, host="127.0.0.1", port=8768)
    finally: database.engine.dispose(); temp.cleanup()
