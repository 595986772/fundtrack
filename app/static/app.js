const endpoints = {
  authMe: "/api/v1/auth/me",
  authLogin: "/api/v1/auth/login",
  authRegister: "/api/v1/auth/register",
  authLogout: "/api/v1/auth/logout",
  health: "/health",
  workspace: "/api/v1/workspace",
  conversations: "/api/v1/conversations",
  agentChat: "/api/v1/agent/chat",
  agentChatStream: "/api/v1/agent/chat/stream",
  developerAgentChat: "/api/v1/developer/agent/chat",
  developerAgentChatStream: "/api/v1/developer/agent/chat/stream",
  knowledge: "/api/v1/knowledge",
  knowledgeDocuments: "/api/v1/knowledge/documents",
  knowledgeRebuild: "/api/v1/knowledge/rebuild",
  funds: "/api/v1/funds",
  fundSearch: "/api/v1/funds/search",
  fundImport: "/api/v1/funds/import",
  fundDataSync: "/api/v1/funds/data-sync",
  marketDataSync: "/api/v1/market/data-sync",
  backgroundJobs: "/background-jobs",
  marketSentiment: "/api/v1/market/sentiment",
  retailIndices: "/api/v1/market/retail-indices",
  largeCapital: "/api/v1/market/large-capital",
  largeCapitalSync: "/api/v1/market/large-capital/data-sync",
  marketEvents: "/api/v1/market/events",
  marketHotspots: "/api/v1/market/hotspots",
  marketEventTopics: "/api/v1/market/events/topics",
  marketEventDataSync: "/api/v1/market/events/data-sync",
  marketEventAnalysis: "/api/v1/market/events/analyze",
  marketEventClustering: "/api/v1/market/events/cluster",
  marketIntelligencePipeline: "/api/v1/market/events/pipeline",
  socialSignals: "/api/v1/market/social-signals",
  socialSignalBrowserSession: "/api/v1/market/social-signals/browser-session",
  socialSignalSync: "/api/v1/market/social-signals/data-sync",
  socialSignalAnalysis: "/api/v1/market/social-signals/analysis",
  portfolios: "/api/v1/portfolios",
  evalDashboard: "/api/v1/evals/dashboard",
  evalRuns: "/api/v1/evals/runs",
  evalDrafts: "/api/v1/evals/drafts",
  feedbackDashboard: "/api/v1/feedback/dashboard",
  feedback: "/api/v1/feedback",
};

// 对话卡片和组合详情共用同一套 LangGraph 阶段名称，避免两处显示口径漂移。
const portfolioResearchStepLabels = Object.freeze([
  "读取组合",
  "规则证据",
  "量化风险",
  "历史业绩",
  "市场上下文",
  "决策历史",
  "AI 综合",
  "生成报告",
]);

// 子页面路由独立于业务 API。同一工作区只显示当前选中的功能。
const workspacePages = window.FundScopeWorkspacePages;
const workspaceOutlineConfig = workspacePages.config;
const initialWorkspaceRoute = workspacePages.readRoute(window.location.hash) || {view: "portfolio", page: "holdings"};

const fallbackMarketTopics = Object.freeze([
  "半导体",
  "人工智能",
  "新能源",
  "医药",
  "消费",
  "金融",
  "军工",
  "黄金",
  "有色金属",
]);

function readStoredMarketTopics() {
  try {
    const value = JSON.parse(localStorage.getItem("fundscope-market-topics") || "[]");
    return Array.isArray(value)
      ? value.filter((item) => typeof item === "string" && item.trim()).slice(0, 3)
      : [];
  } catch (_error) {
    return [];
  }
}

const state = {
  userId: localStorage.getItem("agent-workspace-user") || "chen",
  auth: { authenticated: false, auth_required: false, user: null },
  authMode: "login",
  developerMode: window.location.pathname === "/developer",
  conversations: [],
  currentConversationId: null,
  researchReference: null,
  fundReportReference: null,
  messages: [],
  run: null,
  historicalRun: null,
  runDurationMs: null,
  workspace: null,
  knowledge: null,
  evalDashboard: null,
  evalSuiteId: "fund_agent_v2",
  evalRetrievalMode: "hybrid",
  evalDashboardRequestId: 0,
  evalReviewSaving: null,
  evalReviewDrafts: {},
  evalRunLoading: null,
  evalRunLoadError: null,
  evalProgress: "",
  evalRunning: false,
  evalError: "",
  feedbackDashboard: null,
  feedbackError: "",
  evalDrafts: [],
  evalDraftError: "",
  evalDraftEditorFeedbackId: null,
  evalDraftSavingFeedbackId: null,
  feedbackEditorMessageId: null,
  feedbackSubmittingMessageId: null,
  sending: false,
  // sending 防止重复提交；replyPending 只决定是否显示临时回答，两者结束时间不同。
  replyPending: false,
  activeStreamController: null,
  activeStreamId: null,
  agentPhase: "",
  streamedAnswer: "",
  retryMessage: "",
  retryIdempotencyKey: null,
  retryResearchRefresh: false,
  // 无深链接时从资产开始。旧版记住的市场页不再覆盖新的产品首页。
  activeView: initialWorkspaceRoute?.view || "portfolio",
  workspacePages: Object.fromEntries(Object.keys(workspaceOutlineConfig).map((view) => [
    view, workspacePages.resolvePage(view, initialWorkspaceRoute?.view === view
      ? initialWorkspaceRoute.page : localStorage.getItem(`fundscope-page-${view}`)),
  ])),
  // 分别记住每个功能的阅读位置，不能把业绩页的滚动位置用于社区页。
  workspaceScrollPositions: {},
  researchDetail: null,
  marketEventWindow: "",
  fundCatalog: null,
  funds: [],
  selectedFundCode: localStorage.getItem("fundscope-selected-fund") || null,
  fundDetail: null,
  // 详情、图表、风险必须属于同一次选择，慢响应不得覆盖后来选中的基金。
  fundSelectionRequestId: 0,
  fundChartData: null,
  fundChartLoading: false,
  fundChartError: "",
  terminalFundEvents: null,
  terminalFundEventsLoading: false,
  terminalFundEventsError: "",
  // 终端的全市场资讯单独保存，市场页的关键词/类型筛选不能改变它。
  terminalMarketEvents: null,
  terminalMarketEventsLoading: false,
  terminalMarketEventsError: "",
  terminalChatSubject: null,
  terminalChatError: "",
  // 净值图周期使用带单位的字符串，既能表达“30 天”，也能表达“3/6/12/36 个月”。
  fundChartPeriod: "36m",
  fundRisk: null,
  fundRiskMonths: 12,
  fundRiskError: "",
  fundRiskLoading: false,
  fundAssessment: null,
  fundAssessmentError: "",
  fundAssessmentLoading: false,
  // AI 语义研判需要额外调用模型，因此不随基金详情自动请求，只在用户点击后保存结果。
  fundSemanticReview: null,
  fundSemanticReviewError: "",
  fundSemanticReviewLoading: false,
  portfolios: [],
  selectedPortfolioId: null,
  portfolioDetail: null,
  portfolioAssessment: null,
  // 三个页面分别记住自己的区间，查看业绩不会改掉研究任务的参数。
  portfolioPeriods: { risk: 12, performance: 12, research: 12 },
  portfolioDraftVersion: 0,
  portfolioDraft: null,
  portfolioLoading: false,
  portfolioSaving: false,
  portfolioError: "",
  // 刷新报价不重建组合编辑草稿；状态归属当时的用户和组合。
  holdingsQuoteStatus: null,
  // 真实持仓的流水独立加载；组合列表只携带聚合后的当前持仓，避免每次列表刷新都传全量账本。
  portfolioTransactions: [],
  portfolioTradeOrders: [],
  portfolioTransactionsRequestId: 0,
  portfolioTransactionsLoading: false,
  portfolioTransactionError: "",
  portfolioAssessmentLoading: false,
  portfolioAssessmentError: "",
  portfolioAssessmentRequestId: 0,
  // 组合业绩和规则研判共用 6/12/36 月区间，但分别请求，避免公式结果被 AI 报告覆盖。
  portfolioPerformance: null,
  portfolioPerformanceLoading: false,
  portfolioPerformanceError: "",
  portfolioPerformanceRequestId: 0,
  // 决策日志保存当时快照，列表里的 outcome 会随最新净值动态更新。
  portfolioDecisions: [],
  portfolioDecisionsLoading: false,
  portfolioDecisionSaving: false,
  portfolioDecisionError: "",
  portfolioDecisionRequestId: 0,
  // 事件归因同时依赖事件语义和季报穿透，单独维护请求状态，避免任一来源刷新时闪掉组合研判。
  portfolioEventImpacts: null,
  portfolioEventImpactsLoading: false,
  portfolioEventImpactsError: "",
  portfolioEventImpactsRequestId: 0,
  // LangGraph 研究是独立后台工作流，不和页面自动加载的确定性 assessment 混用状态。
  portfolioResearchJob: null,
  portfolioResearchResult: null,
  portfolioResearchRunning: false,
  portfolioResearchMode: "standard",
  portfolioResearchDraft: {question: "", allow_public_refresh: false, investor_context: {}},
  portfolioResearchError: "",
  portfolioResearchRequestId: 0,
  portfolioReportLoading: false,
  portfolioReportReadJobId: "",
  // 历史列表来自数据库，使页面刷新后仍能恢复最近报告，并为变化审计找到上一份基线。
  portfolioResearchHistory: [],
  portfolioResearchHistoryLoading: false,
  portfolioResearchHistoryError: "",
  portfolioResearchHistoryNotice: "",
  portfolioResearchHistoryRequestId: 0,
  // 对比只在用户点击后按需计算，避免打开页面就产生额外模型调用。
  portfolioResearchComparison: null,
  portfolioResearchComparisonLoading: false,
  portfolioResearchComparisonError: "",
  // 助手消息只持久化轻量 job_id；运行中的权威状态由 SSE 放进这个内存索引。
  chatPortfolioResearchJobs: {},
  chatPortfolioResearchStreams: {},
  // 组合调整与研究报告使用不同 Job。它可以停在 waiting_input，等待用户真正确认约束。
  portfolioAdjustmentJob: null,
  portfolioAdjustmentResult: null,
  portfolioAdjustmentRunning: false,
  portfolioAdjustmentError: "",
  portfolioAdjustmentRequestId: 0,
  // 持续监控与研究 Job 分开：它保存策略、风险快照和规则预警，不会自动执行研究或交易。
  portfolioMonitoring: null,
  portfolioMonitoringJob: null,
  portfolioMonitoringLoading: false,
  portfolioMonitoringSaving: false,
  portfolioMonitoringRunning: false,
  portfolioMonitoringError: "",
  portfolioMonitoringRequestId: 0,
  agentContextMode: "fund",
  marketSentiment: null,
  retailIndices: null,
  retailIndicesLoading: false,
  marketSentimentError: "",
  marketSentimentLoading: false,
  marketSyncing: false,
  // 大资金事实与普通市场快照分表保存；切换筛选只重绘，不重复访问后端。
  largeCapital: null,
  largeCapitalError: "",
  largeCapitalLoading: false,
  largeCapitalSyncing: false,
  largeCapitalRequestId: 0,
  largeCapitalSectorPeriod: "5d",
  // 指数切换只改变当前展示项，不会重新请求接口或调用大模型。
  selectedMarketIndexCode: "000300",
  marketEvents: null,
  marketEventsError: "",
  marketEventsLoading: false,
  // 热点列表来自持久化快照；筛选只改变请求参数，不会触发新闻采集或模型调用。
  marketHotspots: null,
  marketHotspotsError: "",
  marketHotspotsLoading: false,
  marketHotspotLifecycle: "",
  marketHotspotFundOnly: false,
  marketEventSyncing: false,
  marketPipelineJob: null,
  marketEventAnalyzing: false,
  marketEventClustering: false,
  socialSignals: null,
  socialSignalsError: "",
  socialSignalsLoading: false,
  socialSignalsConnecting: false,
  socialSignalsSyncing: false,
  socialSignalsAnalyzing: false,
  socialPosts: null,
  socialPostScope: "window",
  socialPostPage: 1,
  socialPostsLoading: false,
  marketEventQuery: "",
  marketEventType: "",
  marketEventFundOnly: false,
  marketTopicCatalog: null,
  marketSelectedTopics: readStoredMarketTopics(),
  marketUseModelTopicExpansion:
    localStorage.getItem("fundscope-market-topic-model") !== "false",
  fundSearch: "",
  fundSearchResults: [],
  fundSearchLoading: false,
  fundSearchError: "",
  fundSearchRequestId: 0,
  fundImportingCode: null,
  fundLoading: false,
  fundSyncing: false,
};

const elements = {
  sidebar: document.querySelector("#sidebar"),
  conversationList: document.querySelector("#conversation-list"),
  conversationTitle: document.querySelector("#conversation-title"),
  conversationMeta: document.querySelector("#conversation-meta"),
  userId: document.querySelector("#user-id"),
  userAvatar: document.querySelector("#user-avatar"),
  userDisplay: document.querySelector("#user-display"),
  accountButton: document.querySelector("#account-button"),
  authDialog: document.querySelector("#auth-dialog"),
  authTitle: document.querySelector("#auth-title"),
  authModeSwitch: document.querySelector("#auth-mode-switch"),
  authForm: document.querySelector("#auth-form"),
  authDisplayNameField: document.querySelector("#auth-display-name-field"),
  authDisplayName: document.querySelector("#auth-display-name"),
  authUserId: document.querySelector("#auth-user-id"),
  authPassword: document.querySelector("#auth-password"),
  authError: document.querySelector("#auth-error"),
  authSubmit: document.querySelector("#auth-submit"),
  authSessionPanel: document.querySelector("#auth-session-panel"),
  authSessionAvatar: document.querySelector("#auth-session-avatar"),
  authSessionName: document.querySelector("#auth-session-name"),
  authSessionRole: document.querySelector("#auth-session-role"),
  messageList: document.querySelector("#message-list"),
  composer: document.querySelector("#composer"),
  researchRefresh: document.querySelector("#research-refresh"),
  messageInput: document.querySelector("#message-input"),
  sendButton: document.querySelector("#send-message"),
  chatError: document.querySelector("#chat-error"),
  chatErrorText: document.querySelector("#chat-error-text"),
  retryMessageButton: document.querySelector("#retry-message"),
  connectionStatus: document.querySelector("#connection-status"),
  connectionLabel: document.querySelector("#connection-label"),
  inspectorSubtitle: document.querySelector("#inspector-subtitle"),
  runPanel: document.querySelector("#run-panel"),
  workspacePanel: document.querySelector("#workspace-panel"),
  knowledgePanel: document.querySelector("#knowledge-panel"),
  evalsPanel: document.querySelector("#evals-panel"),
  documentInput: document.querySelector("#document-input"),
  toastRegion: document.querySelector("#toast-region"),
  chatShell: document.querySelector("#chat-shell"),
  fundShell: document.querySelector("#fund-shell"),
  inspector: document.querySelector("#inspector"),
  workspaceTitle: document.querySelector("#workspace-title"),
  workspaceViews: document.querySelectorAll("[data-workspace-view]"),
  fundContent: document.querySelector(".fund-content"),
  workspaceOutline: document.querySelector("#workspace-outline"),
  workspaceOutlineTitle: document.querySelector("#workspace-outline-title"),
  workspaceOutlineItems: document.querySelector("#workspace-outline-items"),
  agentSidebarContent: document.querySelector("#agent-sidebar-content"),
  fundSidebarContent: document.querySelector("#fund-sidebar-content"),
  fundSearchInput: document.querySelector("#fund-search-input"),
  fundSearchResults: document.querySelector("#fund-search-results"),
  fundNavList: document.querySelector("#fund-nav-list"),
  fundCardGrid: document.querySelector("#fund-card-grid"),
  fundDetail: document.querySelector("#fund-detail"),
  marketSentiment: document.querySelector("#market-sentiment"),
  retailIndices: document.querySelector("#retail-indices"),
  refreshRetailIndices: document.querySelector("#refresh-retail-indices"),
  largeCapitalSignals: document.querySelector("#large-capital-signals"),
  largeCapitalMeta: document.querySelector("#large-capital-meta"),
  syncLargeCapitalButton: document.querySelector("#sync-large-capital"),
  marketEvents: document.querySelector("#market-events"),
  marketEventsMeta: document.querySelector("#market-events-meta"),
  marketHotspots: document.querySelector("#market-hotspots"),
  marketHotspotsMeta: document.querySelector("#market-hotspots-meta"),
  marketHotspotLifecycle: document.querySelector("#market-hotspot-lifecycle"),
  marketHotspotFundOnly: document.querySelector("#market-hotspot-fund-only"),
  marketPipelineStatus: document.querySelector("#market-pipeline-status"),
  marketEventFilters: document.querySelector("#market-event-filters"),
  marketEventQuery: document.querySelector("#market-event-query"),
  marketEventType: document.querySelector("#market-event-type"),
  marketEventFundOnly: document.querySelector("#market-event-fund-only"),
  marketTopicTriggerLabel: document.querySelector("#market-topic-trigger-label"),
  marketTopicPopover: document.querySelector("#market-topic-popover"),
  marketTopicForm: document.querySelector("#market-topic-form"),
  marketTopicOptions: document.querySelector("#market-topic-options"),
  marketTopicCustomInput: document.querySelector("#market-topic-custom-input"),
  marketTopicUseModel: document.querySelector("#market-topic-use-model"),
  marketTopicLimit: document.querySelector("#market-topic-limit"),
  socialSignals: document.querySelector("#social-signals"),
  socialSignalsMeta: document.querySelector("#social-signals-meta"),
  connectXhsSessionButton: document.querySelector("#connect-xhs-session"),
  syncSocialSignalsButton: document.querySelector("#sync-social-signals"),
  syncMarketEventsButton: document.querySelector("#sync-market-events"),
  analyzeMarketEventsButton: document.querySelector("#analyze-market-events"),
  clusterMarketEventsButton: document.querySelector("#cluster-market-events"),
  fundHeaderMeta: document.querySelector("#fund-header-meta"),
  fundSourceMode: document.querySelector("#fund-source-mode"),
  fundSidebarRange: document.querySelector("#fund-sidebar-range"),
  fundCount: document.querySelector("#fund-count"),
  fundRecordCount: document.querySelector("#fund-record-count"),
  fundSnapshotEnd: document.querySelector("#fund-snapshot-end"),
  fundOverviewMeta: document.querySelector("#fund-overview-meta"),
  fundError: document.querySelector("#fund-error"),
  fundErrorText: document.querySelector("#fund-error-text"),
  fundConnectionStatus: document.querySelector("#fund-connection-status"),
  fundConnectionLabel: document.querySelector("#fund-connection-label"),
  syncFundDataButton: document.querySelector("#sync-fund-data"),
  agentFundContextLabel: document.querySelector("#agent-fund-context-label"),
  portfolioWorkbench: document.querySelector("#portfolio-workbench"),
};

let fundSearchTimer = null;
let researchTerminal = null;
let terminalNewsRequestId = 0;
let terminalMarketNewsRequestId = 0;
let socialSignalCooldownTimer = null;
let retailIndexChart = null;
let agentAuthorizations = null;

class ApiError extends Error {
  constructor(message, status) {
    super(message);
    this.status = status;
  }
}

window.addEventListener("DOMContentLoaded", initialize);

async function initialize() {
  applyExperienceMode();
  agentAuthorizations = window.FundScopeAgentAuthorizations.create({
    dialog: document.querySelector("#agent-authorizations-dialog"),
    trigger: document.querySelector("#open-agent-authorizations"),
    request: apiRequest, getContext: () => ({ userId: state.userId, portfolios: state.portfolios }),
    icons: refreshIcons,
  });
  initializeResearchTerminal();
  elements.userId.value = state.userId;
  updateUserAvatar();
  bindStaticEvents();
  activateProductView(state.activeView, { persist: false, load: false, history: "replace" });
  renderAgentFundContext();
  renderOverviewDashboard();
  renderMessages();
  renderRunPanel();
  if (state.developerMode) {
    renderWorkspacePanel();
    renderKnowledgePanel();
    renderEvalsPanel();
  }
  refreshIcons();

  await loadAuthState();
  if (state.auth.auth_required && !state.auth.authenticated) {
    showAuthDialog();
    await checkConnection();
    return;
  }

  const developerLoads = state.developerMode
    ? [
        loadWorkspace(),
        loadKnowledge(),
        loadEvalDashboard(),
        loadFeedbackDashboard(),
        loadEvalDrafts(),
      ]
    : [];
  const [conversationResult] = await Promise.allSettled([
    loadConversations(),
    checkConnection(),
    loadFunds(),
    loadPortfolios(),
    ...developerLoads,
  ]);
  await loadActiveWorkspacePage();
  restoreResearchDetailFromHash();

  if (conversationResult.status === "fulfilled" && state.conversations.length) {
    const remembered = localStorage.getItem(conversationStorageKey());
    const target = state.conversations.some((item) => item.id === remembered)
      ? remembered
      : state.conversations[0].id;
    await selectConversation(target, { closeMobile: false });
  }

  // 运行中的 Job 或自治会话可能在 Worker 中变化，页面可见时轻量刷新快照。
  window.setInterval(() => {
    if (!document.hidden && shouldPollWorkspace()) {
      loadWorkspace({ silent: true });
    }
  }, 5000);
}

function applyExperienceMode() {
  document.body.classList.toggle("developer-mode", state.developerMode);
  document.querySelectorAll("[data-developer-only]").forEach((element) => {
    element.hidden = !state.developerMode;
  });
  document.querySelectorAll("[data-business-only]").forEach((element) => {
    element.hidden = state.developerMode;
  });
  elements.messageInput.placeholder = state.developerMode
    ? "向 Agent 发起任务..."
    : elements.messageInput.dataset.businessPlaceholder;
  document.title = state.developerMode ? "FundScope Developer" : "FundScope";
  if (!state.developerMode) {
    document.body.classList.remove("inspector-open");
    elements.inspector.classList.add("view-hidden");
  }
}

function bindStaticEvents() {
  elements.refreshRetailIndices.addEventListener("click", () => loadRetailIndices({refresh: true}));
  elements.retailIndices.addEventListener("click", (event) => {
    if (event.target.closest("#retry-retail-indices")) loadRetailIndices();
  });
  elements.accountButton.addEventListener("click", showAuthDialog);
  document.querySelector("#close-auth-dialog").addEventListener("click", closeAuthDialog);
  elements.authModeSwitch.addEventListener("click", (event) => {
    const button = event.target.closest("[data-auth-mode]");
    if (button) setAuthMode(button.dataset.authMode);
  });
  elements.authForm.addEventListener("submit", submitAuthForm);
  document.querySelector("#logout-button").addEventListener("click", logoutCurrentUser);
  elements.authDialog.addEventListener("cancel", (event) => {
    if (state.auth.auth_required && !state.auth.authenticated) {
      event.preventDefault();
    }
  });

  document.querySelectorAll("[data-product-view]").forEach((button) => {
    button.addEventListener("click", () => activateProductView(button.dataset.productView));
  });
  // 所有页内链接共用 hash 路由。浏览器返回、刷新和打开旧链接走相同的恢复路径。
  document.querySelector("#workspace-mobile-page").addEventListener("change", event => {
    const route = workspacePages.readRoute(event.target.value);
    if (route) activateProductView(route.view, {page: route.page});
  });
  document.querySelector("#market-context-fund").addEventListener("change", event => {
    selectFund(event.target.value, {closeMobile: false});
  });
  // pushState 不会触发 hashchange；后退、前进和手动修改地址会进入这里。
  window.addEventListener("hashchange", () => {
    const route = workspacePages.readRoute(window.location.hash);
    activateProductView(route?.view || "portfolio", { page: route?.page, history: "replace" });
    restoreResearchDetailFromHash();
  });
  bindResearchWorkspaceActions();
  document.querySelectorAll("[data-open-workspace-view]").forEach((button) => {
    button.addEventListener("click", () => {
      activateProductView(button.dataset.openWorkspaceView);
    });
  });

  elements.fundSearchInput.addEventListener("input", (event) => {
    state.fundSearch = event.target.value.trim();
    renderFundNavigation();
    renderFundCards();
    scheduleFundDirectorySearch();
  });
  elements.fundSearchInput.addEventListener("keydown", (event) => {
    if (event.key === "Escape") clearFundDirectorySearch();
  });
  elements.fundSearchResults.addEventListener("click", (event) => {
    const button = event.target.closest("[data-directory-fund]");
    if (!button) return;
    const fundCode = button.dataset.directoryFund;
    const result = state.fundSearchResults.find(
      (item) => item.fund_code === fundCode,
    );
    if (result?.is_imported) {
      clearFundDirectorySearch();
      selectFund(fundCode);
    } else {
      importFundFromDirectory(fundCode);
    }
  });
  elements.marketEventFilters.addEventListener("submit", (event) => {
    event.preventDefault();
    state.marketEventQuery = elements.marketEventQuery.value.trim();
    loadMarketEvents();
  });
  elements.marketEventType.addEventListener("change", (event) => {
    state.marketEventType = event.target.value;
    loadMarketEvents();
  });
  elements.marketEventFundOnly.addEventListener("change", (event) => {
    state.marketEventFundOnly = event.target.checked;
    loadMarketEvents();
  });
  elements.marketHotspotLifecycle.addEventListener("change", (event) => {
    state.marketHotspotLifecycle = event.target.value;
    loadMarketHotspots();
  });
  elements.marketHotspotFundOnly.addEventListener("change", (event) => {
    state.marketHotspotFundOnly = event.target.checked;
    loadMarketHotspots();
  });
  document.querySelector("#refresh-market-hotspots").addEventListener("click", () => {
    loadMarketHotspots({ showFeedback: true });
  });
  elements.marketTopicForm.addEventListener("submit", saveMarketTopicConfiguration);
  document.querySelector("#clear-market-topics").addEventListener("click", () => {
    applyMarketTopicConfiguration([], elements.marketTopicUseModel.checked);
    elements.marketTopicPopover.hidePopover();
    showToast("已切换为全市场采集");
  });
  document.querySelector("#refresh-market-events").addEventListener("click", () => {
    state.marketEventQuery = elements.marketEventQuery.value.trim();
    loadMarketEvents({ showFeedback: true });
  });
  elements.syncMarketEventsButton.addEventListener("click", syncMarketEvents);
  elements.syncLargeCapitalButton.addEventListener("click", syncLargeCapitalSignals);
  elements.connectXhsSessionButton.addEventListener("click", connectXhsSession);
  elements.syncSocialSignalsButton.addEventListener("click", syncSocialSignals);
  document.querySelector("#analyze-social-signals").addEventListener("click", analyzeSavedSocialSignals);
  bindSocialEvidenceActions();
  document.querySelector("#social-posts").addEventListener("change", (event) => {
    if (event.target.id === "social-post-scope") {
      state.socialPostScope = event.target.value;
      state.socialPostPage = 1;
      loadSocialPosts();
    }
  });
  elements.analyzeMarketEventsButton.addEventListener("click", analyzeMarketEvents);
  elements.clusterMarketEventsButton.addEventListener("click", clusterMarketEvents);
  elements.marketSentiment.addEventListener("click", (event) => {
    if (event.target.closest("[data-sync-market]")) syncMarketData();
    const indexButton = event.target.closest("[data-market-index-code]");
    if (indexButton) {
      state.selectedMarketIndexCode = indexButton.dataset.marketIndexCode;
      renderMarketSentiment();
    }
  });
  elements.marketSentiment.addEventListener("keydown", (event) => {
    const indexButton = event.target.closest("[data-market-index-code]");
    if (!indexButton || !["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) {
      return;
    }
    const buttons = [...elements.marketSentiment.querySelectorAll("[data-market-index-code]")];
    const currentIndex = buttons.indexOf(indexButton);
    const nextIndex = event.key === "Home"
      ? 0
      : event.key === "End"
        ? buttons.length - 1
        : (currentIndex + (event.key === "ArrowRight" ? 1 : -1) + buttons.length) % buttons.length;
    event.preventDefault();
    state.selectedMarketIndexCode = buttons[nextIndex].dataset.marketIndexCode;
    renderMarketSentiment();
    elements.marketSentiment
      .querySelector(`[data-market-index-code="${state.selectedMarketIndexCode}"]`)
      ?.focus();
  });
  elements.largeCapitalSignals.addEventListener("click", (event) => {
    const period = event.target.closest("[data-capital-sector-period]");
    if (!period || !["1d", "5d", "20d"].includes(period.dataset.capitalSectorPeriod)) return;
    // 窗口切换只读取已返回的统计结果，不同步外部数据，也不调用大模型。
    state.largeCapitalSectorPeriod = period.dataset.capitalSectorPeriod;
    renderLargeCapitalSignals();
    elements.largeCapitalSignals
      .querySelector(`[data-capital-sector-period="${state.largeCapitalSectorPeriod}"]`)
      ?.focus({ preventScroll: true });
  });
  elements.fundNavList.addEventListener("click", (event) => {
    const button = event.target.closest("[data-fund-code]");
    if (button) {
      // 单基金工作区记住当前子页，换基金后仍可继续查看持仓分析。
      activateProductView("overview");
      selectFund(button.dataset.fundCode);
    }
  });
  elements.fundCardGrid.addEventListener("click", (event) => {
    const button = event.target.closest("[data-fund-code]");
    if (button) selectFund(button.dataset.fundCode);
  });
  elements.fundDetail.addEventListener("click", (event) => {
    const analyzeButton = event.target.closest("[data-analyze-fund]");
    if (analyzeButton) {
      openSelectedFundInAgent();
      return;
    }
    const semanticReviewButton = event.target.closest("[data-run-semantic-review]");
    if (semanticReviewButton) {
      runFundSemanticReview();
      return;
    }
    const riskPeriodButton = event.target.closest("[data-fund-risk-period]");
    if (riskPeriodButton) {
      loadFundRisk(Number(riskPeriodButton.dataset.fundRiskPeriod));
      return;
    }
    const button = event.target.closest("[data-fund-period]");
    if (!button) return;
    state.fundChartPeriod = button.dataset.fundPeriod;
    renderFundDetail();
  });
  // 组合区域由 JS 动态渲染，因此使用事件委托统一处理表单和按钮。
  elements.portfolioWorkbench.addEventListener("submit", (event) => {
    if (event.target.matches("[data-portfolio-form]")) {
      savePortfolio(event);
      return;
    }
    if (event.target.matches("[data-portfolio-rename-form]")) {
      renameSelectedPortfolio(event);
      return;
    }
    if (event.target.matches("[data-bookkeeping-form]")) {
      window.FundScopeBookkeeping.submit(event, bookkeepingContext());
      return;
    }
    if (event.target.matches("[data-portfolio-adjustment-form]")) {
      submitPortfolioAdjustmentReview(event);
      return;
    }
    if (event.target.matches("[data-portfolio-monitoring-form]")) {
      savePortfolioMonitoringPolicy(event);
      return;
    }
    if (event.target.matches("[data-portfolio-decision-form]")) {
      createPortfolioDecision(event);
      return;
    }
    if (event.target.matches("[data-decision-review-form]")) {
      reviewPortfolioDecision(event);
    }
  });
  elements.portfolioWorkbench.addEventListener("input", (event) => {
    if (event.target.matches("[data-research-question]")) state.portfolioResearchDraft.question = event.target.value;
    if (event.target.matches("[data-research-personal]")) state.portfolioResearchDraft.investor_context[event.target.dataset.researchPersonal] = event.target.value;
    if (event.target.closest(".bk-workspace")) window.FundScopeBookkeeping.capture(event, bookkeepingContext());
    if (event.target.matches(
      "[data-portfolio-name], [data-position-units], [data-position-cost], [data-position-date]",
    )) {
      updatePortfolioDraftFromForm(event.target.closest("[data-portfolio-form]"));
    }
  });
  elements.portfolioWorkbench.addEventListener("change", (event) => {
    if (event.target.matches("[data-research-refresh]")) { state.portfolioResearchDraft.allow_public_refresh = event.target.checked; return; }
    if (event.target.closest(".bk-workspace")) {
      window.FundScopeBookkeeping.change(event, bookkeepingContext()); return;
    }
    if (event.target.matches("[data-portfolio-research-mode]")) {
      if (!state.portfolioResearchRunning) {
        state.portfolioResearchMode = window.FundScopeResearchCommittee.normalizeMode(event.target.value);
      }
      return;
    }
    if (event.target.matches("[data-briefing-enabled]")) {
      getDailyBriefings().configure(event.target.checked); return;
    }
    if (event.target.matches("[data-portfolio-select]")) {
      selectPortfolio(event.target.value);
      return;
    }
  });
  elements.portfolioWorkbench.addEventListener("click", (event) => {
    if (event.target.closest("[data-bk-action]")) {
      window.FundScopeBookkeeping.click(event, bookkeepingContext()); return;
    }
    if (event.target.closest("[data-briefing-refresh]")) { getDailyBriefings().load(); return; }
    if (event.target.closest("[data-briefing-run]")) { getDailyBriefings().run(); return; }
    if (event.target.closest("[data-review-refresh]")) { getPortfolioReview().load({force:true}); return; }
    const reviewPeriod = event.target.closest("[data-review-months]");
    if (reviewPeriod) { getPortfolioReview().setMonths(Number(reviewPeriod.dataset.reviewMonths)); return; }
    if (event.target.closest("[data-review-research]")) {
      state.portfolioPeriods.research = getPortfolioReview().months(); runPortfolioResearch(); return;
    }
    const reviewReport = event.target.closest("[data-review-report]");
    if (reviewReport) { openReviewedReport(reviewReport.dataset.reviewReport); return; }
    const diagnosisQuestion = event.target.closest("[data-diagnosis-question]");
    if (diagnosisQuestion) {
      const question = state.portfolioResearchResult?.diagnosis?.questions[Number(diagnosisQuestion.dataset.diagnosisQuestion)];
      if (question) followPortfolioReport(question);
      return;
    }
    if (event.target.closest("[data-follow-report]")) { followPortfolioReport(); return; }
    const pageButton = event.target.closest("[data-open-portfolio-page]");
    if (pageButton) {
      activateProductView("portfolio", { page: pageButton.dataset.openPortfolioPage });
      return;
    }
    if (event.target.closest("[data-sync-holdings]")) {
      refreshHoldingsQuotes({sync: true});
      return;
    }
    if (event.target.closest("[data-reload-holdings]")) {
      refreshHoldingsQuotes();
      return;
    }
    if (event.target.closest("[data-new-portfolio]")) {
      startNewPortfolioDraft();
      activateProductView("portfolio", {page: "holdings"});
      return;
    }
    if (event.target.closest("[data-add-draft-fund]")) {
      addFundToPortfolioDraft();
      return;
    }
    const removeDraftFundButton = event.target.closest("[data-remove-draft-fund]");
    if (removeDraftFundButton) {
      removeFundFromPortfolioDraft(removeDraftFundButton.dataset.removeDraftFund);
      return;
    }
    if (event.target.closest("[data-delete-portfolio]")) {
      deleteSelectedPortfolio();
      return;
    }
    if (event.target.closest("[data-analyze-portfolio]")) {
      openSelectedPortfolioInAgent();
      return;
    }
    if (event.target.closest("[data-reload-portfolio-event-impacts]")) {
      loadPortfolioEventImpacts();
      return;
    }
    if (event.target.closest("[data-run-portfolio-research]")) {
      runPortfolioResearch({researchMode: state.portfolioResearchMode});
      return;
    }
    if (event.target.closest("[data-compare-portfolio-research]")) {
      comparePortfolioResearch();
      return;
    }
    if (event.target.closest("[data-run-portfolio-monitor]")) {
      runPortfolioMonitor();
      return;
    }
    if (event.target.closest("[data-reload-portfolio-monitoring]")) {
      loadPortfolioMonitoring();
      return;
    }
    if (event.target.closest("[data-monitor-research]")) {
      runPortfolioResearch();
      return;
    }
    const acknowledgeButton = event.target.closest("[data-acknowledge-alert]");
    if (acknowledgeButton) {
      acknowledgePortfolioAlert(acknowledgeButton.dataset.acknowledgeAlert);
      return;
    }
    if (event.target.closest("[data-start-portfolio-adjustment]")) {
      startPortfolioAdjustment();
      return;
    }
    if (event.target.closest("[data-reload-portfolio-performance]")) {
      loadPortfolioPerformance();
      return;
    }
    if (event.target.closest("[data-reload-portfolio-decisions]")) {
      loadPortfolioDecisions();
      return;
    }
    const deleteDecisionButton = event.target.closest("[data-delete-decision]");
    if (deleteDecisionButton) {
      deletePortfolioDecision(deleteDecisionButton.dataset.deleteDecision);
      return;
    }
    const periodButton = event.target.closest("[data-portfolio-period]");
    if (periodButton) {
      const page = state.workspacePages.portfolio;
      if (!(page in state.portfolioPeriods)) return;
      if (page === "research") resetPortfolioResearch();
      state.portfolioPeriods[page] = Number(periodButton.dataset.portfolioPeriod);
      loadPortfolioPage(page, {refresh: true});
    }
  });
  document
    .querySelector("#refresh-funds")
    .addEventListener("click", refreshFundWorkspace);
  document
    .querySelector("#refresh-fund-sidebar")
    .addEventListener("click", refreshFundWorkspace);
  elements.syncFundDataButton.addEventListener("click", syncFundData);
  document.querySelector("#open-fund-sidebar").addEventListener("click", openSidebar);

  document.querySelector("#new-chat").addEventListener("click", () => { startNewConversation(); activateProductView("agent"); });
  document
    .querySelector("#refresh-conversations")
    .addEventListener("click", () => loadConversations({ showFeedback: true }));
  document.querySelector("#dismiss-error").addEventListener("click", hideChatError);
  elements.retryMessageButton.addEventListener("click", retryLastMessage);

  elements.composer.addEventListener("submit", sendMessage);
  elements.sendButton.addEventListener("click", (event) => {
    if (!state.sending) return;
    event.preventDefault();
    stopActiveAgentRun();
  });
  elements.messageInput.addEventListener("input", resizeComposer);
  elements.messageInput.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      elements.composer.requestSubmit();
    }
  });

  elements.userId.addEventListener("change", switchUserWorkspace);
  elements.userId.addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      elements.userId.blur();
    }
  });

  elements.conversationList.addEventListener("click", async (event) => {
    const selectButton = event.target.closest("[data-conversation-id]");
    const deleteButton = event.target.closest("[data-delete-conversation]");

    if (deleteButton) {
      event.stopPropagation();
      await deleteConversation(deleteButton.dataset.deleteConversation);
      return;
    }
    if (selectButton) {
      activateProductView("agent");
      await selectConversation(selectButton.dataset.conversationId);
    }
  });

  document.querySelectorAll("[data-tab]").forEach((button) => {
    button.addEventListener("click", () => activateInspectorTab(button.dataset.tab));
  });

  document.querySelector("#attach-document").addEventListener("click", () => {
    activateInspectorTab("knowledge");
    elements.documentInput.click();
  });
  elements.documentInput.addEventListener("change", uploadSelectedDocument);

  document.querySelector("#open-sidebar").addEventListener("click", openSidebar);
  document.querySelector("#close-sidebar").addEventListener("click", closePanels);
  document.querySelector("#open-inspector").addEventListener("click", () => {
    closePanels();
    document.body.classList.add("inspector-open");
    document.body.classList.remove("sidebar-open");
  });
  document.querySelector("#close-inspector").addEventListener("click", closePanels);
  document.querySelector("#page-overlay").addEventListener("click", closePanels);
  document.addEventListener("keydown", event => {
    if (event.key === "Escape" && document.body.classList.contains("sidebar-open")) closePanels();
  });
  window.addEventListener("resize", () => {
    if (!window.matchMedia("(max-width: 1080px)").matches) closePanels();
  });
}

async function apiRequest(url, options = {}) {
  const response = await fetch(url, options);
  const contentType = response.headers.get("content-type") || "";
  const data = contentType.includes("application/json")
    ? await response.json()
    : await response.text();

  if (!response.ok) {
    let detail = data?.detail || data || `请求失败 (${response.status})`;
    if (Array.isArray(detail)) {
      detail = detail.map((item) => item.msg || JSON.stringify(item)).join("；");
    }
    if (response.status === 401) showAuthDialog();
    throw new ApiError(String(detail), response.status);
  }
  return data;
}

async function loadAuthState() {
  try {
    state.auth = await apiRequest(endpoints.authMe);
    if (state.auth.authenticated && state.auth.user) {
      if (state.userId !== state.auth.user.user_id) agentAuthorizations?.reset();
      if (state.userId !== state.auth.user.user_id) resetReportEvalViewer();
      state.userId = state.auth.user.user_id;
      localStorage.setItem("agent-workspace-user", state.userId);
      elements.userId.value = state.userId;
      elements.userId.disabled = true;
    } else {
      elements.userId.disabled = false;
    }
    updateUserAvatar();
    renderAuthDialog();
    return state.auth;
  } catch (error) {
    // 认证状态接口和业务 API 同源；失败时保留本地模式，由连接状态统一提示。
    state.auth = { authenticated: false, auth_required: false, user: null };
    return state.auth;
  }
}

function showAuthDialog() {
  renderAuthDialog();
  if (!elements.authDialog.open) elements.authDialog.showModal();
  refreshIcons();
}

function closeAuthDialog() {
  if (state.auth.auth_required && !state.auth.authenticated) return;
  elements.authDialog.close();
}

function setAuthMode(mode) {
  state.authMode = mode === "register" ? "register" : "login";
  elements.authError.hidden = true;
  renderAuthDialog();
}

function renderAuthDialog() {
  const authenticated = state.auth.authenticated && state.auth.user;
  elements.authModeSwitch.hidden = Boolean(authenticated);
  elements.authForm.hidden = Boolean(authenticated);
  elements.authSessionPanel.hidden = !authenticated;
  document.querySelector("#close-auth-dialog").hidden =
    state.auth.auth_required && !authenticated;

  if (authenticated) {
    const user = state.auth.user;
    elements.authTitle.textContent = "当前账户";
    elements.authSessionName.textContent = user.display_name;
    elements.authSessionRole.textContent =
      user.role === "admin" ? `${user.user_id} · 管理员` : `${user.user_id} · 用户`;
    elements.authSessionAvatar.textContent = user.user_id.slice(0, 2).toUpperCase();
    return;
  }

  const registering = state.authMode === "register";
  elements.authTitle.textContent = registering ? "创建研究工作区" : "登录研究工作区";
  elements.authDisplayNameField.hidden = !registering;
  elements.authDisplayName.required = registering;
  elements.authPassword.autocomplete = registering ? "new-password" : "current-password";
  elements.authSubmit.textContent = registering ? "创建并登录" : "登录";
  elements.authModeSwitch.querySelectorAll("[data-auth-mode]").forEach((button) => {
    const active = button.dataset.authMode === state.authMode;
    button.classList.toggle("active", active);
    button.setAttribute("aria-selected", String(active));
  });
}

async function submitAuthForm(event) {
  event.preventDefault();
  const registering = state.authMode === "register";
  const payload = {
    user_id: elements.authUserId.value.trim(),
    password: elements.authPassword.value,
  };
  if (registering) payload.display_name = elements.authDisplayName.value.trim();

  elements.authSubmit.disabled = true;
  elements.authError.hidden = true;
  try {
    const endpoint = registering ? endpoints.authRegister : endpoints.authLogin;
    state.auth = await apiRequest(endpoint, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    localStorage.setItem("agent-workspace-user", state.auth.user.user_id);
    window.location.reload();
  } catch (error) {
    elements.authError.textContent = error.message;
    elements.authError.hidden = false;
  } finally {
    elements.authSubmit.disabled = false;
  }
}

async function logoutCurrentUser() {
  try {
    await apiRequest(endpoints.authLogout, { method: "POST" });
    localStorage.removeItem("agent-workspace-user");
    window.location.reload();
  } catch (error) {
    elements.authError.textContent = error.message;
    elements.authError.hidden = false;
  }
}

async function checkConnection() {
  try {
    const health = await apiRequest(endpoints.health);
    const title = state.developerMode
      ? `${health.app_name} · ${health.agent_model || "模型未配置"}`
      : "FundScope 数据与模型服务正常";
    setConnectionState("online", "服务正常", title);
  } catch (error) {
    setConnectionState("offline", "连接失败", error.message);
  }
}

function setConnectionState(status, label, title) {
  [elements.connectionStatus, elements.fundConnectionStatus].forEach((element) => {
    element.className = `connection-status ${status}`;
    element.title = title;
  });
  elements.connectionLabel.textContent = label;
  elements.fundConnectionLabel.textContent = label;
}

function activateProductView(view, { page = null, persist = true, load = true, history = "push" } = {}) {
  // 产品入口只映射到一个现有页面；不复制数据状态、不挂载第二份报告。
  const destination = workspacePages.destinations[view];
  if (destination) {
    const target = workspacePages.destinationRoute(view, page);
    view = target.view;
    page = target.page;
  }
  // product5 以前把整个研究页叫 funds，这里保留一次无感迁移。
  const migratedView = view === "funds" ? "overview" : view;
  const nextView = ["overview", "portfolio", "market", "agent"].includes(migratedView)
    ? migratedView
    : "portfolio";
  const isAgentView = nextView === "agent";
  const previousView = state.activeView;
  const previousPage = state.workspacePages[previousView];
  const nextPage = workspacePages.resolvePage(nextView, page || state.workspacePages[nextView]);
  const surface = workspacePages.describe(nextView, nextPage);
  if (elements.fundContent && !state.researchDetail) state.workspaceScrollPositions[`${previousView}/${previousPage || ""}`] = elements.fundContent.scrollTop;
  if (nextPage) state.workspacePages[nextView] = nextPage;
  state.activeView = nextView;
  document.body.dataset.productDestination = surface.destination;
  state.researchDetail = null;
  document.querySelector("#research-detail-page").hidden = true;
  document.body.dataset.activeWorkspace = nextView;
  researchTerminal?.update();

  // Agent 使用自己的消息流；其余三个研究页共用同一个数据工作台外壳。
  elements.chatShell.classList.toggle("view-hidden", !isAgentView);
  elements.inspector.classList.toggle(
    "view-hidden",
    !isAgentView || !state.developerMode,
  );
  elements.agentSidebarContent.classList.toggle("view-hidden", surface.destination !== "research");
  elements.fundShell.classList.toggle("view-hidden", isAgentView);
  elements.fundSidebarContent.classList.toggle("view-hidden", surface.destination !== "assets");
  elements.workspaceViews.forEach((section) => {
    section.classList.toggle("view-hidden", section.dataset.workspaceView !== nextView);
  });
  document.querySelectorAll("[data-product-view]").forEach((button) => {
    const active = button.dataset.productView === surface.destination;
    button.classList.toggle("active", active);
    if (active) button.setAttribute("aria-current", "page");
    else button.removeAttribute("aria-current");
  });

  // 标题随工作区变化，帮助用户随时确认当前任务上下文。
  elements.workspaceTitle.textContent = workspacePages.destinations[surface.destination].title;
  elements.fundHeaderMeta.textContent = surface.label;
  renderWorkspaceOutline();
  applyWorkspacePages();
  document.querySelector("#sync-fund-data").hidden = nextView !== "overview";
  document.querySelector("#refresh-funds").title = "重新读取当前页面数据";
  document.querySelector("#refresh-funds").setAttribute("aria-label", "重新读取当前页面数据");
  renderMarketFundContext();
  if (nextView === "overview" && state.fundCatalog) {
    renderFundOverview();
  }

  if (!isAgentView && elements.fundContent) {
    const rememberedTop = persist
      ? state.workspaceScrollPositions[`${nextView}/${nextPage || ""}`] || 0
      : 0;
    window.requestAnimationFrame(() => {
      elements.fundContent.scrollTop = rememberedTop;
    });
  }

  closePanels();
  if (persist) localStorage.setItem("fundscope-product-view", nextView);
  if (persist && nextPage) localStorage.setItem(`fundscope-page-${nextView}`, nextPage);
  const hash = workspacePages.routeHash(nextView, nextPage);
  const detailSuffix = history === "replace" ? window.location.hash.split("?")[1] : null;
  const targetHash = hash + (detailSuffix ? `?${detailSuffix}` : "");
  if (window.location.hash !== targetHash) {
    window.history[history === "replace" ? "replaceState" : "pushState"](null, "", targetHash);
  }
  if (load && !isAgentView) {
    if (!state.fundCatalog && !state.fundLoading) loadFunds();
    Promise.resolve(loadActiveWorkspacePage()).then(restoreResearchDetailFromHash);
  }
}

function renderWorkspaceOutline() {
  const surface = workspacePages.describe(state.activeView, state.workspacePages[state.activeView]);
  const config = workspacePages.destinations[surface.destination];
  const picker = document.querySelector("#workspace-mobile-picker");
  const select = document.querySelector("#workspace-mobile-page");
  elements.workspaceOutline.hidden = false;
  picker.hidden = true;
  elements.workspaceOutlineTitle.textContent = config.title;
  const links = config.pages.map(([path, label]) => `<a href="#${surface.destination}/${path}" ${path === surface.path ? 'aria-current="page"' : ""}>${escapeHtml(label)}</a>`).join("");
  const back = !surface.primary && state.activeView !== "overview"
    ? `<a class="product-back-link" href="#${surface.destination}/${surface.destination === "assets" && !["transactions", "configuration"].includes(surface.path) ? "review" : config.pages[0][0]}"><i data-lucide="arrow-left"></i>返回</a><span class="product-current-tool">${escapeHtml(surface.label)}</span>` : "";
  elements.workspaceOutlineItems.innerHTML = back || links;
  document.querySelector("#research-navigation").innerHTML = workspacePages.destinations.research.pages.map(([path, label]) => `<a href="#research/${path}" ${state.activeView === "agent" && path === "chat" ? 'aria-current="page"' : ""}>${label}</a>`).join("");
  select.innerHTML = config.pages.map(([path, label]) => `<option value="#${surface.destination}/${path}">${label}</option>`).join("");
  refreshIcons();
}

function applyWorkspacePages() {
  const holdingsActive = state.workspacePages.overview === "holdings";
  const reportsActive = state.workspacePages.overview === "reports";
  document.querySelector("#overview-dashboard").hidden = holdingsActive || reportsActive;
  document.querySelector("#fund-report").hidden = holdingsActive || reportsActive;
  document.querySelector("#fund-holdings-page").hidden = !holdingsActive;
  document.querySelector("#fund-reports-page").hidden = !reportsActive;
  if (state.activeView !== "overview" || !reportsActive) fundReportsPage?.pause();
  // 市场控件常驻以保留筛选；组合页由独立 host 按需挂载，不再遍历重建所有表单。
  document.querySelectorAll("#workspace-market [data-workspace-page]").forEach(panel => {
    panel.hidden = panel.dataset.workspacePage !== state.workspacePages.market;
  });
  if (state.activeView === "portfolio") renderPortfolioWorkbench({pages: []});
}

function renderMarketFundContext() {
  const context = document.querySelector("#market-fund-context");
  const page = state.workspacePages.market;
  context.hidden = state.activeView !== "market" || !(page === "capital" || page === "news" && state.marketEventFundOnly || page === "hotspots" && state.marketHotspotFundOnly);
  const select = document.querySelector("#market-context-fund");
  const html = state.funds.map(fund => `<option value="${escapeAttribute(fund.fund_code)}">${escapeHtml(fund.short_name)} · ${fund.fund_code}</option>`).join("");
  if (select.innerHTML !== html) select.innerHTML = html;
  select.value = state.selectedFundCode || "";
  select.disabled = !state.funds.length;
}

function loadActiveMarketFundLinks() {
  // 更换市场关联基金只更新正在查看的模块，不把用户带回终端或刷新无关页。
  if (state.activeView !== "market") return;
  const page = state.workspacePages.market;
  if (page === "capital") loadLargeCapitalSignals();
  if (page === "news" && state.marketEventFundOnly) loadMarketEvents();
  if (page === "hotspots" && state.marketHotspotFundOnly) loadMarketHotspots();
}

function loadTerminalEvidenceTab(tab) {
  // 终端内的局部标签也拥有自己的只读入口，不能依赖用户碰巧访问过市场页。
  const loaders = {capital: loadLargeCapitalSignals, hotspots: loadMarketHotspots, community: loadSocialSignals};
  return loaders[tab]?.() || Promise.resolve();
}

// 页面只声明自己的只读加载器。切换页面不会运行采集、研究或求解任务。
const portfolioPageReads = new Map();
function loadPortfolioPage(page = state.workspacePages.portfolio, {refresh = false} = {}) {
  if (!state.selectedPortfolioId || state.portfolioLoading) return Promise.resolve();
  if (page === "review") return getPortfolioReview().load({force:true});
  if (page === "briefings") return getDailyBriefings().load();
  const key = `${state.userId}:${state.selectedPortfolioId}:${state.portfolioPeriods[page === "holdings" ? "performance" : page] || ""}`;
  const previous = portfolioPageReads.get(page);
  if (!refresh && previous?.key === key) return previous.promise;
  const loaders = {
    holdings: () => state.portfolioDetail?.position_mode === "actual" ? loadPortfolioPerformance() : Promise.resolve(),
    transactions: () => state.portfolioDetail?.position_mode === "actual" ? loadPortfolioTransactions() : Promise.resolve(),
    risk: loadPortfolioAssessment, performance: loadPortfolioPerformance,
    events: loadPortfolioEventImpacts,
    monitoring: loadPortfolioMonitoring, research: loadPortfolioResearchHistory,
    decisions: loadPortfolioDecisions,
  };
  if (!loaders[page]) return Promise.resolve();
  const errors = {holdings: "portfolioPerformanceError", transactions: "portfolioTransactionError", risk: "portfolioAssessmentError", performance: "portfolioPerformanceError",
    events: "portfolioEventImpactsError", monitoring: "portfolioMonitoringError",
    research: "portfolioResearchHistoryError", decisions: "portfolioDecisionError"};
  const read = {key};
  // 返回原页面沿用 DOM 和已读快照；读取失败允许下次进入重试，手动刷新可强制重读。
  read.promise = Promise.resolve().then(loaders[page]).finally(() => {
    if (portfolioPageReads.get(page) === read && state[errors[page]]) portfolioPageReads.delete(page);
  });
  portfolioPageReads.set(page, read);
  return read.promise;
}

function loadActiveWorkspacePage() {
  if (state.activeView === "overview" && state.workspacePages.overview === "reports") return loadFundReports();
  if (state.activeView === "overview" && state.workspacePages.overview === "holdings") return loadFundHoldings();
  if (state.activeView === "portfolio") return loadPortfolioPage();
  if (state.activeView === "market") {
    const loaders = {
      radar: loadSectorRadar,
      status: loadMarketEnvironment, capital: loadLargeCapitalSignals,
      news: () => Promise.all([loadMarketEvents(), loadMarketTopicCatalog()]),
      hotspots: loadMarketHotspots, retail: loadRetailIndices, community: loadSocialSignals,
      "social-posts": loadSocialPosts, "social-review": loadSocialSignals, "social-settings": loadSocialSignals,
    };
    return loaders[state.workspacePages.market]?.() || Promise.resolve();
  }
  if (state.activeView === "overview") return Promise.resolve();
  return Promise.resolve();
}

// 五个工作区复用现有 API；跨页面研究流程独立维护，避免继续扩大导航函数。
let researchWorkspaces = null;
function getResearchWorkspaces() {
  researchWorkspaces ||= window.FundScopeResearchWorkspaces.create({
    getState: () => state, request: apiRequest, endpoints, icons: refreshIcons,
    lists: window.FundScopeResearchLists, labels: fallbackMarketTopics, renderHotspot: renderMarketHotspotItem,
    openFund: code => { activateProductView("overview", {page: "terminal"}); selectFund(code); },
    ask: (question, mode) => {
      if (state.sending) { showToast("请先等待当前回答完成", "error"); return; }
      startNewConversation();
      state.agentContextMode = state.selectedPortfolioId ? "portfolio" : mode;
      activateProductView("agent");
      renderAgentFundContext();
      elements.messageInput.value = question;
      resizeComposer(); elements.messageInput.focus();
    },
  });
  return researchWorkspaces;
}
function bindResearchWorkspaceActions() {
  getResearchWorkspaces().bind();
  document.querySelector("#market-event-window").addEventListener("change", event => {
    state.marketEventWindow = event.target.value; renderMarketEvents();
  });
  document.querySelector("#research-subject").addEventListener("change", async event => {
    const value = event.target.value;
    if (state.sending) { renderAgentFundContext(); showToast("请先等待当前回答完成", "error"); return; }
    const [mode, code] = value.split(":");
    const draft = elements.messageInput.value;
    // 换研究对象时新建对话，但保留尚未发送的问题，不混入上一对象的历史。
    startNewConversation();
    elements.messageInput.value = draft;
    state.agentContextMode = mode;
    if (mode === "portfolio") await selectPortfolio(code);
    if (mode === "fund") await selectFund(code, {closeMobile: false});
    renderAgentFundContext();
    resizeComposer();
  });
}
function loadSectorRadar() { return getResearchWorkspaces().loadRadar(); }
function restoreResearchDetailFromHash() { researchWorkspaces?.restore(); }

function bindSocialEvidenceActions() {
  // 原帖入口现在分布在话题详情、样本页和 AI 引用中，委托给共同的内容区域。
  // 对话框也在该区域内，关闭和分页不再依赖已经拆掉的旧父面板。
  document.querySelector(".fund-content").addEventListener("click", event => {
    const post = event.target.closest("[data-social-post]");
    if (post) openSocialPost(post.dataset.socialPost);
    const pager = event.target.closest("[data-social-page]");
    if (pager) { state.socialPostPage = Number(pager.dataset.socialPage); loadSocialPosts(); }
    if (event.target.closest("[data-social-close]")) document.querySelector("#social-post-detail").close();
  });
}

// 单基金披露页只暴露 show 方法，不把其请求/表格状态塞进组合或终端状态。
let fundReportsPage = null;
function loadFundReports(options = {}) {
  if (!fundReportsPage) fundReportsPage = window.FundScopeFundReports.create({
    element: document.querySelector("#fund-reports-page"), request: apiRequest,
    userId: () => state.userId, waitJob: waitForBackgroundJob, icons: refreshIcons,
    ask: (fund, comparison) => {
      if (state.sending) { showToast("请先等待当前回答完成", "error"); return; }
      startNewConversation();
      state.fundReportReference = {fund_code: fund.fund_code,
        document_ids: comparison.documents.map(doc => doc.id), periods: comparison.documents.map(doc => doc.period)};
      state.agentContextMode = "fund";
      activateProductView("agent"); renderAgentFundContext(); renderMessages();
      elements.messageInput.value = `请对比 ${fund.short_name}（${fund.fund_code}）的 ${comparison.documents[0].period} 和 ${comparison.documents[1].period} 季报：资产配置变化、基金经理观点变化、主要风险。引用原文页码，区分事实与推断，不重新采集。`;
      resizeComposer(); elements.messageInput.focus();
    },
  });
  return fundReportsPage.show(state.funds.find(f => f.fund_code === state.selectedFundCode) || null, options);
}

let fundHoldingsPage = null;
function loadFundHoldings({refresh = false} = {}) {
  if (!fundHoldingsPage) {
    fundHoldingsPage = window.FundScopeFundHoldings.create({
      element: document.querySelector("#fund-holdings-page"),
      request: code => apiRequest(`${endpoints.funds}/${encodeURIComponent(code)}/holdings-analysis`),
      icons: refreshIcons,
      ask: (fund, analysis) => {
        if (state.sending) { showToast("请先等待当前回答完成", "error"); return; }
        // 使用现有助手，不新增第二份模型回答或隐式发送。用户可修改问题再发送。
        startNewConversation();
        state.agentContextMode = "fund";
        renderAgentFundContext();
        activateProductView("agent");
        elements.messageInput.value = `请分析单只基金 ${fund.short_name}（${fund.fund_code}）的已披露持仓、行业集中度和可比变化，再结合已有市场事件解释影响。页面披露截至 ${analysis.disclosure.latest_report_date || "未提供"}，若工具数据变化请先说明。区分披露事实与推断，不重新采集。`;
        resizeComposer();
        elements.messageInput.focus();
      },
    });
  }
  const fund = state.funds.find(item => item.fund_code === state.selectedFundCode) || null;
  return fundHoldingsPage.show(fund, {refresh});
}

// 约束求解与其他组合面板隔离状态；切换页面不会自动调用模型或刷新行情。
let portfolioAllocationWorkbench = null;
function mountPortfolioAllocation() {
  if (!window.FundScopePortfolioAllocation) return;
  portfolioAllocationWorkbench ||= window.FundScopePortfolioAllocation.create({
    request: apiRequest,
    getContext: () => ({ userId: state.userId, portfolioId: state.selectedPortfolioId }),
    refreshIcons,
    askAgent: draft => {
      state.agentContextMode = "portfolio";
      renderAgentFundContext();
      activateProductView("agent");
      elements.messageInput.value = `请读取约束草案 ${draft.draft_id} 的已保存求解结果，解释候选配置改善了什么、牺牲了什么，以及同一压力窗口的表现。引用事实，不重新计算或修改持仓。`;
      resizeComposer();
      elements.messageInput.focus();
    },
  });
  portfolioAllocationWorkbench.mount(elements.portfolioWorkbench.querySelector("[data-allocation-workbench]"));
}

// 压力沙盘独立保存表单和异步状态，不受其他组合模块的并发重绘影响。
let portfolioStressWorkbench = null;
function mountPortfolioStress() {
  if (!window.FundScopePortfolioStress) return;
  portfolioStressWorkbench ||= window.FundScopePortfolioStress.create({
    request: apiRequest,
    getContext: () => ({ userId: state.userId, portfolioId: state.selectedPortfolioId }),
    refreshIcons,
    askAgent: report => {
      state.agentContextMode = "portfolio";
      renderAgentFundContext();
      activateProductView("agent");
      elements.messageInput.value = `请读取并解释当前组合的历史压力测试记录 ${report.run_id}，比较原配置与候选配置，说明收益贡献和局限，不要重新选择行情或计算新情景。`;
      resizeComposer();
      elements.messageInput.focus();
    },
  });
  portfolioStressWorkbench.mount(elements.portfolioWorkbench.querySelector("[data-stress-workbench]"));
}


function renderOverviewDashboard() {
  // 各业务模块完成读取后，只通知终端重绘。不会额外采集、计费或生成一份新状态。
  researchTerminal?.update();
}

function initializeResearchTerminal() {
  // 终端通过窄接口连接现有应用：图表读取、显式同步、原有 SSE 对话和页面跳转。
  researchTerminal = window.FundScopeTerminal.create(document.querySelector("#overview-dashboard"), {
    getState: () => state,
    actions: {
      icons: refreshIcons,
      openResearch: context => {
        if (state.sending) { showToast("请先等待当前回答完成", "error"); return; }
        startNewConversation(); state.agentContextMode = "fund";
        activateProductView("agent"); renderAgentFundContext();
        elements.messageInput.value = `请分析 ${context.name}（${context.code}）的配置、风险与前景，给出证据和反对理由。`;
        resizeComposer(); elements.messageInput.focus();
      },
      markdown: formatMessageContent,
      loadChart: (code) => apiRequest(`${endpoints.funds}/${encodeURIComponent(code)}/chart`),
      loadEvidenceTab: loadTerminalEvidenceTab,
      selectIndex: (code) => { state.selectedMarketIndexCode = code; renderMarketSentiment(); },
      syncFunds: syncFundData,
      syncMarket: syncMarketData,
      stopAI: stopActiveAgentRun,
      reloadNews: () => { loadTerminalMarketEvents(); loadTerminalFundEvents(state.selectedFundCode); },
      newResearch: (context) => {
        if (state.sending) return;
        startNewConversation();
        state.terminalChatSubject = `${context.kind}:${context.code}`;
        state.terminalChatError = "";
      },
      ask: async (question, context) => {
        if (state.sending || !context.code) return;
        const key = `${context.kind}:${context.code}`;
        // 换标的时新建对话，历史仍在服务器保存，防止上一只基金的上下文混进本次分析。
        if (state.terminalChatSubject !== key) startNewConversation();
        state.terminalChatSubject = key;
        state.terminalChatError = "";
        state.agentContextMode = context.kind === "index" ? "market" : "fund";
        renderAgentFundContext();
        elements.messageInput.value = `研究标的：${context.name}（${context.code}）。${question}`;
        await sendMessage({ preventDefault() {} }, { terminal: true });
      },
      openDetail: (section) => {
        if (section === "fund") {
          const report = document.querySelector("#fund-report");
          report.open = true;
          report.scrollIntoView({ behavior: "auto", block: "start" });
          return;
        }
        if (section === "agent") { activateProductView("agent"); return; }
        activateProductView("market", { page: section });
      },
    },
  });
}

async function loadTerminalFundEvents(fundCode) {
  if (!fundCode) return;
  const requestId = ++terminalNewsRequestId;
  state.terminalFundEventsLoading = true;
  state.terminalFundEventsError = "";
  researchTerminal?.update();
  try {
    // 查询数据库里该基金的关联事件，不受市场页面的关键词筛选影响，也不触发抓取。
    const params = new URLSearchParams({ fund_code: fundCode, limit: "12" });
    const result = await apiRequest(`${endpoints.marketEvents}?${params}`);
    if (requestId === terminalNewsRequestId && fundCode === state.selectedFundCode) {
      state.terminalFundEvents = { fundCode, events: result.events };
    }
  } catch (error) {
    if (requestId === terminalNewsRequestId) state.terminalFundEventsError = error.message;
  } finally {
    if (requestId === terminalNewsRequestId) { state.terminalFundEventsLoading = false; researchTerminal?.update(); }
  }
}

async function loadTerminalMarketEvents() {
  const requestId = ++terminalMarketNewsRequestId;
  state.terminalMarketEventsLoading = true;
  state.terminalMarketEventsError = "";
  researchTerminal?.update();
  try {
    // 这是无关键词、类型或基金过滤的数据库读取，不启动新闻采集或模型分析。
    const result = await apiRequest(`${endpoints.marketEvents}?limit=20`);
    if (requestId === terminalMarketNewsRequestId) state.terminalMarketEvents = result;
  } catch (error) {
    if (requestId === terminalMarketNewsRequestId) {
      state.terminalMarketEvents = null;
      state.terminalMarketEventsError = error.message;
    }
  } finally {
    if (requestId === terminalMarketNewsRequestId) {
      state.terminalMarketEventsLoading = false;
      researchTerminal?.update();
    }
  }
}

function refreshFundWorkspace() {
  if (state.activeView === "overview" && state.workspacePages.overview === "reports") {
    loadFundReports({refresh:true});
    return;
  }
  if (state.activeView === "overview" && state.workspacePages.overview === "holdings") {
    loadFundHoldings({refresh:true});
    return;
  }
  // 顶栏刷新只重新读取当前页，不再连带读取全部市场、社区和组合模块。
  if (state.activeView === "portfolio") {
    if (state.workspacePages.portfolio === "holdings" && state.selectedPortfolioId) {
      refreshHoldingsQuotes();
      return;
    }
    loadPortfolios({preferredPortfolioId: state.selectedPortfolioId, showFeedback: true});
    return;
  }
  if (state.activeView === "overview") loadFunds({showFeedback: true});
  loadActiveWorkspacePage();
}

function scheduleFundDirectorySearch() {
  // 本地已关注列表立即过滤；全量目录请求稍作防抖，避免每敲一个字都访问 Provider。
  window.clearTimeout(fundSearchTimer);
  const query = state.fundSearch.trim();
  const requestId = ++state.fundSearchRequestId;
  state.fundSearchResults = [];
  state.fundSearchError = "";

  if (!query) {
    state.fundSearchLoading = false;
    renderFundDirectorySearch();
    return;
  }
  if (query.length < 2 && !/^\d{6}$/.test(query)) {
    state.fundSearchLoading = false;
    renderFundDirectorySearch();
    return;
  }

  state.fundSearchLoading = true;
  renderFundDirectorySearch();
  fundSearchTimer = window.setTimeout(
    () => searchFundDirectory(query, requestId),
    280,
  );
}

async function searchFundDirectory(query, requestId) {
  try {
    const params = new URLSearchParams({ q: query, limit: "12" });
    const response = await apiRequest(`${endpoints.fundSearch}?${params}`);
    if (requestId !== state.fundSearchRequestId) return;
    state.fundSearchResults = response.funds;
    state.fundSearchError = response.message || "";
  } catch (error) {
    if (requestId !== state.fundSearchRequestId) return;
    state.fundSearchResults = [];
    state.fundSearchError = error.message;
  } finally {
    if (requestId === state.fundSearchRequestId) {
      state.fundSearchLoading = false;
      renderFundDirectorySearch();
    }
  }
}

function renderFundDirectorySearch() {
  const query = state.fundSearch.trim();
  elements.fundSearchResults.hidden = !query;
  if (!query) {
    elements.fundSearchResults.innerHTML = "";
    return;
  }
  if (query.length < 2 && !/^\d{6}$/.test(query)) {
    elements.fundSearchResults.innerHTML = fundDirectoryStatus(
      "text-cursor-input",
      "继续输入名称，或输入完整 6 位基金代码",
    );
    refreshIcons();
    return;
  }
  if (state.fundSearchLoading) {
    elements.fundSearchResults.innerHTML = fundDirectoryStatus(
      "loader-circle",
      "正在搜索全量基金目录",
      true,
    );
    refreshIcons();
    return;
  }
  if (state.fundSearchError && !state.fundSearchResults.length) {
    elements.fundSearchResults.innerHTML = fundDirectoryStatus(
      "circle-alert",
      state.fundSearchError,
    );
    refreshIcons();
    return;
  }
  if (!state.fundSearchResults.length) {
    elements.fundSearchResults.innerHTML = fundDirectoryStatus(
      "search-x",
      "没有找到匹配的公募基金",
    );
    refreshIcons();
    return;
  }

  const rows = state.fundSearchResults.map((fund) => {
    const importing = state.fundImportingCode === fund.fund_code;
    const ready = fund.is_imported;
    const actionLabel = importing
      ? "正在导入"
      : ready
        ? "已关注"
        : fund.import_status === "failed"
          ? "重新导入"
          : "导入";
    const actionIcon = importing ? "loader-circle" : ready ? "check" : "download";
    return `
      <button
        class="fund-directory-row"
        type="button"
        data-directory-fund="${escapeAttribute(fund.fund_code)}"
        ${importing ? "disabled" : ""}
      >
        <span class="fund-directory-copy">
          <strong>${escapeHtml(fund.short_name)}</strong>
          <span>${escapeHtml(fund.fund_code)} · ${escapeHtml(fund.fund_type)}</span>
        </span>
        <span class="fund-directory-action" data-ready="${ready}">
          <i data-lucide="${actionIcon}"${importing ? ' class="spinning"' : ""}></i>
          <span>${actionLabel}</span>
        </span>
      </button>
    `;
  }).join("");
  const notice = state.fundSearchError
    ? `<p class="fund-directory-notice">${escapeHtml(state.fundSearchError)}</p>`
    : "";
  elements.fundSearchResults.innerHTML = `${rows}${notice}`;
  refreshIcons();
}

function fundDirectoryStatus(icon, message, spinning = false) {
  return `
    <div class="fund-directory-status">
      <i data-lucide="${escapeAttribute(icon)}"${spinning ? ' class="spinning"' : ""}></i>
      <span>${escapeHtml(message)}</span>
    </div>
  `;
}

function clearFundDirectorySearch() {
  window.clearTimeout(fundSearchTimer);
  state.fundSearchRequestId += 1;
  state.fundSearch = "";
  state.fundSearchResults = [];
  state.fundSearchLoading = false;
  state.fundSearchError = "";
  elements.fundSearchInput.value = "";
  renderFundDirectorySearch();
  renderFundNavigation();
  renderFundCards();
}

async function importFundFromDirectory(fundCode) {
  if (state.fundImportingCode) return;
  state.fundImportingCode = fundCode;
  renderFundDirectorySearch();

  try {
    const accepted = await apiRequest(endpoints.fundImport, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ user_id: state.userId, fund_code: fundCode }),
    });
    showToast("基金导入任务已提交");
    const completedJob = await waitForBackgroundJob(accepted.job.id, state.userId);
    if (completedJob.status !== "completed") {
      throw new ApiError(
        completedJob.error || `导入任务${formatJobStatus(completedJob.status)}`,
        503,
      );
    }

    // 先记住目标代码，目录刷新后 loadFunds 会直接选中刚导入的基金。
    state.selectedFundCode = fundCode;
    localStorage.setItem("fundscope-selected-fund", fundCode);
    clearFundDirectorySearch();
    await loadFunds();
    await selectFund(fundCode, { closeMobile: false, force: true });
    showToast(completedJob.result?.summary || "基金已导入研究工作台");
  } catch (error) {
    state.fundSearchError = error.message;
    showToast(error.message, "error");
  } finally {
    state.fundImportingCode = null;
    renderFundDirectorySearch();
  }
}

async function syncFundData() {
  // “刷新”只重读当前数据库；“同步”才会创建后台 Job 并访问 AKShare。
  if (state.fundSyncing) return;
  state.fundSyncing = true;
  setFundSyncButtonBusy(true);

  try {
    const accepted = await apiRequest(endpoints.fundDataSync, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ user_id: state.userId }),
    });
    const jobId = accepted.job.id;
    showToast("净值同步任务已提交");

    const completedJob = await waitForBackgroundJob(jobId, state.userId);
    if (completedJob.status !== "completed") {
      throw new ApiError(
        completedJob.error || `同步任务${formatJobStatus(completedJob.status)}`,
        503,
      );
    }

    // Job 已经把新净值写入 SQL；重新走原有读取链路即可同步刷新图表、风险和研判。
    await loadFunds();
    showToast(completedJob.result?.summary || "基金净值同步完成");
  } catch (error) {
    showToast(error.message, "error");
  } finally {
    state.fundSyncing = false;
    setFundSyncButtonBusy(false);
  }
}

async function waitForBackgroundJob(
  jobId,
  userId,
  onProgress = null,
  timeoutMs = 6 * 60 * 1000,
) {
  // 简单轮询持久化 Job 状态。即使页面请求与 Worker 不在同一进程，也能读到最新结果。
  // 较长工作流可以传入自己的等待时间，避免后端仍在运行时前端先误报超时。
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const response = await apiRequest(
      `${endpoints.backgroundJobs}/${encodeURIComponent(jobId)}?user_id=${encodeURIComponent(userId)}`,
    );
    if (onProgress) onProgress(response.job);
    if (["completed", "failed", "cancelled"].includes(response.job.status)) {
      return response.job;
    }
    await new Promise((resolve) => window.setTimeout(resolve, 700));
  }
  throw new ApiError("同步任务等待超时，可稍后在后台任务中查看状态", 504);
}

function setFundSyncButtonBusy(isBusy) {
  elements.syncFundDataButton.disabled = isBusy;
  elements.syncFundDataButton.title = isBusy
    ? "正在同步基金净值"
    : "同步全部已关注基金净值";
  elements.syncFundDataButton.setAttribute("aria-label", elements.syncFundDataButton.title);
  const icon = elements.syncFundDataButton.querySelector("svg, i");
  if (icon) icon.classList.toggle("spinning", isBusy);
}

async function refreshHoldingsQuotes({sync = false} = {}) {
  if (!state.selectedPortfolioId || state.holdingsQuoteStatus?.busy || state.fundSyncing) return;
  const userId = state.userId, portfolioId = state.selectedPortfolioId;
  const current = () => state.userId === userId && state.selectedPortfolioId === portfolioId;
  const status = {userId, portfolioId, busy: true, error: false, message: sync ? "正在同步已关注基金的净值与披露…" : "正在读取已保存净值…"};
  state.holdingsQuoteStatus = status;
  if (sync) { state.fundSyncing = true; setFundSyncButtonBusy(true); }
  renderPortfolioWorkbench({pages: ["holdings"], includeTransactions: false});
  try {
    let result = null;
    if (sync) {
      // 复用原有持久化 Job；只在点击更新时访问数据源，不建立定时采集。
      const accepted = await apiRequest(endpoints.fundDataSync, {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({user_id: userId})});
      const job = await waitForBackgroundJob(accepted.job.id, userId);
      if (job.status !== "completed") throw new Error(job.error || `同步任务${formatJobStatus(job.status)}`);
      result = job.result;
    }
    if (!current()) return;
    // 两份快照一起替换：最新涨跌来自基金目录，真实市值与盈亏由组合服务重算。
    const [catalog, portfolio] = await Promise.all([
      apiRequest(endpoints.funds),
      apiRequest(`${endpoints.portfolios}/${encodeURIComponent(portfolioId)}?${new URLSearchParams({user_id: userId})}`),
    ]);
    if (!current()) return;
    state.fundCatalog = catalog;
    state.funds = catalog.funds;
    state.portfolioDetail = portfolio;
    state.portfolios = state.portfolios.map(item => item.id === portfolioId ? portfolio : item);
    // 不改 portfolioDraft、当前研究对象、对话及历史报告，未提交编辑仍保持原样。
    portfolioPageReads.delete("risk");
    portfolioPageReads.delete("performance");
    // 报价和持仓曲线属于同一份资产视图，更新后不能继续沿用旧曲线。
    resetPortfolioPerformance();
    portfolioPageReads.delete("holdings");
    if (portfolio.position_mode === "actual") await loadPortfolioPage("holdings", {refresh:true});
    if (!current()) return;
    renderFundNavigation(); renderFundCards();
    status.message = sync ? result?.summary || "同步完成，各基金以实际披露日期为准。" : "已重新读取保存的净值，未访问外部数据源。";
    if (result?.failed_funds) {
      status.error = true;
      status.message += ` ${result.failed_funds} 只基金同步失败，仍显示原有报价。`;
    }
    showToast(status.message, status.error ? "error" : undefined);
  } catch (error) {
    if (current()) {
      status.error = true;
      status.message = `${error.message}。保留原有报价，可重新读取或重试更新。`;
      showToast(status.message, "error");
    }
  } finally {
    status.busy = false;
    if (sync) { state.fundSyncing = false; setFundSyncButtonBusy(false); }
    renderPortfolioWorkbench({pages: ["holdings"], includeTransactions: false});
  }
}

async function loadRetailIndices({refresh = false} = {}) {
  if (state.retailIndicesLoading) return;
  state.retailIndicesLoading = true;
  elements.refreshRetailIndices.disabled = true;
  elements.refreshRetailIndices.title = refresh ? "正在更新公开指数" : "正在读取指数";
  elements.retailIndices.setAttribute("aria-busy", "true");
  if (!state.retailIndices) elements.retailIndices.innerHTML = "<p>正在读取指数数据</p>";
  try {
    // 刷新才 POST；普通页面读取不会启动社媒采集、后台任务或模型调用。
    state.retailIndices = await apiRequest(`${endpoints.retailIndices}${refresh ? "/refresh" : ""}`,
      refresh ? {method: "POST"} : {});
    retailIndexChart?.dispose();
    elements.retailIndices.innerHTML = window.FundScopeRetailIndices.renderOverview(state.retailIndices);
    retailIndexChart = window.FundScopeRetailIndices.drawChart(
      elements.retailIndices.querySelector("#retail-index-chart"),
      state.retailIndices.indicators.find(item => item.indicator_id === "bagholder50"), window.echarts);
  } catch (error) {
    const errorHtml = `<p class="retail-index-error retail-request-error" role="alert">${escapeHtml(error.message)}，请稍后重试。</p>`;
    if (!state.retailIndices) {
      // 首次失败替换加载占位；这个重试只做 GET，不触发外部数据更新。
      elements.retailIndices.innerHTML = `${errorHtml}<button id="retry-retail-indices" class="icon-button" type="button"
        title="重新读取已保存的指数" aria-label="重新读取已保存的指数"><i data-lucide="refresh-cw"></i></button>`;
      refreshIcons();
    } else {
      // 有缓存时保留已显示的曲线与旧值，单独提示本次读取失败。
      elements.retailIndices.querySelector(".retail-request-error")?.remove();
      elements.retailIndices.insertAdjacentHTML("beforeend", errorHtml);
    }
  } finally {
    state.retailIndicesLoading = false;
    elements.refreshRetailIndices.disabled = false;
    elements.refreshRetailIndices.title = "更新可用的公开指数";
    elements.retailIndices.setAttribute("aria-busy", "false");
  }
}

let marketIntradayView = null;
let marketTurnoverView = null;
let marketTurnoverChart = null;
function loadMarketEnvironment() {
  // 日线曲线、盘中快照各自更新，避免盘中刷新把当日累计值混入每日成交额。
  marketTurnoverChart ||= window.FundScopeTurnoverChart.create({
    element: document.querySelector("#market-turnover-history"), request: apiRequest, refreshIcons,
  });
  marketIntradayView ||= window.FundScopeMarketIntraday.create({
    element: document.querySelector("#market-intraday"), request: apiRequest, refreshIcons,
  });
  marketTurnoverView ||= window.FundScopeMarketIntraday.create({
    element: document.querySelector("#market-turnover"), request: apiRequest, refreshIcons, turnover: true,
  });
  return Promise.all([loadMarketSentiment(), marketIntradayView.load(), marketTurnoverView.load(), marketTurnoverChart.load()]);
}

async function loadMarketSentiment() {
  if (state.marketSentimentLoading) return;
  state.marketSentimentLoading = true;
  state.marketSentimentError = "";
  renderMarketSentiment();

  try {
    state.marketSentiment = await apiRequest(endpoints.marketSentiment);
  } catch (error) {
    state.marketSentiment = null;
    state.marketSentimentError = error.message;
  } finally {
    state.marketSentimentLoading = false;
    renderMarketSentiment();
  }
}

async function syncMarketData() {
  // 市场同步与基金净值同步是两条独立 Job，失败时不会互相污染已有快照。
  if (state.marketSyncing) return;
  state.marketSyncing = true;
  renderMarketSentiment();

  try {
    const accepted = await apiRequest(endpoints.marketDataSync, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ user_id: state.userId }),
    });
    showToast("市场数据同步任务已提交");
    const completedJob = await waitForBackgroundJob(accepted.job.id, state.userId);
    if (completedJob.status !== "completed") {
      throw new ApiError(
        completedJob.error || `同步任务${formatJobStatus(completedJob.status)}`,
        503,
      );
    }

    // 市场状态变化后，单基金和组合研判也需要重新读取同一份 Evidence。
    const refreshes = [loadMarketSentiment(), loadPortfolioAssessment()];
    if (state.selectedFundCode) {
      refreshes.push(
        selectFund(state.selectedFundCode, { closeMobile: false, force: true }),
      );
    }
    await Promise.allSettled(refreshes);
    showToast(completedJob.result?.summary || "市场状态快照同步完成");
  } catch (error) {
    showToast(error.message, "error");
  } finally {
    state.marketSyncing = false;
    renderMarketSentiment();
  }
}

function renderMarketSentiment() {
  renderOverviewDashboard();
  if (state.marketSentimentLoading) {
    elements.marketSentiment.innerHTML = `
      <div class="market-sentiment-loading">
        <i data-lucide="loader-circle" class="spinning"></i>
        <span>正在读取市场环境</span>
      </div>
    `;
    refreshIcons();
    return;
  }

  const market = state.marketSentiment;
  if (!market || state.marketSentimentError) {
    elements.marketSentiment.innerHTML = `
      <div class="market-sentiment-error">
        <i data-lucide="triangle-alert"></i>
        <span>${escapeHtml(state.marketSentimentError || "市场环境暂不可用")}</span>
      </div>
    `;
    refreshIcons();
    return;
  }

  const sourceLinks = market.sources
    .map(
      (source) => `
        <a href="${escapeAttribute(source.source_url)}" target="_blank" rel="noreferrer">
          ${escapeHtml(formatMarketSourceName(source.name))}
        </a>
      `,
    )
    .join("");
  const limitations = market.limitations.join("；");
  const marketModeLabel = market.data_mode === "database"
    ? "数据库每日快照"
    : "本地固定快照";

  elements.marketSentiment.innerHTML = `
    <div class="market-sentiment-summary">
      <div class="market-sentiment-toolbar">
        <span class="market-sentiment-kicker">
          <i data-lucide="activity"></i>
          市场环境 · ${formatDateOnly(market.data_date)} · ${escapeHtml(marketModeLabel)}
        </span>
        <button
          class="icon-button subtle"
          type="button"
          data-sync-market
          title="从外部数据源同步市场环境"
          aria-label="从外部数据源同步市场环境"
          ${state.marketSyncing ? "disabled" : ""}
        >
          <i data-lucide="cloud-download"${state.marketSyncing ? ' class="spinning"' : ""}></i>
        </button>
      </div>
      <div class="market-state-heading">
        <span class="market-state-indicator ${escapeAttribute(marketStateTone(market.state_code))}"></span>
        <h2>${escapeHtml(market.state_label)}</h2>
      </div>
      <p>${escapeHtml(market.summary)}</p>
    </div>
    <div class="market-signal-grid">
      ${marketSignalMetric(
        "trending-up-down",
        "宽基趋势",
        market.trend.label,
        `20 日 ${formatSignedPercent(market.trend.return_20d_pct)} · 30 日 ${formatSignedPercent(market.trend.return_30d_pct)} · 3 个月 ${formatSignedPercent(market.trend.return_3m_pct)}`,
        market.signals.find((item) => item.key === "trend")?.direction,
      )}
      ${marketSignalMetric(
        "waves",
        "市场波动",
        market.volatility.label,
        `20 日年化 ${formatPlainPercent(market.volatility.annualized_volatility_pct)}`,
        market.signals.find((item) => item.key === "volatility")?.direction,
      )}
      ${marketSignalMetric(
        "chart-scatter",
        "市场宽度",
        market.breadth.label,
        `上涨 ${market.breadth.advancing_count.toLocaleString("zh-CN")} · 下跌 ${market.breadth.declining_count.toLocaleString("zh-CN")}`,
        market.signals.find((item) => item.key === "breadth")?.direction,
      )}
      ${marketSignalMetric(
        "gauge",
        "成交活跃",
        market.activity.label,
        `${formatCnyAmount(market.activity.latest_amount_cny)} · 5 日均值的 ${formatPlainPercent(market.activity.latest_vs_five_day_average_pct)}`,
        market.signals.find((item) => item.key === "activity")?.direction,
      )}
    </div>
    ${renderMarketIndexOverview(market.index_overview)}
    <footer class="market-sentiment-footer">
      <div class="market-source-links"><span>来源</span>${sourceLinks}</div>
      <span title="${escapeAttribute(limitations)}">可信度：中 · 代理口径</span>
    </footer>
  `;
  refreshIcons();
  keepSelectedMarketIndexVisible();
}

async function loadLargeCapitalSignals({ showFeedback = false } = {}) {
  const requestId = ++state.largeCapitalRequestId;
  state.largeCapitalLoading = true;
  state.largeCapitalError = "";
  renderLargeCapitalSignals();

  try {
    const params = new URLSearchParams({ user_id: state.userId });
    // 同一份市场数据可以同时关联当前基金和组合；后端只使用已披露持仓计算重合。
    if (state.selectedFundCode) params.set("fund_code", state.selectedFundCode);
    if (state.selectedPortfolioId) params.set("portfolio_id", state.selectedPortfolioId);
    const result = await apiRequest(`${endpoints.largeCapital}?${params}`);
    if (requestId !== state.largeCapitalRequestId) return;
    state.largeCapital = result;
    if (showFeedback) showToast("已读取数据库中的大资金线索");
  } catch (error) {
    if (requestId !== state.largeCapitalRequestId) return;
    state.largeCapital = null;
    state.largeCapitalError = error.message;
  } finally {
    if (requestId !== state.largeCapitalRequestId) return;
    state.largeCapitalLoading = false;
    renderLargeCapitalSignals();
  }
}

async function syncLargeCapitalSignals() {
  // 手动同步才访问外部 Provider；普通页面刷新只读取 SQL 中最后一份成功结果。
  if (state.largeCapitalSyncing) return;
  state.largeCapitalSyncing = true;
  renderLargeCapitalSignals();

  try {
    const accepted = await apiRequest(endpoints.largeCapitalSync, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ user_id: state.userId }),
    });
    showToast("大资金数据同步任务已提交");
    // 首次同步要逐日补上交所历史份额，等待时间比普通快照更长。
    const completedJob = await waitForBackgroundJob(
      accepted.job.id,
      state.userId,
      null,
      8 * 60 * 1000,
    );
    if (completedJob.status !== "completed") {
      throw new ApiError(
        completedJob.error || `同步任务${formatJobStatus(completedJob.status)}`,
        503,
      );
    }
    await loadLargeCapitalSignals();
    showToast(completedJob.result?.summary || "大资金行为线索同步完成");
  } catch (error) {
    showToast(error.message, "error");
  } finally {
    state.largeCapitalSyncing = false;
    renderLargeCapitalSignals();
  }
}

function renderLargeCapitalSignals() {
  renderOverviewDashboard();
  const button = elements.syncLargeCapitalButton;
  button.disabled = state.largeCapitalSyncing;
  button.title = state.largeCapitalSyncing
    ? "正在同步 ETF 份额与机构席位"
    : "同步 ETF 份额与龙虎榜机构席位";
  button.setAttribute("aria-label", button.title);
  button.querySelector("svg, i")?.classList.toggle("spinning", state.largeCapitalSyncing);

  if (state.largeCapitalLoading && !state.largeCapital) {
    elements.largeCapitalMeta.textContent = "正在读取 ETF 份额与机构席位";
    elements.largeCapitalSignals.innerHTML = `
      <div class="large-capital-status">
        <i data-lucide="loader-circle" class="spinning"></i>
        <span>正在整理宽基、行业与机构席位线索</span>
      </div>
    `;
    refreshIcons();
    return;
  }

  const capital = state.largeCapital;
  if (!capital || state.largeCapitalError) {
    elements.largeCapitalMeta.textContent = "公开资金数据暂不可用";
    elements.largeCapitalSignals.innerHTML = `
      <div class="large-capital-status is-error">
        <i data-lucide="triangle-alert"></i>
        <span>${escapeHtml(state.largeCapitalError || "无法读取大资金行为线索")}</span>
      </div>
    `;
    refreshIcons();
    return;
  }

  elements.largeCapitalMeta.textContent = capital.data_updated_at
    ? `数据 ${formatDateOnly(capital.data_date)} · 更新于 ${formatDateTime(capital.data_updated_at)}`
    : "等待首次同步公开资金数据";

  if (capital.data_mode === "unavailable") {
    elements.largeCapitalSignals.innerHTML = `
      <div class="large-capital-empty">
        <i data-lucide="database-zap"></i>
        <div>
          <strong>${escapeHtml(capital.state_label)}</strong>
          <span>${escapeHtml(capital.summary)}</span>
        </div>
      </div>
      ${renderCapitalIdentityBoundary(capital.identity_boundary)}
    `;
    refreshIcons();
    return;
  }

  const topicSignals = capital.topic_signals || [];
  const visibleTopics = topicSignals.filter((item) => item.topic_kind === "broad");
  const institution = capital.institutional_signal || {};
  const links = capital.exposure_links || [];

  elements.largeCapitalSignals.innerHTML = `
    <div class="large-capital-summary" data-tone="${escapeAttribute(capitalStateTone(capital.state_code))}">
      <div class="large-capital-summary-copy">
        <span>
          <i data-lucide="landmark"></i>
          ${formatDateOnly(capital.data_date)} · 可信度${formatCapitalConfidence(capital.confidence)}
        </span>
        <h3>${escapeHtml(capital.state_label)}</h3>
        <p>${escapeHtml(capital.summary)}</p>
      </div>
      <dl class="large-capital-summary-metrics">
        <div><dt>ETF 历史</dt><dd>${capital.etf_history_days} 日</dd></div>
        <div><dt>主题覆盖</dt><dd>${topicSignals.length} 个</dd></div>
        <div><dt>机构证券</dt><dd>${Number(institution.stock_count || 0).toLocaleString("zh-CN")} 只</dd></div>
      </dl>
    </div>

    ${renderCapitalSectorAnalysis(capital.sector_analysis)}

    <section class="capital-topic-section" aria-labelledby="capital-topic-heading">
      <header>
        <div>
          <h3 id="capital-topic-heading">宽基 ETF 份额变化</h3>
          <p>以同一 ETF 起止份额差估算净申购，金额使用最新可用价格。</p>
        </div>
      </header>
      <div class="capital-topic-table" role="table" aria-label="ETF 资金变化">
        <div class="capital-topic-row capital-topic-row-head" role="row">
          <span role="columnheader">主题</span>
          <span role="columnheader">近 1 日</span>
          <span role="columnheader">近 5 日</span>
          <span role="columnheader">近 20 日</span>
          <span role="columnheader">判断</span>
        </div>
        ${visibleTopics.map(renderCapitalTopicRow).join("") || `
          <div class="capital-topic-none">当前分类暂无具备可比起止点的 ETF。</div>
        `}
      </div>
    </section>

    <div class="capital-detail-grid">
      ${renderInstitutionalCapital(institution)}
      ${renderCapitalExposureLinks(links)}
    </div>

    ${renderCapitalIdentityBoundary(capital.identity_boundary)}
    ${renderCapitalMethodology(capital)}
  `;
  refreshIcons();
}

function renderCapitalTopicRow(item) {
  return `
    <div class="capital-topic-row" role="row">
      <div class="capital-topic-name" role="cell">
        <strong>${escapeHtml(item.topic_label)}</strong>
        <span>${item.etf_count} 只 ETF · 5 日覆盖 ${formatPlainPercent(item.coverage_5d_pct)}</span>
      </div>
      <strong role="cell" data-tone="${valueTone(item.flow_1d_cny)}">${formatCapitalFlow(item.flow_1d_cny)}</strong>
      <strong role="cell" data-tone="${valueTone(item.flow_5d_cny)}">${formatCapitalFlow(item.flow_5d_cny)}</strong>
      <strong role="cell" data-tone="${valueTone(item.flow_20d_cny)}">${formatCapitalFlow(item.flow_20d_cny)}</strong>
      <span class="capital-direction" role="cell" data-tone="${capitalDirectionTone(item.direction)}">
        ${escapeHtml(item.direction_label)}
      </span>
    </div>
  `;
}

function renderCapitalSectorAnalysis(analysis) {
  const windowData = analysis?.windows?.[state.largeCapitalSectorPeriod];
  if (!windowData?.rows?.length) {
    return `<section class="capital-sector-section"><h3>热门板块资金</h3>
      <p class="capital-sector-note">暂无行业 ETF 样本，更新大资金数据后查看板块统计。</p></section>`;
  }
  const periodButtons = [1, 5, 20].map(days => `
    <button type="button" data-capital-sector-period="${days}d"
      aria-pressed="${state.largeCapitalSectorPeriod === `${days}d`}"
      class="${state.largeCapitalSectorPeriod === `${days}d` ? "active" : ""}">近 ${days} 日</button>
  `).join("");
  const leaders = [["inflow", "净申购领先"], ["outflow", "净赎回领先"]].map(([key, label]) => {
    const leader = windowData.leaders[key];
    return `<div><dt>${label}</dt><dd>${leader ? `
      ${escapeHtml(leader.topic_label)} <strong data-tone="${valueTone(leader.net_flow_cny)}">${formatCapitalFlow(leader.net_flow_cny)}</strong>
      <small>样本覆盖 ${formatPlainPercent(leader.coverage_pct)}</small>` : "暂无可比样本"}</dd></div>`;
  }).join("");
  return `
    <section class="capital-sector-section" aria-labelledby="capital-sector-heading">
      <header>
        <div><h3 id="capital-sector-heading">热门板块资金</h3>
          <p>行业 ETF 净申购估算 · ${analysis.sector_count} 个关注主题 · 截至 ${formatDateOnly(analysis.data_date)}</p></div>
        <div class="capital-topic-tabs" role="group" aria-label="板块资金统计窗口">${periodButtons}</div>
      </header>
      <dl class="capital-sector-leaders">${leaders}
        <div><dt>已覆盖样本方向</dt><dd>${windowData.inflow_count} 流入 / ${windowData.outflow_count} 流出
          <small>${windowData.neutral_count} 持平 · ${windowData.unavailable_count} 数据不足</small></dd></div>
      </dl>
      <div class="capital-sector-table-wrap" role="region" aria-label="热门板块资金统计" tabindex="0">
        <table class="capital-sector-table">
          <thead><tr><th scope="col">板块 / 可比 ETF</th><th scope="col">近 ${windowData.period} 日净申购估算</th>
            <th scope="col">资金强度</th><th scope="col">规模覆盖率</th><th scope="col">资金观察</th></tr></thead>
          <tbody>${windowData.rows.map(renderCapitalSectorRow).join("")}</tbody>
        </table>
      </div>
      <details class="capital-sector-methodology"><summary>统计口径与边界</summary>
        <p>${escapeHtml(analysis.methodology)}</p><p>${escapeHtml(analysis.boundary)}</p></details>
    </section>`;
}

function renderCapitalSectorRow(item) {
  const available = item.status === "available";
  const dates = item.start_date && item.end_date
    ? `${formatDateOnly(item.start_date)} 至 ${formatDateOnly(item.end_date)}` : "起止数据不足";
  // 名称匹配不等于确定持仓占比，0% 时直接说明尚无可核验暴露。
  const links = (item.exposure_links || []).map(link => `
    <small class="capital-sector-link">${escapeHtml(link.scope_name)} · ${link.known_exposure_pct > 0
      ? `已披露暴露 ${formatPlainPercent(link.known_exposure_pct)}` : "主题相关，暴露待核验"}
      · 披露 ${formatDateOnly(link.disclosure_date)}</small>`).join("");
  return `<tr data-sector-code="${escapeAttribute(item.topic_code)}">
    <th scope="row"><strong>${escapeHtml(item.topic_label)}</strong>
      <small>${item.comparable_etf_count} / ${item.etf_count} 只可比</small></th>
    <td class="capital-sector-numeric" data-tone="${available ? valueTone(item.net_flow_cny) : "neutral"}">
      ${available ? formatCapitalFlow(item.net_flow_cny) : "数据不足"}</td>
    <td class="capital-sector-numeric" data-tone="${available ? valueTone(item.intensity_pct) : "neutral"}">
      ${available ? formatSignedPercent(item.intensity_pct) : "--"}</td>
    <td class="capital-sector-numeric">${available ? formatPlainPercent(item.coverage_pct) : "--"}</td>
    <td class="capital-sector-observation"><p>${escapeHtml(item.interpretation)}</p>
      <small>${item.status === "stale" ? `快照 ${formatDateOnly(item.data_date)}` : dates}</small>${links}</td>
  </tr>`;
}

function renderInstitutionalCapital(institution) {
  const available = institution.status === "available";
  const dates = available
    ? `${formatDateOnly(institution.trade_date_start)} - ${formatDateOnly(institution.trade_date_end)}`
    : "暂无可用交易日";
  return `
    <section class="institution-capital" aria-labelledby="institution-capital-heading">
      <header>
        <div>
          <h3 id="institution-capital-heading">龙虎榜机构席位</h3>
          <p>${escapeHtml(dates)} · ${institution.trading_day_count || 0} 个交易日</p>
        </div>
        <strong data-tone="${capitalDirectionTone(institution.direction)}">
          ${available ? formatCapitalFlow(institution.net_buy_cny) : "暂无数据"}
        </strong>
      </header>
      <div class="institution-capital-summary">
        <span>${escapeHtml(institution.direction_label || "暂无机构席位记录")}</span>
        ${available ? `<small>买入 ${formatCnyAmount(institution.buy_cny)} · 卖出 ${formatCnyAmount(institution.sell_cny)}</small>` : ""}
      </div>
      <p class="capital-sector-note">${available ? `覆盖 ${institution.stock_count} 只上榜证券。` : ""}此处为匿名机构席位总体买卖，不与上方 ETF 申赎相加。</p>
    </section>
  `;
}

function renderCapitalExposureLinks(links) {
  const contextLabel = state.selectedPortfolioId
    ? "当前组合"
    : state.selectedFundCode
      ? "当前基金"
      : "当前研究对象";
  return `
    <section class="capital-exposure" aria-labelledby="capital-exposure-heading">
      <header>
        <div>
          <h3 id="capital-exposure-heading">与持仓的关系</h3>
          <p>只使用基金季报与组合穿透中可核验的披露持仓。</p>
        </div>
        <i data-lucide="waypoints"></i>
      </header>
      ${links.map((link) => `
        <article class="capital-exposure-row">
          <div>
            <strong>${escapeHtml(link.scope_name)}</strong>
            <span>${link.scope === "portfolio" ? "组合" : "基金"} · 披露 ${formatDateOnly(link.disclosure_date)}</span>
          </div>
          <p>${escapeHtml(link.summary)}</p>
          <div class="capital-exposure-tags">
            ${link.matched_topics.map((topic) => `
              <span data-tone="${capitalDirectionTone(topic.direction)}">
                ${escapeHtml(topic.topic_label)} ${formatPlainPercent(topic.known_exposure_pct)}
              </span>
            `).join("")}
            ${link.direct_institutional_securities.map((item) => `
              <span data-tone="${valueTone(item.net_buy_cny)}">
                ${escapeHtml(item.security_name)} ${formatCapitalFlow(item.net_buy_cny)}
              </span>
            `).join("")}
          </div>
        </article>
      `).join("") || `
        <div class="capital-exposure-empty">
          <span>${escapeHtml(contextLabel)}暂无可核验的主题或机构席位重合。</span>
          <small>没有重合不代表没有机构交易，只代表当前披露覆盖内没有直接证据。</small>
        </div>
      `}
    </section>
  `;
}

function renderCapitalIdentityBoundary(boundary) {
  return `
    <aside class="capital-identity-boundary">
      <i data-lucide="shield-check"></i>
      <div>
        <strong>${escapeHtml(boundary?.label || "匿名资金不作身份认定")}</strong>
        <span>${escapeHtml(boundary?.explanation || "公开交易线索不能直接识别具体机构账户。")}</span>
      </div>
    </aside>
  `;
}

function renderCapitalMethodology(capital) {
  return `
    <details class="capital-methodology">
      <summary>
        <span><i data-lucide="book-open-check"></i>方法、来源与限制</span>
        <i data-lucide="chevron-down"></i>
      </summary>
      <div>
        <p>${escapeHtml(capital.methodology)}</p>
        <div class="capital-source-list">
          ${capital.sources.map((source) => `
            <a href="${escapeAttribute(source.source_url)}" target="_blank" rel="noreferrer">
              <span>${escapeHtml(source.name)}</span><b>${escapeHtml(source.source_level)} 级</b>
              <i data-lucide="external-link"></i>
            </a>
          `).join("")}
        </div>
        <ul>${capital.limitations.map((item) => `<li>${escapeHtml(item)}</li>`).join("")}</ul>
        <small>${escapeHtml(capital.disclaimer)}</small>
      </div>
    </details>
  `;
}

function capitalStateTone(stateCode) {
  if (stateCode === "accumulating") return "positive";
  if (stateCode === "withdrawing") return "negative";
  if (stateCode === "diverging") return "caution";
  return "neutral";
}

function capitalDirectionTone(direction) {
  if (direction === "inflow") return "positive";
  if (direction === "outflow") return "negative";
  if (direction === "mixed") return "caution";
  return "neutral";
}

function formatCapitalConfidence(value) {
  return { high: "高", medium: "中", low: "低" }[value] || "待确认";
}

function keepSelectedMarketIndexVisible() {
  const tabList = elements.marketSentiment.querySelector(".market-index-tabs");
  const activeTab = tabList?.querySelector(".market-index-tab.is-active");
  if (!tabList || !activeTab || tabList.scrollWidth <= tabList.clientWidth) return;

  // 重绘会创建新的 Tab DOM；手动恢复滚动位置，让移动端始终看得到当前选项。
  tabList.scrollLeft = Math.max(
    0,
    activeTab.offsetLeft - (tabList.clientWidth - activeTab.offsetWidth) / 2,
  );
}

function renderMarketIndexOverview(overview) {
  if (!overview) return "";
  if (!overview.indices?.length) {
    return `
      <section class="market-index-overview market-index-overview-empty" aria-label="宽基指数分区">
        <i data-lucide="chart-no-axes-combined"></i>
        <div>
          <strong>宽基指数分区等待同步</strong>
          <span>${escapeHtml(overview.summary)}</span>
        </div>
      </section>
    `;
  }

  const selected = overview.indices.find(
    (item) => item.index_code === state.selectedMarketIndexCode,
  ) || overview.indices[0];
  state.selectedMarketIndexCode = selected.index_code;

  const tabs = overview.indices.map((item) => {
    const isSelected = item.index_code === selected.index_code;
    return `
      <button
        id="market-index-tab-${escapeAttribute(item.index_code)}"
        class="market-index-tab${isSelected ? " is-active" : ""}"
        type="button"
        role="tab"
        aria-selected="${isSelected}"
        aria-controls="market-index-detail"
        tabindex="${isSelected ? "0" : "-1"}"
        data-market-index-code="${escapeAttribute(item.index_code)}"
      >
        <span>${escapeHtml(item.market_segment)}</span>
        <strong>${escapeHtml(item.index_name)}</strong>
        <div class="market-index-tab-periods" aria-label="阶段涨跌">
          <em data-tone="${escapeAttribute(valueTone(item.return_30d_pct))}">
            <span>30 日</span>
            <b>${formatSignedPercent(item.return_30d_pct)}</b>
          </em>
          <em data-tone="${escapeAttribute(valueTone(item.return_3m_pct))}">
            <span>3 个月</span>
            <b>${formatSignedPercent(item.return_3m_pct)}</b>
          </em>
        </div>
      </button>
    `;
  }).join("");

  return `
    <section class="market-index-overview" aria-labelledby="market-index-title">
      <header class="market-index-header">
        <div>
          <h3 id="market-index-title">宽基指数分区</h3>
          <p>${escapeHtml(overview.summary)}</p>
        </div>
        <span>近 30 日 / 近 3 个月相对强弱 · 截至 ${formatDateOnly(overview.data_date)}</span>
      </header>
      <div class="market-index-tabs" role="tablist" aria-label="选择宽基指数">
        ${tabs}
      </div>
      <div
        id="market-index-detail"
        class="market-index-detail"
        role="tabpanel"
        aria-labelledby="market-index-tab-${escapeAttribute(selected.index_code)}"
      >
        <div class="market-index-detail-summary">
          <div class="market-index-title-row">
            <div>
              <span>${escapeHtml(selected.market_segment)} · ${escapeHtml(selected.index_code)}</span>
              <h4>${escapeHtml(selected.index_name)}</h4>
            </div>
            <div class="market-index-quote" data-tone="${escapeAttribute(valueTone(selected.daily_return_pct))}">
              <strong>${formatIndexLevel(selected.close)}</strong>
              <span>${formatSignedPercent(selected.daily_return_pct)} 当日</span>
            </div>
          </div>
          <p>${escapeHtml(selected.summary)}</p>
        </div>
        <div class="market-index-detail-metrics">
          ${marketIndexMetric(
            "20 / 30 日",
            `${formatSignedPercent(selected.return_20d_pct)} / ${formatSignedPercent(selected.return_30d_pct)}`,
            "短中期变化",
            valueTone(selected.return_30d_pct),
          )}
          ${marketIndexMetric(
            "近 3 个月",
            formatSignedPercent(selected.return_3m_pct),
            "按 60 个交易日近似",
            valueTone(selected.return_3m_pct),
          )}
          ${marketIndexMetric(
            "20 日年化波动",
            formatPlainPercent(selected.annualized_volatility_20d_pct),
            selected.volatility_label,
            selected.volatility_label === "偏高" ? "caution" : "neutral",
          )}
          ${marketIndexMetric(
            "近 3 个月最大回撤",
            formatSignedPercent(selected.max_drawdown_60d_pct),
            "历史阶段跌幅",
            selected.max_drawdown_60d_pct <= -10 ? "negative" : "neutral",
          )}
          ${marketIndexMetric(
            "成交量 / 5 日均量",
            formatPlainPercent(selected.volume_vs_five_day_average_pct),
            selected.activity_label,
            selected.activity_label === "放量" ? "positive" : selected.activity_label === "缩量" ? "caution" : "neutral",
          )}
          ${marketIndexMetric(
            "相对强弱",
            `${selected.relative_strength_rank_30d} / ${overview.index_count}`,
            `30 日排名 · 3 个月 ${selected.relative_strength_rank_3m}/${overview.index_count}`,
            selected.relative_strength_rank_30d === 1 ? "positive" : "neutral",
          )}
        </div>
        <footer class="market-index-detail-footer">
          <span>日线收盘口径 · 指数之间不比较绝对成交量</span>
          <a href="${escapeAttribute(selected.source_url)}" target="_blank" rel="noreferrer">
            查看数据来源 <i data-lucide="arrow-up-right"></i>
          </a>
        </footer>
      </div>
    </section>
  `;
}

function marketIndexMetric(label, value, note, tone = "neutral") {
  return `
    <div class="market-index-metric" data-tone="${escapeAttribute(tone)}">
      <span>${escapeHtml(label)}</span>
      <strong>${escapeHtml(value)}</strong>
      <small>${escapeHtml(note)}</small>
    </div>
  `;
}

function marketSignalMetric(icon, label, value, detail, direction = "neutral") {
  return `
    <div class="market-signal" data-direction="${escapeAttribute(direction)}">
      <span class="market-signal-label"><i data-lucide="${escapeAttribute(icon)}"></i>${escapeHtml(label)}</span>
      <strong>${escapeHtml(value)}</strong>
      <small>${escapeHtml(detail)}</small>
    </div>
  `;
}

async function loadSocialSignals({ showFeedback = false } = {}) {
  if (state.socialSignalsLoading) return;
  state.socialSignalsLoading = true;
  state.socialSignalsError = "";
  renderSocialSignals();

  try {
    state.socialSignals = await apiRequest(endpoints.socialSignals);
    renderSocialReview(state.socialSignals.review);
    // 原帖分页是独立视图，进入话题不再附带加载另一页的样本。
    if (state.workspacePages.market === "social-posts") await loadSocialPosts();
    if (showFeedback) showToast("社区仓位风向已刷新");
  } catch (error) {
    state.socialSignals = null;
    state.socialSignalsError = error.message;
    if (showFeedback) showToast(error.message, "error");
  } finally {
    state.socialSignalsLoading = false;
    renderSocialSignals();
  }
}

async function connectXhsSession() {
  // 登录发生在后端启动的独立 Chrome Profile 中；前端只等待 Job，不接触任何 Cookie。
  if (state.socialSignalsConnecting || state.socialSignalsSyncing) return;
  // 已连接时不重复启动临时 Chrome，否则持久化 Profile 会被立即识别，窗口看起来
  // 像“闪退”。此时用户真正需要执行的是右侧的社交观点刷新操作。
  if (state.socialSignals?.status?.session_ready && !state.socialSignals?.status?.verification_required) {
    showToast("小红书已经连接，可直接点击右侧按钮刷新社区观点");
    return;
  }
  state.socialSignalsConnecting = true;
  updateSocialSignalControls();

  try {
    const accepted = await apiRequest(endpoints.socialSignalBrowserSession, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ user_id: state.userId }),
    });
    showToast("请在弹出的 Chrome 中完成小红书登录或平台要求的验证，成功后窗口会自动关闭");
    const completedJob = await waitForBackgroundJob(
      accepted.job.id,
      state.userId,
      null,
      7 * 60 * 1000,
    );
    if (completedJob.status !== "completed") {
      throw new ApiError(
        completedJob.error || `小红书连接任务${formatJobStatus(completedJob.status)}`,
        503,
      );
    }
    await loadSocialSignals();
    showToast(completedJob.result?.summary || "小红书已连接");
  } catch (error) {
    showToast(error.message, "error");
    // Provider 检测到登录失效时会清除本地标记；重新读取状态后，连接按钮才能恢复。
    await loadSocialSignals();
  } finally {
    state.socialSignalsConnecting = false;
    updateSocialSignalControls();
  }
}

async function syncSocialSignals() {
  // 单独刷新仍使用 Background Job；长时间的分页采集和模型抽取不会占住 HTTP 请求。
  if (state.socialSignalsSyncing || state.socialSignalsConnecting) return;
  const pauseMessage = socialSignalPauseMessage();
  if (pauseMessage) {
    showToast(pauseMessage, "error");
    return;
  }
  if (!state.socialSignals?.status?.session_ready) {
    showToast("请先连接小红书并完成浏览器登录", "error");
    return;
  }
  state.socialSignalsSyncing = true;
  updateSocialSignalControls();

  try {
    const accepted = await apiRequest(endpoints.socialSignalSync, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ user_id: state.userId }),
    });
    showToast("社交观点采集任务已提交");
    const completedJob = await waitForBackgroundJob(
      accepted.job.id,
      state.userId,
      null,
      11 * 60 * 1000,
    );
    if (completedJob.status !== "completed") {
      throw new ApiError(
        completedJob.error || `社交观点任务${formatJobStatus(completedJob.status)}`,
        503,
      );
    }
    await Promise.allSettled([
      loadSocialSignals(),
      loadMarketEvents(),
      loadTerminalMarketEvents(),
      loadTerminalFundEvents(state.selectedFundCode),
      loadPortfolioAssessment(),
    ]);
    showToast(completedJob.result?.summary || "社交观点刷新完成", completedJob.result?.status === "completed" ? "success" : "error");
  } catch (error) {
    showToast(error.message, "error");
    // 采集过程中若发现登录失效，Provider 会删除会话标记；同步回最新状态后允许重连。
    await loadSocialSignals();
  } finally {
    state.socialSignalsSyncing = false;
    updateSocialSignalControls();
  }
}

function socialSignalPauseMessage() {
  const status = state.socialSignals?.status;
  if (!status) return "";
  if (status.verification_required) return status.access_message || "小红书采集已暂停，请稍后手动处理平台验证。";
  if (!status.session_ready) return status.access_message || "请先连接小红书；已保存样本仍可查看。";
  if (Date.parse(status.next_collection_at || "") > Date.now()) {
    return `本地采集间隔尚未结束，${formatDateTime(status.next_collection_at)} 后可手动再试。已保存样本仍可查看，不会到点自动采集。`;
  }
  return "";
}

function updateSocialSignalControls() {
  const analyzeButton = document.querySelector("#analyze-social-signals");
  analyzeButton.disabled = state.socialSignalsAnalyzing || state.socialSignalsSyncing;
  analyzeButton.title = state.socialSignalsAnalyzing ? "正在分析本地样本" : `分析已有数据（${state.socialPostScope === "all" ? "历史样本" : "当前操作窗口"}）`;
  analyzeButton.querySelector("svg, i")?.classList.toggle("spinning", state.socialSignalsAnalyzing);
  const sessionReady = Boolean(state.socialSignals?.status?.session_ready);
  const verificationRequired = Boolean(state.socialSignals?.status?.verification_required);
  const pauseMessage = socialSignalPauseMessage();
  elements.connectXhsSessionButton.disabled =
    state.socialSignalsConnecting || state.socialSignalsSyncing;
  elements.connectXhsSessionButton.title = state.socialSignalsConnecting
    ? "正在等待小红书登录"
    : verificationRequired
      ? "处理小红书验证"
      : sessionReady
      ? "小红书已连接，无需重复登录"
      : "连接小红书";
  elements.connectXhsSessionButton.setAttribute(
    "aria-label",
    elements.connectXhsSessionButton.title,
  );
  elements.connectXhsSessionButton.dataset.connected = String(sessionReady && !verificationRequired);

  elements.syncSocialSignalsButton.disabled =
    state.socialSignalsSyncing || state.socialSignalsAnalyzing || state.socialSignalsConnecting || !sessionReady || Boolean(pauseMessage);
  elements.syncSocialSignalsButton.title = state.socialSignalsSyncing
    ? "正在采集并聚合社交观点"
    : pauseMessage || (sessionReady
      ? "单独刷新社交观点"
      : "连接小红书后才能刷新");
  elements.syncSocialSignalsButton.setAttribute(
    "aria-label",
    elements.syncSocialSignalsButton.title,
  );
  const connectIcon = elements.connectXhsSessionButton.querySelector("svg, i");
  const syncIcon = elements.syncSocialSignalsButton.querySelector("svg, i");
  if (connectIcon) connectIcon.classList.toggle("spinning", state.socialSignalsConnecting);
  if (syncIcon) syncIcon.classList.toggle("spinning", state.socialSignalsSyncing);
  // 到期仅更新按钮/文案，不发送采集请求，也不访问小红书。
  window.clearTimeout(socialSignalCooldownTimer);
  const remaining = Date.parse(state.socialSignals?.status?.next_collection_at || "") - Date.now();
  if (remaining > 0 && !verificationRequired) {
    socialSignalCooldownTimer = window.setTimeout(renderSocialSignals, Math.min(remaining + 100, 2147483647));
  }
}

function renderSocialSignals() {
  renderOverviewDashboard();
  updateSocialSignalControls();
  document.querySelector("#social-collection-status").textContent = socialSignalPauseMessage()
    || (state.socialSignals?.status?.session_ready ? "已有本机登录状态。采集仅在手动触发后进行。" : "未连接采集会话；已保存的样本仍可查看。");
  if (state.socialSignalsLoading) {
    elements.socialSignalsMeta.textContent = "正在读取最近公开观点";
    elements.socialSignals.innerHTML = socialSignalStatus(
      "loader-circle",
      "正在按作者去重并聚合仓位方向",
      true,
    );
    refreshIcons();
    return;
  }

  const result = state.socialSignals;
  if (!result || state.socialSignalsError) {
    elements.socialSignalsMeta.textContent = "社交观点暂不可用";
    elements.socialSignals.innerHTML = socialSignalStatus(
      "triangle-alert",
      state.socialSignalsError || "社交观点暂不可用",
    );
    refreshIcons();
    return;
  }

  const operationWindow = renderSocialOperationWindow(result.operation_window);
  const operationTarget = result.operation_window
    ? `目标交易日 ${formatDateOnly(result.operation_window.target_trade_date)} · ${result.operation_window.cutoff_time} 截止`
    : `${result.lookback_hours} 小时窗口`;
  if (!result.configured) {
    elements.socialSignalsMeta.textContent = operationTarget;
    elements.socialSignals.innerHTML = `
      ${operationWindow}
      ${socialSignalStatus(
        "monitor-down",
        "安装 Playwright 并重启服务后，即可使用本机 Chrome 采集公开观点",
      )}
    `;
    refreshIcons();
    return;
  }

  // 暂停是采集状态，不是数据库清空。提示后继续渲染此前的分析和原始样本。
  const pauseMessage = socialSignalPauseMessage();
  const pauseNotice = pauseMessage ? socialSignalStatus("shield-alert", pauseMessage) : "";

  const latest = result.status.latest_post_at
    ? ` · 最新 ${formatDateTime(result.status.latest_post_at)}`
    : "";
  const sampleCount = Number(result.sample_count || 0);
  const acceptedSampleCount = Number(result.accepted_sample_count || 0);
  elements.socialSignalsMeta.textContent = `${operationTarget}${latest}`;
  // 独立作者数由每个话题提供，不能把跨话题重复出现的作者相加冒充全站去重。
  const sampleMetrics = window.FundScopeDataVisuals.metrics([
    {label:"已保存帖子", value:window.FundScopeDataVisuals.number(result.sample_count, 0)},
    {label:"纳入操作统计", value:window.FundScopeDataVisuals.number(result.accepted_sample_count, 0)},
    {label:"聚合主题", value:window.FundScopeDataVisuals.number(result.aggregate_count, 0)},
  ]);

  if (!result.aggregates.length) {
    elements.socialSignals.innerHTML = `
      ${sampleMetrics}
      ${operationWindow}
      ${pauseNotice}
      ${socialSignalStatus(
        "message-circle-dashed",
        sampleCount
          ? `当前窗口已保存 ${sampleCount} 条，其中 ${acceptedSampleCount} 条纳入操作统计，暂未形成可聚合观点`
          : `当前 ${result.operation_window?.cutoff_time || "15:00"} 操作窗口内尚未采集到公开观点`,
      )}
      <footer class="social-signal-disclaimer">
        <i data-lucide="info"></i>
        <span>${escapeHtml(result.disclaimer)}</span>
      </footer>
    `;
    refreshIcons();
    return;
  }

  elements.socialSignals.innerHTML = `
    ${sampleMetrics}
    ${operationWindow}
    ${pauseNotice}
    <div class="social-signal-list">
      ${window.FundScopeResearchLists.topics(result.aggregates)}
    </div>
    <footer class="social-signal-disclaimer">
      <i data-lucide="info"></i>
      <span>${escapeHtml(result.disclaimer)}</span>
    </footer>
  `;
  refreshIcons();
}

function renderSocialOperationWindow(windowInfo) {
  if (!windowInfo) return "";
  const phaseIcons = {
    before_cutoff: "timer",
    after_cutoff: "calendar-clock",
    market_closed: "calendar-off",
  };
  const startText = formatDateTime(windowInfo.window_start);
  const targetText = formatDateOnly(windowInfo.target_trade_date);
  return `
    <div
      class="social-operation-window"
      data-phase="${escapeAttribute(windowInfo.phase)}"
    >
      <i data-lucide="${phaseIcons[windowInfo.phase] || "clock-3"}"></i>
      <div>
        <strong>${escapeHtml(windowInfo.label)}</strong>
        <span>目标交易日 ${escapeHtml(targetText)} · 仅统计 ${escapeHtml(startText)} 后发布的观点</span>
      </div>
      <code>MAX ${Number(windowInfo.max_age_hours || 0)}H</code>
    </div>
  `;
}

function renderSocialSignalAggregate(item) {
  const score = Math.max(-100, Math.min(100, Number(item.positioning_score || 0)));
  const markerPosition = (score + 100) / 2;
  const tone = score >= 20 ? "positive" : score <= -20 ? "negative" : "neutral";
  const counts = item.action_counts || {};
  const evidence = (item.evidence_posts || []).map((post) => `
    <a href="${escapeAttribute(post.source_url)}" target="_blank" rel="noreferrer">
      <span>${escapeHtml(post.author_name)}</span>
      <strong>${escapeHtml(post.action_label)}</strong>
      <small>${escapeHtml(post.title || post.rationale || "公开观点")}</small>
      <i data-lucide="external-link"></i>
    </a>
  `).join("");

  return `
    <article class="social-signal-row" data-tone="${escapeAttribute(tone)}">
      <div class="social-signal-topic">
        <span>${escapeHtml(item.topic_type === "fund" ? "基金" : "主题")}</span>
        <h3>${escapeHtml(item.topic_label)}</h3>
        <small>${item.author_count} 位作者 · 置信度 ${Math.round(Number(item.confidence) * 100)}%</small>
      </div>
      <div class="social-positioning">
        <div>
          <strong>${escapeHtml(item.positioning_label)}</strong>
          <span>${score > 0 ? "+" : ""}${score.toFixed(1)}</span>
        </div>
        <span class="social-positioning-track" aria-label="仓位倾向分 ${score.toFixed(1)}">
          <i class="social-positioning-center"></i>
          <i class="social-positioning-marker" style="left:${markerPosition}%"></i>
        </span>
        <small>分歧 ${Math.round(Number(item.disagreement_ratio) * 100)}%</small>
      </div>
      <div class="social-action-counts" aria-label="作者自述操作分布">
        <span><b>${Number(counts.buy || 0) + Number(counts.add || 0)}</b>买入/加仓</span>
        <span><b>${Number(counts.hold || 0) + Number(counts.watch || 0)}</b>持有/观望</span>
        <span><b>${Number(counts.reduce || 0) + Number(counts.sell || 0)}</b>减仓/卖出</span>
      </div>
      <details class="social-signal-evidence">
        <summary>查看 ${item.evidence_posts.length} 条代表观点</summary>
        <div>${evidence}</div>
      </details>
    </article>
  `;
}

function renderSocialSignalSamples(posts, totalCount) {
  // 被模型弃权的帖子仍是“采集结果”，只是不能被当成已经证实的基金风向。
  // 单独展示这一层，可以同时保留透明度和金融结论的证据门槛。
  const visiblePosts = posts;
  if (!visiblePosts.length) return "";

  const rows = visiblePosts.map((post) => {
    const accepted = post.analysis_status === "accepted";
    const label = accepted ? "纳入统计" : post.analysis_status === "pending" ? "待分析" : "仅作观点参考";
    const excerpt = String(post.content_excerpt || "").trim();
    const showExcerpt = excerpt && excerpt !== String(post.title || "").trim();
    return `
      <li data-status="${accepted ? "accepted" : "abstained"}">
        <button class="social-post-row" type="button" data-social-post="${escapeAttribute(post.post_id)}">
          <i data-lucide="${accepted ? "circle-check" : "circle-dashed"}"></i>
          <div class="social-sample-copy">
            <span>
              <strong>${label}</strong>
              <time datetime="${escapeAttribute(post.published_at)}">${escapeHtml(formatDateTime(post.published_at))}</time>
            </span>
            <h3>${escapeHtml(post.title || "未命名公开笔记")}</h3>
            ${showExcerpt ? `<p>${escapeHtml(excerpt)}</p>` : ""}
            <small>${post.images?.length ? `${post.images.length} 张图片` : "未保存图片"}${post.exclusion_reasons?.length ? ` · ${escapeHtml(post.exclusion_reasons[0])}` : ""}</small>
          </div>
          <div class="social-sample-source">
            <strong>${escapeHtml(post.author_name || "小红书用户")}</strong>
            <span>${escapeHtml(post.matched_keyword || "公开搜索")}</span>
          </div>
          <i data-lucide="panel-right-open"></i>
        </button>
      </li>
    `;
  }).join("");

  return `
    <section class="social-sample-feed" aria-label="最近采集样本">
      <header>
        <div>
          <strong>已保存帖子</strong>
          <span>正文、图片证据与逐帖分析</span>
        </div>
        <span>显示 ${visiblePosts.length} / ${Number(totalCount || visiblePosts.length)}</span>
      </header>
      <ol>${rows}</ol>
    </section>
  `;
}

async function analyzeSavedSocialSignals() {
  if (state.socialSignalsAnalyzing || state.socialSignalsSyncing) return;
  state.socialSignalsAnalyzing = true;
  updateSocialSignalControls();
  const review = document.querySelector("#social-review");
  review.innerHTML = socialSignalStatus("loader-circle", "正在识别图片文字并综合已有观点", true);
  try {
    const accepted = await apiRequest(endpoints.socialSignalAnalysis, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ user_id: state.userId, scope: state.socialPostScope }),
    });
    const job = await waitForBackgroundJob(accepted.job.id, state.userId, (current) => {
      review.innerHTML = socialSignalStatus("loader-circle", current.progress_message || "正在分析本地样本", true);
      refreshIcons();
    }, 11 * 60 * 1000);
    if (job.status !== "completed") throw new Error(job.error || job.progress_message || "分析未完成");
    await loadSocialSignals();
    showToast(job.result?.summary || "已有观点分析完成", job.result?.status === "partial" ? "error" : "success");
  } catch (error) {
    renderSocialReview(state.socialSignals?.review);
    review.insertAdjacentHTML("afterbegin", socialSignalStatus("triangle-alert", error.message));
    showToast(error.message, "error");
  } finally {
    state.socialSignalsAnalyzing = false;
    updateSocialSignalControls();
  }
}

async function loadSocialPosts() {
  if (state.socialPostsLoading) return;
  state.socialPostsLoading = true;
  const host = document.querySelector("#social-posts");
  host.setAttribute("aria-busy", "true");
  host.querySelectorAll("button, select").forEach(control => { control.disabled = true; });
  // 分页和范围切换都只读数据库；不调用采集或大模型。
  try {
    const params = new URLSearchParams({ scope: state.socialPostScope, page: state.socialPostPage, page_size: 10 });
    state.socialPosts = await apiRequest(`${endpoints.socialSignals}/posts?${params}`);
    renderSocialPosts();
    if (state.socialPosts.review) renderSocialReview(state.socialPosts.review);
    updateSocialSignalControls();
  } catch (error) {
    host.innerHTML = socialSignalStatus("triangle-alert", error.message) + `<button type="button" data-social-page="${state.socialPostPage}">重新加载帖子</button>`;
  } finally {
    state.socialPostsLoading = false;
    host.setAttribute("aria-busy", "false");
  }
}

function renderSocialPosts() {
  const data = state.socialPosts;
  if (!data) return;
  const pages = Math.max(1, Math.ceil(data.total / data.page_size));
  document.querySelector("#social-posts").innerHTML = `
    <div class="social-post-toolbar"><label for="social-post-scope">帖子范围</label>
      <select id="social-post-scope"><option value="window" ${state.socialPostScope === "window" ? "selected" : ""}>当前操作窗口</option><option value="all" ${state.socialPostScope === "all" ? "selected" : ""}>全部已保存</option></select>
      <span>共 ${data.total} 条</span></div>
    ${data.posts.length ? renderSocialSignalSamples(data.posts, data.total) : socialSignalStatus("inbox", "此范围没有已保存帖子")}
    <nav class="social-pagination" aria-label="帖子分页">
      <button class="icon-button" title="上一页" aria-label="上一页" data-social-page="${data.page - 1}" ${data.page <= 1 ? "disabled" : ""}><i data-lucide="chevron-left"></i></button>
      <span>${data.page} / ${pages}</span>
      <button class="icon-button" title="下一页" aria-label="下一页" data-social-page="${data.page + 1}" ${data.page >= pages ? "disabled" : ""}><i data-lucide="chevron-right"></i></button>
    </nav>`;
  refreshIcons();
}

function renderSocialReview(review) {
  const host = document.querySelector("#social-review");
  if (!review || ["not_generated", "empty"].includes(review.status)) {
    host.innerHTML = `<header><h3>AI 社区解读</h3><span>尚未生成</span></header>`;
    return;
  }
  const finding = (item) => `<article><h4>${escapeHtml(item.heading)}</h4><p>${escapeHtml(item.explanation)}</p><div class="social-citations">${item.post_ids.map((id, index) => `<button type="button" data-social-post="${escapeAttribute(id)}" title="查看引用原帖">来源 ${index + 1}</button>`).join("")}</div></article>`;
  host.innerHTML = `<header><h3>AI 社区解读</h3><span>${review.status === "stale" ? "样本已变化 · 待更新" : "已生成"}</span></header>
    <p class="social-review-meta">${review.scope === "all" ? "历史样本 · " : ""}${escapeHtml(review.model_name || "")} · ${formatDateTime(review.generated_at)} · ${review.sample_count || 0} 条样本 / ${review.author_count || 0} 位作者 · ${review.image_count || 0} 张图片</p>
    <p>${escapeHtml(review.overview || "")}</p>
    ${(review.findings || []).map(finding).join("")}
    ${review.disagreements?.length ? `<h4>观点分歧</h4>${review.disagreements.map(finding).join("")}` : ""}
    <details><summary>样本局限</summary><ul>${(review.limitations || []).map(text => `<li>${escapeHtml(text)}</li>`).join("")}</ul></details>`;
}

async function openSocialPost(postId) {
  const dialog = document.querySelector("#social-post-detail");
  const body = document.querySelector("#social-detail-body");
  body.innerHTML = socialSignalStatus("loader-circle", "正在读取帖子证据", true);
  if (!dialog.open) dialog.showModal();
  try {
    const post = await apiRequest(`${endpoints.socialSignals}/posts/${encodeURIComponent(postId)}`);
    const status = { pending: "待分析", accepted: "已分析 · 纳入操作统计", abstained: "已分析 · 未纳入操作统计" }[post.analysis_status];
    const kinds = { trade: "操作自述", holding: "持仓记录", profit: "收益记录", opinion: "一般观点", unclear: "类型未确定" };
    const ocrLabels = { pending: "待识别", completed: "文字已识别", empty: "未识别到文字", error: "识别失败", unavailable: "识别器不可用" };
    body.innerHTML = `<h3>${escapeHtml(post.title)}</h3><p class="social-review-meta">${escapeHtml(post.author_name)} · ${formatDateTime(post.published_at)} · ${post.like_count} 赞 · ${post.comment_count} 评论</p>
      <a href="${escapeAttribute(safeExternalUrl(post.source_url))}" target="_blank" rel="noreferrer">打开原帖 <i data-lucide="external-link"></i></a>
      <h4>已采集正文</h4><p class="social-full-text">${escapeHtml(post.content_excerpt || "未取得正文")}</p>
      <h4>AI 判断</h4><p>${status} · ${kinds[post.evidence_kind] || "类型未确定"}</p>
      ${post.analysis_status !== "pending" ? `<p>${escapeHtml(post.rationale || "本次分析未返回理由")}</p><p class="social-review-meta">${escapeHtml(post.model_name)} · ${formatDateTime(post.analyzed_at)}</p>` : ""}
      <ul>${[...(post.exclusion_reasons || []), ...(post.uncertainties || [])].map(text => `<li>${escapeHtml(text)}</li>`).join("")}</ul>
      <h4>图片证据</h4>${post.images.length ? `<div class="social-image-evidence">${post.images.map(image => {
        const ocr = image.recognition || {};
        return `<figure><a href="${escapeAttribute(image.image_url)}" target="_blank" rel="noreferrer"><img src="${escapeAttribute(image.image_url)}" alt="原帖第 ${image.image_index} 张图片" loading="lazy"></a>
          <figcaption>图片 ${image.image_index} · ${ocrLabels[ocr.status] || "待识别"}</figcaption>
          <p>${escapeHtml(ocr.error || "")}</p><details><summary>图片识别文字</summary><div class="social-ocr-lines">${(ocr.lines || []).map(line => `<p>${escapeHtml(line.text)} <small>${Math.round(line.confidence * 100)}%</small></p>`).join("") || "暂无识别文字"}</div></details></figure>`;
      }).join("")}</div>` : `<p>这条记录未保存图片，当前分析仅依据已采集文字。</p>`}`;
  } catch (error) {
    body.innerHTML = socialSignalStatus("triangle-alert", error.message);
  }
  refreshIcons();
}

function socialSignalStatus(icon, message, spinning = false) {
  return `
    <div class="social-signals-status">
      <i data-lucide="${escapeAttribute(icon)}"${spinning ? ' class="spinning"' : ""}></i>
      <span>${escapeHtml(message)}</span>
    </div>
  `;
}

async function loadMarketHotspots({ showFeedback = false } = {}) {
  if (state.marketHotspotsLoading) return;
  state.marketHotspotsLoading = true;
  state.marketHotspotsError = "";
  renderMarketHotspots();

  const params = new URLSearchParams({ history_days: "30", limit: "12" });
  if (state.marketHotspotLifecycle) {
    params.set("lifecycle", state.marketHotspotLifecycle);
  }
  if (state.marketHotspotFundOnly && state.selectedFundCode) {
    params.set("fund_code", state.selectedFundCode);
  }

  try {
    state.marketHotspots = await apiRequest(`${endpoints.marketHotspots}?${params}`);
    if (showFeedback) showToast("热点历史已刷新");
  } catch (error) {
    state.marketHotspots = null;
    state.marketHotspotsError = error.message;
    if (showFeedback) showToast(error.message, "error");
  } finally {
    state.marketHotspotsLoading = false;
    renderMarketHotspots();
  }
}

function renderMarketHotspots() {
  renderMarketFundContext();
  renderOverviewDashboard();
  if (state.marketHotspotsLoading && !state.marketHotspots) {
    elements.marketHotspotsMeta.textContent = "正在比较最近 30 天快照";
    elements.marketHotspots.innerHTML = marketHotspotStatus(
      "loader-circle",
      "正在读取热点历史",
      true,
    );
    refreshIcons();
    return;
  }

  const result = state.marketHotspots;
  if (!result || state.marketHotspotsError) {
    elements.marketHotspotsMeta.textContent = "热点追踪暂不可用";
    elements.marketHotspots.innerHTML = marketHotspotStatus(
      "triangle-alert",
      state.marketHotspotsError || "无法读取热点历史",
    );
    refreshIcons();
    return;
  }

  const fundHint = state.marketHotspotFundOnly && state.selectedFundCode
    ? ` · 当前基金 ${state.selectedFundCode}`
    : "";
  const generatedHint = result.generated_at
    ? ` · 记录于 ${formatDateTime(result.generated_at)}`
    : "";
  elements.marketHotspotsMeta.textContent = result.data_cutoff
    ? `${result.total_hotspot_count} 个热点 · 数据截止 ${formatDateOnly(result.data_cutoff)}${generatedHint}${fundHint}`
    : "尚未建立热点历史";

  if (!result.hotspots.length) {
    const message = result.data_mode === "unavailable"
      ? "请先点击市场事件区的“获取最新市场情报”，建立第一份热点基线"
      : "没有符合当前阶段或基金条件的热点";
    elements.marketHotspots.innerHTML = `
      ${marketHotspotStatus(result.data_mode === "unavailable" ? "history" : "filter-x", message)}
      ${result.data_mode === "unavailable" ? '<a class="secondary-action compact" href="#market/news"><i data-lucide="newspaper"></i>前往市场事件</a>' : ""}
      <footer class="market-hotspot-disclaimer">
        <i data-lucide="info"></i>
        <span>${escapeHtml(result.disclaimer)}</span>
      </footer>
    `;
    refreshIcons();
    return;
  }

  const lifecycleCounts = result.lifecycle_counts || {};
  const activeCount = (lifecycleCounts.emerging || 0) + (lifecycleCounts.rising || 0);
  elements.marketHotspots.innerHTML = `
    <div class="market-hotspot-summary" aria-label="热点追踪摘要">
      <div><span>追踪中</span><strong>${result.total_hotspot_count}</strong></div>
      <div><span>新出现 / 升温</span><strong>${activeCount}</strong></div>
      <div><span>正在降温</span><strong>${lifecycleCounts.cooling || 0}</strong></div>
      <div><span>历史时间点</span><strong>${result.history_point_count}</strong></div>
    </div>
    ${window.FundScopeResearchLists.hotspots(result.hotspots)}
    <footer class="market-hotspot-disclaimer">
      <i data-lucide="info"></i>
      <span>${escapeHtml(result.disclaimer)}</span>
    </footer>
  `;
  refreshIcons();
}

function renderMarketHotspotItem(hotspot) {
  const funds = (hotspot.affected_fund_codes || [])
    .map((code) => {
      const fund = state.funds.find((item) => item.fund_code === code);
      return fund ? `${code} ${fund.short_name}` : code;
    })
    .slice(0, 4);
  const themes = (hotspot.themes || []).slice(0, 5);
  const evidence = (hotspot.evidence || []).slice(0, 6).map((item) => `
    <a href="${escapeAttribute(item.source_url)}" target="_blank" rel="noreferrer">
      <span class="source-grade">${escapeHtml(item.source_level)}</span>
      <strong>${escapeHtml(item.source_name)}</strong>
      <time datetime="${escapeAttribute(item.event_date)}">${formatDateOnly(item.event_date)}</time>
      <i data-lucide="external-link"></i>
    </a>
  `).join("");
  const timeline = (hotspot.timeline || []).map((point, index, points) => {
    const isLatest = index === points.length - 1;
    const score = Math.max(4, Math.min(100, Number(point.heat_score || 0)));
    return `
      <span
        class="market-hotspot-bar${isLatest ? " latest" : ""}"
        style="--hotspot-bar-height:${score}%"
        title="${escapeAttribute(formatDateTime(point.observed_at))} · 热度 ${Number(point.heat_score).toFixed(1)}"
      ></span>
    `;
  }).join("");
  const historyLabel = hotspot.snapshot_count > 1
    ? `${hotspot.snapshot_count} 次快照`
    : "首个基线";

  return `
    <li class="market-hotspot-row" data-lifecycle="${escapeAttribute(hotspot.lifecycle)}">
      <div class="market-hotspot-rank" aria-label="热点排名 ${hotspot.rank}">
        <span>${String(hotspot.rank).padStart(2, "0")}</span>
        <i data-lucide="${hotspot.trend_direction === "rising" ? "trending-up" : hotspot.trend_direction === "falling" ? "trending-down" : "minus"}"></i>
      </div>
      <article class="market-hotspot-content">
        <header>
          <div class="market-hotspot-labels">
            <span class="hotspot-stage" data-stage="${escapeAttribute(hotspot.lifecycle)}">${escapeHtml(hotspot.lifecycle_label)}</span>
            <span>热度 ${Math.round(Number(hotspot.heat_score))}</span>
            <span>${hotspot.member_count} 篇 / ${hotspot.source_count} 来源</span>
            <span>${escapeHtml(formatMarketImpact(hotspot.impact_direction))}</span>
          </div>
          <time datetime="${escapeAttribute(hotspot.event_end)}">${formatDateOnly(hotspot.event_end)}</time>
        </header>
        <h3>${escapeHtml(hotspot.title)}</h3>
        <p>${escapeHtml(hotspot.summary)}</p>
        <div class="market-hotspot-trend">
          <div class="market-hotspot-chart" aria-label="${escapeAttribute(historyLabel)}">
            ${timeline}
          </div>
          <dl>
            <div><dt>较上次</dt><dd data-tone="${hotspotDeltaTone(hotspot.change_since_previous)}">${formatHotspotDelta(hotspot.change_since_previous)}</dd></div>
            <div><dt>近 24 小时</dt><dd data-tone="${hotspotDeltaTone(hotspot.heat_change_24h)}">${formatHotspotDelta(hotspot.heat_change_24h)}</dd></div>
            <div><dt>近 3 日</dt><dd data-tone="${hotspotDeltaTone(hotspot.heat_change_3d)}">${formatHotspotDelta(hotspot.heat_change_3d)}</dd></div>
            <div><dt>趋势可信度</dt><dd>${escapeHtml(formatHotspotConfidence(hotspot.trend_confidence))}</dd></div>
          </dl>
        </div>
        <div class="market-hotspot-reason">
          <i data-lucide="activity"></i>
          <span>${escapeHtml(hotspot.tracking_reason)}</span>
        </div>
        ${(themes.length || funds.length) ? `
          <div class="market-hotspot-relations">
            ${themes.map((theme) => `<span>${escapeHtml(theme)}</span>`).join("")}
            ${funds.map((fund) => `<span data-kind="fund">${escapeHtml(fund)}</span>`).join("")}
          </div>
        ` : ""}
        <footer>
          <span>${escapeHtml(historyLabel)}</span>
          <code>${escapeHtml(hotspot.cluster_id)}</code>
          <details class="market-hotspot-evidence">
            <summary>查看 ${hotspot.evidence.length} 条来源证据</summary>
            <div>${evidence}</div>
          </details>
        </footer>
      </article>
    </li>
  `;
}

function formatHotspotDelta(value) {
  if (value === null || value === undefined || value === "") return "基线不足";
  if (!Number.isFinite(Number(value))) return "基线不足";
  const number = Number(value);
  if (Math.abs(number) < 0.05) return "持平";
  return `${number > 0 ? "+" : ""}${number.toFixed(1)}`;
}

function hotspotDeltaTone(value) {
  if (value === null || value === undefined || value === "") return "flat";
  if (!Number.isFinite(Number(value)) || Math.abs(Number(value)) < 0.05) return "flat";
  return Number(value) > 0 ? "rising" : "falling";
}

function formatHotspotConfidence(value) {
  return { high: "高", medium: "中", low: "低" }[value] || "待积累";
}

function marketHotspotStatus(icon, message, spinning = false) {
  return `
    <div class="market-hotspot-status">
      <i data-lucide="${escapeAttribute(icon)}"${spinning ? ' class="spinning"' : ""}></i>
      <span>${escapeHtml(message)}</span>
    </div>
  `;
}

async function loadMarketEvents({ showFeedback = false } = {}) {
  if (state.marketEventsLoading) return;
  state.marketEventsLoading = true;
  state.marketEventsError = "";
  renderMarketEvents();

  // URLSearchParams 会正确编码中文关键词，后端再执行透明的关键词过滤。
  const params = new URLSearchParams({ limit: "20" });
  if (state.marketEventQuery) params.set("q", state.marketEventQuery);
  if (state.marketEventType) params.set("event_type", state.marketEventType);
  if (state.marketEventFundOnly && state.selectedFundCode) {
    params.set("fund_code", state.selectedFundCode);
  }

  try {
    state.marketEvents = await apiRequest(`${endpoints.marketEvents}?${params}`);
    if (showFeedback) showToast("市场事件已刷新");
  } catch (error) {
    state.marketEvents = null;
    state.marketEventsError = error.message;
    if (showFeedback) showToast(error.message, "error");
  } finally {
    state.marketEventsLoading = false;
    renderMarketEvents();
  }
}

async function loadMarketTopicCatalog() {
  try {
    state.marketTopicCatalog = await apiRequest(endpoints.marketEventTopics);
  } catch (_error) {
    // 后端暂不可用时仍保留预设名称；真正提交刷新时 API 会给出权威错误。
    state.marketTopicCatalog = {
      data_kind: "market_topic_catalog",
      max_selected_topics: 3,
      model_expansion_default: true,
      topics: fallbackMarketTopics.map((topic) => ({
        topic,
        dictionary_terms: [topic],
        cached_model_terms: [],
        cached_at: null,
      })),
    };
  }
  renderMarketTopicConfiguration();
}

function renderMarketTopicConfiguration() {
  const catalog = state.marketTopicCatalog;
  const options = catalog?.topics || [];
  const selected = new Set(state.marketSelectedTopics);
  const presetNames = new Set(options.map((item) => item.topic));
  const customTopics = state.marketSelectedTopics.filter(
    (topic) => !presetNames.has(topic),
  );
  const maxTopics = catalog?.max_selected_topics || 3;

  elements.marketTopicTriggerLabel.textContent = state.marketSelectedTopics.length
    ? state.marketSelectedTopics.length === 1
      ? state.marketSelectedTopics[0]
      : `${state.marketSelectedTopics.length} 个板块`
    : "全市场";
  elements.marketTopicLimit.textContent = `最多选择 ${maxTopics} 个板块`;
  elements.marketTopicUseModel.checked = state.marketUseModelTopicExpansion;
  elements.marketTopicCustomInput.value = customTopics.join("、");
  elements.marketTopicOptions.innerHTML = options.map((item) => {
    const cachedCount = item.cached_model_terms?.length || 0;
    const termCount = item.dictionary_terms?.length || 1;
    const meta = cachedCount ? `AI 缓存 ${cachedCount} 词` : `词典 ${termCount} 词`;
    const terms = [
      ...(item.dictionary_terms || []),
      ...(item.cached_model_terms || []),
    ].join("、");
    return `
      <label class="market-topic-option" title="${escapeAttribute(terms)}">
        <input
          type="checkbox"
          name="market-topic"
          value="${escapeAttribute(item.topic)}"
          ${selected.has(item.topic) ? "checked" : ""}
        />
        <span>${escapeHtml(item.topic)}</span>
        <small>${escapeHtml(meta)}</small>
      </label>
    `;
  }).join("");
}

function saveMarketTopicConfiguration(event) {
  event.preventDefault();
  const form = new FormData(elements.marketTopicForm);
  const presetTopics = form.getAll("market-topic").map((topic) => String(topic));
  const customTopics = elements.marketTopicCustomInput.value
    .split(/[,，、\n]/)
    .map((topic) => topic.trim())
    .filter(Boolean);
  const topics = [];
  const seen = new Set();
  [...presetTopics, ...customTopics].forEach((topic) => {
    const key = topic.toLocaleLowerCase();
    if (!seen.has(key)) {
      topics.push(topic);
      seen.add(key);
    }
  });

  const maxTopics = state.marketTopicCatalog?.max_selected_topics || 3;
  if (topics.length > maxTopics) {
    showToast(`一次最多选择 ${maxTopics} 个板块`, "error");
    return;
  }
  const invalidTopic = topics.find((topic) => topic.length > 20);
  if (invalidTopic) {
    showToast(`“${invalidTopic}”超过 20 个字符`, "error");
    return;
  }

  applyMarketTopicConfiguration(topics, elements.marketTopicUseModel.checked);
  elements.marketTopicPopover.hidePopover();
  showToast(topics.length ? `已关注：${topics.join("、")}` : "已切换为全市场采集");
}

function applyMarketTopicConfiguration(topics, useModel) {
  state.marketSelectedTopics = [...topics];
  state.marketUseModelTopicExpansion = Boolean(useModel);
  localStorage.setItem(
    "fundscope-market-topics",
    JSON.stringify(state.marketSelectedTopics),
  );
  localStorage.setItem(
    "fundscope-market-topic-model",
    String(state.marketUseModelTopicExpansion),
  );
  renderMarketTopicConfiguration();
}

async function syncMarketEvents() {
  // 页面只提交一个总 Job；采集、语义分析、聚类和组合监控都发生在 Worker 中。
  if (state.marketEventSyncing) return;
  state.marketEventSyncing = true;
  setMarketEventSyncButtonBusy(true);

  try {
    const accepted = await apiRequest(endpoints.marketIntelligencePipeline, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        user_id: state.userId,
        topics: state.marketSelectedTopics,
        use_model_topic_expansion: state.marketUseModelTopicExpansion,
      }),
    });
    state.marketPipelineJob = accepted.job;
    renderMarketPipelineStatus();
    showToast("市场情报流水线已提交");
    const completedJob = await waitForBackgroundJob(
      accepted.job.id,
      state.userId,
      (job) => {
        state.marketPipelineJob = job;
        renderMarketPipelineStatus();
      },
      // 后端允许流水线运行 15 分钟；前端多留 1 分钟用于排队和最后一次轮询。
      16 * 60 * 1000,
    );
    if (completedJob.status !== "completed") {
      throw new ApiError(
        completedJob.error || `情报任务${formatJobStatus(completedJob.status)}`,
        503,
      );
    }

    // 流水线可能已经产生组合快照或预警，因此事件、研判和监控视图一起刷新。
    const refreshes = [
      loadMarketEvents(),
      loadTerminalMarketEvents(),
      loadTerminalFundEvents(state.selectedFundCode),
      loadMarketHotspots(),
      loadSocialSignals(),
      loadPortfolioAssessment(),
    ];
    if (state.selectedPortfolioId) {
      refreshes.push(loadPortfolioEventImpacts(), loadPortfolioMonitoring());
    }
    if (state.selectedFundCode) {
      refreshes.push(
        selectFund(state.selectedFundCode, { closeMobile: false, force: true }),
      );
    }
    await Promise.allSettled(refreshes);
    showToast(completedJob.result?.summary || "市场情报流水线完成");
  } catch (error) {
    showToast(error.message, "error");
  } finally {
    state.marketEventSyncing = false;
    setMarketEventSyncButtonBusy(false);
  }
}

async function analyzeMarketEvents() {
  // AI 分析与数据采集分开触发：先保存客观原文，再按需生成可替换的模型观点。
  if (state.marketEventAnalyzing) return;
  state.marketEventAnalyzing = true;
  setMarketEventAnalysisButtonBusy(true);

  try {
    const accepted = await apiRequest(endpoints.marketEventAnalysis, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ user_id: state.userId }),
    });
    showToast("事件语义分析任务已提交");
    const completedJob = await waitForBackgroundJob(accepted.job.id, state.userId);
    if (completedJob.status !== "completed") {
      throw new ApiError(
        completedJob.error || `分析任务${formatJobStatus(completedJob.status)}`,
        503,
      );
    }

    // 通过校验的语义结果会进入证据融合，所以三个相关视图都要重新读取。
    const refreshes = [loadMarketEvents(), loadTerminalMarketEvents(), loadTerminalFundEvents(state.selectedFundCode), loadPortfolioAssessment()];
    if (state.selectedPortfolioId) refreshes.push(loadPortfolioEventImpacts());
    if (state.selectedFundCode) {
      refreshes.push(
        selectFund(state.selectedFundCode, { closeMobile: false, force: true }),
      );
    }
    await Promise.allSettled(refreshes);
    showToast(completedJob.result?.summary || "事件语义分析完成");
  } catch (error) {
    showToast(error.message, "error");
  } finally {
    state.marketEventAnalyzing = false;
    setMarketEventAnalysisButtonBusy(false);
  }
}

async function clusterMarketEvents() {
  // 聚类和语义研判是两条独立流水线：这里调用 Qwen Embedding，不调用 DeepSeek。
  if (state.marketEventClustering) return;
  state.marketEventClustering = true;
  setMarketEventClusterButtonBusy(true);

  try {
    const accepted = await apiRequest(endpoints.marketEventClustering, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ user_id: state.userId }),
    });
    showToast("相似报道聚合任务已提交");
    const completedJob = await waitForBackgroundJob(accepted.job.id, state.userId);
    if (completedJob.status !== "completed") {
      throw new ApiError(
        completedJob.error || `聚合任务${formatJobStatus(completedJob.status)}`,
        503,
      );
    }

    // 聚类同时保存热点快照，所以事件、热点、基金研判和组合研判一起刷新。
    const refreshes = [
      loadMarketEvents(),
      loadTerminalMarketEvents(),
      loadTerminalFundEvents(state.selectedFundCode),
      loadMarketHotspots(),
      loadPortfolioAssessment(),
    ];
    if (state.selectedPortfolioId) refreshes.push(loadPortfolioEventImpacts());
    if (state.selectedFundCode) {
      refreshes.push(
        selectFund(state.selectedFundCode, { closeMobile: false, force: true }),
      );
    }
    await Promise.allSettled(refreshes);
    showToast(completedJob.result?.summary || "相似报道聚合完成");
  } catch (error) {
    showToast(error.message, "error");
  } finally {
    state.marketEventClustering = false;
    setMarketEventClusterButtonBusy(false);
  }
}

function setMarketEventSyncButtonBusy(isBusy) {
  elements.syncMarketEventsButton.disabled = isBusy;
  elements.syncMarketEventsButton.title = isBusy
    ? "正在获取最新市场情报"
    : "获取最新市场情报";
  elements.syncMarketEventsButton.setAttribute(
    "aria-label",
    elements.syncMarketEventsButton.title,
  );
  const icon = elements.syncMarketEventsButton.querySelector("svg, i");
  if (icon) icon.classList.toggle("spinning", isBusy);
}

function renderMarketPipelineStatus() {
  const job = state.marketPipelineJob;
  if (!job) {
    elements.marketPipelineStatus.hidden = true;
    elements.marketPipelineStatus.innerHTML = "";
    return;
  }

  const active = ["queued", "running"].includes(job.status);
  const failed = ["failed", "cancelled"].includes(job.status);
  const progress = Math.max(0, Math.min(100, Number(job.progress_percent || 0)));
  const eventSync = job.result?.stages?.event_sync || null;
  const expansion = eventSync?.keyword_expansion || null;
  const collectionKeywords = expansion?.keywords || [];
  const expansionWarning = expansion?.warnings?.[0] || "";
  elements.marketPipelineStatus.hidden = false;
  elements.marketPipelineStatus.dataset.status = failed
    ? "failed"
    : active
      ? "running"
      : "completed";
  elements.marketPipelineStatus.innerHTML = `
    <div>
      <i data-lucide="${failed ? "triangle-alert" : active ? "loader-circle" : "circle-check"}" class="${active ? "spinning" : ""}"></i>
      <span>${escapeHtml(job.progress_message || "市场情报流水线准备运行")}</span>
      <strong>${progress.toFixed(0)}%</strong>
    </div>
    <span class="market-pipeline-progress"><i style="width:${progress}%"></i></span>
    ${collectionKeywords.length ? `
      <details class="market-pipeline-keywords">
        <summary>
          ${escapeHtml((expansion.topics || []).join("、"))}
          <span>${collectionKeywords.length} 个采集词</span>
        </summary>
        <p>${collectionKeywords.map((keyword) => `<span>${escapeHtml(keyword)}</span>`).join("")}</p>
        ${expansionWarning ? `<small>${escapeHtml(expansionWarning)}</small>` : ""}
      </details>
    ` : ""}
  `;
  refreshIcons();
}

function setMarketEventAnalysisButtonBusy(isBusy) {
  elements.analyzeMarketEventsButton.disabled = isBusy;
  elements.analyzeMarketEventsButton.title = isBusy
    ? "DeepSeek 正在分析市场事件"
    : "使用 DeepSeek 分析待处理事件";
  elements.analyzeMarketEventsButton.setAttribute(
    "aria-label",
    elements.analyzeMarketEventsButton.title,
  );
  const icon = elements.analyzeMarketEventsButton.querySelector("svg, i");
  if (icon) icon.classList.toggle("spinning", isBusy);
}

function setMarketEventClusterButtonBusy(isBusy) {
  elements.clusterMarketEventsButton.disabled = isBusy;
  elements.clusterMarketEventsButton.title = isBusy
    ? "Qwen 正在聚合相似报道"
    : "使用 Embedding 聚合相似报道";
  elements.clusterMarketEventsButton.setAttribute(
    "aria-label",
    elements.clusterMarketEventsButton.title,
  );
  const icon = elements.clusterMarketEventsButton.querySelector("svg, i");
  if (icon) icon.classList.toggle("spinning", isBusy);
}

function renderMarketEvents() {
  renderMarketFundContext();
  renderOverviewDashboard();
  if (state.marketEventsLoading) {
    elements.marketEventsMeta.textContent = "正在查询事件快照";
    elements.marketEvents.innerHTML = marketEventStatus(
      "loader-circle",
      "正在读取市场事件",
      true,
    );
    refreshIcons();
    return;
  }

  const result = state.marketEvents;
  if (!result || state.marketEventsError) {
    elements.marketEventsMeta.textContent = "事件数据暂不可用";
    elements.marketEvents.innerHTML = marketEventStatus(
      "triangle-alert",
      state.marketEventsError || "市场事件暂不可用",
    );
    refreshIcons();
    return;
  }

  const filterHint = state.marketEventFundOnly && state.selectedFundCode
    ? ` · 当前基金 ${state.selectedFundCode}`
    : "";
  const dataModeLabel = result.data_mode === "database"
    ? "数据库事件"
    : "本地固定快照";
  const semanticHint = result.data_mode === "database"
    ? ` · AI复核 ${result.semantic_accepted_count}/${result.total_matched}`
    : "";
  const activeEventCount = result.fresh_event_count + result.decaying_event_count;
  const freshnessHint = ` · 有效 ${activeEventCount}/${result.total_matched}`;
  const clusterHint = result.clustered_event_count
    ? ` · ${result.total_matched}篇报道/${result.cluster_count}个事件/${result.source_count}个来源`
    : " · 尚未聚合报道";
  const updatedHint = result.data_updated_at
    ? ` · 更新于 ${formatDateTime(result.data_updated_at)}`
    : "";
  elements.marketEventsMeta.textContent = `${dataModeLabel} · 数据截止 ${formatDateOnly(result.data_cutoff)}${updatedHint}${filterHint}`;

  if (!result.events.length) {
    elements.marketEvents.innerHTML = marketEventStatus(
      "search-x",
      "没有符合当前条件的事件",
    );
    refreshIcons();
    return;
  }

  const displayEvents = collapseMarketEventClusters(result.events);
  elements.marketEvents.innerHTML = `
    ${window.FundScopeResearchLists.events(displayEvents, {windowDays: state.marketEventWindow})}
    <details class="dv-detail"><summary>采集覆盖与研判状态</summary><p>${escapeHtml(`${clusterHint}${semanticHint}${freshnessHint}`)}</p></details>
    <footer class="market-event-disclaimer">
      <i data-lucide="info"></i>
      <span>${escapeHtml(result.disclaimer)}</span>
    </footer>
  `;
  refreshIcons();
}

function collapseMarketEventClusters(events) {
  // 一个簇在时间线只占一个位置，但把当前查询范围内的原始成员留给 details 追溯。
  const groups = new Map();
  events.forEach((event) => {
    const key = event.cluster?.cluster_id || `raw:${event.event_id}`;
    const existing = groups.get(key);
    if (!existing) {
      groups.set(key, { representative: event, members: [event] });
      return;
    }
    existing.members.push(event);
    if (event.cluster?.is_representative) existing.representative = event;
  });
  return [...groups.values()].map(({ representative, members }) => ({
    ...representative,
    cluster_members: members,
  }));
}

function renderMarketEventItem(event) {
  const semantic = event.semantic_analysis;
  const semanticStatus = semantic?.status || "pending";
  const semanticLabel = semanticStatus === "accepted"
    ? `AI复核 ${Math.round(Number(semantic.confidence) * 100)}%`
    : semanticStatus === "abstained"
      ? "AI弃权"
      : "待AI复核";
  const freshnessLabel = formatEventFreshness(
    event.freshness_status,
    event.decay_factor,
  );
  const affectedFunds = event.affected_fund_codes
    .map((code) => {
      const fund = state.funds.find((item) => item.fund_code === code);
      return fund ? `${code} ${fund.short_name}` : code;
    })
    .join(" · ");
  const cluster = event.cluster;
  const heatLabel = cluster
    ? `<span class="heat-label" data-level="${escapeAttribute(cluster.heat_level)}" title="来源数量、来源等级与时效性共同计算">热度 ${Math.round(Number(cluster.heat_score))} · ${cluster.member_count}篇/${cluster.source_count}来源</span>`
    : "";
  const clusterMembers = event.cluster_members || [event];
  const sourceLinks = [...new Map(
    clusterMembers.map((item) => [item.source_url, item]),
  ).values()]
    .map((item) => `<a href="${escapeAttribute(item.source_url)}" target="_blank" rel="noreferrer">${escapeHtml(item.source_name)}<i data-lucide="external-link"></i></a>`)
    .join("");
  const clusterSources = cluster && cluster.member_count > 1
    ? `
      <details class="market-event-sources">
        <summary>查看同一事件的 ${cluster.member_count} 篇原始报道</summary>
        <div>${sourceLinks}</div>
      </details>
    `
    : "";

  return `
    <li class="market-event-item" data-direction="${escapeAttribute(event.impact_direction)}">
      <time datetime="${escapeAttribute(event.event_date)}">${formatDateOnly(event.event_date)}</time>
      <span class="market-event-rail" aria-hidden="true"><span></span></span>
      <article class="market-event-content">
        <div class="market-event-labels">
          <span>${escapeHtml(formatMarketEventType(event.event_type))}</span>
          <span class="source-grade" title="${escapeAttribute(event.source_grade_reason)}">来源 ${escapeHtml(event.source_level)}</span>
          <span class="impact-label">${escapeHtml(formatMarketImpact(event.impact_direction))} · ${escapeHtml(formatImpactHorizon(event.impact_horizon))}</span>
          <span class="semantic-label" data-status="${escapeAttribute(semanticStatus)}">${escapeHtml(semanticLabel)}</span>
          ${heatLabel}
          <span
            class="freshness-label"
            data-status="${escapeAttribute(event.freshness_status)}"
            title="有效至 ${escapeAttribute(formatDateOnly(event.valid_until))}"
          >${escapeHtml(freshnessLabel)}</span>
        </div>
        <h3>${escapeHtml(event.title)}</h3>
        <p>${escapeHtml(event.summary)}</p>
        <div class="market-event-impact">
          <strong>${semanticStatus === "accepted" ? "AI语义研判" : "影响观察"}</strong>
          <span>${escapeHtml(event.impact_reason)}</span>
        </div>
        ${clusterSources}
        <footer>
          <span>${escapeHtml(affectedFunds || "全市场")}</span>
          <span class="market-event-id">${escapeHtml(event.event_id)}</span>
          <a href="${escapeAttribute(event.source_url)}" target="_blank" rel="noreferrer">
            ${escapeHtml(event.source_name)}
            <i data-lucide="external-link"></i>
          </a>
        </footer>
      </article>
    </li>
  `;
}

function marketEventStatus(icon, message, spinning = false) {
  return `
    <div class="market-event-status">
      <i data-lucide="${escapeAttribute(icon)}"${spinning ? ' class="spinning"' : ""}></i>
      <span>${escapeHtml(message)}</span>
    </div>
  `;
}

async function loadFunds({ showFeedback = false } = {}) {
  if (state.fundLoading) return;
  state.fundLoading = true;
  hideFundError();
  if (state.activeView === "overview") {
    elements.fundHeaderMeta.textContent = "正在读取数据快照";
  }

  try {
    const catalog = await apiRequest(endpoints.funds);
    state.fundCatalog = catalog;
    state.funds = catalog.funds;

    // 组合接口可能先返回；基金目录就绪后才能生成完整的默认权重表单。
    if (
      !state.portfolioDraft
      || (
        !state.selectedPortfolioId
        && !Object.keys(state.portfolioDraft.holdings || {}).length
      )
    ) {
      startNewPortfolioDraft({ render: false });
    }
    renderPortfolioWorkbench();

    // 事件可能比基金列表更早返回；基金资料到达后补上“代码 + 简称”。
    if (state.marketEvents) renderMarketEvents();

    renderFundOverview();
    renderFundNavigation();
    renderFundCards();

    // 优先恢复用户上次查看的基金；第一次进入时默认选择列表第一项。
    const rememberedExists = state.funds.some(
      (fund) => fund.fund_code === state.selectedFundCode,
    );
    const targetCode = rememberedExists
      ? state.selectedFundCode
      : state.funds[0]?.fund_code;
    if (targetCode) {
      await selectFund(targetCode, { closeMobile: false, force: true });
    }
    if (showFeedback) showToast("基金快照已刷新");
  } catch (error) {
    showFundError(error.message);
    if (state.activeView === "overview") {
      elements.fundHeaderMeta.textContent = "基金数据不可用";
    }
    elements.fundNavList.innerHTML = fundEmptyState("列表读取失败");
    elements.fundCardGrid.innerHTML = fundEmptyState("暂时无法显示基金");
    elements.fundDetail.innerHTML = fundEmptyState("基金详情暂不可用");
    if (showFeedback) showToast(error.message, "error");
  } finally {
    state.fundLoading = false;
    refreshIcons();
  }
}

function portfolioStorageKey() {
  return `fundscope-selected-portfolio:${state.userId}`;
}

function resetPortfolioResearch() {
  // requestId 让旧组合的迟到 SSE 事件失效，避免覆盖用户刚切换的新组合。
  state.portfolioResearchRequestId += 1;
  state.portfolioReportLoading = false;
  state.portfolioReportReadJobId = "";
  state.portfolioResearchHistoryRequestId += 1;
  state.portfolioResearchJob = null;
  state.portfolioResearchResult = null;
  state.portfolioResearchRunning = false;
  state.portfolioResearchMode = "standard";
  state.portfolioResearchDraft = {question: "", allow_public_refresh: false, investor_context: {}};
  state.portfolioResearchError = "";
  state.portfolioResearchHistory = [];
  state.portfolioResearchHistoryLoading = false;
  state.portfolioResearchHistoryError = "";
  state.portfolioResearchHistoryNotice = "";
  state.portfolioResearchComparison = null;
  state.portfolioResearchComparisonLoading = false;
  state.portfolioResearchComparisonError = "";
  state.portfolioAdjustmentRequestId += 1;
  state.portfolioAdjustmentJob = null;
  state.portfolioAdjustmentResult = null;
  state.portfolioAdjustmentRunning = false;
  state.portfolioAdjustmentError = "";
}

function resetPortfolioMonitoring() {
  // 切换组合后让旧请求失效；Cron 本身仍在后端持续运行，不受页面状态影响。
  state.portfolioMonitoringRequestId += 1;
  state.portfolioMonitoring = null;
  state.portfolioMonitoringJob = null;
  state.portfolioMonitoringLoading = false;
  state.portfolioMonitoringSaving = false;
  state.portfolioMonitoringRunning = false;
  state.portfolioMonitoringError = "";
}

function resetPortfolioTransactions() {
  state.portfolioTransactions = [];
  state.portfolioTradeOrders = [];
  state.portfolioTransactionsRequestId += 1;
  state.portfolioTransactionsLoading = false;
  state.portfolioTransactionError = "";
}

function resetPortfolioExposure() {
  // requestId 让已经切走的组合响应失效，避免旧季报覆盖新组合界面。
}

function resetPortfolioEventImpacts() {
  // 归因结果和组合 ID 强绑定；切换组合后任何迟到响应都必须失效。
  state.portfolioEventImpactsRequestId += 1;
  state.portfolioEventImpacts = null;
  state.portfolioEventImpactsLoading = false;
  state.portfolioEventImpactsError = "";
}

function resetPortfolioPerformance() {
  // 组合切换或交易变化后，旧曲线必须立即失效，不能继续显示上一份组合的收益。
  state.portfolioPerformanceRequestId += 1;
  state.portfolioPerformance = null;
  state.portfolioPerformanceLoading = false;
  state.portfolioPerformanceError = "";
}

function resetPortfolioDecisions() {
  state.portfolioDecisionRequestId += 1;
  state.portfolioDecisions = [];
  state.portfolioDecisionsLoading = false;
  state.portfolioDecisionSaving = false;
  state.portfolioDecisionError = "";
}

function localIsoDate() {
  const now = new Date();
  const localTime = new Date(now.getTime() - now.getTimezoneOffset() * 60_000);
  return localTime.toISOString().slice(0, 10);
}

function defaultPortfolioDraft() {
  const firstFund = state.funds[0];
  const holdings = firstFund
    ? {
        [firstFund.fund_code]: {
          units: "",
          cost_amount: "",
          purchase_date: localIsoDate(),
        },
      }
    : {};
  return { mode: "actual", name: "我的基金组合", holdings };
}

function startNewPortfolioDraft({ render = true } = {}) {
  portfolioPageReads.clear();
  // 主动新建不是普通数据更新：即便仍在“未保存组合”，也必须创建全新的表单实例。
  state.portfolioDraftVersion += 1;
  resetPortfolioResearch();
  resetPortfolioMonitoring();
  resetPortfolioTransactions();
  resetPortfolioExposure();
  resetPortfolioEventImpacts();
  resetPortfolioPerformance();
  resetPortfolioDecisions();
  state.selectedPortfolioId = null;
  state.portfolioDetail = null;
  state.portfolioAssessment = null;
  state.portfolioAssessmentError = "";
  state.portfolioAssessmentLoading = false;
  state.portfolioAssessmentRequestId += 1;
  state.agentContextMode = "fund";
  state.portfolioDraft = defaultPortfolioDraft();
  localStorage.removeItem(portfolioStorageKey());
  renderAgentFundContext();
  if (render) {
    renderPortfolioWorkbench();
    activateProductView("portfolio", { page: "holdings" });
  }
}

async function loadPortfolios({ preferredPortfolioId = null, showFeedback = false } = {}) {
  if (state.portfolioLoading) return;
  portfolioPageReads.clear();
  resetPortfolioResearch();
  resetPortfolioMonitoring();
  resetPortfolioTransactions();
  resetPortfolioExposure();
  resetPortfolioEventImpacts();
  resetPortfolioPerformance();
  resetPortfolioDecisions();
  state.portfolioLoading = true;
  state.portfolioError = "";
  renderPortfolioWorkbench();

  try {
    const params = new URLSearchParams({ user_id: state.userId });
    const result = await apiRequest(`${endpoints.portfolios}?${params}`);
    state.portfolios = result.portfolios;

    const remembered = preferredPortfolioId || localStorage.getItem(portfolioStorageKey());
    const selected = state.portfolios.find((item) => item.id === remembered)
      || state.portfolios[0]
      || null;
    if (selected) {
      state.selectedPortfolioId = selected.id;
      state.portfolioDetail = selected;
      state.portfolioDraft = portfolioDraftFromDetail(selected);
      localStorage.setItem(portfolioStorageKey(), selected.id);
      if (preferredPortfolioId) state.agentContextMode = "portfolio";
    } else {
      startNewPortfolioDraft({ render: false });
    }
    if (showFeedback) showToast("基金组合已刷新");
  } catch (error) {
    state.portfolios = [];
    state.portfolioError = error.message;
    if (!state.portfolioDraft) state.portfolioDraft = defaultPortfolioDraft();
    if (showFeedback) showToast(error.message, "error");
  } finally {
    state.portfolioLoading = false;
    renderAgentFundContext();
    renderPortfolioWorkbench();
  }

  if (state.activeView === "portfolio") await loadPortfolioPage();
}

function portfolioDraftFromDetail(portfolio) {
  if (portfolio.position_mode === "actual") {
    return {
      mode: "actual",
      name: portfolio.name,
      holdings: Object.fromEntries(
        portfolio.holdings.map((holding) => [holding.fund_code, {
          units: holding.units,
          cost_amount: holding.cost_amount,
          purchase_date: holding.first_purchase_date,
        }]),
      ),
    };
  }
  // 旧比例只能告诉我们曾选过哪些基金，不能换算成真实份额或成本。
  // 保持原组合 ID，用户补全并保存后才由后端原子转换为真实账本。
  return {
    mode: "actual",
    name: portfolio.name,
    holdings: Object.fromEntries(
      portfolio.holdings.map((holding) => [holding.fund_code, {
        units: "", cost_amount: "", purchase_date: "",
      }]),
    ),
  };
}

async function selectPortfolio(portfolioId) {
  if (!portfolioId) {
    startNewPortfolioDraft();
    return;
  }
  const portfolio = state.portfolios.find((item) => item.id === portfolioId);
  if (!portfolio) return;

  portfolioPageReads.clear();
  resetPortfolioResearch();
  resetPortfolioMonitoring();
  resetPortfolioTransactions();
  resetPortfolioExposure();
  resetPortfolioEventImpacts();
  resetPortfolioPerformance();
  resetPortfolioDecisions();
  state.selectedPortfolioId = portfolio.id;
  state.portfolioDetail = portfolio;
  state.portfolioDraft = portfolioDraftFromDetail(portfolio);
  state.portfolioAssessment = null;
  state.portfolioAssessmentError = "";
  state.agentContextMode = "portfolio";
  localStorage.setItem(portfolioStorageKey(), portfolio.id);
  renderAgentFundContext();
  renderPortfolioWorkbench();
  await loadPortfolioPage();
}

function activePortfolioDraftForm() {
  // 页面会保留已挂载的隐藏 DOM；只读取当前页，避免隐藏表单覆盖正在输入的份额。
  return elements.portfolioWorkbench.querySelector(
    `#portfolio-page-${state.workspacePages.portfolio} [data-portfolio-form]`,
  );
}

function updatePortfolioDraftFromForm(form = activePortfolioDraftForm()) {
  const nameInput = form?.querySelector("[data-portfolio-name]");
  if (!nameInput) return;

  const holdings = {};
  form.querySelectorAll("[data-position-row]").forEach((row) => {
    const fundCode = row.dataset.positionRow;
    holdings[fundCode] = {
      units: row.querySelector("[data-position-units]")?.value || "",
      cost_amount: row.querySelector("[data-position-cost]")?.value || "",
      purchase_date: row.querySelector("[data-position-date]")?.value || "",
    };
  });
  state.portfolioDraft = { mode: "actual", name: nameInput.value, holdings };
  updatePortfolioDraftStatus();
}

function portfolioDraftSummary() {
  const draft = state.portfolioDraft || defaultPortfolioDraft();
  const positions = Object.values(draft.holdings || {});
  // 已添加的每一行都要完整，不能把漏填的基金悄悄过滤掉。
  const completedPositions = positions.filter((position) => (
    Number.isFinite(Number(position.units)) && Number(position.units) > 0
    && Number.isFinite(Number(position.cost_amount)) && Number(position.cost_amount) > 0
    && Boolean(position.purchase_date)
    && position.purchase_date <= localIsoDate()
  ));
  return {
    mode: "actual",
    name: draft.name.trim(),
    holdingCount: completedPositions.length,
    invalidCount: positions.length - completedPositions.length,
    totalCost: completedPositions.reduce(
      (sum, position) => sum + Number(position.cost_amount),
      0,
    ),
  };
}

function updatePortfolioDraftStatus() {
  const form = activePortfolioDraftForm();
  const status = form?.querySelector("[data-portfolio-total]");
  const saveButton = form?.querySelector("[data-save-portfolio]");
  if (!status || !saveButton) return;

  const summary = portfolioDraftSummary();
  const valid = summary.name && summary.holdingCount > 0 && summary.invalidCount === 0;
  status.textContent = formatMoney(summary.totalCost);
  status.dataset.valid = String(Boolean(valid));
  saveButton.disabled = !valid || state.portfolioSaving;
}

function addFundToPortfolioDraft() {
  const select = activePortfolioDraftForm()?.querySelector("[data-position-fund-picker]");
  const fundCode = select?.value;
  if (!fundCode || !state.portfolioDraft) return;
  updatePortfolioDraftFromForm();
  state.portfolioDraft.holdings[fundCode] = {
    units: "",
    cost_amount: "",
    purchase_date: localIsoDate(),
  };
  renderPortfolioWorkbench({pages: [state.workspacePages.portfolio]});
}

function removeFundFromPortfolioDraft(fundCode) {
  if (!state.portfolioDraft?.holdings?.[fundCode]) return;
  updatePortfolioDraftFromForm();
  delete state.portfolioDraft.holdings[fundCode];
  renderPortfolioWorkbench({pages: [state.workspacePages.portfolio]});
}

async function savePortfolio(event) {
  event.preventDefault();
  if (state.portfolioSaving) return;
  updatePortfolioDraftFromForm(event.target);
  const summary = portfolioDraftSummary();
  const valid = summary.name && summary.holdingCount > 0 && summary.invalidCount === 0;
  if (!valid) {
    showToast("请填写组合名称及每只基金的份额、成本和买入日期；不持有的基金请移除", "error");
    return;
  }

  const payload = {
    user_id: state.userId,
    name: summary.name,
    holdings: Object.entries(state.portfolioDraft.holdings)
      .map(([fundCode, position]) => ({
        fund_code: fundCode,
        units: Number(position.units),
        cost_amount: Number(position.cost_amount),
        purchase_date: position.purchase_date,
      })),
  };
  const editing = Boolean(state.selectedPortfolioId);
  const url = editing
    ? `${endpoints.portfolios}/${encodeURIComponent(state.selectedPortfolioId)}`
    : endpoints.portfolios;

  state.portfolioSaving = true;
  state.portfolioError = "";
  renderPortfolioWorkbench({pages: ["holdings", "configuration"]});
  try {
    const saved = await apiRequest(url, {
      method: editing ? "PUT" : "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    state.portfolioSaving = false;
    showToast(editing ? "已转为真实持仓账本" : "真实持仓已创建");
    await loadPortfolios({ preferredPortfolioId: saved.id });
    activateProductView("portfolio", {page: "holdings"});
  } catch (error) {
    state.portfolioSaving = false;
    state.portfolioError = error.message;
    renderPortfolioWorkbench({pages: ["holdings", "configuration"]});
    showToast(error.message, "error");
  }
}

async function deleteSelectedPortfolio() {
  if (!state.selectedPortfolioId || !state.portfolioDetail) return;
  const confirmed = window.confirm(`删除“${state.portfolioDetail.name}”？`);
  if (!confirmed) return;

  try {
    const params = new URLSearchParams({ user_id: state.userId });
    await apiRequest(
      `${endpoints.portfolios}/${encodeURIComponent(state.selectedPortfolioId)}?${params}`,
      { method: "DELETE" },
    );
    showToast("基金组合已删除");
    startNewPortfolioDraft({ render: false });
    await loadPortfolios();
  } catch (error) {
    showToast(error.message, "error");
  }
}

function portfolioTransactionsEndpoint(portfolioId) {
  return `${endpoints.portfolios}/${encodeURIComponent(portfolioId)}/transactions`;
}

async function loadPortfolioTransactions() {
  if (!state.selectedPortfolioId || state.portfolioDetail?.position_mode !== "actual") return;
  const portfolioId = state.selectedPortfolioId;
  const userId = state.userId;
  const requestId = ++state.portfolioTransactionsRequestId;
  const current = () => state.selectedPortfolioId === portfolioId && state.userId === userId && requestId === state.portfolioTransactionsRequestId;
  state.portfolioTransactionsLoading = true;
  state.portfolioTransactionError = "";
  renderPortfolioWorkbench({pages: ["holdings"]});
  try {
    const params = new URLSearchParams({ user_id: userId });
    const [result, orders] = await Promise.all([
      apiRequest(`${portfolioTransactionsEndpoint(portfolioId)}?${params}`),
      apiRequest(`${endpoints.portfolios}/${encodeURIComponent(portfolioId)}/trade-orders?${params}`),
    ]);
    if (!current()) return;
    state.portfolioTransactions = result.transactions;
    state.portfolioTradeOrders = orders.orders;
  } catch (error) {
    if (!current()) return;
    state.portfolioTransactions = [];
    state.portfolioTradeOrders = [];
    state.portfolioTransactionError = error.message;
  } finally {
    if (current()) {
      state.portfolioTransactionsLoading = false;
      renderPortfolioWorkbench({pages: ["holdings"]});
    }
  }
}

// 独立记账模块只接收所属组合的数据和明确的回调，迟到响应不能切回旧账户。
function bookkeepingContext(portfolio = state.portfolioDetail) {
  const userId = state.userId, portfolioId = portfolio.id;
  const current = () => state.userId === userId && state.selectedPortfolioId === portfolioId;
  return {portfolio, userId, funds:state.funds, transactions:state.portfolioTransactions,
    orders:state.portfolioTradeOrders, loading:state.portfolioTransactionsLoading, error:state.portfolioTransactionError,
    request:apiRequest, createKey:createTransactionIdempotencyKey, notify:showToast, current,
    render:() => { if (current()) renderPortfolioWorkbench({pages:["transactions"]}); },
    refresh:() => current() ? refreshPortfolioBookkeeping(userId,portfolioId) : Promise.resolve(),
  };
}

async function refreshPortfolioBookkeeping(userId, portfolioId) {
  const current = () => state.userId === userId && state.selectedPortfolioId === portfolioId;
  const result = await apiRequest(`${endpoints.portfolios}/${encodeURIComponent(portfolioId)}?${new URLSearchParams({user_id:userId})}`);
  if (!current()) return;
  state.portfolioDetail = result;
  state.portfolios = state.portfolios.map(item => item.id === portfolioId ? result : item);
  state.portfolioDraft = portfolioDraftFromDetail(result);
  // 新账本使旧分析缓存失效；历史报告仍在后端保留，不自动重新调用模型。
  portfolioPageReads.clear();
  resetPortfolioPerformance(); resetPortfolioEventImpacts(); resetPortfolioMonitoring(); resetPortfolioDecisions();
  state.portfolioAssessment = null; state.portfolioAssessmentRequestId += 1;
  state.portfolioAssessmentLoading = false; state.portfolioAssessmentError = "";
  renderPortfolioWorkbench();
  await loadPortfolioTransactions();
}

async function renameSelectedPortfolio(event) {
  event.preventDefault();
  if (!state.selectedPortfolioId || state.portfolioSaving) return;
  const form = new FormData(event.target);
  const name = String(form.get("name") || "").trim();
  if (!name) {
    showToast("组合名称不能为空", "error");
    return;
  }

  state.portfolioSaving = true;
  renderPortfolioWorkbench({pages: ["holdings"]});
  try {
    await apiRequest(`${endpoints.portfolios}/${encodeURIComponent(state.selectedPortfolioId)}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ user_id: state.userId, name }),
    });
    state.portfolioSaving = false;
    showToast("组合名称已更新");
    await loadPortfolios({ preferredPortfolioId: state.selectedPortfolioId });
  } catch (error) {
    state.portfolioSaving = false;
    state.portfolioTransactionError = error.message;
    renderPortfolioWorkbench({pages: ["holdings"]});
    showToast(error.message, "error");
  }
}

function portfolioAssessmentEndpoint(portfolioId, months) {
  const params = new URLSearchParams({
    user_id: state.userId,
    months: String(months),
  });
  return `${endpoints.portfolios}/${encodeURIComponent(portfolioId)}/assessment?${params}`;
}

function portfolioPerformanceEndpoint(portfolioId, months) {
  const params = new URLSearchParams({
    user_id: state.userId,
    months: String(months),
  });
  return `${endpoints.portfolios}/${encodeURIComponent(portfolioId)}/performance?${params}`;
}

function portfolioDecisionsEndpoint(portfolioId) {
  return `${endpoints.portfolios}/${encodeURIComponent(portfolioId)}/decisions`;
}

function portfolioEventImpactsEndpoint(portfolioId) {
  const params = new URLSearchParams({ user_id: state.userId, limit: "12" });
  return `${endpoints.portfolios}/${encodeURIComponent(portfolioId)}/event-impacts?${params}`;
}

function portfolioResearchEndpoint(portfolioId) {
  return `${endpoints.portfolios}/${encodeURIComponent(portfolioId)}/research-runs`;
}

function portfolioResearchHistoryEndpoint(portfolioId, months) {
  const params = new URLSearchParams({
    user_id: state.userId,
    months: String(months),
    limit: "10",
  });
  return `${portfolioResearchEndpoint(portfolioId)}?${params}`;
}

function portfolioResearchComparisonEndpoint(portfolioId, jobId) {
  const params = new URLSearchParams({ user_id: state.userId });
  return `${portfolioResearchEndpoint(portfolioId)}/${encodeURIComponent(jobId)}/comparison?${params}`;
}

function portfolioAdjustmentEndpoint(portfolioId) {
  return `${endpoints.portfolios}/${encodeURIComponent(portfolioId)}/adjustment-runs`;
}

function portfolioAdjustmentResumeEndpoint(portfolioId, jobId) {
  return `${portfolioAdjustmentEndpoint(portfolioId)}/${encodeURIComponent(jobId)}/resume`;
}

function portfolioMonitoringEndpoint(portfolioId) {
  return `${endpoints.portfolios}/${encodeURIComponent(portfolioId)}/monitoring`;
}

async function loadPortfolioResearchHistory({ restoreLatest = true } = {}) {
  if (!state.selectedPortfolioId || state.portfolioResearchHistoryLoading) return;

  const portfolioId = state.selectedPortfolioId;
  const months = state.portfolioPeriods.research;
  const requestId = ++state.portfolioResearchHistoryRequestId;
  state.portfolioResearchHistoryLoading = true;
  state.portfolioResearchHistoryError = "";
  state.portfolioResearchHistoryNotice = "";
  renderPortfolioWorkbench({pages: ["research"]});

  try {
    const history = await apiRequest(
      portfolioResearchHistoryEndpoint(portfolioId, months),
    );
    if (requestId !== state.portfolioResearchHistoryRequestId) return;
    state.portfolioResearchHistory = Array.isArray(history.runs) ? history.runs : [];
    state.portfolioResearchHistoryNotice = history.message || "";

    // 旧版页面只把报告放在 JS 内存里。现在后端返回最近报告，刷新浏览器后也能继续查看。
    if (restoreLatest && !state.portfolioResearchRunning && !state.portfolioReportLoading && history.latest_report) {
      const latest = state.portfolioResearchHistory[0];
      const totalSteps = history.latest_report.research_plan?.total_steps
        || (window.FundScopeResearchCommittee.isCommitteeRun({}, history.latest_report) ? 13 : 8);
      state.portfolioResearchResult = history.latest_report;
      state.portfolioResearchComparison = null;
      state.portfolioResearchComparisonError = "";
      state.portfolioResearchComparisonLoading = false;
      state.portfolioResearchJob = latest
        ? {
            id: latest.job_id,
            status: "completed",
            current_step: totalSteps,
            total_steps: totalSteps,
            progress_percent: 100,
            completed_at: latest.completed_at,
          }
        : null;
    }
  } catch (error) {
    if (requestId !== state.portfolioResearchHistoryRequestId) return;
    state.portfolioResearchHistory = [];
    state.portfolioResearchHistoryError = error.message;
  } finally {
    if (requestId === state.portfolioResearchHistoryRequestId) {
      state.portfolioResearchHistoryLoading = false;
      renderPortfolioWorkbench({pages: ["research"]});
    }
  }
}

async function comparePortfolioResearch() {
  if (
    !state.selectedPortfolioId
    || !state.portfolioResearchResult
    || state.portfolioResearchComparisonLoading
  ) return;

  const targetJobId = state.portfolioResearchResult.run_id
    || state.portfolioResearchJob?.id;
  if (!targetJobId) {
    showToast("当前报告缺少可追溯的研究 Job", "error");
    return;
  }
  if (!window.confirm("比较两份已保存报告，可能调用模型解释变化。不会刷新外部数据或修改持仓。继续？")) return;
  const portfolioId = state.selectedPortfolioId, userId = state.userId;
  const report = state.portfolioResearchResult;
  const active = () => state.portfolioResearchResult === report && state.selectedPortfolioId === portfolioId && state.userId === userId;

  state.portfolioResearchComparisonLoading = true;
  state.portfolioResearchComparisonError = "";
  state.portfolioResearchComparison = null;
  renderPortfolioWorkbench({pages: ["research"]});

  try {
    // 后端会自动选择目标报告之前最近的同区间报告；前端不自行猜测历史顺序。
    const comparison = await apiRequest(
      portfolioResearchComparisonEndpoint(portfolioId, targetJobId),
    );
    if (active()) state.portfolioResearchComparison = comparison;
  } catch (error) {
    if (active()) {
      state.portfolioResearchComparisonError = error.message;
      showToast(error.message, "error");
    }
  } finally {
    if (active()) {
      state.portfolioResearchComparisonLoading = false;
      renderPortfolioWorkbench({pages: ["research"]});
    }
  }
}

async function loadPortfolioMonitoring() {
  if (!state.selectedPortfolioId || state.portfolioMonitoringLoading) return;

  const portfolioId = state.selectedPortfolioId;
  const requestId = ++state.portfolioMonitoringRequestId;
  state.portfolioMonitoringLoading = true;
  state.portfolioMonitoringError = "";
  renderPortfolioWorkbench({pages: ["monitoring"]});

  try {
    const params = new URLSearchParams({ user_id: state.userId });
    const overview = await apiRequest(
      `${portfolioMonitoringEndpoint(portfolioId)}?${params}`,
    );
    if (requestId !== state.portfolioMonitoringRequestId) return;
    state.portfolioMonitoring = overview;
  } catch (error) {
    if (requestId !== state.portfolioMonitoringRequestId) return;
    state.portfolioMonitoring = null;
    state.portfolioMonitoringError = error.message;
  } finally {
    if (requestId === state.portfolioMonitoringRequestId) {
      state.portfolioMonitoringLoading = false;
      renderPortfolioWorkbench({pages: ["monitoring"]});
    }
  }
}

async function savePortfolioMonitoringPolicy(event) {
  event.preventDefault();
  if (!state.selectedPortfolioId || state.portfolioMonitoringSaving) return;

  const form = event.target;
  const values = new FormData(form);
  const policy = {
    user_id: state.userId,
    enabled: form.querySelector('[name="enabled"]')?.checked || false,
    risk_preference: values.get("risk_preference") || "balanced",
    investment_horizon: values.get("investment_horizon") || "one_to_three_years",
    monitor_months: Number(values.get("monitor_months") || 12),
    max_single_adjustment_pct: Number(values.get("max_single_adjustment_pct") || 15),
    drawdown_alert_pct_points: Number(values.get("drawdown_alert_pct_points") || 3),
    concentration_alert_pct_points: Number(values.get("concentration_alert_pct_points") || 5),
    major_event_heat_threshold: Number(values.get("major_event_heat_threshold") || 70),
    event_impact_exposure_threshold_pct: Number(
      values.get("event_impact_exposure_threshold_pct") || 5,
    ),
    alert_on_risk_state_worsening: (
      form.querySelector('[name="alert_on_risk_state_worsening"]')?.checked || false
    ),
    schedule_cron: String(values.get("schedule_cron") || "").trim(),
  };

  state.portfolioMonitoringSaving = true;
  state.portfolioMonitoringError = "";
  renderPortfolioWorkbench({pages: ["monitoring"]});
  try {
    const portfolioId = state.selectedPortfolioId;
    await apiRequest(`${portfolioMonitoringEndpoint(portfolioId)}/policy`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(policy),
    });
    showToast(policy.enabled ? "组合监控已启用" : "组合监控策略已保存");
    state.portfolioMonitoringSaving = false;
    await loadPortfolioMonitoring();
  } catch (error) {
    state.portfolioMonitoringError = error.message;
    showToast(error.message, "error");
  } finally {
    state.portfolioMonitoringSaving = false;
    renderPortfolioWorkbench({pages: ["monitoring"]});
  }
}

async function runPortfolioMonitor() {
  if (!state.selectedPortfolioId || state.portfolioMonitoringRunning) return;

  const portfolioId = state.selectedPortfolioId;
  const requestId = ++state.portfolioMonitoringRequestId;
  state.portfolioMonitoringRunning = true;
  state.portfolioMonitoringError = "";
  state.portfolioMonitoringJob = null;
  renderPortfolioWorkbench({pages: ["monitoring"]});

  try {
    const accepted = await apiRequest(
      `${portfolioMonitoringEndpoint(portfolioId)}/runs`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ user_id: state.userId }),
      },
    );
    if (requestId !== state.portfolioMonitoringRequestId) return;
    state.portfolioMonitoringJob = accepted.job;
    renderPortfolioWorkbench({pages: ["monitoring"]});

    const completedJob = await waitForBackgroundJobEvents(
      accepted.job.id,
      state.userId,
      (job) => {
        if (requestId !== state.portfolioMonitoringRequestId) return;
        state.portfolioMonitoringJob = job;
        renderPortfolioWorkbench({pages: ["monitoring"]});
      },
    );
    if (requestId !== state.portfolioMonitoringRequestId) return;
    state.portfolioMonitoringJob = completedJob;
    if (completedJob.status !== "completed") {
      throw new ApiError(
        completedJob.error || `监控任务${statusLabel(completedJob.status)}`,
        503,
      );
    }
    showToast(completedJob.result?.summary || "组合风险快照已更新");
    state.portfolioMonitoringRunning = false;
    await loadPortfolioMonitoring();
  } catch (error) {
    if (requestId !== state.portfolioMonitoringRequestId) return;
    state.portfolioMonitoringError = error.message;
    showToast(error.message, "error");
  } finally {
    if (requestId === state.portfolioMonitoringRequestId) {
      state.portfolioMonitoringRunning = false;
      renderPortfolioWorkbench({pages: ["monitoring"]});
    }
  }
}

async function acknowledgePortfolioAlert(alertId) {
  if (!state.selectedPortfolioId || !alertId) return;

  try {
    await apiRequest(
      `${portfolioMonitoringEndpoint(state.selectedPortfolioId)}/alerts/${encodeURIComponent(alertId)}/acknowledge`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ user_id: state.userId }),
      },
    );
    showToast("预警已标记为已读");
    await loadPortfolioMonitoring();
  } catch (error) {
    showToast(error.message, "error");
  }
}

async function runPortfolioResearch({researchMode = "standard"} = {}) {
  if (!state.selectedPortfolioId || state.portfolioResearchRunning) return;
  const mode = window.FundScopeResearchCommittee.normalizeMode(researchMode);
  const draft = JSON.parse(JSON.stringify(state.portfolioResearchDraft));
  const refreshNotice = draft.allow_public_refresh ? "仅在发现缺口时补查公开季报和事件，最多两次。" : "只读已保存数据，不刷新公开源。";
  const confirmation = mode === "committee"
    ? `运行多角色研判，包含五个研判阶段，可能增加模型用量与耗时。${refreshNotice}不会修改持仓。继续？`
    : `按所选模式生成配置报告，将调用模型。${mode === "auto" ? "深入质询类问题会进入多角色分析。" : ""}${refreshNotice}不会修改持仓。继续？`;
  if (!window.confirm(confirmation)) return;
  activateProductView("portfolio", { page: "research" });

  const portfolioId = state.selectedPortfolioId;
  const userId = state.userId;
  const months = state.portfolioPeriods.research;
  // 标准模式保留旧缓存键；多角色单独重试，不能复用另一模式的任务或幂等编号。
  const hasCustomPlan = Boolean(draft.question.trim() || draft.allow_public_refresh || Object.values(draft.investor_context).some(value => value.trim()));
  // 无新参数的旧标准/多角色请求仍能恢复原缓存；新问题和授权使用独立请求身份。
  const suffix = !hasCustomPlan && ["standard", "committee"].includes(mode)
    ? (mode === "committee" ? ":committee" : "") : `:${mode}:${encodeURIComponent(JSON.stringify(draft))}`;
  const pendingKey = `fundscope-research:${userId}:${portfolioId}:${months}${suffix}`;
  const submitKey = `${pendingKey}:submit`;
  const requestId = ++state.portfolioResearchRequestId;
  state.portfolioReportLoading = false;
  state.portfolioReportReadJobId = "";
  state.portfolioResearchRunning = true;
  state.portfolioResearchMode = mode;
  state.portfolioResearchError = "";
  state.portfolioResearchResult = null;
  state.portfolioResearchJob = null;
  state.portfolioResearchComparison = null;
  state.portfolioResearchComparisonError = "";
  state.portfolioResearchComparisonLoading = false;
  renderPortfolioWorkbench({pages: ["research"]});

  try {
    // HTTP 只负责创建 Job；LangGraph 各阶段在 Worker 中继续执行。
    const pendingId = localStorage.getItem(pendingKey);
    const idempotencyKey = localStorage.getItem(submitKey) || createIdempotencyKey();
    localStorage.setItem(submitKey, idempotencyKey);
    const accepted = pendingId
      ? await apiRequest(`${endpoints.backgroundJobs}/${encodeURIComponent(pendingId)}?user_id=${encodeURIComponent(userId)}`)
      : await apiRequest(portfolioResearchEndpoint(portfolioId), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        user_id: userId,
        months,
        research_mode: mode,
        ...draft,
        idempotency_key: idempotencyKey,
      }),
    });
    localStorage.setItem(pendingKey, accepted.job.id);
    if (requestId !== state.portfolioResearchRequestId) return;
    state.portfolioResearchJob = accepted.job;
    renderPortfolioWorkbench({pages: ["research"]});

    // SSE 的每条 progress 都是数据库里的权威 Job 快照，API 与 Worker 可以位于不同进程。
    const completedJob = ["completed", "failed", "cancelled"].includes(accepted.job.status) ? accepted.job : await waitForBackgroundJobEvents(
      accepted.job.id,
      userId,
      (job) => {
        if (requestId !== state.portfolioResearchRequestId) return;
        state.portfolioResearchJob = job;
        renderPortfolioWorkbench({pages: ["research"]});
      },
    );
    if (requestId !== state.portfolioResearchRequestId) return;
    state.portfolioResearchJob = completedJob;
    if (completedJob.status !== "completed") {
      localStorage.removeItem(pendingKey); localStorage.removeItem(submitKey);
      throw new ApiError(
        completedJob.error || `研究任务${statusLabel(completedJob.status)}`,
        503,
      );
    }
    // 读取时核验冻结快照并附上两端共享的展示结构，不再次执行研究。
    const report = await apiRequest(`${portfolioResearchEndpoint(portfolioId)}/${encodeURIComponent(completedJob.id)}?user_id=${encodeURIComponent(userId)}`);
    localStorage.removeItem(pendingKey); localStorage.removeItem(submitKey);
    if (requestId !== state.portfolioResearchRequestId) return;
    state.portfolioResearchResult = report;
    await loadPortfolioResearchHistory({ restoreLatest: false });
    showToast("组合研究报告已生成");
  } catch (error) {
    if (requestId !== state.portfolioResearchRequestId) return;
    state.portfolioResearchError = error.message;
    showToast(error.message, "error");
  } finally {
    if (requestId === state.portfolioResearchRequestId) {
      state.portfolioResearchRunning = false;
      renderPortfolioWorkbench({pages: ["research"]});
    }
  }
}

function waitForBackgroundJobEvents(jobId, userId, onProgress) {
  "use strict";
  return new Promise((resolve, reject) => {
    const params = new URLSearchParams({ user_id: userId });
    const source = new EventSource(
      `${endpoints.backgroundJobs}/${encodeURIComponent(jobId)}/events?${params}`,
    );
    let finished = false;

    const readJob = (event) => {
      const job = JSON.parse(event.data);
      onProgress(job);
      return job;
    };
    source.addEventListener("progress", (event) => {
      try {
        readJob(event);
      } catch (error) {
        finished = true;
        source.close();
        reject(new ApiError(`无法读取后台任务进度：${error.message}`, 502));
      }
    });
    source.addEventListener("done", (event) => {
      try {
        const job = readJob(event);
        finished = true;
        source.close();
        resolve(job);
      } catch (error) {
        finished = true;
        source.close();
        reject(new ApiError(`无法读取后台任务结果：${error.message}`, 502));
      }
    });
    source.addEventListener("input_required", (event) => {
      try {
        // waiting_input 不是失败：服务端已经保存 checkpoint，并主动关闭本次 SSE。
        const job = readJob(event);
        finished = true;
        source.close();
        resolve(job);
      } catch (error) {
        finished = true;
        source.close();
        reject(new ApiError(`无法读取人工确认内容：${error.message}`, 502));
      }
    });
    source.onerror = () => {
      if (finished) return;
      finished = true;
      source.close();
      reject(new ApiError("后台任务进度连接已中断", 503));
    };
  });
}

async function startPortfolioAdjustment() {
  if (!state.selectedPortfolioId || state.portfolioAdjustmentRunning) return;
  const sourceJobId = state.portfolioResearchResult?.run_id
    || state.portfolioResearchJob?.id;
  if (!sourceJobId) {
    showToast("请先生成组合研究报告", "error");
    return;
  }

  const portfolioId = state.selectedPortfolioId;
  const requestId = ++state.portfolioAdjustmentRequestId;
  state.portfolioAdjustmentRunning = true;
  state.portfolioAdjustmentError = "";
  state.portfolioAdjustmentJob = null;
  state.portfolioAdjustmentResult = null;
  renderPortfolioWorkbench({pages: ["research"]});

  try {
    const accepted = await apiRequest(portfolioAdjustmentEndpoint(portfolioId), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        user_id: state.userId,
        source_research_job_id: sourceJobId,
      }),
    });
    if (requestId !== state.portfolioAdjustmentRequestId) return;
    state.portfolioAdjustmentJob = accepted.job;
    renderPortfolioWorkbench({pages: ["research"]});

    const pausedJob = await waitForBackgroundJobEvents(
      accepted.job.id,
      state.userId,
      (job) => {
        if (requestId !== state.portfolioAdjustmentRequestId) return;
        state.portfolioAdjustmentJob = job;
        renderPortfolioWorkbench({pages: ["research"]});
      },
    );
    if (requestId !== state.portfolioAdjustmentRequestId) return;
    state.portfolioAdjustmentJob = pausedJob;
    if (pausedJob.status === "waiting_input") {
      showToast("研究证据已整理，请确认投资约束");
      return;
    }
    if (pausedJob.status !== "completed") {
      throw new ApiError(
        pausedJob.error || `调整任务${statusLabel(pausedJob.status)}`,
        503,
      );
    }
    state.portfolioAdjustmentResult = pausedJob.result;
  } catch (error) {
    if (requestId !== state.portfolioAdjustmentRequestId) return;
    state.portfolioAdjustmentError = error.message;
    showToast(error.message, "error");
  } finally {
    if (requestId === state.portfolioAdjustmentRequestId) {
      state.portfolioAdjustmentRunning = false;
      renderPortfolioWorkbench({pages: ["research"]});
    }
  }
}

async function submitPortfolioAdjustmentReview(event) {
  event.preventDefault();
  const job = state.portfolioAdjustmentJob;
  if (!state.selectedPortfolioId || !job || state.portfolioAdjustmentRunning) return;

  const form = event.target;
  const values = new FormData(form);
  const decision = event.submitter?.value === "cancel" ? "cancel" : "continue";
  const payload = {
    user_id: state.userId,
    decision,
    risk_preference: values.get("risk_preference") || "balanced",
    investment_horizon: values.get("investment_horizon") || "one_to_three_years",
    max_single_adjustment_pct: Number(values.get("max_single_adjustment_pct") || 15),
    max_single_fund_weight_pct: Number(values.get("max_single_fund_weight_pct") || 70),
    max_turnover_pct: Number(values.get("max_turnover_pct") || 20),
    additional_constraints: String(values.get("additional_constraints") || "").trim(),
  };

  const portfolioId = state.selectedPortfolioId;
  const requestId = ++state.portfolioAdjustmentRequestId;
  state.portfolioAdjustmentRunning = true;
  state.portfolioAdjustmentError = "";
  renderPortfolioWorkbench({pages: ["research"]});

  try {
    const accepted = await apiRequest(
      portfolioAdjustmentResumeEndpoint(portfolioId, job.id),
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      },
    );
    if (requestId !== state.portfolioAdjustmentRequestId) return;
    state.portfolioAdjustmentJob = accepted.job;

    const completedJob = await waitForBackgroundJobEvents(
      job.id,
      state.userId,
      (nextJob) => {
        if (requestId !== state.portfolioAdjustmentRequestId) return;
        state.portfolioAdjustmentJob = nextJob;
        renderPortfolioWorkbench({pages: ["research"]});
      },
    );
    if (requestId !== state.portfolioAdjustmentRequestId) return;
    state.portfolioAdjustmentJob = completedJob;
    if (completedJob.status !== "completed") {
      throw new ApiError(
        completedJob.error || `调整任务${statusLabel(completedJob.status)}`,
        503,
      );
    }
    state.portfolioAdjustmentResult = completedJob.result;
    showToast(
      completedJob.result?.status === "cancelled_by_user"
        ? "本次调整方案已停止"
        : "组合调整方案已生成",
    );
  } catch (error) {
    if (requestId !== state.portfolioAdjustmentRequestId) return;
    state.portfolioAdjustmentError = error.message;
    showToast(error.message, "error");
  } finally {
    if (requestId === state.portfolioAdjustmentRequestId) {
      state.portfolioAdjustmentRunning = false;
      renderPortfolioWorkbench({pages: ["research"]});
    }
  }
}

async function loadPortfolioAssessment() {
  if (!state.selectedPortfolioId) return;
  const portfolioId = state.selectedPortfolioId;
  const months = state.portfolioPeriods.risk;
  const requestId = ++state.portfolioAssessmentRequestId;
  state.portfolioAssessmentLoading = true;
  state.portfolioAssessmentError = "";
  renderPortfolioWorkbench({pages: ["risk"]});

  try {
    const assessment = await apiRequest(
      portfolioAssessmentEndpoint(portfolioId, months),
    );
    // 用户快速切换组合或区间时，只接收最后一次请求，防止旧响应覆盖新状态。
    if (requestId === state.portfolioAssessmentRequestId) {
      state.portfolioAssessment = assessment;
    }
  } catch (error) {
    if (requestId === state.portfolioAssessmentRequestId) {
      state.portfolioAssessment = null;
      state.portfolioAssessmentError = error.message;
    }
  } finally {
    if (requestId === state.portfolioAssessmentRequestId) {
      state.portfolioAssessmentLoading = false;
      renderPortfolioWorkbench({pages: ["risk"]});
    }
  }
}

async function loadPortfolioPerformance() {
  if (!state.selectedPortfolioId) return;
  const portfolioId = state.selectedPortfolioId;
  const months = state.portfolioPeriods.performance;
  const requestId = ++state.portfolioPerformanceRequestId;
  state.portfolioPerformanceLoading = true;
  state.portfolioPerformanceError = "";
  renderPortfolioWorkbench({pages: ["performance", "holdings"], includeTransactions:false});

  try {
    const performance = await apiRequest(
      portfolioPerformanceEndpoint(portfolioId, months),
    );
    // 与研判请求一样，只让最后一次区间/组合选择更新界面。
    if (requestId === state.portfolioPerformanceRequestId) {
      state.portfolioPerformance = performance;
    }
  } catch (error) {
    if (requestId === state.portfolioPerformanceRequestId) {
      state.portfolioPerformance = null;
      state.portfolioPerformanceError = error.message;
    }
  } finally {
    if (requestId === state.portfolioPerformanceRequestId) {
      state.portfolioPerformanceLoading = false;
      renderPortfolioWorkbench({pages: ["performance", "holdings"], includeTransactions:false});
    }
  }
}

async function loadPortfolioDecisions() {
  if (!state.selectedPortfolioId) return;
  const portfolioId = state.selectedPortfolioId;
  const requestId = ++state.portfolioDecisionRequestId;
  state.portfolioDecisionsLoading = true;
  state.portfolioDecisionError = "";
  renderPortfolioWorkbench({pages: ["decisions"]});

  try {
    const params = new URLSearchParams({ user_id: state.userId });
    const result = await apiRequest(
      `${portfolioDecisionsEndpoint(portfolioId)}?${params}`,
    );
    if (requestId === state.portfolioDecisionRequestId) {
      state.portfolioDecisions = result.decisions || [];
    }
  } catch (error) {
    if (requestId === state.portfolioDecisionRequestId) {
      state.portfolioDecisions = [];
      state.portfolioDecisionError = error.message;
    }
  } finally {
    if (requestId === state.portfolioDecisionRequestId) {
      state.portfolioDecisionsLoading = false;
      renderPortfolioWorkbench({pages: ["decisions"]});
    }
  }
}

async function createPortfolioDecision(event) {
  event.preventDefault();
  if (!state.selectedPortfolioId || state.portfolioDecisionSaving) return;
  const form = event.target;
  const values = new FormData(form);
  // 使用表单展示时绑定的报告，不悄悄替换成其他页面后来加载的研究结果。
  const researchJobId = String(values.get("research_job_id") || "") || null;
  const payload = {
    user_id: state.userId,
    action: values.get("action"),
    thesis: String(values.get("thesis") || "").trim(),
    horizon_days: Number(values.get("horizon_days") || 90),
    research_job_id: researchJobId,
  };

  state.portfolioDecisionSaving = true;
  state.portfolioDecisionError = "";
  renderPortfolioWorkbench({pages: ["decisions"]});
  try {
    await apiRequest(portfolioDecisionsEndpoint(state.selectedPortfolioId), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    showToast("决策和当时证据已保存");
    state.portfolioDecisionSaving = false;
    await loadPortfolioDecisions();
  } catch (error) {
    state.portfolioDecisionError = error.message;
    showToast(error.message, "error");
  } finally {
    state.portfolioDecisionSaving = false;
    renderPortfolioWorkbench({pages: ["decisions"]});
  }
}

async function reviewPortfolioDecision(event) {
  event.preventDefault();
  if (state.portfolioDecisionSaving) return;
  const decisionId = event.target.dataset.decisionReviewForm;
  const values = new FormData(event.target);
  state.portfolioDecisionSaving = true;
  renderPortfolioWorkbench({pages: ["decisions"]});
  try {
    await apiRequest(
      `${portfolioDecisionsEndpoint(state.selectedPortfolioId)}/${encodeURIComponent(decisionId)}`,
      {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          user_id: state.userId,
          review_note: String(values.get("review_note") || "").trim(),
        }),
      },
    );
    showToast("复盘结论已记录");
    state.portfolioDecisionSaving = false;
    await loadPortfolioDecisions();
  } catch (error) {
    state.portfolioDecisionError = error.message;
    showToast(error.message, "error");
  } finally {
    state.portfolioDecisionSaving = false;
    renderPortfolioWorkbench({pages: ["decisions"]});
  }
}

async function deletePortfolioDecision(decisionId) {
  if (!decisionId || state.portfolioDecisionSaving) return;
  if (!window.confirm("删除这条决策日志？已冻结的历史快照也会一并删除。")) return;
  state.portfolioDecisionSaving = true;
  renderPortfolioWorkbench({pages: ["decisions"]});
  try {
    const params = new URLSearchParams({ user_id: state.userId });
    await apiRequest(
      `${portfolioDecisionsEndpoint(state.selectedPortfolioId)}/${encodeURIComponent(decisionId)}?${params}`,
      { method: "DELETE" },
    );
    showToast("决策日志已删除");
    state.portfolioDecisionSaving = false;
    await loadPortfolioDecisions();
  } catch (error) {
    state.portfolioDecisionError = error.message;
    showToast(error.message, "error");
  } finally {
    state.portfolioDecisionSaving = false;
    renderPortfolioWorkbench({pages: ["decisions"]});
  }
}

async function loadPortfolioEventImpacts() {
  if (!state.selectedPortfolioId || state.portfolioEventImpactsLoading) return;
  const portfolioId = state.selectedPortfolioId;
  const requestId = ++state.portfolioEventImpactsRequestId;
  state.portfolioEventImpactsLoading = true;
  state.portfolioEventImpactsError = "";
  renderPortfolioWorkbench({pages: ["events"]});

  try {
    const result = await apiRequest(portfolioEventImpactsEndpoint(portfolioId));
    if (requestId !== state.portfolioEventImpactsRequestId) return;
    state.portfolioEventImpacts = result;
  } catch (error) {
    if (requestId !== state.portfolioEventImpactsRequestId) return;
    state.portfolioEventImpacts = null;
    state.portfolioEventImpactsError = error.message;
  } finally {
    if (requestId === state.portfolioEventImpactsRequestId) {
      state.portfolioEventImpactsLoading = false;
      renderPortfolioWorkbench({pages: ["events"]});
    }
  }
}

// host 负责页面树，renderPortfolioPage 负责当前页的业务内容，两者互不请求数据。
let portfolioPageHost = null;
function renderPortfolioWorkbench({pages = null, includeTransactions = true} = {}) {
  if (!elements.portfolioWorkbench) return;
  portfolioPageHost ||= window.FundScopeWorkspacePageHost.create({
    element: elements.portfolioWorkbench,
    pages: workspaceOutlineConfig.portfolio.items,
    render: renderPortfolioPage,
    beforeReset: () => {
      dailyBriefings?.pause();
      window.FundScopeDataVisuals.unmount(elements.portfolioWorkbench);
      portfolioStressWorkbench?.beforeUnmount();
      portfolioAllocationWorkbench?.beforeUnmount();
    },
    beforeRender: page => {
      window.FundScopeDataVisuals.unmount(document.querySelector(`#portfolio-page-${page}`));
      if (page === "stress") portfolioStressWorkbench?.beforeUnmount();
      if (page === "optimizer") portfolioAllocationWorkbench?.beforeUnmount();
    },
    afterRender: page => {
      window.FundScopeDataVisuals.mount(document.querySelector(`#portfolio-page-${page}`));
      if (page === "holdings" || page === "configuration") updatePortfolioDraftStatus();
      if (page === "stress") mountPortfolioStress();
      if (page === "optimizer") mountPortfolioAllocation();
      refreshIcons();
    },
  });
  portfolioPageHost.update({
    key: `${state.userId}:${state.selectedPortfolioId || `new:${state.portfolioDraftVersion}`}`,
    activePage: state.workspacePages.portfolio,
    header: renderPortfolioHeader(),
    // 交易保存更新流水；仅报价刷新时保留正在填写的交易表单。
    changedPages: includeTransactions && pages?.includes("holdings") ? [...new Set([...pages, "transactions"])] : pages,
    visible: state.activeView === "portfolio",
  });
  refreshIcons();
}

function renderPortfolioHeader() {
  return `<header class="portfolio-header">
    <div class="portfolio-header-actions">
      <label class="portfolio-select-label"><span>当前组合</span>
        <select data-portfolio-select aria-label="选择基金组合">
          <option value="">新建真实持仓</option>
          ${state.portfolios.map(portfolio => `<option value="${escapeAttribute(portfolio.id)}"${portfolio.id === state.selectedPortfolioId ? " selected" : ""}>${escapeHtml(portfolio.name)} · ${portfolio.holding_count} 只${portfolio.position_mode === "actual" ? " · 真实持仓" : " · 待补录持仓"}</option>`).join("")}
        </select>
      </label>
      <button class="icon-button" type="button" data-new-portfolio title="新建组合" aria-label="新建组合"><i data-lucide="plus"></i></button>
    </div>
    ${state.workspacePages.portfolio === "holdings" && state.portfolioDetail?.position_mode === "actual" ? `
      <div class="asset-overview-actions">
        <a class="secondary-action compact" href="#assets/transactions"><i data-lucide="book-plus"></i>记账与流水</a>
        <button class="primary-action compact" type="button" data-analyze-portfolio ${state.sending ? "disabled" : ""}><i data-lucide="sparkles"></i>分析当前配置</button>
      </div>` : ""}
  </header>
  ${state.portfolioError ? `<div class="portfolio-inline-error"><i data-lucide="triangle-alert"></i>${escapeHtml(state.portfolioError)}</div>` : ""}`;
}

let portfolioReview = null;
let dailyBriefings = null;
function getDailyBriefings() {
  return dailyBriefings ||= window.FundScopeDailyBriefings.create({
    context: () => ({user:state.userId, portfolio:state.selectedPortfolioId}), request:apiRequest,
    visible: () => state.activeView === "portfolio" && state.workspacePages.portfolio === "briefings",
    changed: () => renderPortfolioWorkbench({pages:["briefings"]}),
  });
}

function getPortfolioReview() {
  return portfolioReview ||= window.FundScopePortfolioReview.create({
    context: () => ({user:state.userId,portfolio:state.selectedPortfolioId,revision:state.portfolioDetail?.updated_at}), request:apiRequest,
    readPerformance: (context, months) => apiRequest(`/api/v1/portfolios/${encodeURIComponent(context.portfolio)}/performance?${new URLSearchParams({user_id:context.user,months})}`),
    changed: () => renderPortfolioWorkbench({pages:["review"]}),
  });
}

async function openReviewedReport(jobId) {
  const portfolioId = state.selectedPortfolioId, user = state.userId;
  const requestId = ++state.portfolioResearchRequestId;
  const active = () => requestId === state.portfolioResearchRequestId && portfolioId === state.selectedPortfolioId && user === state.userId;
  // 明确选择的报告优先；历史列表、其他报告或旧 Job 晚到都不能覆盖它。
  state.portfolioResearchHistoryRequestId++;
  state.portfolioResearchHistoryLoading = false;
  state.portfolioReportLoading = true;
  state.portfolioReportReadJobId = jobId;
  state.portfolioResearchRunning = false;
  state.portfolioResearchError = "";
  activateProductView("portfolio", {page:"research"});
  renderPortfolioWorkbench({pages:["research"]});
  try {
    const report = await apiRequest(`${portfolioResearchEndpoint(portfolioId)}/${encodeURIComponent(jobId)}?user_id=${encodeURIComponent(user)}`);
    if (!active()) return;
    state.portfolioResearchHistoryRequestId++;
    state.portfolioResearchHistoryLoading = false;
    state.portfolioResearchResult = report;
    state.portfolioResearchJob = {id:report.run_id,status:"completed",progress_percent:100};
    state.portfolioPeriods.research = report.period_months;
    state.portfolioResearchComparison = null;
    state.portfolioResearchComparisonError = "";
    state.portfolioResearchComparisonLoading = false;
    void loadPortfolioResearchHistory({restoreLatest:false});
  } catch (error) {
    if (active()) state.portfolioResearchError = error.message;
  } finally {
    if (active()) {
      state.portfolioReportLoading = false;
      renderPortfolioWorkbench({pages:["research"]});
    }
  }
}

function followPortfolioReport(question = "这份配置报告中，最值得关注的风险是什么？请结合引用的证据解释。") {
  const report = state.portfolioResearchResult;
  if (!report?.run_id || state.sending) return;
  startNewConversation();
  state.researchReference = {job_id:report.run_id,portfolio_id:report.portfolio_id,
    portfolio_name:report.portfolio_name,assessment_date:report.assessment_date};
  state.agentContextMode = "portfolio";
  activateProductView("agent");
  renderAgentFundContext(); renderMessages();
  elements.messageInput.value = question;
  resizeComposer(); elements.messageInput.focus();
}

function renderPortfolioPage(page) {
  const item = workspaceOutlineConfig.portfolio.items.find(item => item.id === page);
  if (state.portfolioLoading && !state.portfolioDraft) return portfolioStatus("loader-circle", "正在读取基金组合", true);
  if (page !== "holdings" && !state.selectedPortfolioId) {
    return `<div class="portfolio-page-empty"><h2>${item.label}</h2><p>尚未选择已保存的组合</p>
      <button class="secondary-action compact" type="button" data-open-portfolio-page="holdings"><i data-lucide="wallet-cards"></i>前往持仓交易</button></div>`;
  }
  const renderers = {
    briefings: () => getDailyBriefings().render(),
    review: () => getPortfolioReview().render(),
    holdings: () => renderPortfolioEditor(state.portfolioDraft || defaultPortfolioDraft()),
    // 真实账本靠交易变更份额，不能误入目标权重表覆盖成另一种组合。
    configuration: () => state.portfolioDetail?.position_mode === "actual"
      ? '<p class="product-empty">当前是真实持仓账本，请在<a href="#assets/transactions">记账与流水</a>中记录份额变化。</p>'
      : renderActualPortfolioDraft(state.portfolioDraft || defaultPortfolioDraft()),
    transactions: () => state.portfolioDetail?.position_mode === "actual"
      ? renderActualPortfolioLedger(state.portfolioDetail, {transactionsOnly: true})
      : '<p class="product-empty">当前是目标配置，不是真实持仓账本，暂无交易流水。</p>',
    risk: renderPortfolioAssessment, performance: renderPortfolioPerformance,
    stress: () => '<div data-stress-workbench></div>',
    optimizer: () => '<div data-allocation-workbench></div>',
    events: renderPortfolioEventImpacts,
    monitoring: renderPortfolioMonitoring, research: renderPortfolioResearch, decisions: renderPortfolioDecisions,
  };
  const period = state.selectedPortfolioId && page in state.portfolioPeriods
    ? `<div class="portfolio-page-period"><span>${page === "performance" ? "业绩区间" : "研究区间"}</span>
        <div class="period-control" role="group" aria-label="${item.label}区间">
          ${[6, 12, 36].map(months => `<button type="button" data-portfolio-period="${months}" aria-pressed="${state.portfolioPeriods[page] === months}" class="${state.portfolioPeriods[page] === months ? "active" : ""}">${months === 6 ? "6月" : months === 12 ? "1年" : "3年"}</button>`).join("")}
        </div></div>`
    : "";
  const tools = page === "risk" ? `<nav class="product-toolbar" aria-label="风险分析工具">
    <a href="#assets/stress"><i data-lucide="activity"></i>压力测试</a>
    <a href="#assets/optimizer"><i data-lucide="sliders-horizontal"></i>调整配置</a>
    <a href="#assets/events">相关事件</a><a href="#assets/monitoring">风险跟踪</a></nav>` : "";
  return tools + period + renderers[page]();
}

function renderPortfolioEditor(draft) {
  if (state.selectedPortfolioId && state.portfolioDetail?.position_mode === "actual") {
    return renderActualPortfolioLedger(state.portfolioDetail);
  }
  if (state.selectedPortfolioId && state.portfolioDetail?.position_mode === "allocation") {
    return renderPendingPositions(state.portfolioDetail);
  }
  return renderActualPortfolioDraft(draft);
}

function renderPendingPositions(portfolio) {
  // 迁移前保留服务器上的原配置，但金额一律待补录，不把历史比例当作真实持仓。
  if (!portfolio) return portfolioStatus("loader-circle", "正在读取配置", true);
  return `<header class="product-section-heading"><h2>${escapeHtml(portfolio.name)}</h2><a href="#assets/configuration"><i data-lucide="pencil"></i>补录实际持仓</a></header>
    <p class="product-provenance">原组合未记录真实份额与成本，市值与盈亏暂不可计算。</p>
    ${renderHoldingsQuoteToolbar()}
    <div class="product-table-scroll holdings-table-scroll" role="region" tabindex="0" aria-label="待补录持仓表"><table class="product-data-table holdings-table"><thead><tr><th>基金</th><th>最新日涨跌</th><th>持有份额</th><th>持仓成本</th><th>当前市值</th><th>累计盈亏</th></tr></thead><tbody>
    ${(portfolio.holdings || []).map(holding => {
      const fund = state.funds.find(row => row.fund_code === holding.fund_code);
      const quotes = window.FundScopeHoldingsQuotes;
      return `<tr><th scope="row">${quotes.identity(holding, fund)}</th>
        <td>${quotes.daily(fund)}</td><td>待录入</td><td>待录入</td><td>待计算</td><td>待计算</td></tr>`;
    }).join("")}</tbody></table></div>`;
}

function renderHoldingsQuoteToolbar() {
  const record = state.holdingsQuoteStatus;
  const status = record?.userId === state.userId && record?.portfolioId === state.selectedPortfolioId ? record : null;
  const busy = status?.busy || state.fundSyncing;
  return `<div class="holding-quotes-toolbar">
    <div><span class="holding-quote-label">已披露净值</span><p>日涨跌对应所列净值日期，非盘中估值。</p></div>
    <div class="holding-quote-actions"><button class="secondary-action compact" type="button" data-sync-holdings ${busy ? "disabled" : ""} title="同步全部已关注基金的最新净值与持仓披露">
      <i data-lucide="${busy ? "loader-circle" : "cloud-download"}"${busy ? ' class="spinning"' : ""}></i><span>${busy ? "更新中" : "更新净值"}</span></button>
      <button class="icon-button" type="button" data-reload-holdings ${busy ? "disabled" : ""} title="仅重新读取保存的净值" aria-label="重新读取持仓净值"><i data-lucide="refresh-cw"></i></button></div>
    </div>${status?.message ? `<p class="holding-quote-status" role="status" data-error="${Boolean(status.error)}">${escapeHtml(status.message)}</p>` : ""}`;
}

function renderActualPortfolioDraft(draft) {
  const converting = Boolean(state.selectedPortfolioId);
  const selectedCodes = Object.keys(draft.holdings || {});
  const holdingRows = selectedCodes.map((fundCode) => {
    // 基金目录暂未加载时也保留草稿行，不能让已选基金无声消失。
    const fund = state.funds.find((item) => item.fund_code === fundCode)
      || state.portfolioDetail?.holdings?.find(item => item.fund_code === fundCode)
      || {fund_code: fundCode, short_name: fundCode};
    const position = draft.holdings[fundCode];
    return `
      <div class="position-draft-row" data-position-row="${escapeAttribute(fundCode)}">
        <div class="position-draft-fund">
          ${window.FundScopeHoldingsQuotes.identity(fund)}
          <button class="icon-button danger-subtle" type="button" data-remove-draft-fund="${escapeAttribute(fundCode)}" title="移除基金" aria-label="移除${escapeAttribute(fund.short_name)}">
            <i data-lucide="x"></i>
          </button>
        </div>
        <div class="position-draft-fields">
          <label>
            <span>持有份额（份）</span>
            <input type="number" min="0.0001" step="0.0001" value="${escapeAttribute(position.units)}" data-position-units inputmode="decimal" required aria-label="${escapeAttribute(fund.short_name)}持有份额" />
          </label>
          <label>
            <span>持仓成本（元）</span>
            <input type="number" min="0.01" step="0.01" value="${escapeAttribute(position.cost_amount)}" data-position-cost inputmode="decimal" required aria-label="${escapeAttribute(fund.short_name)}持仓成本" />
          </label>
          <label>
            <span>首次买入</span>
            <input type="date" max="${localIsoDate()}" value="${escapeAttribute(position.purchase_date)}" data-position-date required aria-label="${escapeAttribute(fund.short_name)}首次买入日期" />
          </label>
        </div>
      </div>
    `;
  }).join("");
  const availableFunds = state.funds.filter((fund) => !selectedCodes.includes(fund.fund_code));

  return `
    <form class="portfolio-editor actual-portfolio-draft" data-portfolio-form>
      <header class="product-section-heading"><h2>${converting ? "补录实际持仓" : "新建真实持仓"}</h2></header>
      ${converting ? '<p class="product-provenance">原组合仍保留。确认保存后，以实际份额和成本替换原目标配置。</p>' : ""}
      <label class="portfolio-name-field">
        <span>组合名称</span>
        <input type="text" maxlength="80" value="${escapeAttribute(draft.name)}" data-portfolio-name required />
      </label>
      <div class="position-draft-add">
        <label>
          <span>添加基金</span>
          <select data-position-fund-picker ${availableFunds.length ? "" : "disabled"}>
            ${availableFunds.length ? "" : '<option value="">已添加全部关注基金</option>'}
            ${availableFunds.map((fund) => `
              <option value="${escapeAttribute(fund.fund_code)}">${escapeHtml(fund.short_name)} · ${escapeHtml(fund.fund_code)}</option>
            `).join("")}
          </select>
        </label>
        <button class="icon-button" type="button" data-add-draft-fund title="添加基金" aria-label="添加基金" ${availableFunds.length ? "" : "disabled"}>
          <i data-lucide="plus"></i>
        </button>
      </div>
      <div class="position-draft-list">
        ${holdingRows || portfolioStatus("circle-plus", "请先从基金目录添加一只基金")}
      </div>
      <p class="product-provenance">成本为当前剩余份额对应的成本。未录入的历史卖出、分红等收益不计入累计盈亏。</p>
      <footer class="portfolio-editor-footer">
        <div>
          <span>持仓成本合计</span>
          <strong data-portfolio-total>¥0.00</strong>
        </div>
        <button class="primary-action compact" type="submit" data-save-portfolio disabled>
          <i data-lucide="wallet-cards"></i>
          <span>${state.portfolioSaving ? "保存中" : converting ? "确认并转为真实持仓" : "保存实际持仓"}</span>
        </button>
      </footer>
    </form>
  `;
}

function renderActualPortfolioLedger(portfolio, {transactionsOnly = false} = {}) {
  if (transactionsOnly) return window.FundScopeBookkeeping.render(bookkeepingContext(portfolio));
  const holdings = portfolio.holdings || [];
  const pnlTone = valueTone(portfolio.total_pnl_amount);
  const holdingsMarkup = holdings.length
    ? holdings.map(renderActualHolding).join("")
    : portfolioStatus("badge-check", "当前已全部卖出，可继续录入新的买入");
  // 首屏聚焦账本结果和持仓；收益贡献、成本口径按需展开，数据仍来自原有接口。
  const trend = state.portfolioPerformanceLoading
    ? '<p class="dv-chart-empty" role="status">正在读取收益曲线…</p>'
    : state.portfolioPerformance
      ? window.FundScopeDataVisuals.trend(state.portfolioPerformance, {compact:true})
      : `<p class="dv-attention" role="status">收益曲线暂不可用：${escapeHtml(state.portfolioPerformanceError || "尚未读取业绩")}</p>`;

  return `
    <div class="portfolio-editor actual-portfolio-ledger">
      <dl class="portfolio-value-summary">
        <div><dt>当前市值</dt><dd>${formatMoney(portfolio.total_market_value)}</dd></div>
        <div data-tone="${pnlTone}"><dt>累计盈亏</dt><dd>${formatSignedMoney(portfolio.total_pnl_amount)}</dd></div>
        <div data-tone="${pnlTone}"><dt>累计收益率</dt><dd>${formatSignedPercent(portfolio.total_return_pct)}</dd></div>
      </dl>
      <div class="dv-toolbar asset-value-note"><span class="dv-note">浮动盈亏 ${formatSignedMoney(portfolio.unrealized_pnl_amount)} · 已实现 ${formatSignedMoney(portfolio.realized_pnl_amount)}</span><a href="#assets/review">收益与风险<i data-lucide="arrow-right"></i></a></div>
      <div class="asset-overview-grid"><div class="asset-overview-trend">${trend}</div>${window.FundScopeDataVisuals.holdings(portfolio, {view:"allocation"})}</div>
      <section class="actual-position-section">
        <header><span>我的基金 · ${portfolio.holding_count} 只</span><strong>持仓成本 ${formatMoney(portfolio.total_cost_amount)}</strong></header>
        ${renderHoldingsQuoteToolbar()}
        <div class="actual-position-list">${holdingsMarkup}</div>
      </section>
      ${holdings.length ? `<details class="dv-detail asset-contribution-detail"><summary>盈亏贡献排行</summary>${window.FundScopeDataVisuals.holdings(portfolio, {view:"contribution"})}</details>` : ""}
      <details class="dv-detail"><summary>盈亏口径与成本</summary>
        <p>累计盈亏 = 当前浮动盈亏 + 已记录交易的已实现盈亏；未录入的历史卖出、分红等收益不包含在内。上方收益曲线为时间加权收益，与账本累计收益率的计算口径不同。</p>
        <p>当前持仓成本 ${formatMoney(portfolio.total_cost_amount)} · 浮动盈亏 ${formatSignedMoney(portfolio.unrealized_pnl_amount)} · 已实现盈亏 ${formatSignedMoney(portfolio.realized_pnl_amount)}</p>
      </details>
      <details class="product-ledger-settings"><summary>账本设置</summary>
      <form class="portfolio-rename-form" data-portfolio-rename-form>
        <label class="portfolio-name-field">
          <span>组合名称</span>
          <input type="text" name="name" maxlength="80" value="${escapeAttribute(portfolio.name)}" />
        </label>
        <button class="icon-button" type="submit" title="保存名称" aria-label="保存组合名称" ${state.portfolioSaving ? "disabled" : ""}>
          <i data-lucide="check"></i>
        </button>
      </form>
      <p class="dv-note">账本保留交易历史。误录请在记账与流水中更正。</p>
      </details>
    </div>
  `;
}

function renderActualHolding(holding) {
  const fund = state.funds.find(item => item.fund_code === holding.fund_code);
  const quotes = window.FundScopeHoldingsQuotes;
  return `
    <article class="actual-position-row">
      <header class="dv-position-main">
        ${quotes.identity(holding, fund)}
        <div><small>最新日涨跌</small>${quotes.daily(fund)}</div>
        <div><small>当前市值</small><strong>${formatMoney(holding.current_value)}</strong></div>
        <div><small>累计盈亏</small><strong data-dv-tone="${valueTone(holding.total_pnl_amount)}">${formatSignedMoney(holding.total_pnl_amount)}</strong></div>
      </header>
      <details class="dv-detail"><summary>份额、成本与区间涨跌</summary>
      <dl>
        <div><dt>持有份额</dt><dd>${formatUnits(holding.units)} 份</dd></div>
        <div><dt>待确认卖出占用</dt><dd>${formatUnits(holding.reserved_units || 0)} 份</dd></div>
        <div><dt>可用份额</dt><dd>${formatUnits(holding.available_units ?? holding.units)} 份</dd></div>
        <div><dt>持仓成本</dt><dd>${formatMoney(holding.cost_amount)}</dd></div>
        <div><dt>平均成本</dt><dd>${formatNav(holding.average_cost_nav)}</dd></div>
        <div><dt>最新净值</dt><dd>${formatNav(holding.latest_nav)}</dd></div>
        <div><dt>当前市值</dt><dd>${formatMoney(holding.current_value)}</dd></div>
        <div data-tone="${valueTone(holding.unrealized_pnl_amount)}"><dt>浮动盈亏</dt><dd>${formatSignedMoney(holding.unrealized_pnl_amount)}</dd></div>
        <div data-tone="${valueTone(holding.realized_pnl_amount)}"><dt>已实现盈亏</dt><dd>${formatSignedMoney(holding.realized_pnl_amount)}</dd></div>
        <div><dt>市值占比</dt><dd>${formatPlainPercent(holding.weight_pct)}</dd></div>
        <div><dt>近30日</dt><dd>${quotes.change(fund?.return_30d_pct, fund?.return_basis_label)}</dd></div>
        <div><dt>近3个月</dt><dd>${quotes.change(fund?.return_3m_pct, fund?.return_basis_label)}</dd></div>
      </dl>
      <footer>
        <span>首次买入 ${formatDateOnly(holding.first_purchase_date)} · 净值日期 ${formatDateOnly(holding.latest_nav_date)}</span>
        <span data-tone="${valueTone(holding.unrealized_pnl_amount)}">持仓收益率 ${formatSignedPercent(holding.unrealized_pnl_pct)}</span>
      </footer>
      </details>
    </article>
  `;
}

function renderPortfolioAssessment() {
  if (!state.selectedPortfolioId) {
    return `
      <div class="portfolio-empty-analysis">
        <i data-lucide="chart-pie"></i>
        <h3>保存后生成组合研判</h3>
        <p>系统会复用每只基金的风险、市场和事件证据，并按当前市值权重计算贡献。</p>
      </div>
    `;
  }
  if (state.portfolioAssessmentLoading) {
    return `<div class="portfolio-empty-analysis">${portfolioStatus("loader-circle", "正在汇总组合证据", true)}</div>`;
  }
  if (!state.portfolioAssessment || state.portfolioAssessmentError) {
    return `
      <div class="portfolio-empty-analysis error">
        ${portfolioStatus("triangle-alert", state.portfolioAssessmentError || "组合研判暂不可用")}
        <button class="secondary-action compact" type="button" data-portfolio-period="${state.portfolioPeriods.risk}">重试</button>
      </div>
    `;
  }

  const assessment = state.portfolioAssessment;
  return `
    <article class="portfolio-assessment" data-state="${escapeAttribute(assessment.state_code)}">
      <header class="portfolio-assessment-header">
        <div>
          <span class="portfolio-state-label">确定性组合研判 · ${escapeHtml(assessment.state_label)}</span>
          <h3>${escapeHtml(assessment.portfolio_name)}</h3>
          <p>${escapeHtml(assessment.summary)}</p>
        </div>
      </header>
      <div class="portfolio-metric-grid">
        ${portfolioMetric("shield-alert", "风险证据", assessment.score_breakdown.caution_points.toFixed(2), "caution")}
        ${portfolioMetric("circle-check", "支撑证据", assessment.score_breakdown.supportive_points.toFixed(2), "supportive")}
        ${portfolioMetric("scan-line", "最大权重", `${assessment.concentration.max_weight_pct.toFixed(2)}%`, "neutral")}
        ${portfolioMetric("layers-3", "有效基金数", assessment.concentration.effective_fund_count.toFixed(2), "neutral")}
      </div>
      <div class="portfolio-analysis-grid">
        <section class="portfolio-allocation-section">
          <h4>配置结构</h4>
          ${assessment.allocations.map((item) => `
            <div class="portfolio-allocation-row">
              <div><span>${escapeHtml(item.label)}</span><strong>${item.weight_pct.toFixed(2)}%</strong></div>
              <span class="portfolio-allocation-track"><span style="width:${Math.min(item.weight_pct, 100)}%"></span></span>
            </div>
          `).join("")}
          <small>HHI ${assessment.concentration.hhi.toFixed(4)} · ${escapeHtml(assessment.concentration.state_label)}</small>
        </section>
        <section class="portfolio-contribution-section">
          <h4>基金贡献</h4>
          ${assessment.fund_contributions.map((item) => `
            <div class="portfolio-contribution-row">
              <div>
                <strong>${escapeHtml(item.short_name)}</strong>
                <span>${escapeHtml(item.fund_code)} · 权重 ${item.weight_pct.toFixed(2)}%</span>
              </div>
              <span class="portfolio-contribution-score">风险 ${item.weighted_caution_points.toFixed(2)} / 支撑 ${item.weighted_supportive_points.toFixed(2)}</span>
            </div>
          `).join("")}
        </section>
      </div>
      <div class="portfolio-evidence-grid">
        ${portfolioEvidenceGroup("主要风险证据", assessment.risk_evidence, "caution")}
        ${portfolioEvidenceGroup("主要支撑证据", assessment.supporting_evidence, "supportive")}
      </div>
      <footer class="portfolio-assessment-footer">
        <span>数据截至 ${formatDateOnly(assessment.assessment_date)} · ${assessment.evidence_count} 条去重证据</span>
        <button class="primary-action compact" type="button" data-analyze-portfolio>
          <i data-lucide="message-square-more"></i><span>交给 Agent 解读</span>
        </button>
      </footer>
      <p class="portfolio-disclaimer">${escapeHtml(assessment.disclaimer)} 上方规则证据分不等同于真实组合波动率；量化指标以组合研究报告为准。</p>
    </article>
  `;
}

function renderPortfolioPerformance() {
  if (state.portfolioPerformanceLoading && !state.portfolioPerformance) {
    return `
      <section class="portfolio-performance" data-status="loading" aria-busy="true">
        ${portfolioStatus("chart-spline", "正在重放交易并计算组合业绩", true)}
      </section>
    `;
  }
  if (!state.portfolioPerformance) {
    return `
      <section class="portfolio-performance" data-status="error">
        <header class="portfolio-performance-header">
          <div><i data-lucide="chart-spline"></i><strong>组合业绩与归因</strong></div>
          <button class="icon-button subtle" type="button" data-reload-portfolio-performance title="重新计算组合业绩" aria-label="重新计算组合业绩">
            <i data-lucide="refresh-cw"></i>
          </button>
        </header>
        <div class="portfolio-inline-error"><i data-lucide="triangle-alert"></i>${escapeHtml(state.portfolioPerformanceError || "组合业绩暂不可用")}</div>
      </section>
    `;
  }

  const performance = state.portfolioPerformance;
  const benchmark = performance.benchmark || {};
  const actual = performance.position_mode === "actual";
  const benchmarkStatus = {
    matched: "已匹配",
    proxy: "含代理分量",
    insufficient: "覆盖不足",
    unavailable: "暂不可用",
  }[benchmark.status] || "状态未知";
  const cashFlows = performance.cash_flows || {};

  return `
    <section class="portfolio-performance" data-status="${escapeAttribute(performance.data_kind)}">
      <header class="portfolio-performance-header">
        <div>
          <i data-lucide="chart-spline"></i>
          <span>
            <strong>组合业绩与归因</strong>
            <small>${escapeHtml(performance.performance_label)} · ${formatDateOnly(performance.period_start)} 至 ${formatDateOnly(performance.period_end)}</small>
          </span>
        </div>
        <span class="portfolio-performance-kind" data-actual="${actual ? "true" : "false"}">${actual ? "真实流水" : "配置模拟"}</span>
      </header>
      <div class="portfolio-performance-metrics">
        ${portfolioPerformanceMetric("TWR", performance.twr_pct, "percent", valueTone(performance.twr_pct))}
        ${portfolioPerformanceMetric("XIRR", performance.xirr_pct, "percent", valueTone(performance.xirr_pct))}
        ${portfolioPerformanceMetric("超额收益", benchmark.excess_return_pct, "percent", valueTone(benchmark.excess_return_pct))}
        ${portfolioPerformanceMetric("最大回撤", performance.max_drawdown?.drawdown_pct, "percent", "negative")}
        ${portfolioPerformanceMetric("夏普", performance.sharpe_ratio, "ratio", valueTone(performance.sharpe_ratio))}
        ${portfolioPerformanceMetric("Sortino", performance.sortino_ratio, "ratio", valueTone(performance.sortino_ratio))}
      </div>
      <div class="portfolio-performance-chart-grid">
        <figure class="portfolio-performance-chart">
          <figcaption>
            <span>组合与业绩基准</span>
            <small>${escapeHtml(benchmark.benchmark_name || "组合业绩基准")} · ${escapeHtml(benchmarkStatus)}</small>
          </figcaption>
          ${renderPortfolioPerformanceChart(performance.series || [])}
          <div class="portfolio-chart-legend">
            <span data-series="portfolio"><i></i>组合</span>
            <span data-series="benchmark"><i></i>基准</span>
          </div>
        </figure>
        <figure class="portfolio-drawdown-chart">
          <figcaption>
            <span>水下回撤</span>
            <small>${performance.observation_count} 个日频样本 · 年化波动 ${formatPlainPercent(performance.annualized_volatility_pct)}</small>
          </figcaption>
          ${renderPortfolioDrawdownChart(performance.series || [])}
        </figure>
      </div>
      ${actual ? `
        <dl class="portfolio-cash-flow-strip">
          <div><dt>区间期初</dt><dd>${formatMoney(cashFlows.opening_value)}</dd></div>
          <div><dt>新增投入</dt><dd>${formatMoney(cashFlows.contributions)}</dd></div>
          <div><dt>资金取出</dt><dd>${formatMoney(cashFlows.withdrawals)}</dd></div>
          <div><dt>累计费用</dt><dd>${formatMoney(cashFlows.fees)}</dd></div>
          <div><dt>模型期末价值</dt><dd>${formatMoney(cashFlows.ending_value)}</dd></div>
        </dl>
      ` : ""}
      ${renderPortfolioReturnAttribution(performance.attribution || [], actual)}
      <footer class="portfolio-performance-footer">
        <span><i data-lucide="badge-info"></i>无风险利率 ${formatPlainPercent(performance.risk_free_rate_annual_pct)} · 基准覆盖 ${Number(benchmark.coverage_pct || 0).toFixed(0)}%</span>
        ${(performance.warnings || []).length ? `
          <details>
            <summary>${performance.warnings.length} 条口径提醒</summary>
            <ul>${performance.warnings.map((item) => `<li>${escapeHtml(item)}</li>`).join("")}</ul>
          </details>
        ` : ""}
      </footer>
    </section>
  `;
}

function portfolioPerformanceMetric(label, value, format, tone) {
  const available = value !== null && value !== undefined && Number.isFinite(Number(value));
  const text = !available
    ? "-"
    : format === "percent"
      ? formatSignedPercent(value)
      : Number(value).toFixed(3);
  return `
    <div data-tone="${escapeAttribute(available ? tone : "neutral")}">
      <span>${escapeHtml(label)}</span>
      <strong>${escapeHtml(text)}</strong>
    </div>
  `;
}

function samplePortfolioSeries(series, limit = 180) {
  if (series.length <= limit) return series;
  const step = (series.length - 1) / (limit - 1);
  return Array.from({ length: limit }, (_, index) => (
    series[Math.round(index * step)]
  ));
}

function portfolioChartFrame({ label, width, height, drawing, ticks, points }) {
  // 曲线可以随容器伸缩，但刻度改用 HTML，始终保持 11px 实际字号。
  // 刻度列保留固定宽度，避免窄图中的负号、百分号被裁切或压到曲线上。
  return `
    <div class="portfolio-chart-frame">
      <div class="portfolio-chart-ticks">${ticks.map((tick) => `<span style="top:${tick.y / height * 100}%">${escapeHtml(tick.label)}</span>`).join("")}</div>
      <svg class="portfolio-chart-canvas" viewBox="0 0 ${width} ${height}" preserveAspectRatio="none" role="img" aria-label="${escapeAttribute(label)}">${drawing}</svg>
      <div class="portfolio-chart-dates"><span>${escapeHtml(formatDateOnly(points[0].date))}</span><span>${escapeHtml(formatDateOnly(points[points.length - 1].date))}</span></div>
    </div>
  `;
}

function renderPortfolioPerformanceChart(series) {
  const points = samplePortfolioSeries(series);
  if (points.length < 2) return portfolioStatus("chart-no-axes-column", "收益样本不足");
  const width = 720;
  const height = 180;
  const padding = { left: 0, right: 0, top: 8, bottom: 8 };
  const values = points.flatMap((point) => [
    Number(point.portfolio_index),
    point.benchmark_index === null ? null : Number(point.benchmark_index),
  ]).filter(Number.isFinite);
  let minValue = Math.min(...values, 100);
  let maxValue = Math.max(...values, 100);
  const valuePadding = Math.max((maxValue - minValue) * 0.12, 0.8);
  minValue -= valuePadding;
  maxValue += valuePadding;
  const x = (index) => padding.left + index / (points.length - 1) * (width - padding.left - padding.right);
  const y = (value) => padding.top + (maxValue - value) / (maxValue - minValue) * (height - padding.top - padding.bottom);
  const portfolioPath = points.map((point, index) => (
    `${index ? "L" : "M"}${x(index).toFixed(2)},${y(Number(point.portfolio_index)).toFixed(2)}`
  )).join(" ");
  let benchmarkPath = "";
  let drawing = false;
  points.forEach((point, index) => {
    const value = point.benchmark_index;
    if (value === null || !Number.isFinite(Number(value))) {
      drawing = false;
      return;
    }
    benchmarkPath += `${drawing ? " L" : " M"}${x(index).toFixed(2)},${y(Number(value)).toFixed(2)}`;
    drawing = true;
  });
  const ticks = [0, 0.5, 1].map((ratio) => {
    const value = maxValue - (maxValue - minValue) * ratio;
    return { y: y(value), label: value.toFixed(1) };
  });
  const grid = ticks.map((tick) => `<line x1="0" y1="${tick.y.toFixed(2)}" x2="${width}" y2="${tick.y.toFixed(2)}"></line>`).join("");
  return portfolioChartFrame({ label: "组合与业绩基准指数曲线", width, height, ticks, points, drawing: `
      <g class="portfolio-chart-gridlines">${grid}</g>
      <path class="portfolio-index-line" d="${portfolioPath}"></path>
      ${benchmarkPath ? `<path class="portfolio-benchmark-line" d="${benchmarkPath.trim()}"></path>` : ""}
  ` });
}

function renderPortfolioDrawdownChart(series) {
  const points = samplePortfolioSeries(series);
  if (points.length < 2) return portfolioStatus("chart-no-axes-column", "回撤样本不足");
  const width = 720;
  const height = 180;
  const padding = { left: 0, right: 0, top: 8, bottom: 8 };
  const minValue = Math.min(...points.map((point) => Number(point.drawdown_pct)), -0.1);
  const x = (index) => padding.left + index / (points.length - 1) * (width - padding.left - padding.right);
  const y = (value) => padding.top + (0 - value) / (0 - minValue) * (height - padding.top - padding.bottom);
  const line = points.map((point, index) => (
    `${index ? "L" : "M"}${x(index).toFixed(2)},${y(Number(point.drawdown_pct)).toFixed(2)}`
  )).join(" ");
  const area = `${line} L${x(points.length - 1).toFixed(2)},${y(0).toFixed(2)} L${x(0).toFixed(2)},${y(0).toFixed(2)} Z`;
  const ticks = [{ y: y(0), label: "0%" }, { y: y(minValue), label: `${minValue.toFixed(1)}%` }];
  return portfolioChartFrame({ label: "组合历史回撤曲线", width, height, ticks, points, drawing: `
      <g class="portfolio-chart-gridlines"><line x1="0" y1="${y(0).toFixed(2)}" x2="${width}" y2="${y(0).toFixed(2)}"></line></g>
      <path class="portfolio-drawdown-area" d="${area}"></path>
      <path class="portfolio-drawdown-line" d="${line}"></path>
  ` });
}

function renderPortfolioReturnAttribution(items, actual) {
  const maxValue = Math.max(
    ...items.map((item) => Math.abs(Number(item.return_contribution_pct_points))),
    0.01,
  );
  return `
    <section class="portfolio-return-attribution">
      <header>
        <span>基金收益贡献</span>
        <small>几何链接贡献之和 = 组合 TWR</small>
      </header>
      <div>
        ${items.map((item) => {
          const contribution = Number(item.return_contribution_pct_points);
          const width = Math.max(Math.abs(contribution) / maxValue * 50, 1);
          return `
            <article data-tone="${valueTone(contribution)}">
              <div>
                <strong>${escapeHtml(item.short_name)}</strong>
                <span>${escapeHtml(item.fund_code)} · 期末权重 ${formatPlainPercent(item.ending_weight_pct)}</span>
              </div>
              <span class="portfolio-attribution-track"><i style="width:${width}%"></i></span>
              <div class="portfolio-attribution-value">
                <strong>${formatSignedPercent(contribution)}</strong>
                ${actual ? `<small>${formatSignedMoney(item.pnl_contribution_amount)}</small>` : ""}
              </div>
            </article>
          `;
        }).join("")}
      </div>
    </section>
  `;
}


function renderPortfolioEventImpacts() {
  if (state.portfolioEventImpactsLoading && !state.portfolioEventImpacts) {
    return `
      <section class="portfolio-event-impact" data-status="loading" aria-busy="true">
        ${portfolioStatus("newspaper", "正在关联市场消息与底层持仓", true)}
      </section>
    `;
  }
  if (!state.portfolioEventImpacts) {
    return `
      <section class="portfolio-event-impact" data-status="error">
        <header class="portfolio-impact-header">
          <div><i data-lucide="newspaper"></i><strong>哪些消息影响了我</strong></div>
          <button class="icon-button subtle" type="button" data-reload-portfolio-event-impacts title="重新加载事件归因" aria-label="重新加载事件归因">
            <i data-lucide="refresh-cw"></i>
          </button>
        </header>
        <div class="portfolio-inline-error"><i data-lucide="triangle-alert"></i>${escapeHtml(state.portfolioEventImpactsError || "事件归因暂不可用")}</div>
      </section>
    `;
  }

  const result = state.portfolioEventImpacts;
  const quality = result.quality;
  const qualityLabels = {
    good: "归因充分",
    partial: "部分归因",
    limited: "覆盖有限",
    no_matches: "暂无直接关联",
  };
  const classifiedTotal = quality.classified_stock_exposure_pct
    + quality.unclassified_stock_exposure_pct;
  const classificationRate = classifiedTotal > 0
    ? quality.classified_stock_exposure_pct / classifiedTotal * 100
    : 0;

  return `
    <section class="portfolio-event-impact" data-status="${escapeAttribute(quality.status)}">
      <header class="portfolio-impact-header">
        <div>
          <i data-lucide="newspaper"></i>
          <span>
            <strong>哪些消息影响了我</strong>
            <small>${escapeHtml(qualityLabels[quality.status] || quality.status)} · 事件截至 ${formatDateOnly(result.event_data_cutoff)}</small>
          </span>
        </div>
        <button class="icon-button subtle" type="button" data-reload-portfolio-event-impacts title="刷新事件归因" aria-label="刷新事件归因">
          <i data-lucide="refresh-cw"></i>
        </button>
      </header>
      <p class="portfolio-impact-summary">${escapeHtml(result.summary)}</p>
      <div class="portfolio-impact-metrics">
        ${portfolioImpactMetric("link-2", "持仓相关事件", String(quality.matched_event_count), quality.status)}
        ${portfolioImpactMetric("shield-alert", "风险关注", String(result.caution_event_count), "caution")}
        ${portfolioImpactMetric("circle-check", "支撑事件", String(result.supportive_event_count), "supportive")}
        ${portfolioImpactMetric("tags", "重仓行业已分类", `${classificationRate.toFixed(0)}%`, quality.status)}
      </div>
      ${renderPortfolioFineSectors(result.sector_exposures)}
      <div class="portfolio-impact-list">
        ${result.impacts.length
          ? renderPortfolioImpactItems(result.impacts)
          : `<p class="portfolio-impact-empty">当前有效事件尚未匹配到底层证券、行业或持仓基金。</p>`}
      </div>
      <footer class="portfolio-impact-footer">
        <span><i data-lucide="database"></i>持仓季报 ${formatDateOnly(result.latest_holding_report_date)} · ${escapeHtml(quality.taxonomy_version)}</span>
        <span>关联仓位不等于预计涨跌或亏损比例</span>
      </footer>
    </section>
  `;
}

function renderPortfolioImpactItems(items) {
  // 最重要的五条直接展示，其余证据保留在原位按需展开，避免报告一打开就无限拉长。
  const primaryItems = items.slice(0, 5);
  const secondaryItems = items.slice(5);
  return `
    ${primaryItems.map(renderPortfolioEventImpactItem).join("")}
    ${secondaryItems.length ? `
      <details class="portfolio-impact-more">
        <summary>
          <span><i data-lucide="list-filter"></i>查看其余 ${secondaryItems.length} 条关联事件</span>
          <i data-lucide="chevron-down"></i>
        </summary>
        <div>${secondaryItems.map(renderPortfolioEventImpactItem).join("")}</div>
      </details>
    ` : ""}
  `;
}

function portfolioImpactMetric(icon, label, value, tone) {
  return `
    <div data-tone="${escapeAttribute(tone)}">
      <i data-lucide="${icon}"></i>
      <span>${escapeHtml(label)}</span>
      <strong>${escapeHtml(value)}</strong>
    </div>
  `;
}

function renderPortfolioFineSectors(sectors) {
  const visible = sectors.slice(0, 6);
  const maxValue = visible[0]?.portfolio_exposure_pct || 1;
  return `
    <section class="portfolio-fine-sectors">
      <header>
        <span>底层细分行业</span>
        <small>已披露重仓口径</small>
      </header>
      <div>
        ${visible.length ? visible.map((item) => `
          <article>
            <span><strong>${escapeHtml(item.sector_name)}</strong><small>${item.security_count} 只证券</small></span>
            <i><i style="width:${Math.max(2, item.portfolio_exposure_pct / maxValue * 100)}%"></i></i>
            <strong>${item.portfolio_exposure_pct.toFixed(2)}%</strong>
          </article>
        `).join("") : `<p class="portfolio-impact-empty">已披露股票暂未匹配到细分行业词典。</p>`}
      </div>
    </section>
  `;
}

function renderPortfolioEventImpactItem(item) {
  const directionLabel = {
    caution: "风险关注",
    supportive: "支撑",
    neutral: "中性信息",
  }[item.direction] || "中性信息";
  const directionIcon = {
    caution: "shield-alert",
    supportive: "circle-check",
    neutral: "info",
  }[item.direction] || "info";
  const targets = portfolioImpactTargets(item);

  return `
    <article class="portfolio-impact-row" data-direction="${escapeAttribute(item.direction)}">
      <div class="portfolio-impact-direction" title="${escapeAttribute(directionLabel)}">
        <i data-lucide="${directionIcon}"></i>
      </div>
      <div class="portfolio-impact-copy">
        <header>
          <span>${escapeHtml(directionLabel)} · ${escapeHtml(item.match_label)}</span>
          <small>${formatDateOnly(item.event_date)} · ${escapeHtml(item.source_name)} · ${freshnessStatusLabel(item.freshness_status)}</small>
        </header>
        <strong>${escapeHtml(item.title)}</strong>
        <p>${escapeHtml(item.reason)}</p>
        <div class="portfolio-impact-targets">
          ${targets.map((target) => `<span>${escapeHtml(target)}</span>`).join("")}
        </div>
        <code>${escapeHtml(item.impact_evidence_id)}</code>
      </div>
      <div class="portfolio-impact-value">
        <span>关联仓位</span>
        <strong>${item.affected_exposure_pct.toFixed(2)}%</strong>
        <small>置信度 ${(item.confidence * 100).toFixed(0)}%</small>
        <a href="${escapeAttribute(item.source_url)}" target="_blank" rel="noreferrer" title="查看事件来源" aria-label="查看事件来源"><i data-lucide="external-link"></i></a>
      </div>
    </article>
  `;
}

function portfolioImpactTargets(item) {
  const targets = [];
  item.matched_securities.slice(0, 4).forEach((security) => {
    targets.push(`${security.security_name} ${security.portfolio_exposure_pct.toFixed(2)}%`);
  });
  item.matched_sectors.slice(0, 3).forEach((sector) => {
    targets.push(`${sector.sector_name} ${sector.portfolio_exposure_pct.toFixed(2)}%`);
  });
  item.matched_industries.slice(0, 2).forEach((industry) => {
    targets.push(`${industry.industry_name} ${industry.portfolio_exposure_pct.toFixed(2)}%`);
  });
  item.matched_funds.slice(0, 2).forEach((fund) => {
    targets.push(`${fund.short_name} ${fund.portfolio_weight_pct.toFixed(2)}%`);
  });
  return [...new Set(targets)].slice(0, 6);
}

function freshnessStatusLabel(status) {
  return {
    fresh: "新鲜",
    decaying: "影响衰减中",
    expired: "已过期",
  }[status] || "时效未知";
}

function freshnessLabel(status) {
  return {
    fresh: "最新报告期仍在正常披露窗口",
    aging: "报告期正在变旧",
    stale: "报告期已经过期",
    unavailable: "部分基金缺少报告期",
  }[status] || "报告期未知";
}

function renderPortfolioMonitoring() {
  const overview = state.portfolioMonitoring;
  const job = state.portfolioMonitoringJob;

  if (state.portfolioMonitoringLoading && !overview) {
    return `
      <section class="portfolio-monitoring" data-status="loading" aria-busy="true">
        ${portfolioStatus("loader-circle", "正在读取组合监控", true)}
      </section>
    `;
  }

  if (!overview) {
    return `
      <section class="portfolio-monitoring" data-status="error">
        <header class="portfolio-monitoring-header">
          <div><i data-lucide="radar"></i><span>持续风险监控</span></div>
          <button class="icon-button subtle" type="button" data-reload-portfolio-monitoring title="重新加载监控" aria-label="重新加载监控">
            <i data-lucide="refresh-cw"></i>
          </button>
        </header>
        <div class="portfolio-inline-error"><i data-lucide="triangle-alert"></i>${escapeHtml(state.portfolioMonitoringError || "监控数据暂不可用")}</div>
      </section>
    `;
  }

  const policy = overview.policy;
  const snapshot = overview.latest_snapshot;
  const alerts = overview.alerts || [];
  const openAlerts = alerts.filter((item) => item.status === "open");
  const progress = Number(job?.progress_percent || 0);
  const generation = snapshot?.generation || {};
  const comparison = snapshot?.comparison || {};
  const statusLabelText = policy.enabled ? "定时监控已启用" : "仅手动运行";

  return `
    <section class="portfolio-monitoring" data-status="${state.portfolioMonitoringRunning ? "running" : policy.enabled ? "enabled" : "manual"}">
      <header class="portfolio-monitoring-header">
        <div>
          <i data-lucide="radar"></i>
          <span>持续风险监控</span>
          <strong>${escapeHtml(statusLabelText)}</strong>
        </div>
        <div class="portfolio-monitoring-actions">
          ${openAlerts.length ? `<span class="portfolio-alert-count">${openAlerts.length} 条待处理</span>` : ""}
          <button class="secondary-action compact" type="button" data-run-portfolio-monitor ${state.portfolioMonitoringRunning ? "disabled" : ""}>
            <i data-lucide="${state.portfolioMonitoringRunning ? "loader-circle" : "scan-search"}" class="${state.portfolioMonitoringRunning ? "spinning" : ""}"></i>
            <span>${state.portfolioMonitoringRunning ? `${progress.toFixed(0)}%` : "立即检查"}</span>
          </button>
        </div>
      </header>

      ${state.portfolioMonitoringRunning ? `
        <div class="portfolio-monitoring-progress">
          <span style="width:${Math.min(progress, 100)}%"></span>
          <small>${escapeHtml(job?.progress_message || "等待 Worker 领取监控任务")}</small>
        </div>
      ` : ""}
      ${state.portfolioMonitoringError ? `<div class="portfolio-inline-error"><i data-lucide="triangle-alert"></i>${escapeHtml(state.portfolioMonitoringError)}</div>` : ""}

      ${snapshot ? renderPortfolioMonitoringSnapshot(snapshot, comparison) : `
        <div class="portfolio-monitoring-baseline">
          <i data-lucide="database"></i>
          <div><strong>尚未建立风险基线</strong><span>首次检查会保存当前状态，之后才比较风险变化。</span></div>
        </div>
      `}

      ${generation.status && generation.status !== "skipped" ? `
        <div class="portfolio-monitoring-explanation" data-status="${escapeAttribute(generation.status)}">
          <i data-lucide="brain-circuit"></i>
          <div>
            <strong>${escapeHtml(generation.headline || "风险变化说明")}</strong>
            <p>${escapeHtml(generation.summary || "")}</p>
          </div>
          <span>${generation.model_used ? "AI 解释" : "规则说明"}</span>
        </div>
      ` : ""}

      ${renderPortfolioAlerts(alerts)}
      ${renderPortfolioMonitoringPolicy(policy)}
    </section>
  `;
}

function renderPortfolioMonitoringSnapshot(snapshot, comparison) {
  const baseline = Boolean(comparison.baseline_created);
  const changeCount = Number(comparison.change_count || 0);
  const changeLabel = baseline
    ? "风险基线"
    : changeCount
      ? `${changeCount} 类变化达到阈值`
      : "未发现显著变化";
  const cautionImpacts = (snapshot.event_impacts || [])
    .filter((item) => item.direction === "caution")
    .sort((left, right) => (
      Number(right.affected_exposure_pct || 0) - Number(left.affected_exposure_pct || 0)
    ));
  const topCautionImpact = cautionImpacts[0] || null;

  return `
    <div class="portfolio-monitoring-snapshot">
      <div class="portfolio-monitoring-snapshot-heading">
        <div><span>最近检查</span><strong>${escapeHtml(changeLabel)}</strong></div>
        <time>${formatDateTime(snapshot.observed_at)}</time>
      </div>
      <div class="portfolio-monitoring-metrics">
        <div><span>风险状态</span><strong>${escapeHtml(snapshot.state_label)}</strong></div>
        <div><span>最大回撤</span><strong>${Number(snapshot.max_drawdown_pct).toFixed(2)}%</strong></div>
        <div><span>年化波动</span><strong>${Number(snapshot.annualized_volatility_pct).toFixed(2)}%</strong></div>
        <div><span>最大权重</span><strong>${Number(snapshot.max_weight_pct).toFixed(2)}%</strong></div>
      </div>
      ${topCautionImpact ? `
        <div class="portfolio-monitoring-impact-summary">
          <i data-lucide="radio-tower"></i>
          <div>
            <span>当前有 ${cautionImpacts.length} 条风险事件可关联真实持仓</span>
            <strong>${escapeHtml(topCautionImpact.title)}</strong>
          </div>
          <b>影响 ${Number(topCautionImpact.affected_exposure_pct).toFixed(2)}%</b>
        </div>
      ` : ""}
    </div>
  `;
}

function renderPortfolioAlerts(alerts) {
  if (!alerts.length) {
    return `
      <div class="portfolio-alerts-empty">
        <i data-lucide="circle-check"></i>
        <span>当前没有达到阈值的风险变化</span>
      </div>
    `;
  }

  return `
    <div class="portfolio-alert-list">
      <div class="portfolio-alert-list-header">
        <strong>风险变化</strong>
        <button class="secondary-action compact" type="button" data-monitor-research ${state.portfolioResearchRunning ? "disabled" : ""}>
          <i data-lucide="workflow"></i><span>重新研究</span>
        </button>
      </div>
      ${alerts.map((alert) => {
        const impactEvents = alert.alert_type === "portfolio_event_exposure_changed"
          ? (alert.metric_data?.events || []).slice(0, 3)
          : [];
        return `
        <article class="portfolio-alert-row" data-severity="${escapeAttribute(alert.severity)}" data-status="${escapeAttribute(alert.status)}">
          <span class="portfolio-alert-marker"><i data-lucide="${alert.severity === "high" ? "shield-alert" : "triangle-alert"}"></i></span>
          <div>
            <div class="portfolio-alert-title">
              <strong>${escapeHtml(alert.title)}</strong>
              <span>${alert.severity === "high" ? "高关注" : "需关注"}</span>
            </div>
            <p>${escapeHtml(alert.detail)}</p>
            ${impactEvents.length ? `
              <div class="portfolio-alert-evidence">
                ${impactEvents.map((item) => `
                  <a href="${escapeAttribute(item.source_url || "#")}" ${item.source_url ? 'target="_blank" rel="noreferrer"' : 'aria-disabled="true"'}>
                    <span>${escapeHtml(item.match_label || "持仓关联")}</span>
                    <strong>${Number(item.affected_exposure_pct || 0).toFixed(2)}%</strong>
                    <i data-lucide="external-link"></i>
                  </a>
                `).join("")}
              </div>
            ` : ""}
            <small>${formatDateTime(alert.created_at)} · ${alert.status === "open" ? "待处理" : "已读"}</small>
          </div>
          ${alert.status === "open" ? `
            <button class="icon-button subtle" type="button" data-acknowledge-alert="${escapeAttribute(alert.id)}" title="标记为已读" aria-label="标记预警为已读">
              <i data-lucide="check"></i>
            </button>
          ` : '<i class="portfolio-alert-read" data-lucide="check-check"></i>'}
        </article>
      `;
      }).join("")}
    </div>
  `;
}

function renderPortfolioMonitoringPolicy(policy) {
  return `
    <details class="portfolio-monitoring-policy">
      <summary>
        <span><i data-lucide="settings-2"></i>监控策略</span>
        <small>${escapeHtml(policy.schedule_cron)}</small>
      </summary>
      <form data-portfolio-monitoring-form>
        <div class="portfolio-monitoring-switches">
          <label>
            <input type="checkbox" name="enabled" ${policy.enabled ? "checked" : ""} />
            <span><strong>定时监控</strong><small>按执行计划自动生成风险快照</small></span>
          </label>
          <label>
            <input type="checkbox" name="alert_on_risk_state_worsening" ${policy.alert_on_risk_state_worsening ? "checked" : ""} />
            <span><strong>状态上升提醒</strong><small>组合风险等级变差时创建预警</small></span>
          </label>
        </div>
        <div class="portfolio-monitoring-policy-grid">
          ${portfolioMonitoringSelect("risk_preference", "风险偏好", policy.risk_preference, [
            ["conservative", "稳健"], ["balanced", "均衡"], ["growth", "成长"],
          ])}
          ${portfolioMonitoringSelect("investment_horizon", "投资期限", policy.investment_horizon, [
            ["under_1y", "1 年内"], ["one_to_three_years", "1-3 年"], ["over_3y", "3 年以上"],
          ])}
          ${portfolioMonitoringSelect("monitor_months", "观察区间", String(policy.monitor_months), [
            ["6", "近 6 月"], ["12", "近 1 年"], ["36", "近 3 年"],
          ])}
          ${portfolioMonitoringNumber("max_single_adjustment_pct", "单次调整上限", policy.max_single_adjustment_pct, 1, 50, 1, "%")}
          ${portfolioMonitoringNumber("drawdown_alert_pct_points", "回撤扩大阈值", policy.drawdown_alert_pct_points, 0.5, 30, 0.5, "pp")}
          ${portfolioMonitoringNumber("concentration_alert_pct_points", "集中度上升阈值", policy.concentration_alert_pct_points, 1, 50, 1, "pp")}
          ${portfolioMonitoringNumber("major_event_heat_threshold", "重大事件热度", policy.major_event_heat_threshold, 0, 100, 1, "")}
          ${portfolioMonitoringNumber("event_impact_exposure_threshold_pct", "事件影响仓位阈值", policy.event_impact_exposure_threshold_pct, 1, 100, 1, "%")}
          <label class="portfolio-monitoring-field">
            <span>执行计划</span>
            <input type="text" name="schedule_cron" maxlength="100" value="${escapeAttribute(policy.schedule_cron)}" required />
          </label>
        </div>
        <footer>
          <span><i data-lucide="shield-off"></i>监控只生成预警，不会自动研究或交易</span>
          <button class="primary-action compact" type="submit" ${state.portfolioMonitoringSaving ? "disabled" : ""}>
            <i data-lucide="${state.portfolioMonitoringSaving ? "loader-circle" : "save"}" class="${state.portfolioMonitoringSaving ? "spinning" : ""}"></i>
            <span>${state.portfolioMonitoringSaving ? "保存中" : "保存策略"}</span>
          </button>
        </footer>
      </form>
    </details>
  `;
}

function portfolioMonitoringSelect(name, label, current, options) {
  return `
    <label class="portfolio-monitoring-field">
      <span>${escapeHtml(label)}</span>
      <select name="${escapeAttribute(name)}">
        ${options.map(([value, text]) => `
          <option value="${escapeAttribute(value)}"${String(current) === value ? " selected" : ""}>${escapeHtml(text)}</option>
        `).join("")}
      </select>
    </label>
  `;
}

function portfolioMonitoringNumber(name, label, value, min, max, step, suffix) {
  return `
    <label class="portfolio-monitoring-field">
      <span>${escapeHtml(label)}</span>
      <span class="portfolio-monitoring-number">
        <input type="number" name="${escapeAttribute(name)}" value="${escapeAttribute(value)}" min="${min}" max="${max}" step="${step}" required />
        ${suffix ? `<small>${escapeHtml(suffix)}</small>` : ""}
      </span>
    </label>
  `;
}

function renderPortfolioResearch() {
  const job = state.portfolioResearchJob;
  const report = state.portfolioResearchResult;
  const progress = job?.progress_percent || 0;
  const completedSteps = job?.current_step || 0;
  const committeeRun = state.portfolioResearchMode === "committee" || window.FundScopeResearchCommittee.isCommitteeRun(job, report);
  const dynamicProgress = committeeRun || ["auto", "explain", "strategy"].includes(state.portfolioResearchMode)
    || (job?.total_steps && job.total_steps !== 8);

  if (state.portfolioReportLoading) return portfolioStatus("loader-circle", "正在读取持仓诊断报告", true);

  if (state.portfolioResearchRunning) {
    return `
      <section class="portfolio-research" data-status="running">
        <header class="portfolio-research-header">
          <div><i data-lucide="git-branch"></i><span>组合研究工作流</span></div>
          <strong>${progress.toFixed(0)}%</strong>
        </header>
        ${window.FundScopeResearchCommittee.modeControl(state.portfolioResearchMode, true, state.portfolioResearchDraft)}
        <div class="portfolio-research-progress" aria-label="研究进度 ${progress.toFixed(0)}%">
          <span style="width:${Math.min(progress, 100)}%"></span>
        </div>
        ${dynamicProgress ? window.FundScopeResearchCommittee.renderProgress(job) : `<div class="portfolio-research-steps">
          ${portfolioResearchStepLabels.map((label, index) => `
            <span data-status="${index < completedSteps ? "completed" : index === completedSteps ? "active" : "pending"}">
              <i data-lucide="${index < completedSteps ? "check" : index === completedSteps ? "loader-circle" : "circle"}"></i>
              ${escapeHtml(label)}
            </span>
          `).join("")}
        </div>`}
        <p>${escapeHtml(job?.progress_message || "等待 Worker 领取研究任务")}</p>
      </section>
    `;
  }

  if (state.portfolioResearchError) {
    return `
      <section class="portfolio-research" data-status="error">
        <header class="portfolio-research-header">
          <div><i data-lucide="triangle-alert"></i><span>组合研究未完成</span></div>
          ${state.portfolioReportReadJobId
            ? `<button class="secondary-action compact" data-review-report="${escapeAttribute(state.portfolioReportReadJobId)}">重读报告</button>`
            : '<button class="secondary-action compact" type="button" data-run-portfolio-research>重试 / 检查任务</button>'}
        </header>
        ${state.portfolioReportReadJobId ? "" : window.FundScopeResearchCommittee.modeControl(state.portfolioResearchMode, false, state.portfolioResearchDraft)}
        <p>${escapeHtml(state.portfolioResearchError)}</p>
      </section>
    `;
  }

  if (report) {
    const currentJobId = report.run_id || state.portfolioResearchJob?.id;
    const canCompare = state.portfolioResearchHistory.some(
      (item) => item.job_id !== currentJobId,
    );
    const markup = window.FundScopePortfolioDiagnosis.render({
      report, history:state.portfolioResearchHistory,
      historyNotice:state.portfolioResearchHistoryError || state.portfolioResearchHistoryNotice,
      audit:renderResearchSnapshotAudit(report.research_snapshot),
      canCompare:canCompare && !state.portfolioResearchHistoryLoading && !state.portfolioResearchComparisonLoading,
      comparison:renderPortfolioResearchComparison(), adjustment:renderPortfolioAdjustment(report),
      metrics:renderPortfolioResearchPerformance(report.performance_snapshot)
        + renderPortfolioQuantRisk(report.quant_risk_snapshot)
        + renderPortfolioResearchDecisions(report.decision_history),
    });
    return window.FundScopeResearchCommittee.enhanceReport(markup, {report, mode: state.portfolioResearchMode, draft: state.portfolioResearchDraft, document});
  }

  return `
    <section class="portfolio-research" data-status="idle">
      <header class="portfolio-research-header">
        <div><i data-lucide="workflow"></i><span>组合研究报告</span></div>
        <span class="portfolio-research-stage-count">${state.portfolioResearchHistoryLoading ? "正在恢复" : "待运行"}</span>
      </header>
      <p>${escapeHtml(state.portfolioResearchHistoryError || state.portfolioResearchHistoryNotice || (state.portfolioResearchHistoryLoading ? "正在读取最近一次研究报告。" : "当前区间尚未生成综合研究报告。"))}</p>
      <div class="research-launch-controls">
        ${window.FundScopeResearchCommittee.modeControl(state.portfolioResearchMode, false, state.portfolioResearchDraft)}
        <button class="primary-action compact" type="button" data-run-portfolio-research>
          <i data-lucide="play"></i><span>运行深度研究</span>
        </button>
      </div>
    </section>
  `;
}

function renderPortfolioResearchComparison() {
  if (state.portfolioResearchComparisonLoading) {
    return `
      <section class="portfolio-research-comparison" data-status="loading">
        <header>
          <span><i data-lucide="loader-circle" class="spinning"></i>正在生成变化审计</span>
          <small>Python 对比事实后，再由 DeepSeek 解释</small>
        </header>
      </section>
    `;
  }

  if (state.portfolioResearchComparisonError) {
    return `
      <section class="portfolio-research-comparison" data-status="error">
        <header>
          <span><i data-lucide="triangle-alert"></i>暂时无法完成对比</span>
          <button class="secondary-action compact" type="button" data-compare-portfolio-research>重试</button>
        </header>
        <p>${escapeHtml(state.portfolioResearchComparisonError)}</p>
      </section>
    `;
  }

  const audit = state.portfolioResearchComparison;
  if (!audit) return "";

  const scopeLabels = {
    none: "没有变化",
    wording_only: "仅表达变化",
    limited: "单路输入变化",
    multi_source: "多路输入变化",
    broad: "广泛输入变化",
  };
  const changedSections = (audit.changed_sections || []).filter((item) => item.changed);
  const facts = (audit.facts || []).slice(0, 8);
  const hiddenFactCount = Math.max(0, (audit.facts || []).length - facts.length);
  const interpretation = audit.interpretation || {};
  const baseDate = audit.base_run?.completed_at
    ? formatDateTime(audit.base_run.completed_at)
    : "较早报告";
  const targetDate = audit.target_run?.completed_at
    ? formatDateTime(audit.target_run.completed_at)
    : "本次报告";

  return `
    <section class="portfolio-research-comparison" data-status="completed">
      <header>
        <span><i data-lucide="git-compare-arrows"></i>研究结论变化审计</span>
        <small>${escapeHtml(baseDate)} → ${escapeHtml(targetDate)}</small>
      </header>
      <div class="portfolio-research-comparison-summary">
        <div>
          <strong>${escapeHtml(interpretation.headline || scopeLabels[audit.change_scope] || "研究变化")}</strong>
          <p>${escapeHtml(interpretation.summary || "已完成两次不可变研究输入的确定性对比。")}</p>
        </div>
        <span data-scope="${escapeAttribute(audit.change_scope)}">${escapeHtml(scopeLabels[audit.change_scope] || audit.change_scope)}</span>
      </div>
      <div class="portfolio-research-change-sections" aria-label="发生变化的研究输入">
        ${(changedSections.length ? changedSections : [{ label: "六路输入哈希保持一致", section: "unchanged" }]).map((item) => `
          <span><i data-lucide="${item.section === "unchanged" ? "equal" : "database-zap"}"></i>${escapeHtml(item.label)}</span>
        `).join("")}
      </div>
      <div class="portfolio-research-comparison-grid">
        <section class="portfolio-research-change-facts">
          <header><strong>确定性变化事实</strong><small>${audit.facts?.length || 0} 项</small></header>
          ${facts.length ? facts.map((fact) => `
            <div data-tone="${escapeAttribute(fact.tone || "neutral")}">
              <span>
                <strong>${escapeHtml(fact.label)}</strong>
                <small>${escapeHtml(fact.section_label)}</small>
              </span>
              <span class="portfolio-research-change-values">
                <del>${escapeHtml(fact.before_text)}</del>
                <i data-lucide="arrow-right"></i>
                <ins>${escapeHtml(fact.after_text)}</ins>
                ${fact.delta_text ? `<em>${escapeHtml(fact.delta_text)}</em>` : ""}
              </span>
            </div>
          `).join("") : '<p>两次冻结输入及结论没有发现可见差异。</p>'}
          ${hiddenFactCount ? `<small class="portfolio-research-change-more">另有 ${hiddenFactCount} 项变化已纳入 AI 综合。</small>` : ""}
        </section>
        <section class="portfolio-research-change-explanation">
          <header>
            <strong>通俗解释</strong>
            <small>${interpretation.model_used ? "DeepSeek 受约束解释" : "Python 确定性回退"}</small>
          </header>
          ${(interpretation.drivers || []).length ? `
            <div>
              ${interpretation.drivers.slice(0, 5).map((driver) => `
                <article data-tone="${escapeAttribute(driver.tone || "neutral")}">
                  <strong>${escapeHtml(driver.title)}</strong>
                  <p>${escapeHtml(driver.detail)}</p>
                  <small>${escapeHtml((driver.references || []).map((item) => item.label).join(" · "))}</small>
                </article>
              `).join("")}
            </div>
          ` : '<p>当前没有需要额外解释的变化主因。</p>'}
        </section>
      </div>
      <footer>
        <span><i data-lucide="shield-check"></i>前后值由 Python 计算，模型只能引用 fact_key</span>
        <code>${escapeHtml(audit.comparison_id || "")}</code>
      </footer>
    </section>
  `;
}

function renderPortfolioResearchPerformance(performance) {
  if (!performance) return "";

  const benchmark = performance.benchmark || {};
  const actual = performance.position_mode === "actual";
  const secondaryMetric = actual
    && performance.xirr_pct !== null
    && performance.xirr_pct !== undefined
    ? ["XIRR", performance.xirr_pct]
    : ["年化收益", performance.annualized_return_pct];
  const attribution = performance.attribution || [];
  const benchmarkLabels = {
    matched: "已匹配基准",
    proxy: "含代理基准",
    insufficient: "基准覆盖不足",
    unavailable: "基准暂不可用",
  };

  return `
    <section class="portfolio-research-performance">
      <header>
        <div>
          <i data-lucide="chart-no-axes-combined"></i>
          <span>
            <strong>历史业绩快照</strong>
            <small>${escapeHtml(performance.performance_label || "组合历史业绩")} · ${formatDateOnly(performance.period_start)} 至 ${formatDateOnly(performance.period_end)}</small>
          </span>
        </div>
        <span>${actual ? "真实流水" : "配置模拟"}</span>
      </header>
      <div class="portfolio-research-performance-metrics">
        ${portfolioResearchPerformanceMetric("TWR", performance.twr_pct)}
        ${portfolioResearchPerformanceMetric(secondaryMetric[0], secondaryMetric[1])}
        ${portfolioResearchPerformanceMetric("相对基准", benchmark.excess_return_pct)}
        ${portfolioResearchPerformanceMetric("最大回撤", performance.max_drawdown?.drawdown_pct)}
      </div>
      <div class="portfolio-research-attribution">
        <header>
          <span>基金收益贡献</span>
          <small>${escapeHtml(benchmarkLabels[benchmark.status] || "基准状态未知")} · 年化波动 ${formatPlainPercent(performance.annualized_volatility_pct)}</small>
        </header>
        ${attribution.length ? attribution.slice(0, 4).map((item) => `
          <div>
            <span><strong>${escapeHtml(item.short_name || item.fund_code)}</strong><small>${escapeHtml(item.fund_code || "")}</small></span>
            <strong data-tone="${valueTone(item.return_contribution_pct_points)}">${formatSignedPercent(item.return_contribution_pct_points).replace("%", " 个百分点")}</strong>
          </div>
        `).join("") : `
          <p>当前区间没有可展示的基金收益归因。</p>
        `}
      </div>
    </section>
  `;
}

function portfolioResearchPerformanceMetric(label, value) {
  const available = value !== null && value !== undefined && Number.isFinite(Number(value));
  return `
    <div data-tone="${available ? valueTone(value) : "neutral"}">
      <span>${escapeHtml(label)}</span>
      <strong>${available ? formatSignedPercent(value) : "-"}</strong>
    </div>
  `;
}

function renderPortfolioResearchDecisions(history) {
  if (!history) return "";

  const decisions = history.decisions || [];
  const hiddenDecisionCount = Number(history.omitted_count || 0)
    + Math.max(0, decisions.length - 3);
  return `
    <section class="portfolio-research-decisions" data-empty="${decisions.length ? "false" : "true"}">
      <header>
        <div>
          <i data-lucide="history"></i>
          <span>
            <strong>历史决策复盘</strong>
            <small>冻结当时依据，对照最新净值观察结果</small>
          </span>
        </div>
        <span>${Number(history.decision_count || 0)} 条记录</span>
      </header>
      ${decisions.length ? `
        <div class="portfolio-research-decision-list">
          ${decisions.slice(0, 3).map((decision) => {
            const snapshot = decision.snapshot || {};
            const assessment = snapshot.assessment || {};
            const outcome = decision.outcome || {};
            return `
              <article>
                <header>
                  <div>
                    <strong>${escapeHtml(decision.action_label || portfolioDecisionActionLabel(decision.action))}</strong>
                    <time>${formatDateOnly(decision.decision_date)}</time>
                  </div>
                  <span>${escapeHtml(portfolioDecisionOutcomeLabel(outcome.status))}</span>
                </header>
                <p>${escapeHtml(decision.thesis || "未记录决策理由")}</p>
                <dl>
                  <div><dt>当时状态</dt><dd>${escapeHtml(assessment.state_label || "未记录")}</dd></div>
                  <div><dt>后续组合</dt><dd data-tone="${valueTone(outcome.portfolio_return_pct)}">${formatSignedPercent(outcome.portfolio_return_pct)}</dd></div>
                  <div><dt>后续超额</dt><dd data-tone="${valueTone(outcome.excess_return_pct)}">${formatSignedPercent(outcome.excess_return_pct)}</dd></div>
                </dl>
                <small>${escapeHtml(outcome.note || "尚无足够数据形成观察结果")}</small>
              </article>
            `;
          }).join("")}
        </div>
        ${hiddenDecisionCount > 0 ? `<p class="portfolio-research-decisions-note">报告仅展示最近三条，另有 ${hiddenDecisionCount} 条请在决策日志查看。</p>` : ""}
      ` : `
        <div class="portfolio-research-decisions-empty">
          <i data-lucide="notebook-tabs"></i>
          <span>${escapeHtml(history.note || "当前组合暂无可复盘决策。")}</span>
        </div>
      `}
      ${decisions.length ? `<footer>${escapeHtml(history.note || "决策日志不代表真实成交。")}</footer>` : ""}
    </section>
  `;
}

function renderPortfolioDecisions() {
  const hasResearchReport = Boolean(
    state.portfolioResearchResult && state.portfolioResearchJob?.id,
  );
  const sourceLabel = hasResearchReport
    ? `将同时冻结研究报告：${state.portfolioResearchJob.id}`
    : "保存当前确定性指标与规则证据";
  return `
    <section class="portfolio-decisions" data-status="${state.portfolioDecisionSaving ? "saving" : "ready"}">
      <header class="portfolio-decisions-header">
        <div>
          <i data-lucide="notebook-pen"></i>
          <span>
            <strong>投资决策日志</strong>
            <small>记录当时为何判断，之后用真实组合曲线复盘</small>
          </span>
        </div>
        <button class="icon-button subtle" type="button" data-reload-portfolio-decisions title="刷新决策结果" aria-label="刷新决策结果">
          <i data-lucide="refresh-cw"></i>
        </button>
      </header>
      <form class="portfolio-decision-form" data-portfolio-decision-form>
        <input type="hidden" name="research_job_id" value="${escapeAttribute(hasResearchReport ? state.portfolioResearchJob.id : "")}" />
        <label>
          <span>本次动作</span>
          <select name="action">
            <option value="watch">继续观察</option>
            <option value="hold" selected>维持持有</option>
            <option value="add">条件性增配</option>
            <option value="reduce">条件性减配</option>
            <option value="rebalance">重新平衡</option>
          </select>
        </label>
        <label>
          <span>观察期</span>
          <select name="horizon_days">
            <option value="30">30 天</option>
            <option value="90" selected>90 天</option>
            <option value="180">180 天</option>
          </select>
        </label>
        <label class="portfolio-decision-thesis">
          <span>决策理由</span>
          <textarea name="thesis" minlength="5" maxlength="2000" rows="3" required placeholder="写下你真正依赖的风险、市场情绪、消息面和触发条件"></textarea>
        </label>
        <footer>
          <span data-report="${hasResearchReport ? "attached" : "metrics"}"><i data-lucide="${hasResearchReport ? "paperclip" : "database"}"></i>${escapeHtml(sourceLabel)}</span>
          <button class="primary-action compact" type="submit" ${state.portfolioDecisionSaving ? "disabled" : ""}>
            <i data-lucide="${state.portfolioDecisionSaving ? "loader-circle" : "save"}" class="${state.portfolioDecisionSaving ? "spinning" : ""}"></i>
            <span>${state.portfolioDecisionSaving ? "保存中" : "保存决策"}</span>
          </button>
        </footer>
      </form>
      ${state.portfolioDecisionError ? `<div class="portfolio-inline-error"><i data-lucide="triangle-alert"></i>${escapeHtml(state.portfolioDecisionError)}</div>` : ""}
      ${state.portfolioDecisionsLoading ? portfolioStatus("loader-circle", "正在读取决策与后续表现", true) : renderPortfolioDecisionList(state.portfolioDecisions)}
    </section>
  `;
}

function renderPortfolioDecisionList(decisions) {
  if (!decisions.length) {
    return `
      <div class="portfolio-decisions-empty">
        <i data-lucide="history"></i>
        <span>还没有决策记录，保存后会冻结当前证据作为复盘基线。</span>
      </div>
    `;
  }
  return `
    <div class="portfolio-decision-list">
      ${decisions.map(renderPortfolioDecisionItem).join("")}
    </div>
  `;
}

function renderPortfolioDecisionItem(decision) {
  const snapshot = decision.snapshot || {};
  const performance = snapshot.performance || {};
  const assessment = snapshot.assessment || {};
  const research = snapshot.research || null;
  const outcome = decision.outcome || {};
  const progress = Math.min(
    Number(outcome.elapsed_days || 0) / Number(outcome.target_days || 1) * 100,
    100,
  );
  return `
    <article class="portfolio-decision-item" data-action="${escapeAttribute(decision.action)}" data-status="${escapeAttribute(outcome.status)}">
      <header>
        <div>
          <span class="portfolio-decision-action">${escapeHtml(portfolioDecisionActionLabel(decision.action))}</span>
          <time>${formatDateOnly(decision.decision_date)}</time>
        </div>
        <div>
          <span class="portfolio-decision-status">${escapeHtml(portfolioDecisionOutcomeLabel(outcome.status))}</span>
          <button class="icon-button danger-subtle" type="button" data-delete-decision="${escapeAttribute(decision.id)}" title="删除决策日志" aria-label="删除决策日志" ${state.portfolioDecisionSaving ? "disabled" : ""}>
            <i data-lucide="trash-2"></i>
          </button>
        </div>
      </header>
      <p class="portfolio-decision-thesis-copy">${escapeHtml(decision.thesis)}</p>
      <div class="portfolio-decision-snapshot">
        <div><span>当时状态</span><strong>${escapeHtml(assessment.state_label || "未记录")}</strong></div>
        <div><span>当时 TWR</span><strong data-tone="${valueTone(performance.twr_pct)}">${formatSignedPercent(performance.twr_pct)}</strong></div>
        <div><span>当时回撤</span><strong>${formatOptionalPercent(performance.max_drawdown_pct)}</strong></div>
        <div><span>当时超额</span><strong data-tone="${valueTone(performance.excess_return_pct)}">${formatSignedPercent(performance.excess_return_pct)}</strong></div>
      </div>
      ${research ? `
        <div class="portfolio-decision-research">
          <i data-lucide="workflow"></i>
          <div><span>已冻结研究报告</span><strong>${escapeHtml(research.headline || "组合研究报告")}</strong></div>
          <small>${(research.evidence_ids || []).length} 条证据引用</small>
        </div>
      ` : ""}
      <div class="portfolio-decision-outcome">
        <div class="portfolio-decision-outcome-heading">
          <span>决策后表现</span>
          <small>${Number(outcome.elapsed_days || 0)} / ${Number(outcome.target_days || decision.horizon_days)} 天 · 截至 ${outcome.as_of_date ? formatDateOnly(outcome.as_of_date) : "暂无"}</small>
        </div>
        <span class="portfolio-decision-progress"><i style="width:${progress}%"></i></span>
        <dl>
          <div><dt>组合</dt><dd data-tone="${valueTone(outcome.portfolio_return_pct)}">${formatSignedPercent(outcome.portfolio_return_pct)}</dd></div>
          <div><dt>基准</dt><dd>${formatSignedPercent(outcome.benchmark_return_pct)}</dd></div>
          <div><dt>超额</dt><dd data-tone="${valueTone(outcome.excess_return_pct)}">${formatSignedPercent(outcome.excess_return_pct)}</dd></div>
        </dl>
        <p>${escapeHtml(outcome.note || "")}</p>
      </div>
      <details class="portfolio-decision-review" ${decision.review_note ? "open" : ""}>
        <summary><span><i data-lucide="clipboard-check"></i>${decision.review_note ? "查看复盘" : "写复盘"}</span><small>目标日 ${formatDateOnly(decision.target_date)}</small></summary>
        <form data-decision-review-form="${escapeAttribute(decision.id)}">
          <textarea name="review_note" minlength="3" maxlength="2000" rows="3" required placeholder="哪些判断成立、哪些证据失效、下次要改变什么">${escapeHtml(decision.review_note || "")}</textarea>
          <button class="secondary-action compact" type="submit" ${state.portfolioDecisionSaving ? "disabled" : ""}>
            <i data-lucide="check"></i><span>${decision.review_note ? "更新复盘" : "记录复盘"}</span>
          </button>
        </form>
      </details>
    </article>
  `;
}

function portfolioDecisionActionLabel(action) {
  const labels = {
    watch: "继续观察",
    hold: "维持持有",
    add: "条件性增配",
    reduce: "条件性减配",
    rebalance: "重新平衡",
  };
  return labels[action] || action || "未标注动作";
}

function portfolioDecisionOutcomeLabel(status) {
  const labels = {
    tracking: "观察中",
    review_ready: "待复盘",
    reviewed: "已复盘",
    unavailable: "数据不足",
  };
  return labels[status] || status || "尚无结果";
}

function renderPortfolioAdjustment(report) {
  const job = state.portfolioAdjustmentJob;
  const plan = state.portfolioAdjustmentResult;
  const progress = Number(job?.progress_percent || 0);
  const completedSteps = Number(job?.current_step || 0);
  const stepLabels = ["整理证据", "人工确认", "生成候选", "场景体检", "装配结果"];

  if (state.portfolioAdjustmentRunning) {
    return `
      <section class="portfolio-adjustment" data-status="running">
        <header class="portfolio-adjustment-header">
          <div><i data-lucide="git-pull-request-draft"></i><span>组合调整方案</span></div>
          <strong>${progress.toFixed(0)}%</strong>
        </header>
        <div class="portfolio-research-progress" aria-label="调整方案进度 ${progress.toFixed(0)}%">
          <span style="width:${Math.min(progress, 100)}%"></span>
        </div>
        <div class="portfolio-adjustment-steps">
          ${stepLabels.map((label, index) => `
            <span data-status="${index < completedSteps ? "completed" : index === completedSteps ? "active" : "pending"}">
              <i data-lucide="${index < completedSteps ? "check" : index === completedSteps ? "loader-circle" : "circle"}"></i>
              ${escapeHtml(label)}
            </span>
          `).join("")}
        </div>
        <p>${escapeHtml(job?.progress_message || "等待 Worker 领取调整任务")}</p>
      </section>
    `;
  }

  if (job?.status === "waiting_input") {
    const review = job.result?.interrupt?.value || {};
    const holdings = review.current_holdings || [];
    const risk = review.risk_snapshot || {};
    return `
      <section class="portfolio-adjustment" data-status="waiting_input">
        <header class="portfolio-adjustment-header">
          <div><i data-lucide="circle-pause"></i><span>${escapeHtml(review.title || "确认组合调整约束")}</span></div>
          <span class="portfolio-hitl-status"><i data-lucide="user-check"></i>等待本人确认</span>
        </header>
        <div class="portfolio-review-summary">
          <div>
            <span>研究结论</span>
            <strong>${escapeHtml(review.source_headline || report.headline)}</strong>
            <p>${escapeHtml(review.source_summary || report.summary)}</p>
          </div>
          <dl>
            <div><dt>年化波动率</dt><dd>${formatOptionalPercent(risk.annualized_volatility_pct)}</dd></div>
            <div><dt>最大回撤</dt><dd>${formatOptionalPercent(risk.max_drawdown_pct)}</dd></div>
            <div><dt>日 CVaR 95%</dt><dd>${formatOptionalPercent(risk.daily_cvar_95_pct)}</dd></div>
          </dl>
        </div>
        <div class="portfolio-review-holdings" aria-label="当前组合权重">
          ${holdings.map((item) => `
            <span><strong>${escapeHtml(item.short_name)}</strong><small>${Number(item.weight_pct).toFixed(2)}%</small></span>
          `).join("")}
        </div>
        ${state.portfolioAdjustmentError ? `
          <div class="portfolio-inline-error"><i data-lucide="triangle-alert"></i>${escapeHtml(state.portfolioAdjustmentError)}</div>
        ` : ""}
        <form class="portfolio-adjustment-form" data-portfolio-adjustment-form>
          <fieldset>
            <legend>风险偏好</legend>
            <div class="portfolio-segmented-control">
              ${portfolioAdjustmentRadio("risk_preference", "conservative", "稳健")}
              ${portfolioAdjustmentRadio("risk_preference", "balanced", "均衡", true)}
              ${portfolioAdjustmentRadio("risk_preference", "growth", "成长")}
            </div>
          </fieldset>
          <fieldset>
            <legend>投资期限</legend>
            <div class="portfolio-segmented-control">
              ${portfolioAdjustmentRadio("investment_horizon", "under_1y", "1 年内")}
              ${portfolioAdjustmentRadio("investment_horizon", "one_to_three_years", "1-3 年", true)}
              ${portfolioAdjustmentRadio("investment_horizon", "over_3y", "3 年以上")}
            </div>
          </fieldset>
          <label class="portfolio-adjustment-number">
            <span>单只基金最大调整</span>
            <span><input type="number" name="max_single_adjustment_pct" min="1" max="50" step="1" value="15" required /><small>%</small></span>
          </label>
          <label class="portfolio-adjustment-number">
            <span>单只基金最终上限</span>
            <span><input type="number" name="max_single_fund_weight_pct" min="10" max="100" step="1" value="70" required /><small>%</small></span>
          </label>
          <label class="portfolio-adjustment-number">
            <span>组合单边换手上限</span>
            <span><input type="number" name="max_turnover_pct" min="0" max="100" step="1" value="20" required /><small>%</small></span>
          </label>
          <label class="portfolio-adjustment-notes">
            <span>补充约束</span>
            <textarea name="additional_constraints" maxlength="1000" rows="3" placeholder="例如：保留债券基金至少 30%"></textarea>
          </label>
          <footer>
            <span><i data-lucide="shield-check"></i>仅生成研究方案，不会创建或执行交易</span>
            <div>
              <button class="secondary-action compact" type="submit" value="cancel">
                <i data-lucide="x"></i><span>停止</span>
              </button>
              <button class="primary-action compact" type="submit" value="continue">
                <i data-lucide="sparkles"></i><span>确认并生成</span>
              </button>
            </div>
          </footer>
        </form>
      </section>
    `;
  }

  if (state.portfolioAdjustmentError) {
    return `
      <section class="portfolio-adjustment" data-status="error">
        <header class="portfolio-adjustment-header">
          <div><i data-lucide="triangle-alert"></i><span>组合调整方案未完成</span></div>
          <button class="secondary-action compact" type="button" data-start-portfolio-adjustment>重新生成</button>
        </header>
        <p>${escapeHtml(state.portfolioAdjustmentError)}</p>
      </section>
    `;
  }

  if (plan) {
    const cancelled = plan.status === "cancelled_by_user";
    const scenarioValidation = plan.scenario_validation || {};
    const trace = plan.graph_trace || {};
    const backend = trace.checkpoint_backend === "postgres"
      ? "PostgreSQL"
      : trace.checkpoint_backend === "sqlite"
        ? "SQLite"
        : "内存";
    if (cancelled) {
      return `
        <section class="portfolio-adjustment" data-status="cancelled">
          <header class="portfolio-adjustment-header">
            <div><i data-lucide="circle-stop"></i><span>${escapeHtml(plan.headline)}</span></div>
            <button class="secondary-action compact" type="button" data-start-portfolio-adjustment>重新开始</button>
          </header>
          <p>${escapeHtml(plan.summary)}</p>
          <footer class="portfolio-adjustment-runtime">
            <span><i data-lucide="brain-circuit"></i>DeepSeek 未调用</span>
            <span><i data-lucide="database"></i>${escapeHtml(backend)} checkpoint 已结束</span>
          </footer>
        </section>
      `;
    }

    return `
      <section class="portfolio-adjustment" data-status="completed">
        <header class="portfolio-adjustment-header">
          <div><i data-lucide="sliders-horizontal"></i><span>条件性组合调整方案</span></div>
          <button class="icon-button subtle" type="button" data-start-portfolio-adjustment title="重新生成方案" aria-label="重新生成方案">
            <i data-lucide="refresh-cw"></i>
          </button>
        </header>
        <div class="portfolio-adjustment-title">
          <div>
            <span>${escapeHtml(formatPortfolioAdjustmentStance(plan.stance))}</span>
            <h4>${escapeHtml(plan.headline)}</h4>
            <p>${escapeHtml(plan.summary)}</p>
          </div>
          <span class="portfolio-adjustment-validation-badge" data-status="${escapeAttribute(scenarioValidation.status || "information_insufficient")}">
            <i data-lucide="${scenarioValidation.status === "completed" ? "badge-check" : "circle-help"}"></i>
            ${escapeHtml(formatPortfolioScenarioStatus(scenarioValidation.status))}
          </span>
        </div>
        ${renderPortfolioScenarioValidation(scenarioValidation)}
        <div class="portfolio-adjustment-table">
          <div class="portfolio-adjustment-table-head">
            <span>基金</span><span>当前</span><span>目标</span><span>变化</span>
          </div>
          ${(plan.adjustments || []).map((item) => {
            const change = Number(item.change_pct_points || 0);
            const changeText = `${change > 0 ? "+" : ""}${change.toFixed(2)}pp`;
            return `
              <div class="portfolio-adjustment-row" data-action="${escapeAttribute(item.action)}">
                <div>
                  <strong>${escapeHtml(item.short_name)}</strong>
                  <small>${escapeHtml(item.fund_code)} · ${escapeHtml(formatPortfolioAdjustmentAction(item.action))}</small>
                </div>
                <span>${Number(item.current_weight_pct).toFixed(2)}%</span>
                <span>${Number(item.target_weight_pct).toFixed(2)}%</span>
                <strong>${escapeHtml(changeText)}</strong>
                <p>${escapeHtml(item.rationale)}</p>
                ${(item.conditions || []).length ? `<small class="portfolio-adjustment-condition">条件：${escapeHtml(item.conditions.join("；"))}</small>` : ""}
              </div>
            `;
          }).join("")}
        </div>
        <div class="portfolio-adjustment-notes-grid">
          ${renderPortfolioAdjustmentList("关键取舍", plan.key_tradeoffs || [], "scale")}
          ${renderPortfolioAdjustmentList("后续观察", plan.monitoring_items || [], "radar")}
        </div>
        ${plan.generation?.status === "fallback" ? `
          <p class="portfolio-model-fallback"><i data-lucide="shield-check"></i>${escapeHtml(plan.generation.fallback_reason || "模型结果未采用，当前权重保持不变。")}</p>
        ` : ""}
        <footer class="portfolio-adjustment-runtime">
          <span><i data-lucide="user-check"></i>${escapeHtml(formatPortfolioRiskPreference(plan.user_constraints?.risk_preference))} · ${escapeHtml(formatPortfolioHorizon(plan.user_constraints?.investment_horizon))}</span>
          <span><i data-lucide="gauge"></i>单只上限 ${Number(plan.user_constraints?.max_single_fund_weight_pct ?? 70).toFixed(0)}% · 换手上限 ${Number(plan.user_constraints?.max_turnover_pct ?? 20).toFixed(0)}%</span>
          <span><i data-lucide="flask-conical"></i>同快照 Python 重算</span>
          <span><i data-lucide="database"></i>${escapeHtml(backend)} 持久化 · interrupt 已恢复</span>
          <span><i data-lucide="shield-off"></i>0 笔交易执行</span>
        </footer>
      </section>
    `;
  }

  return `
    <section class="portfolio-adjustment" data-status="idle">
      <header class="portfolio-adjustment-header">
        <div><i data-lucide="git-pull-request-draft"></i><span>组合调整方案</span></div>
        <span class="portfolio-research-stage-count">5 阶段 · 人工确认</span>
      </header>
      <p>候选权重会与不调整基线在同一冻结历史样本上重新计算后再展示。</p>
      <button class="primary-action compact" type="button" data-start-portfolio-adjustment>
        <i data-lucide="sliders-horizontal"></i><span>生成调整方案</span>
      </button>
    </section>
  `;
}

function renderPortfolioScenarioValidation(validation) {
  if (!validation || validation.status === "skipped") return "";
  const scenarios = validation.scenarios || [];
  const snapshotHash = validation.source_snapshot?.content_hash || "";
  const baselineMessage = validation.baseline_consistency?.message || "";
  const insufficient = validation.status !== "completed";

  return `
    <section class="portfolio-scenario-comparison" data-status="${escapeAttribute(validation.status || "information_insufficient")}">
      <header>
        <div>
          <i data-lucide="flask-conical"></i>
          <span>同快照场景体检</span>
        </div>
        <span>${scenarios.length} 个场景 · ${Number(validation.comparable_scenario_count || 0)} 个可比较</span>
      </header>
      ${insufficient ? `
        <p class="portfolio-scenario-notice"><i data-lucide="circle-help"></i>${escapeHtml(baselineMessage || validation.limitations?.[0] || "冻结输入不足，候选方案未标记为已验证。")}</p>
      ` : ""}
      <div class="portfolio-scenario-table" role="table" aria-label="候选组合历史场景比较">
        <div class="portfolio-scenario-table-head" role="row">
          <span>场景</span><span>年化波动</span><span>日 CVaR</span><span>最大回撤</span><span>集中度</span><span>换手</span><span>目标检查</span>
        </div>
        ${scenarios.map((scenario) => {
          const metrics = scenario.metrics || {};
          const deltas = scenario.metric_deltas || {};
          return `
            <div class="portfolio-scenario-row" role="row" data-status="${escapeAttribute(scenario.comparison_status || "information_insufficient")}">
              <div>
                <strong>${escapeHtml(scenario.label || scenario.scenario_id)}</strong>
                <small>${escapeHtml(formatPortfolioScenarioSource(scenario.source))}</small>
              </div>
              ${renderPortfolioScenarioMetric(metrics.annualized_volatility_pct, deltas.annualized_volatility_pct, "%")}
              ${renderPortfolioScenarioMetric(metrics.daily_cvar_95_pct, deltas.daily_cvar_95_pct, "%")}
              ${renderPortfolioScenarioMetric(metrics.max_drawdown_pct, deltas.max_drawdown_pct, "%")}
              ${renderPortfolioScenarioMetric(metrics.concentration_hhi, deltas.concentration_hhi, "", 4)}
              ${renderPortfolioScenarioMetric(metrics.turnover_pct, deltas.turnover_pct, "%")}
              <span class="portfolio-scenario-objective" data-status="${escapeAttribute(scenario.objective_status || "information_insufficient")}">
                <i data-lucide="${portfolioScenarioStatusIcon(scenario.objective_status)}"></i>
                ${escapeHtml(scenario.objective_label || "暂不能验证")}
              </span>
            </div>
          `;
        }).join("")}
      </div>
      <footer>
        <span><i data-lucide="fingerprint"></i>${snapshotHash ? `快照 ${escapeHtml(snapshotHash.slice(0, 10))}` : "旧研究无场景快照"}</span>
        <span><i data-lucide="receipt-text"></i>费用与赎回限制：信息不足</span>
        <span><i data-lucide="shield-off"></i>不可自动执行</span>
      </footer>
    </section>
  `;
}

function renderPortfolioScenarioMetric(value, delta, suffix = "", digits = 2) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) {
    return `<span class="portfolio-scenario-metric"><strong>--</strong><small>无数据</small></span>`;
  }
  const numericValue = Number(value);
  const numericDelta = delta === null || delta === undefined ? null : Number(delta);
  const deltaText = numericDelta === null || Number.isNaN(numericDelta)
    ? "基线"
    : `${numericDelta > 0 ? "+" : ""}${numericDelta.toFixed(digits)}${suffix}`;
  const direction = numericDelta === null || Math.abs(numericDelta) <= 0.01
    ? "neutral"
    : numericDelta < 0
      ? "down"
      : "up";
  return `
    <span class="portfolio-scenario-metric" data-direction="${direction}">
      <strong>${numericValue.toFixed(digits)}${escapeHtml(suffix)}</strong>
      <small>${escapeHtml(deltaText)}</small>
    </span>
  `;
}

function formatPortfolioScenarioStatus(status) {
  const labels = {
    completed: "历史场景已验证",
    information_insufficient: "验证信息不足",
    failed: "约束未通过",
  };
  return labels[status] || "等待验证";
}

function formatPortfolioScenarioSource(source) {
  const labels = {
    current_weights: "当前权重",
    deterministic_half_step: "目标变化的一半",
    model_target: "模型候选终点",
  };
  return labels[source] || "候选场景";
}

function portfolioScenarioStatusIcon(status) {
  if (status === "met") return "badge-check";
  if (status === "not_met" || status === "failed") return "circle-x";
  if (status === "baseline") return "minus";
  return "circle-help";
}

function portfolioAdjustmentRadio(name, value, label, checked = false) {
  return `
    <label>
      <input type="radio" name="${escapeAttribute(name)}" value="${escapeAttribute(value)}"${checked ? " checked" : ""} />
      <span>${escapeHtml(label)}</span>
    </label>
  `;
}

function renderPortfolioAdjustmentList(title, items, icon) {
  return `
    <section>
      <span><i data-lucide="${escapeAttribute(icon)}"></i>${escapeHtml(title)}</span>
      <ul>${items.map((item) => `<li>${escapeHtml(item)}</li>`).join("")}</ul>
    </section>
  `;
}

function formatPortfolioAdjustmentStance(stance) {
  const labels = {
    reduce_risk: "降低组合风险",
    balanced_rebalance: "均衡再平衡",
    maintain: "维持当前配置",
    increase_growth: "提高成长暴露",
  };
  return labels[stance] || "组合调整判断";
}

function formatPortfolioAdjustmentAction(action) {
  const labels = { increase: "增配", decrease: "减配", hold: "维持" };
  return labels[action] || "维持";
}

function formatPortfolioRiskPreference(value) {
  const labels = { conservative: "稳健偏好", balanced: "均衡偏好", growth: "成长偏好" };
  return labels[value] || "偏好未标记";
}

function formatPortfolioHorizon(value) {
  const labels = {
    under_1y: "1 年内",
    one_to_three_years: "1-3 年",
    over_3y: "3 年以上",
  };
  return labels[value] || "期限未标记";
}

function formatOptionalPercent(value) {
  return value === null || value === undefined || Number.isNaN(Number(value))
    ? "-"
    : `${Number(value).toFixed(2)}%`;
}

function renderPortfolioFindingReferences(finding) {
  const metrics = finding.metric_references || [];
  const evidence = finding.evidence_references || [];
  if (!metrics.length && !evidence.length) return "";

  return `
    <div class="portfolio-finding-references">
      ${metrics.map((item) => `
        <span class="portfolio-metric-reference">
          <small>${escapeHtml(item.label)}</small>
          <strong>${escapeHtml(item.value_text)}</strong>
        </span>
      `).join("")}
      ${evidence.map((item) => `
        <span class="portfolio-evidence-reference" title="${escapeAttribute(item.evidence_id)}">
          <i data-lucide="quote"></i>
          ${escapeHtml(item.title || item.evidence_id)}
          ${item.value_text ? `<strong>${escapeHtml(item.value_text)}</strong>` : ""}
        </span>
      `).join("")}
    </div>
  `;
}

function renderPortfolioInterpretation(interpretation, generation) {
  if (!interpretation) return "";

  const accepted = interpretation.status === "accepted";
  const confidence = Math.round(Number(interpretation.confidence || 0) * 100);
  const conflicts = interpretation.conflicts || [];
  const uncertainties = interpretation.uncertainties || [];
  const followUps = interpretation.follow_up_items || [];

  return `
    <section class="portfolio-model-interpretation" data-status="${accepted ? "accepted" : "fallback"}">
      <header>
        <div>
          <i data-lucide="brain-circuit"></i>
          <span>综合研判</span>
        </div>
        <div class="portfolio-model-status">
          <span>${accepted ? "模型结果已校验" : "确定性回退"}</span>
          <strong>置信度 ${confidence}%</strong>
        </div>
      </header>
      ${generation?.fallback_reason ? `
        <p class="portfolio-model-fallback"><i data-lucide="shield-check"></i>${escapeHtml(generation.fallback_reason)}</p>
      ` : ""}
      <div class="portfolio-conditional-view">
        <span>条件性观点</span>
        <p>${escapeHtml(interpretation.conditional_view)}</p>
      </div>
      <div class="portfolio-model-details">
        ${renderPortfolioInterpretationList("证据冲突", conflicts, "git-compare-arrows", "未识别到需要单列的证据冲突")}
        ${renderPortfolioInterpretationList("不确定性", uncertainties, "circle-help", "暂无额外不确定性")}
        ${renderPortfolioInterpretationList("后续观察", followUps, "list-checks", "暂无额外观察项")}
      </div>
    </section>
  `;
}

function renderPortfolioInterpretationList(title, items, icon, emptyText) {
  return `
    <div>
      <span><i data-lucide="${escapeAttribute(icon)}"></i>${escapeHtml(title)}</span>
      ${items.length
        ? `<ul>${items.map((item) => `<li>${escapeHtml(item)}</li>`).join("")}</ul>`
        : `<p>${escapeHtml(emptyText)}</p>`}
    </div>
  `;
}

function renderPortfolioQuantRisk(quantRisk) {
  if (!quantRisk) return "";

  const contributions = quantRisk.risk_contributions || [];
  const strongestPair = quantRisk.correlation?.strongest_pair;
  const correlationText = strongestPair
    ? `${strongestPair.left_fund_code} / ${strongestPair.right_fund_code} · ${Number(strongestPair.correlation).toFixed(2)}`
    : "单一基金，无基金间相关性";

  return `
    <section class="portfolio-quant-risk">
      <div class="portfolio-quant-risk-metrics">
        <div>
          <span>区间收益</span>
          <strong>${formatSignedPercent(quantRisk.period_return_pct)}</strong>
        </div>
        <div>
          <span>年化波动率</span>
          <strong>${formatPlainPercent(quantRisk.annualized_volatility_pct)}</strong>
        </div>
        <div>
          <span>最大回撤</span>
          <strong>${formatPlainPercent(quantRisk.max_drawdown?.drawdown_pct || 0)}</strong>
        </div>
        <div>
          <span>日 CVaR 95%</span>
          <strong>${formatPlainPercent(quantRisk.daily_cvar_95_pct)}</strong>
        </div>
      </div>
      <div class="portfolio-quant-risk-body">
        <div class="portfolio-quant-risk-contributions">
          <header>
            <span>波动率风险贡献</span>
            <small>${Number(quantRisk.return_observation_count || 0)} 个共同收益样本</small>
          </header>
          ${contributions.map((item) => {
            const share = Math.min(Math.max(Number(item.absolute_risk_share_pct), 0), 100);
            return `
              <div class="portfolio-quant-risk-row">
                <div>
                  <strong>${escapeHtml(item.short_name)}</strong>
                  <span>${escapeHtml(item.fund_code)} · 权重 ${formatPlainPercent(item.weight_pct)}</span>
                </div>
                <div class="portfolio-quant-risk-value">
                  <strong>${formatPlainPercent(item.absolute_risk_share_pct)}</strong>
                  <span><i style="width:${share}%"></i></span>
                </div>
              </div>
            `;
          }).join("")}
        </div>
        <div class="portfolio-quant-risk-method">
          <span>最高绝对相关</span>
          <strong>${escapeHtml(correlationText)}</strong>
          <small>${escapeHtml(quantRisk.return_basis_label || "收益口径未标注")} · ${escapeHtml(quantRisk.engine?.name || "Riskfolio-Lib")} ${escapeHtml(quantRisk.engine?.version || "")}</small>
          ${renderPortfolioCorrelationHeatmap(quantRisk.correlation)}
        </div>
      </div>
    </section>
  `;
}

function renderPortfolioCorrelationHeatmap(correlation) {
  const fundCodes = correlation?.fund_codes || [];
  const values = correlation?.values || [];
  if (!fundCodes.length || values.length !== fundCodes.length) return "";
  return `
    <div class="portfolio-correlation-heatmap" style="grid-template-columns:36px repeat(${fundCodes.length}, minmax(30px, 1fr))" role="img" aria-label="基金收益相关性矩阵">
      <span></span>
      ${fundCodes.map((code) => `<b title="${escapeAttribute(code)}">${escapeHtml(code)}</b>`).join("")}
      ${values.map((row, rowIndex) => `
        <b title="${escapeAttribute(fundCodes[rowIndex])}">${escapeHtml(fundCodes[rowIndex])}</b>
        ${row.map((value, columnIndex) => {
          const number = Number(value);
          const sign = number >= 0 ? "positive" : "negative";
          const alpha = Math.max(Math.min(Math.abs(number), 1), 0.08).toFixed(2);
          const contrast = Math.abs(number) >= 0.55 ? "high" : "low";
          return `<i data-sign="${sign}" data-contrast="${contrast}" style="--heat:${alpha}" title="${escapeAttribute(`${fundCodes[rowIndex]} / ${fundCodes[columnIndex]}：${number.toFixed(2)}`)}"><span>${number.toFixed(2)}</span></i>`;
        }).join("")}
      `).join("")}
    </div>
  `;
}

function portfolioMetric(icon, label, value, tone) {
  return `
    <div class="portfolio-metric" data-tone="${escapeAttribute(tone)}">
      <i data-lucide="${escapeAttribute(icon)}"></i>
      <span>${escapeHtml(label)}</span>
      <strong>${escapeHtml(value)}</strong>
    </div>
  `;
}

function portfolioEvidenceGroup(title, items, tone) {
  const visibleItems = items.slice(0, 3);
  return `
    <section class="portfolio-evidence-group" data-tone="${escapeAttribute(tone)}">
      <header><span>${escapeHtml(title)}</span><strong>${items.length}</strong></header>
      ${visibleItems.map((item) => `
        <article class="portfolio-evidence-item">
          <div><strong>${escapeHtml(item.title)}</strong><code>${escapeHtml(item.evidence_id)}</code></div>
          <p>${escapeHtml(item.explanation)}</p>
          <footer>
            <span>覆盖 ${item.portfolio_weight_coverage_pct.toFixed(2)}% · ${formatDateOnly(item.data_date)}</span>
            ${item.event_id ? `<span class="evidence-freshness" data-status="${escapeAttribute(item.freshness_status)}">${escapeHtml(formatEventFreshness(item.freshness_status, item.decay_factor))}</span>` : ""}
            <a href="${escapeAttribute(item.source_url)}" target="_blank" rel="noreferrer">${escapeHtml(item.source_name)}<i data-lucide="external-link"></i></a>
          </footer>
        </article>
      `).join("") || '<p class="portfolio-evidence-none">暂无该方向证据</p>'}
    </section>
  `;
}

function portfolioStatus(icon, message, spinning = false) {
  return `
    <div class="portfolio-status">
      <i data-lucide="${escapeAttribute(icon)}"${spinning ? ' class="spinning"' : ""}></i>
      <span>${escapeHtml(message)}</span>
    </div>
  `;
}

async function selectFund(fundCode, { closeMobile = true, force = false } = {}) {
  if (!fundCode) return;
  if (state.selectedFundCode !== fundCode) state.fundReportReference = null;
  if (state.activeView === "overview" && state.workspacePages.overview === "reports") {
    state.selectedFundCode = fundCode;
    loadFundReports({refresh: force});
  }
  // 持仓页不等待图表/风险接口，自己的请求序号负责隔离快速换基金产生的慢响应。
  if (state.activeView === "overview" && state.workspacePages.overview === "holdings") {
    state.selectedFundCode = fundCode;
    loadFundHoldings({refresh:force});
  }
  if (closeMobile) researchTerminal?.selectFund();
  if (!force && state.fundDetail?.profile?.fund_code === fundCode) {
    state.selectedFundCode = fundCode;
    state.agentContextMode = "fund";
    renderAgentFundContext();
    renderFundNavigation();
    renderFundCards();
    loadActiveMarketFundLinks();
    if (closeMobile) closePanels();
    return;
  }

  state.selectedFundCode = fundCode;
  state.agentContextMode = "fund";
  const selectionRequestId = ++state.fundSelectionRequestId;
  state.fundDetail = null;
  state.fundChartData = null;
  state.fundChartLoading = true;
  state.fundChartError = "";
  if (state.activeView === "overview") loadTerminalFundEvents(fundCode);
  loadActiveMarketFundLinks();
  state.fundRisk = null;
  state.fundRiskError = "";
  state.fundRiskLoading = true;
  state.fundAssessment = null;
  state.fundAssessmentError = "";
  state.fundAssessmentLoading = true;
  state.fundSemanticReview = null;
  state.fundSemanticReviewError = "";
  state.fundSemanticReviewLoading = false;
  localStorage.setItem("fundscope-selected-fund", fundCode);
  renderAgentFundContext();
  renderFundNavigation();
  renderFundCards();
  elements.fundDetail.innerHTML = `
    <div class="fund-loading">
      <i data-lucide="loader-circle" class="spinning"></i>
      <span>正在读取基金详情</span>
    </div>
  `;
  refreshIcons();

  try {
    // 三个请求互不依赖，因此并行读取。某个分析接口失败时，基础基金详情仍可展示。
    const [detailResult, riskResult, assessmentResult, chartResult] = await Promise.allSettled([
      apiRequest(`${endpoints.funds}/${encodeURIComponent(fundCode)}`),
      apiRequest(fundRiskEndpoint(fundCode, state.fundRiskMonths)),
      apiRequest(fundAssessmentEndpoint(fundCode, state.fundRiskMonths)),
      apiRequest(`${endpoints.funds}/${encodeURIComponent(fundCode)}/chart`),
    ]);
    if (selectionRequestId !== state.fundSelectionRequestId) return;
    if (detailResult.status === "rejected") throw detailResult.reason;

    state.fundDetail = detailResult.value;
    if (chartResult.status === "fulfilled") state.fundChartData = chartResult.value;
    else state.fundChartError = chartResult.reason.message;
    state.fundChartLoading = false;
    if (riskResult.status === "fulfilled") {
      state.fundRisk = riskResult.value;
    } else {
      state.fundRiskError = riskResult.reason.message;
    }
    if (assessmentResult.status === "fulfilled") {
      state.fundAssessment = assessmentResult.value;
    } else {
      state.fundAssessmentError = assessmentResult.reason.message;
    }
    state.fundRiskLoading = false;
    state.fundAssessmentLoading = false;
    renderFundDetail();
    hideFundError();
    if (closeMobile) closePanels();
  } catch (error) {
    if (selectionRequestId !== state.fundSelectionRequestId) return;
    state.fundChartLoading = false;
    state.fundChartError = error.message;
    state.fundDetail = null;
    state.fundRisk = null;
    state.fundRiskError = "";
    state.fundRiskLoading = false;
    state.fundAssessment = null;
    state.fundAssessmentError = "";
    state.fundAssessmentLoading = false;
    state.fundSemanticReview = null;
    state.fundSemanticReviewError = "";
    state.fundSemanticReviewLoading = false;
    elements.fundDetail.innerHTML = fundEmptyState(error.message);
    showFundError(error.message);
    refreshIcons();
  } finally {
    if (selectionRequestId === state.fundSelectionRequestId) renderOverviewDashboard();
  }
}

async function loadFundRisk(months) {
  if (!state.selectedFundCode || ![6, 12, 36].includes(months)) return;

  state.fundRiskMonths = months;
  const fundCode = state.selectedFundCode;
  const selectionRequestId = state.fundSelectionRequestId;
  state.fundRiskLoading = true;
  state.fundRiskError = "";
  state.fundAssessmentLoading = true;
  state.fundAssessmentError = "";
  // 模型研判与统计区间绑定。切换区间后旧结果不能继续显示成当前结论。
  state.fundSemanticReview = null;
  state.fundSemanticReviewError = "";
  state.fundSemanticReviewLoading = false;
  renderFundDetail();

  // 风险面板和综合研判共享同一观察区间，但任一请求失败都不阻塞另一块内容。
  const [riskResult, assessmentResult] = await Promise.allSettled([
    apiRequest(fundRiskEndpoint(state.selectedFundCode, months)),
    apiRequest(fundAssessmentEndpoint(state.selectedFundCode, months)),
  ]);
  if (fundCode !== state.selectedFundCode || months !== state.fundRiskMonths || selectionRequestId !== state.fundSelectionRequestId) return;
  if (riskResult.status === "fulfilled") {
    state.fundRisk = riskResult.value;
  } else {
    state.fundRisk = null;
    state.fundRiskError = riskResult.reason.message;
  }
  if (assessmentResult.status === "fulfilled") {
    state.fundAssessment = assessmentResult.value;
  } else {
    state.fundAssessment = null;
    state.fundAssessmentError = assessmentResult.reason.message;
  }
  state.fundRiskLoading = false;
  state.fundAssessmentLoading = false;
  renderFundDetail();
}

function fundRiskEndpoint(fundCode, months) {
  return `${endpoints.funds}/${encodeURIComponent(fundCode)}/risk?months=${months}`;
}

function fundAssessmentEndpoint(fundCode, months) {
  return `${endpoints.funds}/${encodeURIComponent(fundCode)}/assessment?months=${months}`;
}

function fundSemanticReviewEndpoint(fundCode, months) {
  return `${endpoints.funds}/${encodeURIComponent(fundCode)}/semantic-review?months=${months}`;
}

async function runFundSemanticReview() {
  if (!state.selectedFundCode || state.fundSemanticReviewLoading) return;

  // 保存发起请求时的基金和区间。用户中途切换页面时，旧响应不会污染新基金。
  const fundCode = state.selectedFundCode;
  const months = state.fundRiskMonths;
  state.fundSemanticReviewLoading = true;
  state.fundSemanticReviewError = "";
  renderFundDetail();

  try {
    const review = await apiRequest(
      fundSemanticReviewEndpoint(fundCode, months),
      { method: "POST" },
    );
    if (state.selectedFundCode !== fundCode || state.fundRiskMonths !== months) return;
    state.fundSemanticReview = review;
    showToast("AI 语义研判已生成");
  } catch (error) {
    if (state.selectedFundCode !== fundCode || state.fundRiskMonths !== months) return;
    state.fundSemanticReview = null;
    state.fundSemanticReviewError = error.message;
    showToast(error.message, "error");
  } finally {
    if (state.selectedFundCode === fundCode && state.fundRiskMonths === months) {
      state.fundSemanticReviewLoading = false;
      renderFundDetail();
    }
  }
}

function renderFundOverview() {
  renderOverviewDashboard();
  const catalog = state.fundCatalog;
  if (!catalog) return;
  const totalRecords = state.funds.reduce(
    (total, fund) => total + fund.nav_record_count,
    0,
  );
  const range = `${formatDateOnly(catalog.snapshot_start)} 至 ${formatDateOnly(catalog.snapshot_end)}`;
  const isDatabaseSnapshot = catalog.data_mode === "database";
  const modeLabel = isDatabaseSnapshot ? "数据库每日快照" : "本地固定快照";
  const updateLabel = catalog.data_updated_at
    ? ` · 同步于 ${formatDateTime(catalog.data_updated_at)}`
    : "";

  elements.fundCount.textContent = String(catalog.count);
  elements.fundRecordCount.textContent = totalRecords.toLocaleString("zh-CN");
  elements.fundSnapshotEnd.textContent = formatDateOnly(catalog.snapshot_end);
  elements.fundOverviewMeta.textContent = `${modeLabel} · ${catalog.source} · ${range}${updateLabel}`;
  elements.fundSourceMode.textContent = modeLabel;
  elements.fundSidebarRange.textContent = range;
  if (state.activeView === "overview") {
    elements.fundHeaderMeta.textContent = `${catalog.count} 只已关注基金 · ${modeLabel}截至 ${formatDateOnly(catalog.snapshot_end)}`;
  }
}

function renderFundNavigation() {
  renderMarketFundContext();
  const funds = filteredFunds();
  if (!funds.length) {
    elements.fundNavList.innerHTML = fundEmptyState("没有匹配的基金");
    return;
  }

  elements.fundNavList.innerHTML = funds
    .map((fund) => {
      const active = fund.fund_code === state.selectedFundCode ? " active" : "";
      return `
        <button
          class="fund-nav-button${active}"
          type="button"
          data-fund-code="${escapeAttribute(fund.fund_code)}"
          data-role="${escapeAttribute(fund.sample_role)}"
        >
          <span class="fund-nav-copy">
            <strong>${escapeHtml(fund.short_name)}</strong>
            <span>${escapeHtml(fund.fund_code)} · ${escapeHtml(fund.latest_nav_date)}</span>
          </span>
          <span class="terminal-watch-quote"><b>${formatNav(fund.latest_nav)}</b><b class="t-${FundScopeTerminalData.tone(fund.daily_growth_rate_pct)}">${FundScopeTerminalData.percent(fund.daily_growth_rate_pct)}</b></span>
          <span class="terminal-watch-periods"><span title="近30个自然日，${escapeAttribute(fund.return_basis_label)}">30日 <b class="t-${FundScopeTerminalData.tone(fund.return_30d_pct)}">${FundScopeTerminalData.percent(fund.return_30d_pct)}</b></span><span title="近3个自然月，${escapeAttribute(fund.return_basis_label)}">3月 <b class="t-${FundScopeTerminalData.tone(fund.return_3m_pct)}">${FundScopeTerminalData.percent(fund.return_3m_pct)}</b></span></span>
        </button>
      `;
    })
    .join("");
  refreshIcons();
}

function renderFundCards() {
  const funds = filteredFunds();
  if (!funds.length) {
    elements.fundCardGrid.innerHTML = fundEmptyState("没有匹配的基金");
    return;
  }

  elements.fundCardGrid.innerHTML = funds
    .map((fund) => {
      const active = fund.fund_code === state.selectedFundCode ? " active" : "";
      const growthClass = fundGrowthClass(fund.daily_growth_rate_pct);
      return `
        <button
          class="fund-card${active}"
          type="button"
          data-fund-code="${escapeAttribute(fund.fund_code)}"
          data-role="${escapeAttribute(fund.sample_role)}"
        >
          <span class="fund-card-top">
            <span class="fund-card-title">
              <strong>${escapeHtml(fund.short_name)}</strong>
              <span>${escapeHtml(fund.fund_code)} · ${escapeHtml(fund.fund_type)}</span>
            </span>
            <span class="fund-type-badge">${escapeHtml(formatFundRole(fund.sample_role))}</span>
          </span>
          <span class="fund-card-bottom">
            <span class="fund-card-nav">
              <span>最新净值</span>
              <strong>${formatNav(fund.latest_nav)}</strong>
            </span>
            <span class="fund-card-growth">
              <span>日涨跌</span>
              <strong class="${growthClass}">${formatSignedPercent(fund.daily_growth_rate_pct)}</strong>
            </span>
          </span>
        </button>
      `;
    })
    .join("");
}

function renderFundDetail() {
  renderOverviewDashboard();
  const detail = state.fundDetail;
  if (!detail) return;
  const profile = detail.profile;
  const growthClass = fundGrowthClass(detail.daily_growth_rate_pct);
  const chartDataLabel = detail.data_mode === "database"
    ? "每日同步数据库快照，不代表盘中实时行情"
    : "本地固定历史快照，不代表实时行情";
  const sourceLink = profile.source_url
    ? `<a class="fund-source-link" href="${escapeAttribute(profile.source_url)}" target="_blank" rel="noreferrer"><i data-lucide="external-link"></i><span>查看数据来源</span></a>`
    : "";

  elements.fundDetail.innerHTML = `
    <div class="fund-detail-heading">
      <div>
        <h2>${escapeHtml(profile.short_name)}</h2>
        <p>${escapeHtml(profile.full_name || profile.short_name)} · ${escapeHtml(profile.fund_code)}</p>
      </div>
      <div class="fund-detail-actions">
        ${sourceLink}
        <button class="fund-agent-action" type="button" data-analyze-fund>
          <i data-lucide="sparkles"></i><span>Agent 分析</span>
        </button>
      </div>
    </div>
    <div class="fund-metric-strip">
      ${fundMetric("最新单位净值", formatNav(detail.latest_nav))}
      ${fundMetric("当日涨跌", formatSignedPercent(detail.daily_growth_rate_pct), growthClass)}
      ${fundMetric("净值日期", formatDateOnly(detail.latest_nav_date))}
      ${fundMetric("历史记录", `${detail.nav_record_count.toLocaleString("zh-CN")} 条`)}
    </div>
    ${renderFundRiskSection()}
    ${renderFundAssessmentSection()}
    <div class="fund-detail-layout">
      <section class="fund-chart-panel" aria-label="历史净值曲线">
        <header class="fund-chart-header">
          <div>
            <strong>单位净值走势</strong>
            <span>${escapeHtml(chartDataLabel)}</span>
          </div>
          <div class="period-control" role="group" aria-label="图表区间">
            ${fundPeriodButton("30d", "近 30 日")}
            ${fundPeriodButton("3m", "近 3 月")}
            ${fundPeriodButton("6m", "近 6 月")}
            ${fundPeriodButton("12m", "近 1 年")}
            ${fundPeriodButton("36m", "近 3 年")}
          </div>
        </header>
        ${renderFundChart(detail.nav_points)}
      </section>
      <aside class="fund-profile-panel" aria-label="基金档案">
        <h3>基金档案</h3>
        <dl class="fund-profile-list">
          ${fundProfileRow("基金类型", profile.fund_type)}
          ${fundProfileRow("成立信息", profile.established)}
          ${fundProfileRow("资产规模", profile.net_asset_size)}
          ${fundProfileRow("管理公司", profile.management_company)}
          ${fundProfileRow("基金经理", profile.fund_managers)}
          ${fundProfileRow("管理费率", profile.management_fee)}
          ${fundProfileRow("托管费率", profile.custodian_fee)}
          ${fundProfileRow("跟踪标的", profile.tracking_target)}
          ${fundProfileRow("业绩基准", profile.benchmark)}
        </dl>
      </aside>
    </div>
  `;
  refreshIcons();
}

function renderFundAssessmentSection() {
  const assessment = state.fundAssessment;
  const periodLabel = formatRiskPeriod(state.fundRiskMonths);

  if (state.fundAssessmentLoading) {
    return `
      <section class="fund-assessment-section" aria-label="综合研判" aria-busy="true">
        <div class="fund-assessment-status">
          <i data-lucide="loader-circle" class="spinning"></i>
          <span>正在融合 ${escapeHtml(periodLabel)}风险、市场与事件证据</span>
        </div>
      </section>
    `;
  }

  if (state.fundAssessmentError || !assessment) {
    return `
      <section class="fund-assessment-section" aria-label="综合研判">
        <div class="fund-assessment-status error">
          <i data-lucide="triangle-alert"></i>
          <span>${escapeHtml(state.fundAssessmentError || "综合研判暂不可用")}</span>
        </div>
      </section>
    `;
  }

  const score = assessment.score_breakdown;
  return `
    <section
      class="fund-assessment-section"
      data-state="${escapeAttribute(assessment.state_code)}"
      aria-label="综合研判"
    >
      <header class="fund-assessment-header">
        <div class="fund-assessment-heading">
          <span class="fund-assessment-kicker"><i data-lucide="scan-search"></i>第二层 · 规则信号</span>
          <h3>${escapeHtml(assessment.state_label)}</h3>
          <p>${escapeHtml(assessment.summary)}</p>
        </div>
        <div class="fund-assessment-score" title="${escapeAttribute(score.method)}">
          <div>
            <span>风险证据</span>
            <strong class="score-caution">${Number(score.caution_points).toFixed(2)}</strong>
          </div>
          <div>
            <span>支撑证据</span>
            <strong class="score-supportive">${Number(score.supportive_points).toFixed(2)}</strong>
          </div>
          <div>
            <span>证据覆盖</span>
            <strong>${Number(assessment.coverage_pct).toFixed(0)}%</strong>
          </div>
        </div>
      </header>
      <div class="fund-assessment-evidence-grid">
        ${fundAssessmentEvidenceGroup("主要风险", assessment.risk_evidence, "caution", "shield-alert")}
        ${fundAssessmentEvidenceGroup("支撑因素", assessment.supporting_evidence, "supportive", "trending-up")}
        ${fundAssessmentEvidenceGroup("中性背景", assessment.neutral_evidence, "neutral", "minus")}
      </div>
      ${renderFundSemanticReviewPanel()}
      <footer class="fund-assessment-footer">
        <div>
          <strong>${escapeHtml(formatAssessmentConfidence(assessment.confidence))}可信度</strong>
          <span>${escapeHtml(assessment.confidence_reason)}</span>
        </div>
        <span>${assessment.evidence_count} 条证据 · ${assessment.event_count}篇报道/${assessment.event_cluster_count ?? assessment.event_count}个事件 · 截至 ${formatDateOnly(assessment.assessment_date)}</span>
      </footer>
      <details class="fund-assessment-gaps">
        <summary>已知信息缺口 ${assessment.missing_information.length} 项</summary>
        <ul>
          ${assessment.missing_information.map((item) => `<li>${escapeHtml(item)}</li>`).join("")}
        </ul>
      </details>
      <p class="fund-assessment-disclaimer">${escapeHtml(assessment.disclaimer)}</p>
    </section>
  `;
}

function renderFundSemanticReviewPanel() {
  const review = state.fundSemanticReview;
  const error = state.fundSemanticReviewError;

  if (state.fundSemanticReviewLoading) {
    return `
      <section class="fund-semantic-review" data-status="loading" aria-busy="true">
        <div class="semantic-review-status">
          <i data-lucide="loader-circle" class="spinning"></i>
          <div><strong>DeepSeek 正在复核事件语义</strong><span>方向、关联程度与影响周期</span></div>
        </div>
      </section>
    `;
  }

  if (!review) {
    return `
      <section class="fund-semantic-review" data-status="${error ? "error" : "idle"}">
        <div class="semantic-review-intro">
          <div>
            <span class="semantic-review-kicker"><i data-lucide="sparkles"></i>第三层 · 模型语义判断</span>
            <strong>${error ? "模型研判暂不可用" : "事件语义独立复核"}</strong>
            <p>${escapeHtml(error || "按需调用模型；结果不覆盖历史指标，也不参与规则分。")}</p>
          </div>
          <button class="primary-action compact" type="button" data-run-semantic-review>
            <i data-lucide="${error ? "refresh-cw" : "sparkles"}"></i>
            <span>${error ? "重新生成" : "生成 AI 研判"}</span>
          </button>
        </div>
      </section>
    `;
  }

  const interpretation = review.model_interpretation;
  const judgments = interpretation.event_judgments || [];
  return `
    <section class="fund-semantic-review" data-status="${escapeAttribute(interpretation.status)}">
      <header class="semantic-review-header">
        <div>
          <span class="semantic-review-kicker"><i data-lucide="sparkles"></i>第三层 · 模型语义判断</span>
          <h4>${escapeHtml(formatSemanticOverallView(interpretation.effective_overall_view))}</h4>
          <p>${escapeHtml(interpretation.summary)}</p>
        </div>
        <div class="semantic-review-meta">
          <span>自评置信度</span>
          <strong>${Math.round(Number(interpretation.overall_confidence) * 100)}%</strong>
          <small>${interpretation.calibration_status === "uncalibrated" ? "未校准" : "已校准"}</small>
        </div>
      </header>
      <div class="semantic-review-stats">
        <span><strong>${interpretation.accepted_count}</strong> 接受</span>
        <span><strong>${interpretation.abstained_count}</strong> 弃权</span>
        <span><strong>${interpretation.conflict_count}</strong> 与规则不一致</span>
        <span><strong>${interpretation.reviewed_event_count}</strong> 已复核事件</span>
      </div>
      <div class="semantic-judgment-list">
        ${judgments.map((item) => renderSemanticJudgment(item)).join("") || `
          <div class="semantic-review-empty">当前统计区间没有可供模型复核的事件</div>
        `}
      </div>
      ${renderSemanticReviewNotes(interpretation)}
      <footer class="semantic-review-footer">
        <div>
          <span>${escapeHtml(interpretation.model)} · ${escapeHtml(interpretation.prompt_version)}</span>
          <small>未校准模型观点 · 不参与规则分</small>
        </div>
        <button class="icon-button" type="button" data-run-semantic-review title="重新生成 AI 研判" aria-label="重新生成 AI 研判">
          <i data-lucide="refresh-cw"></i>
        </button>
      </footer>
    </section>
  `;
}

function renderSemanticJudgment(judgment) {
  const sourceLink = judgment.source_url
    ? `<a href="${escapeAttribute(judgment.source_url)}" target="_blank" rel="noreferrer" aria-label="查看事件来源"><i data-lucide="external-link"></i></a>`
    : "";
  const tone = judgment.review_status === "abstained"
    ? "uncertain"
    : judgment.effective_direction;

  return `
    <article class="semantic-judgment" data-tone="${escapeAttribute(tone)}">
      <header>
        <div><strong>${escapeHtml(judgment.title)}</strong><code>${escapeHtml(judgment.event_id)}</code></div>
        <span>${escapeHtml(formatSemanticDirection(judgment.effective_direction))}</span>
      </header>
      <p>${escapeHtml(judgment.reason)}</p>
      <footer>
        <span>${escapeHtml(formatSemanticRelevance(judgment.relevance))}</span>
        <span>${escapeHtml(formatSemanticHorizon(judgment.horizon))}</span>
        <span>置信度 ${Math.round(Number(judgment.confidence) * 100)}%</span>
        <span>${judgment.review_status === "accepted" ? "Python 已接受" : "Python 已弃权"}</span>
        ${judgment.differs_from_rule_baseline ? '<span class="semantic-conflict">与规则不一致</span>' : ""}
        ${sourceLink}
      </footer>
    </article>
  `;
}

function renderSemanticReviewNotes(interpretation) {
  const notes = [
    ...(interpretation.conflicts || []),
    ...(interpretation.uncertainties || []),
  ];
  if (!notes.length) return "";

  return `
    <details class="semantic-review-notes">
      <summary>冲突与不确定性 ${notes.length} 项</summary>
      <ul>${notes.map((item) => `<li>${escapeHtml(item)}</li>`).join("")}</ul>
    </details>
  `;
}

function fundAssessmentEvidenceGroup(title, evidence, tone, icon) {
  const visibleEvidence = evidence.slice(0, 3);
  const remainingCount = Math.max(0, evidence.length - visibleEvidence.length);
  const rows = visibleEvidence.length
    ? visibleEvidence.map((item) => fundAssessmentEvidenceRow(item)).join("")
    : `<div class="assessment-evidence-empty">当前没有计入该方向的证据</div>`;

  return `
    <section class="assessment-evidence-group" data-tone="${escapeAttribute(tone)}">
      <header>
        <span><i data-lucide="${escapeAttribute(icon)}"></i>${escapeHtml(title)}</span>
        <strong>${evidence.length}</strong>
      </header>
      <div class="assessment-evidence-list">${rows}</div>
      ${remainingCount ? `<p class="assessment-evidence-more">另有 ${remainingCount} 条证据已计入汇总</p>` : ""}
    </section>
  `;
}

function fundAssessmentEvidenceRow(evidence) {
  const sourceLink = evidence.source_url
    ? `<a href="${escapeAttribute(evidence.source_url)}" target="_blank" rel="noreferrer" aria-label="查看 ${escapeAttribute(evidence.source_name)} 来源"><i data-lucide="external-link"></i></a>`
    : "";

  return `
    <article class="assessment-evidence-row">
      <div class="assessment-evidence-title">
        <strong>${escapeHtml(evidence.title)}</strong>
        <span>${escapeHtml(evidence.value_text)}</span>
      </div>
      <p>${escapeHtml(evidence.explanation)}</p>
      <div class="assessment-evidence-meta">
        <span>${escapeHtml(formatAssessmentEvidenceKind(evidence.kind))}</span>
        <span>强度 ${evidence.strength}</span>
        ${evidence.event_id ? `<span class="evidence-freshness" data-status="${escapeAttribute(evidence.freshness_status)}">${escapeHtml(formatEventFreshness(evidence.freshness_status, evidence.decay_factor))}</span>` : ""}
        <span>${formatDateOnly(evidence.data_date)}</span>
        ${sourceLink}
      </div>
      <code>${escapeHtml(evidence.evidence_id)}</code>
    </article>
  `;
}

function renderFundRiskSection() {
  const risk = state.fundRisk;
  const periodLabel = formatRiskPeriod(state.fundRiskMonths);
  const periodControl = `
    <div class="period-control" role="group" aria-label="风险统计区间">
      ${fundRiskPeriodButton(6, "近 6 月")}
      ${fundRiskPeriodButton(12, "近 1 年")}
      ${fundRiskPeriodButton(36, "近 3 年")}
    </div>
  `;

  if (state.fundRiskLoading) {
    return `
      <section class="fund-risk-section" aria-label="风险画像" aria-busy="true">
        ${fundRiskHeader("正在计算风险指标", periodControl)}
        <div class="fund-risk-loading">
          <i data-lucide="loader-circle" class="spinning"></i>
          <span>正在分析 ${escapeHtml(periodLabel)}净值序列</span>
        </div>
      </section>
    `;
  }

  if (state.fundRiskError || !risk) {
    return `
      <section class="fund-risk-section" aria-label="风险画像">
        ${fundRiskHeader("风险指标暂不可用", periodControl)}
        <div class="fund-risk-error">
          <i data-lucide="triangle-alert"></i>
          <span>${escapeHtml(state.fundRiskError || "没有可用的风险数据")}</span>
        </div>
      </section>
    `;
  }

  const drawdown = risk.max_drawdown;
  const riskAdjusted = risk.risk_adjusted || {};
  const benchmark = risk.benchmark_comparison || {};
  const recoveryText = drawdown.recovery_date
    ? `恢复于 ${formatDateOnly(drawdown.recovery_date)}，低点后 ${drawdown.recovery_days} 天`
    : `截至 ${formatDateOnly(risk.period_end)} 尚未恢复`;

  return `
    <section class="fund-risk-section" aria-label="风险画像">
      ${fundRiskHeader(`${formatRiskPeriod(risk.period_months)} · 截至 ${formatDateOnly(risk.period_end)}`, periodControl)}
      <div class="fund-risk-grid">
        ${fundRiskMetric("最大回撤", `-${formatPlainPercent(drawdown.drawdown_pct)}`, "risk-alert")}
        ${fundRiskMetric("年化波动率", formatPlainPercent(risk.annualized_volatility_pct), "risk-measure")}
        ${fundRiskMetric("下行波动率", formatPlainPercent(risk.downside_volatility_pct), "risk-measure")}
        ${fundRiskMetric("当前回撤", `-${formatPlainPercent(risk.current_drawdown_pct)}`, "risk-alert")}
        ${fundRiskMetric("最差单日", formatSignedPercent(risk.worst_daily_return_pct), "risk-alert")}
        ${fundRiskMetric("最长回撤期", `${risk.longest_drawdown_days} 天`, "risk-measure")}
      </div>
      <div class="fund-risk-adjusted" title="${escapeAttribute([
        riskAdjusted.methodology,
        benchmark.methodology,
        ...(riskAdjusted.warnings || []),
        ...(benchmark.warnings || []),
      ].filter(Boolean).join(" "))}">
        <header>
          <div>
            <strong>风险调整后表现</strong>
            <span>无风险年利率 ${formatOptionalPercent(riskAdjusted.risk_free_rate_annual_pct)} · ${formatBenchmarkStatus(benchmark.status)}</span>
          </div>
          <small>${escapeHtml(benchmark.benchmark_name || "业绩基准未披露")}</small>
        </header>
        <dl>
          <div><dt>年化收益</dt><dd>${formatOptionalPercent(riskAdjusted.annualized_return_pct)}</dd></div>
          <div><dt>夏普比率</dt><dd>${formatOptionalRatio(riskAdjusted.sharpe_ratio)}</dd></div>
          <div><dt>Sortino 比率</dt><dd>${formatOptionalRatio(riskAdjusted.sortino_ratio)}</dd></div>
          <div><dt>基准超额收益</dt><dd>${formatSignedPercent(benchmark.excess_return_pct)}</dd></div>
        </dl>
      </div>
      <footer class="fund-risk-footer">
        <div>
          <strong>最大回撤路径</strong>
          <span>${formatDateOnly(drawdown.peak_date)} 高点 → ${formatDateOnly(drawdown.trough_date)} 低点 · ${escapeHtml(recoveryText)}</span>
        </div>
        <span title="${escapeAttribute([risk.methodology, ...(risk.data_quality_warnings || [])].join(" "))}">${escapeHtml(risk.return_basis_label || "收益口径未标注")} · ${risk.observation_count.toLocaleString("zh-CN")} 个净值点 · 252 日年化</span>
      </footer>
    </section>
  `;
}

function fundRiskHeader(subtitle, periodControl) {
  return `
    <header class="fund-risk-header">
      <div>
        <span class="fund-risk-kicker"><i data-lucide="shield-alert"></i>风险画像</span>
        <small>${escapeHtml(subtitle)}</small>
      </div>
      ${periodControl}
    </header>
  `;
}

function formatOptionalRatio(value) {
  const number = Number(value);
  return value === null || value === undefined || !Number.isFinite(number)
    ? "-"
    : number.toFixed(3);
}

function formatBenchmarkStatus(status) {
  const labels = {
    matched: "基准已匹配",
    proxy: "基准含透明近似",
    insufficient: "基准覆盖不足",
    unavailable: "基准暂不可用",
  };
  return labels[status] || "基准状态未知";
}

function renderFundChart(allPoints) {
  const points = filterFundPoints(allPoints, state.fundChartPeriod);
  if (points.length < 2) return fundEmptyState("当前区间没有足够的净值数据");

  // SVG 使用固定 viewBox，浏览器只负责等比例缩放，桌面和手机会得到同一条曲线。
  const width = 900;
  const height = 330;
  const margin = { top: 18, right: 18, bottom: 34, left: 58 };
  const plotWidth = width - margin.left - margin.right;
  const plotHeight = height - margin.top - margin.bottom;
  const values = points.map((point) => point.unit_nav);
  const rawMin = Math.min(...values);
  const rawMax = Math.max(...values);
  const padding = Math.max((rawMax - rawMin) * 0.08, rawMax * 0.005, 0.001);
  const min = rawMin - padding;
  const max = rawMax + padding;
  const span = max - min;
  const x = (index) => margin.left + (index / (points.length - 1)) * plotWidth;
  const y = (value) => margin.top + ((max - value) / span) * plotHeight;
  const linePath = points
    .map((point, index) => `${index === 0 ? "M" : "L"}${x(index).toFixed(2)} ${y(point.unit_nav).toFixed(2)}`)
    .join(" ");
  const bottom = margin.top + plotHeight;
  const areaPath = `${linePath} L${x(points.length - 1).toFixed(2)} ${bottom} L${x(0).toFixed(2)} ${bottom} Z`;
  const gridLines = Array.from({ length: 5 }, (_, index) => {
    const ratio = index / 4;
    const gridY = margin.top + ratio * plotHeight;
    const label = max - ratio * span;
    return `
      <line class="chart-grid-line" x1="${margin.left}" y1="${gridY}" x2="${width - margin.right}" y2="${gridY}"></line>
      <text class="chart-axis-label" x="${margin.left - 9}" y="${gridY + 3}" text-anchor="end">${formatNav(label)}</text>
    `;
  }).join("");
  const middleIndex = Math.floor((points.length - 1) / 2);
  const dateLabels = [0, middleIndex, points.length - 1]
    .map((index) => {
      const anchor = index === 0 ? "start" : index === points.length - 1 ? "end" : "middle";
      return `<text class="chart-axis-label" x="${x(index)}" y="${height - 9}" text-anchor="${anchor}">${formatDateOnly(points[index].nav_date)}</text>`;
    })
    .join("");
  const latest = points[points.length - 1];

  return `
    <div class="fund-chart">
      <svg viewBox="0 0 ${width} ${height}" role="img" aria-label="${fundChartPeriodLabel(state.fundChartPeriod)}单位净值曲线">
        ${gridLines}
        <path class="chart-area" d="${areaPath}"></path>
        <path class="chart-line" d="${linePath}"></path>
        <circle class="chart-end-point" cx="${x(points.length - 1)}" cy="${y(latest.unit_nav)}" r="4"></circle>
        ${dateLabels}
      </svg>
    </div>
    <footer class="fund-chart-footer">
      <span>区间最低 <strong>${formatNav(rawMin)}</strong></span>
      <span>${points.length.toLocaleString("zh-CN")} 个交易日</span>
      <span>区间最高 <strong>${formatNav(rawMax)}</strong></span>
    </footer>
  `;
}

function filterFundPoints(points, period) {
  if (!points.length) return [];
  // 完整报告和终端共用日历回退规则，避免月末在两处出现不同的起始日。
  if (!(period in FundScopeTerminalData.periods)) return points;
  const cutoff = FundScopeTerminalData.cutoffDate(points.at(-1).nav_date, period);
  return points.filter((point) => point.nav_date >= cutoff);
}

function fundChartPeriodLabel(period) {
  const labels = {
    "30d": "近 30 日",
    "3m": "近 3 月",
    "6m": "近 6 月",
    "12m": "近 1 年",
    "36m": "近 3 年",
  };
  return labels[period] || "当前区间";
}

function filteredFunds() {
  if (!state.fundSearch) return state.funds;
  const query = state.fundSearch.toLowerCase();
  return state.funds.filter((fund) =>
    `${fund.short_name} ${fund.fund_code} ${fund.fund_type}`
      .toLowerCase()
      .includes(query),
  );
}

function renderAgentFundContext() {
  // 基金页也展示当前账本持仓，组合或账号的异步读取结束后同步更新该只读视图。
  researchTerminal?.update();
  const picker = document.querySelector("#research-subject");
  const options = `<option value="market">市场与板块</option><optgroup label="我的组合">${state.portfolios.map(item => `<option value="portfolio:${escapeAttribute(item.id)}">${escapeHtml(item.name)}</option>`).join("")}</optgroup>
    <optgroup label="自选基金">${state.funds.map(item => `<option value="fund:${escapeAttribute(item.fund_code)}">${escapeHtml(item.short_name)} · ${escapeHtml(item.fund_code)}</option>`).join("")}</optgroup>`;
  if (picker.innerHTML !== options) picker.innerHTML = options;
  picker.value = state.agentContextMode === "market" ? "market" : `${state.agentContextMode}:${state.agentContextMode === "portfolio" ? state.selectedPortfolioId : state.selectedFundCode}`;
  // 发送期间的切换由 change 事件拒绝；不留下无法自动恢复的 disabled 状态。
  if (state.agentContextMode === "market") {
    const index = state.marketSentiment?.index_overview?.indices?.find((item) => item.index_code === state.selectedMarketIndexCode);
    elements.agentFundContextLabel.textContent = `市场 · ${index?.index_name || "宽基指数"}`;
    return;
  }
  const fund = state.funds.find(
    (item) => item.fund_code === state.selectedFundCode,
  );
  const portfolio = state.portfolios.find(
    (item) => item.id === state.selectedPortfolioId,
  );
  elements.agentFundContextLabel.textContent = state.agentContextMode === "portfolio" && portfolio
    ? `组合 · ${portfolio.name}`
    : fund
      ? `${fund.fund_code} · ${fund.short_name}`
      : state.selectedFundCode || "未选择基金";
}

function openSelectedFundInAgent() {
  if (!state.selectedFundCode) {
    showToast("请先选择一只基金", "error");
    return;
  }

  // 显式切回单基金上下文，避免页面中已选组合干扰这次请求的工具路由。
  state.agentContextMode = "fund";
  renderAgentFundContext();
  activateProductView("agent");
  elements.messageInput.value = (
    "请使用完整金融研究流程，全面分析这只基金。请同时检查历史风险、专属业绩基准、"
    + "最新披露持仓、市场环境和相关事件，先告诉我证据是否充分，再通俗解释主要风险、"
    + "传导路径和还缺少什么信息。"
  );
  resizeComposer();
  elements.messageInput.focus();
}

function openSelectedPortfolioInAgent() {
  if (state.sending) {
    showToast("请先等待当前回答完成", "error");
    return;
  }
  if (!state.selectedPortfolioId || !state.portfolioDetail) {
    showToast("请先保存并选择一份基金组合", "error");
    return;
  }

  // 新的配置分析不继承上一份报告引用；旧会话仍保留，只有发送问题后才调用模型。
  startNewConversation();
  state.agentContextMode = "portfolio";
  renderAgentFundContext();
  activateProductView("agent");
  elements.messageInput.value = (
    "请使用完整金融研究流程，全面分析我的当前基金组合。请同时检查组合历史业绩与"
    + "专属基准、量化风险、底层证券和行业、重复持仓以及最近事件影响。先说明证据"
    + "充分度，再通俗解释主要风险、传导路径、反方证据和数据缺口，并引用完整证据编号。"
  );
  resizeComposer();
  elements.messageInput.focus();
}

function closeChatPortfolioResearchStreams({ clearJobs = false } = {}) {
  // 切换会话时主动关闭旧 EventSource，避免后台事件继续刷新已经离开的消息列表。
  Object.values(state.chatPortfolioResearchStreams).forEach((source) => {
    source?.close();
  });
  state.chatPortfolioResearchStreams = {};
  if (clearJobs) state.chatPortfolioResearchJobs = {};
}

function mergeChatPortfolioResearchJob(rawJob) {
  if (!rawJob?.id) return null;

  const existing = state.chatPortfolioResearchJobs[rawJob.id] || {};
  const report = rawJob.result || rawJob.report || existing.report || null;
  const merged = {
    ...existing,
    ...rawJob,
    portfolio_id: rawJob.portfolio_id || existing.portfolio_id || report?.portfolio_id,
    portfolio_name: rawJob.portfolio_name
      || existing.portfolio_name
      || report?.portfolio_name,
    period_months: rawJob.period_months
      || existing.period_months
      || report?.period_months,
    report,
  };
  state.chatPortfolioResearchJobs[rawJob.id] = merged;
  return merged;
}

function registerChatPortfolioResearchJobs(messages) {
  // run_metadata 来自服务端提取的 Tool 结果，不解析模型正文中的 job_id。
  (messages || []).forEach((message) => {
    const jobs = message.run_metadata?.portfolio_research_jobs;
    if (!Array.isArray(jobs)) return;
    jobs.forEach((job) => mergeChatPortfolioResearchJob(job));
  });

  Object.values(state.chatPortfolioResearchJobs).forEach((job) => {
    watchChatPortfolioResearchJob(job);
  });
}

function watchChatPortfolioResearchJob(job) {
  if (!job?.id || state.chatPortfolioResearchStreams[job.id]) return;
  const terminal = ["completed", "failed", "cancelled"].includes(job.status);
  // 历史消息里可能只有裁剪报告；此时仍连接一次 SSE，把 Job Store 中的完整报告取回。
  const hasFullReport = Boolean(job.report?.graph_trace);
  if (terminal && (job.status !== "completed" || hasFullReport)) return;

  const params = new URLSearchParams({ user_id: state.userId });
  const endpoint = job.events_endpoint
    || `${endpoints.backgroundJobs}/${encodeURIComponent(job.id)}/events`;
  const separator = endpoint.includes("?") ? "&" : "?";
  const source = new EventSource(`${endpoint}${separator}${params}`);
  state.chatPortfolioResearchStreams[job.id] = source;

  const applyEvent = (event) => {
    const snapshot = JSON.parse(event.data);
    const merged = mergeChatPortfolioResearchJob({
      ...snapshot,
      stream_state: "connected",
    });
    renderMessages();
    return merged;
  };

  source.addEventListener("progress", (event) => {
    try {
      applyEvent(event);
    } catch (error) {
      mergeChatPortfolioResearchJob({
        id: job.id,
        stream_state: "invalid_event",
        stream_error: error.message,
      });
      renderMessages();
    }
  });
  source.addEventListener("done", (event) => {
    try {
      applyEvent(event);
      source.close();
      delete state.chatPortfolioResearchStreams[job.id];

      // 聊天后台完成只更新聊天状态；用户点开报告时再读取，不覆盖正在看的历史报告。
    } catch (error) {
      source.close();
      delete state.chatPortfolioResearchStreams[job.id];
      mergeChatPortfolioResearchJob({
        id: job.id,
        stream_state: "invalid_event",
        stream_error: error.message,
      });
      renderMessages();
    }
  });
  source.onerror = () => {
    // 原生 EventSource 会自动重连。这里只显示连接状态，不把网络抖动误判为 Job 失败。
    const latest = state.chatPortfolioResearchJobs[job.id];
    if (["completed", "failed", "cancelled"].includes(latest?.status)) return;
    mergeChatPortfolioResearchJob({
      id: job.id,
      stream_state: "reconnecting",
    });
    renderMessages();
  };
}

async function openChatPortfolioResearch(jobId) {
  const job = state.chatPortfolioResearchJobs[jobId];
  if (!job?.portfolio_id) return;

  if (state.portfolios.some((item) => item.id === job.portfolio_id)) {
    await selectPortfolio(job.portfolio_id);
  } else {
    await loadPortfolios({ preferredPortfolioId: job.portfolio_id });
  }
  if (state.selectedPortfolioId === job.portfolio_id) await openReviewedReport(job.id);
}

function showFundError(message) {
  elements.fundErrorText.textContent = message;
  elements.fundError.hidden = false;
  refreshIcons();
}

function hideFundError() {
  elements.fundError.hidden = true;
  elements.fundErrorText.textContent = "";
}

async function loadConversations({ showFeedback = false } = {}) {
  try {
    const data = await apiRequest(
      `${endpoints.conversations}?user_id=${encodeURIComponent(state.userId)}`,
    );
    state.conversations = data.conversations;
    renderConversationList();
    if (showFeedback) showToast("会话列表已刷新");
    return data.conversations;
  } catch (error) {
    renderConversationList(error.message);
    if (showFeedback) showToast(error.message, "error");
    throw error;
  }
}

async function selectConversation(conversationId, { closeMobile = true } = {}) {
  if (!conversationId || state.sending) return;
  elements.researchRefresh.checked = false;

  try {
    const data = await apiRequest(
      `${endpoints.conversations}/${encodeURIComponent(conversationId)}` +
        `?user_id=${encodeURIComponent(state.userId)}`,
    );
    state.currentConversationId = conversationId;
    // 历史对话未声明终端研究标的。清除旧绑定，防止把另一段历史挂到当前基金下面。
    state.terminalChatSubject = null;
    state.terminalChatError = "";
    state.messages = data.messages;
    state.researchReference = findLatestRunMetadata(data.messages)?.research_reference || null;
    state.fundReportReference = findLatestRunMetadata(data.messages)?.fund_report_reference || null;
    if (state.fundReportReference) {
      const ref = state.fundReportReference;
      await selectFund(ref.fund_code);
      state.fundReportReference = ref;
      state.agentContextMode = "fund";
      renderAgentFundContext();
    }
    if (state.researchReference) {
      state.agentContextMode = "portfolio";
      await selectPortfolio(state.researchReference.portfolio_id);
      renderAgentFundContext();
    }
    closeChatPortfolioResearchStreams({ clearJobs: true });
    registerChatPortfolioResearchJobs(data.messages);
    state.run = null;
    state.runDurationMs = null;
    state.historicalRun = findLatestRunMetadata(data.messages);
    state.feedbackEditorMessageId = null;
    localStorage.setItem(conversationStorageKey(), conversationId);
    updateConversationHeading(data.conversation);
    renderConversationList();
    renderMessages({ scrollToBottom: true });
    renderRunPanel();
    if (closeMobile) closePanels();
  } catch (error) {
    if (error.status === 404) {
      await loadConversations();
      startNewConversation();
    }
    showToast(error.message, "error");
  }
}

function startNewConversation() {
  if (state.sending) return;
  state.researchReference = null;
  state.fundReportReference = null;
  elements.researchRefresh.checked = false;
  elements.messageInput.value = "";
  resizeComposer();
  state.terminalChatSubject = null;
  state.terminalChatError = "";
  closeChatPortfolioResearchStreams({ clearJobs: true });
  state.currentConversationId = null;
  state.messages = [];
  state.run = null;
  state.historicalRun = null;
  state.runDurationMs = null;
  state.feedbackEditorMessageId = null;
  localStorage.removeItem(conversationStorageKey());
  updateConversationHeading();
  renderConversationList();
  renderMessages();
  renderRunPanel();
  hideChatError();
  closePanels();
  elements.messageInput.focus();
}

async function deleteConversation(conversationId) {
  const conversation = state.conversations.find((item) => item.id === conversationId);
  const confirmed = window.confirm(`删除“${conversation?.title || "这条对话"}”？`);
  if (!confirmed) return;

  try {
    await apiRequest(
      `${endpoints.conversations}/${encodeURIComponent(conversationId)}` +
        `?user_id=${encodeURIComponent(state.userId)}`,
      { method: "DELETE" },
    );
    if (state.currentConversationId === conversationId) startNewConversation();
    await Promise.all([loadConversations(), loadFeedbackDashboard({ silent: true })]);
    showToast("对话已删除");
  } catch (error) {
    showToast(error.message, "error");
  }
}

async function sendMessage(event, { terminal = false } = {}) {
  event.preventDefault();
  const message = elements.messageInput.value.trim();
  if (!message || state.sending) return;
  const researchRefresh = window.FundScopeResearchRepair.takeAuthorization(elements.researchRefresh, {terminal});

  // 完整助手允许自由切换话题；从这里发问后，终端不再假定这段对话只研究原标的。
  if (!terminal) {
    state.terminalChatSubject = null;
    state.terminalChatError = "";
  }

  hideChatError();
  const idempotencyKey =
    state.retryMessage === message && state.retryResearchRefresh === researchRefresh && state.retryIdempotencyKey
      ? state.retryIdempotencyKey
      : createIdempotencyKey();
  state.retryMessage = "";
  state.retryIdempotencyKey = null;
  const optimisticId = `optimistic-${Date.now()}`;
  state.messages.push({
    id: optimisticId,
    conversation_id: state.currentConversationId,
    role: "user",
    content: message,
    created_at: new Date().toISOString(),
    run_metadata: {},
  });
  state.sending = true;
  state.replyPending = true;
  state.agentPhase = "正在连接 Agent";
  state.streamedAnswer = "";
  state.activeStreamId = null;
  state.activeStreamController = new AbortController();
  setComposerRunState(true);
  elements.messageInput.value = "";
  resizeComposer();
  renderMessages({ scrollToBottom: true });
  elements.conversationMeta.textContent = "Agent 正在执行";
  const startedAt = performance.now();

  try {
    // URL 只决定请求发往哪个后端入口；真正的 developer 权限仍由服务端 Session 校验。
    const chatStreamEndpoint = state.developerMode
      ? endpoints.developerAgentChatStream
      : endpoints.agentChatStream;
    const response = await fetch(chatStreamEndpoint, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        user_id: state.userId,
        conversation_id: state.currentConversationId,
        idempotency_key: idempotencyKey,
        fund_code: state.agentContextMode === "fund" ? state.selectedFundCode : null,
        portfolio_id: state.agentContextMode === "portfolio"
          ? state.selectedPortfolioId
          : null,
        message,
        top_k: 3,
        max_tool_rounds: 10,
        research_refresh: researchRefresh,
        research_job_id: state.agentContextMode === "portfolio" && state.researchReference?.portfolio_id === state.selectedPortfolioId
          ? state.researchReference.job_id : "",
        fund_report_ids: state.agentContextMode === "fund" && state.fundReportReference?.fund_code === state.selectedFundCode
          ? state.fundReportReference.document_ids : [],
      }),
      signal: state.activeStreamController.signal,
    });
    if (!response.ok) {
      let detail = `请求失败（HTTP ${response.status}）`;
      try {
        const payload = await response.json();
        detail = payload.detail || detail;
      } catch (_) {
        // 非 JSON 错误保留上面的 HTTP 状态说明。
      }
      if (response.status === 401) showAuthDialog();
      throw new ApiError(detail, response.status);
    }

    const data = await consumeAgentEventStream(response);
    if (!data) throw new ApiError("Agent 流已结束，但没有返回最终结果", 502);

    // 正式消息接替流式预览，不能把同一份答案当作两条消息同时渲染。
    // 会话列表刷新完成前仍保留 sending，避免新请求被上一轮的 finally 清理。
    state.replyPending = false;
    state.streamedAnswer = "";
    state.agentPhase = "";
    state.currentConversationId = data.conversation.id;
    state.messages = state.messages.filter((item) => item.id !== optimisticId);
    state.messages.push(data.user_message, data.assistant_message);
    state.researchReference = data.assistant_message.run_metadata?.research_reference || null;
    state.fundReportReference = data.assistant_message.run_metadata?.fund_report_reference || null;
    registerChatPortfolioResearchJobs([data.assistant_message]);
    state.run = data.run;
    state.historicalRun = null;
    state.runDurationMs = Math.round(performance.now() - startedAt);
    state.workspace = data.workspace;
    localStorage.setItem(conversationStorageKey(), data.conversation.id);

    updateConversationHeading(data.conversation);
    renderMessages({ scrollToBottom: true });
    renderRunPanel();
    renderWorkspacePanel();
    if (state.developerMode) loadEvalDashboard({ silent: true });
    // 回答已经保存，列表刷新失败不能把它当作生成失败，再提示用户重发。
    await loadConversations().catch((error) => {
      showToast(`回答已保存，但会话列表刷新失败：${error.message}`, "error");
    });
    state.retryIdempotencyKey = null;
  } catch (error) {
    state.replyPending = false;
    state.streamedAnswer = "";
    state.messages = state.messages.filter((item) => item.id !== optimisticId);
    state.terminalChatError = error.message;
    elements.messageInput.value = message;
    resizeComposer();
    renderMessages({ scrollToBottom: true });
    updateConversationHeading();
    state.retryMessage = message;
    state.retryResearchRefresh = researchRefresh;
    state.retryIdempotencyKey = idempotencyKey;
    if (error.name === "AbortError" || error instanceof AgentRunCancelledError) {
      showChatError("本次运行已停止，原问题已放回输入框。", { canRetry: true });
    } else {
      showChatError(error.message, { canRetry: true });
    }
  } finally {
    state.sending = false;
    state.replyPending = false;
    state.activeStreamController = null;
    state.activeStreamId = null;
    state.agentPhase = "";
    state.streamedAnswer = "";
    setComposerRunState(false);
    // 清理状态后同步刷新两个聊天视图，停止和报错时也不留下旧的流式气泡。
    renderMessages();
    if (!elements.messageInput.value) elements.messageInput.focus();
  }
}

function createIdempotencyKey() {
  if (window.crypto?.randomUUID) return window.crypto.randomUUID();
  return `chat_${Date.now()}_${Math.random().toString(16).slice(2)}`;
}

function createTransactionIdempotencyKey() {
  if (window.crypto?.randomUUID) return window.crypto.randomUUID();
  return `txn_${Date.now()}_${Math.random().toString(16).slice(2)}`;
}

class AgentRunCancelledError extends Error {
  constructor(message) {
    super(message);
    this.name = "AgentRunCancelledError";
  }
}

async function consumeAgentEventStream(response) {
  const reader = response.body?.getReader();
  if (!reader) throw new ApiError("当前浏览器不支持流式响应", 500);

  const decoder = new TextDecoder();
  let buffer = "";
  let finalResult = null;

  while (true) {
    const { value, done } = await reader.read();
    buffer += decoder.decode(value || new Uint8Array(), { stream: !done });
    const frames = buffer.split("\n\n");
    buffer = frames.pop() || "";

    for (const frame of frames) {
      if (!frame.trim() || frame.trimStart().startsWith(":")) continue;
      const parsed = parseSseFrame(frame);
      if (!parsed) continue;
      const { event, payload } = parsed;

      if (event === "stream_started") {
        state.activeStreamId = payload.stream_id;
      } else if (event === "phase" || event === "tool") {
        state.agentPhase = payload.label || "Agent 正在执行";
        elements.conversationMeta.textContent = state.agentPhase;
        renderMessages({ scrollToBottom: true });
      } else if (event === "answer_delta") {
        state.streamedAnswer += payload.delta || "";
        renderMessages({ scrollToBottom: true });
      } else if (event === "result") {
        finalResult = payload;
      } else if (event === "cancelled") {
        throw new AgentRunCancelledError(payload.message || "运行已停止");
      } else if (event === "error") {
        throw new ApiError(payload.message || "Agent 运行失败", payload.status || 500);
      }
    }
    if (done) break;
  }
  return finalResult;
}

function parseSseFrame(frame) {
  let event = "message";
  const dataLines = [];
  frame.split("\n").forEach((line) => {
    if (line.startsWith("event:")) event = line.slice(6).trim();
    if (line.startsWith("data:")) dataLines.push(line.slice(5).trimStart());
  });
  if (!dataLines.length) return null;
  try {
    return { event, payload: JSON.parse(dataLines.join("\n")) };
  } catch (_) {
    throw new ApiError("收到无法解析的 Agent 流事件", 502);
  }
}

async function stopActiveAgentRun() {
  if (!state.sending) return;
  elements.sendButton.disabled = true;
  state.agentPhase = "正在停止本次运行";
  renderMessages({ scrollToBottom: true });
  try {
    if (state.activeStreamId) {
      await fetch(
        `${endpoints.agentChatStream}/${encodeURIComponent(state.activeStreamId)}`,
        { method: "DELETE" },
      );
    }
  } finally {
    state.activeStreamController?.abort();
  }
}

function setComposerRunState(running) {
  elements.researchRefresh.disabled = running;
  elements.sendButton.disabled = false;
  elements.sendButton.classList.toggle("stop", running);
  elements.sendButton.title = running ? "停止生成" : "发送";
  elements.sendButton.setAttribute("aria-label", elements.sendButton.title);
  elements.sendButton.innerHTML = running
    ? '<i data-lucide="square"></i>'
    : '<i data-lucide="arrow-up"></i>';
  refreshIcons();
}

function retryLastMessage() {
  if (!state.retryMessage || state.sending) return;
  // 点击“重试”表示重试同一请求；普通重新发送则必须重新勾选。
  elements.researchRefresh.checked = state.retryResearchRefresh;
  elements.messageInput.value = state.retryMessage;
  hideChatError();
  resizeComposer();
  elements.composer.requestSubmit();
}

async function loadWorkspace({ silent = false } = {}) {
  try {
    const snapshot = await apiRequest(
      `${endpoints.workspace}?user_id=${encodeURIComponent(state.userId)}`,
    );
    state.workspace = snapshot;
    renderWorkspacePanel();
    return snapshot;
  } catch (error) {
    if (!silent) {
      elements.workspacePanel.innerHTML = emptyPanel(
        "cloud-off",
        "工作区暂不可用",
        error.message,
      );
      refreshIcons();
    }
    return null;
  }
}

async function loadKnowledge() {
  try {
    state.knowledge = await apiRequest(endpoints.knowledge);
    renderKnowledgePanel();
    return state.knowledge;
  } catch (error) {
    elements.knowledgePanel.innerHTML = emptyPanel(
      "database-zap",
      "知识库暂不可用",
      error.message,
    );
    refreshIcons();
    return null;
  }
}

let reportEvalController;
function getReportEvalController() {
  if (!reportEvalController) {
    reportEvalController = window.FundReportEvals.createController({
      api: apiRequest, userId: () => state.userId, storage: localStorage,
      makeKey: createIdempotencyKey, sleep: ms => new Promise(resolve => setTimeout(resolve, ms)),
      onBusy: busy => { state.evalRunning = busy ? "report" : false; renderEvalsPanel(); },
      onProgress: message => {
        // 进度只更新一行，不重绘整个面板，避免冲掉正在输入的复核说明。
        state.evalProgress = message;
        const progress = elements.evalsPanel.querySelector("#report-eval-progress");
        if (progress) progress.textContent = message;
      },
      onError: message => { state.evalError = message; renderEvalsPanel(); showToast(message, "error"); },
      onResult: async run => {
        const user = state.userId;
        await loadEvalDashboard();
        if (user !== state.userId || state.evalSuiteId !== window.FundReportEvals.suiteId) return;
        if (state.evalDashboard) state.evalDashboard.latest_run = run;
        showToast(`季报评测完成：${run.passed_cases}/${run.total_cases} 自动检查通过，答案待人工复核`);
      },
    });
  }
  return reportEvalController;
}

function resetReportEvalViewer() {
  reportEvalController?.pause();
  state.evalDashboardRequestId += 1;
  state.evalRunning = false;
  state.evalReviewSaving = null;
  state.evalRunLoading = null;
  state.evalRunLoadError = null;
  state.evalDashboard = null;
  state.evalError = "";
  state.evalProgress = "";
}

async function loadEvalDashboard({ silent = false } = {}) {
  const user = state.userId, suite = state.evalSuiteId, mode = state.evalRetrievalMode;
  const requestId = ++state.evalDashboardRequestId;
  const current = () => user === state.userId && suite === state.evalSuiteId && requestId === state.evalDashboardRequestId;
  try {
    const dashboard = await apiRequest(
      `${endpoints.evalDashboard}?user_id=${encodeURIComponent(user)}&suite_id=${encodeURIComponent(suite)}&retrieval_mode=${mode}`,
    );
    if (!current()) return null;
    state.evalDashboard = dashboard;
    state.evalError = "";
    renderEvalsPanel();
    return state.evalDashboard;
  } catch (error) {
    if (!current()) return null;
    state.evalError = error.message;
    if (!silent) renderEvalsPanel();
    return null;
  }
}

async function loadFeedbackDashboard({ silent = false } = {}) {
  try {
    state.feedbackDashboard = await apiRequest(
      `${endpoints.feedbackDashboard}?user_id=${encodeURIComponent(state.userId)}`,
    );
    state.feedbackError = "";
    renderEvalsPanel();
    return state.feedbackDashboard;
  } catch (error) {
    state.feedbackError = error.message;
    if (!silent) renderEvalsPanel();
    return null;
  }
}

async function loadEvalDrafts({ silent = false } = {}) {
  try {
    const data = await apiRequest(
      `${endpoints.evalDrafts}?user_id=${encodeURIComponent(state.userId)}`,
    );
    state.evalDrafts = data.drafts || [];
    state.evalDraftError = "";
    renderEvalsPanel();
    return state.evalDrafts;
  } catch (error) {
    state.evalDraftError = error.message;
    if (!silent) renderEvalsPanel();
    return [];
  }
}

async function saveEvalDraft(feedbackId, form) {
  if (state.evalDraftSavingFeedbackId) return;
  const formData = new FormData(form);
  const requiredTerms = String(formData.get("required_answer_terms") || "")
    .split(/\n|,|，/)
    .map((item) => item.trim())
    .filter(Boolean);
  const payload = {
    user_id: state.userId,
    case_id: String(formData.get("case_id") || "").trim(),
    name: String(formData.get("name") || "").trim(),
    description: String(formData.get("description") || "").trim(),
    category: String(formData.get("category") || "general"),
    fund_code: String(formData.get("fund_code") || "").trim(),
    expected_route: String(formData.get("expected_route") || "direct"),
    expected_tools: formData.getAll("expected_tools").map(String),
    required_answer_terms: requiredTerms,
    answer_match: String(formData.get("answer_match") || "all"),
    require_evidence_reference: formData.get("require_evidence_reference") === "on",
  };

  state.evalDraftSavingFeedbackId = feedbackId;
  renderEvalsPanel();
  try {
    const draft = await apiRequest(
      `${endpoints.evalDrafts}/${encodeURIComponent(feedbackId)}`,
      {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      },
    );
    state.evalDraftEditorFeedbackId = null;
    showToast(`草稿已保存：${draft.file_path}`);
    await loadEvalDrafts({ silent: true });
  } catch (error) {
    showToast(error.message, "error");
  } finally {
    state.evalDraftSavingFeedbackId = null;
    renderEvalsPanel();
  }
}

async function submitMessageFeedback(messageId, payload) {
  const message = state.messages.find((item) => item.id === messageId);
  if (!message || message.role !== "assistant" || !message.conversation_id) return;

  state.feedbackSubmittingMessageId = messageId;
  renderMessages();
  try {
    const feedback = await apiRequest(
      `${endpoints.conversations}/${encodeURIComponent(message.conversation_id)}`
        + `/messages/${encodeURIComponent(message.id)}/feedback`,
      {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ user_id: state.userId, ...payload }),
      },
    );
    message.feedback = feedback;
    state.feedbackEditorMessageId = null;
    showToast(payload.rating === "helpful" ? "感谢反馈" : "已加入待审核候选");
    await loadFeedbackDashboard({ silent: true });
  } catch (error) {
    showToast(error.message, "error");
  } finally {
    state.feedbackSubmittingMessageId = null;
    renderMessages();
  }
}

async function reviewFeedbackCandidate(feedbackId, status) {
  try {
    await apiRequest(
      `${endpoints.feedback}/${encodeURIComponent(feedbackId)}/review`,
      {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ user_id: state.userId, status, review_note: "" }),
      },
    );
    showToast(
      status === "approved"
        ? "已保留候选，尚未写入正式评测集"
        : "候选已忽略",
    );
    await loadFeedbackDashboard();
  } catch (error) {
    showToast(error.message, "error");
  }
}

async function runAgentEvals(mode = "smoke") {
  if (state.evalRunning) return;
  if (state.evalSuiteId === window.FundReportEvals.suiteId) {
    const scenarios = state.evalDashboard?.suite?.scenarios || [];
    const caseIds = scenarios.filter(item => mode === "all" || item.tags?.includes("smoke")).map(item => item.id);
    if (!caseIds.length) return;
    if (!window.confirm(`运行 ${caseIds.length} 道季报题？将调用真实大模型${state.evalRetrievalMode === "hybrid" ? "及 embedding（首次建立独立索引）" : "，仅使用关键词检索"}，按服务商规则计费。`)) return;
    state.evalError = "";
    state.evalProgress = "正在提交评测任务";
    await getReportEvalController().start(caseIds, state.evalRetrievalMode);
    return;
  }
  state.evalRunning = mode;
  state.evalError = "";
  renderEvalsPanel();

  try {
    const scenarios = state.evalDashboard?.suite?.scenarios || [];
    const caseIds = mode === "all"
      ? []
      : scenarios.filter((item) => item.tags?.includes("smoke")).map((item) => item.id);
    const result = await apiRequest(endpoints.evalRuns, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ user_id: state.userId, case_ids: caseIds }),
    });
    showToast(`评测完成：${result.passed_cases}/${result.total_cases} 通过`);
    await loadEvalDashboard();
  } catch (error) {
    state.evalError = error.message;
    showToast(error.message, "error");
  } finally {
    state.evalRunning = false;
    renderEvalsPanel();
  }
}

async function uploadSelectedDocument() {
  const file = elements.documentInput.files?.[0];
  if (!file) return;

  const formData = new FormData();
  formData.append("file", file);
  setKnowledgeBusy(true);

  try {
    const data = await apiRequest(endpoints.knowledgeDocuments, {
      method: "POST",
      body: formData,
    });
    showToast(`${data.document.name} 已上传`);
    await loadKnowledge();
  } catch (error) {
    showToast(error.message, "error");
  } finally {
    elements.documentInput.value = "";
    setKnowledgeBusy(false);
  }
}

async function rebuildKnowledge() {
  setKnowledgeBusy(true, "正在生成向量");
  try {
    const data = await apiRequest(endpoints.knowledgeRebuild, { method: "POST" });
    showToast(`索引已重建，共 ${data.chunk_count} 个片段`);
    await loadKnowledge();
  } catch (error) {
    showToast(error.message, "error");
  } finally {
    setKnowledgeBusy(false);
  }
}

async function deleteKnowledgeDocument(name) {
  if (!window.confirm(`从知识库删除“${name}”？`)) return;
  try {
    await apiRequest(
      `${endpoints.knowledgeDocuments}/${encodeURIComponent(name)}`,
      { method: "DELETE" },
    );
    showToast(`${name} 已删除`);
    await loadKnowledge();
  } catch (error) {
    showToast(error.message, "error");
  }
}

async function switchUserWorkspace() {
  if (state.auth.authenticated) {
    elements.userId.value = state.userId;
    return;
  }
  const candidate = elements.userId.value.trim();
  if (!/^[\w-]{1,50}$/.test(candidate)) {
    elements.userId.value = state.userId;
    showToast("用户 ID 仅支持字母、数字、下划线和连字符", "error");
    return;
  }
  if (candidate === state.userId) return;

  // 切用户立即清空授权弹窗；旧列表、审计及撤销回包不得进入新身份。
  agentAuthorizations?.reset();
  resetReportEvalViewer();
  state.userId = candidate;
  localStorage.setItem("agent-workspace-user", candidate);
  state.conversations = [];
  state.workspace = null;
  state.knowledge = null;
  state.evalDashboard = null;
  state.evalError = "";
  state.feedbackDashboard = null;
  state.feedbackError = "";
  state.evalDrafts = [];
  state.evalDraftError = "";
  state.evalDraftEditorFeedbackId = null;
  state.feedbackEditorMessageId = null;
  state.portfolios = [];
  state.selectedPortfolioId = null;
  state.portfolioDetail = null;
  state.portfolioAssessment = null;
  state.portfolioDraft = null;
  resetPortfolioTransactions();
  resetPortfolioExposure();
  resetPortfolioEventImpacts();
  resetPortfolioPerformance();
  resetPortfolioDecisions();
  state.portfolioAssessmentLoading = false;
  state.portfolioAssessmentRequestId += 1;
  resetPortfolioResearch();
  state.agentContextMode = "fund";
  updateUserAvatar();
  startNewConversation();
  renderWorkspacePanel();
  renderPortfolioWorkbench();
  await Promise.allSettled([
    loadConversations(),
    loadWorkspace(),
    loadKnowledge(),
    loadPortfolios(),
    loadEvalDashboard(),
    loadFeedbackDashboard(),
    loadEvalDrafts(),
  ]);
}

function renderConversationList(errorMessage = "") {
  if (errorMessage) {
    elements.conversationList.innerHTML = `<div class="conversation-empty">${escapeHtml(errorMessage)}</div>`;
    return;
  }
  if (!state.conversations.length) {
    elements.conversationList.innerHTML =
      '<div class="conversation-empty">还没有对话</div>';
    return;
  }

  elements.conversationList.innerHTML = state.conversations
    .map((conversation) => {
      const active = conversation.id === state.currentConversationId ? " active" : "";
      return `
        <div class="conversation-item${active}">
          <button
            class="conversation-select"
            type="button"
            data-conversation-id="${escapeAttribute(conversation.id)}"
          >
            <strong>${escapeHtml(conversation.title)}</strong>
            <span>${formatConversationTime(conversation.updated_at)} · ${conversation.message_count} 条消息</span>
          </button>
          <button
            class="delete-conversation"
            type="button"
            data-delete-conversation="${escapeAttribute(conversation.id)}"
            title="删除对话"
            aria-label="删除对话"
          >
            <i data-lucide="trash-2"></i>
          </button>
        </div>
      `;
    })
    .join("");
  refreshIcons();
}

function renderReportContext() {
  let banner = document.getElementById("research-report-context");
  if (!banner) {
    banner = document.createElement("div"); banner.id = "research-report-context";
    banner.className = "report-context-banner";
    elements.messageList.before(banner);
  }
  const ref = state.researchReference;
  const fundRef = state.agentContextMode === "fund" ? state.fundReportReference : null;
  banner.hidden = !ref && !fundRef;
  if (fundRef) {
    banner.innerHTML = `<span>季报研究：${escapeHtml(fundRef.fund_code)} · ${escapeHtml((fundRef.periods || []).join(" / "))} · 固定两期原文</span>
      <button class="secondary-action compact" data-source-fund-reports>查看引用季报</button><button class="secondary-action compact" data-clear-report ${state.sending ? 'disabled' : ''}>开始新话题</button>`;
    banner.querySelector("[data-source-fund-reports]").onclick = () => {
      activateProductView("overview", {page: "reports", load: false});
      loadFundReports({ids: fundRef.document_ids});
    };
    banner.querySelector("[data-clear-report]").onclick = () => startNewConversation();
    return;
  }
  if (!ref) return;
  banner.innerHTML = `<span>引用报告：${escapeHtml(ref.portfolio_name || ref.headline || ref.job_id)} · ${escapeHtml(ref.assessment_date || '')}</span>
    <button class="secondary-action compact" data-source-report>查看报告</button><button class="secondary-action compact" data-clear-report ${state.sending ? 'disabled' : ''}>开始新话题</button>`;
  banner.querySelector("[data-source-report]").onclick = async () => {
    if (state.selectedPortfolioId !== ref.portfolio_id) await selectPortfolio(ref.portfolio_id);
    await openReviewedReport(ref.job_id);
  };
  banner.querySelector("[data-clear-report]").onclick = () => startNewConversation();
}

function renderMessages({ scrollToBottom = false } = {}) {
  renderReportContext();
  researchTerminal?.update();
  if (!state.messages.length && !state.replyPending) {
    const suggestions = state.researchReference
      ? [["shield-alert", "这份报告最强的反对理由是什么？"], ["search", "哪些证据还不足以支持结论？"]]
      : state.developerMode
      ? [
          ["scan-search", "分析当前项目的核心 Agent 数据流"],
          ["list-checks", "为一个复杂需求制定任务计划"],
          ["database", "检索知识库并给出带来源的回答"],
          ["users", "创建一个队友并委派代码研究任务"],
        ]
      : [
          ["shield-alert", "分析当前基金的主要风险与支撑因素"],
          ["chart-spline", "比较近 6 个月与近 1 年的风险变化"],
          ["newspaper", "结合市场情绪和消息面解释当前状态"],
          ["layout-dashboard", "检查我的基金组合是否过度集中"],
        ];
    elements.messageList.innerHTML = `
      <div class="empty-state">
        <div class="empty-mark"><i data-lucide="${state.developerMode ? "command" : "sparkles"}"></i></div>
        <h2>${state.researchReference ? "继续讨论这份配置报告" : state.developerMode ? "今天要推进什么？" : "今天想研究什么？"}</h2>
        <div class="suggestion-grid">
          ${suggestions.map(([icon, text]) => suggestionButton(icon, text)).join("")}
        </div>
      </div>
    `;
    elements.messageList.querySelectorAll("[data-suggestion]").forEach((button) => {
      button.addEventListener("click", () => {
        elements.messageInput.value = button.dataset.suggestion;
        resizeComposer();
        elements.messageInput.focus();
      });
    });
    refreshIcons();
    return;
  }

  const rows = state.messages.map(renderMessage).join("");
  elements.messageList.innerHTML = `${rows}${state.replyPending ? thinkingMessage() : ""}`;
  elements.messageList.querySelectorAll("[data-copy-message]").forEach((button) => {
    button.addEventListener("click", async () => {
      const message = state.messages.find((item) => item.id === button.dataset.copyMessage);
      if (!message) return;
      await navigator.clipboard.writeText(message.content);
      showToast("回答已复制");
    });
  });
  elements.messageList.querySelectorAll("[data-feedback-rating]").forEach((button) => {
    button.addEventListener("click", () => {
      const messageId = button.dataset.feedbackMessage;
      if (button.dataset.feedbackRating === "helpful") {
        submitMessageFeedback(messageId, { rating: "helpful" });
        return;
      }
      state.feedbackEditorMessageId =
        state.feedbackEditorMessageId === messageId ? null : messageId;
      renderMessages();
    });
  });
  elements.messageList.querySelectorAll("[data-close-feedback]").forEach((button) => {
    button.addEventListener("click", () => {
      state.feedbackEditorMessageId = null;
      renderMessages();
    });
  });
  elements.messageList.querySelectorAll("[data-feedback-form]").forEach((form) => {
    form.addEventListener("submit", (event) => {
      event.preventDefault();
      const formData = new FormData(form);
      submitMessageFeedback(form.dataset.feedbackForm, {
        rating: "unhelpful",
        reason_code: formData.get("reason_code"),
        comment: String(formData.get("comment") || "").trim(),
        correction: String(formData.get("correction") || "").trim(),
      });
    });
  });
  elements.messageList.querySelectorAll("[data-open-chat-research]").forEach((button) => {
    button.addEventListener("click", () => {
      openChatPortfolioResearch(button.dataset.openChatResearch);
    });
  });
  refreshIcons();
  if (scrollToBottom) {
    requestAnimationFrame(() => {
      elements.messageList.scrollTop = elements.messageList.scrollHeight;
    });
  }
}

function renderMessage(message) {
  const isAssistant = message.role === "assistant";
  const avatar = isAssistant
    ? '<span class="message-avatar"><i data-lucide="command"></i></span>'
    : "";
  const copyButton = isAssistant
    ? `
      <button
        class="icon-button copy-message"
        type="button"
        data-copy-message="${escapeAttribute(message.id)}"
        title="复制回答"
        aria-label="复制回答"
      ><i data-lucide="copy"></i></button>
    `
    : "";
  const feedbackControls = isAssistant && message.run_id
    ? renderMessageFeedbackControls(message)
    : "";
  const feedbackEditor = isAssistant
    && state.feedbackEditorMessageId === message.id
    ? renderMessageFeedbackEditor(message)
    : "";

  return `
    <article class="message-row ${isAssistant ? "assistant" : "user"}">
      ${avatar}
      <div class="message-body">
        <div class="message-content">${isAssistant ? (window.FundScopeFundAnswer.render(message.run_metadata?.answer_check) || formatMessageContent(message.content)) : formatUserMessageContent(message.content)}</div>
        ${isAssistant ? window.FundScopeInvestmentContext.render(message.run_metadata?.investment_context) : ""}
        ${isAssistant ? window.FundScopeResearchRepair.render(message.run_metadata?.research_repairs) : ""}
        ${isAssistant ? renderMessagePortfolioResearch(message) : ""}
        ${isAssistant && !window.FundScopeFundAnswer.supports(message.run_metadata?.answer_check) ? renderMessageEvidence(message) : ""}
        <div class="message-meta">
          <span>${isAssistant ? "Agent" : "你"} · ${formatMessageTime(message.created_at)}</span>
          <span class="message-actions">${feedbackControls}${copyButton}</span>
        </div>
        ${feedbackEditor}
      </div>
    </article>
  `;
}

function renderMessagePortfolioResearch(message) {
  const references = Array.isArray(message.run_metadata?.portfolio_research_jobs)
    ? message.run_metadata.portfolio_research_jobs
    : [];
  if (!references.length) return "";

  return references.map((reference) => {
    const job = state.chatPortfolioResearchJobs[reference.id] || reference;
    const status = job.status || "queued";
    const terminal = ["completed", "failed", "cancelled"].includes(status);
    const progress = status === "completed"
      ? 100
      : Math.max(0, Math.min(100, Number(job.progress_percent || 0)));
    const completedSteps = status === "completed"
      ? portfolioResearchStepLabels.length
      : Math.max(0, Math.min(
          portfolioResearchStepLabels.length,
          Number(job.current_step || 0),
        ));
    const report = job.report || null;
    const committeeRun = window.FundScopeResearchCommittee.isCommitteeRun(job, report);
    const connectionHint = job.stream_state === "reconnecting"
      ? "进度连接暂时中断，正在自动重连。"
      : job.stream_state === "invalid_event"
        ? `进度事件解析失败：${job.stream_error || "未知错误"}`
        : "";

    return `
      <section class="message-research-job" data-status="${escapeAttribute(status)}">
        <header class="message-research-header">
          <span class="message-research-title">
            <i data-lucide="workflow"></i>
            <span>
              <strong>组合深度研究</strong>
              <small>${escapeHtml(job.portfolio_name || "当前组合")} · ${Number(job.period_months || 12)} 个月</small>
            </span>
          </span>
          <span class="message-research-status">${escapeHtml(statusLabel(status))}</span>
        </header>
        ${!terminal ? `
          <div class="message-research-progress" aria-label="组合研究进度 ${progress.toFixed(0)}%">
            <span style="width:${progress}%"></span>
          </div>
          <div class="message-research-progress-copy">
            <span>${escapeHtml(job.progress_message || "等待 Worker 领取研究任务")}</span>
            <strong>${progress.toFixed(0)}%</strong>
          </div>
        ` : ""}
        ${committeeRun || (job.total_steps && job.total_steps !== 8) ? window.FundScopeResearchCommittee.renderProgress(job) : `<div class="message-research-steps">
          ${portfolioResearchStepLabels.map((label, index) => {
            const stepStatus = index < completedSteps
              ? "completed"
              : !terminal && index === completedSteps
                ? "active"
                : "pending";
            const icon = stepStatus === "completed"
              ? "check"
              : stepStatus === "active"
                ? "loader-circle"
                : "circle";
            return `
              <span data-status="${stepStatus}">
                <i data-lucide="${icon}"></i>${escapeHtml(label)}
              </span>
            `;
          }).join("")}
        </div>`}
        ${connectionHint ? `<p class="message-research-notice">${escapeHtml(connectionHint)}</p>` : ""}
        ${status === "completed" ? renderMessageResearchReport(report) : ""}
        ${status === "failed" || status === "cancelled" ? `
          <p class="message-research-error">${escapeHtml(job.error || `研究任务${statusLabel(status)}`)}</p>
        ` : ""}
        <footer class="message-research-footer">
          <code>${escapeHtml(job.id)}</code>
          <button
            class="secondary-action compact"
            type="button"
            data-open-chat-research="${escapeAttribute(job.id)}"
            ${job.portfolio_id ? "" : "disabled"}
          ><i data-lucide="layout-dashboard"></i><span>查看组合</span></button>
        </footer>
      </section>
    `;
  }).join("");
}

function renderMessageResearchReport(report) {
  if (!report) {
    return '<p class="message-research-notice">研究已完成，正在读取报告摘要。</p>';
  }

  const performance = report.performance_snapshot || {};
  const benchmark = performance.benchmark || {};
  const quantRisk = report.quant_risk_snapshot || {};
  const market = report.market_snapshot || {};
  const decisions = report.decision_history || {};
  const findings = Array.isArray(report.key_findings)
    ? report.key_findings.slice(0, 3)
    : [];

  return `
    <div class="message-research-report">
      <h4>${escapeHtml(report.headline || "组合研究报告已生成")}</h4>
      <p class="dv-note">历史报告 · 数据截至 ${escapeHtml(report.assessment_date || "未知")}，不是当前持仓实时结论。</p>
      <dl class="message-research-metrics">
        ${messageResearchMetric("TWR", performance.twr_pct, true)}
        ${messageResearchMetric("相对基准", benchmark.excess_return_pct, true)}
        ${messageResearchMetric("年化波动", quantRisk.annualized_volatility_pct, false)}
        ${messageResearchMetric("历史决策", decisions.decision_count, false, " 条")}
      </dl>
      <div class="dv-research-sides">${[['supportive','机会与支撑'],['caution','风险与反对理由']].map(([tone,label])=>`<section data-side="${tone}"><h3>${label}</h3><ul>${findings.filter(item=>item.tone===tone).map(item=>`<li>${escapeHtml(item.title || '研究发现')}</li>`).join('') || '<li>当前摘要未记录，不代表不存在。</li>'}</ul></section>`).join('')}</div>
      <details class="dv-detail"><summary>完整摘要、依据与执行记录</summary><p>${escapeHtml(report.summary || "研究报告已生成。")}</p>
        ${findings.map(item=>`<p><strong>${escapeHtml(item.title)}</strong> ${escapeHtml(item.detail)}</p>`).join('')}
        <p>市场环境：${escapeHtml(market.state_label || "暂无状态")} · ${escapeHtml(market.summary || "暂无市场摘要")}</p>
        ${renderResearchSnapshotAudit(report.research_snapshot, true)}</details>
    </div>
  `;
}

function renderResearchSnapshotAudit(snapshot, compact = false) {
  if (!snapshot?.snapshot_id) return "";

  const manifest = snapshot.source_manifest || {};
  const sourceDates = manifest.source_dates || {};
  const fullHash = String(snapshot.content_hash || "");
  const shortHash = fullHash ? fullHash.slice(0, 12) : "暂无哈希";
  const dateItems = [
    ["组合", sourceDates.portfolio_updated_at],
    ["业绩", sourceDates.performance_period_end],
    ["市场", sourceDates.market_data_date],
    ["事件", sourceDates.event_data_cutoff],
  ].filter(([, value]) => Boolean(value));

  return `
    <section class="research-snapshot-audit ${compact ? "compact" : ""}">
      <span class="research-snapshot-icon"><i data-lucide="fingerprint"></i></span>
      <span class="research-snapshot-copy">
        <strong>研究输入已冻结</strong>
        <small>${formatDateTime(snapshot.captured_at)} · ${escapeHtml(snapshot.schema_version || "输入快照")}</small>
      </span>
      ${!compact && dateItems.length ? `
        <span class="research-snapshot-dates">
          ${dateItems.map(([label, value]) => `<span>${escapeHtml(label)} ${escapeHtml(formatDateOnly(value))}</span>`).join("")}
        </span>
      ` : ""}
      <code title="SHA-256 ${escapeAttribute(fullHash)}">${escapeHtml(shortHash)}</code>
    </section>
  `;
}

function messageResearchMetric(label, value, signed = false, suffix = "%") {
  const numeric = Number(value);
  const available = value !== null && value !== undefined && Number.isFinite(numeric);
  const formatted = available
    ? signed
      ? formatSignedPercent(numeric)
      : `${numeric.toFixed(suffix === " 条" ? 0 : 2)}${suffix}`
    : "-";
  return `
    <div>
      <dt>${escapeHtml(label)}</dt>
      <dd>${escapeHtml(formatted)}</dd>
    </div>
  `;
}

function renderMessageEvidence(message) {
  const evidence = Array.isArray(message.run_metadata?.evidence)
    ? message.run_metadata.evidence
    : [];
  if (!evidence.length) return "";

  const cards = evidence.map((item) => {
    const url = safeExternalUrl(item.source_url);
    const content = `
      <span class="message-evidence-icon"><i data-lucide="file-check-2"></i></span>
      <span class="message-evidence-copy">
        <strong>${escapeHtml(item.title || item.evidence_id)}</strong>
        <span>${escapeHtml(item.source_name || item.tool_name || "本轮工具结果")}${item.data_date ? ` · ${escapeHtml(formatDateOnly(item.data_date))}` : ""}</span>
        <code>${escapeHtml(item.evidence_id)}</code>
      </span>
      ${url ? '<i class="message-evidence-link-icon" data-lucide="external-link"></i>' : ""}
    `;
    return url
      ? `<a class="message-evidence-card" href="${escapeAttribute(url)}" target="_blank" rel="noreferrer">${content}</a>`
      : `<article class="message-evidence-card">${content}</article>`;
  }).join("");

  return `
    <section class="message-evidence" aria-label="本轮参考来源">
      <div class="message-evidence-heading">
        <span>参考来源</span>
        <strong>${evidence.length}</strong>
      </div>
      <div class="message-evidence-list">${cards}</div>
    </section>
  `;
}

function renderMessageFeedbackControls(message) {
  const feedback = message.feedback;
  const busy = state.feedbackSubmittingMessageId === message.id;
  return `
    <button
      class="icon-button message-feedback-button ${feedback?.rating === "helpful" ? "active" : ""}"
      type="button"
      data-feedback-message="${escapeAttribute(message.id)}"
      data-feedback-rating="helpful"
      title="有帮助"
      aria-label="有帮助"
      ${busy ? "disabled" : ""}
    ><i data-lucide="thumbs-up"></i></button>
    <button
      class="icon-button message-feedback-button ${feedback?.rating === "unhelpful" ? "active negative" : ""}"
      type="button"
      data-feedback-message="${escapeAttribute(message.id)}"
      data-feedback-rating="unhelpful"
      title="没帮助"
      aria-label="没帮助"
      ${busy ? "disabled" : ""}
    ><i data-lucide="thumbs-down"></i></button>
  `;
}

function renderMessageFeedbackEditor(message) {
  const feedback = message.feedback || {};
  const selectedReason = feedback.reason_code || "inaccurate";
  const busy = state.feedbackSubmittingMessageId === message.id;
  return `
    <form class="message-feedback-form" data-feedback-form="${escapeAttribute(message.id)}">
      <div class="feedback-form-header">
        <strong>反馈原因</strong>
        <button
          class="icon-button"
          type="button"
          data-close-feedback
          title="关闭反馈"
          aria-label="关闭反馈"
        ><i data-lucide="x"></i></button>
      </div>
      <label>
        <span>问题类型</span>
        <select name="reason_code">${feedbackReasonOptions(selectedReason)}</select>
      </label>
      <label>
        <span>补充说明</span>
        <textarea name="comment" rows="2" maxlength="2000" placeholder="哪里没有解决你的问题">${escapeHtml(feedback.comment || "")}</textarea>
      </label>
      <label>
        <span>正确方向</span>
        <textarea name="correction" rows="2" maxlength="4000" placeholder="你期望它如何回答">${escapeHtml(feedback.correction || "")}</textarea>
      </label>
      <button class="action-button primary" type="submit" ${busy ? "disabled" : ""}>
        <i data-lucide="${busy ? "loader-circle" : "send"}" class="${busy ? "spinning" : ""}"></i>
        <span>${busy ? "正在提交" : "提交反馈"}</span>
      </button>
    </form>
  `;
}

function feedbackReasonOptions(selectedReason) {
  const reasons = {
    inaccurate: "事实不准确",
    incomplete: "信息不完整",
    wrong_tool: "工具或数据不对",
    unclear: "表达不清楚",
    outdated: "信息已过时",
    other: "其他",
  };
  return Object.entries(reasons)
    .map(([value, label]) => (
      `<option value="${value}" ${value === selectedReason ? "selected" : ""}>${label}</option>`
    ))
    .join("");
}

function renderRunPanel() {
  const run = state.run;
  const historical = state.historicalRun;

  if (!run && !historical) {
    elements.runPanel.innerHTML = emptyPanel(
      "activity",
      "暂无运行记录",
      "",
    );
    elements.inspectorSubtitle.textContent = "等待下一次执行";
    refreshIcons();
    return;
  }

  if (!run) {
    elements.runPanel.innerHTML = renderHistoricalRun(historical);
    elements.inspectorSubtitle.textContent = "历史运行摘要";
    refreshIcons();
    return;
  }

  const duration = state.runDurationMs ? `${(state.runDurationMs / 1000).toFixed(1)}s` : "-";
  const tools = run.tool_calls || [];
  elements.inspectorSubtitle.textContent = run.run_id;
  elements.runPanel.innerHTML = `
    <div class="run-summary">
      ${summaryStat("状态", stopReasonLabel(run.stop_reason))}
      ${summaryStat("耗时", duration)}
      ${summaryStat("模型轮次", String(run.rounds?.length || 0))}
      ${summaryStat("工具调用", String(tools.length))}
    </div>

    ${sectionHeading("执行轨迹", `${run.pipeline_steps?.length || 0} 步`)}
    <div class="timeline">
      ${(run.pipeline_steps || [])
        .map(
          (step) => `
            <div class="timeline-item">
              <span class="timeline-marker"></span>
              ${escapeHtml(translatePipelineStep(step))}
            </div>
          `,
        )
        .join("")}
    </div>

    ${sectionHeading("工具调用", `${tools.length} 次`)}
    ${tools.length ? renderToolCalls(tools) : '<div class="resource-detail">本轮由模型直接回答。</div>'}

    ${sectionHeading("Harness 状态", "")}
    <div class="compact-list">
      ${compactMetric("route", "Dynamic Route", run.route?.name || "direct", `${run.tools_available?.length || 0} 个候选工具`)}
      ${compactMetric("shield-check", "Hooks", run.hooks_trace?.length || 0, "权限与执行审计")}
      ${compactMetric("rotate-ccw", "Recovery", run.recovery_trace?.length || 0, "模型失败恢复")}
      ${compactMetric("minimize-2", "Context Compact", run.context_trace?.length || 0, "上下文压缩")}
      ${compactMetric("layers-3", "Prompt Sections", run.prompt_trace?.section_count || 0, `${run.prompt_trace?.total_chars || 0} 字符`)}
      ${compactMetric("coins", "Token Usage", run.usage?.total_tokens || 0, "本轮模型请求累计")}
    </div>
  `;
  refreshIcons();
}

function renderHistoricalRun(metadata) {
  const toolCalls = metadata.tool_calls || [];
  return `
    <div class="run-summary">
      ${summaryStat("状态", stopReasonLabel(metadata.stop_reason))}
      ${summaryStat("耗时", formatDurationMs(metadata.duration_ms))}
      ${summaryStat("模型轮次", String(metadata.round_count || 0))}
      ${summaryStat("工具调用", String(toolCalls.length))}
    </div>
    ${sectionHeading("执行轨迹", `${metadata.pipeline_steps?.length || 0} 步`)}
    <div class="timeline">
      ${(metadata.pipeline_steps || [])
        .map(
          (step) => `<div class="timeline-item"><span class="timeline-marker"></span>${escapeHtml(translatePipelineStep(step))}</div>`,
        )
        .join("")}
    </div>
    ${sectionHeading("工具调用", `${toolCalls.length} 次`)}
    <div class="compact-list">
      ${compactMetric("route", "Dynamic Route", metadata.route?.name || "旧记录", `${metadata.selected_tool_count || 0} 个候选工具`)}
      ${compactMetric("layers-3", "Prompt Size", metadata.prompt_chars || 0, "字符")}
      ${compactMetric("coins", "Token Usage", metadata.total_tokens || 0, metadata.model || "历史执行摘要")}
      ${toolCalls
        .map((call) => compactMetric("wrench", call.name, call.is_error ? "失败" : "完成", "历史执行摘要"))
        .join("") || '<div class="resource-detail">本轮没有调用工具。</div>'}
    </div>
  `;
}

function renderWorkspacePanel() {
  const workspace = state.workspace;
  if (!workspace) {
    elements.workspacePanel.innerHTML = emptyPanel(
      "loader-circle",
      "正在读取工作区",
      "",
      "spinning",
    );
    refreshIcons();
    return;
  }

  const runtime = workspace.runtime;
  const resources = [
    ["list-checks", "短期计划", workspace.todos, "当前 AgentRun 的 Todo"],
    ["clipboard-list", "持久任务", workspace.tasks, "依赖任务与认领状态"],
    ["cpu", "后台 Job", workspace.jobs, "异步执行与进度"],
    ["calendar-clock", "定时计划", workspace.schedules, "Cron 自动触发"],
    ["users", "Agent 团队", workspace.teammates, "长期队友与消息总线"],
    ["handshake", "团队协议", workspace.protocols, "计划审批与关闭握手"],
    ["radio", "自治会话", workspace.autonomy_sessions, "受控任务认领循环"],
    ["git-branch", "Worktrees", workspace.worktrees, "隔离代码工作区"],
    ["plug-zap", "MCP Servers", workspace.mcp_servers, "外部工具连接"],
    ["brain", "长期记忆", workspace.memories, "跨会话事实与偏好"],
  ];

  elements.workspacePanel.innerHTML = `
    <div class="runtime-grid">
      ${keyValue("模型", runtime.model)}
      ${keyValue("工具", `${runtime.tool_count} 个`)}
      ${keyValue("执行器", runtime.execution_backend)}
      ${keyValue("数据库", runtime.database_backend)}
    </div>
    <div class="section-heading">
      <h3>运行资源</h3>
      <button
        class="icon-button subtle"
        type="button"
        data-refresh-workspace
        title="刷新工作区"
        aria-label="刷新工作区"
      ><i data-lucide="refresh-cw"></i></button>
    </div>
    <div class="resource-list">
      ${resources
        .map(([icon, label, items, description]) =>
          resourceRow(icon, label, items?.length || 0, description),
        )
        .join("")}
    </div>
    ${renderActiveWork(workspace)}
  `;
  elements.workspacePanel
    .querySelector("[data-refresh-workspace]")
    .addEventListener("click", () => loadWorkspace());
  refreshIcons();
}

function renderKnowledgePanel() {
  const knowledge = state.knowledge;
  if (!knowledge) {
    elements.knowledgePanel.innerHTML = emptyPanel(
      "loader-circle",
      "正在读取知识库",
      "",
      "spinning",
    );
    refreshIcons();
    return;
  }

  elements.knowledgePanel.innerHTML = `
    <div class="knowledge-status">
      <i data-lucide="${knowledge.vector_ready ? "database" : "database-zap"}"></i>
      <div>
        <strong>${knowledge.vector_ready ? "向量索引可用" : "等待索引重建"}</strong>
        <span>${knowledge.chunk_count} 个片段 · ${escapeHtml(knowledge.embedding_model)}</span>
      </div>
      <span class="status-badge ${knowledge.vector_ready ? "" : "pending"}">
        ${knowledge.vector_ready ? "READY" : "PENDING"}
      </span>
    </div>
    <div class="knowledge-actions">
      <button class="action-button" type="button" data-knowledge-upload>
        <i data-lucide="upload"></i><span>上传文档</span>
      </button>
      <button class="action-button primary" type="button" data-knowledge-rebuild>
        <i data-lucide="refresh-cw"></i><span>重建索引</span>
      </button>
    </div>
    ${sectionHeading("源文档", `${knowledge.document_count} 个`)}
    <div class="document-list">
      ${knowledge.documents.length
        ? knowledge.documents.map(renderKnowledgeDocument).join("")
        : '<div class="resource-detail">知识库中还没有文档。</div>'}
    </div>
    ${knowledge.error ? `<div class="inline-error">${escapeHtml(knowledge.error)}</div>` : ""}
  `;

  elements.knowledgePanel
    .querySelector("[data-knowledge-upload]")
    .addEventListener("click", () => elements.documentInput.click());
  elements.knowledgePanel
    .querySelector("[data-knowledge-rebuild]")
    .addEventListener("click", rebuildKnowledge);
  elements.knowledgePanel.querySelectorAll("[data-delete-document]").forEach((button) => {
    button.addEventListener("click", () => deleteKnowledgeDocument(button.dataset.deleteDocument));
  });
  refreshIcons();
}

function renderEvalsPanel() {
  const opened = Array.from(elements.evalsPanel.querySelectorAll("details.eval-case[open]")).map(item => item.dataset.caseId);
  const dashboard = state.evalDashboard;
  const reportSuite = state.evalSuiteId === window.FundReportEvals.suiteId;
  const pending = reportSuite ? getReportEvalController().pending() : null;
  const locked = state.evalRunning || Boolean(state.evalReviewSaving) || Boolean(state.evalRunLoading);
  const picker = window.FundReportEvals.renderPicker(state.evalSuiteId, locked);
  if (!dashboard) {
    elements.evalsPanel.innerHTML = picker + emptyPanel(
      state.evalError ? "triangle-alert" : "loader-circle",
      state.evalError ? "评测数据暂不可用" : "正在读取评测数据",
      state.evalError,
      state.evalError ? "" : "spinning",
    );
    bindReportEvalControls();
    refreshIcons();
    return;
  }

  const latest = dashboard.latest_run;
  const suite = dashboard.suite;
  const observations = dashboard.observability;
  const feedback = state.feedbackDashboard;
  const smokeCount = suite.scenarios.filter((item) => item.tags?.includes("smoke")).length;
  const scoreDelta = latest?.score_delta;
  const resultByCase = new Map(
    (latest?.results || []).map((item) => [item.case_id, item]),
  );

  elements.evalsPanel.innerHTML = `
    ${picker}
    ${reportSuite ? `<label class="report-eval-field">检索配置<select data-report-eval-mode ${locked ? "disabled" : ""}>
      <option value="hybrid" ${state.evalRetrievalMode === "hybrid" ? "selected" : ""}>语义 + 关键词 · hybrid</option>
      <option value="keyword" ${state.evalRetrievalMode === "keyword" ? "selected" : ""}>关键词 · keyword 对照组</option>
    </select></label>` : ""}
    <div class="run-summary">
      ${summaryStat(reportSuite ? "自动检查得分" : "最近得分", latest ? `${latest.score.toFixed(1)}%` : "-")}
      ${summaryStat(reportSuite ? "自动检查通过" : "通过场景", latest ? `${latest.passed_cases}/${latest.total_cases}` : "-")}
      ${summaryStat("同组变化", Number.isFinite(scoreDelta) ? `${scoreDelta >= 0 ? "+" : ""}${scoreDelta.toFixed(1)}` : "-")}
      ${summaryStat("平均耗时", latest ? formatDurationMs(latest.average_duration_ms) : "-")}
      ${summaryStat("工具调用", latest ? String(latest.total_tool_calls || 0) : "-")}
      ${summaryStat("平均 Token", latest ? String(latest.average_tokens || 0) : "-")}
    </div>

    <div class="eval-dataset-strip">
      <span><i data-lucide="database"></i>${escapeHtml(suite.data_profile || "固定数据")} · ${latest?.total_cases || suite.scenarios.length} 条</span>
      <code>${escapeHtml(suite.data_fingerprint || "暂无指纹")}</code>
      ${latest?.case_set_id ? `<code>${escapeHtml(latest.case_set_id)}</code>` : ""}
    </div>

    ${latest ? (reportSuite ? window.FundReportEvals.renderMetrics(latest) : renderEvalMetricScores(latest.metric_scores || {})) : ""}
    ${reportSuite && dashboard.recent_eval_runs?.length ? `<label class="report-eval-field">评测记录<select data-report-eval-run ${locked ? "disabled" : ""}>
      ${dashboard.recent_eval_runs.map(run => `<option value="${escapeAttribute(run.id)}" ${run.id === latest?.id ? "selected" : ""}>${escapeHtml(formatConversationTime(run.created_at))} · ${run.total_cases} 题 · ${run.passed_cases} 自动通过</option>`).join("")}
    </select></label>` : ""}
    ${state.evalRunLoading ? '<p role="status" class="resource-detail">正在读取所选记录，当前保留上一份结果。</p>' : ""}
    ${state.evalRunLoadError ? `<div class="inline-error" role="alert">所选记录读取失败，仍显示上一份结果：${escapeHtml(state.evalRunLoadError.message)}
      <button class="action-button" type="button" data-report-eval-retry ${locked ? "disabled" : ""}><i data-lucide="refresh-cw"></i>重试读取</button></div>` : ""}

    <div class="eval-actions">
      <button
        class="action-button"
        type="button"
        data-run-evals="smoke"
        ${locked || pending ? "disabled" : ""}
      >
        <i data-lucide="${state.evalRunning ? "loader-circle" : "play"}" class="${state.evalRunning ? "spinning" : ""}"></i>
        <span>${state.evalRunning === "smoke" ? "正在运行" : `冒烟 ${smokeCount} 条`}</span>
      </button>
      <button
        class="action-button primary"
        type="button"
        data-run-evals="all"
        ${locked || pending ? "disabled" : ""}
      >
        <i data-lucide="${state.evalRunning ? "loader-circle" : "flask-conical"}" class="${state.evalRunning ? "spinning" : ""}"></i>
        <span>${state.evalRunning === "all" ? "正在运行" : `完整 ${suite.scenarios.length} 条`}</span>
      </button>
    </div>

    ${reportSuite && pending ? `<div class="report-eval-task">
      <p id="report-eval-progress" role="status">${escapeHtml(state.evalProgress || "已有评测任务，可继续查看。")}</p>
      <div class="eval-actions">
        <button class="action-button" type="button" data-report-eval-resume ${state.evalRunning ? "disabled" : ""}><i data-lucide="refresh-cw"></i>继续查看</button>
        <button class="action-button" type="button" data-report-eval-cancel ${pending.jobId ? "" : "disabled"}><i data-lucide="square"></i>取消任务</button>
      </div>
    </div>` : ""}

    ${state.evalError ? `<div class="inline-error">${escapeHtml(state.evalError)}</div>` : ""}
    ${!reportSuite && state.feedbackError ? `<div class="inline-error">${escapeHtml(state.feedbackError)}</div>` : ""}
    ${!reportSuite && state.evalDraftError ? `<div class="inline-error">${escapeHtml(state.evalDraftError)}</div>` : ""}

    ${!reportSuite ? sectionHeading("真实反馈候选", feedback ? `${feedback.pending_count} 条待审核` : "") : ""}
    ${!reportSuite ? (feedback ? renderFeedbackCandidates(feedback) : '<div class="resource-detail">正在读取用户反馈。</div>') : ""}

    ${sectionHeading("固定场景", `${suite.scenarios.length} 个`)}
    <div class="eval-case-list">
      ${suite.scenarios
        .map((scenario) => renderEvalScenario(scenario, resultByCase.get(scenario.id)))
        .join("")}
    </div>

    ${sectionHeading("最近运行", `${observations.run_count} 次`)}
    <div class="compact-list">
      ${(observations.recent_runs || []).length
        ? observations.recent_runs
            .slice(0, 6)
            .map((run) =>
              compactMetric(
                run.source === "eval" ? "flask-conical" : "message-square",
                run.source === "eval" ? `评测 · ${run.eval_case_id || run.run_id}` : "用户对话",
                run.metrics?.tool_error_count ? "有错误" : "正常",
                `${formatDurationMs(run.duration_ms)} · ${run.usage?.total_tokens || 0} tokens`,
              ),
            )
            .join("")
        : '<div class="resource-detail">还没有可观察的 Agent 运行。</div>'}
    </div>
  `;

  bindReportEvalControls();
  elements.evalsPanel.querySelectorAll("details.eval-case").forEach(item => {item.open = opened.includes(item.dataset.caseId);});
  elements.evalsPanel
    .querySelectorAll("[data-run-evals]")
    .forEach((button) => {
      button.addEventListener("click", () => runAgentEvals(button.dataset.runEvals));
    });
  elements.evalsPanel
    .querySelectorAll("[data-review-feedback]")
    .forEach((button) => {
      button.addEventListener("click", () => {
        reviewFeedbackCandidate(
          button.dataset.reviewFeedback,
          button.dataset.reviewStatus,
        );
      });
    });
  elements.evalsPanel
    .querySelectorAll("[data-edit-eval-draft]")
    .forEach((button) => {
      button.addEventListener("click", () => {
        const feedbackId = button.dataset.editEvalDraft;
        state.evalDraftEditorFeedbackId =
          state.evalDraftEditorFeedbackId === feedbackId ? null : feedbackId;
        renderEvalsPanel();
      });
    });
  elements.evalsPanel
    .querySelectorAll("[data-close-eval-draft]")
    .forEach((button) => {
      button.addEventListener("click", () => {
        state.evalDraftEditorFeedbackId = null;
        renderEvalsPanel();
      });
    });
  elements.evalsPanel
    .querySelectorAll("[data-eval-draft-form]")
    .forEach((form) => {
      form.addEventListener("submit", (event) => {
        event.preventDefault();
        saveEvalDraft(form.dataset.evalDraftForm, form);
      });
    });
  refreshIcons();
}

function bindReportEvalControls() {
  const panel = elements.evalsPanel;
  const locked = () => state.evalRunning || state.evalReviewSaving || state.evalRunLoading;
  panel.querySelector("[data-eval-suite]")?.addEventListener("change", event => {
    if (locked()) return;
    resetReportEvalViewer();
    state.evalSuiteId = event.target.value;
    renderEvalsPanel();
    loadEvalDashboard();
  });
  panel.querySelector("[data-report-eval-mode]")?.addEventListener("change", event => {
    if (locked()) return;
    resetReportEvalViewer();
    state.evalRetrievalMode = event.target.value;
    state.evalDashboard = null;
    renderEvalsPanel();
    loadEvalDashboard();
  });
  panel.querySelector("[data-report-eval-resume]")?.addEventListener("click", () => {
    state.evalError = "";
    getReportEvalController().resume();
  });
  panel.querySelector("[data-report-eval-cancel]")?.addEventListener("click", () => getReportEvalController().cancel());
  panel.querySelector("[data-report-eval-run]")?.addEventListener("change", event => loadReportEvalRun(event.target.value));
  panel.querySelector("[data-report-eval-retry]")?.addEventListener("click", () => loadReportEvalRun(state.evalRunLoadError?.runId));
  panel.querySelectorAll("[data-report-eval-review]").forEach(form => {
    // 草稿以用户/运行/题号隔离。重绘或切记录不会丢失其他题未提交的说明。
    const remember = () => {
      const data = new FormData(form);
      state.evalReviewDrafts[reportEvalDraftKey(form.dataset.run, form.dataset.case)] = {status:data.get("status"), note:data.get("note")};
    };
    form.addEventListener("input", remember);
    form.addEventListener("change", remember);
    form.addEventListener("submit", event => {event.preventDefault(); remember(); saveReportEvalReview(form);});
  });
}

function reportEvalDraftKey(runId, caseId) {
  return `${state.userId}:${runId}:${caseId}`;
}

async function loadReportEvalRun(runId) {
  if (!runId || state.evalRunning || state.evalReviewSaving || state.evalRunLoading) return;
  const user = state.userId, suite = state.evalSuiteId, mode = state.evalRetrievalMode;
  const requestId = ++state.evalDashboardRequestId;
  const current = () => user === state.userId && suite === state.evalSuiteId && mode === state.evalRetrievalMode && requestId === state.evalDashboardRequestId;
  const loading = {runId, requestId};
  state.evalRunLoading = loading;
  state.evalRunLoadError = null;
  renderEvalsPanel();
  try {
    const run = await apiRequest(`${endpoints.evalRuns}/${encodeURIComponent(runId)}?user_id=${encodeURIComponent(user)}`);
    if (current() && state.evalDashboard) state.evalDashboard.latest_run = run;
  } catch (error) {
    if (current()) state.evalRunLoadError = {runId, message:error.message};
  } finally {
    if (state.evalRunLoading === loading) {state.evalRunLoading = null; renderEvalsPanel();}
  }
}

async function saveReportEvalReview(form) {
  if (state.evalReviewSaving || state.evalRunning || state.evalRunLoading) return;
  const user = state.userId, suite = state.evalSuiteId, mode = state.evalRetrievalMode;
  const runId = form.dataset.run, caseId = form.dataset.case, data = new FormData(form);
  const payload = {user_id:user, status:data.get("status"), note:String(data.get("note") || "").trim()};
  if (payload.status === "needs_fix" && !payload.note) {showToast("请记录需要修改的具体原因", "error"); return;}
  const saveKey = reportEvalDraftKey(runId, caseId);
  const submittedDraft = state.evalReviewDrafts[saveKey];
  const saving = {saveKey};
  const current = () => user === state.userId && suite === state.evalSuiteId && mode === state.evalRetrievalMode
    && state.evalDashboard?.latest_run?.id === runId && state.evalReviewSaving === saving;
  state.evalReviewSaving = saving;
  renderEvalsPanel();
  try {
    const run = await apiRequest(`${endpoints.evalRuns}/${encodeURIComponent(runId)}/cases/${encodeURIComponent(caseId)}/review`, {
      method:"PUT", headers:{"Content-Type":"application/json"}, body:JSON.stringify(payload),
    });
    if (!current()) return;
    // 旧请求只能清理自己提交的草稿，不能删除切换账号后重新编辑的新版本。
    if (state.evalReviewDrafts[saveKey] === submittedDraft) delete state.evalReviewDrafts[saveKey];
    state.evalDashboard.latest_run = run;
    showToast("复核已保存，自动评分保持不变");
  } catch (error) {
    if (current()) showToast(error.message, "error");
  } finally {
    if (state.evalReviewSaving === saving) {state.evalReviewSaving = null; renderEvalsPanel();}
  }
}

function renderFeedbackCandidates(feedback) {
  const pendingCandidates = feedback.candidates || [];
  const approvedCandidates = feedback.approved_candidates || [];
  const draftByFeedback = new Map(
    (state.evalDrafts || []).map((draft) => [draft.source_feedback_id, draft]),
  );
  return `
    <div class="feedback-summary" aria-label="用户反馈统计">
      <span><strong>${feedback.helpful_count}</strong>有帮助</span>
      <span><strong>${feedback.unhelpful_count}</strong>没帮助</span>
      <span><strong>${feedback.approved_count}</strong>已保留</span>
    </div>
    <div class="feedback-list-label"><span>待审核</span><b>${pendingCandidates.length}</b></div>
    <div class="feedback-candidate-list">
      ${pendingCandidates.length
        ? pendingCandidates.map(renderFeedbackCandidate).join("")
        : '<div class="resource-detail">当前没有待审核的负向反馈。</div>'}
    </div>
    <div class="feedback-list-label"><span>已保留</span><b>${approvedCandidates.length}</b></div>
    <div class="feedback-candidate-list approved">
      ${approvedCandidates.length
        ? approvedCandidates
            .map((candidate) => renderApprovedFeedbackCandidate(
              candidate,
              draftByFeedback.get(candidate.id),
            ))
            .join("")
        : '<div class="resource-detail">还没有等待整理的已保留案例。</div>'}
    </div>
  `;
}

function renderFeedbackCandidate(candidate) {
  const run = candidate.run;
  const toolNames = (run?.tool_calls || []).map((item) => item.name).join(", ");
  return `
    <details class="feedback-candidate">
      <summary>
        <span class="eval-case-copy">
          <strong>${escapeHtml(feedbackReasonLabel(candidate.reason_code))}</strong>
          <span>${escapeHtml(truncate(candidate.question || "未保存问题", 90))}</span>
        </span>
        <span class="status-badge pending">待审核</span>
      </summary>
      <div class="feedback-candidate-detail">
        <div class="feedback-copy-block">
          <strong>用户问题</strong>
          <p>${escapeHtml(candidate.question || "-")}</p>
        </div>
        <div class="feedback-copy-block">
          <strong>Agent 回答</strong>
          <p>${escapeHtml(candidate.answer || "-")}</p>
        </div>
        ${candidate.comment
          ? `<div class="feedback-copy-block"><strong>补充说明</strong><p>${escapeHtml(candidate.comment)}</p></div>`
          : ""}
        ${candidate.correction
          ? `<div class="feedback-copy-block correction"><strong>正确方向</strong><p>${escapeHtml(candidate.correction)}</p></div>`
          : ""}
        <div class="feedback-run-meta">
          <span>${escapeHtml(candidate.run_id || "无 run_id")}</span>
          <span>${run ? `${run.usage?.total_tokens || 0} tokens` : "无运行观测"}</span>
          <span>${run ? formatDurationMs(run.duration_ms) : "-"}</span>
        </div>
        ${toolNames ? `<div class="eval-expected-tools">工具：${escapeHtml(toolNames)}</div>` : ""}
        <div class="feedback-review-actions">
          <button
            class="action-button"
            type="button"
            data-review-feedback="${escapeAttribute(candidate.id)}"
            data-review-status="dismissed"
          ><i data-lucide="archive-x"></i><span>忽略</span></button>
          <button
            class="action-button primary"
            type="button"
            data-review-feedback="${escapeAttribute(candidate.id)}"
            data-review-status="approved"
          ><i data-lucide="bookmark-check"></i><span>保留候选</span></button>
        </div>
      </div>
    </details>
  `;
}

function renderApprovedFeedbackCandidate(candidate, draft) {
  const editorOpen = state.evalDraftEditorFeedbackId === candidate.id;
  return `
    <details class="feedback-candidate approved" ${editorOpen ? "open" : ""}>
      <summary>
        <span class="eval-case-copy">
          <strong>${escapeHtml(draft?.name || feedbackReasonLabel(candidate.reason_code))}</strong>
          <span>${escapeHtml(truncate(candidate.question || "未保存问题", 90))}</span>
        </span>
        <span class="status-badge ${draft ? "" : "pending"}">${draft ? "草稿已保存" : "待整理"}</span>
      </summary>
      <div class="feedback-candidate-detail">
        <div class="feedback-copy-block">
          <strong>用户问题</strong>
          <p>${escapeHtml(candidate.question || "-")}</p>
        </div>
        ${candidate.correction
          ? `<div class="feedback-copy-block correction"><strong>正确方向</strong><p>${escapeHtml(candidate.correction)}</p></div>`
          : ""}
        ${draft
          ? `<div class="eval-draft-path"><i data-lucide="file-json"></i><code>${escapeHtml(draft.file_path)}</code></div>`
          : ""}
        <button
          class="action-button ${draft ? "" : "primary"}"
          type="button"
          data-edit-eval-draft="${escapeAttribute(candidate.id)}"
        ><i data-lucide="${draft ? "file-pen-line" : "file-plus-2"}"></i><span>${draft ? "编辑草稿" : "创建草稿"}</span></button>
        ${editorOpen ? renderEvalDraftEditor(candidate, draft) : ""}
      </div>
    </details>
  `;
}

function renderEvalDraftEditor(candidate, draft) {
  const route = draft?.expected_route || inferredDraftRoute(candidate);
  const category = draft?.category || inferredDraftCategory(route, candidate);
  const selectedTools = new Set(
    draft?.expected_tools?.length
      ? draft.expected_tools
      : (candidate.run?.tool_calls || []).map((item) => item.name),
  );
  const caseSuffix = candidate.id.replace(/^feedback_/, "").slice(-12);
  const busy = state.evalDraftSavingFeedbackId === candidate.id;
  const description = draft?.description
    || candidate.comment
    || "由真实用户负向反馈整理，验证同类问题不会再次出现。";
  const evidenceRequired = draft
    ? draft.require_evidence_reference
    : Boolean(candidate.run?.evidence_ids?.length);
  return `
    <form class="eval-draft-form" data-eval-draft-form="${escapeAttribute(candidate.id)}">
      <div class="feedback-form-header">
        <strong>评测草稿</strong>
        <button
          class="icon-button"
          type="button"
          data-close-eval-draft
          title="关闭草稿编辑器"
          aria-label="关闭草稿编辑器"
        ><i data-lucide="x"></i></button>
      </div>
      <label>
        <span>Case ID</span>
        <input name="case_id" required maxlength="80" pattern="[a-z0-9]+(?:-[a-z0-9]+)*" value="${escapeAttribute(draft?.case_id || `feedback-${caseSuffix}`)}">
      </label>
      <label>
        <span>场景名称</span>
        <input name="name" required maxlength="120" value="${escapeAttribute(draft?.name || `${feedbackReasonLabel(candidate.reason_code)}回归`)}">
      </label>
      <label>
        <span>场景说明</span>
        <textarea name="description" required maxlength="500" rows="2">${escapeHtml(description)}</textarea>
      </label>
      <div class="eval-draft-field-grid">
        <label>
          <span>预期路由</span>
          <select name="expected_route">${evalDraftRouteOptions(route)}</select>
        </label>
        <label>
          <span>场景分类</span>
          <select name="category">${evalDraftCategoryOptions(category)}</select>
        </label>
      </div>
      <label>
        <span>基金代码</span>
        <input name="fund_code" maxlength="20" inputmode="numeric" value="${escapeAttribute(draft?.fund_code || "")}" placeholder="没有则留空">
      </label>
      <fieldset class="eval-draft-tools">
        <legend>预期工具</legend>
        ${evalDraftToolOptions(selectedTools)}
      </fieldset>
      <label>
        <span>回答关键事实</span>
        <textarea name="required_answer_terms" required rows="3" placeholder="每行一个，例如：&#10;回撤&#10;不确定性">${escapeHtml((draft?.required_answer_terms || []).join("\n"))}</textarea>
      </label>
      <div class="eval-draft-field-grid">
        <label>
          <span>匹配方式</span>
          <select name="answer_match">
            <option value="all" ${draft?.answer_match !== "any" ? "selected" : ""}>全部出现</option>
            <option value="any" ${draft?.answer_match === "any" ? "selected" : ""}>任一出现</option>
          </select>
        </label>
        <label class="eval-draft-toggle">
          <input name="require_evidence_reference" type="checkbox" ${evidenceRequired ? "checked" : ""}>
          <span>要求引用证据 ID</span>
        </label>
      </div>
      <div class="eval-draft-form-actions">
        <button class="action-button primary" type="submit" ${busy ? "disabled" : ""}>
          <i data-lucide="${busy ? "loader-circle" : "save"}" class="${busy ? "spinning" : ""}"></i>
          <span>${busy ? "正在保存" : "保存草稿"}</span>
        </button>
      </div>
    </form>
  `;
}

function inferredDraftRoute(candidate) {
  const route = candidate.run?.metrics?.route_name;
  const supported = [
    "direct",
    "fund_profile",
    "fund_nav",
    "fund_risk",
    "fund_assessment",
    "market",
    "portfolio",
  ];
  return supported.includes(route) ? route : "direct";
}

function inferredDraftCategory(route, candidate) {
  if (route !== "market") return route;
  const tools = (candidate.run?.tool_calls || []).map((item) => item.name);
  return tools.includes("search_market_events_tool")
    ? "market_events"
    : "market_sentiment";
}

function evalDraftRouteOptions(selectedRoute) {
  const routes = {
    direct: "直接回答",
    fund_profile: "基金档案",
    fund_nav: "净值表现",
    fund_risk: "风险指标",
    fund_assessment: "基金综合研判",
    market: "市场与事件",
    portfolio: "组合研判",
  };
  return Object.entries(routes)
    .map(([value, label]) => `<option value="${value}" ${value === selectedRoute ? "selected" : ""}>${label}</option>`)
    .join("");
}

function evalDraftCategoryOptions(selectedCategory) {
  const categories = {
    general: "通用",
    fund_profile: "基金档案",
    fund_nav: "净值表现",
    fund_risk: "基金风险",
    fund_assessment: "基金综合研判",
    market_sentiment: "市场情绪",
    market_events: "市场事件",
    portfolio: "组合研判",
  };
  return Object.entries(categories)
    .map(([value, label]) => `<option value="${value}" ${value === selectedCategory ? "selected" : ""}>${label}</option>`)
    .join("");
}

function evalDraftToolOptions(selectedTools) {
  const tools = [
    ["get_fund_research_brief_tool", "基金完整研究 Brief"],
    ["get_portfolio_research_brief_tool", "组合完整研究 Brief"],
    ["get_fund_profile_tool", "基金档案"],
    ["get_fund_nav_summary_tool", "净值摘要"],
    ["get_fund_risk_tool", "风险指标"],
    ["get_fund_assessment_tool", "基金综合研判"],
    ["get_market_sentiment_tool", "市场情绪"],
    ["search_market_events_tool", "市场事件"],
    ["get_portfolio_assessment_tool", "组合研判"],
  ];
  for (const toolName of selectedTools) {
    if (!tools.some(([name]) => name === toolName)) tools.push([toolName, toolName]);
  }
  return tools
    .map(([name, label]) => `
      <label>
        <input name="expected_tools" type="checkbox" value="${escapeAttribute(name)}" ${selectedTools.has(name) ? "checked" : ""}>
        <span>${escapeHtml(label)}</span>
      </label>
    `)
    .join("");
}

function feedbackReasonLabel(reasonCode) {
  return {
    inaccurate: "事实不准确",
    incomplete: "信息不完整",
    wrong_tool: "工具或数据不对",
    unclear: "表达不清楚",
    outdated: "信息已过时",
    other: "其他问题",
  }[reasonCode] || "未说明原因";
}

function renderEvalMetricScores(metrics) {
  const labels = {
    routing: "路由",
    execution: "执行",
    grounding: "事实",
    answer_quality: "表达",
    safety: "边界",
    efficiency: "效率",
  };
  const entries = Object.entries(labels).filter(([key]) => Number.isFinite(metrics[key]));
  if (!entries.length) return "";
  return `
    <div class="eval-metric-grid" aria-label="评测分项得分">
      ${entries.map(([key, label]) => {
        const value = Math.max(0, Math.min(100, Number(metrics[key])));
        return `
          <div class="eval-metric">
            <span><strong>${escapeHtml(label)}</strong><b>${value.toFixed(1)}</b></span>
            <div><i style="width:${value}%"></i></div>
          </div>
        `;
      }).join("")}
    </div>
  `;
}

function renderEvalScenario(scenario, result) {
  const reportSuite = state.evalSuiteId === window.FundReportEvals.suiteId;
  const status = !result ? "未运行" : result.passed ? (reportSuite ? "自动通过" : "通过") : "未通过";
  const statusClass = !result ? "pending" : result.passed ? "" : "error";
  const manualStatus = result?.report_quality?.manual_review?.status;
  const checks = result?.checks || [];
  return `
    <details class="eval-case" data-case-id="${escapeAttribute(scenario.id)}">
      <summary>
        <span class="eval-case-copy">
          <strong>${escapeHtml(scenario.name)}<small>${escapeHtml(scenario.category || "general")}</small></strong>
          <span>${escapeHtml(scenario.description)}</span>
        </span>
        <span class="report-eval-badges"><span class="status-badge ${statusClass}">${status}</span>
          ${reportSuite && manualStatus ? `<span class="status-badge ${manualStatus === "needs_fix" ? "error" : "pending"}">${({pending:"待复核", accepted:"人工确认", needs_fix:"需修改"})[manualStatus] || "待复核"}</span>` : ""}
        </span>
      </summary>
      <div class="eval-case-detail">
        ${result
          ? `
            <div class="eval-case-meta">
              <span>${result.score.toFixed(1)} 分</span>
              <span>${result.tool_call_count || 0} 次工具</span>
              <span>${result.usage?.total_tokens || 0} tokens</span>
              <span>${formatDurationMs(result.duration_ms)}</span>
            </div>
            <div class="eval-check-list">
              ${checks.map(renderEvalCheck).join("")}
            </div>
            ${reportSuite ? window.FundReportEvals.renderQuality(result, state.evalDashboard?.latest_run?.id,
              state.evalRunning || state.evalReviewSaving || state.evalRunLoading,
              state.evalReviewDrafts[reportEvalDraftKey(state.evalDashboard?.latest_run?.id, result.case_id)]) : result.answer_preview
              ? `<div class="eval-answer-preview"><strong>回答摘要</strong><p>${escapeHtml(result.answer_preview)}</p></div>`
              : ""}
            ${result.error ? `<div class="inline-error">${escapeHtml(result.error)}</div>` : ""}
          `
          : `
            <div class="resource-detail">${escapeHtml(scenario.message)}</div>
            <div class="eval-expected-tools">预期工具：${escapeHtml((scenario.expected_tools || []).join(", ") || "无需工具")}</div>
          `}
      </div>
    </details>
  `;
}

function renderEvalCheck(check) {
  return `
    <div class="eval-check ${check.passed ? "" : "failed"}">
      <i data-lucide="${check.passed ? "circle-check" : "circle-x"}"></i>
      <span><strong>${escapeHtml(check.name)}<small>${escapeHtml(check.dimension || "")}</small></strong>${escapeHtml(check.detail)}</span>
    </div>
  `;
}

function renderKnowledgeDocument(document) {
  return `
    <div class="document-row">
      <span class="resource-icon"><i data-lucide="file-text"></i></span>
      <div class="document-copy">
        <strong>${escapeHtml(document.name)}</strong>
        <span>${formatBytes(document.size_bytes)} · ${formatConversationTime(document.modified_at)}</span>
      </div>
      <button
        class="icon-button document-delete"
        type="button"
        data-delete-document="${escapeAttribute(document.name)}"
        title="删除文档"
        aria-label="删除文档"
      ><i data-lucide="trash-2"></i></button>
    </div>
  `;
}

function renderActiveWork(workspace) {
  const activeJobs = (workspace.jobs || []).filter((item) =>
    ["queued", "running"].includes(item.status),
  );
  const activeTasks = (workspace.tasks || []).filter((item) => item.status === "in_progress");
  const items = [
    ...activeTasks.map((item) => ({
      title: item.subject,
      subtitle: item.owner ? `负责人 ${item.owner}` : "任务执行中",
      status: item.status,
    })),
    ...activeJobs.map((item) => ({
      title: item.title,
      subtitle: `${item.progress_percent || 0}% · ${item.progress_message || "等待执行"}`,
      status: item.status,
    })),
  ].slice(0, 5);

  if (!items.length) return "";
  return `
    ${sectionHeading("正在进行", `${items.length} 项`)}
    <div class="compact-list">
      ${items
        .map(
          (item) => `
            <div class="compact-row">
              <span class="resource-icon"><i data-lucide="activity"></i></span>
              <div class="compact-copy">
                <strong>${escapeHtml(item.title)}</strong>
                <span>${escapeHtml(item.subtitle)}</span>
              </div>
              <span class="status-badge ${escapeAttribute(item.status)}">${statusLabel(item.status)}</span>
            </div>
          `,
        )
        .join("")}
    </div>
  `;
}

function renderToolCalls(toolCalls) {
  return `
    <div class="tool-call-list">
      ${toolCalls
        .map(
          (call) => `
            <details class="tool-call">
              <summary>
                <span class="tool-icon"><i data-lucide="wrench"></i></span>
                <span class="tool-call-name" title="${escapeAttribute(call.tool_name)}">${escapeHtml(toolDisplayName(call.tool_name))}</span>
                <span class="status-badge ${call.is_error ? "error" : ""}">${call.is_error ? "失败" : "完成"}</span>
              </summary>
              <pre class="tool-detail">参数
${escapeHtml(formatJson(call.tool_args))}

结果
${escapeHtml(truncate(String(call.tool_result || ""), 1800))}</pre>
            </details>
          `,
        )
        .join("")}
    </div>
  `;
}

function toolDisplayName(toolName) {
  return {
    get_fund_research_brief_tool: "基金完整研究 Brief",
    get_portfolio_research_brief_tool: "组合完整研究 Brief",
  }[toolName] || toolName;
}

function activateInspectorTab(tabName) {
  document.querySelectorAll("[data-tab]").forEach((button) => {
    button.classList.toggle("active", button.dataset.tab === tabName);
    button.setAttribute("aria-selected", String(button.dataset.tab === tabName));
  });
  document.querySelectorAll(".inspector-panel").forEach((panel) => {
    panel.classList.toggle("active", panel.id === `${tabName}-panel`);
  });
}

function updateConversationHeading(conversation = null) {
  const selected =
    conversation ||
    state.conversations.find((item) => item.id === state.currentConversationId);
  elements.conversationTitle.textContent = selected?.title || "新对话";
  elements.conversationMeta.textContent = selected
    ? `${selected.message_count || state.messages.length} 条消息`
    : "准备就绪";
}

function updateUserAvatar() {
  elements.userAvatar.textContent = state.userId.slice(0, 2).toUpperCase();
  elements.userDisplay.textContent = state.auth.authenticated
    ? state.auth.user.display_name
    : "个人工作区";
}

function resizeComposer() {
  elements.messageInput.style.height = "auto";
  elements.messageInput.style.height = `${Math.min(elements.messageInput.scrollHeight, 180)}px`;
}

function showChatError(message, { canRetry = false } = {}) {
  elements.chatErrorText.textContent = message;
  elements.retryMessageButton.hidden = !canRetry;
  elements.chatError.hidden = false;
  refreshIcons();
}

function hideChatError() {
  elements.chatError.hidden = true;
  elements.chatErrorText.textContent = "";
  elements.retryMessageButton.hidden = true;
}

function showToast(message, type = "success") {
  const toast = document.createElement("div");
  toast.className = `toast ${type}`;
  toast.innerHTML = `
    <i data-lucide="${type === "error" ? "circle-alert" : "circle-check"}"></i>
    <span>${escapeHtml(message)}</span>
  `;
  elements.toastRegion.appendChild(toast);
  refreshIcons();
  window.setTimeout(() => toast.remove(), 3400);
}

function setKnowledgeBusy(isBusy, label = "处理中") {
  elements.knowledgePanel.querySelectorAll("button").forEach((button) => {
    button.disabled = isBusy;
  });
  if (isBusy) showToast(label);
}

let sidebarTrigger = null;
function openSidebar(event) {
  sidebarTrigger = event.currentTarget;
  document.body.classList.add("sidebar-open");
  document.body.classList.remove("inspector-open");
  // 手机抽屉打开时，背景工作台不再接收键盘输入；关闭后恢复原来的触发按钮。
  [elements.fundShell, elements.chatShell, elements.inspector, document.querySelector(".mobile-product-nav")]
    .forEach(element => { if (element) element.inert = true; });
  document.querySelector("#close-sidebar").focus();
}

function closePanels() {
  document.body.classList.remove("sidebar-open", "inspector-open");
  [elements.fundShell, elements.chatShell, elements.inspector, document.querySelector(".mobile-product-nav")]
    .forEach(element => { if (element) element.inert = false; });
  if (sidebarTrigger && window.matchMedia?.("(max-width: 1080px)").matches) sidebarTrigger.focus();
  sidebarTrigger = null;
}

function shouldPollWorkspace() {
  if (!state.workspace) return false;
  const jobsRunning = (state.workspace.jobs || []).some((item) =>
    ["queued", "running"].includes(item.status),
  );
  const autonomyRunning = (state.workspace.autonomy_sessions || []).some((item) =>
    ["queued", "running", "idle", "working"].includes(item.status),
  );
  return jobsRunning || autonomyRunning;
}

function findLatestRunMetadata(messages) {
  return [...messages]
    .reverse()
    .find((item) => item.role === "assistant" && item.run_metadata?.stop_reason)
    ?.run_metadata;
}

function fundMetric(label, value, valueClass = "") {
  return `
    <div class="fund-metric">
      <span>${escapeHtml(label)}</span>
      <strong class="${escapeAttribute(valueClass)}">${escapeHtml(value)}</strong>
    </div>
  `;
}

function fundPeriodButton(period, label) {
  const active = state.fundChartPeriod === period ? " active" : "";
  return `
    <button
      class="period-button${active}"
      type="button"
      data-fund-period="${escapeAttribute(period)}"
      aria-pressed="${state.fundChartPeriod === period}"
    >${escapeHtml(label)}</button>
  `;
}

function fundRiskPeriodButton(months, label) {
  const active = state.fundRiskMonths === months ? " active" : "";
  return `
    <button
      class="period-button${active}"
      type="button"
      data-fund-risk-period="${months}"
      aria-pressed="${state.fundRiskMonths === months}"
      ${state.fundRiskLoading || state.fundAssessmentLoading ? "disabled" : ""}
    >${escapeHtml(label)}</button>
  `;
}

function fundRiskMetric(label, value, valueClass = "") {
  return `
    <div class="fund-risk-metric">
      <span>${escapeHtml(label)}</span>
      <strong class="${escapeAttribute(valueClass)}">${escapeHtml(value)}</strong>
    </div>
  `;
}

function formatRiskPeriod(months) {
  const labels = { 6: "近 6 个月", 12: "近 1 年", 36: "近 3 年" };
  return labels[months] || `${months} 个月`;
}

function formatAssessmentConfidence(confidence) {
  const labels = { high: "高", medium: "中等", low: "较低" };
  return labels[confidence] || "未知";
}

function formatAssessmentEvidenceKind(kind) {
  const labels = {
    fund_risk: "历史风险",
    market_state: "市场状态",
    market_event: "关联事件",
  };
  return labels[kind] || "其他证据";
}

function formatSemanticOverallView(view) {
  const labels = {
    supportive: "语义支撑因素较多",
    caution: "语义风险因素较多",
    mixed: "事件影响存在分化",
    insufficient: "模型证据不足",
  };
  return labels[view] || "模型证据不足";
}

function formatSemanticDirection(direction) {
  const labels = {
    supportive: "偏支撑",
    caution: "需关注",
    neutral: "中性",
    uncertain: "不确定",
  };
  return labels[direction] || "不确定";
}

function formatSemanticRelevance(relevance) {
  const labels = {
    direct: "直接相关",
    related: "间接相关",
    context: "背景信息",
    uncertain: "关联不明",
  };
  return labels[relevance] || "关联不明";
}

function formatSemanticHorizon(horizon) {
  const labels = {
    short_term: "短期",
    medium_term: "中期",
    long_term: "长期",
    uncertain: "周期不明",
  };
  return labels[horizon] || "周期不明";
}

function formatPlainPercent(value) {
  return `${Number(value).toFixed(2)}%`;
}

function formatCnyAmount(value) {
  const amountInYi = Number(value) / 100_000_000;
  return `${amountInYi.toLocaleString("zh-CN", { maximumFractionDigits: 0 })} 亿元`;
}

function formatCapitalFlow(value) {
  // null 代表历史不足，和真实的 0 元必须显示成不同状态。
  if (value === null || value === undefined || !Number.isFinite(Number(value))) {
    return "-";
  }
  const amountInYi = Number(value) / 100_000_000;
  if (Math.abs(amountInYi) < 0.05) return "≈0 亿";
  const sign = amountInYi > 0 ? "+" : "";
  const digits = Math.abs(amountInYi) < 10 ? 1 : 0;
  return `${sign}${amountInYi.toLocaleString("zh-CN", {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  })} 亿`;
}

function formatIndexLevel(value) {
  const number = Number(value);
  if (!Number.isFinite(number)) return "-";
  return number.toLocaleString("zh-CN", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
}

function formatMoney(value) {
  const number = Number(value);
  if (!Number.isFinite(number)) return "-";
  return new Intl.NumberFormat("zh-CN", {
    style: "currency",
    currency: "CNY",
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  }).format(number);
}

function formatSignedMoney(value) {
  const number = Number(value);
  if (!Number.isFinite(number)) return "-";
  const sign = number > 0 ? "+" : "";
  return `${sign}${formatMoney(number)}`;
}

function formatUnits(value) {
  const number = Number(value);
  if (!Number.isFinite(number)) return "-";
  return number.toLocaleString("zh-CN", { maximumFractionDigits: 4 });
}

function valueTone(value) {
  const number = Number(value);
  if (number > 0) return "positive";
  if (number < 0) return "negative";
  return "neutral";
}

function marketStateTone(stateCode) {
  const tones = {
    constructive: "positive",
    cautious: "negative",
    high_volatility: "caution",
    mixed_recovery: "mixed",
    mixed_weakening: "mixed",
  };
  return tones[stateCode] || "neutral";
}

function formatMarketSourceName(name) {
  if (name.includes("LeGuLeGu")) return "LeGu 市场宽度";
  // 成交额接口有主备两个来源，链接名称必须反映本次数据库快照的真实来源。
  if (name.includes("CSI Index")) return "中证指数";
  if (name.includes("Eastmoney Index")) return "Eastmoney 指数";
  return "Eastmoney 基金";
}

function formatMarketEventType(type) {
  const labels = {
    policy: "政策",
    economic_data: "经济数据",
    regulation: "监管",
    fund_announcement: "基金公告",
    market_notice: "市场公告",
    social_positioning: "社交仓位观点",
  };
  return labels[type] || "事件";
}

function formatMarketImpact(direction) {
  const labels = {
    positive: "偏积极",
    neutral: "中性",
    negative: "偏消极",
    caution: "风险提示",
    mixed: "多空交织",
  };
  return labels[direction] || "待观察";
}

function formatImpactHorizon(horizon) {
  const labels = {
    short_term: "短期",
    medium_term: "中期",
    long_term: "长期",
  };
  return labels[horizon] || "期限未定";
}

function formatEventFreshness(status, decayFactor) {
  const factorPercent = Math.round(Number(decayFactor || 0) * 100);
  if (status === "fresh") return "新鲜 · 100%";
  if (status === "decaying") return `衰减中 · ${factorPercent}%`;
  if (status === "expired") return "已过期 · 不计分";
  return "时效未知";
}

function fundProfileRow(label, value) {
  return `
    <div class="fund-profile-row">
      <dt>${escapeHtml(label)}</dt>
      <dd>${escapeHtml(value || "-")}</dd>
    </div>
  `;
}

function fundEmptyState(message) {
  return `
    <div class="fund-empty">
      <i data-lucide="circle-dashed"></i>
      <span>${escapeHtml(message)}</span>
    </div>
  `;
}

function formatFundRole(role) {
  const labels = {
    broad_market_index: "宽基指数",
    industry_theme: "行业主题",
    bond_fund: "债券基金",
    general_fund: "综合基金",
  };
  return labels[role] || "基金";
}

function fundRoleIcon(role) {
  const icons = {
    broad_market_index: "chart-no-axes-combined",
    industry_theme: "target",
    bond_fund: "landmark",
    general_fund: "line-chart",
  };
  return icons[role] || "line-chart";
}

function fundGrowthClass(value) {
  if (value > 0) return "positive";
  if (value < 0) return "negative";
  return "neutral";
}

function formatSignedPercent(value) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "-";
  const number = Number(value);
  const sign = number > 0 ? "+" : "";
  return `${sign}${number.toFixed(2)}%`;
}

function formatNav(value) {
  const number = Number(value);
  return Number.isFinite(number) ? number.toFixed(4) : "-";
}

function formatDateOnly(value) {
  const date = new Date(`${value}T00:00:00`);
  if (Number.isNaN(date.getTime())) return String(value || "-");
  return new Intl.DateTimeFormat("zh-CN", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).format(date);
}

function formatDateTime(value) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value || "-");
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(date);
}

function formatDurationMs(value) {
  const milliseconds = Number(value || 0);
  if (!Number.isFinite(milliseconds) || milliseconds <= 0) return "-";
  if (milliseconds < 1000) return `${Math.round(milliseconds)}ms`;
  return `${(milliseconds / 1000).toFixed(1)}s`;
}

function suggestionButton(icon, text) {
  return `
    <button class="suggestion-button" type="button" data-suggestion="${escapeAttribute(text)}">
      <i data-lucide="${icon}"></i><span>${escapeHtml(text)}</span>
    </button>
  `;
}

function thinkingMessage() {
  if (state.streamedAnswer) {
    return `
      <article class="message-row assistant streaming-message">
        <span class="message-avatar"><i data-lucide="command"></i></span>
        <div class="message-body">
          <div class="message-content">${formatMessageContent(state.streamedAnswer)}</div>
          <div class="stream-phase"><i data-lucide="loader-circle" class="spinning"></i><span>${escapeHtml(state.agentPhase || "正在输出回答")}</span></div>
        </div>
      </article>
    `;
  }
  return `
    <article class="message-row assistant">
      <span class="message-avatar"><i data-lucide="command"></i></span>
      <div class="message-body">
        <div class="thinking-row" aria-label="${escapeAttribute(state.agentPhase || "Agent 正在思考")}">
          <span class="thinking-dot"></span><span class="thinking-dot"></span><span class="thinking-dot"></span>
          <span class="thinking-label">${escapeHtml(state.agentPhase || "Agent 正在思考")}</span>
        </div>
      </div>
    </article>
  `;
}

function emptyPanel(icon, title, detail, extraClass = "") {
  return `
    <div class="empty-panel ${extraClass}">
      <i data-lucide="${icon}"></i>
      <strong>${escapeHtml(title)}</strong>
      ${detail ? `<span>${escapeHtml(detail)}</span>` : ""}
    </div>
  `;
}

function sectionHeading(title, count) {
  return `<div class="section-heading"><h3>${escapeHtml(title)}</h3><span>${escapeHtml(count)}</span></div>`;
}

function summaryStat(label, value) {
  return `<div class="summary-stat"><span>${escapeHtml(label)}</span><strong title="${escapeAttribute(value)}">${escapeHtml(value)}</strong></div>`;
}

function keyValue(label, value) {
  return `<div class="key-value"><span>${escapeHtml(label)}</span><strong title="${escapeAttribute(value)}">${escapeHtml(value)}</strong></div>`;
}

function resourceRow(icon, label, count, description) {
  return `
    <div class="resource-row">
      <span class="resource-icon"><i data-lucide="${icon}"></i></span>
      <div class="resource-copy"><strong>${escapeHtml(label)}</strong><span>${escapeHtml(description)}</span></div>
      <span class="resource-count">${count}</span>
    </div>
  `;
}

function compactMetric(icon, label, value, description) {
  const statusClass = value === "失败" ? "error" : "";
  return `
    <div class="compact-row">
      <span class="resource-icon"><i data-lucide="${icon}"></i></span>
      <div class="compact-copy"><strong>${escapeHtml(label)}</strong><span>${escapeHtml(description)}</span></div>
      <span class="status-badge ${statusClass}">${escapeHtml(String(value))}</span>
    </div>
  `;
}

function formatMessageContent(content) {
  const source = String(content || "");
  if (window.marked?.parse && window.DOMPurify?.sanitize) {
    const rendered = window.marked.parse(source, {
      gfm: true,
      breaks: true,
    });
    return window.DOMPurify.sanitize(rendered, {
      USE_PROFILES: { html: true },
      FORBID_TAGS: ["style", "iframe", "form", "input", "button"],
      ADD_ATTR: ["target", "rel"],
    });
  }
  // CDN 暂时不可用时仍保持安全可读，不把未经清洗的模型 HTML 放入页面。
  return formatPlainText(source) || "<p></p>";
}

function formatUserMessageContent(content) {
  return `<p>${escapeHtml(String(content || "")).replace(/\n/g, "<br>")}</p>`;
}

function formatPlainText(text) {
  if (!text) return "";
  return text
    .split(/\n{2,}/)
    .map((paragraph) => {
      let safe = escapeHtml(paragraph);
      safe = safe.replace(/`([^`]+)`/g, "<code>$1</code>");
      safe = safe.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
      return `<p>${safe.replace(/\n/g, "<br>")}</p>`;
    })
    .join("");
}

function safeExternalUrl(value) {
  if (!value) return "";
  try {
    const url = new URL(String(value), window.location.origin);
    return ["http:", "https:"].includes(url.protocol) ? url.href : "";
  } catch (_) {
    return "";
  }
}

function translatePipelineStep(step) {
  const translations = [
    [/^Create agent run /, "创建 Agent 运行 "],
    [/^Receive user question$/, "接收用户请求"],
    [/^Load (\d+) persisted conversation message\(s\)$/, "加载 $1 条会话历史"],
    [/^Assemble system prompt from (\d+) sections, total_chars=(\d+)$/, "组装系统提示词：$1 个区段，共 $2 字符"],
    [/^Bind tools to chat model$/, "向模型绑定可用工具"],
    [/^Tool pool contains (\d+) MCP tool\(s\)$/, "载入 $1 个 MCP 动态工具"],
    [/^Start agent loop, max_tool_rounds=(\d+)$/, "启动 Agent Loop，最多 $1 轮"],
    [/^Round (\d+): model requested (\d+) tool call\(s\)$/, "第 $1 轮：模型请求 $2 次工具调用"],
    [/^Round (\d+): tool results appended to messages$/, "第 $1 轮：工具结果已回填上下文"],
    [/^Round (\d+): model answered without tools$/, "第 $1 轮：模型生成最终回答"],
    [/^Round (\d+): context compacted (\d+) tool message\(s\)$/, "第 $1 轮：压缩 $2 条工具消息"],
    [/^MCP connection changed:/, "MCP 连接变化："],
    [/^Reached max_tool_rounds,/, "达到工具轮次上限，"],
  ];

  let translated = String(step || "");
  for (const [pattern, replacement] of translations) {
    if (pattern.test(translated)) return translated.replace(pattern, replacement);
  }
  if (translated.includes("recovery action=")) return `错误恢复：${translated}`;
  return translated;
}

function stopReasonLabel(reason) {
  const labels = {
    model_finished: "正常完成",
    max_tool_rounds_reached: "达到轮次上限",
  };
  return labels[reason] || reason || "未知";
}

function statusLabel(status) {
  const labels = {
    pending: "待处理",
    queued: "排队中",
    running: "运行中",
    waiting_input: "等待人工输入",
    in_progress: "进行中",
    working: "工作中",
    completed: "已完成",
    failed: "失败",
    cancelled: "已取消",
  };
  return labels[status] || status;
}

function formatConversationTime(value) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "刚刚";
  const now = new Date();
  if (date.toDateString() === now.toDateString()) {
    return new Intl.DateTimeFormat("zh-CN", {
      hour: "2-digit",
      minute: "2-digit",
      hour12: false,
    }).format(date);
  }
  return new Intl.DateTimeFormat("zh-CN", { month: "numeric", day: "numeric" }).format(date);
}

function formatMessageTime(value) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "刚刚";
  return new Intl.DateTimeFormat("zh-CN", {
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(date);
}

function formatBytes(bytes) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function formatJson(value) {
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}

function truncate(value, maxLength) {
  return value.length <= maxLength ? value : `${value.slice(0, maxLength)}\n...`;
}

function conversationStorageKey() {
  return `agent-workspace-conversation:${state.userId}`;
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function escapeAttribute(value) {
  return escapeHtml(value).replaceAll("`", "&#096;");
}

function refreshIcons() {
  if (window.lucide) {
    window.lucide.createIcons({ attrs: { "stroke-width": 1.8 } });
  }
}
