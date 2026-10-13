/* 收益与风险页独立拥有读取状态，只消费既有计算结果，不发起采集或模型任务。 */
(function (root) {
  "use strict";
  const esc = value => String(value ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
  const visuals = typeof module !== "undefined" && module.exports ? require('./data-visuals') : root.FundScopeDataVisuals;
  function create({context, request, changed, readPerformance}) {
    let key = "", generation = 0, loading = false, error = "", data = null, months = 12;
    const currentKey = () => { const c = context(); return `${c.user}:${c.portfolio}:${months}:${c.revision || ""}`; };
    async function load({force = false} = {}) {
      const c = context(), next = currentKey();
      if (!c.portfolio || (!force && next === key && (loading || data))) return;
      key = next; const version = ++generation;
      loading = true; error = ""; data = null; changed();
      try {
        // Web 额外读取已有业绩接口；不改变小程序使用的摘要接口，也不重复执行模型。
        const results = await Promise.allSettled([
          request(`/api/v1/portfolios/${encodeURIComponent(c.portfolio)}/review?${new URLSearchParams({user_id:c.user,months})}`),
          readPerformance ? readPerformance(c, months) : Promise.resolve(null),
        ]);
        if (results[0].status === 'rejected') throw results[0].reason;
        const result = results[0].value;
        if (results[1].status === 'fulfilled' && results[1].value) result.performance = results[1].value;
        if (results[1].status === 'rejected') result.chartError = results[1].reason.message;
        // 晚到的数据只能更新发起时的用户、账本与区间。
        if (version === generation && key === currentKey()) data = result;
      } catch (e) { if (version === generation && key === currentKey()) error = e.message; }
      finally { if (version === generation) { loading = false; changed(); } }
    }
    function render() {
      const valid = key === currentKey(), result = valid ? data : null;
      const p = result?.performance, a = result?.assessment, report = result?.latest_report;
      const holdings = [...(result?.portfolio?.holdings || [])].sort((x,y)=>(y.weight_pct ?? -Infinity)-(x.weight_pct ?? -Infinity));
      return `<div class="asset-review">
        <header class="asset-review-heading"><h2>收益与风险</h2><div class="asset-review-actions"><button class="primary-action compact" data-review-research ${!result?.portfolio?.holdings?.length ? 'disabled' : ''}><i data-lucide="sparkles"></i>分析当前配置</button><button class="icon-button" data-review-refresh title="重读已保存数据" aria-label="重读收益与风险" ${loading ? "disabled" : ""}><i data-lucide="refresh-cw"></i></button></div></header>
        <div class="period-control" role="group" aria-label="分析区间">${[6,12,36].map(m => `<button data-review-months="${m}" aria-pressed="${months===m}" class="${months===m?'active':''}">${m===6?'6个月':m===12?'1年':'3年'}</button>`).join('')}</div>
        ${loading || !valid ? '<p role="status">正在读取收益与风险…</p>' : error ? `<p role="alert">${esc(error)}</p>` : ''}
        ${result ? `<section class="asset-review-section">${visuals.performanceMetrics(p)}
          ${result.chartError ? `<p class="dv-attention" role="status">曲线读取失败：${esc(result.chartError)}。可点击重读重试。</p>` : ''}
          <div class="dv-split">${visuals.trend(p)}${visuals.trend(p,{drawdown:true})}</div>
          ${(p?.observation_count != null && p.observation_count < 30) ? '<p class="dv-attention">历史样本不足 30 个观测日，波动和回撤仅反映这段短期记录。</p>' : ''}
          <details class="dv-detail"><summary>业绩口径与指标说明</summary>${(p?.warnings || []).map(w=>`<p>${esc(w)}</p>`).join('')}<p>时间加权收益排除投入、取出资金的影响；年化波动描述历史起伏，不能解释成未来亏损概率。</p><a href="#assets/performance">完整业绩与归因</a></details></section>
          <section class="asset-review-section"><div class="dv-split"><div><h3>持仓集中度</h3>
            ${visuals.bars(holdings.map(row=>({label:row.short_name||row.fund_code,value:row.weight_pct,text:visuals.finite(row.weight_pct)?`${visuals.number(row.weight_pct)}%`:'未知'})),{percent:true,label:'当前组合单只基金占比'})}
            <p class="dv-note">占比按当前市值计算，不等于风险贡献比例。</p></div><div><h3>${esc(a?.state_label || '风险信息暂不完整')}</h3><p class="product-provenance">规则研判 · 截至 ${esc(a?.assessment_date || '--')}</p>
            <ul>${(a?.risk_evidence || []).slice(0,3).map(e=>`<li>${esc(e.title || e.label || '风险证据')} ${esc(e.value_text || '')}</li>`).join('')}</ul>
            <p class="dv-note">证据可能来自单只基金，不能直接当作组合整体指标。</p>
            <details class="dv-detail"><summary>判断理由与证据</summary><p>${esc(a?.summary || '')}</p>${(a?.risk_evidence || []).slice(0,3).map(e=>`<div class="asset-review-finding"><strong>${esc(e.title || e.label || e.evidence_id)}</strong><p>${esc(e.explanation || e.detail || e.reason || e.description || '')}</p><small>${esc(e.evidence_id)}</small></div>`).join('')}<a href="#assets/risk">查看完整风险证据</a></details></div></div></section>
          ${(result.gaps || []).map(g=>`<p class="asset-review-gap" role="status">${g.section==='performance'?'业绩':'风险'}暂不可用：${esc(g.message)}</p>`).join('')}
          <section class="asset-review-section"><h3>配置研判</h3><p class="product-provenance">${report ? `已有报告 · ${esc(report.created_at || report.assessment_date || '')}。历史报告不随持仓更新。` : '当前区间尚无配置报告。'}</p>
            <div class="asset-review-actions">${report ? `<button class="secondary-action compact" data-review-report="${esc(report.job_id)}"><i data-lucide="file-text"></i>查看上次报告</button>` : ''}</div></section>
          <details class="asset-review-section"><summary>更多分析</summary><nav class="product-toolbar"><a href="#assets/stress">压力测试</a><a href="#assets/optimizer">调整配置</a><a href="#assets/events">事件关联</a><a href="#assets/monitoring">风险跟踪</a></nav></details>` : ''}
      </div>`;
    }
    return {load, render, months:()=>months, setMonths(value) { if ([6,12,36].includes(value)) { months=value; return load(); } }};
  }
  const api = {create};
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.FundScopePortfolioReview = api;
})(typeof window === "undefined" ? globalThis : window);
