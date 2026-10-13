/** 持仓诊断的独立页面：展示冻结报告，不把研究、比较、追问混成一份可变记录。 */
import {request, identity, queryUser, scope, storageKey, errorText, confirm, connection, Portfolio} from '../../lib/api';
import {analysis, researchDraft, copySource} from '../../lib/navigation';
import {reportBlocks, Block} from '../../lib/reports';
import {JobTracker, JobState} from '../../lib/jobs';

const emptyJob=():JobState=>({busy:false,message:'',error:'',job:null});
Page({
  data:{portfolio:'',portfolios:[] as Portfolio[],selected:0,id:'',months:12,periods:[6,12,36],periodLabels:['6个月','1年','3年'],periodIndex:1,
    loading:false,busy:false,error:'',record:null as any,diagnosis:null as any,history:[] as any[],historyNotice:'',canCompare:false,
    findings:[] as any[],moreFindings:[] as any[],moreOpen:false,holdingsOpen:false,metricsOpen:false,historyOpen:false,technicalOpen:false,
    blocks:[] as Block[],comparison:[] as Block[],job:emptyJob()},
  _version:0,_alive:false,_scope:'',_followJob:true,_tracker:null as JobTracker|null,
  onLoad(query:Record<string,string>){this.setData({portfolio:query.portfolio||'',id:query.id||''});this._followJob=!query.id;},
  onShow(){
    this._alive=true;
    if(this._scope&&this._scope!==scope()){this.setData({portfolio:'',portfolios:[],selected:0,id:'',record:null,diagnosis:null,history:[],comparison:[],job:emptyJob()});this._followJob=true;}
    this._scope=scope();void this.load();
  },
  onHide(){this._alive=false;this._version++;this._tracker?.pause();this.setData({loading:false,busy:false});},
  onUnload(){this.onHide();},
  active(version:number,account:string){return this._alive&&version===this._version&&account===scope();},
  showReport(report:any){
    if(report.portfolio_id!==this.data.portfolio||!report.diagnosis)throw new Error('报告与当前账本不符或缺少展示数据，请重读报告');
    // 展示使用报告自己的日期和样本区间，而不是此刻的持仓或页面默认值。
    const d=report.diagnosis,samePeriod=this.data.history.filter(r=>r.period_months===report.period_months);
    const index=samePeriod.findIndex(r=>r.job_id===report.run_id);
    this.setData({record:report,diagnosis:d,id:report.run_id,months:report.period_months,
      periodIndex:Math.max(0,this.data.periods.indexOf(report.period_months)),
      findings:d.findings.map((r:any)=>({...r,open:false})),moreFindings:d.more_findings.map((r:any)=>({...r,open:false})),
      canCompare:index>=0&&index<samePeriod.length-1,
      moreOpen:false,holdingsOpen:false,metricsOpen:false,technicalOpen:false,blocks:[],comparison:[]});
  },
  async load(){
    const version=++this._version,account=scope();this._tracker?.pause();this._tracker=null;
    this.setData({loading:true,busy:false,error:'',record:null,diagnosis:null,history:[],historyNotice:'',comparison:[],job:emptyJob()});
    try{
      const user=await identity();if(!this.active(version,account))return;
      const {portfolios}=await request<{portfolios:Portfolio[]}>(`/portfolios${queryUser(user)}`);
      if(!this.active(version,account))return;
      const requested=this.data.portfolio,preferred=requested||wx.getStorageSync(storageKey('portfolio'));
      let selected=portfolios.findIndex(p=>p.id===preferred);
      // 指定了账本却不可访问时，不能偷偷改成列表里的第一本。
      if(requested&&selected<0)throw new Error('该账本不存在或当前账号无权访问');
      if(selected<0)selected=0;
      const portfolio=portfolios[selected]?.id||'';
      this.setData({portfolios,selected,portfolio});if(!portfolio)return;
      const history=await request<any>(`/portfolios/${portfolio}/research-runs${queryUser(user)}&limit=20`);
      if(!this.active(version,account))return;
      this.setData({history:history.runs||[],historyNotice:history.message||''});
      if(this.data.id)await this.readReport(this.data.id,user,version,account);
      else if(history.latest_report)this.showReport(history.latest_report);
      if(!this.active(version,account))return;
      this._tracker=new JobTracker(`analysis:research:${portfolio}`,state=>{
        if(this.active(version,account))this.setData({job:{...this.data.job,...state}});
      },job=>{
        if(!this.active(version,account)||!this._followJob)return;
        // 不直接展示 Job 中的原始 JSON；详情接口会验证归属和冻结数据，再提供诊断结构。
        this.setData({id:job.id});void this.load();
      });
    }catch(error){if(this.active(version,account))this.setData({error:errorText(error)});}
    finally{
      if(this.active(version,account)){
        this.setData({loading:false});
        if(this._followJob)void this._tracker?.resume();
      }
    }
  },
  async readReport(id:string,user:string,version:number,account:string){
    const report=await request<any>(`/portfolios/${this.data.portfolio}/research-runs/${encodeURIComponent(id)}${queryUser(user)}`);
    if(this.active(version,account))this.showReport(report);
  },
  choose(e:WechatMiniprogram.CustomEvent){
    this._followJob=true;this.setData({selected:Number(e.detail.value),portfolio:this.data.portfolios[Number(e.detail.value)].id,id:'',historyOpen:false});void this.load();
  },
  period(e:WechatMiniprogram.CustomEvent){this.setData({periodIndex:Number(e.detail.value),months:this.data.periods[Number(e.detail.value)]});},
  async selectReport(e:WechatMiniprogram.TouchEvent){
    if(this.data.busy||this.data.loading)return;
    this._followJob=false;this._tracker?.pause();this.setData({id:e.currentTarget.dataset.id,historyOpen:false});await this.load();
  },
  toggle(e:WechatMiniprogram.TouchEvent){
    const field=e.currentTarget.dataset.field;
    if(!['moreOpen','holdingsOpen','metricsOpen','historyOpen','technicalOpen'].includes(field))return;
    // 完整指标只在用户需要时铺开，首屏不再逐字段打印所有报告对象。
    if(field==='metricsOpen'&&!this.data.metricsOpen){
      const r=this.data.record||{};
      this.setData({blocks:reportBlocks({performance_snapshot:r.performance_snapshot,quant_risk_snapshot:r.quant_risk_snapshot,
        assessment_snapshot:r.assessment_snapshot,market_snapshot:r.market_snapshot,decision_history:r.decision_history})});
    }
    this.setData({[field]:!this.data[field as keyof typeof this.data]});
  },
  evidence(e:WechatMiniprogram.TouchEvent){
    const field=e.currentTarget.dataset.group==='more'?'moreFindings':'findings',id=e.currentTarget.dataset.id;
    this.setData({[field]:this.data[field].map(r=>r.id===id?{...r,open:!r.open}:r)});
  },
  async generate(){
    if(this.data.busy||this.data.loading||this.data.job.busy||!this._tracker)return;
    const version=this._version,account=scope(),tracker=this._tracker;this.setData({busy:true,error:''});
    try{
      if(!await confirm('分析当前配置','使用已保存的数据生成新报告，将调用模型。不自动采集新数据，也不修改持仓。继续？'))return;
      if(!this.active(version,account))return;
      this._followJob=true;
      await tracker.submit(`/portfolios/${this.data.portfolio}/research-runs`,{months:this.data.months});
    }catch(error){if(this.active(version,account))this.setData({error:errorText(error)});}
    finally{if(this.active(version,account))this.setData({busy:false});}
  },
  async compare(){
    if(this.data.busy||this.data.loading||!this.data.canCompare)return;
    const version=this._version,account=scope(),id=this.data.id;this.setData({busy:true,error:''});
    try{
      if(!await confirm('比较历史报告','使用已保存报告解释变化，可能调用模型。不会改变持仓。继续？'))return;
      if(!this.active(version,account))return;
      const user=await identity();if(!this.active(version,account))return;
      const result=await request<any>(`/portfolios/${this.data.portfolio}/research-runs/${encodeURIComponent(id)}/comparison${queryUser(user)}`,'GET',undefined,connection(),180000);
      // 比较结果是辅助区块，不覆盖正在追问的原报告和它的编号。
      if(this.active(version,account))this.setData({comparison:reportBlocks(result),historyOpen:true});
    }catch(error){if(this.active(version,account))this.setData({error:errorText(error)});}
    finally{if(this.active(version,account))this.setData({busy:false});}
  },
  ask(e?:WechatMiniprogram.TouchEvent){
    const r=this.data.record;if(!r?.run_id||this.data.loading)return;
    const index=e?.currentTarget.dataset.index;
    const question=index==null?'这份配置报告中，最值得关注的风险是什么？请结合引用的证据解释。':r.diagnosis.questions[Number(index)];
    if(question)researchDraft(question,{portfolio_id:r.portfolio_id,research_job_id:r.run_id,report_label:`${r.portfolio_name} · ${r.assessment_date}`});
  },
  adjust(){if(this.data.record?.run_id)analysis('adjustment',this.data.record.run_id,this.data.portfolio);},
  review(){if(this.data.portfolio)wx.navigateTo({url:`/pages/portfolio-review/index?portfolio=${encodeURIComponent(this.data.portfolio)}`});},
  source(e:WechatMiniprogram.TouchEvent){copySource(e.currentTarget.dataset.url);},
  progress(){this._followJob=true;void this._tracker?.resume();},
});
