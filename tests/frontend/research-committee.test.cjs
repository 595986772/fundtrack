// 全部使用冻结夹具和内存请求；不连接后端、模型或采集服务。
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {JSDOM} = require('../../miniprogram/node_modules/jsdom');
const committee = require('../../app/static/research-committee.js');
const diagnosis = require('../../app/static/portfolio-diagnosis.js');
const root = path.resolve(__dirname, '../..');
const read = name => fs.readFileSync(path.join(root, name), 'utf8');

function fixture() {
  const finding = (role, index) => ({claim_id:`${role}:claim:${index}`, tone:role === 'risk' ? 'caution' : 'supportive',
    title:role === 'risk' ? '历史波动仍需关注' : '配置存在分散支撑', detail:'仅依据冻结快照，未来变化仍需核验。',
    metric_keys:['portfolio.volatility'], metric_references:[{key:'portfolio.volatility',label:'历史波动',value_text:'0.00%',data_date:'2026-09-01'}],
    evidence_ids:['event:1'], evidence_references:[{evidence_id:'event:1',title:'已保存市场事件'}], fund_codes:[]});
  const roles = ['opportunity', 'risk'].map(role => ({role,label:role === 'risk' ? '风险研究员' : '机会研究员',status:'accepted',
    headline:role === 'risk' ? '风险仍集中于少数资产' : '分散配置提供支撑', summary:'历史证据与后续观察条件分开呈现。',
    key_findings:[finding(role, 1), finding(role, 2), finding(role, 3)], uncertainties:['最新披露仍待核对。'],duration_ms:1200,model_calls:1}));
  const reviews = roles.map(stage => ({role:stage.role,label:stage.label,status:'accepted',
    responses:[{target_claim_id:`${stage.role === 'risk' ? 'opportunity' : 'risk'}:claim:1`,verdict:'qualify',finding:{...finding(stage.role, 1),claim_id:undefined}}],
    uncertainties:[],duration_ms:900,model_calls:1}));
  return {run_id:'fixture-report',portfolio_id:'fixture',portfolio_name:'测试组合',period_months:12,
    headline:'主报告结论保持不变',generation:{mode:'multi_agent_committee',model_used:true},
    key_findings:[],key_evidence:{market:[{evidence_id:'event:1',source_url:'https://example.com/evidence',source_name:'冻结市场快照',data_date:'2026-09-01',summary:'已保存事件摘要'}]},
    diagnosis:{headline:'主报告结论保持不变',summary:'主报告摘要',portfolio_name:'测试组合',report_date:'2026-09-01',interpretation_label:'AI 研判',
      performance_label:'历史业绩',findings:[],more_findings:[],holdings:[],conflicts:[],uncertainties:[],follow_ups:[],questions:[],technical:[],limitations:[]},
    committee:{version:'portfolio-committee-v1',status:'completed',snapshot_id:'snapshot:fixture',roles,reviews,warnings:[],model_calls:5,
      synthesis:{status:'accepted',model_calls:1,duration_ms:1800}}};
}

function doc(html) { return new JSDOM(html).window.document; }

function appContext(t) {
  const dom = new JSDOM(read('app/static/index.html'), {url:'http://localhost/#research/reports',runScripts:'outside-only'});
  t.after(() => dom.window.close());
  const {window} = dom;
  const addEventListener = window.addEventListener.bind(window);
  window.addEventListener = (name, ...args) => { if (name !== 'DOMContentLoaded') addEventListener(name, ...args); };
  window.FundScopeWorkspacePages = require('../../app/static/workspace-pages.js');
  window.FundScopeResearchCommittee = committee;
  window.FundScopePortfolioDiagnosis = diagnosis;
  window.fetch = () => { throw new Error('Unexpected real request'); };
  const context = dom.getInternalVMContext();
  const run = code => vm.runInContext(code, context);
  run(read('app/static/app.js'));
  run(`bindResearchWorkspaceActions = () => {}; bindStaticEvents();
    state.userId='reader'; state.selectedPortfolioId='fixture';
    activateProductView=renderPortfolioWorkbench=refreshIcons=showToast=()=>{};
    loadPortfolioResearchHistory=async()=>{};
    globalThis.requests=[]; globalThis.confirmations=[]; globalThis.sequence=0;
    window.confirm=message=>{confirmations.push(message);return true;};
    createIdempotencyKey=()=> 'key-'+(++sequence);
    apiRequest=async (url,options)=>{requests.push({url,options});
      if(options?.method==='POST')return {job:{id:'done',status:'completed'}};
      return {run_id:'done',portfolio_id:'fixture'};};`);
  function mount() { window.document.querySelector('#portfolio-workbench').innerHTML = run('renderPortfolioResearch()'); }
  function select(mode) {
    const node = window.document.querySelector('[data-portfolio-research-mode]');
    node.value = mode;
    node.dispatchEvent(new window.Event('change', {bubbles:true}));
  }
  return {window,run,mount,select,document:window.document};
}

