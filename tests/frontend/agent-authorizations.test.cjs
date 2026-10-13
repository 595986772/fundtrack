// 授权入口只查记录，不启动模型；用可控回包验证身份隔离、撤销边界和键盘焦点。
const { test } = require('node:test');
const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const { JSDOM } = require('../../miniprogram/node_modules/jsdom');
const { create } = require('../../app/static/agent-authorizations.js');

const html = readFileSync(require.resolve('../../app/static/index.html'), 'utf8');
const source = new JSDOM(html).window.document;
const fixtureHTML = source.querySelector('#open-agent-authorizations').outerHTML
  + source.querySelector('#agent-authorizations-dialog').outerHTML;
const tick = () => new Promise(resolve => setImmediate(resolve));
const deferred = () => {
  let resolve, reject;
  const promise = new Promise((done, fail) => { resolve = done; reject = fail; });
  return { promise, resolve, reject };
};
const grant = (overrides = {}) => ({
  id: 'grant-a', run_id: 'run-a', agent_id: 'research-main', parent_grant_id: null,
  source: 'product-api', status: 'active', effective: true,
  portfolio_ids: ['portfolio-a'], capabilities: ['portfolio:read', 'portfolio:research-run'],
  created_at: '2026-09-21T01:00:00Z', expires_at: '2099-09-21T02:30:00Z',
  revoked_at: null, revoke_reason: null, ...overrides,
});
const list = (grants = [grant()], overrides = {}) => ({ enabled: true, strict_identity: true, grants, ...overrides });
const detail = (overrides = {}) => ({ ...grant(), audit: [], ...overrides });

function harness(respond = async () => list()) {
  const dom = new JSDOM(fixtureHTML, { url: 'https://example.test' });
  const document = dom.window.document;
  const dialog = document.querySelector('dialog');
  const trigger = document.querySelector('#open-agent-authorizations');
  const calls = [];
  let context = { userId: 'chen', portfolios: [{ id: 'portfolio-a', name: '稳健组合' }] };
  // jsdom 不实现顶层 dialog；这里只模拟开关，浏览器原生焦点约束由主 Agent 验证。
  dialog.showModal = () => { dialog.open = true; };
  dialog.close = () => {
    dialog.open = false;
    queueMicrotask(() => dialog.dispatchEvent(new dom.window.Event('close')));
  };
  const page = create({ dialog, trigger, getContext: () => context, request: (url, options) => {
    calls.push({ url, options });
    return respond(url, options);
  } });
  return {
    dom, document, dialog, trigger, page, calls,
    query: selector => dialog.querySelector(selector),
    click(selector) { const button = dialog.querySelector(selector); assert.ok(button, selector); button.click(); },
    escape() { dialog.dispatchEvent(new dom.window.Event('cancel', { cancelable: true })); },
    switchUser(userId, portfolios = [], reset = true) {
      if (reset) page.reset();
      context = { userId, portfolios };
    },
  };
}

test('入口属于聊天头部且使用 shield-check；资源先于 app 加载，没有新增导航', () => {
  const button = source.querySelector('.chat-header #open-agent-authorizations');
  assert.ok(button);
  assert.equal(button.textContent.trim(), '任务授权');
  assert.ok(button.querySelector('[data-lucide="shield-check"]'));
  assert.equal(button.getAttribute('aria-haspopup'), 'dialog');
  assert.equal(source.querySelectorAll('#open-agent-authorizations').length, 1);
  assert.ok(source.querySelector('link[href^="/static/agent-authorizations.css"]'));
  assert.ok(html.indexOf('/static/agent-authorizations.js') < html.indexOf('/static/app.js'));
  assert.equal(source.querySelector('nav #open-agent-authorizations'), null);
  const app = readFileSync(require.resolve('../../app/static/app.js'), 'utf8');
  assert.match(app, /FundScopeAgentAuthorizations\.create/);
  assert.match(app, /agentAuthorizations\?\.reset\(\);\s*resetReportEvalViewer\(\);\s*state\.userId = candidate/);
});

test('初始化不发请求，打开只 GET 当前用户，组合名称、范围和北京时间完整', async () => {
  const h = harness();
  assert.equal(h.calls.length, 0);
  await h.page.open();
  assert.equal(h.calls.length, 1);
  assert.equal(h.calls[0].url, '/api/v1/agent/authorizations?user_id=chen');
  assert.equal(h.calls[0].options.method, undefined);
  assert.match(h.dialog.textContent, /稳健组合/);
  assert.match(h.dialog.textContent, /读取组合、运行组合研究/);
  assert.match(h.dialog.textContent, /2099\/09\/21 10:30/);
  assert.equal(h.query('.agent-auth-status').textContent, '有效');
  assert.equal(h.query('[data-auth-content]').getAttribute('aria-busy'), 'false');
  assert.equal(h.query('form, input, select, textarea'), null);
});

