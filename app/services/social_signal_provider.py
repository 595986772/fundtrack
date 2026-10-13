"""小红书公开投资观点的数据 Provider。

本文件只负责“从哪里取得公开帖子并规范化字段”，不调用大模型、不计算情绪，也不
写业务数据库。当前实现使用本机 Playwright + 独立 Chrome Profile 读取用户在浏览器
中本来就能看到的公开页面，不调用受保护的内部接口，也不处理验证码。

第一次使用时，用户需要在项目打开的独立 Chrome 窗口中手动登录。Cookie 只保存在
``data/xhs_browser_profile``，不会进入 SQL、日志或 Git。
"""

from __future__ import annotations

from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
from importlib.util import find_spec
import json
from pathlib import Path
import re
from threading import Lock
import time
from typing import Any, Callable, Iterator, Protocol
from urllib.parse import parse_qs, quote, urljoin, urlparse
from zoneinfo import ZoneInfo

from app.config import settings


PROJECT_ROOT = Path(__file__).resolve().parents[2]
XHS_HOME_URL = "https://www.xiaohongshu.com/explore"
XHS_SEARCH_URL = "https://www.xiaohongshu.com/search_result?keyword={}"
MAX_EXCERPT_CHARS = 3000
SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")

# Playwright 的持久化 Profile 不能被两个 Chrome 进程同时打开。线程锁先保护本地默认
# 执行器；文件锁再保护 API 与 Celery Worker恰好同时收到刷新任务的情况。
_BROWSER_THREAD_LOCK = Lock()


class SocialSignalProviderError(RuntimeError):
    """浏览器、登录会话或公开页面暂时无法提供可用帖子。"""


class SocialSignalAccessPausedError(SocialSignalProviderError):
    """访问已暂停，不属于可通过重试自动恢复的普通网络错误。"""

    status = "verification_required"


class SocialSignalLoginRequiredError(SocialSignalAccessPausedError):
    """平台明确要求重新登录或扫码时使用，调用方必须立即停止继续访问。"""

    status = "login_required"


class SocialSignalCooldownError(SocialSignalAccessPausedError):
    """本地采集间隔尚未到期，不能再打开浏览器。"""

    status = "cooldown"


@dataclass(frozen=True)
class SocialPostBatch:
    """一次采集的规范化帖子，以及各关键词的局部成功/失败信息。"""

    posts: tuple[dict[str, Any], ...]
    successful_keywords: tuple[str, ...]
    source_errors: tuple[str, ...]
    status: str = "completed"


class SocialInvestmentPostProvider(Protocol):
    """真实浏览器 Provider 与测试 Fake Provider 共同遵守的最小接口。"""

    source: str

    @property
    def configured(self) -> bool: ...

    @property
    def session_ready(self) -> bool: ...

    def fetch_posts(
        self,
        *,
        since: datetime,
        until: datetime,
        known_post_ids: set[str] | None = None,
    ) -> SocialPostBatch: ...

    def prepare_session(self) -> dict[str, Any]: ...

    def status_details(self) -> dict[str, Any]: ...


