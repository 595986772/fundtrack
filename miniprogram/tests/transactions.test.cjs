/** 使用真实页面/网络封装验证选基与记账边界；所有响应为测试替身，不写用户持仓。 */
const {test,beforeEach}=require('node:test');
const assert=require('node:assert/strict');
const api=require('../dist/lib/api');
const selection=require('../dist/lib/trade-selection');
const {orderTimePayload,beijingToday}=require('../dist/lib/trade-timing');
const {tradePayload}=require('../dist/lib/ledger');
const id='portfolio_1234567890abcdef';
const fund=(code,name)=>({fund_code:code,short_name:name,fund_type:'混合型',latest_nav:1.2,latest_nav_date:'2026-09-10'});
const a=fund('110020','已有基金'),b=fund('008888','未持有的半导体基金'),c=fund('015789','新导入基金');
let storage,calls,nav,loadedFunds,holdings,handler,modal;
const reply=(o,data)=>o.success({statusCode:200,data});
const tick=()=>new Promise(setImmediate);
function page(name='transactions'){
  let definition;global.Page=d=>definition=d;
  delete require.cache[require.resolve(`../dist/pages/${name}/index`)];require(`../dist/pages/${name}/index`);
  const p={...definition,data:structuredClone(definition.data),setData(v){Object.assign(this.data,v);}};
  if(name==='transactions')p.onLoad({id});return p;
}
beforeEach(()=>{
  storage=new Map();calls=[];nav=[];loadedFunds=[a,b];holdings=[{...a,units:100}];modal=o=>o.success({confirm:true});
  handler=o=>reply(o,o.url.endsWith('/auth/me')?{authenticated:false,auth_required:false}:o.url.endsWith('/funds')?{funds:loadedFunds}:
    o.url.includes('/transactions')?o.method==='POST'?{}:{transactions:[]}:o.url.includes('/portfolios?')?{portfolios:[{id,name:'测试组合',position_mode:'actual',holdings}]}:{id,name:'测试组合',position_mode:'actual',holdings});
  global.wx={getStorageSync:k=>storage.get(k),setStorageSync:(k,v)=>storage.set(k,structuredClone(v)),removeStorageSync:k=>storage.delete(k),
    request(o){calls.push(o);handler(o);},showModal:o=>modal(o),showToast(){},switchTab:o=>nav.push(o.url),navigateTo:o=>nav.push(o.url)};
});

