/** 单基金详情只画已披露的单位净值，分红拆分提示随数据一起展示。 */
import { request, errorText, Fund } from '../../lib/api';
import { number, signed, tone, NavPoint, navWindow, navOption } from '../../lib/format';
import { analysis, researchDraft } from '../../lib/navigation';
const echarts = require('../../components/ec-canvas/echarts.js');
interface Detail { profile: { short_name: string; fund_code: string; fund_type: string; management_company?: string }; latest_nav: number; latest_nav_date: string; daily_growth_rate_pct: number | null; nav_points: NavPoint[]; source: string; data_mode: string }
Page({
  data: { loading: true, error: '', name: '', code: '', type: '', company: '', nav: '--', date: '', day: '--', dayTone: '', month: '--', quarter: '--', source: '', period: 30, periods: [{ days: 30, label: '近30日' }, { days: 90, label: '近3月' }, { days: 180, label: '近6月' }, { days: 365, label: '近1年' }], points: [] as NavPoint[], visiblePoints: [] as NavPoint[], ec: { lazyLoad: true } },
  _chart: null as null | { setOption(option: object, replace?: boolean): void; dispose(): void },
  _alive: true,
  _initializing: false,
  onLoad(query: Record<string, string>) { this.setData({ code: query.code || '' }); void this.load(); },
  onUnload() { this._alive = false; this._chart?.dispose(); this._chart = null; },
  async load() {
    this.setData({ loading: true, error: '' });
    try {
      if (!/^\d{6}$/.test(this.data.code)) throw new Error('基金代码无效');
      const [d, list] = await Promise.all([request<Detail>(`/funds/${this.data.code}`), request<{ funds: Fund[] }>('/funds')]);
      if (!this._alive) return;
      const summary = list.funds.find(f => f.fund_code === this.data.code);
      this.setData({ name: d.profile.short_name, type: d.profile.fund_type, company: d.profile.management_company || '', nav: number(d.latest_nav, 4), date: d.latest_nav_date, day: signed(d.daily_growth_rate_pct, '%'), dayTone: tone(d.daily_growth_rate_pct), month: signed(summary?.return_30d_pct, '%'), quarter: signed(summary?.return_3m_pct, '%'), source: d.data_mode === 'fixture' ? '教学样本数据' : d.source, points: d.nav_points, loading: false }, () => this.draw());
    } catch (error) { if (this._alive) this.setData({ error: errorText(error), loading: false }); }
  },
  period(e: WechatMiniprogram.TouchEvent) { this.setData({ period: Number(e.currentTarget.dataset.days) }, () => this.draw()); },
  analysis(e: WechatMiniprogram.TouchEvent) { analysis(e.currentTarget.dataset.kind,this.data.code); },
  ask() { researchDraft(`请分析${this.data.name}的风险、持仓和近期市场证据。区分事实与推断，不自动采集。`,{fund_code:this.data.code}); },
  draw() {
    const points = navWindow(this.data.points, this.data.period);
    this.setData({ visiblePoints: points }, () => {
      if (!this._alive) return;
      if (this._chart) { this._chart.setOption(navOption(points), true); return; }
      if (this._initializing) return;
      // lazyLoad 保证页面已完成布局；不是自己实现图表引擎。
      const component = this.selectComponent('#nav-chart');
      if (!component) return;
      this._initializing = true;
      component?.init((canvas: object & { setChart(chart: unknown): void }, width: number, height: number, dpr: number) => {
        const chart = echarts.init(canvas, undefined, { width, height, devicePixelRatio: dpr });
        if (!this._alive) { chart.dispose(); return chart; }
        canvas.setChart(chart); chart.setOption(navOption(this.data.visiblePoints)); this._chart = chart;
        this._initializing = false;
        return chart;
      });
    });
  },
});
