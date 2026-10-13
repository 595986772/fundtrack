// 使用固定数据检查快照展示；不访问真实聊天服务或模型。
const test = require('node:test');
const assert = require('node:assert/strict');
const ui = require('../../app/static/investment-context.js');

test('empty or legacy messages do not add an empty panel', () => {
  assert.equal(ui.render(null), '');
  assert.equal(ui.render({facts: {}}), '');
});
test('snapshot renders original user statements with expandable native control', () => {
  const html = ui.render({facts: {horizon: {text: '三年不用'}, loss_tolerance: {text: '接受亏损5%'}}, pending_fields: ['liquidity']});
  assert.match(html, /<details/);
  assert.match(html, /三年不用/);
  assert.match(html, /接受亏损5%/);
  assert.match(html, /待补充：近期用款/);
});
test('external text is escaped and unknown fields are not rendered', () => {
  const html = ui.render({facts: {purpose: {text: '<img src=x onerror=alert(1)>'}, bad: {text: 'secret'}}});
  assert.doesNotMatch(html, /<img|secret/);
  assert.match(html, /&lt;img/);
});
test('unconfirmed state is open and clearly excludes old values from judgment', () => {
  const html = ui.render({needs_confirmation: true, facts: {horizon: {text: '三年'}}});
  assert.match(html, /class="investment-context" open/);
  assert.match(html, /旧条件暂不用于判断/);
});
test('cleared snapshot never pretends the previous profile still applies', () => {
  assert.match(ui.render({facts: {}, update_status: 'cleared'}), /不再沿用之前的投资条件/);
});
test('first research with all conditions missing shows pending questions', () => {
  const html = ui.render({facts: {}, pending_fields: ['horizon', 'loss_tolerance', 'liquidity']});
  assert.match(html, /class="investment-context" open/);
  assert.match(html, /待补充：投资期限、可接受亏损、近期用款/);
});
