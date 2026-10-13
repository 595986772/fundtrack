"""读取、清洗并检索 FundScope 的市场事件。

这里刻意不调用大模型。同步成功后优先读取 SQL；数据库为空时读取固定 JSON 作为
离线兜底。REST API、前端和 Agent Tool 都复用这一个 Service，因此三处看到的
事件顺序、来源等级和去重结果完全一致。
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import func, select

from app.database import Database, runtime_database
from app.models import (
    MarketEventClusterMemberModel,
    MarketEventClusterModel,
    MarketEventModel,
    MarketEventSemanticAnalysisModel,
)
from app.services.market_event_freshness import market_event_freshness_service
from app.services.market_event_fund_links import (
    MarketEventFundLinkService,
    market_event_fund_link_service,
)
from app.services.eval_context import frozen_eval_data_enabled
from app.services.errors import ServiceError


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_EVENT_FIXTURE = (
    PROJECT_ROOT / "data" / "market_fixtures" / "market_events.json"
)

EVENT_TYPES = {
    "policy",
    "economic_data",
    "regulation",
    "fund_announcement",
    "market_notice",
    "social_positioning",
}
SOURCE_LEVELS = {"A", "B", "C", "D"}


class MarketEventError(ServiceError):
    """事件文件缺失、字段格式错误或查询条件不合法。"""


class MarketEventRepository:
    """负责 `market_events` 表的增量写入和查询。"""

    def __init__(self, database: Database = runtime_database) -> None:
        self.database = database
        self.database.create_schema()

    def upsert(
        self,
        *,
        events: list[dict[str, Any]],
        fetched_at: datetime,
    ) -> dict[str, int]:
        """按 event_id 更新、按 content_hash 去重，返回本次写入统计。"""

        if not events:
            return {"inserted_count": 0, "updated_count": 0, "deduplicated_count": 0}

        inserted_count = 0
        updated_count = 0
        deduplicated_count = 0
        hashes = {str(event["content_hash"]) for event in events}

        with self.database.session() as session, session.begin():
            # 先一次性加载已有哈希，避免每条事件都额外查询数据库。
            existing_hashes = dict(
                session.execute(
                    select(MarketEventModel.content_hash, MarketEventModel.event_id).where(
                        MarketEventModel.content_hash.in_(hashes)
                    )
                ).all()
            )

            for event in events:
                event_id = str(event["event_id"])
                content_hash = str(event["content_hash"])
                hash_owner = existing_hashes.get(content_hash)
                if hash_owner is not None and hash_owner != event_id:
                    # 两个采集入口指向同一份原文时保留先入库的稳定 ID。
                    deduplicated_count += 1
                    continue

                model = session.get(MarketEventModel, event_id)
                if model is None:
                    model = MarketEventModel(event_id=event_id)
                    session.add(model)
                    inserted_count += 1
                else:
                    updated_count += 1

                model.event_date = date.fromisoformat(str(event["event_date"]))
                model.published_date = date.fromisoformat(str(event["published_date"]))
                model.event_type = str(event["event_type"])
                model.title = str(event["title"])
                model.summary = str(event["summary"])
                model.impact_direction = str(event["impact_direction"])
                model.impact_horizon = str(event["impact_horizon"])
                model.impact_reason = str(event["impact_reason"])
                model.affected_fund_codes = list(event.get("affected_fund_codes", []))
                model.tags = list(event.get("tags", []))
                model.source_name = str(event["source_name"])
                model.source_level = str(event["source_level"])
                model.source_grade_reason = str(event["source_grade_reason"])
                model.source_url = str(event["source_url"])
                model.content_hash = content_hash
                model.fetched_at = fetched_at
                existing_hashes[content_hash] = event_id

        return {
            "inserted_count": inserted_count,
            "updated_count": updated_count,
            "deduplicated_count": deduplicated_count,
        }

    def load(self, *, limit: int = 1000) -> dict[str, Any] | None:
        """读取数据库事件，并转换成原有 MarketEventService 输入结构。"""

        statement = (
            select(MarketEventModel)
            .order_by(
                MarketEventModel.event_date.desc(),
                MarketEventModel.published_date.desc(),
                MarketEventModel.event_id.desc(),
            )
            .limit(limit)
        )
        with self.database.session() as session:
            models = list(session.scalars(statement))
            if not models:
                return None
            event_ids = [model.event_id for model in models]
            analyses = {
                analysis.event_id: analysis
                for analysis in session.scalars(
                    select(MarketEventSemanticAnalysisModel).where(
                        MarketEventSemanticAnalysisModel.event_id.in_(event_ids)
                    )
                )
            }
            event_hashes = {model.event_id: model.content_hash for model in models}
            # 聚类是原始事件之外的派生层。只有成员 input_hash 仍匹配当前原文时才公开，
            # 这样事件被同步更新后，旧簇不会暂时冒充新内容的聚类结果。
            cluster_rows = session.execute(
                select(MarketEventClusterMemberModel, MarketEventClusterModel)
                .join(
                    MarketEventClusterModel,
                    MarketEventClusterMemberModel.cluster_id
                    == MarketEventClusterModel.cluster_id,
                )
                .where(MarketEventClusterMemberModel.event_id.in_(event_ids))
            ).all()
            clusters = {
                member.event_id: self._cluster_to_public(member, cluster)
                for member, cluster in cluster_rows
                if event_hashes.get(member.event_id) == member.input_hash
            }
            newest_fetch = max(model.fetched_at for model in models)
            return {
                "data_kind": "database_market_events",
                "data_mode": "database",
                "generated_at": self._as_utc(newest_fetch).isoformat(),
                "data_updated_at": self._as_utc(newest_fetch).isoformat(),
                "data_cutoff": max(model.event_date for model in models).isoformat(),
                "events": [
                    {
                        **self._to_event(model, analyses.get(model.event_id)),
                        "cluster": clusters.get(model.event_id),
                    }
                    for model in models
                ],
            }

    def pending_for_analysis(
        self,
        *,
        prompt_version: str,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """返回缺少分析、原文变化或 Prompt 版本变化的最新事件。"""

        event_statement = select(MarketEventModel).order_by(
            MarketEventModel.event_date.desc(),
            MarketEventModel.event_id.desc(),
        )
        with self.database.session() as session:
            events = list(session.scalars(event_statement))
            analyses = {
                analysis.event_id: analysis
                for analysis in session.scalars(select(MarketEventSemanticAnalysisModel))
            }
            pending = []
            for event in events:
                analysis = analyses.get(event.event_id)
                if (
                    analysis is not None
                    and analysis.input_hash == event.content_hash
                    and analysis.prompt_version == prompt_version
                ):
                    continue
                # 待分析输入只包含原始事件字段，不包含旧模型观点，避免自我强化。
                pending.append(self._to_event(event, None))
                if len(pending) >= limit:
                    break
            return pending

    def save_analyses(
        self,
        *,
        analyses: list[dict[str, Any]],
        analyzed_at: datetime,
    ) -> dict[str, int]:
        """按 event_id 保存校验后的模型结果，原始事件表不发生修改。"""

        inserted_count = 0
        updated_count = 0
        with self.database.session() as session, session.begin():
            for item in analyses:
                event_id = str(item["event_id"])
                event = session.get(MarketEventModel, event_id)
                if event is None:
                    # 模型不能通过虚构 ID 创建事件；这里只保存数据库已存在的事件。
                    continue
                model = session.get(MarketEventSemanticAnalysisModel, event_id)
                if model is None:
                    model = MarketEventSemanticAnalysisModel(event_id=event_id)
                    session.add(model)
                    inserted_count += 1
                else:
                    updated_count += 1

                model.input_hash = event.content_hash
                model.status = str(item["status"])
                model.model_name = str(item["model_name"])
                model.prompt_version = str(item["prompt_version"])
                model.entities = list(item.get("entities", []))
                model.themes = list(item.get("themes", []))
                model.direction = str(item["direction"])
                model.horizon = str(item["horizon"])
                model.severity = int(item["severity"])
                model.relevance = str(item["relevance"])
                model.confidence = float(item["confidence"])
                model.reason = str(item["reason"])
                model.affected_fund_codes = list(item.get("affected_fund_codes", []))
                model.uncertainties = list(item.get("uncertainties", []))
                model.analyzed_at = analyzed_at

        return {"inserted_count": inserted_count, "updated_count": updated_count}

    def analysis_status(self) -> dict[str, int]:
        """统计与当前原文哈希匹配的有效分析和待分析数量。"""

        with self.database.session() as session:
            events = {
                event_id: content_hash
                for event_id, content_hash in session.execute(
                    select(MarketEventModel.event_id, MarketEventModel.content_hash)
                )
            }
            analyses = list(session.scalars(select(MarketEventSemanticAnalysisModel)))

        current = [
            analysis
            for analysis in analyses
            if events.get(analysis.event_id) == analysis.input_hash
        ]
        accepted_count = sum(item.status == "accepted" for item in current)
        abstained_count = sum(item.status == "abstained" for item in current)
        return {
            "pending_analysis_count": max(0, len(events) - len(current)),
            "analyzed_count": len(current),
            "accepted_analysis_count": accepted_count,
            "abstained_analysis_count": abstained_count,
        }

    def status(self) -> dict[str, Any]:
        """返回事件数量、日期覆盖范围和最近抓取时间。"""

        statement = select(
            func.count(MarketEventModel.event_id),
            func.min(MarketEventModel.event_date),
            func.max(MarketEventModel.event_date),
            func.max(MarketEventModel.fetched_at),
        )
        with self.database.session() as session:
            record_count, event_start, event_end, fetched_at = session.execute(
                statement
            ).one()
        return {
            "record_count": int(record_count or 0),
            "event_start": event_start.isoformat() if event_start else None,
            "event_end": event_end.isoformat() if event_end else None,
            "last_successful_sync_at": (
                self._as_utc(fetched_at).isoformat() if fetched_at else None
            ),
        }

    @classmethod
    def _to_event(
        cls,
        model: MarketEventModel,
        analysis: MarketEventSemanticAnalysisModel | None,
    ) -> dict[str, Any]:
        event = {
            "event_id": model.event_id,
            "event_date": model.event_date.isoformat(),
            "published_date": model.published_date.isoformat(),
            "event_type": model.event_type,
            "title": model.title,
            "summary": model.summary,
            "impact_direction": model.impact_direction,
            "impact_horizon": model.impact_horizon,
            "impact_reason": model.impact_reason,
            "affected_fund_codes": list(model.affected_fund_codes or []),
            "tags": list(model.tags or []),
            "source_name": model.source_name,
            "source_level": model.source_level,
            "source_grade_reason": model.source_grade_reason,
            "source_url": model.source_url,
            "content_hash": model.content_hash,
            "semantic_analysis": None,
        }
        # 只接受针对当前 content_hash 的结果；原文更新后旧分析立即失效。
        if analysis is None or analysis.input_hash != model.content_hash:
            return event

        semantic_analysis = cls._analysis_to_public(analysis)
        event["semantic_analysis"] = semantic_analysis
        if analysis.status != "accepted":
            return event

        # 通过校验的模型方向只覆盖对外“解释层”，绝不回写 MarketEventModel 原始字段。
        event["impact_direction"] = {
            "supportive": "positive",
            "caution": "caution",
            "neutral": "neutral",
        }.get(analysis.direction, "neutral")
        event["impact_horizon"] = analysis.horizon
        event["impact_reason"] = analysis.reason
        event["affected_fund_codes"] = list(
            dict.fromkeys(
                [
                    *event["affected_fund_codes"],
                    *list(analysis.affected_fund_codes or []),
                ]
            )
        )
        event["tags"] = list(
            dict.fromkeys([*event["tags"], *list(analysis.themes or [])])
        )
        return event

    @classmethod
    def _analysis_to_public(
        cls,
        model: MarketEventSemanticAnalysisModel,
    ) -> dict[str, Any]:
        return {
            "status": model.status,
            "model_name": model.model_name,
            "prompt_version": model.prompt_version,
            "entities": list(model.entities or []),
            "themes": list(model.themes or []),
            "direction": model.direction,
            "horizon": model.horizon,
            "severity": model.severity,
            "relevance": model.relevance,
            "confidence": round(model.confidence, 4),
            "reason": model.reason,
            "affected_fund_codes": list(model.affected_fund_codes or []),
            "uncertainties": list(model.uncertainties or []),
            "analyzed_at": cls._as_utc(model.analyzed_at).isoformat(),
        }

    @classmethod
    def _cluster_to_public(
        cls,
        member: MarketEventClusterMemberModel,
        cluster: MarketEventClusterModel,
    ) -> dict[str, Any]:
        """把簇表和成员表合并成前端需要的一层公开元数据。"""

        return {
            "cluster_id": cluster.cluster_id,
            "representative_event_id": cluster.representative_event_id,
            "is_representative": member.is_representative,
            "member_count": cluster.member_count,
            "source_count": cluster.source_count,
            "source_names": list(cluster.source_names or []),
            "source_levels": list(cluster.source_levels or []),
            "event_start": cluster.event_start.isoformat(),
            "event_end": cluster.event_end.isoformat(),
            "heat_score": round(cluster.heat_score, 2),
            "heat_level": cluster.heat_level,
            "similarity": round(member.similarity, 6),
            "algorithm_version": cluster.algorithm_version,
            "embedding_model": cluster.embedding_model,
            "generated_at": cls._as_utc(cluster.generated_at).isoformat(),
        }

    @staticmethod
    def _as_utc(value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)


class MarketEventService:
    """把数据库或固定快照事件转换成可追溯、可检索的统一列表。"""

    def __init__(
        self,
        fixture_path: Path = DEFAULT_EVENT_FIXTURE,
        repository: MarketEventRepository | None = None,
        fund_link_service: MarketEventFundLinkService | None = None,
    ) -> None:
        # 测试可以传入临时 JSON，从而验证去重和日期规则，不污染真实样例。
        self.fixture_path = fixture_path
        # 普通测试默认 fixture-only；只有全局产品 Service 显式注入运行时 Repository。
        self.repository = repository
        # 只有产品级实例注入动态关联服务，fixture 单元测试继续保持冻结结果。
        self.fund_link_service = fund_link_service

    def search(
        self,
        *,
        query: str | None = None,
        fund_code: str | None = None,
        event_type: str | None = None,
        source_level: str | None = None,
        date_from: date | None = None,
        date_to: date | None = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        """按查询条件筛选事件，并按事件日期从新到旧返回。"""

        if event_type and event_type not in EVENT_TYPES:
            raise MarketEventError(f"不支持的事件类型：{event_type}", code="invalid_arguments")
        if source_level and source_level not in SOURCE_LEVELS:
            raise MarketEventError(f"不支持的来源等级：{source_level}", code="invalid_arguments")
        if date_from and date_to and date_from > date_to:
            raise MarketEventError("date_from 不能晚于 date_to", code="invalid_arguments")
        if not 1 <= limit <= 50:
            raise MarketEventError("limit 必须在 1 到 50 之间", code="invalid_arguments")

        metadata, events = self._load_events()
        # 数据库事件会随当前已导入基金动态补充关联。固定评测快照不走这一步，
        # 从而避免今天新增的基金改变过去已经冻结的测试答案。
        if metadata["data_mode"] == "database" and self.fund_link_service is not None:
            events = self.fund_link_service.link_events(events)
        freshness_as_of = date.fromisoformat(metadata["data_cutoff"])
        # 时效性是对当前数据截面的解释，原始事件字典和数据库记录都不被修改。
        events = [
            {
                **event,
                **market_event_freshness_service.evaluate(
                    event_date=event["event_date"],
                    horizon=event["impact_horizon"],
                    as_of=freshness_as_of,
                ),
            }
            for event in events
        ]
        normalized_query = self._normalize_text(query or "")
        normalized_fund_code = str(fund_code or "").strip()

        filtered: list[dict[str, Any]] = []
        for event in events:
            if (
                normalized_fund_code
                and normalized_fund_code not in event["affected_fund_codes"]
            ):
                continue
            if event_type and event["event_type"] != event_type:
                continue
            if source_level and event["source_level"] != source_level:
                continue
            if date_from and event["event_date"] < date_from.isoformat():
                continue
            if date_to and event["event_date"] > date_to.isoformat():
                continue
            if normalized_query and not self._matches_query(event, normalized_query):
                continue
            filtered.append(event)

        filtered.sort(
            key=lambda item: (
                item["event_date"],
                item["published_date"],
                item["event_id"],
            ),
            reverse=True,
        )

        accepted_count = sum(
            event.get("semantic_analysis", {}).get("status") == "accepted"
            for event in filtered
            if event.get("semantic_analysis")
        )
        abstained_count = sum(
            event.get("semantic_analysis", {}).get("status") == "abstained"
            for event in filtered
            if event.get("semantic_analysis")
        )
        pending_count = (
            max(0, len(filtered) - accepted_count - abstained_count)
            if metadata["data_mode"] == "database"
            else 0
        )
        fresh_count = sum(
            event["freshness_status"] == "fresh" for event in filtered
        )
        decaying_count = sum(
            event["freshness_status"] == "decaying" for event in filtered
        )
        expired_count = sum(
            event["freshness_status"] == "expired" for event in filtered
        )
        # count 是原始报道数；cluster_count 是把同一事件的重复报道合并后的数量。
        cluster_keys = {
            (
                event["cluster"]["cluster_id"]
                if event.get("cluster")
                else f"raw:{event['event_id']}"
            )
            for event in filtered
        }
        clustered_event_count = sum(event.get("cluster") is not None for event in filtered)
        source_names = sorted({event["source_name"] for event in filtered})
        social_count = sum(event["source_level"] == "D" for event in filtered)
        if metadata["data_mode"] == "database" and accepted_count:
            disclaimer = (
                f"{accepted_count} 条事件已通过模型语义校验，{abstained_count} 条弃权，"
                f"{pending_count} 条待分析；模型观点不代表收益预测，也不构成投资建议。"
            )
        else:
            disclaimer = (
                "数据库自动采集事件默认使用中性方向，尚未完成模型语义复核；"
                "固定快照事件保留人工研究标注。以上均不代表收益预测，也不构成投资建议。"
            )
        if social_count:
            disclaimer += (
                f" 其中 {social_count} 条 D 级社交观点只聚合作者自述，"
                "未验证真实成交，不能当作市场资金流。"
            )

        return {
            "data_kind": metadata["data_kind"],
            "data_mode": metadata["data_mode"],
            "data_updated_at": metadata["data_updated_at"],
            "generated_at": metadata["generated_at"],
            "data_cutoff": metadata["data_cutoff"],
            "count": min(len(filtered), limit),
            "total_matched": len(filtered),
            "semantic_accepted_count": accepted_count,
            "semantic_abstained_count": abstained_count,
            "semantic_pending_count": pending_count,
            "fresh_event_count": fresh_count,
            "decaying_event_count": decaying_count,
            "expired_event_count": expired_count,
            "cluster_count": len(cluster_keys),
            "clustered_event_count": clustered_event_count,
            "source_count": len(source_names),
            "source_names": source_names,
            "events": filtered[:limit],
            "source_grade_legend": {
                "A": "发布主体、监管部门或基金管理人官方来源",
                "B": "政府网站转载、地方权威机关或官方监测来源",
                "C": "媒体、研究机构或其他第三方来源",
                "D": "公开社交平台作者自述，经过聚合但未核验真实交易",
            },
            "disclaimer": disclaimer,
        }

    def _load_events(self) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """优先读取数据库；没有同步记录时再回退到固定 JSON。"""

        # 数据库新闻每天变化；回归评测只读取带版本的固定事件集合。
        if self.repository is not None and not frozen_eval_data_enabled():
            database_payload = self.repository.load()
            if database_payload is not None:
                events = list(database_payload.pop("events"))
                return database_payload, events

        return self._load_fixture_events()

    def _load_fixture_events(self) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """读取 JSON，统一日期，并按来源文本哈希去掉重复事件。"""

        if not self.fixture_path.is_file():
            raise MarketEventError("市场事件快照不存在", code="data_unavailable")
        try:
            payload = json.loads(self.fixture_path.read_text(encoding="utf-8"))
            metadata = {
                "data_kind": "frozen_market_events",
                "data_mode": "fixture",
                "data_updated_at": None,
                "generated_at": str(payload["generated_at"]),
                "data_cutoff": self._normalize_date(payload["data_cutoff"]),
            }
            raw_events = payload["events"]
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise MarketEventError("市场事件快照格式错误") from exc

        if not isinstance(raw_events, list):
            raise MarketEventError("市场事件 events 必须是列表")

        events: list[dict[str, Any]] = []
        seen_hashes: set[str] = set()
        seen_event_ids: set[str] = set()
        for raw_event in raw_events:
            event = self._normalize_event(raw_event)

            # 同一篇原始内容即使被重复采集，也只进入结果一次。
            # 这里用稳定 SHA-256，而不是 Python hash()，因为后者每次进程启动会变化。
            if event["content_hash"] in seen_hashes:
                continue
            if event["event_id"] in seen_event_ids:
                raise MarketEventError(f"事件 ID 重复：{event['event_id']}")
            seen_hashes.add(event["content_hash"])
            seen_event_ids.add(event["event_id"])
            events.append(event)

        return metadata, events

    def _normalize_event(self, raw_event: Any) -> dict[str, Any]:
        """把单条来源记录转换成前端和 Tool 共用的字段。"""

        if not isinstance(raw_event, dict):
            raise MarketEventError("每条市场事件必须是对象")
        try:
            source_text = self._normalize_text(raw_event["source_text"])
            event_type = str(raw_event["event_type"])
            source_level = str(raw_event["source_level"])
            if event_type not in EVENT_TYPES or source_level not in SOURCE_LEVELS:
                raise ValueError("unsupported classification")

            return {
                "event_id": str(raw_event["event_id"]),
                "event_date": self._normalize_date(raw_event["event_date"]),
                # 来源可能只给日期，也可能给带时区的时间；对外统一成 YYYY-MM-DD。
                "published_date": self._normalize_date(raw_event["published_at"]),
                "event_type": event_type,
                "title": str(raw_event["title"]),
                "summary": str(raw_event["summary"]),
                "impact_direction": str(raw_event["impact_direction"]),
                "impact_horizon": str(raw_event["impact_horizon"]),
                "impact_reason": str(raw_event["impact_reason"]),
                "affected_fund_codes": [
                    str(code) for code in raw_event.get("affected_fund_codes", [])
                ],
                "tags": [str(tag) for tag in raw_event.get("tags", [])],
                "source_name": str(raw_event["source_name"]),
                "source_level": source_level,
                "source_grade_reason": str(raw_event["source_grade_reason"]),
                "source_url": str(raw_event["source_url"]),
                "content_hash": f"sha256:{hashlib.sha256(source_text.encode('utf-8')).hexdigest()}",
            }
        except (KeyError, TypeError, ValueError) as exc:
            event_id = raw_event.get("event_id", "unknown")
            raise MarketEventError(f"市场事件格式错误：{event_id}") from exc

    @classmethod
    def _matches_query(cls, event: dict[str, Any], query: str) -> bool:
        """使用透明的关键词匹配；当前阶段不引入向量检索和隐藏排序。"""

        searchable = cls._normalize_text(
            " ".join(
                [
                    event["title"],
                    event["summary"],
                    event["impact_reason"],
                    event["source_name"],
                    *event["tags"],
                    *event["affected_fund_codes"],
                ]
            )
        )
        # 有空格时按多个关键词做 AND；中文短语没有空格时直接做包含判断。
        return all(token in searchable for token in query.split())

    @staticmethod
    def _normalize_text(value: Any) -> str:
        """折叠空白并忽略英文大小写，用于哈希和关键词匹配。"""

        return re.sub(r"\s+", " ", str(value)).strip().casefold()

    @staticmethod
    def _normalize_date(value: Any) -> str:
        """把日期或 ISO 时间统一成 YYYY-MM-DD。"""

        text = str(value).strip()
        if not text:
            raise ValueError("empty date")
        normalized = text.replace("Z", "+00:00")
        try:
            if "T" in normalized:
                return datetime.fromisoformat(normalized).date().isoformat()
            return date.fromisoformat(normalized).isoformat()
        except ValueError as exc:
            raise ValueError(f"invalid date: {text}") from exc


market_event_repository = MarketEventRepository(runtime_database)
market_event_service = MarketEventService(
    repository=market_event_repository,
    fund_link_service=market_event_fund_link_service,
)
