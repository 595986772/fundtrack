/** 验证原生页面的真实调用逻辑；网络、模型与用户数据全部用隔离替身。 */
const {test,beforeEach,afterEach}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const {JSDOM}=require('jsdom');
const api=require('../dist/lib/api');
const {EventDecoder,streamChat}=require('../dist/lib/stream');
const {JobTracker}=require('../dist/lib/jobs');
const {reportBlocks}=require('../dist/lib/reports');
const {chinaTime}=require('../dist/lib/format');
const {researchDraft}=require('../dist/lib/navigation');
// 页面行为测试不启动微信 Canvas；图表实际渲染在浏览器检查中验证。
const chartPath=require.resolve('../dist/components/ec-canvas/echarts.js');
require.cache[chartPath]={id:chartPath,filename:chartPath,loaded:true,exports:{init(){throw new Error('测试不应启动 Canvas');}}};
let calls,storage,cleanup,respond,nav;
const portfolio={id:'portfolio_1234567890abcdef',name:'测试账本',holdings:[]};
const frame=(event,data)=>Buffer.from(`event: ${event}\ndata: ${JSON.stringify(data)}\n\n`);
const buffer=b=>b.buffer.slice(b.byteOffset,b.byteOffset+b.byteLength);
const tick=()=>new Promise(r=>setImmediate(r));
function page(name){let definition;global.Page=d=>definition=d;delete require.cache[require.resolve(`../dist/pages/${name}/index`)];require(`../dist/pages/${name}/index`);const p={...definition,data:structuredClone(definition.data),setData(v,cb){Object.assign(this.data,v);cb?.();},selectComponent(){return null;}};cleanup.push(()=>p.onUnload?.());return p;}
beforeEach(()=>{
  calls=[];storage=new Map();cleanup=[];nav=[];
  respond=o=>o.url.includes('/auth/me')?{authenticated:true,user:{user_id:'alice'}}:o.url.endsWith('/portfolios?user_id=alice')?{portfolios:[portfolio]}:undefined;
  global.wx={getStorageSync:k=>storage.get(k),setStorageSync:(k,v)=>storage.set(k,structuredClone(v)),removeStorageSync:k=>storage.delete(k),
    request(o){calls.push(o);let receive;const task={abort(){o.fail?.({errMsg:'aborted'});},onChunkReceived(fn){receive=fn;},chunk(b){receive({data:buffer(b)});}};o.task=task;
      queueMicrotask(()=>{const value=respond(o);if(value!==undefined)o.success({statusCode:200,data:value});});return task;},
    showModal:o=>o.success({confirm:true}),showToast(){},navigateTo:o=>nav.push(o.url),redirectTo:o=>nav.push(o.url),switchTab:o=>nav.push(o.url),stopPullDownRefresh(){}};
});
afterEach(()=>cleanup.forEach(fn=>fn()));
test('SSE任意中文拆包、CRLF、多帧和心跳均可解析',()=>{
  const events=[],decoder=new EventDecoder(e=>events.push(e));
  const bytes=Buffer.concat([Buffer.from(': keep-alive\r\n\r\n'),frame('answer_delta',{delta:'半导体风险'}),frame('result',{answer:'已完成'})]);
  for(const b of bytes)decoder.push(buffer(Buffer.from([b])));
  assert.deepEqual(events.map(e=>e.event),['answer_delta','result']);assert.equal(events[0].data.delta,'半导体风险');
});
test('流式请求保留Bearer、最终结果后断线不误报失败',async()=>{
  api.saveConnection({baseUrl:'http://localhost:8000',userId:'alice',token:'test-token'});
  const events=[],stream=streamChat({message:'测试'},e=>events.push(e));const call=calls[0];
  assert.equal(call.header.Authorization,'Bearer test-token');assert.equal(call.enableChunked,true);assert.ok(call.timeout>60000);
  call.task.chunk(frame('result',{assistant_message:{content:'完成'}}));await stream.done;
  call.fail({errMsg:'timeout'});assert.equal(events.length,1);
});
test('SSE未取得最终结果、HTTP401、业务错误均不报成功',async()=>{
  for(const mode of ['eof','auth','event']){const stream=streamChat({},()=>{}),c=calls.at(-1);const rejected=assert.rejects(stream.done);
    if(mode==='event')c.task.chunk(frame('error',{status:502,message:'模型失败'}));else c.success({statusCode:mode==='auth'?401:200,data:buffer(Buffer.alloc(0))});await rejected;}
});
test('多视角入口只填草稿，不自动调用模型',()=>{
  const p=page('research');p.multi();assert.match(p.data.question,/多视角/);assert.equal(calls.length,0);
  researchDraft('查看当前资产',{portfolio_id:portfolio.id});assert.equal(calls.length,0);assert.equal(nav.at(-1),'/pages/research/index');
});

