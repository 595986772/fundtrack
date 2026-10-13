/** 使用实际 Page 和网络层，只替换 wx.request；不联网、不向用户账本添加基金。 */
const {test, beforeEach, afterEach} = require('node:test');
const assert = require('node:assert/strict');
const api = require('../dist/lib/api');
let page, calls, storage, handler;
const fund = {fund_code:'000001', short_name:'测试公募基金A', fund_type:'混合型', is_imported:false};
const saved = {...fund, latest_nav:1.2345, latest_nav_date:'2026-09-10', daily_growth_rate_pct:0.2};
const list = funds => ({funds, data_mode:'database', source:'test'});
const respond = (request, data) => request.success({statusCode:200, data});
const flush = () => new Promise(setImmediate);
beforeEach(() => {
  calls=[]; storage=new Map(); handler=() => {};
  global.wx = {
    getStorageSync:key => storage.get(key), setStorageSync:(key,value) => storage.set(key, structuredClone(value)),
    removeStorageSync:key => storage.delete(key), request:request => {calls.push(request); handler(request);},
  };
  let definition; global.Page = value => {definition=value;};
  delete require.cache[require.resolve('../dist/pages/assets/index')];
  require('../dist/pages/assets/index');
  page={...definition, data:structuredClone(definition.data), setData(patch) {Object.assign(this.data, patch);}};
  page.data.segment='funds'; page._loadedScope=api.scope(); page.data.catalogFunds=[fund];
});
afterEach(() => page.onHide());

test('输入和切换基金库不搜索，只有点击搜索才查询公募目录', async () => {
  page.catalogField({detail:{value:'华夏 & A'}});
  page.segment({currentTarget:{dataset:{value:'funds'}}});
  assert.equal(calls.length, 0);
  const pending=page.searchCatalog();
  assert.match(calls[0].url, /\/api\/v1\/funds\/search\?q=/);
  assert.equal(new URL(calls[0].url).searchParams.get('q'), '华夏 & A');
  respond(calls[0], {funds:[fund], source:'test', message:'使用目录缓存'});
  await pending;
  assert.equal(page.data.catalogFunds[0].fund_code, '000001');
  assert.match(page.data.catalogInfo, /使用目录缓存/);
  assert.equal(page.data.catalogLoading, false);
  assert.equal(calls.length, 1);
});

test('搜索空结果和上游失败可区分，不制造假基金', async () => {
  page.catalogField({detail:{value:'不存在的基金'}});
  let pending=page.searchCatalog(); respond(calls.at(-1), {funds:[], source:'test', message:''}); await pending;
  assert.equal(page.data.catalogSearched,true); assert.deepEqual(page.data.catalogFunds,[]);
  pending=page.searchCatalog(); calls.at(-1).fail({errMsg:'request:fail timeout'}); await pending;
  assert.equal(page.data.catalogSearched,false); assert.match(page.data.catalogError,/超时/);
});

test('改关键词、离开页面、换账号后忽略旧搜索响应', async () => {
  for (const change of [() => page.catalogField({detail:{value:'新关键词'}}), () => page.onHide(),
    () => api.saveConnection({baseUrl:'http://localhost:8000', userId:'new-user',token:''})]) {
    page.catalogField({detail:{value:'原关键词'}});
    const pending=page.searchCatalog(), request=calls.at(-1);
    change(); respond(request,{funds:[fund], source:'old',message:''}); await pending;
    assert.deepEqual(page.data.catalogFunds,[]);
  }
});

function importServer(status='running') {
  handler=request => {
    if(request.url.endsWith('/auth/me')) respond(request,{authenticated:false,auth_required:false});
    else if(request.url.endsWith('/funds/import')) respond(request,{job:{id:'import-test',status:'queued'}});
    else if(request.url.includes('/background-jobs/')) respond(request,{job:{id:'import-test',status,progress_percent:33.33,error:status==='failed'?'数据源暂不可用':null}});
    else if(request.url.endsWith('/funds')) respond(request,list(status==='completed'?[saved]:[]));
    else throw new Error(`Unexpected request ${request.url}`);
  };
}

test('重复点击只提交一个导入，任务受理后不可提前显示已导入', async () => {
  importServer();
  const pending=page.submitImport(fund); await page.submitImport(fund); await pending;
  assert.equal(calls.filter(c=>c.method==='POST').length,1);
  const post=calls.find(c=>c.method==='POST');
  assert.equal(post.data.fund_code,'000001'); assert.ok(post.data.idempotency_key);
  assert.match(calls.at(-1).url,/^http:\/\/127\.0\.0\.1:8000\/background-jobs\/import-test\?user_id=chen$/);
  assert.equal(page.data.pendingImport.job_id,'import-test');
  assert.equal(page.data.observed.length,0); assert.match(page.data.importMessage,/33.33%/);
  assert.equal(page.data.importBusy,false);
});

