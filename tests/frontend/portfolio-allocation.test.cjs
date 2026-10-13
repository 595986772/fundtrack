// UI契约：草案与确认分离，编辑撤销确认，异步返回不穿越组合边界。
const test = require('node:test');
const assert = require('node:assert/strict');
const ui = require('../../app/static/portfolio-allocation.js');
globalThis.FundScopePortfolioStress = require('../../app/static/portfolio-stress.js');
const draft = () => ({ draft_id: 'alloc_0123456789abcdef', draft_hash:'a'.repeat(64), input_hash:'b'.repeat(64),
  source_run_id:'stress_0123456789abcdef', status:'awaiting_confirmation', question:'债券至少40%', portfolio_name:'测试组合',
  created_at:'2026-09-08T01:00:00Z', captured_at:'2026-09-08T01:00:00Z', holdings:[], classifications:[], limitations:[], result:null,
  sample:{period_start:'2023-01-01',period_end:'2023-12-31',eligible_count:242,excluded_count:1,calendar_note:'共同日期可能缺失'},
  window:{period_start:'2023-10-01',period_end:'2023-10-30'},data_sources:[{fund_code:'110020',return_basis_label:'分红再投资总回报',source:'本地数据',data_quality_warnings:['部分日期缺失']}],
  proposal:{min_bond_weight_pct:40,max_single_fund_weight_pct:70,max_turnover_pct:null,questions:['最多换手多少？'],unsupported_constraints:[]} });
const readyState = () => ({...ui.initialState(), draft:draft(), question:draft().question, source:draft().source_run_id,
  values:{min_bond_weight_pct:40,max_single_fund_weight_pct:70,max_turnover_pct:20}, confirmed:true});
