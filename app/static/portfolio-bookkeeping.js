/* 记账工作区：待确认申请与已成交账本分离；仅登记外部结果，不发起真实交易。
 * 金额以输入字符串送后端，浏览器不负责成本核算。草稿按用户和组合隔离，刷新不丢输入。
 */
(function (root) {
  "use strict";
  const sessions = new Map();
  const statuses = {pending:"待确认", cancel_requested:"撤单处理中", cancelled:"已撤单", rejected:"申请失败", confirmed:"已确认", settled:"赎回已到账", reversed:"记账已冲正"};
  const actions = {confirm:"登记成交确认", cancel_request:"登记撤单申请", cancel_success:"登记撤单成功", cancel_rejected:"登记撤单失败", reject:"登记申请失败", settle:"登记赎回到账", replace:"更正记录", reverse:"冲正误录"};
  const esc = value => String(value ?? "").replace(/[&<>"']/g, char => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[char]));
  const numeric = (value, digits=2) => value == null ? "--" : Number(value).toLocaleString("zh-CN", {minimumFractionDigits:digits, maximumFractionDigits:digits});
  const dateText = value => String(value || "--").slice(0, 10);
  // 所有业务日期按北京时间显示，不随设备时区变化。
  const nowChina = () => new Date(Date.now() + 8 * 3600000).toISOString().slice(0,16);
  const keyOf = ctx => `${ctx.userId}:${ctx.portfolio.id}`;
  function session(ctx) {
    const key = keyOf(ctx);
    if (!sessions.has(key)) sessions.set(key, {mode:"posted", list:ctx.portfolio.pending_order_count ? "orders" : "transactions", editor:null, drafts:{}, busy:false, error:"", retry:null});
    return sessions.get(key);
  }
  function allowed(order) {
    if (order.status === "pending") return ["confirm", "cancel_request", "cancel_success", "reject"];
    if (order.status === "cancel_requested") return ["cancel_success", "cancel_rejected", "confirm", "reject"];
    if (order.status === "confirmed" && order.transaction_type === "sell") return ["settle"];
    return [];
  }
  function defaults(ctx) {
    return {fund_code:ctx.portfolio.holdings?.[0]?.fund_code || ctx.funds[0]?.fund_code || "", transaction_type:"buy",
      trade_date:nowChina().slice(0,10), submitted_at:nowChina(), fee:"0", channel:"off_exchange"};
  }
  function formKey(s) { return s.editor ? `${s.editor.kind}:${s.editor.id}:${s.editor.action}` : s.mode; }
  function draft(ctx, s) { return s.drafts[formKey(s)] ||= defaults(ctx); }
  function funds(ctx) {
    // 基金库和当前持仓取并集，基金取消关注后仍然可以减仓或核对历史。
    return [...new Map([...(ctx.funds || []), ...(ctx.portfolio.holdings || [])].map(fund => [fund.fund_code, fund])).values()];
  }
  function field(d, name, label, type="text", attrs="") {
    return `<label><span>${label}</span><input name="${name}" type="${type}" value="${esc(d[name] ?? "")}" ${attrs}></label>`;
  }
  function fundField(ctx, d) {
    return `<label class="bk-wide"><span>基金</span><select name="fund_code" required>${funds(ctx).map(f => `<option value="${esc(f.fund_code)}" ${f.fund_code === d.fund_code ? "selected" : ""}>${esc(f.short_name || f.fund_code)} · ${esc(f.fund_code)}</option>`).join("")}</select></label>`;
  }
  function direction(d) {
    return `<fieldset class="bk-segment bk-wide"><legend class="visually-hidden">交易方向</legend>${["buy","sell"].map(type => `<label><input type="radio" name="transaction_type" value="${type}" ${d.transaction_type === type ? "checked" : ""}><span>${type === "buy" ? "买入 / 加仓" : "卖出 / 减仓"}</span></label>`).join("")}</fieldset>`;
  }
  function actualFields(d) {
    return field(d,"trade_date","成交净值日","date",`max="${nowChina().slice(0,10)}" required`)
      + field(d,"units","确认份额","number",'min="0.0001" max="1000000000000" step="0.0001" required')
      + field(d,"unit_price","成交净值","number",'min="0.000001" max="1000000000000" step="0.000001" required')
      + field(d,"fee","手续费（元）","number",'min="0" max="1000000000000" step="0.01" required');
  }
  function editor(ctx, s) {
    const d = draft(ctx,s), edit = s.editor;
    let title = s.mode === "pending" ? "登记待确认申请" : "登记已成交";
    let fields = "", warning = "";
    if (edit) {
      title = actions[edit.action];
      const target = edit.kind === "order" ? ctx.orders.find(o => o.id === edit.id) : ctx.transactions.find(t => t.id === edit.id);
      if (!target) return '<p role="status">记录已变化，请刷新后重新选择。</p>';
      fields += `<p class="bk-wide bk-subject">${esc(target.fund_code)} · ${target.transaction_type === "buy" ? "买入" : "卖出"}<small>${esc(target.id)}</small></p>`;
      if (edit.action === "confirm") {
        fields += actualFields(d) + field(d,"confirmation_date","平台确认日期","date",`max="${nowChina().slice(0,10)}" required`);
      } else if (edit.action === "settle") {
        fields += field(d,"settlement_date","实际到账日期","date",`max="${nowChina().slice(0,10)}" required`);
      } else if (edit.action === "replace") {
        fields += fundField(ctx,d) + direction(d) + actualFields(d) + field(d,"note","备注","text",'maxlength="200"');
      }
      warning = edit.kind === "correction" ? "仅更正本地误录，不能撤回真实成交。原记录保留。"
        : edit.action === "cancel_request" ? "撤单处理中仍占用卖出份额，尚未解除。"
        : edit.action === "cancel_success" ? "请以平台撤单成功结果为准；撤单成功不代表退款已到账。" : "请按外部平台的实际结果登记。";
      fields += `<label class="bk-wide"><span>${edit.kind === "correction" ? "更正原因" : "外部结果依据"}</span><textarea name="reason" rows="2" maxlength="300" required>${esc(d.reason || "")}</textarea></label>`;
    } else {
      fields = fundField(ctx,d) + direction(d);
      if (s.mode === "pending") {
        fields += field(d,"submitted_at","申请时间（北京时间）","datetime-local",`max="${nowChina()}" required`);
        fields += `<label><span>渠道</span><select name="channel"><option value="off_exchange" ${d.channel === "off_exchange" ? "selected" : ""}>场外基金</option><option value="exchange" ${d.channel === "exchange" ? "selected" : ""}>场内交易</option></select></label>`;
        fields += d.transaction_type === "buy"
          ? field(d,"requested_amount","申购金额（含费，元）","number",'min="0.01" max="1000000000000" step="0.01" required')
          : field(d,"requested_units","申请卖出份额","number",'min="0.0001" max="1000000000000" step="0.0001" required');
      } else fields += actualFields(d);
      if (d.transaction_type === "sell") {
        const h = ctx.portfolio.holdings?.find(item => item.fund_code === d.fund_code);
        fields += `<p class="bk-availability bk-wide">持有 ${numeric(h?.units || 0,4)} 份 · 占用 ${numeric(h?.reserved_units || 0,4)} 份 · <strong>可用 ${numeric(h?.available_units ?? h?.units ?? 0,4)} 份</strong></p>`;
      }
      fields += field(d,"note","备注","text",'maxlength="200"');
    }
    return `<section class="transaction-entry-section bk-entry"><header><h3 tabindex="-1">${title}</h3>${edit ? '<button class="icon-button" data-bk-action="close" title="关闭编辑" aria-label="关闭编辑"><i data-lucide="x"></i></button>' : ""}</header>
      ${!edit ? `<div class="bk-segment" role="group" aria-label="记账方式">${["posted","pending"].map(mode => `<button type="button" data-bk-action="mode" data-value="${mode}" aria-pressed="${s.mode === mode}" ${s.busy ? "disabled" : ""}>${mode === "posted" ? "已成交" : "待确认"}</button>`).join("")}</div>` : ""}
      <form data-bookkeeping-form><fieldset class="bk-fields" ${s.busy || ctx.loading || ctx.error ? "disabled" : ""}><legend class="visually-hidden">${title}</legend>${fields}
        ${warning ? `<p class="bk-warning bk-wide">${warning}</p>` : ""}
        <button type="submit" class="primary-action compact bk-submit"><i data-lucide="${s.busy ? "loader-circle" : "check"}"></i>${s.busy ? "正在保存" : title}</button>
      </fieldset></form></section>`;
  }
  function actionMenu(kind,id,available,s) {
    if (!available.length) return "";
    return `<label class="bk-result-action"><span class="visually-hidden">${kind === "order" ? "登记结果" : "更正流水"}</span><select data-bk-result="${kind}" data-id="${esc(id)}" ${s.busy ? "disabled" : ""}><option value="">${kind === "order" ? "登记结果…" : "更正流水…"}</option>${available.map(action => `<option value="${action}">${actions[action]}</option>`).join("")}</select></label>`;
  }
  function orderRow(ctx,order,s) {
    const fund = funds(ctx).find(f => f.fund_code === order.fund_code);
    const size = order.transaction_type === "buy" ? `${numeric(order.requested_amount)} 元` : `${numeric(order.requested_units,4)} 份`;
    return `<article class="bk-row" data-bk-order="${esc(order.id)}"><div class="bk-row-heading"><strong>${esc(fund?.short_name || order.fund_code)}</strong><span class="bk-status" data-status="${esc(order.status)}">${statuses[order.status] || esc(order.status)}</span></div>
      <div class="bk-row-meta"><span>${order.transaction_type === "buy" ? "申购" : "赎回"} ${size}</span><time>${new Date(order.submitted_at).toLocaleString("zh-CN",{timeZone:"Asia/Shanghai",hour12:false})}</time></div>
      ${order.confirmed_amount != null ? `<p class="bk-amount">${order.transaction_type === "buy" ? "确认支出" : "确认净额"} ${numeric(order.confirmed_amount)} 元${order.status === "confirmed" && order.transaction_type === "sell" ? " · 待登记到账" : ""}</p>` : ""}
      <div class="bk-row-bottom"><details class="bk-events"><summary>处理记录 · ${(order.events || []).length}</summary><ol>${(order.events || []).map(e => `<li><strong>${e.action === "created" ? "登记申请" : actions[e.action] || "账务更正"}</strong><span>${esc(e.reason)}</span><small>${dateText(e.created_at)}</small></li>`).join("")}</ol></details>${actionMenu("order",order.id,allowed(order),s)}</div></article>`;
  }
  function transactionRow(row,s) {
    const active = row.effective !== false, p = row.exact_values || row;
    return `<article class="bk-row" data-bk-transaction="${esc(row.id)}" data-effective="${active}"><div class="bk-row-heading"><strong>${row.transaction_type === "buy" ? "买入" : "卖出"} ${esc(row.short_name || row.fund_code)}</strong><span class="bk-status">${{corrected:"已更正",reversed:"已冲正"}[row.status] || "已入账"}</span></div>
      <div class="bk-row-meta"><time>${dateText(row.trade_date)}</time><span>${esc(p.units)} 份 × ${esc(p.unit_price)}</span><strong>${numeric(row.cash_flow)} 元</strong></div>
      <p class="bk-note">手续费 ${numeric(p.fee)} 元${row.note ? ` · ${esc(row.note)}` : ""}</p>
      ${active && row.realized_pnl_amount != null ? `<p class="bk-amount">本笔已实现盈亏 ${numeric(row.realized_pnl_amount)} 元</p>` : ""}
      ${row.correction ? `<p class="bk-warning">${esc(row.correction.reason)}${row.correction.replacement_transaction_id ? ` · 新流水 ${esc(row.correction.replacement_transaction_id)}` : ""}</p>` : ""}
      <div class="bk-row-bottom"><small>${esc(row.id)}</small>${active ? actionMenu("correction",row.id,["replace","reverse"],s) : ""}</div></article>`;
  }
  function render(ctx) {
    const s = session(ctx), p = ctx.portfolio;
    const rows = s.list === "orders" ? ctx.orders.map(order => orderRow(ctx,order,s)) : ctx.transactions.map(row => transactionRow(row,s));
    return `<div class="bk-workspace"><header class="bk-summary"><dl><div><dt>待确认申请</dt><dd>${p.pending_order_count || 0}<small>笔</small></dd></div><div><dt>待确认申购</dt><dd>${numeric(p.pending_buy_amount || 0)}<small>元</small></dd></div><div><dt>赎回待到账</dt><dd>${numeric(p.unsettled_redemption_amount || 0)}<small>元</small></dd></div></dl><button class="icon-button" data-bk-action="refresh" aria-label="刷新账本" title="刷新账本" ${s.busy || ctx.loading ? "disabled" : ""}><i data-lucide="refresh-cw"></i></button></header>
      <p class="bk-boundary">仅登记外部交易，不执行下单或撤单。</p>
      ${s.error || ctx.error ? `<p class="bk-error" role="alert">${esc(s.error || ctx.error)}</p>` : ""}
      <div class="bk-layout">${editor(ctx,s)}<section class="bk-history"><header><div class="bk-tabs" role="tablist" aria-label="记录类型">${["orders","transactions"].map(type => `<button type="button" role="tab" aria-controls="bk-records" aria-selected="${s.list === type}" data-bk-action="list" data-value="${type}">${type === "orders" ? "交易申请" : "成交流水"}<small>${type === "orders" ? ctx.orders.length : ctx.transactions.length}</small></button>`).join("")}</div></header>
      <div id="bk-records" role="tabpanel" aria-label="${s.list === "orders" ? "交易申请" : "成交流水"}">${ctx.loading ? '<p class="bk-empty" role="status">正在读取账本…</p>' : ctx.error ? '<p class="bk-empty">暂时无法读取记录，请刷新重试。</p>' : rows.join("") || `<p class="bk-empty">${s.list === "orders" ? "暂无待确认或历史申请" : "暂无成交流水"}</p>`}</div></section></div></div>`;
  }
  function capture(event,ctx) {
    const form = event.target.closest("[data-bookkeeping-form]");
    if (form) Object.assign(draft(ctx,session(ctx)), Object.fromEntries(new FormData(form)));
  }
  function change(event,ctx) {
    const s = session(ctx), result = event.target.closest("[data-bk-result]");
    if (s.busy) return;
    if (result?.value) {
      s.editor = {kind:result.dataset.bkResult,id:result.dataset.id,action:result.value}; s.error = "";
      if (s.editor.kind === "correction") {
        const row = ctx.transactions.find(t => t.id === s.editor.id);
        if (row) s.drafts[formKey(s)] ||= {...row,...row.exact_values};
      } else {
        const order = ctx.orders.find(o => o.id === s.editor.id);
        s.drafts[formKey(s)] ||= {...defaults(ctx), units:order?.requested_units || ""};
      }
      ctx.render();
      // 窄屏列表位于表单下方，选择结果后把焦点交给编辑区，避免用户以为没有响应。
      root.requestAnimationFrame?.(() => { if (ctx.current()) root.document?.querySelector('.bk-entry h3')?.focus(); });
      return;
    }
    capture(event,ctx);
    if (["fund_code","transaction_type"].includes(event.target.name)) ctx.render();
  }
  function click(event,ctx) {
    const target = event.target.closest("[data-bk-action]");
    if (!target) return false;
    const s = session(ctx); if (s.busy) return true;
    const action = target.dataset.bkAction;
    if (action === "refresh") {
      s.busy = true; s.error = ""; ctx.render();
      Promise.resolve().then(() => ctx.refresh()).catch(error => { s.error = error.message; })
        .finally(() => { s.busy = false; if (ctx.current()) ctx.render(); });
      return true;
    }
    if (action === "mode") { s.mode = target.dataset.value; s.list = s.mode === "pending" ? "orders" : "transactions"; s.editor = null; s.error = ""; }
    if (action === "list") s.list = target.dataset.value;
    if (action === "close") { s.editor = null; s.error = ""; }
    ctx.render(); return true;
  }
  function command(ctx,s,d) {
    const edit = s.editor, base = `/api/v1/portfolios/${encodeURIComponent(ctx.portfolio.id)}`;
    const actual = () => ({fund_code:d.fund_code, transaction_type:d.transaction_type, trade_date:d.trade_date,
      units:d.units, unit_price:d.unit_price, fee:d.fee || "0", note:d.note || ""});
    if (edit?.kind === "correction") return {url:`${base}/transactions/${encodeURIComponent(edit.id)}/corrections`,
      body:{reason:d.reason, replacement:edit.action === "replace" ? actual() : null}};
    if (edit?.kind === "order") {
      const body = {action:edit.action, reason:d.reason};
      if (edit.action === "confirm") Object.assign(body,{trade_date:d.trade_date,confirmation_date:d.confirmation_date,units:d.units,unit_price:d.unit_price,fee:d.fee || "0"});
      if (edit.action === "settle") body.settlement_date = d.settlement_date;
      return {url:`${base}/trade-orders/${encodeURIComponent(edit.id)}/actions`,body};
    }
    if (s.mode === "pending") return {url:`${base}/trade-orders`,body:{fund_code:d.fund_code,transaction_type:d.transaction_type,
      submitted_at:`${d.submitted_at}:00+08:00`,channel:d.channel || "off_exchange",note:d.note || "",
      ...(d.transaction_type === "buy" ? {requested_amount:d.requested_amount} : {requested_units:d.requested_units})}};
    return {url:`${base}/transactions`,body:actual()};
  }
  async function submit(event,ctx) {
    event.preventDefault(); const s = session(ctx);
    if (s.busy || ctx.loading || ctx.error) return;
    capture(event,ctx);
    const cmd = command(ctx,s,draft(ctx,s)); cmd.body.user_id = ctx.userId;
    const fingerprint = JSON.stringify(cmd);
    // 先保存请求键，再发请求。超时重试复用相同键，不把未知结果误记为第二笔。
    if (s.retry?.fingerprint !== fingerprint) s.retry = {fingerprint,key:ctx.createKey()};
    cmd.body.idempotency_key = s.retry.key;
    const draftKey = formKey(s), nextList = s.editor ? (s.editor.kind === "order" ? "orders" : "transactions") : (s.mode === "pending" ? "orders" : "transactions");
    s.busy = true; s.error = ""; ctx.render();
    let saved = false;
    try {
      const result = await ctx.request(cmd.url,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(cmd.body)});
      saved = true; s.retry = null; delete s.drafts[draftKey]; s.editor = null; s.list = nextList;
      if (ctx.current()) { ctx.notify(result.idempotency_replayed ? "已保存，本次没有重复记账" : "记录已保存"); await ctx.refresh(); }
    } catch (error) { s.error = saved ? `记录已保存，但刷新失败：${error.message}` : error.message; }
    finally { s.busy = false; if (ctx.current()) ctx.render(); }
  }
  const api = {render,capture,change,click,submit,allowed,command,session};
  root.FundScopeBookkeeping = api;
  if (typeof module !== "undefined") module.exports = api;
})(typeof window === "undefined" ? globalThis : window);
