// Web 投研页也会被手机访问，不能只修 /mini 的输入框字号。
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const read = (file) => fs.readFileSync(path.join(__dirname, '../../app/static', file), 'utf8');

test('手机输入字号在组件样式之后加载，并保留手动缩放', () => {
  const html = read('index.html');
  const css = read('mobile-input.css');
  assert.ok(html.indexOf('mobile-input.css') > html.indexOf('terminal.css'));
  assert.match(css, /@media[^\{]*max-width: 760px/);
  assert.match(css, /input[^\{]*select,[\s\S]*textarea\s*\{\s*font-size: 16px !important/);
  const viewport = html.match(/<meta[^>]+name="viewport"[^>]+>/)[0];
  assert.doesNotMatch(viewport, /maximum-scale|user-scalable/);
});
