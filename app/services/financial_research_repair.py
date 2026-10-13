"""综合研究的有限补查：缺人问人，缺公开披露才查源，查不到就保留缺口。

本模块不让模型自由上网。它只调度已有业务分支和一个白名单披露 Provider。
授权与预算属于当前请求，不属于模型参数或长期记忆；重复调用工具也不会重置预算。
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from threading import Lock
from time import monotonic
from typing import Any, Callable, Iterator

from app.services.eval_context import frozen_eval_data_enabled
from app.services.chat_execution_budget import check_chat_active, chat_deadline


MAX_REPAIR_ROUNDS = 2
DISCLOSURE_TIMEOUT_SECONDS = 25
BRIEF_TOOLS = frozenset({"get_fund_research_brief_tool", "get_portfolio_research_brief_tool"})


@dataclass
class ResearchRepairBudget:
    """一次聊天共享两轮预算；锁避免并行 Tool 同时抢到最后一个名额。"""

    allow_public_refresh: bool = False
    attempted: set[str] = field(default_factory=set)
    used_rounds: int = 0
    cancelled: bool = False
    checkpoint: Callable[[dict], None] | None = field(default=None, repr=False)
    lock: Lock = field(default_factory=Lock, repr=False)

    def reserve(self, action: str) -> bool:
        with self.lock:
            check_chat_active()  # 已截止的工具线程不能再申请下一次公开补查。
            if self.cancelled or action in self.attempted or self.used_rounds >= MAX_REPAIR_ROUNDS:
                return False
            self.attempted.add(action)
            self.used_rounds += 1
            # 先保存名额，再访问公开源。后续模型总结失败或进程中断时，同一幂等
            # 请求重试仍然使用这个预算，不能把已尝试的来源再领一遍。
            if self.checkpoint is not None:
                self.checkpoint({"attempted": sorted(self.attempted), "used_rounds": self.used_rounds})
            return True


_BUDGET: ContextVar[ResearchRepairBudget | None] = ContextVar("research_repair_budget", default=None)


@contextmanager
def research_repair_scope(
    *, allow_public_refresh: bool = False, previous: dict | None = None,
    checkpoint: Callable[[dict], None] | None = None,
) -> Iterator[ResearchRepairBudget]:
    """仅 HTTP/内部可信请求能开启本轮刷新，退出（包括取消）后撤销权限。"""

    saved = previous or {}
    budget = ResearchRepairBudget(
        allow_public_refresh=allow_public_refresh,
        attempted=set(saved.get("attempted", [])), used_rounds=int(saved.get("used_rounds", 0)),
        checkpoint=checkpoint,
    )
    token = _BUDGET.set(budget)
    try:
        yield budget
    finally:
        # 同步 Tool 在线程里运行，协程取消不一定立即终止线程。标记后禁止继续补查/落库。
        with budget.lock:
            budget.cancelled = True
        _BUDGET.reset(token)


def current_repair_budget() -> ResearchRepairBudget | None:
    return _BUDGET.get()


def _refresh_disclosure(fund_code: str, budget: ResearchRepairBudget) -> dict[str, Any]:
    """在有硬超时的子进程里只抓取，返回后由当前进程写入现有 SQL 披露表。"""

    from datetime import datetime, timezone
    from app.services.fund_disclosure_fetch import fetch_disclosure_bounded
    from app.services.fund_disclosures import DISCLOSURE_SOURCE, fund_disclosure_repository

    check_chat_active()
    deadline = chat_deadline()
    timeout = min(DISCLOSURE_TIMEOUT_SECONDS, max(.001, deadline - monotonic())) if deadline else DISCLOSURE_TIMEOUT_SECONDS
    batch = fetch_disclosure_bounded(fund_code, timeout_seconds=timeout)
    # 空表/更早报告不是补充证据，不能把数据库已有披露覆盖为空或退回旧期。
    if not batch.security_holdings and not batch.industry_allocations:
        return {"status": "no_update", "detail": "公开源未返回可用明细，保留已有披露"}
    for kind in ("stock", "bond", "industry"):
        rows = batch.industry_allocations if kind == "industry" else [
            row for row in batch.security_holdings if row.asset_type == kind
        ]
        # '--' 等非法权重可能被 Provider 全部过滤，留下“有日期、无明细”。
        # 另一类资产有效不能证明这个空类也有效，不能据此删除同报告期旧持仓。
        if getattr(batch, f"{kind}_report_date") is not None and not rows:
            return {"status": "no_update", "detail": f"{kind} 报告期存在但没有有效明细，保留原披露"}
    with budget.lock:
        check_chat_active()  # 协程已超时但线程刚返回时，不再提交本轮迟到的刷新结果。
        if budget.cancelled:
            return {"status": "cancelled", "detail": "本轮已停止，未写入抓取结果"}
        previous = fund_disclosure_repository.latest(fund_code=fund_code) or {}
        for kind in ("stock", "bond", "industry"):
            old = (previous.get("report_dates") or {}).get(kind)
            new = getattr(batch, f"{kind}_report_date")
            if old and (new is None or new.isoformat() < old):
                return {"status": "no_update", "detail": "源数据缺少已有资产类别或报告期倒退，保留原披露"}
        fund_disclosure_repository.replace_latest(
            batch=batch, source=DISCLOSURE_SOURCE, fetched_at=datetime.now(timezone.utc),
        )
    return {"status": "completed", "detail": "已读取公开披露并保存；是否改善证据须重新检查"}


class FinancialResearchRepairService:
    """预算内执行补查，每次完成后重新跑同一个证据闸门，不人为抬高覆盖分数。"""

    def __init__(self, refresh_disclosure: Callable = _refresh_disclosure) -> None:
        self.refresh_disclosure = refresh_disclosure

    def run(
        self, *, gate: dict, task_results: dict, tasks: dict, evaluate: Callable,
        run_tasks: Callable, fund_codes: list[str],
        disclosure_tasks: tuple[str, ...], scope_key: str,
    ) -> tuple[dict, dict]:
        # 独立调用 Brief 没有聊天授权，默认只能重读本地数据。
        budget = current_repair_budget() or ResearchRepairBudget()
        prior_used_rounds = budget.used_rounds
        prior_actions = sorted(budget.attempted)
        initial = self._gate_snapshot(gate)
        attempts: list[dict] = []
        public_allowed = budget.allow_public_refresh and not frozen_eval_data_enabled()

        # 先重试真正失败的本地读取，零事件/披露覆盖不全等合法结果不等于执行失败。
        failed = {key: tasks[key] for key, result in task_results.items() if result["status"] == "failed"}
        if failed and budget.reserve(f"local:{scope_key}"):
            before = self._gate_snapshot(gate)
            reread = run_tasks(failed)
            task_results.update(reread)
            gate = evaluate()
            attempts.append({
                "action": "local_reread", "label": "重读失败的本地证据", "tasks": list(failed),
                "status": "completed" if all(row["status"] == "completed" for row in reread.values()) else "failed",
                "before": before, "after": self._gate_snapshot(gate),
            })

        # 只在闸门确实发现持仓缺口时检查相关基金，组合按仓位顺序传入，优先补大仓位。
        holding_key = "holdings_exposure" if scope_key.startswith("fund:") else "lookthrough_exposure"
        candidates: list[str] = []
        diagnostics: list[str] = []
        requirements = {item["key"]: item for item in gate["requirements"]}
        if (requirements.get(holding_key) or {}).get("status") in {"missing", "partial"}:
            # 使用本轮已经取得的元数据，不在预算之外再次读取失败分支。
            # 组合穿透的 fund_reports 已带每只基金的时效与覆盖，未知则保留候选。
            if scope_key.startswith("fund:"):
                disclosure_map = {fund_codes[0]: task_results["fund_disclosure"].get("data") or {}}
            else:
                exposure = task_results["portfolio_exposure"].get("data") or {}
                disclosure_map = {row["fund_code"]: row for row in exposure.get("fund_reports", [])}
            for code in dict.fromkeys(fund_codes):
                if self._needs_disclosure(disclosure_map.get(code, {})):
                    candidates.append(code)

        for code in candidates:
            if not public_allowed:
                break
            if not budget.reserve(f"disclosure:{code}"):
                continue
            before = self._gate_snapshot(gate)
            try:
                result = self.refresh_disclosure(code, budget)
                if result["status"] == "completed" and not budget.cancelled:
                    reread = run_tasks({key: tasks[key] for key in disclosure_tasks})
                    for key, row in reread.items():
                        # 新读取失败不删除之前成功的事实；额外标记“旧证据保留”，避免冒充新证据。
                        if row["status"] == "completed" or task_results[key]["status"] != "completed":
                            task_results[key] = row
                        else:
                            diagnostics.append(f"{key} 更新后重读失败，仍使用补查前结果：{row.get('error', '')}")
                gate = evaluate()
                after = self._gate_snapshot(gate)
                outcome = result["status"]
                if outcome == "completed":
                    # 状态变化可能是发现更多风险，不把所有变化都命名为“改善”。
                    outcome = "evidence_changed" if after != before else "checked_no_change"
                attempts.append({
                    "action": "disclosure_refresh", "label": f"检查 {code} 最新持仓披露",
                    "fund_code": code, "status": outcome, "detail": result.get("detail", ""),
                    "source": "AKShare / 东方财富定期披露", "before": before, "after": after,
                })
            except Exception as exc:
                # 失败本身不能变成证据。继续保留原结果及日期，且同一来源不会无限重试。
                attempts.append({
                    "action": "disclosure_refresh", "label": f"检查 {code} 最新持仓披露",
                    "fund_code": code, "status": "failed", "detail": str(exc)[:240],
                    "source": "AKShare / 东方财富定期披露",
                    "before": before, "after": self._gate_snapshot(gate),
                })

        remaining = self._remaining(gate, public_allowed=public_allowed, candidates=candidates)
        stopped = "cancelled" if budget.cancelled else (
            "budget_exhausted" if remaining and budget.used_rounds >= MAX_REPAIR_ROUNDS else
            "manual_refresh_required" if candidates and not public_allowed else
            "gaps_remain" if remaining else "complete"
        )
        return gate, {
            "version": "research-repair-v1", "public_refresh_authorized": budget.allow_public_refresh,
            "public_refresh_enabled": public_allowed, "max_rounds": MAX_REPAIR_ROUNDS,
            "used_rounds": budget.used_rounds, "stop_reason": stopped,
            "prior_used_rounds": prior_used_rounds, "prior_actions": prior_actions,
            "initial_gate": initial, "final_gate": self._gate_snapshot(gate),
            "attempts": attempts, "remaining_gaps": remaining, "warnings": diagnostics,
            "unattempted_fund_codes": [code for code in candidates if f"disclosure:{code}" not in budget.attempted],
        }

    @staticmethod
    def _needs_disclosure(data: dict) -> bool:
        return data.get("data_mode") == "unavailable" or data.get("freshness_status") in {"stale", "unavailable", None} or not (
            data.get("security_count") or data.get("industry_count")
            or data.get("security_disclosure_pct") or data.get("industry_asset_disclosure_pct")
        )

    @staticmethod
    def _gate_snapshot(gate: dict) -> dict:
        """保留检查前后状态与报告日期，不重复保存整份研究数据。"""

        return {
            "status": gate["status"], "coverage_score_pct": gate["coverage_score_pct"],
            "requirements": [{key: item.get(key) for key in ("key", "status", "as_of_date")} for item in gate["requirements"]],
        }

    @staticmethod
    def _remaining(gate: dict, *, public_allowed: bool, candidates: list[str]) -> list[dict]:
        items = []
        for gap in gate.get("missing_or_partial", []):
            key = gap["key"]
            if key == "investor_context":
                kind, action = "user_information", "请简短补问缺少的投资条件，不去公开源推测用户背景"
            elif key in {"portfolio_holdings", "scope_profile"}:
                kind, action = "user_information", "请用户确认研究对象和组合持仓"
            elif key in {"holdings_exposure", "lookthrough_exposure"}:
                kind = "public_data"
                action = ("可勾选本轮补查公开数据后再研究" if candidates and not public_allowed else
                          "保留披露日期与覆盖限制；已检查的来源不重复抓取，未检查的基金可另起一轮补查")
            else:
                kind, action = "public_data", "本轮没有对应补查适配器；可手动同步基金或市场数据后重新研究"
            items.append({**gap, "kind": kind, "next_action": action})
        items.extend({**item, "kind": "unsupported", "next_action": "当前数据能力未覆盖，保留限制，不通过重复抓取或模型猜测补齐"} for item in gate.get("asset_specific_gaps", []))
        return items


financial_research_repair_service = FinancialResearchRepairService()
