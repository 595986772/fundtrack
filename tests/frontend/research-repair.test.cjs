// 补查 UI 的纯函数测试，不发消息、不访问真实数据源。
const test = require("node:test");
const assert = require("node:assert/strict");
const {render, takeAuthorization} = require("../../app/static/research-repair.js");

test("授权只消费一次，终端不会借用助手授权", () => {
  const input = {checked: true};
  assert.equal(takeAuthorization(input, {terminal: true}), false);
  assert.equal(input.checked, true);
  assert.equal(takeAuthorization(input), true);
  assert.equal(takeAuthorization(input), false);
});
test("旧会话和无需补查的完整证据不增加空组件", () => {
  assert.equal(render(undefined), "");
  assert.equal(render([{attempts: [], remaining_gaps: []}]), "");
});
test("授权但没有缺口时显示未补查，不伪装成已更新", () => {
  const html = render([{public_refresh_authorized: true, public_refresh_enabled: true, stop_reason: "complete", used_rounds: 0, max_rounds: 2}]);
  assert.match(html, /未执行额外补查/);
  assert.match(html, /已用 0 \/ 2 轮/);
});
test("保留失败、前后覆盖、缺口类别，并转义来源文本", () => {
  const html = render([{
    stop_reason: "gaps_remain", used_rounds: 1, max_rounds: 2,
    attempts: [{label: "检查披露", status: "failed", detail: "<img src=x onerror=alert(1)>", before: {coverage_score_pct: 60}, after: {coverage_score_pct: 60}}],
    remaining_gaps: [{kind: "user_information", label: "投资期限"}, {kind: "public_data", label: "持仓披露"}],
  }]);
  assert.match(html, /读取失败/);
  assert.match(html, /60% → 60%/);
  assert.match(html, /待你补充/);
  assert.match(html, /公开数据/);
  assert.doesNotMatch(html, /<img/);
});
test("同报告不标为更新，预算耗尽可见", () => {
  const html = render([{stop_reason: "budget_exhausted", attempts: [{status: "checked_no_change"}]}]);
  assert.match(html, /已到补查上限/);
  assert.match(html, /证据状态未变/);
});
test("轨迹显示来源与披露日期，即使覆盖分数没变", () => {
  const html = render([{attempts: [{
    action: "disclosure_refresh", source: "AKShare / 东方财富定期披露", status: "evidence_changed",
    before: {coverage_score_pct: 60, requirements: [{key: "holdings_exposure", as_of_date: "2024-12-31"}]},
    after: {coverage_score_pct: 60, requirements: [{key: "holdings_exposure", as_of_date: "2026-06-30"}]},
  }]}]);
  assert.match(html, /AKShare \/ 东方财富/);
  assert.match(html, /2024-12-31/);
  assert.match(html, /2026-06-30/);
});
test("重试保留已用预算，不宣称又查了一遍", () => {
  const html = render([{public_refresh_authorized: true, prior_used_rounds: 2, used_rounds: 2, max_rounds: 2}]);
  assert.match(html, /此前已使用 2 轮/);
  assert.match(html, /不重复补查/);
});
