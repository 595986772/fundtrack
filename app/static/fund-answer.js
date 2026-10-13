/* 用后端已核对的结构渲染单基金回答。所有外部文本转义，不直接插入模型 HTML。 */
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.FundScopeFundAnswer = api;
})(typeof window !== "undefined" ? window : globalThis, function () {
  const escape = (value) => String(value ?? "").replace(/[&<>"']/g, (char) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[char]));
  const statuses = {consistent: "关键事实与工具一致", corrected: "关键事实已修正", limited: "事实核对未完成"};
  const supports = (report) => ["fund-answer-v1", "fund-answer-v2"].includes(report?.version);
  const conditionLabels = {user_context: "用户已提供", assumption: "模型假设", missing_information: "待你补充"};

  function sourceLink(value) {
    // 只接受绝对 HTTP(S) 来源；javascript:、data: 等不是可打开的证据来源。
    try {
      const url = new URL(value);
      if (!["https:", "http:"].includes(url.protocol)) return "";
      return `<a href="${escape(url.href)}" target="_blank" rel="noopener noreferrer">查看原始来源</a>`;
    } catch { return ""; }
  }

  function renderRelations(title, relations, gap, byId) {
    // 名称、数值、统计期只读后端事实表；模型只能补充二者的关系说明。
    return `<h5>${title}</h5>${relations.length ? `<ul class="fund-answer-relations">${relations.map((relation) => {
      const fact = byId.get(relation.evidence_id);
      return `<li><p>${escape(relation.explanation)}</p>
        ${fact ? `<p class="fund-answer-reference-fact"><strong>${escape(fact.label)}：${escape(fact.value)}${escape(fact.unit)}</strong><br>${escape(fact.period)}</p>
          <p class="fund-answer-references">${escape(fact.meaning)}<br>${escape(fact.source_name)} ${sourceLink(fact.source_url)}</p>` : "<p>引用事实暂不可用。</p>"}
        <p class="fund-answer-references"><code>${escape(relation.evidence_id)}</code></p></li>`;
    }).join("")}</ul>` : ""}${gap ? `<p class="fund-answer-gap">${escape(gap)}</p>` : ""}`;
  }

  function renderJudgment(judgment, report, byId) {
    const conclusion = `<p>${escape(judgment.text)}</p>`;
    // 旧历史没有正反分类，不擅自给旧引用补上“支持”之类的新含义。
    if (report.version === "fund-answer-v1") {
      return `<div class="fund-answer-judgment">${conclusion}
        ${judgment.evidence_ids?.length ? `<p class="fund-answer-references">参考：${judgment.evidence_ids.map((id) => escape(byId.get(id)?.label || id)).join("、")}</p>` : ""}</div>`;
    }
    return `<div class="fund-answer-judgment">${conclusion}
      <div class="dv-research-sides">${[['支持依据',judgment.supporting_evidence,'supportive',judgment.supporting_evidence_gap],['反向依据',judgment.counter_evidence,'caution',judgment.counter_evidence_gap]].map(([label,rows,tone,gap])=>`<section data-side="${tone}"><h3>${label}</h3>${rows?.length?`<ul>${rows.slice(0,3).map(row=>{const fact=byId.get(row.evidence_id);return `<li>${escape(fact?`${fact.label}：${fact.value}${fact.unit}`:'引用事实暂不可用')}<small class="dv-note">${escape(fact?.period || '')}</small></li>`;}).join('')}</ul>`:`<p class="dv-note">${escape(gap || '暂无已引用依据')}</p>`}</section>`).join('')}</div>
      <details class="fund-answer-reasoning"><summary>判断依据与适用条件</summary>
        ${renderRelations("支持依据", judgment.supporting_evidence || [], judgment.supporting_evidence_gap, byId)}
        ${renderRelations("反向依据", judgment.counter_evidence || [], judgment.counter_evidence_gap, byId)}
        <h5>适用条件</h5><ul class="fund-answer-conditions">${(judgment.conditions || []).map((condition) => `<li>
          <p><strong>${escape(conditionLabels[condition.basis] || "条件")}：</strong>${escape(condition.text)}</p>
          ${condition.basis === "user_context" ? (condition.background_fields || []).map((key) => {
            const fact = report.investment_background?.[key];
            return fact ? `<p class="fund-answer-user-quote">用户原话 · ${escape(fact.label)}：${escape(fact.text)}</p>` : "";
          }).join("") : ""}</li>`).join("")}</ul>
        <h5>什么变化需要重看</h5><ul>${(judgment.change_triggers || []).map((trigger) => `<li>${escape(trigger)}</li>`).join("")}</ul>
      </details></div>`;
  }

  function render(report) {
    if (!supports(report)) return "";
    const facts = Array.isArray(report.facts) ? report.facts : [];
    const judgments = Array.isArray(report.judgments) ? report.judgments : [];
    const uncertainties = Array.isArray(report.uncertainties) ? report.uncertainties : [];
    const byId = new Map(facts.map((fact) => [fact.evidence_id, fact]));
    return `<section class="fund-answer" aria-label="单基金研究回答">
      <h3>${escape(report.fund_name)} <span>${escape(report.fund_code)}</span></h3>
      <p class="fund-answer-status" data-status="${escape(report.status)}">${escape(statuses[report.status] || "事实核对记录")}</p>
      <p class="fund-answer-scope">${escape(report.verification_scope)}</p>
      <h4>模型判断 <span>解释与条件式观点</span></h4>
      ${judgments.length ? judgments.map((judgment) => renderJudgment(judgment, report, byId)).join("") : "<p>本轮未展示模型判断。</p>"}
      <h4>工具事实</h4><dl class="dv-metrics">${facts.slice(0,6).map(fact=>`<div><dt>${escape(fact.label)}</dt><dd>${escape(fact.value)}${escape(fact.unit)}</dd><small>${escape(fact.period)}</small></div>`).join('')}</dl>
      <details class="dv-detail"><summary>全部事实与来源 · ${facts.length} 项</summary>
      ${facts.length ? facts.map((fact) => `<details class="fund-answer-fact">
        <summary><span>${escape(fact.label)}</span> <strong>${escape(fact.value)}${escape(fact.unit)}</strong></summary>
        <dl>
          <div><dt>对象</dt><dd>${escape(report.fund_name)} · ${escape(fact.fund_code)}</dd></div>
          <div><dt>统计范围</dt><dd>${escape(fact.period)}</dd></div>
          <div><dt>口径</dt><dd>${escape(fact.meaning)}</dd></div>
          <div><dt>数据来源</dt><dd>${escape(fact.source_name)} ${sourceLink(fact.source_url)}</dd></div>
          <div><dt>工具字段</dt><dd><code>${escape(fact.source_path)}</code></dd></div>
          <div><dt>引用编号</dt><dd><code>${escape(fact.evidence_id)}</code></dd></div>
        </dl>
      </details>`).join("") : "<p>暂无可核对事实。</p>"}
      </details>
      <h4>不确定性与待补充</h4>
      ${uncertainties.length ? `<ul>${uncertainties.map((item) => `<li>${escape(item)}</li>`).join("")}</ul>` : "<p>未列出额外缺口，不代表没有投资风险。</p>"}
    </section>`;
  }
  return {render, supports};
});
