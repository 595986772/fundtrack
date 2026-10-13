"""模拟组合持久化、确定性研判和 Agent Tool 的完整回归测试。

测试数据库使用临时 SQLite 文件，不会污染开发者真实组合；单基金事实、风险、市场和
事件继续复用项目冻结快照，因此同一输入每次都能得到相同结果。
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import date
import json
from pathlib import Path
import tempfile
from threading import Barrier
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.database import Database
from app.main import app
from app.services.portfolio_assessment import PortfolioAssessmentService
from app.services.portfolio_agent_tools import get_portfolio_assessment_tool
from app.services.portfolios import (
    PortfolioIdempotencyConflictError,
    PortfolioNotFoundError,
    PortfolioService,
    PortfolioValidationError,
)
from app.services.prompt_builder import prompt_builder_service
from app.services.eval_context import use_frozen_eval_data
from app.services.tool_permissions import check_tool_permission
from app.services.agent_tools import get_agent_tools


SAMPLE_HOLDINGS = [
    {"fund_code": "110020", "weight_pct": 40},
    {"fund_code": "161725", "weight_pct": 35},
    {"fund_code": "110007", "weight_pct": 25},
]


class TemporaryPortfolioTestCase(unittest.TestCase):
    """每条测试拥有自己的数据库文件，便于验证真实 SQLAlchemy 行为。"""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        database_path = Path(self.temp_dir.name) / "portfolios.db"
        self.database = Database(f"sqlite:///{database_path}")
        self.portfolios = PortfolioService(self.database)
        self.assessments = PortfolioAssessmentService(portfolios=self.portfolios)

    def tearDown(self) -> None:
        self.database.engine.dispose()
        self.temp_dir.cleanup()

    def create_sample(self, *, user_id: str = "chen") -> dict:
        return self.portfolios.create(
            user_id=user_id,
            name="稳健观察组合",
            holdings=SAMPLE_HOLDINGS,
        )


class PortfolioServiceTests(TemporaryPortfolioTestCase):
    def test_convert_allocation_to_units_keeps_id_and_computes_money(self) -> None:
        """旧组合只换事实源，风险/Agent 引用的组合 ID 不变，也不再要求手填比例。"""

        original = self.create_sample()
        converted = self.portfolios.update(
            user_id="chen", portfolio_id=original["id"], name="真实持仓",
            holdings=[
                {"fund_code": "110020", "units": 200, "cost_amount": 300,
                 "purchase_date": date(2025, 1, 2)},
                {"fund_code": "110007", "units": 100, "cost_amount": 120,
                 "purchase_date": date(2025, 1, 2)},
            ],
        )
        self.assertEqual(converted["id"], original["id"])
        self.assertEqual(converted["position_mode"], "actual")
        self.assertEqual(converted["total_cost_amount"], 420)
        self.assertEqual(converted["holding_count"], 2)
        total_value = 0
        for holding in converted["holdings"]:
            value = holding["units"] * holding["latest_nav"]
            total_value += round(value, 2)
            self.assertEqual(holding["current_value"], round(value, 2))
            self.assertEqual(holding["total_pnl_amount"], round(value - holding["cost_amount"], 2))
        self.assertEqual(converted["total_market_value"], round(total_value, 2))
        self.assertEqual(converted["total_pnl_amount"], round(total_value - 420, 2))
        self.assertEqual(len(self.portfolios.list_transactions(
            user_id="chen", portfolio_id=original["id"],
        )), 2)

        # 转换重复提交不能再次写入初始买入，更不能覆盖后续账本。
        with self.assertRaises(PortfolioValidationError):
            self.portfolios.update(
                user_id="chen", portfolio_id=original["id"], name="重复转换",
                holdings=[{"fund_code": "110020", "units": 10, "cost_amount": 20,
                           "purchase_date": date(2025, 1, 2)}],
            )
        self.assertEqual(len(self.portfolios.list_transactions(
            user_id="chen", portfolio_id=original["id"],
        )), 2)

    def test_incomplete_conversion_does_not_remove_original_allocation(self) -> None:
        original = self.create_sample()
        with self.assertRaises(PortfolioValidationError):
            self.portfolios.update(
                user_id="chen", portfolio_id=original["id"], name="未完成",
                holdings=[{"fund_code": "110020", "units": 100}],
            )
        current = self.portfolios.get(user_id="chen", portfolio_id=original["id"])
        self.assertEqual(current["position_mode"], "allocation")
        self.assertEqual(current["holdings"], original["holdings"])
        self.assertEqual(self.portfolios.list_transactions(
            user_id="chen", portfolio_id=original["id"],
        ), [])

    def test_cumulative_pnl_includes_realized_losses_even_after_selling_all(self) -> None:
        """浮动盈亏随净值变，已实现盈亏不能在清仓后消失。"""

        portfolio = self.portfolios.create(
            user_id="chen", name="盈亏账本",
            holdings=[{"fund_code": "110020", "units": 100, "cost_amount": 100,
                       "purchase_date": date(2025, 1, 2)}],
        )
        self.portfolios.add_transaction(
            user_id="chen", portfolio_id=portfolio["id"], fund_code="110020",
            transaction_type="sell", trade_date=date(2025, 2, 3),
            units=40, unit_price=0.8, fee=2, idempotency_key="pnl-partial-sell",
        )
        current = self.portfolios.get(user_id="chen", portfolio_id=portfolio["id"])
        holding = current["holdings"][0]
        self.assertEqual(holding["units"], 60)
        self.assertEqual(holding["cost_amount"], 60)
        self.assertEqual(holding["realized_pnl_amount"], -10)
        self.assertAlmostEqual(holding["total_pnl_amount"], holding["unrealized_pnl_amount"] - 10, places=2)
        self.assertEqual(current["total_pnl_amount"], holding["total_pnl_amount"])

        self.portfolios.add_transaction(
            user_id="chen", portfolio_id=portfolio["id"], fund_code="110020",
            transaction_type="sell", trade_date=date(2025, 2, 4),
            units=60, unit_price=1.5, fee=0, idempotency_key="pnl-complete-sell",
        )
        closed = self.portfolios.get(user_id="chen", portfolio_id=portfolio["id"])
        self.assertEqual(closed["holdings"], [])
        self.assertEqual(closed["unrealized_pnl_amount"], 0)
        self.assertEqual(closed["realized_pnl_amount"], 20)
        self.assertEqual(closed["total_pnl_amount"], 20)

    def test_crud_persists_holdings_and_isolates_users(self) -> None:
        created = self.create_sample()

        self.assertTrue(created["id"].startswith("portfolio_"))
        self.assertEqual(created["holding_count"], 3)
        self.assertEqual(created["total_weight_pct"], 100.0)
        self.assertEqual(len(self.portfolios.list(user_id="chen")), 1)
        self.assertEqual(self.portfolios.list(user_id="other"), [])
        with self.assertRaises(PortfolioNotFoundError):
            self.portfolios.get(user_id="other", portfolio_id=created["id"])

        updated = self.portfolios.update(
            user_id="chen",
            portfolio_id=created["id"],
            name="调整后的组合",
            holdings=[
                {"fund_code": "110020", "weight_pct": 60},
                {"fund_code": "110007", "weight_pct": 40},
            ],
        )
        self.assertEqual(updated["name"], "调整后的组合")
        self.assertEqual(updated["holding_count"], 2)

        self.portfolios.delete(user_id="chen", portfolio_id=created["id"])
        self.assertEqual(self.portfolios.list(user_id="chen"), [])

    def test_validation_rejects_bad_total_duplicate_and_invalid_number(self) -> None:
        invalid_cases = [
            [
                {"fund_code": "110020", "weight_pct": 50},
                {"fund_code": "110007", "weight_pct": 40},
            ],
            [
                {"fund_code": "110020", "weight_pct": 50},
                {"fund_code": "110020", "weight_pct": 50},
            ],
            [{"fund_code": "110020", "weight_pct": "not-a-number"}],
        ]

        for holdings in invalid_cases:
            with self.subTest(holdings=holdings):
                with self.assertRaises(PortfolioValidationError):
                    self.portfolios.create(
                        user_id="chen",
                        name="非法组合",
                        holdings=holdings,
                    )

    def test_actual_positions_replay_transactions_with_average_cost(self) -> None:
        portfolio = self.portfolios.create(
            user_id="chen",
            name="真实持仓",
            holdings=[
                {
                    "fund_code": "110020",
                    "units": 100,
                    "cost_amount": 100,
                    "purchase_date": date(2025, 1, 2),
                }
            ],
        )

        self.assertEqual(portfolio["position_mode"], "actual")
        self.assertEqual(portfolio["holdings"][0]["average_cost_nav"], 1.0)
        self.assertEqual(
            len(
                self.portfolios.list_transactions(
                    user_id="chen",
                    portfolio_id=portfolio["id"],
                )
            ),
            1,
        )

        self.portfolios.add_transaction(
            user_id="chen",
            portfolio_id=portfolio["id"],
            fund_code="110020",
            transaction_type="buy",
            trade_date=date(2025, 2, 3),
            units=100,
            unit_price=2,
            idempotency_key="average-buy-20250203",
        )
        sell = self.portfolios.add_transaction(
            user_id="chen",
            portfolio_id=portfolio["id"],
            fund_code="110020",
            transaction_type="sell",
            trade_date=date(2025, 3, 4),
            units=50,
            unit_price=2,
            idempotency_key="average-sell-20250304",
        )
        current = self.portfolios.get(
            user_id="chen",
            portfolio_id=portfolio["id"],
        )
        holding = current["holdings"][0]

        # 100 元 / 100 份，再以 2 元买 100 份，移动平均成本变为 1.5 元。
        # 卖出 50 份释放 75 元成本，收入 100 元，因此已实现盈利 25 元。
        self.assertEqual(holding["units"], 150.0)
        self.assertEqual(holding["cost_amount"], 225.0)
        self.assertEqual(holding["average_cost_nav"], 1.5)
        self.assertEqual(holding["realized_pnl_amount"], 25.0)
        self.assertEqual(sell["realized_pnl_amount"], 25.0)
        self.assertEqual(sell["running_units"], 150.0)

        with self.assertRaises(PortfolioValidationError):
            self.portfolios.update(
                user_id="chen",
                portfolio_id=portfolio["id"],
                name="不允许覆盖流水",
                holdings=SAMPLE_HOLDINGS,
            )

    def test_invalid_transaction_and_history_correction_are_atomic(self) -> None:
        portfolio = self.portfolios.create(
            user_id="chen",
            name="回滚测试",
            holdings=[
                {
                    "fund_code": "110020",
                    "units": 100,
                    "cost_amount": 100,
                    "purchase_date": date(2025, 1, 2),
                }
            ],
        )
        self.portfolios.add_transaction(
            user_id="chen",
            portfolio_id=portfolio["id"],
            fund_code="110020",
            transaction_type="sell",
            trade_date=date(2025, 2, 3),
            units=80,
            unit_price=1.5,
            idempotency_key="rollback-sell-20250203",
        )
        transactions = self.portfolios.list_transactions(
            user_id="chen",
            portfolio_id=portfolio["id"],
        )
        opening = next(item for item in transactions if item["note"] == "初始持仓")

        # 删除首笔买入后，后续卖出会变成历史超卖；事务必须回滚，原流水仍在。
        with self.assertRaises(PortfolioValidationError):
            self.portfolios.delete_transaction(
                user_id="chen",
                portfolio_id=portfolio["id"],
                transaction_id=opening["id"],
            )
        remaining = self.portfolios.list_transactions(
            user_id="chen",
            portfolio_id=portfolio["id"],
        )
        self.assertEqual(len(remaining), 2)

        with self.assertRaises(PortfolioValidationError):
            self.portfolios.add_transaction(
                user_id="chen",
                portfolio_id=portfolio["id"],
                fund_code="110020",
                transaction_type="sell",
                trade_date=date(2025, 3, 4),
                units=30,
                unit_price=1.5,
                idempotency_key="invalid-sell-20250304",
            )

    def test_duplicate_transaction_key_replays_without_double_booking(self) -> None:
        """网络重试应返回原交易，不能再次增加份额或成本。"""

        portfolio = self.portfolios.create(
            user_id="chen",
            name="幂等记账测试",
            holdings=[
                {
                    "fund_code": "110020",
                    "units": 100,
                    "cost_amount": 100,
                    "purchase_date": date(2025, 1, 2),
                }
            ],
        )
        request = {
            "user_id": "chen",
            "portfolio_id": portfolio["id"],
            "fund_code": "110020",
            "transaction_type": "buy",
            "trade_date": date(2025, 2, 3),
            "units": 10,
            "unit_price": 1.2,
            "fee": 0.5,
            "note": "网络重试",
            "idempotency_key": "duplicate-buy-20250203",
        }

        first = self.portfolios.add_transaction(**request)
        replay = self.portfolios.add_transaction(**request)
        current = self.portfolios.get(
            user_id="chen",
            portfolio_id=portfolio["id"],
        )
        transactions = self.portfolios.list_transactions(
            user_id="chen",
            portfolio_id=portfolio["id"],
        )

        self.assertEqual(first["id"], replay["id"])
        self.assertFalse(first["idempotency_replayed"])
        self.assertTrue(replay["idempotency_replayed"])
        self.assertEqual(replay["idempotency_key"], request["idempotency_key"])
        self.assertEqual(len(transactions), 2)  # 初始持仓 + 本次买入
        self.assertEqual(current["holdings"][0]["units"], 110.0)
        self.assertEqual(current["holdings"][0]["cost_amount"], 112.5)

    def test_same_transaction_key_with_different_payload_is_conflict(self) -> None:
        """幂等键不是覆盖开关，另一笔交易误用旧 key 必须明确失败。"""

        portfolio = self.portfolios.create(
            user_id="chen",
            name="幂等冲突测试",
            holdings=[
                {
                    "fund_code": "110020",
                    "units": 100,
                    "cost_amount": 100,
                    "purchase_date": date(2025, 1, 2),
                }
            ],
        )
        request = {
            "user_id": "chen",
            "portfolio_id": portfolio["id"],
            "fund_code": "110020",
            "transaction_type": "buy",
            "trade_date": date(2025, 2, 3),
            "units": 10,
            "unit_price": 1.2,
            "idempotency_key": "conflicting-buy-20250203",
        }
        self.portfolios.add_transaction(**request)

        with self.assertRaises(PortfolioIdempotencyConflictError):
            self.portfolios.add_transaction(**{**request, "units": 20})

        current = self.portfolios.get(
            user_id="chen",
            portfolio_id=portfolio["id"],
        )
        self.assertEqual(current["holdings"][0]["units"], 110.0)

    def test_concurrent_duplicate_transactions_create_one_ledger_entry(self) -> None:
        """模拟多个 API Worker 同时收到同一请求，由数据库唯一索引最终仲裁。"""

        portfolio = self.portfolios.create(
            user_id="chen",
            name="并发幂等测试",
            holdings=[
                {
                    "fund_code": "110020",
                    "units": 100,
                    "cost_amount": 100,
                    "purchase_date": date(2025, 1, 2),
                }
            ],
        )
        worker_count = 4
        barrier = Barrier(worker_count)

        def submit_once() -> dict:
            barrier.wait()
            return self.portfolios.add_transaction(
                user_id="chen",
                portfolio_id=portfolio["id"],
                fund_code="110020",
                transaction_type="buy",
                trade_date=date(2025, 2, 3),
                units=10,
                unit_price=1.2,
                idempotency_key="concurrent-buy-20250203",
            )

        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            results = list(executor.map(lambda _: submit_once(), range(worker_count)))

        self.assertEqual(len({result["id"] for result in results}), 1)
        self.assertEqual(
            sum(not result["idempotency_replayed"] for result in results),
            1,
        )
        transactions = self.portfolios.list_transactions(
            user_id="chen",
            portfolio_id=portfolio["id"],
        )
        current = self.portfolios.get(
            user_id="chen",
            portfolio_id=portfolio["id"],
        )
        self.assertEqual(len(transactions), 2)
        self.assertEqual(current["holdings"][0]["units"], 110.0)

    def test_actual_weights_follow_latest_nav_on_every_read(self) -> None:
        portfolio = self.portfolios.create(
            user_id="chen",
            name="动态市值权重",
            holdings=[
                {
                    "fund_code": "110020",
                    "units": 100,
                    "cost_amount": 100,
                    "purchase_date": date(2025, 1, 2),
                },
                {
                    "fund_code": "110007",
                    "units": 100,
                    "cost_amount": 100,
                    "purchase_date": date(2025, 1, 2),
                },
            ],
        )

        from app.services import portfolios as portfolio_module

        original_get_fund = portfolio_module.fund_catalog_service.get_fund

        def get_fund_with_changed_nav(fund_code: str) -> dict:
            detail = deepcopy(original_get_fund(fund_code))
            detail["latest_nav"] = 2.0 if fund_code == "110020" else 1.0
            return detail

        # 模拟夜间同步后净值变化，但期间没有新增交易；读取仍应得到 2:1 的市值权重。
        with patch.object(
            portfolio_module.fund_catalog_service,
            "get_fund",
            side_effect=get_fund_with_changed_nav,
        ):
            current = self.portfolios.get(
                user_id="chen",
                portfolio_id=portfolio["id"],
            )

        weights = {
            holding["fund_code"]: holding["weight_pct"]
            for holding in current["holdings"]
        }
        self.assertEqual(weights, {"110020": 66.67, "110007": 33.33})
        self.assertEqual(current["total_weight_pct"], 100.0)
        self.assertEqual(current["total_cost_amount"], 200)
        self.assertEqual(current["total_market_value"], 300)
        self.assertEqual(current["total_pnl_amount"], 100)


class PortfolioAssessmentTests(TemporaryPortfolioTestCase):
    def test_actual_position_weights_are_not_described_as_target_allocation(self) -> None:
        from app.schemas import PortfolioAssessmentResponse
        from app.services.portfolio_agent_tools import _bounded_portfolio_result

        with use_frozen_eval_data():
            portfolio = self.portfolios.create(user_id="chen", name="份额口径验收", holdings=[
                {"fund_code": "110020", "units": 1000, "cost_amount": 1500,
                 "purchase_date": date(2025, 8, 25)},
                {"fund_code": "110007", "units": 1000, "cost_amount": 1800,
                 "purchase_date": date(2025, 8, 25)},
            ])
            result = self.assessments.assess(user_id="chen", portfolio_id=portfolio["id"])
        # 同时检查 API 和 Tool 的返回，避免只修 REST 展示、模型仍拿到旧描述。
        for payload in (PortfolioAssessmentResponse.model_validate(result).model_dump(),
                        _bounded_portfolio_result(result)):
            self.assertEqual(payload["position_mode"], "actual")
            self.assertEqual(payload["weight_basis"], "current_position_market_value")
            self.assertNotIn("模拟目标权重", " ".join(payload["missing_information"]))
            self.assertEqual({item["fund_code"]: item["weight_pct"] for item in payload["fund_contributions"]},
                {item["fund_code"]: item["weight_pct"] for item in portfolio["holdings"]})

    def test_assessment_calculates_concentration_and_weighted_contributions(self) -> None:
        portfolio = self.create_sample()
        # 组合状态依赖市场与事件数据；确定性单测必须使用冻结 fixture，不能被本机刚刚
        # 同步的实时事件改变预期标签。
        with use_frozen_eval_data():
            result = self.assessments.assess(
                user_id="chen",
                portfolio_id=portfolio["id"],
                months=12,
            )

        self.assertEqual(result["data_kind"], "deterministic_portfolio_assessment")
        self.assertEqual(result["weight_basis"], "target_allocation")
        self.assertIn("模拟目标权重", " ".join(result["missing_information"]))
        self.assertEqual(result["state_code"], "cautious")
        self.assertEqual(result["concentration"]["hhi"], 0.345)
        self.assertEqual(result["concentration"]["effective_fund_count"], 2.9)
        self.assertEqual(result["concentration"]["max_weight_pct"], 40.0)
        self.assertAlmostEqual(
            sum(item["weight_pct"] for item in result["allocations"]),
            100.0,
        )
        self.assertEqual(len(result["fund_contributions"]), 3)

        # 股票市场证据覆盖宽基和行业基金，债券基金仅把它作为不计分背景。
        all_evidence = [
            *result["risk_evidence"],
            *result["supporting_evidence"],
            *result["neutral_evidence"],
        ]
        self.assertEqual(
            len({item["evidence_id"] for item in all_evidence}),
            len(all_evidence),
        )
        self.assertTrue(
            any(item["portfolio_weight_coverage_pct"] == 75.0 for item in all_evidence)
        )
        self.assertTrue(
            any("不计算真实组合波动率" in item for item in result["missing_information"])
        )


class PortfolioApiTests(TemporaryPortfolioTestCase):
    def test_api_converts_existing_portfolio_without_supplying_weights(self) -> None:
        from app.routers import portfolios as portfolio_router

        original = self.create_sample(user_id="api-user")
        payload = {"user_id": "api-user", "name": "按份额记账", "holdings": [
            {"fund_code": "110020", "units": 500.5, "cost_amount": 600.6,
             "purchase_date": "2025-01-02"},
        ]}
        with patch.object(portfolio_router, "portfolio_service", self.portfolios):
            url = f"/api/v1/portfolios/{original['id']}"
            response = self.client.put(url, json=payload)
            self.assertEqual(response.status_code, 200)
            data = response.json()
            self.assertEqual(data["id"], original["id"])
            self.assertEqual(data["position_mode"], "actual")
            self.assertEqual(data["holdings"][0]["units"], 500.5)
            self.assertEqual(data["total_cost_amount"], 600.6)
            self.assertIsNotNone(data["total_pnl_amount"])
            self.assertEqual(self.client.put(url, json=payload).status_code, 400)

    def setUp(self) -> None:
        super().setUp()
        self.client = TestClient(app)

    def test_api_create_list_assess_update_and_delete(self) -> None:
        from app.routers import portfolios as portfolio_router

        with (
            patch.object(portfolio_router, "portfolio_service", self.portfolios),
            patch.object(
                portfolio_router,
                "portfolio_assessment_service",
                self.assessments,
            ),
        ):
            create_response = self.client.post(
                "/api/v1/portfolios",
                json={
                    "user_id": "api-user",
                    "name": "API 组合",
                    "holdings": SAMPLE_HOLDINGS,
                },
            )
            self.assertEqual(create_response.status_code, 201)
            portfolio_id = create_response.json()["id"]

            list_response = self.client.get(
                "/api/v1/portfolios?user_id=api-user"
            )
            self.assertEqual(list_response.status_code, 200)
            self.assertEqual(list_response.json()["count"], 1)

            assessment_response = self.client.get(
                f"/api/v1/portfolios/{portfolio_id}/assessment"
                "?user_id=api-user&months=12"
            )
            self.assertEqual(assessment_response.status_code, 200)
            self.assertEqual(
                assessment_response.json()["concentration"]["state_code"],
                "moderate",
            )

            update_response = self.client.put(
                f"/api/v1/portfolios/{portfolio_id}",
                json={
                    "user_id": "api-user",
                    "name": "API 调整组合",
                    "holdings": [
                        {"fund_code": "110020", "weight_pct": 55},
                        {"fund_code": "110007", "weight_pct": 45},
                    ],
                },
            )
            self.assertEqual(update_response.status_code, 200)
            self.assertEqual(update_response.json()["holding_count"], 2)

            delete_response = self.client.delete(
                f"/api/v1/portfolios/{portfolio_id}?user_id=api-user"
            )
            self.assertEqual(delete_response.status_code, 200)
            self.assertEqual(delete_response.json()["portfolio_id"], portfolio_id)

    def test_api_rejects_weights_that_do_not_total_one_hundred(self) -> None:
        from app.routers import portfolios as portfolio_router

        with patch.object(portfolio_router, "portfolio_service", self.portfolios):
            response = self.client.post(
                "/api/v1/portfolios",
                json={
                    "user_id": "api-user",
                    "name": "错误组合",
                    "holdings": [
                        {"fund_code": "110020", "weight_pct": 60},
                        {"fund_code": "110007", "weight_pct": 30},
                    ],
                },
            )

        self.assertEqual(response.status_code, 400)
        self.assertIn("100%", response.json()["detail"])

    def test_api_manages_actual_positions_and_transaction_history(self) -> None:
        from app.routers import portfolios as portfolio_router

        with patch.object(portfolio_router, "portfolio_service", self.portfolios):
            created = self.client.post(
                "/api/v1/portfolios",
                json={
                    "user_id": "api-user",
                    "name": "我的真实持仓",
                    "holdings": [
                        {
                            "fund_code": "110020",
                            "units": 100,
                            "cost_amount": 120,
                            "purchase_date": "2025-01-02",
                        }
                    ],
                },
            )
            self.assertEqual(created.status_code, 201)
            portfolio_id = created.json()["id"]
            self.assertEqual(created.json()["position_mode"], "actual")

            added = self.client.post(
                f"/api/v1/portfolios/{portfolio_id}/transactions",
                json={
                    "user_id": "api-user",
                    "idempotency_key": "api-buy-20250203",
                    "fund_code": "110020",
                    "transaction_type": "buy",
                    "trade_date": "2025-02-03",
                    "units": 10,
                    "unit_price": 1.3,
                    "fee": 0.5,
                    "note": "定投",
                },
            )
            self.assertEqual(added.status_code, 201)
            self.assertEqual(added.headers["Idempotency-Replayed"], "false")
            transaction_id = added.json()["id"]

            replayed = self.client.post(
                f"/api/v1/portfolios/{portfolio_id}/transactions",
                json={
                    "user_id": "api-user",
                    "idempotency_key": "api-buy-20250203",
                    "fund_code": "110020",
                    "transaction_type": "buy",
                    "trade_date": "2025-02-03",
                    "units": 10,
                    "unit_price": 1.3,
                    "fee": 0.5,
                    "note": "定投",
                },
            )
            self.assertEqual(replayed.status_code, 201)
            self.assertEqual(replayed.headers["Idempotency-Replayed"], "true")
            self.assertTrue(replayed.json()["idempotency_replayed"])
            self.assertEqual(replayed.json()["id"], transaction_id)

            conflict = self.client.post(
                f"/api/v1/portfolios/{portfolio_id}/transactions",
                json={
                    "user_id": "api-user",
                    "idempotency_key": "api-buy-20250203",
                    "fund_code": "110020",
                    "transaction_type": "buy",
                    "trade_date": "2025-02-03",
                    "units": 20,
                    "unit_price": 1.3,
                    "fee": 0.5,
                    "note": "定投",
                },
            )
            self.assertEqual(conflict.status_code, 409)

            listed = self.client.get(
                f"/api/v1/portfolios/{portfolio_id}/transactions"
                "?user_id=api-user"
            )
            self.assertEqual(listed.status_code, 200)
            self.assertEqual(listed.json()["count"], 2)

            renamed = self.client.patch(
                f"/api/v1/portfolios/{portfolio_id}",
                json={"user_id": "api-user", "name": "长期观察组合"},
            )
            self.assertEqual(renamed.status_code, 200)
            self.assertEqual(renamed.json()["name"], "长期观察组合")

            deleted = self.client.delete(
                f"/api/v1/portfolios/{portfolio_id}/transactions/{transaction_id}"
                "?user_id=api-user"
            )
            self.assertEqual(deleted.status_code, 400)
            corrected = self.client.post(
                f"/api/v1/portfolios/{portfolio_id}/transactions/{transaction_id}/corrections",
                json={"user_id": "api-user", "idempotency_key": "reverse-api-buy",
                      "reason": "重复录入外部成交，冲正多记的一笔"},
            )
            self.assertEqual(corrected.status_code, 201, corrected.text)
            listed = self.client.get(f"/api/v1/portfolios/{portfolio_id}/transactions?user_id=api-user").json()
            self.assertEqual(listed["count"], 2)
            self.assertEqual(next(row for row in listed["transactions"] if row["id"] == transaction_id)["status"], "reversed")


class PortfolioAgentToolTests(TemporaryPortfolioTestCase):
    def test_tool_is_registered_allowed_bounded_and_uses_prompt_context(self) -> None:
        portfolio = self.create_sample()

        from app.services import portfolio_agent_tools as tool_module

        with patch.object(
            tool_module,
            "portfolio_assessment_service",
            self.assessments,
        ):
            raw_result = get_portfolio_assessment_tool.invoke(
                {
                    "user_id": "chen",
                    "portfolio_id": portfolio["id"],
                    "months": 12,
                }
            )
        payload = json.loads(raw_result)

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["tool_result_scope"], "bounded_agent_summary")
        self.assertLess(len(raw_result), 4_000)
        self.assertIn(
            "get_portfolio_assessment_tool",
            {tool.name for tool in get_agent_tools()},
        )
        self.assertTrue(
            check_tool_permission("get_portfolio_assessment_tool", {}).allowed
        )

        prompt = prompt_builder_service.build(
            user_id="chen",
            tool_names=["get_portfolio_assessment_tool"],
            active_portfolio_id=portfolio["id"],
        )
        portfolio_section = next(
            section for section in prompt.sections if section.name == "portfolio_context"
        )
        self.assertIn(portfolio["id"], portfolio_section.content)
        self.assertIn("必须调用", portfolio_section.content)
        self.assertIn("真实组合波动率", prompt.system_prompt)
        self.assertIn("至少引用一个", prompt.system_prompt)
        self.assertIn("evidence_id", prompt.system_prompt)


if __name__ == "__main__":
    unittest.main()
