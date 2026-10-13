"""季报检索的排序与执行预算，不负责下载文件或生成投资结论。

BM25 用中文词项找到字面相关的段落，向量找到语义相关的段落，RRF 只合并排名。
可选精排再对候选逐项打分；最终仍由原文校验器核对引用，排名不代表事实正确性。
所有结果沿用原 evidence_id，历史对话不会因检索算法升级而失去原文定位。
"""

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from functools import lru_cache
import math
import re
from time import monotonic
import unicodedata
from urllib.parse import urlsplit

import httpx
from rank_bm25 import BM25Plus

from app.config import settings
from app.services.agent_authorization import check_active_task_authorization
from app.services.ai_governance_context import current_ai_scope
from app.services.governed_model import model_permit
from app.services.tracing import traced


RETRIEVAL_VERSION = "report-bm25-rrf-v1"
CANDIDATES_PER_QUARTER = 12
RESULTS_PER_QUARTER = 4
RRF_CONSTANT = 60
_DEADLINE = ContextVar("fund_report_retrieval_deadline", default=None)
_STOP_WORDS = frozenset({"请问", "帮我", "告诉", "一下", "这个", "那个", "什么", "如何", "是否", "哪些"})


@contextmanager
def report_retrieval_scope(deadline=None):
    """将外层问答的绝对截止时间传播给同步检索，排队/多季度查询不能各自重置预算。"""
    previous = _DEADLINE.get()
    value = min(previous, deadline) if previous is not None and deadline is not None else (
        previous if deadline is None else deadline)
    token = _DEADLINE.set(value)
    try:
        yield
    finally:
        _DEADLINE.reset(token)


def check_retrieval_active():
    from app.services.chat_execution_budget import check_chat_active
    check_chat_active()
    check_active_task_authorization()
    check = current_ai_scope().check_cancelled
    if check:
        check()
    deadline = _DEADLINE.get()
    if deadline is not None and monotonic() >= deadline:
        raise TimeoutError("季报检索时间预算已用完")


def retrieval_timeout(limit):
    check_retrieval_active()
    deadline = _DEADLINE.get()
    from app.services.chat_execution_budget import chat_deadline
    shared = chat_deadline()
    if shared is not None:
        deadline = min(deadline, shared) if deadline is not None else shared
    return min(limit, max(0.001, deadline - monotonic())) if deadline is not None else limit


@lru_cache(maxsize=1)
def _tokenizer():
    import jieba
    # 独立分词器，不修改其他模块的全局词典。金融专有词保留，搜索模式兼顾短词。
    tokenizer = jieba.Tokenizer()
    for word in ("基金经理", "半导体", "资产配置", "固定收益", "净资产", "总资产", "回撤", "久期"):
        tokenizer.add_word(word)
    return tokenizer


def tokenize(text):
    normalized = unicodedata.normalize("NFKC", text).lower()
    return [word for word in _tokenizer().cut_for_search(normalized)
            if word not in _STOP_WORDS and re.fullmatch(r"[a-z0-9\u4e00-\u9fff]+", word)
            and (len(word) > 1 or word.isdigit())]


def bm25_rank(question, chunks, limit=CANDIDATES_PER_QUARTER):
    """每季度建立小型词项索引。无词项命中的块不因稳定排序而被硬凑进结果。

    BM25Plus 在很小的季报语料上保持正的 IDF；它的 delta 会给未命中块也加常数，
    所以仍须显式检查词项交集。页级证据及金融表格的强制保留由调用方负责。
    """
    tokens = tokenize(question)
    corpus = [tokenize(chunk["text"]) for chunk in chunks]
    if not tokens or not any(corpus):
        return []
    scores = BM25Plus(corpus).get_scores(tokens)
    query = set(tokens)
    indexes = [i for i, terms in enumerate(corpus) if query.intersection(terms)]
    indexes.sort(key=lambda i: (-float(scores[i]), chunks[i]["evidence_id"]))
    return [{"evidence_id": chunks[i]["evidence_id"], "score": float(scores[i])}
            for i in indexes[:limit]]


def fuse_rankings(keyword_rows, vector_ids):
    """RRF 使用名次而不是把 BM25 分数与余弦距离直接相加，去重后保留两路出处。"""
    merged = {}
    for source, ids in (("bm25", [row["evidence_id"] for row in keyword_rows]), ("vector", vector_ids)):
        for rank, key in enumerate(dict.fromkeys(ids), 1):
            row = merged.setdefault(key, {"evidence_id": key, "rrf_score": 0.0, "ranks": {}})
            row["ranks"][source] = rank
            row["rrf_score"] += 1 / (RRF_CONSTANT + rank)
    return sorted(merged.values(), key=lambda row: (-row["rrf_score"], row["evidence_id"]))


class RerankUnavailable(RuntimeError):
    """仅外部精排故障允许回退；额度/授权/取消异常不属于此类型。"""


@dataclass(frozen=True)
class RerankResult:
    scores: dict[str, float]
    usage: dict | None
    model: str


