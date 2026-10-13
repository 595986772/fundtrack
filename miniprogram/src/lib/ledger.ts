/** 表单只做明确的输入校验；移动平均成本、卖出盈亏仍交给后端。 */
import { today } from './format';
import { OrderTimeFields, orderTimePayload, beijingToday } from './trade-timing';
export interface PositionRow { fund_code: string; short_name: string; units: string; cost_amount: string; purchase_date: string }
export function validDate(value: string): boolean {
  const d = new Date(`${value}T00:00:00Z`);
  return /^\d{4}-\d{2}-\d{2}$/.test(value) && Number.isFinite(d.getTime()) && d.toISOString().slice(0, 10) === value && value <= today();
}
export function positionsPayload(name: string, rows: PositionRow[], userId: string) {
  if (!name.trim() || name.trim().length > 80) throw new Error('请填写 1–80 字的账本名称');
  if (!rows.length || rows.length > 10) throw new Error('请选择 1–10 只基金');
  if (new Set(rows.map(r => r.fund_code)).size !== rows.length) throw new Error('基金不能重复');
  return { name: name.trim(), user_id: userId, holdings: rows.map(r => {
    if (!/^\d{6}$/.test(r.fund_code) || !Number.isFinite(Number(r.units)) || Number(r.units) <= 0 || !Number.isFinite(Number(r.cost_amount)) || Number(r.cost_amount) <= 0 || !validDate(r.purchase_date)) {
      throw new Error(`请补全${r.short_name}的正数份额、成本和持仓日期`);
    }
    return { fund_code: r.fund_code, units: Number(r.units), cost_amount: Number(r.cost_amount), purchase_date: r.purchase_date };
  }) };
}
export interface TradeForm extends OrderTimeFields { fund_code: string; transaction_type: 'buy' | 'sell'; trade_date: string; units: string; unit_price: string; fee: string; note: string }
export function tradePayload(form: TradeForm, userId: string) {
  if (!/^\d{6}$/.test(form.fund_code)) throw new Error('请选择基金');
  const date = new Date(`${form.trade_date}T00:00:00Z`);
  if (!/^\d{4}-\d{2}-\d{2}$/.test(form.trade_date) || !Number.isFinite(date.getTime()) || date.toISOString().slice(0, 10) !== form.trade_date || form.trade_date > beijingToday()) throw new Error('请选择有效的历史或当日成交净值日');
  const units = Number(form.units), price = Number(form.unit_price), fee = Number(form.fee);
  if (!Number.isFinite(units) || units <= 0 || !Number.isFinite(price) || price <= 0 || !Number.isFinite(fee) || fee < 0) throw new Error('份额和成交净值须大于零，费用不能为负数');
  if (form.note.length > 200) throw new Error('备注不能超过 200 字');
  const timing = orderTimePayload(form);
  if (timing.submitted_at && Date.parse(timing.submitted_at) > Date.now()) throw new Error('未来下单可预览，但不能登记为已确认成交');
  if (timing.submitted_at && form.trade_date < form.submitted_date!) throw new Error('成交净值日不能早于下单日期');
  const { submitted_date, submitted_time, channel, ...trade } = form;
  return { ...trade, ...timing, units, unit_price: price, fee, user_id: userId };
}
/** 网络超时不等于没有记账。相同请求保留同一个幂等键，成功才清除。 */
export function submissionKey(key: string, payload: unknown): string {
  const fingerprint = JSON.stringify(payload);
  const pending = wx.getStorageSync(key) as { fingerprint: string; id: string } | undefined;
  if (pending?.fingerprint === fingerprint) return pending.id;
  const id = `mini-${Date.now()}-${Math.random().toString(36).slice(2, 12)}`;
  wx.setStorageSync(key, { fingerprint, id });
  return id;
}
