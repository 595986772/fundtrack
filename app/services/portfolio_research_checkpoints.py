"""为组合研究 LangGraph 创建可持久化的 checkpoint 存储。

Background Job 表保存的是“整个任务最终成功还是失败”；checkpoint 保存的则是
“八节点工作流已经走到哪一步、每一步产生了什么 State”；ResearchInputSnapshot
保存“这次运行使用了哪批冻结输入”。三类记录职责不同，但默认共用同一个 SQLite /
PostgreSQL 数据库，部署时不用再维护额外存储。
"""

from __future__ import annotations

import atexit
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy.engine import make_url

from app.config import settings
from app.database import PROJECT_ROOT


SUPPORTED_CHECKPOINT_BACKENDS = {"auto", "memory", "sqlite", "postgres"}


@dataclass
class PortfolioResearchCheckpointRuntime:
    """把 checkpointer、展示信息和底层连接的关闭动作放在一起管理。"""

    checkpointer: Any
    backend: str
    durable: bool
    _close_resource: Callable[[], None] | None = field(
        default=None,
        repr=False,
    )
    _closed: bool = field(default=False, init=False, repr=False)

    def close(self) -> None:
        """关闭数据库连接；重复调用不会重复关闭同一个资源。"""

        if self._closed:
            return
        self._closed = True
        if self._close_resource is not None:
            self._close_resource()


def create_portfolio_research_checkpoint_runtime(
    *,
    backend: str | None = None,
    database_url: str | None = None,
) -> PortfolioResearchCheckpointRuntime:
    """根据环境创建 Memory、SQLite 或 PostgreSQL checkpointer。

    ``auto`` 会跟随 ``DATABASE_URL``：本地默认使用 SQLite 文件，Docker 环境使用
    PostgreSQL。显式 ``memory`` 只适合单元测试或临时实验，进程退出后状态会丢失。
    """

    requested_backend = str(
        backend or settings.langgraph_checkpoint_backend
    ).strip().lower()
    if requested_backend not in SUPPORTED_CHECKPOINT_BACKENDS:
        supported = "、".join(sorted(SUPPORTED_CHECKPOINT_BACKENDS))
        raise ValueError(f"LANGGRAPH_CHECKPOINT_BACKEND 仅支持：{supported}。")

    checkpoint_url = str(
        database_url or settings.langgraph_checkpoint_database_url
    ).strip()
    resolved_backend = _resolve_backend(requested_backend, checkpoint_url)

    if resolved_backend == "memory":
        return PortfolioResearchCheckpointRuntime(
            checkpointer=InMemorySaver(),
            backend="memory",
            durable=False,
        )

    if resolved_backend == "sqlite":
        runtime = _create_sqlite_runtime(checkpoint_url)
    else:
        runtime = _create_postgres_runtime(checkpoint_url)

    # API 与 Celery Worker 都会长期持有连接。进程正常退出时统一释放；测试也可以
    # 主动调用 close()，且该方法是幂等的。
    atexit.register(runtime.close)
    return runtime


def _resolve_backend(requested_backend: str, database_url: str) -> str:
    """把 ``auto`` 翻译为数据库 URL 对应的真实后端，并校验显式配置。"""

    if requested_backend == "memory":
        return "memory"
    if not database_url:
        raise ValueError("持久化 LangGraph checkpoint 时数据库地址不能为空。")

    driver_name = make_url(database_url).drivername
    database_backend = (
        "sqlite"
        if driver_name.startswith("sqlite")
        else "postgres"
        if driver_name.startswith("postgresql")
        else ""
    )
    if not database_backend:
        raise ValueError("LangGraph checkpoint 目前只支持 SQLite 或 PostgreSQL。")
    if requested_backend != "auto" and requested_backend != database_backend:
        raise ValueError(
            "LANGGRAPH_CHECKPOINT_BACKEND 与 checkpoint 数据库地址类型不一致。"
        )
    return database_backend


def _create_sqlite_runtime(database_url: str) -> PortfolioResearchCheckpointRuntime:
    """创建适合本地开发的文件型 SqliteSaver。"""

    from langgraph.checkpoint.sqlite import SqliteSaver

    database_name = make_url(database_url).database
    if not database_name:
        raise ValueError("SQLite checkpoint 数据库必须包含文件路径。")

    if database_name == ":memory:":
        database_path = database_name
    else:
        path = Path(database_name).expanduser()
        if not path.is_absolute():
            path = PROJECT_ROOT / path
        path = path.resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        database_path = str(path)

    # LangGraph 的同步图可能在线程池里执行节点，因此关闭 SQLite 的同线程限制。
    connection = sqlite3.connect(database_path, check_same_thread=False)
    saver = SqliteSaver(connection)
    saver.setup()
    return PortfolioResearchCheckpointRuntime(
        checkpointer=saver,
        backend="sqlite",
        durable=database_path != ":memory:",
        _close_resource=connection.close,
    )


def _create_postgres_runtime(
    database_url: str,
) -> PortfolioResearchCheckpointRuntime:
    """创建适合 API + 多 Worker 共享状态的 PostgreSQL 连接池。"""

    from langgraph.checkpoint.postgres import PostgresSaver
    from psycopg.rows import dict_row
    from psycopg_pool import ConnectionPool

    # SQLAlchemy 使用 ``postgresql+psycopg`` 标识驱动；原生 psycopg 连接串不认识
    # ``+psycopg``，所以只替换 drivername，用户名、密码和 query 参数全部保留。
    parsed_url = make_url(database_url).set(drivername="postgresql")
    conninfo = parsed_url.render_as_string(hide_password=False)
    pool = ConnectionPool(
        conninfo=conninfo,
        kwargs={
            "autocommit": True,
            "prepare_threshold": 0,
            "row_factory": dict_row,
        },
        min_size=1,
        max_size=5,
        open=False,
    )
    pool.open(wait=True)
    saver = PostgresSaver(pool)
    # PostgreSQL saver 不会自动建表，首次启动必须执行官方迁移。
    saver.setup()
    return PortfolioResearchCheckpointRuntime(
        checkpointer=saver,
        backend="postgres",
        durable=True,
        _close_resource=pool.close,
    )


# 组合研究和组合调整是两张独立 LangGraph，但共享同一套 checkpoint 基础设施。
# thread_id 使用各自全局唯一的 Job ID，因此状态不会互相覆盖。
portfolio_workflow_checkpoint_runtime = (
    create_portfolio_research_checkpoint_runtime()
)