test('只有任务完成且基金进入可用列表才显示成功，不创建持仓或交易', async () => {
  importServer('completed'); await page.submitImport(fund);
  assert.equal(page.data.pendingImport,null);
  assert.equal(page.data.observed[0].fund_code,'000001');
  assert.equal(page.data.catalogFunds[0].is_imported,true);
  assert.match(page.data.importMessage,/已导入/);
  assert.equal(storage.has(api.storageKey('fund-import')),false);
  assert.equal(calls.filter(c=>c.method!=='GET').length,1);
  assert.ok(calls.every(c=>!c.url.includes('/portfolios')));
});

test('提交响应丢失时沿用幂等键，进度读取失败时不重复提交', async () => {
  importServer(); const normal=handler;
  handler=request => request.url.endsWith('/funds/import') ? request.fail({errMsg:'request:fail timeout'}) : normal(request);
  await page.submitImport(fund);
  const first=calls.find(c=>c.method==='POST').data.idempotency_key;
  assert.equal(page.data.pendingImport.job_id,'');
  importServer(); await page.retryImport();
  assert.equal(calls.filter(c=>c.method==='POST')[1].data.idempotency_key,first);
  const count=calls.filter(c=>c.method==='POST').length;
  handler=request => request.fail({errMsg:'request:fail timeout'});
  await page.retryImport();
  assert.match(page.data.importError,/读取导入进度失败/);
  assert.equal(page.data.pendingImport.job_id,'import-test');
  assert.equal(calls.filter(c=>c.method==='POST').length,count);
});

test('后台失败保留原因，用户明确重试才创建新任务', async () => {
  importServer('failed'); await page.submitImport(fund);
  const first=calls.find(c=>c.method==='POST').data.idempotency_key;
  assert.equal(page.data.pendingImport,null); assert.match(page.data.importError,/数据源暂不可用/);
  importServer('completed'); await page.submitImport(fund);
  assert.notEqual(calls.filter(c=>c.method==='POST')[1].data.idempotency_key,first);
});

test('completed 但尚无可用基金时保留检查入口，不伪报导入成功', async () => {
  importServer('completed'); const normal=handler;
  handler=request=>request.url.endsWith('/funds') ? respond(request,list([])) : normal(request);
  await page.submitImport(fund);
  assert.ok(page.data.pendingImport); assert.equal(page.data.observed.length,0);
  assert.match(page.data.importError,/尚未出现在可用列表/);
});

test('切页后保存迟到的受理结果，返回只查已有任务；任务记录按账号隔离', async () => {
  importServer(); const normal=handler;
  handler=request=>request.url.endsWith('/funds/import') ? undefined : normal(request);
  const pending=page.submitImport(fund); await flush();
  const post=calls.find(c=>c.method==='POST');
  page.onHide(); respond(post,{job:{id:'late-job',status:'queued'}}); await pending;
  assert.equal(storage.get(api.storageKey('fund-import')).job_id,'late-job');
  importServer();
  page.segment({currentTarget:{dataset:{value:'funds'}}}); await flush();
  assert.equal(calls.filter(c=>c.method==='POST').length,1);
  assert.ok(calls.at(-1).url.includes('/background-jobs/late-job'));
  page.onHide(); api.saveConnection({baseUrl:'http://localhost:8000',userId:'other',token:''});
  const count=calls.length;
  page.segment({currentTarget:{dataset:{value:'funds'}}});
  assert.equal(page.data.pendingImport,null); assert.equal(calls.length,count);
});

test('使用同一 Bearer 访问后台任务，不拼错业务前缀或把令牌带到 URL', async () => {
  const pending=api.getBackgroundJob('job-test','alice',{baseUrl:'http://localhost:8000',userId:'alice',token:'local-test-token'});
  const request=calls[0];
  assert.equal(request.url,'http://localhost:8000/background-jobs/job-test?user_id=alice');
  assert.equal(request.header.Authorization,'Bearer local-test-token');
  respond(request,{job:{id:'job-test',status:'completed'}}); await pending;
});

test('导入后新增基金可在实际持仓选择器里选中', async () => {
  let definition; global.Page=value=>{definition=value;};
  delete require.cache[require.resolve('../dist/pages/positions/index')]; require('../dist/pages/positions/index');
  const positions={...definition,data:structuredClone(definition.data),setData(patch){Object.assign(this.data,patch);}};
  handler=request=>respond(request,request.url.endsWith('/auth/me')?{authenticated:false,auth_required:false}:list([saved]));
  await positions.load(); positions.add();
  assert.equal(positions.data.rows[0].fund_code,'000001');
  assert.equal(positions.data.rows[0].units,'');
  assert.ok(calls.every(c=>c.method==='GET'));
});
