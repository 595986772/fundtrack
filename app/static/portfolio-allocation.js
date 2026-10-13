/* 约束求解独立工作区：自然语言只整理草案，确认按钮才计算，结果从不写入持仓。 */
(function (root) {
  "use strict";
  const esc = v => String(v ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const num = (v, digits = 2) => Number.isFinite(Number(v)) ? Number(v).toFixed(digits) : "--";
  const icon = name => `<i data-lucide="${name}" aria-hidden="true"></i>`;
  const fields = { min_bond_weight_pct: "债券类基金至少 %", max_single_fund_weight_pct: "单只基金不超过 %", max_turnover_pct: "单边换手不超过 %" };
  const statusLabel = { awaiting_confirmation: "待确认条件", solved: "已完成求解", infeasible: "条件无解" };
  const changedBasis = s => Boolean(s.draft && (s.question !== s.draft.question || s.source !== s.draft.source_run_id));
  const initialState = () => ({ question: "", source: "", draft: null, values: {}, confirmed: false, history: [], busy: false, error: "", notice: "" });
  function canConfirm(s) {
    return Boolean(s.draft?.sample && !s.draft.result && !s.busy && s.confirmed && s.question === s.draft.question && s.source === s.draft.source_run_id
      && !(s.draft.proposal.unsupported_constraints || []).length && Object.keys(fields).every(k =>
        s.values[k] !== "" && s.values[k] != null && Number.isFinite(Number(s.values[k])) && Number(s.values[k]) >= 0 && Number(s.values[k]) <= 100));
  }
  function render(s) {
    const d = s.draft, r = d?.result, disabled = s.busy ? "disabled" : "";
    const immutable = s.busy || r ? "disabled" : "";
    return `<div class="stress-workbench allocation-workbench" tabindex="-1" aria-label="约束求解工作区" aria-busy="${s.busy}">
      <header class="stress-heading"><h2>组合约束求解</h2><span>${d ? statusLabel[d.status] : "待整理需求"} · 不改变实际持仓</span></header>
      <form data-allocation-form="new" class="allocation-request">
        <label>配置要求<textarea data-allocation-field="question" required minlength="2" maxlength="2000" ${disabled} placeholder="例如：债券类基金至少40%，单只基金不超过60%，单边换手最多20%，降低历史风险。">${esc(s.question)}</textarea></label>
        <div class="allocation-request-actions"><label>沿用压力记录（可选）<input data-allocation-field="source" pattern="stress_[a-f0-9]{16}" value="${esc(s.source)}" ${disabled} placeholder="stress_…"></label>
        <button type="submit" class="primary-action compact" ${disabled}>${icon("sparkles")}${s.busy ? "处理中" : d ? "整理为新草案" : "AI 整理条件"}</button></div>
      </form>
      <div class="stress-history"><button type="button" class="secondary-action compact" data-allocation-action="history" ${disabled}>${icon("history")}读取草案记录</button>
        <select data-allocation-history aria-label="选择约束草案" ${disabled}><option value="">选择记录</option>${s.history.map(h => `<option value="${esc(h.draft_id)}" ${h.draft_id === d?.draft_id ? "selected" : ""}>${esc(h.draft_id)} · ${statusLabel[h.status] || ""} · ${esc(h.question)}</option>`).join("")}</select></div>
      ${s.error ? `<p class="stress-error" role="alert">${esc(s.error)}</p>` : ""}
      <p class="stress-notice" role="status">${esc(s.notice || (s.busy ? "正在处理，请稍候…" : ""))}</p>
      ${d ? `<section class="allocation-section"><div class="stress-section-heading"><h3>核对约束与基金范围</h3><span>${esc(d.draft_id)}</span></div>
        <p class="allocation-original">原始需求：${esc(d.question)}</p>
        <p data-allocation-stale class="stress-error" role="status" ${changedBasis(s) ? "" : "hidden"}>需求或压力来源已修改，下方仍是旧草案；请点击“整理为新草案”后再确认。</p>
        <p class="stress-method">${esc(d.portfolio_name)} · 冻结于 ${esc(new Date(d.captured_at).toLocaleString("zh-CN"))}<br>目标：历史单日95% CVaR最小；只使用下列基金，非负且合计100%。</p>
        ${d.sample ? `<p class="stress-method">实际历史：${esc(d.sample.period_start)} 至 ${esc(d.sample.period_end)} · ${d.sample.eligible_count}个完整共同观察日，排除${d.sample.excluded_count}个跨缺日区间。<br>压力窗口：${esc(d.window.period_start)} 至 ${esc(d.window.period_end)}。${esc(d.sample.calendar_note)}</p>` : `<p class="stress-error">此旧草案没有完整样本摘要，请新建草案后核对。</p>`}
        <ul class="allocation-sources">${(d.data_sources || []).map(source => `<li>${esc(source.fund_code)} · ${esc(source.return_basis_label || source.return_basis || "收益口径未标明")} · ${esc(source.return_basis_status || "请核对来源说明")} · ${esc(source.source_name || source.source || "来源未标明")}${(source.data_quality_warnings || []).map(v => `<br>${esc(v)}`).join("")}</li>`).join("")}</ul>
        <div class="stress-table-scroll"><table><caption>基金类型来自本地档案，债券类基金不等于纯债资产。</caption><thead><tr><th>基金</th><th>类型</th><th>计入债券类</th><th>原权重</th></tr></thead><tbody>${d.holdings.map(h => {
          const type = d.classifications.find(v => v.fund_code === h.fund_code) || {};
          return `<tr><th><strong>${esc(h.short_name)}</strong><small>${esc(h.fund_code)}</small></th><td>${esc(type.fund_type)}</td><td>${type.is_bond === true ? "是" : type.is_bond === false ? "否" : "未知"}</td><td>${num(h.weight_pct)}%</td></tr>`;
        }).join("")}</tbody></table></div>
        ${d.proposal.questions.length && !r ? `<ul class="allocation-questions">${d.proposal.questions.map(q => `<li>${esc(q)}</li>`).join("")}</ul>` : ""}
        ${d.proposal.unsupported_constraints.length ? `<div class="stress-error"><h3>以下条件尚不支持，不能继续求解</h3><ul>${d.proposal.unsupported_constraints.map(q => `<li>${esc(q)}</li>`).join("")}</ul><p>请修改需求后新建草案，这些条件没有被自动忽略。</p></div>` : ""}
        <form data-allocation-form="confirm"><div class="allocation-fields">${Object.entries(fields).map(([key, label]) => `<label>${label}<input data-allocation-value="${key}" type="number" min="0" max="100" step="any" required value="${esc(s.values[key] ?? "")}" ${immutable}></label>`).join("")}</div>
          <p class="stress-method">单边换手 = 各基金权重变化绝对值之和 ÷ 2。无最低要求填0，无上限要求填100。</p>
          ${r ? "" : `<label class="allocation-confirm"><input type="checkbox" data-allocation-ack ${s.confirmed ? "checked" : ""} ${disabled}>我已核对条件、基金分类和冻结数据，仅生成研究方案。</label>
          <button type="submit" data-allocation-confirm-button class="primary-action compact" ${canConfirm(s) ? "" : "disabled"}>${icon("calculator")}确认条件并求解</button>`}
        </form>
      </section>
      ${r?.status === "infeasible" ? `<section class="allocation-section"><h3>当前条件无法同时满足</h3><p>${esc(r.reason)}</p><ul>${r.conflicts.map(c => `<li>${esc(c.label)}：${num(c.value_pct)}%</li>`).join("")}</ul><p>${esc(r.question)}</p><p class="stress-method">以上是一个不可约冲突子集，不代表所有冲突或最小放宽幅度。</p></section>` : ""}
      ${r?.status === "solved" ? renderResult(d) : ""}
      <details class="stress-audit" ${s.auditOpen ? "open" : ""}><summary>冻结依据与计算口径</summary><dl><dt>草案指纹</dt><dd>${esc(d.draft_hash)}</dd><dt>行情指纹</dt><dd>${esc(d.input_hash)}</dd><dt>原情景</dt><dd>${esc(d.source_run_id)}</dd><dt>求解版本</dt><dd>${esc(d.solver_version)}</dd></dl><ul>${d.limitations.map(v => `<li>${esc(v)}</li>`).join("")}</ul><pre>${esc(JSON.stringify({ classifications: d.classifications, checks: r?.checks, facts: r?.facts, sources: d.data_sources }, null, 2))}</pre></details>`
        : `<div class="stress-empty"><h3>尚无约束草案</h3><p>历史样本默认近1年。数据只来自已保存净值，行情不足时会停止，不会自动采集。</p></div>`}
    </div>`;
  }
  function renderResult(d) {
    const r = d.result, stress = r.stress_report;
    const delta = r.candidate.mean_daily_return_pct - r.baseline.mean_daily_return_pct;
    return `<section class="allocation-section"><div class="stress-section-heading"><h3>候选配置与权衡</h3><button type="button" class="secondary-action compact" data-allocation-action="agent">${icon("sparkles")}AI 解读</button></div>
      <p>${r.risk_improved ? "样本内尾部损失指标降低。" : "候选配置没有降低样本内尾部损失指标，不能称为风险改善。"} 单边换手 ${num(r.turnover_pct)}%。平均日收益${delta >= 0 ? "提高" : "降低"} ${num(Math.abs(delta), 4)} 个百分点。</p>
      <div class="stress-table-scroll"><table><caption>${r.sample_count}个完整共同观察日，排除${r.excluded_count}个跨缺日区间；尾部约${num(r.tail_observations, 1)}个样本。CVaR越低表示该样本下尾部损失越小。</caption><thead><tr><th>历史指标</th><th>原配置</th><th>候选配置</th></tr></thead><tbody><tr><th>单日95% CVaR</th><td>${num(r.baseline.cvar_95_daily_pct, 4)}%</td><td>${num(r.candidate.cvar_95_daily_pct, 4)}%</td></tr><tr><th>平均日收益</th><td>${num(r.baseline.mean_daily_return_pct, 4)}%</td><td>${num(r.candidate.mean_daily_return_pct, 4)}%</td></tr></tbody></table></div>
      <div class="stress-table-scroll"><table><caption>满足的条件</caption><thead><tr><th>约束</th><th>确认值</th><th>实际值</th><th>校验</th></tr></thead><tbody>${r.checks.map(c => `<tr><th>${esc(c.label)}</th><td>${num(c.limit_pct)}%</td><td>${num(c.actual_pct)}%</td><td>${c.passed ? "通过" : "未通过"}</td></tr>`).join("")}</tbody></table></div>
      <div class="stress-table-scroll"><table><caption>仅供研究，不代表已成交</caption><thead><tr><th>基金</th><th>原权重</th><th>候选权重</th><th>变化 / 百分点</th></tr></thead><tbody>${d.holdings.map(h => `<tr><th><strong>${esc(h.short_name)}</strong><small>${esc(h.fund_code)}</small></th><td>${num(h.weight_pct)}%</td><td>${num(r.target_weights[h.fund_code])}%</td><td>${num(r.target_weights[h.fund_code] - h.weight_pct)}</td></tr>`).join("")}</tbody></table></div>
    </section><section class="allocation-section"><h3>同一历史压力情景</h3><p class="stress-method">${esc(stress.window.period_start)} 至 ${esc(stress.window.period_end)} · 原配置最差连续${stress.window.days}个共同观察日。没有为候选方案重新挑选窗口。</p>
      <div class="stress-table-scroll"><table><thead><tr><th>配置</th><th>区间收益</th><th>最大回撤幅度</th></tr></thead><tbody>${stress.scenarios.map(c => `<tr><th>${esc(c.label)}</th><td>${num(c.period_return_pct)}%</td><td>${num(c.max_drawdown_pct)}%</td></tr>`).join("")}</tbody></table></div>
      <div data-allocation-chart class="stress-chart" role="img" aria-label="同一压力窗口下原配置与候选配置净值曲线"></div>
      <p class="stress-method">样本内优化不是未来保证，也不保证其他风险指标改善。申赎成本及交易限制未计入。</p></section>`;
  }
  function focusKey(el) {
    if (!el?.dataset) return null;
    if (el.matches?.("summary")) return "audit";
    return el.dataset.allocationField || el.dataset.allocationValue || el.dataset.allocationAction
      || (el.hasAttribute?.("data-allocation-history") ? "history-select" : null)
      || (el.hasAttribute?.("data-allocation-ack") ? "ack" : null)
      || (el.matches?.("button[type=submit]") ? el.closest("form")?.dataset.allocationForm : null);
  }
  function create({ request, getContext, refreshIcons, askAgent, onEditEnd }) {
    let s = initialState(), host = null, key = "", sequence = 0, pending = "", pendingKey = "", chart = null, observer = null;
    let focus = null, restore = false;
    let composing = false;
    const ctxKey = ctx => `${ctx.userId}:${ctx.portfolioId || ""}`;
    const dispose = () => { observer?.disconnect(); observer = null; chart?.dispose(); chart = null; };
    function draw() {
      const active = root.document?.activeElement, inside = host?.contains?.(active);
      if (inside) focus = focusKey(active) || focus;
      const audit = host?.querySelector("details"); if (audit) s.auditOpen = audit.open;
      const shouldRestore = inside || (restore && (!active || active === root.document.body)); restore = false;
      dispose(); if (!host) return;
      host.innerHTML = render(s); refreshIcons?.();
      if (shouldRestore && focus) {
        const el = Array.from(host.querySelectorAll("input,textarea,select,button,summary")).find(e => focusKey(e) === focus);
        (el && !el.disabled ? el : host.querySelector(".allocation-workbench"))?.focus({ preventScroll: true });
      }
      const canvas = host.querySelector("[data-allocation-chart]");
      if (canvas && root.echarts) {
        const resize = () => {
          if (!canvas.isConnected || !canvas.clientWidth) return;
          if (!chart) { chart = root.echarts.init(canvas); chart.setOption(root.FundScopePortfolioStress.chartOption(s.draft.result.stress_report)); }
          else chart.resize();
        };
        if (root.ResizeObserver) { observer = new root.ResizeObserver(resize); observer.observe(canvas); }
        resize();
      }
    }
    function accept(d) {
      s.draft = d; s.question = d.question; s.source = d.source_run_id; s.confirmed = false;
      s.values = { ...(d.result?.constraints || Object.fromEntries(Object.keys(fields).map(k => [k, d.proposal[k] ?? ""]))) };
      s.history = [d, ...s.history.filter(h => h.draft_id !== d.draft_id)].slice(0, 20);
    }
    async function execute(action, id) {
      if (s.busy || (action === "confirm" && !canConfirm(s))) return;
      const ctx = getContext(), origin = ctxKey(ctx), turn = ++sequence;
      if (!ctx.portfolioId || origin !== key) return;
      const base = `/api/v1/portfolios/${encodeURIComponent(ctx.portfolioId)}/allocation-drafts`;
      s.busy = true; s.error = ""; s.notice = "";
      try {
        let url = base, options = {};
        if (action === "new" || action === "confirm") {
          let body;
          if (action === "new") {
            body = { user_id: ctx.userId, question: s.question, source_run_id: s.source || null };
            const signature = JSON.stringify(body);
            if (signature !== pending) { const value = root.FundScopePortfolioStress.requestKey(); pending = signature; pendingKey = value; }
            body.idempotency_key = pendingKey;
          } else {
            url += `/${encodeURIComponent(s.draft.draft_id)}/confirm`;
            body = { user_id: ctx.userId, draft_hash: s.draft.draft_hash, confirm: true,
              constraints: Object.fromEntries(Object.keys(fields).map(k => [k, Number(s.values[k])])) };
          }
          options = { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };
        } else {
          if (action === "read") url += `/${encodeURIComponent(id)}`;
          url += `?user_id=${encodeURIComponent(ctx.userId)}`;
        }
        draw();
        const value = await request(url, options);
        // 外层异步重绘和切换账户/组合时，旧请求不能覆盖新上下文。
        if (sequence !== turn || origin !== ctxKey(getContext())) return;
        if (action === "history") { s.history = value; s.notice = value.length ? "已读取最近草案" : "暂无保存草案"; }
        else { accept(value); s.notice = value.result ? "结果已保存，实际持仓未变动。" : "草案已保存，请核对条件。"; }
        if (action === "new") { pending = ""; pendingKey = ""; }
      } catch (error) {
        if (sequence === turn && origin === ctxKey(getContext())) s.error = error.message;
      } finally {
        if (sequence === turn && origin === ctxKey(getContext())) { s.busy = false; draw(); }
      }
    }
    function bind() {
      // 中文组合输入期间不能替换编辑节点；失焦后外层才处理被延后的工作台重绘。
      host.oncompositionstart = () => { composing = true; };
      host.oncompositionend = () => { composing = false; };
      host.onfocusout = () => { root.queueMicrotask?.(() => onEditEnd?.()); };
      host.oninput = event => {
        const e = event.target;
        if (!e.dataset.allocationField && !e.dataset.allocationValue) return;
        if (e.dataset.allocationField) s[e.dataset.allocationField] = e.value;
        if (e.dataset.allocationValue) s.values[e.dataset.allocationValue] = e.value;
        const warning = host.querySelector("[data-allocation-stale]"); if (warning) warning.hidden = !changedBasis(s);
        // 修改任何确认依据都会撤销勾选，避免确认旧值后悄悄改数字。
        s.confirmed = false;
        const ack = host.querySelector("[data-allocation-ack]"); if (ack) ack.checked = false;
        const button = host.querySelector("[data-allocation-confirm-button]"); if (button) button.disabled = true;
      };
      host.onchange = event => {
        if (event.target.matches("[data-allocation-ack]")) {
          s.confirmed = event.target.checked;
          host.querySelector("[data-allocation-confirm-button]").disabled = !canConfirm(s);
        }
        if (event.target.matches("[data-allocation-history]") && event.target.value) execute("read", event.target.value);
      };
      host.onsubmit = event => {
        const action = event.target.dataset.allocationForm; if (!action) return;
        event.preventDefault(); event.stopPropagation(); execute(action);
      };
      host.onclick = event => {
        const action = event.target.closest("[data-allocation-action]")?.dataset.allocationAction;
        if (!action || s.busy) return;
        if (action === "agent") askAgent(s.draft); else execute(action);
      };
    }
    return {
      isEditing() {
        const active = root.document?.activeElement;
        return key === ctxKey(getContext()) && Boolean(composing || (host?.contains?.(active) && active?.matches?.("input,textarea,select")));
      },
      beforeUnmount() { const active = root.document?.activeElement; restore = Boolean(host?.contains?.(active)); if (restore) focus = focusKey(active) || focus; },
      mount(element) {
        const next = ctxKey(getContext()); let changed = false;
        if (next !== key) { key = next; s = initialState(); sequence++; pending = ""; pendingKey = ""; focus = null; restore = false; composing = false; changed = true; }
        if (host && element && host !== element && !changed && element.replaceWith) {
          // 外层只是刷新其他模块时复用原DOM，保留details、滚动、字段选区及图表实例。
          element.replaceWith(host);
          if (restore && focus) Array.from(host.querySelectorAll("input,textarea,select,button,summary"))
            .find(e => focusKey(e) === focus)?.focus({ preventScroll: true });
          restore = false;
        } else if (host !== element || changed) { dispose(); host = element; if (host) { bind(); draw(); } }
      },
      execute,
    };
  }
  const api = { create, render, initialState, canConfirm, focusKey, changedBasis };
  if (typeof module !== "undefined" && module.exports) module.exports = api; else root.FundScopePortfolioAllocation = api;
})(typeof window === "undefined" ? globalThis : window);
