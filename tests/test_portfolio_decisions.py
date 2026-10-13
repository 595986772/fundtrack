"""投资决策快照、动态结果和人工复盘的持久化测试。"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
import tempfile
import unittest

from app.database import Database
from app.services.portfolio_decisions import (
    PortfolioDecisionError,
    PortfolioDecisionService,
)


class FakePortfolios:
    portfolio = {
        "id": "portfolio_1234567890abcdef",
        "name": "学习组合",
        "position_mode": "allocation",
        "holdings": [
            {
                "fund_code": "110020",
                "short_name": "基金A",
                "weight_pct": 100.0,
            }
        ],
    }

    def get(self, *, user_id, portfolio_id):
        if user_id != "chen" or portfolio_id != self.portfolio["id"]:
            raise RuntimeError("组合不可见")
        return dict(self.portfolio)


class FakePerformance:
    def analyze(self, *, user_id, portfolio_id, months):
        FakePortfolios().get(user_id=user_id, portfolio_id=portfolio_id)
        today = date.today()
        earlier = today - timedelta(days=10)
        return {
            "period_end": today.isoformat(),
            "twr_pct": 8.0,
            "xirr_pct": None,
            "annualized_volatility_pct": 12.0,
            "max_drawdown": {"drawdown_pct": -6.0},
            "sharpe_ratio": 0.6,
            "sortino_ratio": 0.8,
            "benchmark": {"status": "matched", "excess_return_pct": 2.0},
            "series": [
                {
                    "date": earlier.isoformat(),
                    "portfolio_index": 100.0,
                    "benchmark_index": 100.0,
                },
                {
                    "date": today.isoformat(),
                    "portfolio_index": 108.0,
                    "benchmark_index": 106.0,
                },
            ],
        }


class FakeAssessments:
    def assess(self, *, user_id, portfolio_id, months):
        FakePortfolios().get(user_id=user_id, portfolio_id=portfolio_id)
        return {
            "assessment_date": date.today(),
            "state_code": "balanced",
            "state_label": "风险收益均衡",
            "summary": "风险与支撑证据暂时均衡。",
            "risk_evidence": [{"evidence_id": "risk:test"}],
            "supporting_evidence": [{"evidence_id": "support:test"}],
        }


class PortfolioDecisionTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = Database(
            f"sqlite:///{Path(self.temp_dir.name) / 'decisions.db'}"
        )
        self.service = PortfolioDecisionService(
            self.database,
            portfolios=FakePortfolios(),
            performance=FakePerformance(),
            assessments=FakeAssessments(),
        )

    def tearDown(self):
        self.database.engine.dispose()
        self.temp_dir.cleanup()

    def test_snapshot_stays_frozen_and_review_updates_only_review_fields(self):
        report = {
            "headline": "组合风险仍可控",
            "summary": "保留观察。",
            "thread_id": "portfolio-research:test",
            "model_interpretation": {
                "conditional_view": "条件不变时维持持有。",
                "confidence": 0.72,
            },
            "key_findings": [
                {
                    "evidence_references": [
                        {"evidence_id": "risk:drawdown:test"}
                    ]
                }
            ],
        }
        created = self.service.create(
            user_id="chen",
            portfolio_id=FakePortfolios.portfolio["id"],
            action="hold",
            thesis="风险仍在承受范围，继续观察消息面",
            horizon_days=90,
            research_job_id="research_job_1",
            research_report=report,
        )
        original_snapshot = created["snapshot"]

        reviewed = self.service.review(
            user_id="chen",
            portfolio_id=FakePortfolios.portfolio["id"],
            decision_id=created["id"],
            review_note="证据仍有效，但需要继续观察回撤。",
        )

        self.assertEqual(reviewed["snapshot"], original_snapshot)
        self.assertEqual(reviewed["outcome"]["status"], "reviewed")
        self.assertEqual(
            reviewed["snapshot"]["research"]["evidence_ids"],
            ["risk:drawdown:test"],
        )
        self.assertEqual(len(self.service.list(
            user_id="chen",
            portfolio_id=FakePortfolios.portfolio["id"],
        )), 1)

    def test_delete_and_validation(self):
        with self.assertRaises(PortfolioDecisionError):
            self.service.create(
                user_id="chen",
                portfolio_id=FakePortfolios.portfolio["id"],
                action="guess",
                thesis="一个非法动作测试",
                horizon_days=30,
            )
        created = self.service.create(
            user_id="chen",
            portfolio_id=FakePortfolios.portfolio["id"],
            action="watch",
            thesis="等待更多市场事件和净值数据",
            horizon_days=30,
        )
        self.service.delete(
            user_id="chen",
            portfolio_id=FakePortfolios.portfolio["id"],
            decision_id=created["id"],
        )
        self.assertEqual(self.service.list(
            user_id="chen",
            portfolio_id=FakePortfolios.portfolio["id"],
        ), [])


if __name__ == "__main__":
    unittest.main()