test('重复打开和加载中点击只读一次；关闭中止读取并恢复入口焦点', async () => {
  const pending = deferred(), h = harness(() => pending.promise);
  h.trigger.focus();
  const opened = h.page.open();
  h.page.open();
  h.trigger.click();
  h.click('[data-auth-refresh]');
  assert.equal(h.calls.length, 1);
  assert.match(h.dialog.textContent, /正在读取任务授权/);
  assert.equal(h.document.activeElement, h.query('[data-auth-close]'));
  h.escape();
  assert.equal(h.dialog.open, false);
  assert.equal(h.document.activeElement, h.trigger);
  assert.equal(h.trigger.getAttribute('aria-expanded'), 'false');
  assert.equal(h.calls[0].options.signal.aborted, true);
  pending.resolve(list());
  await opened;
  assert.doesNotMatch(h.dialog.textContent, /稳健组合/);
});

test('空记录、未启用、兼容身份和异常列表不会伪装成有效授权', async () => {
  const empty = harness(async () => list([]));
  await empty.page.open();
  assert.match(empty.dialog.textContent, /暂无任务授权/);
  assert.equal(empty.query('[data-auth-action="revoke"]'), null);
  const disabled = harness(async () => list([grant()], { enabled: false, strict_identity: false }));
  await disabled.page.open();
  assert.match(disabled.dialog.textContent, /任务授权校验未启用/);
  assert.match(disabled.dialog.textContent, /用户 ID 不是已验证/);
  assert.equal(disabled.query('[data-auth-action="revoke"]'), null);
  const malformed = harness(async () => ({ enabled: true, grants: 'invalid' }));
  await malformed.page.open();
  assert.match(malformed.query('[role="alert"]').textContent, /无效的授权列表/);
  assert.doesNotMatch(malformed.dialog.textContent, /暂无任务授权/);
});

test('列表错误可重试，失败恢复按钮，异常文本转义', async () => {
  let count = 0;
  const h = harness(async () => { if (++count === 1) throw new Error('<img src=x onerror=alert(1)>'); return list(); });
  await h.page.open();
  assert.match(h.query('[role="alert"]').textContent, /读取失败/);
  assert.equal(h.query('img'), null);
  assert.equal(h.query('[data-auth-refresh]').disabled, false);
  h.click('[data-auth-action="retry"]');
  await tick();
  assert.equal(h.calls.length, 2);
  assert.equal(h.query('[role="alert"]'), null);
  assert.match(h.dialog.textContent, /稳健组合/);
});

test('最近排序、有效性与各状态清晰，缺失组合映射保留资源编号', async () => {
  const records = [
    grant({ id: 'a', status: 'active', effective: false, inactive_reason: '父任务授权已撤销' }),
    grant({ id: 'b', status: 'revoked', effective: false }),
    grant({ id: 'c', status: 'expired', effective: false }),
    grant({ id: 'd', status: 'pending', effective: false }),
    grant({ id: 'e', status: 'failed', effective: false }),
    grant({ id: 'f', effective: undefined }),
    grant({ id: 'g', expires_at: '2000-01-01T00:00:00Z' }),
    grant({ id: 'latest', created_at: '2026-09-22T00:00:00Z', portfolio_ids: ['unknown'] }),
  ];
  const h = harness(async () => list(records));
  await h.page.open();
  assert.match(h.query('.agent-auth-grant h3').textContent, /组合 unknown/);
  assert.deepEqual([...h.dialog.querySelectorAll('.agent-auth-grant-heading .agent-auth-status')].map(node => node.textContent),
    ['有效', '未生效', '已撤销', '已到期', '待生效', '授权失败', '有效性待核验', '已到期']);
  assert.match(h.dialog.textContent, /父任务授权已撤销/);
  assert.equal(h.dialog.querySelectorAll('[data-auth-action="revoke"]').length, 4);
});

