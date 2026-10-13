/* 季报页面契约：只读进入、版本草稿、慢响应隔离和错误恢复。 */
const {test}=require('node:test');
const assert=require('node:assert/strict');
const {JSDOM}=require('../../miniprogram/node_modules/jsdom');
const {create}=require('../../app/static/fund-reports.js');
const routes=require('../../app/static/workspace-pages.js');
const tick=()=>new Promise(r=>setImmediate(r));
function fixture(code='110020') {
  const documents=[1,2].map(i=>({id:`fr_${i}`,fund_code:code,period:`2026-Q${i}`,published_at:'2026-07-20',title:`季报${i}`,page_count:20,semantic_ready:true}));
  return {fund_code:code,documents,selected_ids:documents.map(d=>d.id),allocation_changes:[{label:'基金投资',before:{ratio_percent:'90.00',page:12},after:{ratio_percent:'92.12',page:12},change_percentage_points:'2.12'}],limitations:['历史披露，不代表实时持仓']};
}
function harness(request) {
  const dom=new JSDOM('<section id="reports"></section>',{url:'https://example.test'});
  const element=dom.window.document.querySelector('section'),asks=[];
  global.localStorage=dom.window.localStorage;
  return {element,asks,page:create({element,request,userId:()=> 'test',waitJob:async()=>({status:'failed',error:'下载失败'}),ask:(...args)=>asks.push(args)}),
    click(selector){element.querySelector(selector).click();}};
}
test('季度报告是基金详情的独立子页面，不增加一级工作区',()=>{
  assert.equal(routes.routeHash('overview','reports'),'#assets/fund/reports');
  assert.deepEqual(routes.readRoute('#assets/fund/reports'),{view:'overview',page:'reports'});
  assert.equal(Object.keys(routes.destinations).length,5);
});
test('进入仅GET，原文页仅GET，AI按钮只传精确的两期草稿',async()=>{
  const calls=[];const h=harness(async(url,options)=>{calls.push({url,options});return url.includes('/pages/')?{...fixture().documents[0],page:12,text:'原文',source_url:'https://example.test/report.pdf#page=12'}:fixture();});
  await h.page.show({fund_code:'110020',short_name:'测试基金'});
  assert.equal(calls.length,2);assert.ok(calls.every(c=>!c.options));assert.equal(h.asks.length,0);
  h.click('[data-ask]');assert.deepEqual(h.asks[0][1].documents.map(d=>d.id),['fr_1','fr_2']);
  h.click('[data-document="0"]');await tick();assert.match(h.element.textContent,/第 12/);
  assert.equal(calls.length,3);assert.ok(calls.every(c=>!c.options));
});
test('外部标题和原文转义，缺失数值不填零',async()=>{
  const data=fixture();data.documents[0].title='<img src=x onerror=alert(1)>';data.allocation_changes[0].before=null;data.allocation_changes[0].change_percentage_points=null;
  const h=harness(async()=>data);await h.page.show({fund_code:'110020',short_name:'<script>bad</script>'});
  assert.equal(h.element.querySelectorAll('script,img').length,0);assert.match(h.element.textContent,/未提取/);assert.match(h.element.textContent,/不可计算/);
});
test('跨基金晚到响应不会替换新基金或AI草稿',async()=>{
  let resolve;const h=harness(url=>url.includes('110020')?new Promise(r=>resolve=r):Promise.resolve(fixture('000001')));
  const a=h.page.show({fund_code:'110020',short_name:'旧基金'});await h.page.show({fund_code:'000001',short_name:'新基金'});
  resolve(fixture());await a;assert.match(h.element.textContent,/新基金/);assert.doesNotMatch(h.element.textContent,/旧基金/);
  h.click('[data-ask]');assert.equal(h.asks[0][0].fund_code,'000001');
});
test('固定历史编号不替换为最新编号，后台失败不显示同步成功',async()=>{
  const calls=[];const h=harness(async(url,options)=>{calls.push({url,options});return options?{job:{id:'job-test'}}:fixture();});
  await h.page.show({fund_code:'110020'},{ids:['old1','old2']});assert.match(calls[1].url,/document_ids=old1&document_ids=old2/);
  h.click('[data-sync]');await tick();assert.match(h.element.textContent,/下载失败/);assert.doesNotMatch(h.element.textContent,/两期季报已保存/);
  assert.equal(calls.filter(c=>c.options?.method==='POST').length,1);
});
