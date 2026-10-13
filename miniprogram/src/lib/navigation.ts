/** 页面跳转只传业务标识，不携带金额、令牌，也不自动发送 AI 问题。 */
import { storageKey } from './api';
export function settings() { wx.navigateTo({ url: '/pages/account/index' }); }
export function analysis(kind: string, id = '', portfolio = '') {
  if (kind === 'research') { wx.navigateTo({url:portfolioReportUrl(id,portfolio)}); return; }
  wx.navigateTo({ url: `/pages/analysis/index?kind=${encodeURIComponent(kind)}&id=${encodeURIComponent(id)}&portfolio=${encodeURIComponent(portfolio)}` });
}
export function portfolioReportUrl(id = '', portfolio = '') {
  return `/pages/portfolio-report/index?id=${encodeURIComponent(id)}&portfolio=${encodeURIComponent(portfolio)}`;
}
export function researchDraft(message: string, context: { fund_code?: string; portfolio_id?: string; research_job_id?: string; report_label?: string; fund_report_ids?: string[] } = {}) {
  wx.setStorageSync(storageKey('research-draft'), { message: message.slice(0, 800), ...context });
  wx.switchTab({ url: '/pages/research/index' });
}
export function copySource(url: string) {
  // 微信不能任意打开网页；复制公开来源供用户核验，不接收脚本或本地文件路径。
  if (/^https?:\/\/[^\s]+$/.test(url)) wx.setClipboardData({ data: url });
}
