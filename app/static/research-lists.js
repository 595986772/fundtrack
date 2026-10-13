/*
 * 事件、社区和板块的列表呈现。这里只整理已返回的数据，不采集、不调用模型。
 * 列表使用简短摘要；详细证据由主程序交给独立详情页。这样刷新列表不会销毁
 * 其他工作区的表单，也不需要维护第二套金融计算规则。
 */
(function (root) {
  "use strict";
  const escape = value => String(value ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;", "<":"&lt;", ">":"&gt;", '"':"&quot;", "'":"&#39;"}[c]));
  const visuals = typeof module !== "undefined" && module.exports ? require('./data-visuals') : root.FundScopeDataVisuals;
  const amount = value => !visuals.finite(value) ? "数据不足" : `${value > 0 ? "+" : ""}${(value / 100000000).toFixed(2)} 亿`;
  const detailLink = (kind, id, title) => `<button class="product-row-title" type="button" data-research-detail="${escape(kind)}" data-detail-id="${escape(id)}">${escape(title)}<i data-lucide="arrow-up-right" aria-hidden="true"></i></button>`;
  const provenance = text => `<p class="product-provenance">${escape(text)}</p>`;

  function safeLink(url, label) {
    try {
      const parsed = new URL(url);
      if (!["https:", "http:"].includes(parsed.protocol)) return escape(label);
      return `<a href="${escape(parsed.href)}" target="_blank" rel="noopener noreferrer">${escape(label)}<i data-lucide="external-link" aria-hidden="true"></i></a>`;
    } catch (_) { return escape(label); }
  }

  function events(items, {windowDays = "", now = Date.now()} = {}) {
    const cutoff = windowDays ? now - Number(windowDays) * 86400000 : null;
    // API 提供的是 published_date（日粒度），没有精确发布时间时以北京时间当日末尾筛选。
    const visible = items.filter(item => cutoff === null || Date.parse(`${item.published_date || item.event_date}T23:59:59+08:00`) >= cutoff);
    if (!visible.length) return '<p class="product-empty" role="status">当前已读取的事件中，没有符合时间范围的结果。</p>';
    const related = visible.filter(item=>(item.affected_fund_codes || []).length).length;
    return `<div class="dv-event-counts"><span><strong>${visible.length}</strong>当前事件</span><span><strong>${related}</strong>有关联基金</span><span><strong>${visible.filter(item=>item.semantic_analysis?.status==='accepted').length}</strong>已有 AI 复核</span></div>
    <ol class="product-news-list dv-timeline">${visible.map(item => {
      // member_count 是整个簇的数量，当前响应可能只包含其中几篇，不能冒称全量。
      const sources = new Set((item.cluster_members || [item]).map(row => row.source_url)).size;
      const themes = item.semantic_analysis?.status === "accepted" ? item.semantic_analysis.themes || [] : [];
      const relatedCount = (item.affected_fund_codes || []).length;
      return `<li><time class="dv-timeline-date">${escape(item.published_date || item.event_date)}</time><div class="dv-timeline-content">
        ${detailLink("event", item.event_id, item.title)}
        <div class="dv-event-tags"><span>${escape(themes.slice(0,3).join(" · ") || "未标注板块")}</span><span data-related="${relatedCount>0}">${relatedCount} 只关联基金</span><span>${item.semantic_analysis?.status === "accepted" ? "已有 AI 复核" : item.semantic_analysis?.status === "abstained" ? "AI 暂不判断" : "待研判"}</span></div>
        <div class="product-row-meta"><span>${escape(item.source_name)}</span><span>${item.cluster ? `${Number(item.cluster.member_count)} 篇聚合报道` : "独立报道"} · ${sources} 个已载入来源</span></div></div></li>`;
    }).join("")}</ol>`;
  }

  function topics(items) {
    return `<ol class="product-topic-list">${items.map(item => {
      const counts = item.action_counts || {};
      // 使用后端已按话题去重的自述计数，不能把不同话题的作者数相加冒充独立人数。
      const count = key => visuals.finite(counts[key]) && counts[key]>=0 ? counts[key] : 0;
      const groups = [{label:'买入 / 加仓',value:count('buy')+count('add'),tone:'positive'},
        {label:'减仓 / 卖出',value:count('reduce')+count('sell'),tone:'negative'},
        {label:'持有 / 观察',value:count('hold')+count('watch'),tone:'neutral'},
        {label:'未明确',value:count('unknown'),tone:'neutral'}];
      const total = groups.reduce((sum,row)=>sum+row.value,0);
      const disagreement = visuals.finite(item.disagreement_ratio) ? `${(item.disagreement_ratio*100).toFixed(0)}%` : '未知';
      return `<li><div class="dv-topic-heading">${detailLink("topic", item.topic_key, item.topic_label)}<span>${escape(item.positioning_label || "观点待归纳")}</span></div>
        <div class="product-row-meta"><span>${Number(item.author_count || 0)} 位独立作者（该话题内）</span><span>分歧 ${disagreement}</span></div>
        ${total ? `<div class="dv-opinion-strip" role="img" aria-label="作者自述分布：${groups.map(row=>`${row.label}${row.value}`).join('，')}">${groups.filter(row=>row.value).map(row=>`<span data-dv-tone="${row.tone}" style="width:${row.value/total*100}%"></span>`).join('')}</div>
          <div class="dv-opinion-legend">${groups.filter(row=>row.value||row.tone!=='neutral').map(row=>`<span data-dv-tone="${row.tone}">${row.label} <strong>${row.value}</strong></span>`).join('')}</div>` : '<p class="dv-note">暂无可比较的自述操作分布</p>'}
        <p class="dv-note">${escape(item.window_start?.slice(0,10) || '--')} 至 ${escape(item.window_end?.slice(0,10) || '--')} · 当前窗口统计，暂无可比较的分歧历史</p>
      </li>`;
    }).join("")}</ol>`;
  }

  function hotspots(items) {
    return `<ol class="product-news-list">${items.map(item => `<li><div class="product-row-meta"><time>${escape(item.event_end)}</time><span>${escape(item.lifecycle_label)}</span><span>${Number(item.source_count)} 个来源</span></div>
      ${detailLink("hotspot", item.cluster_id, item.title)}<p class="product-row-excerpt">${escape(item.summary)}</p>
      <div class="product-row-meta"><span>热度 ${Number(item.heat_score).toFixed(0)}</span><span>${escape(item.tracking_reason)}</span></div></li>`).join("")}</ol>`;
  }

  function topicDetail(item) {
    return `<h2>${escape(item.topic_label)}</h2>${provenance(`${Number(item.author_count || 0)} 位话题内独立作者；自述操作不是已验证交易。`)}
      <p>${escape(item.positioning_label || "观点待归纳")} · 分歧 ${(Number(item.disagreement_ratio || 0) * 100).toFixed(0)}%</p>
      <h3>代表观点</h3><ul class="product-evidence-list">${(item.evidence_posts || []).map(post => `<li><strong>${escape(post.author_name)}</strong><span>${escape(post.action_label)}</span><p>${escape(post.rationale || post.title)}</p>${post.post_id ? `<button type="button" data-social-post="${escape(post.post_id)}">查看原帖证据</button>` : safeLink(post.source_url, "查看原文")}</li>`).join("")}</ul>`;
  }

  function eventDetail(item) {
    const members = item.cluster_members || [item];
    const unique = [...new Map(members.map(row => [row.source_url, row])).values()];
    return `<h2>${escape(item.title)}</h2>${provenance(`${item.event_date} · ${item.source_name} · 事件 ${item.event_id}`)}
      <h3>事件摘要</h3><p>${escape(item.summary)}</p>
      <h3>${item.semantic_analysis?.status === "accepted" ? "已有 AI 影响解释" : "影响线索（待 AI 复核）"}</h3><p>${escape(item.impact_reason || "暂无影响解释")}</p>
      <h3>关联基金</h3><div class="product-toolbar">${(item.affected_fund_codes || []).map(code => `<button class="product-text-action" data-research-fund="${escape(code)}" type="button">${escape(code)}<i data-lucide="arrow-up-right"></i></button>`).join("") || "暂无已确认的关联基金"}</div>
      <h3>已载入的原始来源</h3><ul class="product-evidence-list">${unique.map(row => `<li>${safeLink(row.source_url, row.title || row.source_name)}${provenance(`${row.source_name} · ${row.event_date}`)}</li>`).join("")}</ul>
      ${item.cluster && item.cluster.member_count > members.length ? provenance(`该事件共 ${item.cluster.member_count} 篇报道，当前查询载入 ${members.length} 篇；这里不是全部原文。`) : ""}`;
  }

  // 板块只按已有标准主题标签关联。没有标签的报道不凭标题模糊匹配，
  // 不把讨论热度、资金量和未来收益混成一个“推荐分”。
  function sectorRows(capital, hotspots, labels = []) {
    // 两套现有数据源的同义标签显式对齐，仅合并名称，不推测事件所属行业。
    const canonical = label => ({"医药医疗": "医药", "大金融": "金融"}[label] || label);
    const signals = (capital?.topic_signals || []).filter(row => row.topic_kind !== "broad");
    const byLabel = new Map(signals.map(row => [canonical(row.topic_label), row]));
    return [...new Set([...labels, ...byLabel.keys()])].map(label => {
      const signal = byLabel.get(label);
      const events = (hotspots?.hotspots || []).filter(row => (row.themes || []).map(canonical).includes(label));
      return {code: signal?.topic_code || label, label, signal, events, eventsAvailable: Array.isArray(hotspots?.hotspots)};
    });
  }

  function radar(rows, {sort = "flow", query = ""} = {}) {
    const filtered = rows.filter(row => row.label.includes(query));
    const sorted = [...filtered].sort((a, b) => sort === "name" ? a.label.localeCompare(b.label, "zh-CN")
      : sort === "flow" ? (b.signal?.flow_5d_cny ?? -Infinity) - (a.signal?.flow_5d_cny ?? -Infinity)
      : b.events.length - a.events.length);
    const max = Math.max(...sorted.map(row=>visuals.finite(row.signal?.flow_5d_cny)?Math.abs(row.signal.flow_5d_cny):0),0);
    return `<div class="dv-sector-list"><div class="dv-toolbar"><h3>近5日 ETF 样本净申购</h3><span class="dv-note">左：净赎回 · 右：净申购 · 单位：亿元</span></div><ol class="dv-bars" aria-label="板块净申购排行">${sorted.map(row=>{
      const value=row.signal?.flow_5d_cny, width=visuals.finite(value)&&max?Math.abs(value)/max*50:0;
      return `<li data-dv-tone="${visuals.tone(value)}"><span class="dv-bar-name">${detailLink('sector',row.code,row.label)}</span>
        <span class="dv-bar-track" data-signed="true" aria-hidden="true"><i style="left:${value<0?50-width:50}%;width:${width}%"></i></span><strong>${escape(amount(value))}</strong>
        <div class="dv-sector-meta"><span>样本覆盖 ${visuals.finite(row.signal?.coverage_5d_pct)?`${row.signal.coverage_5d_pct.toFixed(0)}%`:'未知'}</span><span>相关热点 ${row.eventsAvailable?row.events.length:'未知'}</span><span>截至 ${escape(row.signal?.data_date || '未知')}</span></div></li>`;
    }).join('') || '<li>没有符合条件的板块</li>'}</ol></div>
      <p class="dv-attention">资金为已覆盖 ETF 样本的净申购估算，不是板块总资金；净申购不代表未来上涨。</p>
      <p class="dv-note">近30日、近3个月板块价格序列尚未接入；前景研判待研究。</p>
      <details class="dv-detail"><summary>数据覆盖与待研究项</summary>${provenance("热点数仅统计本次载入且有匹配标签的记录；未匹配不等于没有事件。资金规模不能代替产业、盈利与估值判断。")}</details>`;
  }

  function sectorDetail(row) {
    const signal = row.signal;
    return `<h2>${escape(row.label)}</h2>${provenance(`资金证据日期 ${signal?.data_date || "暂无"} · 热门不等于有前景`)}
      <h3>资金线索</h3>${visuals.metrics([['近1日',signal?.flow_1d_cny],['近5日',signal?.flow_5d_cny],['近20日',signal?.flow_20d_cny]].map(([label,value])=>({label:`${label} ETF 净申购估算`,value:amount(value),tone:value})))}
      ${provenance(`样本覆盖 ${signal?.coverage_5d_pct == null ? "未知" : `${signal.coverage_5d_pct}%`} · 证据 ${signal?.evidence_id || "未取得"}`)}
      <div class="product-toolbar"><a href="#sectors/capital">完整资金证据</a></div>
      <h3>相关事件</h3><ul class="product-evidence-list">${row.events.map(event => `<li><strong>${escape(event.title)}</strong><p>${escape(event.summary)}</p>${provenance(`${event.event_end || ""} · ${event.source_count} 个来源`)}${(event.evidence || []).map(source => safeLink(source.source_url, source.source_name)).join(" · ")}</li>`).join("") || "<li>当前样本没有匹配该标签的热点，不代表没有相关事件。</li>"}</ul>
      <h3>前景依据</h3><p>尚未生成该板块的前景研判。已有资金和事件线索不能替代产业、盈利与估值证据。</p>
      <h3>待验证事项</h3><p>数据时效、样本覆盖、产业需求与估值依据，以及与现有持仓的风险重叠。</p>`;
  }

  const api = {escape, safeLink, events, topics, hotspots, topicDetail, eventDetail, sectorRows, radar, sectorDetail};
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.FundScopeResearchLists = api;
})(typeof globalThis === "undefined" ? this : globalThis);
