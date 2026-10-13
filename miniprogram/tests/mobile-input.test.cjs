/** 浏览器预览输入回归：使用真实 DOM 实现，不触碰接口和用户账本。 */
const { test, before, beforeEach, afterEach } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { JSDOM } = require('jsdom');
const morphdom = require('morphdom');
let patchView, dom, document, root;
before(async () => { ({ patchView } = await import('../preview/dom-update.mjs')); });
beforeEach(() => {
  dom = new JSDOM('<main id="screen"></main>'); document = dom.window.document;
  global.HTMLElement = dom.window.HTMLElement;
  root = document.querySelector('main');
});
afterEach(() => { dom.window.close(); delete global.HTMLElement; });
function target(html) {
  const next = root.cloneNode(false); next.innerHTML = html; return next;
}
function render(html) { patchView(root, target(html), morphdom); }

test('连续打字保留同一个输入框、焦点和插入点，不发生 blur/focus', () => {
  render('<input data-focus-key="units">');
  const input = root.firstChild; let blur = 0, focus = 0;
  input.focus(); input.addEventListener('blur', () => blur++); input.addEventListener('focus', () => focus++);
  for (const value of ['1', '12', '12.3', '12.34']) {
    input.value = value; input.setSelectionRange(value.length - 1, value.length - 1);
    const next = target('<input data-focus-key="units">'); next.firstChild.value = value;
    patchView(root, next, morphdom);
    assert.equal(root.firstChild, input); assert.equal(document.activeElement, input);
    assert.equal(input.selectionStart, value.length - 1);
  }
  assert.equal(blur, 0); assert.equal(focus, 0);
});
test('显示校验提示不会替换正在输入的表单节点', () => {
  render('<div data-focus-key="error:condition" style="display:contents"></div><section data-focus-key="form"><input data-focus-key="name"></section>');
  const input = root.querySelector('input'); input.value = '基金'; input.focus();
  const next = target('<div data-focus-key="error:condition" style="display:contents"><p>请补全成本</p></div><section data-focus-key="form"><input data-focus-key="name"></section>');
  next.querySelector('input').value = input.value; patchView(root, next, morphdom);
  assert.equal(root.querySelector('input'), input); assert.equal(document.activeElement, input);
});
test('中文组词期间不覆盖候选文本，完成后可正常更新', () => {
  render('<input data-focus-key="name">'); const input = root.firstChild;
  input.value = 'jijin'; input._miniComposing = true;
  render('<input data-focus-key="name" value="旧值">');
  assert.equal(input.value, 'jijin');
  input._miniComposing = false;
  render('<input data-focus-key="name" value="基金">');
  assert.equal(input.value, '基金'); assert.equal(root.firstChild, input);
});
test('复用按钮时更新事件闭包，不重复触发旧回调', () => {
  let first = 0, second = 0;
  const a = target('<button data-focus-key="save">保存</button>'); a.firstChild.onclick = () => first++;
  patchView(root, a, morphdom); const button = root.firstChild; button.click();
  const b = target('<button data-focus-key="save">保存</button>'); b.firstChild.onclick = () => second++;
  patchView(root, b, morphdom); button.click();
  assert.equal(root.firstChild, button); assert.equal(first, 1); assert.equal(second, 1);
});
test('删除前一只基金后仍复用后面基金的输入节点和新索引', () => {
  render('<section data-focus-key="fund-a"><input data-focus-key="fund-a:units"></section><section data-focus-key="fund-b"><input data-focus-key="fund-b:units"></section>');
  const input = root.lastChild.firstChild; input.value = '200';
  const next = target('<section data-focus-key="fund-b"><input data-focus-key="fund-b:units"></section>');
  let result; next.firstChild.firstChild.value = '200'; next.firstChild.firstChild.oninput = e => { result = [0, e.currentTarget.value]; };
  patchView(root, next, morphdom); input.dispatchEvent(new dom.window.Event('input'));
  assert.equal(root.querySelector('input'), input); assert.deepEqual(result, [0, '200']);
});
test('日期、下拉和禁用状态仍能更新', () => {
  render('<input type="date" data-focus-key="date"><select data-focus-key="fund"><option value="a">A</option><option value="b">B</option></select>');
  const date = root.firstChild, select = root.lastChild;
  const next = target('<input type="date" data-focus-key="date" disabled><select data-focus-key="fund"><option value="a">A</option><option value="b">B</option></select>');
  next.firstChild.value = '2026-09-10'; next.lastChild.value = 'b'; patchView(root, next, morphdom);
  assert.equal(root.firstChild, date); assert.equal(date.value, '2026-09-10'); assert.ok(date.disabled);
  assert.equal(root.lastChild, select); assert.equal(select.value, 'b');
});
test('更新图区旁的文字时不移除 Canvas', () => {
  render('<div class="mini-chart" id="chart"></div><p data-focus-key="count">30日</p>');
  const chart = root.firstChild, canvas = document.createElement('canvas'); chart.append(canvas);
  render('<div class="mini-chart" id="chart"></div><p data-focus-key="count">3个月</p>');
  assert.equal(root.firstChild, chart); assert.equal(chart.firstChild, canvas); assert.ok(canvas.isConnected);
  assert.equal(root.lastChild.textContent, '3个月');
});
test('所有可编辑控件字号至少16px，保留用户缩放', () => {
  const style = document.createElement('style');
  style.textContent = fs.readFileSync(path.join(__dirname, '../preview/preview.css'), 'utf8'); document.head.append(style);
  root.innerHTML = '<input><input type="password"><input type="date" class="mini-picker"><select class="mini-picker"></select><textarea></textarea>';
  for (const control of root.children) assert.ok(parseFloat(dom.window.getComputedStyle(control).fontSize) >= 16);
  const html = fs.readFileSync(path.join(__dirname, '../preview/index.html'), 'utf8');
  assert.ok(!/user-scalable\s*=\s*(no|0)|maximum-scale\s*=\s*1(?:[",\s]|$)/.test(html));
  const runtime = fs.readFileSync(path.join(__dirname, '../preview/runtime.js'), 'utf8');
  assert.ok(!/\.focus\(/.test(runtime));
});
