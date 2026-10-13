// 导航与组合页面的回归测试：只运行本地 JS，不访问数据库、平台或模型。
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const pages = require('../../app/static/workspace-pages.js');

function appContext({ hash = '', storage = {} } = {}) {
  // 最小 DOM 替身只验证页面编排；真正的可见性与布局另在浏览器验证。
  const panels = Object.fromEntries(Object.entries(pages.config).map(([view, config]) => [
    view, config.items.map(item => ({ hidden: false, dataset: { workspacePage: item.id } })),
  ]));
  const nodes = new Map();
  function node(selector) {
    if (!nodes.has(selector)) nodes.set(selector, {
      dataset: {}, innerHTML: '', hidden: false, textContent: '', scrollTop: 0,
      classList: { toggle() {}, remove() {} },
      querySelector() { return null; }, querySelectorAll() { return []; },
      contains() { return false; }, setAttribute() {}, removeAttribute() {},
    });
    return nodes.get(selector);
  }
  const history = [];
  const location = { hash, pathname: '/' };
  const context = vm.createContext({
    console, URLSearchParams,
    localStorage: { getItem: key => storage[key] ?? null, setItem: (key, value) => { storage[key] = value; }, removeItem: key => { delete storage[key]; } },
    document: {
      body: node('body'), activeElement: null, querySelector: node,
      querySelectorAll: selector => {
        const view = /^#workspace-(portfolio|market) \[data-workspace-page\]$/.exec(selector)?.[1];
        return view ? panels[view] : [];
      },
    },
    window: {
      FundScopeWorkspacePages: pages, location, addEventListener() {}, requestAnimationFrame: cb => cb(),
      FundScopePortfolioReview: require('../../app/static/portfolio-review.js'),
      FundScopeBookkeeping: require('../../app/static/portfolio-bookkeeping.js'),
      FundScopeDailyBriefings: require('../../app/static/daily-briefings.js'),
      FundScopePortfolioDiagnosis: require('../../app/static/portfolio-diagnosis.js'),
      FundScopeResearchCommittee: require('../../app/static/research-committee.js'),
      FundScopeHoldingsQuotes: require('../../app/static/holdings-quotes.js'),
      FundScopeDataVisuals: require('../../app/static/data-visuals.js'),
      FundScopeMarketIntraday: require('../../app/static/market-intraday.js'),
      FundScopeTurnoverChart: require('../../app/static/market-turnover-chart.js'),
      // DOM 生命周期由 workspace-page-host.test.cjs 测试；这里仅观察导航交给 host 的选页。
      FundScopeWorkspacePageHost: { create: () => ({update: ({activePage}) => {
        panels.portfolio.forEach(panel => { panel.hidden = panel.dataset.workspacePage !== activePage; });
      }}) },
      history: Object.fromEntries(['pushState', 'replaceState'].map(method => [method, (_state, _unused, hash) => {
        history.push({ method, hash }); location.hash = hash;
      }])),
    },
    fetch() { throw new Error('Navigation must not submit or collect data'); },
  });
  vm.runInContext(fs.readFileSync(path.join(__dirname, '../../app/static/app.js'), 'utf8'), context);
  return { run: code => vm.runInContext(code, context), panels, nodes, history, location };
}

test('all function routes round-trip with unique IDs', () => {
  for (const [view, config] of Object.entries(pages.config)) {
    assert.equal(new Set(config.items.map(item => item.id)).size, config.items.length);
    for (const { id } of config.items) assert.deepEqual(pages.readRoute(pages.routeHash(view, id)), { view, page: id });
  }
});

test('late bookkeeping refresh cannot overwrite the next selected portfolio',async()=>{
  const app=appContext();
  app.run(`state.userId='chen';state.selectedPortfolioId='first';state.portfolioDetail={id:'first'};
    globalThis.resolveRead=null; apiRequest=()=>new Promise(resolve=>{resolveRead=resolve;});`);
  const pending=app.run(`refreshPortfolioBookkeeping('chen','first')`);
  app.run(`state.selectedPortfolioId='second';state.portfolioDetail={id:'second'};resolveRead({id:'first',holdings:[]});`);
  await pending;
  assert.equal(app.run('state.portfolioDetail.id'),'second');
});

test('desktop and mobile groups cover each destination exactly once', () => {
  for (const config of Object.values(pages.config)) {
    const ids = config.groups.flatMap(group => group.pages);
    assert.equal(new Set(ids).size, config.items.length);
    assert.deepEqual(ids, config.items.map(item => item.id));
  }
});

test('portfolio page reads are isolated, reused on return, and refreshable explicitly', async () => {
  const app = appContext();
  app.run(`state.selectedPortfolioId = 'fixture'; globalThis.reads = [];
    loadPortfolioAssessment = async () => reads.push('risk');
    loadPortfolioPerformance = async () => reads.push('performance');
    loadPortfolioResearchHistory = async () => reads.push('research');`);
  await app.run('loadPortfolioPage("risk")');
  await app.run('loadPortfolioPage("risk")');
  assert.equal(app.run('reads.join(",")'), 'risk');
  await app.run('loadPortfolioPage("performance")');
  await app.run('loadPortfolioPage("risk", {refresh:true})');
  assert.equal(app.run('reads.join(",")'), 'risk,performance,risk');
  await app.run('loadPortfolioPage("optimizer"); loadPortfolioPage("stress")');
  assert.equal(app.run('reads.length'), 3);
});

