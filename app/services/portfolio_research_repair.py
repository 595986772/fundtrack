"""冻结前的有限补查。区分缺原文、缺公开事件、缺用户条件，并保留每次检查结果。"""

from copy import deepcopy
from datetime import date
from time import monotonic

from app.services.ai_governance_context import AIControlError
from app.services.eval_context import frozen_eval_data_enabled
from app.services.financial_research_repair import ResearchRepairBudget, MAX_REPAIR_ROUNDS
from app.services.fund_report_retrieval import check_retrieval_active, report_retrieval_scope
from app.services.job_runtime import JobCancelledError
from app.services.research_event_fetch import fetch_research_events


def evidence_gaps(reports, market, plan):
    gaps = []
    for row in reports.get("funds", []):
        code = row["fund_code"]
        if row["status"] == "missing_reports":
            gaps.append({"key": f"quarters:{code}", "kind": "missing_quarter", "fund_code": code,
                         "detail": "本地未保存两个不同季度的报告"})
        elif row["status"] != "available" or (plan["mode"] == "strategy" and row.get("missing_manager_document_ids")):
            gaps.append({"key": f"sections:{code}", "kind": "local_section", "fund_code": code,
                         "document_ids": row.get("document_ids", []), "detail": "当前摘录不足以覆盖所问章节"})
    if "market" in plan["sources"]:
        cutoff = market.get("event_data_cutoff")
        try:
            stale = (date.today() - date.fromisoformat(str(cutoff)[:10])).days > 7
        except (ValueError, TypeError):
            stale = True
        if not market.get("events") or stale:
            gaps.append({"key": "events", "kind": "public_events", "detail": "相关事件为空或保存窗口较旧"})
    gaps.extend({"key": f"personal:{index}", "kind": "user_information", "detail": question}
                for index, question in enumerate(plan.get("clarification_questions", [])))
    gaps.extend({"key": f"coverage:{code}", "kind": "coverage_limit", "fund_code": code,
                 "detail": "超出本轮季报覆盖数量，可单独研究该基金"} for code in reports.get("omitted_fund_codes", []))
    return gaps