test('默认轻量综合，只接受明确 committee，模块可按 window 模式加载', () => {
  for (const value of [undefined, null, '', 'standard', 'COMMITTEE', 'unknown']) {
    assert.equal(committee.normalizeMode(value), 'standard');
    assert.equal(doc(committee.modeControl(value)).querySelector('select').value, 'standard');
  }
  const select = doc(committee.modeControl('committee', true)).querySelector('select');
  assert.equal(select.value, 'committee'); assert.equal(select.disabled, true);
  assert.match(select.querySelector('[value="committee"]').textContent, /额外模型用量/);
  const context = vm.createContext({window:{},URL});
  vm.runInContext(read('app/static/research-committee.js'), context);
  assert.equal(typeof context.window.FundScopeResearchCommittee.render, 'function');
});

test('完成报告保留机会风险对照、指标零值和冻结证据的日期与安全链接', () => {
  const report = fixture(), before = JSON.stringify(report), page = doc(committee.render(report));
  assert.equal(page.querySelector('.research-committee').dataset.status, 'completed');
  assert.equal(page.querySelectorAll('[data-committee-role]').length, 2);
  assert.match(page.querySelector('.committee-metrics').textContent, /0\.00%/);
  assert.match(page.querySelector('.committee-evidence').textContent, /2026-09-01/);
  assert.equal(page.querySelector('.committee-evidence a').href, 'https://example.com/evidence');
  assert.equal(page.querySelector('.committee-evidence a').rel, 'noopener noreferrer');
  assert.equal(JSON.stringify(report), before);
});

test('一次质询和额外观点默认折叠，目标显示观点标题而不是编号堆', () => {
  const report = fixture();
  report.committee.reviews[1].responses.push({...report.committee.reviews[1].responses[0],verdict:'disagree'});
  report.committee.reviews[0].responses[0].verdict = 'agree';
  const page = doc(committee.render(report));
  assert.equal(page.querySelectorAll('.committee-cross-review').length, 1);
  assert.ok([...page.querySelectorAll('details')].every(item => !item.open));
  assert.equal(page.querySelectorAll('.committee-role > .committee-finding').length, 4);
  assert.equal(page.querySelectorAll('.committee-role > details > .committee-finding').length, 2);
  assert.match(page.querySelector('.committee-response > p').textContent, /风险研究员 · 历史波动/);
  assert.doesNotMatch(page.querySelector('.committee-response > p').textContent, /claim:/);
  assert.deepEqual([...page.querySelectorAll('.committee-verdict')].map(n => n.textContent), ['同意','有条件同意','不同意']);
  assert.ok([...page.querySelectorAll('code')].every(n => n.closest('details')));
});

