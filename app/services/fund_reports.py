"""季报用例：手动同步、两期对比、按基金和文档版本检索，不在这里生成 AI 答案。"""

from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import re
from pathlib import Path
from uuid import uuid4
from contextlib import contextmanager
from contextvars import ContextVar

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.config import settings
from app.services.tracing import traced
from app.services.ai_governance_context import AIControlError
from app.services.job_runtime import JobCancelledError
from app.database import PROJECT_ROOT, runtime_database
from app.models import FundReportDocumentModel
from app.services.fund_report_source import (
    FundReportSource, page_chunks, parse_pdf, manager_chunks, manager_section_coverage,
    report_dates, extract_allocation_table, allocation_facts, allocation_chunks,
)
from app.services.fund_report_query import report_query_profile
from app.services.fund_report_retrieval import (
    CANDIDATES_PER_QUARTER, DashScopeReportReranker, bm25_rank, fuse_rankings,
    select_ranked_evidence, check_retrieval_active, retrieval_timeout,
)


class FundReportService:
    def __init__(self, database=runtime_database, source=None, directory=None, chroma_directory=None,
                 reranker=None):
        self.database = database
        self.source = source or FundReportSource()
        self.directory = Path(directory or settings.fund_report_storage_dir or PROJECT_ROOT / "data/fund_reports")
        # 评测传入独立目录，不会把固定考卷写进用户的产品向量集合。
        self.chroma_directory = chroma_directory
        # 排序客户端不建立网络连接；空配置会在诊断中标记，不冒充已执行模型精排。
        self.reranker = reranker if reranker is not None else DashScopeReportReranker()

    @property
    def index_version(self):
        # 切换模型/维度后使用新集合，不能拿不同维度的向量在原集合继续检索。
        signature = f"{settings.embedding_base_url}|{settings.embedding_model}|{settings.embedding_dimensions}|pages-v1"
        return "fund_reports_" + hashlib.sha256(signature.encode()).hexdigest()[:16]

    def _collection(self):
        import chromadb
        path = Path(self.chroma_directory or settings.chroma_persist_dir)
        if not path.is_absolute():
            path = PROJECT_ROOT / path
        return chromadb.PersistentClient(path=str(path)).get_or_create_collection(
            name=self.index_version, metadata={"hnsw:space": "cosine"})

    def _embedder(self):
        from app.services.vector_rag import DashScopeEmbeddingClient
        return DashScopeEmbeddingClient(timeout=retrieval_timeout(15), max_retries=0, governed=True)

    @staticmethod
    def _code(code):
        if not re.fullmatch(r"\d{6}", code):
            raise ValueError("请提供六位基金代码。")

    def _documents(self, code):
        self._code(code)
        with self.database.session_factory() as session:
            return list(session.scalars(select(FundReportDocumentModel).where(
                FundReportDocumentModel.fund_code == code).order_by(
                FundReportDocumentModel.period.desc(), FundReportDocumentModel.published_at.desc(),
                FundReportDocumentModel.created_at.desc())))

    def _info(self, row, *, full=False):
        info = {key: getattr(row, key) for key in (
            "id", "fund_code", "period", "title", "published_at", "source_url", "content_hash")}
        # 原文已经持久化：升级解析规则时按原文生成只读视图，不覆盖历史PDF或旧问答，
        # 也不要求用户重下报告。数字和状态使用同一次解析，防止旧缓存数字与新状态矛盾。
        table = extract_allocation_table(row.pages)
        for item in table["rows"]:
            if item["page"] is not None:
                item.update(evidence_id=f"report:{row.id}:p{item['page']}:allocation",
                            source_url=f"{row.source_url}#page={item['page']}")
        info.update(report_dates(row.period))
        info["facts"] = allocation_facts(table)
        info.update(page_count=len(row.pages), semantic_ready=row.index_version == self.index_version)
        if full:
            info["pages"] = row.pages
            info["allocation_table"] = table
        return info

    def list_reports(self, fund_code, document_ids=None):
        if document_ids is not None:
            # 从旧对话返回时精确读取旧版本，不受最近 20 条列表的分页上限影响。
            pair = self.pair(fund_code, document_ids)
            return {"fund_code": fund_code, "documents": [
                {k: v for k, v in doc.items() if k not in ("pages", "allocation_table")} for doc in pair],
                "selected_ids": [doc["id"] for doc in pair]}
        rows = self._documents(fund_code)
        # 历史更正版本仍保留供旧对话引用；新分析默认选择两个不同季度的最新版本。
        latest = {}
        for row in rows:
            latest.setdefault(row.period, row.id)
        return {"fund_code": fund_code, "documents": [self._info(row) for row in rows[:20]],
                "selected_ids": list(latest.values())[:2],
                "notice": "历史季度披露，不代表实时持仓。首次同步会下载公开 PDF 并调用 embedding。"}

    def pair(self, fund_code, document_ids=None):
        self._code(fund_code)
        ids = document_ids if document_ids is not None else self.list_reports(fund_code)["selected_ids"]
        if len(ids) != 2 or len(set(ids)) != 2:
            raise ValueError("请先手动同步并选择两个不同季度的季报。")
        # 编号、基金和季度一起检查，不能借另一个基金的 PDF 回答当前问题。
        with self.database.session_factory() as session:
            rows = list(session.scalars(select(FundReportDocumentModel).where(
                FundReportDocumentModel.id.in_(ids), FundReportDocumentModel.fund_code == fund_code)))
        if len(rows) != 2 or rows[0].period == rows[1].period:
            raise ValueError("所选季报不存在、基金不匹配，或属于同一季度。")
        return [self._info(row, full=True) for row in sorted(rows, key=lambda row: row.period)]

    def page(self, fund_code, document_id, page):
        with self.database.session_factory() as session:
            row = session.get(FundReportDocumentModel, document_id)
        if not row or row.fund_code != fund_code or not 1 <= page <= len(row.pages):
            raise ValueError("找不到该基金的原文页。")
        return {**self._info(row), **row.pages[page - 1],
                "source_url": f"{row.source_url}#page={page}"}

    def sync(self, fund_code, progress_callback=lambda step, message: None, *, build_index=True):
        self._code(fund_code)
        progress_callback(1, "正在查找最近两期公开季报")
        announcements = self.source.announcements(fund_code)
        if len(announcements) < 2:
            raise ValueError("公开来源暂未找到两个不同季度的完整季报。已有资料未删除。")
        warnings = []
        for info in announcements:
            check_retrieval_active()
            progress_callback(2, f"正在读取 {info['period']} 季报")
            content = self.source.download(info["source_url"])
            digest = hashlib.sha256(content).hexdigest()
            document_id = "fr_" + hashlib.sha256(f"{fund_code}:{digest}".encode()).hexdigest()[:32]
            pages, facts = parse_pdf(content, fund_code, info["period"])
            check_retrieval_active()
            progress_callback(2, f"已核对 {info['period']}，保存 {len(pages)} 页原文")
            self.directory.mkdir(parents=True, exist_ok=True)
            # 内容寻址 + 原子替换。重复同步不会覆盖另一个版本或聊天已引用的原文。
            path = self.directory / f"{document_id}.pdf"
            temporary = path.with_suffix(f".{uuid4().hex}.tmp")
            try:
                temporary.write_bytes(content)
                temporary.replace(path)
            finally:
                temporary.unlink(missing_ok=True)
            with self.database.session_factory() as session:
                if not session.get(FundReportDocumentModel, document_id):
                    session.add(FundReportDocumentModel(id=document_id, fund_code=fund_code,
                        period=info["period"], title=info["title"], published_at=info["published_at"],
                        source_url=info["source_url"], content_hash=digest, pages=pages, facts=facts,
                        created_at=datetime.now(timezone.utc)))
                    try:
                        session.commit()
                    except IntegrityError:
                        session.rollback()  # 两个手动任务同时导入同一文件时复用已有记录。
                row = session.get(FundReportDocumentModel, document_id)
                indexed = row.index_version == self.index_version
                document = self._info(row, full=True)
            # 缺季度补查先保证可读原文，不隐式启动整份文档的付费向量化。
            if indexed or not build_index:
                continue
            progress_callback(3, f"正在建立 {info['period']} 的页码检索索引")
            try:
                self.index_document(document, lambda message: progress_callback(3, message))
            except (AIControlError, JobCancelledError):
                raise
            except Exception:
                # Job 的取消和超时必须传播，不降级成一次看似成功的同步。
                progress_callback(3, "检查任务状态")
                warnings.append(f"{info['period']} 原文已保存，语义索引未完成；暂用原文关键词检索，可手动重试。")
                continue
        return {"ok": True, **self.list_reports(fund_code), "warnings": warnings}

    def index_document(self, document, progress_callback=lambda message: None):
        """产品同步和隔离评测走同一套切块/embedding，不能为评测另造一个简化检索器。"""
        chunks = page_chunks(document)
        collection, embedder = self._collection(), self._embedder()
        for start in range(0, len(chunks), 10):
            check_retrieval_active()
            progress_callback(f"{document['fund_code']} {document['period']} 索引 {min(start + 10, len(chunks))}/{len(chunks)}")
            batch = chunks[start:start + 10]
            vectors = embedder.embed_texts([item["text"] for item in batch])
            collection.upsert(ids=[item["evidence_id"] for item in batch], embeddings=vectors,
                documents=[item["text"] for item in batch],
                metadatas=[{k: v for k, v in item.items() if k != "text"} for item in batch])
        # 只有最后一个批次也成功，才允许后续检索把文档视为语义索引就绪。
        progress_callback(f"{document['period']} 索引完成")
        with self.database.session_factory() as session:
            row = session.get(FundReportDocumentModel, document["id"])
            row.index_version = self.index_version
            session.commit()

    def compare(self, fund_code, document_ids=None):
        return self._compare_documents(self.pair(fund_code, document_ids))

    def _compare_documents(self, documents):
        """普通比较和RAG研究共用同一份已核对文档，避免重复读取、重复解析。"""
        before, after = documents
        for doc in (before, after):
            for fact in doc["facts"]:
                fact["evidence_id"] = f"report:{doc['id']}:p{fact['page']}:allocation"
                fact["source_url"] = f"{doc['source_url']}#page={fact['page']}"
        old = {item["label"]: item for item in before["facts"]}
        new = {item["label"]: item for item in after["facts"]}
        old_states = {item["label"]: item["status"] for item in before["allocation_table"]["rows"]}
        new_states = {item["label"]: item["status"] for item in after["allocation_table"]["rows"]}
        changes = []
        for label in dict.fromkeys([*old, *new]):
            left, right = old.get(label), new.get(label)
            delta = (str(Decimal(right["ratio_percent"]) - Decimal(left["ratio_percent"]))
                     if left and right and left["denominator"] == right["denominator"] else None)
            changes.append({"label": label, "before": left, "after": right,
                            "before_status": old_states[label], "after_status": new_states[label],
                            "change_percentage_points": delta, "denominator": "基金总资产"})
        return {"fund_code": before["fund_code"], "documents": [
            {key: value for key, value in document.items() if key not in ("pages", "allocation_table")} for document in documents],
            "allocation_changes": changes,
            # 单独保留八类主项的状态，包含“两期都没有数字”的行；旧数字变化接口不补零。
            "allocation_tables": [{"document_id": doc["id"], "period": doc["period"],
                                   **doc["allocation_table"]} for doc in documents],
            "limitations": ["只比较标准资产组合表中成功提取的数字；未提取不等于零。",
                "资产表数据截至data_as_of（按已核对季度计算的季末），published_at仅为公告披露日。",
                "reported_dash是原表横杠，not_found是未找到主项，parse_failed是行已找到但未可靠解析；均不补零或计算差值。",
                "比例变化不等于交易流水，不能据此声称买入、清仓或识别当日持仓。",
                "两期期末金额及占比不能分解期间买卖、申赎、估值的贡献，不能确定是主动或被动变化。",
                "ETF 联接基金的大部分资产可能在目标 ETF；这不是目标 ETF 的穿透持仓。",
                "经理观点是历史表述，不能作为当前行情或未来收益的保证。"]}

    @traced("rag.retrieve", **{"langfuse.observation.type": "retriever"})
    def research(self, fund_code, question, document_ids=None):
        check_retrieval_active()
        documents = self.pair(fund_code, document_ids)
        result = self._compare_documents(documents)
        # 每个季度独立召回，避免相关性排序只选中较长的那一期。
        evidence, modes, warnings, groups = [], [], [], []
        numeric_only = report_query_profile(question, fund_code)["mode"] == "allocation"
        vector_query = collection = None
        # 纯主项数字题已有确定性表格，无须为查一个比例再调用 embedding 和精排。
        if not numeric_only and any(doc["semantic_ready"] for doc in documents):
            try:
                collection = self._collection()
                vector_query = self._embedder().embed_query(question)
            except (AIControlError, JobCancelledError):
                raise
            except Exception:
                check_retrieval_active()
                warnings.append("本轮语义检索不可用，改用保存的原文关键词检索。")
        for document in documents:
            check_retrieval_active()
            chunks = page_chunks(document)
            lookup = {chunk["evidence_id"]: chunk for chunk in chunks}
            keyword_rows = [] if numeric_only else bm25_rank(question, chunks)
            vector_ids = []
            mode = "structured" if numeric_only else "keyword"
            if chunks and document["semantic_ready"] and vector_query is not None:
                try:
                    query = collection.query(query_embeddings=[vector_query],
                        n_results=min(CANDIDATES_PER_QUARTER, len(chunks)), where={"$and": [
                            {"fund_code": fund_code}, {"document_id": document["id"]}]}, include=["distances"])
                    vector_ids = list(dict.fromkeys(key for key in query["ids"][0] if key in lookup))
                    if vector_ids:
                        mode = "hybrid"
                except (AIControlError, JobCancelledError):
                    raise
                except Exception:
                    check_retrieval_active()
                    warnings.append(f"{document['period']} 语义检索暂不可用，本轮回退原文关键词检索。")
            elif not numeric_only:
                warnings.append(f"{document['period']} 本轮使用原文关键词检索。")
            ranks = fuse_rankings(keyword_rows, vector_ids)[:CANDIDATES_PER_QUARTER]
            groups.append({"document_id": document["id"], "period": document["period"], "ranks": ranks,
                           "result_limit": 4 if mode == "hybrid" else 2,
                           "candidates": [lookup[row["evidence_id"]] for row in ranks]})
            modes.append({"period": document["period"], "mode": mode})
        selected, diagnostics = select_ranked_evidence(question, groups, reranker=self.reranker,
                                                       numeric_only=numeric_only)
        if diagnostics["rerank"]["status"] == "fallback":
            warnings.append("本轮精排不可用，保留 BM25/向量融合排名；未将回退结果标记为模型精排。")
        for document in documents:
            quarter = [item for item in selected if item["document_id"] == document["id"]]
            # 运作分析按章节提取，避免页尾标题和下一页正文被普通切块分开。
            if not numeric_only:
                quarter.extend(manager_chunks(document))
            # 配置表的横杠没有embedding意义上的“观点”，仍是回答缺失值问题所需的原始证据。
            quarter.extend(allocation_chunks(document))
            evidence.extend({item["evidence_id"]: item for item in quarter}.values())
        diagnostics["selected_evidence_count"] = len(evidence)
        diagnostics["selected_evidence_chars"] = sum(len(item["text"]) for item in evidence)
        diagnostics["query_mode"] = "allocation" if numeric_only else "semantic"
        return {"ok": True, "data_kind": "fund_report_research", **result, "retrieval": modes, "evidence": evidence,
                "evidence_ids": [item["evidence_id"] for item in evidence], "warnings": warnings,
                "retrieval_diagnostics": diagnostics,
                # 即使结果很短、不触发压缩，也要让模型知道章节是否有缺口。
                "manager_sections": [manager_section_coverage(doc, evidence) for doc in documents]}


fund_report_service = FundReportService()

# 仅服务端评测代码能进入此上下文；它随 asyncio/to_thread 传播，不修改全局实例。
_REPORT_READER = ContextVar("fund_report_reader", default=None)


@contextmanager
def fund_report_reader_scope(reader):
    token = _REPORT_READER.set(reader)
    try:
        yield
    finally:
        _REPORT_READER.reset(token)


def get_fund_report_reader():
    return _REPORT_READER.get() or fund_report_service
