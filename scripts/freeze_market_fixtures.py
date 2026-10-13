"""抓取一份市场宽度和成交额快照，供 FundScope 离线演示使用。

这个脚本只在人工刷新数据时访问网络。FastAPI 服务和自动测试始终读取已经
生成的 JSON，因此不会因为财经接口暂时不可用而随机失败。
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import akshare as ak
import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parents[1]
OUTPUT_PATH = PROJECT_ROOT / "data" / "market_fixtures" / "market_snapshot.json"
SNAPSHOT_DATE = pd.Timestamp("2026-08-25")


def _market_activity_values() -> dict[str, object]:
    """把 AKShare 返回的 item/value 表格转换成便于取值的字典。"""

    frame = ak.stock_market_activity_legu()
    return dict(zip(frame["item"], frame["value"]))


def main() -> None:
    """抓取原始值并写入一个结构清晰的 JSON 快照。"""

    activity = _market_activity_values()
    observed_at = pd.Timestamp(str(activity["统计日期"]))
    if observed_at.date() != SNAPSHOT_DATE.date():
        raise RuntimeError(
            f"市场宽度接口返回 {observed_at.date()}，与冻结日期 "
            f"{SNAPSHOT_DATE.date()} 不一致"
        )

    index_frame = ak.stock_zh_index_daily_em(
        symbol="sh000300",
        start_date=(SNAPSHOT_DATE - pd.Timedelta(days=20)).strftime("%Y%m%d"),
        end_date=SNAPSHOT_DATE.strftime("%Y%m%d"),
    )
    latest_amounts = index_frame.tail(5)
    if len(latest_amounts) != 5:
        raise RuntimeError("沪深300指数成交额不足 5 个交易日")

    payload = {
        "fixture_version": 1,
        "purpose": "FundScope deterministic market context and offline tests",
        "generated_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(
            timespec="seconds"
        ),
        "data_date": SNAPSHOT_DATE.date().isoformat(),
        "broad_market_proxy": {
            "fund_code": "110020",
            "name": "易方达沪深300ETF联接A",
            "tracking_target": "沪深300指数",
            "source": "AKShare / Eastmoney Fund",
            "source_url": "https://fundf10.eastmoney.com/jbgk_110020.html",
        },
        "breadth": {
            "advancing_count": int(float(activity["上涨"])),
            "declining_count": int(float(activity["下跌"])),
            "flat_count": int(float(activity["平盘"])),
            "suspended_count": int(float(activity["停牌"])),
            "limit_up_count": int(float(activity["涨停"])),
            "limit_down_count": int(float(activity["跌停"])),
            "market_activity_pct": float(str(activity["活跃度"]).rstrip("%")),
            "observed_at": observed_at.tz_localize("Asia/Shanghai").isoformat(),
            "source": "LeGuLeGu via AKShare",
            "source_url": "https://legulegu.com/stockdata/market-activity",
        },
        "turnover_proxy": {
            "benchmark_code": "000300",
            "benchmark_name": "沪深300指数",
            "source": "Eastmoney Index via AKShare",
            "source_url": "https://quote.eastmoney.com/zs000300.html",
            "daily_amounts": [
                {
                    "date": pd.Timestamp(row["date"]).date().isoformat(),
                    "amount_cny": round(float(row["amount"]), 2),
                }
                for _, row in latest_amounts.iterrows()
            ],
        },
    }

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"已冻结 {payload['data_date']} 市场状态快照：{OUTPUT_PATH}")


if __name__ == "__main__":
    main()