test('降级顶部只保留总体说明和阶段状态，详细错误按阶段保留，warnings去重', () => {
  const report = fixture();
  report.committee.status = 'degraded';
  Object.assign(report.committee.roles[1], {status:'failed',failure_reason:'风险分析超时',headline:'不应展示的拒绝内容'});
  Object.assign(report.committee.reviews[0], {status:'skipped',failure_reason:'对方分析未通过'});
  report.committee.reviews[1].status = 'failed';
  Object.assign(report.committee.synthesis, {status:'skipped',failure_reason:'前序必要阶段未完成，未调用综合研判，最终使用规则回退',model_calls:0});
  report.generation.mode = 'deterministic_fallback';
  report.committee.warnings = ['风险分析超时', '对方分析未通过', report.committee.synthesis.failure_reason,
    '综合结论已回退为规则说明，非多角色共识。', '请保留数据缺口', '请保留数据缺口。'];
  const page = doc(committee.render(report)), alert = page.querySelector('.committee-alert');
  assert.equal(page.querySelector('.research-committee').dataset.status, 'degraded');
  assert.equal(alert.closest('details'), null);
  assert.match(alert.textContent, /规则说明/);
  assert.match(alert.textContent, /风险研究员分析：失败/);
  assert.match(alert.textContent, /机会研究员回应：未执行/);
  assert.match(alert.textContent, /综合研判：未执行/);
  assert.equal(alert.querySelectorAll('li').length, 5);
  for (const text of ['风险分析超时','对方分析未通过','前序必要阶段未完成']) assert.ok(!alert.textContent.includes(text));
  assert.match(page.querySelector('[data-committee-role="risk"]').textContent, /风险分析超时/);
  assert.match(page.querySelector('.committee-cross-review').textContent, /对方分析未通过/);
  assert.match(page.querySelector('.committee-diagnostics').textContent, /综合研判未执行 · 角色调用 0/);
  assert.match(page.querySelector('.committee-diagnostics').textContent, /未调用综合研判/);
  assert.match(page.querySelector('.committee-cross-review > summary').textContent, /未完成/);
  assert.doesNotMatch(page.body.textContent, /不应展示的拒绝内容/);
  assert.match(page.querySelector('.committee-warnings').textContent, /数据缺口/);
  assert.equal(page.querySelectorAll('.committee-warnings li').length, 1);
  assert.ok(page.querySelector('[data-committee-role="opportunity"] .committee-finding'));
});

test('缺失角色、回应或阶段诊断不能显示全员完成，未知数值不填零', () => {
  const report = fixture();
  report.committee.roles = [report.committee.roles[0]];
  report.committee.roles[0].key_findings = [];
  report.committee.reviews[0].responses = [];
  report.committee.reviews[1].duration_ms = null;
  report.committee.synthesis = undefined;
  report.committee.model_calls = null;
  const page = doc(committee.render(report));
  assert.equal(page.querySelector('.research-committee').dataset.status, 'degraded');
  assert.match(page.querySelector('.committee-alert').textContent, /机会研究员分析：记录不全/);
  assert.match(page.querySelector('.committee-alert').textContent, /机会研究员回应：记录不全/);
  assert.match(page.querySelector('.committee-alert').textContent, /综合研判：未完成/);
  assert.match(page.querySelector('.committee-diagnostics').textContent, /耗时未记录/);
  assert.match(page.querySelector('.committee-diagnostics').textContent, /角色调用未记录/);
});

test('角色调用统计与综合阶段诊断可展开，没有供应商计费次数承诺', () => {
  const report = fixture(); report.committee.model_calls = 8; report.committee.synthesis.model_calls = 4;
  const page = doc(committee.render(report)), details = page.querySelector('.committee-diagnostics');
  assert.equal(details.open, false); assert.match(details.textContent, /角色调用8 次/);
  assert.match(details.textContent, /综合研判已完成 · 角色调用 4 · 1\.8 秒/);
  assert.match(details.textContent, /snapshot:fixture/);
  assert.doesNotMatch(page.body.textContent, /最多.?5|计费次数|供应商调用/);
});

test('没有 committee 的旧报告兼容，新格式损坏显示缺失而不是成功', () => {
  assert.equal(committee.render({}), '');
  assert.equal(committee.render({generation:{mode:'deterministic_fallback'}}), '');
  for (const report of [{generation:{mode:'multi_agent_committee'}}, {committee:{version:'future'}}]) {
    assert.match(committee.render(report), /记录缺失或版本不受支持/);
  }
  const report = fixture(); report.committee.roles = null; report.committee.reviews = {}; report.committee.warnings = null;
  assert.match(committee.render(report), /研判未完成/);
});

