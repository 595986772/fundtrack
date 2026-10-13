"""固定 PDF 的检索前后对照，不调用模型，不改产品数据库和原有题目。

用法：python -m scripts.eval_report_retrieval --output data/artifacts/report-retrieval.json
只测关键词退化路径；结果不能代表真实向量/精排或最终回答正确率。
同时给出等候选数量的原始召回，以及产品完整证据装配后的标注命中和上下文字数。
"""

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from app.database import Database, PROJECT_ROOT
from app.models import FundReportDocumentModel
from app.services.fund_report_retrieval import RETRIEVAL_VERSION, bm25_rank
from app.services.fund_report_source import parse_pdf, page_chunks, manager_chunks, allocation_chunks
from app.services.fund_reports import FundReportService


def normalize(text):
    return re.sub(r"\s+", "", text)


def hit_count(rows, gold, documents):
    ids = {doc["period"]: doc["id"] for doc in documents}
    return sum(any(row["document_id"] == ids[item["period"]] and row["page"] == item["page"]
                   and normalize(item["quote"]) in normalize(row["text"]) for row in rows) for item in gold)


def compare():
    directory = PROJECT_ROOT / "data/evals/fund_reports_v1"
    manifest = json.loads((directory / "documents.json").read_text())["documents"]
    suite = json.loads((PROJECT_ROOT / "data/evals/fund_report_rag_v1.json").read_text())
    with TemporaryDirectory() as temporary:
        database = Database(f"sqlite:///{temporary}/eval.db")
        database.create_schema()
        # 明确关闭外部精排，保证此脚本可离线运行，不偷偷发送资料或消费余额。
        reader = FundReportService(database, reranker=SimpleNamespace(available=False))
        try:
            for doc in manifest:
                content = (directory / doc["pdf_path"]).read_bytes()
                if hashlib.sha256(content).hexdigest() != doc["content_hash"]:
                    raise ValueError("固定 PDF 哈希不一致，停止对照")
                pages, facts = parse_pdf(content, doc["fund_code"], doc["period"])
                if pages != doc["pages"]:
                    raise ValueError("固定 PDF 解析文本不一致，停止对照")
                with database.session_factory() as session:
                    session.add(FundReportDocumentModel(**{key: doc[key] for key in (
                        "id", "fund_code", "period", "title", "published_at", "source_url", "content_hash")},
                        pages=pages, facts=facts, created_at=datetime.now(timezone.utc)))
                    session.commit()
            cases = []
            for case in suite["scenarios"]:
                ids = [doc["id"] for doc in manifest if doc["fund_code"] == case["fund_code"]]
                documents = reader.pair(case["fund_code"], ids)
                questions = [case["message"]] + ([case["follow_up"]] if case.get("follow_up") else [])
                for turn, question in enumerate(questions, 1):
                    baseline, raw_before, raw_after = [], [], []
                    terms = set(re.findall(r"[a-z0-9]{2,}|[\u4e00-\u9fff]{2}", question.lower()))
                    for doc in documents:
                        chunks = page_chunks(doc)
                        old = sorted(chunks, key=lambda row: sum(term in row["text"].lower() for term in terms), reverse=True)[:2]
                        raw_before.extend(old)
                        lookup = {row["evidence_id"]: row for row in chunks}
                        raw_after.extend(lookup[row["evidence_id"]] for row in bm25_rank(question, chunks, limit=2))
                        baseline.extend({row["evidence_id"]: row for row in old + manager_chunks(doc) + allocation_chunks(doc)}.values())
                    current = reader.research(case["fund_code"], question, ids)["evidence"]
                    gold = case.get("gold_evidence", [])
                    cases.append({"id": case["id"], "turn": turn, "gold_count": len(gold),
                        "raw_top2_before": hit_count(raw_before, gold, documents),
                        "raw_top2_after": hit_count(raw_after, gold, documents),
                        "assembled_before": hit_count(baseline, gold, documents),
                        "assembled_after": hit_count(current, gold, documents),
                        "evidence_chars_before": sum(len(row["text"]) for row in baseline),
                        "evidence_chars_after": sum(len(row["text"]) for row in current)})
            keys = ("gold_count", "raw_top2_before", "raw_top2_after", "assembled_before", "assembled_after",
                    "evidence_chars_before", "evidence_chars_after")
            return {"version": RETRIEVAL_VERSION, "created_at": datetime.now(timezone.utc).isoformat(),
                "scope": "fixed-pdf-keyword-only", "external_model_calls": 0,
                "limitations": ["不测向量和真实精排", "标注尚待所有者人工验收", "召回率不代表最终回答正确率",
                    "原始召回固定每季度2条；完整产品证据字数可能不同，不声称同Token预算提速"],
                "totals": {key: sum(case[key] for case in cases) for key in keys}, "cases": cases}
        finally:
            database.engine.dispose()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = compare()
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({key: value for key, value in result.items() if key != "cases"}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