class XiaohongshuBrowserProvider:
    """通过本机 Chrome 读取小红书公开搜索页和作者主页。

    这个 Provider 刻意保持低频：每次只读取有限关键词、有限卡片和有限详情页。页面
    要求登录或出现验证码时会停止并提示用户处理，不尝试绕过平台的访问控制。
    """

    source = "小红书公开网页（本机 Chrome）"

    def __init__(
        self,
        *,
        keyword_loader: Callable[[], list[str]] | None = None,
        context_factory: Callable[[bool], Any] | None = None,
        profile_dir: str | Path | None = None,
        session_ready: bool | None = None,
    ) -> None:
        self.keyword_loader = keyword_loader
        # context_factory 只用于单元测试注入假浏览器，测试不会访问真实小红书。
        self.context_factory = context_factory
        self.profile_dir = self._resolve_profile_dir(
            profile_dir or settings.xhs_browser_profile_dir
        )
        self.session_marker = self.profile_dir / "fundscope-session.json"
        # 只记录暂停原因和时间，不保存页面内容、Cookie 或请求令牌。
        self.access_marker = self.profile_dir / "fundscope-access.json"
        self._session_ready_override = session_ready
        self._images_remaining = 0

    @property
    def browser_available(self) -> bool:
        """只检查 Python 驱动是否安装；Chrome 是否可启动会在 Job 中给出明确错误。"""

        return self.context_factory is not None or find_spec("playwright") is not None

    @property
    def configured(self) -> bool:
        """自采集不需要 API Key，只要求功能开启且 Playwright 已安装。"""

        return bool(settings.xhs_social_signal_enabled and self.browser_available)

    @property
    def session_ready(self) -> bool:
        """Marker 表示曾成功检测到登录 Cookie；真正刷新时还会再次验证 Cookie。"""

        if self._session_ready_override is not None:
            return self._session_ready_override
        return self.session_marker.is_file()

    def status_details(self) -> dict[str, Any]:
        """给前端返回非敏感状态，绝不返回 Cookie、浏览器数据或绝对路径。"""

        access = self._read_access_state()
        next_at = access.get("next_collection_at")
        cooling = bool(next_at and datetime.fromisoformat(next_at) > datetime.now(timezone.utc))
        verification = bool(access.get("verification_required"))
        message = access.get("access_message", "") if verification else ""
        if not verification and not self.session_ready:
            message = "请连接小红书并完成登录；已保存样本仍可查看。"
        elif not verification and cooling:
            message = "处于本地采集间隔内，暂不再访问小红书；已保存样本仍可查看。"
        return {
            "browser_available": self.browser_available,
            "session_ready": self.session_ready,
            "login_required": self.configured and not self.session_ready,
            "verification_required": verification,
            "collection_allowed": self.configured and self.session_ready and not verification and not cooling,
            "next_collection_at": next_at,
            "access_message": message,
            "tracked_author_count": len(self._author_urls()),
            "keyword_count": len(self._current_keywords()),
        }

    def prepare_session(self) -> dict[str, Any]:
        """打开独立 Chrome，等待用户手动完成登录并检测登录 Cookie。

        登录成功后窗口会自动关闭。整个过程不接触用户名、密码或验证码文本，只等待
        小红书自己写入登录 Cookie。
        """

        if not settings.xhs_social_signal_enabled:
            raise SocialSignalProviderError("小红书公开观点采集已关闭")
        if not self.browser_available:
            raise SocialSignalProviderError(
                "缺少 Playwright，请安装 requirements.txt 后重启服务"
            )

        self.profile_dir.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + max(
            30,
            settings.xhs_browser_login_timeout_seconds,
        )

        with self._browser_lock():
            try:
                with self._browser_context(headless=False) as context:
                    page = self._primary_page(context)
                    page.goto(
                        self._read_access_state().get("recovery_url") or XHS_HOME_URL,
                        wait_until="domcontentloaded",
                        timeout=self._navigation_timeout_ms,
                    )
                    # DOM 刚完成时登录弹层可能还没渲染。先等待页面稳定，再开始判断，
                    # 避免只看到匿名 Cookie 就把窗口立即关掉。
                    self._settle_page(page)
                    while time.monotonic() < deadline:
                        if self._has_authenticated_session(context, page):
                            self._write_session_marker()
                            # 人工验证成功只清除验证暂停；仍保留最短采集间隔。
                            access = self._read_access_state()
                            access.update(verification_required=False, access_message="", recovery_url="")
                            self._write_access_state(access)
                            return {
                                "workflow": "xiaohongshu_browser_login",
                                "status": "connected",
                                "provider": self.source,
                                "summary": "已连接小红书，本机登录会话已保存。",
                            }
                        # 页面被用户提前关闭时，Playwright 访问 pages 可能直接抛错。
                        if not getattr(context, "pages", []):
                            break
                        time.sleep(1)
            except Exception as exc:
                if self._is_browser_closed_error(exc):
                    raise SocialSignalProviderError(
                        "登录窗口已关闭，但尚未检测到登录成功"
                    ) from exc
                raise self._friendly_browser_error(exc) from exc

        raise SocialSignalProviderError(
            "等待小红书登录超时，请重新连接并在弹出的 Chrome 中完成登录"
        )

    def fetch_posts(
        self,
        *,
        since: datetime,
        until: datetime,
        known_post_ids: set[str] | None = None,
    ) -> SocialPostBatch:
        """读取搜索页/作者主页卡片，再打开少量新帖详情补齐正文。"""

        if not self.configured:
            raise SocialSignalProviderError(
                "小红书浏览器采集器未就绪，请先安装 Playwright"
            )
        if not self.session_ready:
            raise SocialSignalProviderError(
                "请先在市场页点击“连接小红书”，完成一次浏览器登录"
            )

        keywords = self._current_keywords()
        author_urls = self._author_urls()
        if not keywords and not author_urls:
            raise SocialSignalProviderError("没有可用于采集的关键词或作者主页")

        candidates: dict[str, dict[str, Any]] = {}
        successful_sources: set[str] = set()
        source_errors: list[str] = []
        posts: list[dict[str, Any]] = []
        batch_status = "completed"

        with self._browser_lock():
            # 文件锁内检查并占用时间窗口，防止不同标签页或 Worker 排队后连续采集。
            self._claim_collection_slot()
            self._images_remaining = max(0, min(settings.xhs_social_image_budget, 20))
            try:
                with self._browser_context(
                    headless=settings.xhs_browser_headless
                ) as context:
                    if not self._has_login_cookie(context):
                        self._clear_session_marker()
                        raise SocialSignalLoginRequiredError(
                            "小红书登录已失效，请点击“连接小红书”重新登录"
                        )

                    page = self._primary_page(context)
                    for keyword in keywords:
                        if self._candidate_limit_reached(candidates):
                            break
                        try:
                            cards = self._search_keyword(page, keyword)
                            successful_sources.add(keyword)
                            self._merge_candidates(candidates, cards)
                        except SocialSignalAccessPausedError:
                            # 登录弹层出现后继续搜索只会产生更多无效请求，并可能让平台
                            # 提高验证强度，因此不能把它当成普通关键词失败后继续。
                            raise
                        except Exception as exc:
                            source_errors.append(f"关键词“{keyword}”：{exc}")

                    # 作者主页是可选补充来源。配置后只读取公开主页中的最新卡片，不访问
                    # 关注列表、私信、个人资产等非公开区域。
                    for author_url in author_urls:
                        if self._candidate_limit_reached(candidates):
                            break
                        label = f"关注作者:{self._author_label(author_url)}"
                        try:
                            cards = self._collect_author_cards(
                                page,
                                author_url=author_url,
                                matched_keyword=label,
                            )
                            successful_sources.add(label)
                            self._merge_candidates(candidates, cards)
                        except SocialSignalAccessPausedError:
                            raise
                        except Exception as exc:
                            source_errors.append(f"作者主页“{author_url}”：{exc}")

                    known_ids = known_post_ids or set()
                    detail_limit = max(
                        0,
                        settings.xhs_social_signal_max_detail_requests,
                    )
                    # 先排除窗口外旧帖与数据库已有帖子，避免每次刷新都重开相同详情页。
                    detail_candidates = [
                        candidate
                        for candidate in candidates.values()
                        if candidate["post_id"] not in known_ids
                        and self._candidate_may_be_in_window(
                            candidate,
                            since=since,
                            until=until,
                        )
                    ]
                    detail_candidates.sort(
                        key=lambda item: self._published_at_from_post_id(
                            item["post_id"],
                            reference_time=until,
                        )
                        or datetime.min.replace(tzinfo=timezone.utc),
                        reverse=True,
                    )
                    for candidate in detail_candidates[:detail_limit]:
                        try:
                            post = self._read_detail(
                                page,
                                candidate=candidate,
                                reference_time=until,
                            )
                        except SocialSignalAccessPausedError:
                            raise
                        except Exception as exc:
                            source_errors.append(
                                f"笔记 {candidate['post_id']}：{exc}"
                            )
                            continue
                        if post is None:
                            source_errors.append(
                                f"笔记 {candidate['post_id']}：未识别公开正文或发布时间"
                            )
                            continue
                        if since <= post["published_at"] <= until:
                            posts.append(self._finalize_post(post))
            except SocialSignalAccessPausedError as exc:
                # 暂停后不再导航。先前已读到的完整帖子照常交回服务层保存，不整批丢弃。
                batch_status = exc.status
                source_errors.append(str(exc))
            except SocialSignalProviderError:
                raise
            except Exception as exc:
                raise self._friendly_browser_error(exc) from exc

        if not successful_sources and batch_status == "completed":
            message = "；".join(source_errors[:6]) or "公开页面没有返回可用内容"
            raise SocialSignalProviderError(f"所有小红书来源均采集失败：{message}")

        return SocialPostBatch(
            posts=tuple(posts),
            successful_keywords=tuple(sorted(successful_sources)),
            source_errors=tuple(source_errors),
            status=batch_status,
        )

    def _search_keyword(self, page: Any, keyword: str) -> list[dict[str, Any]]:
        """打开公开搜索页，轻量滚动后从 DOM 中提取笔记链接。"""

        url = XHS_SEARCH_URL.format(quote(keyword))
        response = page.goto(
            url,
            wait_until="domcontentloaded",
            timeout=self._navigation_timeout_ms,
        )
        self._settle_page(page)
        self._ensure_page_access(page, response=response)
        self._scroll_page(page)
        self._ensure_page_access(page)
        cards = self._extract_cards(page)
        return [
            normalized
            for item in cards
            if (normalized := self._normalize_card(item, matched_keyword=keyword))
            is not None
        ]

    def _collect_author_cards(
        self,
        page: Any,
        *,
        author_url: str,
        matched_keyword: str,
    ) -> list[dict[str, Any]]:
        """读取用户主动配置的公开作者主页，不自动发现或批量扩散作者。"""

        response = page.goto(
            author_url,
            wait_until="domcontentloaded",
            timeout=self._navigation_timeout_ms,
        )
        self._settle_page(page)
        self._ensure_page_access(page, response=response)
        self._scroll_page(page)
        self._ensure_page_access(page)
        cards = self._extract_cards(page)
        return [
            normalized
            for item in cards
            if (
                normalized := self._normalize_card(
                    item,
                    matched_keyword=matched_keyword,
                )
            )
            is not None
        ]

    def _read_detail(
        self,
        page: Any,
        *,
        candidate: dict[str, Any],
        reference_time: datetime,
    ) -> dict[str, Any] | None:
        """打开一篇公开笔记，优先读取页面状态中的精确时间，再读取可见 DOM。"""

        response = page.goto(
            candidate["source_url"],
            wait_until="domcontentloaded",
            timeout=self._navigation_timeout_ms,
        )
        self._settle_page(page)
        self._ensure_page_access(page, response=response)
        raw = page.evaluate(self._detail_extraction_script(candidate["post_id"]))
        if not isinstance(raw, dict):
            return None
        if self._raw_page_requests_login(raw):
            self._clear_session_marker()
            raise SocialSignalLoginRequiredError(
                "小红书要求重新登录或扫码，采集已立即停止；请稍后重新连接"
            )

        title = self._clean_text(raw.get("title") or candidate.get("title"))
        # 详情页首屏字段变化时，保留搜索卡片已公开展示的文字作为降级摘要。
        content = self._clean_description(
            raw.get("content") or candidate.get("card_text")
        )
        if not title and not content:
            return None

        published_at = self._parse_published_at(
            raw.get("published_value") or raw.get("published_text"),
            reference_time=reference_time,
        )
        if published_at is None:
            # DOM/首屏状态仍是优先依据；只有缺失时才用笔记 ID 中的秒数兜底。
            published_at = self._published_at_from_post_id(
                candidate["post_id"],
                reference_time=reference_time,
            )
        if published_at is None:
            return None

        author_name = self._clean_text(
            raw.get("author_name") or candidate.get("author_name")
        )
        author_url = self._clean_text(
            raw.get("author_url") or candidate.get("author_url")
        )
        author_id = self._author_id(author_url=author_url, author_name=author_name)
        if not author_id:
            return None

        images = self._capture_images(page, raw.get("image_urls") or [], candidate["post_id"])
        return {
            "post_id": candidate["post_id"],
            "platform": "xiaohongshu",
            "author_id": author_id,
            "author_name": author_name or "小红书用户",
            "published_at": published_at,
            "title": title,
            "content_excerpt": content,
            "images": images,
            "source_url": candidate["source_url"],
            "matched_keyword": candidate["matched_keyword"],
            "like_count": self._parse_count(
                raw.get("like_count") or candidate.get("like_count")
            ),
            "comment_count": self._parse_count(raw.get("comment_count")),
            "collect_count": self._parse_count(raw.get("collect_count")),
        }

    def _capture_images(self, page: Any, urls: list[str], post_id: str) -> list[dict]:
        """仅在正常详情采集期间缓存少量公开 CDN 图片；遇限流立即停止，不补抓旧帖。"""
        from app.services.social_signal_media import allowed_image_url, cache_image, MAX_IMAGE_BYTES

        images = []
        unique_urls = list(dict.fromkeys(str(url) for url in urls))
        for index, url in enumerate(unique_urls[:max(0, min(settings.xhs_social_image_limit, 6))]):
            if self._images_remaining <= 0:
                break
            if not allowed_image_url(url):
                continue
            self._ensure_page_access(page)
            self._images_remaining -= 1
            response = None
            try:
                # 不使用代理、签名逆向或重试。禁止重定向，避免 CDN URL 指向未知地址。
                response = page.context.request.get(url, timeout=15000, max_redirects=0)
                if response.status in {403, 429}:
                    self._pause_access(page, message="小红书图片来源限制访问，已暂停采集；已有内容可继续分析。")
                if response.status != 200 or int(response.headers.get("content-length", 0)) > MAX_IMAGE_BYTES:
                    continue
                images.append(cache_image(response.body(), post_id=post_id, index=index + 1, source_url=url))
            except SocialSignalAccessPausedError:
                raise
            except Exception:
                # 一张损坏图片不能抹掉已读到的正文；未取得图片时界面明确显示缺失。
                continue
            finally:
                if response is not None:
                    response.dispose()
            self._ensure_page_access(page)
        return images

    @staticmethod
    def _extract_cards(page: Any) -> list[dict[str, Any]]:
        """只读取页面已经渲染出的链接和文本，不调用网页内部接口。"""

        result = page.evaluate(
            """
            () => {
              const anchors = Array.from(document.querySelectorAll(
                'a[href*="/explore/"], a[href*="/discovery/item/"]'
              ));
              return anchors.map((anchor) => {
                const container = anchor.closest('section.note-item, article, .note-item, section')
                  || anchor.parentElement?.parentElement
                  || anchor.parentElement;
                // 同一卡片经常同时有封面和标题两个链接。优先使用带 xsec_token 的
                // 链接，否则直接访问短链接时详情正文可能不会加载。
                const cardAnchors = Array.from(container?.querySelectorAll(
                  'a[href*="/explore/"], a[href*="/discovery/item/"]'
                ) || []);
                const detailAnchor = cardAnchors.find((item) =>
                  (item.href || item.getAttribute('href') || '').includes('xsec_token=')
                ) || anchor;
                const pickText = (selectors) => {
                  for (const selector of selectors) {
                    const node = container?.querySelector(selector);
                    const value = node?.textContent?.trim();
                    if (value) return value;
                  }
                  return '';
                };
                const authorAnchor = container?.querySelector(
                  'a[href*="/user/profile/"], a[href*="/user/profile"]'
                );
                return {
                  href: detailAnchor.href || detailAnchor.getAttribute('href') || '',
                  title: pickText(['.title', '[class*="title"]', '[class*="desc"]']),
                  author_name: pickText([
                    '.author-wrapper .name', '.author .name', '[class*="author"] [class*="name"]'
                  ]),
                  author_url: authorAnchor?.href || '',
                  like_count: pickText(['.like-wrapper .count', '[class*="like"] [class*="count"]']),
                  card_text: container?.innerText?.trim() || ''
                };
              });
            }
            """
        )
        return result if isinstance(result, list) else []

    @staticmethod
    def _detail_extraction_script(post_id: str) -> str:
        """构造只读页面脚本；post_id 经 JSON 编码后不会进入可执行代码。"""

        safe_post_id = json.dumps(str(post_id), ensure_ascii=False)
        return f"""
            () => {{
              const expectedId = {safe_post_id};
              const text = (selectors) => {{
                for (const selector of selectors) {{
                  const value = document.querySelector(selector)?.textContent?.trim();
                  if (value) return value;
                }}
                return '';
              }};
              const attr = (selector, name) =>
                document.querySelector(selector)?.getAttribute(name) || '';

              // 公开详情页有时只把毫秒时间戳放在首屏状态里。这里遍历已加载的页面对象，
              // 不发送新请求；找到当前 noteId 后只读取正文、作者和互动字段。
              let stateNote = null;
              const roots = [window.__INITIAL_STATE__, window.__INITIAL_SSR_STATE__]
                .filter(Boolean);
              const seen = new WeakSet();
              const queue = [...roots];
              let visited = 0;
              while (queue.length && visited < 12000 && !stateNote) {{
                const current = queue.shift();
                visited += 1;
                if (!current || typeof current !== 'object' || seen.has(current)) continue;
                seen.add(current);
                const id = String(current.noteId || current.note_id || current.id || '');
                if (id === expectedId && (current.desc || current.title || current.time)) {{
                  stateNote = current;
                  break;
                }}
                for (const value of Object.values(current)) {{
                  if (value && typeof value === 'object') queue.push(value);
                }}
              }}

              const noteUser = stateNote?.user || stateNote?.userInfo || {{}};
              const interact = stateNote?.interactInfo || stateNote?.interact_info || {{}};
              const authorLink = document.querySelector('a[href*="/user/profile/"]');
              const bodyText = document.body?.innerText || '';
              return {{
                image_urls: [...new Set([
                  ...(stateNote?.imageList || stateNote?.image_list || []).map(image =>
                    image.urlDefault || image.url || image.infoList?.find(i => i.imageScene === 'WB_DFT')?.url || image.infoList?.[0]?.url),
                  ...Array.from(document.querySelectorAll('.note-slider img, .swiper-slide img, img.note-image'))
                    .map(image => image.currentSrc || image.src)
                ].filter(url => typeof url === 'string' && url.startsWith('https://')))],
                title: stateNote?.title
                  || attr('meta[property="og:title"]', 'content')
                  || text(['#detail-title', '.note-content .title', 'h1']),
                content: stateNote?.desc
                  || attr('meta[property="og:description"]', 'content')
                  || attr('meta[name="description"]', 'content')
                  || text(['#detail-desc', '.note-content .desc', '[class*="note-content"] [class*="desc"]']),
                author_name: noteUser.nickname
                  || text(['.author-wrapper .username', '.author-container .name', '[class*="author"] [class*="name"]']),
                author_url: authorLink?.href || '',
                published_value: stateNote?.time || stateNote?.createTime || stateNote?.create_time || '',
                published_text: attr('meta[property="article:published_time"]', 'content')
                  || attr('time[datetime]', 'datetime')
                  || text(['.bottom-container .date', '.note-content .date', '[class*="date"]']),
                like_count: interact.likedCount || interact.liked_count
                  || text(['[class*="like"] [class*="count"]']),
                comment_count: interact.commentCount || interact.comment_count
                  || text(['[class*="comment"] [class*="count"]']),
                collect_count: interact.collectedCount || interact.collected_count
                  || text(['[class*="collect"] [class*="count"]']),
                login_required: /登录后|扫码登录|手机号登录/.test(bodyText)
                  && !document.querySelector('.note-content, [class*="note-content"]')
              }};
            }}
        """

    def _normalize_card(
        self,
        item: Any,
        *,
        matched_keyword: str,
    ) -> dict[str, Any] | None:
        if not isinstance(item, dict):
            return None
        raw_url = self._clean_text(item.get("href"))
        source_url = urljoin(XHS_HOME_URL, raw_url)
        post_id = self._post_id_from_url(source_url)
        if not post_id:
            return None

        card_text = self._clean_text(item.get("card_text"))
        title = self._clean_text(item.get("title"))
        if not title and card_text:
            title = card_text.split(" ", 1)[0]
        return {
            "post_id": post_id,
            "source_url": source_url,
            "title": title[:500],
            "author_name": self._clean_text(item.get("author_name")),
            "author_url": self._clean_text(item.get("author_url")),
            "like_count": self._parse_count(item.get("like_count")),
            "card_text": card_text[:MAX_EXCERPT_CHARS],
            "matched_keyword": matched_keyword,
        }

    @staticmethod
    def _post_id_from_url(url: str) -> str:
        match = re.search(r"/(?:explore|discovery/item)/([A-Za-z0-9_-]+)", url)
        return match.group(1) if match else ""

    @classmethod
    def _candidate_may_be_in_window(
        cls,
        candidate: dict[str, Any],
        *,
        since: datetime,
        until: datetime,
    ) -> bool:
        """能从 ID 判断为旧帖时直接跳过；无法判断时仍给详情页一次机会。"""

        published_at = cls._published_at_from_post_id(
            candidate.get("post_id"),
            reference_time=until,
        )
        return published_at is None or since <= published_at <= until

    @classmethod
    def _published_at_from_post_id(
        cls,
        post_id: Any,
        *,
        reference_time: datetime,
    ) -> datetime | None:
        """从 24 位十六进制笔记 ID 读取创建秒数，作为页面时间缺失时的兜底。"""

        text = cls._clean_text(post_id)
        if not re.fullmatch(r"[0-9a-fA-F]{24}", text):
            return None
        try:
            parsed = datetime.fromtimestamp(int(text[:8], 16), tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None

        # 若平台以后改变 ID 规则，合理范围限制会让代码回到 DOM 解析，而不是制造假时间。
        earliest = datetime(2013, 1, 1, tzinfo=timezone.utc)
        latest = reference_time.astimezone(timezone.utc) + timedelta(days=1)
        return parsed if earliest <= parsed <= latest else None

    @classmethod
    def _author_id(cls, *, author_url: str, author_name: str) -> str:
        match = re.search(r"/user/profile/([A-Za-z0-9_-]+)", author_url)
        if match:
            return match.group(1)
        if author_name:
            digest = hashlib.sha256(author_name.encode("utf-8")).hexdigest()[:20]
            return f"name:{digest}"
        return ""

    @classmethod
    def _parse_published_at(
        cls,
        value: Any,
        *,
        reference_time: datetime,
    ) -> datetime | None:
        """解析毫秒时间戳、ISO 时间和常见中文相对时间，最终统一为 UTC。"""

        if isinstance(value, datetime):
            parsed = value
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=SHANGHAI_TZ)
            return parsed.astimezone(timezone.utc)

        text = cls._clean_text(value)
        if not text:
            return None
        if isinstance(value, (int, float)) or text.isdigit():
            timestamp = float(value)
            if timestamp > 10_000_000_000:
                timestamp /= 1000
            try:
                return datetime.fromtimestamp(timestamp, tz=timezone.utc)
            except (OverflowError, OSError, ValueError):
                return None

        local_reference = reference_time.astimezone(SHANGHAI_TZ)
        if "刚刚" in text:
            return reference_time.astimezone(timezone.utc)
        relative_match = re.search(r"(\d+)\s*(分钟|小时)前", text)
        if relative_match:
            amount = int(relative_match.group(1))
            delta = (
                timedelta(minutes=amount)
                if relative_match.group(2) == "分钟"
                else timedelta(hours=amount)
            )
            return (local_reference - delta).astimezone(timezone.utc)

        cleaned = re.sub(r"^(发布于|编辑于)\s*", "", text)
        cleaned = cleaned.replace("年", "-").replace("月", "-").replace("日", " ")
        cleaned = re.sub(r"\s+(北京|上海|广东|江苏|浙江|四川|海外|IP属地).*", "", cleaned)
        cleaned = cleaned.strip()

        day_offset = 0
        if cleaned.startswith("昨天"):
            day_offset = 1
            cleaned = cleaned.removeprefix("昨天").strip()
        elif cleaned.startswith("今天"):
            cleaned = cleaned.removeprefix("今天").strip()
        if day_offset or text.startswith("今天"):
            time_match = re.search(r"(\d{1,2}):(\d{2})", cleaned)
            hour = int(time_match.group(1)) if time_match else 12
            minute = int(time_match.group(2)) if time_match else 0
            local = (local_reference - timedelta(days=day_offset)).replace(
                hour=hour,
                minute=minute,
                second=0,
                microsecond=0,
            )
            return local.astimezone(timezone.utc)

        iso_text = cleaned.replace("Z", "+00:00")
        with suppress(ValueError):
            parsed = datetime.fromisoformat(iso_text)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=SHANGHAI_TZ)
            return parsed.astimezone(timezone.utc)

        for pattern in ("%Y-%m-%d %H:%M", "%Y-%m-%d"):
            with suppress(ValueError):
                parsed = datetime.strptime(cleaned, pattern).replace(tzinfo=SHANGHAI_TZ)
                # 只有日期时使用中午，避免把平台的日期级信息伪装成精确时刻。
                if pattern == "%Y-%m-%d":
                    parsed = parsed.replace(hour=12)
                return parsed.astimezone(timezone.utc)

        month_day = re.search(
            r"(?<!\d)(\d{1,2})-(\d{1,2})(?:\s+(\d{1,2}):(\d{2}))?",
            cleaned,
        )
        if month_day:
            local = local_reference.replace(
                month=int(month_day.group(1)),
                day=int(month_day.group(2)),
                hour=int(month_day.group(3) or 12),
                minute=int(month_day.group(4) or 0),
                second=0,
                microsecond=0,
            )
            if local > local_reference + timedelta(days=1):
                local = local.replace(year=local.year - 1)
            return local.astimezone(timezone.utc)
        return None

    def _current_keywords(self) -> list[str]:
        """组合固定关键词和已导入基金画像，并使用一个全局数量上限。"""

        values = list(settings.xhs_social_signal_keywords)
        if self.keyword_loader is not None:
            values.extend(self.keyword_loader())
        return self._unique_limited(
            values,
            limit=max(1, settings.xhs_social_signal_max_keywords),
        )

    def _author_urls(self) -> list[str]:
        values = []
        for raw_url in settings.xhs_social_signal_author_urls:
            url = self._clean_text(raw_url)
            parsed = urlparse(url)
            if (
                parsed.scheme in {"http", "https"}
                and parsed.netloc.lower().endswith("xiaohongshu.com")
                and "/user/profile/" in parsed.path
            ):
                values.append(url)
        return self._unique_limited(
            values,
            limit=max(0, settings.xhs_social_signal_max_authors),
        )

    @classmethod
    def _unique_limited(cls, values: list[str], *, limit: int) -> list[str]:
        result: list[str] = []
        seen: set[str] = set()
        for value in values:
            cleaned = cls._clean_text(value)[:500]
            if not cleaned or cleaned in seen:
                continue
            seen.add(cleaned)
            result.append(cleaned)
            if len(result) >= limit:
                break
        return result

    def _merge_candidates(
        self,
        target: dict[str, dict[str, Any]],
        cards: list[dict[str, Any]],
    ) -> None:
        for card in cards:
            existing = target.get(card["post_id"])
            if existing is None:
                target[card["post_id"]] = card
            elif self._detail_url_score(card["source_url"]) > self._detail_url_score(
                existing["source_url"]
            ):
                # 搜索页同一笔记可能出现多个链接；保留带访问上下文的那一个。
                existing["source_url"] = card["source_url"]
            if self._candidate_limit_reached(target):
                break

    @staticmethod
    def _detail_url_score(url: str) -> int:
        """带 xsec_token 的公开搜索链接最适合继续打开详情页。"""

        if "xsec_token=" in url:
            return 2
        return 1 if "?" in url else 0

    @staticmethod
    def _author_label(author_url: str) -> str:
        match = re.search(r"/user/profile/([A-Za-z0-9_-]+)", author_url)
        return match.group(1)[:16] if match else "公开主页"

    @staticmethod
    def _resolve_profile_dir(value: str | Path) -> Path:
        path = Path(value).expanduser()
        if not path.is_absolute():
            path = PROJECT_ROOT / path
        return path.resolve()

    @property
    def _navigation_timeout_ms(self) -> int:
        return int(max(5, settings.xhs_browser_navigation_timeout_seconds) * 1000)

    def _candidate_limit_reached(self, values: dict[str, Any]) -> bool:
        return len(values) >= max(1, settings.xhs_social_signal_max_posts_per_refresh)

    def _settle_page(self, page: Any) -> None:
        page.wait_for_timeout(int(max(0, settings.xhs_browser_settle_seconds) * 1000))

    def _scroll_page(self, page: Any) -> None:
        for _ in range(max(0, settings.xhs_browser_scroll_rounds)):
            self._ensure_page_access(page)
            page.evaluate("window.scrollBy(0, Math.max(window.innerHeight, 800))")
            page.wait_for_timeout(600)
            self._ensure_page_access(page)

    @staticmethod
    def _primary_page(context: Any) -> Any:
        pages = getattr(context, "pages", [])
        return pages[0] if pages else context.new_page()

    def _has_login_cookie(self, context: Any) -> bool:
        expected = set(settings.xhs_browser_login_cookie_names)
        try:
            cookies = context.cookies(["https://www.xiaohongshu.com"])
        except TypeError:
            cookies = context.cookies()
        return any(
            str(cookie.get("name") or "") in expected
            and bool(cookie.get("value"))
            for cookie in cookies
            if isinstance(cookie, dict)
        )

    def _has_authenticated_session(self, context: Any, page: Any) -> bool:
        """Cookie 与可见页面必须同时表明已登录，才能自动关闭登录窗口。"""

        return (
            self._has_login_cookie(context)
            and not self._page_requests_login(page)
            and not self._page_requests_verification(page)
        )

    @staticmethod
    def _page_requests_verification(page: Any) -> bool:
        """识别明确的访问门禁文案，不因帖子提到普通“验证码/风控”词语就暂停。"""

        try:
            body = page.locator("body").inner_text(timeout=1500)
        except Exception as exc:
            # 看不到页面状态时，不能把它解释为“没有验证”。本轮先停止访问。
            raise SocialSignalProviderError("无法确认小红书页面状态，本轮采集已停止") from exc
        return not body.strip() or bool(re.search(
            r"安全验证|请完成验证|访问(?:过于|过度)?频繁|操作(?:过于)?频繁|"
            r"访问异常|网络环境异常|异常流量|扫码验证|请拖动.{0,12}滑块|"
            r"请使用小红书.{0,15}扫码|access denied|403 forbidden|too many requests",
            body,
            re.IGNORECASE,
        ))

    def _ensure_page_access(self, page: Any, *, response: Any = None) -> None:
        """先识别访问限制，再读取帖子。403/429 不当成可继续换关键词的空页面。"""

        try:
            restricted = getattr(response, "status", None) in {403, 429} or self._page_requests_verification(page)
        except SocialSignalProviderError:
            self._pause_access(page, message="无法确认小红书页面状态，采集已暂停。请稍后手动检查连接，已保存样本仍可查看。")
        if restricted:
            self._pause_access(page, message="小红书要求安全验证或限制访问，采集已暂停。请停止反复刷新，稍后点击连接按钮人工处理；已保存样本仍可查看。")
        if self._page_requests_login(page):
            self._clear_session_marker()
            raise SocialSignalLoginRequiredError(
                "小红书要求重新登录或扫码，采集已停止；请稍后手动连接，已保存样本仍可查看。"
            )

    def _pause_access(self, page: Any, *, message: str) -> None:
        """暂停标记一直保留到人工重新连接确认，不会靠重启或定时器自动解除。"""

        access = self._read_access_state()
        # 仅保留公开搜索地址供人工恢复；详情令牌和验证 URL 参数不落入状态文件。
        recovery = XHS_HOME_URL
        current = urlparse(str(getattr(page, "url", "")))
        if current.hostname == "www.xiaohongshu.com" and current.path == "/search_result":
            keyword = parse_qs(current.query).get("keyword", [""])[0][:100]
            recovery = XHS_SEARCH_URL.format(quote(keyword))
        access.update(verification_required=True, access_message=message, recovery_url=recovery)
        try:
            self._write_access_state(access)
        except OSError as exc:
            raise SocialSignalAccessPausedError(f"{message}本地暂停状态保存失败，本轮仍已停止。") from exc
        raise SocialSignalAccessPausedError(message)

    def _read_access_state(self) -> dict[str, Any]:
        """进程之间共享少量状态；损坏时暂停，不能因为读失败就重新发起采集。"""

        if not self.access_marker.exists():
            return {}
        try:
            value = json.loads(self.access_marker.read_text(encoding="utf-8"))
            if not isinstance(value, dict):
                raise ValueError("invalid access state")
            if value.get("next_collection_at"):
                parsed = datetime.fromisoformat(value["next_collection_at"])
                if parsed.tzinfo is None:
                    raise ValueError("missing time zone")
            # 恢复入口只能是本平台公开页面，不能让本地状态引导浏览器去其他站点。
            if value.get("recovery_url"):
                url = urlparse(value["recovery_url"])
                if url.scheme != "https" or url.hostname != "www.xiaohongshu.com" or url.path not in {"/explore", "/search_result"}:
                    raise ValueError("invalid recovery page")
            return value
        except (OSError, ValueError, TypeError):
            return {"verification_required": True, "access_message": "本地采集状态无法读取，已暂停自动访问；请手动重新连接。"}

    def _write_access_state(self, value: dict[str, Any]) -> None:
        # 写入发生在既有浏览器锁内；原子替换让状态 GET 不会读到半截 JSON。
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        temporary = self.access_marker.with_suffix(".tmp")
        temporary.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        temporary.replace(self.access_marker)

    def _claim_collection_slot(self) -> None:
        access = self._read_access_state()
        if access.get("verification_required"):
            raise SocialSignalAccessPausedError(access.get("access_message") or "采集已暂停，请手动检查小红书连接。")
        now = datetime.now(timezone.utc)
        next_at = access.get("next_collection_at")
        if next_at and datetime.fromisoformat(next_at) > now:
            raise SocialSignalCooldownError("距上次采集未满本地限频间隔，本次未访问小红书；请稍后手动重试。")
        access["next_collection_at"] = (
            now + timedelta(seconds=max(0, settings.xhs_browser_min_refresh_seconds))
        ).isoformat()
        self._write_access_state(access)

    @staticmethod
    def _page_requests_login(page: Any) -> bool:
        try:
            body = page.locator("body").inner_text(timeout=1500)
        except Exception:
            return False
        return bool(re.search(r"登录后|扫码登录|手机号登录", body))

    @staticmethod
    def _raw_page_requests_login(value: dict[str, Any]) -> bool:
        return value.get("login_required") is True

    def _write_session_marker(self) -> None:
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self.session_marker.write_text(
            json.dumps(
                {
                    "connected_at": datetime.now(timezone.utc).isoformat(),
                    "provider": self.source,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

    def _clear_session_marker(self) -> None:
        if self._session_ready_override is not None:
            self._session_ready_override = False
        with suppress(FileNotFoundError):
            self.session_marker.unlink()

    @contextmanager
    def _browser_context(self, *, headless: bool) -> Iterator[Any]:
        """创建持久化浏览器；测试可完全替换这层而不安装或启动 Chrome。"""

        if self.context_factory is not None:
            with self.context_factory(headless) as context:
                yield context
            return

        try:
            from playwright.sync_api import sync_playwright
        except ModuleNotFoundError as exc:
            raise SocialSignalProviderError(
                "缺少 Playwright，请先安装 requirements.txt"
            ) from exc

        self.profile_dir.mkdir(parents=True, exist_ok=True)
        with sync_playwright() as playwright:
            launch_options: dict[str, Any] = {
                "user_data_dir": str(self.profile_dir),
                "headless": headless,
                "locale": "zh-CN",
                "timezone_id": "Asia/Shanghai",
                "viewport": {"width": 1360, "height": 900},
            }
            channel = settings.xhs_browser_channel.strip()
            if channel:
                launch_options["channel"] = channel
            context = playwright.chromium.launch_persistent_context(**launch_options)
            try:
                context.set_default_timeout(self._navigation_timeout_ms)
                yield context
            finally:
                with suppress(Exception):
                    context.close()

    @contextmanager
    def _browser_lock(self) -> Iterator[None]:
        """序列化同一 Profile 的操作；文件锁不可用时仍保留线程锁。"""

        self.profile_dir.mkdir(parents=True, exist_ok=True)
        lock_path = self.profile_dir.parent / ".xhs-browser.lock"
        with _BROWSER_THREAD_LOCK:
            with lock_path.open("a+", encoding="utf-8") as handle:
                file_lock_module: Any | None = None
                try:
                    import fcntl

                    file_lock_module = fcntl
                    file_lock_module.flock(handle.fileno(), file_lock_module.LOCK_EX)
                except (ImportError, OSError):
                    pass
                try:
                    yield
                finally:
                    if file_lock_module is not None:
                        with suppress(OSError):
                            file_lock_module.flock(
                                handle.fileno(),
                                file_lock_module.LOCK_UN,
                            )

    @classmethod
    def _friendly_browser_error(cls, exc: Exception) -> SocialSignalProviderError:
        text = cls._clean_text(exc)
        if "Executable doesn't exist" in text or "chromium" in text.lower():
            return SocialSignalProviderError(
                "没有找到可启动的 Chrome；请安装 Google Chrome，或执行 playwright install chromium"
            )
        if "Target page, context or browser has been closed" in text:
            return SocialSignalProviderError("小红书浏览器窗口被提前关闭")
        return SocialSignalProviderError(f"小红书公开页面采集失败：{text[:300]}")

    @staticmethod
    def _is_browser_closed_error(exc: Exception) -> bool:
        return "has been closed" in str(exc).lower()

    @classmethod
    def _finalize_post(cls, post: dict[str, Any]) -> dict[str, Any]:
        title = cls._clean_text(post.get("title"))[:500]
        excerpt = cls._clean_text(post.get("content_excerpt"))[:MAX_EXCERPT_CHARS]
        hash_source = " ".join(
            [str(post["post_id"]), str(post["author_id"]), title, excerpt]
            + [image["media_id"] for image in post.get("images", [])]
        ).casefold()
        digest = hashlib.sha256(hash_source.encode("utf-8")).hexdigest()
        return {
            **post,
            "title": title,
            "content_excerpt": excerpt,
            "content_hash": f"sha256:{digest}",
        }

    @classmethod
    def _clean_description(cls, value: Any) -> str:
        text = cls._clean_text(value)
        # og:description 偶尔追加平台品牌后缀；只删除明确的尾部品牌，不猜测正文。
        return re.sub(r"\s*[-|]\s*小红书\s*$", "", text).strip()

    @staticmethod
    def _clean_text(value: Any) -> str:
        return re.sub(r"\s+", " ", str(value or "")).strip()

    @classmethod
    def _parse_count(cls, value: Any) -> int:
        text = cls._clean_text(value).replace(",", "")
        if not text:
            return 0
        multiplier = 10_000 if text.endswith("万") else 1
        if multiplier > 1:
            text = text[:-1]
        try:
            return max(0, int(float(text) * multiplier))
        except ValueError:
            return 0