test('所有模型文本均转义，原始来源拒绝脚本、文件、带凭据链接', () => {
  const report = fixture(), attack = '<img src=x onerror="alert(1)"><script>bad()</script>';
  Object.assign(report.committee.roles[0], {label:attack,headline:attack,summary:attack,uncertainties:[attack]});
  Object.assign(report.committee.roles[0].key_findings[0], {title:attack,detail:attack,claim_id:attack});
  Object.assign(report.committee.roles[0].key_findings[0].metric_references[0], {label:attack,value_text:attack,data_date:attack});
  report.committee.snapshot_id = attack; report.committee.warnings = [attack];
  report.committee.synthesis = {status:'failed',failure_reason:attack};
  report.committee.reviews[0].responses[0].target_claim_id = attack;
  report.committee.reviews[0].responses[0].finding.detail = attack;
  for (const url of ['javascript:alert(1)', 'file:///tmp/evidence', 'data:text/html,bad', 'https://user:secret@example.com', '//example.com']) {
    Object.assign(report.key_evidence.market[0], {source_url:url,source_name:attack,summary:attack});
    const page = doc(committee.render(report));
    assert.equal(page.querySelectorAll('img, script, [onerror], [onclick]').length, 0);
    assert.equal(page.querySelectorAll('a').length, 0);
    assert.ok(page.body.textContent.includes(attack));
  }
});

test('空指标保持未知，找不到质询目标保持缺口，不凭空关联证据', () => {
  const report = fixture(), item = report.committee.roles[0].key_findings[0];
  item.metric_references[0].value_text = null;
  item.metric_keys.push('unresolved-key');
  item.evidence_ids = ['unknown-id']; item.evidence_references = [];
  report.committee.reviews[0].responses[0].target_claim_id = 'missing';
  const page = doc(committee.render(report));
  assert.match(page.querySelector('.committee-metrics').textContent, /未记录/);
  assert.match(page.querySelector('.committee-role').textContent, /部分指标引用尚未解析/);
  assert.equal(page.querySelector('.committee-role > .committee-finding').querySelector('a'), null);
  assert.match(page.querySelector('.committee-response').textContent, /目标观点未保留/);
});

test('研判过程位于主报告之后，独立观点默认折叠；旧报告只增加模式选择', () => {
  const report = fixture(), document = doc('');
  const markup = diagnosis.render({report});
  const page = doc(committee.enhanceReport(markup, {report,document}));
  assert.equal(page.querySelector('h2').textContent, report.diagnosis.headline);
  assert.equal(page.querySelector('.portfolio-diagnosis > .diagnosis-section').classList.contains('diagnosis-findings'), true);
  assert.equal(page.querySelector('.research-committee').previousElementSibling.classList.contains('diagnosis-outlook'),true);
  assert.equal(page.querySelector('.research-committee').nextElementSibling.classList.contains('diagnosis-appendix'),true);
  assert.equal(page.querySelector('.committee-analysis').open,false);
  assert.equal(page.querySelector('[data-portfolio-research-mode]').value, 'standard');
  delete report.committee; report.generation.mode = 'structured_model_synthesis';
  const old = doc(committee.enhanceReport(markup, {report,document}));
  assert.equal(old.querySelector('.research-committee'), null);
  old.querySelector('.research-request-fields').remove();
  assert.equal(old.body.innerHTML, doc(markup).body.innerHTML);
  assert.equal(committee.enhanceReport('<p>重读报告</p>', {report:{run_id:'old'},document}), '<p>重读报告</p>');
});

test('启动区默认 standard，切换模式与展开结果不提交、不重读、不重建页面', t => {
  const app = appContext(t); app.mount();
  const select = app.document.querySelector('[data-portfolio-research-mode]');
  assert.equal(select.value, 'standard');
  app.select('committee');
  assert.equal(app.run('state.portfolioResearchMode'), 'committee');
  assert.equal(app.document.querySelector('[data-portfolio-research-mode]'), select);
  app.select('standard');
  assert.equal(app.run('requests.length'), 0); assert.equal(app.run('confirmations.length'), 0);
});