class DashScopeReportReranker:
    """百炼 qwen3-rerank 兼容协议，通过原有治理入口预留、限流和结算。"""

    def __init__(self, *, url=None, api_key=None, model=None, timeout=None, transport=None):
        self.url = settings.fund_report_rerank_url if url is None else url
        self.key = settings.dashscope_api_key if api_key is None else api_key
        self.model = model or settings.fund_report_rerank_model
        self.timeout = settings.fund_report_rerank_timeout_seconds if timeout is None else timeout
        self.transport = transport

    @property
    def available(self):
        return bool(self.url and self.key)

    @traced("rag.rerank", **{"langfuse.observation.type": "retriever"})
    def rank(self, question, candidates):
        # 地址只能来自服务端配置，不能由帖子或模型提供；不跟随跳转，避免凭据外带。
        parts = urlsplit(self.url)
        if not self.available or parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
            raise RerankUnavailable("精排服务未正确配置")
        if not math.isfinite(self.timeout) or self.timeout <= 0:
            raise RerankUnavailable("精排超时配置无效")
        if not candidates:
            return RerankResult({}, {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}, self.model)
        payload = {"model": self.model, "query": question,
                   "documents": [row["text"] for row in candidates], "top_n": len(candidates)}
        timeout = retrieval_timeout(self.timeout)
        # 排序没有生成 token，预留时避免默认多预留一份回答输出；用量缺失仍保留待确认。
        with model_permit(provider="dashscope-rerank", model=self.model,
                          payload={**payload, "max_tokens": 0}, timeout=timeout) as permit:
            # 排队可能已消耗预算，因此发送前再次核对并缩短超时。
            timeout = retrieval_timeout(self.timeout)
            try:
                with httpx.Client(timeout=timeout, follow_redirects=False, transport=self.transport) as client:
                    response = client.post(self.url, headers={"Authorization": f"Bearer {self.key}"}, json=payload)
                    response.raise_for_status()
                    data = response.json()
                raw_usage = data.get("usage") if isinstance(data, dict) else None
                total = (raw_usage or {}).get("total_tokens")
                if type(total) is int and total >= 0:
                    permit.usage = {"input_tokens": total, "output_tokens": 0, "total_tokens": total}
                # 先记真实计費用量，再验证协议；格式不合格也不能把已经发生的调用记为零。
                rows = data["results"]
                if not isinstance(rows, list) or len(rows) != len(candidates):
                    raise ValueError("精排候选数量不完整")
                scores = {}
                seen = set()
                for row in rows:
                    index, score = row["index"], row["relevance_score"]
                    if (type(index) is not int or index in seen or not 0 <= index < len(candidates)
                            or type(score) not in (int, float) or not math.isfinite(score) or not 0 <= score <= 1):
                        raise ValueError("精排索引或分数无效")
                    seen.add(index)
                    scores[candidates[index]["evidence_id"]] = float(score)
            except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
                # 不回传供应商响应体、URL或请求正文，错误诊断只保留故障类别。
                raise RerankUnavailable(f"精排服务不可用（{type(exc).__name__}）") from exc
            permit.check()
            check_retrieval_active()
            return RerankResult(scores, permit.usage, self.model)


def select_ranked_evidence(question, groups, *, reranker=None, numeric_only=False):
    """合并候选只调用一次精排，再按季度分配名额，避免某期长文占满全部上下文。"""
    check_retrieval_active()
    candidates = [row for group in groups for row in group["candidates"]]
    detail = {"version": RETRIEVAL_VERSION, "candidate_count": len(candidates),
              "rerank": {"status": "not_configured", "model": None}, "quarters": []}
    scores = {}
    if numeric_only:
        detail["rerank"]["status"] = "not_needed"
    elif not candidates:
        detail["rerank"]["status"] = "no_candidates"
    elif reranker is not None and reranker.available:
        try:
            result = reranker.rank(question, candidates)
            scores = result.scores
            detail["rerank"] = {"status": "applied", "model": result.model, "usage": result.usage}
        except RerankUnavailable as exc:
            # 限流、取消、授权撤销和总时限均不在此异常范围内，必须向上层传播。
            detail["rerank"] = {"status": "fallback", "model": reranker.model, "reason": str(exc)}
    selected = []
    for group in groups:
        rows = group["ranks"]
        if scores:
            rows = sorted(rows, key=lambda row: (-scores[row["evidence_id"]], -row["rrf_score"], row["evidence_id"]))
        # 保留原路径的上下文规模：本地关键词两条、混合检索四条。提升召回候选量
        # 并不意味着必须把所有候选塞给生成模型，也不靠扩大上下文伪装排名改进。
        limit = group.get("result_limit", RESULTS_PER_QUARTER)
        ids = [row["evidence_id"] for row in rows[:limit]]
        lookup = {row["evidence_id"]: row for row in group["candidates"]}
        selected.extend(lookup[key] for key in ids)
        # 只输出入选项的排名诊断，不把所有落选候选再次塞进模型上下文。
        detail["quarters"].append({"document_id": group["document_id"], "period": group["period"],
            "candidate_count": len(rows), "selected_ids": ids,
            "selected_ranks": [{**row, "rerank_score": scores.get(row["evidence_id"])}
                               for row in rows[:limit]]})
    check_retrieval_active()
    return selected, detail