test('market navigation loads only the selected page and no community data in news', async () => {
  const app = appContext();
  app.run(`state.activeView = 'market'; state.workspacePages.market = 'news'; globalThis.reads = [];
    loadMarketEvents = async () => reads.push('news');
    loadMarketTopicCatalog = async () => reads.push('topics');
    loadSocialSignals = async () => {throw new Error('unrelated request');};
    loadMarketHotspots = async () => {throw new Error('unrelated request');};`);
  await app.run('loadActiveWorkspacePage()');
  assert.equal(app.run('reads.join(",")'), 'news,topics');
});

test('market environment reads daily and intraday independently without POST', async () => {
  const app = appContext();
  app.run(`state.activeView='market'; state.workspacePages.market='status'; globalThis.reads=[];
    loadMarketSentiment=async()=>reads.push('daily'); refreshIcons=()=>{};
    window.FundScopeMarketIntraday={create:({turnover})=>({load:async()=>reads.push(turnover?'turnover-get':'intraday-get')})};
    window.FundScopeTurnoverChart={create:()=>({load:async()=>reads.push('history-get')})};`);
  await app.run('loadActiveWorkspacePage()');
  assert.equal(app.run('reads.join(",")'), 'daily,intraday-get,turnover-get,history-get');
});

test('portfolio periods do not change each other or query the wrong time range', async () => {
  const app = appContext();
  app.run(`state.selectedPortfolioId = 'fixture'; state.portfolioPeriods.performance = 36;
    globalThis.urls = []; apiRequest = async url => { urls.push(url); return {}; };
    renderPortfolioWorkbench = () => {};`);
  await app.run('loadPortfolioAssessment(); loadPortfolioPerformance()');
  assert.equal(app.run('state.portfolioPeriods.risk'), 12);
  assert.equal(app.run('state.portfolioPeriods.research'), 12);
  assert.equal(app.run('urls.length'), 2);
  assert.ok(app.run('urls[0].includes("months=12")'));
  assert.ok(app.run('urls[1].includes("months=36")'));
});

