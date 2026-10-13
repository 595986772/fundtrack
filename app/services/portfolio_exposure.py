"""基金组合的底层持仓穿透与行业暴露计算。

所有数值都由 Python 确定性计算：

``组合中的证券暴露 = 基金当前市值权重 × 该证券占基金净值比例``

同一证券出现在多只基金中时再相加，就能看见表面上分散、底层却重复持有的风险。
大模型只通过 Tool 读取这里的结果并负责通俗解释，不能改写权重、报告期或覆盖率。
"""

from __future__ import annotations

from datetime import date
from itertools import combinations
from typing import Any, Protocol

from app.services.fund_disclosures import (
    FundDisclosureError,
    fund_disclosure_service,
)
from app.services.portfolios import (
    PortfolioError,
    PortfolioNotFoundError,
    portfolio_service,
)


from app.services.errors import ServiceError


class PortfolioExposureError(ServiceError):
    """组合或披露数据无法生成可信的穿透结果。"""


class PortfolioReader(Protocol):
    def get(self, *, user_id: str, portfolio_id: str) -> dict[str, Any]: ...


class DisclosureReader(Protocol):
    def list_for_funds(
        self,
        fund_codes: list[str],
    ) -> dict[str, dict[str, Any]]: ...


class PortfolioExposureService:
    """把基金层权重展开为证券、行业、资产类别和重合度。"""

    def __init__(
        self,
        *,
        portfolios: PortfolioReader = portfolio_service,
        disclosures: DisclosureReader = fund_disclosure_service,
    ) -> None:
        self.portfolios = portfolios
        self.disclosures = disclosures

    def analyze(self, *, user_id: str, portfolio_id: str) -> dict[str, Any]:
        """计算一份组合在最新可用季报下的底层暴露。"""

        try:
            portfolio = self.portfolios.get(
                user_id=user_id,
                portfolio_id=portfolio_id,
            )
            holdings = list(portfolio.get("holdings") or [])
            if not holdings:
                raise PortfolioExposureError("当前组合没有可穿透的持仓", code="data_unavailable")
            disclosure_map = self.disclosures.list_for_funds(
                [item["fund_code"] for item in holdings]
            )
        except PortfolioNotFoundError:
            raise
        except PortfolioExposureError:
            raise
        except (PortfolioError, FundDisclosureError, KeyError, TypeError) as exc:
            raise PortfolioExposureError(f"无法读取组合披露数据：{exc}") from exc

        # 聚合字典先保留未舍入的浮点数和贡献基金，最后统一排序、舍入并生成 evidence_id。
        security_groups: dict[tuple[str, str], dict[str, Any]] = {}
        industry_groups: dict[str, dict[str, Any]] = {}
        fund_security_weights: dict[str, dict[tuple[str, str], dict[str, Any]]] = {}
        fund_reports: list[dict[str, Any]] = []
        unavailable_codes: list[str] = []
        all_warnings: list[str] = []

        equity_industry_exposure = 0.0
        fixed_income_exposure = 0.0
        unpenetrated_exposure = 0.0
        security_coverage = 0.0
        industry_asset_coverage = 0.0
        gross_security_exposure = 0.0
        gross_industry_asset_exposure = 0.0

        for holding in holdings:
            fund_code = holding["fund_code"]
            fund_name = holding["short_name"]
            fund_weight = float(holding["weight_pct"])
            disclosure = disclosure_map[fund_code]
            securities = disclosure["security_holdings"]
            industries = disclosure["industry_allocations"]

            stock_total = sum(
                float(item["weight_pct"])
                for item in securities
                if item["asset_type"] == "stock"
            )
            bond_total = sum(
                float(item["weight_pct"])
                for item in securities
                if item["asset_type"] == "bond"
            )
            industry_total = sum(float(item["weight_pct"]) for item in industries)
            security_total = stock_total + bond_total
            industry_asset_total = industry_total + bond_total

            # coverage 使用 100% 封顶，回答“组合净值有多少已知”；gross 不封顶，保留
            # 债券基金通过回购等方式形成的杠杆暴露，二者不能混为一个数字。
            security_coverage += fund_weight * min(100.0, security_total) / 100
            industry_asset_coverage += (
                fund_weight * min(100.0, industry_asset_total) / 100
            )
            gross_security_exposure += fund_weight * security_total / 100
            gross_industry_asset_exposure += (
                fund_weight * industry_asset_total / 100
            )
            equity_industry_exposure += fund_weight * industry_total / 100
            fixed_income_exposure += fund_weight * bond_total / 100
            unpenetrated_exposure += (
                fund_weight * max(0.0, 100.0 - min(100.0, industry_asset_total)) / 100
            )

            if disclosure["data_mode"] == "unavailable":
                unavailable_codes.append(fund_code)
            all_warnings.extend(
                f"{fund_code}：{message}" for message in disclosure.get("warnings", [])
            )

            fund_security_weights[fund_code] = {}
            for item in securities:
                key = (item["asset_type"], item["security_code"])
                within_weight = float(item["weight_pct"])
                contribution = fund_weight * within_weight / 100
                source_fund = {
                    "fund_code": fund_code,
                    "short_name": fund_name,
                    "fund_weight_pct": round(fund_weight, 4),
                    "within_fund_weight_pct": round(within_weight, 4),
                    "portfolio_exposure_pct": round(contribution, 4),
                    "report_date": item["report_date"],
                }
                target = security_groups.setdefault(
                    key,
                    {
                        "asset_type": item["asset_type"],
                        "security_code": item["security_code"],
                        "security_name": item["security_name"],
                        "portfolio_exposure_pct": 0.0,
                        "source_funds": [],
                        "report_dates": set(),
                        "source_urls": set(),
                    },
                )
                target["portfolio_exposure_pct"] += contribution
                target["source_funds"].append(source_fund)
                target["report_dates"].add(str(item["report_date"]))
                target["source_urls"].add(item["source_url"])
                fund_security_weights[fund_code][key] = item

            for item in industries:
                within_weight = float(item["weight_pct"])
                contribution = fund_weight * within_weight / 100
                target = industry_groups.setdefault(
                    item["industry_name"],
                    {
                        "industry_name": item["industry_name"],
                        "portfolio_exposure_pct": 0.0,
                        "source_funds": [],
                        "report_dates": set(),
                        "source_urls": set(),
                    },
                )
                target["portfolio_exposure_pct"] += contribution
                target["source_funds"].append(
                    {
                        "fund_code": fund_code,
                        "short_name": fund_name,
                        "fund_weight_pct": round(fund_weight, 4),
                        "within_fund_weight_pct": round(within_weight, 4),
                        "portfolio_exposure_pct": round(contribution, 4),
                        "report_date": item["report_date"],
                    }
                )
                target["report_dates"].add(str(item["report_date"]))
                target["source_urls"].add(item["source_url"])

            fund_reports.append(
                {
                    "fund_code": fund_code,
                    "short_name": fund_name,
                    "portfolio_weight_pct": round(fund_weight, 4),
                    "data_mode": disclosure["data_mode"],
                    "report_dates": disclosure["report_dates"],
                    "latest_report_date": disclosure["latest_report_date"],
                    "report_age_days": disclosure["report_age_days"],
                    "freshness_status": disclosure["freshness_status"],
                    "security_disclosure_pct": round(security_total, 4),
                    "security_coverage_pct": round(min(100.0, security_total), 4),
                    "industry_asset_disclosure_pct": round(
                        industry_asset_total,
                        4,
                    ),
                    "industry_asset_coverage_pct": round(
                        min(100.0, industry_asset_total),
                        4,
                    ),
                    "unpenetrated_pct": round(
                        max(0.0, 100.0 - min(100.0, industry_asset_total)),
                        4,
                    ),
                    "source": disclosure["source"],
                    "source_url": disclosure["source_url"],
                    "warnings": disclosure.get("warnings", []),
                }
            )

        security_exposures = self._public_security_groups(
            portfolio_id=portfolio_id,
            groups=security_groups,
        )
        industry_exposures = self._public_industry_groups(
            portfolio_id=portfolio_id,
            groups=industry_groups,
        )
        overlaps = self._fund_overlaps(
            holdings=holdings,
            fund_security_weights=fund_security_weights,
        )

        security_concentration = self._security_concentration(
            exposures=security_exposures,
            coverage_pct=security_coverage,
        )
        industry_concentration = self._industry_concentration(
            exposures=industry_exposures,
            # 固收披露很完整并不代表股票行业数据完整，行业状态只看已识别股票行业。
            coverage_pct=equity_industry_exposure,
        )
        quality = self._data_quality(
            security_coverage=security_coverage,
            industry_asset_coverage=industry_asset_coverage,
            fund_reports=fund_reports,
            unavailable_codes=unavailable_codes,
            warnings=all_warnings,
        )
        report_dates = [
            date.fromisoformat(value)
            for item in fund_reports
            for value in item["report_dates"].values()
            if value
        ]

        top_security = security_exposures[0] if security_exposures else None
        top_industry = industry_exposures[0] if industry_exposures else None
        security_text = (
            f"最大底层证券为{top_security['security_name']} "
            f"{top_security['portfolio_exposure_pct']:.2f}%"
            if top_security
            else "暂无可用底层证券"
        )
        industry_text = (
            f"最大股票行业为{top_industry['industry_name']} "
            f"{top_industry['portfolio_exposure_pct']:.2f}%"
            if top_industry
            else "暂无可用股票行业"
        )

        return {
            "data_kind": "deterministic_portfolio_lookthrough",
            "portfolio_id": portfolio_id,
            "user_id": user_id,
            "portfolio_name": portfolio["name"],
            "calculated_at": date.today().isoformat(),
            "oldest_report_date": min(report_dates).isoformat() if report_dates else None,
            "latest_report_date": max(report_dates).isoformat() if report_dates else None,
            "summary": (
                f"按当前市值权重，底层证券已穿透 {security_coverage:.2f}%，"
                f"行业与固收类别已覆盖 {industry_asset_coverage:.2f}%；"
                f"{security_text}，{industry_text}。"
            ),
            "data_quality": quality,
            "concentration": {
                **security_concentration,
                **industry_concentration,
                "overall_state": self._overall_state(
                    security_concentration["security_state"],
                    industry_concentration["industry_state"],
                ),
            },
            "asset_exposures": [
                {
                    "code": "equity_industry",
                    "label": "已识别股票行业",
                    "portfolio_exposure_pct": round(equity_industry_exposure, 4),
                },
                {
                    "code": "fixed_income",
                    "label": "已披露固定收益",
                    "portfolio_exposure_pct": round(fixed_income_exposure, 4),
                },
                {
                    "code": "unpenetrated",
                    "label": "其他 / 尚未穿透",
                    "portfolio_exposure_pct": round(unpenetrated_exposure, 4),
                },
            ],
            "security_exposures": security_exposures,
            "industry_exposures": industry_exposures,
            "fund_overlaps": overlaps,
            "fund_reports": sorted(
                fund_reports,
                key=lambda item: item["portfolio_weight_pct"],
                reverse=True,
            ),
            "gross_security_exposure_pct": round(gross_security_exposure, 4),
            "gross_industry_asset_exposure_pct": round(
                gross_industry_asset_exposure,
                4,
            ),
            "methodology": (
                "证券/行业组合暴露 = Σ(基金当前市值权重 × 季报内占净值比例)。"
                "证券重合度 = 两只基金共同证券权重的逐项较小值之和；coverage 对每只"
                "基金封顶 100%，gross exposure 不封顶以保留债券杠杆。"
            ),
            "limitations": [
                "基金持仓来自定期报告，不代表盘中实时仓位。",
                "基金投资、ETF 联接份额、现金和未披露长尾资产会进入“其他 / 尚未穿透”。",
                "行业名称沿用信息披露口径，可能比投资者常用的主题行业更宽泛。",
                "底层重合揭示重复暴露，不等同于收益相关性或未来涨跌预测。",
            ],
            "disclaimer": "持仓穿透与行业暴露仅用于研究和风险识别，不构成投资建议。",
        }

    @staticmethod
    def _public_security_groups(
        *,
        portfolio_id: str,
        groups: dict[tuple[str, str], dict[str, Any]],
    ) -> list[dict[str, Any]]:
        items = []
        for target in groups.values():
            report_dates = sorted(target["report_dates"])
            source_urls = sorted(target["source_urls"])
            items.append(
                {
                    "evidence_id": (
                        f"lookthrough:security:{portfolio_id}:"
                        f"{target['asset_type']}:{target['security_code']}:"
                        f"{report_dates[0]}"
                    ),
                    "asset_type": target["asset_type"],
                    "security_code": target["security_code"],
                    "security_name": target["security_name"],
                    "portfolio_exposure_pct": round(
                        target["portfolio_exposure_pct"],
                        4,
                    ),
                    "source_fund_count": len(target["source_funds"]),
                    "report_date": report_dates[0],
                    "report_dates": report_dates,
                    "source_url": source_urls[0],
                    "source_funds": sorted(
                        target["source_funds"],
                        key=lambda item: item["portfolio_exposure_pct"],
                        reverse=True,
                    ),
                }
            )
        return sorted(
            items,
            key=lambda item: item["portfolio_exposure_pct"],
            reverse=True,
        )

    @staticmethod
    def _public_industry_groups(
        *,
        portfolio_id: str,
        groups: dict[str, dict[str, Any]],
    ) -> list[dict[str, Any]]:
        items = []
        for target in groups.values():
            report_dates = sorted(target["report_dates"])
            source_urls = sorted(target["source_urls"])
            normalized_name = target["industry_name"].replace(":", "_")
            items.append(
                {
                    "evidence_id": (
                        f"lookthrough:industry:{portfolio_id}:"
                        f"{normalized_name}:{report_dates[0]}"
                    ),
                    "industry_name": target["industry_name"],
                    "portfolio_exposure_pct": round(
                        target["portfolio_exposure_pct"],
                        4,
                    ),
                    "source_fund_count": len(target["source_funds"]),
                    "report_date": report_dates[0],
                    "report_dates": report_dates,
                    "source_url": source_urls[0],
                    "source_funds": sorted(
                        target["source_funds"],
                        key=lambda item: item["portfolio_exposure_pct"],
                        reverse=True,
                    ),
                }
            )
        return sorted(
            items,
            key=lambda item: item["portfolio_exposure_pct"],
            reverse=True,
        )

    @staticmethod
    def _fund_overlaps(
        *,
        holdings: list[dict[str, Any]],
        fund_security_weights: dict[str, dict[tuple[str, str], dict[str, Any]]],
    ) -> list[dict[str, Any]]:
        """逐基金两两比较共同证券，既返回基金内重合，也返回组合实际暴露。"""

        pairs = []
        for left, right in combinations(holdings, 2):
            left_rows = fund_security_weights.get(left["fund_code"], {})
            right_rows = fund_security_weights.get(right["fund_code"], {})
            common_keys = set(left_rows).intersection(right_rows)
            common_items = []
            overlap_pct = 0.0
            combined_exposure = 0.0
            for key in common_keys:
                left_weight = float(left_rows[key]["weight_pct"])
                right_weight = float(right_rows[key]["weight_pct"])
                overlap_pct += min(left_weight, right_weight)
                contribution = (
                    float(left["weight_pct"]) * left_weight / 100
                    + float(right["weight_pct"]) * right_weight / 100
                )
                combined_exposure += contribution
                common_items.append(
                    {
                        "asset_type": key[0],
                        "security_code": key[1],
                        "security_name": left_rows[key]["security_name"],
                        "combined_portfolio_exposure_pct": round(contribution, 4),
                    }
                )
            common_items.sort(
                key=lambda item: item["combined_portfolio_exposure_pct"],
                reverse=True,
            )
            pairs.append(
                {
                    "left_fund_code": left["fund_code"],
                    "left_fund_name": left["short_name"],
                    "right_fund_code": right["fund_code"],
                    "right_fund_name": right["short_name"],
                    "common_security_count": len(common_keys),
                    "fund_overlap_pct": round(overlap_pct, 4),
                    "combined_portfolio_exposure_pct": round(
                        combined_exposure,
                        4,
                    ),
                    "top_common_securities": common_items[:5],
                }
            )
        return sorted(
            pairs,
            key=lambda item: (
                item["combined_portfolio_exposure_pct"],
                item["common_security_count"],
            ),
            reverse=True,
        )

    @staticmethod
    def _security_concentration(
        *,
        exposures: list[dict[str, Any]],
        coverage_pct: float,
    ) -> dict[str, Any]:
        known_total = sum(item["portfolio_exposure_pct"] for item in exposures)
        hhi = (
            sum((item["portfolio_exposure_pct"] / known_total) ** 2 for item in exposures)
            if known_total > 0
            else 0.0
        )
        top = exposures[0] if exposures else None
        top_pct = float(top["portfolio_exposure_pct"]) if top else 0.0
        if coverage_pct < 20:
            state = "insufficient"
        elif top_pct >= 15 or hhi >= 0.15:
            state = "high"
        elif top_pct >= 8 or hhi >= 0.08:
            state = "moderate"
        else:
            state = "diversified"
        return {
            "known_security_hhi": round(hhi, 4),
            "known_security_count": len(exposures),
            "top_security_exposure_pct": round(top_pct, 4),
            "top_security_code": top["security_code"] if top else None,
            "top_security_name": top["security_name"] if top else None,
            "security_state": state,
        }

    @staticmethod
    def _industry_concentration(
        *,
        exposures: list[dict[str, Any]],
        coverage_pct: float,
    ) -> dict[str, Any]:
        top = exposures[0] if exposures else None
        top_pct = float(top["portfolio_exposure_pct"]) if top else 0.0
        if coverage_pct < 30:
            state = "insufficient"
        elif top_pct >= 45:
            state = "high"
        elif top_pct >= 25:
            state = "moderate"
        else:
            state = "diversified"
        return {
            "top_industry_exposure_pct": round(top_pct, 4),
            "top_industry_name": top["industry_name"] if top else None,
            "industry_state": state,
        }

    @staticmethod
    def _overall_state(security_state: str, industry_state: str) -> str:
        states = {security_state, industry_state}
        if "high" in states:
            return "high"
        if "moderate" in states:
            return "moderate"
        if states == {"insufficient"}:
            return "insufficient"
        if "insufficient" in states:
            return "partial"
        return "diversified"

    @staticmethod
    def _data_quality(
        *,
        security_coverage: float,
        industry_asset_coverage: float,
        fund_reports: list[dict[str, Any]],
        unavailable_codes: list[str],
        warnings: list[str],
    ) -> dict[str, Any]:
        freshness_rank = {"fresh": 0, "aging": 1, "stale": 2, "unavailable": 3}
        statuses = [item["freshness_status"] for item in fund_reports]
        freshness = max(statuses, key=lambda item: freshness_rank[item])
        if len(unavailable_codes) == len(fund_reports):
            status = "unavailable"
        elif unavailable_codes or security_coverage < 30:
            status = "limited"
        elif security_coverage < 70:
            status = "partial"
        else:
            status = "complete"
        confidence = (
            "high"
            if status == "complete" and freshness == "fresh"
            else "medium"
            if status in {"complete", "partial"} and freshness != "stale"
            else "low"
        )
        return {
            "status": status,
            "confidence": confidence,
            "security_coverage_pct": round(security_coverage, 4),
            "industry_asset_coverage_pct": round(industry_asset_coverage, 4),
            "unpenetrated_security_pct": round(
                max(0.0, 100.0 - min(100.0, security_coverage)),
                4,
            ),
            "freshness_status": freshness,
            "unavailable_fund_codes": sorted(unavailable_codes),
            "warnings": list(dict.fromkeys(warnings))[:12],
        }


portfolio_exposure_service = PortfolioExposureService()
