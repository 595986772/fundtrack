"""把多来源的相似市场报道合并成可追溯的逻辑事件簇。

这层解决的是“同一件事被多个来源重复报道”的问题。原始新闻仍逐条保存在
``market_events``；本服务只额外保存 Embedding、事件簇和成员关系，因而可以随时
更换阈值后重建，而不会污染原始证据。
"""

from __future__ import annotations

import hashlib
import math
from collections import defaultdict
from datetime import date, datetime, timezone
from typing import Any, Callable

from sqlalchemy import delete, func, select

from app.config import settings
from app.database import Database, runtime_database
from app.models import (
    MarketEventClusterMemberModel,
    MarketEventClusterModel,
    MarketEventEmbeddingModel,
    MarketEventModel,
)
from app.services.market_event_freshness import market_event_freshness_service
from app.services.market_events import MarketEventRepository, market_event_repository
from app.services.vector_rag import DashScopeEmbeddingClient


# 算法版本进入数据库，未来调阈值后可以明确知道旧结果来自哪一版规则。
CLUSTER_ALGORITHM_VERSION = "semantic-cluster-v1"
GENERAL_SIMILARITY_THRESHOLD = 0.82
FUND_ANNOUNCEMENT_THRESHOLD = 0.88
SAME_SOURCE_SIMILARITY_THRESHOLD = 0.90
MAX_EVENT_GAP_DAYS = 7

SOURCE_LEVEL_RANK = {"A": 4, "B": 3, "C": 2, "D": 1}
SOURCE_AUTHORITY_SCORE = {"A": 30.0, "B": 22.0, "C": 12.0, "D": 5.0}


class MarketEventClusterError(RuntimeError):
    """Embedding 响应或数据库事件不满足聚类要求。"""


class MarketEventClusterRepository:
    """集中读写事件向量、事件簇和成员关系三张派生表。"""

    def __init__(self, database: Database = runtime_database) -> None:
        self.database = database
        self.database.create_schema()

    def load_embeddings(self) -> dict[str, MarketEventEmbeddingModel]:
        """读取现有向量缓存，以 event_id 建索引供 Service 判断是否可复用。"""

        with self.database.session() as session:
            rows = list(session.scalars(select(MarketEventEmbeddingModel)))
            # ORM 对象离开 Session 后仍可读，因为项目 Session 使用 expire_on_commit=False。
            return {row.event_id: row for row in rows}

    def save_embeddings(
        self,
        *,
        items: list[dict[str, Any]],
        embedded_at: datetime,
    ) -> None:
        """以 event_id 更新向量缓存；不存在于原始事件表的 ID 会被忽略。"""

        if not items:
            return
        with self.database.session() as session, session.begin():
            for item in items:
                event_id = str(item["event_id"])
                if session.get(MarketEventModel, event_id) is None:
                    continue
                model = session.get(MarketEventEmbeddingModel, event_id)
                if model is None:
                    model = MarketEventEmbeddingModel(event_id=event_id)
                    session.add(model)
                model.input_hash = str(item["input_hash"])
                model.embedding_model = str(item["embedding_model"])
                model.dimensions = len(item["vector"])
                model.vector = [float(value) for value in item["vector"]]
                model.embedded_at = embedded_at

    def replace_clusters(
        self,
        *,
        clusters: list[dict[str, Any]],
        members: list[dict[str, Any]],
    ) -> None:
        """在一个事务里整体替换聚类快照，避免 API 读到半套成员关系。"""

        with self.database.session() as session, session.begin():
            # 成员引用簇，因此先删成员、再删簇；即便 SQLite 未开外键也保持正确顺序。
            session.execute(delete(MarketEventClusterMemberModel))
            session.execute(delete(MarketEventClusterModel))
            session.add_all(
                [MarketEventClusterModel(**cluster) for cluster in clusters]
            )
            session.add_all(
                [MarketEventClusterMemberModel(**member) for member in members]
            )

    def status(self) -> dict[str, Any]:
        """统计当前有效缓存与聚类覆盖率，供状态 API 和前端展示。"""

        with self.database.session() as session:
            event_hashes = dict(
                session.execute(
                    select(MarketEventModel.event_id, MarketEventModel.content_hash)
                ).all()
            )
            embeddings = list(session.scalars(select(MarketEventEmbeddingModel)))
            members = list(session.scalars(select(MarketEventClusterMemberModel)))
            cluster_count = int(
                session.scalar(select(func.count(MarketEventClusterModel.cluster_id)))
                or 0
            )
            last_clustered_at = session.scalar(
                select(func.max(MarketEventClusterModel.generated_at))
            )

        current_embeddings = {
            item.event_id
            for item in embeddings
            if event_hashes.get(item.event_id) == item.input_hash
            and item.embedding_model == settings.embedding_model
        }
        current_members = {
            item.event_id
            for item in members
            if event_hashes.get(item.event_id) == item.input_hash
        }
        return {
            "embedding_count": len(current_embeddings),
            "cluster_count": cluster_count,
            "clustered_event_count": len(current_members),
            "pending_cluster_event_count": max(0, len(event_hashes) - len(current_members)),
            "last_clustered_at": (
                self._as_utc(last_clustered_at).isoformat()
                if last_clustered_at is not None
                else None
            ),
        }

    @staticmethod
    def _as_utc(value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)


