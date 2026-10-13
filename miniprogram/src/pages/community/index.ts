/** 社区复用已保存正文、图片和作者去重结果。风控暂停不能由前端绕过。 */
import { request, scope, storageKey, errorText, confirm } from '../../lib/api';
import { settings, analysis } from '../../lib/navigation';
import { JobTracker, JobState } from '../../lib/jobs';
import { reportBlocks, Block, stateLabel } from '../../lib/reports';
import { chinaTime } from '../../lib/format';
Page({
  data:{segment:'topics',segments:[{value:'topics',label:'话题'},{value:'samples',label:'原始样本'},{value:'review',label:'观点复核'}],
    loading:false,error:'',info:'',rows:[] as any[],blocks:[] as Block[],status:null as any,
    sampleScope:0,scopes:['当前操作窗口','全部已保存样本'],page:1,total:0,
    job:{busy:false,message:'',error:'',job:null} as JobState},
  _version:0,_scope:'',_tracker:null as JobTracker|null,
  onShow() {
    if(this._scope!==scope()) {this._tracker?.pause();this._scope=scope();this.setData({rows:[],blocks:[],status:null,job:{busy:false,message:'',error:'',job:null}});}
    this._tracker=new JobTracker('community',v=>this.setData({job:{...this.data.job,...v}}),()=>{void this.load();});
    void this._tracker.resume(); void this.load();
  },
  onHide(){this._version++;this._tracker?.pause();this.setData({loading:false});}, onUnload(){this.onHide();},
  onPullDownRefresh(){void this.load().finally(()=>wx.stopPullDownRefresh());}, settings,
  change(e:WechatMiniprogram.TouchEvent){this._version++;this.setData({segment:e.currentTarget.dataset.value,rows:[],blocks:[],page:1,loading:false});void this.load();},
  scopeChange(e:WechatMiniprogram.CustomEvent){this.setData({sampleScope:Number(e.detail.value),page:1});void this.load();},
  async load(){
    const version=++this._version,account=scope();this.setData({loading:true,error:''});
    try{
      const result=await request<any>('/market/social-signals');
      let rows=result.aggregates, review=result.review,total=0;
      if(this.data.segment!=='topics'){
        const posts=await request<any>(`/market/social-signals/posts?scope=${this.data.sampleScope?'all':'window'}&page=${this.data.page}&page_size=10`);
        rows=posts.posts;review=posts.review;total=posts.total;
      }
      if(version!==this._version||account!==scope())return;
      this.setData({status:result.status,total,blocks:reportBlocks(review),
        rows:(rows||[]).map((r:any)=>({...r,id:r.topic_key||r.post_id,title:r.topic_label||r.title,
          meta:r.topic_key?`${r.author_count}位作者 · ${r.sample_count}条样本 · ${r.positioning_label}`:`${r.author_name} · ${chinaTime(r.published_at)} · ${stateLabel(r.analysis_status)}`})),
        info:`${result.operation_window?.label||''} · 当前窗口保存 ${result.sample_count} 条，${result.accepted_sample_count} 条纳入操作统计。${this.data.segment!=='topics'&&this.data.sampleScope?`全部历史样本共 ${total} 条。`:''}${result.disclaimer||''}`});
    }catch(error){if(version===this._version&&account===scope())this.setData({error:errorText(error)});}
    finally{if(version===this._version&&account===scope())this.setData({loading:false});}
  },
  page(e:WechatMiniprogram.TouchEvent){this.setData({page:Math.max(1,this.data.page+Number(e.currentTarget.dataset.delta))});void this.load();},
  detail(e:WechatMiniprogram.TouchEvent){const row=this.data.rows.find(r=>r.id===e.currentTarget.dataset.id);if(!row)return;const kind=this.data.segment==='topics'?'topic':'post';wx.setStorageSync(storageKey('detail'),{kind,id:row.id,value:row});analysis(kind,row.id);},
  source(e:WechatMiniprogram.TouchEvent){const url=e.currentTarget.dataset.url;if(/^https?:\/\//.test(url))wx.setClipboardData({data:url});},
  async collect(){
    if(this.data.job.busy||!this.data.status?.collection_allowed)return;
    if(!await confirm('采集社区样本','将在后端电脑使用已有登录会话采集。遇到平台验证即暂停，不自动重试。继续？'))return;
    await this._tracker?.submit('/market/social-signals/data-sync');
  },
  async review(){if(this.data.job.busy||!await confirm('分析已保存样本','将调用模型处理已保存的正文和图片识别结果，可能产生费用；不会重新采集帖子。'))return;await this._tracker?.submit('/market/social-signals/analysis',{scope:this.data.sampleScope?'all':'window'});},
  checkJob(){void this._tracker?.resume();},
  config(){analysis('social-settings');},
});
