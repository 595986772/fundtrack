/* 在真实app.js和最小DOM上回归历史记录/人工复核竞态，不请求生产服务。 */
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const {JSDOM} = require('../../miniprogram/node_modules/jsdom');
const reports = require('../../app/static/fund-report-evals.js');
const pages = require('../../app/static/workspace-pages.js');

function fixture(id='eval_A') {
  return {id,score:100,passed_cases:2,total_cases:2,created_at:'2026-09-14T00:00:00Z',results:['a','b'].map(case_id=>({
    case_id,name:case_id,score:100,passed:true,duration_ms:1,checks:[],report_quality:{manual_review:{status:'pending',note:''},
      reference_answer:'对照原文',review_criteria:[],turns:[],gold_evidence:[]}}))};
}

function harness(request) {
  const dom = new JSDOM('<div id="evals-panel"></div>',{url:'http://localhost/developer'});
  const context = vm.createContext({document:dom.window.document,localStorage:dom.window.localStorage,FormData:dom.window.FormData,
    console,URLSearchParams,setTimeout,requestMock:request,notifications:[],
    window:{location:{pathname:'/developer',hash:'#research/chat'},addEventListener(){},FundScopeWorkspacePages:pages,FundReportEvals:reports}});
  vm.runInContext(fs.readFileSync('app/static/app.js','utf8'),context);
  const run = code => vm.runInContext(code,context);
  context.dashboard={suite:{id:reports.suiteId,scenarios:['a','b'].map(id=>({id,name:id,description:'固定题',tags:[]}))},
    latest_run:fixture(),recent_eval_runs:[fixture(),fixture('eval_B')],observability:{run_count:0,recent_runs:[]}};
  run('apiRequest=requestMock; showToast=message=>notifications.push(message); state.evalSuiteId=window.FundReportEvals.suiteId; state.evalDashboard=dashboard; renderEvalsPanel();');
  const panel=dom.window.document.querySelector('#evals-panel');
  const input=(caseId,note)=>{
    const textarea=panel.querySelector(`[data-report-eval-review][data-case="${caseId}"] textarea`);
    textarea.value=note;textarea.dispatchEvent(new dom.window.Event('input',{bubbles:true}));
  };
  return {run,panel,input,context};
}

test('记录B读取失败恢复A选择、暂停复核，并提供原地重试', async () => {
  let reject;const h=harness(()=>new Promise((_resolve,r)=>reject=r));
  const loading=h.run('loadReportEvalRun("eval_B")');
  assert.equal(h.panel.querySelector('[data-report-eval-run]').value,'eval_A');
  assert.equal(h.panel.querySelector('[data-report-eval-run]').disabled,true);
  assert.equal(h.panel.querySelector('[data-report-eval-review] button').disabled,true);
  assert.match(h.panel.textContent,/正在读取所选记录/);
  reject(new Error('读取中断'));await loading;
  assert.equal(h.panel.querySelector('[data-report-eval-run]').value,'eval_A');
  assert.match(h.panel.textContent,/读取中断/);assert.ok(h.panel.querySelector('[data-report-eval-retry]'));
  assert.equal(h.panel.querySelector('[data-report-eval-review] button').disabled,false);
});

test('复核提交立即锁定套件配置记录，迟到响应不能替换新配置', async () => {
  let resolve;const h=harness(()=>new Promise(r=>resolve=r));h.input('a','复核内容');
  const saving=h.run('saveReportEvalReview(elements.evalsPanel.querySelector("[data-report-eval-review]"))');
  for(const selector of ['[data-eval-suite]','[data-report-eval-mode]','[data-report-eval-run]']) assert.equal(h.panel.querySelector(selector).disabled,true);
  // 模拟换账号/配置等外部上下文变更，即使绕过UI，响应归属检查仍需成立。
  h.run('state.evalRetrievalMode="keyword"; state.evalDashboard.latest_run.id="eval_new"');
  resolve(fixture());await saving;
  assert.equal(h.run('state.evalDashboard.latest_run.id'),'eval_new');
  assert.equal(h.run('state.evalRetrievalMode'),'keyword');
});

test('保存A题不丢失B题草稿，切换运行后返回仍保留独立草稿', async () => {
  let response=fixture();const h=harness(async()=>response);
  h.input('a','A待保存');h.input('b','B尚未提交');
  await h.run('saveReportEvalReview(elements.evalsPanel.querySelector("[data-report-eval-review]"))');
  assert.equal(h.panel.querySelector('[data-case="b"] textarea').value,'B尚未提交');
  response=fixture('eval_B');await h.run('loadReportEvalRun("eval_B")');
  assert.equal(h.panel.querySelector('[data-case="b"] textarea').value,'');
  response=fixture();await h.run('loadReportEvalRun("eval_A")');
  assert.equal(h.panel.querySelector('[data-case="b"] textarea').value,'B尚未提交');
});

test('新上下文读取开始后，旧记录响应不覆盖新视图，也不留下永久加载状态', async () => {
  let resolve;const h=harness(()=>new Promise(r=>resolve=r));
  const loading=h.run('loadReportEvalRun("eval_B")');
  h.run('state.evalDashboardRequestId+=1; state.evalDashboard.latest_run.id="eval_new"');
  resolve(fixture('eval_B'));await loading;
  assert.equal(h.run('state.evalDashboard.latest_run.id'),'eval_new');
  assert.equal(h.run('state.evalRunLoading'),null);
});

test('切账号再返回编辑同题，旧保存响应不能删除新草稿', async () => {
  let resolve;const h=harness(()=>new Promise(r=>resolve=r));h.input('a','旧提交内容');
  const saving=h.run('saveReportEvalReview(elements.evalsPanel.querySelector("[data-report-eval-review]"))');
  h.run('resetReportEvalViewer(); state.userId="bob"; state.userId="chen"; state.evalDashboard=dashboard; renderEvalsPanel();');
  h.input('a','回来之后的新草稿');
  resolve(fixture());await saving;
  h.run('renderEvalsPanel()');
  assert.equal(h.panel.querySelector('[data-case="a"] textarea').value,'回来之后的新草稿');
});
