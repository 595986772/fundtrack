/* 展示后端实际补查记录，不从模型回答中猜测“是否已更新”。也供离线前端测试使用。 */
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.FundScopeResearchRepair = api;
})(typeof window !== "undefined" ? window : globalThis, function () {
  const escape = (value) => String(value ?? "").replace(/[&<>"']/g, (char) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[char]));
  const statuses = {
    complete: "检查完成", gaps_remain: "仍有证据缺口", budget_exhausted: "已到补查上限",
    manual_refresh_required: "公开数据待手动补查", cancelled: "已停止",
    completed: "读取成功", failed: "读取失败", evidence_changed: "证据已变化",
    checked_no_change: "已检查，证据状态未变", no_update: "未取得新明细",
  };
  const kinds = {user_information: "待你补充", public_data: "公开数据", unsupported: "能力限制"};

  function evidenceDates(snapshot, action) {
    const rows = (snapshot?.requirements || []).filter((row) => action !== "disclosure_refresh" || ["holdings_exposure", "lookthrough_exposure"].includes(row.key));
    const dates = [...new Set(rows.map((row) => row.as_of_date).filter(Boolean))];
    return dates.length ? dates.map(escape).join("、") : "未取得日期";
  }

  function takeAuthorization(input, {terminal = false} = {}) {
    // 终端侧栏没有这个授权控件，不借用完整助手里尚未发送的勾选状态。
    if (terminal) return false;
    const allowed = Boolean(input?.checked);
    if (input) input.checked = false;
    return allowed;
  }

  function render(reports) {
    if (!Array.isArray(reports)) return "";
    return reports.map((report) => {
      const attempts = report.attempts || [];
      const gaps = report.remaining_gaps || [];
      const warnings = report.warnings || [];
      if (!attempts.length && !gaps.length && !warnings.length && !report.public_refresh_authorized) return "";
      return `<details class="research-repair">
        <summary><span>证据检查与补查</span><span>${escape(statuses[report.stop_reason] || "检查记录")}</span></summary>
        <p>${escape(report.scope?.name || "当前研究")} · ${report.public_refresh_enabled ? "本轮已授权公开补查" : "本轮仅使用已保存数据"} · 已用 ${escape(report.used_rounds)} / ${escape(report.max_rounds)} 轮</p>
        ${report.prior_used_rounds ? `<p>同一请求此前已使用 ${escape(report.prior_used_rounds)} 轮预算，已尝试来源不重复补查。</p>` : ""}
        ${attempts.length ? `<ol>${attempts.map((attempt) => `<li>
          <strong>${escape(attempt.label)}</strong> · ${escape(statuses[attempt.status] || attempt.status)}
          <p>证据覆盖 ${escape(attempt.before?.coverage_score_pct)}% → ${escape(attempt.after?.coverage_score_pct)}%${attempt.detail ? ` · ${escape(attempt.detail)}` : ""}</p>
          <p>来源：${escape(attempt.source || "已保存的业务数据")}<br>补查前证据日期：${evidenceDates(attempt.before, attempt.action)}<br>补查后证据日期：${evidenceDates(attempt.after, attempt.action)}</p>
        </li>`).join("")}</ol>` : "<p>未执行额外补查。</p>"}
        ${gaps.length ? `<ul>${gaps.map((gap) => `<li><strong>${escape(kinds[gap.kind] || "证据缺口")} · ${escape(gap.label || gap.key)}</strong><p>${escape(gap.detail || gap.reason || "")} ${escape(gap.next_action)}</p></li>`).join("")}</ul>` : ""}
        ${report.unattempted_fund_codes?.length ? `<p>尚未检查公开源：${report.unattempted_fund_codes.map(escape).join("、")}</p>` : ""}
        ${warnings.map((warning) => `<p>${escape(warning)}</p>`).join("")}
      </details>`;
    }).join("");
  }
  return {render, takeAuthorization};
});
