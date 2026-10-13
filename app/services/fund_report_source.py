"""季报来源与确定性解析：只访问公开公告，不让模型猜数值或下载任意网址。"""

from datetime import date
from decimal import Decimal
from io import BytesIO
import re
import time

import httpx
from pypdf import PdfReader


def report_period(title: str) -> str | None:
    match = re.search(r"(20\d{2})\s*年\s*第\s*([1234一二三四])\s*季度报告", title)
    if not match:
        return None
    quarter = str("一二三四".index(match[2]) + 1) if match[2] in "一二三四" else match[2]
    return f"{match[1]}-Q{quarter}"


def report_dates(period: str) -> dict:
    """季报的期末资产是季度末快照，公告发布得晚，不会让这份快照变成更新的数据。

    这里按已核对的季度计算日历季末，并明确计算依据；不是从披露日倒推，也不
    把季末套到经理展望等所有内容上。未知季度不拿公告日期充当数据日期。
    """
    match = re.fullmatch(r"(20\d{2})-Q([1-4])", period)
    period_end = None
    if match:
        month, day = ((3, 31), (6, 30), (9, 30), (12, 31))[int(match[2]) - 1]
        period_end = date(int(match[1]), month, day).isoformat()
    return {"period_end": period_end, "data_as_of": period_end,
            "data_as_of_basis": "report_quarter_end" if period_end else "unknown"}


def select_quarters(rows: list[dict], fund_code: str, today: date | None = None) -> list[dict]:
    """一季度可能有更正版本：先取该季度最新公告，再选最近两个不同季度。"""
    today = today or date.today()
    selected = {}
    for row in rows:
        title = str(row.get("TITLE", ""))
        period = report_period(title)
        published = str(row.get("PUBLISHDATE", ""))[:10]
        announcement = str(row.get("ID", ""))
        if (str(row.get("FUNDCODE")) != fund_code or not period or "摘要" in title
                or not re.fullmatch(r"AN\d{18}", announcement)):
            continue
        try:
            published_date = date.fromisoformat(published)
        except ValueError:
            continue
        if published_date > today or published_date.year < int(period[:4]):
            continue
        item = {"fund_code": fund_code, "period": period, "title": title,
                "published_at": published, "announcement_id": announcement,
                "source_url": f"https://pdf.dfcfw.com/pdf/H2_{announcement}_1.pdf"}
        if period not in selected or published > selected[period]["published_at"]:
            selected[period] = item
    return [selected[key] for key in sorted(selected, reverse=True)[:2]]


class FundReportSource:
    """小范围手动同步：一只基金、最多两个 PDF，网络超时与文件大小都有上限。"""

    def announcements(self, fund_code: str) -> list[dict]:
        from app.services.fund_report_retrieval import retrieval_timeout
        with httpx.Client(timeout=retrieval_timeout(20), follow_redirects=False) as client:
            response = client.get("https://api.fund.eastmoney.com/f10/JJGG",
                params={"fundcode": fund_code, "pageIndex": 1, "pageSize": 30, "type": 3},
                headers={"Referer": "https://fundf10.eastmoney.com/", "User-Agent": "Mozilla/5.0"})
            response.raise_for_status()
            rows = response.json().get("Data")
        if not isinstance(rows, list):
            raise ValueError("公告来源暂时不可用，请稍后手动重试。")
        return select_quarters(rows, fund_code)

    def download(self, url: str) -> bytes:
        from app.services.fund_report_retrieval import check_retrieval_active, retrieval_timeout
        # URL 只由经过校验的公告编号生成，拒绝跳转，避免成为任意内网下载器。
        if not re.fullmatch(r"https://pdf\.dfcfw\.com/pdf/H2_AN\d{18}_1\.pdf", url):
            raise ValueError("不是支持的公开季报来源。")
        content = bytearray()
        started = time.monotonic()
        with httpx.stream("GET", url, timeout=retrieval_timeout(20), follow_redirects=False) as response:
            response.raise_for_status()
            for part in response.iter_bytes():
                check_retrieval_active()
                if time.monotonic() - started > 40:
                    raise ValueError("季报下载超过 40 秒，请稍后手动重试。")
                content.extend(part)
                if len(content) > 10 * 1024 * 1024:
                    raise ValueError("季报超过 10 MB，本次不导入。")
        return bytes(content)


