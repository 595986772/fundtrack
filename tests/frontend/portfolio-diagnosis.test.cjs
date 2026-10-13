// 纯展示测试：结论不重写，指标与来源不丢失，详细内容默认折叠。
const test=require('node:test'),assert=require('node:assert/strict');
const {JSDOM}=require('../../miniprogram/node_modules/jsdom');
const {render,groupFindings}=require('../../app/static/portfolio-diagnosis.js');
function fixture(){return {run_id:'old',diagnosis:{headline:'历史配置结论',summary:'报告摘要',portfolio_name:'测试账本',report_date:'2026-09-01',
  interpretation_label:'AI 研判',fallback_reason:'',risk_period:'2025-09-01 至 2026-09-01',
  findings:[0,1,2].map(i=>({id:`f${i}`,tone:'caution',tone_label:'需关注',title:`发现${i}`,detail:'冻结依据',scope:'组合整体',holdings:[],gap:'',
    references:[{id:`e${i}`,label:'波动',value:'0.00%',date:'2026-08-31',source:'净值快照',explanation:'历史区间',url:'https://example.com/report'}]})),
  more_findings:[],holdings:[],top_risk:null,conflicts:[],uncertainties:[],conditional_view:'条件改变后需要重看',follow_ups:[],limitations:['历史不代表未来'],
  questions:['为什么这样判断？'],technical:[{label:'输入指纹',value:'frozen-hash'}]}};}
test('结论与摘要先展示，完整观点先分类再呈现，解释和证据就地展开',()=>{
  const report=fixture();report.diagnosis.more_findings=[{...report.diagnosis.findings[0],id:'f3',tone:'supportive',title:'第四条支撑'}];
  const before=JSON.stringify(report);
  const html=render({report,metrics:'<p>完整指标内容</p>'}),doc=new JSDOM(html).window.document;
  assert.equal(doc.querySelector('h2').textContent,'历史配置结论');
  assert.equal(doc.querySelector('.diagnosis-conclusion .diagnosis-summary').textContent,'报告摘要');
  assert.equal(doc.querySelectorAll('.diagnosis-summary').length,1);
  assert.equal(doc.querySelectorAll('.diagnosis-finding').length,4);
  assert.equal(doc.querySelectorAll('[data-side="caution"] .diagnosis-finding').length,3);
  assert.ok([...doc.querySelectorAll('details')].every(d=>!d.open));
  const fourth=doc.querySelector('[data-side="supportive"] .diagnosis-finding > summary');
  assert.match(fourth.textContent,/第四条支撑/);
  assert.equal(fourth.parentElement.parentElement.closest('details'),null);
  assert.match(doc.querySelector('.diagnosis-evidence').textContent,/0.00%/);assert.match(doc.body.textContent,/2026-08-31/);
  assert.ok(doc.querySelector('.diagnosis-technical').closest('details'));assert.ok(html.indexOf('关键依据')<html.indexOf('完整指标内容'));
  assert.equal(JSON.stringify(report),before);
});
test('前三条全是风险时，后续中性和支撑仍归入各自分类，不制造支撑内容',()=>{
  const report=fixture(),d=report.diagnosis;
  d.more_findings=[{...d.findings[0],id:'f3',tone:'neutral',title:'债券表现平稳'},
    {...d.findings[0],id:'f4',tone:'unknown',title:'分类待核验'}];
  const groups=groupFindings(d),doc=new JSDOM(render({report})).window.document;
  assert.deepEqual(groups.neutral.map(row=>row.id),['f3','f4']);assert.equal(groups.supportive.length,0);
  assert.equal(doc.querySelector('[data-side="supportive"]').dataset.empty,'true');
  assert.match(doc.querySelector('[data-side="supportive"]').textContent,/未形成明确支撑观点/);
  assert.equal(doc.querySelectorAll('[data-side="neutral"] .diagnosis-finding').length,2);
  assert.equal(doc.querySelector('.dv-research-sides'),null);
});
test('历史与指标默认折叠，已有对比时展开；追问及历史操作仍可访问',()=>{
  const report=fixture(),doc=new JSDOM(render({report,comparison:'<p>已完成对比</p>',
    history:[{job_id:'old',headline:'历史配置结论',assessment_date:'2026-09-01'}],canCompare:true})).window.document;
  assert.equal(doc.querySelectorAll('[data-follow-report]').length,1);
  assert.equal(doc.querySelector('[data-diagnosis-question]').dataset.diagnosisQuestion,'0');
  assert.equal(doc.querySelector('.diagnosis-history-section').open,true);
  assert.equal(doc.querySelector('[data-compare-portfolio-research]').disabled,false);
  assert.equal(doc.querySelector('.diagnosis-history[aria-current="true"]').dataset.reviewReport,'old');
  assert.equal(doc.querySelector('.diagnosis-audit').open,false);
  assert.equal(doc.querySelector('.diagnosis-holdings').open,false);
});
test('缺失反对理由不等于没有反对证据，未知风险不填零',()=>{
  const html=render({report:fixture()});assert.match(html,/不代表不存在反对证据/);assert.match(html,/未提供可确认的最大风险贡献基金/);
  assert.match(html,/历史快照，非当前持仓实时结论/);assert.doesNotMatch(html,/最大历史波动风险来源：.*0\.00%/);
});
test('来源与模型文本只能作为文本，脚本和带凭据链接不能点击',()=>{
  const r=fixture();r.diagnosis.headline='<img src=x onerror=alert(1)>';
  r.diagnosis.findings[0].references[0].url='javascript:alert(1)';
  r.diagnosis.findings[1].references[0].url='https://user:pass@example.com';
  const doc=new JSDOM(render({report:r})).window.document;
  assert.equal(doc.querySelectorAll('img').length,0);assert.match(doc.querySelector('h2').textContent,/<img/);
  assert.equal(doc.querySelectorAll('.diagnosis-evidence a').length,1);
});
test('旧的原始Job结果缺展示数据时只重读，不生成新报告',()=>{
  const html=render({report:{run_id:'old'}});assert.match(html,/data-review-report="old"/);assert.doesNotMatch(html,/data-run-portfolio-research/);
});
test('报告首屏提供唯一追问入口，历史数据、分歧与后续关注各有独立语义',()=>{
  const report=fixture();report.performance_snapshot={twr_pct:0,benchmark:{excess_return_pct:-1.2}};
  report.diagnosis.conflicts=['反对意见'];report.diagnosis.uncertainties=['仍待补查'];report.diagnosis.follow_ups=['下一披露日'];
  const d=new JSDOM(render({report})).window.document;
  assert.equal(d.querySelectorAll('[data-follow-report]').length,1);
  assert.ok(d.querySelector('.diagnosis-heading [data-follow-report]'));
  assert.match(d.querySelector('.diagnosis-key-data').textContent,/0.00%/);
  assert.equal(d.querySelectorAll('.diagnosis-outlook-grid > div').length,2);
  assert.match(d.querySelector('.diagnosis-outlook-grid').textContent,/反对意见.*仍待补查.*下一披露日/s);
  assert.equal(d.querySelector('.diagnosis-question').dataset.diagnosisQuestion,'0');
});
