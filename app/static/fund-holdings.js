/* 单基金持仓页：独立管理只读请求、局部视图和错误，不依赖组合状态或全局重绘。 */
(function (root) {
  "use strict";
  const labels = {stock: "股票", bond: "债券", industry: "行业"};
  const esc = value => String(value ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;", "<":"&lt;", ">":"&gt;", '"':"&quot;", "'":"&#39;"}[c]));
  const number = value => typeof value === "number" && Number.isFinite(value) ? value.toFixed(2) : null;
  const percent = value => number(value) === null ? "未披露" : `${number(value)}%`;
  function fetchedTime(value) {
    if (!value || !Number.isFinite(new Date(value).getTime())) return "未记录";
    return new Intl.DateTimeFormat("zh-CN", {timeZone:"Asia/Shanghai", dateStyle:"short", timeStyle:"short", hour12:false}).format(new Date(value)) + "（北京时间）";
  }
  const icon = name => `<i data-lucide="${name}" aria-hidden="true"></i>`;
  function link(url, text = "来源") {
    // 来源和名称都来自外部披露；拒绝脚本协议，并转义文本和属性。
    try {
      const parsed = new URL(url);
      if (!["https:", "http:"].includes(parsed.protocol)) return esc(text);
      return `<a href="${esc(parsed.href)}" target="_blank" rel="noopener noreferrer">${esc(text)}${icon("external-link")}</a>`;
    } catch { return esc(text); }
  }
  const bar = value => `<span class="holdings-bar" aria-hidden="true"><span style="width:${Math.min(100, Math.max(0, Number(value) || 0))}%"></span></span>`;

  function render(state) {
    const {fund, data, loading, error, section} = state;
    if (!fund) return '<div class="fund-holdings-empty"><h2>尚未选择基金</h2><p>暂无可分析的基金披露。</p></div>';
    const heading = `<header class="fund-holdings-heading"><div><h2>${esc(fund.short_name || fund.fund_code)}</h2><p>${esc(fund.fund_code)} · 单基金持仓分析</p></div><div class="fund-holdings-actions">
      <button type="button" class="secondary-action compact" data-holdings-ask ${!data || loading || data.disclosure.data_mode === "unavailable" ? "disabled" : ""}>${icon("sparkles")}交给 AI 解读</button>
      <button type="button" class="icon-button" data-holdings-reload title="重新读取已保存的持仓披露" aria-label="重新读取已保存的持仓披露" ${loading ? "disabled" : ""}>${icon("refresh-cw")}</button></div></header>`;
    const feedback = error ? `<div class="fund-holdings-error" role="alert">${esc(error)}${data ? " · 下方保留上次读取结果" : ""}<button type="button" class="secondary-action compact" data-holdings-reload>重试读取</button></div>` : "";
    if (!data) return heading + feedback + (loading ? '<p role="status">正在读取已保存的持仓披露...</p>' : "");
    const d = data.disclosure;
    const mode = {fixture:"冻结样本", database:"数据库披露", unavailable:"暂无披露"}[d.data_mode];
    const freshness = {fresh:"披露期正常", aging:"披露较旧", stale:"披露已过旧", unavailable:"暂无披露"}[d.freshness_status];
    const meta = `<div class="fund-holdings-meta"><span>${mode} · ${freshness}</span><span>披露期 ${esc(d.oldest_report_date || "--")} 至 ${esc(d.latest_report_date || "--")}</span><span>采集于 ${esc(fetchedTime(d.fetched_at))}</span></div>`;
    const tabs = `<div class="fund-holdings-switch" role="group" aria-label="持仓分析视图">${[["overview","披露概览"],["changes","持仓变化"]].map(([id,label]) => `<button type="button" data-holdings-section="${id}" aria-pressed="${section === id}">${label}</button>`).join("")}</div>`;
    const warnings = `<details class="fund-holdings-boundary" ${d.data_mode === "unavailable" || data.lookthrough_status === "target_etf_unavailable" ? "open" : ""}><summary>披露范围与数据缺口</summary><ul>${[...data.limitations,...d.warnings].map(text => `<li>${esc(text)}</li>`).join("")}</ul>${link(d.source_url, d.source || "披露来源")}</details>`;
    const etfBoundary = data.lookthrough_status === "target_etf_unavailable" ? '<p class="fund-holdings-etf-note">ETF 联接基金：当前仅展示直接披露，尚未穿透目标 ETF。低覆盖率不代表低股票仓位。</p>' : "";
    return heading + feedback + meta + `<p class="fund-holdings-note">定期披露，不是实时仓位。所有比例均占该基金净值；未披露部分不等于现金。</p>` + etfBoundary + tabs
      + (section === "changes" ? renderChanges(data) : renderOverview(data)) + warnings;
  }

  function renderOverview(data) {
    const d = data.disclosure, c = data.concentration;
    const metrics = [["同报告期证券覆盖", data.security_coverage_pct], ["未解释净值比例", data.unresolved_weight_pct], ["已披露前五大股票", c.top_five_disclosed_stocks_pct]];
    return `<dl class="fund-holdings-metrics">${metrics.map(([label,value]) => `<div><dt>${label}</dt><dd>${value === null ? "无法确定" : percent(value)}</dd></div>`).join("")}</dl>
      <div class="fund-holdings-grid"><section><h3>已披露资产</h3><p>证券明细小计，不是完整资产配置。</p><dl class="fund-holdings-assets">${data.asset_evidence.map(row => `<div><dt>${esc(row.label)}<small>${esc(row.report_date || "缺少完整披露")}</small></dt><dd>${percent(row.weight_pct)}</dd></div>`).join("")}</dl></section>
      <section><h3>股票行业分布</h3><p>行业披露期 ${esc(d.report_dates.industry || "--")} · 覆盖 ${d.industry_allocations.length ? percent(d.industry_coverage_pct) : "未知"}</p>${d.industry_allocations.length ? `<ul class="fund-holdings-industries">${[...d.industry_allocations].sort((a,b) => b.weight_pct-a.weight_pct).map(row => `<li><span>${link(row.source_url, row.industry_name)}</span><strong>${percent(row.weight_pct)}</strong>${bar(row.weight_pct)}</li>`).join("")}</ul>` : '<p class="fund-holdings-empty">暂无行业配置披露，不能判断行业集中度。</p>'}</section></div>
      <section class="fund-holdings-securities"><h3>已披露重仓证券</h3><p>只展示真实披露明细，不用基金名称推测持仓。</p>${d.security_holdings.length ? `<div class="fund-holdings-table-scroll" tabindex="0" role="region" aria-label="证券持仓表"><table><thead><tr><th>证券</th><th>类型</th><th>占基金净值</th><th>披露期</th><th>来源</th></tr></thead><tbody>${[...d.security_holdings].sort((a,b) => b.weight_pct-a.weight_pct).map(row => `<tr><th scope="row">${esc(row.security_name)}<small>${esc(row.security_code)}</small></th><td>${labels[row.asset_type]}</td><td>${percent(row.weight_pct)}</td><td>${esc(row.report_date)}</td><td>${link(row.source_url)}</td></tr>`).join("")}</tbody></table></div>` : '<p class="fund-holdings-empty">暂无证券明细；不代表基金没有持仓。</p>'}</section>`;
  }

  function renderChanges(data) {
    const status = {newly_disclosed:"本期新披露", not_disclosed:"本期未再披露", comparable:"两期可比"};
    return `<div class="fund-holdings-changes">${data.comparisons.map(group => `<section><h3>${labels[group.kind]}披露变化</h3><p>${esc(group.message)}</p>${group.status === "available" ? `<p>${esc(group.previous_report_date)} → ${esc(group.report_date)}</p><div class="fund-holdings-table-scroll" tabindex="0" role="region" aria-label="${labels[group.kind]}变化表"><table><thead><tr><th>项目</th><th>上次披露</th><th>本次披露</th><th>变化（百分点）</th><th>状态</th></tr></thead><tbody>${group.rows.map(row => `<tr><th scope="row">${esc(row.name)}<small>${esc(row.key)}</small></th><td>${percent(row.previous_weight_pct)} ${row.previous_source_url ? link(row.previous_source_url) : ""}</td><td>${percent(row.weight_pct)} ${row.source_url ? link(row.source_url) : ""}</td><td>${row.change_pp === null ? "不可计算" : `${row.change_pp > 0 ? "+" : ""}${number(row.change_pp)}`}</td><td>${status[row.status]}</td></tr>`).join("")}</tbody></table></div>` : '<p class="fund-holdings-empty">暂无可比记录</p>'}</section>`).join("")}</div>`;
  }

  function create({element, request, ask, icons = () => {}}) {
    const state = {fund:null, data:null, loading:false, error:"", section:"overview"};
    let requestId = 0, pending = null;
    const paint = () => { element.innerHTML = render(state); icons(); };
    async function show(fund, {refresh = false} = {}) {
      const changed = fund?.fund_code !== state.fund?.fund_code;
      if (changed) {
        ++requestId;
        Object.assign(state, {fund, data:null, error:"", loading:false, section:"overview"});
      }
      if (!fund) { paint(); return; }
      if (!changed && !refresh && (state.data || state.loading)) return pending;
      const id = ++requestId;
      state.loading = true; state.error = ""; paint();
      // 每次显式刷新都有新序号。慢响应不得把 A 基金的持仓写到 B 基金页面上。
      pending = (async () => {
        try {
          const result = await request(fund.fund_code);
          if (id !== requestId) return;
          if (result.fund_code !== fund.fund_code) throw new Error("返回的基金代码不匹配，请重新读取。");
          state.data = result;
        } catch (error) {
          if (id === requestId) state.error = error.message;
        } finally {
          if (id === requestId) { state.loading = false; paint(); }
        }
      })();
      return pending;
    }
    element.addEventListener("click", event => {
      if (event.target.closest("[data-holdings-reload]")) { show(state.fund, {refresh:true}); return; }
      const tab = event.target.closest("[data-holdings-section]");
      if (tab) {
        state.section = tab.dataset.holdingsSection;
        paint();
        element.querySelector(`[data-holdings-section="${state.section}"]`)?.focus({preventScroll:true});
      }
      if (event.target.closest("[data-holdings-ask]") && state.data && !state.loading) ask(state.fund, state.data);
    });
    return {show};
  }
  const api = {render, create};
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.FundScopeFundHoldings = api;
})(typeof window === "undefined" ? globalThis : window);
