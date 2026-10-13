// 盘中行情拥有独立的请求与渲染状态，不影响日线快照、组合或聊天上下文。
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.FundScopeMarketIntraday = api;
})(typeof window !== "undefined" ? window : globalThis, function () {
  "use strict";
  const escape = value => String(value ?? "").replace(/[&<>"']/g, ch => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[ch]);
  const number = value => typeof value === "number" && Number.isFinite(value);

  function timestamp(value) {
    if (!value || Number.isNaN(Date.parse(value))) return "未获取";
    return new Intl.DateTimeFormat("zh-CN", {
      timeZone: "Asia/Shanghai", year: "numeric", month: "2-digit", day: "2-digit",
      hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23",
    }).format(new Date(value));
  }

  function metric(value, divisor, unit) {
    // 空值不当零；万元在后端转为元，这里只负责元->亿元、手->万手。
    return number(value) ? `${(value / divisor).toLocaleString("zh-CN", {minimumFractionDigits: 2, maximumFractionDigits: 2})}<small>${unit}</small>` : '<span class="intraday-muted">暂无</span>';
  }

  function render({data = null, busy = false, error = ""} = {}) {
    const rows = (data?.indices || []).map(item => {
      const status = item.status === "cached_error" ? "更新失败 · 保留旧值"
        : item.status === "error" ? "获取失败"
          : ({recent: "当日快照", stale: "行情时间偏旧", previous_date: "非今日行情", missing: "尚未获取"}[item.freshness] || "待核验");
      const warning = item.status === "cached_error" || item.status === "error" || item.freshness === "stale" || item.freshness === "previous_date";
      return `<tr>
        <th scope="row"><strong>${escape(item.index_name)}</strong><small>${escape(item.index_code)}</small></th>
        <td class="intraday-amount">${metric(item.amount_cny, 1e8, "亿元")}</td>
        <td>${metric(item.volume_lots, 1e4, "万手")}</td>
        <td class="intraday-time"><time>${escape(timestamp(item.quote_at))}</time>
          <small data-warning="${warning}" title="${escape(item.error || `抓取时间：${timestamp(item.fetched_at)}`)}">${escape(status)}</small></td>
      </tr>`;
    }).join("");
    const errors = [...new Set((data?.indices || []).map(item => item.error).filter(Boolean))];
    return `<header class="intraday-heading">
      <div><h2 id="market-intraday-title">盘中成交</h2><p>截至行情时间累计 · 指数口径，不作全市场合计</p></div>
      <button type="button" class="secondary-action" data-refresh-intraday ${busy ? "disabled" : ""}
        title="获取最新盘中行情；30秒内复用同一次请求">
        <i data-lucide="${busy ? "loader-circle" : "refresh-cw"}"${busy ? ' class="spinning"' : ""}></i>
        <span>${busy ? "正在读取" : "更新盘中行情"}</span>
      </button>
    </header>
    ${error ? `<p class="intraday-error" role="alert">${escape(error)}${data ? "；已保留上次显示的数据。" : ""}</p>` : ""}
    ${data?.refreshing ? '<p class="intraday-notice" role="status">另一请求正在更新，稍后再次读取。</p>' : ""}
    ${rows ? `<div class="intraday-table-scroll" role="region" aria-label="指数成交行情" tabindex="0">
      <table class="intraday-table"><thead><tr><th scope="col">指数</th><th scope="col">累计成交额</th><th scope="col">累计成交量</th><th scope="col">行情时间 · 北京时间</th></tr></thead><tbody>${rows}</tbody></table>
      </div>` : `<p class="intraday-muted" role="status">${busy ? "正在读取已保存行情…" : "尚未获取盘中行情"}</p>`}
    ${errors.length ? `<p class="intraday-error" role="status">${errors.map(escape).join(" ")}</p>` : ""}
    <footer class="intraday-footer"><span>${escape(data?.source_name || "腾讯财经公开行情")} · 手动快照 · 可能延迟</span>
      <span>最近请求：${escape(timestamp(data?.attempted_at))}</span></footer>`;
  }

  function renderTurnover({data = null, busy = false, error = ""} = {}) {
    const snapshot = data?.snapshot;
    const stateLabel = data?.status === "cached_error" ? "更新失败 · 保留旧批次"
      : data?.status === "error" ? "获取失败"
        : ({recent: "当日参考日期", stale: "参考行情时间偏旧", previous_date: "非今日行情", missing: "尚未获取"}[data?.freshness] || "尚未获取");
    // 沪深合计不再加进三市合计：两组结果均由后端的互斥市场集合单独计算。
    const totals = [["沪深两市 A 股", snapshot?.sh_sz_amount_cny], ["沪深北 A 股", snapshot?.sh_sz_bj_amount_cny]];
    return `<header class="intraday-heading">
      <div><h2 id="market-turnover-title">全市场成交额</h2><p>A 股行情列表汇总 · 不含基金、债券、B 股</p></div>
      <button type="button" class="secondary-action" data-refresh-intraday ${busy ? "disabled" : ""}
        title="完整采集A股列表；3分钟内复用本次结果，与指数刷新独立">
        <i data-lucide="${busy ? "loader-circle" : "refresh-cw"}"${busy ? ' class="spinning"' : ""}></i>
        <span>${busy ? "正在读取" : "更新全市场成交额"}</span>
      </button></header>
      <dl class="turnover-totals">${totals.map(([label, value]) => `<div><dt>${label}</dt><dd>${metric(value, 1e8, "亿元")}</dd></div>`).join("")}</dl>
      ${error || data?.error ? `<p class="intraday-error" role="alert">${escape(error || data.error)}${snapshot ? "；仍展示上次完整批次。" : ""}</p>` : ""}
      ${data?.refreshing ? '<p class="intraday-notice" role="status">全市场正在采集中，稍后再次读取。</p>' : ""}
      ${snapshot ? `<dl class="turnover-exchanges">${snapshot.exchanges.map(item => `<div><dt>${escape(item.label)}<small>${escape(item.security_count)} 只</small></dt><dd>${metric(item.amount_cny, 1e8, "亿元")}</dd></div>`).join("")}</dl>
        <div class="turnover-status"><span>${escape(stateLabel)} · 覆盖 ${escape(snapshot.security_count)} / ${escape(snapshot.expected_count)} 只</span>
          <span>参考行情：${escape(timestamp(snapshot.reference_at))}</span>
          <span>采集窗口：${escape(timestamp(snapshot.started_at))} 至 ${escape(timestamp(snapshot.fetched_at))}</span></div>`
        : `<p class="intraday-muted" role="status">${busy ? "正在读取全市场成交额…" : stateLabel}</p>`}
      <details class="turnover-method"><summary>统计范围与时间口径</summary>
        <p>${escape(data?.scope || "沪市、深市和北交所A股分别统计，不相加宽基指数。")}</p>
        <p>${escape(data?.time_basis || "行情日期参考同源大盘；个股列表不提供独立交易日期。")}</p>
        <p>${escape(data?.limitation || "分页快照汇总，不是同一毫秒截面或交易所结算数据。")}</p></details>
      <footer class="intraday-footer"><span>${escape(data?.source_name || "新浪财经A股行情列表汇总")}</span>
        <span>最近请求：${escape(timestamp(data?.attempted_at))}</span></footer>`;
  }

  function create({element, request, refreshIcons = () => {}, turnover = false, onUpdated = () => {}}) {
    const state = {data: null, busy: false, error: ""};
    const endpoint = turnover ? "/api/v1/market/turnover" : "/api/v1/market/intraday";
    let pending = null;
    function paint() {
      element.innerHTML = (turnover ? renderTurnover : render)(state);
      element.setAttribute("aria-busy", String(state.busy));
      refreshIcons();
    }
    function load({refresh = false} = {}) {
      // 防止重复点击，并保留失败前的快照。没有定时器或后台自动刷新。
      if (pending) return pending;
      state.busy = true;
      state.error = "";
      paint();
      pending = Promise.resolve().then(() => request(
        refresh ? `${endpoint}/refresh` : endpoint,
        refresh ? {method: "POST"} : undefined,
      )).then(data => {
        state.data = data;
        // 只通知历史组件重新GET；图表读取失败不改变本次行情刷新结果。
        if (refresh) Promise.resolve().then(onUpdated).catch(() => {});
      }).catch(error => {
        state.error = error.message || "行情读取失败，请重试。";
      }).finally(() => { state.busy = false; pending = null; paint(); });
      return pending;
    }
    element.addEventListener("click", event => {
      if (event.target.closest("[data-refresh-intraday]")) load({refresh: true});
    });
    return {load};
  }
  return {render, renderTurnover, create, timestamp};
});