test('季报页只读两期版本、按页溯源，分析按钮仅传研究草稿',async()=>{
  const documents=[1,2].map(i=>({id:`fr_${i}`,fund_code:'110020',period:`2026-Q${i}`,published_at:'2026-07-20',page_count:20,semantic_ready:true}));
  const data={documents,selected_ids:['fr_1','fr_2'],allocation_changes:[{label:'基金投资',before:{ratio_percent:'90.00',page:12},after:{ratio_percent:'92.12',page:12},change_percentage_points:'2.12'}],limitations:[]};
  respond=o=>o.url.includes('/pages/')?{...documents[0],page:12,text:'原文页',source_url:'https://example.test/report.pdf#page=12'}:data;
  const p=page('fund-reports');p.onLoad({code:'110020'});p._alive=true;p._scope=api.scope();await p.load();
  assert.equal(p.data.error,'');assert.equal(p.data.changes[0].changeText,'+2.12 个百分点');assert.ok(calls.every(c=>c.method==='GET'));
  p.ask();const draft=storage.get(api.storageKey('research-draft'));assert.deepEqual(draft.fund_report_ids,['fr_1','fr_2']);assert.equal(draft.fund_code,'110020');
  await p.openPage({currentTarget:{dataset:{document:0,page:12}}});assert.equal(p.data.page.text,'原文页');assert.ok(calls.every(c=>c.method==='GET'));
});

