"""FastAPI 应用创建文件。

这个文件负责四件事：
1. 创建 FastAPI 应用对象 `app`；
2. 定义根路径 `/`；
3. 把不同业务模块的 router 注册到应用里。
4. 在应用启动和关闭时管理 Cron、MCP，以及本地降级模式的 Job 线程。
"""

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.config import settings
# 每个 router 文件是一组接口。
# 这里导入 rag 后，下面 include_router(rag.router) 才能把 /rag/* 接口注册到应用里。
from app.routers import (
    agent_teams,
    auth,
    ai_usage,
    background_jobs,
    cron,
    evals,
    feedback,
    funds,
    fund_reports,
    health,
    knowledge,
    market,
    mcp,
    portfolios,
    tools,
    workspace,
)
from app.services.background_jobs import background_job_service
from app.services.auth import require_admin
from app.services.cron_scheduler import cron_scheduler_service
from app.services.daily_briefings import daily_briefing_service
from app.routers import daily_briefings
from app.routers import agent_authorizations
from app.services.fund_data_sync import fund_nav_sync_service
from app.services.market_data_sync import market_snapshot_sync_service
from app.services.market_intelligence_pipeline import (
    market_intelligence_pipeline_service,
)
from app.services.mcp_tools import mcp_tool_service
from app.services.portfolio_monitoring import portfolio_monitoring_service
from app.services.request_context import request_observability_middleware
from app.services.ai_governance_context import AIControlError
from app.services.deployment import production_request_guard, validate_production_settings
from app.services.tracing import TraceHTTPMiddleware, initialize_tracing, shutdown_tracing


@asynccontextmanager
async def lifespan(_: FastAPI):
    """管理需要伴随 FastAPI 进程运行的后台线程。"""

    validate_production_settings()
    initialize_tracing()

    # 每个工作日晚间由同一 Cron/Job 底座同步基金净值；这里只注册定义，不会在启动时
    # 直接访问 AKShare。关闭开关后仍可通过 REST 手动发起同步。
    if settings.fund_nav_sync_enabled:
        fund_nav_sync_service.ensure_daily_schedule(cron_scheduler_service)
    # 市场截面安排在基金净值之后，两个任务都复用同一 Cron/Job 基础设施。
    if settings.market_snapshot_sync_enabled:
        market_snapshot_sync_service.ensure_daily_schedule(cron_scheduler_service)
    # 市场情报默认改为页面手动触发。关闭开关时也主动删除旧版持久化 Cron，
    # 否则仅修改默认值并不能阻止磁盘中已经保存的日程在重启后继续运行。
    if settings.market_event_sync_enabled:
        market_intelligence_pipeline_service.ensure_daily_schedule(
            cron_scheduler_service
        )
    else:
        market_intelligence_pipeline_service.disable_daily_schedule(
            cron_scheduler_service
        )
    # 用户保存的组合监控策略在数据库中；启动时把已启用策略恢复成持久化 Cron。
    portfolio_monitoring_service.ensure_all_schedules(cron_scheduler_service)
    # s14 的调度线程必须在应用存活期间持续运行，不能依赖某个 HTTP 请求来启动。
    cron_scheduler_service.set_tick_callback(daily_briefing_service.tick)
    cron_scheduler_service.start()
    # 只补投已提交的投研任务并检查失联租约，不触发新的行情抓取。
    background_job_service.start_reliable_delivery()
    try:
        yield
    finally:
        # Celery 模式下 Worker 是独立进程，这里的 shutdown 不会关闭 Worker；
        # local 模式下则等待当前进程创建的线程自行结束。
        cron_scheduler_service.stop(wait=True)
        background_job_service.shutdown(wait=False)
        # s19 的 stdio MCP Server 是 FastAPI 启动的子进程，应用退出时必须回收。
        await mcp_tool_service.shutdown()
        shutdown_tracing()

# 创建 FastAPI 应用对象。
# title/version/description 会显示在自动生成的接口文档 `/docs` 中。
app = FastAPI(
    title=settings.app_name,
    version="1.0.0",
    description=(
        "A production-shaped FastAPI Agent workspace with persistent conversations, "
        "tool orchestration, RAG, background execution, teams, memory, and MCP."
    ),
    lifespan=lifespan,
)

