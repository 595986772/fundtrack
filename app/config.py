"""项目配置文件。

真实 Agent 项目里经常会有模型名、API Key、数据库地址等配置。
这些配置不适合硬编码在业务代码中，更适合通过环境变量读取。
"""

import os

try:
    from dotenv import load_dotenv
except ModuleNotFoundError:
    load_dotenv = None

if load_dotenv is not None:
    # 如果项目根目录有 .env 文件，就先加载里面的环境变量。
    # 这样本地开发时不用每次都手动 export DEEPSEEK_API_KEY。
    load_dotenv()


class Settings:
    """集中管理项目配置。

    os.getenv("变量名", "默认值") 的意思是：
    - 如果系统环境变量里配置了这个值，就使用环境变量；
    - 如果没有配置，就使用第二个参数里的默认值。
    """

    # 应用名称，会展示在 `/docs` 文档页顶部。
    app_name: str = os.getenv("APP_NAME", "Agent Workspace")

    # 正式运行模式会在启动时核对认证、持久化和 HTTPS 配置。本地教学模式不受影响。
    deployment_mode: str = os.getenv("DEPLOYMENT_MODE", "local").strip().lower()
    public_origin: str = os.getenv("PUBLIC_ORIGIN", "").strip().rstrip("/")
    public_host: str = os.getenv("PUBLIC_HOST", "").strip().lower()

    # 追踪只记录步骤和用量。默认仅写本地，配置远程出口后才上传脱敏记录。
    trace_enabled: bool = os.getenv("TRACE_ENABLED", "true").lower() == "true"
    trace_local_enabled: bool = os.getenv("TRACE_LOCAL_ENABLED", "true").lower() == "true"
    trace_local_dir: str = os.getenv("TRACE_LOCAL_DIR", "data/traces")
    trace_sample_rate: float = float(os.getenv("TRACE_SAMPLE_RATE", "1.0"))
    trace_service_name: str = os.getenv("TRACE_SERVICE_NAME", "fundscope")
    trace_release: str = os.getenv("TRACE_RELEASE", "development")
    # 通用 OTLP 使用完整 traces 地址；为空时可用下面的 Langfuse 项目凭据导出。
    trace_otlp_endpoint: str = os.getenv("TRACE_OTLP_ENDPOINT", "")
    langfuse_base_url: str = os.getenv("LANGFUSE_BASE_URL", "")
    langfuse_public_key: str = os.getenv("LANGFUSE_PUBLIC_KEY", "")
    langfuse_secret_key: str = os.getenv("LANGFUSE_SECRET_KEY", "")

    # DeepSeek 官方 OpenAI 兼容接口地址。
    deepseek_base_url: str = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")

    # DeepSeek API Key。不要写死在代码里，应该在终端或 .env 中配置。
    deepseek_api_key: str | None = os.getenv("DEEPSEEK_API_KEY")

    # DeepSeek 模型名。默认使用当前官方推荐的新模型名之一。
    deepseek_model: str = os.getenv("DEEPSEEK_MODEL", "deepseek-v4-pro")

    # 给模型的系统提示词，用来规定助手的角色和回答风格。
    agent_system_prompt: str = os.getenv(
        "AGENT_SYSTEM_PROMPT",
        "You are a helpful assistant. Answer clearly and concisely.",
    )

    # 对外展示的 Agent 模型名。health 接口会读取这个字段。
    agent_model: str = deepseek_model

    # 投研第二轮需要消化工具证据，等待时间独立于轻量分类调用。
    # 限制单次模型响应时间，同时保留取消入口；不是增加 Agent 工具轮数。
    agent_model_timeout_seconds: float = float(os.getenv("AGENT_MODEL_TIMEOUT_SECONDS", "90"))
    agent_model_max_attempts: int = int(os.getenv("AGENT_MODEL_MAX_ATTEMPTS", "2"))
    # HTTP 产品聊天总时限；内部阶段共享截止时间，后台 Job 不继承它。
    agent_chat_budget_enabled: bool = os.getenv("AGENT_CHAT_BUDGET_ENABLED", "true").lower() not in {"0", "false", "no"}
    agent_chat_timeout_seconds: float = float(os.getenv("AGENT_CHAT_TIMEOUT_SECONDS", "120"))
    agent_chat_finalize_reserve_seconds: float = float(os.getenv("AGENT_CHAT_FINALIZE_RESERVE_SECONDS", "20"))
    # 请求内防空转与取证清单；不替代轮数、费用和授权限制。可关闭以做同题对照。
    agent_loop_guard_enabled: bool = os.getenv("AGENT_LOOP_GUARD_ENABLED", "true").lower() not in {"0", "false", "no"}

    # 只给复杂产品问题开放按需工具发现；不增加主循环轮数或模型调用额度。
    agent_tool_discovery_enabled: bool = os.getenv("AGENT_TOOL_DISCOVERY_ENABLED", "true").lower() not in {"0", "false", "no"}
    agent_tool_discovery_max_calls: int = int(os.getenv("AGENT_TOOL_DISCOVERY_MAX_CALLS", "2"))
    agent_tool_discovery_top_k: int = int(os.getenv("AGENT_TOOL_DISCOVERY_TOP_K", "3"))
    # 这是标准化 Schema 字符上限，不是供应商 Token 计费数。
    agent_tool_discovery_max_schema_chars: int = int(os.getenv("AGENT_TOOL_DISCOVERY_MAX_SCHEMA_CHARS", "16000"))

    # 任务授权限制本次 Agent 可读的组合，不改变用户本人在页面上的原有权限。
    # OpenFGA 不可用时拒绝受保护的操作，绝不自动切回“全部允许”。
    agent_task_authorization_enabled: bool = os.getenv("AGENT_TASK_AUTHORIZATION_ENABLED", "true").lower() not in {"0", "false", "no"}
    agent_task_grant_seconds: int = int(os.getenv("AGENT_TASK_GRANT_SECONDS", "1800"))
    openfga_url: str = os.getenv("OPENFGA_URL", "http://127.0.0.1:8091")
    openfga_store_id: str = os.getenv("OPENFGA_STORE_ID", "")
    openfga_model_id: str = os.getenv("OPENFGA_MODEL_ID", "")
    openfga_token: str = os.getenv("OPENFGA_TOKEN", "")
    openfga_timeout_seconds: float = float(os.getenv("OPENFGA_TIMEOUT_SECONDS", "3"))
    openfga_config_path: str = os.getenv("OPENFGA_CONFIG_PATH", "data/openfga.json")

    # 季报读取、分析、重试和引用修正共用总预算；不改变普通Agent的原有超时。
    fund_report_timeout_seconds: float = float(os.getenv("FUND_REPORT_TIMEOUT_SECONDS", "110"))
    # 先保留原模式：evidence关闭整理轮思考，balanced保留低强度思考。
    # 候选未通过真实质量验收前不默认启用，也不影响明确数字题已有的轻量路径。
    fund_report_synthesis_mode: str = os.getenv("FUND_REPORT_SYNTHESIS_MODE", "standard")

    # 可选的备用 OpenAI 兼容模型。未配置 fallback model/key 时不会创建额外客户端；
    # 配置后，主供应商网络或服务异常才按顺序切换，正常请求仍只调用主模型。
    llm_fallback_model: str | None = os.getenv("LLM_FALLBACK_MODEL") or None
    llm_fallback_base_url: str = os.getenv(
        "LLM_FALLBACK_BASE_URL",
        deepseek_base_url,
    )
    llm_fallback_api_key: str | None = (
        os.getenv("LLM_FALLBACK_API_KEY") or deepseek_api_key
    )

    # 认证迁移模式：本地默认允许旧 user_id，便于继续访问已有学习数据；部署环境应设为
    # true，此时没有有效 HttpOnly Session Cookie 的业务请求会直接返回 401。
    auth_required: bool = os.getenv("AUTH_REQUIRED", "false").strip().lower() in {
        "1",
        "true",
        "yes",
    }
    auth_cookie_name: str = os.getenv("AUTH_COOKIE_NAME", "fundscope_session")
    auth_session_days: int = int(os.getenv("AUTH_SESSION_DAYS", "7"))
    auth_cookie_secure: bool = os.getenv(
        "AUTH_COOKIE_SECURE",
        "false",
    ).strip().lower() in {"1", "true", "yes"}

    # 聊天入口与模型请求分别限流：一次聊天可能包含多轮模型调用。
    chat_rate_limit_per_minute: int = int(
        os.getenv("CHAT_RATE_LIMIT_PER_MINUTE", "20")
    )

    # 调用治理不改变业务工具，只给模型调用加上资源和 Token 边界。
    # auto 在 Celery 模式使用 Redis，本地单进程使用 memory；Redis 故障不静默降级。
    ai_governance_enabled: bool = os.getenv("AI_GOVERNANCE_ENABLED", "true").lower() not in {"0", "false", "no"}
    ai_control_backend: str = os.getenv("AI_CONTROL_BACKEND", "auto").lower()
    ai_redis_url: str = os.getenv("AI_REDIS_URL", "redis://127.0.0.1:6380/2")
    ai_redis_prefix: str = os.getenv("AI_REDIS_PREFIX", "fundscope:{ai}")
    ai_model_requests_per_minute: int = int(os.getenv("AI_MODEL_REQUESTS_PER_MINUTE", "30"))
    ai_global_concurrency: int = int(os.getenv("AI_GLOBAL_CONCURRENCY", "4"))
    ai_user_concurrency: int = int(os.getenv("AI_USER_CONCURRENCY", "2"))
    ai_global_queue_size: int = int(os.getenv("AI_GLOBAL_QUEUE_SIZE", "20"))
    ai_user_queue_size: int = int(os.getenv("AI_USER_QUEUE_SIZE", "4"))
    ai_queue_wait_seconds: float = float(os.getenv("AI_QUEUE_WAIT_SECONDS", "15"))
    # 长调用会续租；进程意外退出后，槽位最迟在租约到期后可重新使用。
    ai_slot_lease_seconds: int = int(os.getenv("AI_SLOT_LEASE_SECONDS", "120"))
    # Token 不是人民币：预留用保守估计，结算只信供应商 usage。未知费用不自动归零。
    ai_daily_token_limit: int = int(os.getenv("AI_DAILY_TOKEN_LIMIT", "2000000"))
    ai_run_token_limit: int = int(os.getenv("AI_RUN_TOKEN_LIMIT", "500000"))
    ai_max_output_tokens: int = int(os.getenv("AI_MAX_OUTPUT_TOKENS", "16384"))
    # 限制待执行/执行中的后台工作总量；等待人工确认的 Job 不占队列容量。
    ai_global_pending_jobs: int = int(os.getenv("AI_GLOBAL_PENDING_JOBS", "32"))
    ai_user_pending_jobs: int = int(os.getenv("AI_USER_PENDING_JOBS", "6"))

    # Embedding 服务配置。默认按阿里云百炼 / Qwen 的 OpenAI 兼容接口来配。
    # 这里不要把 API Key 写死在代码里，应该放在 .env 的 DASHSCOPE_API_KEY。
    embedding_provider: str = os.getenv("EMBEDDING_PROVIDER", "dashscope")
    dashscope_api_key: str | None = os.getenv("DASHSCOPE_API_KEY")
    embedding_base_url: str = os.getenv(
        "EMBEDDING_BASE_URL",
        "https://dashscope.aliyuncs.com/compatible-mode/v1",
    )
    embedding_model: str = os.getenv("EMBEDDING_MODEL", "text-embedding-v4")
    embedding_dimensions: int = int(os.getenv("EMBEDDING_DIMENSIONS", "1024"))

    # 排序模型与生成模型分开配置。空地址表示未启用外部精排，仍执行本地 BM25/RRF。
    # 必须填写供应商实际的 /reranks 完整地址，不假定旧 embedding 域名支持此接口。
    fund_report_rerank_url: str = os.getenv("FUND_REPORT_RERANK_URL", "")
    fund_report_rerank_model: str = os.getenv("FUND_REPORT_RERANK_MODEL", "qwen3-rerank")
    fund_report_rerank_timeout_seconds: float = float(os.getenv("FUND_REPORT_RERANK_TIMEOUT_SECONDS", "8"))

    # Chroma 本地向量库目录。这里保存的是向量索引和 metadata，不要提交到 git。
    chroma_persist_dir: str = os.getenv("CHROMA_PERSIST_DIR", "data/chroma")
    # 正式部署固定到共享卷；本地留空时保留已有资料目录，不偷偷迁移用户文件。
    fund_report_storage_dir: str = os.getenv("FUND_REPORT_STORAGE_DIR", "")
    chroma_collection: str = os.getenv("CHROMA_COLLECTION", "fastapi_demo_docs")

    # Agent Runtime 数据库。
    # 不启动 Docker 时默认落到本地 SQLite，方便初学者直接运行；
    # Docker Compose 会覆盖为 PostgreSQL，因此 API 和 Celery Worker 能共享状态。
    database_url: str = os.getenv(
        "DATABASE_URL",
        "sqlite:///data/agent_runtime.db",
    )
    # 本地和测试仍可自动创建表；Docker/生产由 Alembic 在 API 启动前升级 schema。
    database_auto_create_schema: bool = os.getenv(
        "DATABASE_AUTO_CREATE_SCHEMA",
        "true",
    ).strip().lower() not in {"0", "false", "no"}

    # LangGraph 节点级 checkpoint。auto 会跟随 DATABASE_URL：Mac 本地使用 SQLite，
    # Docker Compose 使用 PostgreSQL。也可以单独设置数据库地址进行存储隔离。
    langgraph_checkpoint_backend: str = os.getenv(
        "LANGGRAPH_CHECKPOINT_BACKEND",
        "auto",
    ).strip().lower()
    langgraph_checkpoint_database_url: str = os.getenv(
        "LANGGRAPH_CHECKPOINT_DATABASE_URL",
        database_url,
    ).strip() or database_url

    # local：仍由当前 Python 进程的线程执行，但状态已经持久化；
    # celery：把任务发送到 Redis，由独立 Celery Worker 执行。
    job_execution_mode: str = os.getenv("JOB_EXECUTION_MODE", "local").strip().lower()
    celery_broker_url: str = os.getenv("CELERY_BROKER_URL", "redis://127.0.0.1:6379/0")
    celery_result_backend: str = os.getenv(
        "CELERY_RESULT_BACKEND",
        "redis://127.0.0.1:6379/1",
    )

    # Worker 运行边界。单个 Job 也可以在请求中给出更小的 timeout/max_retries。
    job_default_timeout_seconds: int = int(
        os.getenv("JOB_DEFAULT_TIMEOUT_SECONDS", "300")
    )
    job_default_max_retries: int = int(os.getenv("JOB_DEFAULT_MAX_RETRIES", "2"))

    # 只为新建的组合投研 Job 启用可靠投递；关闭后，已纳管任务仍保持租约保护。
    reliable_research_jobs_enabled: bool = os.getenv("RELIABLE_RESEARCH_JOBS_ENABLED", "true").lower() == "true"
    job_lease_seconds: float = max(3.0, float(os.getenv("JOB_LEASE_SECONDS", "60")))
    job_recovery_max_attempts: int = max(0, int(os.getenv("JOB_RECOVERY_MAX_ATTEMPTS", "2")))
    job_outbox_poll_seconds: float = max(0.1, float(os.getenv("JOB_OUTBOX_POLL_SECONDS", "2")))
    job_delivery_ack_seconds: float = max(5.0, float(os.getenv("JOB_DELIVERY_ACK_SECONDS", "30")))

    # SSE 不依赖 Redis Pub/Sub，而是读取共享数据库。这样即使浏览器晚连接，
    # 仍能拿到最新状态，而不是错过已经发出的进度事件。
    job_sse_poll_seconds: float = float(os.getenv("JOB_SSE_POLL_SECONDS", "0.35"))

    # FundScope 每个工作日晚间同步一次公募基金单位净值。Cron 使用运行机器的本地时区；
    # Mac 直接运行时就是系统时区，Docker 部署时应同时配置 TZ=Asia/Shanghai。
    fund_nav_sync_enabled: bool = os.getenv(
        "FUND_NAV_SYNC_ENABLED",
        "true",
    ).strip().lower() not in {"0", "false", "no"}
    fund_nav_sync_cron: str = os.getenv(
        "FUND_NAV_SYNC_CRON",
        "0 20 * * 1-5",
    )

    # 夏普和 Sortino 必须先确定无风险收益假设。默认 0% 便于离线教学和稳定回归，
    # 但 API/前端会明确展示该值；部署时可替换成团队认可的固定研究口径。
    fund_risk_free_rate_annual_pct: float = float(
        os.getenv("FUND_RISK_FREE_RATE_ANNUAL_PCT", "0.0")
    )
    # 部分基金业绩基准含少量“活期存款税后利率”。当前不伪装成实时存款利率，
    # 而是用可配置的固定假设，并把这类基准标为 proxy。
    fund_benchmark_cash_rate_annual_pct: float = float(
        os.getenv("FUND_BENCHMARK_CASH_RATE_ANNUAL_PCT", "0.0")
    )

    # 市场宽度和指数成交额在基金净值同步之后采集，避免综合研判混用不同交易日。
    market_snapshot_sync_enabled: bool = os.getenv(
        "MARKET_SNAPSHOT_SYNC_ENABLED",
        "true",
    ).strip().lower() not in {"0", "false", "no"}
    market_snapshot_sync_cron: str = os.getenv(
        "MARKET_SNAPSHOT_SYNC_CRON",
        "15 20 * * 1-5",
    )

    # 基金消息不要求盘中高频更新，默认由用户在页面中手动触发完整情报流水线。
    # 如果以后确实需要无人值守运行，可显式改成 true 并继续复用下面的 Cron 时间。
    market_event_sync_enabled: bool = os.getenv(
        "MARKET_EVENT_SYNC_ENABLED",
        "false",
    ).strip().lower() not in {"0", "false", "no"}
    market_event_sync_cron: str = os.getenv(
        "MARKET_EVENT_SYNC_CRON",
        "30 20 * * 1-5",
    )
    # 基金公告接口按这个回看窗口增量筛选；CCTV 只抓最近三天，避免每日请求过多。
    market_event_sync_lookback_days: int = int(
        os.getenv("MARKET_EVENT_SYNC_LOOKBACK_DAYS", "120")
    )
    # 每批最多 10 条；限制批次数既能清理日常积压，也避免模型异常时无限循环。
    market_intelligence_max_analysis_batches: int = int(
        os.getenv("MARKET_INTELLIGENCE_MAX_ANALYSIS_BATCHES", "5")
    )
    # 官方网站抓取边界：单次 HTTP 最多等待 12 秒，每个来源最多保存 30 条。
    # 这两个值属于采集成本控制，不影响已入库事件的查询和聚类。
    market_event_http_timeout_seconds: float = float(
        os.getenv("MARKET_EVENT_HTTP_TIMEOUT_SECONDS", "12")
    )
    market_event_max_items_per_source: int = int(
        os.getenv("MARKET_EVENT_MAX_ITEMS_PER_SOURCE", "30")
    )

    # 小红书公开观点通过本机 Chrome + Playwright 低频采集公开网页。浏览器使用独立
    # Profile 保存登录会话，不读取日常 Chrome Profile，也不把 Cookie 写入业务数据库。
    xhs_social_signal_enabled: bool = os.getenv(
        "XHS_SOCIAL_SIGNAL_ENABLED",
        "true",
    ).strip().lower() not in {"0", "false", "no"}
    xhs_browser_profile_dir: str = os.getenv(
        "XHS_BROWSER_PROFILE_DIR",
        "data/xhs_browser_profile",
    )
    # Mac 默认复用本机安装的 Google Chrome，因此不需要额外下载 Playwright Chromium。
    # 设为空字符串时改用 `playwright install chromium` 安装的浏览器。
    xhs_browser_channel: str = os.getenv("XHS_BROWSER_CHANNEL", "chrome")
    xhs_browser_headless: bool = os.getenv(
        "XHS_BROWSER_HEADLESS",
        "false",
    ).strip().lower() in {"1", "true", "yes"}
    xhs_browser_login_timeout_seconds: int = int(
        os.getenv("XHS_BROWSER_LOGIN_TIMEOUT_SECONDS", "300")
    )
    xhs_browser_navigation_timeout_seconds: float = float(
        os.getenv("XHS_BROWSER_NAVIGATION_TIMEOUT_SECONDS", "30")
    )
    xhs_browser_settle_seconds: float = float(
        os.getenv("XHS_BROWSER_SETTLE_SECONDS", "2")
    )
    xhs_browser_scroll_rounds: int = int(
        os.getenv("XHS_BROWSER_SCROLL_ROUNDS", "1")
    )
    # 跨点击、API 进程和 Worker 共用的最短采集间隔。到期只允许手动再试，不自动采集；
    # 这是本项目的限频设置，不是小红书承诺的安全频率或风控解除时间。
    xhs_browser_min_refresh_seconds: int = int(
        os.getenv("XHS_BROWSER_MIN_REFRESH_SECONDS", "900")
    )
    # Cookie 名只用于判断“平台是否已经完成登录”，代码从不读取或返回 Cookie 值。
    xhs_browser_login_cookie_names: tuple[str, ...] = tuple(
        name.strip()
        for name in os.getenv(
            "XHS_BROWSER_LOGIN_COOKIE_NAMES",
            "web_session",
        ).split(",")
        if name.strip()
    )
    # 只缓存本次正常采集页面公开给出的图片；限制额外 CDN 请求与本地存储。
    xhs_social_image_limit: int = int(os.getenv("XHS_SOCIAL_IMAGE_LIMIT", "3"))
    xhs_social_image_budget: int = int(os.getenv("XHS_SOCIAL_IMAGE_BUDGET", "6"))
    social_media_dir: str = os.getenv("SOCIAL_MEDIA_DIR", "data/social_media")

    xhs_social_signal_keywords: tuple[str, ...] = tuple(
        keyword.strip()
        for keyword in os.getenv(
            "XHS_SOCIAL_SIGNAL_KEYWORDS",
            "基金实盘,基金今日加仓,基金今日减仓,基金操作",
        ).split(",")
        if keyword.strip()
    )
    # 可选作者主页使用逗号分隔，必须是 xiaohongshu.com/user/profile/... 公开地址。
    # 不配置时仍会按关键词采集；上限防止一次刷新意外打开过多主页。
    xhs_social_signal_author_urls: tuple[str, ...] = tuple(
        url.strip()
        for url in os.getenv("XHS_SOCIAL_SIGNAL_AUTHOR_URLS", "").split(",")
        if url.strip()
    )
    xhs_social_signal_max_authors: int = int(
        os.getenv("XHS_SOCIAL_SIGNAL_MAX_AUTHORS", "50")
    )
    # 默认只使用 4 个高意图通用词。帖子和详情页都有较低硬上限，避免一次手动刷新
    # 连续打开几十个页面而触发平台登录校验，也控制后续 DeepSeek 语义抽取费用。
    xhs_social_signal_max_keywords: int = int(
        os.getenv("XHS_SOCIAL_SIGNAL_MAX_KEYWORDS", "4")
    )
    xhs_social_signal_max_posts_per_refresh: int = int(
        os.getenv("XHS_SOCIAL_SIGNAL_MAX_POSTS_PER_REFRESH", "24")
    )
    xhs_social_signal_max_detail_requests: int = int(
        os.getenv("XHS_SOCIAL_SIGNAL_MAX_DETAIL_REQUESTS", "8")
    )
    # 18 小时是绝对年龄上限；实际起点还会受到“上一交易日 15:00”约束。
    # 两者取较晚者，避免周末或长假把很早的帖子带入当前操作判断。
    xhs_social_signal_lookback_hours: int = int(
        os.getenv("XHS_SOCIAL_SIGNAL_LOOKBACK_HOURS", "18")
    )
    xhs_social_signal_analysis_batches: int = int(
        os.getenv("XHS_SOCIAL_SIGNAL_ANALYSIS_BATCHES", "4")
    )
    # 国内场外公募通常以交易日 15:00 作为当日申购赎回确认分界。这里允许配置，
    # 但只作为社区观点时效窗口，不声称覆盖每只基金或每个平台的特殊业务规则。
    xhs_social_signal_cutoff_hour: int = int(
        os.getenv("XHS_SOCIAL_SIGNAL_CUTOFF_HOUR", "15")
    )
    xhs_social_signal_cutoff_minute: int = int(
        os.getenv("XHS_SOCIAL_SIGNAL_CUTOFF_MINUTE", "0")
    )
    # 至少由三位不同作者表达后才投影成市场事件；零散帖子仍保留在社交观点 API 中。
    xhs_social_signal_min_authors: int = int(
        os.getenv("XHS_SOCIAL_SIGNAL_MIN_AUTHORS", "3")
    )


# 创建一个全局配置对象，其他文件直接导入 `settings` 使用。
settings = Settings()