test('季报追问沿用同一文档ID，换基金后解除引用',async()=>{
  const p=page('research');p.setData({context:1,funds:[{fund_code:'110020'}],fund:0,question:'经理观点呢',conversationId:'report-chat',fundReportReference:{fund_code:'110020',document_ids:['fr_1','fr_2']}});
  const pending=p.send();await tick();const call=calls.find(c=>c.enableChunked);
  assert.deepEqual(call.data.fund_report_ids,['fr_1','fr_2']);assert.equal(call.data.research_job_id,'');assert.equal(call.data.research_refresh,false);
  call.task.chunk(frame('result',{conversation:{id:'report-chat'},user_message:{id:'u1',role:'user',content:'问题'},assistant_message:{id:'a1',role:'assistant',content:'回答',run_metadata:{fund_report_reference:{fund_code:'110020',document_ids:['fr_1','fr_2']}}}}));
  await pending;assert.deepEqual(p.data.fundReportReference.document_ids,['fr_1','fr_2']);
  p.field({currentTarget:{dataset:{field:'fund'}},detail:{value:1}});assert.equal(p.data.fundReportReference,null);
});
test('历史季报所属基金不可用时不默认研究第一只基金',async()=>{
  respond=()=>({messages:[{id:'a',role:'assistant',run_metadata:{fund_report_reference:{fund_code:'110020',document_ids:['fr_1','fr_2']}}}]});
  const p=page('research');p.setData({funds:[{fund_code:'161725'}],conversationId:'',fund:0});
  await assert.rejects(p.readConversation('old-report-chat'),/季报所属基金已不可用/);
  assert.equal(p.data.conversationId,'');assert.equal(p.data.fundReportReference,null);
  assert.ok(calls.every(c=>c.method==='GET'));
});
test('真实聊天最终结果只显示一次，并与后端会话ID关联',async()=>{
  const p=page('research');p.setData({context:2,question:'测试中文问题',conversationId:'conv_test'});
  const pending=p.send();await tick();const call=calls.find(c=>c.enableChunked);assert.equal(call.data.conversation_id,'conv_test');
  call.task.chunk(frame('answer_delta',{delta:'完整回答'}));
  const response={conversation:{id:'conv_test',title:'测试'},user_message:{id:'u1',role:'user',content:'测试中文问题'},assistant_message:{id:'a1',role:'assistant',content:'完整回答'}};
  call.task.chunk(frame('result',response));call.task.chunk(frame('result',response));await pending;
  assert.equal(p.data.partial,'');assert.equal(p.data.messages.filter(m=>m.role==='assistant').length,1);assert.equal(p.data.busy,false);
});
test('持仓进入收益风险只读，生成需确认，完成后打开同一份报告',async()=>{
  const prior=respond;respond=o=>prior(o)??(o.url.includes('/review?')?{
    portfolio:{...portfolio,holdings:[{}]},performance:{twr_pct:0},assessment:{risk_evidence:[{title:'集中风险',explanation:'样本有限',evidence_id:'risk:1'}]},gaps:[],latest_report:null
  }:o.url.includes('/research-runs')?{job:{id:'report-1'}}:o.url.includes('/background-jobs/')?{job:{id:'report-1',status:'completed',result:{portfolio_id:portfolio.id,assessment_date:'2026-09-01'},progress_percent:100}}:undefined);
  const assets=page('assets');assets.setData({current:portfolio});assets.review();assert.match(nav.at(-1),/portfolio-review/);
  const review=page('portfolio-review');review.onLoad({portfolio:portfolio.id});review._alive=true;await review.load();
  assert.equal(review.data.error,'');assert.match(review.data.metrics[0].value,/0.00/);assert.equal(review.data.metrics[1].value,'--');assert.equal(review.data.risk[0].detail,'样本有限');
  assert.ok(calls.every(c=>c.method==='GET'));
  global.wx.showModal=o=>o.success({confirm:false});await review.generate();assert.ok(calls.every(c=>c.method==='GET'));
  global.wx.showModal=o=>o.success({confirm:true});await review.generate();
  assert.equal(calls.filter(c=>c.method==='POST').length,1);assert.equal(review.data.result.latest_report.job_id,'report-1');
  review.report();assert.match(nav.at(-1),/id=report-1/);assert.match(nav.at(-1),new RegExp(`portfolio=${portfolio.id}`));
});
test('报告追问传递精确引用，第二轮保留，改研究对象后解除',async()=>{
  const prior=respond;respond=o=>prior(o)??(o.url.includes('/conversations?')?{conversations:[]}:o.url.endsWith('/funds')?{funds:[]}:undefined);
  const report=page('portfolio-report');report.setData({portfolio:portfolio.id,record:{run_id:'report-old',portfolio_id:portfolio.id,headline:'历史报告'}});report.ask();
  assert.equal(calls.length,0);
  const research=page('research');await research.load();
  assert.equal(research.data.researchReference.job_id,'report-old');assert.equal(research.data.conversationId,'');
  research.setData({conversationId:'conv-report'});
  const first=research.send();await tick();const c1=calls.find(c=>c.enableChunked);
  assert.equal(c1.data.research_job_id,'report-old');assert.equal(c1.data.portfolio_id,portfolio.id);assert.equal(c1.data.research_refresh,false);
  const ref={job_id:'report-old',portfolio_id:portfolio.id,assessment_date:'2026-09-01'};
  c1.task.chunk(frame('result',{conversation:{id:'conv-report'},user_message:{id:'u1',role:'user'},assistant_message:{id:'a1',role:'assistant',content:'有来源的回答',run_metadata:{research_reference:ref}}}));await first;
  research.setData({question:'还有什么风险'});const second=research.send();await tick();const c2=calls.filter(c=>c.enableChunked).at(-1);
  assert.equal(c2.data.research_job_id,'report-old');c2.fail({errMsg:'timeout'});await second;
  research.sourceReport();assert.match(nav.at(-1),/id=report-old/);
  research.field({currentTarget:{dataset:{field:'context'}},detail:{value:2}});assert.equal(research.data.researchReference,null);
});

