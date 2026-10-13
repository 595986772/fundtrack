const test = require('node:test');
const assert = require('node:assert/strict');
const {render, chartOptions, create} = require('../../app/static/market-turnover-chart.js');

const sample = (id = 'reference:sh_sz') => ({
  series_id: id, label: id.startsWith('index:') ? '科创50' : '沪深成交额参考合计', group: 'reference',
  series: [{series_id:'reference:sh_sz',label:'沪深成交额参考合计',group:'reference'}, {series_id:'index:sh000688',label:'科创50',group:'indices'}],
  data_date:'2026-09-08', range_start:'2026-06-11', range_end:'2026-09-08', warnings:[], scope:'行情口径参考值', methodology:'每个交易日一个点',
  points: [
    {date:'2026-09-07',amount_cny:200e8},
    {date:'2026-09-08',amount_cny:300e8},
  ],
});

test('daily chart has one category per trading day with no fabricated weekend zero', () => {
  const data = sample();
  const options = chartOptions(data);
  assert.equal(options.xAxis.type, 'category');
  assert.deepEqual(options.xAxis.data, ['2026-09-07','2026-09-08']);
  assert.deepEqual(options.series[0].data, [200,300]);
  assert.equal(options.series[0].smooth, false);
  assert.equal(options.series[0].showSymbol, true);
  assert.equal(options.series[0].connectNulls, false);
  data.points[1].amount_cny=null;
  assert.deepEqual(chartOptions(data).series[0].data,[200,null]);
  assert.equal(options.xAxis.axisLabel.formatter('2026-09-08'), '09-08');
});

test('empty, one day and source warnings have explicit states; intraday controls are removed', () => {
  assert.match(render(), /暂无日线成交额/);
  const data = sample();
  data.points = data.points.slice(0,1);
  const html = render({data,catalog:data.series});
  assert.match(html, /只有1个交易日数据/);
  assert.match(html, /每日明细/);
  assert.match(html, /近30日/);
  assert.match(html, /近3个月/);
  assert.doesNotMatch(html, /采样点|data-turnover-date|分时行情/);
  assert.match(html, /科创50/);
  assert.equal(chartOptions(data).series[0].data.length, 1);
});

test('source text is escaped and tooltips are not executable HTML', () => {
  const data = sample(); data.label = '<img src=x onerror=bad>'; data.warnings = ['<script>bad</script>'];
  assert.doesNotMatch(render({data}), /<img|<script/);
  assert.match(render({data}), /&lt;img/);
  assert.equal(chartOptions(data).tooltip.renderMode, 'richText');
});

function setup(request) {
  const element = {innerHTML:'',addEventListener(){},removeEventListener(){},setAttribute(){},querySelector(){return null;}};
  return {element, controller:create({element,request})};
}

test('switching series and periods only performs daily GET without a collection POST', async () => {
  const calls=[];
  const {controller}=setup(async (url, options)=>{calls.push([url,options]);return sample('index:sh000688');});
  await controller.load({seriesId:'index:sh000688',period:'30d'});
  assert.deepEqual(calls,[['/api/v1/market/turnover/daily?series_id=index%3Ash000688&period=30d',undefined]]);
});

test('a late response cannot replace the newer series selection', async () => {
  const resolvers=[];
  const {controller,element}=setup(()=>new Promise(resolve=>resolvers.push(resolve)));
  const first=controller.load(); const second=controller.load({seriesId:'index:sh000688'});
  resolvers[1](sample('index:sh000688')); await second;
  resolvers[0](sample()); await first;
  assert.match(element.innerHTML,/科创50 · 2026-06-11/);
  assert.doesNotMatch(element.innerHTML,/沪深成交额参考合计 · 2026-06-11/);
});

test('history read failure preserves existing curve and recovers on retry', async () => {
  let fail=false;
  const {controller,element}=setup(async ()=>{if(fail)throw new Error('读取失败'); return sample();});
  await controller.load(); fail=true; await controller.load();
  assert.match(element.innerHTML,/保留上次曲线/);
  assert.match(element.innerHTML,/2 个交易日记录/);
  fail=false; await controller.load();
  assert.doesNotMatch(element.innerHTML,/读取失败/);
});

test('refresh notifies history independently without letting its failure erase a quote', async () => {
  const {create:quoteCreate}=require('../../app/static/market-intraday.js');
  let notified=0;
  const element={innerHTML:'',addEventListener(){},setAttribute(){}};
  const controller=quoteCreate({element,turnover:true,request:async()=>({}),onUpdated:()=>{notified++;throw new Error('history failed');}});
  await controller.load(); assert.equal(notified,0);
  await controller.load({refresh:true}); await Promise.resolve();
  assert.equal(notified,1);
  assert.doesNotMatch(element.innerHTML,/history failed/);
});

test('redraw and disposal release ECharts instances and ResizeObservers', async () => {
  let created=0, disposed=0, observed=0, disconnected=0;
  const original=globalThis.ResizeObserver;
  globalThis.ResizeObserver=class {observe(){observed++;}disconnect(){disconnected++;}};
  try {
    const element={innerHTML:'',addEventListener(){},removeEventListener(){},setAttribute(){},querySelector(){return {};}};
    const controller=create({element,request:async()=>sample(),echarts:{init(){created++;return {setOption(){},resize(){},dispose(){disposed++;}};}}});
    await controller.load(); await controller.load(); controller.dispose();
    assert.equal(created,3); assert.equal(disposed,created); assert.equal(disconnected,observed);
  } finally { globalThis.ResizeObserver=original; }
});

test('daily manual refresh uses its own POST and blocks repeated clicks', async () => {
  const calls=[]; let resolve;
  const {controller}=setup((url,options)=>{calls.push([url,options]);return new Promise(r=>{resolve=r;});});
  const first=controller.load({refresh:true});
  await controller.load({refresh:true});
  assert.equal(calls.length,1);
  assert.match(calls[0][0],/turnover\/daily\/refresh\?/);
  assert.deepEqual(calls[0][1],{method:'POST'});
  resolve(sample()); await first;
});
