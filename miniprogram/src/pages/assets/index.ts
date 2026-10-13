/** 资产首页：组合只用于分组，市值和盈亏以实际份额账本为准。 */
import { request, identity, scope, storageKey, queryUser, errorText, FundList, Portfolio, Fund, confirm, connection, getBackgroundJob, DirectoryFund, DirectoryResult, ImportJob } from '../../lib/api';
import { number, signed, tone } from '../../lib/format';
import { submissionKey } from '../../lib/ledger';
import { settings, analysis, researchDraft } from '../../lib/navigation';
import { currentTradeSelection, returnToTrade, TradeSelection } from '../../lib/trade-selection';

// 保存后台任务身份，不保存基金持仓，也不把“任务已受理”当成“导入成功”。
interface PendingImport { fund_code: string; short_name: string; user_id: string; job_id: string; submission_key: string }

Page({
  data: {
    loading: false, syncing: false, error: '', message: '', segment: 'holdings',
    portfolios: [] as Portfolio[], funds: [] as Fund[], selected: 0,
    current: null as Portfolio | null, rows: [] as object[], observed: [] as object[],
    total: '--', cost: '--', pnl: '--', realized: '--', unrealized: '--', pnlTone: '', source: '',
    catalogQuery: '', catalogSearched: false, catalogLoading: false, catalogError: '', catalogInfo: '',
    catalogFunds: [] as DirectoryFund[],
    pendingImport: null as PendingImport | null, importBusy: false, importChecking: false,
    importMessage: '', importError: '',
    tradeSelection: null as TradeSelection | null,
  },
  _sequence: 0,
  _fundsVersion: 0,
  _loadedScope: '',
  _catalogSequence: 0,
  _importSequence: 0,
  _pollCount: 0,
  _importTimer: undefined as ReturnType<typeof setTimeout> | undefined,
  onShow() {
    // 从买入页跳来时直接打开基金库，复用原有搜索/导入流程，不另建一份基金目录。
    const tradeSelection = currentTradeSelection();
    this.setData({ tradeSelection, ...(tradeSelection ? { segment: 'funds' } : {}) });
    void this.load();
    // 返回页面只恢复查询已有 Job，不自动重新提交导入，更不会重新搜市场目录。
    this.setData({ pendingImport: wx.getStorageSync(storageKey('fund-import')) || null });
    if (this.data.segment === 'funds') { this._pollCount = 0; void this.checkImport(); }
  },
  onHide() {
    this._sequence++; this._catalogSequence++; this.pauseImport();
    this.setData({ loading: false, catalogLoading: false, importBusy: false });
  },
  onUnload() { this.onHide(); },
  onPullDownRefresh() { void this.load().finally(() => wx.stopPullDownRefresh()); },
  async load() {
    const sequence = ++this._sequence, startScope = scope(), fundsVersion = this._fundsVersion;
    if (this._loadedScope !== startScope) {
      // 原生 tab 页会缓存。换账号后先清屏，再等待新账号结果，不能闪现旧持仓。
      this.setData({ current: null, portfolios: [], rows: [], funds: [], observed: [], message: '',
        catalogQuery: '', catalogSearched: false, catalogFunds: [], catalogInfo: '', catalogError: '',
        importMessage: '', importError: '' });
      this._loadedScope = startScope;
    }
    this.setData({ loading: true, error: '' });
    try {
      const user = await identity();
      const result = await Promise.all([
        request<{ portfolios: Portfolio[] }>(`/portfolios${queryUser(user)}`), request<FundList>('/funds'),
      ]);
      // 快速切页或更换账号时，丢弃旧请求，避免上一人的金额覆盖新页面。
      if (sequence !== this._sequence || scope() !== startScope) return;
      const [list, funds] = result;
      const savedId = wx.getStorageSync(storageKey('portfolio'));
      const index = Math.max(0, list.portfolios.findIndex(p => p.id === savedId));
      this.setData({ portfolios: list.portfolios, selected: index });
      // 导入完成可能先刷新了基金列表；晚到的旧读取不能把新基金覆盖掉。
      if (fundsVersion === this._fundsVersion) this.applyFunds(funds);
      this.selectPortfolio(index);
    } catch (error) {
      if (sequence === this._sequence) this.setData({ error: errorText(error), current: null, rows: [], portfolios: [] });
    } finally { if (sequence === this._sequence) this.setData({ loading: false }); }
  },
  applyFunds(list: FundList) {
    this._fundsVersion++;
    this.setData({ funds: list.funds,
      source: list.data_mode === 'fixture' ? '教学样本数据' : '已保存的基金净值',
      observed: list.funds.map((f, i) => ({ ...f, mark: i % 4, nav: number(f.latest_nav, 4), day: signed(f.daily_growth_rate_pct, '%'), dayTone: tone(f.daily_growth_rate_pct), month: signed(f.return_30d_pct, '%'), quarter: signed(f.return_3m_pct, '%') })),
      catalogFunds: this.data.catalogFunds.map(f => ({ ...f, is_imported: list.funds.some(saved => saved.fund_code === f.fund_code) })),
    });
  },
  selectPortfolio(index: number) {
    const current = this.data.portfolios[index] || null;
    if (current) wx.setStorageSync(storageKey('portfolio'), current.id);
    this.setData({ selected: index, current,
      total: number(current?.total_market_value), cost: number(current?.total_cost_amount),
      pnl: signed(current?.total_pnl_amount), pnlTone: tone(current?.total_pnl_amount),
      realized: signed(current?.realized_pnl_amount), unrealized: signed(current?.unrealized_pnl_amount),
      rows: (current?.holdings || []).map((h, i) => {
        const f = this.data.funds.find(fund => fund.fund_code === h.fund_code);
        return { ...h, mark: i % 4, value: number(h.current_value), unitsText: number(h.units), costText: number(h.cost_amount),
          pnl: signed(h.total_pnl_amount), pnlTone: tone(h.total_pnl_amount),
          day: signed(f?.daily_growth_rate_pct, '%'), dayTone: tone(f?.daily_growth_rate_pct),
          month: signed(f?.return_30d_pct, '%'), quarter: signed(f?.return_3m_pct, '%') };
      }),
    });
  },
  choose(e: WechatMiniprogram.CustomEvent) { this.selectPortfolio(Number(e.detail.value)); },
  segment(e: WechatMiniprogram.TouchEvent) {
    this.setData({ segment: e.currentTarget.dataset.value });
    if (this.data.segment === 'funds') {
      this.setData({ pendingImport: wx.getStorageSync(storageKey('fund-import')) || null });
      this._pollCount = 0; void this.checkImport();
    } else { this._catalogSequence++; this.setData({ catalogLoading: false, importBusy: false }); this.pauseImport(); }
  },
  catalogField(e: WechatMiniprogram.CustomEvent) {
    this._catalogSequence++;
    this.setData({ catalogQuery: e.detail.value, catalogLoading: false, catalogSearched: false,
      catalogFunds: [], catalogError: '', catalogInfo: '' });
  },
  async searchCatalog() {
    const query = this.data.catalogQuery.trim();
    if (this.data.catalogLoading) return;
    if (!query || query.length > 80) { this.setData({ catalogError: '请输入基金名称或六位代码' }); return; }
    const sequence = ++this._catalogSequence, startScope = scope();
    this.setData({ catalogLoading: true, catalogSearched: false, catalogError: '', catalogFunds: [], catalogInfo: '' });
    try {
      // 只有点击搜索才查询在线目录，输入每个字不会触发采集。
      const result = await request<DirectoryResult>(`/funds/search?q=${encodeURIComponent(query)}&limit=12`);
      if (sequence !== this._catalogSequence || scope() !== startScope) return;
      this.setData({ catalogFunds: result.funds, catalogSearched: true,
        catalogInfo: [result.message, result.source, result.funds.length === 12 ? '最多展示12项，可输入完整代码缩小范围' : ''].filter(Boolean).join(' · ') });
    } catch (error) {
      if (sequence === this._catalogSequence && scope() === startScope) this.setData({ catalogError: errorText(error) });
    } finally { if (sequence === this._catalogSequence && scope() === startScope) this.setData({ catalogLoading: false }); }
  },
  async importFund(e: WechatMiniprogram.TouchEvent) {
    const f = this.data.catalogFunds.find(item => item.fund_code === e.currentTarget.dataset.code);
    if (f) await this.submitImport(f);
  },
  async retryImport() {
    const pending = wx.getStorageSync(storageKey('fund-import')) as PendingImport | undefined;
    if (!pending) return;
    this.setData({ pendingImport: pending });
    if (pending.job_id) { this._pollCount = 0; await this.checkImport(); }
    else await this.submitImport(pending);
  },
  async submitImport(f: Pick<DirectoryFund, 'fund_code' | 'short_name'>) {
    if (this.data.importBusy || this.data.pendingImport?.job_id) return;
    if (this.data.pendingImport && this.data.pendingImport.fund_code !== f.fund_code) return;
    if (this.data.funds.some(saved => saved.fund_code === f.fund_code)) return;
    const startScope = scope(), c = connection(), pendingKey = storageKey('fund-import');
    const key = storageKey(`fund-import-submit:${f.fund_code}`), sequence = ++this._importSequence;
    this.setData({ importBusy: true, importError: '', importMessage: '正在提交基金导入' });
    try {
      const user = await identity();
      if (scope() !== startScope || sequence !== this._importSequence) return;
      const payload = { user_id: user, fund_code: f.fund_code };
      const idempotency_key = submissionKey(key, payload);
      const pending: PendingImport = { ...f, user_id: user, job_id: '', submission_key: key };
      wx.setStorageSync(pendingKey, pending); this.setData({ pendingImport: pending });
      const result = await request<{ job: ImportJob }>('/funds/import', 'POST', { ...payload, idempotency_key }, c);
      // 即使提交时用户切页，也保存已受理的 Job，回来后可以继续查进度。
      pending.job_id = result.job.id; wx.setStorageSync(pendingKey, pending);
      if (scope() !== startScope || sequence !== this._importSequence) return;
      this.setData({ pendingImport: pending, importMessage: '已提交，正在读取基金档案和净值' });
      this._pollCount = 0; await this.checkImport();
    } catch (error) {
      if (scope() === startScope && sequence === this._importSequence) this.setData({ importError: errorText(error), importMessage: '' });
    } finally { if (scope() === startScope && sequence === this._importSequence) this.setData({ importBusy: false }); }
  },
  pauseImport() {
    if (this._importTimer !== undefined) clearTimeout(this._importTimer);
    this._importTimer = undefined; this._importSequence++;
    this.setData({ importChecking: false });
  },
  async checkImport() {
    const pending = this.data.pendingImport;
    if (!pending?.job_id || this.data.importChecking) return;
    if (this._importTimer !== undefined) clearTimeout(this._importTimer);
    const sequence = this._importSequence, startScope = scope(), c = connection(), pendingKey = storageKey('fund-import');
    const active = () => sequence === this._importSequence && scope() === startScope;
    this.setData({ importChecking: true, importError: '' });
    try {
      const { job } = await getBackgroundJob(pending.job_id, pending.user_id, c);
      if (!active()) return;
      if (job.status === 'completed') {
        // 完成后还要读已导入列表确认可用，不能凭 HTTP 200 提前显示成功。
        const list = await request<FundList>('/funds', 'GET', undefined, c);
        if (!active()) return;
        if (!list.funds.some(f => f.fund_code === pending.fund_code)) throw new Error('任务已结束，但基金尚未出现在可用列表，请稍后检查进度');
        this.applyFunds(list); this.selectPortfolio(this.data.selected);
        wx.removeStorageSync(pendingKey); wx.removeStorageSync(pending.submission_key);
        this.setData({ pendingImport: null, importMessage: `${pending.short_name} 已导入，可用于持仓录入或交易记账` });
      } else if (['failed', 'cancelled'].includes(job.status)) {
        wx.removeStorageSync(pendingKey); wx.removeStorageSync(pending.submission_key);
        this.setData({ pendingImport: null, importMessage: '', importError: job.error || '导入未完成，可重新点击该基金的导入按钮' });
      } else {
        const progress = Number.isFinite(job.progress_percent) ? ` · ${Math.min(100, Math.max(0, job.progress_percent))}%` : '';
        this.setData({ importMessage: `${pending.short_name} 正在导入${progress}` });
        // 只跟踪用户刚提交的任务，离开页面即停止；最多自动读取约六分钟。
        if (this._pollCount++ < 120 && this.data.segment === 'funds') this._importTimer = setTimeout(() => { void this.checkImport(); }, 3000);
      }
    } catch (error) {
      if (active()) this.setData({ importError: `读取导入进度失败：${errorText(error)}` });
    } finally { if (active()) this.setData({ importChecking: false }); }
  },
  fund(e: WechatMiniprogram.TouchEvent) { wx.navigateTo({ url: `/pages/fund/index?code=${e.currentTarget.dataset.code}` }); },
  libraryFund(e: WechatMiniprogram.TouchEvent) {
    if (!this.data.tradeSelection) { this.fund(e); return; }
    const code = e.currentTarget.dataset.code;
    if (!this.data.funds.some(f => f.fund_code === code)) {
      this.setData({ importError: '该基金尚未导入完成，请检查进度或刷新基金库' }); return;
    }
    // 仅带回基金代码。真正记账仍需用户填写成交信息并再次确认。
    returnToTrade(code); this.setData({ tradeSelection: null });
  },
  returnTrade() { returnToTrade(); this.setData({ tradeSelection: null }); },
  account: settings,
  review() {if(this.data.current)wx.navigateTo({url:`/pages/portfolio-review/index?portfolio=${this.data.current.id}`});},
  analysis(e: WechatMiniprogram.TouchEvent) { if(this.data.current) analysis(e.currentTarget.dataset.kind,'',this.data.current.id); },
  ask() { if(this.data.current) researchDraft('请综合当前持仓、风险和市场证据分析我的配置，说明机会、风险和最强反对理由，不自动采集或修改持仓。',{portfolio_id:this.data.current.id}); },
  create() { wx.navigateTo({ url: '/pages/positions/index' }); },
  edit() { if (this.data.current) wx.navigateTo({ url: `/pages/positions/index?id=${this.data.current.id}` }); },
  transactions() { if (this.data.current) wx.navigateTo({ url: `/pages/transactions/index?id=${this.data.current.id}` }); },
  async sync() {
    if (this.data.syncing || !await confirm('更新基金净值', '从数据源同步已导入基金的最新披露净值？基金净值不是盘中估值。')) return;
    this.setData({ syncing: true, error: '', message: '' });
    try {
      const user = await identity();
      const result = await request<{ message: string }>('/funds/data-sync', 'POST', { user_id: user });
      this.setData({ message: result.message || '净值同步已提交。任务完成后，可刷新持仓查看。' });
    } catch (error) { this.setData({ error: errorText(error) }); }
    finally { this.setData({ syncing: false }); }
  },
});
