/* 季报页独立管理数据与选中版本。普通读取不下载文档、不调用模型。 */
(function(root) {
  'use strict';
  const esc = v => String(v ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const icon = name => `<i data-lucide="${name}" aria-hidden="true"></i>`;
  function create({element, request, userId, waitJob, ask, icons = () => {}}) {
    let fund=null, documents=[], ids=[], comparison=null, source=null, version=0, loading=false, busy=false, error='', message='';
    const url = () => `/api/v1/funds/${fund.fund_code}/reports`;
    const key = () => `fundscope-reports:${userId()}:${fund.fund_code}`;
    const active = (v, user) => version===v && userId()===user;
    function paint() {
      if(!fund) {element.innerHTML='<p>尚未选择基金</p>';return;}
      const heading=`<header><div><h2>${esc(fund.short_name || fund.fund_code)} · 季报</h2><p>${esc(fund.fund_code)} · 历史披露，不代表实时持仓</p></div><div class="report-actions">
        <button class="secondary-action compact" data-sync ${busy||loading?'disabled':''}>${icon('download')}同步最近两期</button>
        <button class="fund-agent-action" data-ask ${busy||loading||!comparison?'disabled':''}>${icon('sparkles')}AI 对比分析</button></div></header>`;
      const status=`${error?`<p role="alert">${esc(error)} <button class="secondary-action compact" data-reload>重新读取</button></p>`:''}${loading||message?`<p role="status">${esc(loading?'正在读取…':message)}</p>`:''}`;
      if(source) {
        element.innerHTML=heading+status+`<section class="report-source"><div class="report-actions"><button class="secondary-action compact" data-back>${icon('arrow-left')}返回比较</button><a href="${esc(source.source_url)}" target="_blank" rel="noopener noreferrer">打开 PDF 第${source.page}页 ${icon('external-link')}</a></div><h3>${esc(source.period)} · 第 ${source.page} / ${source.page_count} 页</h3><pre>${esc(source.text||'本页没有可提取文字')}</pre><div class="report-actions"><button class="secondary-action compact" data-turn="-1" ${loading||source.page<=1?'disabled':''}>上一页</button><button class="secondary-action compact" data-turn="1" ${loading||source.page>=source.page_count?'disabled':''}>下一页</button></div></section>`;
        icons(); return;
      }
      const selectors=documents.length?`<div class="report-selectors">${[0,1].map(i=>`<label>报告${i===0?'一':'二'}<select data-pair="${i}" ${loading||busy?'disabled':''}>${documents.map(d=>`<option value="${esc(d.id)}" ${ids[i]===d.id?'selected':''}>${esc(d.period)} · ${esc(d.published_at)}</option>`).join('')}</select></label>`).join('')}</div>`:'';
      const value=(r,name,i)=>r[name]?`<button class="report-evidence" data-document="${i}" data-page="${r[name].page}" title="查看该数值的原文页">${esc(r[name].ratio_percent)}% ${icon('file-text')}</button>`:'未提取';
      const body=comparison?`<section><h3>资产配置变化</h3><p>${esc(comparison.documents[0].period)} → ${esc(comparison.documents[1].period)} · 占基金总资产</p>
        <div class="report-table" role="region" aria-label="两期资产比例变化" tabindex="0"><table><thead><tr><th>资产类型</th><th>前期比例</th><th>本期比例</th><th>变化 / 百分点</th></tr></thead><tbody>${comparison.allocation_changes.map(r=>`<tr><th scope="row">${esc(r.label)}</th><td>${value(r,'before',0)}</td><td>${value(r,'after',1)}</td><td>${esc(r.change_percentage_points??'不可计算')}</td></tr>`).join('')}</tbody></table></div>
        ${!comparison.allocation_changes.length?'<p>未可靠提取到可比表格，可查看原文并分析经理观点。</p>':''}</section>
        <section><h3>原始报告</h3><div class="report-documents">${comparison.documents.map((d,i)=>`<article><h4>${esc(d.title)}</h4><p>披露 ${esc(d.published_at)} · ${d.page_count} 页 · ${d.semantic_ready?'语义索引就绪':'关键词检索'}</p><button class="secondary-action compact" data-document="${i}" data-page="1">${icon('book-open')}查看原文</button></article>`).join('')}</div></section>
        <details><summary>披露范围与数据缺口</summary><ul>${comparison.limitations.map(s=>`<li>${esc(s)}</li>`).join('')}</ul></details>`:
        (!documents.length&&!loading?'<section><h3>尚未保存季报</h3><p>当前基金还没有可比较的季度披露。</p></section>':'');
      element.innerHTML=heading+status+`<p class="report-footnote">来源：东方财富公开公告。同步与分析可能产生模型 API 费用。</p>`+selectors+body;
      icons();
    }
    async function compare(v,user) {
      const data=await request(`${url()}/compare?${ids.map(id=>`document_ids=${encodeURIComponent(id)}`).join('&')}`);
      if(active(v,user)) comparison=data;
    }
    // 每次进入只读取数据库。序号和账号共同拦截晚到响应，不把 A 的资料画到 B。
    async function show(next, options={}) {
      const v=++version,user=userId(); fund=next; source=null; comparison=null; error='';loading=true;busy=false;paint();
      if(!fund){loading=false;paint();return;}
      try {
        const query=options.ids?'?'+options.ids.map(id=>`document_ids=${encodeURIComponent(id)}`).join('&'):'';
        const data=await request(url()+query);if(!active(v,user))return;
        documents=data.documents;ids=options.ids||data.selected_ids;
        if(ids.length===2)await compare(v,user);
      } catch(e) {if(active(v,user))error=e.message;}
      finally{if(active(v,user)){loading=false;paint();}}
    }
    async function sync() {
      if(busy||loading||!fund)return;
      const v=++version,user=userId(),storage=key();busy=true;error='';message='正在提交同步';paint();
      // 同一提交失败可重试，复用幂等键；后台受理后持久保存 Job ID。
      let saved;
      try {saved=JSON.parse(localStorage.getItem(storage)||'null');}catch{saved=null;}
      if(!saved)saved={idempotency_key:crypto.randomUUID()};
      localStorage.setItem(storage,JSON.stringify(saved));
      try {
        if(!saved.job_id){const accepted=await request(`${url()}/sync`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({user_id:user,idempotency_key:saved.idempotency_key})});saved.job_id=accepted.job.id;localStorage.setItem(storage,JSON.stringify(saved));}
        const job=await waitJob(saved.job_id,user,current=>{if(active(v,user)){message=current.progress_message;paint();}});
        localStorage.removeItem(storage);
        if(!active(v,user))return;
        if(job.status!=='completed')throw new Error(job.error||'同步未完成，已有季报仍保留，可手动重试。');
        message=(job.result?.warnings||[]).join(' ')||'两期季报已保存';
        await show(fund,{refresh:true});
      }catch(e){if(active(v,user))error=e.message;}
      finally{if(active(v,user)){busy=false;paint();}}
    }
    // 页码是 PDF 的物理页；正文只当文本呈现，不执行外部文档中的 HTML 或脚本。
    async function openPage(document,page) {
      if(loading)return;
      const v=version,user=userId();loading=true;error='';paint();
      try {const result=await request(`${url()}/${document.id}/pages/${page}`);if(active(v,user))source=result;}
      catch(e){if(active(v,user))error=e.message;}
      finally{if(active(v,user)){loading=false;paint();}}
    }
    // 事件委托让局部重绘后按钮仍可用，分析动作只填现有聊天草稿。
    element.addEventListener('click',e=>{
      if(e.target.closest('[data-sync]'))void sync();
      if(e.target.closest('[data-reload]'))void show(fund,{refresh:true,ids});
      if(e.target.closest('[data-ask]')&&comparison)ask(fund,comparison);
      if(e.target.closest('[data-back]')){source=null;paint();}
      const doc=e.target.closest('[data-document]');if(doc&&comparison)void openPage(comparison.documents[Number(doc.dataset.document)],Number(doc.dataset.page));
      const turn=e.target.closest('[data-turn]');if(turn&&source)void openPage(source,source.page+Number(turn.dataset.turn));
    });
    element.addEventListener('change',async e=>{
      if(!e.target.matches('[data-pair]')||loading||busy)return;
      ids[Number(e.target.dataset.pair)]=e.target.value;comparison=null;error='';loading=true;paint();
      const v=version,user=userId();try{await compare(v,user);}catch(e){if(active(v,user))error=e.message;}
      finally{if(active(v,user)){loading=false;paint();}}
    });
    return {show,pause(){version++;loading=false;busy=false;}};
  }
  root.FundScopeFundReports={create};
  if(typeof module!=='undefined')module.exports={create};
})(typeof window!=='undefined'?window:globalThis);
