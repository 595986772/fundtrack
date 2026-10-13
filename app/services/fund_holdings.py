"""单基金持仓分析：把已保存披露整理成页面和 Agent 共用的事实。

这里不创建临时组合，也不乘以用户组合权重。百分比始终是占这只基金净值的比例。
不抓取、不调用模型、不推测现金仓位，也不把重仓榜的进出说成实际买卖。
"""

from typing import Any

from app.services.fund_disclosures import fund_disclosure_service
from app.services.funds import fund_catalog_service


class FundHoldingsService:
    def __init__(self, *, disclosures=fund_disclosure_service, catalog=fund_catalog_service):
        self.disclosures = disclosures
        self.catalog = catalog

    def analyze(self, *, fund_code: str) -> dict[str, Any]:
        """只消费现有基金档案和披露，未知字段使用 None，不伪造 0%。"""
        profile = self.catalog.get_fund(fund_code)["profile"]
        disclosure = self.disclosures.get_fund(fund_code=fund_code)
        securities = disclosure["security_holdings"]
        industries = disclosure["industry_allocations"]
        dates = disclosure["report_dates"]
        assets = []
        for kind, label in (("stock", "已披露股票"), ("bond", "已披露债券")):
            rows = [row for row in securities if row["asset_type"] == kind]
            assets.append({"kind": kind, "label": label, "report_date": dates.get(kind),
                           "weight_pct": round(sum(row["weight_pct"] for row in rows), 4) if rows else None})
        assets.append({"kind": "cash", "label": "现金及其他资产", "weight_pct": None, "report_date": None})

        # 不同报告期不能拼成一份资产饼图；超过 100% 的债券暴露也不能悄悄裁掉。
        security_dates = {row["report_date"] for row in securities}
        same_period = len(security_dates) == 1
        gross = round(sum(row["weight_pct"] for row in securities), 4) if same_period else None
        coverage = min(100.0, gross) if gross is not None else None
        stock_rows = sorted((row for row in securities if row["asset_type"] == "stock"), key=lambda row: row["weight_pct"], reverse=True)
        industry_rows = sorted(industries, key=lambda row: row["weight_pct"], reverse=True)
        linked = "联接" in str(profile.get("short_name", "")) or "联接" in str(profile.get("name", ""))
        limitations = [
            "持仓来自定期披露，不代表盘中或今日真实仓位。",
            "股票和债券可能只披露部分重仓；未披露部分不等于现金，也不能认定没有持有。",
            "行业配置与证券明细是两种口径，不能相加；各自日期以来源为准。",
            "比例变化会受价格、基金净值及份额等因素影响，不能直接判断基金经理加仓或减仓。",
        ]
        if len(security_dates) > 1:
            limitations.append("股票和债券来自不同报告期，未合并计算总覆盖率。")
        if gross is not None and gross > 100:
            limitations.append("已披露证券比例超过 100%，可能涉及杠杆或披露口径；原始比例保留，覆盖率仅封顶展示。")
        if linked:
            limitations.append("该基金为联接基金：目前没有目标 ETF 的可靠持有比例和对应穿透映射，本页不代表目标 ETF 的完整底层持仓。")

        # 三类披露各自比较，不跨股票/债券类别、来源或 fixture/数据库混用历史。
        comparisons = []
        for kind in ("stock", "bond", "industry"):
            current = industries if kind == "industry" else [row for row in securities if row["asset_type"] == kind]
            previous = None
            if current and dates.get(kind) and disclosure["data_mode"] == "database":
                previous = self.disclosures.repository.previous_report(
                    fund_code=fund_code, kind=kind, before=dates[kind], source=disclosure["source"],
                )
            comparisons.append(self._compare(kind, current, dates.get(kind), previous))

        return {
            "fund_code": fund_code, "short_name": profile["short_name"],
            "fund_type": profile.get("fund_type", ""), "disclosure": disclosure,
            "asset_evidence": assets, "gross_disclosed_weight_pct": gross,
            "security_coverage_pct": coverage,
            "unresolved_weight_pct": round(100 - coverage, 4) if coverage is not None else None,
            "concentration": {
                "top_stock_weight_pct": stock_rows[0]["weight_pct"] if stock_rows else None,
                "top_five_disclosed_stocks_pct": round(sum(row["weight_pct"] for row in stock_rows[:5]), 4) if stock_rows else None,
                "top_industry_name": industry_rows[0]["industry_name"] if industry_rows else None,
                "top_industry_weight_pct": industry_rows[0]["weight_pct"] if industry_rows else None,
            },
            "comparisons": comparisons, "lookthrough_status": "target_etf_unavailable" if linked else "direct_disclosure_only",
            "limitations": limitations,
        }

    @staticmethod
    def _compare(kind, current, report_date, previous):
        """只对两期都披露的项目算百分点差；榜单进出保持未知，不把缺席补为零。"""
        result = {"kind": kind, "status": "available" if previous else "unavailable",
                  "report_date": report_date, "previous_report_date": previous["report_date"] if previous else None,
                  "rows": [], "message": "仅比较上次已保存的同来源非空披露，不一定是上一季度；新出现或未再披露不等于买入或清仓。" if previous else "缺少同来源可比历史披露，暂不能判断变化。"}
        if not previous:
            return result
        now = {(row["industry_name"] if kind == "industry" else row["security_code"]): row for row in current}
        old = {row["key"]: row for row in previous["rows"]}
        for key in sorted(now.keys() | old.keys()):
            left, right = old.get(key), now.get(key)
            change = "newly_disclosed" if left is None else "not_disclosed" if right is None else "comparable"
            result["rows"].append({
                "key": key, "name": (right.get("industry_name") or right.get("security_name")) if right else left["name"],
                "status": change, "previous_weight_pct": left["weight_pct"] if left else None,
                "weight_pct": right["weight_pct"] if right else None,
                "change_pp": round(right["weight_pct"] - left["weight_pct"], 4) if left and right else None,
                "source_url": right["source_url"] if right else None,
                "previous_source_url": left["source_url"] if left else None,
            })
        return result


fund_holdings_service = FundHoldingsService()
