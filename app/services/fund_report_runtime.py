"""季报问答的时间账本：同一请求共享截止时间，失败也留下分段耗时。

这里只管执行预算，不选择证据、不改数字，也不判定模型观点是否正确。
standard先使用模型默认思考；经理文字题超时才在原预算内尝试不思考整理。
evidence关闭整理轮思考；balanced保留低强度思考。
评测覆盖值使用 ContextVar，不能把一组实验的设置泄漏到并发用户请求。
"""

import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
import json
import logging
import math
from time import monotonic

from app.config import settings
from app.services.errors import AgentConfigError, AgentRuntimeError
from app.services.ai_governance_context import AIControlError


_MODE = ContextVar("fund_report_synthesis_mode", default=None)
logger = logging.getLogger(__name__)
PHASE_LABELS = {"prepare": "准备上下文", "model_decision": "模型选择工具",
    "retrieval": "读取季报证据", "synthesis": "整理季报分析",
    "validation": "核对原文引用", "repair": "修正引用"}
SYNTHESIS_POLICY = "manager-timeout-fallback-v1"


def synthesis_mode():
    mode = _MODE.get() or settings.fund_report_synthesis_mode
    if mode not in {"standard", "evidence", "balanced"}:
        raise AgentConfigError("FUND_REPORT_SYNTHESIS_MODE 只支持 standard、evidence 或 balanced")
    return mode


@contextmanager
def report_synthesis_scope(mode):
    """仅供服务端同题对照；不允许模型通过工具参数改变运行策略。"""
    if mode not in {"standard", "evidence", "balanced"}:
        raise ValueError("季报整理模式只支持 standard、evidence 或 balanced")
    token = _MODE.set(mode)
    try:
        yield
    finally:
        _MODE.reset(token)


class ReportExecutionError(AgentRuntimeError):
    """携带无密钥、无草稿的诊断，沿用产品原有失败/重试入口。"""

    def __init__(self, execution, phase, *, timed_out, http_status=None):
        self.execution_diagnostics = execution.snapshot(status="timeout" if timed_out else "error")
        label = PHASE_LABELS[phase]
        reason = ("模型服务账户余额不足（HTTP 402），请检查模型账户余额" if http_status == 402 else
            "模型服务身份验证失败（HTTP 401），请检查服务端模型配置" if http_status == 401 else
            "时间预算已用完" if timed_out else "调用失败")
        super().__init__(f"季报分析在“{label}”阶段{reason}，已停止本轮执行。"
            "未输出未经引用核对的草稿。已保存的季报仍可查看，可稍后重试；无需重新同步报告。")


class ReportExecution:
    """每次问答一个实例；时间用单调时钟计算，不受系统时钟校准影响。"""

    def __init__(self, *, run_id, started_at=None, query_mode="full"):
        self.run_id = run_id
        self.query_mode = query_mode
        self.mode = synthesis_mode()
        self.budget = settings.fund_report_timeout_seconds
        if not math.isfinite(self.budget) or self.budget <= 0:
            raise AgentConfigError("季报总时间预算必须为大于0的有限秒数")
        self.started_at = monotonic() if started_at is None else started_at
        self.deadline = self.started_at + self.budget
        self.stages = []
        self.attempts = []
        self.validation_checks = []
        self.evidence_ready = False
        self.synthesis_thinking = "model_default"
        self.record("prepare", self.started_at, "ok")

    def remaining(self):
        return max(0.0, self.deadline - monotonic())

    def record(self, phase, started, status, **details):
        row = {"phase": phase, "label": PHASE_LABELS[phase], "status": status,
            "duration_ms": round((monotonic() - started) * 1000, 2), **details}
        self.stages.append(row)
        # 日志只记阶段、时间、状态，不记消息/模型思考/原始异常，防止泄漏正文或凭据。
        logger.info("fund_report_stage %s", json.dumps({"run_id": self.run_id, **row}, ensure_ascii=False))

    def record_attempt(self, row):
        self.attempts.append(dict(row))

    def record_validation(self, *, checkpoint, issues, round_index, repair_status=None):
        """分别保留初检和复检原因；旧的 issues 合集无法判断某条错误是否已修好。"""
        self.validation_checks.append({"checkpoint": checkpoint, "round_index": round_index,
            "issues": list(issues), "passed": not issues, "repair_status": repair_status})

    @contextmanager
    def measure(self, phase):
        """本地引用核对单独计时，不把 Python 核对耗时算到模型身上。"""
        started = monotonic()
        status = "ok"
        try:
            yield
        except BaseException:
            status = "error"
            raise
        finally:
            self.record(phase, started, status)

    async def run(self, phase, operation, *, reserve_seconds=0, limit_seconds=None, round_index=0):
        """operation收到绝对截止时间，重试和供应商切换不能重置这只时钟。

        分析轮给引用修正预留时间；真正不用修正时立即返回，不刻意等满预算。
        使用工厂而不是预创建coroutine，预算耗尽时不会产生无人await的调用。
        """
        started = monotonic()
        deadline = self.deadline - reserve_seconds
        if limit_seconds is not None:
            deadline = min(deadline, started + limit_seconds)
        status = "ok"
        timed_out = False
        error = None
        error_details = {}
        try:
            if deadline <= started:
                raise TimeoutError()
            # 外层还约束备用供应商、恢复等待等开销，不只依赖SDK的单次HTTP超时。
            async with asyncio.timeout(deadline - started):
                return await operation(deadline)
        except asyncio.CancelledError:
            status = "cancelled"
            raise
        except AIControlError as exc:
            # 额度拒绝保留明确原因，不改写成“语义分析失败”，也不触发引用修正重试。
            status = "resource_rejected"
            error = exc
            raise
        except AgentConfigError:
            status = "config_error"
            raise
        except Exception as exc:
            timed_out = isinstance(exc, TimeoutError) or bool(getattr(exc, "is_timeout", False))
            status = "timeout" if timed_out else "error"
            error = exc
            # 分类信息足以排查余额/认证/网络问题；不持久化供应商响应体或请求内容。
            original = getattr(exc, "original_error", exc)
            error_details = {"error_type": type(original).__name__, "http_status": getattr(original, "status_code", None)}
        finally:
            self.record(phase, started, status, round_index=round_index,
                allotted_ms=round(max(0, deadline - started) * 1000, 2), remaining_ms=round(self.remaining() * 1000, 2), **error_details)
            if isinstance(error, AIControlError):
                error.execution_diagnostics = self.snapshot(status=status)
        raise ReportExecutionError(self, phase, timed_out=timed_out, http_status=error_details.get("http_status")) from error

    def snapshot(self, *, status="completed"):
        known_usage = {key: sum((row.get("usage") or {}).get(key, 0) for row in self.attempts)
            for key in ("input_tokens", "output_tokens", "total_tokens")}
        return {"version": "fund-report-runtime-v2", "run_id": self.run_id, "status": status,
            "synthesis_mode": self.mode, "synthesis_thinking": self.synthesis_thinking,
            "query_mode": self.query_mode, "synthesis_policy": SYNTHESIS_POLICY,
            "budget_seconds": self.budget, "duration_ms": round((monotonic() - self.started_at) * 1000, 2),
            "evidence_ready": self.evidence_ready, "stages": [dict(row) for row in self.stages],
            "model_attempts": [dict(row) for row in self.attempts],
            "validation_checks": [dict(row, issues=list(row["issues"])) for row in self.validation_checks],
            "usage": known_usage, "usage_complete": bool(self.attempts) and all(row.get("usage") is not None for row in self.attempts)}
