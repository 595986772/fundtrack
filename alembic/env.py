"""Alembic 运行环境：读取与 FastAPI 相同的 DATABASE_URL 和 ORM metadata。"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from app.config import settings
from app.database import Base
import app.models  # noqa: F401  # 导入后所有表才会注册到 Base.metadata


config = context.config
config.set_main_option("sqlalchemy.url", settings.database_url)
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata

# LangGraph 的 SQLite checkpointer 会在同一个数据库里维护这两张内部表，
# 它们不属于本项目 ORM，也不应被 Alembic 误判成“需要删除”的业务表。
EXTERNALLY_MANAGED_TABLES = {"checkpoints", "writes"}


def include_object(
    object_,
    name: str | None,
    type_: str,
    reflected: bool,
    compare_to,
) -> bool:
    """把第三方运行时自己维护的表排除在 schema diff 之外。"""

    if (
        type_ == "table"
        and reflected
        and compare_to is None
        and name in EXTERNALLY_MANAGED_TABLES
    ):
        return False
    return True


def run_migrations_offline() -> None:
    """离线模式只生成 SQL，不创建数据库连接。"""

    context.configure(
        url=settings.database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        include_object=include_object,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """在线模式使用同步 SQLAlchemy Engine 执行 revision。"""

    configuration = config.get_section(config.config_ini_section) or {}
    connectable = engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            include_object=include_object,
            render_as_batch=connection.dialect.name == "sqlite",
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
