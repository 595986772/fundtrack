// 纯渲染/请求隔离测试，不联网、不触发采集或模型。
const test = require('node:test');
const assert = require('node:assert/strict');
const {render, create, timestamp} = require('../../app/static/market-intraday.js');
const {renderTurnover} = require('../../app/static/market-intraday.js');
const sample = () => ({source_name: '腾讯财经公开行情', attempted_at: '2026-09-09T06:30:00Z', indices: [{
  index_code: '000001', index_name: '上证指数', amount_cny: 1234567800, volume_lots: 1234567,
  quote_at: '2026-09-09T06:29:30Z', fetched_at: '2026-09-09T06:30:00Z',
  status: 'available', freshness: 'recent', error: '',
}]});

test('amount and volume use different units and source time in Shanghai', () => {
  const html = render({data: sample()});
  assert.match(html, /12\.35<small>亿元/);
  assert.match(html, /123\.46<small>万手/);
  assert.match(html, /14:29:30/);
  assert.match(html, /不作全市场合计/);
  assert.match(html, /可能延迟/);
  assert.equal(timestamp(null), '未获取');
});

test('additional indices including STAR 50 are rendered without a six-row limit', () => {
  const data = sample();
  const codes = ['000001', '399001', '000300', '000905', '000852', '399006', '000688'];
  data.indices = codes.map(code => ({...data.indices[0], index_code: code, index_name: code === '000688' ? '科创50' : code}));
  const html = render({data});
  assert.match(html, /科创50/);
  assert.match(html, /000688/);
  assert.equal((html.match(/<tr>/g) || []).length, 8); // 7个指数行 + 表头，不含任何合计行。
});

test('missing, zero, delayed and failed quotes are distinguishable', () => {
  const data = sample();
  data.indices[0].amount_cny = null;
  data.indices[0].volume_lots = 0;
  data.indices[0].freshness = 'previous_date';
  let html = render({data});
  assert.match(html, /暂无/);
  assert.match(html, /0\.00<small>万手/);
  assert.match(html, /非今日行情/);
  data.indices[0].status = 'cached_error';
  html = render({data});
  assert.match(html, /更新失败 · 保留旧值/);
  assert.doesNotMatch(html, /当日快照/);
});

test('provider text and errors are escaped, never inserted as executable markup', () => {
  const data = sample();
  data.source_name = '<script>alert(1)</script>';
  data.indices[0].error = '" onmouseover="bad';
  data.indices[0].index_name = '<img src=x onerror=bad>';
  const html = render({data, error: '<b>error</b>'});
  assert.doesNotMatch(html, /<script>|<img|<b>error/);
  assert.match(html, /&lt;img/);
  assert.match(html, /&quot;/);
});

function setup(request) {
  const element = {innerHTML: '', handlers: {}, addEventListener(name, handler) { this.handlers[name] = handler; }, setAttribute() {}};
  return {element, controller: create({element, request})};
}

test('navigation only reads GET; explicit refresh is one POST even on duplicate clicks', async () => {
  const calls = [];
  let resolve;
  const {controller, element} = setup((url, options) => {
    calls.push({url, options});
    return options?.method === 'POST' ? new Promise(r => {resolve = r;}) : Promise.resolve(sample());
  });
  await controller.load();
  assert.deepEqual(calls, [{url: '/api/v1/market/intraday', options: undefined}]);
  const first = controller.load({refresh: true});
  assert.equal(controller.load({refresh: true}), first);
  await Promise.resolve();
  assert.match(element.innerHTML, /data-refresh-intraday disabled/);
  resolve(sample());
  await first;
  assert.equal(calls.length, 2);
  assert.equal(calls[1].url, '/api/v1/market/intraday/refresh');
  assert.equal(calls[1].options.method, 'POST');
});

test('network error keeps quote and allows manual retry', async () => {
  let fail = false;
  const {controller, element} = setup(async () => {
    if (fail) throw new Error('请求失败');
    return sample();
  });
  await controller.load();
  fail = true;
  await controller.load({refresh: true});
  assert.match(element.innerHTML, /12\.35<small>亿元/);
  assert.match(element.innerHTML, /请求失败/);
  assert.doesNotMatch(element.innerHTML, /data-refresh-intraday disabled/);
  fail = false;
  await controller.load({refresh: true});
  assert.doesNotMatch(element.innerHTML, /请求失败/);
});

test('whole-market totals keep their scopes, counts and time basis separate from indices', () => {
  const html = renderTurnover({data: {status:'available', freshness:'recent', snapshot: {
    sh_sz_amount_cny: 100e8, sh_sz_bj_amount_cny: 110e8,
    security_count:3, expected_count:3, exchanges:[{label:'北交所A股',security_count:1,amount_cny:10e8}],
    started_at:'2026-09-09T06:30:00Z', fetched_at:'2026-09-09T06:30:05Z', reference_at:'2026-09-09T06:30:00Z',
  }}});
  assert.match(html, /沪深两市 A 股/);
  assert.match(html, /沪深北 A 股/);
  assert.match(html, /100\.00<small>亿元/);
  assert.match(html, /110\.00<small>亿元/);
  assert.match(html, /覆盖 3 \/ 3 只/);
  assert.match(html, /采集窗口/);
  assert.match(html, /个股列表不提供独立交易日期/);
  assert.doesNotMatch(renderTurnover(), /0\.00<small>亿元/);
});

test('whole-market refresh uses its own endpoint and does not collect index or social data', async () => {
  const calls=[];
  const element={innerHTML:'',addEventListener(){},setAttribute(){}};
  const controller=create({element,turnover:true,request:async (url,options)=>{calls.push([url,options?.method]);return {};}});
  await controller.load();
  await controller.load({refresh:true});
  assert.deepEqual(calls,[['/api/v1/market/turnover',undefined],['/api/v1/market/turnover/refresh','POST']]);
});