test('legacy evidence loaders remain read-only; fund detail no longer duplicates their tabs', async () => {
  const app = appContext();
  app.run(`globalThis.reads = [];
    loadLargeCapitalSignals = async () => reads.push('capital');
    loadMarketHotspots = async () => reads.push('hotspots');
    loadSocialSignals = async () => reads.push('community');`);
  await app.run('loadTerminalEvidenceTab("capital")');
  assert.equal(app.run('reads.join(",")'), 'capital');
  await app.run('loadTerminalEvidenceTab("hotspots"); loadTerminalEvidenceTab("community")');
  assert.equal(app.run('reads.join(",")'), 'capital,hotspots,community');
  const terminal = fs.readFileSync(path.join(__dirname, '../../app/static/terminal.js'), 'utf8');
  assert.doesNotMatch(terminal, /data-research-tab="/);
});

test('starting another unsaved portfolio uses a new page-tree key', () => {
  const app = appContext();
  app.run(`globalThis.keys = []; renderAgentFundContext = () => {};
    window.FundScopeWorkspacePageHost = {create: () => ({update: value => keys.push(value.key)})};
    startNewPortfolioDraft({render:false}); renderPortfolioWorkbench();
    state.portfolioDraft.name = "不应留下的旧名称";
    startNewPortfolioDraft({render:false}); renderPortfolioWorkbench();`);
  assert.notEqual(app.run('keys[0]'), app.run('keys[1]'));
  assert.equal(app.run('state.portfolioDraft.name'), '我的基金组合');
});

test('decision submission uses the displayed source even if research changes in another page', async () => {
  const app = appContext();
  app.run(`state.selectedPortfolioId = 'fixture'; state.portfolioResearchResult = {};
    state.portfolioResearchJob = {id:'report-displayed'};`);
  const html = app.run('renderPortfolioDecisions()');
  assert.match(html, /name="research_job_id" value="report-displayed"/);
  assert.match(html, /将同时冻结研究报告：report-displayed/);
  app.run(`state.portfolioResearchJob = {id:'report-loaded-later'};
    globalThis.FormData = class { constructor(form) {this.form = form;} get(key) {return this.form[key];} };
    globalThis.savedPayload = null;
    apiRequest = async (url, options) => {savedPayload = JSON.parse(options.body); return {};};
    loadPortfolioDecisions = async () => {}; renderPortfolioWorkbench = () => {}; showToast = () => {};`);
  await app.run(`createPortfolioDecision({preventDefault(){}, target:{research_job_id:'report-displayed', action:'hold', thesis:'保留原证据', horizon_days:90}})`);
  assert.equal(app.run('savedPayload.research_job_id'), 'report-displayed');
  await app.run(`createPortfolioDecision({preventDefault(){}, target:{research_job_id:'', action:'hold', thesis:'仅冻结指标', horizon_days:90}})`);
  assert.equal(app.run('savedPayload.research_job_id'), null);
});

test('retail initial read failure replaces loading and offers read-only retry', async () => {
  const app = appContext({hash:'#market/retail'});
  await app.run('apiRequest = async () => { throw new Error("读取失败"); }; refreshIcons = () => {}; loadRetailIndices()');
  const html = app.nodes.get('#retail-indices').innerHTML;
  assert.match(html, /读取失败/);
  assert.doesNotMatch(html, /正在读取/);
  assert.match(html, /id="retry-retail-indices"/);
  assert.match(html, /重新读取已保存的指数/);
  assert.equal(app.run('state.retailIndicesLoading'), false);
});

test('invalid and legacy URLs cannot select arbitrary panels', () => {
  assert.equal(pages.readRoute('#<script>'), null);
  assert.deepEqual(pages.readRoute('#market/missing'), { view: 'market', page: 'status' });
  assert.deepEqual(pages.readRoute('#portfolio'), { view: 'portfolio', page: 'holdings' });
  assert.deepEqual(pages.readRoute('#agent'), { view: 'agent', page: null });
  assert.deepEqual(pages.readRoute('#overview/holdings'), {view:'overview',page:'holdings'});
  assert.deepEqual(pages.readRoute('#portfolio/exposure'), {view:'portfolio',page:'risk'});
  assert.equal(pages.config.portfolio.items.some(item=>item.id==='exposure'),false);
});

test('single fund holdings is an exclusive page and its read does not load market or portfolio',async()=>{
  const app=appContext({hash:'#overview/holdings'});
  app.run(`globalThis.reads=[];loadFundHoldings=async()=>reads.push('holdings');
    loadMarketSentiment=()=>{throw new Error('unexpected market read');};
    loadPortfolioPage=()=>{throw new Error('unexpected portfolio read');};
    applyWorkspacePages();`);
  assert.equal(app.nodes.get('#fund-holdings-page').hidden,false);
  assert.equal(app.nodes.get('#overview-dashboard').hidden,true);
  assert.equal(app.nodes.get('#fund-report').hidden,true);
  await app.run('loadActiveWorkspacePage()');
  assert.equal(app.run('reads.join(",")'),'holdings');
  app.run('state.workspacePages.overview="terminal";applyWorkspacePages()');
  assert.equal(app.nodes.get('#fund-holdings-page').hidden,true);
  assert.equal(app.nodes.get('#overview-dashboard').hidden,false);
});

test('deep link wins over stored selections and stale storage falls back', () => {
  const app = appContext({ hash: '#market/news', storage: { 'fundscope-product-view': 'portfolio', 'fundscope-page-market': 'community', 'fundscope-page-portfolio': 'removed' } });
  assert.equal(app.run('state.activeView'), 'market');
  assert.equal(app.run('state.workspacePages.market'), 'news');
  assert.equal(app.run('state.workspacePages.portfolio'), 'holdings');
});

test('blank entry opens asset holdings even if a previous visit stored a research tool', () => {
  const app = appContext({storage: {'fundscope-product-view': 'agent', 'fundscope-page-portfolio': 'optimizer'}});
  assert.equal(app.run('state.activeView'), 'portfolio');
  assert.equal(app.run('state.workspacePages.portfolio'), 'holdings');
});

test('legacy allocation shows missing positions, with a separate quantity conversion form', () => {
  const app = appContext();
  app.run(`state.selectedPortfolioId = 'allocation';
    state.portfolioDetail = {name:'示例配置', position_mode:'allocation', holding_count:1, holdings:[{fund_code:'000001',weight_pct:100}]};
    state.portfolioDraft = portfolioDraftFromDetail(state.portfolioDetail);
    state.funds = [{fund_code:'000001',short_name:'示例基金',return_30d_pct:1.2,return_3m_pct:-2,latest_nav_date:'2026-09-04'}];`);
  const summary = app.run('renderPortfolioPage("holdings")');
  assert.match(summary, /待补录持仓表/);
  assert.match(summary, /href="#assets\/configuration"/);
  assert.match(summary, /data-research-fund="000001"/);
  assert.match(summary, /未记录真实份额与成本/);
  assert.doesNotMatch(summary, /data-portfolio-form|data-portfolio-weight|data-save-portfolio/);
  const form = app.run('renderPortfolioPage("configuration")');
  assert.match(form, /data-position-units|data-position-cost/);
  assert.match(form, /确认并转为真实持仓/);
  assert.doesNotMatch(form, /data-portfolio-weight|权重合计/);
  assert.equal(app.run('state.portfolioDraft.holdings["000001"].units'), '');
  assert.equal(app.run('state.portfolioDraft.holdings["000001"].cost_amount'), '');
  assert.doesNotMatch(app.run('renderPortfolioPage("transactions")'), /data-portfolio-transaction-form/);
});

test('every selected fund requires complete quantities and cost, with no silent omissions', () => {
  const app = appContext();
  app.run(`state.portfolioDraft = {mode:'actual',name:'实际持仓',holdings:{
    '110020':{units:123.4,cost_amount:987.65,purchase_date:'2025-01-02'},
    '008888':{units:'',cost_amount:'',purchase_date:''}
  }};`);
  assert.equal(app.run('portfolioDraftSummary().invalidCount'), 1);
  assert.equal(app.run('portfolioDraftSummary().totalCost'), 987.65);
  app.run(`state.portfolioDraft.holdings['008888'] = {units:200,cost_amount:300,purchase_date:'2025-01-02'};`);
  assert.equal(app.run('portfolioDraftSummary().invalidCount'), 0);
  assert.equal(app.run('portfolioDraftSummary().totalCost'), 1287.65);
  for (const invalid of ['0', '-1', 'Infinity']) {
    app.run(`state.portfolioDraft.holdings['008888'].units = '${invalid}';`);
    assert.equal(app.run('portfolioDraftSummary().invalidCount'), 1);
  }
});

test('quantity draft keeps uncatalogued rows and offers newly followed funds without weights', () => {
  const app = appContext();
  app.run(`state.funds = [{fund_code:'008888',short_name:'华夏半导体'}];
    state.portfolioDraft = {mode:'actual',name:'迁移',holdings:{'110020':{units:'',cost_amount:'',purchase_date:''}}};`);
  const html = app.run('renderActualPortfolioDraft(state.portfolioDraft)');
  assert.match(html, /data-position-row="110020"/);
  assert.match(html, /option value="008888"/);
  assert.match(html, /华夏半导体/);
  assert.doesNotMatch(html, /data-portfolio-weight/);
});

test('adding a fund refreshes the active conversion page and retains entered quantities', () => {
  const app = appContext();
  app.run(`state.workspacePages.portfolio = 'configuration';
    state.portfolioDraft = {mode:'actual',name:'补录',holdings:{'110020':{units:12,cost_amount:30,purchase_date:'2025-01-02'}}};
    globalThis.changed = [];
    activePortfolioDraftForm = () => ({querySelector: selector => selector === '[data-position-fund-picker]' ? {value:'008888'} : null});
    renderPortfolioWorkbench = ({pages}) => changed.push(pages.join(','));
    addFundToPortfolioDraft();`);
  assert.equal(app.run('changed[0]'), 'configuration');
  assert.equal(app.run('state.portfolioDraft.holdings["110020"].units'), 12);
  assert.equal(app.run('state.portfolioDraft.holdings["008888"].units'), '');
  app.run('removeFundFromPortfolioDraft("008888")');
  assert.equal(app.run('changed[1]'), 'configuration');
});

test('draft reads only the current page instead of a hidden form', () => {
  const app = appContext();
  app.run(`state.workspacePages.portfolio = 'configuration';
    globalThis.selectorUsed = '';
    elements.portfolioWorkbench.querySelector = selector => {selectorUsed = selector; return null;};
    activePortfolioDraftForm();`);
  assert.equal(app.run('selectorUsed'), '#portfolio-page-configuration [data-portfolio-form]');
});

test('an incomplete row prevents submission instead of silently excluding that fund', async () => {
  const app = appContext();
  app.run(`state.portfolioDraft = {mode:'actual',name:'实盘',holdings:{
    '110020':{units:100,cost_amount:200,purchase_date:'2025-01-02'},
    '008888':{units:'',cost_amount:'',purchase_date:''}
  }}; globalThis.notices=[]; showToast = message => notices.push(message);
    apiRequest = async () => {throw new Error('Must not submit incomplete positions');};`);
  await app.run('savePortfolio({preventDefault(){}})');
  assert.match(app.run('notices[0]'), /每只基金/);
  assert.equal(app.run('state.portfolioDraft.holdings["008888"].units'), '');
});

test('save converts using the same portfolio ID and actual fields only, then opens holdings', async () => {
  const app = appContext();
  app.run(`state.selectedPortfolioId = 'legacy'; state.userId = 'chen';
    state.portfolioDraft = {mode:'actual',name:'实盘',holdings:{'008888':{units:'321.1234',cost_amount:'456.78',purchase_date:'2025-01-02'}}};
    renderPortfolioWorkbench = () => {}; showToast = () => {};
    loadPortfolios = async () => {}; activateProductView = (view, options) => {globalThis.openedPage = options.page;};
    apiRequest = async (url, options) => {globalThis.saved = {url,...options}; return {id:'legacy'};};`);
  await app.run('savePortfolio({preventDefault(){}})');
  assert.equal(app.run('saved.method'), 'PUT');
  assert.match(app.run('saved.url'), /\/legacy$/);
  assert.deepEqual(JSON.parse(app.run('saved.body')).holdings, [
    {fund_code:'008888',units:321.1234,cost_amount:456.78,purchase_date:'2025-01-02'},
  ]);
  assert.equal(app.run('openedPage'), 'holdings');
});

test('failed conversion retains the quantities and original portfolio for retry', async () => {
  const app = appContext();
  app.run(`state.selectedPortfolioId = 'legacy'; state.portfolioDetail = {position_mode:'allocation'};
    state.portfolioDraft = {mode:'actual',name:'实盘',holdings:{'008888':{units:100,cost_amount:150,purchase_date:'2025-01-02'}}};
    renderPortfolioWorkbench = () => {}; showToast = () => {};
    apiRequest = async () => {throw new Error('保存失败');};`);
  await app.run('savePortfolio({preventDefault(){}})');
  assert.equal(app.run('state.portfolioDetail.position_mode'), 'allocation');
  assert.equal(app.run('state.portfolioDraft.holdings["008888"].units'), 100);
  assert.equal(app.run('state.portfolioSaving'), false);
  assert.equal(app.run('state.portfolioError'), '保存失败');
});

test('holding and portfolio show cost and distinct cumulative, realized and floating PnL', () => {
  const app = appContext();
  const holding = {fund_code:'008888',short_name:'华夏半导体',units:100,cost_amount:150,
    current_value:200,unrealized_pnl_amount:50,realized_pnl_amount:-20,total_pnl_amount:30,
    unrealized_pnl_pct:33.33,latest_nav:2,latest_nav_date:'2026-09-08',average_cost_nav:1.5};
  const html = app.run(`renderActualHolding(${JSON.stringify(holding)})`);
  assert.match(html, /累计盈亏/); assert.match(html, /持有份额/);
  assert.match(html, /持仓成本/); assert.match(html, /浮动盈亏/); assert.match(html, /已实现盈亏/);
  assert.match(html, /30\.00/); assert.match(html, /50\.00/); assert.match(html, /-.*20\.00/);
  const summary = app.run(`renderActualPortfolioLedger({holdings:[],total_pnl_amount:30,realized_pnl_amount:-20})`);
  assert.match(summary, /累计收益率/); assert.match(summary, /已实现盈亏/);
  assert.match(summary, /未录入的历史卖出、分红/);
});

test('actual holdings and bookkeeping do not mount each other and cannot edit target weights', () => {
  const app = appContext();
  app.run(`state.selectedPortfolioId = 'actual';
    state.workspacePages.portfolio = 'holdings';
    state.portfolioDetail = {name:'真实账本',position_mode:'actual',holding_count:0,holdings:[]};
    state.portfolioDraft = {mode:'actual',name:'真实账本',holdings:{}};`);
  const holdings = app.run('renderPortfolioPage("holdings")');
  const transactions = app.run('renderPortfolioPage("transactions")');
  // 主操作移动到组合选择旁，页面仍提供交易入口，但不重复挂载交易表单。
  const header = app.run('renderPortfolioHeader()');
  assert.match(header, /href="#assets\/transactions"/);
  assert.match(header, /data-analyze-portfolio/);
  assert.doesNotMatch(holdings, /data-analyze-portfolio/);
  assert.doesNotMatch(holdings, /transaction-entry-section|transaction-history-section/);
  assert.match(transactions, /transaction-entry-section/);
  assert.doesNotMatch(transactions, /portfolio-value-summary|actual-position-section/);
  assert.doesNotMatch(app.run('renderPortfolioPage("configuration")'), /data-portfolio-form|data-portfolio-weight/);
  app.run('state.workspacePages.portfolio = "research";');
  assert.doesNotMatch(app.run('renderPortfolioHeader()'), /data-analyze-portfolio/);
});

test('configuration mount initializes totals without submitting a save', () => {
  const app = appContext();
  app.run(`globalThis.totalsUpdated = 0; refreshIcons = () => {};
    updatePortfolioDraftStatus = () => totalsUpdated++;
    window.FundScopeWorkspacePageHost = {create: options => {
      globalThis.pageLifecycle = options; return {update(){}};
    }};
    renderPortfolioWorkbench(); pageLifecycle.afterRender('configuration');`);
  assert.equal(app.run('totalsUpdated'), 1);
});

test('post evidence from split pages shares open, pagination and close handlers', () => {
  const app = appContext();
  app.run(`globalThis.opened = []; globalThis.pagesRead = 0; globalThis.closed = 0;
    openSocialPost = id => opened.push(id); loadSocialPosts = () => pagesRead++;
    document.querySelector('.fund-content').addEventListener = (_type, handler) => {globalThis.evidenceClick = handler;};
    document.querySelector('#social-post-detail').close = () => closed++;
    bindSocialEvidenceActions();
    evidenceClick({target:{closest: selector => selector === '[data-social-post]' ? {dataset:{socialPost:'stable-post'}} : null}});
    evidenceClick({target:{closest: selector => selector === '[data-social-page]' ? {dataset:{socialPage:'2'}} : null}});
    evidenceClick({target:{closest: selector => selector === '[data-social-close]' ? {} : null}});`);
  assert.equal(app.run('opened.join(",")'), 'stable-post');
  assert.equal(app.run('state.socialPostPage'), 2);
  assert.equal(app.run('pagesRead'), 1);
  assert.equal(app.run('closed'), 1);
});

function quoteRefreshFixture() {
  const app = appContext();
  app.run(`state.userId = 'chen'; state.selectedPortfolioId = 'portfolio_test';
    state.portfolios = [{id:'portfolio_test',name:'原组合'}];
    state.portfolioDetail = state.portfolios[0];
    state.portfolioDraft = {mode:'allocation',name:'未提交的名称',holdings:{'000001':100}};
    state.agentContextMode = 'market'; globalThis.calls = []; globalThis.notices = [];
    renderPortfolioWorkbench = () => {}; renderFundNavigation = () => {}; renderFundCards = () => {};
    setFundSyncButtonBusy = () => {}; showToast = message => notices.push(message);
    apiRequest = async (url, options) => {
      calls.push({url,options});
      if (options?.method === 'POST') return {job:{id:'test-job'}};
      if (url === endpoints.funds) return {funds:[{fund_code:'000001',daily_growth_rate_pct:1.5,latest_nav_date:'2026-09-08'}]};
      return {id:'portfolio_test',name:'原组合',total_market_value:1500};
    };
    waitForBackgroundJob = async (id,user) => {calls.push({job:id,user});return {status:'completed',result:{summary:'已同步1只基金'}};};`);
  return app;
}

test('holdings reread performs only GET, updates valuations, and keeps research context and draft', async () => {
  const app = quoteRefreshFixture();
  await app.run('refreshHoldingsQuotes()');
  assert.equal(app.run('calls.length'), 2);
  assert.equal(app.run('calls.some(call => call.options?.method === "POST")'), false);
  assert.equal(app.run('state.portfolioDetail.total_market_value'), 1500);
  assert.equal(app.run('state.funds[0].daily_growth_rate_pct'), 1.5);
  assert.equal(app.run('state.portfolioDraft.name'), '未提交的名称');
  assert.equal(app.run('state.agentContextMode'), 'market');
});

test('explicit holdings sync waits for durable job then rereads, blocking duplicate clicks', async () => {
  const app = quoteRefreshFixture();
  app.run('globalThis.run = refreshHoldingsQuotes({sync:true}); refreshHoldingsQuotes({sync:true})');
  await app.run('run');
  assert.equal(app.run('calls.length'), 4);
  assert.equal(app.run('calls[0].options.method'), 'POST');
  assert.equal(app.run('JSON.parse(calls[0].options.body).user_id'), 'chen');
  assert.equal(app.run('calls[1].job'), 'test-job');
  assert.equal(app.run('state.fundSyncing'), false);
  assert.equal(app.run('state.holdingsQuoteStatus.busy'), false);
});

test('actual holdings reread replaces the old curve without submitting trades or model work', async () => {
  const app = quoteRefreshFixture();
  app.run(`state.portfolioPerformance = {twr_pct:999};
    const originalRequest = apiRequest;
    apiRequest = async (url,options) => {
      if (url.includes('/performance?')) {
        calls.push({url,options});
        if (state.portfolioPerformance !== null) throw new Error('stale curve retained');
        return {twr_pct:2,series:[]};
      }
      const result = await originalRequest(url,options);
      if (result.id === 'portfolio_test') result.position_mode = 'actual';
      return result;
    };`);
  await app.run('refreshHoldingsQuotes()');
  assert.equal(app.run('calls.length'),3);
  assert.equal(app.run('state.portfolioPerformance.twr_pct'),2);
  assert.equal(app.run('calls.some(call => call.options?.method === "POST")'),false);
  assert.equal(app.run('state.portfolioDraft.name'),'未提交的名称');
});

test('a curve read failure does not discard newly read holdings or pretend the old curve is current', async () => {
  const app = quoteRefreshFixture();
  app.run(`state.portfolioPerformance = {twr_pct:999};
    const originalRequest = apiRequest;
    apiRequest = async (url,options) => {
      if (url.includes('/performance?')) throw new Error('curve unavailable');
      const result = await originalRequest(url,options);
      if (result.id === 'portfolio_test') result.position_mode = 'actual';
      return result;
    };`);
  await app.run('refreshHoldingsQuotes()');
  assert.equal(app.run('state.portfolioDetail.total_market_value'),1500);
  assert.equal(app.run('state.portfolioPerformance'),null);
  assert.equal(app.run('state.portfolioPerformanceError'),'curve unavailable');
  assert.equal(app.run('portfolioPageReads.has("holdings")'),false);
});

test('failed holdings sync preserves old quotes and can retry', async () => {
  const app = quoteRefreshFixture();
  app.run(`state.funds = [{fund_code:'old'}]; waitForBackgroundJob = async () => ({status:'failed',error:'provider unavailable'});`);
  await app.run('refreshHoldingsQuotes({sync:true})');
  assert.equal(app.run('state.funds[0].fund_code'), 'old');
  assert.match(app.run('state.holdingsQuoteStatus.message'), /provider unavailable/);
  assert.equal(app.run('state.holdingsQuoteStatus.busy'), false);
  await app.run('refreshHoldingsQuotes()');
  assert.equal(app.run('state.funds[0].fund_code'), '000001');
});

test('partial sync keeps an explicit warning instead of reporting all quotes updated', async () => {
  const app = quoteRefreshFixture();
  app.run(`waitForBackgroundJob = async () => ({status:'completed',result:{summary:'1/2只已同步',failed_funds:1}});`);
  await app.run('refreshHoldingsQuotes({sync:true})');
  assert.equal(app.run('state.holdingsQuoteStatus.error'), true);
  assert.match(app.run('state.holdingsQuoteStatus.message'), /1 只基金同步失败/);
});

test('a late quote response cannot replace another user or portfolio', async () => {
  for (const change of ["state.selectedPortfolioId = 'another'", "state.userId = 'another'"]) {
    const app = quoteRefreshFixture();
    app.run(`globalThis.resolveQuotes = null; apiRequest = url => url === endpoints.funds
      ? new Promise(resolve => {resolveQuotes = resolve;})
      : Promise.resolve({id:'portfolio_test',total_market_value:999});
      globalThis.run = refreshHoldingsQuotes(); ${change}; resolveQuotes({funds:[{fund_code:'late'}]});`);
    await app.run('run');
    assert.equal(app.run('state.portfolioDetail.total_market_value'), undefined);
    assert.equal(app.run('state.funds.length'), 0);
  }
});

test('quote refresh invalidates holdings only, preserving a mounted transaction editor', () => {
  const app = appContext();
  app.run(`globalThis.changes = [];
    window.FundScopeWorkspacePageHost = {create: () => ({update: value => changes.push(value.changedPages)})};
    renderPortfolioWorkbench({pages:['holdings'],includeTransactions:false});
    renderPortfolioWorkbench({pages:['holdings']});`);
  assert.equal(app.run('changes[0].join(",")'), 'holdings');
  assert.equal(app.run('changes[1].join(",")'), 'holdings,transactions');
});

test('actual holding uses the same dated daily quote as the allocation table', () => {
  const app = appContext();
  app.run(`state.funds = [{fund_code:'000001',latest_nav_date:'2026-09-08',daily_growth_rate_pct:-0.5,return_30d_pct:2}];`);
  const html = app.run(`renderActualHolding({fund_code:'000001',short_name:'真实持仓',sample_role:'bond_fund',weight_pct:25,units:100,latest_nav:1.2})`);
  assert.match(html, /最新日涨跌/); assert.match(html, /2026-09-08/);
  assert.match(html, /data-kind="bond_fund"/); assert.match(html, /近30日/);
});

test('one panel per workspace remains visible, including after a data update', () => {
  const app = appContext();
  app.run('state.activeView = "portfolio"; state.workspacePages.portfolio = "research"; state.workspacePages.market = "community"; applyWorkspacePages()');
  assert.deepEqual(app.panels.portfolio.filter(p => !p.hidden).map(p => p.dataset.workspacePage), ['research']);
  assert.deepEqual(app.panels.market.filter(p => !p.hidden).map(p => p.dataset.workspacePage), ['community']);
  app.panels.portfolio.forEach(p => { p.hidden = false; });
  app.run('applyWorkspacePages()');
  assert.equal(app.panels.portfolio.filter(p => !p.hidden).length, 1);
});

test('navigation changes URL, title, and panels without rebuilding forms', () => {
  const app = appContext();
  app.run('elements.portfolioWorkbench.innerHTML = "unsaved form"; activateProductView("portfolio", {page:"performance",load:false})');
  assert.equal(app.location.hash, '#assets/performance');
  assert.equal(app.nodes.get('#workspace-title').textContent, '我的资产');
  assert.equal(app.nodes.get('#portfolio-workbench').innerHTML, 'unsaved form');
  assert.match(app.nodes.get('#workspace-outline-items').innerHTML, /href="#assets\/review"/);
  assert.match(app.nodes.get('#workspace-outline-items').innerHTML, /product-current-tool/);
  app.run('activateProductView("market", {page:"news",load:false}); activateProductView("portfolio", {load:false})');
  assert.equal(app.location.hash, '#assets/performance');
  app.run('activateProductView("market", {page:"status",load:false,history:"replace"})');
  assert.equal(app.history.at(-1).method, 'replaceState');
});

test('loading and unsaved portfolio keep all destinations with a usable empty state', () => {
  const app = appContext();
  app.run('state.portfolioLoading = true');
  let html = app.run('renderPortfolioPage("risk")');
  assert.match(html, /正在读取基金组合/);
  app.run('state.portfolioLoading = false; renderPortfolioEditor = () => "EDITOR"');
  html = app.run('workspaceOutlineConfig.portfolio.items.map(item => renderPortfolioPage(item.id)).join("")');
  assert.match(html, /前往持仓交易/);
  assert.equal((html.match(/data-open-portfolio-page="holdings"/g) || []).length, pages.config.portfolio.items.length - 1);
});

test('risk failure does not suppress independent research and performance', () => {
  const app = appContext();
  app.run(`state.selectedPortfolioId = 'fixture'; state.portfolioAssessmentError = 'risk unavailable';
    renderPortfolioEditor = () => 'EDITOR'; renderPortfolioPerformance = () => 'PERFORMANCE';
    renderPortfolioExposure = () => 'EXPOSURE'; renderPortfolioEventImpacts = () => 'EVENTS';
    renderPortfolioMonitoring = () => 'MONITORING'; renderPortfolioResearch = () => 'RESEARCH';
    renderPortfolioDecisions = () => 'DECISIONS';`);
  const html = app.run('workspaceOutlineConfig.portfolio.items.map(item => renderPortfolioPage(item.id)).join("")');
  assert.match(html, /risk unavailable/);
  for (const text of ['PERFORMANCE', 'RESEARCH', 'DECISIONS']) assert.ok(html.includes(text));
  assert.doesNotMatch(app.run('renderPortfolioAssessment()'), /PERFORMANCE|RESEARCH|DECISIONS/);
});

test('研究提交响应丢失复用幂等键，已受理任务断线后只读同一个Job', async () => {
  const app=appContext();
  app.run(`state.selectedPortfolioId='fixture'; window.confirm=()=>true;
    activateProductView=renderPortfolioWorkbench=showToast=()=>{};
    loadPortfolioResearchHistory=async()=>{}; createIdempotencyKey=()=> 'same-submit-key';
    globalThis.requests=[]; globalThis.mode='lost';
    apiRequest=async (url,options)=>{requests.push({url,options});
      if(mode==='lost')throw new Error('response lost');
      if(url.includes('/research-runs/report-accepted?'))return {run_id:'report-accepted',portfolio_id:'fixture',diagnosis:{version:'portfolio-diagnosis-v1'}};
      return {job:{id:'report-accepted',status:mode==='complete'?'completed':'running',result:{run_id:'report-accepted',portfolio_id:'fixture'}}};};
    waitForBackgroundJobEvents=async()=>{throw new Error('stream disconnected');};`);
  await app.run('runPortfolioResearch()');
  app.run(`mode='accepted'`);await app.run('runPortfolioResearch()');
  assert.equal(app.run(`JSON.parse(requests[0].options.body).idempotency_key`),'same-submit-key');
  assert.equal(app.run(`JSON.parse(requests[1].options.body).idempotency_key`),'same-submit-key');
  app.run(`mode='complete'`);await app.run('runPortfolioResearch()');
  assert.equal(app.run('requests[2].options'),undefined);
  assert.match(app.run('requests[2].url'),/report-accepted/);
  assert.equal(app.run('state.portfolioResearchResult.run_id'),'report-accepted');
  assert.match(app.run('requests[3].url'),/research-runs\/report-accepted\?/);
  assert.equal(app.run('state.portfolioResearchResult.diagnosis.version'),'portfolio-diagnosis-v1');
  assert.equal(app.run('state.portfolioResearchRunning'),false);
});

test('明确选择历史报告后，另一份晚到的报告不能替换它',async()=>{
  const app=appContext();
  app.run(`state.selectedPortfolioId='fixture'; state.userId='alice';
    activateProductView=renderPortfolioWorkbench=showToast=()=>{}; loadPortfolioResearchHistory=async()=>{};
    globalThis.pending={};apiRequest=url=>new Promise(resolve=>pending[url.includes('/old?')?'old':'new']=resolve);`);
  const old=app.run(`openReviewedReport('old')`),fresh=app.run(`openReviewedReport('new')`);
  app.run(`pending.new({run_id:'new',portfolio_id:'fixture',period_months:6})`);await fresh;
  app.run(`pending.old({run_id:'old',portfolio_id:'fixture',period_months:12})`);await old;
  assert.equal(app.run('state.portfolioResearchResult.run_id'),'new');
  assert.equal(app.run('state.portfolioPeriods.research'),6);
  assert.equal(app.run('state.portfolioReportLoading'),false);
});

test('读取历史报告失败只提供重读入口，不把重试变成新研究',async()=>{
  const app=appContext();app.run(`state.selectedPortfolioId='fixture';
    activateProductView=renderPortfolioWorkbench=showToast=()=>{};
    apiRequest=async()=>{throw new Error('网络中断')};`);
  await app.run(`openReviewedReport('old')`);
  const html=app.run('renderPortfolioResearch()');
  assert.match(html,/data-review-report="old"/);assert.doesNotMatch(html,/data-run-portfolio-research/);
});

test('market markup defines every panel exactly once and defaults to status only', () => {
  const html = fs.readFileSync(path.join(__dirname, '../../app/static/index.html'), 'utf8');
  for (const { id } of pages.config.market.items) {
    assert.equal((html.match(new RegExp(`data-workspace-page="${id}"`, 'g')) || []).length, 1);
  }
  assert.ok(html.indexOf('workspace-pages.js') < html.indexOf('app.js'));
});

test('capital sector analysis renders a useful empty state', () => {
  const app = appContext();
  assert.match(app.run('renderCapitalSectorAnalysis(null)'), /暂无行业 ETF 样本/);
});

test('sector periods show all rows, coverage, and escaped evidence without fetching', () => {
  const app = appContext();
  app.run(`const sectorRow = {
    topic_code:'semiconductor', topic_label:'半导体<script>', status:'available',
    net_flow_cny:100000000, intensity_pct:1.25, coverage_pct:50, etf_count:4,
    comparable_etf_count:2, start_date:'2026-08-31', end_date:'2026-09-04',
    interpretation:'已覆盖样本净申购', exposure_links:[{scope_name:'我的基金',known_exposure_pct:0}]
  };
  const sectorFixture = {sector_count:9, data_date:'2026-09-04',methodology:'资金强度不是收益率',boundary:'ETF样本',windows:{
    '5d':{period:5,leaders:{inflow:null,outflow:null},inflow_count:9,outflow_count:0,neutral_count:0,unavailable_count:0,
      rows:Array.from({length:9},(_,i)=>({...sectorRow,topic_code:'sector'+i}))},
    '1d':{period:1,leaders:{inflow:null,outflow:null},inflow_count:0,outflow_count:0,neutral_count:0,unavailable_count:1,
      rows:[{...sectorRow,status:'insufficient',net_flow_cny:null,intensity_pct:null}]}
  }};`);
  const html = app.run('renderCapitalSectorAnalysis(sectorFixture)');
  assert.equal((html.match(/data-sector-code=/g) || []).length, 9);
  assert.match(html, /半导体&lt;script&gt;/);
  assert.match(html, /50\.00%/);
  assert.match(html, /资金强度不是收益率/);
  assert.match(html, /暴露待核验/);
  app.run('state.largeCapitalSectorPeriod = "1d"');
  const daily = app.run('renderCapitalSectorAnalysis(sectorFixture)');
  assert.match(daily, /近 1 日净申购估算/);
  assert.match(daily, /数据不足/);
  assert.doesNotMatch(daily, /\+1\.25%/);
});

test('institution section keeps totals but no individual stock ranking', () => {
  const app = appContext();
  const html = app.run(`renderInstitutionalCapital({status:'available', stock_count:110,
    buy_cny:20000000,sell_cny:10000000,net_buy_cny:10000000,
    top_net_buys:[{stock_name:'不能显示的股票',net_buy_cny:10000000}]})`);
  assert.match(html, /110 只上榜证券/);
  assert.doesNotMatch(html, /不能显示的股票|净买入靠前|institution-security-columns/);
  assert.doesNotMatch(app.run('renderInstitutionalCapital({status:"unavailable"})'), /买入 0/);
});
