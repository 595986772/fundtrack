/** 交易页和基金库之间的选基草稿。切换 tab 会销毁交易页，因此暂存表单再带回。 */
import { storageKey } from './api';
import { TradeForm } from './ledger';

export interface TradeSelection {
  portfolioId: string;
  portfolioName: string;
  form: TradeForm;
  stage: 'choosing' | 'returned';
}
const key = () => storageKey('trade-fund-selection');

export function currentTradeSelection(): TradeSelection | null {
  const value = wx.getStorageSync(key()) as TradeSelection | undefined;
  return value?.stage === 'choosing' ? value : null;
}

export function openTradeFundLibrary(portfolioId: string, portfolioName: string, form: TradeForm) {
  wx.setStorageSync(key(), { portfolioId, portfolioName, form, stage: 'choosing' });
  wx.switchTab({ url: '/pages/assets/index' });
}

export function returnToTrade(fundCode?: string) {
  const selection = currentTradeSelection();
  if (!selection) return;
  const changed = fundCode && fundCode !== selection.form.fund_code;
  // 不能把上一只基金的成交份额、价格带到新基金；未选新基金返回则保留原草稿。
  const form = changed ? { ...selection.form, fund_code: fundCode, units: '', unit_price: '', fee: '0' } : selection.form;
  wx.setStorageSync(key(), { ...selection, form, stage: 'returned' });
  wx.navigateTo({ url: `/pages/transactions/index?id=${encodeURIComponent(selection.portfolioId)}` });
}

export function takeTradeSelection(portfolioId: string): TradeForm | null {
  const value = wx.getStorageSync(key()) as TradeSelection | undefined;
  if (value?.stage !== 'returned' || value.portfolioId !== portfolioId) return null;
  wx.removeStorageSync(key());
  return value.form;
}
