// 报价展示不请求网络，固定北京时间检查今天、未披露、缺失及涨跌语义。
const test = require('node:test');
const assert = require('node:assert/strict');
const quotes = require('../../app/static/holdings-quotes.js');
const {JSDOM} = require('../../miniprogram/node_modules/jsdom');
const now = new Date('2026-09-08T04:00:00Z');

test('daily quote retains its actual NAV date instead of relabeling yesterday as today', () => {
  const old = quotes.daily({latest_nav_date:'2026-09-07',daily_growth_rate_pct:0.56}, now);
  assert.match(old,/2026-09-07/); assert.match(old,/今日暂无新披露/); assert.match(old,/\+0.56%/);
  const today = quotes.daily({latest_nav_date:'2026-09-08',daily_growth_rate_pct:-0.46}, now);
  assert.match(today,/今日已披露/); assert.match(today,/data-quote-tone="negative"/); assert.match(today,/-0.46%/);
});
test('missing or undated values are not zero; true zero remains neutral', () => {
  assert.match(quotes.daily(null,now),/未提供/);
  assert.doesNotMatch(quotes.daily({daily_growth_rate_pct:3},now),/3.00%/);
  assert.match(quotes.daily({latest_nav_date:'2026-09-08',daily_growth_rate_pct:0},now),/data-quote-tone="neutral"/);
  assert.match(quotes.daily({latest_nav_date:'2026-09-08',daily_growth_rate_pct:0},now),/0.00%/);
  assert.doesNotMatch(quotes.daily({latest_nav_date:'2026-09-09',daily_growth_rate_pct:3},now),/3.00%/);
});
test('today uses Shanghai date even before the UTC day changes', () => {
  assert.equal(quotes.chinaDate(new Date('2026-09-07T16:05:00Z')),'2026-09-08');
});
test('fund identity has a stable category, readable labels, and escaped external text', () => {
  const html = quotes.identity({fund_code:'000001',short_name:'基金<script>',sample_role:'bond_fund'});
  assert.match(html,/data-kind="bond_fund"/); assert.match(html,/债券基金/); assert.match(html,/000001/);
  assert.doesNotMatch(html,/<script>/); assert.match(html,/&lt;script&gt;/);
  assert.match(quotes.identity({fund_code:'000002',sample_role:'unknown'}),/data-kind="general_fund"/);
});
test('longer periods preserve signs and keep missing values separate', () => {
  assert.match(quotes.change(2.95),/data-quote-tone="positive"/);
  assert.match(quotes.change(-2.24),/-2.24%/);
  assert.match(quotes.change(null),/未提供/);
  assert.doesNotMatch(quotes.change(null),/0.00%/);
});

const positionContext = () => ({portfolioId:'p1',userId:'test-user',fundCode:'000001',portfolio:{
  id:'p1',user_id:'test-user',name:'当前账本',position_mode:'actual',holdings:[{
    fund_code:'000001',current_value:1250.4,total_pnl_amount:-100.5,units:1000,
    average_cost_nav:1.3509,weight_pct:65,latest_nav_date:'2026-09-18',
  }],
}});
test('基金详情的持仓只显示账本事实，保留净值日期而不重新计算收益',()=>{
  const context=positionContext(),before=JSON.stringify(context);
  const d=new JSDOM(quotes.position(context)).window.document;
  assert.match(d.body.textContent,/¥1,250.40/);assert.match(d.body.textContent,/-¥100.50/);
  assert.match(d.body.textContent,/65.00%/);assert.match(d.body.textContent,/2026-09-18/);
  assert.equal(d.querySelector('[data-quote-tone]').dataset.quoteTone,'negative');
  assert.equal(d.querySelector('a').getAttribute('href'),'#assets/holdings');
  assert.equal(JSON.stringify(context),before);
});
test('跨用户、跨组合、加载中及读取失败不显示旧的账本金额',()=>{
  for (const patch of [{userId:'other'},{portfolioId:'p2'},{loading:true},{error:'读取失败'},{portfolio:null}]) {
    const html=quotes.position({...positionContext(),...patch});
    assert.doesNotMatch(html,/1,250.40/);assert.doesNotMatch(html,/未持有这只基金/);
    assert.match(html,/role="status"/);
  }
});
test('未持有、待补录和未知数值不冒充零持仓或零收益，名称只能作为文本',()=>{
  assert.match(quotes.position({...positionContext(),fundCode:'000002'}),/当前组合未持有/);
  const context=positionContext();context.portfolio.position_mode='allocation';
  assert.match(quotes.position(context),/尚未补齐/);
  context.portfolio.position_mode='actual';context.portfolio.name='<img src=x>';
  Object.assign(context.portfolio.holdings[0],{current_value:null,total_pnl_amount:0,average_cost_nav:null});
  const d=new JSDOM(quotes.position(context)).window.document;
  assert.equal(d.querySelectorAll('img').length,0);assert.match(d.body.textContent,/<img src=x>/);
  assert.equal(d.querySelector('dd').textContent,'--');
  assert.equal(d.querySelector('[data-quote-tone]').textContent,'¥0.00');
});
