/** 前端只组装带时区的下单时间，节假日与收益日期全部由后端判断。 */
export interface OrderTimeFields { submitted_date?: string; submitted_time?: string; channel?: 'off_exchange' | 'exchange' }
export interface TradeTiming {
  status: 'estimated' | 'manual_required' | 'calendar_unavailable';
  submitted_at: string; reason: string; nav_date: string | null; confirmation_date: string | null;
  first_return_nav_date: string | null; last_return_nav_date: string | null;
  visibility: string; warnings: string[];
}
export function beijingToday(): string { return new Date(Date.now() + 8 * 3600000).toISOString().slice(0, 10); }
export function orderTimePayload(form: OrderTimeFields) {
  const date = form.submitted_date || '', time = form.submitted_time || '';
  if (!date && !time) return {};
  const parsed = new Date(`${date}T00:00:00Z`);
  if (!/^\d{4}-\d{2}-\d{2}$/.test(date) || !Number.isFinite(parsed.getTime()) || parsed.toISOString().slice(0, 10) !== date || !/^([01]\d|2[0-3]):[0-5]\d$/.test(time)) {
    throw new Error('请完整选择下单日期和北京时间，或清除下单时间');
  }
  // +08:00 写进请求，手机在海外或服务器使用 UTC 也不会错过 15:00 分界。
  return { submitted_at: `${date}T${time}:00+08:00`, channel: form.channel || 'off_exchange' };
}
