/** 不联网、不调用模型、不触碰用户账本；直接测构建后的原生业务模块。 */
const { test, beforeEach } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const api = require('../dist/lib/api');
const fmt = require('../dist/lib/format');
const ledger = require('../dist/lib/ledger');
let storage, calls;
beforeEach(() => {
  storage = new Map(); calls = [];
  global.wx = { getStorageSync: k => storage.get(k), setStorageSync: (k, v) => storage.set(k, v), removeStorageSync: k => storage.delete(k), request: o => calls.push(o) };
});
test('缺失净值和盈亏显示 --，不是 0', () => {
  assert.equal(fmt.number(null), '--'); assert.equal(fmt.signed(undefined), '--');
  assert.equal(fmt.signed(0, '%'), '0.00%'); assert.equal(fmt.signed(1234.56), '+1,234.56');
  assert.equal(fmt.tone(-1), 'down'); assert.equal(fmt.amount(2e12), '2.00 万亿');
});
test('30 日窗口按最新数据日计算且不修改原数组', () => {
  const points = [{ nav_date: '2026-09-10', unit_nav: 2 }, { nav_date: '2026-08-10', unit_nav: 1 }, { nav_date: '2026-08-11', unit_nav: 1.1 }];
  assert.deepEqual(fmt.navWindow(points, 30).map(p => p.nav_date), ['2026-08-11', '2026-09-10']);
  assert.equal(points[0].nav_date, '2026-09-10'); assert.deepEqual(fmt.navWindow([], 90), []);
});
test('地址只允许 HTTP(S) 服务根地址，不允许凭证或查询字符串', () => {
  assert.equal(api.normalizeUrl(' http://127.0.0.1:8000/ '), 'http://127.0.0.1:8000');
  for (const url of ['javascript:alert(1)', 'https://u:p@example.com', 'https://api.test?token=x', 'https://api.test/path']) assert.throws(() => api.normalizeUrl(url));
});
test('请求明确携带 Bearer，令牌不进入 URL', async () => {
  api.saveConnection({ baseUrl: 'http://127.0.0.1:8000', userId: 'alice', token: 'test-secret' });
  const result = api.request('/funds');
  assert.equal(calls[0].header.Authorization, 'Bearer test-secret'); assert.ok(!calls[0].url.includes('test-secret'));
  calls[0].success({ statusCode: 200, data: { funds: [] } }); assert.deepEqual(await result, { funds: [] });
});
test('网络错误和 FastAPI 校验错误有可读信息', async () => {
  const a = api.request('/funds'); calls[0].fail({}); await assert.rejects(a, /无法连接/);
  const b = api.request('/portfolios'); calls[1].success({ statusCode: 422, data: { detail: [{ msg: 'invalid units' }] } }); await assert.rejects(b, /invalid units/);
});
test('微信网络错误保留具体原因，不能全部误报为服务器未启动', async () => {
  const cases = [
    ['request:fail url not in domain list', /未放行此请求域名/],
    ['request:fail timeout', /连接后端超时/],
    ['request:fail net::ERR_CONNECTION_REFUSED', /连接被拒绝/],
    ['request:fail net::ERR_NAME_NOT_RESOLVED', /无法解析后端域名/],
    ['request:fail local network prohibited', /没有本地网络访问权限/],
    ['request:fail SSL certificate error', /HTTPS 连接未通过/],
  ];
  for (const [errMsg, expected] of cases) {
    const pending = api.request('/auth/me', 'GET', undefined, {baseUrl:'http://192.168.1.10:8000', userId:'chen', token:''});
    calls.at(-1).fail({errMsg, errno: 600001});
    await assert.rejects(pending, error => {
      assert.match(error.message, expected);
      assert.match(error.message, /600001/);
      assert.ok(error.message.includes(errMsg));
      assert.doesNotMatch(error.message, /当前使用回环地址/);
      return true;
    });
  }
});
test('连接按钮按新填写的地址诊断，而不是拿旧的已保存地址误报', async () => {
  api.saveConnection({baseUrl:'http://192.168.1.10:8000', userId:'chen', token:''});
  for (const baseUrl of ['http://127.0.0.1:8000', 'http://localhost:8000', 'http://[::1]:8000']) {
    const pending = api.request('/auth/me', 'GET', undefined, {baseUrl, userId:'chen', token:''});
    calls.at(-1).fail({errMsg:'request:fail timeout'});
    await assert.rejects(pending, /手机真机请改填 Mac 的局域网 IP/);
  }
});
test('诊断文本不回显请求凭据，也不修改保存的连接和用户数据', async () => {
  const c = {baseUrl:'http://192.168.1.10:8000', userId:'chen', token:'test-secret'};
  api.saveConnection(c);
  const pending = api.request('/auth/me');
  calls.at(-1).fail({errMsg:'request:fail https://example.invalid?token=private-value Bearer test-secret sk-test-do-not-leak'});
  await assert.rejects(pending, error => {
    assert.doesNotMatch(error.message, /private-value|test-secret|sk-test-do-not-leak/);
    return true;
  });
  assert.deepEqual(api.connection(), c);
});
test('有过期令牌时不能以本地用户身份继续', async () => {
  api.saveConnection({ baseUrl: 'http://localhost:8000', userId: 'alice', token: 'expired' });
  const result = api.identity(); calls[0].success({ statusCode: 200, data: { authenticated: false, auth_required: false } });
  await assert.rejects(result, /重新登录/);
});
test('真实登录身份优先于本地输入的用户 ID', async () => {
  const result = api.identity(); calls[0].success({ statusCode: 200, data: { authenticated: true, user: { user_id: 'alice' } } });
  assert.equal(await result, 'alice');
});
const row = { fund_code: '110020', short_name: '示例基金', units: '100', cost_amount: '200', purchase_date: '2025-01-02' };
test('实际持仓提交份额成本，不提交权重', () => {
  const p = ledger.positionsPayload('我的账本', [row], 'alice');
  assert.equal(p.holdings[0].units, 100); assert.equal(p.holdings[0].cost_amount, 200);
  assert.ok(!('weight_pct' in p.holdings[0]));
});
test('初始持仓空值、重复、负数、无效日期都不能保存', () => {
  for (const patch of [{ units: '' }, { units: '-1' }, { units: 'Infinity' }, { cost_amount: '0' }, { purchase_date: '2025-02-31' }, { purchase_date: '2099-01-01' }]) assert.throws(() => ledger.positionsPayload('账本', [{ ...row, ...patch }], 'alice'));
  assert.throws(() => ledger.positionsPayload('账本', [row, row], 'alice'));
  assert.throws(() => ledger.positionsPayload('', [row], 'alice'));
});
const trade = { fund_code: '110020', transaction_type: 'buy', trade_date: '2025-01-03', units: '10', unit_price: '2', fee: '1', note: '' };
test('记账要求真实成交净值，空净值不能默认使用最新净值', () => {
  assert.equal(ledger.tradePayload(trade, 'alice').unit_price, 2);
  assert.throws(() => ledger.tradePayload({ ...trade, unit_price: '' }, 'alice'));
  assert.throws(() => ledger.tradePayload({ ...trade, fee: '-1' }, 'alice'));
});
test('失败后相同请求复用幂等键，改了内容则生成新键', () => {
  const first = ledger.submissionKey('trade', trade);
  assert.equal(ledger.submissionKey('trade', trade), first);
  assert.notEqual(ledger.submissionKey('trade', { ...trade, units: '20' }), first);
});
test('本地账本选择与幂等键按服务器和账号隔离', () => {
  const first = api.storageKey('portfolio'); api.saveConnection({ baseUrl: 'http://localhost:8000', userId: 'bob', token: '' });
  assert.notEqual(api.storageKey('portfolio'), first);
});
test('全部页面、图标和组件已构建，原生主包不携带浏览器适配代码', () => {
  const root = path.join(__dirname, '../dist'), manifest = JSON.parse(fs.readFileSync(path.join(root, 'app.json')));
  assert.equal(manifest.pages.length, 14);
  assert.deepEqual(manifest.tabBar.list.map(t=>t.text),['资产','事件情报','社区热点','板块雷达','AI投研']);
  for (const page of manifest.pages) for (const ext of ['.js', '.json', '.wxml']) assert.ok(fs.existsSync(path.join(root, page + ext)), page + ext);
  for (const tab of manifest.tabBar.list) for (const key of ['iconPath', 'selectedIconPath']) assert.ok(fs.statSync(path.join(root, tab[key])).size > 100);
  assert.ok(fs.existsSync(path.join(root, 'components/ec-canvas/ec-canvas.json')));
  assert.ok(!fs.existsSync(path.join(root, 'runtime.js')));
  const preview=JSON.parse(fs.readFileSync(path.join(root,'preview-manifest.json')));
  assert.match(preview.styles['pages/fund-reports/index'],/source-text/);
  assert.ok(fs.existsSync(path.join(root,'pages/fund-reports/index.wxss')));
});
test('切换页面后丢弃未完成的资产响应', async () => {
  let definition;
  global.Page = d => definition = d;
  delete require.cache[require.resolve('../dist/pages/assets/index')]; require('../dist/pages/assets/index');
  const page = { ...definition, data: structuredClone(definition.data), setData(patch) { Object.assign(this.data, patch); } };
  const pending = page.load();
  calls[0].success({ statusCode: 200, data: { authenticated: false, auth_required: false } });
  await new Promise(r => setImmediate(r));
  page.onHide();
  calls[1].success({ statusCode: 200, data: { portfolios: [{ id: 'old-private-portfolio', name: '旧账号' }] } });
  calls[2].success({ statusCode: 200, data: { funds: [], data_mode: 'database' } });
  await pending; assert.equal(page.data.current, null); assert.deepEqual(page.data.portfolios, []);
});
test('浏览器预览的混合文本绑定、数组和布尔值不会混淆', async () => {
  const { binding } = await import('../preview/bindings.mjs');
  const data = { value: '100.00', cost: '90.00', list: [1, 2], loading: false };
  assert.equal(binding('{{value}} / {{cost}}', data), '100.00 / 90.00');
  assert.deepEqual(binding('{{list}}', data), [1, 2]);
  assert.equal(binding('{{loading}}', data), false);
  assert.equal(binding('静态文案', data), '静态文案');
});
test('原生 tab 缓存的上个账号持仓在新请求前清除', async () => {
  let definition; global.Page = d => definition = d;
  delete require.cache[require.resolve('../dist/pages/assets/index')]; require('../dist/pages/assets/index');
  const page = { ...definition, _loadedScope: 'old-account', data: { ...structuredClone(definition.data), current: { name: '旧账号资产' } }, setData(patch) { Object.assign(this.data, patch); } };
  const pending = page.load(); assert.equal(page.data.current, null);
  calls[0].fail({}); await pending;
});
test('市场打开只读快照，手动刷新才调用行情刷新接口', async () => {
  let definition; global.Page = d => definition = d;
  delete require.cache[require.resolve('../dist/pages/market/index')]; require('../dist/pages/market/index');
  const page = { ...definition, data: structuredClone(definition.data), setData(patch) { Object.assign(this.data, patch); } };
  for (const manual of [false, true]) {
    calls.length = 0;
    const pending = page.load(manual);
    assert.deepEqual(calls.map(c => c.method), manual ? ['POST', 'POST', 'GET'] : ['GET', 'GET', 'GET']);
    assert.ok(calls[0].url.endsWith(manual ? '/intraday/refresh' : '/intraday'));
    calls[0].success({ statusCode: 200, data: { source_name: 'test', indices: [{ index_code: '000001', last: 100, change_pct: 1, amount_cny: 1e8, volume_lots: 100, quote_at: '2026-09-09T15:00:00', freshness: 'previous_date' }] } });
    calls[1].success({ statusCode: 200, data: { snapshot: { sh_sz_amount_cny: 1e12, sh_sz_bj_amount_cny: 1.1e12, reference_at: '2026-09-09T15:00:00' }, error: '保留旧快照', limitation: '测试口径' } });
    calls[2].success({ statusCode: 200, data: { indices: [{ index_code: '000001', data_date: '2026-09-08', return_30d_pct: 2, return_3m_pct: 3 }] } });
    await pending;
    assert.equal(page.data.quotes[0].month, '+2.00%');
    assert.equal(page.data.quotes[0].dailyDate, '2026-09-08');
    assert.equal(page.data.quotes[0].freshnessText, '历史快照');
    assert.equal(page.data.error, '保留旧快照');
  }
});
