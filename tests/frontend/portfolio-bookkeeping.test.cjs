// 纯本地验证状态按钮、精度字符串、草稿隔离和失败重试，不访问用户数据库。
const test = require('node:test');
const assert = require('node:assert/strict');
const ui = require('../../app/static/portfolio-bookkeeping.js');
let sequence = 0;
function context() {
  return {userId:'fixture',portfolio:{id:`portfolio-${++sequence}`,holdings:[{fund_code:'110020',short_name:'测试基金',units:1000,available_units:700,reserved_units:300}]},
    funds:[],orders:[],transactions:[],loading:false,error:'',current:()=>true, render(){},refresh:async()=>{},notify(){}, createKey:()=>`request-key-${++sequence}`};
}
test('order results respect current state; buying does not offer redemption settlement',()=>{
  assert.deepEqual(ui.allowed({status:'cancel_requested'}),['cancel_success','cancel_rejected','confirm','reject']);
  assert.deepEqual(ui.allowed({status:'confirmed',transaction_type:'buy'}),[]);
  assert.deepEqual(ui.allowed({status:'confirmed',transaction_type:'sell'}),['settle']);
  for(const status of ['cancelled','reversed','settled','rejected']) assert.deepEqual(ui.allowed({status}),[]);
});
test('render escapes external notes and retains correction evidence without delete buttons',()=>{
  const ctx = context(); ui.session(ctx).list = 'transactions';
  ctx.transactions=[{id:'txn-test',fund_code:'110020',units:10,unit_price:1,fee:0,cash_flow:-10,effective:false,status:'reversed',correction:{reason:'<script>alert(1)</script>'}}];
  const html=ui.render(ctx);
  assert.match(html,/已冲正/); assert.match(html,/&lt;script&gt;/);
  assert.doesNotMatch(html,/<script>|data-delete/);
});
test('amounts stay decimal strings and Beijing time is explicit',()=>{
  const ctx = context(), s=ui.session(ctx); s.mode='pending';
  const cmd=ui.command(ctx,s,{fund_code:'110020',transaction_type:'buy',requested_amount:'100.01',submitted_at:'2025-02-03T14:30'});
  assert.equal(cmd.body.requested_amount,'100.01');
  assert.equal(cmd.body.submitted_at,'2025-02-03T14:30:00+08:00');
  assert.equal(cmd.body.requested_units,undefined);
  s.mode='posted';
  assert.equal(ui.command(ctx,s,{units:'0.1234',unit_price:'1.234567',fee:'0.01'}).body.unit_price,'1.234567');
});
test('confirmation, reversal and settlement use distinct commands',()=>{
  const ctx=context(), s=ui.session(ctx);
  s.editor={kind:'order',id:'ord-test',action:'settle'};
  assert.deepEqual(ui.command(ctx,s,{settlement_date:'2025-02-06',reason:'回执'}).body,{action:'settle',reason:'回执',settlement_date:'2025-02-06'});
  s.editor={kind:'correction',id:'txn-test',action:'reverse'};
  assert.deepEqual(ui.command(ctx,s,{reason:'误录'}).body,{reason:'误录',replacement:null});
});
test('drafts are isolated by user and portfolio and no NAV is guessed',()=>{
  const ctx=context(), other={...ctx,userId:'someone-else'};
  ui.render(ctx); ui.render(other);
  ui.session(ctx).drafts.posted.units='12';
  assert.equal(ui.session(other).drafts.posted.units,undefined);
  assert.equal(ui.session(ctx).drafts.posted.unit_price,undefined);
});
test('unknown request result reuses key; draft survives until success; rapid double submit is ignored',async()=>{
  const ctx=context(), s=ui.session(ctx), requests=[];
  s.drafts.posted={fund_code:'110020',transaction_type:'buy',trade_date:'2025-02-03',units:'1',unit_price:'1.234567',fee:'0'};
  const event={preventDefault(){},target:{closest:()=>null}};
  let release;
  ctx.request=async(_url,options)=>{requests.push(JSON.parse(options.body));await new Promise(r=>release=r);throw Error('超时');};
  const pending=ui.submit(event,ctx);
  await ui.submit(event,ctx); assert.equal(requests.length,1);
  release(); await pending;
  assert.equal(s.drafts.posted.units,'1');
  ctx.request=async(_url,options)=>{requests.push(JSON.parse(options.body));return {idempotency_replayed:true};};
  await ui.submit(event,ctx);
  assert.equal(requests[0].idempotency_key,requests[1].idempotency_key);
  assert.equal(s.drafts.posted,undefined); assert.equal(s.retry,null);
});
test('late result cannot refresh or navigate a different active portfolio',async()=>{
  const ctx=context(); ctx.current=()=>false; let refreshed=false;
  ctx.refresh=async()=>{refreshed=true;}; ctx.request=async()=>({});
  await ui.submit({preventDefault(){},target:{closest:()=>null}},ctx);
  assert.equal(refreshed,false);
});
test('correction returns to ledger even when application entry was selected',async()=>{
  const ctx=context(),s=ui.session(ctx);s.mode='pending';s.editor={kind:'correction',id:'txn-one',action:'reverse'};
  s.drafts['correction:txn-one:reverse']={reason:'误录'};ctx.request=async()=>({});
  await ui.submit({preventDefault(){},target:{closest:()=>null}},ctx);
  assert.equal(s.list,'transactions');
});
