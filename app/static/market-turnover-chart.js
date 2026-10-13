// 日成交额曲线：GET只读历史；独立的手动POST补齐日线，不连接盘中采样点。
(function (root, factory) {
  const api = factory(root);
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.FundScopeTurnoverChart = api;
})(typeof window !== "undefined" ? window : globalThis, function (root) {
  "use strict";
  const escape = value => String(value ?? "").replace(/[&<>"']/g, ch => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[ch]);
  const number = value => typeof value === "number" && Number.isFinite(value);
  const amount = value => number(value) ? (value / 1e8).toLocaleString("zh-CN", {maximumFractionDigits: 2, minimumFractionDigits: 2}) : "缺失";

  function chartOptions(data) {
    // 分类轴一个交易日一格，不生成周末/节假日的零成交。缺失的合计保持断点。
    const points = data.points.map(p => number(p.amount_cny) ? p.amount_cny / 1e8 : null);
    return {
      animation: false,
      grid: {left: 60, right: 16, top: 28, bottom: 32},
      tooltip: {trigger: "axis", renderMode: "richText", confine: true,
        formatter: rows => rows.length ? `${data.label}\n${rows[0].axisValue}\n${number(rows[0].value) ? rows[0].value.toFixed(2) + " 亿元" : "缺失"}` : ""},
      xAxis: {type: "category", data: data.points.map(p => p.date), boundaryGap: true,
        axisLabel: {color: "#b0b1bb", fontSize: 11, hideOverlap: true, formatter: value => value.slice(5)},
        axisLine: {lineStyle: {color: "#484a55"}}, splitLine: {show: false}},
      yAxis: {type: "value", min: 0, name: "亿元", nameTextStyle: {color: "#b0b1bb", fontSize: 11},
        axisLabel: {color: "#b0b1bb", fontSize: 11}, splitLine: {lineStyle: {color: "#303138"}}},
      series: [{name: data.label, type: "line", data: points, smooth: false, connectNulls: false,
        showSymbol: true, symbolSize: 4, lineStyle: {width: 2, color: "#edc85b"},
        itemStyle: {color: "#edc85b"}}],
    };
  }

  function render({data = null, catalog = [], seriesId = "reference:sh_sz", period = "3m", busy = false, error = ""} = {}) {
    const points = data?.points || [];
    const options = catalog.length ? catalog : [{series_id: "reference:sh_sz", label: "沪深成交额参考合计", group: "reference"}];
    return `<header class="turnover-chart-heading">
      <div><h2 id="market-turnover-history-title">日成交额走势</h2><p>每个交易日一个点 · 不含今日未确认金额</p></div>
      <div class="turnover-chart-controls">
        <label>市场<select data-turnover-series aria-label="成交额曲线市场">${["reference", "indices"].map(group => `<optgroup label="${group === "reference" ? "沪深参考口径" : "指数口径"}">${options.filter(item => item.group === group).map(item => `<option value="${escape(item.series_id)}"${seriesId === item.series_id ? " selected" : ""}>${escape(item.label)}</option>`).join("")}</optgroup>`).join("")}</select></label>
        <div class="turnover-chart-periods" role="group" aria-label="成交额曲线周期">
          ${[["30d", "近30日"], ["3m", "近3个月"]].map(([key,label]) => `<button type="button" data-turnover-period="${key}" aria-pressed="${period === key}">${label}</button>`).join("")}</div>
        <button type="button" class="secondary-action turnover-chart-reload" data-refresh-turnover-daily aria-label="更新日成交额" title="从数据源更新当前市场的历史日线；3分钟内复用结果" ${busy ? "disabled" : ""}><i data-lucide="refresh-cw"></i></button>
      </div></header>
      ${error || data?.error ? `<p class="intraday-error" role="alert">${escape(error || data.error)}${points.length ? "；保留上次曲线。" : ""}</p>` : ""}
      ${data?.refreshing ? '<p class="intraday-notice">日线正在更新，稍后重新读取页面。</p>' : ""}
      ${data ? `<p class="turnover-chart-caption">${escape(data.label)} · ${escape(data.range_start)} 至 ${escape(data.range_end)} · ${points.length} 个交易日记录${busy ? " · 正在读取" : ""}</p>` : ""}
      ${points.length ? `<div class="turnover-chart-canvas" data-turnover-canvas role="img" aria-label="${escape(data.label)}日成交额曲线，${points.length}个交易日"></div>
        ${points.length === 1 ? '<p class="intraday-notice">目前只有1个交易日数据，尚不足以观察变化趋势。</p>' : ""}
        <details class="turnover-chart-records"><summary>每日明细（${points.length}）</summary><div tabindex="0" role="region" aria-label="每日成交额明细"><table>
          <thead><tr><th scope="col">交易日期</th><th scope="col">日成交额 / 亿元</th></tr></thead>
          <tbody>${points.map(p => `<tr><td>${escape(p.date)}</td><td>${escape(amount(p.amount_cny))}</td></tr>`).join("")}</tbody>
        </table></div></details>` : `<p class="turnover-chart-empty" role="status">${busy ? "正在读取日成交额…" : "所选范围暂无日线成交额，可点击更新日成交额补齐历史。"}</p>`}
      ${(data?.warnings || []).map(warning => `<p class="intraday-notice">${escape(warning)}</p>`).join("")}
      ${data ? `<p class="turnover-chart-note">${escape(data.scope)}</p><p class="turnover-chart-note">${escape(data.methodology)}</p>
        <footer class="intraday-footer"><span>${escape(data.source_name)} · 截至 ${escape(data.data_date || "暂无")}</span><span>最近获取：${data.fetched_at ? escape(new Date(data.fetched_at).toLocaleString("zh-CN", {timeZone: "Asia/Shanghai", hour12:false})) : "未获取"}</span></footer>` : ""}`;
  }

  function create({element, request, refreshIcons = () => {}, echarts = root.echarts}) {
    const state = {data: null, catalog: [], seriesId: "reference:sh_sz", period: "3m", busy: false, error: ""};
    let revision = 0, chart = null, observer = null, refreshPending = false;
    function disposeChart() {
      observer?.disconnect(); observer = null;
      chart?.dispose(); chart = null;
    }
    function paint() {
      // 替换节点前销毁ECharts和尺寸监听，切换周期不会留下失效canvas或多份监听。
      disposeChart();
      element.innerHTML = render(state);
      element.setAttribute("aria-busy", String(state.busy));
      refreshIcons();
      const canvas = element.querySelector("[data-turnover-canvas]");
      if (!canvas || !state.data?.points.length) return;
      if (!echarts) { canvas.textContent = "图表组件未加载，每日明细仍可查看。"; return; }
      chart = echarts.init(canvas);
      chart.setOption(chartOptions(state.data));
      if (root.ResizeObserver) {
        observer = new root.ResizeObserver(() => chart?.resize());
        observer.observe(canvas);
      }
    }
    async function load({seriesId = state.seriesId, period = state.period, refresh = false} = {}) {
      if (refresh && refreshPending) return;
      if (refresh) refreshPending = true;
      const current = ++revision;
      if (seriesId !== state.seriesId || period !== state.period) state.data = null;
      state.seriesId = seriesId; state.period = period; state.busy = true; state.error = "";
      paint();
      try {
        const params = new URLSearchParams({series_id: seriesId, period});
        const data = await request(`/api/v1/market/turnover/daily${refresh ? "/refresh" : ""}?${params}`, refresh ? {method:"POST"} : undefined);
        // 较早的慢响应不能覆盖用户后来选择的市场或周期。
        if (current !== revision) return;
        state.data = data; state.catalog = data.series;
      } catch (error) {
        if (current === revision) state.error = error.message || "曲线读取失败";
      } finally {
        if (refresh) refreshPending = false;
        if (current === revision) { state.busy = false; paint(); }
      }
    }
    const change = event => {
      if (event.target.matches("[data-turnover-series]")) load({seriesId: event.target.value});
    };
    const click = event => {
      if (event.target.closest("[data-refresh-turnover-daily]")) load({refresh:true});
      const period = event.target.closest("[data-turnover-period]")?.dataset.turnoverPeriod;
      if (period) load({period});
    };
    element.addEventListener("change", change);
    element.addEventListener("click", click);
    return {load, dispose() {
      revision++; disposeChart();
      element.removeEventListener("change", change); element.removeEventListener("click", click);
    }};
  }
  return {create, render, chartOptions};
});
