"""把持仓相关季报整理成组合工作流可冻结、可引用的一份证据。

不是另一个聊天 Agent：只读取已经保存的季报，不下载、不修改报告、不推测缺失数据。
同一任务的各个角色共享采集结果，避免每位研究员重新检索后拿到不同季度的材料。
"""

from time import monotonic

from app.services.ai_governance_context import AIControlError
from app.services.fund_report_retrieval import check_retrieval_active, report_retrieval_scope
from app.services.fund_reports import fund_report_service
from app.services.job_runtime import JobCancelledError
from app.services.tracing import traced


REPORT_EVIDENCE_VERSION = "portfolio-report-evidence-v1"
DEFAULT_QUESTION = "对比两期基金经理投资策略、资产配置变化、风险与成立条件，区分历史观点和当前事实。"


class PortfolioReportEvidenceService:
    def __init__(self, reader=fund_report_service, *, max_funds=3, timeout_seconds=40):
        self.reader = reader
        self.max_funds = max_funds
        self.timeout_seconds = timeout_seconds

    @traced("research.retrieve.fund_reports", **{"langfuse.observation.type": "retriever"})
    def capture(self, portfolio, *, question=DEFAULT_QUESTION, fund_codes=None, document_ids_by_fund=None):
        """先确定研究对象，再锁定两期编号。预算只限制研究覆盖，不偷偷缩小用户持仓。

        默认按仓位优先读取最多三只，未覆盖的基金明确列出。后续按需计划可以指定
        用户真正问到的基金，但不允许传入组合外基金。所有文本必须整块保留。
        """
        holdings = {str(row["fund_code"]): row for row in portfolio.get("holdings", [])}
        requested = list(dict.fromkeys(fund_codes if fund_codes is not None else holdings))
        if set(requested) - holdings.keys():
            raise ValueError("季报研究对象必须属于当前组合")
        requested.sort(key=lambda code: (-float(holdings[code].get("weight_pct") or 0), code))
        selected, omitted = requested[:self.max_funds], requested[self.max_funds:]
        result = {"version": REPORT_EVIDENCE_VERSION, "status": "unavailable", "question": question,
            "requested_fund_codes": requested, "omitted_fund_codes": omitted, "funds": [],
            "documents": [], "evidence": [], "warnings": [], "retrieval_warnings": [],
            "limitations": ["季报是历史披露，不代表当前实际持仓或今日交易。",
                "仅引用所列页内摘录，未入选片段不等于原文没有披露。"]}
        if omitted:
            result["warnings"].append("本轮季报覆盖达到基金数量上限，未读取：" + "、".join(omitted))
        deadline = monotonic() + self.timeout_seconds
        for code in selected:
            check_retrieval_active()
            if monotonic() >= deadline:
                result["funds"].append({"fund_code": code, "status": "budget_exhausted", "document_ids": []})
                continue
            try:
                # 后续期间即使有人同步新版季报，research 仍只读这两个已选定编号。
                listing = self.reader.list_reports(code)
                ids = (document_ids_by_fund or {}).get(code, listing["selected_ids"])
                if len(ids) != 2:
                    result["funds"].append({"fund_code": code, "status": "missing_reports", "document_ids": ids})
                    continue
                with report_retrieval_scope(deadline):
                    payload = self.reader.research(code, question, ids)
                documents = payload["documents"]
                if payload.get("fund_code") != code or {doc["id"] for doc in documents} != set(ids):
                    raise ValueError("季报返回的基金或文档版本不一致")
                chosen, verified_documents = [], []
                for doc in documents:
                    if doc.get("fund_code") != code:
                        raise ValueError("季报文档不属于当前基金")
                    verified_documents.append({key: doc.get(key) for key in (
                        "id", "fund_code", "period", "title", "published_at", "data_as_of", "content_hash", "source_url")})
                    rows = [row for row in payload["evidence"] if row["document_id"] == doc["id"]]
                    # 保留排序最高的普通片段，并补一份策略/表格原文。优先整段选择，
                    # 不从长段随手截断造成否定句丢失；每期最多两项，各期独立配额。
                    ordinary = [row for row in rows if row.get("section") not in {"manager_review", "allocation_table"}]
                    sections = [row for row in rows if row.get("section") == "manager_review"]
                    tables = [row for row in rows if row.get("section") == "allocation_table"]
                    candidates = ordinary[:1] + sections + tables + ordinary[1:]
                    per_quarter = []
                    for row in candidates:
                        if len(per_quarter) == 2:
                            break
                        if row.get("fund_code") != code or not row["evidence_id"].startswith(f"report:{doc['id']}:p"):
                            raise ValueError("季报证据作用域不一致")
                        # 一张长资产表仍由单基金工具完整提供，组合概览不塞入无限正文。
                        if not row.get("text") or len(row["text"]) > 1200:
                            continue
                        if row["text"] in [item["summary"] for item in per_quarter]:
                            continue
                        per_quarter.append({"evidence_id": row["evidence_id"], "kind": "fund_report",
                            "direction": "neutral", "title": f"{code} {doc['period']}季报 · 第{row['page']}页",
                            "summary": row["text"], "quote": row["text"], "value_text": None,
                            "fund_codes": [code], "document_id": doc["id"], "period": doc["period"],
                            "page": row["page"], "section": row.get("section", "page_excerpt"),
                            "data_date": doc.get("data_as_of"), "published_at": doc.get("published_at"),
                            "content_hash": doc.get("content_hash"), "source_url": row["source_url"],
                            "source_name": "基金定期报告原文", "coverage": "excerpt"})
                    chosen.extend(per_quarter)
                # 只有本基金全部文档通过作用域校验后才提交，异常不能留下半份元数据。
                result["documents"].extend(verified_documents)
                result["evidence"].extend(chosen)
                covered = {row["document_id"] for row in chosen}
                status = "available" if covered == set(ids) else "partial" if covered else "no_evidence"
                result["funds"].append({"fund_code": code, "status": status,
                    "document_ids": ids, "evidence_count": len(chosen),
                    "missing_evidence_document_ids": [doc_id for doc_id in ids if doc_id not in covered],
                    "missing_manager_document_ids": [doc_id for doc_id in ids if not any(
                        row["document_id"] == doc_id and row["section"] == "manager_review" for row in chosen)],
                    "retrieval_diagnostics": payload.get("retrieval_diagnostics", {})})
                result["warnings"].extend(f"{code}：{warning}" for warning in payload.get("warnings", []))
                result["retrieval_warnings"].extend(f"{code}：{warning}" for warning in payload.get("warnings", []))
            except (AIControlError, JobCancelledError):
                raise
            except Exception as exc:
                # 只保留错误类别。单只基金资料异常不阻断已有账务/风险分析，也不能
                # 把失败当成“这只基金没有风险”。任务取消和授权失效依然向上传播。
                check_retrieval_active()
                result["funds"].append({"fund_code": code, "status": "unavailable", "document_ids": [],
                    "reason": type(exc).__name__})
        incomplete = omitted or any(row["status"] != "available" for row in result["funds"])
        if result["evidence"]:
            result["status"] = "partial" if incomplete else "available"
        for row in result["funds"]:
            if row["status"] != "available":
                result["warnings"].append(f"{row['fund_code']} 季报证据未完整取得（{row['status']}）；结论不得假定已覆盖。")
        return result


portfolio_report_evidence_service = PortfolioReportEvidenceService()
