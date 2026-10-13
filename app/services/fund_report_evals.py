"""季报专项评测：复用 AgentEvalService，只扩展隔离输入、跨轮运行和可审计评分。

产品数据库仍服务真实用户；考卷装入临时SQLite，语义索引使用data/eval_indexes。
同一服务类负责产品和评测检索，不会用“假检索成功”当作真实性能。
"""
from __future__ import annotations

import asyncio
from contextvars import ContextVar
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from types import SimpleNamespace

from app.config import settings
from app.database import Database
from app.models import FundReportDocumentModel
from app.schemas import AgentChatRequest, AgentTokenUsage, AgentWorkspaceSnapshot, AgentWorkspaceRuntimeInfo, AgentEvalDashboard
from app.services.agent_evals import AgentEvalService, AgentEvalError, PROJECT_ROOT
from app.services.agent_execution_context import AgentExecutionContext, FUND_READ
from app.services.comprehensive_agent import ComprehensiveAgentService
from app.services.conversations import ConversationService
from app.services.eval_context import fixture_fingerprint
from app.services.fund_report_eval_grading import GRADER_VERSION, grade_report_case, normalize, report_turn_diagnostics
from app.services.fund_report_source import parse_pdf, page_chunks
from app.services.fund_reports import FundReportService, fund_report_reader_scope
from app.services.observability import AgentObservabilityService

DATA = PROJECT_ROOT / "data/evals/fund_reports_v1"
SUITE = PROJECT_ROOT / "data/evals/fund_report_rag_v1.json"
_ENVIRONMENT = ContextVar("report_eval_environment", default=None)
_MODE = ContextVar("report_eval_retrieval_mode", default="hybrid")


class EvaluationWorkspace:
    """评测没有用户任务/队友列表；只给应用层返回隔离工作区元信息。"""
    def workspace_snapshot(self, *, user_id, tool_names):
        return AgentWorkspaceSnapshot(user_id=user_id, runtime=AgentWorkspaceRuntimeInfo(
            app_name="FundScope 季报评测", model="evaluation", execution_backend="local",
            database_backend="sqlite", tool_count=len(tool_names), mcp_tool_count=0))


