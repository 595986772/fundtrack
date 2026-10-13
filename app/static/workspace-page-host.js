/*
 * 功能页面的 DOM 生命周期，不包含基金计算、请求或聊天逻辑。
 * 每页第一次访问才渲染；离开时保留 DOM。某页数据更新只标记该页，
 * 不会重建其他页的表单、文本选区、展开状态或图表。
 */
(function (root) {
  "use strict";

  function create({ element, pages, render, beforeReset = () => {}, beforeRender = () => {}, afterRender = () => {} }) {
    let context;
    let headerHtml;
    const panels = new Map();
    const markup = new Map();
    const dirty = new Set();

    function update({ key, activePage, header, changedPages = [], visible = true }) {
      // 换用户、换组合或主动新建草稿才拆除页面树，避免旧输入混入新对象。
      if (key !== context) {
        beforeReset();
        context = key;
        headerHtml = undefined;
        panels.clear();
        markup.clear();
        dirty.clear();
        element.innerHTML = '<div data-workspace-header></div><div data-workspace-pages></div>';
        for (const page of pages) dirty.add(page.id);
      }
      if (headerHtml !== header) {
        element.querySelector("[data-workspace-header]").innerHTML = header;
        headerHtml = header;
      }
      for (const id of changedPages === null ? pages.map(page => page.id) : changedPages) dirty.add(id);
      for (const [id, panel] of panels) panel.hidden = id !== activePage;
      if (!visible) return;

      const page = pages.find(item => item.id === activePage);
      if (!page) return;
      let panel = panels.get(page.id);
      if (!panel) {
        panel = element.ownerDocument.createElement("section");
        panel.id = `portfolio-page-${page.id}`;
        panel.className = "portfolio-function-page";
        panel.dataset.workspacePage = page.id;
        panel.setAttribute("role", "region");
        panel.setAttribute("aria-label", page.label);
        panel.tabIndex = -1;
        element.querySelector("[data-workspace-pages]").append(panel);
        panels.set(page.id, panel);
      }
      panel.hidden = false;
      if (!dirty.has(page.id)) return;
      const html = render(page.id);
      // 同一数据重复通知也不改 DOM，保留未提交输入和浏览位置。
      if (markup.get(page.id) !== html) {
        beforeRender(page.id);
        panel.innerHTML = html;
        markup.set(page.id, html);
        afterRender(page.id);
      }
      dirty.delete(page.id);
    }
    return { update };
  }

  const api = { create };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.FundScopeWorkspacePageHost = api;
})(typeof window === "undefined" ? globalThis : window);
