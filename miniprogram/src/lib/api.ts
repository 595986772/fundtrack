/** 微信和浏览器预览共用的网络层。令牌只发给用户保存的后端，不写进 URL。 */
export interface Connection { baseUrl: string; userId: string; token: string }
const KEY = 'fundscope-mini-connection';
export function connection(): Connection {
  return wx.getStorageSync(KEY) || { baseUrl: 'http://127.0.0.1:8000', userId: 'chen', token: '' };
}
export function normalizeUrl(value: string): string {
  const url = value.trim().replace(/\/+$/, '');
  // 开发机允许 HTTP；部署时必须换成微信后台登记的 HTTPS 域名。
  if (!/^https?:\/\/[a-zA-Z0-9.[\]:-]+(?::\d+)?$/.test(url)) {
    throw new Error('请输入后端地址，例如 http://127.0.0.1:8000');
  }
  return url;
}
export function saveConnection(value: Connection) {
  wx.setStorageSync(KEY, { ...value, baseUrl: normalizeUrl(value.baseUrl) });
}
export function scope(): string {
  const c = connection();
  return `${c.baseUrl}|${c.userId}|${c.token}`;
}
export function storageKey(name: string): string {
  const c = connection();
  return `fundscope-mini:${c.baseUrl}:${c.userId}:${name}`;
}
export class ApiError extends Error {
  constructor(message: string, public status = 0) { super(message); }
}
/** 微信的 fail 并不等于服务器关闭：域名限制、网络权限、超时都会进入这里。 */
export function networkError(error: Partial<WechatMiniprogram.RequestFailCallbackErr>, baseUrl: string): ApiError {
  const raw = error.errMsg || '';
  let message = '无法连接后端，请检查服务地址和当前网络';
  if (/not in domain list|url not in domain|不在.*合法域名/i.test(raw)) {
    message = '微信未放行此请求域名，请检查当前调试模式和域名配置';
  } else if (/local network.*denied|local network.*prohibited|local network.*permission|本地网络.*拒绝/i.test(raw)) {
    message = '微信没有本地网络访问权限，请在手机系统设置中允许微信访问本地网络';
  } else if (/timeout|timed out/i.test(raw)) {
    message = '连接后端超时，请检查服务地址、同一 Wi-Fi 连接及 VPN 的局域网设置';
  } else if (/connection refused|connection_refused|econnrefused/i.test(raw)) {
    message = '连接被拒绝，请确认地址与端口对应的后端服务已启动';
  } else if (/name_not_resolved|name resolution|resolve host|dns/i.test(raw)) {
    message = '无法解析后端域名，请检查地址拼写和网络 DNS';
  } else if (/certificate|ssl|tls/i.test(raw)) {
    message = 'HTTPS 连接未通过，请检查服务协议与证书是否有效';
  }
  // 不猜测运行设备；仅在请求确实发往回环地址时提醒真机与电脑的区别。
  if (/^https?:\/\/(?:localhost|127(?:\.\d{1,3}){3}|\[::1\])(?::\d+)?\/?$/i.test(baseUrl)) {
    message += '。当前使用回环地址；手机真机请改填 Mac 的局域网 IP';
  }
  // 保留微信提供的诊断信息，但不展示请求 URL、令牌或密钥，方便截图排查。
  const detail = raw.replace(/https?:\/\/[^\s'"<>]+/gi, '[请求地址]')
    .replace(/\bBearer\s+\S+/gi, 'Bearer [已隐藏]')
    .replace(/\bsk-[\w.-]+/gi, '[密钥已隐藏]').replace(/\s+/g, ' ').trim().slice(0, 240);
  const code = Number.isFinite(error.errno) ? `错误码 ${error.errno}` : '';
  const diagnostic = [detail, code].filter(Boolean).join('；');
  return new ApiError(diagnostic ? `${message}。${diagnostic}` : message);
}
export function request<T>(path: string, method: 'GET' | 'POST' | 'PUT' | 'DELETE' = 'GET', data?: unknown, c = connection(), timeout = 60000): Promise<T> {
  return sendRequest<T>(`/api/v1${path}`, method, data, c, timeout);
}
/** 后台任务沿用现有根路径，不能误拼成 /api/v1/background-jobs。 */
export function getBackgroundJob(jobId: string, userId: string, c = connection()) {
  return sendRequest<{ job: ImportJob }>(`/background-jobs/${encodeURIComponent(jobId)}${queryUser(userId)}`, 'GET', undefined, c);
}
function sendRequest<T>(path: string, method: 'GET' | 'POST' | 'PUT' | 'DELETE', data: unknown, c: Connection, timeout = 60000): Promise<T> {
  return new Promise((resolve, reject) => {
    wx.request({
      url: `${c.baseUrl}${path}`, method, data: data as object,
      timeout,
      header: { 'Content-Type': 'application/json', ...(c.token ? { Authorization: `Bearer ${c.token}` } : {}) },
      success(res) {
        if (res.statusCode >= 200 && res.statusCode < 300) { resolve(res.data as T); return; }
        const body = res.data as { detail?: string | Array<{ msg: string }> };
        const detail = typeof body?.detail === 'string' ? body.detail : Array.isArray(body?.detail) ? body.detail.map(item => item.msg).join('；') : '';
        reject(new ApiError(detail || `请求失败 (${res.statusCode})`, res.statusCode));
      },
      fail(error) { reject(networkError(error, c.baseUrl)); },
    });
  });
}
/** 先确认身份再读私有账本；过期令牌不会被偷偷降级为本地用户。 */
export async function identity() {
  const c = connection();
  const state = await request<{ authenticated: boolean; auth_required: boolean; user?: { user_id: string } }>('/auth/me');
  if (!state.authenticated && (c.token || state.auth_required)) throw new ApiError('请到「我的」重新登录', 401);
  return state.user?.user_id || c.userId;
}
export function queryUser(userId: string) { return `?user_id=${encodeURIComponent(userId)}`; }
export function errorText(error: unknown) { return error instanceof Error ? error.message : '请求未完成，请重试'; }
export function confirm(title: string, content: string): Promise<boolean> {
  return new Promise(resolve => wx.showModal({ title, content, confirmColor: '#8a6916', success: r => resolve(r.confirm), fail: () => resolve(false) }));
}
export function notify(title: string) { wx.showToast({ title, icon: 'none' }); }

/** API 契约只描述页面需要的字段；金额和盈亏由后端账本统一计算。 */
export interface Fund {
  fund_code: string; short_name: string; fund_type: string; latest_nav: number;
  latest_nav_date: string; daily_growth_rate_pct: number | null;
  return_30d_pct: number | null; return_3m_pct: number | null;
}
export interface Holding {
  fund_code: string; short_name: string; units: number | null; cost_amount: number | null;
  latest_nav: number; latest_nav_date: string; current_value: number | null;
  unrealized_pnl_amount: number | null; total_pnl_amount: number | null;
}
export interface Portfolio {
  id: string; name: string; position_mode: 'actual' | 'allocation' | 'empty'; holdings: Holding[];
  total_cost_amount: number | null; total_market_value: number | null;
  unrealized_pnl_amount: number | null; realized_pnl_amount: number; total_pnl_amount: number | null;
}
export interface FundList { funds: Fund[]; source: string; data_mode: string; data_updated_at: string | null }
/** 目录只说明基金存在；档案和净值导入完成后才能拿来记账、分析。 */
export interface DirectoryFund { fund_code: string; short_name: string; fund_type: string; is_imported: boolean }
export interface DirectoryResult { funds: DirectoryFund[]; source: string; data_mode: string; message: string; catalog_updated_at: string | null }
export interface ImportJob { id: string; status: string; progress_percent: number; progress_message?: string; result?: Record<string, unknown> | null; error?: string | null }
