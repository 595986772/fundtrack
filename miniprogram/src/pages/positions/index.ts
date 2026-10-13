/** 新建真实持仓或一次性转换旧权重组合；已有交易账本不能整体覆盖。 */
import { request, identity, queryUser, errorText, Portfolio, Fund, confirm, notify, storageKey } from '../../lib/api';
import { PositionRow, positionsPayload } from '../../lib/ledger';
import { today } from '../../lib/format';
Page({
  data: { id: '', name: '', rows: [] as PositionRow[], funds: [] as Fund[], fundIndex: 0, loading: true, saving: false, error: '', locked: false, today: today() },
  onLoad(query: Record<string, string>) { this.setData({ id: query.id || '' }); void this.load(); },
  async load() {
    this.setData({ loading: true, error: '' });
    try {
      const user = await identity();
      const list = await request<{ funds: Fund[] }>('/funds');
      this.setData({ funds: list.funds });
      if (this.data.id) {
        if (!/^portfolio_[a-f0-9]{16}$/.test(this.data.id)) throw new Error('账本 ID 无效');
        const p = await request<Portfolio>(`/portfolios/${this.data.id}${queryUser(user)}`);
        this.setData({ name: p.name, locked: p.position_mode !== 'allocation', rows: p.holdings.map(h => ({ fund_code: h.fund_code, short_name: h.short_name, units: '', cost_amount: '', purchase_date: '' })) });
      }
    } catch (error) { this.setData({ error: errorText(error), locked: true }); }
    finally { this.setData({ loading: false }); }
  },
  name(e: WechatMiniprogram.CustomEvent) { this.setData({ name: e.detail.value }); },
  choose(e: WechatMiniprogram.CustomEvent) { this.setData({ fundIndex: Number(e.detail.value) }); },
  add() {
    const f = this.data.funds[this.data.fundIndex];
    if (!f) return;
    if (this.data.rows.length >= 10) { notify('每个账本最多 10 只基金'); return; }
    if (this.data.rows.some(r => r.fund_code === f.fund_code)) { notify('已添加这只基金'); return; }
    this.setData({ rows: [...this.data.rows, { fund_code: f.fund_code, short_name: f.short_name, units: '', cost_amount: '', purchase_date: '' }] });
  },
  remove(e: WechatMiniprogram.TouchEvent) { this.setData({ rows: this.data.rows.filter((_, index) => index !== Number(e.currentTarget.dataset.index)) }); },
  field(e: WechatMiniprogram.CustomEvent) {
    const { index, field } = e.currentTarget.dataset;
    this.setData({ rows: this.data.rows.map((r, i) => i === Number(index) ? { ...r, [field]: e.detail.value } : r) });
  },
  async save() {
    if (this.data.saving || this.data.locked || this.data.loading) return;
    this.setData({ saving: true, error: '' });
    try {
      const user = await identity(), payload = positionsPayload(this.data.name, this.data.rows, user);
      if (!await confirm('保存实际持仓', `将保存 ${payload.holdings.length} 只基金的份额与成本。这是账本记录，不会向基金平台下单。`)) return;
      const result = await request<Portfolio>(`/portfolios${this.data.id ? `/${this.data.id}` : ''}`, this.data.id ? 'PUT' : 'POST', payload);
      wx.setStorageSync(storageKey('portfolio'), result.id); notify('持仓已保存'); wx.switchTab({ url: '/pages/assets/index' });
    } catch (error) { this.setData({ error: errorText(error) }); }
    finally { this.setData({ saving: false }); }
  },
});