test('买入列出整个已导入基金库，卖出只列正份额持仓',async()=>{
  holdings.push({...b,units:0});const p=page();await p.onShow();
  assert.deepEqual(p.data.funds.map(f=>f.fund_code),['110020','008888']);
  assert.match(p.data.funds[1].optionLabel,/008888.*半导体/);
  p.type({currentTarget:{dataset:{value:'sell'}}});assert.deepEqual(p.data.funds.map(f=>f.fund_code),['110020']);
  p.type({currentTarget:{dataset:{value:'buy'}}});assert.equal(p.data.funds.length,2);
  assert.ok(calls.every(c=>c.method==='GET'));
});
test('返回交易页重新读取新导入基金，按代码恢复选择而非沿用旧索引',async()=>{
  const p=page();await p.onShow();p.choose({detail:{value:1}});
  p.setData({form:{...p.data.form,units:'15',unit_price:'2.01',note:'已确认成交'}});
  p.onHide();loadedFunds=[c,b,a];await p.onShow();
  assert.equal(p.data.funds.length,3);assert.equal(p.data.fundIndex,1);assert.equal(p.data.form.fund_code,'008888');assert.equal(p.data.form.units,'15');
  p.onHide();loadedFunds=[b,c,a];await p.onShow();assert.equal(p.data.fundIndex,0);assert.equal(p.data.form.fund_code,'008888');
});
test('切换基金或买卖方向清空旧份额与成交价，不拿最新净值充当成交价',async()=>{
  const p=page();await p.onShow();p.setData({form:{...p.data.form,units:'100',unit_price:'1.5',fee:'1'}});
  p.choose({detail:{value:1}});assert.equal(p.data.form.units,'');assert.equal(p.data.form.unit_price,'');assert.equal(p.data.form.fee,'0');
  p.setData({form:{...p.data.form,units:'20',unit_price:'3'}});p.type({currentTarget:{dataset:{value:'sell'}}});
  assert.equal(p.data.form.fund_code,'110020');assert.equal(p.data.form.units,'');assert.equal(p.data.form.unit_price,'');
});
test('无当前持仓不能提交卖出，仍可从基金库选择买入',async()=>{
  holdings=[];const p=page();await p.onShow();p.type({currentTarget:{dataset:{value:'sell'}}});
  assert.equal(p.data.funds.length,0);await p.save();assert.match(p.data.error,/可用基金/);assert.ok(calls.every(c=>c.method==='GET'));
  p.type({currentTarget:{dataset:{value:'buy'}}});assert.equal(p.data.funds.length,2);
});
test('买入页跳转基金库，选中未持有基金后带回原组合且不自动记账',async()=>{
  const p=page();await p.onShow();p.setData({form:{...p.data.form,units:'77',unit_price:'5'}});p.library();
  assert.equal(nav.at(-1),'/pages/assets/index');assert.equal(selection.currentTradeSelection().portfolioId,id);
  const assets=page('assets');assets.onShow();await tick();
  assert.equal(assets.data.segment,'funds');assert.equal(assets.data.tradeSelection.portfolioId,id);
  assets.libraryFund({currentTarget:{dataset:{code:'008888'}}});assets.onHide();
  assert.equal(nav.at(-1),`/pages/transactions/index?id=${id}`);
  const next=page();await next.onShow();assert.equal(next.data.form.fund_code,'008888');assert.equal(next.data.funds[next.data.fundIndex].fund_code,'008888');
  assert.equal(next.data.form.units,'');assert.equal(next.data.form.unit_price,'');assert.equal(selection.currentTradeSelection(),null);
  assert.ok(calls.every(c=>c.method==='GET'));
});
test('搜索目录未导入的基金不能被当成可用基金带回',async()=>{
  const p=page();await p.onShow();p.library();const assets=page('assets');assets.onShow();await tick();
  assets.libraryFund({currentTarget:{dataset:{code:'000001'}}});
  assert.match(assets.data.importError,/尚未导入完成/);assert.equal(nav.at(-1),'/pages/assets/index');assert.ok(selection.currentTradeSelection());assets.onHide();
});
test('从基金库不选基金直接返回，保留原表单且只恢复到原组合一次',async()=>{
  const p=page();await p.onShow();p.setData({form:{...p.data.form,units:'12',unit_price:'1.8',note:'我的备注'}});const form=structuredClone(p.data.form);p.library();
  selection.returnToTrade();assert.equal(selection.takeTradeSelection('other'),null);
  assert.deepEqual(selection.takeTradeSelection(id),form);assert.equal(selection.takeTradeSelection(id),null);
});
test('选基草稿与旧账号的异步响应均不串到另一个账号',async()=>{
  const p=page();await p.onShow();p.library();
  api.saveConnection({baseUrl:'http://127.0.0.1:8000',userId:'bob',token:''});assert.equal(selection.currentTradeSelection(),null);
  const pending=[];const original=handler;handler=o=>o.url.endsWith('/funds')?pending.push(o):original(o);
  const load=p.onShow();await tick();api.saveConnection({baseUrl:'http://127.0.0.1:8000',userId:'carol',token:''});reply(pending[0],{funds:[c]});await load;
  assert.deepEqual(p.data.funds,[]);assert.deepEqual(p.data.trades,[]);
});
test('离开页面后晚到的基金列表不能覆盖状态',async()=>{
  const p=page(),pending=[];const original=handler;handler=o=>o.url.endsWith('/funds')?pending.push(o):original(o);
  const load=p.onShow();await tick();p.onHide();reply(pending[0],{funds:[c]});await load;assert.equal(p.data.funds.length,0);
});
test('网络读取失败后可重读列表，保留原来的未提交成交表单',async()=>{
  const p=page();await p.onShow();p.choose({detail:{value:1}});p.setData({form:{...p.data.form,units:'12',unit_price:'1.8'}});
  const original=handler;handler=o=>o.url.endsWith('/funds')?o.fail({errMsg:'timeout'}):original(o);
  await p.refreshFunds();assert.equal(p.data.locked,true);assert.equal(p.data.form.units,'12');
  handler=original;await p.refreshFunds();assert.equal(p.data.locked,false);assert.equal(p.data.form.fund_code,'008888');assert.equal(p.data.form.units,'12');
});
test('确认后只提交所选未持有基金，不受旧索引影响，重复点击不重复记账',async()=>{
  const p=page();await p.onShow();p.choose({detail:{value:1}});p.setData({form:{...p.data.form,units:'12',unit_price:'1.8'}});
  let accept;modal=o=>{accept=()=>o.success({confirm:true});};const save=p.save();await tick();await p.save();
  assert.equal(calls.filter(c=>c.method==='POST').length,0);accept();await save;
  const posts=calls.filter(c=>c.method==='POST');assert.equal(posts.length,1);assert.equal(posts[0].data.fund_code,'008888');
  assert.equal(posts[0].data.units,12);assert.equal(posts[0].data.unit_price,1.8);assert.ok(posts[0].data.idempotency_key);assert.equal(p.data.saving,false);
});

