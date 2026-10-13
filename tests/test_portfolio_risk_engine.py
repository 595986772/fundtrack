"""真实 Riskfolio-Lib 适配层的确定性测试。

运行方式：

    conda run --no-capture-output -n dl \
      python -m unittest tests.test_portfolio_risk_engine -v

测试使用人工净值，不访问数据库、网络或大模型，但会真实调用已安装的 Riskfolio-Lib。
"""

from __future__ import annotations

import unittest
from datetime import date, timedelta

from app.services.portfolio_risk_engine import (
    PortfolioRiskEngine,
    PortfolioRiskEngineError,
)


PORTFOLIO_ID = "portfolio_0123456789abcdef"


class FakePortfolioService:
    def get(self, *, user_id: str, portfolio_id: str) -> dict:
        return {
            "id": portfolio_id,
            "user_id": user_id,
            "name": "量化风险测试组合",
            "holdings": [
                {
                    "fund_code": "FUND_A",
                    "short_name": "权益样本",
                    "weight_pct": 60.0,
                },
                {
                    "fund_code": "FUND_B",
                    "short_name": "固收样本",
                    "weight_pct": 40.0,
                },
            ],
        }


class FakeFundCatalog:
    """生成有涨有跌的净值，并故意让第二只基金缺少一个交易日。"""

    def __init__(self) -> None:
        dates = self._business_dates(start=date(2025, 9, 1), count=180)
        self.details = {
            "FUND_A": self._detail(
                code="FUND_A",
                dates=dates,
                daily_pattern=[0.012, -0.009, 0.004, -0.006, 0.008],
            ),
            "FUND_B": self._detail(
                code="FUND_B",
                dates=[value for index, value in enumerate(dates) if index != 45],
                daily_pattern=[0.003, -0.002, 0.002, -0.001, 0.001],
            ),
        }

    def get_fund(self, fund_code: str) -> dict:
        return self.details[fund_code]

    @staticmethod
    def _business_dates(*, start: date, count: int) -> list[date]:
        values: list[date] = []
        current = start
        while len(values) < count:
            if current.weekday() < 5:
                values.append(current)
            current += timedelta(days=1)
        return values

    @staticmethod
    def _detail(
        *,
        code: str,
        dates: list[date],
        daily_pattern: list[float],
    ) -> dict:
        nav = 1.0
        points = []
        for index, nav_date in enumerate(dates):
            if index:
                nav *= 1 + daily_pattern[(index - 1) % len(daily_pattern)]
            points.append(
                {
                    "nav_date": nav_date.isoformat(),
                    "unit_nav": round(nav, 8),
                }
            )
        return {
            "profile": {"fund_code": code, "short_name": code},
            "source": "人工测试净值",
            "data_mode": "fixture",
            "snapshot_end": dates[-1].isoformat(),
            "nav_points": points,
            "corporate_actions": [],
            "corporate_action_coverage": {
                "intervals": [
                    {
                        "coverage_start": dates[0].isoformat(),
                        "coverage_end": dates[-1].isoformat(),
                    }
                ]
            },
        }


class PortfolioRiskEngineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.engine = PortfolioRiskEngine(
            portfolios=FakePortfolioService(),
            funds=FakeFundCatalog(),
        )

    def test_analyze_uses_aligned_returns_and_real_riskfolio_metrics(self) -> None:
        result = self.engine.analyze(
            user_id="chen",
            portfolio_id=PORTFOLIO_ID,
            months=6,
        )

        self.assertEqual(result["data_kind"], "quantitative_portfolio_risk")
        self.assertEqual(result["engine"]["name"], "Riskfolio-Lib")
        self.assertEqual(result["return_basis"], "total_return_reinvested")
        self.assertEqual(result["return_basis_status"], "verified")
        self.assertNotEqual(result["engine"]["version"], "unknown")
        self.assertGreater(result["return_observation_count"], 20)
        self.assertGreater(result["annualized_volatility_pct"], 0)
        self.assertGreater(result["daily_cvar_95_pct"], 0)
        self.assertGreater(result["max_drawdown"]["drawdown_pct"], 0)
        self.assertEqual(len(result["risk_contributions"]), 2)

        # MV 风险贡献相加应回到组合年化波动率；输出经过四位/两位小数舍入，留少量误差。
        contribution_total = sum(
            item["annualized_risk_contribution_pct_points"]
            for item in result["risk_contributions"]
        )
        self.assertAlmostEqual(
            contribution_total,
            result["annualized_volatility_pct"],
            delta=0.03,
        )
        absolute_share_total = sum(
            item["absolute_risk_share_pct"]
            for item in result["risk_contributions"]
        )
        self.assertAlmostEqual(absolute_share_total, 100.0, delta=0.02)
        self.assertIsNotNone(result["correlation"]["strongest_pair"])

    def test_research_snapshot_can_recalculate_candidate_from_frozen_returns(self) -> None:
        public_result = self.engine.analyze(
            user_id="chen",
            portfolio_id=PORTFOLIO_ID,
            months=6,
        )
        frozen_result = self.engine.analyze_for_research_snapshot(
            user_id="chen",
            portfolio_id=PORTFOLIO_ID,
            months=6,
        )

        self.assertNotIn("_scenario_validation_inputs", public_result)
        scenario_inputs = frozen_result["_scenario_validation_inputs"]
        self.assertEqual(
            scenario_inputs["return_observation_count"],
            frozen_result["return_observation_count"],
        )

        baseline = self.engine.evaluate_frozen_weights(
            scenario_inputs=scenario_inputs,
            weights_pct={"FUND_A": 60.0, "FUND_B": 40.0},
        )
        candidate = self.engine.evaluate_frozen_weights(
            scenario_inputs=scenario_inputs,
            weights_pct={"FUND_A": 50.0, "FUND_B": 50.0},
        )

        self.assertAlmostEqual(
            baseline["annualized_volatility_pct"],
            frozen_result["annualized_volatility_pct"],
            delta=0.01,
        )
        self.assertLess(
            candidate["annualized_volatility_pct"],
            baseline["annualized_volatility_pct"],
        )
        self.assertEqual(
            candidate["return_observation_count"],
            baseline["return_observation_count"],
        )

    def test_analyze_rejects_unsupported_period(self) -> None:
        with self.assertRaisesRegex(PortfolioRiskEngineError, "6、12 或 36"):
            self.engine.analyze(
                user_id="chen",
                portfolio_id=PORTFOLIO_ID,
                months=24,
            )

    def test_aligned_indexes_remove_dividend_drop_before_portfolio_returns(self) -> None:
        """组合层不能重新对单位净值 pct_change，绕过单基金的分红复权。"""

        dates = [date(2026, 1, 1) + timedelta(days=index) for index in range(30)]
        event_date = dates[10]

        class DividendCatalog:
            def get_fund(self, fund_code: str) -> dict:
                values = (
                    [2.0] * 10 + [1.8] * 20
                    if fund_code == "FUND_A"
                    else [1.0 + index * 0.001 for index in range(30)]
                )
                actions = (
                    [
                        {
                            "action_date": event_date.isoformat(),
                            "action_type": "cash_dividend",
                            "cash_dividend_per_unit": 0.2,
                            "split_ratio": None,
                        }
                    ]
                    if fund_code == "FUND_A"
                    else []
                )
                return {
                    "source": "unit-test",
                    "data_mode": "fixture",
                    "snapshot_end": dates[-1].isoformat(),
                    "nav_points": [
                        {
                            "nav_date": nav_date.isoformat(),
                            "unit_nav": value,
                        }
                        for nav_date, value in zip(dates, values)
                    ],
                    "corporate_actions": actions,
                    "corporate_action_coverage": {
                        "intervals": [
                            {
                                "coverage_start": dates[0].isoformat(),
                                "coverage_end": dates[-1].isoformat(),
                            }
                        ]
                    },
                }

        engine = PortfolioRiskEngine(
            portfolios=FakePortfolioService(),
            funds=DividendCatalog(),
        )
        indexes, metadata = engine._aligned_return_indexes(
            holdings=FakePortfolioService().get(
                user_id="chen",
                portfolio_id=PORTFOLIO_ID,
            )["holdings"]
        )

        event_return = indexes["FUND_A"].pct_change().loc[event_date.isoformat()]
        self.assertAlmostEqual(float(event_return), 0.0)
        self.assertTrue(
            all(item["return_basis_status"] == "verified" for item in metadata)
        )


if __name__ == "__main__":
    unittest.main()