function host() {
  const ack={checked:false}, button={disabled:true};
  return { innerHTML:'', querySelector: selector => selector === '[data-allocation-ack]' ? ack : selector === '[data-allocation-confirm-button]' ? button : null };
}
function controller(request) {
  const ctx={userId:'chen',portfolioId:'portfolio_0123456789abcdef'}, element=host();
  const instance=ui.create({request,getContext:()=>ctx,refreshIcons(){},askAgent(){}});instance.mount(element);
  return {ctx,element,instance};
}
function input(element, dataset, value) { element.oninput({target:{dataset,value}}); }
function ack(element) {
  const checkbox=element.querySelector('[data-allocation-ack]'); checkbox.checked=true;
  // 模拟浏览器顺序：先input再change，不能在input阶段清掉刚勾选的状态。
  element.oninput({target:{dataset:{},checked:true}});
  element.onchange({target:{checked:checkbox.checked,matches:s=>s==='[data-allocation-ack]'}});
}
test('confirmation requires exact reviewed draft and finite complete constraints',()=>{
  assert.equal(ui.canConfirm(readyState()),true);
  for(const change of [{confirmed:false},{busy:true},{question:'其他需求'},{source:'stress_other'},{values:{}}])
    assert.equal(ui.canConfirm({...readyState(),...change}),false);
  const s=readyState(); s.values.max_turnover_pct=NaN;assert.equal(ui.canConfirm(s),false);
  s.values.max_turnover_pct=20;s.draft.proposal.unsupported_constraints=['收益20%'];assert.equal(ui.canConfirm(s),false);
});
test('escaped content and pending state never show candidate success',()=>{
  const s=readyState();s.draft.question='<script>alert(1)</script>';
  const html=ui.render(s);assert.doesNotMatch(html,/<script>/);assert.match(html,/&lt;script/);
  assert.match(html,/确认条件并求解/);assert.doesNotMatch(html,/已完成求解/);
  assert.match(html,/2023-01-01 至 2023-12-31/);assert.match(html,/分红再投资总回报/);assert.match(html,/部分日期缺失/);
});
test('mount and remount never call a model or fetch',()=>{
  let calls=0;const {instance}=controller(()=>{calls++;});instance.mount(host());assert.equal(calls,0);
});
test('draft creation then explicit checkbox confirmation sends exact hash',async()=>{
  const calls=[];const {instance,element}=controller(async(url,options)=>{calls.push({url,body:JSON.parse(options.body)});return draft();});
  input(element,{allocationField:'question'},draft().question);
  await instance.execute('new'); await instance.execute('confirm');assert.equal(calls.length,1);
  input(element,{allocationValue:'max_turnover_pct'},'20');ack(element);
  assert.equal(element.querySelector('[data-allocation-confirm-button]').disabled,false);
  await instance.execute('confirm');assert.equal(calls.length,2);
  assert.match(calls[1].url,/\/confirm$/);assert.equal(calls[1].body.draft_hash,draft().draft_hash);
  assert.equal(calls[1].body.constraints.max_turnover_pct,20);
});
test('editing a confirmed number resets checkbox before submit',async()=>{
  const calls=[];const {instance,element}=controller(async()=>{calls.push(1);return draft();});
  await instance.execute('read',draft().draft_id);
  input(element,{allocationValue:'max_turnover_pct'},'20');ack(element);
  input(element,{allocationValue:'max_turnover_pct'},'30');await instance.execute('confirm');
  assert.equal(calls.length,1);assert.equal(element.querySelector('[data-allocation-ack]').checked,false);
});
test('creation retry uses same key and shows failure without inventing result',async()=>{
  const bodies=[];const {instance,element}=controller(async(_,options)=>{bodies.push(JSON.parse(options.body));throw Error('模型不可用');});
  input(element,{allocationField:'question'},'降低组合历史风险');
  await instance.execute('new');await instance.execute('new');
  assert.equal(bodies[0].idempotency_key,bodies[1].idempotency_key);assert.match(element.innerHTML,/模型不可用/);
});
test('inflight result discarded after switching portfolio',async()=>{
  let resolve;const {ctx,instance}=controller(()=>new Promise(done=>resolve=done));
  const p=instance.execute('read',draft().draft_id);ctx.portfolioId='portfolio_ffffffffffffffff';
  const next=host();instance.mount(next);resolve(draft());await p;
  assert.match(next.innerHTML,/尚无约束草案/);assert.doesNotMatch(next.innerHTML,/alloc_0123/);
});
test('history reads are explicit and do not submit confirmation',async()=>{
  const calls=[];const {instance}=controller(async(url,options)=>{calls.push({url,options});return [];});
  await instance.execute('history');assert.equal(calls[0].options.method,undefined);
  assert.match(calls[0].url,/user_id=chen/);
});
test('source edit immediately displays stale warning without replacing DOM',async()=>{
  const {instance,element}=controller(async()=>draft());await instance.execute('read',draft().draft_id);
  const warning={hidden:true};const query=element.querySelector;
  element.querySelector=s=>s==='[data-allocation-stale]'?warning:query(s);
  input(element,{allocationField:'source'},'stress_ffffffffffffffff');assert.equal(warning.hidden,false);
  input(element,{allocationField:'source'},draft().source_run_id);assert.equal(warning.hidden,true);
});
test('outer remount reuses original DOM including input selection and disclosure',()=>{
  const {instance,element}=controller(()=>{});element.selectionStart=3;element.selectionEnd=5;element.detailsOpen=true;
  let moved;const next={replaceWith(node){moved=node;}};
  instance.mount(next);assert.equal(moved,element);assert.equal(moved.selectionStart,3);assert.equal(moved.selectionEnd,5);assert.equal(moved.detailsOpen,true);
});
test('composition marks the workbench busy editing; context change releases it',()=>{
  const {instance,element,ctx}=controller(()=>{});
  element.oncompositionstart();assert.equal(instance.isEditing(),true);
  element.oncompositionend();assert.equal(instance.isEditing(),false);
  element.oncompositionstart();ctx.portfolioId='portfolio_ffffffffffffffff';assert.equal(instance.isEditing(),false);
});
