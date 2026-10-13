/* 两个民间指数的独立展示模块。所有数值来自后端，不在浏览器生成情绪分。 */
(function (root) {
  "use strict";
  const statuses = {
    available: "已保存公开数据", not_synced: "尚未更新", source_unavailable: "未接通真实数据源",
    stale: "数据偏旧", cached_error: "更新失败，保留旧值", error: "更新失败",
  };
  const escape = value => String(value ?? "").replace(/[&<>"']/g, char => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[char]));
  const number = value => typeof value === "number" && Number.isFinite(value);
  const pct = value => number(value) ? `${value > 0 ? "+" : ""}${value.toFixed(2)}%` : "暂无数据";
  const tone = value => !number(value) || value === 0 ? "neutral" : value > 0 ? "up" : "down";

  function link(url, label) {
    // 来源是外部数据，仍只允许 HTTPS，不能把任意协议拼入可点击链接。
    try {
      const parsed = new URL(url);
      if (parsed.protocol !== "https:") return escape(label);
      return `<a href="${escape(parsed.href)}" target="_blank" rel="noopener noreferrer">${escape(label)}</a>`;
    } catch { return escape(label); }
  }

  function renderIndicator(item) {
    const hasValue = number(item.value);
    const returns = Object.entries(item.returns || {});
    const history = item.history || [];
    return `<section class="retail-index" aria-labelledby="retail-title-${escape(item.indicator_id)}">
      <header class="retail-index-heading">
        <div><h3 id="retail-title-${escape(item.indicator_id)}">${escape(item.name)}</h3>
          <p>${escape(item.version)}</p></div>
        <span class="retail-index-status" data-status="${escape(item.status)}">${escape(statuses[item.status] || "状态未知")}</span>
      </header>
      ${item.last_error ? `<p class="retail-index-error" role="status">${escape(item.last_error)}</p>` : ""}
      <p class="retail-index-summary">${escape(item.summary)}</p>
      ${hasValue ? `<dl class="retail-index-metrics">
        <div><dt>指数点位</dt><dd>${escape(item.value.toFixed(4))}<small>${escape(item.unit)}</small></dd></div>
        ${returns.map(([, row]) => `<div><dt>${escape(row.label)}</dt>
          <dd data-return-tone="${tone(row.value_pct)}">${pct(row.value_pct)}</dd>
          <span>${row.start_date ? `${escape(row.start_date)} 起` : "历史不足"}</span></div>`).join("")}
      </dl><p class="retail-index-dates">数据截至 ${escape(item.data_date)} · 最近读取 ${escape(item.fetched_at ? new Date(item.fetched_at).toLocaleString("zh-CN", {hour12:false}) : "未知")}</p>` : ""}
      ${history.length ? `<div id="retail-index-chart" class="retail-index-chart" role="img"
        aria-label="韭菜50近三个月指数点位走势，历史观测值可在下方展开"></div>
        <p class="retail-index-dates">近3个月 · ${escape(item.publication_date)} 前为模拟回测，之后为发布后表现</p>
        <details class="retail-index-history"><summary>历史观测值（${history.length} 条）</summary>
          <div class="retail-history-scroll" tabindex="0" aria-label="指数历史观测值">
            <table><thead><tr><th scope="col">日期</th><th scope="col">点位</th></tr></thead><tbody>
              ${history.map(point => `<tr><td>${escape(point.date)}</td><td>${number(point.value) ? point.value.toFixed(4) : "暂无数据"}</td></tr>`).join("")}
            </tbody></table></div></details>` : ""}
      <details class="retail-index-method"><summary>口径与来源</summary>
        <p>${escape(item.methodology)}</p><ul>${(item.limitations || []).map(text => `<li>${escape(text)}</li>`).join("")}</ul>
        <p>${link(item.source_url, item.source_name)} · ${link(item.methodology_url, "编制说明")}</p>
        ${item.evidence_id ? `<p class="retail-evidence-id">证据 ${escape(item.evidence_id)}</p>` : ""}
      </details>
    </section>`;
  }

  function renderOverview(result) {
    // 名单固定为两项，不把外部返回的其他产品、股票推荐或成分明细带进页面。
    return ["mom", "bagholder50"].map(id => result?.indicators?.find(item => item.indicator_id === id))
      .filter(Boolean).map(renderIndicator).join("");
  }

  function drawChart(element, item, echarts) {
    if (!element || !echarts || !item?.history?.length) return null;
    const chart = echarts.init(element);
    chart.setOption({
      animation: false,
      grid: {left: 58, right: 16, top: 22, bottom: 35},
      tooltip: {trigger: "axis", valueFormatter: value => `${Number(value).toFixed(4)} 点`},
      xAxis: {type: "category", data: item.history.map(p => p.date), boundaryGap: false,
        axisLabel: {color: "#b0b1bb", fontSize: 11, formatter: value => value.slice(5)},
        axisLine: {lineStyle: {color: "#484a55"}}},
      yAxis: {type: "value", scale: true, axisLabel: {color: "#b0b1bb", fontSize: 11},
        splitLine: {lineStyle: {color: "#303138"}}},
      series: [{name: "韭菜50", type: "line", showSymbol: false, smooth: false,
        data: item.history.map(p => p.value), lineStyle: {width: 2, color: "#edc85b"},
        itemStyle: {color: "#edc85b"},
        markLine: {symbol: "none", silent: true, data: [{xAxis: item.publication_date}],
          label: {show: false}, lineStyle: {color: "#8ab5f5", type: "dashed"}},
      }],
    });
    // 面板初始 hidden 时尺寸为零，切到该功能后自动重新布局；销毁时释放监听。
    const observer = new ResizeObserver(() => chart.resize());
    observer.observe(element);
    return {dispose() { observer.disconnect(); chart.dispose(); }};
  }

  const api = {renderOverview, renderIndicator, drawChart};
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.FundScopeRetailIndices = api;
})(typeof window === "undefined" ? globalThis : window);