test('展开审计才 GET，重复展开不并发，详情缺失 effective 不覆盖列表状态', async () => {
  const pending = deferred();
  const h = harness(url => url.includes('/grant-a?') ? pending.promise : Promise.resolve(list([grant({ effective: false })])));
  await h.page.open();
  h.click('[data-auth-action="audit"]');
  assert.equal(h.calls.length, 2);
  assert.equal(h.query('[data-auth-action="audit"]').getAttribute('aria-expanded'), 'true');
  h.click('[data-auth-action="audit"]');
  h.click('[data-auth-action="audit"]');
  assert.equal(h.calls.length, 2);
  pending.resolve(detail({ effective: undefined, audit: [
    { action: 'portfolio:read', resource_id: 'portfolio-a', allowed: true, reason: 'allowed', created_at: '2026-09-21T01:15:00Z' },
    { action: 'portfolio:research-run', resource_id: 'elsewhere', allowed: false, reason: 'resource_denied', created_at: '2026-09-21T01:00:00Z' },
  ] }));
  await tick();
  assert.equal(h.query('.agent-auth-grant-heading .agent-auth-status').textContent, '未生效');
  assert.match(h.query('.agent-auth-audit').textContent, /允许/);
  assert.match(h.query('.agent-auth-audit').textContent, /拒绝/);
  assert.match(h.query('.agent-auth-audit').textContent, /稳健组合 · 校验通过/);
  assert.match(h.query('.agent-auth-audit').textContent, /09:15/);
  assert.ok(h.calls.every(call => !call.options.method));
});

test('审计空态、失败重试和完整文本均为只读，服务端字段不会注入 HTML', async () => {
  let count = 0;
  const malicious = '<img src=x onerror=alert(1)>';
  const unsafe = grant({ id: 'id/with?query', source: malicious, capabilities: [malicious], inactive_reason: malicious });
  const h = harness(async url => {
    if (url.includes('id%2Fwith%3Fquery?')) {
      if (++count === 1) throw new Error(malicious);
      return { ...unsafe, audit: count === 2 ? [] : [{ action: malicious, resource_id: malicious, reason: malicious, allowed: false }] };
    }
    return list([unsafe]);
  });
  h.switchUser('alice & bob', [{ id: 'portfolio-a', name: malicious }]);
  await h.page.open();
  assert.match(h.calls[0].url, /user_id=alice\+%26\+bob/);
  h.click('[data-auth-action="audit"]');
  await tick();
  assert.match(h.query('[role="alert"]').textContent, /审计读取失败/);
  h.click('[data-auth-action="audit-retry"]');
  await tick();
  assert.match(h.dialog.textContent, /暂无审计记录/);
  h.click('[data-auth-refresh]');
  await tick();
  h.click('[data-auth-action="audit"]');
  await tick();
  assert.match(h.query('.agent-auth-audit-list').textContent, /<img/);
  assert.equal(h.query('img, script'), null);
  assert.equal(h.query('input, textarea'), null);
});

test('撤销必须二次确认，取消和 ESC 不提交；确认默认聚焦非破坏性操作', async () => {
  const h = harness();
  await h.page.open();
  h.click('[data-auth-action="revoke"]');
  assert.equal(h.calls.length, 1);
  assert.match(h.dialog.textContent, /停止此任务及其子任务.*后续访问/);
  assert.match(h.dialog.textContent, /不会撤回既有回答或交易/);
  assert.equal(h.document.activeElement, h.query('[data-auth-action="cancel"]'));
  assert.equal(h.dialog.getAttribute('aria-describedby'), 'agent-auth-revoke-warning');
  h.escape();
  assert.equal(h.dialog.open, true);
  assert.equal(h.query('[data-auth-action="confirm"]'), null);
  assert.equal(h.document.activeElement, h.query('[data-auth-action="revoke"]'));
  h.click('[data-auth-action="revoke"]');
  h.click('[data-auth-action="cancel"]');
  assert.equal(h.calls.length, 1);
  h.escape();
  assert.equal(h.dialog.open, false);
  assert.equal(h.document.activeElement, h.trigger);
});

test('确认期间已到期的授权不提交撤销，并提供刷新核对提示', async t => {
  const h = harness();
  await h.page.open();
  h.click('[data-auth-action="revoke"]');
  t.mock.method(Date, 'now', () => new Date('2100-01-01T00:00:00Z').valueOf());
  h.click('[data-auth-action="confirm"]');
  await tick();
  assert.equal(h.calls.length, 1);
  assert.match(h.query('[role="alert"]').textContent, /已到期或状态已变化/);
  assert.equal(h.document.activeElement, h.query('[data-auth-action="cancel"]'));
});

