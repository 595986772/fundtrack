/* 任务授权只提供查询与撤销；创建授权始终由后端研究任务决定。 */
(function (root) {
  "use strict";

  const endpoint = "/api/v1/agent/authorizations";
  const capabilities = Object.freeze({
    "portfolio:read": "读取组合",
    "portfolio:research-run": "运行组合研究",
    "fund:read": "读取基金",
    "market:read": "读取市场",
    "knowledge:read": "检索知识",
    "calculator:use": "使用计算器",
    "tools:discover": "发现研究工具",
  });
  const reasons = Object.freeze({
    allowed: "校验通过",
    grant_inactive: "任务或父任务授权未生效",
    grant_expired: "任务授权已到期",
    action_denied: "操作未获授权",
    resource_denied: "组合不在授权范围内",
    authorization_unavailable: "授权校验暂不可用",
    tool_not_granted: "工具未获授权",
    user_mismatch: "用户身份不一致",
    resource_binding_mismatch: "资源绑定不一致",
  });
  const esc = value => String(value ?? "").replace(/[&<>"']/g, char => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[char]));
  const label = (labels, value) => Object.hasOwn(labels, value) ? labels[value] : String(value ?? "未记录");
  const items = value => Array.isArray(value) ? value : [];
  const icon = name => `<i data-lucide="${name}" aria-hidden="true"></i>`;
  const date = value => {
    if (!value) return null;
    const parsed = new Date(/Z$|[+-]\d\d:\d\d$/i.test(value) ? value : `${value}Z`);
    return Number.isNaN(parsed.valueOf()) ? null : parsed;
  };
  const timestamp = value => {
    const parsed = date(value);
    return parsed ? parsed.toLocaleString("zh-CN", {
      timeZone: "Asia/Shanghai", year: "numeric", month: "2-digit", day: "2-digit",
      hour: "2-digit", minute: "2-digit", hour12: false,
    }) : "未记录";
  };

  function status(grant) {
    if (grant.status === "revoked") return { text: "已撤销", tone: "muted" };
    if (grant.status === "expired" || (grant.status === "active" && date(grant.expires_at)?.valueOf() <= Date.now())) {
      return { text: "已到期", tone: "muted" };
    }
    if (grant.status === "pending") return { text: "待生效", tone: "warning" };
    if (grant.status === "failed") return { text: "授权失败", tone: "danger" };
    // 子授权记录仍可能是 active；只有服务端 effective 能说明整条授权链有效。
    if (grant.status === "active" && grant.effective === true) return { text: "有效", tone: "active" };
    if (grant.effective === false) return { text: "未生效", tone: "warning" };
    return { text: "有效性待核验", tone: "warning" };
  }

  function create({ dialog, trigger, request, getContext, icons = () => {} }) {
    const document = dialog.ownerDocument;
    const content = dialog.querySelector("[data-auth-content]");
    const message = dialog.querySelector("[data-auth-message]");
    const refresh = dialog.querySelector("[data-auth-refresh]");
    const closeButton = dialog.querySelector("[data-auth-close]");
    let userId = null, session = 0, revision = 0, data = null, loading = false;
    let error = "", notice = "", confirmation = null, confirmationError = "", listPromise = null;
    const expanded = new Set(), audits = new Map(), reads = new Set(), writes = new Map();
    const currentUser = () => String(getContext().userId || "");
    const ticket = () => ({ userId, session, revision });
    const sameSession = token => dialog.open && token.userId === currentUser()
      && token.userId === userId && token.session === session;
    const current = token => sameSession(token) && token.revision === revision;
    const writeKey = id => JSON.stringify([userId, id]);
    const writing = () => Array.from(writes.values()).some(token => token.userId === userId);
    const grants = () => items(data?.grants);
    const find = id => grants().find(grant => grant.id === id);
    const portfolioName = id => items(getContext().portfolios).find(portfolio => portfolio.id === id)?.name || `组合 ${id}`;
    const portfolioNames = grant => items(grant.portfolio_ids).map(portfolioName).join("、") || "未绑定组合";
    const scope = grant => items(grant.capabilities).map(value => label(capabilities, value)).join("、") || "未授予操作权限";
    const canRevoke = grant => data?.enabled === true && ["active", "pending"].includes(grant.status)
      && !(date(grant.expires_at)?.valueOf() <= Date.now());

    function abortReads() {
      reads.forEach(controller => controller.abort());
      reads.clear();
    }

    async function read(url) {
      const controller = new AbortController();
      reads.add(controller);
      try { return await request(url, { signal: controller.signal }); }
      finally { reads.delete(controller); }
    }

    function focus(key) {
      const target = Array.from(dialog.querySelectorAll("[data-auth-focus]")).find(node => node.dataset.authFocus === key);
      (target && !target.disabled ? target : closeButton).focus();
    }

    function renderAudit(grant, index) {
      const entry = audits.get(grant.id);
      const detail = entry?.detail || grant;
      const metadata = `<dl class="agent-auth-metadata">
        <dt>授权</dt><dd>${esc(grant.id)}</dd>
        <dt>任务</dt><dd>${esc(grant.run_id || "未记录")}</dd>
        <dt>Agent</dt><dd>${esc(grant.agent_id || "未记录")}</dd>
        <dt>来源</dt><dd>${esc(grant.source || "未记录")}</dd>
        ${grant.parent_grant_id ? `<dt>父授权</dt><dd>${esc(grant.parent_grant_id)}</dd>` : ""}
        <dt>创建</dt><dd>${esc(timestamp(grant.created_at))}</dd>
        ${detail.revoked_at ? `<dt>撤销</dt><dd>${esc(timestamp(detail.revoked_at))}</dd><dt>原因</dt><dd>${esc(detail.revoke_reason || "未记录")}</dd>` : ""}
      </dl>`;
      if (!entry || entry.loading) return `${metadata}<p class="agent-auth-state" role="status">正在读取审计...</p>`;
      if (entry.error) return `${metadata}<p class="agent-auth-error" role="alert">审计读取失败：${esc(entry.error)}</p>
        <button type="button" class="action-button" data-auth-action="audit-retry" data-grant-index="${index}" data-auth-focus="audit-retry-${index}">${icon("refresh-cw")}重试审计</button>`;
      const events = items(detail.audit);
      if (!events.length) return `${metadata}<p class="agent-auth-state">暂无审计记录</p>`;
      return `${metadata}<ol class="agent-auth-audit-list">${events.map(event => {
        const action = label({ ...capabilities, "grant:create": "创建授权", "grant:revoke": "撤销授权" }, event.action);
        const resource = event.resource_id === "public" ? "公开数据" : (event.resource_id
          ? String(event.resource_id).split(",").map(id => items(getContext().portfolios).find(portfolio => portfolio.id === id)?.name || id).join("、") : "未指定资源");
        return `<li><div class="agent-auth-audit-heading"><span>${esc(action)}</span>
          <span class="agent-auth-status" data-tone="${event.allowed === false ? "danger" : "muted"}">${event.allowed === true ? "允许" : event.allowed === false ? "拒绝" : "结果未记录"}</span></div>
          <p>${esc(resource)} · ${esc(label(reasons, event.reason))}</p><time>${esc(timestamp(event.created_at))}</time></li>`;
      }).join("")}</ol>`;
    }

    function renderGrant(grant, index) {
      const state = status(grant), open = expanded.has(grant.id);
      return `<li class="agent-auth-grant">
        <div class="agent-auth-grant-heading"><h3>${esc(portfolioNames(grant))}</h3>
          <span class="agent-auth-status" data-tone="${state.tone}">${state.text}</span></div>
        <p class="agent-auth-scope">范围：${esc(scope(grant))}</p>
        <div class="agent-auth-row"><span>到期：${esc(timestamp(grant.expires_at))}</span>
          <div class="agent-auth-row-actions">
            <button type="button" class="agent-auth-disclosure" data-auth-action="audit" data-grant-index="${index}"
              data-auth-focus="audit-${index}" aria-expanded="${open}" aria-controls="agent-auth-audit-${index}">${icon(open ? "chevron-up" : "chevron-down")}只读审计</button>
            ${canRevoke(grant) ? `<button type="button" class="action-button agent-auth-revoke" data-auth-action="revoke" data-grant-index="${index}"
              data-auth-focus="revoke-${index}" aria-label="撤销${esc(portfolioNames(grant))}的任务授权" ${loading || writing() ? "disabled" : ""}>${icon("shield-off")}撤销</button>` : ""}
          </div>
        </div>
        ${grant.inactive_reason ? `<p class="agent-auth-inactive">${esc(label(reasons, grant.inactive_reason))}</p>` : ""}
        <section id="agent-auth-audit-${index}" class="agent-auth-audit" aria-label="${esc(portfolioNames(grant))}的只读审计" ${open ? "" : "hidden"}>${open ? renderAudit(grant, index) : ""}</section>
      </li>`;
    }

    function render(focusKey) {
      if (!dialog.open || userId !== currentUser()) return;
      const previous = document.activeElement?.dataset.authFocus;
      refresh.disabled = loading || writing() || Boolean(confirmation);
      content.setAttribute("aria-busy", String(loading));
      message.textContent = notice || (loading ? "正在读取任务授权..." : "");
      const grant = find(confirmation);
      dialog.setAttribute("aria-describedby", grant ? "agent-auth-revoke-warning" : "agent-authorizations-description");
      if (grant) {
        content.innerHTML = `<section class="agent-auth-confirm" aria-labelledby="agent-auth-confirm-title">
          <h3 id="agent-auth-confirm-title">确认撤销任务授权？</h3>
          <p>${esc(portfolioNames(grant))}</p><p class="agent-auth-scope">范围：${esc(scope(grant))}</p>
          <p class="agent-auth-task">任务：${esc(grant.run_id || grant.id)}</p>
          <p id="agent-auth-revoke-warning" class="agent-auth-warning">撤销将停止此任务及其子任务在授权范围内的后续访问，不会撤回既有回答或交易。</p>
          ${confirmationError ? `<p class="agent-auth-error" role="alert">${esc(confirmationError)}</p>` : ""}
          <div class="agent-auth-confirm-actions">
            <button type="button" class="action-button" data-auth-action="cancel" data-auth-focus="cancel">${icon("x")}取消</button>
            <button type="button" class="action-button agent-auth-danger" data-auth-action="confirm" data-auth-focus="confirm" ${writing() ? "disabled" : ""}>${icon("shield-off")}${writing() ? "正在撤销..." : "确认撤销"}</button>
          </div></section>`;
      } else {
        content.innerHTML = `${data?.enabled === false ? '<p class="agent-auth-warning">任务授权校验未启用，以下仅为历史记录。</p>' : ""}
          ${data?.strict_identity === false ? '<p class="agent-auth-warning">本地兼容模式：用户 ID 不是已验证的登录身份。</p>' : ""}
          ${error ? `<p class="agent-auth-error" role="alert">任务授权读取失败：${esc(error)}。请重试。</p>
            <button type="button" class="action-button" data-auth-action="retry" data-auth-focus="retry">${icon("refresh-cw")}重试读取</button>` : ""}
          ${data ? `<p class="agent-auth-count">最近 ${grants().length} 条</p>${grants().length
            ? `<ul class="agent-auth-list">${grants().map(renderGrant).join("")}</ul>`
            : '<div class="agent-auth-state"><h3>暂无任务授权</h3><p>当前用户还没有任务授权记录。</p></div>'}`
            : loading ? '<div class="agent-auth-skeleton" aria-hidden="true"><span></span><span></span><span></span></div>' : ""}`;
      }
      icons();
      if (focusKey || (previous && content.contains(document.activeElement) === false)) focus(focusKey || previous);
    }

    async function load(force = false) {
      if (!dialog.open || userId !== currentUser()) return;
      if (loading && !force) return listPromise;
      abortReads();
      revision += 1;
      const token = ticket();
      loading = true;
      error = "";
      expanded.clear();
      audits.clear();
      render();
      listPromise = (async () => {
        try {
          const result = await read(`${endpoint}?${new URLSearchParams({ user_id: token.userId })}`);
          if (!current(token)) return;
          if (!result || typeof result.enabled !== "boolean" || typeof result.strict_identity !== "boolean" || !Array.isArray(result.grants)
            || result.grants.some(grant => !grant || typeof grant.id !== "string" || !grant.id)) {
            throw new Error("服务返回了无效的授权列表");
          }
          data = { ...result, grants: [...result.grants].sort((a, b) => (date(b.created_at)?.valueOf() || 0) - (date(a.created_at)?.valueOf() || 0)) };
        } catch (failure) {
          if (current(token)) error = failure.message || "服务暂不可用";
        } finally {
          if (current(token)) { loading = false; listPromise = null; render(); }
        }
      })();
      return listPromise;
    }

    async function loadAudit(grant) {
      if (audits.get(grant.id)?.loading) return;
      const token = ticket();
      audits.set(grant.id, { loading: true });
      render();
      try {
        const detail = await read(`${endpoint}/${encodeURIComponent(grant.id)}?${new URLSearchParams({ user_id: token.userId })}`);
        if (!current(token)) return;
        if (detail?.id !== grant.id || !Array.isArray(detail.audit)
          || detail.audit.some(event => !event || typeof event !== "object" || Array.isArray(event))) {
          throw new Error("服务返回了无效的审计记录");
        }
        // 详情接口可能不带 effective，不用它覆盖列表中的授权链状态。
        audits.set(grant.id, { detail });
      } catch (failure) {
        if (current(token)) audits.set(grant.id, { error: failure.message || "服务暂不可用" });
      } finally {
        if (current(token)) render();
      }
    }

    function cancelConfirmation() {
      const index = grants().findIndex(grant => grant.id === confirmation);
      confirmation = null;
      confirmationError = "";
      render(`revoke-${index}`);
    }

    async function revoke() {
      const grant = find(confirmation);
      if (!grant || writing() || loading) return;
      if (!canRevoke(grant)) {
        confirmationError = "授权已到期或状态已变化，请取消后刷新核对。";
        render("cancel");
        return;
      }
      const token = ticket(), key = writeKey(grant.id);
      writes.set(key, token);
      confirmationError = "";
      render("cancel");
      try {
        const result = await request(`${endpoint}/${encodeURIComponent(grant.id)}/revoke`, {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ user_id: token.userId, reason: "用户撤销" }),
        });
        if (result?.id !== grant.id || result.status !== "revoked") throw new Error("未收到撤销确认，请重新读取后核对状态");
        if (!sameSession(token)) return;
        // 先使可见后代失效，再重新读取服务端列表，避免审计旧回包恢复已撤销状态。
        const revoked = new Set([grant.id]);
        for (let size = -1; size !== revoked.size;) {
          size = revoked.size;
          grants().forEach(item => { if (revoked.has(item.parent_grant_id)) revoked.add(item.id); });
        }
        data.grants = grants().map(item => item.id === grant.id
          ? { ...item, ...result, effective: false, inactive_reason: "用户已撤销授权" }
          : revoked.has(item.id) ? { ...item, effective: false, inactive_reason: "父任务授权已撤销" } : item);
        confirmation = null;
        notice = "授权已撤销，后续访问已停止；既有回答或交易不会撤回。";
      } catch (failure) {
        if (sameSession(token)) {
          confirmationError = `撤销未确认：${failure.message || "服务暂不可用"}。可取消后刷新核对。`;
          if (!confirmation) notice = confirmationError;
        }
      } finally {
        writes.delete(key);
        if (sameSession(token)) {
          render(confirmation ? "cancel" : "close");
          if (!confirmation) await load(true);
        } else if (dialog.open && userId === token.userId && currentUser() === token.userId) {
          // 关闭后重开不消费旧写回执，只重新 GET；写锁在整个请求期间保留。
          await load(true);
        }
      }
    }

    function clear() {
      session += 1;
      abortReads();
      data = null;
      loading = false;
      listPromise = null;
      error = notice = confirmationError = "";
      confirmation = null;
      expanded.clear();
      audits.clear();
      content.replaceChildren();
      content.setAttribute("aria-busy", "false");
      message.textContent = "";
      dialog.setAttribute("aria-describedby", "agent-authorizations-description");
      trigger.setAttribute("aria-expanded", "false");
    }

    function close() {
      clear();
      if (dialog.open) dialog.close();
      trigger.focus();
    }

    function reset() {
      const wasOpen = dialog.open;
      clear();
      userId = null;
      if (wasOpen) { dialog.close(); trigger.focus(); }
    }

    function open() {
      if (userId !== currentUser()) reset();
      if (dialog.open) return listPromise;
      clear();
      userId = currentUser();
      dialog.showModal();
      trigger.setAttribute("aria-expanded", "true");
      closeButton.focus();
      render();
      return load();
    }

    trigger.addEventListener("click", open);
    closeButton.addEventListener("click", close);
    refresh.addEventListener("click", () => { notice = ""; load(); });
    dialog.addEventListener("cancel", event => {
      event.preventDefault();
      if (confirmation && !writing()) cancelConfirmation();
      else close();
    });
    dialog.addEventListener("close", () => {
      // close 事件异步派发，旧事件不能清空刚重开的弹窗。
      if (!dialog.open) clear();
    });
    content.addEventListener("click", event => {
      const button = event.target.closest("[data-auth-action]");
      if (!button || button.disabled || !dialog.open) return;
      if (userId !== currentUser()) { reset(); return; }
      const action = button.dataset.authAction;
      if (action === "retry") { load(); return; }
      if (action === "cancel") { cancelConfirmation(); return; }
      if (action === "confirm") { revoke(); return; }
      const grant = grants()[Number(button.dataset.grantIndex)];
      if (!grant) return;
      if (action === "audit") {
        if (expanded.has(grant.id)) expanded.delete(grant.id);
        else expanded.add(grant.id);
        render(`audit-${button.dataset.grantIndex}`);
        if (expanded.has(grant.id) && !audits.has(grant.id)) loadAudit(grant);
      } else if (action === "audit-retry") loadAudit(grant);
      else if (action === "revoke" && canRevoke(grant) && !loading && !writing()) {
        confirmation = grant.id;
        notice = "";
        confirmationError = "";
        render("cancel");
      }
    });

    return { open, close, reset };
  }

  const api = { create };
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.FundScopeAgentAuthorizations = api;
})(typeof window === "undefined" ? globalThis : window);
