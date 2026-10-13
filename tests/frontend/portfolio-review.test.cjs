// 只读汇总模块独立测试：不接触真实基金、网络或模型。
const {test}=require('node:test');
const assert=require('node:assert/strict');
const {create}=require('../../app/static/portfolio-review');
test('同一组合更正流水后按新修订读取，不再复用旧收益缓存',async()=>{
  let revision='before',reads=0;
  const page=create({context:()=>({user:'chen',portfolio:'same',revision}),changed(){},request:async()=>{
    reads++;return {portfolio:{holdings:[]},assessment:{summary:revision}};
  }});
  await page.load(); await page.load(); assert.equal(reads,1);
  revision='after'; await page.load(); assert.equal(reads,2);
  assert.match(page.render(),/after/); assert.doesNotMatch(page.render(),/>before</);
});
test('缺失指标与零值分开，风险证据转义，打开页面只读取摘要',async()=>{
  const calls=[];
  const page=create({context:()=>({user:'chen',portfolio:'fixture'}),changed(){},request:async url=>{
    calls.push(url);return {portfolio:{holdings:[{}]},performance:{twr_pct:0,observation_count:2},
      assessment:{risk_evidence:[{title:'<script>',explanation:'来源说明',evidence_id:'risk:1'}]},gaps:[{section:'performance',message:'净值不足'}],latest_report:{job_id:'report-old'}};
  }});
  await page.load();await page.load();const html=page.render();
  assert.equal(calls.length,1);assert.match(calls[0],/\/review\?.*months=12/);
  assert.match(html,/0.00%/);assert.match(html,/--/);assert.match(html,/2 个观测日/);
  assert.match(html,/&lt;script&gt;/);assert.match(html,/来源说明/);assert.match(html,/净值不足/);
  assert.match(html,/data-review-report="report-old"/);
  await page.setMonths(6);assert.match(calls[1],/months=6/);
});
test('切换用户账本后晚到响应不污染新页面，手动重读不产生写请求',async()=>{
  let context={user:'chen',portfolio:'one'};const pending=[];
  const page=create({context:()=>context,changed(){},request:url=>new Promise(resolve=>pending.push({url,resolve}))});
  const old=page.load();context={user:'alice',portfolio:'two'};const current=page.load();
  pending[1].resolve({portfolio:{},assessment:{summary:'新账本'}});await current;
  pending[0].resolve({portfolio:{},assessment:{summary:'旧账本'}});await old;
  assert.match(page.render(),/新账本/);assert.doesNotMatch(page.render(),/旧账本/);
  const refresh=page.load({force:true});assert.match(pending[2].url,/two\/review\?user_id=alice/);
  pending[2].resolve({portfolio:{}});await refresh;
});
test('Web 曲线读取与摘要独立，曲线失败仍显示已知指标与缺口',async()=>{
  let attempts=0;
  const page=create({context:()=>({user:'test',portfolio:'fixture'}),changed(){},
    request:async()=>({portfolio:{holdings:[]},performance:{twr_pct:2,observation_count:4},assessment:{summary:'风险摘要'}}),
    readPerformance:async(c,months)=>{assert.equal(c.user,'test');assert.equal(months,12);attempts++;throw new Error('曲线超时');}});
  await page.load();assert.match(page.render(),/\+2.00%/);assert.match(page.render(),/曲线读取失败：曲线超时/);
  assert.match(page.render(),/风险摘要/);assert.equal(attempts,1);
});
test('Web 图表和标题只接收当前账本的同区间完整业绩',async()=>{
  const pending=[];let context={user:'a',portfolio:'one'};
  const page=create({context:()=>context,changed(){},request:async()=>({portfolio:{holdings:[]}}),
    readPerformance:()=>new Promise(resolve=>pending.push(resolve))});
  const old=page.load();context={user:'b',portfolio:'two'};const fresh=page.load();
  pending[1]({performance_label:'新曲线',series:[]});await fresh;
  pending[0]({performance_label:'旧曲线',series:[]});await old;
  assert.match(page.render(),/新曲线/);assert.doesNotMatch(page.render(),/旧曲线/);
});
