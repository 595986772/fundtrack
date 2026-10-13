"""s19 教学 MCP Server。

运行方式：
    conda run -n dl python -m app.mcp_servers.learning_server

正常情况下不需要手动运行。FastAPI 中的 MCP Client 会启动这个模块作为子进程，
然后通过 stdio 发送 ``tools/list`` 和 ``tools/call`` 请求。

注意：stdio 的标准输出属于 MCP 协议通道，不能随意 ``print`` 调试文字，否则会
破坏 JSON-RPC 消息。需要排查 Server 时应写到 stderr 或使用日志。
"""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations


# FastMCP 负责协议层细节，例如初始化握手、tools/list 和 tools/call。
# 业务开发者只需要像写普通 Python 函数一样声明工具。
mcp = FastMCP(
    name="agent-learning",
    instructions=(
        "只读的 Agent Harness 课程资料服务。"
        "它用于演示 MCP 工具发现、命名空间和跨进程调用。"
    ),
    # Server 日志走 stderr；教学测试时只显示 warning 以上，避免终端被请求日志刷屏。
    log_level="WARNING",
)

# annotations 是 MCP 工具提供给客户端的安全提示。
# 当前两个教学工具都不写文件、不访问网络，重复调用也不会改变任何状态。
READ_ONLY_ANNOTATIONS = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=False,
)


# 这份小型资料库故意保存在 MCP Server 内部。
# 主 Agent 不直接 import 它，只能通过 MCP 协议查询，借此体现外部服务边界。
CHAPTERS: dict[str, dict[str, Any]] = {
    "s18": {
        "title": "Worktree Isolation",
        "summary": "为并行代码任务创建独立 Git 工作目录和分支，避免成员互相覆盖。",
        "concepts": ["git worktree", "task binding", "workspace context", "review"],
    },
    "s19": {
        "title": "MCP Tools",
        "summary": "通过标准协议发现并调用外部工具，再把它们合并进 Agent 工具池。",
        "concepts": [
            "MCP Client",
            "MCP Server",
            "tools/list",
            "tools/call",
            "mcp__server__tool",
        ],
    },
    "s20": {
        "title": "Comprehensive Agent",
        "summary": "把工具、权限、Hooks、任务、记忆、团队和 MCP 合回同一个稳定循环。",
        "concepts": ["single harness", "shared loop", "observability", "integration"],
    },
}


@mcp.tool(
    name="search_course_notes",
    title="搜索课程资料",
    description=(
        "按关键词搜索 s18-s20 的 Agent Harness 课程摘要。"
        "适合查询 MCP、Worktree、综合 Agent 等概念。"
    ),
    annotations=READ_ONLY_ANNOTATIONS,
    structured_output=True,
)
def search_course_notes(query: str, max_results: int = 3) -> dict[str, Any]:
    """搜索 Server 自己管理的课程资料。

    Args:
        query: 要查找的关键词，例如 MCP、tools/list、worktree。
        max_results: 最多返回几条结果，范围 1-5。
    """

    normalized_query = str(query or "").strip().casefold()
    if not normalized_query:
        raise ValueError("query 不能为空")

    bounded_limit = max(1, min(int(max_results), 5))
    matches: list[dict[str, Any]] = []

    for chapter_id, chapter in CHAPTERS.items():
        searchable_text = " ".join(
            [
                chapter_id,
                str(chapter["title"]),
                str(chapter["summary"]),
                *[str(item) for item in chapter["concepts"]],
            ]
        ).casefold()

        if normalized_query in searchable_text:
            matches.append(
                {
                    "chapter_id": chapter_id,
                    "title": chapter["title"],
                    "summary": chapter["summary"],
                    "concepts": chapter["concepts"],
                }
            )

    return {
        "query": query,
        "count": len(matches[:bounded_limit]),
        "results": matches[:bounded_limit],
        "source": "agent-learning MCP server",
    }


@mcp.tool(
    name="get_chapter_outline",
    title="读取章节提纲",
    description="按 s18、s19 或 s20 读取一章的标题、摘要和核心概念。",
    annotations=READ_ONLY_ANNOTATIONS,
    structured_output=True,
)
def get_chapter_outline(chapter_id: str = "s19") -> dict[str, Any]:
    """读取一个确定章节的完整提纲。"""

    normalized_id = str(chapter_id or "").strip().casefold()
    chapter = CHAPTERS.get(normalized_id)
    if chapter is None:
        raise ValueError("未知章节，只支持 s18、s19、s20")

    return {
        "chapter_id": normalized_id,
        **chapter,
        "source": "agent-learning MCP server",
    }


if __name__ == "__main__":
    # 子进程会一直等待 MCP Client 的 JSON-RPC 消息，直到 Client 断开。
    mcp.run(transport="stdio")