class FundReportEvalService(AgentEvalService):
    def __init__(self, *, data_dir=DATA, suite_path=SUITE, **kwargs):
        self.data_dir = Path(data_dir)
        super().__init__(suite_path=suite_path, fixture_paths=[self.data_dir / "documents.json"], **kwargs)

    def _manifest(self):
        return json.loads((self.data_dir / "documents.json").read_text(encoding="utf-8"))["documents"]

    def _input_fingerprint(self):
        # 题目、原PDF和文档版本一起锁定；keyword与hybrid成绩不能互相冒充同组基线。
        docs = self._manifest()
        digest = fixture_fingerprint([self.suite_path, self.data_dir / "documents.json",
            *[self.data_dir / doc["pdf_path"] for doc in docs]])
        mode = _MODE.get()
        embedding = hashlib.sha256(f"{settings.embedding_base_url}|{settings.embedding_model}|{settings.embedding_dimensions}".encode()).hexdigest()[:10]
        return f"{digest}:{mode}" + (f":{embedding}" if mode == "hybrid" else "")

    def _data_fingerprint(self):
        # 同样的PDF但评分口径变了，不能与旧版“有链接就过”直接计算分数涨跌。
        # 保留来源指纹，附上评分版本；旧记录不重算，也不因此重建相同语义索引。
        return f"{self._input_fingerprint()}:{GRADER_VERSION}"

    def _resolve_scenario(self, scenario, *, portfolio_ids):
        documents = [doc for doc in self._manifest() if doc["fund_code"] == scenario["fund_code"]
            and doc["period"] in scenario["report_periods"]]
        if len(documents) != 2:
            raise AgentEvalError("季报考卷没有指定的两期文档。")
        return {**scenario, "document_ids": [doc["id"] for doc in documents]}

    def dashboard(self, *, user_id, retrieval_mode="hybrid"):
        """只把同检索配置、同考卷、同题组的成绩进行比较。读取面板不会建索引。"""
        if retrieval_mode not in {"keyword", "hybrid"}:
            raise AgentEvalError("仅支持 keyword 或 hybrid 检索评测。")
        token = _MODE.set(retrieval_mode)
        try:
            suite = self.suite_info()
            recent = [run for run in self.observability.recent_eval_runs(
                user_id=user_id, suite_id=suite.id, limit=100)
                if run.data_fingerprint == suite.data_fingerprint]
            latest = recent[0] if recent else None
            previous = next((run for run in recent[1:]
                if latest and run.case_set_id == latest.case_set_id), None)
            if previous:
                latest = latest.model_copy(update={"score_delta": round(latest.score - previous.score, 1)})
                recent[0] = latest
            return AgentEvalDashboard(suite=suite, latest_run=latest, previous_run=previous,
                recent_eval_runs=recent[:6], observability=self.observability.summary(user_id=user_id))
        finally:
            _MODE.reset(token)

    def validate_fixture(self):
        """先验证考卷本身，再调用付费模型。参考短句必须确实位于标注物理页。"""
        docs = self._manifest()
        if len(docs) != 6 or len({doc["id"] for doc in docs}) != 6:
            raise AgentEvalError("季报固定样本应为三只基金的六份不同文档。")
        for doc in docs:
            path = (self.data_dir / doc["pdf_path"]).resolve()
            if not path.is_relative_to(self.data_dir.resolve()):
                raise AgentEvalError("样本PDF路径超出考卷目录。")
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            expected_id = "fr_" + hashlib.sha256(f"{doc['fund_code']}:{digest}".encode()).hexdigest()[:32]
            if digest != doc["content_hash"] or expected_id != doc["id"]:
                raise AgentEvalError("季报PDF指纹不匹配，请检查样本版本，不能继续评分。")
        suite = self._load_suite()
        for case in suite["scenarios"]:
            self._resolve_scenario(case, portfolio_ids={})
            for gold in case.get("gold_evidence", []):
                doc = next(doc for doc in docs if doc["fund_code"] == case["fund_code"] and doc["period"] == gold["period"])
                if not 1 <= gold["page"] <= len(doc["pages"]) or normalize(gold["quote"]) not in normalize(doc["pages"][gold["page"] - 1]["text"]):
                    raise AgentEvalError(f"{case['id']} 的参考原文或页码不匹配。")
        return docs

    def _prepare_reader(self, database, docs, progress, mode):
        # 每轮重新用当前PDF解析代码读取固定PDF，独立答案仍来自case JSON。
        database.create_schema()
        parser_hash = hashlib.sha256((PROJECT_ROOT / "app/services/fund_report_source.py").read_bytes()).hexdigest()[:10]
        cache = PROJECT_ROOT / "data/eval_indexes" / self._input_fingerprint().replace(":", "_") / parser_hash
        reader = FundReportService(database, directory=cache / "pdfs", chroma_directory=cache / "chroma")
        for doc in docs:
            progress(f"核对 {doc['fund_code']} {doc['period']} 固定原文")
            pages, facts = parse_pdf((self.data_dir / doc["pdf_path"]).read_bytes(), doc["fund_code"], doc["period"])
            # 逐页文本的校验防止只改manifest而不改PDF来制造“标准答案”。
            if pages != doc["pages"]:
                raise AgentEvalError("当前PDF提取结果和固定页文本不同，请显式制作新版考卷。")
            with database.session_factory() as session:
                session.add(FundReportDocumentModel(**{key:doc[key] for key in (
                    "id", "fund_code", "period", "title", "published_at", "source_url", "content_hash")},
                    pages=pages, facts=facts, created_at=datetime.now(timezone.utc)))
                session.commit()
            if mode == "keyword":
                continue
            # 原文、模型和切块版本相同才复用完整索引；不复用用户产品库的向量。
            progress(f"检查 {doc['fund_code']} {doc['period']} 评测语义索引")
            collection = reader._collection()
            expected_ids = {item["evidence_id"] for item in page_chunks(doc)}
            existing = set(collection.get(ids=sorted(expected_ids), include=[])["ids"])
            if existing == expected_ids:
                with database.session_factory() as session:
                    session.get(FundReportDocumentModel, doc["id"]).index_version = reader.index_version
                    session.commit()
            else:
                reader.index_document({**doc, "pages": pages}, progress)
        return reader

    async def run_suite(self, *, user_id, case_ids=None, progress_callback=None, retrieval_mode="hybrid"):
        if retrieval_mode not in {"keyword", "hybrid"}:
            raise AgentEvalError("仅支持 keyword 或 hybrid 检索评测。")
        # 验证场景ID放在建索引之前，输错ID不会消耗embedding额度。
        self._select_scenarios(self._load_suite()["scenarios"], case_ids or [])
        docs = self.validate_fixture()
        mode_token = _MODE.set(retrieval_mode)
        total = len(case_ids or self._load_suite()["scenarios"])
        stopped = Event()
        def progress(message):
            if stopped.is_set():
                raise AgentEvalError("季报评测已取消。")
            if progress_callback:
                progress_callback(0, total, message)
        try:
            with TemporaryDirectory(prefix="fund-report-eval-") as directory:
                database = Database(f"sqlite:///{directory}/evaluation.db")
                try:
                    # 取消await不能杀掉线程。先通知索引线程停止并等它退出，才清理临时SQL。
                    preparation = asyncio.create_task(asyncio.to_thread(
                        self._prepare_reader, database, docs, progress, retrieval_mode))
                    try:
                        reader = await asyncio.shield(preparation)
                    except asyncio.CancelledError:
                        stopped.set()
                        await asyncio.gather(preparation, return_exceptions=True)
                        raise
                    token = _ENVIRONMENT.set((database, reader, progress_callback, total))
                    try:
                        with fund_report_reader_scope(reader):
                            return await super().run_suite(user_id=user_id, case_ids=case_ids, progress_callback=progress_callback)
                    finally:
                        _ENVIRONMENT.reset(token)
                finally:
                    database.engine.dispose()
        finally:
            _MODE.reset(mode_token)

    async def _execute_case(self, scenario, eval_user_id):
        database, reader, progress, total = _ENVIRONMENT.get()
        service = ComprehensiveAgentService(conversations=ConversationService(database), agent=self.agent,
            runtime_state=EvaluationWorkspace(), observability=AgentObservabilityService(database))
        # 不授予交易、写文件或创建研究Job能力。研究文档仍由真实路由选择。
        access = AgentExecutionContext(user_id=eval_user_id, role="user", mode="product",
            capabilities=frozenset({FUND_READ}), source="report-evaluation")
        request = AgentChatRequest(message=scenario["message"], user_id=eval_user_id,
            fund_code=scenario["fund_code"], fund_report_ids=scenario["document_ids"], max_tool_rounds=3)
        turns = []
        async def monitor(event):
            # 每个模型/工具边界让Job检查取消；进度条由外层逐题更新，不在这里归零。
            if progress:
                progress(None, total, event.get("label", "正在运行季报场景"))
        async def chat_turn(current_request):
            try:
                return await asyncio.wait_for(service.chat(current_request, execution_context=access, progress_callback=monitor), timeout=120)
            except Exception as exc:
                # 执行失败仍不参与语义评分，但不能丢掉已完成轮次和已知用量。
                diagnostics = dict(getattr(exc, "execution_diagnostics", {}))
                completed = [report_turn_diagnostics(turn, index + 1) for index, turn in enumerate(turns)]
                failed = {"turn": len(turns) + 1, "question": current_request.message,
                    "answer_status": diagnostics.get("status", "error"), "execution": dict(diagnostics),
                    "usage": diagnostics.get("usage"), "answer_alignment": None}
                diagnostics.update(turns=[*completed, failed], run_config=self._run_config(),
                    usage={key: sum(turn["run"].usage.model_dump()[key] for turn in turns)
                        + (failed["usage"] or {}).get(key, 0) for key in ("input_tokens", "output_tokens", "total_tokens")})
                diagnostics["usage_complete"] = False
                exc.execution_diagnostics = diagnostics
                raise

        first = await chat_turn(request)
        turns.append({"run": first.run, "reference": first.assistant_message.run_metadata.get("fund_report_reference")})
        if scenario.get("follow_up"):
            # 第二轮只发送会话ID和问题，让生产应用层负责恢复基金与不可变文档引用。
            second_request = AgentChatRequest(message=scenario["follow_up"], user_id=eval_user_id,
                conversation_id=first.conversation.id, max_tool_rounds=3)
            second = await chat_turn(second_request)
            turns.append({"run": second.run, "reference": second.assistant_message.run_metadata.get("fund_report_reference")})
        last = turns[-1]["run"]
        combined = SimpleNamespace(**dict(last))
        combined.tool_calls = [call for turn in turns for call in turn["run"].tool_calls]
        combined.rounds = [item for turn in turns for item in turn["run"].rounds]
        combined.usage = AgentTokenUsage(**{key:sum(getattr(turn["run"].usage, key) for turn in turns)
            for key in ("input_tokens", "output_tokens", "total_tokens")})
        combined.eval_turns = turns
        return combined

    def grade_case(self, *, scenario, run, duration_ms, data_profile="", data_fingerprint=""):
        result = super().grade_case(scenario=scenario, run=run, duration_ms=duration_ms,
            data_profile=data_profile, data_fingerprint=data_fingerprint)
        documents = [doc for doc in self._manifest() if doc["id"] in scenario["document_ids"]]
        config = self._run_config(model=run.used_model)
        checks, quality = grade_report_case(scenario, run, documents, config)
        result.checks.extend(checks)
        result.report_quality = quality
        result.metric_scores = self._metric_scores(result.checks)
        # 比例指标保留部分命中情况；只看布尔“全部召回”会掩盖2/3与0/3的区别。
        for metric, dimension in (("evidence_recall", "retrieval"), ("disclosure_numeric", "numeric"), ("citation_locatable", "citations")):
            value = quality["metrics"][metric]["value"]
            if value is not None:
                result.metric_scores[dimension] = round(value * 100, 1)
        result.passed = all(item.passed for item in result.checks if item.required)
        result.score = round(sum(item.weight for item in result.checks if item.passed) / sum(item.weight for item in result.checks) * 100, 1)
        return result

    def _run_config(self, model=None):
        """成功和失败都记录同一份无密钥配置，方便按相同实验条件对照。"""
        pipeline_paths = [PROJECT_ROOT / f"app/services/{name}.py" for name in (
            "fund_report_source", "fund_reports", "fund_report_tools", "fund_report_answer", "fund_report_query", "fund_report_runtime",
            "fund_report_sentences", "fund_report_eval_grading", "fund_report_retrieval",
            "recovery", "llm", "governed_model", "ai_capacity", "ai_usage",
            "prompt_builder", "context_compactor", "tool_agent")]
        from app.services.fund_report_runtime import SYNTHESIS_POLICY, synthesis_mode
        return {"model": model or settings.deepseek_model, "embedding_model": settings.embedding_model,
            "synthesis_mode": synthesis_mode(), "synthesis_policy": SYNTHESIS_POLICY,
            "report_timeout_seconds": settings.fund_report_timeout_seconds,
            "model_timeout_seconds": settings.agent_model_timeout_seconds, "model_max_attempts": settings.agent_model_max_attempts,
            "embedding_dimensions": settings.embedding_dimensions, "retrieval_mode": _MODE.get(),
            "reranker": {"configured": bool(settings.fund_report_rerank_url and settings.dashscope_api_key),
                "model": settings.fund_report_rerank_model,
                "timeout_seconds": settings.fund_report_rerank_timeout_seconds},
            # 额度/输出长度也是实验条件，不能把受新上限影响的结果当成同预算提速。
            "ai_governance": ({"enabled": True, "max_output_tokens": settings.ai_max_output_tokens,
                "run_token_limit": settings.ai_run_token_limit, "daily_token_limit": settings.ai_daily_token_limit,
                "queue_wait_seconds": settings.ai_queue_wait_seconds,
                "global_concurrency": settings.ai_global_concurrency, "user_concurrency": settings.ai_user_concurrency,
                "model_requests_per_minute": settings.ai_model_requests_per_minute}
                if settings.ai_governance_enabled else {"enabled": False}),
            "pipeline_fingerprint": fixture_fingerprint(pipeline_paths),
            "token_scope": "主Agent各轮LLM用量；不包含embedding/精排 Token，完整用量见AI账本", "grader": GRADER_VERSION}


fund_report_eval_service = FundReportEvalService()
