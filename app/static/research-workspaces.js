/*
 * 列表 -> 详情 -> AI 草稿的页面流程。数据通过注入的只读请求取得，
 * 不引用 app.js 的内部变量，不启动采集、模型或交易。板块快照单独保存，
 * 避免被事件页的搜索条件或“当前基金”筛选污染。
 */
(function (root) {
  "use strict";
  function create({getState, request, endpoints, icons, ask, openFund, lists, labels, renderHotspot}) {
    let radarData = null;
    let radarKey = "";
    let radarRequest = 0;
    let radarSort = "flow";
    let radarQuery = "";
    let selectedDetail = null;
    const content = document.querySelector(".fund-content");
    const detail = document.querySelector("#research-detail-page");
    const e = lists.escape;

    function renderRadar() {
      const host = document.querySelector("#sector-radar-content");
      if (!radarData) return;
      host.innerHTML = `<div class="product-toolbar"><label>板块 <input type="search" data-radar-search value="${e(radarQuery)}" placeholder="搜索板块" /></label>
        <label>排序 <select data-radar-sort><option value="events">相关热点数</option><option value="flow">近5日净申购</option><option value="name">板块名称</option></select></label></div>
        ${radarData.errors.map(message => `<p class="product-warning" role="status">${e(message)}</p>`).join("")}
        <p class="product-provenance">资金截至 ${e(radarData.capital?.data_date || "未知")} · 热点截至 ${e(radarData.hotspots?.data_cutoff || "未知")}</p>
        <div data-radar-table>${lists.radar(rows(), {sort: radarSort, query: radarQuery})}</div>`;
      host.querySelector("[data-radar-sort]").value = radarSort;
      icons();
    }
    function rows() { return lists.sectorRows(radarData?.capital, radarData?.hotspots, labels); }
    async function loadRadar() {
      const state = getState();
      const key = `${state.userId}:${state.selectedPortfolioId || ""}`;
      const version = ++radarRequest;
      if (key !== radarKey) radarData = null;
      radarKey = key;
      const host = document.querySelector("#sector-radar-content");
      host.setAttribute("aria-busy", "true");
      if (!radarData) host.innerHTML = '<p class="product-empty" role="status">正在读取板块证据...</p>';
      // 两个来源独立失败；一份暂不可用不隐藏另一份已经取得的数据。
      const params = new URLSearchParams({user_id: state.userId});
      if (state.selectedPortfolioId) params.set("portfolio_id", state.selectedPortfolioId);
      const results = await Promise.allSettled([
        request(`${endpoints.largeCapital}?${params}`),
        request(`${endpoints.marketHotspots}?history_days=30&limit=30`),
      ]);
      if (version !== radarRequest) return;
      radarData = {
        capital: results[0].status === "fulfilled" ? results[0].value : null,
        hotspots: results[1].status === "fulfilled" ? results[1].value : null,
        errors: results.flatMap((result, index) => result.status === "rejected" ? [`${index ? "热点" : "资金"}读取失败：${result.reason.message}`] : []),
      };
      host.setAttribute("aria-busy", "false");
      renderRadar();
      restore();
    }

    function findDetail(kind, id) {
      const state = getState();
      if (kind === "event") {
        const events = state.marketEvents?.events || [];
        const item = events.find(row => row.event_id === id);
        return item ? {...item, cluster_members: item.cluster ? events.filter(row => row.cluster?.cluster_id === item.cluster.cluster_id) : [item]} : null;
      }
      if (kind === "topic") return state.socialSignals?.aggregates?.find(row => row.topic_key === id);
      if (kind === "sector") return rows().find(row => row.code === id);
      if (kind === "hotspot") return state.marketHotspots?.hotspots?.find(row => row.cluster_id === id);
      return null;
    }
    function showDetail(kind, id, {push = true} = {}) {
      if (!["event", "topic", "sector", "hotspot"].includes(kind)) return;
      const state = getState();
      const base = window.location.hash.split("?")[0];
      const item = findDetail(kind, id);
      selectedDetail = {kind, id, item};
      state.researchDetail = {kind, id};
      if (push) {
        state.workspaceScrollPositions[`${state.activeView}/${state.workspacePages[state.activeView] || ""}`] = content.scrollTop;
        window.history.pushState(null, "", `${base}?${new URLSearchParams({detail: kind, id})}`);
      }
      document.querySelectorAll(".fund-content > .workspace-view").forEach(panel => {panel.classList.add("view-hidden");});
      detail.hidden = false;
      const title = item?.title || item?.topic_label || item?.label || "研究详情";
      let body = item ? kind === "event" ? lists.eventDetail(item) : kind === "topic" ? lists.topicDetail(item) : kind === "sector" ? lists.sectorDetail(item)
        : `<ol class="market-hotspot-list">${renderHotspot(item)}</ol>`
        : '<p class="product-empty" role="status">该条记录不在当前已载入的结果中。请返回列表重新选择，或重新读取数据。</p>';
      detail.innerHTML = `<header class="product-detail-header"><a href="${e(base)}"><i data-lucide="arrow-left"></i>返回列表</a>${item ? '<button class="secondary-action compact" type="button" data-detail-ask><i data-lucide="sparkles"></i>交给 AI 分析</button>' : ""}</header><article>${body}</article>`;
      if (push) { content.scrollTop = 0; detail.focus({preventScroll: true}); }
      document.querySelector("#workspace-title").textContent = title;
      icons();
    }
    function restore() {
      const query = new URLSearchParams(window.location.hash.split("?")[1] || "");
      const kind = query.get("detail"), id = query.get("id");
      const owner = {event: "news", topic: "community", sector: "radar", hotspot: "hotspots"}[kind];
      if (kind && id && getState().activeView === "market" && getState().workspacePages.market === owner) showDetail(kind, id, {push: false});
      else selectedDetail = null;
    }
    function askDetail() {
      const value = selectedDetail;
      if (!value?.item) return;
      const item = value.item;
      const title = item.title || item.topic_label || item.label;
      const identity = value.kind === "event" ? `事件 ID ${item.event_id}，日期 ${item.event_date}`
        : value.kind === "sector" ? `资金证据 ${item.signal?.evidence_id || "未取得"}，日期 ${item.signal?.data_date || "未知"}`
        : value.kind === "topic" ? `话题 ${item.topic_key}，统计窗口 ${getState().socialSignals?.operation_window?.target_trade_date || "未知"}`
        : `事件簇 ${item.cluster_id}，日期 ${item.event_end}`;
      // 仅建立草稿；用户确认发送后才调用现有 Agent。引用文本视为证据，不是指令。
      ask(`请研究“${title}”对当前配置的影响和前景。页面线索：${identity}。请读取现有证据，区分事实、社区观点与推断，说明机会、反对理由和待验证条件。未取得的数据明确说明，不自动采集。`, "market");
    }
    function bind() {
      content.addEventListener("click", event => {
        const opener = event.target.closest("[data-research-detail]");
        if (opener) showDetail(opener.dataset.researchDetail, opener.dataset.detailId);
        if (event.target.closest("[data-detail-ask]")) askDetail();
        const fund = event.target.closest("[data-research-fund]");
        if (fund) openFund(fund.dataset.researchFund);
      });
      content.addEventListener("change", event => {
        if (event.target.matches("[data-radar-sort]")) {
          radarSort = event.target.value;
          document.querySelector("[data-radar-table]").innerHTML = lists.radar(rows(), {sort: radarSort, query: radarQuery}); icons();
        }
      });
      content.addEventListener("input", event => {
        if (event.target.matches("[data-radar-search]")) {
          radarQuery = event.target.value.trim();
          document.querySelector("[data-radar-table]").innerHTML = lists.radar(rows(), {sort: radarSort, query: radarQuery}); icons();
        }
      });
    }
    return {bind, loadRadar, restore};
  }
  root.FundScopeResearchWorkspaces = {create};
})(window);
