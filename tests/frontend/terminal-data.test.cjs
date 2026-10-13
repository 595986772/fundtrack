// 图表展示的纯函数测试，不需要启动服务、浏览器或付费模型。
const test = require('node:test');
const assert = require('node:assert/strict');
const D = require('../../app/static/terminal-data.js');

test('calendar month rollback clamps leap-year month end', () => {
  assert.equal(D.cutoffDate('2024-05-31', '3m'), '2024-02-29');
  assert.equal(D.cutoffDate('2026-03-31', '30d'), '2026-03-01');
});
test('missing numbers never look like zero returns', () => {
  assert.equal(D.percent(null), '--');
  assert.equal(D.percent(0), '0.00%');
  assert.equal(D.tone(-1), 'down');
});
test('comparison rebases both series on shared dates', () => {
  const primary = [{date:'2026-01-01',value:1}, {date:'2026-01-02',value:2}, {date:'2026-01-03',value:3}];
  const comparison = [{date:'2026-01-02',value:10}, {date:'2026-01-03',value:12}];
  const plot = D.chartSeries(primary, comparison, '3m');
  assert.deepEqual(plot.dates, ['2026-01-02','2026-01-03']);
  assert.deepEqual(plot.primary, [0,50]);
  assert.ok(Math.abs(plot.comparison[1] - 20) < 1e-8);
  assert.ok(plot.warnings.some((text) => text.includes('共同交易日')));
});
test('no common dates preserves main curve and discloses missing comparison', () => {
  const plot = D.chartSeries([{date:'2026-01-01',value:1}, {date:'2026-01-02',value:2}], [{date:'2025-01-01',value:1}], '30d');
  assert.equal(plot.dates.length, 2);
  assert.equal(plot.comparison.length, 0);
  assert.ok(plot.warnings.some((text) => text.includes('暂不绘制')));
});
test('NAV mode uses actual NAV rather than rebased return index', () => {
  const plot = D.chartSeries([{date:'2026-01-01',value:1,nav:2}, {date:'2026-01-02',value:1,nav:1.8}], [], '30d', 'nav');
  assert.deepEqual(plot.primary, [2,1.8]);
});
test('source URLs reject script and data protocols and text is escaped', () => {
  assert.equal(D.safeUrl('javascript:alert(1)'), '');
  assert.equal(D.safeUrl('data:text/html,x'), '');
  assert.equal(D.escape('<img onerror="x">'), '&lt;img onerror=&quot;x&quot;&gt;');
});