test('问题、投资条件和明确刷新授权随请求传递，输入本身不触发任务', async t => {
  const app = appContext(t); app.mount(); app.select('auto');
  for (const [selector,value] of [['[data-research-question]','比较两季策略变化'],['[data-research-personal="horizon"]','三年']]) {
    const node = app.document.querySelector(selector); node.value=value;
    node.dispatchEvent(new app.window.Event('input', {bubbles:true}));
  }
  const permission = app.document.querySelector('[data-research-refresh]');
  assert.equal(permission.checked,false); permission.checked=true;
  permission.dispatchEvent(new app.window.Event('change',{bubbles:true}));
  assert.equal(app.run('requests.length'),0);
  app.document.querySelector('[data-run-portfolio-research]').click();
  const payload = JSON.parse(app.run('requests[0].options.body'));
  assert.equal(payload.question,'比较两季策略变化');
  assert.deepEqual(payload.investor_context,{horizon:'三年'});
  assert.equal(payload.allow_public_refresh,true); assert.equal(payload.research_mode,'auto');
  await new Promise(resolve=>setImmediate(resolve));
});

test('四节点路径按服务端总数显示，不冒充八阶段市场读取或多角色研判', t => {
  const app = appContext(t);
  app.run(`state.portfolioResearchMode='strategy';state.portfolioResearchRunning=true;
    state.portfolioResearchJob={status:'running',current_step:2,total_steps:4,progress_percent:50};`);
  app.mount();
  assert.match(app.document.querySelector('.committee-progress').textContent,/2 \/ 4/);
  assert.doesNotMatch(app.document.querySelector('.committee-progress').textContent,/多角色/);
  assert.equal(app.document.querySelector('.portfolio-research-steps'),null);
  assert.equal(app.document.querySelector('[data-research-question]').disabled,true);
});

test('显式点击启动才提交 committee 参数，取消确认不提交', async t => {
  const app = appContext(t); app.mount(); app.select('committee');
  app.document.querySelector('[data-run-portfolio-research]').click();
  assert.equal(app.run('requests.length'), 1);
  assert.equal(app.run('JSON.parse(requests[0].options.body).research_mode'), 'committee');
  assert.match(app.run('requests[0].url'), /\/api\/v1\/portfolios\/fixture\/research-runs$/);
  assert.match(app.run('confirmations[0]'), /五个研判阶段.*增加模型用量/);
  await new Promise(resolve => setImmediate(resolve));
  app.run('window.confirm=()=>false');
  await app.run('runPortfolioResearch({researchMode:"committee"})');
  assert.equal(app.run('requests.filter(r=>r.options?.method==="POST").length'), 1);
});

test('旧直接启动入口保持 standard，不继承报告页的多角色选择', async t => {
  const app = appContext(t); app.run('state.portfolioResearchMode="committee"');
  await app.run('runPortfolioResearch()');
  assert.equal(app.run('JSON.parse(requests[0].options.body).research_mode'), 'standard');
  assert.doesNotMatch(app.run('confirmations[0]'), /五个研判阶段/);
  app.run('requests=[]'); await app.run('runPortfolioResearch({researchMode:"unknown"})');
  assert.equal(app.run('JSON.parse(requests[0].options.body).research_mode'), 'standard');
});

test('提交响应丢失的幂等键按研究模式隔离，各模式原样重试复用', async t => {
  const app = appContext(t);
  app.run('apiRequest=async(url,options)=>{requests.push({url,options});throw new Error("response lost")}');
  await app.run('runPortfolioResearch()');
  await app.run('runPortfolioResearch({researchMode:"committee"})');
  await app.run('runPortfolioResearch({researchMode:"committee"})');
  await app.run('runPortfolioResearch()');
  const keys = JSON.parse(app.run('JSON.stringify(requests.map(r=>JSON.parse(r.options.body).idempotency_key))'));
  assert.notEqual(keys[0], keys[1]); assert.equal(keys[1], keys[2]); assert.equal(keys[0], keys[3]);
});

