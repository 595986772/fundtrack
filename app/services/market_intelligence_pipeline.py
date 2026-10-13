"""市场情报自动化流水线：把采集、模型处理和组合预警串成一个 Job。

这层只负责步骤编排，不重新实现任何领域算法：

1. MarketEventSyncService 采集并幂等写入原始事件；
2. MarketEventAnalysisService 让 DeepSeek 分批提取结构化语义；
3. MarketEventClusterService 用 Embedding 聚合重复报道；
4. MarketHotspotTrackingService 在聚类阶段保存可回放的热度快照；
5. PortfolioEventImpactService 计算事件实际影响的持仓比例；
6. PortfolioMonitoringService 比较快照并按 Python 阈值创建预警。

每个 Service 仍可单独测试和调用。Pipeline 只是让生产环境不再依赖用户手动依次
点击三个按钮，同时把阶段结果交给 Background Job 保存为可恢复检查点。
"""

from __future__ import annotations

from hashlib import sha256
from typing import Any, Callable, Protocol

from app.config import settings
from app.services.market_event_analysis import market_event_analysis_service
from app.services.market_event_clusters import market_event_cluster_service
from app.services.market_event_sync import (
    MARKET_EVENT_SYNC_SCHEDULE_ID,
    MARKET_EVENT_SYNC_SYSTEM_USER_ID,
    market_event_sync_service,
)
from app.services.market_hotspots import market_hotspot_tracking_service
from app.services.market_topic_keywords import market_topic_keyword_service
from app.services.portfolio_event_impacts import portfolio_event_impact_service
from app.services.portfolio_monitoring import portfolio_monitoring_service
from app.services.social_signals import social_signal_service


PIPELINE_STAGE_KEYS = (
    "event_sync",
    "semantic_analysis",
    "event_clustering",
    "portfolio_impacts",
    "portfolio_monitoring",
)
PIPELINE_TOTAL_STEPS = len(PIPELINE_STAGE_KEYS)

# stage_callback 在一个阶段完整成功后调用。JobRunner 会在同一条 SQL 更新中保存
# current_step 和该阶段结果，所以进程在下一阶段崩溃时可以从检查点继续。
PipelineStageCallback = Callable[[int, str, str, dict[str, Any]], None]
PipelineCancellationCheck = Callable[[], None]


class EventSyncer(Protocol):
    def sync(
        self,
        *,
        keyword_expansion: dict[str, Any] | None = None,
    ) -> dict[str, Any]: ...

    def status(self) -> dict[str, Any]: ...


class EventAnalyzer(Protocol):
    async def analyze_pending(self, *, limit: int = 10) -> dict[str, Any]: ...


class EventClusterer(Protocol):
    def build(self) -> dict[str, Any]: ...


class HotspotTracker(Protocol):
    def capture(self) -> dict[str, Any]: ...


class SocialSignalRefresher(Protocol):
    async def refresh(self) -> dict[str, Any]: ...


class TopicKeywordExpander(Protocol):
    async def expand(
        self,
        *,
        topics: list[str] | tuple[str, ...],
        use_model: bool,
    ) -> dict[str, Any]: ...


class EventImpactReader(Protocol):
    def analyze(
        self,
        *,
        user_id: str,
        portfolio_id: str,
        limit: int,
    ) -> dict[str, Any]: ...


class PortfolioMonitor(Protocol):
    def list_enabled_policies(
        self,
        *,
        user_id: str | None = None,
    ) -> list[dict[str, Any]]: ...

    def run_monitor(self, **kwargs: Any) -> dict[str, Any]: ...


