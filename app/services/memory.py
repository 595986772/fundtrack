"""Agent Memory 服务。

s09 Memory 要学习的重点是“跨请求保存重要事实”：
- TodoWrite 是一次任务内的短期计划；
- Context Compact 是一次 Agent Loop 内的上下文整理；
- Memory 是服务重启后仍然存在的长期信息。

当前教学版不用数据库，先把记忆保存成 data/memory/ 下的 Markdown 文件。
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import datetime
import json
from pathlib import Path
import re
from typing import Any, Literal
from uuid import uuid4


PROJECT_ROOT = Path(__file__).resolve().parents[2]

# 所有长期记忆都固定保存在当前项目的 data/memory/ 下面。
# 这样做是为了教学清晰，也避免工具随意读写用户电脑上的其他目录。
MEMORY_ROOT = PROJECT_ROOT / "data" / "memory"

# 单条记忆不要太长。Memory 适合保存“可复用事实”，不适合塞整篇文档。
MAX_MEMORY_CONTENT_CHARS = 3_000

# 搜索时最多返回多少条结果，防止一次把太多记忆塞回模型上下文。
MAX_MEMORY_RESULTS = 20

# 标签数量也要限制。标签只是帮助检索，不应该变成另一段长文本。
MAX_TAGS = 8

# MemoryType 是给类型检查和 Pydantic schema 看的枚举。
# 当前 demo 先支持四类常见长期记忆。
MemoryType = Literal[
    "user_preference",
    "project_fact",
    "feedback",
    "reference_clue",
]

# memory_type 到文件夹名的映射。
# 例如 user_preference 会保存到 data/memory/user-preferences/<user_id>/。
MEMORY_TYPE_DIRS: dict[str, str] = {
    "user_preference": "user-preferences",
    "project_fact": "project-facts",
    "feedback": "feedback",
    "reference_clue": "reference-clues",
}


@dataclass(frozen=True)
class MemoryRecord:
    """一条长期记忆的结构化信息。"""

    # 记忆唯一 ID，用 uuid 生成，方便以后引用或删除。
    id: str

    # 记忆类型：用户偏好、项目事实、反馈、引用线索。
    memory_type: str

    # 这条记忆属于哪个用户。当前 demo 用 user_id 做简单隔离。
    user_id: str

    # 记忆正文，也就是以后模型真正可能需要参考的内容。
    content: str

    # 标签用于粗略检索和分类，不参与复杂向量搜索。
    tags: list[str]

    # 来源，例如 agent、test、manual。方便知道是谁写入的。
    source: str

    # 重要程度，1-5。搜索排序时会稍微参考这个值。
    importance: int

    # 创建时间，用 ISO 字符串保存，方便排序和阅读。
    created_at: str

    # 记忆文件在项目内的相对路径。
    path: str

    # 搜索时动态计算出来的匹配分数；保存文件时通常是 0。
    score: int = 0


class MemoryService:
    """负责保存、搜索和生成 memory index。"""

    def remember(
        self,
        *,
        memory_type: str,
        content: str,
        user_id: str = "guest",
        tags: list[str] | None = None,
        source: str = "agent",
        importance: int = 3,
    ) -> dict[str, Any]:
        """把一条长期记忆写入 data/memory/。"""

        # 先统一清洗所有输入。
        # 工具参数来自模型，后端不能直接信任，所以这里要做二次校验。
        normalized_type = self._normalize_memory_type(memory_type)
        normalized_user_id = self._normalize_user_id(user_id)
        normalized_content = self._normalize_content(content)
        normalized_tags = self._normalize_tags(tags or [])
        normalized_source = self._normalize_source(source)

        # importance 限制在 1-5，避免模型传入 999 这种没有意义的值。
        normalized_importance = max(1, min(int(importance), 5))

        # 每条记忆都有独立 ID 和创建时间。
        # ID 用来稳定引用，时间用来排序和生成文件名。
        memory_id = f"mem_{uuid4().hex[:12]}"
        created_at = datetime.now().isoformat(timespec="seconds")

        # 根据 memory_type 和 user_id 组织目录。
        # 例如：data/memory/user-preferences/chen/xxx.md
        folder = MEMORY_ROOT / MEMORY_TYPE_DIRS[normalized_type] / normalized_user_id
        folder.mkdir(parents=True, exist_ok=True)

        # 文件名里放时间和正文 slug，方便你在本地文件夹里肉眼查看。
        filename = f"{created_at.replace(':', '-')}-{self._slugify(normalized_content)}.md"
        path = folder / filename

        # 记忆文件采用 Markdown：
        # frontmatter 放机器可读的 metadata；
        # 正文放人类可读的 memory content。
        markdown = self._build_markdown(
            memory_id=memory_id,
            memory_type=normalized_type,
            user_id=normalized_user_id,
            content=normalized_content,
            tags=normalized_tags,
            source=normalized_source,
            importance=normalized_importance,
            created_at=created_at,
        )
        path.write_text(markdown, encoding="utf-8")

        # 写入成功后，把同样的信息组装成 MemoryRecord，
        # 再转成接口/工具适合返回的 dict。
        record = MemoryRecord(
            id=memory_id,
            memory_type=normalized_type,
            user_id=normalized_user_id,
            content=normalized_content,
            tags=normalized_tags,
            source=normalized_source,
            importance=normalized_importance,
            created_at=created_at,
            path=self._relative_path(path),
        )

        return {
            "ok": True,
            "memory": self._record_to_public_dict(record),
            "message": "长期记忆已保存到 data/memory/。",
        }

    def search(
        self,
        *,
        query: str,
        user_id: str = "guest",
        memory_type: str | None = None,
        max_results: int = 5,
    ) -> dict[str, Any]:
        """按关键词搜索长期记忆。"""

        # query 是用户或模型要检索的关键词/问题。
        # 为空时搜索没有意义，所以直接报错。
        normalized_query = str(query or "").strip()

        if not normalized_query:
            raise ValueError("query 不能为空。")

        # user_id 做隔离：默认只搜当前用户和 global 共享记忆。
        normalized_user_id = self._normalize_user_id(user_id)

        # memory_type 可选；传了就只搜某一类，不传就搜所有类型。
        normalized_type = (
            self._normalize_memory_type(memory_type)
            if memory_type
            else None
        )

        # max_results 也要收口，避免一次返回太多内容。
        max_results = max(1, min(int(max_results), MAX_MEMORY_RESULTS))

        # 先把磁盘上的 Markdown 记忆加载成 MemoryRecord。
        records = self._load_records(user_id=normalized_user_id, memory_type=normalized_type)

        # 当前教学版不用 embedding/vector db，只做简单关键词打分。
        # 这样你能先看懂“检索长期记忆”的底层流程。
        scored_records = [
            self._score_record(record=record, query=normalized_query)
            for record in records
        ]

        # score=0 表示完全没匹配上，就不返回给模型。
        hits = [
            record
            for record in scored_records
            if record.score > 0
        ]

        # 分数越高越靠前；同分时 importance 更高、创建时间更新的靠前。
        hits.sort(
            key=lambda item: (item.score, item.importance, item.created_at),
            reverse=True,
        )

        return {
            "ok": True,
            "query": normalized_query,
            "user_id": normalized_user_id,
            "memory_type": normalized_type,
            "count": len(hits[:max_results]),
            "hits": [
                self._record_to_public_dict(record)
                for record in hits[:max_results]
            ],
        }

    def recent_index(
        self,
        *,
        user_id: str,
        max_items: int = 8,
    ) -> list[dict[str, Any]]:
        """返回最近记忆的轻量索引，给接口调试看。"""

        # current_memories 只展示最近几条，不返回所有记忆。
        # 这和 Context Compact 的思路类似：上下文里只放必要摘要。
        records = self._load_records(user_id=self._normalize_user_id(user_id))
        records.sort(key=lambda item: item.created_at, reverse=True)

        return [
            self._record_to_public_dict(record, include_full_content=False)
            for record in records[:max_items]
        ]

    def index_for_prompt(self, *, user_id: str, max_items: int = 6) -> str:
        """生成放进 system prompt 的轻量 memory index。"""

        # system prompt 里不应该放所有 memory 正文。
        # 这里只放轻量目录，让模型知道“有什么可能相关的长期记忆”。
        index = self.recent_index(user_id=user_id, max_items=max_items)

        if not index:
            return "当前用户暂无长期记忆。"

        lines = [
            "当前用户长期记忆索引（只放轻量摘要，不放所有正文）：",
        ]

        for item in index:
            # 每一行都保持短小：类型 + 摘要 + 标签 + 文件路径。
            # 如果模型需要更详细内容，可以再调用 search_memory_tool。
            lines.append(
                "- "
                f"[{item['memory_type']}] "
                f"{item['content_preview']} "
                f"(tags={item['tags']}, path={item['path']})"
            )

        return "\n".join(lines)

    def _load_records(
        self,
        *,
        user_id: str,
        memory_type: str | None = None,
    ) -> list[MemoryRecord]:
        """从 data/memory/ 扫描 Markdown 记忆。"""

        # 第一次运行时 data/memory/ 可能还不存在，直接返回空列表即可。
        if not MEMORY_ROOT.exists():
            return []

        # 当前用户可以看到自己的记忆，也可以看到 global 共享记忆。
        # 其他用户的记忆不会被当前请求检索出来。
        allowed_user_ids = {user_id, "global"}
        records: list[MemoryRecord] = []

        # 用 rglob 扫描所有 Markdown 文件。
        # 当前 demo 文件量很小，这种方式最直观；真实系统可换成数据库/索引。
        for path in sorted(MEMORY_ROOT.rglob("*.md")):
            try:
                record = self._read_memory_file(path)
            except Exception:
                # 某个文件坏了不要影响整个 memory 检索。
                # 教学 demo 里直接跳过；生产系统应记录日志。
                continue

            # 用户隔离：只保留当前用户和 global。
            if record.user_id not in allowed_user_ids:
                continue

            # 如果指定 memory_type，就过滤掉其他类型。
            if memory_type and record.memory_type != memory_type:
                continue

            records.append(record)

        return records

    def _read_memory_file(self, path: Path) -> MemoryRecord:
        """读取单个 Markdown 记忆文件。"""

        # 文件格式是：
        # ---
        # { JSON metadata }
        # ---
        #
        # 记忆正文
        text = path.read_text(encoding="utf-8")
        metadata, content = self._parse_frontmatter(text)

        # metadata 缺字段时给默认值，避免旧文件或手写文件导致服务崩溃。
        return MemoryRecord(
            id=str(metadata.get("id") or path.stem),
            memory_type=str(metadata.get("memory_type") or "project_fact"),
            user_id=str(metadata.get("user_id") or "guest"),
            content=content.strip(),
            tags=self._parse_tags(metadata.get("tags")),
            source=str(metadata.get("source") or "unknown"),
            importance=int(metadata.get("importance") or 3),
            created_at=str(metadata.get("created_at") or ""),
            path=self._relative_path(path),
        )

    def _score_record(self, *, record: MemoryRecord, query: str) -> MemoryRecord:
        """给搜索结果打一个简单关键词分数。"""

        # 为了搜索方便，统一转小写。
        query_text = query.lower()

        # 被搜索的目标文本包含：正文 + 类型 + 标签。
        # 这样搜 "preference"、"学习"、"style" 都可能匹配到相关记忆。
        target_text = " ".join(
            [
                record.content,
                record.memory_type,
                " ".join(record.tags),
            ]
        ).lower()
        query_terms = self._tokenize(query_text)

        score = 0

        # 如果整句 query 出现在记忆里，说明匹配很强。
        if query_text in target_text:
            score += 6

        # 英文/数字词逐个匹配，命中一个加一点分。
        for term in query_terms:
            if term in target_text:
                score += 2

        # 中文问题没有空格时，单个字符也能提供一点弱匹配能力。
        for char in query_text:
            if "\u4e00" <= char <= "\u9fff" and char in target_text:
                score += 1

        # dataclass 是 frozen=True，不允许原地修改 score。
        # 所以这里复制一份新的 MemoryRecord，把 score 填进去。
        return MemoryRecord(
            **{
                **asdict(record),
                "score": score,
            }
        )

    def _build_markdown(
        self,
        *,
        memory_id: str,
        memory_type: str,
        user_id: str,
        content: str,
        tags: list[str],
        source: str,
        importance: int,
        created_at: str,
    ) -> str:
        """构造带 frontmatter 的 Markdown 记忆。"""

        # metadata 是机器读的部分，便于后续扫描、过滤和排序。
        metadata = {
            "id": memory_id,
            "memory_type": memory_type,
            "user_id": user_id,
            "created_at": created_at,
            "source": source,
            "importance": importance,
            "tags": tags,
        }

        # 正文是人读的部分，你可以直接在 Obsidian/PyCharm 里打开看。
        return (
            "---\n"
            f"{json.dumps(metadata, ensure_ascii=False, indent=2)}\n"
            "---\n\n"
            f"{content}\n"
        )

    def _parse_frontmatter(self, text: str) -> tuple[dict[str, Any], str]:
        """解析当前 demo 使用的 JSON frontmatter。"""

        # 没有 frontmatter 就当成普通正文。
        if not text.startswith("---\n"):
            return {}, text

        # 找到第二个 ---，它表示 metadata 结束。
        end_index = text.find("\n---\n", 4)

        if end_index == -1:
            return {}, text

        raw_metadata = text[4:end_index].strip()
        content = text[end_index + len("\n---\n") :]

        try:
            metadata = json.loads(raw_metadata)
        except json.JSONDecodeError:
            # metadata 写坏时不要让服务崩溃，退化成无 metadata 的记忆。
            metadata = {}

        return metadata, content

    def _record_to_public_dict(
        self,
        record: MemoryRecord,
        *,
        include_full_content: bool = True,
    ) -> dict[str, Any]:
        """把 MemoryRecord 转成接口和工具返回的安全结构。"""

        # 默认先返回轻量字段，包括 preview、标签、路径和分数。
        # 是否返回完整 content 由 include_full_content 控制。
        data = {
            "id": record.id,
            "memory_type": record.memory_type,
            "user_id": record.user_id,
            "content_preview": self._preview(record.content),
            "tags": record.tags,
            "source": record.source,
            "importance": record.importance,
            "created_at": record.created_at,
            "path": record.path,
            "score": record.score,
        }

        if include_full_content:
            # search_memory_tool 需要把正文交给模型，所以搜索结果会带 content。
            # recent_index / prompt index 则通常不带完整正文。
            data["content"] = record.content

        return data

    def _normalize_memory_type(self, memory_type: str | None) -> str:
        """校验并统一 memory_type。"""

        # 允许模型传 user-preference，也统一转成 user_preference。
        normalized = str(memory_type or "").strip().replace("-", "_")

        if normalized not in MEMORY_TYPE_DIRS:
            allowed = ", ".join(MEMORY_TYPE_DIRS)
            raise ValueError(f"memory_type 只能是：{allowed}")

        return normalized

    def _normalize_user_id(self, user_id: str) -> str:
        """限制 user_id，避免写出 data/memory/。"""

        normalized = str(user_id or "guest").strip()

        # 只允许简单字符，防止类似 ../../xxx 的路径穿越。
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,50}", normalized):
            raise ValueError("user_id 只能包含字母、数字、下划线和短横线。")

        return normalized

    def _normalize_content(self, content: str) -> str:
        """校验记忆正文。"""

        normalized = str(content or "").strip()

        # 空内容没有保存价值。
        if not normalized:
            raise ValueError("记忆内容不能为空。")

        # 长期记忆应是“简洁事实”，不是大段文档。
        if len(normalized) > MAX_MEMORY_CONTENT_CHARS:
            raise ValueError(f"记忆内容不能超过 {MAX_MEMORY_CONTENT_CHARS} 字符。")

        return normalized

    def _normalize_tags(self, tags: list[str]) -> list[str]:
        """清洗标签，避免标签过多或过长。"""

        normalized_tags: list[str] = []

        # 只处理前 MAX_TAGS 个标签，后面的忽略。
        for tag in tags[:MAX_TAGS]:
            normalized = str(tag or "").strip().lower()

            # 标签只保留英文、数字、下划线、短横线和中文。
            # 其他符号统一替换成短横线。
            normalized = re.sub(r"[^a-z0-9_\-\u4e00-\u9fff]", "-", normalized)
            normalized = normalized.strip("-")

            # 去掉空标签和重复标签，并限制单个标签长度。
            if normalized and normalized not in normalized_tags:
                normalized_tags.append(normalized[:32])

        return normalized_tags

    def _normalize_source(self, source: str) -> str:
        """限制 source 字段。"""

        normalized = str(source or "agent").strip()

        # source 只用于调试展示，不允许包含路径分隔符或复杂符号。
        normalized = re.sub(r"[^A-Za-z0-9_\-]", "-", normalized)
        return normalized[:60] or "agent"

    def _parse_tags(self, value: Any) -> list[str]:
        """从 metadata 里解析 tags。"""

        # 新格式里 tags 是数组。
        if isinstance(value, list):
            return [str(item) for item in value]

        # 兼容手写文件里用逗号分隔的 tags。
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]

        return []

    def _tokenize(self, text: str) -> list[str]:
        """简单英文/数字分词。中文会在 _score_record 里按字符弱匹配。"""

        # 当前只是教学版关键词搜索，不做复杂 NLP。
        return re.findall(r"[a-z0-9_\-]{2,}", text.lower())

    def _slugify(self, content: str) -> str:
        """从记忆内容里生成文件名片段。"""

        # 只用英文数字生成 slug；中文内容会退化成 memory。
        # 这不影响正文保存，只影响文件名可读性。
        slug = re.sub(r"[^A-Za-z0-9]+", "-", content.lower()).strip("-")

        return (slug[:40] or "memory").strip("-")

    def _preview(self, content: str, max_chars: int = 140) -> str:
        """生成适合放进 prompt 或响应的短摘要。"""

        # 先压平空白，避免换行把 prompt 变得很散。
        normalized = " ".join(content.split())

        if len(normalized) <= max_chars:
            return normalized

        # 超长就截断并加省略号。
        return f"{normalized[:max_chars]}..."

    def _relative_path(self, path: Path) -> str:
        """返回项目内相对路径。"""

        # 接口里只展示相对路径，不暴露冗长的本机绝对路径。
        return path.resolve().relative_to(PROJECT_ROOT).as_posix()


# 单例：其他模块直接 import memory_service 使用同一个 MemoryService。
memory_service = MemoryService()
