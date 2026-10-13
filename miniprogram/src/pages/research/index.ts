/** 真实投研对话：同一会话数据库、同一工具权限，SSE 阶段与最终回答分开显示。 */
import { request, identity, queryUser, scope, storageKey, errorText, confirm, Fund, Portfolio } from '../../lib/api';
import { settings, analysis } from '../../lib/navigation';
import { streamChat } from '../../lib/stream';
import { submissionKey } from '../../lib/ledger';
interface Message {id:string;role:string;content:string;run_metadata?:Record<string,any>}
interface Conversation {id:string;title:string}
Page({
  data:{loading:false,busy:false,error:'',phase:'',question:'',conversationId:'',conversations:[] as Conversation[],messages:[] as Message[],
    showHistory:false,context:0,contexts:['当前账本','单基金','市场综合'],funds:[] as Fund[],fund:0,portfolios:[] as Portfolio[],portfolio:0,
    refreshAllowed:false,partial:'',trace:[] as string[],showTrace:false,researchReference:null as any,fundReportReference:null as any},
  _version:0,_scope:'',_stream:null as ReturnType<typeof streamChat>|null,_streamId:'',
  onShow(){void this.load();},
  onHide(){this._version++;this._stream?.abort();this._stream=null;this.setData({loading:false,busy:false});},onUnload(){this.onHide();},settings,
  field(e:WechatMiniprogram.CustomEvent){
    // 换研究对象时解除报告，不能带着旧报告去回答另一个账本的问题。
    const field=e.currentTarget.dataset.field;
    this.setData({[field]:e.detail.value,...(['context','portfolio','fund'].includes(field)?{researchReference:null,fundReportReference:null}:{})});
  },
  toggleHistory(){this.setData({showHistory:!this.data.showHistory});},toggleTrace(){this.setData({showTrace:!this.data.showTrace});},
  async load(){const version=++this._version,account=scope();
    if(this._scope!==account){this._scope=account;this.setData({messages:[],conversations:[],conversationId:'',question:'',partial:'',trace:[],portfolios:[],funds:[],researchReference:null,fundReportReference:null});}
    this.setData({loading:true,error:''});
    try{const user=await identity();if(version!==this._version||account!==scope())return;
      const [history,p,f]=await Promise.all([request<{conversations:Conversation[]}>(`/conversations${queryUser(user)}`),request<{portfolios:Portfolio[]}>(`/portfolios${queryUser(user)}`),request<{funds:Fund[]}>('/funds')]);
      if(version!==this._version||account!==scope())return;
      const selected=wx.getStorageSync(storageKey('portfolio'));
      this.setData({conversations:history.conversations,portfolios:p.portfolios,funds:f.funds,portfolio:Math.max(0,p.portfolios.findIndex(r=>r.id===selected))});
      const saved=this.data.conversationId||wx.getStorageSync(storageKey('conversation'));
      if(saved&&history.conversations.some(r=>r.id===saved))await this.readConversation(saved,version);
      const draft=wx.getStorageSync(storageKey('research-draft'));
      if(version!==this._version||account!==scope())return;
      if(draft){
        if(draft.portfolio_id&&!p.portfolios.some(r=>r.id===draft.portfolio_id))throw new Error('草稿所属账本已不可用，请返回持仓重新选择');
        // 季报版本与基金绑定，基金不在列表时不能悄悄使用第一只基金。
        if(draft.fund_report_ids?.length&&!f.funds.some(r=>r.fund_code===draft.fund_code))throw new Error('季报所属基金已不可用，请先重新导入该基金');
        // 报告追问开独立会话，避免混入之前无关研究。后续消息沿用同一会话。
        if(draft.research_job_id||draft.fund_report_ids?.length){wx.removeStorageSync(storageKey('conversation'));this.setData({conversationId:'',messages:[],partial:''});}
        this.setData({researchReference:draft.research_job_id?{job_id:draft.research_job_id,portfolio_id:draft.portfolio_id,headline:draft.report_label}:null,
        fundReportReference:draft.fund_report_ids?.length?{fund_code:draft.fund_code,document_ids:draft.fund_report_ids}:null,
        question:draft.message,context:draft.fund_code?1:draft.portfolio_id?0:2,
        fund:Math.max(0,f.funds.findIndex(r=>r.fund_code===draft.fund_code)),portfolio:draft.portfolio_id?Math.max(0,p.portfolios.findIndex(r=>r.id===draft.portfolio_id)):this.data.portfolio});wx.removeStorageSync(storageKey('research-draft'));}
    }catch(error){if(version===this._version&&account===scope())this.setData({error:errorText(error)});}finally{if(version===this._version&&account===scope())this.setData({loading:false});}
  },
  async readConversation(id:string,requestVersion?:number){const version=requestVersion??this._version,account=scope(),user=await identity();if(version!==this._version||account!==scope())return;
    const d=await request<{messages:Message[]}>(`/conversations/${encodeURIComponent(id)}${queryUser(user)}`);
    if(version!==this._version||account!==scope())return;
    const latest=[...d.messages].reverse().find(m=>m.role==='assistant'),ref=latest?.run_metadata?.research_reference || null;
    const fundRef=latest?.run_metadata?.fund_report_reference||null;
    const portfolio=ref?this.data.portfolios.findIndex(p=>p.id===ref.portfolio_id):-1;
    if(ref&&portfolio<0)throw new Error('报告所属账本已不可用');
    const fund=fundRef?this.data.funds.findIndex(f=>f.fund_code===fundRef.fund_code):-1;
    if(fundRef&&fund<0)throw new Error('季报所属基金已不可用，请先重新导入该基金');
    wx.setStorageSync(storageKey('conversation'),id);this.setData({conversationId:id,messages:d.messages,partial:'',showHistory:false,
      researchReference:ref,fundReportReference:fundRef,...(ref?{context:0,portfolio}:fundRef?{context:1,fund}:{})});
  },
  async selectConversation(e:WechatMiniprogram.TouchEvent){if(this.data.busy)return;const version=++this._version;try{await this.readConversation(e.currentTarget.dataset.id,version);}catch(error){if(version===this._version)this.setData({error:errorText(error)});}},
  newConversation(){if(this.data.busy)return;this._version++;wx.removeStorageSync(storageKey('conversation'));this.setData({conversationId:'',messages:[],partial:'',trace:[],error:'',phase:'',question:'',showHistory:false,researchReference:null,fundReportReference:null});},
  async deleteConversation(){if(this.data.busy||!this.data.conversationId||!await confirm('删除当前对话','只删除这条聊天记录，不删除你的持仓和研究报告。'))return;
    const id=this.data.conversationId,version=++this._version,account=scope();try{const user=await identity();if(version!==this._version||account!==scope())return;await request(`/conversations/${encodeURIComponent(id)}${queryUser(user)}`,'DELETE');if(version===this._version&&account===scope()){this.newConversation();await this.load();}}catch(error){if(version===this._version&&account===scope())this.setData({error:errorText(error)});}},
  async send(){
    const question=this.data.question.trim();if(this.data.busy||this.data.loading)return;
    if(!question||question.length>800){this.setData({error:'请输入1至800字的问题'});return;}
    const version=++this._version,account=scope();this.setData({busy:true,error:'',partial:'',phase:'确认会话',trace:[]});
    const active=()=>version===this._version&&account===scope();
    try{
      const user=await identity();if(!active())return;
      const context=Number(this.data.context), portfolio=this.data.portfolios[this.data.portfolio],fund=this.data.funds[this.data.fund];
      if(context===0&&!portfolio)throw new Error('请先选择或创建账本，也可以切换到市场综合');
      if(context===1&&!fund)throw new Error('请先导入基金，也可以切换到市场综合');
      let id=this.data.conversationId;
      if(!id){const conversation=await request<Conversation>('/conversations','POST',{user_id:user});if(!active())return;id=conversation.id;wx.setStorageSync(storageKey('conversation'),id);this.setData({conversationId:id,conversations:[conversation,...this.data.conversations]});}
      const body={message:question,user_id:user,conversation_id:id,research_refresh:this.data.refreshAllowed,
        research_job_id:context===0&&this.data.researchReference?.portfolio_id===portfolio?.id?this.data.researchReference.job_id:'',
        fund_report_ids:context===1&&this.data.fundReportReference?.fund_code===fund?.fund_code?this.data.fundReportReference.document_ids:[],
        ...(context===0?{portfolio_id:portfolio.id}:context===1?{fund_code:fund.fund_code}:{}),max_tool_rounds:10};
      const key=storageKey(`chat-submit:${id}`),idempotency_key=submissionKey(key,body);
      // 错误重试复用同一个 key；最终 result 替换答案，不能再把它追加一次。
      let resultReceived=false;
      this._stream=streamChat({...body,idempotency_key},event=>{
        if(!active())return;const d=event.data;
        if(event.event==='stream_started')this._streamId=String(d.stream_id||'');
        else if(event.event==='answer_delta')this.setData({partial:this.data.partial+String(d.delta||'')});
        else if(event.event==='result'){
          resultReceived=true;wx.removeStorageSync(key);
          const existing=this.data.messages.filter(m=>m.id!==d.user_message.id&&m.id!==d.assistant_message.id);
          this.setData({messages:[...existing,d.user_message,d.assistant_message],partial:'',question:'',phase:'已完成',refreshAllowed:false,
            researchReference:d.assistant_message.run_metadata?.research_reference||null,
            fundReportReference:d.assistant_message.run_metadata?.fund_report_reference||null,
            conversations:[d.conversation,...this.data.conversations.filter(c=>c.id!==id)]});
        }else if(event.event==='cancelled')this.setData({phase:'已停止'});
        else {const label=String(d.label||d.message||d.tool_name||'');if(label)this.setData({phase:label,trace:[...this.data.trace,label]});}
      });
      await this._stream.done;
      if(!resultReceived)throw new Error('未收到完整回答，请重试');
    }catch(error){if(active())this.setData({error:errorText(error),phase:'本次未完成，可用原问题重试'});}
    finally{if(active()){this._stream=null;this._streamId='';this.setData({busy:false});}}
  },
  stop(){this._stream?.abort();if(this._streamId)void request(`/agent/chat/stream/${encodeURIComponent(this._streamId)}`,'DELETE').catch(()=>{});},
  copy(e:WechatMiniprogram.TouchEvent){const m=this.data.messages.find(r=>r.id===e.currentTarget.dataset.id);if(m)wx.setClipboardData({data:m.content});},
  evidence(e:WechatMiniprogram.TouchEvent){const m=this.data.messages.find(r=>r.id===e.currentTarget.dataset.id);if(!m)return;wx.setStorageSync(storageKey('detail'),{kind:'answer',id:m.id,value:m.run_metadata});analysis('answer',m.id);},
  reports(){analysis('research');},records(){analysis('decisions');},
  sourceReport(){const r=this.data.researchReference;if(r)analysis('research',r.job_id,r.portfolio_id);},
  sourceFundReports(){const r=this.data.fundReportReference;if(r)wx.navigateTo({url:`/pages/fund-reports/index?code=${r.fund_code}&ids=${r.document_ids.join(',')}`});},
  multi(){if(!this.data.busy)this.setData({question:'请针对当前配置开展多视角投研，调用可用的专业分析或子 Agent 工具，分别分析机会、最强反对理由和风险，再基于同一份证据汇总共识与分歧。引用证据，说明信息缺口，不以多数同意作为正确性依据。不要自动采集或修改持仓。'});},
});