class MarketIntelligencePipelineService:
    """执行五阶段市场情报流水线，并生成紧凑的结构化运行结果。"""

    def __init__(
        self,
        *,
        syncer: EventSyncer = market_event_sync_service,
        analyzer: EventAnalyzer = market_event_analysis_service,
        clusterer: EventClusterer = market_event_cluster_service,
        hotspot_tracker: HotspotTracker | None = None,
        social_refresher: SocialSignalRefresher | None = None,
        impacts: EventImpactReader = portfolio_event_impact_service,
        monitoring: PortfolioMonitor = portfolio_monitoring_service,
        topic_expander: TopicKeywordExpander = market_topic_keyword_service,
        max_analysis_batches: int | None = None,
    ) -> None:
        self.syncer = syncer
        self.analyzer = analyzer
        self.clusterer = clusterer
        # 普通隔离测试默认不触碰运行时数据库；产品级实例在文件末尾显式注入。
        self.hotspot_tracker = hotspot_tracker
        self.social_refresher = social_refresher
        self.impacts = impacts
        self.monitoring = monitoring
        self.topic_expander = topic_expander
        configured_batches = (
            settings.market_intelligence_max_analysis_batches
            if max_analysis_batches is None
            else int(max_analysis_batches)
        )
        self.max_analysis_batches = max(1, min(configured_batches, 10))

    async def run(
        self,
        *,
        run_id: str,
        requesting_user_id: str,
        market_topics: list[str] | tuple[str, ...] | None = None,
        use_model_topic_expansion: bool = True,
        resume_from_step: int = 0,
        previous_stage_results: dict[str, Any] | None = None,
        stage_callback: PipelineStageCallback | None = None,
        cancellation_check: PipelineCancellationCheck | None = None,
    ) -> dict[str, Any]:
        """运行或恢复流水线；系统 Cron 可处理全部用户，手动请求只能处理本人。"""

        stages = dict(previous_stage_results or {})
        resume_step = self._safe_resume_step(
            requested_step=resume_from_step,
            stages=stages,
        )

        def check_runtime() -> None:
            if cancellation_check is not None:
                cancellation_check()

        def complete_stage(
            step: int,
            key: str,
            message: str,
            result: dict[str, Any],
        ) -> None:
            stages[key] = result
            if stage_callback is not None:
                stage_callback(step, key, message, result)

        if resume_step < 1:
            check_runtime()
            topics = list(market_topics or [])
            if topics:
                keyword_expansion = await self.topic_expander.expand(
                    topics=topics,
                    use_model=use_model_topic_expansion,
                )
                check_runtime()
                sync_result = self.syncer.sync(
                    keyword_expansion=keyword_expansion,
                )
            else:
                # 不选板块时保持原来的全市场采集，也让旧 Fake Syncer 无需接收新参数。
                sync_result = self.syncer.sync()
            # 社交来源属于可选补充证据。未配置 Token 或单个供应商失败时，把状态写入
            # 当前阶段但继续处理官方来源，不能让低等级数据拖垮整条市场情报流水线。
            if self.social_refresher is not None:
                try:
                    social_result = await self.social_refresher.refresh()
                except Exception as exc:
                    social_result = {
                        "workflow": "social_signal_refresh",
                        "status": "failed",
                        "summary": f"社交观点采集失败，已保留其他市场来源：{exc}",
                    }
                sync_result = {**sync_result, "social_signals": social_result}
            complete_stage(
                1,
                "event_sync",
                "原始市场事件已同步并写入数据库。",
                sync_result,
            )

        if resume_step < 2:
            analysis_result = await self._analyze_all_pending(
                cancellation_check=check_runtime,
            )
            complete_stage(
                2,
                "semantic_analysis",
                "待处理事件已完成分批语义复核。",
                analysis_result,
            )

        if resume_step < 3:
            check_runtime()
            cluster_result = self.clusterer.build()
            # 聚类表下一次会被整体替换，所以必须在这里把当前状态复制到历史表。
            # 热点追踪只做数据库读取和 Python 比较，不会增加模型调用。
            if self.hotspot_tracker is not None:
                hotspot_result = self.hotspot_tracker.capture()
                cluster_result = {
                    **cluster_result,
                    "hotspot_tracking": hotspot_result,
                }
            complete_stage(
                3,
                "event_clustering",
                "事件去重簇与热点历史快照已更新。",
                cluster_result,
            )

        # 普通用户手动运行时只能读取自己的启用策略。只有内部 Cron 用户才能扫描
        # 全部启用组合，避免一个用户借流水线看到其他人的组合摘要。
        policy_user_id = (
            None
            if requesting_user_id == MARKET_EVENT_SYNC_SYSTEM_USER_ID
            else requesting_user_id
        )
        policies = self.monitoring.list_enabled_policies(user_id=policy_user_id)

        # 正常执行时第四阶段的完整结果会直接传给第五阶段，避免重复穿透持仓。
        # 如果 Worker 在第四阶段之后崩溃，重试时会重新做这次廉价的确定性计算；
        # SQL 检查点只保存紧凑摘要，不把全部底层证券塞进 Job 表。
        impact_reports, impact_summary = self._build_portfolio_impacts(
            policies=policies,
            cancellation_check=check_runtime,
        )
        if resume_step < 4:
            complete_stage(
                4,
                "portfolio_impacts",
                "已计算启用组合与最新事件之间的持仓关联。",
                impact_summary,
            )

        if resume_step < 5:
            monitoring_result = self._run_portfolio_monitors(
                run_id=run_id,
                policies=policies,
                impact_reports=impact_reports,
                cancellation_check=check_runtime,
            )
            complete_stage(
                5,
                "portfolio_monitoring",
                "组合快照已比较，达到阈值的变化已生成预警。",
                monitoring_result,
            )

        analysis = stages.get("semantic_analysis", {})
        clustering = stages.get("event_clustering", {})
        hotspot_tracking = clustering.get("hotspot_tracking", {})
        monitoring = stages.get("portfolio_monitoring", {})
        pending_count = int(analysis.get("pending_analysis_count", 0))
        alert_count = int(monitoring.get("alert_count", 0))
        portfolio_count = int(monitoring.get("portfolio_count", 0))
        return {
            "data_kind": "market_intelligence_pipeline_run",
            "workflow": "market_intelligence_pipeline",
            "status": "completed_with_pending" if pending_count else "completed",
            "run_id": run_id,
            "summary": (
                f"市场情报流水线完成：语义处理 "
                f"{analysis.get('processed_count', 0)} 条，形成 "
                f"{clustering.get('cluster_count', 0)} 个事件簇，检查 "
                f"并记录 {hotspot_tracking.get('captured_count', 0)} 个热点，检查 "
                f"{portfolio_count} 个启用组合，生成 {alert_count} 条预警。"
            ),
            "stages": stages,
            "pending_analysis_count": pending_count,
            "monitored_portfolio_count": portfolio_count,
            "alert_count": alert_count,
        }

    async def _analyze_all_pending(
        self,
        *,
        cancellation_check: PipelineCancellationCheck,
    ) -> dict[str, Any]:
        """每批最多十条，循环到清空或达到配置上限。"""

        batches: list[dict[str, Any]] = []
        for _ in range(self.max_analysis_batches):
            cancellation_check()
            result = await self.analyzer.analyze_pending(limit=10)
            requested_count = int(result.get("requested_count", 0))
            if requested_count == 0:
                break
            batches.append(result)

        # status 直接查数据库，不会再次调用模型。它告诉前端是否仍有积压。
        status = self.syncer.status()
        return {
            "workflow": "market_event_analysis_batches",
            "batch_count": len(batches),
            "batch_limit": self.max_analysis_batches,
            "processed_count": sum(
                int(item.get("requested_count", 0)) for item in batches
            ),
            "accepted_count": sum(
                int(item.get("accepted_count", 0)) for item in batches
            ),
            "abstained_count": sum(
                int(item.get("abstained_count", 0)) for item in batches
            ),
            "pending_analysis_count": int(
                status.get("pending_analysis_count", 0)
            ),
            "batches": batches,
        }

    def _build_portfolio_impacts(
        self,
        *,
        policies: list[dict[str, Any]],
        cancellation_check: PipelineCancellationCheck,
    ) -> tuple[dict[tuple[str, str], dict[str, Any]], dict[str, Any]]:
        reports: dict[tuple[str, str], dict[str, Any]] = {}
        summaries: list[dict[str, Any]] = []
        for policy in policies:
            cancellation_check()
            key = (policy["user_id"], policy["portfolio_id"])
            report = self.impacts.analyze(
                user_id=key[0],
                portfolio_id=key[1],
                limit=30,
            )
            reports[key] = report
            top = (report.get("impacts") or [None])[0]
            summaries.append(
                {
                    "user_id": key[0],
                    "portfolio_id": key[1],
                    "portfolio_name": report.get("portfolio_name", ""),
                    "matched_event_count": int(report.get("total_matched", 0)),
                    "caution_event_count": int(
                        report.get("caution_event_count", 0)
                    ),
                    "top_impact": (
                        {
                            "title": top.get("title"),
                            "direction": top.get("direction"),
                            "affected_exposure_pct": top.get(
                                "affected_exposure_pct"
                            ),
                        }
                        if top
                        else None
                    ),
                }
            )
        return reports, {
            "workflow": "portfolio_event_impact_refresh",
            "portfolio_count": len(policies),
            "matched_event_count": sum(
                item["matched_event_count"] for item in summaries
            ),
            "portfolios": summaries,
        }

    def _run_portfolio_monitors(
        self,
        *,
        run_id: str,
        policies: list[dict[str, Any]],
        impact_reports: dict[tuple[str, str], dict[str, Any]],
        cancellation_check: PipelineCancellationCheck,
    ) -> dict[str, Any]:
        results = []
        for policy in policies:
            cancellation_check()
            key = (policy["user_id"], policy["portfolio_id"])
            result = self.monitoring.run_monitor(
                user_id=key[0],
                portfolio_id=key[1],
                run_id=self._portfolio_run_id(
                    pipeline_run_id=run_id,
                    user_id=key[0],
                    portfolio_id=key[1],
                ),
                event_impact_report=impact_reports[key],
            )
            results.append(
                {
                    "user_id": key[0],
                    "portfolio_id": key[1],
                    "status": result.get("status"),
                    "summary": result.get("summary"),
                    "alert_count": int(result.get("alert_count", 0)),
                    "alerts": [
                        {
                            "id": item.get("id"),
                            "alert_type": item.get("alert_type"),
                            "severity": item.get("severity"),
                            "title": item.get("title"),
                        }
                        for item in result.get("alerts", [])
                    ],
                }
            )
        return {
            "workflow": "portfolio_monitoring_batch",
            "portfolio_count": len(results),
            "alert_count": sum(item["alert_count"] for item in results),
            "portfolios": results,
        }

    def ensure_daily_schedule(self, scheduler: Any) -> dict[str, Any]:
        """把旧的“只同步事件”Cron 原位升级为完整市场情报流水线。"""

        schedules = scheduler.list_schedules(
            user_id=MARKET_EVENT_SYNC_SYSTEM_USER_ID,
            max_results=100,
        )["schedules"]
        existing = next(
            (
                item
                for item in schedules
                if item["id"] == MARKET_EVENT_SYNC_SCHEDULE_ID
            ),
            None,
        )
        matches = bool(
            existing
            and existing["cron"] == settings.market_event_sync_cron
            and existing["job_type"] == "market_intelligence_pipeline"
        )
        if existing is not None and not matches:
            scheduler.cancel_schedule(
                user_id=MARKET_EVENT_SYNC_SYSTEM_USER_ID,
                schedule_id=MARKET_EVENT_SYNC_SCHEDULE_ID,
            )
            existing = None
        if existing is not None:
            return {"ok": True, "created": False, "schedule": existing}

        result = scheduler.create_schedule(
            user_id=MARKET_EVENT_SYNC_SYSTEM_USER_ID,
            schedule_id=MARKET_EVENT_SYNC_SCHEDULE_ID,
            cron=settings.market_event_sync_cron,
            prompt="更新 FundScope 市场情报并检查已启用组合",
            job_type="market_intelligence_pipeline",
            recurring=True,
            durable=True,
        )
        return {"ok": True, "created": True, "schedule": result["schedule"]}

    def disable_daily_schedule(self, scheduler: Any) -> dict[str, Any]:
        """删除市场情报的持久化 Cron，让刷新只由页面按钮触发。

        配置开关可能是在日程已经写入磁盘之后才关闭的，所以不能只是不再调用
        ``ensure_daily_schedule``。这里按固定系统用户和固定 schedule ID 查找并删除；
        重复启动时找不到日程也视为成功，保持操作幂等。
        """

        schedules = scheduler.list_schedules(
            user_id=MARKET_EVENT_SYNC_SYSTEM_USER_ID,
            max_results=100,
        )["schedules"]
        existing = next(
            (
                item
                for item in schedules
                if item["id"] == MARKET_EVENT_SYNC_SCHEDULE_ID
            ),
            None,
        )
        if existing is None:
            return {
                "ok": True,
                "removed": False,
                "schedule": None,
                "message": "市场情报已处于手动刷新模式。",
            }

        cancelled = scheduler.cancel_schedule(
            user_id=MARKET_EVENT_SYNC_SYSTEM_USER_ID,
            schedule_id=MARKET_EVENT_SYNC_SCHEDULE_ID,
        )
        return {
            "ok": True,
            "removed": True,
            "schedule": cancelled["schedule"],
            "message": "市场情报自动日程已删除，后续只响应手动刷新。",
        }

    @staticmethod
    def _safe_resume_step(
        *,
        requested_step: int,
        stages: dict[str, Any],
    ) -> int:
        """只信任结果也已落库的连续步骤，避免进度数字与检查点不一致。"""

        requested = max(0, min(int(requested_step), PIPELINE_TOTAL_STEPS))
        contiguous = 0
        for index, key in enumerate(PIPELINE_STAGE_KEYS, start=1):
            if index > requested or key not in stages:
                break
            contiguous = index
        return contiguous

    @staticmethod
    def _portfolio_run_id(
        *,
        pipeline_run_id: str,
        user_id: str,
        portfolio_id: str,
    ) -> str:
        """相同 Pipeline Job 重试时生成相同监控 ID，复用现有 SQL 幂等约束。"""

        digest = sha256(
            f"{pipeline_run_id}:{user_id}:{portfolio_id}".encode("utf-8")
        ).hexdigest()[:20]
        return f"pipeline-{digest}"


market_intelligence_pipeline_service = MarketIntelligencePipelineService(
    social_refresher=social_signal_service,
    hotspot_tracker=market_hotspot_tracking_service,
)
