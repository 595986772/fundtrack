/** 原生 wx.request 分块读取 SSE。先拼完整 UTF-8 行，避免中文被网络分块切坏。 */
import { connection, networkError, ApiError } from './api';
export interface StreamEvent { event: string; data: Record<string, any> }
export class EventDecoder {
  private bytes: number[] = [];
  private event = '';
  private lines: string[] = [];
  constructor(private emit: (event: StreamEvent) => void) {}
  push(buffer: ArrayBuffer) {
    for (const byte of new Uint8Array(buffer)) {
      if (byte !== 10) { this.bytes.push(byte); if (this.bytes.length > 4 * 1024 * 1024) throw new Error('单条流式消息过大'); continue; }
      const line = decodeURIComponent(this.bytes.map(b => `%${b.toString(16).padStart(2, '0')}`).join('')).replace(/\r$/, '');
      this.bytes = [];
      if (!line) {
        if (this.lines.length) this.emit({ event: this.event || 'message', data: JSON.parse(this.lines.join('\n')) });
        this.lines = []; this.event = '';
      } else if (line.startsWith('event:')) this.event = line.slice(6).trim();
      else if (line.startsWith('data:')) this.lines.push(line.slice(5).replace(/^ /, ''));
    }
  }
}
export function streamChat(body: object, onEvent: (event: StreamEvent) => void) {
  const c = connection(); let settled = false, received = false;
  let resolve!: () => void, reject!: (error: Error) => void;
  const done = new Promise<void>((yes, no) => { resolve = yes; reject = no; });
  const fail = (error: Error) => { if (!settled) { settled = true; reject(error); } };
  const decoder = new EventDecoder(event => {
    if (settled) return;
    if (event.event === 'error') { fail(new ApiError(String(event.data.message || 'AI 运行失败'), Number(event.data.status))); return; }
    if (event.event === 'result') received = true;
    onEvent(event);
    // 完整结果已落库后，即使最后的 done 帧断开，也不能把成功回答误报为失败。
    if(received){settled=true;resolve();}
  });
  const task = wx.request({
    url: `${c.baseUrl}/api/v1/agent/chat/stream`, method: 'POST', data: body,
    enableChunked: true, responseType: 'arraybuffer', dataType:'其他', timeout: 600000,
    header: { 'Content-Type': 'application/json', ...(c.token ? { Authorization: `Bearer ${c.token}` } : {}) },
    success(res) {
      if (settled) return;
      if (res.statusCode < 200 || res.statusCode >= 300) { fail(new ApiError(`AI 请求失败 (${res.statusCode})`, res.statusCode)); return; }
      if (!received) { fail(new Error('回答传输中断，可用原问题重试；不会重复保存已完成的回答')); return; }
      settled = true; resolve();
    },
    fail(error) { fail(networkError(error, c.baseUrl)); },
  });
  if(typeof task.onChunkReceived!=='function'){fail(new Error('当前微信基础库不支持流式回答，请更新微信或使用2.20.2以上基础库'));task.abort();}
  else task.onChunkReceived(({ data }) => { try { decoder.push(data); } catch { fail(new Error('AI 数据解析失败，请重试')); task.abort(); } });
  return { done, abort() { task.abort(); fail(new Error('已停止本次回答')); } };
}
