/** 新闻与热点是不同视图：检索已保存报道，采集和模型处理需单独点击。 */
import { request, scope, storageKey, errorText, confirm } from '../../lib/api';
import { settings, analysis } from '../../lib/navigation';
import { JobTracker, JobState } from '../../lib/jobs';
import { stateLabel } from '../../lib/reports';
Page({
  data: { segment:'events', query:'', dateFrom:'', dateTo:'', loading:false, error:'', info:'', rows:[] as any[],
    topics:[{topic:'全部板块'}], topic:0, expansion:true, limit:20, total:0, showFilters:false, showCollection:false,
    fundCode:'', eventType:0, sourceLevel:0, types:['全部类型','政策','经济数据','监管','基金公告','市场通知','社媒操作'], levels:['全部来源','A','B','C','D'],
    job:{busy:false,message:'',error:'',job:null} as JobState },
  _version:0, _scope:'', _tracker:null as JobTracker | null,
  onShow() {
    if (this._scope !== scope()) { this._tracker?.pause(); this._scope = scope(); this.setData({rows:[],error:'',info:'',job:{busy:false,message:'',error:'',job:null}}); }
    this._tracker = new JobTracker('intelligence', value => this.setData({job:{...this.data.job,...value}}), () => { void this.load(); });
    void this._tracker.resume(); void this.load();
    const current = this._scope;
    void request<{topics:{topic:string}[]}>('/market/events/topics').then(r => { if (scope() === current) this.setData({topics:[{topic:'全部板块'},...r.topics]}); }).catch(() => {});
  },
  onHide() { this._version++; this._tracker?.pause(); this.setData({loading:false}); },
  onUnload() { this.onHide(); },
  onPullDownRefresh() { void this.load().finally(() => wx.stopPullDownRefresh()); },
  settings,
  field(e:WechatMiniprogram.CustomEvent) { this.setData({[e.currentTarget.dataset.field]:e.detail.value}); },
  toggleFilters(){this.setData({showFilters:!this.data.showFilters});},
  toggleCollection(){this.setData({showCollection:!this.data.showCollection});},
  clearFilters(){this.setData({query:'',fundCode:'',dateFrom:'',dateTo:'',eventType:0,sourceLevel:0});void this.load();},
  change(e:WechatMiniprogram.TouchEvent) { this._version++; this.setData({segment:e.currentTarget.dataset.value,rows:[],loading:false,limit:20}); void this.load(); },
  async load() {
    const version = ++this._version, account = scope();
    if (this.data.dateFrom && this.data.dateTo && this.data.dateFrom > this.data.dateTo) { this.setData({error:'开始日期不能晚于结束日期'}); return; }
    if(this.data.segment==='events'&&this.data.fundCode&&!/^\d{6}$/.test(this.data.fundCode)){this.setData({error:'关联基金请填写六位代码'});return;}
    this.setData({loading:true,error:''});
    try {
      const params = [`q=${encodeURIComponent(this.data.query.trim())}`,`limit=${this.data.segment === 'trends' ? Math.min(30,this.data.limit) : this.data.limit}`];
      if (this.data.segment === 'events') {
        if (this.data.dateFrom) params.push(`date_from=${this.data.dateFrom}`);
        if (this.data.dateTo) params.push(`date_to=${this.data.dateTo}`);
        // 这些是数据库检索条件，不会悄悄变成外部采集请求。
        if(this.data.fundCode)params.push(`fund_code=${this.data.fundCode}`);
        if(Number(this.data.eventType))params.push(`event_type=${['','policy','economic_data','regulation','fund_announcement','market_notice','social_positioning'][Number(this.data.eventType)]}`);
        if(Number(this.data.sourceLevel))params.push(`source_level=${this.data.levels[Number(this.data.sourceLevel)]}`);
      }
      const result = await request<any>(`/market/${this.data.segment === 'events' ? 'events' : 'hotspots'}?${params.join('&')}`);
      if (version !== this._version || account !== scope()) return;
      const rows = (result.events || result.hotspots || []).map((r:any) => ({...r,id:r.event_id || r.cluster_id,
        meta:this.data.segment === 'events' ? `${r.event_date} · ${r.source_name} · ${r.source_level}级来源 · ${stateLabel(r.freshness_status)}` : `${r.lifecycle_label} · 热度 ${r.heat_score} · ${r.source_count}个来源`,
        ai:r.semantic_analysis?.reason || ''}));
      this.setData({rows,total:result.total_matched || 0, info:`数据截至 ${result.data_cutoff || '暂无'}${result.data_mode === 'fixture' ? ' · 教学样本' : ''} · ${result.disclaimer || ''}`});
    } catch(error) { if (version === this._version && account === scope()) this.setData({error:errorText(error)}); }
    finally { if (version === this._version && account === scope()) this.setData({loading:false}); }
  },
  more() { this.setData({limit:50}); void this.load(); },
  detail(e:WechatMiniprogram.TouchEvent) {
    const row = this.data.rows.find(r => r.id === e.currentTarget.dataset.id); if (!row) return;
    const kind = this.data.segment === 'events' ? 'event' : 'hotspot';
    wx.setStorageSync(storageKey('detail'), {kind,id:row.id,value:row}); analysis(kind,row.id);
  },
  async refresh() {
    if (this.data.job.busy || !await confirm('获取最新情报', '将采集公开资讯并调用模型分析、聚类，可能产生模型费用。继续？')) return;
    const topic = this.data.topics[this.data.topic]?.topic;
    await this._tracker?.submit('/market/events/pipeline', {topics:this.data.topic && topic ? [topic] : [],use_model_topic_expansion:this.data.expansion});
  },
  async processSaved(e:WechatMiniprogram.TouchEvent){
    const action=e.currentTarget.dataset.action;
    if(!['analyze','cluster'].includes(action)||this.data.job.busy)return;
    if(!await confirm('处理已保存资讯',action==='analyze'?'仅调用模型分析数据库中待处理资讯，不重新采集。':'仅为已保存资讯生成向量并聚类，不重新采集。'))return;
    await this._tracker?.submit(`/market/events/${action}`);
  },
  checkJob() { void this._tracker?.resume(); },
});
