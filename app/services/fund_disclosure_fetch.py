"""复用 AKShare 披露 Provider，给聊天中的一次补查加上真正可终止的超时。

AKShare 某些底层请求没有 timeout 参数。线程超时只是不再等待，线程仍会运行；
因此这里用固定 Python 子进程抓取，超时由 subprocess 杀掉并回收。子进程不写披露表，
只有完整、匹配当前基金的结果才交给父进程保存。没有 shell，也不接受模型传入命令。
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import date
import json
from pathlib import Path
import re
import subprocess
import sys


def fetch_disclosure_bounded(fund_code: str, *, timeout_seconds: int):
    from app.services.fund_disclosures import FundDisclosureBatch, FundSecurityHolding, FundIndustryAllocation

    if not re.fullmatch(r"\d{6}", fund_code):
        raise ValueError("基金代码必须是六位数字")
    try:
        process = subprocess.run(
            [sys.executable, "-m", "app.services.fund_disclosure_fetch", fund_code],
            cwd=Path(__file__).resolve().parents[2], capture_output=True, text=True,
            timeout=timeout_seconds, check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise TimeoutError(f"持仓披露公开源超过 {timeout_seconds} 秒未返回，已停止本次补查") from exc
    if process.returncode:
        # stderr 可能包含代理/运行环境信息，不直接回显给模型或浏览器。
        raise RuntimeError("持仓披露公开源读取失败，已有证据保留")
    payload = json.loads(process.stdout)
    if payload["fund_code"] != fund_code:
        raise ValueError("披露来源返回了不同基金，未保存")
    for key, cls in (("security_holdings", FundSecurityHolding), ("industry_allocations", FundIndustryAllocation)):
        rows = []
        for row in payload[key]:
            if row["fund_code"] != fund_code:
                raise ValueError("披露明细包含其他基金，未保存")
            rows.append(cls(**{**row, "report_date": date.fromisoformat(row["report_date"])}))
        payload[key] = tuple(rows)
    for key in ("stock_report_date", "bond_report_date", "industry_report_date"):
        payload[key] = date.fromisoformat(payload[key]) if payload[key] else None
    payload["warnings"] = tuple(payload["warnings"])
    return FundDisclosureBatch(**payload)


def main() -> None:
    from contextlib import redirect_stdout
    from app.services.fund_disclosures import AkshareFundDisclosureProvider

    # 第三方进度/提示统一去 stderr，stdout 只传结构化数据，避免破坏父进程 JSON 解析。
    with redirect_stdout(sys.stderr):
        batch = AkshareFundDisclosureProvider().fetch(fund_code=sys.argv[1])
    print(json.dumps(asdict(batch), default=lambda value: value.isoformat(), ensure_ascii=False))


if __name__ == "__main__":
    main()
