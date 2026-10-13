"""Agent Context Compact 服务。

s08 Context Compact 要学习的重点是：
- Agent Loop 里 messages 会越来越长；
- 特别是 read_file_tool、load_skill_tool、rag_search_tool 这类工具，结果可能很大；
- 后端可以把过长的 ToolMessage 压缩成占位摘要，再把完整内容保存到本地文件。

当前是教学版，只做“单次 /agent/chat 请求内”的压缩，不做跨会话 memory。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
import json
from pathlib import Path
import re
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4


PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONTEXT_STORE_DIR = PROJECT_ROOT / "data" / "agent_context"


@dataclass(frozen=True)
class CompactTrace:
    """一次 Context Compact 的调试记录。"""

    event: str
    round_index: int
    before_chars: int
    after_chars: int
    compacted_messages: int
    reason: str
    saved_paths: list[str] = field(default_factory=list)
    details: list[dict[str, Any]] = field(default_factory=list)


class ContextCompactorService:
    """负责统计 messages 长度，并压缩过长的 ToolMessage。"""

    def compact_messages(
        self,
        messages: list[Any],
        *,
        round_index: int,
        max_total_chars: int = 16_000,
        max_tool_message_chars: int = 4_000,
        min_compact_chars: int = 1_500,
        preserve_recent_tool_messages: int = 1,
        force: bool = False,
    ) -> list[CompactTrace]:
        """必要时压缩 messages 里的 ToolMessage。

        messages 会原地修改，因为 Agent Loop 下一轮要直接使用压缩后的上下文。
        """

        before_chars = self.estimate_messages_chars(messages)
        tool_indexes = [
            index
            for index, message in enumerate(messages)
            if self._is_tool_message(message)
        ]

        if not tool_indexes:
            return []

        recent_tool_indexes = (
            set(tool_indexes[-preserve_recent_tool_messages:])
            if preserve_recent_tool_messages > 0
            else set()
        )
        saved_paths: list[str] = []
        details: list[dict[str, Any]] = []

        for message_index in tool_indexes:
            message = messages[message_index]
            content = self._message_content_to_text(message)

            if self._is_already_compacted(content):
                continue

            content_chars = len(content)
            is_recent_tool = message_index in recent_tool_indexes

            should_compact_for_size = content_chars > max_tool_message_chars
            should_compact_for_budget = (
                before_chars > max_total_chars and content_chars > min_compact_chars
            )
            should_compact_for_recovery = force and content_chars > min_compact_chars

            # 最近一个工具结果默认保留完整；但如果它本身已经太长，
            # 仍然要压缩，否则下一轮模型调用还是会被大结果撑大。
            if is_recent_tool and not should_compact_for_size:
                continue

            if (
                not should_compact_for_size
                and not should_compact_for_budget
                and not should_compact_for_recovery
            ):
                continue

            if should_compact_for_recovery:
                reason = "context_overflow_recovery"
            elif should_compact_for_size:
                reason = "tool_result_too_long"
            else:
                reason = "messages_over_budget"
            saved_path = self._save_original_tool_result(
                round_index=round_index,
                message_index=message_index,
                message=message,
                content=content,
                reason=reason,
            )
            compacted_content = self._build_compacted_content(
                original_content=content,
                saved_path=saved_path,
                reason=reason,
                original_chars=content_chars,
                message=message,
            )
            self._replace_message_content(
                messages=messages,
                message_index=message_index,
                compacted_content=compacted_content,
            )

            saved_paths.append(saved_path)
            details.append(
                {
                    "message_index": message_index,
                    "tool_call_id": str(getattr(message, "tool_call_id", "")),
                    "original_chars": content_chars,
                    "compacted_chars": len(compacted_content),
                    "reason": reason,
                    "saved_path": saved_path,
                }
            )

        if not details:
            return []

        after_chars = self.estimate_messages_chars(messages)

        return [
            CompactTrace(
                event="ContextCompact",
                round_index=round_index,
                before_chars=before_chars,
                after_chars=after_chars,
                compacted_messages=len(details),
                reason="compacted long tool message(s)",
                saved_paths=saved_paths,
                details=details,
            )
        ]

    def estimate_messages_chars(self, messages: list[Any]) -> int:
        """粗略估算当前 messages 中正文占用的字符数。

        这里先用字符数而不是 tokenizer，是为了让学习 demo 更直观。
        """

        return sum(len(self._message_content_to_text(message)) for message in messages)

    def _is_tool_message(self, message: Any) -> bool:
        """判断一个 LangChain message 是否是 ToolMessage。"""

        return (
            getattr(message, "type", "") == "tool"
            or message.__class__.__name__ == "ToolMessage"
        )

    def _message_content_to_text(self, message: Any) -> str:
        """把 LangChain message 的 content 统一转成字符串。"""

        content = getattr(message, "content", "")

        if isinstance(content, str):
            return content

        return json.dumps(content, ensure_ascii=False)

    def _is_already_compacted(self, content: str) -> bool:
        """避免同一个 ToolMessage 被重复压缩。"""

        return '"context_compacted": true' in content

    def _save_original_tool_result(
        self,
        *,
        round_index: int,
        message_index: int,
        message: Any,
        content: str,
        reason: str,
    ) -> str:
        """把完整工具结果保存到 data/agent_context/。"""

        CONTEXT_STORE_DIR.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        filename = f"round-{round_index}-message-{message_index}-{timestamp}-{uuid4().hex[:8]}.json"
        target_path = CONTEXT_STORE_DIR / filename

        payload = {
            "round_index": round_index,
            "message_index": message_index,
            "tool_call_id": str(getattr(message, "tool_call_id", "")),
            "reason": reason,
            "original_chars": len(content),
            "content": content,
        }
        target_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        return target_path.relative_to(PROJECT_ROOT).as_posix()

    def _build_compacted_content(
        self,
        *,
        original_content: str,
        saved_path: str,
        reason: str,
        original_chars: int,
        message: Any,
    ) -> str:
        """生成放回 ToolMessage 的压缩占位内容。"""

        # 金融研究 Brief 本来就是结构化对象。对它取前 900 个字符会只留下研究计划，
        # 而把真正的风险指标和证据编号截掉。这里使用字段级压缩：完整结果仍保存到
        # saved_path，模型上下文只保留结论所需的确定性摘要。
        try:
            payload = json.loads(original_content)
        except (json.JSONDecodeError, TypeError):
            payload = None
        if isinstance(payload, dict) and payload.get("ok") is False:
            # 压缩也必须保留失败身份及后端处理建议，不能让默认占位的 ok:true
            # 把失败变成“已取得证据”。完整错误仍可从原审计记录查看。
            compacted = {key: payload[key] for key in (
                "ok", "error_code", "retryable", "suggested_action", "field_errors",
                "fund_code", "portfolio_id", "permission",
            ) if key in payload}
            compacted.update(error=str(payload.get("error") or payload.get("reason") or "工具未完成")[:500],
                             context_compacted=True, saved_path=saved_path)
            return json.dumps(compacted, ensure_ascii=False)
        if (
            isinstance(payload, dict)
            and payload.get("data_kind")
            == "deterministic_financial_research_brief"
        ):
            return self._compact_financial_research_brief(
                payload=payload,
                saved_path=saved_path,
                reason=reason,
                original_chars=original_chars,
                message=message,
            )

        if isinstance(payload, dict) and payload.get("data_kind") == "fund_report_research":
            return self._compact_fund_report_research(payload, saved_path=saved_path)

        if isinstance(payload, dict) and payload.get("data_kind") == "portfolio_research_job":
            return self._compact_portfolio_research_job(payload, saved_path=saved_path)

        if isinstance(payload, dict) and payload.get("data_kind") == "portfolio_allocation":
            # 确认状态、约束、冲突和事实不能被截断丢失；工具已删去图表逐点数据。
            compacted = {key: payload[key] for key in (
                "ok", "data_kind", "draft_id", "portfolio_id", "status", "proposal", "result", "limitations",
            ) if key in payload}
            compacted.update(context_compacted=True, saved_path=saved_path)
            return json.dumps(compacted, ensure_ascii=False)

        if isinstance(payload, dict) and payload.get("data_kind") == "portfolio_stress":
            # 压力报告不能截成字符串开头，否则会丢候选权重、同情景编号或收益贡献。
            # 上游已经移除曲线；这里再移除来源明细，保留可解释的事实和所有基金权重。
            compacted = {key: payload[key] for key in (
                "ok", "data_kind", "run_id", "root_run_id", "portfolio_id", "months",
                "window", "holdings", "scenarios", "facts", "limitations",
            ) if key in payload}
            compacted.update(context_compacted=True, saved_path=saved_path,
                             compact_strategy="portfolio_stress_fields_v1")
            return json.dumps(compacted, ensure_ascii=False)

        preview = original_content[:900]
        tail_preview = original_content[-300:] if original_chars > 1_200 else ""

        payload = {
            "ok": True,
            "context_compacted": True,
            "message": "这个工具结果太长，已被 Context Compact 压缩。完整内容已保存到本地文件。",
            "reason": reason,
            "tool_call_id": str(getattr(message, "tool_call_id", "")),
            "original_chars": original_chars,
            "saved_path": saved_path,
            "preview": preview,
            "tail_preview": tail_preview,
        }

        return json.dumps(payload, ensure_ascii=False)

    def _compact_portfolio_research_job(self, payload: dict, *, saved_path: str) -> str:
        """保留原报告的观点与引用关系，不用头尾预览代替质询证据。"""

        compacted = {key: payload[key] for key in (
            "ok", "data_kind", "action", "accepted", "message", "error", "portfolio_id",
            "portfolio_name", "period_months", "events_endpoint",
        ) if key in payload and payload[key] is not None}
        job = payload.get("job") or {}
        compacted["job"] = {key: job[key] for key in (
            "id", "job_type", "title", "status", "current_step", "total_steps",
            "progress_percent", "progress_message", "error", "completed_at",
        ) if key in job and job[key] is not None}
        report = payload.get("report")
        if isinstance(report, dict):
            report = {key: report[key] for key in (
                "data_kind", "run_id", "portfolio_id", "portfolio_name", "period_months",
                "assessment_date", "research_mode", "research_plan", "headline", "summary", "research_snapshot",
                "assessment_snapshot", "quant_risk_snapshot", "performance_snapshot",
                "market_snapshot", "decision_history", "generation", "key_findings",
                "model_interpretation", "key_evidence", "conflicts", "uncertainties",
                "limitations", "disclaimer", "committee",
            ) if key in report}
            if "key_findings" in report:
                report["key_findings"] = [
                    self._compact_committee_finding(item) for item in report["key_findings"]
                ]
            committee = report.get("committee")
            if isinstance(committee, dict):
                result = {key: committee[key] for key in (
                    "version", "status", "snapshot_id", "warnings", "uncertainties",
                ) if key in committee}
                # 上游限制两位研究员、每人至多三条观点/回应。软阈值不能截掉第三条
                # 或去掉句尾条件；保留原 claim_id 与全部引用，不重新读取后台数据。
                for group in ("roles", "reviews"):
                    result[group] = []
                    for stage in committee.get(group, []):
                        row = {key: stage[key] for key in (
                            "role", "label", "status", "headline", "summary",
                            "uncertainties", "failure_reason",
                        ) if key in stage and stage[key] is not None}
                        if "key_findings" in stage:
                            row["key_findings"] = [
                                self._compact_committee_finding(item) for item in stage["key_findings"]
                            ]
                        if "responses" in stage:
                            row["responses"] = [{
                                "target_claim_id": item["target_claim_id"],
                                "verdict": item["verdict"],
                                "finding": self._compact_committee_finding(item["finding"]),
                            } for item in stage["responses"]]
                        result[group].append(row)
                if isinstance(committee.get("synthesis"), dict):
                    result["synthesis"] = {key: committee["synthesis"][key] for key in (
                        "role", "label", "status", "failure_reason", "uncertainties",
                    ) if key in committee["synthesis"]}
                report["committee"] = result
        compacted["report"] = report
        compacted.update(context_compacted=True, saved_path=saved_path,
                         compact_strategy="portfolio_research_fields_v1")
        return json.dumps(compacted, ensure_ascii=False)

    def _compact_committee_finding(self, item: dict) -> dict:
        result = {key: item[key] for key in (
            "claim_id", "tone", "title", "detail", "fund_codes", "evidence_ids", "metric_keys",
        ) if key in item}
        for field in ("evidence_references", "metric_references"):
            if field not in item:
                continue
            result[field] = []
            for reference in item[field]:
                row = {key: reference[key] for key in (
                    "evidence_id", "key", "kind", "direction", "title", "summary", "explanation",
                    "label", "value", "unit", "value_text", "data_date", "event_date", "report_date",
                    "period_start", "period_end", "scope", "fund_code", "fund_codes",
                    "source_name", "source_url", "source_urls", "warnings", "limitations",
                    "document_id", "quote", "page", "section", "period", "content_hash", "published_at",
                ) if key in reference}
                # 只移除数值完全相同的展示副本，保留其单位；带口径说明或舍入差异的
                # value_text 不是冗余数据，不能为缩短上下文而丢掉。
                if field == "metric_references" and type(row.get("value")) in {int, float}:
                    display = row.get("value_text")
                    match = (re.fullmatch(r"([+-]?\d+(?:\.\d+)?)\s*(%|分|条|个百分点)?", display.strip())
                             if isinstance(display, str) else None)
                    value = Decimal(str(row["value"]))
                    if match and value.is_finite() and value == Decimal(match[1]):
                        unit = match[2] or ""
                        if row.get("unit", unit) == unit:
                            if unit:
                                row["unit"] = unit
                            row.pop("value_text")
                if "source_url" in row and not self._is_research_source_url(row["source_url"]):
                    row.pop("source_url")
                if "source_urls" in row:
                    row["source_urls"] = [url for url in row["source_urls"] or []
                                          if self._is_research_source_url(url)]
                result[field].append(row)
        return result

    @staticmethod
    def _is_research_source_url(value: Any) -> bool:
        """只保留原文中可用的 HTTP(S) 来源，不猜链接、不联网验证。"""

        if not isinstance(value, str) or any(
            character.isspace() or ord(character) < 32 or ord(character) == 127 for character in value
        ):
            return False
        try:
            parsed = urlsplit(value)
            return bool(parsed.scheme in {"http", "https"} and parsed.hostname
                        and parsed.username is None and parsed.password is None
                        and (parsed.port is None or parsed.port > 0))
        except ValueError:
            return False

    def _compact_fund_report_research(self, payload: dict, *, saved_path: str) -> str:
        """对季报做字段级压缩：保留日期、表格状态及原文，不二次截断经理句子。"""
        from app.services.fund_report_source import manager_section_coverage
        from app.services.fund_report_sentences import CONTRACT, number_report_evidence, restore_evidence

        numbered = payload.get("citation_contract") == CONTRACT
        if numbered:
            # 错误恢复可能再次压缩已经编号的消息。先无损恢复做原有预算计算，
            # 选完仍保留的整段再编号；不能把被删除的旧编号残留给后续模型。
            payload = {**payload, "evidence": [restored for row in payload.get("evidence", [])
                if (restored := restore_evidence(row)) is not None]}

        compacted = {key: payload.get(key) for key in (
            "ok", "data_kind", "fund_code", "allocation_changes", "allocation_tables", "retrieval", "warnings", "limitations", "query_profile")}
        if payload.get("retrieval_diagnostics"):
            # 诊断不等于证据；保留状态和用量以解释降级，但无需重复整份候选排名。
            compacted["retrieval_diagnostics"] = {key: payload["retrieval_diagnostics"].get(key)
                for key in ("version", "query_mode", "rerank", "candidate_count")}
        compacted["documents"] = [{key: doc.get(key) for key in (
            "id", "fund_code", "period", "title", "period_end", "data_as_of", "data_as_of_basis", "published_at", "source_url", "page_count")}
            for doc in payload.get("documents", [])[:2]]
        evidence, sections = [], []
        for doc in compacted["documents"]:
            rows = [row for row in payload.get("evidence", []) if row.get("document_id") == doc["id"]]
            manager = [row for row in rows if row.get("section") == "manager_review"]
            table = [row for row in rows if row.get("section") == "allocation_table"]
            other = [row for row in rows if row.get("section") not in ("manager_review", "allocation_table")][:2]
            # 主项原文单独预留2400字，足够八行各240字与350字表头，不挤掉经理章节。
            # 行状态和原始值也保留在allocation_tables，未找到的行不能伪造页码或引用。
            table_budget = 2400
            for row in table:
                if len(row.get("text", "")) <= table_budget:
                    evidence.append({**row, "excerpt": True})
                    table_budget -= len(row.get("text", ""))
            # 仍沿用原先每期最多4200字正文的预算。经理章节最多六段、每段700字；
            # 普通召回使用剩余额度。摘录要么整段保留，要么舍弃，不能悄悄裁掉末尾的否定词。
            remaining, chosen = 4200, []
            for row in {row["evidence_id"]: row for row in manager + other}.values():
                if len(row.get("text", "")) <= remaining:
                    chosen.append({**row, "excerpt": True})
                    remaining -= len(row.get("text", ""))
            evidence.extend(chosen)
            coverage = manager_section_coverage(doc, chosen)
            if (payload.get("query_profile") or {}).get("mode") == "allocation":
                coverage.update(status="not_requested", included_chunks=0, total_chunks=0)
            if manager and coverage["status"] == "not_found":
                # 极端长的旧格式结果可能一段也装不下，不能冒充解析器未找到章节。
                coverage.update(status="partial", total_chunks=len(manager))
            sections.append(coverage)
        if payload.get("query_profile"):
            # 审计记录最终送入模型的字数，而不是筛选后又被压缩掉的原始长度。
            compacted["query_profile"] = {**payload["query_profile"],
                "selected_evidence_chars": sum(len(row.get("text", "")) for row in evidence)}
        compacted.update(evidence=evidence, evidence_ids=[row["evidence_id"] for row in evidence],
            manager_sections=sections, context_compacted=True, saved_path=saved_path,
            compact_strategy="fund_report_sections_v3",
            coverage_note="manager_sections仅描述已识别4.4经理章节的覆盖。partial/not_found不能用于断言前期没有某观点；"
                          "complete也不代表读完PDF。allocation_tables是八类主项的行状态及原文，不是整个资产表；"
                          "quote_truncated表示过长异常单元格只保留部分。普通召回是页内摘录，未召回不等于未披露。")
        return json.dumps(number_report_evidence(compacted) if numbered else compacted, ensure_ascii=False)

    def _compact_financial_research_brief(
        self,
        *,
        payload: dict[str, Any],
        saved_path: str,
        reason: str,
        original_chars: int,
        message: Any,
    ) -> str:
        """压缩综合研究结果，同时保留模型回答所需的金融事实。"""

        plan = payload.get("research_plan") or {}
        gate = payload.get("evidence_gate") or {}
        summary = payload.get("evidence_summary") or {}
        scope = payload.get("scope") or {}
        scope_kind = scope.get("kind")

        compacted = {
            "ok": payload.get("ok", True),
            "data_kind": payload.get("data_kind"),
            "brief_version": payload.get("brief_version"),
            "context_compacted": True,
            "compact_strategy": "financial_research_fields_v1",
            "reason": reason,
            "tool_call_id": str(getattr(message, "tool_call_id", "")),
            "original_chars": original_chars,
            "saved_path": saved_path,
            "scope": scope,
            "period_months": payload.get("period_months"),
            "research_plan": {
                "intent": plan.get("intent"),
                "intent_label": plan.get("intent_label"),
                "asset_classes": plan.get("asset_classes") or [],
                "asset_specific_checks": plan.get("asset_specific_checks") or [],
            },
            "evidence_gate": {
                key: gate.get(key)
                for key in (
                    "status",
                    "status_label",
                    "coverage_score_pct",
                    "score_meaning",
                    "conclusion_policy",
                    "failed_task_count",
                    "critical_missing_keys",
                    "asset_specific_gaps",
                    "missing_or_partial",
                    "clarification_questions",
                )
            },
            "execution": self._compact_research_execution(
                payload.get("execution") or {}
            ),
            # 补查轨迹本身最多两轮，必须保留授权、日期、失败原因和剩余缺口，
            # 否则模型只看到最后的覆盖分数，可能误说“所有数据已经更新”。
            "evidence_repair": payload.get("evidence_repair") or {},
            # 最多 19 条核心事实。编号和值必须一起保留，最终回答才能逐字段核对。
            "answer_facts": payload.get("answer_facts") or [],
            "evidence_summary": (
                self._compact_fund_brief_summary(summary)
                if scope_kind == "fund"
                else self._compact_portfolio_brief_summary(summary)
            ),
            "answer_contract": payload.get("answer_contract") or [],
            "disclaimer": payload.get("disclaimer"),
        }
        return json.dumps(compacted, ensure_ascii=False)

    @staticmethod
    def _compact_fund_brief_summary(summary: dict[str, Any]) -> dict[str, Any]:
        """保留模型解释单基金所需的数值，不重复携带展示页字段。"""

        profile = summary.get("profile") or {}
        historical = summary.get("historical_risk") or {}
        risk_adjusted = historical.get("risk_adjusted") or {}
        benchmark = historical.get("benchmark") or {}
        assessment = summary.get("rule_assessment") or {}
        score = assessment.get("score_breakdown") or {}
        disclosure = summary.get("disclosure") or {}
        return {
            "profile": ContextCompactorService._select_non_empty(
                profile,
                (
                    "fund_code",
                    "short_name",
                    "fund_type",
                    "sample_role",
                    "management_company",
                    "fund_managers",
                    "benchmark",
                    "tracking_target",
                ),
            ),
            "historical_risk": ContextCompactorService._without_empty(
                {
                    **ContextCompactorService._select_non_empty(
                        historical,
                        (
                            "period_start",
                            "period_end",
                            "observation_count",
                            "period_return_pct",
                            "annualized_volatility_pct",
                            "downside_volatility_pct",
                            "current_drawdown_pct",
                            "worst_daily_return_pct",
                            "longest_drawdown_days",
                            "return_basis_label",
                            "return_basis_status",
                        ),
                    ),
                    "max_drawdown": ContextCompactorService._select_non_empty(
                        historical.get("max_drawdown") or {},
                        (
                            "drawdown_pct",
                            "peak_date",
                            "trough_date",
                            "recovery_date",
                            "recovery_days",
                        ),
                    ),
                    "risk_adjusted": ContextCompactorService._select_non_empty(
                        risk_adjusted,
                        (
                            "annualized_return_pct",
                            "risk_free_rate_annual_pct",
                            "sharpe_ratio",
                            "sortino_ratio",
                        ),
                    ),
                    "benchmark": ContextCompactorService._without_empty(
                        {
                            **ContextCompactorService._select_non_empty(
                                benchmark,
                                (
                                    "benchmark_name",
                                    "status",
                                    "period_return_pct",
                                    "fund_aligned_return_pct",
                                    "excess_return_pct",
                                    "coverage_pct",
                                ),
                            ),
                            "warnings": list(benchmark.get("warnings") or [])[:2],
                        }
                    ),
                    "data_quality_warnings": list(
                        historical.get("data_quality_warnings") or []
                    )[:2],
                }
            ),
            "rule_assessment": ContextCompactorService._without_empty({
                "state_code": assessment.get("state_code"),
                "state_label": assessment.get("state_label"),
                "summary": str(assessment.get("summary") or "")[:240],
                "score_breakdown": ContextCompactorService._select_non_empty(
                    score,
                    (
                        "supportive_points",
                        "caution_points",
                        "net_points",
                        "neutral_evidence_count",
                        "expired_event_count",
                    ),
                ),
                "component_dates": assessment.get("component_dates"),
                "top_risk_evidence": ContextCompactorService._short_evidence(
                    assessment.get("top_risk_evidence"),
                    limit=2,
                ),
                "top_supporting_evidence": ContextCompactorService._short_evidence(
                    assessment.get("top_supporting_evidence"),
                    limit=1,
                ),
                "active_event_count": assessment.get("active_event_count"),
            }),
            "disclosure": ContextCompactorService._without_empty({
                **ContextCompactorService._select_non_empty(
                    disclosure,
                    (
                        "latest_report_date",
                        "freshness_status",
                        "security_count",
                        "industry_count",
                        "disclosed_stock_weight_pct",
                        "disclosed_bond_weight_pct",
                        "industry_coverage_pct",
                    ),
                ),
                "warnings": list(disclosure.get("warnings") or [])[:2],
                "top_securities": [
                    ContextCompactorService._compact_disclosure_item(item)
                    for item in list(disclosure.get("top_securities") or [])[:3]
                ],
                "top_industries": [
                    ContextCompactorService._compact_disclosure_item(item)
                    for item in list(disclosure.get("top_industries") or [])[:2]
                ],
            }),
        }

    @staticmethod
    def _compact_portfolio_brief_summary(
        summary: dict[str, Any],
    ) -> dict[str, Any]:
        assessment = summary.get("rule_assessment") or {}
        score = assessment.get("score_breakdown") or {}
        quant = summary.get("quantitative_risk") or {}
        performance = summary.get("performance") or {}
        lookthrough = summary.get("lookthrough") or {}
        event_impacts = summary.get("event_impacts") or {}
        return {
            "holdings": [
                ContextCompactorService._select_non_empty(
                    item,
                    (
                        "fund_code",
                        "short_name",
                        "fund_type",
                        "sample_role",
                        "weight_pct",
                    ),
                )
                for item in list(summary.get("holdings") or [])[:6]
            ],
            "rule_assessment": ContextCompactorService._without_empty({
                "state_code": assessment.get("state_code"),
                "state_label": assessment.get("state_label"),
                "summary": str(assessment.get("summary") or "")[:220],
                "concentration": ContextCompactorService._select_non_empty(
                    assessment.get("concentration") or {},
                    (
                        "hhi",
                        "effective_fund_count",
                        "max_weight_pct",
                        "max_weight_fund_code",
                        "state_code",
                        "state_label",
                    ),
                ),
                "score_breakdown": ContextCompactorService._select_non_empty(
                    score,
                    ("supportive_points", "caution_points", "net_points"),
                ),
                "top_risk_evidence": ContextCompactorService._short_evidence(
                    assessment.get("top_risk_evidence"),
                    limit=2,
                ),
                "top_supporting_evidence": ContextCompactorService._short_evidence(
                    assessment.get("top_supporting_evidence"),
                    limit=1,
                ),
            }),
            "quantitative_risk": ContextCompactorService._without_empty({
                **ContextCompactorService._select_non_empty(
                    quant,
                    (
                        "period_start",
                        "period_end",
                        "observation_count",
                        "period_return_pct",
                        "annualized_volatility_pct",
                        "daily_cvar_95_pct",
                        "max_drawdown",
                        "return_basis_label",
                    ),
                ),
                "top_risk_contributor": ContextCompactorService._select_non_empty(
                    quant.get("top_risk_contributor") or {},
                    (
                        "fund_code",
                        "short_name",
                        "weight_pct",
                        "risk_contribution_pct",
                    ),
                ),
                "data_quality_warnings": list(
                    quant.get("data_quality_warnings") or []
                )[:2],
            }),
            "performance": ContextCompactorService._compact_performance(performance),
            "lookthrough": ContextCompactorService._without_empty({
                "summary": str(lookthrough.get("summary") or "")[:220],
                "oldest_report_date": lookthrough.get("oldest_report_date"),
                "latest_report_date": lookthrough.get("latest_report_date"),
                "data_quality": ContextCompactorService._select_non_empty(
                    lookthrough.get("data_quality") or {},
                    (
                        "status",
                        "confidence",
                        "security_coverage_pct",
                        "industry_asset_coverage_pct",
                        "unpenetrated_security_pct",
                        "freshness_status",
                        "warnings",
                    ),
                ),
                "concentration": ContextCompactorService._select_non_empty(
                    lookthrough.get("concentration") or {},
                    (
                        "known_security_count",
                        "top_security_exposure_pct",
                        "top_security_name",
                        "security_state",
                        "top_industry_exposure_pct",
                        "top_industry_name",
                        "industry_state",
                        "overall_state",
                    ),
                ),
                "top_securities": [
                    ContextCompactorService._compact_exposure_item(item)
                    for item in list(lookthrough.get("top_securities") or [])[:3]
                ],
                "top_industries": [
                    ContextCompactorService._compact_exposure_item(item)
                    for item in list(lookthrough.get("top_industries") or [])[:2]
                ],
            }),
            "event_impacts": ContextCompactorService._without_empty({
                "summary": str(event_impacts.get("summary") or "")[:220],
                "event_data_cutoff": event_impacts.get("event_data_cutoff"),
                "latest_holding_report_date": event_impacts.get(
                    "latest_holding_report_date"
                ),
                "quality": ContextCompactorService._select_non_empty(
                    event_impacts.get("quality") or {},
                    ("status", "confidence", "warnings"),
                ),
                "top_impacts": [
                    ContextCompactorService._compact_event_impact(item)
                    for item in list(event_impacts.get("top_impacts") or [])[:2]
                ],
            }),
        }

    @staticmethod
    def _short_evidence(items: Any, *, limit: int) -> list[dict[str, Any]]:
        """证据只保留引用、方向、数值和一句解释。"""

        return [
            ContextCompactorService._without_empty({
                key: item.get(key)
                for key in (
                    "evidence_id",
                    "kind",
                    "direction",
                    "title",
                    "value_text",
                    "data_date",
                    "portfolio_weight_coverage_pct",
                )
            }
            | {"explanation": str(item.get("explanation") or "")[:100]}
            )
            for item in list(items or [])[:limit]
        ]

    @staticmethod
    def _compact_research_execution(execution: dict[str, Any]) -> dict[str, Any]:
        """执行耗时保留在完整审计结果，模型只需要知道哪一路成功或失败。"""

        return ContextCompactorService._without_empty({
            **ContextCompactorService._select_non_empty(
                execution,
                ("mode", "task_count", "completed_count", "failed_count"),
            ),
            "tasks": [
                ContextCompactorService._without_empty(
                    {
                        "name": item.get("name"),
                        "status": item.get("status"),
                        "error": item.get("error"),
                    }
                )
                for item in list(execution.get("tasks") or [])
            ],
        })

    @staticmethod
    def _compact_disclosure_item(item: dict[str, Any]) -> dict[str, Any]:
        return ContextCompactorService._select_non_empty(
            item,
            (
                "evidence_id",
                "asset_type",
                "security_code",
                "security_name",
                "industry_name",
                "weight_pct",
                "report_date",
            ),
        )

    @staticmethod
    def _compact_exposure_item(item: dict[str, Any]) -> dict[str, Any]:
        return ContextCompactorService._select_non_empty(
            item,
            (
                "evidence_id",
                "security_code",
                "security_name",
                "industry_name",
                "portfolio_exposure_pct",
                "source_fund_count",
                "report_date",
            ),
        )

    @staticmethod
    def _compact_event_impact(item: dict[str, Any]) -> dict[str, Any]:
        return ContextCompactorService._without_empty({
            **ContextCompactorService._select_non_empty(
                item,
                (
                    "impact_evidence_id",
                    "event_id",
                    "title",
                    "event_date",
                    "direction",
                    "affected_exposure_pct",
                    "match_label",
                    "source_name",
                ),
            ),
            "reason": str(item.get("reason") or "")[:120],
            "uncertainties": [
                str(value)[:100]
                for value in list(item.get("uncertainties") or [])[:2]
            ],
        })

    @staticmethod
    def _compact_performance(performance: dict[str, Any]) -> dict[str, Any]:
        benchmark = performance.get("benchmark") or {}
        return ContextCompactorService._without_empty({
            **ContextCompactorService._select_non_empty(
                performance,
                (
                    "data_kind",
                    "performance_label",
                    "position_mode",
                    "period_start",
                    "period_end",
                    "twr_pct",
                    "xirr_pct",
                    "annualized_return_pct",
                    "annualized_volatility_pct",
                    "sharpe_ratio",
                    "sortino_ratio",
                    "max_drawdown",
                ),
            ),
            "benchmark": ContextCompactorService._select_non_empty(
                benchmark,
                (
                    "benchmark_name",
                    "status",
                    "period_return_pct",
                    "portfolio_aligned_return_pct",
                    "excess_return_pct",
                    "coverage_pct",
                ),
            ),
            "warnings": list(performance.get("warnings") or [])[:2],
        })

    @staticmethod
    def _select_non_empty(
        payload: dict[str, Any],
        keys: tuple[str, ...],
    ) -> dict[str, Any]:
        """从展示对象中挑模型真正需要的字段，并删除空占位。"""

        return ContextCompactorService._without_empty(
            {key: payload.get(key) for key in keys}
        )

    @staticmethod
    def _without_empty(payload: dict[str, Any]) -> dict[str, Any]:
        """保留 0 和 False 等有效值，只删除没有信息量的空字段。"""

        return {
            key: value
            for key, value in payload.items()
            if value is not None and value != "" and value != [] and value != {}
        }

    def _replace_message_content(
        self,
        *,
        messages: list[Any],
        message_index: int,
        compacted_content: str,
    ) -> None:
        """替换 message content，同时保留原来的 message 类型和 tool_call_id。"""

        message = messages[message_index]

        try:
            message.content = compacted_content
            return
        except Exception:
            pass

        if hasattr(message, "model_copy"):
            messages[message_index] = message.model_copy(
                update={"content": compacted_content}
            )
            return

        if hasattr(message, "copy"):
            messages[message_index] = message.copy(update={"content": compacted_content})
            return

        raise TypeError("无法替换 ToolMessage content，当前 message 类型不支持复制。")


context_compactor_service = ContextCompactorService()
