/** 季报独立页：只读保存的资料，显式同步才联网；两期编号随草稿进入现有聊天。 */
import {request, scope, errorText} from '../../lib/api';
import {researchDraft, copySource} from '../../lib/navigation';
import {JobTracker, JobState} from '../../lib/jobs';

Page({
  data:{code:'',loading:false,error:'',documents:[] as any[],labels:[] as string[],left:0,right:1,
    comparison:null as any,changes:[] as any[],page:null as any,pageNumber:1,view:'compare',document:0,
    job:{busy:false,message:'',error:'',job:null} as JobState,warnings:[] as string[]},
  _version:0,_alive:false,_scope:'',_tracker:null as JobTracker|null,_pinned:[] as string[],
  onLoad(query:Record<string,string>){this.setData({code:query.code||''});this._pinned=(query.ids||'').split(',').filter(Boolean);},
  onShow(){this._alive=true;this._scope=scope();void this.load();},
  onHide(){this._alive=false;this._version++;this._tracker?.pause();this.setData({loading:false});},
  onUnload(){this.onHide();},
  active(version:number){return this._alive&&version===this._version&&scope()===this._scope;},
  async load(){
    const version=++this._version;this._tracker?.pause();this.setData({loading:true,error:'',page:null,view:'compare'});
    try{
      const query=this._pinned.length?'?'+this._pinned.map(id=>`document_ids=${encodeURIComponent(id)}`).join('&'):'';
      const data=await request<any>(`/funds/${this.data.code}/reports${query}`);if(!this.active(version))return;
      const ids=this._pinned.length?this._pinned:data.selected_ids;
      const left=data.documents.findIndex((d:any)=>d.id===ids[0]),right=data.documents.findIndex((d:any)=>d.id===ids[1]);
      this.setData({documents:data.documents,labels:data.documents.map((d:any)=>`${d.period} · ${d.published_at}`),
        left:Math.max(0,left),right:Math.max(0,right),comparison:null,changes:[]});
      if(ids.length===2){if(left<0||right<0)throw new Error('引用版本不在当前列表，请返回研究对话查看原始来源');await this.compare(version);}
      if(!this.active(version))return;
      this._tracker=new JobTracker(`fund-reports:${this.data.code}`,state=>{
        if(this.active(version))this.setData({job:{...this.data.job,...state}});
      },job=>{if(this.active(version)){this.setData({warnings:((job as any).result?.warnings||[])});void this.load();}});
    }catch(error){if(this.active(version))this.setData({error:errorText(error)});}
    finally{if(this.active(version)){this.setData({loading:false});void this._tracker?.resume();}}
  },
  async compare(version:number){
    const docs=[this.data.documents[this.data.left],this.data.documents[this.data.right]];
    if(docs.some(d=>!d)||docs[0].period===docs[1].period)throw new Error('请选择两个不同季度');
    const query=docs.map(d=>`document_ids=${encodeURIComponent(d.id)}`).join('&');
    const comparison=await request<any>(`/funds/${this.data.code}/reports/compare?${query}`);
    if(this.active(version))this.setData({comparison,changes:comparison.allocation_changes.map((r:any)=>({...r,
      beforeText:r.before?`${r.before.ratio_percent}%`:'未提取',afterText:r.after?`${r.after.ratio_percent}%`:'未提取',
      changeText:r.change_percentage_points===null?'不可计算':`${Number(r.change_percentage_points)>0?'+':''}${r.change_percentage_points} 个百分点`}))});
  },
  async select(e:WechatMiniprogram.CustomEvent){
    if(this.data.loading||this.data.job.busy)return;
    this.setData({[e.currentTarget.dataset.field]:Number(e.detail.value),comparison:null,error:'',loading:true});
    const version=this._version;
    try{await this.compare(version);}catch(error){if(this.active(version))this.setData({error:errorText(error)});}
    finally{if(this.active(version))this.setData({loading:false});}
  },
  sync(){if(!this.data.loading&&!this.data.job.busy){this._pinned=[];void this._tracker?.submit(`/funds/${this.data.code}/reports/sync`);}},
  progress(){void this._tracker?.resume();},
  ask(){const d=this.data.comparison;if(!d)return;
    researchDraft(`请对比 ${this.data.code} 的 ${d.documents[0].period} 和 ${d.documents[1].period} 季报：资产配置变化、基金经理观点变化、主要风险。引用原文页码，区分披露事实与推断，不重新采集。`,
      {fund_code:this.data.code,fund_report_ids:d.documents.map((doc:any)=>doc.id)});
  },
  async openPage(e:WechatMiniprogram.TouchEvent){this.setData({document:Number(e.currentTarget.dataset.document),view:'source',pageNumber:Number(e.currentTarget.dataset.page||1)});await this.readPage();},
  async readPage(){const version=this._version;this.setData({loading:true,error:'',page:null});
    try{const doc=this.data.comparison.documents[this.data.document];
      const page=await request<any>(`/funds/${this.data.code}/reports/${doc.id}/pages/${this.data.pageNumber}`);
      if(this.active(version))this.setData({page});
    }catch(error){if(this.active(version))this.setData({error:errorText(error)});}
    finally{if(this.active(version))this.setData({loading:false});}
  },
  turn(e:WechatMiniprogram.TouchEvent){if(this.data.loading)return;this.setData({pageNumber:this.data.pageNumber+Number(e.currentTarget.dataset.step)});void this.readPage();},
  back(){this.setData({view:'compare',error:''});},
  source(){if(this.data.page)copySource(this.data.page.source_url);},
});
