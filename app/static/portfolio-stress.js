/* 历史压力沙盘：独立页面状态。只在用户点击时计算或读取，不自动更新行情。 */
(function (root) {
  "use strict";
  const esc = value => String(value ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const number = value => Number.isFinite(Number(value)) ? Number(value).toFixed(2) : "--";
  const signed = value => `${Number(value) > 0 ? "+" : ""}${number(value)}`;
  const icon = name => `<i data-lucide="${name}" aria-hidden="true"></i>`;
  const tone = value => Number(value) > 0 ? "positive" : Number(value) < 0 ? "negative" : "";
  const dateTime = value => value ? new Date(value).toLocaleString("zh-CN", { hour12: false }) : "--";

  function requestKey(cryptoSource = root.crypto) {
    // 某些浏览器或局域网HTTP没有 randomUUID，但仍支持 getRandomValues。
    if (typeof cryptoSource?.randomUUID === "function") return cryptoSource.randomUUID();
    if (typeof cryptoSource?.getRandomValues === "function") {
      const bytes = cryptoSource.getRandomValues(new Uint8Array(16));
      return "stress-" + Array.from(bytes, b => b.toString(16).padStart(2, "0")).join("");
    }
    throw Error("浏览器无法生成请求编号，请使用 localhost 或 HTTPS 地址重试");
  }

  function focusToken(element) {
    if (!element?.matches) return null;
    if (element.matches("[data-stress-weight]")) return "weight:" + element.dataset.stressWeight;
    if (element.matches("[data-stress-action]")) return "action:" + element.dataset.stressAction;
    if (element.matches("[data-stress-history]")) return "history";
    if (element.matches("select[name]")) return "parameter:" + element.name;
    if (element.matches("button[type=submit]")) return element.closest("[data-stress-start]") ? "submit:new" : "submit:compare";
    if (element.matches("summary")) return "audit";
    return null;
  }

  function initialState() {
    return { months: 12, days: 20, report: null, history: [], weights: {}, busy: false, error: "", notice: "" };
  }

  function totalWeights(weights) {
    const values = Object.values(weights);
    if (!values.length || values.some(v => v === "" || !Number.isFinite(Number(v)) || Number(v) < 0 || Number(v) > 100)) return null;
    return values.reduce((sum, value) => sum + Number(value), 0);
  }

  function chartOption(report) {
    return {
      animation: false, color: ["#edc85b", "#79aefa"],
      grid: { left: 54, right: 18, top: 44, bottom: 34 },
      tooltip: { trigger: "axis", renderMode: "richText", valueFormatter: number },
      legend: { top: 4, textStyle: { color: "#e9e9ee" } },
      xAxis: { type: "category", boundaryGap: false, data: report.scenarios[0].curve.map(p => p.date), axisLabel: { color: "#b0b1bb", hideOverlap: true } },
      yAxis: { type: "value", scale: true, name: "起点 = 100", nameTextStyle: { color: "#b0b1bb" }, axisLabel: { color: "#b0b1bb" }, splitLine: { lineStyle: { color: "#303138" } } },
      series: report.scenarios.map(s => ({ name: s.label, type: "line", showSymbol: false, smooth: false, lineStyle: { width: 2 }, data: s.curve.map(p => p.index) })),
    };
  }

  function render(state) {
    const r = state.report, [baseline, candidate] = r?.scenarios || [];
    const disabled = state.busy ? "disabled" : "";
    const total = totalWeights(state.weights);
    const valid = total !== null && Math.abs(total - 100) < 0.0001;
    return `<div class="stress-workbench" tabindex="-1" aria-label="历史压力测试工作区" aria-busy="${state.busy}">
      <header class="stress-heading"><h2>历史压力测试</h2><span>历史情景 · 不代表未来损失</span></header>
      <form data-stress-start class="stress-controls">
        <label>历史样本<select name="months" ${disabled}>${[6, 12, 36].map(n => `<option value="${n}"${state.months === n ? " selected" : ""}>近${n === 6 ? "6个月" : n === 12 ? "1年" : "3年"}</option>`).join("")}</select></label>
        <label>连续观察日<select name="days" ${disabled}>${[20, 40, 60].map(n => `<option value="${n}"${state.days === n ? " selected" : ""}>${n}日</option>`).join("")}</select></label>
        <button class="primary-action compact" type="submit" ${disabled}>${icon(state.busy ? "loader-circle" : "play")}${state.busy ? "处理中" : r ? "新建情景" : "重演历史行情"}</button>
      </form>
      <div class="stress-history"><button class="secondary-action compact" type="button" data-stress-action="history" ${disabled}>${icon("history")}历史记录</button>
        <select data-stress-history aria-label="选择已保存的压力测试记录" ${disabled}><option value="">选择记录</option>${state.history.map(item => `<option value="${esc(item.run_id)}"${item.run_id === r?.run_id ? " selected" : ""}>${esc(dateTime(item.created_at))} · ${esc(item.window.period_start)} / ${esc(item.window.days)}日 · ${esc(item.run_id.slice(-6))}</option>`).join("")}</select>
        ${r ? `<button class="icon-button" type="button" data-stress-action="verify" aria-label="复算核对" title="复算核对" ${disabled}>${icon("rotate-ccw")}</button><button class="secondary-action compact" type="button" data-stress-action="agent" ${disabled}>${icon("sparkles")}AI 解读</button>` : ""}
      </div>
      ${state.error ? `<p class="stress-error" role="alert">${esc(state.error)}${r ? "。当前仍展示上一份已保存报告。" : ""}</p>` : ""}
      <p class="stress-notice" role="status">${esc(state.notice || (state.busy ? "正在处理本地历史数据…" : ""))}</p>
      ${r ? `
        <div class="stress-period"><h3>${esc(r.window.period_start)} <span>至</span> ${esc(r.window.period_end)}</h3><span>${r.window.days} 个共同观察日 · 原配置累计收益最低窗口</span></div>
        <div class="stress-table-scroll"><table class="stress-summary"><caption class="sr-only">同一历史区间的方案比较</caption><thead><tr><th>配置</th><th>区间收益</th><th>最大回撤</th><th>最差单日收益</th></tr></thead><tbody>${r.scenarios.map(s => `<tr><th><span class="stress-key ${s.scenario_id === "candidate" ? "candidate" : ""}"></span>${esc(s.label)}</th><td class="${tone(s.period_return_pct)}">${signed(s.period_return_pct)}%</td><td>${number(s.max_drawdown_pct)}%</td><td class="${tone(s.worst_daily_return_pct)}">${signed(s.worst_daily_return_pct)}%</td></tr>`).join("")}</tbody></table></div>
        ${candidate ? `<p class="stress-delta">候选相对原配置：区间收益 <strong>${signed(candidate.period_return_delta_pp)} 个百分点</strong> · 回撤幅度 <strong>${signed(candidate.max_drawdown_delta_pp)} 个百分点</strong></p>` : ""}
        <div data-stress-chart class="stress-chart" role="img" aria-label="原配置与候选配置在同一历史窗口的模拟净值曲线，起点100"></div>
        <form data-stress-compare class="stress-comparison"><div class="stress-section-heading"><h3>配置与收益贡献</h3><span>贡献单位：百分点</span></div>
          <div class="stress-table-scroll"><table><thead><tr><th>基金</th><th>原权重</th><th>候选权重 %</th><th>原贡献</th><th>候选贡献</th></tr></thead><tbody>${r.holdings.map(h => {
            const a = baseline.contributions.find(v => v.fund_code === h.fund_code);
            const b = candidate?.contributions.find(v => v.fund_code === h.fund_code);
            return `<tr><th><strong>${esc(h.short_name)}</strong><small>${esc(h.fund_code)}</small></th><td>${number(h.weight_pct)}%</td><td><input aria-label="${esc(h.short_name)}候选权重" data-stress-weight="${esc(h.fund_code)}" type="number" min="0" max="100" step="any" value="${esc(state.weights[h.fund_code])}" required ${disabled}></td><td class="${tone(a?.return_contribution_pp)}">${signed(a?.return_contribution_pp)}</td><td class="${b ? tone(b.return_contribution_pp) : ""}">${b ? signed(b.return_contribution_pp) : "--"}</td></tr>`;
          }).join("")}</tbody></table></div>
          <div class="stress-compare-actions"><output data-stress-total class="${valid ? "" : "stress-error"}">权重合计 ${total === null ? "--" : number(total)}%</output><button data-stress-compare-button class="primary-action compact" type="submit" ${state.busy || !valid ? "disabled" : ""}>${icon("git-compare-arrows")}比较配置</button></div>
          <p class="stress-method">候选修改只用于新报告；当前曲线对应已保存的权重，不改变实际持仓。</p>
        </form>
        <details class="stress-audit"><summary>计算口径与数据来源</summary><dl><dt>冻结时间</dt><dd>${esc(dateTime(r.captured_at))}</dd><dt>实际样本</dt><dd>${esc(r.window.search_start)} 至 ${esc(r.window.search_end)} · ${r.window.candidate_windows} 个有效窗口</dd><dt>排除缺日窗口</dt><dd>${r.window.excluded_windows}</dd><dt>报告编号</dt><dd>${esc(r.run_id)}</dd><dt>输入指纹</dt><dd>${esc(r.input_hash)}</dd></dl><ul>${r.limitations.map(t => `<li>${esc(t)}</li>`).join("")}</ul><pre>${esc(JSON.stringify(r.data_sources, null, 2))}</pre></details>
      ` : `<div class="stress-empty"><h3>尚无历史重演结果</h3><p>已保存净值需覆盖至少 ${state.days + 1} 个共同日期。计算仅使用本地数据，不自动采集。</p></div>`}
    </div>`;
  }

  function create({ request, getContext, askAgent, refreshIcons }) {
    let state = initialState(), key = "", host = null, chart = null, observer = null, sequence = 0;
    // 失败后相同参数重试复用幂等键；成功后清掉，用户可显式新建下一份情景。
    let pendingSignature = "", pendingKey = "";
    let lastFocus = null, restoreAfterMount = false;
    const contextKey = ctx => `${ctx.userId}:${ctx.portfolioId || ""}`;
    const disposeChart = () => { observer?.disconnect(); observer = null; chart?.dispose(); chart = null; };
    function draw() {
      const active = root.document?.activeElement;
      const inside = host?.contains?.(active);
      if (inside) lastFocus = focusToken(active) || lastFocus;
      // 请求期间用户已点击其他区域时，不把焦点强行抢回来。
      const restore = inside || (restoreAfterMount && (!active || active === root.document.body));
      restoreAfterMount = false;
      disposeChart();
      if (!host) return;
      host.innerHTML = render(state);
      refreshIcons?.();
      if (restore && lastFocus) {
        const target = Array.from(host.querySelectorAll("button,input,select,summary")).find(e => focusToken(e) === lastFocus);
        // 等待阶段控件 disabled，用可程序聚焦的工作区暂时承接；完成后恢复原控件。
        (target && !target.disabled ? target : host.querySelector(".stress-workbench"))?.focus({ preventScroll: true });
      }
      const canvas = host.querySelector("[data-stress-chart]");
      if (canvas && root.echarts && state.report) {
        // 隐藏页宽度可能为0，等它可见后才初始化，避免首次打开图表空白。
        const resize = () => {
          if (!canvas.isConnected || canvas.clientWidth === 0) return;
          if (!chart) { chart = root.echarts.init(canvas); chart.setOption(chartOption(state.report)); }
          else chart.resize();
        };
        if (root.ResizeObserver) { observer = new root.ResizeObserver(resize); observer.observe(canvas); }
        resize();
      }
    }
    function accept(report) {
      state.report = report;
      state.months = report.months; state.days = report.window.days;
      state.weights = { ...(report.scenarios.find(s => s.scenario_id === "candidate") || report.scenarios[0]).weights };
      state.history = [report, ...state.history.filter(r => r.run_id !== report.run_id)].slice(0, 20);
    }
    async function execute(action, runId) {
      if (state.busy) return;
      const ctx = getContext(), origin = contextKey(ctx), turn = ++sequence;
      if (!ctx.portfolioId || origin !== key) return;
      const base = `/api/v1/portfolios/${encodeURIComponent(ctx.portfolioId)}/stress-runs`;
      let url = base, options = {}, signature = "";
      state.busy = true; state.error = ""; state.notice = "";
      try {
        if (action === "new" || action === "compare") {
          if (action === "compare" && (totalWeights(state.weights) === null || Math.abs(totalWeights(state.weights) - 100) >= 0.0001)) return;
          const body = action === "new" ? { months: state.months, window_days: state.days }
            : { source_run_id: state.report.run_id, target_weights: Object.fromEntries(Object.entries(state.weights).map(([k, v]) => [k, Number(v)])) };
          body.user_id = ctx.userId;
          signature = JSON.stringify(body);
          if (signature !== pendingSignature) {
            const generated = requestKey();
            // 先成功生成，再一起更新重试状态，不能留下签名有效、键却为空的半状态。
            pendingSignature = signature; pendingKey = generated;
          }
          body.idempotency_key = pendingKey;
          options = { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };
        } else {
          if (action === "read" || action === "verify") url += `/${encodeURIComponent(runId)}`;
          if (action === "verify") { url += "/verify"; options.method = "POST"; }
          url += `?user_id=${encodeURIComponent(ctx.userId)}`;
        }
        draw();
        const result = await request(url, options);
        // 切换组合/用户后，旧请求不能把旧账户的数据画进新页面。
        if (turn !== sequence || origin !== contextKey(getContext())) return;
        if (action === "history") { state.history = result; state.notice = result.length ? `已读取 ${result.length} 条最近记录` : "暂无保存记录"; }
        else if (action === "verify") state.notice = result.consistent ? "复算一致：冻结输入与原计算结果匹配。不代表未来表现得到验证。" : "复算不一致，请保留记录并检查计算版本。";
        else { accept(result); state.notice = "已读取保存记录；没有更新行情或持仓。"; }
        if (signature) { pendingSignature = ""; pendingKey = ""; }
      } catch (error) {
        if (turn === sequence && origin === contextKey(getContext())) state.error = error.message;
      } finally {
        if (turn === sequence && origin === contextKey(getContext())) { state.busy = false; draw(); }
      }
    }
    function bind() {
      host.oninput = event => {
        const code = event.target.dataset.stressWeight;
        if (!code) return;
        state.weights[code] = event.target.value;
        const total = totalWeights(state.weights), valid = total !== null && Math.abs(total - 100) < 0.0001;
        const output = host.querySelector("[data-stress-total]");
        output.textContent = `权重合计 ${total === null ? "--" : number(total)}%`;
        output.classList.toggle("stress-error", !valid);
        host.querySelector("[data-stress-compare-button]").disabled = state.busy || !valid;
      };
      host.onchange = event => {
        if (event.target.name === "months") state.months = Number(event.target.value);
        if (event.target.name === "days") state.days = Number(event.target.value);
        if (event.target.matches("[data-stress-history]") && event.target.value) execute("read", event.target.value);
      };
      host.onsubmit = event => {
        if (!event.target.matches("[data-stress-start], [data-stress-compare]")) return;
        event.preventDefault(); event.stopPropagation();
        execute(event.target.matches("[data-stress-start]") ? "new" : "compare");
      };
      host.onclick = event => {
        const action = event.target.closest("[data-stress-action]")?.dataset.stressAction;
        if (!action || state.busy) return;
        if (action === "agent") askAgent(state.report);
        else execute(action, state.report?.run_id);
      };
    }
    return {
      beforeUnmount() {
        // 外层组合页还会因其他数据到达而重绘，拆除旧DOM前保存焦点位置。
        const active = root.document?.activeElement;
        restoreAfterMount = Boolean(host?.contains?.(active));
        if (restoreAfterMount) lastFocus = focusToken(active) || lastFocus;
      },
      mount(element) {
        const nextKey = contextKey(getContext());
        let changed = false;
        if (key !== nextKey) { key = nextKey; state = initialState(); sequence++; pendingSignature = ""; pendingKey = ""; lastFocus = null; restoreAfterMount = false; changed = true; }
        if (host !== element || changed) { disposeChart(); host = element; if (host) { bind(); draw(); } }
      },
      // 显式方法供模块测试；浏览器入口仍是按钮事件。
      execute,
    };
  }
  const api = { create, render, initialState, chartOption, totalWeights, requestKey, focusToken };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.FundScopePortfolioStress = api;
})(typeof window === "undefined" ? globalThis : window);
