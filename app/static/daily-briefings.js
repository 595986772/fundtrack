/* 每日简报独立页面：读取状态不触发采集。只有开关和“运行今日简报”会发写请求。 */
(function (root) {
  "use strict";
  const esc = value => String(value ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;", "<":"&lt;", ">":"&gt;", '"':"&quot;", "'":"&#39;"}[c]));
  const timestamp = value => {
    if (!value) return "未记录";
    const parsed = new Date(/Z$|[+-]\d\d:\d\d$/.test(value) ? value : `${value}Z`);
    return Number.isNaN(parsed.valueOf()) ? "时间待核验" : new Intl.DateTimeFormat("zh-CN", {
      timeZone:"Asia/Shanghai", month:"2-digit", day:"2-digit", hour:"2-digit", minute:"2-digit", hour12:false,
    }).format(parsed);
  };
  const labels = {queued:"排队中", running:"生成中", completed:"已完成", failed:"未完成", cancelled:"已取消"};
  const sourceLabels = {ready:"可用", partial:"部分可用", failed:"不可用"};

  function create({context, request, changed, visible = () => true, schedule = setTimeout, unschedule = clearTimeout}) {
    let key = "", sequence = 0, data = null, error = "", notice = "", loading = false, sending = false, timer = null;
    const identify = c => `${c.user}:${c.portfolio}`;
    function pause() { if (timer !== null) unschedule(timer); timer = null; }
    function current() {
      const c = context(), next = identify(c);
      if (next !== key) {
        pause(); key = next; sequence++; data = null; error = notice = ""; loading = sending = false;
      }
      return c;
    }
    const url = c => `/api/v1/portfolios/${encodeURIComponent(c.portfolio)}/daily-briefing`;
    const query = c => `user_id=${encodeURIComponent(c.user)}`;
    const active = (c, version) => key === identify(c) && identify(context()) === key && version === sequence;
    function poll() {
      pause();
      if (!visible() || !data) return;
      // 有任务时短轮询；开启定时时慢轮询，打开页面可以看到后台新建的任务。
      const pending = data.runs.some(run => ["queued", "running"].includes(run.status));
      if (pending || data.policy.enabled) timer = schedule(() => { timer = null; if (visible()) load(); }, pending ? 5000 : 60000);
    }
    async function read(c, version) {
      const response = await request(`${url(c)}?${query(c)}`);
      if (active(c, version)) { data = response; error = ""; }
    }
    async function load() {
      const c = current();
      if (!c.portfolio || loading || sending) return;
      pause(); const version = ++sequence; loading = true; changed();
      try { await read(c, version); }
      catch (e) { if (active(c, version)) error = e.message || "读取失败，请重试。"; }
      finally { if (active(c, version)) { loading = false; changed(); if (!error) poll(); } }
    }
    async function act(kind, enabled) {
      const c = current();
      if (!c.portfolio || sending) return;
      pause(); const version = ++sequence; sending = true; loading = false; error = notice = ""; changed();
      try {
        if (kind === "configure") {
          // 字符串请求体必须声明 JSON，否则浏览器默认按 text/plain 发送，后端会拒绝。
          const response = await request(url(c), {method:"PUT", headers:{"Content-Type":"application/json"},
            body:JSON.stringify({user_id:c.user, enabled})});
          if (active(c, version)) { data = response; notice = enabled ? "已开启交易日自动简报" : "已关闭自动简报，已有任务不受影响"; }
        } else {
          const response = await request(`${url(c)}/runs?${query(c)}`, {method:"POST"});
          if (active(c, version)) notice = response.accepted === false ? "今日任务已存在，未重复创建或收费" : "今日简报已加入队列";
          await read(c, version);
        }
      } catch (e) { if (active(c, version)) error = e.message || "操作未成功，请重试。"; }
      finally { if (active(c, version)) { sending = false; changed(); if (!error) poll(); } }
    }

    function render() {
      current();
      const busy = sending ? " disabled" : "";
      const runs = data?.runs || [];
      return `<div class="briefing-page" aria-busy="${loading || sending}">
        <header class="briefing-heading"><div><h2>每日简报</h2><p>A 股交易日 <strong>14:30</strong> · 北京时间</p></div>
          <div class="briefing-actions"><button type="button" class="icon-button" data-briefing-refresh title="读取任务状态" aria-label="读取任务状态"${loading || sending ? " disabled" : ""}><i data-lucide="refresh-cw"></i></button>
            <button type="button" class="primary-action compact" data-briefing-run${!data || sending ? " disabled" : ""}><i data-lucide="play"></i>运行今日简报</button></div></header>
        <div class="briefing-schedule"><label class="briefing-switch"><input type="checkbox" role="switch" data-briefing-enabled${data?.policy.enabled ? " checked" : ""}${!data ? " disabled" : busy}><span>自动生成</span></label>
          <span>${data?.policy.enabled ? `下次启动 ${timestamp(data.next_run_at)}` : "尚未开启"}</span></div>
        <p class="briefing-caption">开启后将自动刷新公开数据并调用模型，计入当前账号额度。电脑需保持唤醒、联网，服务保持运行。</p>
        ${data?.calendar_status === "unavailable" ? '<p class="briefing-warning">交易日历不可用，自动任务将跳过。请先更新服务的交易日历。</p>' : ""}
        ${data?.policy.last_error ? `<p class="briefing-warning">${esc(data.policy.last_error)}</p>` : ""}
        ${error ? `<p role="alert" class="briefing-warning">${esc(error)}</p>` : ""}
        ${notice ? `<p role="status" class="briefing-notice">${esc(notice)}</p>` : ""}
        <section class="briefing-history" aria-label="简报记录"><div class="briefing-history-heading"><h3>简报记录</h3><a href="#research/records">决策记录<i data-lucide="arrow-up-right"></i></a></div>
        ${!runs.length ? `<p class="briefing-empty">${loading ? "正在读取简报记录…" : "暂无简报"}</p>` : runs.map(renderRun).join("")}</section>
        <details class="briefing-boundaries"><summary>时间与数据口径</summary><p>自动启动窗口为 14:30 至 14:40，错过不在晚间补跑。手动运行与自动运行共用当天任务，每个组合每天只建一份。</p><p>基金涨跌来自最近已披露净值，并非当天盘中估值；指数行情单独标注时间。关键数据缺失时不生成 AI 结论，小红书不参与自动采集。</p></details>
      </div>`;
    }
    function renderRun(run) {
      const done = run.status === "completed";
      return `<article class="briefing-run"><header><div><time>${esc(run.trade_date)}</time><span class="briefing-status" data-status="${esc(run.status)}">${esc(labels[run.status] || "状态待核验")}${done ? (run.model_used ? " · AI" : " · 规则回退") : ""}</span></div>
        ${done ? `<button type="button" class="secondary-action compact" data-review-report="${esc(run.job_id)}"><i data-lucide="file-text"></i>查看与追问</button>` : ""}</header>
        ${run.headline ? `<h4>${esc(run.headline)}</h4>` : ""}
        <p>${esc(done ? run.summary : run.progress_message || "等待执行")}</p>
        <div class="briefing-run-meta"><span>${run.trigger === "scheduled" ? "定时触发" : "手动触发"}</span><span>创建 ${timestamp(run.created_at)}</span>${run.captured_at ? `<span>数据截面 ${timestamp(run.captured_at)}</span>` : ""}</div>
        ${run.error ? `<p class="briefing-warning">${esc(run.error)}</p>` : ""}
        ${run.sources?.length || run.warnings?.length ? `<details class="briefing-sources"><summary>数据状态${run.gate?.status === "blocked" ? " · 未通过检查" : ""}</summary>
          <ul>${(run.sources || []).map(source => `<li><span>${esc(source.label)}</span><strong data-source-status="${esc(source.status)}">${esc(sourceLabels[source.status] || "待核验")}</strong><time>${timestamp(source.finished_at)}</time></li>`).join("")}</ul>
          ${(run.warnings || []).map(warning => `<p>${esc(warning)}</p>`).join("")}</details>` : ""}</article>`;
    }
    return {load, render, pause, configure:enabled => act("configure", enabled), run:() => act("run")};
  }
  const api = {create};
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.FundScopeDailyBriefings = api;
})(typeof window === "undefined" ? globalThis : window);