# 先注册同源检查，再让观测中间件处于外层；被拒绝的请求也有 Request ID。
app.middleware("http")(production_request_guard)
app.middleware("http")(request_observability_middleware)
# 正式部署只接受公开域名；容器内部的探活使用 localhost，不经过公网入口。
if settings.deployment_mode == "production":
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=[settings.public_host, "localhost", "127.0.0.1"])
app.add_middleware(TraceHTTPMiddleware)
app.include_router(agent_authorizations.router)


@app.exception_handler(AIControlError)
async def ai_control_error_handler(request, exc: AIControlError):
    # 保留前端已认识的 detail 字符串，额外提供机器可读 code 和重试间隔。
    return JSONResponse(status_code=exc.status_code,
        content={"detail": str(exc), "code": exc.code, "retry_after": exc.retry_after},
        headers={"Retry-After": str(exc.retry_after)})

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"

# 托管前端静态文件。浏览器访问 /static/app.js、/static/styles.css 时会从这里读取。
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

# 小程序源码独立维护；这里只提供本地浏览器预览，不改变现有 Web 入口。
MINIPROGRAM_DIR = BASE_DIR.parent / "miniprogram"
app.mount("/mini-dist", StaticFiles(directory=MINIPROGRAM_DIR / "dist", check_dir=False), name="mini-dist")
app.mount("/mini-preview", StaticFiles(directory=MINIPROGRAM_DIR / "preview", check_dir=False), name="mini-preview")


@app.get("/mini", include_in_schema=False)
async def miniprogram_preview():
    """浏览器预览同一份小程序页面；它不是微信官方模拟器。"""

    if not (MINIPROGRAM_DIR / "dist" / "preview-manifest.json").is_file():
        raise HTTPException(status_code=503, detail="请先在 miniprogram 目录执行 pnpm build")
    return FileResponse(MINIPROGRAM_DIR / "preview" / "index.html", headers={"Cache-Control": "no-store"})


@app.get("/")
async def frontend():
    """返回 Agent Workspace 产品界面。"""
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/developer")
async def developer_console(_=Depends(require_admin)):
    """返回独立开发者入口；前端根据路径启用 Harness 与评测面板。"""

    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api")
