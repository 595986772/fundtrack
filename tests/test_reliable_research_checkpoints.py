"""验证接管隔离后的真实 LangGraph 断点，不用字典模拟图执行。"""

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock

from app.database import Database
from app.services.portfolio_research_checkpoints import create_portfolio_research_checkpoint_runtime
from app.services.portfolio_research_graph import PortfolioResearchGraphService
from app.services.portfolio_research_snapshots import PortfolioResearchSnapshotService
from tests.test_portfolio_research_graph import (
    PORTFOLIO_ID, FakePortfolioService, FakeAssessmentService, FakePortfolioRiskEngine,
    FakePortfolioPerformanceService, FakePortfolioDecisionService, FakeMarketSentimentService,
    FakeMarketEventService, FakePortfolioInterpreter, FailOncePortfolioInterpreter,
)


def build_durable_graph(database, runtime, interpreter=None):
    """复用业务测试中的固定数据，唯独存储与图调度使用真实实现。"""
    return PortfolioResearchGraphService(
        portfolios=FakePortfolioService(), assessments=FakeAssessmentService(),
        quant_risk=FakePortfolioRiskEngine(), performance=FakePortfolioPerformanceService(),
        decisions=FakePortfolioDecisionService(), market_sentiment=FakeMarketSentimentService(),
        market_events=FakeMarketEventService(), interpreter=interpreter or FakePortfolioInterpreter(),
        snapshots=PortfolioResearchSnapshotService(database), checkpointer=runtime.checkpointer,
        checkpoint_backend=runtime.backend, checkpoint_durable=runtime.durable,
    )


class ReliableCheckpointTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.db = Database(f"sqlite:///{Path(self.directory.name) / 'snapshots.db'}")
        self.addCleanup(self.db.engine.dispose)
        self.runtime = create_portfolio_research_checkpoint_runtime(
            backend="sqlite", database_url=f"sqlite:///{Path(self.directory.name) / 'checkpoints.db'}")
        self.addCleanup(self.runtime.close)

    def run_graph(self, graph, thread, progress=None, user="chen"):
        return graph.run(run_id="stable-job", user_id=user, portfolio_id=PORTFOLIO_ID,
            checkpoint_thread_id=thread, progress_callback=progress)

    def test_recovery_keeps_frozen_inputs_and_old_worker_cannot_pollute_new_checkpoint(self):
        interpreter = FailOncePortfolioInterpreter()
        graph = build_durable_graph(self.db, self.runtime, interpreter)
        with self.assertRaises(RuntimeError):
            self.run_graph(graph, "v1")
        graph.prepare_attempt_checkpoint(target_thread_id="v2", source_thread_ids=("v1", "empty-attempt"))
        # 接管开始后，旧分区被故意写入错误状态。新分区必须仍能完成原来的正确研究。
        graph.graph.update_state({"configurable": {"thread_id": "v1"}}, {"portfolio": {"bad": True}}, as_node="load_portfolio")
        graph._get_or_capture_snapshot = Mock(side_effect=AssertionError("不得重新抓取输入"))
        nodes = []
        report = self.run_graph(graph, "v2", lambda node, _: nodes.append(node))
        self.assertEqual(nodes, ["interpret_with_llm", "compose_report"])
        self.assertEqual(interpreter.attempt_count, 2)
        self.assertEqual(report["run_id"], "stable-job")
        self.assertEqual(report["graph_trace"]["checkpoint_thread_id"], "v2")
        self.assertTrue(report["graph_trace"]["resumed_from_checkpoint"])
        self.assertTrue(report["research_snapshot"]["immutable"])

    def test_completed_graph_recovered_without_another_model_call(self):
        graph = build_durable_graph(self.db, self.runtime)
        first = self.run_graph(graph, "v1")
        graph.interpreter.interpret = Mock(side_effect=AssertionError("不能再次调用模型"))
        graph.prepare_attempt_checkpoint(target_thread_id="v2", source_thread_ids=("v1",))
        report = self.run_graph(graph, "v2")
        self.assertEqual(first["summary"], report["summary"])
        self.assertEqual(first["research_snapshot"], report["research_snapshot"])
        self.assertTrue(report["graph_trace"]["resumed_from_checkpoint"])
        with self.assertRaisesRegex(ValueError, "user_id"):
            self.run_graph(graph, "v2", user="other-user")

    def test_parallel_pending_writes_survive_attempt_copy(self):
        graph = build_durable_graph(self.db, self.runtime)
        def interrupt(node, message):
            if node == "calculate_quant_risk":
                raise RuntimeError("单个并行分支中断")
        with self.assertRaises(RuntimeError):
            self.run_graph(graph, "v1", interrupt)
        graph.prepare_attempt_checkpoint(target_thread_id="v2", source_thread_ids=("v1",))
        nodes = []
        self.run_graph(graph, "v2", lambda node, _: nodes.append(node))
        self.assertIn("calculate_quant_risk", nodes)
        self.assertNotIn("calculate_assessment", nodes)
        self.assertNotIn("read_market_context", nodes)
        self.assertEqual(nodes[-2:], ["interpret_with_llm", "compose_report"])


if __name__ == "__main__":
    unittest.main()
