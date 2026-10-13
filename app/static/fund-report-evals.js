/* 季报评测的局部UI与任务控制：不调用聊天接口，不自动运行或自动改Prompt。 */
(function (root) {
  "use strict";
  const escape = value => String(value ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
  const safePdf = url => /^https:\/\/pdf\.dfcfw\.com\/pdf\/H2_AN\d{18}_1\.pdf#page=\d+$/.test(url || "") ? url : "";
  const reviewLabels = {pending: "待人工复核", accepted: "人工确认", needs_fix: "需修改"};
  const suiteId = "fund_report_rag_v1";

  function renderPicker(selected, disabled) {
    return `<label class="report-eval-field">评测套件<select aria-label="评测套件" data-eval-suite ${disabled ? "disabled" : ""}>
      <option value="fund_agent_v2" ${selected === "fund_agent_v2" ? "selected" : ""}>基金 Agent 综合评测</option>
      <option value="${suiteId}" ${selected === suiteId ? "selected" : ""}>季报 RAG · 三类基金</option></select></label>`;
  }

  function renderMetrics(run) {
    const metrics = run?.report_metrics || {};
    const format = value => value?.value == null ? "不适用" : `${(value.value * 100).toFixed(1)}% (${value.numerator}/${value.denominator})`;
    const review = metrics.manual_review || {};
    return `<div class="report-eval-metrics">
      ${[["evidence_recall","标注证据召回"],["disclosure_numeric","披露数字核对"],["citation_locatable","引用可定位"]]
        .map(([key, label]) => `<div><span>${label}</span><strong>${format(metrics[key])}</strong></div>`).join("")}
      <p>人工确认 ${review.accepted || 0} · 待复核 ${review.pending || 0} · 需修改 ${review.needs_fix || 0}${metrics.execution_failures ? ` · 执行失败 ${metrics.execution_failures}` : ""}</p>
      <p>完成评分 ${metrics.graded_cases || 0}/${metrics.total_cases || run?.total_cases || 0} 题；失败题不进入上述比例。</p>
      <p>自动分不等于回答准确率。引用支持性、观点解释和无答案问题尚需复核。</p>
    </div>`;
  }

  function renderQuality(result, runId, busy, draft) {
    const quality = result?.report_quality;
    if (!quality) return "";
    const review = quality.manual_review || {status:"pending"};
    const fields = draft || review;
    const config = quality.run_config || {};
    const sourceLink = gold => safePdf(gold.url) ? `<a href="${escape(gold.url)}" target="_blank" rel="noopener noreferrer">${escape(gold.period)} · 第${escape(gold.page)}页</a>` : "来源地址不可用";
    return `<section class="report-eval-quality">
      <h4>原文对照 · ${reviewLabels[review.status] || reviewLabels.pending}</h4>
      <p class="resource-detail">${escape(config.model)} · ${escape(config.retrieval_mode)} · ${escape(config.embedding_model)}</p>
      <p class="resource-detail">${escape(config.token_scope)}</p>
      <strong>参考答案（待项目所有者验收）</strong><p>${escape(quality.reference_answer)}</p>
      ${(quality.gold_evidence || []).map(gold => `<div class="report-eval-source">${sourceLink(gold)}<span>${gold.retrieved ? "已召回" : "未召回"}</span><p>${escape(gold.quote)}</p></div>`).join("")}
      <details><summary>完整回答与逐轮引用</summary>
        ${(quality.turns || []).map((turn, i) => `<strong>第 ${i + 1} 轮：${escape(turn.question)}</strong><pre class="report-eval-answer" tabindex="0">${escape(turn.answer)}</pre><p class="resource-detail">${escape(turn.reference?.fund_code)} · ${escape((turn.reference?.periods || []).join(" / "))}</p>`).join("")}
      </details>
      <details><summary>检索原文与运行版本</summary>
        <code class="report-eval-fingerprint">${escape(config.pipeline_fingerprint)}</code>
        ${(quality.retrieved_evidence || []).map(row => `<p><strong>${escape(row.period)} · 第${escape(row.page)}页</strong></p><pre class="report-eval-answer" tabindex="0">${escape(row.text)}</pre>`).join("")}
      </details>
      <ul>${(quality.review_criteria || []).map(criterion => `<li>${escape(criterion)}</li>`).join("")}</ul>
      <form data-report-eval-review data-run="${escape(runId)}" data-case="${escape(result.case_id)}">
        <label class="report-eval-field">人工复核<select aria-label="人工复核" name="status" ${busy ? "disabled" : ""}>${Object.entries(reviewLabels).map(([key,label]) => `<option value="${key}" ${key === fields.status ? "selected" : ""}>${label}</option>`).join("")}</select></label>
        <label class="report-eval-field">复核说明<textarea name="note" maxlength="2000" rows="3" ${busy ? "disabled" : ""}>${escape(fields.note)}</textarea></label>
        <button class="action-button" type="submit" ${busy ? "disabled" : ""}><i data-lucide="save"></i>保存复核</button>
      </form>
    </section>`;
  }

  function createController({api, userId, storage, onBusy, onProgress, onResult, onError, makeKey, sleep}) {
    let busy = false, generation = 0;
    const keyFor = user => `fundscope-report-eval:${user}`;
    const read = user => {try {return JSON.parse(storage.getItem(keyFor(user)) || "null");} catch {return null;}};
    const store = (user, pending) => storage.setItem(keyFor(user), JSON.stringify(pending));
    const pending = () => read(userId());
    const pause = () => {generation++; busy = false;};

    async function watch(saved) {
      if (busy) return;
      busy = true;
      const user = userId(), version = ++generation;
      const active = () => user === userId() && generation === version;
      onBusy(true);
      try {
        if (!saved.jobId) {
          // 受理响应丢失时，恢复操作仍用同一个键，不能新建第二份付费任务。
          const accepted = await api("/api/v1/evals/fund-reports/jobs", {method:"POST", headers:{"Content-Type":"application/json"},
            body:JSON.stringify({user_id:user, case_ids:saved.caseIds, retrieval_mode:saved.retrievalMode, idempotency_key:saved.key})});
          saved = {...saved, jobId:accepted.job.id}; store(user, saved);
        }
        if (!active()) return;
        onBusy(true);
        const deadline = Date.now() + 61 * 60 * 1000;
        while (active() && Date.now() < deadline) {
          const response = await api(`/background-jobs/${encodeURIComponent(saved.jobId)}?user_id=${encodeURIComponent(user)}`);
          if (!active()) return;
          const job = response.job;
          onProgress(`${job.current_step}/${job.total_steps} · ${job.progress_message || job.status}`);
          if (["completed","failed","cancelled"].includes(job.status)) {
            if (job.status !== "completed") {
              storage.removeItem(keyFor(user));
              throw new Error(job.error || (job.status === "cancelled" ? "评测已取消；已发生的模型调用不会撤销。" : "评测任务失败。"));
            }
            if (!job.result?.eval_run_id) throw new Error("任务结束但未返回评测编号，请检查后台任务。");
            const run = await api(`/api/v1/evals/runs/${encodeURIComponent(job.result.eval_run_id)}?user_id=${encodeURIComponent(user)}`);
            if (!active()) return;
            storage.removeItem(keyFor(user));
            await onResult(run);
            return;
          }
          await sleep(1000);
        }
        if (active()) throw new Error("等待超时。任务编号已保留，可稍后继续查看。");
      } catch (error) {
        if (active()) onError(error.message);
      } finally {
        if (active()) {busy = false; onBusy(false);}
      }
    }

    async function start(caseIds, retrievalMode="hybrid") {
      if (busy) return;
      if (pending()) {onError("还有一项已提交或待确认的评测，请先继续查看。"); return;}
      const saved = {key:makeKey(), caseIds:[...caseIds], retrievalMode, jobId:null};
      store(userId(), saved);
      await watch(saved);
    }

    async function cancel() {
      const user = userId(), saved = read(user);
      if (!saved?.jobId) return;
      try {
        await api(`/background-jobs/${encodeURIComponent(saved.jobId)}/cancel`, {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({user_id:user})});
        if (user === userId()) onProgress("已请求取消，正在等待当前调用退出。");
      } catch(error) {if(user === userId()) onError(error.message);}
    }

    return {start, cancel, pending, pause, resume: () => {const saved=pending(); return saved ? watch(saved) : Promise.resolve();}};
  }

  const api = {suiteId, renderPicker, renderMetrics, renderQuality, createController};
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.FundReportEvals = api;
})(typeof window === "undefined" ? globalThis : window);