function diagnosisReport(id='old',months=12){return {run_id:id,portfolio_id:portfolio.id,portfolio_name:'测试账本',period_months:months,assessment_date:'2026-09-01',
  performance_snapshot:{twr_pct:0},diagnosis:{headline:'测试结论',summary:'摘要',report_date:'2026-09-01',interpretation_label:'AI 研判',portfolio_name:'测试账本',
    findings:[{id:'f1',title:'关注点',detail:'有限证据',tone_label:'需关注',scope:'组合整体',holdings:[],gap:'',references:[{id:'e1',label:'波动',value:'0.00%',date:'2026-08-31',source:'快照',url:''}]}],
    more_findings:[],holdings:[],top_risk:null,conflicts:[],uncertainties:[],follow_ups:[],technical:[],limitations:[],questions:['为什么这样判断？']}};}
test('报告旧链接跳到独立诊断页，导航不触发模型',()=>{
  const p=page('analysis');p.onLoad({kind:'research',id:'old',portfolio:portfolio.id});
  assert.match(nav.at(-1),/pages\/portfolio-report\/index\?id=old/);assert.equal(calls.length,0);
});
test('指定历史报告优先于最新报告，展开指标与追问只是读数据和填草稿',async()=>{
  const prior=respond;respond=o=>prior(o)??(o.url.includes('/research-runs/old?')?diagnosisReport():o.url.includes('/research-runs?')?{runs:[{job_id:'latest',period_months:12},{job_id:'old',period_months:12}],latest_report:diagnosisReport('latest')}:undefined);
  const p=page('portfolio-report');p.onLoad({id:'old',portfolio:portfolio.id});p._alive=true;await p.load();
  assert.equal(p.data.error,'');assert.equal(p.data.id,'old');assert.equal(p.data.metricsOpen,false);assert.equal(p.data.blocks.length,0);
  assert.equal(p.data.findings[0].open,false);p.evidence({currentTarget:{dataset:{id:'f1',group:'main'}}});assert.equal(p.data.findings[0].open,true);
  p.toggle({currentTarget:{dataset:{field:'metricsOpen'}}});assert.ok(p.data.blocks.some(r=>r.text==='0.00%'));
  const count=calls.length;p.ask({currentTarget:{dataset:{index:0}}});assert.equal(calls.length,count);
  assert.equal(storage.get(api.storageKey('research-draft')).research_job_id,'old');assert.ok(calls.every(c=>c.method==='GET'));
});
test('指定账本不可访问时不回退到其他账本',async()=>{
  const p=page('portfolio-report');p.onLoad({id:'old',portfolio:'portfolio_0000000000000000'});p._alive=true;await p.load();
  assert.match(p.data.error,/无权访问/);assert.equal(p.data.record,null);assert.ok(!calls.some(c=>c.url.includes('research-runs')));
});
test('切换历史报告后丢弃晚到响应，读取失败可只读重试',async()=>{
  const prior=respond;respond=o=>prior(o)??(o.url.includes('/research-runs?')?{runs:[]}:undefined);
  const p=page('portfolio-report');p.onLoad({id:'old',portfolio:portfolio.id});p._alive=true;
  const old=p.load();await tick();const slow=calls.find(c=>c.url.includes('/research-runs/old?'));
  p.setData({id:'new'});const fresh=p.load();await tick();const fast=calls.find(c=>c.url.includes('/research-runs/new?'));
  fast.success({statusCode:200,data:diagnosisReport('new')});await fresh;
  slow.success({statusCode:200,data:diagnosisReport('old')});await old;assert.equal(p.data.id,'new');
  const failed=p.load();await tick();calls.at(-1).fail({errMsg:'timeout'});await failed;assert.ok(p.data.error);assert.equal(p.data.record,null);
  assert.ok(calls.every(c=>c.method==='GET'));
});
test('比较报告不覆盖原报告，取消生成不POST，离页后的比较结果不写回',async()=>{
  const prior=respond;respond=o=>prior(o)??(o.url.includes('/comparison?')?{summary:'历史变化'}:undefined);
  const p=page('portfolio-report');p._alive=true;p.setData({portfolio:portfolio.id,history:[{job_id:'target',period_months:12},{job_id:'base',period_months:12}]});p.showReport(diagnosisReport('target'));
  await p.compare();assert.equal(p.data.record.run_id,'target');assert.equal(p.data.id,'target');assert.ok(p.data.comparison.some(b=>b.text==='历史变化'));
  let submissions=0;p._tracker={pause(){},submit(){submissions++;}};wx.showModal=o=>o.success({confirm:false});await p.generate();assert.equal(submissions,0);
  wx.showModal=o=>o.success({confirm:true});respond=o=>prior(o);const pending=p.compare();await tick();const c=calls.at(-1);p.onHide();c.success({statusCode:200,data:{summary:'迟到结果'}});await pending;
  assert.ok(!p.data.comparison.some(b=>b.text==='迟到结果'));
});
test('原生模板参数保持对象，证据文本不会当成表达式执行',async()=>{
  const {templateData}=await import('../preview/bindings.mjs');
  assert.deepEqual(templateData("{{item,group:'main'}}",{item:{value:'<script>bad</script>'}}),{item:{value:'<script>bad</script>'},group:'main'});
  assert.deepEqual(templateData('{{blocks:comparison}}',{comparison:[]}),{blocks:[]});
});
test('聊天失败重试沿用幂等键，切换账号后旧答案不落入新页面',async()=>{
  const p=page('research');p.setData({context:2,question:'研究风险',conversationId:'conv_test'});
  const first=p.send();await tick();const c1=calls.find(c=>c.enableChunked);c1.fail({errMsg:'timeout'});await first;
  const second=p.send();await tick();const c2=calls.filter(c=>c.enableChunked).at(-1);assert.equal(c1.data.idempotency_key,c2.data.idempotency_key);
  api.saveConnection({baseUrl:'http://localhost:8000',userId:'bob',token:''});c2.task.chunk(frame('result',{assistant_message:{id:'old',content:'旧账号'},user_message:{id:'u'}}));await second;
  assert.equal(p.data.messages.length,0);
});
test('后台任务断网重提沿用编号，读进度失败不重新POST',async()=>{
  const updates=[],tracker=new JobTracker('test',v=>updates.push(v),()=>{});cleanup.push(()=>tracker.pause());
  const first=tracker.submit('/market/events/pipeline',{topics:['半导体']});await tick();const c1=calls.find(c=>c.method==='POST');c1.fail({errMsg:'timeout'});await first;
  const second=tracker.submit('/market/events/pipeline',{topics:['半导体']});await tick();const c2=calls.filter(c=>c.method==='POST').at(-1);assert.equal(c1.data.idempotency_key,c2.data.idempotency_key);
  c2.success({statusCode:200,data:{job:{id:'job_test'}}});await tick();calls.at(-1).fail({errMsg:'timeout'});await second;
  const retry=tracker.resume();calls.at(-1).success({statusCode:200,data:{job:{id:'job_test',status:'completed',progress_percent:100}}});await retry;
  assert.equal(calls.filter(c=>c.method==='POST').length,2);assert.ok(updates.some(v=>v.job?.status==='completed'));
});
test('人工确认任务在切页后保留，继续查询同一个任务',async()=>{
  const tracker=new JobTracker('interrupt',()=>{},()=>{});cleanup.push(()=>tracker.pause());
  const pending=tracker.follow({id:'j'},'alice');calls.at(-1).success({statusCode:200,data:{job:{id:'j',status:'waiting_input',progress_percent:20,result:{status:'waiting_input'}}}});await pending;
  assert.equal(storage.get(api.storageKey('job:interrupt')).id,'j');tracker.pause();
  const again=tracker.resume();calls.at(-1).success({statusCode:200,data:{job:{id:'j',status:'waiting_input',progress_percent:20}}});await again;assert.ok(calls.every(c=>c.method==='GET'));
});
test('社区风控暂停时不发采集请求',async()=>{
  const p=page('community');p.setData({status:{collection_allowed:false,verification_required:true}});await p.collect();assert.equal(calls.length,0);
});
test('新闻搜索只读已保存记录，不隐式采集或调用模型',async()=>{
  const p=page('intelligence');p.setData({query:'半导体',dateFrom:'2026-09-01',dateTo:'2026-09-10'});
  respond=o=>o.url.includes('/market/events?')?{events:[],total_matched:0,data_cutoff:'2026-09-10'}:undefined;
  await p.load();assert.equal(calls.length,1);assert.equal(calls[0].method,'GET');assert.match(calls[0].url,/date_from=2026-09-01/);assert.match(calls[0].url,/q=%/);
});
test('主题雷达与网页同义词一致，缺失资金不补零',()=>{
  page('sectors');const {sectorRows}=require('../dist/pages/sectors/index');
  const rows=sectorRows({topic_signals:[{topic_kind:'sector',topic_label:'医药医疗'}]},{hotspots:[{themes:['医药'],title:'真实事件'}]},['医药']);
  assert.equal(rows.length,1);assert.equal(rows[0].events.length,1);assert.match(rows[0].summary,/--/);
});
test('未知值、0值、持仓覆盖与来源链接在报告中明确保留',()=>{
  const b=reportBlocks({disclosure:{security_holdings:[{security_name:'测试证券',weight_pct:0}],security_coverage_pct:null,source_url:'https://example.com/source'},secret_token:'不应显示'});
  assert.ok(b.some(r=>r.text==='0.00%'));assert.ok(b.some(r=>r.text==='--'));assert.ok(b.some(r=>r.url==='https://example.com/source'));assert.ok(!JSON.stringify(b).includes('不应显示'));
});
test('报告重复叙述只展示一次，不去掉不同口径的数值',()=>{
  const b=reportBlocks({headline:'真实结论',period_return_pct:0,model_interpretation:{headline:'真实结论',confidence:0},months:12});
  assert.equal(b.filter(r=>r.text==='真实结论').length,1);assert.ok(b.some(r=>r.text==='0.00%'));assert.ok(b.some(r=>r.label==='置信度'&&r.text==='0.00'));assert.ok(b.some(r=>r.text==='12'));
});
test('操作时间按北京时间显示，未知时区不猜测',()=>{
  assert.equal(chinaTime('2026-09-04T07:01:00Z'),'2026-09-04 15:01 北京时间');
  assert.equal(chinaTime('2026-09-04T15:01:00+08:00'),'2026-09-04 15:01 北京时间');
  assert.equal(chinaTime('2026-09-04'),'2026-09-04');assert.equal(chinaTime(null),'--');
});
test('新闻基金/类型/来源筛选与成交额窗口只读取指定快照',async()=>{
  const news=page('intelligence');news.setData({fundCode:'110020',eventType:1,sourceLevel:2});respond=()=>({events:[]});await news.load();
  assert.match(calls[0].url,/fund_code=110020/);assert.match(calls[0].url,/event_type=policy/);assert.match(calls[0].url,/source_level=B/);
  const report=page('analysis');report.setData({turnoverSeries:[{series_id:'index:sh000688',label:'科创50'}],turnoverPeriod:0});assert.equal(report.turnoverQuery(),'?series_id=index%3Ash000688&period=30d');
  assert.ok(calls.every(c=>c.method==='GET'));
});
test('约束必须显式确认，空数值或不支持条件不能自动求解',()=>{
  const p=page('analysis');p.setData({kind:'optimizer'});p.showReport({question:'债券最低40%',sample:{},proposal:{min_bond_weight_pct:40,max_single_fund_weight_pct:60,max_turnover_pct:20,unsupported_constraints:[]}});
  assert.equal(p.data.canSolve,false);p.setData({ack:true});p.validateConstraints();assert.equal(p.data.canSolve,true);
  p.constraint({currentTarget:{dataset:{key:'max_turnover_pct'}},detail:{value:''}});assert.equal(p.data.canSolve,false);
  p.setData({ack:true,question:'修改了需求'});p.validateConstraints();assert.equal(p.data.canSolve,false);
});
test('压力候选不能用空值凑100%，非法输入不请求计算',async()=>{
  const p=page('analysis');p.setData({kind:'stress',portfolio:portfolio.id,weights:[{code:'110020',value:''}],record:{run_id:'stress_1234567890abcdef'}});
  await p.act({currentTarget:{dataset:{action:'weights'}}});assert.match(p.data.error,/非负数且合计100/);assert.ok(calls.every(c=>c.method==='GET'));
});
test('各分析页只读入口不POST，未生成压力/草案不会自动计算',async()=>{
  const {definitions}=require('../dist/pages/analysis/index');
  const prior=respond;respond=o=>prior(o)??(o.url.includes('stress-runs')||o.url.includes('allocation-drafts')?[]:o.url.includes('decisions')?{decisions:[]}:o.url.includes('research-runs')?{runs:[],latest_report:null}:{summary:'测试报告'});
  for(const kind of ['risk','performance','events','stress','optimizer','decisions','fund-risk','fund-holdings','capital','retail','turnover']){
    const p=page('analysis');p.onLoad({kind,id:kind.startsWith('fund-')?'110020':'',portfolio:portfolio.id});await p.load();assert.equal(p.data.error,'',kind);assert.equal(p.data.needsPortfolio,!!definitions[kind].portfolio);
  }
  assert.ok(calls.every(c=>c.method==='GET'));
});
test('全部新页面的初始/空数据模板都能读取，不出现null属性导致白屏',async()=>{
  const {binding}=await import('../preview/bindings.mjs');
  function checkChildren(parent,data){let matched=false;for(const node of parent.childNodes){if(node.nodeType===3){binding(node.textContent,data);continue;}if(node.nodeType!==1)continue;
    if(node.hasAttribute('wx:if')){matched=!!binding(node.getAttribute('wx:if'),data);if(!matched)continue;}
    else if(node.hasAttribute('wx:elif')){const show=!matched&&!!binding(node.getAttribute('wx:elif'),data);matched||=show;if(!show)continue;}
    else if(node.hasAttribute('wx:else')){if(matched)continue;matched=true;}else matched=false;
    const rows=node.hasAttribute('wx:for')?binding(node.getAttribute('wx:for'),data):null;
    function check(d){for(const attr of node.attributes)if(!['wx:if','wx:elif','wx:for'].includes(attr.name))binding(attr.value,d);checkChildren(node,d);}
    if(rows)rows.forEach((item,index)=>check({...data,[node.getAttribute('wx:for-item')||'item']:item,index}));else if(!node.hasAttribute('wx:for'))check(data);
  }}
  for(const name of ['intelligence','community','sectors','research','analysis','transactions','portfolio-review','portfolio-report']){
    const p=page(name),src=fs.readFileSync(`src/pages/${name}/index.wxml`,'utf8').replace(/<(image|ec-canvas|switch|textarea)\b([^>]*?)\/>/g,'<$1$2></$1>');
    const root=new JSDOM(src).window.document.body;
    const kinds=name==='analysis'?Object.keys(require('../dist/pages/analysis/index').definitions):[''];
    for(const kind of kinds){if(kind)p.setData({kind,needsPortfolio:!!require('../dist/pages/analysis/index').definitions[kind].portfolio});assert.doesNotThrow(()=>checkChildren(root,p.data),`${name}:${kind}`);}
    if(name==='transactions'){
      p.setData({loading:false,locked:false,trades:[{id:'old',timing:null}]});assert.doesNotThrow(()=>checkChildren(root,p.data));
      p.setData({timing:{status:'estimated',nav_date:'2025-01-06',confirmation_date:'2025-01-07',first_return_nav_date:'2025-01-07',last_return_nav_date:null,reason:'假期顺延',visibility:'等待净值披露',warnings:['不保证盈利']}});assert.doesNotThrow(()=>checkChildren(root,p.data));
    }
  }
});