test('审计响应损坏保留可重试错误，不抛出渲染异常或假报暂无记录', async () => {
  const h = harness(async url => url.includes('/grant-a?') ? detail({ audit: [null] }) : list());
  await h.page.open();
  h.click('[data-auth-action="audit"]');
  await tick();
  assert.match(h.query('[role="alert"]').textContent, /无效的审计记录/);
  assert.ok(h.query('[data-auth-action="audit-retry"]'));
  assert.doesNotMatch(h.dialog.textContent, /暂无审计记录/);
});

test('确认撤销仅提交一次固定 JSON，成功后刷新父子授权并可查看最新审计', async () => {
  const pending = deferred();
  let revoked = false;
  const parent = grant(), child = grant({ id: 'child', parent_grant_id: parent.id });
  const h = harness(async (url, options) => {
    if (options.method === 'POST') return pending.promise;
    if (url.includes('/grant-a?')) return detail({ status: 'revoked', audit: [{ action: 'grant:revoke', allowed: true, reason: '用户撤销' }] });
    return list(revoked ? [{ ...parent, status: 'revoked', effective: false }, { ...child, effective: false, inactive_reason: '父任务授权已撤销' }] : [parent, child]);
  });
  await h.page.open();
  h.click('[data-auth-action="revoke"]');
  h.click('[data-auth-action="confirm"]');
  h.click('[data-auth-action="confirm"]');
  const sent = h.calls.filter(call => call.options.method === 'POST');
  assert.equal(sent.length, 1);
  assert.equal(sent[0].url, '/api/v1/agent/authorizations/grant-a/revoke');
  assert.equal(sent[0].options.headers['Content-Type'], 'application/json');
  assert.deepEqual(JSON.parse(sent[0].options.body), { user_id: 'chen', reason: '用户撤销' });
  assert.equal(h.query('[data-auth-action="confirm"]').disabled, true);
  revoked = true;
  pending.resolve({ ...parent, status: 'revoked', effective: undefined });
  await tick();
  assert.match(h.query('[data-auth-message]').textContent, /授权已撤销/);
  assert.match(h.dialog.textContent, /父任务授权已撤销/);
  assert.deepEqual([...h.dialog.querySelectorAll('.agent-auth-grant-heading .agent-auth-status')].map(node => node.textContent), ['已撤销', '未生效']);
  h.click('[data-auth-action="audit"]');
  await tick();
  assert.match(h.query('.agent-auth-audit-list').textContent, /撤销授权/);
});

test('撤销失败不伪报成功，可保留确认后重试；返回非 revoked 也不能当成功', async () => {
  let writes = 0;
  const h = harness(async (url, options) => {
    if (options.method !== 'POST') return list();
    if (++writes === 1) throw new Error('服务不可用');
    return grant();
  });
  await h.page.open();
  h.click('[data-auth-action="revoke"]');
  h.click('[data-auth-action="confirm"]');
  await tick();
  assert.match(h.query('[role="alert"]').textContent, /撤销未确认.*服务不可用/);
  assert.equal(h.query('[data-auth-action="confirm"]').disabled, false);
  assert.doesNotMatch(h.query('[data-auth-message]').textContent, /授权已撤销/);
  h.click('[data-auth-action="confirm"]');
  await tick();
  assert.match(h.query('[role="alert"]').textContent, /未收到撤销确认/);
  h.click('[data-auth-action="cancel"]');
  assert.equal(h.query('.agent-auth-status').textContent, '有效');
});

test('父授权撤销成功但重读失败时，已知后代立即失效且不被旧审计覆盖', async () => {
  const audit = deferred(), revoke = deferred();
  let reads = 0;
  const h = harness(async (url, options) => {
    if (options.method === 'POST') return revoke.promise;
    if (url.includes('/grant-a?')) return audit.promise;
    if (++reads > 1) throw new Error('刷新失败');
    return list([grant(), grant({ id: 'child', parent_grant_id: 'grant-a' })]);
  });
  await h.page.open();
  h.click('[data-auth-action="audit"]');
  h.click('[data-auth-action="revoke"]');
  h.click('[data-auth-action="confirm"]');
  revoke.resolve(grant({ status: 'revoked' }));
  await tick();
  audit.resolve(detail({ audit: [{ action: '旧审计', allowed: true }] }));
  await tick();
  assert.match(h.query('[data-auth-message]').textContent, /授权已撤销/);
  assert.match(h.query('[role="alert"]').textContent, /刷新失败/);
  assert.deepEqual([...h.dialog.querySelectorAll('.agent-auth-grant-heading .agent-auth-status')].map(node => node.textContent), ['已撤销', '未生效']);
  assert.doesNotMatch(h.dialog.textContent, /旧审计/);
});

