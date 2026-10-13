// 页面状态测试：网络替身只返回人工报告，不连接真实账号。
const test = require('node:test');
const assert = require('node:assert/strict');
const ui = require('../../app/static/portfolio-stress.js');
const report = () => ({
  run_id: 'stress_0123456789abcdef', root_run_id: 'stress_0123456789abcdef',
  portfolio_id: 'portfolio_0123456789abcdef', portfolio_name: '<script>bad</script>',
  months: 12, created_at: '2026-01-01T00:00:00Z', captured_at: '2026-01-01T00:00:00Z', input_hash: 'abc',
  window: { days: 20, period_start: '2025-11-01', period_end: '2025-12-01', candidate_windows: 30, excluded_windows: 2 },
  holdings: [{ fund_code: '110020', short_name: '<img src=x onerror=alert(1)>', weight_pct: 100 }],
  scenarios: [{ scenario_id: 'baseline', label: '原配置', weights: { '110020': 100 }, period_return_pct: -5, max_drawdown_pct: 6, worst_daily_return_pct: -2,
    curve: [{ date: '2025-11-01', index: 100 }, { date: '2025-12-01', index: 95 }],
    contributions: [{ fund_code: '110020', return_contribution_pp: -5 }] }],
  data_sources: [], limitations: ['历史情景，不是未来预测'],
});
function host() { return { innerHTML: '', querySelector() { return null; } }; }
function controller(request) {
  const ctx = { userId: 'chen', portfolioId: 'portfolio_0123456789abcdef' }, element = host();
  const instance = ui.create({ request, getContext: () => ctx, refreshIcons() {}, askAgent() {} });
  instance.mount(element);
  return { ctx, element, instance };
}
test('empty state and numeric constraints', () => {
  assert.match(ui.render(ui.initialState()), /尚无历史重演结果/);
  assert.equal(ui.totalWeights({ x: '', y: 100 }), null);
  assert.equal(ui.totalWeights({ x: -1, y: 101 }), null);
  assert.equal(ui.totalWeights({ x: '30', y: 70 }), 100);
});
test('request IDs support older browsers and fail clearly without randomness', () => {
  assert.equal(ui.requestKey({ getRandomValues: bytes => bytes.fill(7) }), 'stress-' + '07'.repeat(16));
  assert.throws(() => ui.requestKey({}), /localhost/);
});
test('key-generation failure never poisons retry state or sends an empty key', async () => {
  const original = Object.getOwnPropertyDescriptor(globalThis, 'crypto');
  const calls = [];
  try {
    Object.defineProperty(globalThis, 'crypto', { configurable: true, value: {} });
    const { instance, element } = controller(async (_url, options) => { calls.push(JSON.parse(options.body)); return report(); });
    await instance.execute('new');
    assert.equal(calls.length, 0); assert.match(element.innerHTML, /localhost/);
    Object.defineProperty(globalThis, 'crypto', { configurable: true, value: { getRandomValues: bytes => bytes.fill(5) } });
    await instance.execute('new');
    assert.match(calls[0].idempotency_key, /^stress-05/);
  } finally { Object.defineProperty(globalThis, 'crypto', original); }
});
test('report escapes fund names and separates output from weight draft', () => {
  const state = { ...ui.initialState(), report: report(), weights: { '110020': 100 } };
  const html = ui.render(state);
  assert.doesNotMatch(html, /<img src=x/);
  assert.match(html, /&lt;img/);
  assert.match(html, /当前曲线对应已保存的权重/);
  assert.match(html, /data-stress-chart/);
  assert.equal(ui.chartOption(report()).series[0].data[1], 95);
});
test('mount and rerender do not fetch; explicit run and compare share source', async () => {
  const calls = [];
  const { instance, element } = controller(async (url, options) => { calls.push({ url, options }); return report(); });
  assert.equal(calls.length, 0);
  await instance.execute('new');
  instance.mount(host());
  assert.equal(calls.length, 1);
  await instance.execute('compare');
  const body = JSON.parse(calls[1].options.body);
  assert.equal(body.source_run_id, report().run_id);
  assert.deepEqual(body.target_weights, { '110020': 100 });
  assert.equal(body.months, undefined);
});
test('failed retry retains idempotency key and error is actionable', async () => {
  const calls = [];
  const { instance, element } = controller(async (_url, options) => { calls.push(JSON.parse(options.body)); throw Error('本地净值不足'); });
  await instance.execute('new');
  assert.match(element.innerHTML, /本地净值不足/);
  await instance.execute('new');
  assert.equal(calls[0].idempotency_key, calls[1].idempotency_key);
});
test('inflight result cannot cross portfolio boundary', async () => {
  let resolve;
  const { instance, ctx } = controller(() => new Promise(done => { resolve = done; }));
  const pending = instance.execute('new');
  ctx.portfolioId = 'portfolio_1111111111111111';
  const next = host(); instance.mount(next);
  resolve(report()); await pending;
  assert.match(next.innerHTML, /尚无历史重演结果/);
  assert.doesNotMatch(next.innerHTML, /2025-11-01/);
});
test('history and verify are explicit reads without new snapshot writes', async () => {
  const calls = [];
  const { instance } = controller(async (url, options) => { calls.push({ url, options }); return url.includes('verify') ? { consistent: true } : []; });
  await instance.execute('history'); await instance.execute('verify', report().run_id);
  assert.equal(calls[0].options.method, undefined);
  assert.match(calls[1].url, /\/verify\?user_id=chen$/);
  assert.equal(calls[1].options.body, undefined);
});