def parse_pdf(content: bytes, fund_code: str, period: str) -> tuple[list[dict], list[dict]]:
    """先验证封面和基金代码，再用 pypdf 提取物理页；扫描件不伪装成成功。"""
    if not content.startswith(b"%PDF"):
        raise ValueError("来源没有返回 PDF 文件。")
    reader = PdfReader(BytesIO(content))
    if reader.is_encrypted or not 1 <= len(reader.pages) <= 80:
        raise ValueError("暂不支持加密或超过 80 页的季报。")
    pages = [{"page": i + 1, "text": page.extract_text() or ""} for i, page in enumerate(reader.pages)]
    front = "\n".join(page["text"] for page in pages[:5])
    if report_period(front) != period or not re.search(rf"(?<!\d){re.escape(fund_code)}(?!\d)", front):
        raise ValueError("季报的基金代码或季度无法核对，未纳入分析。")
    if sum(len(page["text"].strip()) for page in pages) < 300:
        raise ValueError("季报没有足够的可提取文字，扫描件需要 OCR，未纳入分析。")
    return pages, extract_allocation(pages)


ALLOCATION_LABELS = ("权益投资", "基金投资", "固定收益投资", "贵金属投资", "金融衍生品投资",
                     "买入返售金融资产", "银行存款和结算备付金合计", "其他资产")


def extract_allocation_table(pages: list[dict]) -> dict:
    """读取5.1表的八类主项，分别说明数字、原表横杠、缺行和解析失败。

    不拿“其中：债券”等子项替代主项，也不读取5.2之后的净资产比例。只保留每行
    标签和两个单元格，避免把整份PDF塞给模型；不可靠的数字一律不参与差值计算。
    """
    label_patterns = [r"\s*".join(map(re.escape, label)) for label in ALLOCATION_LABELS]
    row_pattern = r"(?<!\d)[1-9]\s+(?:" + "|".join(label_patterns) + r")\s*"
    boundary = row_pattern + r"|(?<!\d)[1-9]\s+合计|其中\s*[:：]|资产支持证券"
    # 金额的千分位、两位小数必须完整匹配，不能把损坏的数字取一截当成解析成功。
    amount_pattern = r"(?:\d{1,3}(?:,\d{3})+|\d+)\.\d{2}"
    rows = {label: {"label": label, "status": "not_found", "amount_yuan": None,
                   "ratio_percent": None, "denominator": None, "page": None, "quote": None}
            for label in ALLOCATION_LABELS}
    active, found, basis, header = False, False, None, None
    for page in pages:
        text = page["text"]
        if not found:
            start = re.search(r"5\s*[.．]\s*1\s+报告期末基金资产组合情况", text)
            if not start:
                continue
            active, found, text = True, True, text[start.end():]
            first_row = re.search(row_pattern, text)
            header_text = re.sub(r"\s+", " ", text[:first_row.start() if first_row else 350]).strip()[:350]
            if "基金总资产" in re.sub(r"\s+", "", header_text):
                basis = "基金总资产"
            header = {"page": page["page"], "quote": header_text}
        if not active:
            break
        end = re.search(r"(?:^|\n)\s*5\s*[.．]\s*[2-9](?!\d)", text)
        if end:
            text, active = text[:end.start()], False
        compact = re.sub(r"\s+", " ", text)
        for label, label_pattern in zip(ALLOCATION_LABELS, label_patterns):
            match = re.search(rf"(?<!\d)[1-9]\s+{label_pattern}\s*", compact)
            if not match or rows[label]["status"] != "not_found":
                continue
            tail = compact[match.end():]
            stop = re.search(boundary, tail)
            # 行缺值时不能借下一行的序号或数字来凑齐两列，跨页断开的单行也不猜。
            cells = re.match(r"\s*(\S+)(?:\s+(\S+))?", tail[:stop.start() if stop else len(tail)])
            amount, ratio = (cells[1], cells[2]) if cells else (None, None)
            quote = compact[match.start():match.end() + (cells.end() if cells else 0)].strip()
            row = rows[label]
            row.update(status="parse_failed", page=page["page"], quote=quote[:240],
                       quote_truncated=len(quote) > 240, denominator=basis)
            if basis and amount and ratio and not row["quote_truncated"]:
                if re.fullmatch(amount_pattern, amount) and re.fullmatch(r"\d{1,3}\.\d{2}", ratio) and Decimal(ratio) <= 100:
                    row.update(status="value", amount_yuan=amount.replace(",", ""), ratio_percent=str(Decimal(ratio)))
                elif re.fullmatch(r"[-—–－]+", amount) and re.fullmatch(r"[-—–－]+", ratio):
                    row["status"] = "reported_dash"
    return {"status": "located" if basis else "unrecognized_header" if found else "not_found",
            "denominator": basis, "header": header, "rows": list(rows.values())}


def allocation_facts(table: dict) -> list[dict]:
    """旧的数字接口保持不变：真实的0.00是数字，横杠等状态不补成零。"""
    return [{key: row[key] for key in ("label", "amount_yuan", "ratio_percent", "denominator", "page", "quote")}
            for row in table["rows"] if row["status"] == "value"]


def extract_allocation(pages: list[dict]) -> list[dict]:
    return allocation_facts(extract_allocation_table(pages))


