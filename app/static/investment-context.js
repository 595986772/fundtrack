// 聊天消息的投资背景快照。只渲染后端元数据，不从模型回答正文猜用户偏好。
(function (root, factory) {
  if (typeof module === "object" && module.exports) module.exports = factory();
  else root.FundScopeInvestmentContext = factory();
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  const labels = {horizon: "投资期限", loss_tolerance: "可接受亏损", liquidity: "近期用款", purpose: "资金用途"};
  function escape(value) {
    return String(value ?? "").replace(/[&<>"']/g, char => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[char]));
  }
  function render(context) {
    if (!context || typeof context !== "object") return "";
    const facts = context.facts || {};
    const present = Object.keys(labels).filter(key => typeof facts[key]?.text === "string" && facts[key].text);
    const missing = (context.pending_fields || []).filter(key => labels[key]).map(key => labels[key]);
    if (!present.length && !missing.length && context.update_status !== "cleared" && !context.needs_confirmation) return "";
    const status = context.needs_confirmation ? "待重新确认" : context.update_status === "cleared" ? "已清除" : present.length ? "用户自述" : "待补充";
    // 展开内容保留原句；旧消息显示当时状态，不会被后续更正重写历史。
    const rows = present.map(key => `<div><dt>${labels[key]}</dt><dd>${escape(facts[key].text)}</dd></div>`).join("");
    return `<details class="investment-context"${context.needs_confirmation || missing.length ? " open" : ""}>
      <summary>本轮投资背景 <span>${status}</span></summary>
      ${context.needs_confirmation ? '<p role="status">本次背景更新未能确认，以下旧条件暂不用于判断。请重新说明本次投资条件。</p>' : ""}
      ${rows ? `<dl>${rows}</dl>` : ""}
      ${context.update_status === "cleared" ? "<p>本会话不再沿用之前的投资条件。</p>" : ""}
      ${missing.length ? `<p>待补充：${missing.join("、")}</p>` : ""}
    </details>`;
  }
  return {render};
});
