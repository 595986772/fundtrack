"""把三只教学基金的最新定期持仓冻结为离线 fixture。

普通单元测试和页面初次启动只读取生成后的 JSON，不会访问财经网站。需要刷新样例时，
由开发者手工运行本脚本；生产同步仍通过 Background Job 写入 SQL。
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import sys


# 直接执行 ``python scripts/...`` 时，Python 默认只把 scripts/ 放进模块搜索路径。
# 显式加入项目根目录后，脚本才能复用应用里的 Provider，而不是复制一份清洗逻辑。
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.services.fund_disclosures import (  # noqa: E402
    DEFAULT_DISCLOSURE_FIXTURE,
    AkshareFundDisclosureProvider,
)


SAMPLE_FUND_CODES = ("110020", "161725", "110007")


def main() -> None:
    provider = AkshareFundDisclosureProvider()
    funds = []
    for fund_code in SAMPLE_FUND_CODES:
        batch = provider.fetch(fund_code=fund_code)
        funds.append(
            {
                "fund_code": fund_code,
                "source_url": (
                    f"https://fundf10.eastmoney.com/ccmx_{fund_code}.html"
                ),
                "report_dates": {
                    "stock": _date_text(batch.stock_report_date),
                    "bond": _date_text(batch.bond_report_date),
                    "industry": _date_text(batch.industry_report_date),
                },
                "warnings": list(batch.warnings),
                "security_holdings": [
                    {
                        "report_date": item.report_date.isoformat(),
                        "asset_type": item.asset_type,
                        "security_code": item.security_code,
                        "security_name": item.security_name,
                        "weight_pct": item.weight_pct,
                        "market_value_10k": item.market_value_10k,
                        "rank": item.rank,
                        "source": provider.source,
                        "source_url": item.source_url,
                    }
                    for item in batch.security_holdings
                ],
                "industry_allocations": [
                    {
                        "report_date": item.report_date.isoformat(),
                        "industry_name": item.industry_name,
                        "weight_pct": item.weight_pct,
                        "market_value_10k": item.market_value_10k,
                        "rank": item.rank,
                        "source": provider.source,
                        "source_url": item.source_url,
                    }
                    for item in batch.industry_allocations
                ],
            }
        )

    payload = {
        "fixture_version": 1,
        "purpose": "FundScope offline fund disclosure and look-through tests",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": provider.source,
        "funds": funds,
    }
    output = Path(DEFAULT_DISCLOSURE_FIXTURE)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"wrote {output} ({len(funds)} funds)")


def _date_text(value):
    return value.isoformat() if value else None


if __name__ == "__main__":
    main()
