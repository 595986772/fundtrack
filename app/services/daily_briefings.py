"""交易日简报的控制面：保存开关、判断北京时间、创建幂等的投研 Job。

这里只负责“何时做、给谁做”。刷新、冻结证据和模型分析仍由原来的 Worker 执行。
同一用户、同一组合、同一交易日共用一个任务键，多个 API 进程同时触发也只建一单。
"""

from datetime import datetime, time, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.database import Database, runtime_database
from app.models import BackgroundJobModel, PortfolioDailyBriefingPolicyModel
from app.services.fund_transaction_timing import CHINA_TIMEZONE, _calendar
from app.services.portfolios import portfolio_service


RUN_TIME = time(14, 30)
LAST_START = time(14, 40)


def china_now() -> datetime:
    return datetime.now(CHINA_TIMEZONE)


def trading_day(moment: datetime, calendar=None) -> bool:
    """日历越界就报错，不能把节假日误判成普通工作日。"""
    return bool((calendar or _calendar()).is_session(moment.astimezone(CHINA_TIMEZONE).date().isoformat()))


class DailyBriefingService:
    def __init__(self, *, database: Database = runtime_database,
                 portfolios: Any = portfolio_service, jobs=None, calendar=None, clock=china_now):
        self.database, self.portfolios = database, portfolios
        self._jobs, self.calendar, self.clock = jobs, calendar, clock
        self.database.create_schema()

    @property
    def jobs(self):
        # 延迟导入，避免 Scheduler -> Jobs -> Worker -> Scheduler 的循环依赖。
        if self._jobs is None:
            from app.services.background_jobs import background_job_service
            return background_job_service
        return self._jobs

    def configure(self, *, user_id: str, portfolio_id: str, enabled: bool) -> dict:
        self.portfolios.get(user_id=user_id, portfolio_id=portfolio_id)
        # 两个浏览器首次同时保存时，让数据库唯一主键负责仲裁。
        for attempt in range(2):
            try:
                with self.database.session() as session, session.begin():
                    row = session.get(PortfolioDailyBriefingPolicyModel, (user_id, portfolio_id))
                    if row is None:
                        row = PortfolioDailyBriefingPolicyModel(user_id=user_id, portfolio_id=portfolio_id)
                        session.add(row)
                    row.enabled, row.last_error, row.updated_at = enabled, None, self.clock()
                break
            except IntegrityError:
                if attempt:
                    raise
        return self.overview(user_id=user_id, portfolio_id=portfolio_id)

    def overview(self, *, user_id: str, portfolio_id: str) -> dict:
        """页面查询只读；打开页面绝不隐式刷新数据或调用模型。"""
        self.portfolios.get(user_id=user_id, portfolio_id=portfolio_id)
        now = self.clock().astimezone(CHINA_TIMEZONE)
        prefix = f"briefing:{portfolio_id}:"
        with self.database.session() as session:
            row = session.get(PortfolioDailyBriefingPolicyModel, (user_id, portfolio_id))
            policy = {"enabled": bool(row and row.enabled), "last_error": row.last_error if row else None,
                      "time": "14:30", "timezone": "Asia/Shanghai", "catch_up_until": "14:40"}
            runs = session.scalars(select(BackgroundJobModel).where(
                BackgroundJobModel.user_id == user_id,
                BackgroundJobModel.idempotency_key.startswith(prefix),
            ).order_by(BackgroundJobModel.created_at.desc()).limit(20)).all()
            history = [self._summary(item) for item in runs]

        calendar_status, next_run = "available", None
        try:
            for offset in range(40):
                day = now + timedelta(days=offset)
                planned = datetime.combine(day.date(), RUN_TIME, CHINA_TIMEZONE)
                already = any(run["trade_date"] == day.date().isoformat() for run in history)
                if trading_day(day, self.calendar) and not already and (
                    planned >= now or (offset == 0 and now.time() < LAST_START)
                ):
                    next_run = max(planned, now).isoformat()
                    break
            if next_run is None:
                calendar_status = "unavailable"
        except (ValueError, KeyError, IndexError):
            calendar_status = "unavailable"
        return {"portfolio_id": portfolio_id, "policy": policy,
                "calendar_status": calendar_status, "next_run_at": next_run if policy["enabled"] else None,
                "runs": history}

    def run_today(self, *, user_id: str, portfolio_id: str, trigger: str = "manual", now=None) -> dict:
        now = (now or self.clock()).astimezone(CHINA_TIMEZONE)
        portfolio = self.portfolios.get(user_id=user_id, portfolio_id=portfolio_id)
        if not portfolio.get("holdings"):
            raise ValueError("请先为组合添加持仓。")
        try:
            is_open = trading_day(now, self.calendar)
        except (ValueError, KeyError, IndexError) as exc:
            raise ValueError("交易日历未覆盖今天，请更新日历后再运行。") from exc
        if not is_open:
            raise ValueError("今天不是 A 股交易日，未创建盘中简报。")
        # 手动可以在盘中提前试跑，也可以收盘后生成当日截面；早于开市没有当天行情。
        if now.time() < time(9, 30):
            raise ValueError("请在交易日 09:30 后运行，当前还没有当日盘中行情。")
        if trigger == "scheduled" and not RUN_TIME <= now.time() < LAST_START:
            raise ValueError("已错过今天 14:30 至 14:40 的自动启动窗口。")
        date_text = now.date().isoformat()
        return self.jobs.start_job(
            user_id=user_id, job_type="portfolio_research", portfolio_id=portfolio_id,
            title=f"{date_text} 每日简报 · {portfolio['name']}", portfolio_months=12,
            timeout_seconds=900, max_retries=0,
            idempotency_key=f"briefing:{portfolio_id}:{date_text}",
            daily_briefing={"trade_date": date_text, "trigger": trigger,
                           "requested_at": now.isoformat(), "timezone": "Asia/Shanghai"},
        )

    def tick(self, moment: datetime) -> None:
        """每分钟检查一次，只有十分钟启动窗口内才访问策略表和交易日历。"""
        now = moment.astimezone(CHINA_TIMEZONE)
        if not RUN_TIME <= now.time() < LAST_START:
            return
        with self.database.session() as session:
            keys = list(session.execute(select(PortfolioDailyBriefingPolicyModel.user_id,
                PortfolioDailyBriefingPolicyModel.portfolio_id).where(
                    PortfolioDailyBriefingPolicyModel.enabled.is_(True))))
        for user_id, portfolio_id in keys:
            error = None
            try:
                if not trading_day(now, self.calendar):
                    continue
                self.run_today(user_id=user_id, portfolio_id=portfolio_id, trigger="scheduled", now=now)
            except Exception as exc:
                # 不把供应商原始异常、请求头或凭据写进用户能看到的页面。
                error = f"自动建单未成功（{type(exc).__name__}），请检查日历、持仓或执行资源。"
            with self.database.session() as session, session.begin():
                row = session.get(PortfolioDailyBriefingPolicyModel, (user_id, portfolio_id))
                if row:
                    row.last_error = error

    @staticmethod
    def _summary(row) -> dict:
        result = row.result or {}
        briefing = (result.get("market_snapshot") or {}).get("daily_briefing") or result.get("daily_briefing_input") or {}
        return {"job_id": row.id, "trade_date": (row.payload.get("daily_briefing") or {}).get("trade_date"),
                "trigger": (row.payload.get("daily_briefing") or {}).get("trigger"),
                "status": row.status, "progress_message": row.progress_message,
                "created_at": row.created_at, "completed_at": row.completed_at,
                "headline": result.get("headline"), "summary": result.get("summary"),
                "model_used": (result.get("model_interpretation") or {}).get("model_used", False),
                "error": "任务未完成，请检查数据状态和 AI 额度，再从任务记录排查。" if row.error else None,
                "captured_at": briefing.get("captured_at"), "sources": briefing.get("sources", []),
                "warnings": briefing.get("warnings", []), "gate": briefing.get("gate")}


daily_briefing_service = DailyBriefingService()
