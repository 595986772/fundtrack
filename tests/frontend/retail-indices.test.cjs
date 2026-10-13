// 验证空值、来源转义和点位/涨跌口径；不访问外网，不启动真实行情采集。
const test = require('node:test');
const assert = require('node:assert/strict');
const ui = require('../../app/static/retail-indices.js');
const pages = require('../../app/static/workspace-pages.js');

const sample = {
  indicator_id: 'bagholder50', name: '韭菜指数', version: '冷西西韭菜50',
  value: 0.3138, unit: '点', status: 'available', data_date: '2026-09-04',
  source_url: 'https://indices-toocoldcc.pages.dev/', methodology_url: 'https://github.com/toocoldcc/bagholder50',
  source_name: '冷西西', returns: {'30d': {label: '近30日', value_pct: -3.58, start_date: '2026-08-05'}},
  history: [{date:'2026-09-04', value:0.3138}], publication_date: '2026-07-13',
};

test('only the two requested indicators are rendered and route is independent', () => {
  const html = ui.renderOverview({indicators: [sample, {indicator_id:'other', name:'不应出现'}]});
  assert.doesNotMatch(html, /不应出现/);
  assert.match(html, /韭菜指数/);
  assert.deepEqual(pages.readRoute('#market/retail'), {view:'market', page:'retail'});
});
test('missing mom data is not zero or a neutral fabricated score', () => {
  const html = ui.renderIndicator({indicator_id:'mom', name:'宝妈指数', value:null, status:'source_unavailable'});
  assert.match(html, /未接通真实数据源/);
  assert.doesNotMatch(html, /retail-index-metrics|0\.00|50\.00|retail-index-chart/);
});
test('precise point and percent have different units and signed color', () => {
  const html = ui.renderIndicator(sample);
  assert.match(html, /0\.3138<small>点/);
  assert.match(html, /-3\.58%/);
  assert.doesNotMatch(html, /-358/);
  assert.match(html, /data-return-tone="down"/);
  assert.match(html, /2026-08-05 起/);
  assert.match(html, /2026-07-13 前为模拟回测/);
});
test('external text and unsafe links are never executed', () => {
  const html = ui.renderIndicator({...sample, source_url:'javascript:alert(1)', source_name:'<img onerror=alert(1)>', last_error:'<script>bad</script>'});
  assert.doesNotMatch(html, /href="javascript:|<img|<script>/);
  assert.match(html, /&lt;script&gt;/);
});
test('failed refresh can show stored values with an explicit cache label', () => {
  const html = ui.renderIndicator({...sample, status:'cached_error', last_error:'来源不可用'});
  assert.match(html, /更新失败，保留旧值/);
  assert.match(html, /来源不可用/);
  assert.match(html, /0\.3138/);
});