async def api_index():
    """API 入口说明。"""

    return {
        "message": "Agent Workspace is running",
        "frontend": "/",
        "developer_console": "/developer",
        "docs": "/docs",
        "health": "/health",
        "product_chat": "/api/v1/agent/chat",
        "developer_chat": "/api/v1/developer/agent/chat",
        "workspace": "/api/v1/workspace",
        "ai_usage": "/api/v1/ai/usage",
        "conversations": "/api/v1/conversations",
        "knowledge": "/api/v1/knowledge",
        "funds": "/api/v1/funds",
        "fund_data_status": "/api/v1/funds/data-status",
        "fund_data_sync": "/api/v1/funds/data-sync",
        "market_sentiment": "/api/v1/market/sentiment",
        "retail_indices": "/api/v1/market/retail-indices",
        "market_indices": "/api/v1/market/indices",
        "market_intraday": "/api/v1/market/intraday",
        "market_turnover": "/api/v1/market/turnover",
        "market_turnover_history": "/api/v1/market/turnover/history",
        "market_daily_turnover": "/api/v1/market/turnover/daily",
        "market_data_status": "/api/v1/market/data-status",
        "market_data_sync": "/api/v1/market/data-sync",
        "large_capital": "/api/v1/market/large-capital",
        "large_capital_status": "/api/v1/market/large-capital/status",
        "large_capital_sync": "/api/v1/market/large-capital/data-sync",
        "market_events": "/api/v1/market/events",
        "market_hotspots": "/api/v1/market/hotspots",
        "market_hotspot_detail": "/api/v1/market/hotspots/{cluster_id}",
        "market_event_data_status": "/api/v1/market/events/data-status",
        "market_event_data_sync": "/api/v1/market/events/data-sync",
        "market_event_topics": "/api/v1/market/events/topics",
        "market_event_analysis": "/api/v1/market/events/analyze",
        "market_event_clustering": "/api/v1/market/events/cluster",
        "market_intelligence_pipeline": "/api/v1/market/events/pipeline",
        "social_signals": "/api/v1/market/social-signals",
        "social_signal_status": "/api/v1/market/social-signals/status",
        "social_signal_browser_session": "/api/v1/market/social-signals/browser-session",
        "social_signal_sync": "/api/v1/market/social-signals/data-sync",
        "fund_assessment": "/api/v1/funds/{fund_code}/assessment",
        "fund_semantic_review": "/api/v1/funds/{fund_code}/semantic-review",
        "portfolios": "/api/v1/portfolios",
        "portfolio_assessment": "/api/v1/portfolios/{portfolio_id}/assessment",
        "portfolio_exposure": "/api/v1/portfolios/{portfolio_id}/exposure",
        "portfolio_event_impacts": "/api/v1/portfolios/{portfolio_id}/event-impacts",
        "portfolio_research": "/api/v1/portfolios/{portfolio_id}/research-runs",
        "portfolio_research_comparison": "/api/v1/portfolios/{portfolio_id}/research-runs/{job_id}/comparison",
        "portfolio_adjustment": "/api/v1/portfolios/{portfolio_id}/adjustment-runs",
        "portfolio_adjustment_resume": "/api/v1/portfolios/{portfolio_id}/adjustment-runs/{job_id}/resume",
        "portfolio_monitoring": "/api/v1/portfolios/{portfolio_id}/monitoring",
        "portfolio_monitoring_run": "/api/v1/portfolios/{portfolio_id}/monitoring/runs",
        "eval_dashboard": "/api/v1/evals/dashboard",
        "run_evals": "/api/v1/evals/runs",
        "eval_drafts": "/api/v1/evals/drafts",
        "observability": "/api/v1/observability",
        "feedback_dashboard": "/api/v1/feedback/dashboard",
        # 下列接口是运维/集成 API，不再作为前端教学按钮逐个暴露。
        "tools": "/tools",
        "stateless_agent_chat": "/agent/chat",
        "background_jobs": "/background-jobs",
        "background_job_runtime": "/background-jobs/runtime",
        "background_job_events": "/background-jobs/{job_id}/events",
        "cron_schedules": "/cron-schedules",
        "agent_team_teammates": "/agent-teams/teammates",
        "agent_team_inbox": "/agent-teams/inbox",
        "agent_team_protocols": "/agent-teams/protocols",
        "agent_team_plan_submit": "/agent-teams/protocols/plans",
        "agent_team_shutdown": "/agent-teams/protocols/shutdown",
        "autonomy_tasks": "/agent-teams/autonomy/tasks",
        "autonomy_sessions": "/agent-teams/autonomy/sessions",
        "worktree_readiness": "/agent-teams/worktrees/readiness",
        "worktrees": "/agent-teams/worktrees",
        "mcp_state": "/mcp",
        "mcp_connect": "/mcp/servers/agent-learning/connect",
        "mcp_call": "/mcp/call",
    }


# 产品路由：浏览器只依赖版本化的 workspace 与 knowledge API。
app.include_router(health.router)
app.include_router(auth.router)
app.include_router(ai_usage.router)
app.include_router(workspace.router)
app.include_router(knowledge.router)
app.include_router(funds.router)
app.include_router(fund_reports.router)
app.include_router(market.router)
app.include_router(portfolios.router)
app.include_router(daily_briefings.router)
app.include_router(evals.router, dependencies=[Depends(require_admin)])
app.include_router(feedback.router)

# Harness 与运维路由：保留独立 API，方便 Worker、外部系统和自动化测试调用。
app.include_router(tools.router, dependencies=[Depends(require_admin)])
app.include_router(mcp.router, dependencies=[Depends(require_admin)])
app.include_router(background_jobs.router)
app.include_router(cron.router, dependencies=[Depends(require_admin)])
app.include_router(agent_teams.router, dependencies=[Depends(require_admin)])
