/** 一笔真实成交对应一条流水。失败重试复用幂等键，避免网络抖动重复记账。 */
import { request, identity, queryUser, errorText, Fund, Holding, confirm, notify, storageKey, scope, Portfolio } from '../../lib/api';
import { TradeForm, tradePayload, submissionKey } from '../../lib/ledger';
import { number, signed, tone, chinaTime } from '../../lib/format';
import { beijingToday, orderTimePayload, TradeTiming } from '../../lib/trade-timing';
import { openTradeFundLibrary, takeTradeSelection } from '../../lib/trade-selection';
interface Trade { id: string; short_name: string; transaction_type: string; trade_date: string; units: number; unit_price: number; cash_flow: number; fee: number; realized_pnl_amount: number | null; note: string; timing?: TradeTiming | null }
interface FundOption { fund_code: string; short_name: string; optionLabel: string }
const blank = (): TradeForm => ({ fund_code: '', transaction_type: 'buy', trade_date: beijingToday(), units: '', unit_price: '', fee: '0', note: '', submitted_date: '', submitted_time: '', channel: 'off_exchange' });
Page({
  data: { id: '', name: '', funds: [] as FundOption[], allFunds: [] as Fund[], holdings: [] as Holding[], fundIndex: 0, form: blank(), trades: [] as object[], loading: true, saving: false, error: '', today: beijingToday(), locked: false, timing: null as TradeTiming | null, timingLoading: false, timingError: '', channels: ['场外申购／赎回', '场内买卖'] },
  _version: 0, _scope: '', _timingVersion: 0,
  onLoad(query: Record<string, string>) { this.setData({ id: query.id || '' }); },
  onShow() {
    // 首次进入与从基金库返回都刷新列表；不能只在 onLoad 时读取一次。
    const account = scope();
    if (this._scope && this._scope !== account) this.setData({ form: blank(), name: '', funds: [], allFunds: [], holdings: [], trades: [], locked: true });
    this._scope = account;
    this.invalidateTiming();
    this.setData({ today: beijingToday() });
    const draft = takeTradeSelection(this.data.id);
    if (draft) this.setData({ form: draft });
    return this.load();
  },
  onHide() { this._version++; this.invalidateTiming(); this.setData({ loading: false, saving: false }); },
  onUnload() { this.onHide(); },
  active(version: number, account: string) { return version === this._version && account === scope(); },
  async refreshFunds() { if (!this.data.saving) await this.load(); },
  async load() {
    this.invalidateTiming();
    const version = ++this._version, account = scope();
    this.setData({ loading: true, error: '' });
    try {
      if (!/^portfolio_[a-f0-9]{16}$/.test(this.data.id)) throw new Error('账本 ID 无效');
      const user = await identity(), path = `/portfolios/${this.data.id}`;
      if (!this.active(version, account)) return;
      const [p, list, records] = await Promise.all([request<Portfolio>(`${path}${queryUser(user)}`), request<{ funds: Fund[] }>('/funds'), request<{ transactions: Trade[] }>(`${path}/transactions${queryUser(user)}`)]);
      if (!this.active(version, account)) return;
      this.setData({ name: p.name, locked: p.position_mode === 'allocation', allFunds: list.funds, holdings: p.holdings, trades: records.transactions.map(t => ({ ...t, label: t.transaction_type === 'buy' ? '买入' : t.transaction_type === 'sell' ? '卖出' : '初始持仓', unitsText: number(t.units), price: number(t.unit_price, 4), flow: signed(t.cash_flow), feeText: number(t.fee), pnl: signed(t.realized_pnl_amount), tone: tone(t.realized_pnl_amount), submittedText: t.timing ? chinaTime(t.timing.submitted_at) : '', returnDate: t.timing?.first_return_nav_date || t.timing?.last_return_nav_date })) });
      this.updateOptions();
    } catch (error) { if (this.active(version, account)) this.setData({ error: errorText(error), locked: true }); }
    finally { if (this.active(version, account)) this.setData({ loading: false }); }
  },
  updateOptions() {
    // 买入可选整个已导入基金库，不受组合持仓约束；卖出才限于当前正份额持仓。
    const held = this.data.holdings.filter(h => h.units != null && h.units > 0);
    const source = this.data.form.transaction_type === 'buy' ? this.data.allFunds : held;
    const funds = source.map(f => ({ fund_code: f.fund_code, short_name: f.short_name, optionLabel: `${f.fund_code} · ${f.short_name}` }));
    const previous = this.data.form.fund_code;
    const fundIndex = Math.max(0, funds.findIndex(f => f.fund_code === previous));
    const code = funds[fundIndex]?.fund_code || '';
    const changed = code !== previous;
    if (changed) this.invalidateTiming();
    this.setData({ funds, fundIndex, form: { ...this.data.form, fund_code: code, ...(changed ? { units: '', unit_price: '', fee: '0' } : {}) } });
  },
  field(e: WechatMiniprogram.CustomEvent) {
    const field = e.currentTarget.dataset.field;
    if (this.data.saving) return;
    if (['submitted_date', 'submitted_time'].includes(field)) this.invalidateTiming();
    this.setData({ form: { ...this.data.form, [field]: e.detail.value } });
  },
  invalidateTiming() {
    // 基金/方向/时间改变后旧估算立即失效，晚到的响应不能恢复旧结果。
    this._timingVersion++;
    this.setData({ timing: null, timingError: '', timingLoading: false });
  },
  channel(e: WechatMiniprogram.CustomEvent) {
    if (this.data.saving) return;
    this.invalidateTiming();
    this.setData({ form: { ...this.data.form, channel: Number(e.detail.value) === 1 ? 'exchange' : 'off_exchange' } });
  },
  clearOrderTime() {
    if (this.data.saving) return;
    this.invalidateTiming();
    this.setData({ form: { ...this.data.form, submitted_date: '', submitted_time: '' } });
  },
  async previewTiming() {
    if (this.data.saving || this.data.timingLoading) return;
    this.invalidateTiming();
    const revision = this._timingVersion, version = this._version, account = scope(), form = { ...this.data.form };
    this.setData({ timingLoading: true });
    try {
      const input = orderTimePayload(form);
      if (!input.submitted_at) throw new Error('请选择下单日期和北京时间');
      if (!form.fund_code) throw new Error('请先选择基金');
      const result = await request<TradeTiming>(`/funds/${form.fund_code}/transaction-timing`, 'POST', { ...input, transaction_type: form.transaction_type });
      if (this.active(version, account) && revision === this._timingVersion) this.setData({ timing: result });
    } catch (error) {
      if (this.active(version, account) && revision === this._timingVersion) this.setData({ timingError: errorText(error) });
    } finally {
      if (this.active(version, account) && revision === this._timingVersion) this.setData({ timingLoading: false });
    }
  },
  applyNavDate() {
    const timing = this.data.timing;
    // 仅用户明确点击才填写成交日，未来日期不能当成今天已经确认的流水。
    if (!this.data.saving && timing?.status === 'estimated' && timing.nav_date && timing.nav_date <= beijingToday()) this.setData({ form: { ...this.data.form, trade_date: timing.nav_date } });
  },
  choose(e: WechatMiniprogram.CustomEvent) {
    if (this.data.saving || this.data.loading) return;
    const index = Number(e.detail.value), fund = this.data.funds[index];
    if (!fund || fund.fund_code === this.data.form.fund_code) return;
    this.invalidateTiming();
    this.setData({ fundIndex: index, form: { ...this.data.form, fund_code: fund.fund_code, units: '', unit_price: '', fee: '0' } });
  },
  type(e: WechatMiniprogram.TouchEvent) {
    const value = e.currentTarget.dataset.value;
    if (this.data.saving || this.data.loading || !['buy', 'sell'].includes(value) || value === this.data.form.transaction_type) return;
    this.invalidateTiming();
    this.setData({ form: { ...this.data.form, transaction_type: value, units: '', unit_price: '', fee: '0' }, error: '' });
    this.updateOptions();
  },
  library() {
    if (this.data.saving || this.data.loading || this.data.locked || this.data.form.transaction_type !== 'buy') return;
    openTradeFundLibrary(this.data.id, this.data.name, this.data.form);
  },
  async save() {
    if (this.data.saving || this.data.loading || this.data.locked) return;
    const version = this._version, account = scope(), form = { ...this.data.form };
    this.setData({ saving: true, error: '' });
    try {
      if (!this.data.funds.some(f => f.fund_code === form.fund_code)) throw new Error('请先选择可用基金');
      const user = await identity(), payload = tradePayload(form, user);
      if (!this.active(version, account)) return;
      const mismatch = this.data.timing?.nav_date && this.data.timing.nav_date !== form.trade_date ? '您填写的净值日与估算不同，请核对成交回单。' : '';
      if (!await confirm('确认交易记账', `${payload.transaction_type === 'buy' ? '买入' : '卖出'} ${payload.fund_code}，净值日 ${payload.trade_date}，${payload.units} 份，成交净值 ${payload.unit_price} 元。${mismatch}仅记录已确认成交，不执行下单。`)) return;
      if (!this.active(version, account)) return;
      const key = storageKey(`trade:${this.data.id}`);
      await request(`/portfolios/${this.data.id}/transactions`, 'POST', { ...payload, idempotency_key: submissionKey(key, payload) });
      if (!this.active(version, account)) return;
      wx.removeStorageSync(key);
      this.invalidateTiming();
      this.setData({ form: { ...blank(), transaction_type: form.transaction_type, fund_code: form.fund_code }, saving: false });
      notify('交易已记账'); await this.load();
    } catch (error) { if (this.active(version, account)) this.setData({ error: errorText(error) }); }
    finally { if (this.active(version, account)) this.setData({ saving: false }); }
  },
  async remove(e: WechatMiniprogram.TouchEvent) {
    if (this.data.saving || this.data.loading) return;
    const version = this._version, account = scope();
    this.setData({ saving: true, error: '' });
    try {
      if (!await confirm('删除交易记录', '删除后将重新计算后续成本和盈亏。如导致后续超额卖出，后端会拒绝删除。')) return;
      const user = await identity(); if (!this.active(version, account)) return;
      await request(`/portfolios/${this.data.id}/transactions/${e.currentTarget.dataset.id}${queryUser(user)}`, 'DELETE');
      if (this.active(version, account)) { this.setData({ saving: false }); await this.load(); }
    } catch (error) { if (this.active(version, account)) this.setData({ error: errorText(error) }); }
    finally { if (this.active(version, account)) this.setData({ saving: false }); }
  },
});
