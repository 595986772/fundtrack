"""排序与供应商契约测试：真实中文分词/BM25/SQL 账本，HTTP 使用隔离响应。"""

from contextlib import ExitStack
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import httpx

from app.config import settings
from app.database import Database
from app.services.ai_capacity import MemoryAICapacity
from app.services.ai_governance_context import AIControlError, ai_execution_scope
from app.services.ai_usage import AIUsageService
from app.services.fund_report_retrieval import (
    bm25_rank, tokenize, fuse_rankings, select_ranked_evidence,
    DashScopeReportReranker, RerankResult, RerankUnavailable, report_retrieval_scope,
)
from app.services.job_runtime import JobCancelledError


def chunk(key, text, period="2026-Q1"):
    return {"evidence_id": key, "text": text, "document_id": period, "period": period}


def group(period, count=6):
    chunks = [chunk(f"{period}:{i}", f"基金经理调整策略方向 {i}", period) for i in range(count)]
    ranks = fuse_rankings([{"evidence_id": row["evidence_id"]} for row in chunks], [])
    return {"document_id": period, "period": period, "candidates": chunks, "ranks": ranks}


class RankingTests(unittest.TestCase):
    def test_chinese_financial_terms_and_no_match_do_not_select_first_page(self):
        self.assertIn("半导体", tokenize("请问这只半导体基金如何配置？"))
        self.assertIn("300", tokenize("沪深３００ ETF"))
        rows = [chunk("irrelevant", "管理费和托管费的计提规则"), chunk("strategy", "基金经理增加半导体配置")]
        self.assertEqual([row["evidence_id"] for row in bm25_rank("半导体配置", rows)], ["strategy"])
        self.assertEqual(bm25_rank("海洋养殖", rows), [])
        self.assertEqual(bm25_rank("请问", rows), [])
        self.assertEqual(bm25_rank("半导体", []), [])

    def test_fusion_rewards_agreement_and_does_not_double_count_duplicate_hits(self):
        result = fuse_rankings([{"evidence_id": "a"}, {"evidence_id": "b"}], ["b", "b", "c"])
        self.assertEqual([row["evidence_id"] for row in result], ["b", "a", "c"])
        self.assertEqual(result[0]["ranks"], {"bm25": 2, "vector": 1})
        self.assertAlmostEqual(result[0]["rrf_score"], 1 / 62 + 1 / 61)

    def test_one_rerank_call_then_quarter_quotas_even_when_one_quarter_scores_higher(self):
        groups = [group("Q1"), group("Q2")]
        ranker = Mock(available=True)
        scores = {row["evidence_id"]: (0.9 if g["period"] == "Q1" else 0.1) + i / 100
                  for g in groups for i, row in enumerate(g["candidates"])}
        ranker.rank.return_value = RerankResult(scores, None, "test-rerank")
        selected, diagnostics = select_ranked_evidence("投资策略", groups, reranker=ranker)
        ranker.rank.assert_called_once()
        self.assertEqual([row["evidence_id"] for row in selected[:4]], [f"Q1:{i}" for i in (5, 4, 3, 2)])
        self.assertEqual(sum(row["period"] == "Q2" for row in selected), 4)
        self.assertEqual(diagnostics["rerank"]["status"], "applied")

    def test_explicit_fallback_preserves_rrf_but_cannot_swallow_cancellation_or_budget(self):
        ranker = Mock(available=True, model="test-rerank")
        ranker.rank.side_effect = RerankUnavailable("供应商超时")
        rows, diagnostics = select_ranked_evidence("策略", [group("Q1")], reranker=ranker)
        self.assertEqual(rows[0]["evidence_id"], "Q1:0")
        self.assertEqual(diagnostics["rerank"]["status"], "fallback")
        for error in (AIControlError("budget", "额度不足"), JobCancelledError("cancel"), TimeoutError()):
            ranker.rank.side_effect = error
            with self.assertRaises(type(error)):
                select_ranked_evidence("策略", [group("Q1")], reranker=ranker)

    def test_numeric_path_and_missing_configuration_are_distinguishable(self):
        ranker = Mock(available=True)
        _, detail = select_ranked_evidence("占比", [group("Q1")], reranker=ranker, numeric_only=True)
        ranker.rank.assert_not_called()
        self.assertEqual(detail["rerank"]["status"], "not_needed")
        _, detail = select_ranked_evidence("方向", [group("Q1")])
        self.assertEqual(detail["rerank"]["status"], "not_configured")

    def test_deadline_prevents_late_model_requests_and_is_restored(self):
        ranker = Mock(available=True)
        with report_retrieval_scope(monotonic() - 1):
            with self.assertRaises(TimeoutError):
                select_ranked_evidence("策略", [group("Q1")], reranker=ranker)
        ranker.rank.assert_not_called()
        # 离开上一请求后不污染新请求的 ContextVar。
        self.assertEqual(len(select_ranked_evidence("策略", [group("Q1")])[0]), 4)


class RerankerContractTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        directory = self.stack.enter_context(TemporaryDirectory())
        self.db = Database(f"sqlite:///{Path(directory) / 'test.db'}")
        self.stack.callback(self.db.engine.dispose)
        self.ledger = AIUsageService(self.db)
        self.stack.enter_context(patch("app.services.governed_model.get_ai_usage_service", return_value=self.ledger))
        self.stack.enter_context(patch("app.services.governed_model.get_ai_capacity", return_value=MemoryAICapacity()))
        self.stack.enter_context(patch.object(settings, "ai_run_token_limit", 100000))
        self.stack.enter_context(patch.object(settings, "ai_daily_token_limit", 100000))
        self.stack.enter_context(patch.object(settings, "ai_model_requests_per_minute", 100))
        self.stack.enter_context(ai_execution_scope("test-rerank-user", "rank:1", "test"))
        self.documents = [chunk("one", "指数投资策略延续"), chunk("two", "转向关注半导体设备")]
        self.requests = []

    def ranker(self, body=None, *, error=None, status=200):
        def handle(request):
            self.requests.append(request)
            if error:
                raise error
            return httpx.Response(status, json=body, request=request)
        return DashScopeReportReranker(url="https://example.test/compatible-api/v1/reranks", api_key="test-only",
                                      model="qwen3-rerank", transport=httpx.MockTransport(handle))

    def summary(self):
        return self.ledger.summary(user_id="test-rerank-user")

    def test_valid_response_settles_actual_usage_and_uses_only_returned_indexes(self):
        ranker = self.ranker({"results": [{"index": 1, "relevance_score": .9}, {"index": 0, "relevance_score": .1}],
                              "usage": {"total_tokens": 42}})
        result = ranker.rank("配置变化", self.documents)
        self.assertEqual(result.scores, {"two": .9, "one": .1})
        request = json.loads(self.requests[0].content)
        self.assertEqual(request["top_n"], 2)
        self.assertNotIn("input", request)
        self.assertEqual(self.summary()["used_tokens"], 42)
        self.assertEqual(self.summary()["calls"][0]["status"], "settled")

    def test_missing_usage_is_unknown_not_zero(self):
        self.ranker({"results": [{"index": 0, "relevance_score": .7}, {"index": 1, "relevance_score": .6}]}).rank("配置", self.documents)
        self.assertEqual(self.summary()["calls"][0]["status"], "unknown")
        self.assertGreater(self.summary()["uncertain_tokens"], 0)

    def test_bad_results_still_settle_known_usage_and_do_not_expose_body(self):
        for rows in ([{"index": 2, "relevance_score": .7}, {"index": 1, "relevance_score": .2}],
                     [{"index": 0, "relevance_score": .7}, {"index": 0, "relevance_score": .2}],
                     [{"index": 0, "relevance_score": float("nan")}, {"index": 1, "relevance_score": .2}],
                     [{"index": 0, "relevance_score": .7}]):
            # JSON 标准不能编码 NaN；用非法分数 -1 验证同一拒绝分支。
            rows = [{**row, "relevance_score": -1 if str(row["relevance_score"]) == "nan" else row["relevance_score"]} for row in rows]
            with self.assertRaises(RerankUnavailable):
                self.ranker({"results": rows, "usage": {"total_tokens": 5}}).rank("配置", self.documents)
        self.assertEqual(self.summary()["used_tokens"], 20)

    def test_timeout_records_uncertain_usage_and_does_not_retry(self):
        ranker = self.ranker(error=httpx.ReadTimeout("do-not-expose-provider-body"))
        with self.assertRaises(RerankUnavailable) as error:
            ranker.rank("配置", self.documents)
        self.assertNotIn("do-not-expose-provider-body", str(error.exception))
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(self.summary()["calls"][0]["status"], "unknown")

    def test_budget_rejection_never_contacts_provider(self):
        ranker = self.ranker({})
        with patch.object(settings, "ai_run_token_limit", 1), self.assertRaises(AIControlError):
            ranker.rank("配置", self.documents)
        self.assertEqual(self.requests, [])

    def test_configuration_rejects_http_without_transmitting_key(self):
        ranker = self.ranker({})
        ranker.url = "http://example.test/reranks"
        with self.assertRaises(RerankUnavailable):
            ranker.rank("配置", self.documents)
        self.assertEqual(self.requests, [])


if __name__ == "__main__":
    unittest.main()