class PortfolioResearchRepairService:
    def __init__(self, events=fetch_research_events):
        self.events = events

    def run(self, *, portfolio, market, plan, reports_service, compact_event, previous=None, checkpoint=None):
        """最多两次动作、总计四十秒。名额先持久化，失败和重投不重置已用预算。"""
        market = deepcopy(market)
        reports = market.get("fund_reports") or {}
        budget = ResearchRepairBudget(allow_public_refresh=plan["allow_public_refresh"],
            attempted=set((previous or {}).get("attempted", [])),
            used_rounds=int((previous or {}).get("used_rounds", 0)), checkpoint=checkpoint)
        allowed = budget.allow_public_refresh and not frozen_eval_data_enabled()
        initial = evidence_gaps(reports, market, plan)
        attempts = []
        deadline = monotonic() + 40
        # 本地章节优先，不用缺个人条件作上网理由，也不重复同一来源。
        for gap in sorted(initial, key=lambda row: row["kind"] != "local_section"):
            check_retrieval_active()
            if monotonic() >= deadline:
                break
            kind, code = gap["kind"], gap.get("fund_code")
            if kind in {"user_information", "coverage_limit"} or (kind != "local_section" and not allowed):
                continue
            if kind != "public_events" and reports_service is None:
                continue
            if not budget.reserve(gap["key"]):
                continue
            before = [(row["evidence_id"], row["quote"]) for row in reports.get("evidence", [])]
            action = {"action": gap["key"], "kind": kind, "status": "checked_no_change"}
            try:
                with report_retrieval_scope(deadline):
                    if kind in {"missing_quarter", "local_section"}:
                        if kind == "missing_quarter":
                            reports_service.reader.sync(code, build_index=False)
                        # 已有两期的章节补查坚持同一文档编号；缺季度同步完成后才允许选新版本。
                        ids = gap.get("document_ids", [])
                        repaired = reports_service.capture(portfolio, fund_codes=[code],
                            question=plan["question"] + " 基金经理投资策略、运作分析、风险及成立条件",
                            document_ids_by_fund={code: ids} if len(ids) == 2 else None)
                        old_evidence = [row for row in reports.get("evidence", []) if code in row.get("fund_codes", [])]
                        old_documents = {row["document_id"] for row in old_evidence if row.get("document_id")}
                        new_documents = {row["document_id"] for row in repaired.get("evidence", []) if row.get("document_id")}
                        # 补查可以换更相关的摘录，但不能把原来的两季证据降为一季。
                        # 章节补查还必须保留已有的经理观点覆盖，避免修一个缺口引入另一个。
                        old_manager = {row["document_id"] for row in old_evidence
                            if row.get("document_id") and row.get("section") == "manager_review"}
                        new_manager = {row["document_id"] for row in repaired.get("evidence", [])
                            if row.get("document_id") and row.get("section") == "manager_review"}
                        coverage_preserved = (old_documents <= new_documents and old_manager <= new_manager
                            if kind == "local_section" else len(new_documents) >= len(old_documents))
                        if repaired.get("evidence") and coverage_preserved:
                            reports["retrieval_warnings"] = list(dict.fromkeys([
                                *reports.get("retrieval_warnings", []), *repaired.get("retrieval_warnings", [])]))
                            for key in ("funds", "documents", "evidence"):
                                reports[key] = [row for row in reports.get(key, [])
                                    if row.get("fund_code", (row.get("fund_codes") or [None])[0]) != code] + repaired[key]
                            action["status"] = "evidence_changed" if before != [
                                (row["evidence_id"], row["quote"]) for row in reports["evidence"]] else "checked_no_change"
                        elif repaired.get("evidence"):
                            action["reason"] = "coverage_regression_prevented"
                    else:
                        codes = [row["fund_code"] for row in sorted(portfolio["holdings"],
                            key=lambda row: -float(row.get("weight_pct") or 0))[:3]]
                        batch = self.events(codes, timeout_seconds=min(25, max(.01, deadline - monotonic())))
                        check_retrieval_active()
                        relevant = [row for row in batch.get("events", []) if not row.get("affected_fund_codes")
                            or set(row["affected_fund_codes"]).intersection(codes)]
                        extra = [compact_event(row) for row in relevant[:8] if row.get("event_id")]
                        if extra:
                            # 已冻结快照不存在于此阶段；保留旧来源，按稳定 ID 合并新增证据。
                            combined = {row["event_id"]: row for row in market.get("events", [])}
                            old = deepcopy(combined)
                            combined.update({row["event_id"]: row for row in extra if row.get("event_id")})
                            market["events"] = list(combined.values())[-12:]
                            market["relevant_event_count"] = len(market["events"])
                            market["event_data_cutoff"] = max(str(row.get("event_date") or "") for row in market["events"]) or None
                            market["event_checked_at"] = date.today().isoformat()
                            action["status"] = "evidence_changed" if combined != old else "checked_no_change"
                        action["sources"] = batch.get("successful_sources", [])
            except (AIControlError, JobCancelledError):
                raise
            except Exception as exc:
                check_retrieval_active()
                action.update(status="failed", reason=type(exc).__name__)
            attempts.append(action)
        remaining = evidence_gaps(reports, market, plan)
        # 用最终重新检查的覆盖状况更新显示，不保留“已补齐但仍称缺失”的旧提示。
        if reports:
            incomplete = any(row["kind"] != "user_information" and row.get("fund_code") for row in remaining)
            reports["status"] = "partial" if incomplete and reports.get("evidence") else "available" if reports.get("evidence") else "unavailable"
            reports["warnings"] = list(dict.fromkeys([*reports.get("retrieval_warnings", []),
                *(row["detail"] + (f"（{row['fund_code']}）" if row.get("fund_code") else "")
                  for row in remaining if row.get("fund_code"))]))
            market["fund_reports"] = reports
        trace = {"version": "portfolio-repair-v1", "public_refresh_authorized": allowed,
            "max_rounds": MAX_REPAIR_ROUNDS, "used_rounds": budget.used_rounds,
            "prior_actions": sorted((previous or {}).get("attempted", [])), "attempts": attempts,
            "initial_gaps": initial, "remaining_gaps": remaining,
            "stop_reason": "complete" if not remaining else "time_budget_exhausted" if monotonic() >= deadline
                else "budget_exhausted" if budget.used_rounds >= MAX_REPAIR_ROUNDS
                else "manual_refresh_required" if not allowed and any(row["kind"] in {"missing_quarter", "public_events"} for row in remaining)
                else "gaps_remain"}
        market["research_repair"] = trace
        return market


portfolio_research_repair_service = PortfolioResearchRepairService()
