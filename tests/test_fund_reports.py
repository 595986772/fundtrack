"""季报 RAG 的离线契约测试：不抓取财经数据，不消耗真实模型额度。"""
from datetime import date, datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient
from app.database import Database
from app.main import app
from app.models import FundReportDocumentModel
from app.schemas import AgentChatRequest
from app.services.fund_report_source import select_quarters, report_period, extract_allocation, page_chunks, manager_chunks, parse_pdf, FundReportSource
from app.services.fund_reports import FundReportService
from app.services.fund_report_context import resolve_fund_report_context
from app.services.agent_routing import agent_request_router
from app.services.agent_tool_executor import AgentToolExecutor
from app.services.agent_execution_context import AgentExecutionContext
from app.services.errors import AgentRuntimeError
from app.services.context_compactor import context_compactor_service
from app.services.evidence import ensure_answer_evidence_index
from app.services.prompt_builder import prompt_builder_service
import json
from app.services.job_runtime import JobCancelledError
from app.services.comprehensive_agent import ComprehensiveAgentService
from app.services.conversations import ConversationService
from app.services.observability import AgentObservabilityService
from tests.test_conversations import FakeAgent, FakeRuntimeState

CODE = "110020"
IDS = ["fr_" + "1" * 32, "fr_" + "2" * 32]
TABLE = [{"page": 12, "text": "5.1 报告期末基金资产组合情况\n序号 项目 金额(元) 占基金总资产的比例(%)\n1 权益投资 153,175,690.57 0.69\n2 基金投资 20,534,910,307.09 92.12\n"},
         {"page": 13, "text": "7 银行存款和结算备付金合计 931,320,786.88 4.18\n8 其他资产 - -\n5.2 报告期末按行业分类的股票投资组合\n1 权益投资 199.00 99.00"}]


class ReportParsingTests(unittest.TestCase):
    def test_quarters_filter_versions_and_future_announcements(self):
        def row(title, published, n=1, code=CODE):
            return dict(TITLE=title, PUBLISHDATE=published, ID=f"AN{n:018d}", FUNDCODE=code)
        rows=[row("2026年第2季度报告", "2026-07-20"), row("2026年第1季度报告", "2026-04-20", 2),
            row("2026年第二季度报告（更正）", "2026-07-22", 3), row("2026年中期报告", "2026-08-30", 4),
            row("2026年第3季度报告", "2026-10-20", 5), row("2026年第2季度报告摘要", "2026-07-24", 6),
            row("2026年第2季度报告", "2026-07-23", 7, "000001")]
        selected=select_quarters(rows, CODE, date(2026,9,14))
        self.assertEqual([r["period"] for r in selected], ["2026-Q2", "2026-Q1"])
        self.assertEqual(selected[0]["published_at"], "2026-07-22")
        self.assertEqual(report_period("2025 年 第四季度报告"), "2025-Q4")

    def test_cross_page_table_keeps_denominator_decimal_and_unknown(self):
        facts=extract_allocation(TABLE)
        self.assertEqual([r["ratio_percent"] for r in facts], ["0.69", "92.12", "4.18"])
        self.assertEqual(facts[-1]["page"], 13)
        self.assertTrue(all(r["denominator"] == "基金总资产" for r in facts))
        self.assertNotIn("其他资产", [r["label"] for r in facts])

    def test_chunks_never_cross_page_and_ids_are_stable(self):
        doc={"id":IDS[0], "fund_code":CODE, "period":"2026-Q1", "source_url":"https://example.com/a.pdf",
             "pages":[{"page":1,"text":"甲"*1300}, {"page":2,"text":"乙"*1000}]}
        chunks=page_chunks(doc)
        self.assertEqual(chunks,page_chunks(doc))
        self.assertEqual(len(chunks),4)
        self.assertTrue(all(not ("甲" in c["text"] and "乙" in c["text"]) for c in chunks))
        self.assertTrue(all(f"#page={c['page']}" in c["source_url"] for c in chunks))

    def test_net_asset_header_cannot_be_labelled_total_asset(self):
        pages=[{**TABLE[0], "text":TABLE[0]["text"].replace("基金总资产", "基金资产净值")}]
        self.assertEqual(extract_allocation(pages), [])

    def test_manager_section_at_page_end_retains_next_page_and_stops_before_performance(self):
        doc={"id":IDS[0],"fund_code":CODE,"period":"2026-Q1","source_url":"https://example.com/a.pdf",
             "pages":[{"page":10,"text":"经理履历"*200+"\n4.4 报告期内基金投资策略和运作分析\n本季度采取指数投资。"*2},
                      {"page":11,"text":"对市场保持谨慎，关注盈利变化。"*70+"\n4.5 报告期内基金的业绩表现\n不要读这里的业绩表"}]}
        chunks=manager_chunks(doc);combined="".join(c["text"] for c in chunks)
        self.assertNotIn("经理履历",combined);self.assertNotIn("不要读这里的业绩表",combined)
        self.assertIn("对市场保持谨慎",combined)
        self.assertEqual({c["page"] for c in chunks},{10,11})

    def test_pdf_links_replace_unreadable_id_footer_but_do_not_hide_missing_sources(self):
        urls=[f"https://pdf.dfcfw.com/pdf/H2_AN{i:018d}_1.pdf#page=12" for i in (1,2)]
        payload={"ok":True,"evidence":[dict(document_id=doc_id,period=f"2026-Q{i+1}",page=12,
            evidence_id=f"report:{doc_id}:p12:manager0",source_url=urls[i]) for i,doc_id in enumerate(IDS)]}
        calls=[SimpleNamespace(tool_name="get_fund_report_research_tool",tool_result=json.dumps(payload))]
        answer="已经比较，来源 "+" · ".join(urls)
        self.assertEqual(ensure_answer_evidence_index(answer=answer,tool_calls=calls),(answer,[]))
        repaired,added=ensure_answer_evidence_index(answer="已经比较",tool_calls=calls)
        self.assertIn("[2026-Q1 第12页]",repaired);self.assertIn("[2026-Q2 第12页]",repaired)
        self.assertNotIn("证据索引",repaired);self.assertEqual(len(added),2)

    def test_pdf_identity_and_scanned_documents_fail_closed(self):
        page=Mock();page.extract_text.return_value="2026年第2季度报告 110020 " + "公开披露"*100
        with patch("app.services.fund_report_source.PdfReader", return_value=SimpleNamespace(is_encrypted=False,pages=[page])):
            self.assertEqual(len(parse_pdf(b"%PDF-test",CODE,"2026-Q2")[0]),1)
            for code,period in (("000001","2026-Q2"),(CODE,"2026-Q1")):
                with self.assertRaises(ValueError):parse_pdf(b"%PDF-test",code,period)
            page.extract_text.return_value=""
            with self.assertRaises(ValueError):parse_pdf(b"%PDF-test",CODE,"2026-Q2")
        with self.assertRaises(ValueError):FundReportSource().download("http://127.0.0.1/private")


class ReportServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp=TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.db=Database(f"sqlite:///{self.tmp.name}/test.db");self.db.create_schema();self.addCleanup(self.db.engine.dispose)
        self.service=FundReportService(self.db, directory=Path(self.tmp.name)/"pdf")
        self.seed()

    def seed(self):
        for i,doc_id in enumerate(IDS):
            # 测试原文与数字一致，当前读取会按保存的原文生成状态，不依赖陈旧facts缓存。
            table=[dict(page) for page in TABLE]
            if i==0:
                table[0]["text"]=table[0]["text"].replace("92.12", "90.00")
                table[1]["text"]=table[1]["text"].split("\n",1)[1]
            facts=extract_allocation(table)
            pages=[{"page":1,"text":"投资策略和运作分析：权益市场风险。"*40}]
            pages.extend({"page":page,"text":""} for page in range(2,12))
            pages.extend(table)
            with self.db.session_factory() as session:
                session.add(FundReportDocumentModel(id=doc_id,fund_code=CODE,period=f"2026-Q{i+1}",title=f"测试基金 2026年第{i+1}季度报告",
                    published_at=f"2026-0{4+i*3}-20",source_url="https://example.com/report.pdf",content_hash=str(i)*64,
                    pages=pages,facts=facts,
                    created_at=datetime.now(timezone.utc)))
                session.commit()

    def test_comparison_exact_percent_points_and_missing_is_not_zero(self):
        r=self.service.compare(CODE, IDS)
        self.assertEqual(r["allocation_changes"][1]["change_percentage_points"], "2.12")
        self.assertIsNone(r["allocation_changes"][-1]["before"])
        self.assertIsNone(r["allocation_changes"][-1]["change_percentage_points"])
        self.assertIn("#page=12",r["allocation_changes"][0]["after"]["source_url"])

    def test_wrong_fund_duplicate_and_missing_documents_are_rejected(self):
        for code,ids in (("000001",IDS),(CODE,[IDS[0]]),(CODE,[IDS[0],IDS[0]]),(CODE,[IDS[0],"missing"])):
            with self.assertRaises(ValueError):self.service.pair(code,ids)
        with self.assertRaises(ValueError):self.service.page("000001",IDS[0],1)
        with self.assertRaises(ValueError):self.service.page(CODE,IDS[0],14)

    def test_fallback_is_explicit_and_reads_each_quarter_without_sync(self):
        with patch.object(self.service.source,"announcements") as network:
            r=self.service.research(CODE,"投资策略风险",IDS)
        network.assert_not_called()
        self.assertEqual({e["period"] for e in r["evidence"]},{"2026-Q1","2026-Q2"})
        self.assertTrue(all(row["mode"]=="keyword" for row in r["retrieval"]))
        self.assertEqual(len(r["warnings"]),2)

    def test_compaction_preserves_both_quarters_numbers_and_source_links(self):
        payload=self.service.research(CODE,"投资策略风险",IDS)
        content=json.dumps(payload,ensure_ascii=False)
        compacted=json.loads(context_compactor_service._build_compacted_content(
            original_content=content,saved_path="unused.json",reason="test",original_chars=len(content),message=SimpleNamespace()))
        self.assertEqual(compacted["compact_strategy"],"fund_report_sections_v3")
        self.assertEqual({e["period"] for e in compacted["evidence"]},{"2026-Q1","2026-Q2"})
        self.assertEqual(compacted["allocation_changes"][1]["change_percentage_points"],"2.12")
        self.assertTrue(all(e["source_url"] and e["page"] for e in compacted["evidence"]))

    def test_vector_query_has_fund_and_version_filter_and_embeds_once(self):
        with self.db.session_factory() as session:
            for doc_id in IDS:session.get(FundReportDocumentModel,doc_id).index_version=self.service.index_version
            session.commit()
        collection=Mock();collection.query.side_effect=[{"ids":[[f"report:{doc_id}:p1:c0"]]} for doc_id in IDS]
        embedder=Mock();embedder.embed_query.return_value=[1.,0.]
        with patch.object(self.service,"_collection",return_value=collection),patch.object(self.service,"_embedder",return_value=embedder):
            result=self.service.research(CODE,"风险",IDS)
        embedder.embed_query.assert_called_once()
        for call,doc_id in zip(collection.query.call_args_list, IDS):
            self.assertEqual(call.kwargs["where"],{"$and":[{"fund_code":CODE},{"document_id":doc_id}]})
        self.assertTrue(all(r["mode"]=="hybrid" for r in result["retrieval"]))

    def test_numeric_question_skips_embedding_and_rerank_but_keeps_exact_table(self):
        with self.db.session_factory() as session:
            for doc_id in IDS:
                session.get(FundReportDocumentModel, doc_id).index_version = self.service.index_version
            session.commit()
        with patch.object(self.service, "_embedder") as embedding, patch.object(self.service.reranker, "rank") as rerank:
            result = self.service.research(CODE, "两期基金投资占比变化多少个百分点？", IDS)
        embedding.assert_not_called()
        rerank.assert_not_called()
        self.assertEqual(result["retrieval_diagnostics"]["rerank"]["status"], "not_needed")
        self.assertTrue(all(row["mode"] == "structured" for row in result["retrieval"]))
        self.assertEqual(result["allocation_changes"][1]["change_percentage_points"], "2.12")

    def test_embedding_budget_error_is_not_disguised_as_keyword_fallback(self):
        from app.services.ai_governance_context import AIControlError
        with self.db.session_factory() as session:
            for doc_id in IDS:
                session.get(FundReportDocumentModel, doc_id).index_version = self.service.index_version
            session.commit()
        with patch.object(self.service, "_collection"), patch.object(self.service, "_embedder", side_effect=AIControlError("budget", "额度不足")):
            with self.assertRaises(AIControlError):
                self.service.research(CODE, "经理的策略发生什么变化？", IDS)

    def test_sync_partial_embedding_does_not_lose_original_and_is_idempotent(self):
        items=[dict(fund_code=CODE,period=f"2026-Q{i}",title=f"季报{i}",published_at="2026-07-20",source_url=f"https://example.com/{i}.pdf") for i in (1,2)]
        source=Mock();source.announcements.return_value=items;source.download.side_effect=lambda url:url.encode()
        self.service.source=source
        with patch("app.services.fund_reports.parse_pdf",return_value=([{"page":1,"text":"已核对的公开原文"*50}],[])),patch.object(self.service,"_collection",side_effect=RuntimeError("索引不可用")):
            first=self.service.sync(CODE);second=self.service.sync(CODE)
        self.assertEqual(len(first["warnings"]),2)
        self.assertEqual(len(first["documents"]),len(second["documents"]))
        self.assertEqual(len(list((Path(self.tmp.name)/"pdf").glob("*.pdf"))),2)

    def test_cancel_is_not_swallowed_as_index_warning(self):
        self.service.source=Mock()
        self.service.source.announcements.return_value=[dict(period="2026-Q1",source_url="x",title="季报",published_at="2026-04-20")]*2
        self.service.source.download.return_value=b"pdf"
        def progress(step,message):
            if message=="检查任务状态":raise JobCancelledError("cancel")
        with patch("app.services.fund_reports.parse_pdf",return_value=([{"page":1,"text":"原文"*500}],[])),patch.object(self.service,"_collection",side_effect=JobCancelledError()):
            with self.assertRaises(JobCancelledError):
                self.service.sync(CODE,progress_callback=progress)

    def test_context_inherits_exact_versions_and_clears_on_subject_change(self):
        request=AgentChatRequest(message="分析季报",fund_code=CODE,fund_report_ids=IDS)
        ref,context=resolve_fund_report_context(request,{},self.service)
        self.assertIn(IDS[0],context["content"])
        following=AgentChatRequest(message="风险呢")
        self.assertEqual(resolve_fund_report_context(following,{"fund_report_reference":ref},self.service)[0],ref)
        for update in ({"fund_report_ids":[]},{"fund_code":"000001"},{"portfolio_id":"portfolio_1234567890abcdef"}):
            self.assertEqual(resolve_fund_report_context(following.model_copy(update=update),{"fund_report_reference":ref},self.service),(None,None))
        with self.assertRaises(AgentRuntimeError):resolve_fund_report_context(request.model_copy(update={"fund_code":"000001"}),{},self.service)

    def test_http_validation_and_read_only_endpoints(self):
        with patch("app.routers.fund_reports.fund_report_service",self.service):
            client=TestClient(app)
            self.assertEqual(client.get(f"/api/v1/funds/{CODE}/reports").status_code,200)
            self.assertEqual(client.get(f"/api/v1/funds/{CODE}/reports/compare",params=[("document_ids",i) for i in IDS]).status_code,200)
            self.assertEqual(client.get(f"/api/v1/funds/000001/reports/{IDS[0]}/pages/1").status_code,404)
            self.assertEqual(client.get(f"/api/v1/funds/{CODE}/reports/{IDS[0]}/pages/0").status_code,422)
            self.assertEqual(client.post(f"/api/v1/funds/{CODE}/reports/sync",json={"idempotency_key":"x"}).status_code,422)

    def test_route_and_trusted_tool_inputs(self):
        request=AgentChatRequest(message="风险呢",fund_code=CODE,fund_report_ids=IDS)
        route=agent_request_router.route(request)
        self.assertEqual(route.tool_names,("get_fund_report_research_tool",))
        # 执行器不接受模型把基金或季度换成另一个对象。
        executor=AgentToolExecutor()
        args={"fund_code":"000001","document_ids":["wrong"],"question":"wrong"}
        executor._inject_trusted_arguments(tool_name="get_fund_report_research_tool",tool_args=args,request=request,
            execution_context=AgentExecutionContext.product(user_id=request.user_id))
        self.assertEqual(args,{"fund_code":CODE,"document_ids":IDS,"question":"风险呢"})
        prompt=prompt_builder_service.build(user_id=request.user_id,tool_names=list(route.tool_names),
            active_fund_code=CODE,domains=route.domains)
        self.assertIn("当前选中的基金代码：110020",prompt.system_prompt)


