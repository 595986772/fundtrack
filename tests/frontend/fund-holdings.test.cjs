// 单基金持仓页只使用假请求，覆盖重绘、快速切换和不可信来源文本。
const test = require('node:test');
const assert = require('node:assert/strict');
const {render,create} = require('../../app/static/fund-holdings.js');

function fixture(code = '000001') {
  return {fund_code:code, short_name:'测试基金', disclosure:{data_mode:'database',freshness_status:'fresh',
    oldest_report_date:'2026-06-30',latest_report_date:'2026-06-30',fetched_at:null,source:'Provider',source_url:'https://example.com',
    report_dates:{stock:'2026-06-30',bond:null,industry:null},warnings:[],industry_coverage_pct:0,industry_allocations:[],
    security_holdings:[{security_code:'600001',security_name:'已披露股票',asset_type:'stock',weight_pct:15,report_date:'2026-06-30',source_url:'https://example.com/stock'}]},
    asset_evidence:[{label:'已披露股票',weight_pct:15,report_date:'2026-06-30'},{label:'现金及其他资产',weight_pct:null,report_date:null}],
    security_coverage_pct:15,unresolved_weight_pct:85,concentration:{top_five_disclosed_stocks_pct:15},comparisons:[],limitations:['非实时仓位']};
}
function harness(request) {
  const listeners = {};
  const element = {innerHTML:'',addEventListener:(type,fn)=>{listeners[type]=fn;},querySelector:()=>null};
  const asks = [];
  return {element,asks,page:create({element,request,ask:(...args)=>asks.push(args)}), click(selector,dataset={}) {
    listeners.click({target:{closest:query=>query===selector?{dataset}:null}});
  }};
}
test('missing assets are unknown, dates and source remain visible, and no portfolio weight appears',()=>{
  const html = render({fund:{fund_code:'000001',short_name:'测试基金'},data:fixture(),section:'overview'});
  assert.match(html,/现金及其他资产/); assert.match(html,/<dd>未披露<\/dd>/);
  assert.match(html,/2026-06-30/); assert.match(html,/15.00%/);
  assert.doesNotMatch(html,/占组合净值/);
});
test('external names and unsafe links cannot execute scripts',()=>{
  const data=fixture(); data.disclosure.security_holdings[0].security_name='<script>alert(1)</script>';
  data.disclosure.security_holdings[0].source_url='javascript:alert(1)';
  const html=render({fund:{fund_code:'000001'},data,section:'overview'});
  assert.match(html,/&lt;script&gt;/); assert.doesNotMatch(html,/href="javascript:|<script>/);
});
test('ETF limits appear before percentages and collection time is explicitly Beijing time',()=>{
  const data=fixture();data.lookthrough_status='target_etf_unavailable';data.disclosure.fetched_at='2026-09-05T03:11:00Z';
  const html=render({fund:{fund_code:'000001'},data,section:'overview'});
  assert.ok(html.indexOf('尚未穿透目标 ETF') < html.indexOf('同报告期证券覆盖'));
  assert.match(html,/11:11（北京时间）/);assert.match(html,/覆盖 未知/);
});
test('create and tab navigation do not query or call AI; repeated show reuses the result',async()=>{
  let reads=0; const h=harness(async code=>{reads++;return fixture(code);});
  assert.equal(reads,0);
  await h.page.show({fund_code:'000001'}); await h.page.show({fund_code:'000001'});
  h.click('[data-holdings-section]',{holdingsSection:'changes'});
  assert.equal(reads,1); assert.equal(h.asks.length,0);
  h.click('[data-holdings-ask]'); assert.equal(h.asks.length,1);
  await h.page.show({fund_code:'000001'},{refresh:true}); assert.equal(reads,2);
});
test('slow response from A cannot replace B or become B AI context',async()=>{
  const resolve = {}; const h=harness(code=>new Promise(done=>{resolve[code]=done;}));
  const a=h.page.show({fund_code:'000001',short_name:'基金A'});
  const b=h.page.show({fund_code:'000002',short_name:'基金B'});
  resolve['000002'](fixture('000002')); await b;
  resolve['000001'](fixture()); await a;
  assert.match(h.element.innerHTML,/基金B/); assert.doesNotMatch(h.element.innerHTML,/基金A/);
  h.click('[data-holdings-ask]'); assert.equal(h.asks[0][1].fund_code,'000002');
});
test('failed read can retry without reusing an old fund result',async()=>{
  let fail=true; const h=harness(async code=>{if(fail)throw new Error('测试读取失败');return fixture(code);});
  await h.page.show({fund_code:'000001'}); assert.match(h.element.innerHTML,/测试读取失败/);
  fail=false; await h.page.show({fund_code:'000001'}); assert.match(h.element.innerHTML,/已披露股票/);
  fail=true; await h.page.show({fund_code:'000002'}); assert.doesNotMatch(h.element.innerHTML,/已披露股票/);
});
test('new or removed disclosure rows use unknown rather than fabricated zero and percentage changes',()=>{
  const data=fixture(); data.comparisons=[{kind:'stock',status:'available',message:'榜单变化不等于买卖',report_date:'2026-06-30',previous_report_date:'2025-12-31',rows:[{key:'600002',name:'新披露',status:'newly_disclosed',previous_weight_pct:null,weight_pct:5,change_pp:null}]}];
  const html=render({fund:{fund_code:'000001'},data,section:'changes'});
  assert.match(html,/变化（百分点）/);assert.match(html,/不可计算/);assert.doesNotMatch(html,/>0.00%/);
  assert.match(html,/本期新披露/);
});
