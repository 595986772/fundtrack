/*
 * 持仓中的基金标识与净值报价。这里只展示 API 已提供的事实，不估算盘中涨跌。
 * 净值日期和“刚刚读取”的时间不是一回事：读到旧日期时仍保留旧日期。
 */
(function (root) {
  "use strict";
  const escape = value => String(value ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;", "<":"&lt;", ">":"&gt;", '"':"&quot;", "'":"&#39;"}[c]));
  const finite = value => typeof value === "number" && Number.isFinite(value);
  const tone = value => !finite(value) || value === 0 ? "neutral" : value > 0 ? "positive" : "negative";
  const percent = value => finite(value) ? `${value > 0 ? "+" : ""}${value.toFixed(2)}%` : "未提供";
  const roles = {
    broad_market_index: ["chart-no-axes-combined", "宽基指数"],
    industry_theme: ["target", "行业主题"],
    bond_fund: ["landmark", "债券基金"],
    general_fund: ["chart-line", "综合基金"],
  };

  function chinaDate(now = new Date()) {
    // 明确使用中国市场所在时区，不依赖访问者电脑或浏览器的本地时区。
    return new Intl.DateTimeFormat("en-CA", {timeZone: "Asia/Shanghai", year: "numeric", month: "2-digit", day: "2-digit"}).format(now);
  }
  function identity(holding, quote) {
    const role = Object.hasOwn(roles, holding.sample_role || quote?.sample_role) ? holding.sample_role || quote.sample_role : "general_fund";
    const [icon, label] = roles[role];
    const name = holding.short_name || quote?.short_name || holding.fund_code;
    return `<button class="holding-identity" type="button" data-research-fund="${escape(holding.fund_code)}" data-kind="${role}" title="查看${escape(name)}" aria-label="${escape(name)}">
      <span class="holding-mark" aria-hidden="true"><i data-lucide="${icon}"></i></span>
      <span class="holding-name"><strong>${escape(name)}</strong><small>${escape(holding.fund_code)} <span>${label}</span></small></span>
      <i class="holding-open" data-lucide="arrow-up-right" aria-hidden="true"></i></button>`;
  }
  function daily(quote, now = new Date()) {
    const date = quote?.latest_nav_date;
    const hasDate = typeof date === "string" && /^\d{4}-\d{2}-\d{2}$/.test(date);
    const today = chinaDate(now);
    const label = !hasDate ? "净值日期缺失" : date === today ? "今日已披露" : date < today ? "今日暂无新披露" : "日期待核验";
    // 没有可对应的日期时不把一个孤立涨跌值当成今日报价。
    const value = hasDate && date <= today ? quote.daily_growth_rate_pct : null;
    return `<span class="holding-daily"><strong data-quote-tone="${tone(value)}">${percent(value)}</strong>
      <small>${escape(hasDate ? date : "日期未知")}</small><small class="holding-freshness" data-current="${date === today}">${label}</small></span>`;
  }
  function change(value, basis = "") {
    return `<span class="holding-return" data-quote-tone="${tone(value)}"${basis ? ` title="${escape(basis)}"` : ""}>${percent(value)}</span>`;
  }

  function position({portfolio, portfolioId, userId, fundCode, loading, error}) {
    // 详情沿用当前账本快照。切换账号、组合或读取失败时不能展示上一份持仓。
    const back = '<a href="#assets/holdings">查看我的资产<i data-lucide="arrow-right" aria-hidden="true"></i></a>';
    const message = text => `<h3>我的持仓</h3><p class="terminal-position-note" role="status">${escape(text)}</p>${back}`;
    if (loading) return message("正在读取当前组合持仓…");
    if (error) return message("持仓读取失败，暂不展示账本金额。");
    if (!portfolioId || !portfolio || portfolio.id !== portfolioId || portfolio.user_id !== userId) return message("尚未读取当前组合。");
    if (portfolio.position_mode !== "actual") return message("当前组合尚未补齐实际份额与成本。");
    const holding = portfolio.holdings.find(row => row.fund_code === fundCode);
    const heading = `<h3>我的持仓</h3><p class="terminal-position-note">${escape(portfolio.name)}</p>`;
    if (!holding) return `${heading}<p class="terminal-position-note">当前组合未持有这只基金。</p>${back}`;
    const number = (value, digits = 2) => finite(value) ? value.toLocaleString("zh-CN", {minimumFractionDigits:digits, maximumFractionDigits:digits}) : "--";
    const money = value => finite(value) ? `¥${number(value)}` : "--";
    const pnl = holding.total_pnl_amount;
    const signedMoney = finite(pnl) ? `${pnl > 0 ? "+" : pnl < 0 ? "-" : ""}¥${number(Math.abs(pnl))}` : "--";
    // 金额、份额、占比均是后端结果，前端不拿今日报价重新推算账本盈亏。
    return `${heading}<dl class="terminal-position-metrics">
      <div><dt>当前市值</dt><dd>${money(holding.current_value)}</dd></div>
      <div><dt>累计盈亏</dt><dd class="holding-return" data-quote-tone="${tone(pnl)}">${signedMoney}</dd></div>
      <div><dt>持有份额</dt><dd>${number(holding.units, 4)}</dd></div>
      <div><dt>平均成本净值</dt><dd>${number(holding.average_cost_nav, 4)}</dd></div>
      </dl><p class="terminal-position-note">组合占比 ${finite(holding.weight_pct) ? `${number(holding.weight_pct)}%` : "未知"}<br>账本净值日期 ${escape(holding.latest_nav_date || "未知")}</p>
      <div class="terminal-position-links">${back}<a href="#assets/transactions">记账与流水<i data-lucide="book-plus" aria-hidden="true"></i></a></div>`;
  }
  const api = {identity, daily, change, chinaDate, position};
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.FundScopeHoldingsQuotes = api;
})(typeof window === "undefined" ? globalThis : window);