test('切用户立即清空弹窗；旧列表成功或失败均不会覆盖新身份', async () => {
  for (const rejected of [false, true]) {
    const old = deferred();
    const h = harness(url => url.endsWith('chen') ? old.promise : Promise.resolve(list([grant({ id: 'new', portfolio_ids: ['b'] })])));
    const opening = h.page.open();
    h.switchUser('bob', [{ id: 'b', name: '新用户组合' }]);
    assert.equal(h.dialog.open, false);
    assert.equal(h.query('[data-auth-content]').textContent, '');
    await h.page.open();
    if (rejected) old.reject(new Error('旧用户读取失败'));
    else old.resolve(list([grant({ portfolio_ids: ['旧用户机密'] })]));
    await opening;
    assert.match(h.dialog.textContent, /新用户组合/);
    assert.doesNotMatch(h.dialog.textContent, /旧用户/);
    assert.equal(h.query('[role="alert"]'), null);
  }
});

test('切用户后的旧审计与撤销回包不显示在新用户弹窗，也不为新用户发刷新', async () => {
  const audit = deferred(), revoke = deferred();
  const h = harness((url, options) => {
    if (options.method === 'POST') return revoke.promise;
    if (url.includes('/grant-a?')) return audit.promise;
    return Promise.resolve(list());
  });
  await h.page.open();
  h.click('[data-auth-action="audit"]');
  h.click('[data-auth-action="revoke"]');
  h.click('[data-auth-action="confirm"]');
  h.switchUser('bob', [{ id: 'portfolio-a', name: '新组合' }]);
  await h.page.open();
  const count = h.calls.length;
  audit.resolve(detail({ audit: [{ action: '旧用户审计', reason: '机密' }] }));
  revoke.resolve(grant({ status: 'revoked' }));
  await tick();
  assert.equal(h.calls.length, count);
  assert.match(h.dialog.textContent, /新组合/);
  assert.doesNotMatch(h.dialog.textContent, /旧用户审计|机密|授权已撤销/);
  assert.equal(h.query('.agent-auth-status').textContent, '有效');
});

test('用户 A-B-A 切换和关闭重开均不消费旧列表；序号隔离不只比较用户名', async () => {
  const old = deferred();
  let count = 0;
  const h = harness(() => ++count === 1 ? old.promise : Promise.resolve(list([grant({ portfolio_ids: ['最新记录'] })])));
  const opening = h.page.open();
  h.switchUser('bob');
  h.switchUser('chen');
  await h.page.open();
  old.resolve(list([grant({ portfolio_ids: ['旧记录'] })]));
  await opening;
  assert.match(h.dialog.textContent, /最新记录/);
  assert.doesNotMatch(h.dialog.textContent, /组合 旧记录/);
  h.page.close();
  const reopened = h.page.open();
  await reopened;
  await tick();
  assert.equal(h.dialog.open, true);
  assert.match(h.dialog.textContent, /最新记录/);
});

test('撤销中关闭再打开仍保留写锁，旧写回执只引发新 GET，不重复撤销', async () => {
  const pending = deferred();
  let revoked = false;
  const h = harness(async (url, options) => options.method === 'POST' ? pending.promise
    : list([grant(revoked ? { status: 'revoked', effective: false } : {})]));
  await h.page.open();
  h.click('[data-auth-action="revoke"]');
  h.click('[data-auth-action="confirm"]');
  h.escape();
  assert.equal(h.dialog.open, false);
  await h.page.open();
  assert.match(h.dialog.textContent, /稳健组合/);
  assert.equal(h.query('[data-auth-action="revoke"]').disabled, true);
  h.click('[data-auth-action="revoke"]');
  assert.equal(h.calls.filter(call => call.options.method === 'POST').length, 1);
  revoked = true;
  pending.resolve(grant({ status: 'revoked' }));
  await tick();
  assert.equal(h.query('.agent-auth-status').textContent, '已撤销');
  assert.equal(h.query('[data-auth-refresh]').disabled, false);
  assert.doesNotMatch(h.query('[data-auth-message]').textContent, /授权已撤销/);
});

test('无切用户通知时，点击旧内容也不能用新身份撤销旧授权', async () => {
  const h = harness();
  await h.page.open();
  h.click('[data-auth-action="revoke"]');
  h.switchUser('bob', [], false);
  h.click('[data-auth-action="confirm"]');
  assert.equal(h.dialog.open, false);
  assert.equal(h.calls.filter(call => call.options.method === 'POST').length, 0);
  assert.equal(h.query('[data-auth-content]').textContent, '');
});