class MarketEventClusterService:
    """使用真实 Embedding 和透明规则重建事件簇。"""

    def __init__(
        self,
        *,
        repository: MarketEventClusterRepository,
        event_repository: MarketEventRepository,
        embedding_factory: Callable[[], Any] = DashScopeEmbeddingClient,
        embedding_model: str | None = None,
    ) -> None:
        # embedding_factory 可在测试中换成 Fake Client，避免单元测试请求外网。
        self.repository = repository
        self.event_repository = event_repository
        self.embedding_factory = embedding_factory
        self.embedding_model = embedding_model or settings.embedding_model

    def build(self, *, max_new_embeddings: int | None = None, embedding_factory=None) -> dict[str, Any]:
        """为数据库中的全部事件补向量，并生成一份新的聚类快照。"""

        payload = self.event_repository.load(limit=1000)
        if payload is None:
            raise MarketEventClusterError("市场事件数据库为空，请先同步事件")

        events = list(payload["events"])
        as_of = date.fromisoformat(str(payload["data_cutoff"]))
        cached = self.repository.load_embeddings()
        vectors: dict[str, list[float]] = {}
        pending_events: list[dict[str, Any]] = []

        for event in events:
            cache = cached.get(event["event_id"])
            if (
                cache is not None
                and cache.input_hash == event["content_hash"]
                and cache.embedding_model == self.embedding_model
                and cache.vector
            ):
                vectors[event["event_id"]] = [float(value) for value in cache.vector]
            else:
                pending_events.append(event)

        reused_count = len(vectors)
        deferred_count = 0
        if max_new_embeddings is not None:
            # 自动简报限制新增向量开销；未处理的报道仍保留为独立事件，不伪装成已去重。
            pending_events.sort(key=lambda item: str(item.get("event_date", "")), reverse=True)
            limit = max(0, int(max_new_embeddings))
            deferred_count = max(0, len(pending_events) - limit)
            pending_events = pending_events[:limit]
        if pending_events:
            # 标题和摘要描述“发生了什么”；不加入 AI 研判，避免模型观点反过来影响聚类。
            texts = [self._embedding_text(event) for event in pending_events]
            generated_vectors = (embedding_factory or self.embedding_factory)().embed_texts(texts)
            if len(generated_vectors) != len(pending_events):
                raise MarketEventClusterError("Embedding 返回数量与待处理事件数量不一致")
            if any(not vector for vector in generated_vectors):
                raise MarketEventClusterError("Embedding 返回了空向量")
            dimensions = {len(vector) for vector in generated_vectors}
            if len(dimensions) != 1:
                raise MarketEventClusterError("Embedding 返回的向量维度不一致")

            embedded_at = datetime.now(timezone.utc)
            new_items = []
            for event, vector in zip(pending_events, generated_vectors, strict=True):
                normalized_vector = [float(value) for value in vector]
                vectors[event["event_id"]] = normalized_vector
                new_items.append(
                    {
                        "event_id": event["event_id"],
                        "input_hash": event["content_hash"],
                        "embedding_model": self.embedding_model,
                        "vector": normalized_vector,
                    }
                )
            self.repository.save_embeddings(items=new_items, embedded_at=embedded_at)

        generated_at = datetime.now(timezone.utc)
        components, closest_pairs = self._components(events=events, vectors=vectors)
        clusters, members = self._build_records(
            components=components,
            as_of=as_of,
            generated_at=generated_at,
        )
        self.repository.replace_clusters(clusters=clusters, members=members)

        heat_counts = {
            level: sum(cluster["heat_level"] == level for cluster in clusters)
            for level in ("high", "medium", "low")
        }
        return {
            "summary": (
                f"{len(events)} 条原始报道已归并为 {len(clusters)} 个逻辑事件簇，"
                f"减少 {len(events) - len(clusters)} 条重复计分。"
            ),
            "workflow": "market_event_clustering",
            "event_count": len(events),
            "cluster_count": len(clusters),
            "merged_report_count": len(events) - len(clusters),
            "reused_embedding_count": reused_count,
            "deferred_embedding_count": deferred_count,
            "generated_embedding_count": len(pending_events),
            "heat_level_counts": heat_counts,
            "embedding_model": self.embedding_model,
            "algorithm_version": CLUSTER_ALGORITHM_VERSION,
            # 最近的若干对用于教学调参，不参与 API 的长期业务契约。
            "closest_pairs": closest_pairs[:5],
        }

    def _components(
        self,
        *,
        events: list[dict[str, Any]],
        vectors: dict[str, list[float]],
    ) -> tuple[list[list[dict[str, Any]]], list[dict[str, Any]]]:
        """计算两两相似度，用并查集把可连通的事件合并起来。"""

        parent = list(range(len(events)))

        def find(index: int) -> int:
            while parent[index] != index:
                parent[index] = parent[parent[index]]
                index = parent[index]
            return index

        def union(left: int, right: int) -> None:
            left_root = find(left)
            right_root = find(right)
            if left_root != right_root:
                parent[right_root] = left_root

        pair_results: list[dict[str, Any]] = []
        for left in range(len(events)):
            for right in range(left + 1, len(events)):
                first = events[left]
                second = events[right]
                if first["event_id"] not in vectors or second["event_id"] not in vectors:
                    continue
                similarity = self._cosine_similarity(
                    vectors[first["event_id"]],
                    vectors[second["event_id"]],
                )
                compatible, threshold = self._compatible(first, second)
                clustered = compatible and similarity >= threshold
                if clustered:
                    union(left, right)
                pair_results.append(
                    {
                        "left_event_id": first["event_id"],
                        "right_event_id": second["event_id"],
                        "similarity": round(similarity, 4),
                        "threshold": threshold,
                        "compatible": compatible,
                        "clustered": clustered,
                    }
                )

        grouped: dict[int, list[dict[str, Any]]] = defaultdict(list)
        for index, event in enumerate(events):
            grouped[find(index)].append(event)
        pair_results.sort(key=lambda item: item["similarity"], reverse=True)
        return list(grouped.values()), pair_results

    def _build_records(
        self,
        *,
        components: list[list[dict[str, Any]]],
        as_of: date,
        generated_at: datetime,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """把内存中的连通分量转换成 ORM 可以直接写入的字段。"""

        clusters: list[dict[str, Any]] = []
        members: list[dict[str, Any]] = []
        embedding_cache = self.repository.load_embeddings()

        for component in components:
            anchor = min(
                component,
                key=lambda event: (event["event_date"], event["event_id"]),
            )
            representative = max(
                component,
                key=lambda event: (
                    SOURCE_LEVEL_RANK[event["source_level"]],
                    event["event_date"],
                    len(event["summary"]),
                    event["event_id"],
                ),
            )
            cluster_id = self._stable_cluster_id(anchor["event_id"])
            heat = self._heat(component=component, as_of=as_of)
            source_names = sorted({event["source_name"] for event in component})
            source_levels = sorted(
                {event["source_level"] for event in component},
                key=lambda level: SOURCE_LEVEL_RANK[level],
                reverse=True,
            )
            representative_vector = embedding_cache.get(representative["event_id"])
            embedded = bool(representative_vector and representative_vector.input_hash == representative["content_hash"]
                            and representative_vector.embedding_model == self.embedding_model)
            clusters.append(
                {
                    "cluster_id": cluster_id,
                    "anchor_event_id": anchor["event_id"],
                    "representative_event_id": representative["event_id"],
                    "member_count": len(component),
                    "source_count": len(source_names),
                    "source_names": source_names,
                    "source_levels": source_levels,
                    "event_start": min(date.fromisoformat(event["event_date"]) for event in component),
                    "event_end": max(date.fromisoformat(event["event_date"]) for event in component),
                    "heat_score": heat["heat_score"],
                    "heat_level": heat["heat_level"],
                    "algorithm_version": CLUSTER_ALGORITHM_VERSION if embedded else "singleton-unembedded-v1",
                    "embedding_model": self.embedding_model if embedded else "pending",
                    "generated_at": generated_at,
                }
            )

            for event in component:
                event_vector = embedding_cache.get(event["event_id"])
                # 未做向量的报道只与自身同组；1 表示同一条原文，不是模型测得的语义相似度。
                similarity = 1.0 if len(component) == 1 else self._cosine_similarity(
                    list(event_vector.vector), list(representative_vector.vector))
                members.append(
                    {
                        "event_id": event["event_id"],
                        "cluster_id": cluster_id,
                        "input_hash": event["content_hash"],
                        "similarity": round(similarity, 6),
                        "is_representative": event["event_id"] == representative["event_id"],
                    }
                )

        return clusters, members

    @staticmethod
    def _embedding_text(event: dict[str, Any]) -> str:
        return f"标题：{event['title']}\n摘要：{event['summary']}"

    @staticmethod
    def _stable_cluster_id(anchor_event_id: str) -> str:
        digest = hashlib.sha256(anchor_event_id.encode("utf-8")).hexdigest()[:16]
        return f"cluster-{digest}"

    @staticmethod
    def _compatible(
        first: dict[str, Any],
        second: dict[str, Any],
    ) -> tuple[bool, float]:
        """在语义相似度之外，阻止跨时间或跨基金的误合并。"""

        first_date = date.fromisoformat(first["event_date"])
        second_date = date.fromisoformat(second["event_date"])
        if abs((first_date - second_date).days) > MAX_EVENT_GAP_DAYS:
            return False, GENERAL_SIMILARITY_THRESHOLD

        first_codes = set(first.get("affected_fund_codes", []))
        second_codes = set(second.get("affected_fund_codes", []))
        if first_codes and second_codes and first_codes.isdisjoint(second_codes):
            return False, FUND_ANNOUNCEMENT_THRESHOLD

        is_announcement = (
            first["event_type"] == "fund_announcement"
            or second["event_type"] == "fund_announcement"
        )
        threshold = (
            FUND_ANNOUNCEMENT_THRESHOLD
            if is_announcement
            else GENERAL_SIMILARITY_THRESHOLD
        )
        # 同一家媒体连续快讯经常只是主题接近，不代表同一事件；因此需要更高阈值。
        if first["source_name"] == second["source_name"]:
            threshold = max(threshold, SAME_SOURCE_SIMILARITY_THRESHOLD)
        return True, threshold

    @staticmethod
    def _cosine_similarity(left: list[float], right: list[float]) -> float:
        """返回 -1 到 1 的夹角相似度，1 表示方向完全一致。"""

        if not left or len(left) != len(right):
            return -1.0
        left_norm = math.sqrt(sum(value * value for value in left))
        right_norm = math.sqrt(sum(value * value for value in right))
        if left_norm == 0 or right_norm == 0:
            return -1.0
        return sum(a * b for a, b in zip(left, right, strict=True)) / (
            left_norm * right_norm
        )

    @staticmethod
    def _heat(*, component: list[dict[str, Any]], as_of: date) -> dict[str, Any]:
        """用来源数量、最高来源等级和事件新鲜度计算可解释热度。"""

        source_count = len({event["source_name"] for event in component})
        source_score = min(40.0, 10.0 + 15.0 * (source_count - 1))
        authority_score = max(
            SOURCE_AUTHORITY_SCORE[event["source_level"]] for event in component
        )
        freshest_score = max(
            market_event_freshness_service.evaluate(
                event_date=event["event_date"],
                horizon=event["impact_horizon"],
                as_of=as_of,
            )["freshness_score"]
            for event in component
        )
        heat_score = round(source_score + authority_score + freshest_score * 0.30, 2)
        heat_level = "high" if heat_score >= 70 else "medium" if heat_score >= 45 else "low"
        return {"heat_score": heat_score, "heat_level": heat_level}


market_event_cluster_repository = MarketEventClusterRepository()
market_event_cluster_service = MarketEventClusterService(
    repository=market_event_cluster_repository,
    event_repository=market_event_repository,
)