test('标准旧缓存不能被 committee 接管，断线只恢复各自 Job', async t => {
  const app = appContext(t);
  app.window.localStorage.setItem('fundscope-research:reader:fixture:12', 'legacy-standard');
  app.run(`apiRequest=async(url,options)=>{requests.push({url,options});return {job:{id:options?'committee-job':'legacy-standard',status:'running'}}};
    waitForBackgroundJobEvents=async()=>{throw new Error('stream disconnected')};`);
  await app.run('runPortfolioResearch({researchMode:"committee"})');
  assert.equal(app.run('requests[0].options.method'), 'POST');
  await app.run('runPortfolioResearch({researchMode:"committee"})');
  assert.match(app.run('requests[1].url'), /background-jobs\/committee-job\?/);
  assert.equal(app.run('requests[1].options'), undefined);
  await app.run('runPortfolioResearch()');
  assert.match(app.run('requests[2].url'), /background-jobs\/legacy-standard\?/);
  assert.equal(app.run('requests[2].options'), undefined);
});

test('新上下文恢复 standard；运行中禁用模式，显示后端实际总阶段数', t => {
  const app = appContext(t);
  app.run(`state.portfolioResearchMode='committee'; state.portfolioResearchRunning=true;
    state.portfolioResearchJob={current_step:9,total_steps:13,progress_percent:69,progress_message:'等待综合研判'};`);
  app.mount();
  assert.equal(app.document.querySelector('select[data-portfolio-research-mode]').disabled, true);
  assert.match(app.document.querySelector('#portfolio-workbench').textContent, /9 \/ 13/);
  assert.equal(app.document.querySelector('.portfolio-research-steps'), null);
  app.select('standard'); assert.equal(app.run('state.portfolioResearchMode'), 'committee');
  app.run('resetPortfolioResearch()'); assert.equal(app.run('state.portfolioResearchMode'), 'standard');
});

test('13节点图的模式路由不当成综合完成，并行进度不猜角色完成顺序', t => {
  const app = appContext(t);
  assert.equal(committee.isCommitteeRun({total_steps:13}), true);
  assert.equal(committee.isCommitteeRun({}, {research_mode:'committee'}), true);
  assert.equal(committee.isCommitteeRun({}, {graph_trace:{completed_nodes:['interpret_with_llm','committee_risk']}}), true);
  assert.equal(committee.isCommitteeRun({}, {graph_trace:{completed_nodes:['interpret_with_llm']}}), false);
  app.run(`globalThis.message={run_metadata:{portfolio_research_jobs:[{id:'job',status:'running',current_step:7,total_steps:13,progress_percent:54}]}};`);
  const chat = doc(app.run('renderMessagePortfolioResearch(message)'));
  assert.match(chat.body.textContent, /7 \/ 13/);
  assert.equal(chat.querySelector('.message-research-steps'), null);
  assert.doesNotMatch(chat.body.textContent, /AI 综合|已完成综合/);
  app.run('message.run_metadata.portfolio_research_jobs[0].total_steps=8');
  assert.equal(doc(app.run('renderMessagePortfolioResearch(message)')).querySelectorAll('.message-research-steps > span').length, 8);
});

test('恢复多角色历史报告的图计数为13，不改变下一次运行默认模式', async t => {
  const app = appContext(t);
  // 取回真实只读加载器，其他请求仍为内存替身。
  const source = read('app/static/app.js');
  app.run(source.slice(source.indexOf('async function loadPortfolioResearchHistory('), source.indexOf('async function comparePortfolioResearch(')));
  app.run(`apiRequest=async()=>({runs:[{job_id:'saved'}],latest_report:{run_id:'saved',research_mode:'committee'}});`);
  await app.run('loadPortfolioResearchHistory()');
  assert.equal(app.run('state.portfolioResearchJob.total_steps'), 13);
  assert.equal(app.run('state.portfolioResearchJob.current_step'), 13);
  assert.equal(app.run('state.portfolioResearchMode'), 'standard');
});

