/** 不把缺失值写成零。红涨绿跌沿用 A 股习惯，不代表收益承诺。 */
export function number(value: number | null | undefined, digits = 2): string {
  if (value == null || !Number.isFinite(value)) return '--';
  const [whole, decimal] = value.toFixed(digits).split('.');
  return whole.replace(/\B(?=(\d{3})+(?!\d))/g, ',') + (decimal ? `.${decimal}` : '');
}
export function signed(value: number | null | undefined, suffix = ''): string {
  return value == null ? '--' : `${value > 0 ? '+' : ''}${number(value)}${suffix}`;
}
export function tone(value: number | null | undefined): string { return value ? (value > 0 ? 'up' : 'down') : ''; }
export function amount(value: number | null | undefined): string {
  if (value == null) return '--';
  return Math.abs(value) >= 1e12 ? `${number(value / 1e12)} 万亿` : `${number(value / 1e8)} 亿`;
}
/** 交易窗口统一用北京时间；无时区的旧数据保留原文，不猜测它属于哪个时区。 */
export function chinaTime(value: unknown): string {
  if (!value) return '--';
  const raw=String(value),timestamp=Date.parse(raw);
  if(!/(Z|[+-]\d{2}:?\d{2})$/.test(raw)||!Number.isFinite(timestamp))return raw;
  return new Date(timestamp+8*3600000).toISOString().slice(0,16).replace('T',' ')+' 北京时间';
}
export function today(): string {
  const d = new Date();
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
}
export interface NavPoint { nav_date: string; unit_nav: number }
/** 按最新净值日往前取自然日，不把“30 日”误算成 30 个交易日。 */
export function navWindow(points: NavPoint[], days: number): NavPoint[] {
  const sorted = [...points].sort((a, b) => a.nav_date.localeCompare(b.nav_date));
  if (!sorted.length) return [];
  const cutoff = Date.parse(sorted[sorted.length - 1].nav_date) - days * 86400000;
  return sorted.filter(p => Date.parse(p.nav_date) >= cutoff);
}
/** 同一份 ECharts 配置用于原生 Canvas 和浏览器预览，曲线只画已披露净值。 */
export function navOption(points: NavPoint[]) {
  return {
    animation: false, backgroundColor: 'transparent',
    grid: { left: 52, right: 10, top: 20, bottom: 32 },
    tooltip: { trigger: 'axis', confine: true },
    xAxis: { type: 'category', data: points.map(p => p.nav_date), boundaryGap: false,
      axisLabel: { color: '#989aa7', fontSize: 10 }, axisLine: { lineStyle: { color: '#303138' } }, axisTick: { show: false } },
    yAxis: { type: 'value', scale: true, axisLabel: { color: '#989aa7', fontSize: 10 }, splitLine: { lineStyle: { color: '#303138' } } },
    series: [{ type: 'line', data: points.map(p => p.unit_nav), showSymbol: false,
      lineStyle: { color: '#edc85b', width: 2 }, itemStyle: { color: '#edc85b' } }],
  };
}
