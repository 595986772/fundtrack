"""Agent 评测运行时的数据隔离上下文。

线上基金数据会持续同步到数据库，如果评测直接读取“今天的数据”，同一个问题明天
可能得到不同事实，得分也就无法比较。这里使用 ``ContextVar`` 给当前评测协程加一个
局部标记：业务 Service 看到标记后只读取仓库内的固定 fixture。

它不是全局开关。真实用户请求和另一个并发任务仍然可以继续读取数据库最新快照。
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
import hashlib
from pathlib import Path
from typing import Iterator


DEFAULT_EVAL_DATA_PROFILE = "fundscope-fixture-v1"

# None 表示普通产品请求；非空字符串表示当前调用正在运行可复现评测。
_ACTIVE_EVAL_DATA_PROFILE: ContextVar[str | None] = ContextVar(
    "active_eval_data_profile",
    default=None,
)


@contextmanager
def use_frozen_eval_data(
    profile: str = DEFAULT_EVAL_DATA_PROFILE,
) -> Iterator[None]:
    """在当前调用链内启用固定数据，退出后恢复原来的上下文。"""

    token = _ACTIVE_EVAL_DATA_PROFILE.set(profile)
    try:
        yield
    finally:
        # reset 使用进入上下文前的 token，嵌套评测也不会把外层状态误清空。
        _ACTIVE_EVAL_DATA_PROFILE.reset(token)


def frozen_eval_data_enabled() -> bool:
    """供基金、市场和事件 Service 判断是否必须跳过数据库增量。"""

    return _ACTIVE_EVAL_DATA_PROFILE.get() is not None


def active_eval_data_profile() -> str | None:
    """返回当前固定数据集名称，便于写入评测报告。"""

    return _ACTIVE_EVAL_DATA_PROFILE.get()


def fixture_fingerprint(paths: list[Path]) -> str:
    """计算 fixture 内容指纹，证明两次评测使用的是同一批原始数据。

    文件名和内容都会进入 SHA-256。只显示前 16 位已经足够用于本地回归辨识，完整
    文件仍由 Git 负责版本追踪。
    """

    digest = hashlib.sha256()
    for path in sorted((Path(item) for item in paths), key=lambda item: str(item)):
        if not path.is_file():
            raise FileNotFoundError(f"评测 fixture 不存在：{path}")
        digest.update(path.name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return f"sha256:{digest.hexdigest()[:16]}"


def eval_case_set_id(case_ids: list[str]) -> str:
    """为一组评测 case 生成稳定 ID，供前后两轮成绩判断是否可比。

    同一批 case 即使调用方传入顺序不同，也会得到同一个 ID；少一条或多一条都会
    得到不同 ID。这样完整回归不会错误地拿单场景调试结果作为“上次成绩”。
    """

    normalized = sorted({str(case_id).strip() for case_id in case_ids if str(case_id).strip()})
    digest = hashlib.sha256("\0".join(normalized).encode("utf-8")).hexdigest()[:16]
    return f"cases:{len(normalized)}:{digest}"
