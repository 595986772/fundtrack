"""FundScope 市场事件的每日采集、增量入库和 Cron 注册。

这一层不调用大模型。Provider 只负责把第三方数据转换成统一事件；Repository 负责
幂等写入；MarketEventService 再把数据库事件提供给 REST、前端和 Agent Tool。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import hashlib
import re
from typing import Any, Callable, Protocol
from urllib.parse import urljoin

from app.config import settings
from app.services.market_event_fund_links import (
    MarketEventFundLinkService,
    market_event_fund_link_service,
)
from app.services.market_events import MarketEventRepository, market_event_repository


MARKET_EVENT_SYNC_SYSTEM_USER_ID = "fundscope-system"
MARKET_EVENT_SYNC_SCHEDULE_ID = "market-event-daily-sync"
AKSHARE_EVENT_SOURCE = "Eastmoney Fund + CCTV + Eastmoney Finance + CLS"
OFFICIAL_EVENT_SOURCE = "NDRC + CSRC official websites"
MARKET_EVENT_SYNC_SOURCE = f"{AKSHARE_EVENT_SOURCE} + {OFFICIAL_EVENT_SOURCE}"
DEFAULT_FUND_CODES = ("110020", "161725", "110007")

# 新闻联播包含大量非财经消息，只保留与 FundScope 风险研究相关的透明关键词。
FINANCIAL_KEYWORDS = (
    "经济",
    "消费",
    "财政",
    "货币",
    "利率",
    "证券",
    "资本市场",
    "基金",
    "债券",
    "国债",
    "银行",
    "保险",
    "监管",
    "工业",
    "就业",
    "物价",
    "零售",
    "投资",
    "外贸",
    "进出口",
    "关税",
    "收购",
    "增长",
    "数据",
    "发改委",
    "央行",
    "证监会",
    "上市公司",
    "房地产",
    "汇率",
    "产能",
    "融资",
    "投融资",
    "产业",
    "能源",
    "价格",
    "项目",
    "物流",
)

# 财经媒体比官方栏目内容更杂，不能继续用“增长、投资”这类宽泛词直接收录。
# 下面两组词要求标题明确指向权威发布主体或宏观市场主题。
MEDIA_AUTHORITY_KEYWORDS = (
    "国务院",
    "国常会",
    "发改委",
    "国家发展改革委",
    "央行",
    "人民银行",
    "证监会",
    "财政部",
    "商务部",
    "国家统计局",
    "金融监管总局",
    "多部门",
    "九部门",
)
MEDIA_MACRO_KEYWORDS = (
    "关税",
    "逆回购",
    "利率",
    "汇率",
    "国债",
    "债市",
    "资本市场",
    "房地产",
    "房贷",
    "CPI",
    "PPI",
    "PMI",
    "GDP",
    "社融",
    "公募基金",
    "基金行业",
    "基金销售",
    "基金费率",
    "外贸",
    "进出口",
    "消费政策",
    "经济数据",
)
# “A股”也可能只是在描述某家上市公司，所以必须再出现指数、成交或涨跌等市场行为词。
MEDIA_MARKET_SCOPE_KEYWORDS = ("A股", "沪深", "港股")
MEDIA_MARKET_ACTION_KEYWORDS = (
    "指数",
    "开盘",
    "收盘",
    "成交额",
    "放量",
    "缩量",
    "上涨",
    "下跌",
    "涨幅",
    "跌幅",
    "板块",
    "北向资金",
    "主力资金",
)


class MarketEventSyncError(RuntimeError):
    """所有外部来源都失败，或返回的数据无法转换成统一事件。"""


@dataclass(frozen=True)
class MarketEventBatch:
    """Provider 一次采集的结果，同时保留成功来源和局部错误。"""

    events: tuple[dict[str, Any], ...]
    successful_sources: tuple[str, ...]
    source_errors: tuple[str, ...]


class MarketEventProvider(Protocol):
    """真实 Provider 和测试 Fake Provider 共同遵守的最小接口。"""

    source: str

    def fetch_events(
        self,
        *,
        since: date,
        until: date,
        keywords: tuple[str, ...] = (),
    ) -> MarketEventBatch: ...


class AkshareMarketEventProvider:
    """通过 AKShare 抓取基金公告与央视官方媒体财经信息。"""

    source = AKSHARE_EVENT_SOURCE

    def __init__(
        self,
        fund_codes: tuple[str, ...] | None = None,
        fund_code_loader: Callable[[], list[str]] | None = None,
    ) -> None:
        # 测试可传固定代码；产品运行时由 loader 读取当前已导入基金。
        self.fund_codes = fund_codes
        self.fund_code_loader = fund_code_loader

    def _current_fund_codes(self) -> tuple[str, ...]:
        """每次同步前读取基金目录，让后来导入的基金也能采集公告。"""

        if self.fund_codes is not None:
            return tuple(dict.fromkeys(self.fund_codes))
        if self.fund_code_loader is not None:
            return tuple(dict.fromkeys(self.fund_code_loader()))
        return DEFAULT_FUND_CODES

    def fetch_events(
        self,
        *,
        since: date,
        until: date,
        keywords: tuple[str, ...] = (),
    ) -> MarketEventBatch:
        """采集公告和新闻；传入关键词时在入库前执行板块定向筛选。"""

        try:
            import akshare as ak
        except ModuleNotFoundError as exc:
            raise MarketEventSyncError("请先安装 requirements.txt 中的 akshare") from exc

        events: list[dict[str, Any]] = []
        successful_sources: set[str] = set()
        source_errors: list[str] = []

        events.extend(
            self._fetch_fund_announcements(
                ak=ak,
                since=since,
                until=until,
                successful_sources=successful_sources,
                source_errors=source_errors,
            )
        )
        events.extend(
            self._fetch_cctv_news(
                ak=ak,
                since=max(since, until - timedelta(days=2)),
                until=until,
                keywords=keywords,
                successful_sources=successful_sources,
                source_errors=source_errors,
            )
        )
        events.extend(
            self._fetch_eastmoney_news(
                ak=ak,
                since=since,
                until=until,
                keywords=keywords,
                successful_sources=successful_sources,
                source_errors=source_errors,
            )
        )
        events.extend(
            self._fetch_cls_news(
                ak=ak,
                since=since,
                until=until,
                keywords=keywords,
                successful_sources=successful_sources,
                source_errors=source_errors,
            )
        )

        if not successful_sources:
            raise MarketEventSyncError(
                "所有市场事件来源均获取失败：" + "；".join(source_errors[:8])
            )

        # Provider 内也按哈希去重一次，Repository 会再处理跨批次重复。
        unique_events: dict[str, dict[str, Any]] = {}
        for event in events:
            unique_events.setdefault(str(event["content_hash"]), event)
        return MarketEventBatch(
            events=tuple(unique_events.values()),
            successful_sources=tuple(sorted(successful_sources)),
            source_errors=tuple(source_errors),
        )

    def _fetch_fund_announcements(
        self,
        *,
        ak: Any,
        since: date,
        until: date,
        successful_sources: set[str],
        source_errors: list[str],
    ) -> list[dict[str, Any]]:
        """读取三类基金公告；单个接口失败不会丢掉其他已成功来源。"""

        category_fetchers = (
            ("定期报告", "medium_term", ak.fund_announcement_report_em),
            ("分红配送", "short_term", ak.fund_announcement_dividend_em),
            ("人事调整", "medium_term", ak.fund_announcement_personnel_em),
        )
        events: list[dict[str, Any]] = []
        for fund_code in self._current_fund_codes():
            for category, horizon, fetcher in category_fetchers:
                try:
                    frame = fetcher(symbol=fund_code)
                    successful_sources.add("Eastmoney Fund Announcements via AKShare")
                except Exception as exc:
                    source_errors.append(f"基金 {fund_code} {category}：{exc}")
                    continue

                required = {"基金代码", "公告标题", "基金名称", "公告日期", "报告ID"}
                if not required.issubset(frame.columns):
                    source_errors.append(f"基金 {fund_code} {category}：字段不完整")
                    continue

                for _, row in frame.iterrows():
                    published_date = self._as_date(row["公告日期"])
                    if not since <= published_date <= until:
                        continue
                    events.append(
                        self._fund_announcement_event(
                            row=row,
                            category=category,
                            horizon=horizon,
                            published_date=published_date,
                        )
                    )
        return events

    def _fund_announcement_event(
        self,
        *,
        row: Any,
        category: str,
        horizon: str,
        published_date: date,
    ) -> dict[str, Any]:
        """把天天基金公告的一行转换成统一事件。"""

        fund_code = str(row["基金代码"]).strip()
        fund_name = self._clean_text(row["基金名称"])
        title = self._clean_text(row["公告标题"])
        report_id = str(row["报告ID"]).strip()
        source_text = f"{report_id} {fund_code} {fund_name} {published_date} {title}"
        return {
            "event_id": f"evt-fund-{report_id.lower()}",
            "event_date": published_date.isoformat(),
            "published_date": published_date.isoformat(),
            "event_type": "fund_announcement",
            "title": title,
            "summary": f"{fund_name}发布{category}公告，具体内容和数据应以公告原文为准。",
            "impact_direction": "neutral",
            "impact_horizon": horizon,
            "impact_reason": "当前阶段只完成公告采集和归档，尚未调用模型判断影响方向。",
            "affected_fund_codes": [fund_code],
            "tags": ["基金公告", category, fund_name],
            "source_name": "天天基金公开披露",
            "source_level": "B",
            "source_grade_reason": "公开基金公告聚合页，原始发布主体为基金管理人",
            "source_url": (
                f"https://fund.eastmoney.com/gonggao/{fund_code},{report_id}.html"
            ),
            "content_hash": self._content_hash(source_text),
        }

    def _fetch_cctv_news(
        self,
        *,
        ak: Any,
        since: date,
        until: date,
        keywords: tuple[str, ...],
        successful_sources: set[str],
        source_errors: list[str],
    ) -> list[dict[str, Any]]:
        """读取最近三天新闻联播文字稿，并用公开关键词筛出财经信息。"""

        events: list[dict[str, Any]] = []
        current = since
        while current <= until:
            compact_date = current.strftime("%Y%m%d")
            try:
                frame = ak.news_cctv(date=compact_date)
                successful_sources.add("CCTV News via AKShare")
            except Exception as exc:
                source_errors.append(f"央视网 {current.isoformat()}：{exc}")
                current += timedelta(days=1)
                continue

            required = {"date", "title", "content"}
            if not required.issubset(frame.columns):
                source_errors.append(f"央视网 {current.isoformat()}：字段不完整")
                current += timedelta(days=1)
                continue

            for _, row in frame.iterrows():
                title = self._clean_text(row["title"])
                content = self._clean_text(row["content"])
                # 全市场模式沿用严格标题词。板块模式则允许标题或正文命中扩展词，
                # 因为“芯片”等具体术语比“经济”更不容易造成泛财经误收。
                topic_tags = self._topic_tags(f"{title} {content}", keywords)
                financial_tags = self._financial_tags(title)
                tags = (
                    self._merge_tags(topic_tags, financial_tags)
                    if keywords
                    else financial_tags
                )
                if keywords and not topic_tags:
                    continue
                if not tags:
                    continue
                events.append(
                    self._cctv_event(
                        event_date=current,
                        title=title,
                        content=content,
                        tags=tags[:6],
                    )
                )
            current += timedelta(days=1)
        return events

    def _cctv_event(
        self,
        *,
        event_date: date,
        title: str,
        content: str,
        tags: list[str],
    ) -> dict[str, Any]:
        """将央视文字稿转换为中性、可追溯的统一事件。"""

        source_text = f"{event_date.isoformat()} {title} {content}"
        digest = hashlib.sha256(source_text.encode("utf-8")).hexdigest()
        event_type = self._classify_event_type(title=title, content=content)
        return {
            "event_id": f"evt-cctv-{event_date.strftime('%Y%m%d')}-{digest[:16]}",
            "event_date": event_date.isoformat(),
            "published_date": event_date.isoformat(),
            "event_type": event_type,
            "title": title,
            "summary": self._shorten(content, 220),
            "impact_direction": "neutral",
            "impact_horizon": "short_term",
            "impact_reason": "关键词仅用于事件归类，尚未调用模型研判利好、利空或影响强度。",
            # Provider 只保留原始事实，基金关联统一交给 MarketEventFundLinkService。
            "affected_fund_codes": [],
            "tags": tags,
            "source_name": "央视网新闻联播",
            "source_level": "B",
            "source_grade_reason": "中央官方媒体公开文字稿",
            "source_url": (
                "https://tv.cctv.com/lm/xwlb/day/"
                f"{event_date.strftime('%Y%m%d')}.shtml"
            ),
            "content_hash": f"sha256:{digest}",
        }

    def _fetch_eastmoney_news(
        self,
        *,
        ak: Any,
        since: date,
        until: date,
        keywords: tuple[str, ...],
        successful_sources: set[str],
        source_errors: list[str],
    ) -> list[dict[str, Any]]:
        """读取东方财富财经快讯，并只保留标题命中透明财经关键词的记录。"""

        try:
            frame = ak.stock_info_global_em()
            successful_sources.add("Eastmoney Finance via AKShare")
        except Exception as exc:
            source_errors.append(f"东方财富财经快讯：{exc}")
            return []

        required = {"标题", "摘要", "发布时间", "链接"}
        if not required.issubset(frame.columns):
            source_errors.append("东方财富财经快讯：字段不完整")
            return []

        events: list[dict[str, Any]] = []
        for _, row in frame.iterrows():
            published_date = self._as_date(row["发布时间"])
            title = self._clean_text(row["标题"])
            raw_summary = self._clean_text(row["摘要"])
            topic_tags = self._topic_tags(f"{title} {raw_summary}", keywords)
            tags = (
                self._merge_tags(topic_tags, self._media_tags(title))
                if keywords
                else self._media_tags(title)
            )
            if (
                not since <= published_date <= until
                or not tags
                or (keywords and not topic_tags)
            ):
                continue
            summary = self._clean_financial_summary(
                title=title,
                content=raw_summary,
            )
            events.append(
                self._financial_news_event(
                    source_key="eastmoney",
                    source_name="东方财富财经",
                    source_url=str(row["链接"]).strip(),
                    source_grade_reason="公开财经媒体快讯，经 AKShare 统一读取",
                    published_date=published_date,
                    title=title,
                    summary=summary,
                    tags=tags,
                )
            )
            if len(events) >= settings.market_event_max_items_per_source:
                break
        return events

    def _fetch_cls_news(
        self,
        *,
        ak: Any,
        since: date,
        until: date,
        keywords: tuple[str, ...],
        successful_sources: set[str],
        source_errors: list[str],
    ) -> list[dict[str, Any]]:
        """读取财联社电报；它和东方财富可能报道同一事件，交给 Embedding 聚类。"""

        try:
            frame = ak.stock_info_global_cls()
            successful_sources.add("CLS Telegraph via AKShare")
        except Exception as exc:
            source_errors.append(f"财联社电报：{exc}")
            return []

        required = {"标题", "内容", "发布日期", "发布时间"}
        if not required.issubset(frame.columns):
            source_errors.append("财联社电报：字段不完整")
            return []

        events: list[dict[str, Any]] = []
        for _, row in frame.iterrows():
            published_date = self._as_date(row["发布日期"])
            title = self._clean_text(row["标题"])
            raw_content = self._clean_text(row["内容"])
            topic_tags = self._topic_tags(f"{title} {raw_content}", keywords)
            tags = (
                self._merge_tags(topic_tags, self._media_tags(title))
                if keywords
                else self._media_tags(title)
            )
            if (
                not since <= published_date <= until
                or not tags
                or (keywords and not topic_tags)
            ):
                continue
            summary = self._clean_financial_summary(
                title=title,
                content=raw_content,
            )
            events.append(
                self._financial_news_event(
                    source_key="cls",
                    source_name="财联社电报",
                    # AKShare 这一接口不返回单条链接，因此保留公开电报入口并明确来源等级 C。
                    source_url="https://www.cls.cn/telegraph",
                    source_grade_reason="公开财经媒体电报入口，经 AKShare 统一读取",
                    published_date=published_date,
                    title=title,
                    summary=summary,
                    tags=tags,
                )
            )
            if len(events) >= settings.market_event_max_items_per_source:
                break
        return events

    def _financial_news_event(
        self,
        *,
        source_key: str,
        source_name: str,
        source_url: str,
        source_grade_reason: str,
        published_date: date,
        title: str,
        summary: str,
        tags: list[str],
    ) -> dict[str, Any]:
        """把不同财经媒体字段收敛成同一份中性 MarketEvent。"""

        source_text = f"{source_key} {source_url} {published_date} {title} {summary}"
        digest = hashlib.sha256(source_text.encode("utf-8")).hexdigest()
        event_type = self._classify_event_type(title=title, content=summary)
        return {
            "event_id": f"evt-{source_key}-{digest[:16]}",
            "event_date": published_date.isoformat(),
            "published_date": published_date.isoformat(),
            "event_type": event_type,
            "title": title,
            "summary": self._shorten(summary, 280),
            "impact_direction": "neutral",
            "impact_horizon": "short_term",
            "impact_reason": "媒体快讯只作为原始证据，影响方向等待独立语义分析。",
            "affected_fund_codes": [],
            "tags": tags[:8],
            "source_name": source_name,
            "source_level": "C",
            "source_grade_reason": source_grade_reason,
            "source_url": source_url,
            "content_hash": f"sha256:{digest}",
        }

    @staticmethod
    def _financial_tags(title: str) -> list[str]:
        """只根据标题筛选，避免正文顺带出现“经济”造成大量无关快讯。"""

        return [keyword for keyword in FINANCIAL_KEYWORDS if keyword in title][:8]

    @staticmethod
    def _media_tags(title: str) -> list[str]:
        """为财经媒体使用更严格的标题门槛，排除普通公司经营快讯。"""

        authority_tags = [
            keyword for keyword in MEDIA_AUTHORITY_KEYWORDS if keyword in title
        ]
        macro_tags = [keyword for keyword in MEDIA_MACRO_KEYWORDS if keyword in title]
        market_scope_tags = [
            keyword for keyword in MEDIA_MARKET_SCOPE_KEYWORDS if keyword in title
        ]
        has_market_action = any(
            keyword in title for keyword in MEDIA_MARKET_ACTION_KEYWORDS
        )
        market_tags = market_scope_tags if has_market_action else []
        if not authority_tags and not macro_tags and not market_tags:
            return []
        return list(
            dict.fromkeys([*authority_tags, *macro_tags, *market_tags])
        )[:8]

    @classmethod
    def _topic_tags(cls, text: str, keywords: tuple[str, ...]) -> list[str]:
        """返回真正命中的扩展词；英文缩写使用词边界，避免 `AI` 命中普通单词。"""

        return [
            keyword
            for keyword in keywords
            if cls._contains_keyword(text=text, keyword=keyword)
        ][:8]

    @staticmethod
    def _contains_keyword(*, text: str, keyword: str) -> bool:
        normalized_text = text.casefold()
        normalized_keyword = keyword.strip().casefold()
        if not normalized_keyword:
            return False
        if re.fullmatch(r"[a-z0-9+./-]+", normalized_keyword):
            pattern = rf"(?<![a-z0-9]){re.escape(normalized_keyword)}(?![a-z0-9])"
            return re.search(pattern, normalized_text) is not None
        return normalized_keyword in normalized_text

    @staticmethod
    def _merge_tags(*groups: list[str]) -> list[str]:
        return list(dict.fromkeys(tag for group in groups for tag in group))[:8]

    @classmethod
    def _clean_financial_summary(cls, *, title: str, content: str) -> str:
        """去掉媒体常见的重复标题前缀，让 Embedding 输入更干净。"""

        repeated_prefix = f"【{title}】"
        cleaned = (
            content[len(repeated_prefix) :]
            if content.startswith(repeated_prefix)
            else content
        )
        return cls._clean_text(cleaned)

    @staticmethod
    def _classify_event_type(*, title: str, content: str) -> str:
        """标题优先分类，只有标题不明确时才参考正文。"""

        if any(
            keyword in title
            for keyword in ("监管", "证监会", "证券法", "金融监管总局")
        ):
            return "regulation"
        if any(
            keyword in title
            for keyword in ("同比", "增长", "数据", "物价", "就业", "零售", "收购")
        ):
            return "economic_data"
        if any(
            keyword in title
            for keyword in (
                "政策",
                "国务院",
                "财政",
                "货币",
                "央行",
                "人民银行",
                "发改委",
                "商务部",
            )
        ):
            return "policy"
        if any(keyword in content for keyword in ("监管", "证监会", "证券法")):
            return "regulation"
        if any(keyword in content for keyword in ("同比", "增长", "物价", "就业", "零售")):
            return "economic_data"
        if any(keyword in content for keyword in ("政策", "国务院", "财政", "货币")):
            return "policy"
        return "market_notice"

    @staticmethod
    def _content_hash(value: str) -> str:
        normalized = re.sub(r"\s+", " ", value).strip().casefold()
        return f"sha256:{hashlib.sha256(normalized.encode('utf-8')).hexdigest()}"

    @staticmethod
    def _clean_text(value: Any) -> str:
        return re.sub(r"\s+", " ", str(value)).strip()

    @staticmethod
    def _shorten(value: str, limit: int) -> str:
        return value if len(value) <= limit else f"{value[: limit - 1]}…"

    @staticmethod
    def _as_date(value: Any) -> date:
        if isinstance(value, datetime):
            return value.date()
        if isinstance(value, date):
            return value
        return date.fromisoformat(str(value).strip()[:10])


class OfficialWebsiteMarketEventProvider:
    """从国家发改委和中国证监会官方网站读取公开事件。

    官方来源直接标记为 A 级。这里只做日期、文本和类型清洗，不猜测利好利空；
    方向仍由后续独立 DeepSeek 语义层判断。
    """

    source = OFFICIAL_EVENT_SOURCE
    ndrc_list_url = "https://www.ndrc.gov.cn/xwdt/xwfb/"
    csrc_list_url = "https://www.csrc.gov.cn/csrc/xwfb/index.shtml"

    def __init__(
        self,
        *,
        client_factory: Callable[[], Any] | None = None,
    ) -> None:
        # 测试传 Fake Client；正式运行时才延迟创建 httpx.Client。
        self.client_factory = client_factory or self._default_client

    def fetch_events(
        self,
        *,
        since: date,
        until: date,
        keywords: tuple[str, ...] = (),
    ) -> MarketEventBatch:
        events: list[dict[str, Any]] = []
        successful_sources: set[str] = set()
        source_errors: list[str] = []

        with self.client_factory() as client:
            source_fetchers = (
                ("国家发展改革委", self._fetch_ndrc),
                ("中国证监会", self._fetch_csrc),
            )
            for source_name, fetcher in source_fetchers:
                try:
                    source_events = fetcher(
                        client=client,
                        since=since,
                        until=until,
                        keywords=keywords,
                        source_errors=source_errors,
                    )
                    events.extend(source_events)
                    # 页面能成功读取就算来源健康，即使窗口内刚好没有新文章。
                    successful_sources.add(source_name)
                except Exception as exc:
                    source_errors.append(f"{source_name}：{exc}")

        if not successful_sources:
            raise MarketEventSyncError(
                "官方网站来源均获取失败：" + "；".join(source_errors[:8])
            )

        unique_events = {
            str(event["content_hash"]): event
            for event in events
        }
        return MarketEventBatch(
            events=tuple(unique_events.values()),
            successful_sources=tuple(sorted(successful_sources)),
            source_errors=tuple(source_errors),
        )

    def _fetch_ndrc(
        self,
        *,
        client: Any,
        since: date,
        until: date,
        keywords: tuple[str, ...],
        source_errors: list[str],
    ) -> list[dict[str, Any]]:
        """解析发改委新闻发布列表，再读取命中窗口的文章正文。"""

        from bs4 import BeautifulSoup

        response = client.get(self.ndrc_list_url)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")
        candidates: list[tuple[date, str, str, list[str]]] = []
        for row in soup.select("ul.u-list li"):
            link = row.find("a", href=True)
            date_node = row.find("span")
            if link is None or date_node is None:
                continue
            try:
                published_date = datetime.strptime(
                    date_node.get_text(strip=True),
                    "%Y/%m/%d",
                ).date()
            except ValueError:
                continue
            title = AkshareMarketEventProvider._clean_text(
                link.get("title") or link.get_text(" ", strip=True)
            )
            topic_tags = AkshareMarketEventProvider._topic_tags(title, keywords)
            financial_tags = AkshareMarketEventProvider._financial_tags(title)
            tags = (
                AkshareMarketEventProvider._merge_tags(
                    topic_tags,
                    financial_tags,
                )
                if keywords
                else financial_tags
            )
            if keywords and not topic_tags:
                continue
            if since <= published_date <= until and tags:
                candidates.append(
                    (
                        published_date,
                        title,
                        urljoin(self.ndrc_list_url, str(link["href"])),
                        tags,
                    )
                )

        events: list[dict[str, Any]] = []
        for published_date, title, source_url, tags in candidates[
            : settings.market_event_max_items_per_source
        ]:
            try:
                article = client.get(source_url)
                article.raise_for_status()
                article_soup = BeautifulSoup(article.text, "html.parser")
                body = article_soup.select_one(".TRS_Editor")
                summary = AkshareMarketEventProvider._shorten(
                    AkshareMarketEventProvider._clean_text(
                        body.get_text(" ", strip=True) if body else title
                    ),
                    280,
                )
                events.append(
                    self._official_event(
                        source_key="ndrc",
                        source_name="国家发展改革委",
                        source_grade_reason="国家发展改革委官方网站发布",
                        published_date=published_date,
                        title=title,
                        summary=summary,
                        source_url=source_url,
                        tags=tags,
                    )
                )
            except Exception as exc:
                source_errors.append(f"国家发展改革委文章 {source_url}：{exc}")
        return events

    def _fetch_csrc(
        self,
        *,
        client: Any,
        since: date,
        until: date,
        keywords: tuple[str, ...],
        source_errors: list[str],
    ) -> list[dict[str, Any]]:
        """从证监会首页发现要闻链接，并以文章 meta 里的精确日期为准。"""

        from bs4 import BeautifulSoup

        response = client.get(self.csrc_list_url)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")
        links: list[str] = []
        for node in soup.select(".szyw-lists a[href*='/csrc/c100028/']"):
            href = str(node.get("href") or "")
            if href.endswith("content.shtml"):
                url = urljoin(self.csrc_list_url, href)
                if url not in links:
                    links.append(url)

        events: list[dict[str, Any]] = []
        for source_url in links[: settings.market_event_max_items_per_source]:
            try:
                article = client.get(source_url)
                article.raise_for_status()
                article_soup = BeautifulSoup(article.text, "html.parser")
                title = self._meta(article_soup, "ArticleTitle")
                published_text = self._meta(article_soup, "PubDate")
                if not title or not published_text:
                    raise ValueError("缺少 ArticleTitle 或 PubDate")
                published_date = date.fromisoformat(published_text[:10])
                if not since <= published_date <= until:
                    continue
                description = (
                    self._meta(article_soup, "Description")
                    or self._meta(article_soup, "description")
                    or title
                )
                topic_tags = AkshareMarketEventProvider._topic_tags(
                    f"{title} {description}",
                    keywords,
                )
                if keywords and not topic_tags:
                    continue
                tags = AkshareMarketEventProvider._merge_tags(
                    topic_tags,
                    ["证监会", "资本市场"],
                    AkshareMarketEventProvider._financial_tags(title),
                )
                events.append(
                    self._official_event(
                        source_key="csrc",
                        source_name="中国证监会",
                        source_grade_reason="中国证券监督管理委员会官方网站发布",
                        published_date=published_date,
                        title=title,
                        summary=AkshareMarketEventProvider._shorten(description, 280),
                        source_url=source_url,
                        tags=list(dict.fromkeys(tags)),
                        event_type="regulation",
                    )
                )
            except Exception as exc:
                source_errors.append(f"中国证监会文章 {source_url}：{exc}")
        return events

    @staticmethod
    def _meta(soup: Any, name: str) -> str:
        node = soup.find("meta", attrs={"name": name})
        return str(node.get("content") or "").strip() if node else ""

    @staticmethod
    def _official_event(
        *,
        source_key: str,
        source_name: str,
        source_grade_reason: str,
        published_date: date,
        title: str,
        summary: str,
        source_url: str,
        tags: list[str],
        event_type: str | None = None,
    ) -> dict[str, Any]:
        """把两个官方网站的文章转换成统一 MarketEvent。"""

        source_text = f"{source_key} {source_url} {published_date} {title} {summary}"
        digest = hashlib.sha256(source_text.encode("utf-8")).hexdigest()
        normalized_type = event_type or AkshareMarketEventProvider._classify_event_type(
            title=title,
            content=summary,
        )
        return {
            "event_id": f"evt-{source_key}-{digest[:16]}",
            "event_date": published_date.isoformat(),
            "published_date": published_date.isoformat(),
            "event_type": normalized_type,
            "title": title,
            "summary": summary,
            "impact_direction": "neutral",
            "impact_horizon": "short_term",
            "impact_reason": "官方原文只作为客观证据，影响方向等待独立语义分析。",
            "affected_fund_codes": [],
            "tags": list(dict.fromkeys(tags))[:8],
            "source_name": source_name,
            "source_level": "A",
            "source_grade_reason": source_grade_reason,
            "source_url": source_url,
            "content_hash": f"sha256:{digest}",
        }

    @staticmethod
    def _default_client() -> Any:
        import httpx

        return httpx.Client(
            timeout=settings.market_event_http_timeout_seconds,
            follow_redirects=True,
            headers={
                "User-Agent": "FundScope/1.0 (+local educational research project)",
            },
        )


class CompositeMarketEventProvider:
    """组合多个 Provider，并允许其中一个来源暂时失败。"""

    def __init__(self, providers: tuple[MarketEventProvider, ...]) -> None:
        if not providers:
            raise ValueError("CompositeMarketEventProvider 至少需要一个 Provider")
        self.providers = providers
        self.source = " + ".join(provider.source for provider in providers)

    def fetch_events(
        self,
        *,
        since: date,
        until: date,
        keywords: tuple[str, ...] = (),
    ) -> MarketEventBatch:
        events: list[dict[str, Any]] = []
        successful_sources: set[str] = set()
        source_errors: list[str] = []

        for provider in self.providers:
            try:
                batch = (
                    provider.fetch_events(
                        since=since,
                        until=until,
                        keywords=keywords,
                    )
                    if keywords
                    else provider.fetch_events(since=since, until=until)
                )
            except Exception as exc:
                source_errors.append(f"{provider.source}：{exc}")
                continue
            events.extend(batch.events)
            successful_sources.update(batch.successful_sources)
            source_errors.extend(batch.source_errors)

        if not successful_sources:
            raise MarketEventSyncError(
                "所有市场事件 Provider 均获取失败：" + "；".join(source_errors[:8])
            )

        # 这里只去掉完全相同的来源文本；不同媒体对同一事件的报道必须保留给聚类层。
        unique_events: dict[str, dict[str, Any]] = {}
        for event in events:
            unique_events.setdefault(str(event["content_hash"]), event)
        return MarketEventBatch(
            events=tuple(unique_events.values()),
            successful_sources=tuple(sorted(successful_sources)),
            source_errors=tuple(source_errors),
        )


class MarketEventSyncService:
    """编排 Provider 抓取、Repository 入库和每日 Cron 注册。"""

    def __init__(
        self,
        *,
        repository: MarketEventRepository,
        provider: MarketEventProvider | None = None,
        fund_link_service: MarketEventFundLinkService | None = None,
    ) -> None:
        self.repository = repository
        self.fund_link_service = fund_link_service
        self.provider = provider or CompositeMarketEventProvider(
            (
                AkshareMarketEventProvider(
                    fund_code_loader=(
                        fund_link_service.available_fund_codes
                        if fund_link_service is not None
                        else None
                    )
                ),
                OfficialWebsiteMarketEventProvider(),
            )
        )

    def sync(
        self,
        *,
        keyword_expansion: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """回看固定窗口并幂等写入；可在采集阶段按板块扩展词过滤。"""

        until = datetime.now().astimezone().date()
        since = until - timedelta(days=settings.market_event_sync_lookback_days)
        keywords = tuple(
            str(keyword)
            for keyword in (keyword_expansion or {}).get("keywords", [])
            if str(keyword).strip()
        )
        batch = (
            self.provider.fetch_events(
                since=since,
                until=until,
                keywords=keywords,
            )
            if keywords
            else self.provider.fetch_events(since=since, until=until)
        )
        events = list(batch.events)
        if self.fund_link_service is not None:
            events = self.fund_link_service.link_events(events)
        fetched_at = datetime.now(timezone.utc)
        write_stats = self.repository.upsert(
            events=events,
            fetched_at=fetched_at,
        )
        linked_fund_codes = {
            code
            for event in events
            for code in event.get("affected_fund_codes", [])
        }
        return {
            "summary": (
                f"市场事件同步完成：采集 {len(events)} 条，"
                f"新增 {write_stats['inserted_count']} 条。"
            ),
            "workflow": "market_event_sync",
            "source": self.provider.source,
            "collection_mode": (
                "topic_keyword_filter" if keywords else "broad_market_filter"
            ),
            "collection_topics": list(
                (keyword_expansion or {}).get("topics", [])
            ),
            "collection_keywords": list(keywords),
            "keyword_expansion": keyword_expansion,
            "window_start": since.isoformat(),
            "window_end": until.isoformat(),
            "fetched_count": len(events),
            "linked_event_count": sum(
                bool(event.get("affected_fund_codes")) for event in events
            ),
            "linked_fund_count": len(linked_fund_codes),
            **write_stats,
            "successful_sources": list(batch.successful_sources),
            "source_errors": list(batch.source_errors[:8]),
            "fetched_at": fetched_at.isoformat(),
        }

    def status(self) -> dict[str, Any]:
        database_status = self.repository.status()
        analysis_status = self.repository.analysis_status()
        # 延迟导入避免 cluster service -> market events -> sync service 的初始化环。
        from app.services.market_event_clusters import market_event_cluster_repository

        cluster_status = market_event_cluster_repository.status()
        return {
            "ok": True,
            "data_mode": "database" if database_status["record_count"] else "fixture",
            "source": self.provider.source,
            "sync_enabled": settings.market_event_sync_enabled,
            "sync_cron": settings.market_event_sync_cron,
            "schedule_id": MARKET_EVENT_SYNC_SCHEDULE_ID,
            "lookback_days": settings.market_event_sync_lookback_days,
            **database_status,
            **analysis_status,
            **cluster_status,
        }

    def ensure_daily_schedule(self, scheduler: Any) -> dict[str, Any]:
        """启动时确保系统用户只有一条市场事件同步 Cron。"""

        current = scheduler.list_schedules(user_id=MARKET_EVENT_SYNC_SYSTEM_USER_ID)
        existing = next(
            (
                item
                for item in current["schedules"]
                if item["id"] == MARKET_EVENT_SYNC_SCHEDULE_ID
            ),
            None,
        )
        if existing is not None:
            return {"ok": True, "created": False, "schedule": existing}

        result = scheduler.create_schedule(
            user_id=MARKET_EVENT_SYNC_SYSTEM_USER_ID,
            schedule_id=MARKET_EVENT_SYNC_SCHEDULE_ID,
            cron=settings.market_event_sync_cron,
            prompt="同步 FundScope 每日基金公告与市场事件",
            job_type="market_event_sync",
            recurring=True,
            durable=True,
        )
        return {"ok": True, "created": True, "schedule": result["schedule"]}


market_event_sync_service = MarketEventSyncService(
    repository=market_event_repository,
    fund_link_service=market_event_fund_link_service,
)