def allocation_chunks(document: dict) -> list[dict]:
    """直接提供资产表主项原文，不让横杠行的召回依赖问题关键词或向量排名。

    每期只有八个主项，每行最多240字、表头最多350字，且引用仍局限在一个物理页。
    这些是主项摘录，不是完整的表格图片或整个5.1章节。
    """
    table = document["allocation_table"]
    by_page = {}
    if table["header"]:
        by_page[table["header"]["page"]] = [table["header"]["quote"]]
    for row in table["rows"]:
        if row["quote"]:
            by_page.setdefault(row["page"], []).append(row["quote"])
    return [{"evidence_id": f"report:{document['id']}:p{page}:allocation",
             "document_id": document["id"], "fund_code": document["fund_code"], "period": document["period"],
             "page": page, "section": "allocation_table", "text": "\n".join(quotes),
             "source_url": f"{document['source_url']}#page={page}"} for page, quotes in by_page.items()]


def page_chunks(document: dict) -> list[dict]:
    """跨页不合并，900 字块、120 字重叠；每个引用始终对应一个确定的 PDF 页。"""
    chunks = []
    for page in document["pages"]:
        text = page["text"].strip()
        for offset in range(0, len(text), 780):
            snippet = text[offset:offset + 900]
            if len(snippet) < 25:
                continue
            chunks.append({"evidence_id": f"report:{document['id']}:p{page['page']}:c{offset // 780}",
                "document_id": document["id"], "fund_code": document["fund_code"],
                "period": document["period"], "page": page["page"], "text": snippet,
                "source_url": f"{document['source_url']}#page={page['page']}"})
    return chunks


def manager_chunks(document: dict) -> list[dict]:
    """经理段落常从页尾开始，按 4.4 标题截取，不能把标题前的履历当成经理观点。

    每段仍局限在同一物理页，尽量在句末切分。超过六段时保留开头和结尾，明确
    标记章节不完整；不能把没有送给模型的中间内容当作“原文没有提到”。
    """
    # 有些基金使用“基金的投资策略”，PDF 还会在汉字之间插入空格或换行。
    heading = (r"(?:^|\n)\s*4\s*[.．]\s*4\s*" + r"\s*".join("报告期内基金")
               + r"(?:\s*的)?\s*" + r"\s*".join("投资策略和运作分析"))
    boundary = r"(?:^|\n)\s*(?:4\s*[.．]\s*[5-9](?!\d)|§\s*5\s|5\s*[.．]\s*1\s)"
    found, closed, chunks = False, False, []
    for page in document["pages"]:
        text = page["text"]
        if not found:
            start = re.search(heading, text)
            if not start:
                continue
            found, text = True, text[start.end():]
        end = re.search(boundary, text)
        if end:
            text, closed = text[:end.start()], True
        text, offset, page_index = text.strip(), 0, 0
        while offset < len(text):
            stop = min(offset + 700, len(text))
            if stop < len(text):
                # 正文在 PDF 中的普通换行不一定是段落，优先使用句号/分号等语义边界。
                endings = list(re.finditer(r"[。！？；][”’\"']?|\n\s*\n", text[offset:stop]))
                if endings and endings[-1].end() >= 350:
                    stop = offset + endings[-1].end()
            # 小于25字的跨页尾句也保留。无重叠切块，拼接后仍是原来的整节文字。
            chunks.append({"evidence_id": f"report:{document['id']}:p{page['page']}:manager{page_index}",
                "document_id": document["id"], "fund_code": document["fund_code"], "period": document["period"],
                "page": page["page"], "text": text[offset:stop], "section": "manager_review",
                # 全节顺序在删减前确定；跨页引用只连接连续片段，不能跨过未读的中间段。
                "section_chunk_index": len(chunks),
                "source_url": f"{document['source_url']}#page={page['page']}"})
            offset, page_index = stop, page_index + 1
        if closed:
            break
    total = len(chunks)
    selected = chunks if total <= 6 else chunks[:3] + chunks[-3:]
    return [{**chunk, "section_total_chunks": total, "section_complete": closed and total <= 6}
            for chunk in selected]


def manager_section_coverage(document: dict, evidence: list[dict]) -> dict:
    """描述实际提供给模型的章节覆盖，不把“找到了一个片段”说成完整阅读。

    检索服务和压缩器用同一个判定，压缩后若少了一段，complete 也必须变成 partial。
    complete 仅指识别到边界的4.4章节文本，不代表整个PDF或PDF文字提取绝对无误。
    """
    rows = {row["evidence_id"]: row for row in evidence
            if row.get("document_id") == document["id"] and row.get("section") == "manager_review"}
    total = max((row.get("section_total_chunks", 0) for row in rows.values()), default=0)
    complete = bool(rows) and total == len(rows) and all(row.get("section_complete") for row in rows.values())
    return {"document_id": document["id"], "period": document.get("period"),
            "status": "complete" if complete else "partial" if rows else "not_found",
            "included_chunks": len(rows), "total_chunks": total}
