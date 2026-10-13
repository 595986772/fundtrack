/*
 * 组合、市场的功能页面目录。这里只解析导航，不读取数据或启动后台任务。
 * URL、导航按钮和内容面板共用同一组 ID，避免跳到了页面却高亮另一个功能。
 */
(function (root) {
  "use strict";
  const config = {
    overview: {
      title: "单基金研究",
      groups: [{label: "研究视图", pages: ["terminal", "holdings", "reports"]}],
      items: [
        {id: "terminal", label: "行情与研判", icon: "chart-spline"},
        {id: "holdings", label: "持仓分析", icon: "layers-3"},
        {id: "reports", label: "季度报告", icon: "book-open"},
      ],
    },
    portfolio: {
      title: "组合功能",
      groups: [
        { label: "资产与业绩", pages: ["holdings", "review", "performance", "transactions", "configuration"] },
        { label: "风险与配置", pages: ["risk", "stress", "optimizer"] },
        { label: "研究与跟踪", pages: ["events", "research", "briefings", "monitoring", "decisions"] },
      ],
      items: [
        { id: "holdings", label: "持仓交易", icon: "wallet-cards" },
        { id: "review", label: "收益与风险", icon: "chart-spline" },
        { id: "risk", label: "风险研判", icon: "shield-alert" },
        { id: "stress", label: "压力测试", icon: "activity" },
        { id: "optimizer", label: "约束求解", icon: "sliders-horizontal" },
        { id: "performance", label: "业绩", icon: "chart-spline" },
        { id: "events", label: "事件归因", icon: "newspaper" },
        { id: "monitoring", label: "持续监控", icon: "scan-search" },
        { id: "research", label: "深度研究", icon: "workflow" },
        { id: "briefings", label: "每日简报", icon: "calendar-clock" },
        { id: "decisions", label: "决策复盘", icon: "notebook-tabs" },
        { id: "transactions", label: "交易记录", icon: "receipt-text" },
        { id: "configuration", label: "补录实际持仓", icon: "pencil" },
      ],
    },
    market: {
      title: "市场功能",
      groups: [
        { label: "行情与资金", pages: ["radar", "status", "capital"] },
        { label: "资讯与主题", pages: ["news", "hotspots"] },
        { label: "情绪与社区", pages: ["retail", "community", "social-posts", "social-review", "social-settings"] },
      ],
      items: [
        { id: "radar", label: "板块观察", icon: "radar" },
        { id: "status", label: "市场状态", icon: "activity" },
        { id: "hotspots", label: "热点追踪", icon: "flame" },
        { id: "capital", label: "大资金", icon: "landmark" },
        { id: "retail", label: "情绪指数", icon: "gauge" },
        { id: "community", label: "社区风向", icon: "messages-square" },
        { id: "news", label: "市场事件", icon: "newspaper" },
        { id: "social-posts", label: "原始样本", icon: "files" },
        { id: "social-review", label: "观点复核", icon: "scan-text" },
        { id: "social-settings", label: "采集设置", icon: "settings-2" },
      ],
    },
  };

  // 分组顺序同时用于桌面导航、手机选择器和键盘浏览，不再维护三份功能列表。
  for (const view of Object.values(config)) {
    const byId = new Map(view.items.map(item => [item.id, item]));
    view.items = view.groups.flatMap(group => group.pages.map(id => byId.get(id)));
  }

  // 对外的五个产品入口，与内部数据拥有者分离。迁移界面不需要重写服务，
  // 也不会把同一份组合草稿或市场筛选复制到多个工作区。
  const destinations = {
    assets: {title: "我的资产", icon: "wallet-cards", pages: [
      ["holdings", "持仓", "portfolio", "holdings"],
      ["review", "收益与风险", "portfolio", "review"],
    ]},
    intelligence: {title: "事件情报", icon: "newspaper", pages: [
      ["events", "事件列表", "market", "news"],
      ["trends", "热度变化", "market", "hotspots"],
    ]},
    community: {title: "社区热点", icon: "messages-square", pages: [
      ["topics", "话题", "market", "community"],
      ["samples", "原始样本", "market", "social-posts"],
      ["review", "观点复核", "market", "social-review"],
    ]},
    sectors: {title: "板块雷达", icon: "radar", pages: [
      ["overview", "板块观察", "market", "radar"],
      ["market", "市场环境", "market", "status"],
    ]},
    research: {title: "AI 投研", icon: "sparkles", pages: [
      ["chat", "研究对话", "agent", null],
      ["reports", "研究报告", "portfolio", "research"],
      ["briefings", "每日简报", "portfolio", "briefings"],
    ]},
  };
  const auxiliary = [
    ["research", "records", "决策记录", "portfolio", "decisions"],
    ["assets", "performance", "业绩明细", "portfolio", "performance"],
    ["assets", "risk", "风险证据", "portfolio", "risk"],
    ["assets", "fund", "基金行情", "overview", "terminal"],
    ["assets", "fund/holdings", "基金持仓分析", "overview", "holdings"],
    ["assets", "fund/reports", "季度报告", "overview", "reports"],
    ["assets", "transactions", "交易记录", "portfolio", "transactions"],
    ["assets", "configuration", "补录实际持仓", "portfolio", "configuration"],
    ["assets", "stress", "压力测试", "portfolio", "stress"],
    ["assets", "optimizer", "调整配置", "portfolio", "optimizer"],
    ["assets", "monitoring", "风险跟踪", "portfolio", "monitoring"],
    ["assets", "events", "持仓事件关联", "portfolio", "events"],
    ["community", "settings", "采集设置", "market", "social-settings"],
    ["sectors", "capital", "资金证据", "market", "capital"],
    ["sectors", "sentiment", "情绪参考", "market", "retail"],
  ];
  const routes = [
    ...Object.entries(destinations).flatMap(([destination, value]) => value.pages.map(
      ([path, label, view, page]) => ({destination, path, label, view, page, primary: true}))),
    ...auxiliary.map(([destination, path, label, view, page]) => ({destination, path, label, view, page, primary: false})),
  ];
  function describe(view, page) {
    return routes.find(item => item.view === view && item.page === resolvePage(view, page)) || routes[0];
  }
  function destinationRoute(destination, path) {
    const item = routes.find(item => item.destination === destination && item.path === path)
      || routes.find(item => item.destination === destination);
    return item ? {view: item.view, page: item.page} : null;
  }

  function resolvePage(view, page) {
    // 旧组合穿透链接回到组合风险页，不能悄悄把分析对象换成另一只基金。
    if (view === "portfolio" && page === "exposure") return "risk";
    const items = config[view]?.items || [];
    return items.find((item) => item.id === page)?.id || (view === "market" ? "status" : items[0]?.id) || null;
  }

  function readRoute(hash) {
    // 只接受已声明的工作区；错误的子页面回到该工作区首页，不拼接任意选择器。
    const [view, ...parts] = String(hash || "").split("?")[0].replace(/^#\/?/, "").split("/");
    const page = parts.join("/");
    if (destinations[view]) return destinationRoute(view, page);
    if (!["overview", "portfolio", "market", "agent"].includes(view)) return null;
    return { view, page: resolvePage(view, page) };
  }

  function routeHash(view, page) {
    const target = destinations[view] ? destinationRoute(view, page) : {view, page};
    const item = describe(target.view, target.page);
    return `#${item.destination}/${item.path}`;
  }

  const api = { config, destinations, routes, describe, destinationRoute, resolvePage, readRoute, routeHash };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.FundScopeWorkspacePages = api;
})(typeof window === "undefined" ? globalThis : window);
