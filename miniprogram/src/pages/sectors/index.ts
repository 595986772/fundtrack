/** 与网页版同口径：标准主题关联热点，ETF样本净申购不冒充全板块资金。 */
import { request, identity, queryUser, scope, storageKey, errorText } from '../../lib/api';
import { settings, analysis } from '../../lib/navigation';
import { amount } from '../../lib/format';
export function sectorRows(capital:any,hotspots:any,topics:string[]=[]){
  const canonical=(s:string)=>({'医药医疗':'医药','大金融':'金融'} as Record<string,string>)[s]||s;
  const signals=(capital?.topic_signals||[]).filter((r:any)=>r.topic_kind!=='broad');
  const byLabel=new Map<string,any>(signals.map((r:any)=>[canonical(r.topic_label),r]));
  return [...new Set([...topics,...byLabel.keys()])].map(label=>{
    const signal=byLabel.get(label), events=(hotspots?.hotspots||[]).filter((r:any)=>(r.themes||[]).map(canonical).includes(label));
    return {id:signal?.topic_code||label,title:label,signal,events,summary:`近5日 ETF 净申购估算 ${amount(signal?.flow_5d_cny)}`,meta:`${hotspots ? events.length+'个相关热点' : '热点未取得'} · 资金截至 ${signal?.data_date||'暂无'}`};
  });
}
Page({
  data:{loading:false,error:'',query:'',sort:0,sorts:['相关热点数','近5日净申购','板块名称'],rows:[] as any[],visible:[] as any[]},_version:0,
  onShow(){void this.load();},onHide(){this._version++;this.setData({loading:false});},onUnload(){this.onHide();},onPullDownRefresh(){void this.load().finally(()=>wx.stopPullDownRefresh());},settings,
  field(e:WechatMiniprogram.CustomEvent){this.setData({[e.currentTarget.dataset.field]:e.detail.value});this.filter();},
  filter(){const rows=this.data.rows.filter(r=>r.title.includes(this.data.query.trim()));rows.sort((a,b)=>Number(this.data.sort)===2?a.title.localeCompare(b.title,'zh-CN'):Number(this.data.sort)===1?(b.signal?.flow_5d_cny??-Infinity)-(a.signal?.flow_5d_cny??-Infinity):b.events.length-a.events.length);this.setData({visible:rows});},
  async load(){const version=++this._version,account=scope();this.setData({loading:true,error:'',rows:[],visible:[]});try{
    const user=await identity();if(version!==this._version||account!==scope())return;
    const results=await Promise.all([request<any>(`/market/large-capital${queryUser(user)}`),request<any>('/market/hotspots?limit=30'),request<any>('/market/events/topics')].map(p=>p.then(value=>({value,error:''})).catch(error=>({value:null,error:errorText(error)}))));
    if(version!==this._version||account!==scope())return;
    const value=(i:number)=>results[i].value;
    this.setData({rows:sectorRows(value(0),value(1),(value(2)?.topics||[]).map((r:any)=>r.topic)),error:results.flatMap((r,i)=>r.error?[`${['资金','热点','板块目录'][i]}：${r.error}`]:[]).join('；')});this.filter();
  }catch(error){if(version===this._version&&account===scope())this.setData({error:errorText(error)});}finally{if(version===this._version&&account===scope())this.setData({loading:false});}},
  detail(e:WechatMiniprogram.TouchEvent){const row=this.data.rows.find(r=>r.id===e.currentTarget.dataset.id);if(!row)return;wx.setStorageSync(storageKey('detail'),{kind:'sector',id:row.id,value:row});analysis('sector',row.id);},
  market(){wx.navigateTo({url:'/pages/market/index'});},capital(){analysis('capital');},sentiment(){analysis('retail');},
});
