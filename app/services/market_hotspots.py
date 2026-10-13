"""把当前事件热度保存成历史序列，并计算热点生命周期。

事件聚类回答“哪些报道说的是同一件事”，热点追踪回答“这件事的关注度正在怎样
变化”。本模块不重新抓新闻、不重新调用大模型，也不修改原始事件；它只在每次
市场情报刷新后记录一份派生快照，再用透明 Python 规则比较历史。
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select

from app.database import Database, runtime_database
from app.models import (
    MarketEventClusterMemberModel,
    MarketEventClusterModel,
    MarketEventModel,
    MarketEventSemanticAnalysisModel,
    MarketHotspotSnapshotModel,
)
from app.services.market_event_freshness import market_event_freshness_service


HOTSPOT_LIFECYCLES = {
    "emerging",
    "rising",
    "peaking",
    "steady",
    "cooling",
    "faded",
}
HOTSPOT_LIFECYCLE_LABELS = {
    "emerging": "新出现",
    "rising": "正在升温",
    "peaking": "高位持续",
    "steady": "平稳关注",
    "cooling": "正在降温",
    "faded": "影响消退",
}
HOTSPOT_ALGORITHM_VERSION = "hotspot-tracking-v1"
# 低于 70 分且只有一个来源的普通事件仍保存历史，但不会占据热点榜。多来源相互印证
# 的事件直接进入追踪；单来源事件以后越过门槛时，旧快照仍可计算真实升温幅度。
HOTSPOT_ENTRY_SCORE = 70.0
IMPACT_DIRECTION_MAP = {
    "supportive": "positive",
    "caution": "caution",
    "neutral": "neutral",
    "uncertain": "neutral",
}


class MarketHotspotError(RuntimeError):
    """当前聚类为空，或热点查询参数不符合业务约束。"""


class MarketHotspotRepository:
    """集中读取当前事件簇，并持久化、查询热点历史快照。"""

    def __init__(self, database: Database = runtime_database) -> None:
        self.database = database
        self.database.create_schema()

    def load_current_clusters(self) -> list[dict[str, Any]]:
        """读取最新聚类及其全部原始成员，作为本次快照的事实输入。"""

        with self.database.session() as session:
            representative_rows = session.execute(
                select(
                    MarketEventClusterModel,
                    MarketEventModel,
                    MarketEventSemanticAnalysisModel,
                )
                .join(
                    MarketEventModel,
                    MarketEventModel.event_id
                    == MarketEventClusterModel.representative_event_id,
                )
                .outerjoin(
                    MarketEventSemanticAnalysisModel,
                    MarketEventSemanticAnalysisModel.event_id
                    == MarketEventModel.event_id,
                )
                .order_by(MarketEventClusterModel.heat_score.desc())
            ).all()
            member_rows = session.execute(
                select(
                    MarketEventClusterMemberModel.cluster_id,
                    MarketEventModel,
                    MarketEventSemanticAnalysisModel,
                )
                .join(
                    MarketEventModel,
                    MarketEventModel.event_id
                    == MarketEventClusterMemberModel.event_id,
                )
                .outerjoin(
                    MarketEventSemanticAnalysisModel,
                    MarketEventSemanticAnalysisModel.event_id
                    == MarketEventModel.event_id,
                )
            ).all()

            members_by_cluster: dict[str, list[dict[str, Any]]] = defaultdict(list)
            for cluster_id, event, analysis in member_rows:
                members_by_cluster[str(cluster_id)].append(
                    self._event_payload(event, analysis)
                )

            return [
                {
                    "cluster_id": cluster.cluster_id,
                    "representative": self._event_payload(event, analysis),
                    "members": members_by_cluster.get(cluster.cluster_id, []),
                    "member_count": cluster.member_count,
                    "source_count": cluster.source_count,
                    "source_names": list(cluster.source_names or []),
                    "source_levels": list(cluster.source_levels or []),
                    "event_start": cluster.event_start,
                    "event_end": cluster.event_end,
                    "heat_score": float(cluster.heat_score),
                    "heat_level": cluster.heat_level,
                    "cluster_algorithm_version": cluster.algorithm_version,
                }
                for cluster, event, analysis in representative_rows
            ]

    def save_snapshots(self, snapshots: list[dict[str, Any]]) -> None:
        """在一个事务中保存整批热点；复合主键让同一时刻写入保持幂等。"""

        if not snapshots:
            return
        with self.database.session() as session, session.begin():
            for snapshot in snapshots:
                session.merge(MarketHotspotSnapshotModel(**snapshot))

    def load_latest_batch(self) -> list[MarketHotspotSnapshotModel]:
        """返回最近一次完整采集批次中的全部热点。"""

        with self.database.session() as session:
            latest_at = session.scalar(
                select(func.max(MarketHotspotSnapshotModel.observed_at))
            )
            if latest_at is None:
                return []
            return list(
                session.scalars(
                    select(MarketHotspotSnapshotModel)
                    .where(MarketHotspotSnapshotModel.observed_at == latest_at)
                    .order_by(
                        MarketHotspotSnapshotModel.heat_score.desc(),
                        MarketHotspotSnapshotModel.cluster_id,
                    )
                )
            )

    def load_history(
        self,
        *,
        cluster_ids: list[str],
        since: datetime,
    ) -> list[MarketHotspotSnapshotModel]:
        """批量读取多个热点的历史，避免列表页对每个热点单独查询数据库。"""

        if not cluster_ids:
            return []
        with self.database.session() as session:
            return list(
                session.scalars(
                    select(MarketHotspotSnapshotModel)
                    .where(
                        MarketHotspotSnapshotModel.cluster_id.in_(cluster_ids),
                        MarketHotspotSnapshotModel.observed_at >= since,
                    )
                    .order_by(
                        MarketHotspotSnapshotModel.cluster_id,
                        MarketHotspotSnapshotModel.observed_at,
                    )
                )
            )

    @staticmethod
    def _event_payload(
        event: MarketEventModel,
        analysis: MarketEventSemanticAnalysisModel | None,
    ) -> dict[str, Any]:
        """只在模型结果仍对应当前原文时使用语义字段。"""

        analysis_is_current = bool(
            analysis is not None
            and analysis.input_hash == event.content_hash
            and analysis.status == "accepted"
        )
        return {
            "event_id": event.event_id,
            "event_date": event.event_date,
            "title": event.title,
            "summary": event.summary,
            "impact_direction": (
                IMPACT_DIRECTION_MAP.get(analysis.direction, "neutral")
                if analysis_is_current
                else event.impact_direction
            ),
            "impact_horizon": (
                analysis.horizon if analysis_is_current else event.impact_horizon
            ),
            "affected_fund_codes": list(
                dict.fromkeys(
                    [
                        *list(event.affected_fund_codes or []),
                        *(
                            list(analysis.affected_fund_codes or [])
                            if analysis_is_current
                            else []
                        ),
                    ]
                )
            ),
            "themes": list(
                dict.fromkeys(
                    [
                        *list(event.tags or []),
                        *(
                            list(analysis.themes or [])
                            if analysis_is_current
                            else []
                        ),
                    ]
                )
            ),
            "source_name": event.source_name,
            "source_level": event.source_level,
            "source_url": event.source_url,
        }


class MarketHotspotTrackingService:
    """记录热点快照，并把离散刷新结果转换成可解释的趋势。"""

    def __init__(
        self,
        repository: MarketHotspotRepository | None = None,
    ) -> None:
        self.repository = repository or MarketHotspotRepository()

    def capture(self, *, observed_at: datetime | None = None) -> dict[str, Any]:
        """在聚类完成后保存一批快照；本方法不访问外网和大模型。"""

        clusters = self.repository.load_current_clusters()
        if not clusters:
            return {
                "workflow": "market_hotspot_capture",
                "status": "empty",
                "captured_count": 0,
                "summary": "当前没有事件簇，未生成热点快照。",
            }

        captured_at = self._as_utc(observed_at or datetime.now(timezone.utc))
        data_cutoff = max(item["event_end"] for item in clusters)
        snapshots = [
            self._snapshot_from_cluster(
                cluster,
                observed_at=captured_at,
                data_cutoff=data_cutoff,
            )
            for cluster in clusters
        ]
        self.repository.save_snapshots(snapshots)
        return {
            "workflow": "market_hotspot_capture",
            "status": "captured",
            "captured_count": len(snapshots),
            "observed_at": captured_at.isoformat(),
            "data_cutoff": data_cutoff.isoformat(),
            "summary": f"已保存 {len(snapshots)} 个热点的本轮热度快照。",
        }

    def overview(
        self,
        *,
        query: str | None = None,
        fund_code: str | None = None,
        lifecycle: str | None = None,
        history_days: int = 30,
        limit: int = 12,
    ) -> dict[str, Any]:
        """返回最新热点，以及它们在指定历史窗口内的趋势。"""

        if lifecycle and lifecycle not in HOTSPOT_LIFECYCLES:
            raise MarketHotspotError(f"不支持的热点阶段：{lifecycle}")
        if not 7 <= history_days <= 90:
            raise MarketHotspotError("history_days 必须在 7 到 90 之间")
        if not 1 <= limit <= 30:
            raise MarketHotspotError("limit 必须在 1 到 30 之间")

        latest = self.repository.load_latest_batch()
        if not latest:
            return self._empty_overview(history_days=history_days)

        latest_at = max(self._as_utc(item.observed_at) for item in latest)
        history = self.repository.load_history(
            cluster_ids=[item.cluster_id for item in latest],
            since=latest_at - timedelta(days=history_days),
        )
        history_by_cluster: dict[str, list[MarketHotspotSnapshotModel]] = defaultdict(list)
        for item in history:
            history_by_cluster[item.cluster_id].append(item)

        tracked_items = [
            self._to_hotspot(
                current=item,
                history=history_by_cluster.get(item.cluster_id, [item]),
            )
            for item in latest
        ]
        hotspots = [
            item
            for item in tracked_items
            if item["peak_heat_score"] >= HOTSPOT_ENTRY_SCORE
            or item["source_count"] >= 2
        ]
        normalized_query = " ".join(str(query or "").casefold().split())
        normalized_fund = str(fund_code or "").strip()
        filtered = [
            item
            for item in hotspots
            if (not lifecycle or item["lifecycle"] == lifecycle)
            and (
                not normalized_fund
                or normalized_fund in item["affected_fund_codes"]
            )
            and (
                not normalized_query
                or self._matches_query(item, normalized_query)
            )
        ]
        lifecycle_order = {
            "rising": 0,
            "emerging": 1,
            "peaking": 2,
            "steady": 3,
            "cooling": 4,
            "faded": 5,
        }
        filtered.sort(
            key=lambda item: (
                lifecycle_order[item["lifecycle"]],
                -float(item["heat_score"]),
                item["cluster_id"],
            )
        )
        visible = filtered[:limit]
        for rank, item in enumerate(visible, start=1):
            item["rank"] = rank

        lifecycle_counts = {
            key: sum(item["lifecycle"] == key for item in hotspots)
            for key in HOTSPOT_LIFECYCLE_LABELS
        }
        tracked_cluster_ids = {item["cluster_id"] for item in hotspots}
        tracked_history_count = sum(
            len(history_by_cluster.get(cluster_id, []))
            for cluster_id in tracked_cluster_ids
        )
        return {
            "data_kind": "market_hotspot_tracking",
            "data_mode": "database",
            "generated_at": latest_at.isoformat(),
            "data_cutoff": max(item.data_cutoff for item in latest).isoformat(),
            "history_days": history_days,
            "count": len(visible),
            "total_matched": len(filtered),
            "total_hotspot_count": len(hotspots),
            "history_point_count": tracked_history_count,
            "lifecycle_counts": lifecycle_counts,
            "hotspots": visible,
            "disclaimer": (
                "热点表示多来源关注度及其变化，不等于利好、利空或未来涨跌预测；"
                "社交来源仍属于未经成交核验的作者自述。"
            ),
        }

    def detail(
        self,
        *,
        cluster_id: str,
        history_days: int = 30,
    ) -> dict[str, Any]:
        """返回一个热点的完整历史；仅允许读取当前批次仍存在的事件簇。"""

        if not 7 <= history_days <= 90:
            raise MarketHotspotError("history_days 必须在 7 到 90 之间")
        latest = self.repository.load_latest_batch()
        current = next(
            (item for item in latest if item.cluster_id == cluster_id),
            None,
        )
        if current is None:
            raise MarketHotspotError("热点不存在，或尚未生成本轮快照")
        current_at = self._as_utc(current.observed_at)
        history = self.repository.load_history(
            cluster_ids=[cluster_id],
            since=current_at - timedelta(days=history_days),
        )
        hotspot = self._to_hotspot(current=current, history=history)
        # 最新批次已按热度降序读取，因此详情页仍能显示它在全市场中的热度排名。
        hotspot["rank"] = next(
            index
            for index, item in enumerate(latest, start=1)
            if item.cluster_id == cluster_id
        )
        return hotspot

    @staticmethod
    def _snapshot_from_cluster(
        cluster: dict[str, Any],
        *,
        observed_at: datetime,
        data_cutoff: date,
    ) -> dict[str, Any]:
        representative = cluster["representative"]
        members = cluster["members"] or [representative]
        directions = {
            item["impact_direction"]
            for item in members
            if item["impact_direction"] not in {"neutral", "uncertain"}
        }
        direction = (
            "mixed"
            if len(directions) > 1
            else next(iter(directions), representative["impact_direction"])
        )
        affected_funds = sorted(
            {
                code
                for item in members
                for code in item["affected_fund_codes"]
            }
        )
        themes = list(
            dict.fromkeys(
                theme
                for item in members
                for theme in item["themes"]
                if str(theme).strip()
            )
        )[:12]
        freshness = market_event_freshness_service.evaluate(
            event_date=cluster["event_end"],
            horizon=representative["impact_horizon"],
            as_of=data_cutoff,
        )
        evidence = sorted(
            (
                {
                    "event_id": item["event_id"],
                    "event_date": item["event_date"].isoformat(),
                    "source_name": item["source_name"],
                    "source_level": item["source_level"],
                    "source_url": item["source_url"],
                }
                for item in members
            ),
            key=lambda item: (item["event_date"], item["source_level"]),
            reverse=True,
        )[:12]
        return {
            "cluster_id": cluster["cluster_id"],
            "observed_at": observed_at,
            "data_cutoff": data_cutoff,
            "representative_event_id": representative["event_id"],
            "title": representative["title"],
            "summary": representative["summary"],
            "event_start": cluster["event_start"],
            "event_end": cluster["event_end"],
            "impact_direction": direction,
            "impact_horizon": representative["impact_horizon"],
            "member_count": cluster["member_count"],
            "source_count": cluster["source_count"],
            "source_names": cluster["source_names"],
            "source_levels": cluster["source_levels"],
            "affected_fund_codes": affected_funds,
            "themes": themes,
            "evidence_items": evidence,
            "heat_score": cluster["heat_score"],
            "heat_level": cluster["heat_level"],
            "freshness_status": freshness["freshness_status"],
            "freshness_score": freshness["freshness_score"],
            "algorithm_version": HOTSPOT_ALGORITHM_VERSION,
        }

    def _to_hotspot(
        self,
        *,
        current: MarketHotspotSnapshotModel,
        history: list[MarketHotspotSnapshotModel],
    ) -> dict[str, Any]:
        ordered = sorted(history, key=lambda item: self._as_utc(item.observed_at))
        # load_history 通常已包含 current；按主键再去重，便于 Fake Repository 测试。
        by_time = {self._as_utc(item.observed_at): item for item in ordered}
        by_time[self._as_utc(current.observed_at)] = current
        ordered = [by_time[key] for key in sorted(by_time)]
        previous = ordered[-2] if len(ordered) >= 2 else None
        heat_change = self._difference(current, previous, "heat_score")
        member_delta = int(self._difference(current, previous, "member_count") or 0)
        source_delta = int(self._difference(current, previous, "source_count") or 0)
        lifecycle, reason = self._classify_lifecycle(
            current=current,
            previous=previous,
            heat_change=heat_change,
            member_delta=member_delta,
            source_delta=source_delta,
        )
        current_at = self._as_utc(current.observed_at)
        baseline_24h = self._baseline_at_or_before(
            ordered,
            current_at - timedelta(hours=24),
        )
        baseline_3d = self._baseline_at_or_before(
            ordered,
            current_at - timedelta(days=3),
        )
        confidence = (
            "high"
            if current.source_count >= 3 and len(ordered) >= 2
            else "medium"
            if current.source_count >= 2 or len(ordered) >= 2
            else "low"
        )
        return {
            "rank": 0,
            "cluster_id": current.cluster_id,
            "representative_event_id": current.representative_event_id,
            "title": current.title,
            "summary": current.summary,
            "event_start": current.event_start.isoformat(),
            "event_end": current.event_end.isoformat(),
            "observed_at": current_at.isoformat(),
            "impact_direction": current.impact_direction,
            "impact_horizon": current.impact_horizon,
            "affected_fund_codes": list(current.affected_fund_codes or []),
            "themes": list(current.themes or []),
            "source_count": current.source_count,
            "source_names": list(current.source_names or []),
            "source_levels": list(current.source_levels or []),
            "member_count": current.member_count,
            "heat_score": round(float(current.heat_score), 2),
            "peak_heat_score": round(
                max(float(item.heat_score) for item in ordered),
                2,
            ),
            "heat_level": current.heat_level,
            "freshness_status": current.freshness_status,
            "freshness_score": round(float(current.freshness_score), 2),
            "lifecycle": lifecycle,
            "lifecycle_label": HOTSPOT_LIFECYCLE_LABELS[lifecycle],
            "trend_direction": self._trend_direction(heat_change),
            "change_since_previous": heat_change,
            "heat_change_24h": self._difference(
                current,
                baseline_24h,
                "heat_score",
            ),
            "heat_change_3d": self._difference(
                current,
                baseline_3d,
                "heat_score",
            ),
            "member_delta": member_delta,
            "source_delta": source_delta,
            "snapshot_count": len(ordered),
            "trend_confidence": confidence,
            "tracking_reason": reason,
            "evidence": list(current.evidence_items or []),
            "timeline": [
                {
                    "observed_at": self._as_utc(item.observed_at).isoformat(),
                    "data_cutoff": item.data_cutoff.isoformat(),
                    "heat_score": round(float(item.heat_score), 2),
                    "member_count": item.member_count,
                    "source_count": item.source_count,
                }
                for item in ordered[-20:]
            ],
            "algorithm_version": current.algorithm_version,
        }

    @staticmethod
    def _classify_lifecycle(
        *,
        current: MarketHotspotSnapshotModel,
        previous: MarketHotspotSnapshotModel | None,
        heat_change: float | None,
        member_delta: int,
        source_delta: int,
    ) -> tuple[str, str]:
        """使用热度、报道增量和数据日期判定阶段，模型不参与打分。"""

        if current.freshness_status == "expired":
            return "faded", "事件已超出当前影响有效期，保留为历史背景。"
        if previous is None:
            age_days = max(0, (current.data_cutoff - current.event_end).days)
            if age_days <= 1 and current.heat_score >= 45:
                return "emerging", "首次进入追踪，且事件仍处于最新数据窗口。"
            return "steady", "已建立首个基线快照，暂无历史变化可比较。"

        if member_delta > 0 or source_delta > 0:
            return (
                "rising",
                f"较上次增加 {member_delta} 篇报道、{source_delta} 个独立来源。",
            )
        if heat_change is not None and heat_change >= 5:
            return "rising", f"热度较上次上升 {heat_change:.1f} 分。"

        # 同一天连续点击刷新不会被误判成见顶或降温；至少数据截面向前推进后再判断。
        data_advanced = current.data_cutoff > previous.data_cutoff
        if data_advanced and current.heat_score >= 70 and abs(heat_change or 0) < 5:
            return "peaking", "高热度跨数据截面持续，但独立来源暂未继续增加。"
        if data_advanced and (
            (heat_change is not None and heat_change <= -5)
            or current.freshness_status == "decaying"
        ):
            return "cooling", "没有新增独立来源，且热度或时效权重正在下降。"
        return "steady", "与上次快照相比，报道数量和热度没有显著变化。"

    @classmethod
    def _baseline_at_or_before(
        cls,
        history: list[MarketHotspotSnapshotModel],
        target: datetime,
    ) -> MarketHotspotSnapshotModel | None:
        candidates = [
            item for item in history if cls._as_utc(item.observed_at) <= target
        ]
        return candidates[-1] if candidates else None

    @staticmethod
    def _difference(
        current: MarketHotspotSnapshotModel,
        baseline: MarketHotspotSnapshotModel | None,
        field: str,
    ) -> float | None:
        if baseline is None:
            return None
        return round(float(getattr(current, field)) - float(getattr(baseline, field)), 2)

    @staticmethod
    def _trend_direction(change: float | None) -> str:
        if change is None:
            return "insufficient_history"
        if change >= 2:
            return "rising"
        if change <= -2:
            return "falling"
        return "flat"

    @staticmethod
    def _matches_query(item: dict[str, Any], query: str) -> bool:
        searchable = " ".join(
            [
                item["title"],
                item["summary"],
                *item["themes"],
                *item["source_names"],
                *item["affected_fund_codes"],
            ]
        ).casefold()
        return all(token in searchable for token in query.split())

    @staticmethod
    def _empty_overview(*, history_days: int) -> dict[str, Any]:
        return {
            "data_kind": "market_hotspot_tracking",
            "data_mode": "unavailable",
            "generated_at": None,
            "data_cutoff": None,
            "history_days": history_days,
            "count": 0,
            "total_matched": 0,
            "total_hotspot_count": 0,
            "history_point_count": 0,
            "lifecycle_counts": {
                key: 0 for key in HOTSPOT_LIFECYCLE_LABELS
            },
            "hotspots": [],
            "disclaimer": (
                "尚未生成热点历史。请先在市场页执行一次“获取最新市场情报”。"
            ),
        }

    @staticmethod
    def _as_utc(value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)


market_hotspot_repository = MarketHotspotRepository(runtime_database)
market_hotspot_tracking_service = MarketHotspotTrackingService(
    market_hotspot_repository
)
