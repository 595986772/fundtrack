/* 离线验证评测任务控制：断网不重建付费任务，切用户不接收旧结果。 */
const {test} = require('node:test');
const assert = require('node:assert/strict');
const {JSDOM} = require('../../miniprogram/node_modules/jsdom');
const {createController, renderQuality, renderMetrics, renderPicker} = require('../../app/static/fund-report-evals.js');
const tick = () => new Promise(resolve => setImmediate(resolve));

function harness(api) {
  const entries = new Map(), results = [], errors = [], progress = [];
  let user = 'alice', keys = 0;
  const storage = {getItem:key=>entries.get(key), setItem:(key,value)=>entries.set(key,value), removeItem:key=>entries.delete(key)};
  const controller = createController({api,userId:()=>user,storage,onBusy:()=>{},onProgress:message=>progress.push(message),
    onResult:run=>results.push(run),onError:message=>errors.push(message),makeKey:()=>`unique-${++keys}`,sleep:tick});
  return {controller,results,errors,entries,progress,setUser:value=>{controller.pause();user=value;}};
}

test('显式启动后轮询同一任务，重复点击不重复创建', async () => {
  const calls = []; let accept;
  const h = harness(async(url,options)=>{
    calls.push({url,options});
    if (url.endsWith('/jobs')) return new Promise(resolve=>accept=resolve);
    if (url.startsWith('/background-jobs')) return {job:{status:'completed',current_step:3,total_steps:3,result:{eval_run_id:'eval_test'}}};
    return {id:'eval_test'};
  });
  assert.equal(calls.length,0);
  const first = h.controller.start(['report-index-allocation']);
  await h.controller.start(['report-active-equity']);
  assert.equal(calls.length,1);
  accept({job:{id:'job_test'}}); await first;
  assert.equal(h.results[0].id,'eval_test');
  assert.equal(h.controller.pending(),null);
});

test('受理响应丢失后恢复仍用相同幂等键，不生成新的付费请求身份', async () => {
  const payloads = []; let lost = true;
  const h = harness(async(url,options)=>{
    if (url.endsWith('/jobs')) {
      payloads.push(JSON.parse(options.body));
      if (lost) {lost=false;throw new Error('网络中断');}
      return {job:{id:'existing_job'}};
    }
    if (url.startsWith('/background-jobs')) return {job:{status:'completed',result:{eval_run_id:'eval_test'}}};
    return {id:'eval_test'};
  });
  await h.controller.start(['a'],'keyword');
  assert.ok(h.controller.pending());
  await h.controller.start(['b']);
  assert.equal(payloads.length,1);
  await h.controller.resume();
  assert.deepEqual(payloads[0],payloads[1]);
  assert.equal(h.results.length,1);
});

test('切用户后晚到的受理响应只保留在原用户，不显示为新用户结果', async () => {
  let accept; const calls=[];
  const h = harness((url)=>{calls.push(url);return new Promise(resolve=>accept=resolve);});
  const pending = h.controller.start(['a']);
  h.setUser('bob');
  accept({job:{id:'alice_job'}}); await pending;
  assert.equal(calls.length,1); assert.equal(h.results.length,0); assert.equal(h.controller.pending(),null);
  h.setUser('alice'); assert.equal(h.controller.pending().jobId,'alice_job');
});

test('任务失败不显示成功，取消发到同一任务且不新建任务', async () => {
  let poll;const calls=[];
  const h = harness(async(url,options)=>{
    calls.push({url,options});
    if (url.endsWith('/jobs')) return {job:{id:'job_test'}};
    if (url.endsWith('/cancel')) {poll({job:{status:'cancelled'}});return {};}
    return new Promise(resolve=>poll=resolve);
  });
  const pending=h.controller.start(['a']);await tick();await h.controller.cancel();await pending;
  assert.equal(h.results.length,0);assert.match(h.errors[0],/取消/);
  assert.equal(calls.filter(call=>call.url.endsWith('/jobs')).length,1);
  assert.equal(h.controller.pending(),null);
});

test('检索引用通过仍待人审，原文/模型输出转义且不接受任意来源链接', () => {
  const result={case_id:'a',report_quality:{reference_answer:'<img src=x onerror=alert(1)>',manual_review:{status:'pending'},
    gold_evidence:[{url:'javascript:alert(1)',quote:'<script>bad</script>',period:'Q1',page:1}],
    turns:[{question:'test',answer:'<svg onload=alert(1)>'.repeat(200)}],review_criteria:['核对观点是否有原文支持']}};
  const dom=new JSDOM(renderPicker('fund_report_rag_v1',false)+renderQuality(result,'eval_run',false));
  assert.equal(dom.window.document.querySelectorAll('script,img,svg,a').length,0);
  assert.match(dom.window.document.body.textContent,/待人工复核/);
  assert.equal(dom.window.document.querySelector('pre').textContent,result.report_quality.turns[0].answer);
  const real=renderQuality({...result,report_quality:{...result.report_quality,gold_evidence:[{
    url:'https://pdf.dfcfw.com/pdf/H2_AN202604221821399649_1.pdf#page=12',period:'2026-Q1',page:12,quote:'92.05'}]}},'eval',false);
  assert.equal(new JSDOM(real).window.document.querySelectorAll('a').length,1);
});

test('无适用项不显示虚假的100分；执行失败显示覆盖数量', () => {
  const text=new JSDOM(renderMetrics({total_cases:3,report_metrics:{graded_cases:1,total_cases:3,execution_failures:2}})).window.document.body.textContent;
  assert.match(text,/不适用/);assert.match(text,/完成评分 1\/3/);assert.match(text,/执行失败 2/);assert.doesNotMatch(text,/100/);
});
