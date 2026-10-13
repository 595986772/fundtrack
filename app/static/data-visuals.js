/*
 * Web 数据视图：把服务端事实转成图表、指标和对比条，不抓取数据、不调用模型。
 * 空值一直保持未知；前端只换算显示单位，不重新计算收益、风险或推荐分。
 */
(function (root) {
  "use strict";
  const esc = value => String(value ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
  const finite = value => typeof value === "number" && Number.isFinite(value);
  const tone = value => !finite(value) || value === 0 ? "neutral" : value > 0 ? "positive" : "negative";
  const number = (value, digits = 2) => finite(value) ? value.toLocaleString("zh-CN", {minimumFractionDigits:digits, maximumFractionDigits:digits}) : "--";
  const pct = value => finite(value) ? `${value > 0 ? "+" : ""}${number(value)}%` : "--";
  const money = value => finite(value) ? `${value > 0 ? "+" : value < 0 ? "-" : ""}¥${number(Math.abs(value))}` : "--";

  function metrics(items) {
    return `<dl class="dv-metrics">${items.map(item => `<div><dt>${esc(item.label)}</dt><dd data-dv-tone="${tone(item.tone)}">${esc(item.value)}</dd>${item.note ? `<small>${esc(item.note)}</small>` : ""}</div>`).join("")}</dl>`;
  }

  // 所有条形共用同一尺度。正负条以零为中心，零值不制造最小长度。
  function bars(rows, {signed = false, percent = false, label = "数据对比"} = {}) {
    const max = percent ? 100 : Math.max(...rows.filter(row => finite(row.value)).map(row => Math.abs(row.value)), 0);
    return `<ol class="dv-bars" aria-label="${esc(label)}">${rows.map((row, index) => {
      const width = !finite(row.value) || !max ? 0 : Math.min(Math.abs(row.value) / max, 1) * (signed ? 50 : 100);
      const left = signed ? row.value < 0 ? 50 - width : 50 : 0;
      return `<li data-dv-tone="${signed ? tone(row.value) : "category"}" data-dv-color="${index % 5}">
        <span class="dv-bar-name">${esc(row.label)}</span><span class="dv-bar-track" data-signed="${signed}" aria-hidden="true"><i style="left:${left}%;width:${width}%"></i></span>
        <strong>${esc(row.text ?? number(row.value))}</strong>${row.note ? `<small>${esc(row.note)}</small>` : ""}</li>`;
    }).join("") || '<li class="dv-empty">暂无可比较数据</li>'}</ol>`;
  }

  function holdings(portfolio, {view = "all"} = {}) {
    const rows = [...(portfolio.holdings || [])].sort((a,b) => (b.weight_pct ?? -Infinity) - (a.weight_pct ?? -Infinity));
    if (!rows.length) return "";
    const known = rows.filter(row => finite(row.weight_pct) && row.weight_pct >= 0);
    const pnl = [...rows].sort((a,b) => (b.unrealized_pnl_amount ?? -Infinity) - (a.unrealized_pnl_amount ?? -Infinity));
    const allocation = `<section class="dv-allocation" aria-label="仓位分布"><h3>仓位分布</h3>
      <div class="dv-allocation-strip" aria-hidden="true">${known.map((row,i) => `<span data-dv-color="${i % 5}" style="width:${Math.min(row.weight_pct,100)}%"></span>`).join("")}</div>
      <div class="dv-allocation-list" tabindex="0" role="region" aria-label="全部基金仓位">${bars(rows.map(row=>({label:row.short_name || row.fund_code,value:row.weight_pct,text:finite(row.weight_pct)?`${number(row.weight_pct)}%`:"未知"})), {percent:true,label:"按当前市值计算的仓位占比"})}</div>
      <p class="dv-note">按当前市值 · 各基金净值日期可能不同</p></section>`;
    const contribution = `<section class="dv-contribution"><h3>谁在贡献盈亏</h3>${bars(pnl.map(row=>({label:row.short_name || row.fund_code,value:row.unrealized_pnl_amount,text:money(row.unrealized_pnl_amount)})),{signed:true,label:"当前持仓浮动盈亏贡献，单位元"})}
      <p class="dv-note">当前浮动盈亏，非当日收益；已实现收益见账本明细</p></section>`;
    // 同一份事实可以分区展示，避免为了布局在 app.js 再维护一套计算或排序。
    if (view === "allocation") return allocation;
    if (view === "contribution") return contribution;
    return `<div class="dv-split dv-holdings-distribution">${allocation}${contribution}</div>`;
  }

  function performanceMetrics(p) {
    return metrics([
      {label:"区间收益（TWR）",value:pct(p?.twr_pct),tone:p?.twr_pct},
      {label:"相对基准",value:pct(p?.benchmark?.excess_return_pct),tone:p?.benchmark?.excess_return_pct},
      {label:"历史最大回撤",value:pct(p?.max_drawdown?.drawdown_pct)},
      {label:"年化波动",value:finite(p?.annualized_volatility_pct)?`${number(p.annualized_volatility_pct)}%`:"--"},
    ]);
  }

  // 指数已由后端从 100 起算，减去 100 只是转换为累计收益百分比。
  // 不填补缺失基准，不平滑曲线，不压缩采样，保留实际观测日期。
  function chartData(p, drawdown = false) {
    return {drawdown, benchmark:p?.benchmark?.benchmark_name || "业绩基准", points:(p?.series || []).filter(point=>point.date).map(point=>({
      date:point.date,
      value:drawdown ? finite(point.drawdown_pct)?point.drawdown_pct:null : finite(point.portfolio_index)?point.portfolio_index-100:null,
      benchmark:!drawdown && finite(point.benchmark_index)?point.benchmark_index-100:null,
    }))};
  }

  function trend(p, {drawdown = false, compact = false} = {}) {
    const data = chartData(p, drawdown);
    const title = drawdown ? "历史回撤" : "收益走势";
    const available = data.points.filter(point=>finite(point.value)).length >= 2;
    return `<figure class="dv-trend${compact ? " dv-trend-compact" : ""}"><figcaption><h3>${title}</h3><span>${esc(p?.period_start || "--")} 至 ${esc(p?.period_end || "--")}</span></figcaption>
      ${available ? `<div class="dv-chart" data-dv-chart="${esc(JSON.stringify(data))}" role="img" aria-label="${title}，${esc(p.period_start)}至${esc(p.period_end)}，${data.points.length}个观测日"></div>` : '<p class="dv-chart-empty" role="status">至少需要两个有效观测日，暂无可绘制曲线。</p>'}
      <p class="dv-note">${drawdown ? "相对历史高点的回落幅度，非未来亏损预测" : `${esc(p?.performance_label || "业绩口径暂不可用")} · ${esc(p?.observation_count ?? data.points.length)} 个观测日`}${!drawdown ? ` · 基准：${esc(data.benchmark)}${data.points.some(point=>finite(point.benchmark)) ? "" : "（暂无可比数据）"}` : ""}</p>
      ${available ? `<details class="dv-chart-values"><summary>逐日数值</summary><div class="product-table-scroll" tabindex="0" role="region" aria-label="${title}逐日数据"><table class="product-data-table"><thead><tr><th>日期</th><th>${drawdown?"回撤":"组合收益"}</th>${drawdown?"":"<th>基准收益</th>"}</tr></thead><tbody>${data.points.map(point=>`<tr><th>${esc(point.date)}</th><td>${pct(point.value)}</td>${drawdown?"":`<td>${pct(point.benchmark)}</td>`}</tr>`).join("")}</tbody></table></div></details>` : ""}</figure>`;
  }

  function researchMetrics(report) {
    const p = report.performance_snapshot || {}, q = report.quant_risk_snapshot || {};
    return metrics([{label:"历史区间收益",value:pct(p.twr_pct),tone:p.twr_pct},
      {label:"相对基准",value:pct(p.benchmark?.excess_return_pct),tone:p.benchmark?.excess_return_pct},
      {label:"历史年化波动",value:finite(q.annualized_volatility_pct)?`${number(q.annualized_volatility_pct)}%`:"--"}]);
  }

  // 图表的生命周期跟随所属页面，隐藏时不销毁，重绘或换账本时释放观察器。
  const mounted = new Map();
  function mount(scope) {
    if (!scope) return;
    scope.querySelectorAll("[data-dv-chart]").forEach(element => {
      if (mounted.has(element)) return;
      const data = JSON.parse(element.dataset.dvChart);
      let chart;
      const draw = () => {
        if (!element.isConnected || element.clientWidth === 0) return;
        if (!root.echarts) { element.textContent = "图表组件暂不可用，请展开逐日数值。"; return; }
        if (chart) { chart.resize(); return; }
        const style = getComputedStyle(element), color = name => style.getPropertyValue(name).trim();
        chart = root.echarts.init(element, null, {renderer:"canvas"});
        const primary = data.drawdown ? color("--info") || "#8ab5f5" : color("--accent");
        // 保留图表的简短 aria-label；完整数值另有可访问表格，避免自动描述把空值读成 NaN。
        chart.setOption({animation:false,aria:{enabled:false},textStyle:{fontFamily:"Arial, PingFang SC, sans-serif",color:color("--muted")},
          grid:{left:58,right:20,top:34,bottom:40},
          tooltip:{trigger:"axis",renderMode:"richText",valueFormatter:value=>finite(value)?`${number(value)}%`:"--"},
          legend:{show:!data.drawdown,top:0,textStyle:{color:color("--muted")},data:["组合",data.benchmark]},
          xAxis:{type:"category",data:data.points.map(point=>point.date),boundaryGap:false,axisLine:{lineStyle:{color:color("--border")}},axisTick:{show:false},axisLabel:{fontSize:11,color:color("--muted"),hideOverlap:true}},
          yAxis:{type:"value",scale:true,axisLabel:{fontSize:11,color:color("--muted"),formatter:value=>`${value.toFixed(1)}%`},splitLine:{lineStyle:{color:color("--border")}}},
          series:[{name:data.drawdown?"回撤":"组合",type:"line",data:data.points.map(point=>point.value),connectNulls:false,smooth:false,showSymbol:data.points.length<10,symbolSize:5,itemStyle:{color:primary},lineStyle:{width:2},...(data.drawdown?{areaStyle:{opacity:0.12}}:{})},
            ...(!data.drawdown && data.points.some(point=>finite(point.benchmark))?[{name:data.benchmark,type:"line",data:data.points.map(point=>point.benchmark),connectNulls:false,smooth:false,showSymbol:false,itemStyle:{color:color("--comparison-line")||"#79aefa"},lineStyle:{width:1.5,type:"dashed"}}]:[])]});
      };
      const observer = new ResizeObserver(draw);
      observer.observe(element);
      mounted.set(element, ()=>{observer.disconnect();chart?.dispose();});
      draw();
    });
  }
  function unmount(scope) {
    for (const [element,dispose] of mounted) if (!element.isConnected || scope?.contains(element)) {dispose();mounted.delete(element);}
  }

  const api={esc,finite,number,pct,money,tone,metrics,bars,holdings,performanceMetrics,chartData,trend,researchMetrics,mount,unmount};
  if (typeof module!=="undefined" && module.exports) module.exports=api;
  else root.FundScopeDataVisuals=api;
})(typeof window==="undefined"?globalThis:window);