const timing={status:'estimated',submitted_at:'2025-01-03T16:00:00+08:00',nav_date:'2025-01-06',confirmation_date:'2025-01-07',first_return_nav_date:'2025-01-07',last_return_nav_date:null,reason:'15:00后顺延',visibility:'待净值披露',warnings:[]};
function order(p){p.setData({form:{...p.data.form,submitted_date:'2025-01-03',submitted_time:'16:00'}});}
test('下单时间显式携带北京时间，空时间兼容旧记账，不接受半填和无效日期',()=>{
  assert.deepEqual(orderTimePayload({}),{});
  assert.equal(orderTimePayload({submitted_date:'2025-01-03',submitted_time:'16:00'}).submitted_at,'2025-01-03T16:00:00+08:00');
  for(const form of [{submitted_date:'2025-01-03'},{submitted_time:'14:00'},{submitted_date:'2025-02-30',submitted_time:'14:00'},{submitted_date:'2025-01-03',submitted_time:'25:00'}])assert.throws(()=>orderTimePayload(form));
  const original=Date.now;Date.now=()=>Date.parse('2026-09-10T16:30:00Z');try{assert.equal(beijingToday(),'2026-09-11');}finally{Date.now=original;}
});
test('收益日期按需预览，不下单、不自动改成交净值日或净值',async()=>{
  const p=page();await p.onShow();order(p);const form=structuredClone(p.data.form),prior=handler;
  handler=o=>o.url.endsWith('/transaction-timing')?reply(o,timing):prior(o);
  await p.previewTiming();assert.deepEqual(p.data.timing,timing);assert.deepEqual(p.data.form,form);
  assert.equal(calls.filter(o=>o.method==='POST').length,1);assert.ok(calls.at(-1).url.endsWith('/transaction-timing'));
  assert.equal(calls.at(-1).data.submitted_at,timing.submitted_at);
  p.applyNavDate();assert.equal(p.data.form.trade_date,'2025-01-06');assert.equal(p.data.form.unit_price,'');
});
test('切换基金、方向、渠道和时间清除旧估算，晚到结果不能覆盖',async()=>{
  const p=page();await p.onShow();order(p);let pending;const prior=handler;
  handler=o=>o.url.endsWith('/transaction-timing')?pending=o:prior(o);
  const task=p.previewTiming();p.choose({detail:{value:1}});reply(pending,timing);await task;assert.equal(p.data.timing,null);
  p.setData({timing});p.type({currentTarget:{dataset:{value:'sell'}}});assert.equal(p.data.timing,null);
  p.setData({timing});p.channel({detail:{value:1}});assert.equal(p.data.timing,null);
  p.setData({timing});p.field({currentTarget:{dataset:{field:'submitted_time'}},detail:{value:'14:00'}});assert.equal(p.data.timing,null);
  const late=p.previewTiming();p.onHide();reply(pending,timing);await late;assert.equal(p.data.timing,null);
});
test('空输入不请求估算，网络失败可重试，特殊规则或未来日期不能自动套用',async()=>{
  const p=page();await p.onShow();await p.previewTiming();assert.match(p.data.timingError,/请选择/);assert.ok(calls.every(c=>c.method==='GET'));order(p);
  const prior=handler;handler=o=>o.url.endsWith('/transaction-timing')?o.fail({errMsg:'timeout'}):prior(o);
  await p.previewTiming();assert.ok(p.data.timingError);assert.equal(p.data.timingLoading,false);
  handler=o=>o.url.endsWith('/transaction-timing')?reply(o,{...timing,status:'manual_required',nav_date:null}):prior(o);
  await p.previewTiming();assert.equal(p.data.timingError,'');const date=p.data.form.trade_date;p.applyNavDate();assert.equal(p.data.form.trade_date,date);
  p.setData({timing:{...timing,nav_date:'2099-01-01'}});p.applyNavDate();assert.equal(p.data.form.trade_date,date);
  p.clearOrderTime();assert.equal(p.data.timing,null);assert.deepEqual(orderTimePayload(p.data.form),{});
});
test('新记账提交原始时间而非客户端推算结果，不记录未来或早于下单的成交',async()=>{
  const p=page();await p.onShow();order(p);p.setData({form:{...p.data.form,units:'10',unit_price:'2',trade_date:'2025-01-06'}});
  await p.save();const post=calls.find(c=>c.method==='POST');assert.equal(post.data.submitted_at,'2025-01-03T16:00:00+08:00');
  assert.equal(post.data.timing,undefined);assert.equal(post.data.submitted_date,undefined);
  const form={...p.data.form,units:'10',unit_price:'2',trade_date:'2025-01-02',submitted_date:'2025-01-03',submitted_time:'16:00'};
  assert.throws(()=>tradePayload(form,'chen'),/不能早于/);
  assert.throws(()=>tradePayload({...form,submitted_date:'2099-01-01'},'chen'),/未来下单/);
});
test('流水显示已保存时间快照，旧流水不猜下单时刻',async()=>{
  const p=page(),prior=handler;
  handler=o=>o.url.includes('/transactions?')?reply(o,{transactions:[{id:'old',timing:null},{id:'new',transaction_type:'buy',timing}]}):prior(o);
  await p.onShow();assert.equal(p.data.trades[0].submittedText,'');assert.equal(p.data.trades[1].submittedText,'2025-01-03 16:00 北京时间');assert.equal(p.data.trades[1].returnDate,'2025-01-07');
});
test('刷新基金列表时取消旧估算，不留下无法重试的加载状态',async()=>{
  const p=page();await p.onShow();order(p);let pending;const prior=handler;
  handler=o=>o.url.endsWith('/transaction-timing')?pending=o:prior(o);
  const task=p.previewTiming();await p.refreshFunds();reply(pending,timing);await task;
  assert.equal(p.data.timing,null);assert.equal(p.data.timingLoading,false);
});
