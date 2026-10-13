// 只校验展示契约，不连接供应商、不运行模型，也不修改真实账本。
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const {JSDOM} = require('../../miniprogram/node_modules/jsdom');
const visuals = require('../../app/static/data-visuals');
const lists = require('../../app/static/research-lists');
const doc = html => new JSDOM(html).window.document;
const performance = {period_start:'2026-09-01',period_end:'2026-09-03',observation_count:3,
  performance_label:'交易流水调整后的业绩',benchmark:{benchmark_name:'测试基准'},
  series:[{date:'2026-09-01',portfolio_index:100,benchmark_index:100,drawdown_pct:0},
    {date:'2026-09-02',portfolio_index:102,benchmark_index:null,drawdown_pct:0},
    {date:'2026-09-03',portfolio_index:101,benchmark_index:101.5,drawdown_pct:-0.98}]};

test('收益曲线仅转换已有指数，缺失基准保留断点，回撤不用价格推算',()=>{
  assert.deepEqual(visuals.chartData(performance).points.map(p=>p.value),[0,2,1]);
  assert.deepEqual(visuals.chartData(performance).points.map(p=>p.benchmark),[0,null,1.5]);
  assert.deepEqual(visuals.chartData(performance,true).points.map(p=>p.value),[0,0,-0.98]);
  const html=visuals.trend(performance),d=doc(html);
  assert.equal(d.querySelectorAll('tbody tr').length,3);
  assert.equal(d.querySelector('details').open,false);
  assert.match(d.body.textContent,/2026-09-01 至 2026-09-03/);
});
test('未知不是零：null、字符串、NaN 不进入数值计算',()=>{
  const p={...performance,series:[{date:'2026-09-01',portfolio_index:null},{date:'2026-09-02',portfolio_index:'105'}]};
  assert.deepEqual(visuals.chartData(p).points.map(p=>p.value),[null,null]);
  assert.doesNotMatch(visuals.trend(p),/data-dv-chart=/);
  assert.match(visuals.trend(p),/至少需要两个有效观测日/);
  assert.match(visuals.performanceMetrics({twr_pct:0}),/0.00%/);
  assert.equal(visuals.pct(null),'--');assert.equal(visuals.money(NaN),'--');
});
test('正负条共用零轴与尺度，零值不画出虚假宽度',()=>{
  const d=doc(visuals.bars([{label:'负',value:-100},{label:'零',value:0},{label:'正',value:50}],{signed:true}));
  const bars=[...d.querySelectorAll('.dv-bar-track i')];
  assert.equal(bars[0].style.left,'0%');assert.equal(bars[0].style.width,'50%');
  assert.equal(bars[1].style.width,'0%');assert.equal(bars[2].style.left,'50%');assert.equal(bars[2].style.width,'25%');
});
test('仓位用实际权重，盈亏贡献仅用浮动盈亏，不混入已实现或当日收益',()=>{
  const html=visuals.holdings({holdings:[{fund_code:'A',weight_pct:20,unrealized_pnl_amount:-10,total_pnl_amount:900},
    {fund_code:'B',weight_pct:80,unrealized_pnl_amount:40}]});
  const d=doc(html),values=[...d.querySelectorAll('.dv-allocation-strip span')].map(e=>e.style.width);
  assert.deepEqual(values,['80%','20%']);assert.match(html,/\+¥40.00/);assert.match(html,/-¥10.00/);
  assert.doesNotMatch(html,/900/);assert.match(html,/非当日收益/);
});
test('概览与贡献可独立呈现，不截断基金，也不改变各自的排序或数值',()=>{
  const portfolio={holdings:Array.from({length:12},(_,i)=>({fund_code:`基金${i}`,weight_pct:i===0?null:i,unrealized_pnl_amount:i-5}))};
  const before=JSON.stringify(portfolio),allocation=doc(visuals.holdings(portfolio,{view:'allocation'})),contribution=doc(visuals.holdings(portfolio,{view:'contribution'}));
  assert.equal(allocation.querySelectorAll('.dv-bars li').length,12);
  assert.equal(allocation.querySelector('.dv-allocation-list').tabIndex,0);
  assert.equal(allocation.querySelector('.dv-contribution'),null);
  assert.equal(contribution.querySelector('.dv-allocation'),null);
  assert.equal(contribution.querySelectorAll('.dv-bars li').length,12);
  assert.match(allocation.body.textContent,/未知/);assert.match(contribution.body.textContent,/非当日收益/);
  assert.equal(JSON.stringify(portfolio),before);
});
test('来源文本和属性全部转义，图表工具提示不执行 HTML',()=>{
  const p={...performance,performance_label:'<img src=x>',benchmark:{benchmark_name:'<script>'}};
  const d=doc(visuals.trend(p));assert.equal(d.querySelectorAll('script,img').length,0);
  assert.equal(JSON.parse(d.querySelector('[data-dv-chart]').dataset.dvChart).benchmark,'<script>');
  assert.equal(doc(visuals.bars([{label:'<script>',value:2,text:'<img>'}])).querySelectorAll('script,img').length,0);
});
test('事件列表只显示标题与关联，正文留在详情且详情入口稳定',()=>{
  const item={event_id:'event-x',title:'事件标题',summary:'很长的完整正文',published_date:'2026-09-03',source_url:'https://example.test',affected_fund_codes:['123456']};
  const d=doc(lists.events([item]));assert.equal(d.querySelectorAll('.dv-timeline > li').length,1);
  assert.equal(d.querySelector('[data-research-detail]').dataset.detailId,'event-x');
  assert.doesNotMatch(d.body.textContent,/很长的完整正文/);
  assert.match(lists.eventDetail(item),/很长的完整正文/);
});
test('板块正负资金可比较，缺失值与真实零分开，筛选排序有效',()=>{
  const rows=[{code:'a',label:'A',events:[],signal:{flow_5d_cny:-2e8}},{code:'b',label:'B',events:[],signal:{flow_5d_cny:1e8}},{code:'c',label:'C',events:[],signal:{flow_5d_cny:null}}];
  const d=doc(lists.radar(rows)),items=[...d.querySelectorAll('.dv-bars > li')];
  assert.equal(items[0].querySelector('button').dataset.detailId,'b');
  assert.equal(items[1].querySelector('i[style]').style.left,'0%');
  assert.match(items[2].textContent,/数据不足/);assert.match(d.body.textContent,/尚未接入/);
  assert.equal(doc(lists.radar(rows,{query:'B'})).querySelectorAll('.dv-bars > li').length,1);
});
test('社区统计不合并跨话题作者，未知分歧与无样本不会显示成零或全看多',()=>{
  const items=[{topic_key:'one',topic_label:'主题一',author_count:3,action_counts:{buy:1,add:1,sell:1},disagreement_ratio:0.66},
    {topic_key:'two',topic_label:'主题二',author_count:3,action_counts:{}}];
  const d=doc(lists.topics(items)),topics=d.querySelectorAll('.product-topic-list > li');
  assert.equal(topics.length,2);assert.match(topics[0].textContent,/分歧 66%/);
  assert.match(topics[1].textContent,/分歧 未知/);assert.equal(topics[1].querySelector('.dv-opinion-strip'),null);
  assert.match(d.body.textContent,/暂无可比较的分歧历史/);
  assert.doesNotMatch(d.body.textContent,/6 位独立作者/);
});
test('图表只初始化一次，隐藏后可恢复尺寸，释放时清理观察器',()=>{
  const dom=new JSDOM(visuals.trend(performance),{runScripts:'outside-only'}),w=dom.window;
  const observers=[],options=[];let init=0,resize=0,disposed=0,disconnected=0;
  w.ResizeObserver=class {constructor(fn){this.fn=fn;observers.push(this);}observe(){}disconnect(){disconnected++;}};
  w.echarts={init(){init++;return {setOption(value){options.push(value);},resize(){resize++;},dispose(){disposed++;}};}};
  const element=w.document.querySelector('[data-dv-chart]');let width=0;Object.defineProperty(element,'clientWidth',{get:()=>width});
  element.style.setProperty('--muted','#b0b1bb');
  w.eval(fs.readFileSync('app/static/data-visuals.js','utf8'));
  w.FundScopeDataVisuals.mount(w.document);assert.equal(init,0);
  width=700;observers[0].fn();w.FundScopeDataVisuals.mount(w.document);assert.equal(init,1);
  observers[0].fn();assert.equal(resize,1);assert.equal(options[0].tooltip.renderMode,'richText');
  assert.equal(options[0].series[1].connectNulls,false);assert.equal(options[0].series[0].smooth,false);
  assert.equal(options[0].aria.enabled,false);
  assert.match(element.getAttribute('aria-label'),/3个观测日/);
  assert.equal(options[0].xAxis.axisLabel.color,'#b0b1bb');
  assert.equal(options[0].yAxis.axisLabel.color,'#b0b1bb');
  w.FundScopeDataVisuals.unmount(w.document);assert.equal(disposed,1);assert.equal(disconnected,1);dom.window.close();
});
