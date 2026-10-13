"""从 AKShare 抓取三只样例基金，生成可重复测试的冻结数据。

这个脚本只在人工刷新 Fixture 时访问网络。正常单元测试直接
读取已生成的 JSON/CSV，不依赖外部财经接口。
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import akshare as ak
import pandas as pd

from app.services.fund_corporate_actions import (
    AkshareFundCorporateActionProvider,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
OUTPUT_DIR = PROJECT_ROOT / "data" / "fund_fixtures"

# 使用固定时间窗口，保证以后重新生成时不会无限追加新数据。
SNAPSHOT_START = pd.Timestamp("2023-08-25")
SNAPSHOT_END = pd.Timestamp("2026-08-25")

# 三只基金分别代表宽基指数、行业主题和债券类型。
# 选择它们是为了制造明显的风险差异，不代表任何投资建议。
SAMPLE_FUNDS = (
    {"fund_code": "110020", "sample_role": "broad_market_index"},
    {"fund_code": "161725", "sample_role": "industry_theme"},
    {"fund_code": "110007", "sample_role": "bond_fund"},
)
CORPORATE_ACTION_PROVIDER = AkshareFundCorporateActionProvider()


def _clean_text(value: object) -> str | None:
    """将 DataFrame 中的空值和混合类型转成可序列化文本。"""
    if pd.isna(value):
        return None
    return str(value).strip()


def _load_profile(sample: dict[str, str]) -> dict[str, object]:
    """读取一只基金的基本档案，并只保留后续会用到的字段。"""
    fund_code = sample["fund_code"]
    frame = ak.fund_overview_em(symbol=fund_code)
    if frame.empty:
        raise RuntimeError(f"未获取到基金 {fund_code} 的基本资料")

    row = frame.iloc[0]
    return {
        "fund_code": fund_code,
        "sample_role": sample["sample_role"],
        "full_name": _clean_text(row.get("基金全称")),
        "short_name": _clean_text(row.get("基金简称")),
        "fund_type": _clean_text(row.get("基金类型")),
        "issue_date": _clean_text(row.get("发行日期")),
        "established": _clean_text(row.get("成立日期/规模")),
        "net_asset_size": _clean_text(row.get("净资产规模")),
        "share_size": _clean_text(row.get("份额规模")),
        "management_company": _clean_text(row.get("基金管理人")),
        "custodian": _clean_text(row.get("基金托管人")),
        "fund_managers": _clean_text(row.get("基金经理人")),
        "management_fee": _clean_text(row.get("管理费率")),
        "custodian_fee": _clean_text(row.get("托管费率")),
        "benchmark": _clean_text(row.get("业绩比较基准")),
        "tracking_target": _clean_text(row.get("跟踪标的")),
        "source_url": f"https://fundf10.eastmoney.com/jbgk_{fund_code}.html",
    }


def _load_nav(fund_code: str) -> pd.DataFrame:
    """读取单位净值，并转换为项目内部使用的稳定列名。"""
    frame = ak.fund_open_fund_info_em(
        symbol=fund_code,
        indicator="单位净值走势",
    )
    if frame.empty:
        raise RuntimeError(f"未获取到基金 {fund_code} 的净值数据")

    nav_dates = pd.to_datetime(frame["净值日期"])
    selected = frame.loc[
        nav_dates.between(SNAPSHOT_START, SNAPSHOT_END),
        ["净值日期", "单位净值", "日增长率"],
    ].copy()
    if selected.empty:
        raise RuntimeError(f"基金 {fund_code} 在冻结时间窗口内没有净值")

    result = pd.DataFrame(
        {
            "fund_code": fund_code,
            "nav_date": pd.to_datetime(selected["净值日期"]).dt.strftime("%Y-%m-%d"),
            "unit_nav": pd.to_numeric(selected["单位净值"], errors="raise"),
            "daily_growth_rate_pct": pd.to_numeric(
                selected["日增长率"], errors="coerce"
            ),
        }
    )
    return result.drop_duplicates(subset=["fund_code", "nav_date"]).sort_values(
        "nav_date"
    )


def main() -> None:
    """抓取资料、净值和公司行为，最后写入可重复使用的 Fixture。"""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    profiles: list[dict[str, object]] = []
    nav_frames: list[pd.DataFrame] = []
    corporate_action_funds: list[dict[str, object]] = []
    for sample in SAMPLE_FUNDS:
        fund_code = sample["fund_code"]
        profiles.append(_load_profile(sample))
        nav_frames.append(_load_nav(fund_code))
        actions = CORPORATE_ACTION_PROVIDER.fetch_actions(
            fund_code=fund_code,
            date_from=SNAPSHOT_START.date(),
            date_to=SNAPSHOT_END.date(),
        )
        corporate_action_funds.append(
            {
                "fund_code": fund_code,
                "actions": [action.as_dict() for action in actions],
            }
        )

    generated_at = datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds")
    profile_payload = {
        "fixture_version": 1,
        "purpose": "FundScope development and offline tests",
        "generated_at": generated_at,
        "snapshot_start": SNAPSHOT_START.date().isoformat(),
        "snapshot_end": SNAPSHOT_END.date().isoformat(),
        "source": "AKShare / Eastmoney Fund",
        "funds": profiles,
    }
    (OUTPUT_DIR / "fund_profiles.json").write_text(
        json.dumps(profile_payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    nav_frame = pd.concat(nav_frames, ignore_index=True).sort_values(
        ["fund_code", "nav_date"]
    )
    nav_frame.to_csv(OUTPUT_DIR / "fund_nav.csv", index=False, encoding="utf-8")

    # 空 actions 也必须写入，因为它配合 coverage 表示“已经查询，区间内确实没有”。
    corporate_action_payload = {
        "fixture_version": 1,
        "purpose": "FundScope offline total-return and drawdown tests",
        "generated_at": generated_at,
        "coverage_start": SNAPSHOT_START.date().isoformat(),
        "coverage_end": SNAPSHOT_END.date().isoformat(),
        "source": CORPORATE_ACTION_PROVIDER.source,
        "source_url": "https://fund.eastmoney.com/",
        "funds": corporate_action_funds,
    }
    (OUTPUT_DIR / "fund_corporate_actions.json").write_text(
        json.dumps(corporate_action_payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    print(
        f"已冻结 {len(profiles)} 只基金、{len(nav_frame)} 条净值记录和 "
        f"{sum(len(item['actions']) for item in corporate_action_funds)} 条公司行为，"
        f"截止 {SNAPSHOT_END.date().isoformat()}"
    )


if __name__ == "__main__":
    main()
