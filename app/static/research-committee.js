/* 多角色报告只呈现冻结结果；选择模式和展开依据都不触发请求。 */
(function (root) {
  "use strict";
  const esc = value => String(value ?? "").replace(/[&<>"']/g, char => ({"&":"&amp;", "<":"&lt;", ">":"&gt;", '"':"&quot;", "'":"&#39;"}[char]));
  const rows = value => Array.isArray(value) ? value : [];
  const objects = value => rows(value).filter(item => item && typeof item === "object");
  const roleNames = new Map([["opportunity", "机会研究员"], ["risk", "风险研究员"]]);
  const verdicts = new Map([["agree", "同意"], ["qualify", "有条件同意"], ["disagree", "不同意"]]);
  const normalizeMode = value => ["auto", "explain", "strategy", "committee"].includes(value) ? value : "standard";
  const recorded = value => value === null || value === undefined || value === "" ? "未记录" : value;
  const count = value => Number.isInteger(value) && value >= 0 ? String(value) : "未记录";
  const label = (stage, role) => stage?.label || roleNames.get(role) || "综合研判";
  const status = stage => stage?.status === "accepted" ? "已完成" : stage?.status === "failed" ? "失败" : stage?.status === "skipped" ? "未执行" : "未完成";

  function isCommitteeRun(job = {}, report = job?.report || job?.result) {
    return job?.research_mode === "committee" || job?.payload?.research_mode === "committee"
      || job?.total_steps === 13 || report?.research_mode === "committee" || Boolean(report?.committee)
      || report?.generation?.mode === "multi_agent_committee"
      || rows(report?.graph_trace?.completed_nodes).some(node => typeof node === "string" && node.startsWith("committee_"));
  }

  function renderProgress(job) {
    const total = Number.isInteger(job?.total_steps) && job.total_steps > 0 ? job.total_steps : null;
    if (total === null) return '<p class="committee-progress">等待确认分析计划</p>';
    const current = Number.isInteger(job?.current_step) ? Math.max(0, Math.min(total, job.current_step)) : 0;
    // 并行节点只知道完成数量时，不猜哪个研究员先完成，也不把模式路由算成已完成综合。
    return `<p class="committee-progress">${isCommitteeRun(job) ? '多角色' : '研究'}流程进度 · ${current} / ${total} 阶段</p>`;
  }

  function sourceUrl(value) {
    try {
      const url = new URL(value);
      return ["http:", "https:"].includes(url.protocol) && !url.username && !url.password ? url.href : "";
    } catch { return ""; }
  }

  function modeControl(mode = "standard", disabled = false, draft = {}) {
    const selected = normalizeMode(mode);
    const attrs = disabled ? "disabled" : "";
    return `<div class="research-request-fields"><label class="research-question-control"><span>研究问题</span>
      <textarea data-research-question maxlength="800" rows="2" ${attrs} placeholder="例如：比较持仓基金两期季报的策略变化">${esc(draft.question || "")}</textarea></label>
      <label class="research-mode-control"><span>研究模式</span>
      <select data-portfolio-research-mode aria-label="研究模式" ${disabled ? "disabled" : ""}>
        <option value="auto" ${selected === "auto" ? "selected" : ""}>按问题选择</option>
        <option value="explain" ${selected === "explain" ? "selected" : ""}>配置解释</option>
        <option value="strategy" ${selected === "strategy" ? "selected" : ""}>季报策略比较</option>
        <option value="standard" ${selected === "standard" ? "selected" : ""}>综合分析</option>
        <option value="committee" ${selected === "committee" ? "selected" : ""}>多角色研判（额外模型用量）</option>
      </select></label>
      <label class="research-refresh-control"><input type="checkbox" data-research-refresh ${draft.allow_public_refresh ? "checked" : ""} ${attrs}>本轮允许补查公开季报与事件</label>
      <details class="research-personal-fields"><summary>个人投资条件（可选）</summary><div>
        ${[["horizon", "持有期限"], ["loss_tolerance", "可接受亏损"], ["liquidity", "近期用款需求"]].map(([key, name]) =>
          `<label><span>${name}</span><input data-research-personal="${key}" maxlength="240" value="${esc(draft.investor_context?.[key] || "")}" ${attrs}></label>`).join("")}
      </div></details></div>`;
  }

  function uncertainties(stage) {
    const items = rows(stage?.uncertainties);
    return items.length ? `<div class="committee-uncertainties"><strong>待核验</strong><ul>${items.map(item => `<li>${esc(item)}</li>`).join("")}</ul></div>` : "";
  }

  function evidenceCatalog(report) {
    return new Map(Object.values(report.key_evidence || {}).flatMap(objects)
      .filter(item => item.evidence_id).map(item => [item.evidence_id, item]));
  }

  function finding(item, catalog, targetClaimId = "") {
    const metrics = objects(item.metric_references);
    const refs = new Map(objects(item.evidence_references).map(ref => [ref.evidence_id, ref]));
    const evidenceIds = [...new Set([...rows(item.evidence_ids), ...refs.keys()])].filter(Boolean);
    const unresolvedMetrics = rows(item.metric_keys).filter(key => !metrics.some(metric => metric.key === key));
    const claim = item.claim_id || targetClaimId;
    return `<article class="committee-finding"><h5>${esc(item.title || "研究发现")}</h5><p>${esc(item.detail)}</p>
      ${metrics.length ? `<dl class="committee-metrics">${metrics.map(metric => `<div><dt>${esc(metric.label || metric.key)}</dt><dd>${esc(recorded(metric.value_text ?? metric.value))}</dd></div>`).join("")}</dl>` : ""}
      ${!metrics.length && !evidenceIds.length ? '<p class="committee-gap">观点未附可核验的指标或证据。</p>' : ""}
      ${unresolvedMetrics.length ? '<p class="committee-gap">部分指标引用尚未解析。</p>' : ""}
      ${metrics.length || evidenceIds.length || claim || unresolvedMetrics.length ? `<details class="committee-references"><summary>依据与来源 · ${metrics.length + evidenceIds.length} 项</summary>
        ${metrics.map(metric => `<div class="committee-evidence"><strong>${esc(metric.label || metric.key)} · ${esc(recorded(metric.value_text ?? metric.value))}</strong><small>报告内计算指标 · ${esc(recorded(metric.data_date))}</small></div>`).join("")}
        ${evidenceIds.map(id => {
          const ref = {...catalog.get(id), ...refs.get(id)};
          const url = sourceUrl(ref.source_url);
          return `<div class="committee-evidence"><strong>${esc(ref.title || id)}${ref.value_text !== undefined && ref.value_text !== null ? ` · ${esc(ref.value_text)}` : ""}</strong>
            <p>${esc(ref.summary || ref.explanation || "报告未保留该引用的解释。")}</p>
            <small>${esc(recorded(ref.data_date))} · ${esc(ref.source_name || "报告未保留来源明细")}</small>
            ${url ? `<a href="${esc(url)}" target="_blank" rel="noopener noreferrer">查看原始来源</a>` : ""}</div>`;
        }).join("")}
        ${unresolvedMetrics.length ? `<small>未解析指标：${unresolvedMetrics.map(esc).join("、")}</small>` : ""}
        ${claim ? `<small>${item.claim_id ? "观点编号" : "回应目标"}：<code>${esc(claim)}</code></small>` : ""}
      </details>` : ""}</article>`;
  }

  function roleView(role, stage, catalog) {
    const accepted = stage?.status === "accepted";
    const findings = accepted ? objects(stage.key_findings) : [];
    return `<section class="committee-role" data-committee-role="${role}">
      <header><h4>${esc(label(stage, role))}</h4><span class="committee-stage-status" data-complete="${accepted}">${status(stage)}</span></header>
      ${accepted ? `<p class="committee-headline"><strong>${esc(stage.headline)}</strong></p><p>${esc(stage.summary)}</p>
        ${findings.length ? findings.slice(0, 2).map(item => finding(item, catalog)).join("") : '<p class="committee-gap">尚无有效观点，不代表没有机会或风险。</p>'}
        ${findings.length > 2 ? `<details><summary>其余 ${findings.length - 2} 条观点</summary>${findings.slice(2).map(item => finding(item, catalog)).join("")}</details>` : ""}`
        : `<p class="committee-gap">${esc(stage?.failure_reason || "该角色未返回通过校验的分析。")}</p>`}
      ${uncertainties(stage)}</section>`;
  }

  function reviewView(role, stage, claims, catalog) {
    const accepted = stage?.status === "accepted";
    const responses = accepted ? objects(stage.responses) : [];
    return `<section class="committee-review"><header><h4>${esc(label(stage, role))}回应</h4><span class="committee-stage-status" data-complete="${accepted}">${status(stage)}</span></header>
      ${accepted ? responses.map(response => {
        const target = claims.get(response.target_claim_id);
        const verdict = verdicts.get(response.verdict);
        return `<div class="committee-response"><p><strong class="committee-verdict" data-verdict="${verdict ? response.verdict : "unknown"}">${verdict || "回应待核验"}</strong>
          <span>回应：${target ? esc(`${target.label} · ${target.title}`) : "目标观点未保留，无法核对"}</span></p>
          ${finding(response.finding && typeof response.finding === "object" ? response.finding : {}, catalog, response.target_claim_id)}</div>`;
      }).join("") || '<p class="committee-gap">未保留有效回应，不能视为达成一致。</p>'
        : `<p class="committee-gap">${esc(stage?.failure_reason || "本轮质询未完成，不能视为达成一致。")}</p>`}
      ${uncertainties(stage)}</section>`;
  }

  function render(report) {
    const committee = report?.committee;
    if (!committee && report?.generation?.mode !== "multi_agent_committee") return "";
    if (!committee || committee.version !== "portfolio-committee-v1") {
      return '<section class="research-committee diagnosis-section" aria-label="多角色研判"><h3>多角色研判</h3><p class="committee-alert" role="status">多角色研判记录缺失或版本不受支持，无法核对各角色与质询结果。</p></section>';
    }
    const roles = new Map(objects(committee.roles).map(stage => [stage.role, stage]));
    const reviews = new Map(objects(committee.reviews).map(stage => [stage.role, stage]));
    const stages = [
      ...[...roleNames.keys()].map(role => ({stage: roles.get(role), name: `${label(roles.get(role), role)}分析`, resultKey: "key_findings"})),
      ...[...roleNames.keys()].map(role => ({stage: reviews.get(role), name: `${label(reviews.get(role), role)}回应`, resultKey: "responses"})),
      {stage: committee.synthesis, name: "综合研判"},
    ];
    const stageStatus = ({stage, resultKey}) => stage?.status === "accepted" && resultKey && !objects(stage[resultKey]).length ? "记录不全" : status(stage);
    const fallback = report.generation?.mode === "deterministic_fallback";
    const complete = committee.status === "completed" && !fallback && stages.every(item => stageStatus(item) === "已完成");
    const summary = fallback ? "综合结论已回退为规则说明，非多角色共识。" : "多角色研判未完整完成，已保留通过校验的角色结果。";
    const noticeKey = value => String(value ?? "").trim().replace(/[。.!！；;]+$/, "");
    // 详细失败原因已在对应阶段保留，warnings 只补充尚未呈现的信息。
    const covered = new Set([summary, ...stages.map(item => `${item.name}：${stageStatus(item)}`),
      ...stages.map(({stage}) => stage?.failure_reason)].map(noticeKey));
    const warnings = [...new Map(rows(committee.warnings).map(item => [noticeKey(item), item]))]
      .filter(([key]) => key && !covered.has(key)).map(([, item]) => item);
    const catalog = evidenceCatalog(report);
    const claims = new Map();
    for (const role of roleNames.keys()) {
      const stage = roles.get(role);
      if (stage?.status === "accepted") objects(stage.key_findings).forEach(item => {
        if (item.claim_id) claims.set(item.claim_id, {title: item.title || "研究发现", label: label(stage, role)});
      });
    }
    return `<section class="research-committee diagnosis-section" aria-label="多角色研判" data-status="${complete ? "completed" : "degraded"}">
      <header class="committee-heading"><h3>多角色研判</h3><strong class="committee-stage-status" data-complete="${complete}">${complete ? "已完成" : "降级结果 · 研判未完成"}</strong></header>
      ${!complete ? `<div class="committee-alert" role="status"><p>${summary}</p><ul class="committee-stage-summary">${stages.map(item => `<li>${esc(item.name)}：${stageStatus(item)}</li>`).join("")}</ul></div>` : ""}
      ${warnings.length ? `<ul class="committee-warnings">${warnings.map(item => `<li>${esc(item)}</li>`).join("")}</ul>` : ""}
      <details class="committee-analysis"><summary>研究员独立观点<span>机会研究员 / 风险研究员</span></summary>
        <div class="committee-columns">${[...roleNames.keys()].map(role => roleView(role, roles.get(role), catalog)).join("")}</div></details>
      <details class="committee-cross-review"><summary>一次相互质询${[...roleNames.keys()].some(role => reviews.get(role)?.status !== "accepted" || !objects(reviews.get(role)?.responses).length) ? " · 未完成" : ""}</summary>
        <div class="committee-columns">${[...roleNames.keys()].map(role => reviewView(role, reviews.get(role), claims, catalog)).join("")}</div></details>
      <details class="committee-diagnostics"><summary>研判阶段记录</summary><dl>
        <div><dt>冻结快照</dt><dd>${esc(recorded(committee.snapshot_id))}</dd></div>
        <div><dt>角色调用</dt><dd>${count(committee.model_calls)}${count(committee.model_calls) !== "未记录" ? " 次" : ""}</dd></div>
        ${stages.map(({stage, name}) => `<div><dt>${esc(name)}</dt><dd>${status(stage)} · 角色调用 ${count(stage?.model_calls)} · ${typeof stage?.duration_ms === "number" && Number.isFinite(stage.duration_ms) && stage.duration_ms >= 0 ? `${(stage.duration_ms / 1000).toFixed(1)} 秒` : "耗时未记录"}${stage?.failure_reason ? `<small>${esc(stage.failure_reason)}</small>` : ""}</dd></div>`).join("")}
      </dl></details></section>`;
  }

  function enhanceReport(markup, {report, mode = "standard", draft = {}, document = root.document}) {
    // 在已有报告的语义节点接入，保留主结论、历史操作与无展示数据时的只读恢复入口。
    if (!report.diagnosis) return markup;
    const template = document.createElement("template");
    template.innerHTML = markup;
    const view = template.content.querySelector(".portfolio-diagnosis");
    if (!view) return markup;
    const run = view.querySelector("[data-run-portfolio-research]");
    if (run) {
      const control = document.createElement("template");
      control.innerHTML = modeControl(mode, false, draft);
      run.before(control.content);
    }
    const discussion = document.createElement("template");
    discussion.innerHTML = render(report);
    // 最终结论与依据先读完，再按需核对研究员原始观点，避免两套分析抢占报告开头。
    const processAnchor = view.querySelector(".diagnosis-appendix");
    if (processAnchor) processAnchor.before(discussion.content);
    else view.append(discussion.content);
    return template.innerHTML;
  }

  const api = {normalizeMode, modeControl, isCommitteeRun, renderProgress, render, enhanceReport};
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.FundScopeResearchCommittee = api;
})(typeof window === "undefined" ? globalThis : window);