test('历史读取不升级模式，读取失败只重读，运行失败保留所选模式供明确重试', async t => {
  const app = appContext(t);
  app.run(`loadPortfolioResearchHistory=async()=>{};
    apiRequest=async()=>({run_id:'old',portfolio_id:'fixture',period_months:12,committee:{version:'portfolio-committee-v1'}});`);
  await app.run('openReviewedReport("old")');
  assert.equal(app.run('state.portfolioResearchMode'), 'standard');
  app.run('apiRequest=async()=>{throw new Error("read failed")}');
  await app.run('openReviewedReport("old")'); app.mount();
  assert.ok(app.document.querySelector('[data-review-report]'));
  assert.equal(app.document.querySelector('[data-portfolio-research-mode]'), null);
  assert.equal(app.document.querySelector('[data-run-portfolio-research]'), null);
  app.run('state.portfolioReportReadJobId=""; state.portfolioResearchMode="committee"'); app.mount();
  assert.equal(app.document.querySelector('[data-portfolio-research-mode]').value, 'committee');
});

test('新模块顺序正确，每日简报没有模式或多角色 UI 接入', () => {
  const html = read('app/static/index.html');
  assert.equal((html.match(/src="\/static\/research-committee\.js/g) || []).length, 1);
  assert.ok(html.indexOf('research-committee.js') < html.indexOf('/static/app.js'));
  assert.match(html, /research-committee\.css/);
  assert.doesNotMatch(read('app/static/daily-briefings.js'), /FundScopeResearchCommittee|data-portfolio-research-mode|committee-columns/);
});

test('桌面和手机真实渲染无溢出，原生模式控件与质询键盘可用', {skip:!process.env.FUNDSCOPE_COMMITTEE_BROWSER_QA}, async t => {
  const {chromium} = require('playwright');
  const browser = await chromium.launch({headless:true});
  t.after(() => browser.close());
  const page = await browser.newPage();
  const errors = [], requests = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.route('**/*', route => { requests.push(route.request().url()); return route.abort(); });
  const css = ['styles.css','design-system.css','portfolio-diagnosis.css','data-workspaces.css','mobile-input.css','research-committee.css'].map(file => read(`app/static/${file}`)).join('\n');
  const report = fixture();
  const document = doc('');
  const output = committee.enhanceReport(diagnosis.render({report}), {report,document});
  for (const width of [1440, 390]) {
    await page.setViewportSize({width,height:1000});
    await page.setContent(`<html lang="zh-CN"><head><meta charset="UTF-8"><style>${css}</style></head><body><main id="portfolio-workbench" style="max-width:1000px;margin:auto;padding:16px"><div class="research-launch-controls">${committee.modeControl()}<button type="button" class="primary-action compact">运行深度研究</button></div>${output}</main></body></html>`);
    const select = page.locator('[data-portfolio-research-mode]').first();
    await select.focus(); await page.keyboard.press('ArrowDown'); await page.keyboard.press('Enter');
    assert.equal(await select.inputValue(), 'committee');
    const review = page.locator('.committee-cross-review > summary');
    await review.focus(); await page.keyboard.press('Enter');
    assert.equal(await page.locator('.committee-cross-review').evaluate(node => node.open), true);
    const sizes = await page.evaluate(() => ({width:innerWidth,scroll:document.documentElement.scrollWidth,
      columns:getComputedStyle(document.querySelector('.committee-columns')).gridTemplateColumns,
      font:getComputedStyle(document.querySelector('[data-portfolio-research-mode]')).fontSize}));
    assert.ok(sizes.scroll <= sizes.width, JSON.stringify(sizes));
    assert.equal(sizes.columns.split(' ').length, width < 760 ? 1 : 2);
    if (width < 760) assert.equal(sizes.font, '16px');
    await page.screenshot({path:`/tmp/fundscope-committee-${width}.png`,fullPage:true});
  }
  assert.deepEqual(errors, []); assert.deepEqual(requests, []);
});