class ReportChatTests(unittest.IsolatedAsyncioTestCase):
    async def test_two_rounds_persist_only_reference_not_duplicate_pdf_messages(self):
        with TemporaryDirectory() as directory:
            database=Database(f"sqlite:///{directory}/chat.db")
            agent=FakeAgent();conversations=ConversationService(database)
            service=ComprehensiveAgentService(conversations=conversations,agent=agent,runtime_state=FakeRuntimeState(),observability=AgentObservabilityService(database))
            docs=[dict(id=doc_id,fund_code=CODE,period=f"2026-Q{i+1}") for i,doc_id in enumerate(IDS)]
            with patch("app.services.fund_report_context.fund_report_service.pair",return_value=docs) as reader:
                first=await service.chat(AgentChatRequest(message="分析季报",user_id="chen",fund_code=CODE,fund_report_ids=IDS))
                second=await service.chat(AgentChatRequest(message="继续解释",user_id="chen",conversation_id=first.conversation.id))
                self.assertEqual(second.assistant_message.run_metadata["fund_report_reference"]["document_ids"],IDS)
                self.assertEqual(reader.call_count,2)
                self.assertEqual(len(conversations.detail(user_id="chen",conversation_id=first.conversation.id)["messages"]),4)
                self.assertIn(IDS[0],agent.histories[-1][-1]["content"])
            database.engine.dispose()
