/* 独立报告组件只负责呈现；取数、Job 和对话生命周期仍由工作区负责。 */
(function(root) {
  'use strict';
  const esc = value => String(value ?? '').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const visuals = typeof module !== 'undefined' && module.exports ? require('./data-visuals') : root.FundScopeDataVisuals;
  const sourceUrl = value => { try {const u=new URL(value);return ['https:','http:'].includes(u.protocol)&&!u.username&&!u.password?u.href:'';}catch{return '';} };
  const list = rows => `<ul>${rows.map(row=>`<li>${esc(row)}</li>`).join('')}</ul>`;

  // 后端为旧页面保留了前三条/其余条目；Web 先还原完整顺序再分类，不能漏掉后面的支撑或中性观点。
  function groupFindings(d) {
    const groups={caution:[],supportive:[],neutral:[]};
    for(const item of [...(d.findings || []),...(d.more_findings || [])]) {
      const tone=['caution','supportive'].includes(item.tone)?item.tone:'neutral';
      groups[tone].push(item);
    }
    return groups;
  }

  function finding(item) {
    // 一个展开入口对应一条观点，解释、持仓关系和原始证据放在一起，不再分散到其他区域。
    return `<details class="diagnosis-finding" data-finding-id="${esc(item.id)}">
      <summary><i data-lucide="chevron-down" aria-hidden="true"></i><span class="diagnosis-finding-title">${esc(item.title)}</span><span class="diagnosis-reference-count">${item.references.length} 项依据</span></summary>
      <div class="diagnosis-finding-body"><p>${esc(item.detail)}</p><p class="diagnosis-meta">${esc(item.scope)}</p>
        ${item.gap?`<p class="diagnosis-gap">${esc(item.gap)}</p>`:''}
        ${item.holdings.map(row=>`<p class="diagnosis-holding">${esc(row.name)} <span>报告时占比 ${esc(row.weight)} · 绝对波动风险占比 ${esc(row.risk_share)}</span></p>`).join('')}
        ${item.references.map(ref=>`<div class="diagnosis-evidence"><strong>${esc(ref.label)} <span>${esc(ref.value)}</span></strong>${ref.explanation?`<p>${esc(ref.explanation)}</p>`:''}<p class="diagnosis-meta">${esc(ref.date)} · ${esc(ref.source)}</p><small>${esc(ref.id)}</small>${sourceUrl(ref.url)?`<a href="${esc(sourceUrl(ref.url))}" target="_blank" rel="noopener noreferrer">查看原始来源</a>`:''}</div>`).join('')}
      </div></details>`;
  }

  function findingGroup(tone, title, items, empty) {
    // 空分类只占一行；不人为补写机会，也不留一个与风险列表等高的空白栏。
    return `<section class="diagnosis-finding-group" data-side="${tone}" data-empty="${!items.length}">
      <header><h4>${title}</h4><span>${items.length} 条</span></header>
      <div class="diagnosis-group-items">${items.length?items.map(finding).join(''):`<p class="diagnosis-meta">${empty}</p>`}</div></section>`;
  }
  function render({report, metrics='', comparison='', adjustment='', audit='', canCompare=false, history=[], historyNotice=''}) {
    const d=report.diagnosis;
    if(!d)return `<p role="status">正在读取完整报告展示数据。</p><button data-review-report="${esc(report.run_id)}">重新读取报告</button>`;
    const groups=groupFindings(d);
    const riskRows=[...(report.quant_risk_snapshot?.risk_contributions || [])].sort((a,b)=>(b.absolute_risk_share_pct ?? -Infinity)-(a.absolute_risk_share_pct ?? -Infinity));
    return `<article class="portfolio-diagnosis" aria-label="持仓诊断报告">
      <header class="diagnosis-heading"><a href="#assets/review"><i data-lucide="arrow-left"></i>返回收益与风险</a><span>${esc(d.interpretation_label)} · ${esc(report.research_plan?.label || (report.research_mode==='committee' || report.committee?'多角色研判':'轻量综合'))}</span><button class="primary-action compact" data-follow-report><i data-lucide="message-circle"></i>继续追问</button></header>
      <section class="diagnosis-conclusion" aria-label="研究结论">
        <h2>${esc(d.headline)}</h2>
        ${report.research_plan?.question ? `<p class="diagnosis-meta">研究问题：${esc(report.research_plan.question)}</p>` : ''}
        <p class="diagnosis-meta">${esc(d.portfolio_name)} · 报告日期 ${esc(d.report_date)} · 历史快照，非当前持仓实时结论</p>
        <div class="diagnosis-summary-grid"><div class="diagnosis-summary-copy">
        ${d.summary?`<p class="diagnosis-summary">${esc(d.summary)}</p>`:''}
        ${d.fallback_reason?`<p class="diagnosis-gap" role="status">${esc(d.fallback_reason)}</p>`:''}
        <p class="diagnosis-meta">${esc(d.performance_label || '业绩口径未记录')}</p>
        </div><aside class="diagnosis-key-data" aria-label="本报告的历史关键数据">${visuals.researchMetrics(report)}</aside></div>
      </section>
      <section class="diagnosis-section diagnosis-findings" aria-label="关键依据"><h3>关键依据</h3>
        ${findingGroup('caution','风险与反对理由',groups.caution,'本报告未列出明确风险观点，不代表不存在反对证据。')}
        ${findingGroup('supportive','机会与支撑',groups.supportive,'本报告未形成明确支撑观点，不等于没有机会。')}
        ${findingGroup('neutral','中性观察',groups.neutral,'本报告未单列中性观点。')}
      </section>
      <section class="diagnosis-section diagnosis-outlook"><h3>分歧与后续关注</h3>
        <div class="diagnosis-outlook-grid">
        <div class="diagnosis-outlook-group"><h4>分歧与信息缺口</h4>
          ${d.conflicts.length?list(d.conflicts):'<p class="diagnosis-meta">报告未记录明确反对理由，不代表不存在反对证据。</p>'}
          ${d.uncertainties.length?list(d.uncertainties):''}</div>
        <div class="diagnosis-outlook-group"><h4>接下来关注什么</h4><p>${esc(d.conditional_view || '报告尚未给出条件式观点。')}</p>${d.follow_ups.length?list(d.follow_ups):''}</div></div>
        ${d.questions.length?`<details class="diagnosis-suggested-questions"><summary>围绕这份报告继续问</summary><div class="diagnosis-questions">${d.questions.map((q,i)=>`<button class="diagnosis-question" data-diagnosis-question="${i}"><span>${esc(q)}</span><i data-lucide="arrow-up-right" aria-hidden="true"></i></button>`).join('')}</div></details>`:''}
      </section>
      <div class="diagnosis-appendix" aria-label="报告明细">
        ${researchCoverage(report)}
        <details class="diagnosis-section diagnosis-holdings"><summary>持仓影响与风险贡献<span>${d.holdings.length} 只基金</span></summary><p class="diagnosis-meta">${esc(d.risk_period)}</p>
          ${d.top_risk?`<p>报告中的最大历史波动风险来源：<strong>${esc(d.top_risk.name)}</strong>，绝对风险占比 ${esc(d.top_risk.risk_share)}。</p>`:'<p class="diagnosis-gap">报告未提供可确认的最大风险贡献基金。</p>'}
          ${riskRows.length?visuals.bars(riskRows.map(row=>({label:row.short_name||row.fund_code,value:row.absolute_risk_share_pct,text:visuals.finite(row.absolute_risk_share_pct)?`${visuals.number(row.absolute_risk_share_pct)}%`:'未知'})),{percent:true,label:'报告时绝对波动风险占比'}):''}
          <p class="diagnosis-meta">风险占比不等于持仓占比、亏损概率或未来亏损比例。</p>
          <div class="diagnosis-table" tabindex="0" role="region" aria-label="报告时持仓"><table><thead><tr><th>基金</th><th>报告时占比</th><th>绝对波动风险占比</th></tr></thead><tbody>${d.holdings.map(row=>`<tr><th>${esc(row.name)}<small>${esc(row.code)}</small></th><td>${esc(row.weight)}</td><td>${esc(row.risk_share)}</td></tr>`).join('')}</tbody></table></div>
        </details>
        <details class="diagnosis-section"><summary>完整指标与计算口径</summary>${metrics}${list(d.limitations)}</details>
        <details class="diagnosis-section diagnosis-history-section" ${comparison?'open':''}><summary>历史报告与后续研究</summary><div class="diagnosis-questions"><button class="secondary-action compact" data-run-portfolio-research><i data-lucide="play"></i>重新生成报告</button><button class="secondary-action compact" data-compare-portfolio-research ${canCompare?'':'disabled'}><i data-lucide="git-compare-arrows"></i>与上次对比</button></div>
          ${historyNotice?`<p class="diagnosis-meta">${esc(historyNotice)}</p>`:''}${history.map(row=>`<button class="diagnosis-history" data-review-report="${esc(row.job_id)}" ${row.job_id===report.run_id?'aria-current="true"':''}>${esc(row.headline || '历史报告')}<small>${esc(row.assessment_date || row.created_at || '')}${row.job_id===report.run_id?' · 当前报告':''}</small></button>`).join('')}${comparison}${adjustment}</details>
        <details class="diagnosis-section diagnosis-audit"><summary>数据来源与技术记录</summary><dl class="diagnosis-technical">${d.technical.map(row=>`<div><dt>${esc(row.label)}</dt><dd>${esc(row.value)}</dd></div>`).join('')}</dl>${audit}</details>
      </div>
    </article>`;
  }
  function researchCoverage(report) {
    const reports = report.market_snapshot?.fund_reports;
    const repair = report.market_snapshot?.research_repair;
    if (!reports && !repair) return '';
    const status = {available:'已取得两期摘录',partial:'部分覆盖',unavailable:'资料不足'};
    const stop = {complete:'检查完成',gaps_remain:'仍有缺口',manual_refresh_required:'等待刷新授权',budget_exhausted:'已达次数上限',time_budget_exhausted:'已达时间上限'};
    return `<details class="diagnosis-section research-coverage"><summary>季报证据与补查记录<span>${esc(status[reports?.status] || '本轮未读取季报')}</span></summary>
      ${reports ? `<p class="diagnosis-meta">覆盖 ${(reports.funds || []).filter(row=>row.status==='available').length} / ${(reports.requested_fund_codes || []).length} 只基金 · 仅为历史页内摘录</p>` : ''}
      ${(reports?.evidence || []).map(row=>`<details><summary>${esc(row.title)}</summary><blockquote>${esc(row.quote || row.summary)}</blockquote><p class="diagnosis-meta">披露日 ${esc(row.published_at || '未知')} · 文档 ${esc(row.document_id)}</p>${sourceUrl(row.source_url)?`<a href="${esc(sourceUrl(row.source_url))}" target="_blank" rel="noopener noreferrer">查看原文第 ${esc(row.page)} 页</a>`:''}</details>`).join('')}
      ${repair ? `<p class="diagnosis-meta">补查 ${esc(repair.used_rounds)} / ${esc(repair.max_rounds)} 次 · ${esc(stop[repair.stop_reason] || repair.stop_reason)}</p>
        ${list((repair.attempts || []).map(row=>`${row.action}：${({evidence_changed:'已补充证据',checked_no_change:'检查后无变化',failed:'失败，保留原证据'})[row.status] || row.status}`))}
        ${list((repair.remaining_gaps || []).map(row=>`${row.fund_code ? row.fund_code+'：' : ''}${row.detail}`))}` : ''}
    </details>`;
  }
  const api={render,groupFindings};if(typeof module!=='undefined'&&module.exports)module.exports=api;else root.FundScopePortfolioDiagnosis=api;
})(typeof window==='undefined'?globalThis:window);
