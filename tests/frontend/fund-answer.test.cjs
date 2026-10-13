// 只测试结构化回答的渲染，不发送聊天或读取真实用户会话。
const test = require("node:test");
const assert = require("node:assert/strict");
const {render, supports} = require("../../app/static/fund-answer.js");
const report = {
  version: "fund-answer-v1", fund_code: "012345", fund_name: "测试主题基金", status: "consistent",
  verification_scope: "仅核对工具事实，不验证模型推理。",
  facts: [{evidence_id: "fact:fund:012345:abc", fund_code: "012345", label: "历史最大回撤幅度", value: "18.00", unit: "%",
    period: "2025-08-31 至 2026-08-31", source_name: "本轮历史风险计算", source_path: "evidence_summary.historical_risk.max_drawdown.drawdown_pct",
    meaning: "历史跌幅，不是未来亏损上限。", source_url: "https://example.test/source"}],
  judgments: [{text: "波动明显，需考虑你的资金使用期限。", evidence_ids: ["fact:fund:012345:abc"]}],
  uncertainties: ["未取得最新披露。"],
};

test("老消息没有核对元数据时保持原有渲染", () => {
  assert.equal(render(undefined), "");
  assert.equal(render({version: "unsupported"}), "");
});
test("事实有原生展开控件，日期、口径、字段和值一一对应", () => {
  const html = render(report);
  assert.match(html, /<details class="fund-answer-fact">/);
  for (const value of ["18.00%", "2025-08-31 至 2026-08-31", "统计范围", "不是未来亏损上限", "引用编号"]) {
    assert.ok(html.includes(value), value);
  }
  assert.match(html, /rel="noopener noreferrer"/);
});
test("事实、模型判断与不确定性分开，引用显示指标名", () => {
  const html = render(report);
  for (const label of ["工具事实", "模型判断", "不确定性与待补充", "参考：历史最大回撤幅度", "未取得最新披露"]) {
    assert.ok(html.includes(label), label);
  }
});
test("修正和降级有明确状态，不冒充研究已验证", () => {
  assert.match(render({...report, status: "corrected"}), /关键事实已修正/);
  const html = render({...report, status: "limited", judgments: []});
  assert.match(html, /事实核对未完成/);
  assert.match(html, /本轮未展示模型判断/);
});
test("模型、来源、缺口中的 HTML 全部转义，不允许脚本链接", () => {
  const html = render({...report, fund_name: "<img src=x onerror=alert(1)>",
    facts: [{...report.facts[0], source_url: "javascript:alert(1)"}],
    judgments: [{text: "<script>alert(1)</script>", evidence_ids: []}], uncertainties: ["<iframe>"]});
  assert.doesNotMatch(html, /<img|<script|<iframe|href="javascript:/);
  assert.match(html, /&lt;script&gt;/);
});
test("空事实不补造数字", () => {
  const html = render({...report, facts: [], judgments: [], uncertainties: []});
  assert.match(html, /暂无可核对事实/);
  assert.doesNotMatch(html, /0\.00%/);
});
test("统一助手保留报告渲染及旧消息；基金详情不重复显示聊天答案", () => {
  const fs = require("node:fs");
  const path = require("node:path");
  for (const name of ["app.js"]) {
    const code = fs.readFileSync(path.join(__dirname, "../../app/static", name), "utf8");
    assert.match(code, /FundScopeFundAnswer\.render\(message\.run_metadata\?\.answer_check\)/);
  }
  const terminal = fs.readFileSync(path.join(__dirname, "../../app/static/terminal.js"), "utf8");
  assert.doesNotMatch(terminal, /function renderAI\(/);
  const app = fs.readFileSync(path.join(__dirname, "../../app/static/app.js"), "utf8");
  assert.match(app, /!window\.FundScopeFundAnswer\.supports\(message\.run_metadata\?\.answer_check\)/);
});

const explained = {
  ...report, version: "fund-answer-v2",
  judgments: [{text: "不宜把历史回撤当作未来亏损上限。", evidence_ids: [report.facts[0].evidence_id],
    supporting_evidence: [{evidence_id: report.facts[0].evidence_id, explanation: "统计结果只覆盖这个历史区间，不能为未来提供下限。"}],
    counter_evidence: [], supporting_evidence_gap: "", counter_evidence_gap: "本轮未找到适用的反向证据。",
    conditions: [
      {basis: "user_context", text: "你的安排允许更长观察期。", background_fields: ["horizon"]},
      {basis: "assumption", text: "假设用途不变。", background_fields: []},
      {basis: "missing_information", text: "还需补充可接受亏损。", background_fields: ["loss_tolerance"]},
    ], change_triggers: ["资金用途改变时需要重看。"]}],
  investment_background: {horizon: {label: "投资期限", text: "这笔钱三年不用", source_turn: "turn-1", updated_at: "2026-09-06"}},
};

test("新旧协议均可展示，旧判断不补造正反解释", () => {
  assert.ok(supports(report));
  assert.ok(supports(explained));
  assert.equal(supports({version: "fund-answer-v3"}), false);
  assert.doesNotMatch(render(report), /判断依据与适用条件|支持依据|反向依据/);
});
test("新判断先显示结论，再原生展开支持依据、反向缺口、条件与重看事项", () => {
  const html = render(explained);
  assert.match(html, /<details class="fund-answer-reasoning"><summary>判断依据与适用条件<\/summary>/);
  assert.ok(html.indexOf(explained.judgments[0].text) < html.indexOf('class="fund-answer-reasoning"'));
  for (const text of ["支持依据", "反向依据", "本轮未找到适用的反向证据", "用户已提供", "模型假设", "待你补充",
    "用户原话 · 投资期限：这笔钱三年不用", "什么变化需要重看", "资金用途改变时需要重看", "18.00%", report.facts[0].period]) {
    assert.ok(html.includes(text), text);
  }
  assert.doesNotMatch(html, /<details class="fund-answer-reasoning" open/);
});
test("正反证据独立展示自己的事实范围，不把不同披露日变成统一日期", () => {
  const counter = {...report.facts[0], evidence_id: "fact:counter", label: "持仓权重", value: "8", period: "2026-06-30"};
  const html = render({...explained, facts: [...explained.facts, counter], judgments: [{...explained.judgments[0],
    counter_evidence: [{evidence_id: counter.evidence_id, explanation: "集中持仓削弱分散配置的作用。"}], counter_evidence_gap: ""}]});
  assert.match(html, /集中持仓削弱分散配置的作用/);
  assert.match(html, /持仓权重：8%/);
  assert.match(html, /2026-06-30/);
  assert.match(html, /2025-08-31 至 2026-08-31/);
  assert.doesNotMatch(html, /本轮未找到适用的反向证据/);
});
test("新字段及用户原话也必须转义，无法找到的引用不会伪造数值", () => {
  const html = render({...explained,
    investment_background: {horizon: {label: "<script>", text: "<img onerror=alert(1)>"}},
    judgments: [{...explained.judgments[0], supporting_evidence: [{evidence_id: "<script>", explanation: "<iframe>"}],
      counter_evidence_gap: "<img>", change_triggers: ["<script>"],
      conditions: [{basis: "user_context", text: "<svg>", background_fields: ["horizon"]}]}]});
  assert.doesNotMatch(html, /<script>|<img|<iframe>|<svg>/);
  assert.match(html, /&lt;iframe&gt;/);
  assert.match(html, /用户原话/);
  assert.match(html, /引用事实暂不可用/);
});
