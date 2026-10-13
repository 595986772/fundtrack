/** 跟踪已受理的后台任务。离开页面暂停轮询，重新进入继续读同一个 Job。 */
import { connection, scope, storageKey, identity, request, getBackgroundJob, ImportJob, errorText } from './api';
import { submissionKey } from './ledger';
interface SavedJob { id: string; user: string }
export interface JobState { busy: boolean; message: string; error: string; job: ImportJob | null }
export class JobTracker {
  private timer?: ReturnType<typeof setTimeout>;
  private version = 0;
  private reading = false;
  private count = 0;
  private startScope = scope();
  private key: string;
  constructor(name: string, private update: (state: Partial<JobState>) => void, private completed: (job: ImportJob) => void) {
    this.key = storageKey(`job:${name}`);
  }
  pause() { this.version++; if (this.timer !== undefined) clearTimeout(this.timer); this.reading = false; }
  async follow(job: ImportJob, user: string) {
    wx.setStorageSync(this.key,{id:job.id,user});this.count=0;this.reading=false;await this.resume();
  }
  async submit(path: string, data: Record<string, unknown> = {}) {
    if (this.reading || wx.getStorageSync(this.key)) { await this.resume(); return; }
    this.reading = true; const version = this.version, c = connection();
    this.update({ busy: true, message: '正在提交', error: '' });
    try {
      const user = await identity();
      if (scope() !== this.startScope || version !== this.version) return;
      const body={...data,user_id:user};
      const idempotency_key=submissionKey(`${this.key}:submit`,{path,...body});
      const result = await request<{job: ImportJob}>(path, 'POST', { ...body,idempotency_key }, c);
      // 即使用户已切走，也记住已返回的任务；不会在重进页面时重复采集。
      wx.setStorageSync(this.key, { id: result.job.id, user });
      if (scope() !== this.startScope || version !== this.version) return;
      this.reading = false; this.count = 0; await this.resume();
    } catch (error) {
      if (version === this.version && scope() === this.startScope) this.update({ busy: false, error: errorText(error), message: '提交未确认，请先检查后台任务，勿连续重复提交。' });
    } finally { if (version === this.version) this.reading = false; }
  }
  async resume() {
    const saved = wx.getStorageSync(this.key) as SavedJob | undefined;
    if (!saved || this.reading || scope() !== this.startScope) return;
    const version = this.version;
    if (this.timer !== undefined) clearTimeout(this.timer);
    this.reading = true;
    try {
      const { job } = await getBackgroundJob(saved.id, saved.user);
      if (version !== this.version || scope() !== this.startScope) return;
      const terminal = ['completed', 'failed', 'cancelled', 'waiting_input'].includes(job.status);
      this.update({ job, busy: !terminal, message: job.progress_message || `${job.status === 'completed' ? '已完成' : job.status === 'waiting_input' ? '等待确认' : '处理中'} · ${job.progress_percent}%`, error: job.error || '' });
      if (terminal) {
        // interrupt 不是完成；保留任务 ID，重新打开后还可继续人工确认。
        if(job.status !== 'waiting_input') { wx.removeStorageSync(this.key); wx.removeStorageSync(`${this.key}:submit`); }
        if (job.status === 'completed' || job.status === 'waiting_input') this.completed(job);
      } else if (this.count++ < 300) this.timer = setTimeout(() => { void this.resume(); }, 3000);
      else this.update({ busy: false, message: '任务仍在后台运行，点击检查进度继续查看。' });
    } catch (error) { if (version === this.version && scope() === this.startScope) this.update({ busy: false, error: errorText(error) }); }
    finally { if (version === this.version) this.reading = false; }
  }
}
