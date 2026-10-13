// 真正挂载基金终端，但数据、图表和动作均为本地替身，禁止网络或模型请求。
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const {JSDOM} = require('../../miniprogram/node_modules/jsdom');

test('基金详情更新持仓不重建图表，换标的清除旧金额，AI 入口仅转交上下文', () => {
  const dom = new JSDOM('<div id="terminal"></div>', {runScripts:'outside-only'});
  const w = dom.window, frames = [], actions = [];
  let init = 0, disposed = 0;
  w.requestAnimationFrame = callback => frames.push(callback);
  w.ResizeObserver = class {observe() {} disconnect() {}};
  w.echarts = {init() {init++; return {setOption() {}, resize() {}, dispose() {disposed++;}};}};
  w.fetch = () => {throw new Error('此视图不得自行发请求');};
  for (const file of ['terminal-data', 'holdings-quotes', 'terminal']) {
    w.eval(fs.readFileSync(`app/static/${file}.js`, 'utf8'));
  }
  const state = {
    userId:'u',selectedPortfolioId:'p',selectedFundCode:'000001',funds:[{fund_code:'000001',short_name:'测试基金'}],
    portfolioDetail:{id:'p',user_id:'u',name:'我的组合',position_mode:'actual',holdings:[{
      fund_code:'000001',units:100,current_value:1000,total_pnl_amount:50,weight_pct:100,
    }]},
    fundChartData:{fund_code:'000001',points:[{date:'2026-09-01',value:1},{date:'2026-09-02',value:1.1}]},
  };
  const root = w.document.querySelector('#terminal');
  const terminal = w.FundScopeTerminal.create(root, {getState:()=>state, actions:{
    icons() {}, openResearch:context=>actions.push(context),
  }});
  const chartElement = root.querySelector('#terminal-chart');
  Object.defineProperty(chartElement, 'clientWidth', {value:600});
  const flush = () => {while(frames.length) frames.shift()();};
  flush();
  assert.equal(init,1);assert.match(root.querySelector('#terminal-position').textContent,/¥1,000.00/);
  state.portfolioDetail.holdings[0].current_value = 1200;
  terminal.update();flush();
  assert.equal(root.querySelector('#terminal-chart'),chartElement);assert.equal(init,1);
  assert.match(root.querySelector('#terminal-position').textContent,/¥1,200.00/);
  assert.equal(actions.length,0);
  root.querySelector('[data-terminal-action="ask-detail"]').click();flush();
  assert.equal(actions.length,1);assert.equal(actions[0].code,'000001');
  state.sending = true;terminal.update();flush();
  assert.equal(root.querySelector('[data-terminal-action="ask-detail"]').disabled,true);
  state.sending = false;state.selectedFundCode = '000002';
  state.funds.push({fund_code:'000002',short_name:'另一基金'});
  terminal.update();flush();
  assert.match(root.querySelector('#terminal-position').textContent,/未持有/);
  assert.doesNotMatch(root.querySelector('#terminal-position').textContent,/1,200.00/);
  state.userId = 'other';terminal.update();flush();
  assert.match(root.querySelector('#terminal-position').textContent,/尚未读取/);
  terminal.dispose();assert.equal(disposed,1);dom.window.close();
});
