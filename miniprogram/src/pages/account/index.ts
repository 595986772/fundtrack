/** 本地试用和真实账号登录分开，切换地址时不把旧服务器令牌带过去。 */
import { connection, saveConnection, normalizeUrl, request, errorText, confirm, notify } from '../../lib/api';
Page({
  data: { baseUrl: '', userId: '', password: '', loggedIn: false, status: '未连接', error: '', busy: false },
  onShow() { const c = connection(); this.setData({ baseUrl: c.baseUrl, userId: c.userId, password: '' }); void this.check(); },
  field(e: WechatMiniprogram.CustomEvent) { this.setData({ [e.currentTarget.dataset.field]: e.detail.value }); },
  async check() {
    this.setData({ busy: true, error: '' });
    try {
      const state = await request<{ authenticated: boolean; auth_required: boolean; user?: { display_name: string } }>('/auth/me');
      this.setData({ loggedIn: state.authenticated, status: state.authenticated ? `已登录 · ${state.user?.display_name}` : connection().token ? '登录已失效，请重新登录' : state.auth_required ? '服务需要登录' : '本地开发模式 · 未启用登录限制' });
    } catch (error) { this.setData({ error: errorText(error), status: '连接失败', loggedIn: false }); }
    finally { this.setData({ busy: false }); }
  },
  async connect() {
    if (this.data.busy) return;
    this.setData({ busy: true, error: '' });
    try {
      const c = { baseUrl: normalizeUrl(this.data.baseUrl), userId: this.data.userId.trim(), token: '' };
      if (!/^[\w-]{1,50}$/.test(c.userId)) throw new Error('本地用户 ID 只能含字母、数字、下划线或短横线');
      const state = await request<{ auth_required: boolean }>('/auth/me', 'GET', undefined, c);
      if (state.auth_required) throw new Error('此服务必须登录，请使用下方账号密码登录');
      if (connection().token && !await confirm('切换本地模式', '切换后将退出当前小程序账号，查看所填用户的本地账本。')) return;
      saveConnection(c); notify('本地连接已保存'); await this.check();
    } catch (error) { this.setData({ error: errorText(error) }); }
    finally { this.setData({ busy: false }); }
  },
  async login() {
    if (this.data.busy) return;
    this.setData({ busy: true, error: '' });
    try {
      const c = { baseUrl: normalizeUrl(this.data.baseUrl), userId: this.data.userId.trim(), token: '' };
      if (!c.userId || !this.data.password) throw new Error('请填写账号和密码');
      const state = await request<{ access_token: string; user: { user_id: string } }>('/auth/token', 'POST', { user_id: c.userId, password: this.data.password }, c);
      saveConnection({ ...c, userId: state.user.user_id, token: state.access_token });
      this.setData({ password: '' }); notify('登录成功'); await this.check();
    } catch (error) { this.setData({ error: errorText(error) }); }
    finally { this.setData({ busy: false }); }
  },
  async logout() {
    if (!await confirm('退出登录', '将撤销当前小程序会话，不会删除持仓。')) return;
    this.setData({ busy: true, error: '' });
    try { await request('/auth/logout', 'POST'); saveConnection({ ...connection(), token: '' }); await this.check(); }
    catch (error) { this.setData({ error: errorText(error) }); }
    finally { this.setData({ busy: false }); }
  },
});
