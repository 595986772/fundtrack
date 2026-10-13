// 新简报页的读写边界、迟到响应和轮询均用假 API 验证，不启动收费任务。
const test = require('node:test'), assert = require('node:assert/strict');
const {JSDOM} = require('../../miniprogram/node_modules/jsdom');
const {create} = require('../../app/static/daily-briefings');
const fixture = (enabled = false, runs = []) => ({policy:{enabled}, runs, calendar_status:'available'});
const context = () => ({user:'chen', portfolio:'portfolio_a'});

test('打开和重读只 GET，默认关闭，选择组合才有页面', async () => {
  const calls=[];
  const page=create({context, changed(){}, request:async (...args)=>{calls.push(args);return fixture();}});
  await page.load(); await page.load();
  assert.equal(calls.length,2);assert.ok(calls.every(item=>item[1]===undefined));
  const doc=new JSDOM(page.render()).window.document;
  assert.equal(doc.querySelector('[data-briefing-enabled]').checked,false);
  assert.match(doc.body.textContent,/暂无简报/);
  assert.match(doc.body.textContent,/不是|并非/);
});
test('修改策略不会提交研究；明确运行才 POST，重复点击只提交一次', async () => {
  let release, sends=0; const calls=[];
  const page=create({context, changed(){}, visible:()=>false, request:async(url,opts)=>{
    calls.push({url,opts});
    if(opts?.method==='PUT')return fixture(JSON.parse(opts.body).enabled);
    if(opts?.method==='POST'){sends++;return new Promise(resolve=>{release=resolve;});}
    return fixture();
  }});
  await page.load(); await page.configure(true);
  assert.equal(calls.filter(item=>item.opts?.method==='POST').length,0);
  const first=page.run(); await page.run();assert.equal(sends,1);
  release({accepted:false});await first;
  assert.match(page.render(),/未重复创建或收费/);
});
test('开关按 JSON 请求保存，重新读取保持状态，开关本身不启动研究', async () => {
  let enabled=false; const methods=[];
  const page=create({context, changed(){}, visible:()=>false, request:async(url,options)=>{
    // Request 会像浏览器一样为未声明类型的字符串加 text/plain，捕获本次遗漏。
    const outgoing=new Request(`http://localhost${url}`,options);
    methods.push(outgoing.method);
    if(outgoing.method==='PUT'){
      assert.equal(outgoing.headers.get('content-type'),'application/json');
      const body=await outgoing.json();
      assert.equal(body.user_id,'chen');assert.equal(typeof body.enabled,'boolean');
      enabled=body.enabled;
    }
    return fixture(enabled);
  }});
  await page.load();
  for(const next of [true,false]){
    await page.configure(next);await page.load();
    const doc=new JSDOM(page.render()).window.document;
    assert.equal(doc.querySelector('[data-briefing-enabled]').checked,next);
    assert.equal(doc.querySelector('[role="alert"]'),null);
    assert.equal(enabled,next);
  }
  assert.deepEqual(methods,['GET','PUT','GET','PUT','GET']);
});
test('切换身份或组合后，旧查询和旧写入回执都不会污染新上下文',async()=>{
  let c=context(); const pending=[];
  const page=create({context:()=>c,changed(){},request:()=>new Promise(resolve=>pending.push(resolve))});
  const old=page.load();c={user:'bob',portfolio:'portfolio_b'};const fresh=page.load();
  pending[1](fixture(false,[{trade_date:'新组合',status:'failed',progress_message:'新消息'}]));await fresh;
  pending[0](fixture(true,[{trade_date:'旧组合',status:'completed',headline:'旧消息'}]));await old;
  assert.match(page.render(),/新消息/);assert.doesNotMatch(page.render(),/旧消息/);
});
test('来源文字转义，失败不标完成；完成报告指向已有查看追问入口',async()=>{
  const page=create({context,changed(){},visible:()=>false,request:async()=>fixture(false,[
    {job_id:'old',trade_date:'2026-09-17',status:'completed',model_used:false,headline:'<script>unsafe</script>',
     sources:[{label:'<img>',status:'partial',finished_at:'2026-09-17T06:30:00Z'}],warnings:['缺少部分来源']},
    {job_id:'failed',trade_date:'2026-09-16',status:'failed',gate:{status:'blocked'},error:'数据不足'},
  ])});
  await page.load();const doc=new JSDOM(page.render()).window.document;
  assert.equal(doc.querySelectorAll('script,img').length,0);
  assert.equal(doc.querySelectorAll('[data-review-report]').length,1);
  assert.equal(doc.querySelector('[data-review-report]').dataset.reviewReport,'old');
  assert.match(doc.body.textContent,/规则回退/);assert.match(doc.body.textContent,/部分可用/);
  assert.match(doc.body.textContent,/14:30/);assert.match(doc.body.textContent,/数据不足/);
});
test('页面隐藏后不再发轮询请求，暂停清理计时器',async()=>{
  let visible=true,calls=0,callback,cleared=0;
  const page=create({context,changed(){},visible:()=>visible,schedule:fn=>{callback=fn;return 1;},unschedule:()=>{cleared++;},
    request:async()=>{calls++;return fixture(true,[{status:'queued'}]);}});
  await page.load();visible=false;callback();assert.equal(calls,1);
  visible=true;await page.load();page.pause();assert.ok(cleared>0);
});
test('写请求失败恢复按钮且保留旧策略，不假报已启用',async()=>{
  const page=create({context,changed(){},request:async(url,opts)=>{if(opts)throw new Error('服务不可用');return fixture();}});
  await page.load();await page.configure(true);const doc=new JSDOM(page.render()).window.document;
  assert.equal(doc.querySelector('[data-briefing-enabled]').checked,false);
  assert.equal(doc.querySelector('[data-briefing-enabled]').disabled,false);
  assert.equal(doc.querySelector('[role="alert"]').textContent,'服务不可用');
});
