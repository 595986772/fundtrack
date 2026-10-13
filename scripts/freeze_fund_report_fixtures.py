"""手动冻结公开季报样本；评测运行时绝不调用这个下载脚本。

固定 URL 而不是查询“最新季报”，避免下次执行时悄悄换掉考卷。
PDF 和逐页文本一起保存，人工可以回到原文件核对参考答案。
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.services.fund_report_source import FundReportSource, parse_pdf

SOURCES = {
    "110020": ("index", "易方达沪深300ETF联接A", "202607211827165072", "202604221821399649"),
    "015789": ("active_equity", "永赢高端装备智选混合发起A", "202607211827192740", "202604221821418032"),
    "110007": ("bond", "易方达稳健收益债券A", "202607211827164584", "202604221821399699"),
}


def freeze():
    directory = ROOT / "data/evals/fund_reports_v1"
    target = directory / "documents.json"
    if target.exists():
        raise SystemExit("样本已存在。新一版数据请另建目录，不能覆盖已用于比较的考卷。")
    source, documents = FundReportSource(), []
    for code, (kind, name, q2, q1) in SOURCES.items():
        for quarter, announcement in ((1, q1), (2, q2)):
            url = f"https://pdf.dfcfw.com/pdf/H2_AN{announcement}_1.pdf"
            content = source.download(url)
            digest = hashlib.sha256(content).hexdigest()
            doc_id = "fr_" + hashlib.sha256(f"{code}:{digest}".encode()).hexdigest()[:32]
            period = f"2026-Q{quarter}"
            pages, facts = parse_pdf(content, code, period)
            path = directory / "pdfs" / f"{code}_{period}.pdf"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
            documents.append(dict(id=doc_id, fund_code=code, fund_kind=kind,
                title=f"{name} {period} 季度报告", period=period,
                published_at="2026-04-22" if quarter == 1 else "2026-07-21",
                source_url=url, content_hash=digest, pdf_path=str(path.relative_to(directory)),
                pages=pages, facts=facts))
            print(f"{code} {period}: {len(pages)} 页，{len(facts)} 项标准表格数字", flush=True)
    target.write_text(json.dumps({"version": "1", "frozen_at": "2026-09-14",
        "source": "东方财富公开基金公告 PDF", "documents": documents},
        ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    freeze()
