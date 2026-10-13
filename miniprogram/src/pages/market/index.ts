/** 只读已有快照，只有点击刷新才请求数据源，不开启后台定时采集。 */
import { request, errorText } from '../../lib/api';
import { amount, number, signed, tone } from '../../lib/format';
import { analysis } from '../../lib/navigation';
interface Quote { index_code: string; index_name: string; last: number | null; change_pct: number | null; amount_cny: number | null; volume_lots: number | null; quote_at: string | null; freshness: string; error: string }
interface Turnover { snapshot: null | { sh_sz_amount_cny: number; sh_sz_bj_amount_cny: number; fetched_at: string; reference_at: string }; error: string; limitation: string }
interface Daily { index_code: string; data_date: string; return_30d_pct: number; return_3m_pct: number }
Page({
  data: { loading: false, error: '', quotes: [] as object[], turnover: '--', shsz: '--', asof: '', limitation: '', source: '' },
  _sequence: 0,
  onShow() { void this.load(false); },
  onHide() { this._sequence++; this.setData({ loading: false }); },
  onPullDownRefresh() { void this.load(true).finally(() => wx.stopPullDownRefresh()); },
  refresh() { void this.load(true); },
  turnoverChart() { analysis('turnover'); },
  sentiment() { analysis('sentiment'); },
  async load(refresh: boolean) {
    if (this.data.loading) return;
    const sequence = ++this._sequence;
    this.setData({ loading: true, error: '' });
    const suffix = refresh ? '/refresh' : '', method = refresh ? 'POST' : 'GET';
    try {
      // 两个来源独立返回；一个失败时仍显示另一个成功结果。
      const [indices, turnover, history] = await Promise.all([
        request<{ indices: Quote[]; source_name: string }>(`/market/intraday${suffix}`, method).catch(error => ({ error: errorText(error) })),
        request<Turnover>(`/market/turnover${suffix}`, method).catch(error => ({ error: errorText(error) })),
        // 短周期涨跌复用已保存日线，和盘中报价分别标日期，不在此隐式同步日线。
        request<{ indices: Daily[] }>('/market/indices').catch(error => ({ error: errorText(error) })),
      ]);
      if (sequence !== this._sequence) return;
      const errors: string[] = [];
      if ('error' in history) errors.push(`日线指标：${history.error}`);
      if ('indices' in indices) this.setData({ source: indices.source_name, quotes: indices.indices.map(q => {
        const daily = 'indices' in history ? history.indices.find(d => d.index_code === q.index_code) : undefined;
        return { ...q, month: signed(daily?.return_30d_pct, '%'), quarter: signed(daily?.return_3m_pct, '%'), dailyDate: daily?.data_date || '暂无日线',
        price: number(q.last), change: signed(q.change_pct, '%'), tone: tone(q.change_pct),
        amount: amount(q.amount_cny), volume: number(q.volume_lots == null ? null : q.volume_lots / 10000),
        time: q.quote_at?.replace('T', ' ').slice(0, 19) || '尚无行情',
        freshnessText: ({ recent: '最新快照', stale: '延迟快照', previous_date: '历史快照', missing: '未同步' } as Record<string, string>)[q.freshness] || q.freshness,
      }; }) }); else errors.push(indices.error);
      if ('snapshot' in turnover) this.setData({ turnover: amount(turnover.snapshot?.sh_sz_bj_amount_cny), shsz: amount(turnover.snapshot?.sh_sz_amount_cny), asof: (turnover.snapshot?.reference_at || turnover.snapshot?.fetched_at || '').replace('T', ' ').slice(0, 19), limitation: turnover.limitation });
      if (turnover.error) errors.push(turnover.error);
      this.setData({ error: errors.join('；') });
    } catch (error) { if (sequence === this._sequence) this.setData({ error: errorText(error) }); }
    finally { if (sequence === this._sequence) this.setData({ loading: false }); }
  },
});
