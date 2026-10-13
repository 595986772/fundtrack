"""启动临时记账验收页：python -m scripts.preview_portfolio_bookkeeping。

仅监听 127.0.0.1:8124，数据库放临时目录，进程退出后清理。
使用正式路由/状态机和正式前端模块，不启动 Cron，不写开发者的真实账本。
"""

import os
from pathlib import Path
import tempfile


def main():
    with tempfile.TemporaryDirectory(prefix="fundscope-bookkeeping-") as directory:
        os.environ["DATABASE_URL"] = f"sqlite:///{directory}/preview.db"
        from datetime import date
        from fastapi import FastAPI
        from fastapi.responses import FileResponse
        from fastapi.staticfiles import StaticFiles
        import uvicorn
        from app.routers import portfolios as router
        from app.services.portfolios import portfolio_service

        portfolio = portfolio_service.create(user_id="bookkeeping-preview", name="隔离测试账本", holdings=[{
            "fund_code": "110020", "units": 1000, "cost_amount": 1000, "purchase_date": date(2025, 1, 2),
        }])
        preview = FastAPI()
        preview.include_router(router.router)
        project = Path(__file__).resolve().parents[1]
        preview.mount("/static", StaticFiles(directory=project / "app/static"), name="static")

        @preview.get("/")
        def index():
            return FileResponse(project / "tests/frontend/fixtures/bookkeeping.html")

        @preview.get("/fixture")
        def fixture():
            return {"user_id": "bookkeeping-preview", "portfolio_id": portfolio["id"]}

        uvicorn.run(preview, host="127.0.0.1", port=8124, log_level="warning")


if __name__ == "__main__":
    main()
