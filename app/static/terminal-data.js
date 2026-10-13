/*
 * 终端展示的数据整理函数，不访问网络、不调用模型。
 * 后端负责分红复权；这里仅负责时间截取、共同日期对齐与显示格式。
 * 同时导出给 Node 测试，避免图表里的日期逻辑只能靠肉眼验证。
 */
(function (root) {
  "use strict";
  const periods = { "30d": "近30日", "3m": "近3个月", "6m": "近6个月", "12m": "近1年", "36m": "近3年" };
  function cutoffDate(end, period) {
    const value = new Date(`${end}T00:00:00Z`);
    if (period === "30d") value.setUTCDate(value.getUTCDate() - 30);
    else {
      // 先移到月初再回退月份，防止 5 月 31 日回退三个月溢出到 3 月。
      const day = value.getUTCDate();
      value.setUTCDate(1);
      value.setUTCMonth(value.getUTCMonth() - Number.parseInt(period, 10));
      const lastDay = new Date(Date.UTC(value.getUTCFullYear(), value.getUTCMonth() + 1, 0)).getUTCDate();
      value.setUTCDate(Math.min(day, lastDay));
    }
    return value.toISOString().slice(0, 10);
  }

  function windowPoints(points, period) {
    if (!points.length) return { points: [], partial: true };
    const cutoff = cutoffDate(points.at(-1).date, period);
    // 非交易日向前取最近一点作为收益起点，和后端列表摘要使用相同规则。
    let start = points.findLastIndex((point) => point.date <= cutoff);
    const partial = start < 0;
    if (partial) start = 0;
    return { points: points.slice(start), partial };
  }

  function chartSeries(primary, comparison, period, mode = "return") {
    const window = windowPoints(primary, period);
    let points = window.points;
    let other = [];
    const warnings = [];
    if (window.partial && points.length) warnings.push("所选周期历史不足，显示全部可用历史");
    if (comparison.length && mode === "return" && points.length) {
      // 只比较两条曲线都存在的日期，不补零、不前向填充，不比较不同起点的涨跌。
      const byDate = new Map(comparison.map((point) => [point.date, point]));
      const common = points.filter((point) => byDate.has(point.date));
      if (common.length >= 2) {
        if (common.length !== points.length) warnings.push("对比仅覆盖共同交易日，区间可能缩短");
        points = common;
        other = common.map((point) => byDate.get(point.date));
      } else warnings.push("共同交易日不足，暂不绘制对比曲线");
    }
    const normalize = (values) => values.map((point) => mode === "nav"
      ? point.nav ?? point.value
      : (point.value / values[0].value - 1) * 100);
    return { dates: points.map((point) => point.date), primary: normalize(points), comparison: normalize(other), warnings };
  }

  const escape = (value) => String(value ?? "").replace(/[&<>"']/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[char]));
  function safeUrl(value) {
    try { const url = new URL(value); return ["https:", "http:"].includes(url.protocol) ? url.href : ""; }
    catch (_) { return ""; }
  }
  const number = (value, digits = 2) => Number.isFinite(value) ? value.toLocaleString("en-US", { minimumFractionDigits: digits, maximumFractionDigits: digits }) : "--";
  const percent = (value) => Number.isFinite(value) ? `${value > 0 ? "+" : ""}${number(value)}%` : "--";
  const tone = (value) => Number.isFinite(value) ? value > 0 ? "up" : value < 0 ? "down" : "flat" : "flat";
  const api = { periods, cutoffDate, windowPoints, chartSeries, escape, safeUrl, number, percent, tone };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.FundScopeTerminalData = Object.freeze(api);
})(typeof window !== "undefined" ? window : globalThis);
