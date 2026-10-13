/** 持仓研究主线：只读收益风险 -> 用户确认生成报告 -> 打开同一份报告。 */
import {request, identity, queryUser, scope, errorText, confirm} from '../../lib/api';
import {analysis} from '../../lib/navigation';
import {number, signed} from '../../lib/format';
import {JobTracker, JobState} from '../../lib/jobs';

Page({
  data: {portfolio:'', name:'', loading:false, error:'', months:12, options:[6,12,36], periodLabels:['6个月','1年','3年'], index:1,
    result:null as any, metrics:[] as any[], warnings:[] as string[], risk:[] as any[],
    job:{busy:false,message:'',error:'',job:null} as JobState},
  _version:0, _alive:false, _confirming:false, _tracker:null as JobTracker|null,
  onLoad(query:Record<string,string>) { this.setData({portfolio:query.portfolio || ''}); },
  onShow() { this._alive=true; void this.load(); },
  onHide() { this._alive=false; this._version++; this._tracker?.pause(); this.setData({loading:false}); },
  onUnload() { this.onHide(); },
  async load() {
    const version=++this._version, account=scope(), portfolio=this.data.portfolio;
    this._tracker?.pause(); this._tracker=null;
    this.setData({loading:true,error:'',result:null,metrics:[],risk:[],warnings:[],name:''});
    const active=()=>this._alive && version===this._version && account===scope();
    try {
      if (!/^portfolio_[a-f0-9]{16}$/.test(portfolio)) throw new Error('请从持仓页选择账本后进入');
      const user=await identity(); if(!active())return;
      const result=await request<any>(`/portfolios/${portfolio}/review${queryUser(user)}&months=${this.data.months}`);
      if(!active())return;
      const p=result.performance;
      this.setData({result,name:result.portfolio.name,
        metrics:[{label:'区间收益（TWR）',value:signed(p?.twr_pct,'%')},
          {label:'历史最大回撤',value:signed(p?.max_drawdown?.drawdown_pct,'%')},
          {label:'年化波动',value:p?.annualized_volatility_pct==null?'--':`${number(p.annualized_volatility_pct)}%`}],
        warnings:p?.warnings || [],risk:(result.assessment?.risk_evidence || []).slice(0,3).map((e:any)=>({
          id:e.evidence_id,title:e.title||e.label||e.evidence_id,detail:e.explanation||e.detail||e.reason||e.description||''}))});
      // 与原报告页共用 Job 身份。切页只暂停读取，不再生成一个新任务。
      this._tracker=new JobTracker(`analysis:research:${portfolio}`,state=>{if(active())this.setData({job:{...this.data.job,...state}});},job=>{
        if(!active())return;
        if(job.result?.portfolio_id!==portfolio){this.setData({error:'研究结果不属于当前账本'});return;}
        this.setData({result:{...this.data.result,latest_report:{job_id:job.id,created_at:job.result.assessment_date}}});
      });
      await this._tracker.resume();
    } catch(error) { if(active())this.setData({error:errorText(error)}); }
    finally { if(active())this.setData({loading:false}); }
  },
  period(e:WechatMiniprogram.CustomEvent) {this.setData({index:Number(e.detail.value),months:this.data.options[Number(e.detail.value)]});void this.load();},
  detail(e:WechatMiniprogram.TouchEvent) {analysis(e.currentTarget.dataset.kind,'',this.data.portfolio);},
  async generate() {
    if(this.data.loading||this.data.job.busy||this._confirming||!this._tracker)return;
    const tracker=this._tracker, account=scope(), version=this._version;
    // 确认弹窗出现期间也拦住连点；只有用户确认后才交给幂等 Job 提交器。
    this._confirming=true;
    let accepted=false;
    try {accepted=await confirm('分析当前配置','使用已保存数据生成配置报告，将调用模型。不会采集新数据或改变实际持仓。继续？');}
    finally {this._confirming=false;}
    if(!accepted)return;
    if(!this._alive||version!==this._version||account!==scope())return;
    await tracker.submit(`/portfolios/${this.data.portfolio}/research-runs`,{months:this.data.months});
  },
  report() {const id=this.data.result?.latest_report?.job_id;if(id)analysis('research',id,this.data.portfolio);},
  progress() {void this._tracker?.resume();},
  holdings() {wx.switchTab({url:'/pages/assets/index'});},
});
