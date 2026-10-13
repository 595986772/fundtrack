/*
 * FundScope 研究终端：稳定布局、图表实例与局部交互放在这里。
 * app.js 仍是业务状态和请求的唯一所有者；本模块通过 actions 复用原有 API，
 * 不另建一套 Agent 会话，也不在切换基金或指数时自动调用模型。
 */
(function () {
  "use strict";
  const D = window.FundScopeTerminalData;
  const e = D.escape;
  const icon = (name) => `<i data-lucide="${name}" aria-hidden="true"></i>`;
  const empty = (text) => `<p class="terminal-empty">${e(text)}</p>`;
  const metric = (label, value, tone = "flat") => `<div><dt>${e(label)}</dt><dd class="t-${tone}">${e(value)}</dd></div>`;
  function sourceLink(url, label) {
    const safe = D.safeUrl(url);
    return safe ? `<a href="${e(safe)}" target="_blank" rel="noopener noreferrer">${e(label)}${icon("arrow-up-right")}</a>` : `<span>${e(label)}</span>`;
  }

  function create(root, { getState, actions }) {
    const view = { kind: "fund", period: "3m", mode: "return", compare: "", comparison: null, compareError: "", compareLoading: false, requestId: 0 };
    let chart = null;
    let chartKey = "";
    let scheduled = false;
    let lastFund = null;
    let fundOptionsKey = "";
    // DOM 主骨架只创建一次。行情、流式回答更新时不会销毁输入框或图表缩放状态。
    root.innerHTML = `
      <div class="terminal-grid">
        <div class="terminal-main">
          <section class="terminal-instrument" aria-label="当前研究标的">
            <div id="terminal-subject"></div>
            <div class="terminal-subject-actions"><button class="terminal-primary" data-terminal-action="ask-detail" title="交给 AI 分析" aria-label="交给 AI 分析">${icon("sparkles")}<span>AI 分析</span></button><button class="icon-button" data-terminal-action="report" title="打开完整报告" aria-label="打开完整报告">${icon("file-chart-column-increasing")}</button><button class="icon-button" data-terminal-action="sync-subject" title="同步标的数据" aria-label="同步标的数据">${icon("cloud-download")}</button></div>
          </section>
          <dl id="terminal-quotes" class="terminal-quotes"></dl>
          <div class="terminal-fund-layout">
          <section class="terminal-chart-section" aria-label="历史走势图">
            <div class="terminal-chart-toolbar">
              <div class="terminal-segments" aria-label="图表类型"><button data-chart-mode="return" aria-pressed="true">区间收益</button><button data-chart-mode="nav" aria-pressed="false">单位净值</button></div>
              <div class="terminal-periods" aria-label="图表周期">${Object.entries(D.periods).map(([value, label]) => `<button data-chart-period="${value}" aria-pressed="${value === view.period}">${label}</button>`).join("")}</div>
            </div>
            <div class="terminal-chart-legend"><span id="terminal-chart-basis"></span><label for="terminal-comparison">对比</label><select id="terminal-comparison" aria-label="选择对比曲线"><option value="">不对比</option></select><button class="icon-button" data-terminal-action="reset-chart" title="重置图表缩放" aria-label="重置图表缩放">${icon("maximize")}</button></div>
            <div class="terminal-chart-stage"><div id="terminal-chart" role="img" aria-label="历史收益走势图"></div><div id="terminal-chart-empty" class="terminal-chart-empty" role="status" hidden></div></div>
            <p id="terminal-chart-caption" class="terminal-caption"></p>
            <p id="terminal-chart-warning" class="terminal-warning" hidden></p>
          </section>
          <aside id="terminal-position" class="terminal-position" aria-label="当前组合中的基金持仓"></aside>
          </div>
          <section class="terminal-research" aria-label="研究证据">
            <div class="terminal-research-heading"><h3 id="terminal-tab-risk">历史风险</h3><button class="icon-button" data-terminal-action="research-detail" title="查看完整研究" aria-label="查看完整研究">${icon("arrow-up-right")}</button></div>
            <div id="terminal-research-body" aria-labelledby="terminal-tab-risk"></div>
          </section>
        </div>
      </div>
      <footer class="terminal-statusbar"><span id="terminal-data-status"></span><span>手动刷新 · 历史快照</span></footer>`;
    const $ = (id) => root.querySelector(`#${id}`);
    const paint = (id, html) => { if ($(id).innerHTML !== html) $(id).innerHTML = html; };
    const subject = (state) => view.kind === "index"
      ? (state.marketSentiment?.index_overview?.indices || []).find((item) => item.index_code === state.selectedMarketIndexCode)
      : state.funds.find((item) => item.fund_code === state.selectedFundCode);
    const subjectContext = (state) => { const item = subject(state); return { kind: view.kind, code: item?.fund_code || item?.index_code, name: item?.short_name || item?.index_name || "当前标的" }; };

    function update() {
      if (scheduled) return;
      scheduled = true;
      requestAnimationFrame(() => { scheduled = false; render(); });
    }

    function render() {
      const state = getState();
      const indices = state.marketSentiment?.index_overview?.indices || [];
      if (lastFund !== state.selectedFundCode) {
        lastFund = state.selectedFundCode;
        clearComparison();
      }
      const item = subject(state);
      const isFund = view.kind === "fund";
      const name = item?.short_name || item?.index_name || "选择研究标的";
      paint("terminal-subject", `<h2>${e(name)}</h2><p><span class="terminal-code">${e(item?.fund_code || item?.index_code || "--")}</span><span>${e(item?.fund_type || item?.market_segment || "")}</span><span>${e(isFund ? item?.latest_nav_date || "" : item?.data_date || "")} ${isFund ? "净值" : "收盘"}</span></p>`);
      paint("terminal-quotes", metric(isFund ? "单位净值" : "收盘点位", D.number(isFund ? item?.latest_nav : item?.close, isFund ? 4 : 2)) + metric("日涨跌", D.percent(isFund ? item?.daily_growth_rate_pct : item?.daily_return_pct), D.tone(isFund ? item?.daily_growth_rate_pct : item?.daily_return_pct)) + metric(isFund ? "近30日" : "近30交易日", D.percent(item?.return_30d_pct), D.tone(item?.return_30d_pct)) + metric(isFund ? "近3个月" : "近3月 · 60交易日", D.percent(item?.return_3m_pct), D.tone(item?.return_3m_pct)));
      root.querySelector('[data-terminal-action="sync-subject"]').disabled = isFund ? state.fundSyncing : state.marketSyncing;
      root.querySelector('[data-terminal-action="ask-detail"]').disabled = !item || state.sending;
      root.querySelector('[data-chart-mode="nav"]').textContent = isFund ? "单位净值" : "指数点位";
      // 与图表使用同一标的；持仓快照更新只重绘侧栏，不重建 ECharts 实例。
      $("terminal-position").hidden = !isFund;
      if (isFund) paint("terminal-position", window.FundScopeHoldingsQuotes.position({
        portfolio:state.portfolioDetail, portfolioId:state.selectedPortfolioId, userId:state.userId,
        fundCode:state.selectedFundCode, loading:state.portfolioLoading, error:state.portfolioError,
      }));
      renderChart(state, item);
      renderResearch(state, item);
      $("terminal-data-status").textContent = `基金数据截至 ${state.fundCatalog?.snapshot_end || "--"}`;
      actions.icons();
    }

    function renderChart(state, item) {
      const isFund = view.kind === "fund";
      const data = isFund && state.fundChartData?.fund_code === state.selectedFundCode ? state.fundChartData : null;
      const points = isFund ? data?.points || [] : (item?.history || []).map((point) => ({ date: point.date, value: point.close }));
      const loading = isFund && state.fundChartLoading;
      const error = isFund ? state.fundChartError : state.marketSentimentError;
      const optionsKey = `${view.kind}:${state.selectedFundCode}:${state.funds.map((fund) => fund.fund_code).join()}:${data?.benchmark_points?.length || 0}`;
      if (fundOptionsKey !== optionsKey) {
        fundOptionsKey = optionsKey;
        $("terminal-comparison").innerHTML = '<option value="">不对比</option>' + (isFund ? `<option value="benchmark" ${data?.benchmark_points?.length ? "" : "disabled"}>业绩基准${data?.benchmark_points?.length ? "" : " · 暂缺"}</option>` + state.funds.filter((fund) => fund.fund_code !== state.selectedFundCode).map((fund) => `<option value="${e(fund.fund_code)}">${e(fund.short_name)}</option>`).join("") : "");
      }
      $("terminal-comparison").value = view.compare;
      $("terminal-comparison").disabled = !isFund || view.mode === "nav" || loading;
      $("terminal-chart-basis").textContent = isFund ? data?.return_basis_label || "收益口径待读取" : "日线收盘价";
      const comparison = view.compare === "benchmark" ? data?.benchmark_points || [] : view.comparison?.points || [];
      const plot = D.chartSeries(points, comparison, view.period, view.mode);
      const warning = [...plot.warnings, ...(data?.data_quality_warnings || []), ...(view.mode === "return" && view.compare === "benchmark" ? data?.benchmark_warnings || [] : []), ...(view.mode === "return" && view.comparison ? view.comparison.data_quality_warnings || [] : []), view.compareError, error, view.compareLoading ? "正在读取对比曲线..." : ""].filter(Boolean);
      $("terminal-chart-warning").hidden = !warning.length;
      $("terminal-chart-warning").textContent = warning.join("；");
      const missing = plot.dates.length < 2;
      $("terminal-chart-empty").hidden = !missing && !loading;
      $("terminal-chart-empty").textContent = loading ? "正在读取图表..." : error || "历史记录不足，请同步数据后重试";
      $("terminal-chart").style.visibility = missing || loading ? "hidden" : "visible";
      const compareName = view.compare === "benchmark" ? data?.benchmark_name || "业绩基准" : view.comparison?.short_name;
      $("terminal-chart-caption").textContent = plot.dates.length ? `${plot.dates[0]} 至 ${plot.dates.at(-1)} · ${plot.dates.length} 个数据点 · ${view.mode === "return" ? "起点归零" : isFund ? "单位净值未复权，分红会影响曲线" : "收盘点位"}${plot.comparison.length ? ` · 对比 ${compareName}` : ""}` : "等待可用历史数据";
      root.querySelectorAll("[data-chart-period]").forEach((button) => button.setAttribute("aria-pressed", String(button.dataset.chartPeriod === view.period)));
      root.querySelectorAll("[data-chart-mode]").forEach((button) => button.setAttribute("aria-pressed", String(button.dataset.chartMode === view.mode)));
      const key = JSON.stringify([view.kind, item?.fund_code || item?.index_code, view.period, view.mode, view.compare, plot]);
      if (!window.echarts) { $("terminal-chart-empty").hidden = false; $("terminal-chart-empty").textContent = "图表引擎加载失败，请刷新页面重试"; return; }
      if (missing || loading || key === chartKey) return;
      if (!$("terminal-chart").clientWidth) return;
      chartKey = key;
      chart ||= window.echarts.init($("terminal-chart"), null, { renderer: "canvas" });
      const line = (name, values, color) => ({ name, type: "line", data: values, showSymbol: false, symbolSize: 5, lineStyle: { width: 2, color }, itemStyle: { color }, connectNulls: false, smooth: false });
      // 新的标的/区间重置缩放，普通 UI 重绘不触碰图表状态。
      chart.setOption({
        animation: false,
        backgroundColor: "transparent",
        textStyle: { fontFamily: "Arial, PingFang SC, sans-serif", color: "#b0b1bb" },
        grid: { left: 60, right: 22, top: 38, bottom: 62 },
        legend: { top: 5, left: 16, textStyle: { color: "#bfc0c6", fontSize: 11 }, itemWidth: 14, itemHeight: 2 },
        tooltip: { trigger: "axis", confine: true, renderMode: "richText", backgroundColor: "#232428", borderColor: "#41434a", textStyle: { color: "#f2f2f4", fontSize: 12 }, valueFormatter: (value) => `${D.number(value, view.mode === "nav" && isFund ? 4 : 2)}${view.mode === "return" ? "%" : ""}`, axisPointer: { type: "cross", label: { backgroundColor: "#41434a" } } },
        // 坐标轴有自己的默认文字色，必须显式覆盖，不能仅依赖全局 textStyle。
        xAxis: { type: "category", boundaryGap: false, data: plot.dates, axisLine: { lineStyle: { color: "#35363c" } }, axisTick: { show: false }, axisLabel: { color: "#b0b1bb", hideOverlap: true, formatter: (value) => value.slice(5), fontSize: 11 } },
        yAxis: { type: "value", scale: true, splitNumber: 4, axisLabel: { color: "#b0b1bb", fontSize: 11, formatter: (value) => `${D.number(value, view.mode === "nav" && isFund ? 2 : 1)}${view.mode === "return" ? "%" : ""}` }, splitLine: { lineStyle: { color: "#292a30", type: "dashed" } } },
        dataZoom: [{ type: "inside", zoomOnMouseWheel: "ctrl", moveOnMouseMove: true }, { type: "slider", bottom: 8, height: 20, showDetail: false, borderColor: "#36373d", backgroundColor: "#191a1e", fillerColor: "#ffffff0c", handleStyle: { color: "#b5b6bf", borderWidth: 0 }, dataBackground: { lineStyle: { color: "#757782" }, areaStyle: { color: "#303138" } }, selectedDataBackground: { lineStyle: { color: "#b0b1b9" }, areaStyle: { color: "#303138" } }, brushSelect: false }],
        series: [line(item?.short_name || item?.index_name || "当前标的", plot.primary, "#edc85b"), ...(plot.comparison.length ? [line(compareName, plot.comparison, "#79aefa")] : [])],
      }, true);
      $("terminal-chart").setAttribute("aria-label", `${item?.short_name || item?.index_name}，${$("terminal-chart-caption").textContent}，末值 ${D.number(plot.primary.at(-1))}${view.mode === "return" ? "%" : ""}`);
    }

    // 基金详情只保留本基金风险；市场、社区与 AI 各自有独立工作区。
    function renderResearch(state) {
      const risk = state.fundRisk;
      const html = !risk || state.fundRiskLoading
        ? empty(state.fundRiskError || "正在读取风险证据...")
        : `<div class="terminal-section-meta"><span>风险观察期 ${risk.period_months} 个月</span><span>${e(risk.period_start)} 至 ${e(risk.period_end)}</span></div><dl class="terminal-risk-metrics">${metric("最大回撤", D.percent(-risk.max_drawdown.drawdown_pct), "down")}${metric("年化波动率", D.percent(risk.annualized_volatility_pct))}${metric("夏普比率", D.number(risk.risk_adjusted?.sharpe_ratio))}${metric("距区间高点", D.percent(-risk.current_drawdown_pct), "down")}</dl><p class="terminal-research-summary">${e(state.fundAssessment?.summary || "完整风险指标已就绪")}</p><footer class="terminal-evidence-footer"><span>${e(risk.return_basis_label)} · ${risk.observation_count} 个样本</span><button data-terminal-action="report">查看证据${icon("arrow-up-right")}</button></footer>`;
      paint("terminal-research-body", html);
    }

    function clearComparison() { view.requestId++; view.compare = ""; view.comparison = null; view.compareError = ""; view.compareLoading = false; }
    async function compare(value) {
      clearComparison();
      view.compare = value;
      const requestId = view.requestId;
      if (!value || value === "benchmark") { update(); return; }
      view.compareLoading = true;
      update();
      try {
        const result = await actions.loadChart(value);
        if (requestId === view.requestId) view.comparison = result;
      } catch (error) { if (requestId === view.requestId) view.compareError = `对比曲线读取失败：${error.message}`; }
      finally { if (requestId === view.requestId) { view.compareLoading = false; update(); } }
    }

    root.addEventListener("click", (event) => {
      const button = event.target.closest("button");
      if (!button || button.disabled) return;
      const state = getState();
      if (button.dataset.chartPeriod) view.period = button.dataset.chartPeriod;
      if (button.dataset.chartMode) view.mode = button.dataset.chartMode;
      const action = button.dataset.terminalAction;
      if (action === "ask-detail") actions.openResearch(subjectContext(state));
      if (action === "report") actions.openDetail(view.kind === "fund" ? "fund" : "market");
      if (action === "sync-subject") view.kind === "fund" ? actions.syncFunds() : actions.syncMarket();
      if (action === "reset-chart") chart?.dispatchAction({ type: "dataZoom", start: 0, end: 100 });
      if (action === "research-detail") actions.openDetail("fund");
      update();
    });
    $("terminal-comparison").addEventListener("change", (event) => compare(event.target.value));
    const observer = new ResizeObserver(() => { chart?.resize(); update(); });
    observer.observe($("terminal-chart"));
    update();
    return { update, selectFund() { view.kind = "fund"; update(); }, dispose() { observer.disconnect(); chart?.dispose(); } };
  }
  window.FundScopeTerminal = { create };
})();
