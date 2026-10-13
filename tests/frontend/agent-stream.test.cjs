// 使用真实发送、SSE 解析和消息列表渲染代码；网络只返回本地构造的数据，不调用模型。
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const pages = require("../../app/static/workspace-pages.js");

const answer = "候选配置的历史回撤更低，但这不是未来收益保证。";
const result = {
  conversation: {id: "conversation-test"},
  user_message: {id: "user-test", role: "user", content: "解释压力测试"},
  assistant_message: {id: "assistant-test", role: "assistant", content: answer},
  run: {}, workspace: {},
};
const flush = () => new Promise(setImmediate);

function harness() {
  const nodes = new Map();
  const paints = [];
  const notices = [];
  const requests = [];
  const node = (selector) => {
    if (!nodes.has(selector)) nodes.set(selector, {
      dataset: {}, value: "", textContent: "", hidden: false,
      classList: {toggle() {}, remove() {}},
      querySelectorAll: () => [], querySelector: (child) => node(`${selector} ${child}`),
      focus() {}, setAttribute() {}, removeAttribute() {},
      get innerHTML() { return this.html || ""; },
      set innerHTML(value) { this.html = value; if (selector === "#message-list") paints.push(value); },
    });
    return nodes.get(selector);
  };
  let stream;
  const body = new ReadableStream({start(controller) { stream = controller; }});
  const context = vm.createContext({
    console, URLSearchParams, TextDecoder, AbortController, performance,
    requestAnimationFrame: (callback) => callback(),
    document: {body: node("body"), querySelector: node, querySelectorAll: () => [], getElementById: id => node(`#${id}`)},
    localStorage: {getItem: () => null, setItem() {}},
    window: {
      location: {hash: "#agent"}, addEventListener() {},
      FundScopeWorkspacePages: pages,
      FundScopeResearchRepair: {takeAuthorization: () => false},
    },
    fetch: async (...args) => {
      requests.push(args);
      // 浏览器取消 fetch 时，正在等待的 reader.read() 也会失败。
      args[1].signal.addEventListener("abort", () => stream.error(new DOMException("请求已停止", "AbortError")));
      return new Response(body);
    },
    notice: (...args) => notices.push(args),
  });
  const run = (code) => vm.runInContext(code, context);
  run(fs.readFileSync(path.join(__dirname, "../../app/static/app.js"), "utf8"));
  // 只替换本测试无关的面板、图标与格式化；保留 sendMessage/renderMessages/流解析。
  run(`
    for (const name of ["hideChatError", "resizeComposer", "refreshIcons", "setComposerRunState",
      "registerChatPortfolioResearchJobs", "updateConversationHeading", "renderRunPanel", "renderWorkspacePanel"]) {
      globalThis[name] = () => {};
    }
    showChatError = showToast = notice;
    formatMessageContent = (value) => value;
    renderMessage = (message) => '<article data-role="' + message.role + '">' + message.content + '</article>';
    loadConversations = async () => [];
  `);
  const emit = (event, payload) => stream.enqueue(new TextEncoder().encode(
    `event: ${event}\ndata: ${JSON.stringify(payload)}\n\n`,
  ));
  return {
    run, context, paints, notices, requests, emit,
    close: () => stream.close(),
    html: () => node("#message-list").innerHTML,
    send: () => run('elements.messageInput.value = "解释压力测试"; sendMessage({preventDefault() {}})'),
  };
}

test('报告追问明确传递组合与历史报告，不混入单基金或默认刷新',async()=>{
  const app=harness();
  app.run(`state.agentContextMode='portfolio';state.selectedPortfolioId='portfolio-fixture';
    state.researchReference={job_id:'report-old',portfolio_id:'portfolio-fixture'};`);
  const pending=app.send();app.emit('result',{...result,assistant_message:{...result.assistant_message,run_metadata:{research_reference:{job_id:'report-old',portfolio_id:'portfolio-fixture'}}}});app.emit('done',{});app.close();await pending;
  const body=JSON.parse(app.requests[0][1].body);
  assert.equal(body.research_job_id,'report-old');assert.equal(body.portfolio_id,'portfolio-fixture');
  assert.ok(!body.fund_code);assert.equal(body.research_refresh,false);
  assert.equal(app.run('state.researchReference.job_id'),'report-old');
});

test('持仓页分析开启新的组合问题，不继承历史报告引用，也不自动调用模型',()=>{
  const app=harness();
  app.run(`
    let newConversationCount=0;
    startNewConversation=()=>{newConversationCount++;state.currentConversationId=null;state.researchReference=null;};
    activateProductView=()=>{};
    state.selectedPortfolioId='current';state.portfolioDetail={id:'current'};
    state.researchReference={job_id:'old-report'};
    openSelectedPortfolioInAgent();
  `);
  assert.equal(app.run('newConversationCount'),1);
  assert.equal(app.run('state.agentContextMode'),'portfolio');
  assert.equal(app.run('state.researchReference'),null);
  assert.match(app.run('elements.messageInput.value'),/当前基金组合/);
  assert.equal(app.requests.length,0);
  app.run('state.sending=true;openSelectedPortfolioInAgent();');
  assert.equal(app.run('newConversationCount'),1);
  app.close();
});

test("正式答案替换预览，列表慢刷新期间也只显示一份，并继续阻止重复发送", async () => {
  const app = harness();
  let release;
  app.context.refreshList = () => new Promise((resolve) => { release = resolve; });
  app.run("loadConversations = refreshList");
  const pending = app.send();
  app.emit("answer_delta", {delta: answer});
  await flush();
  assert.match(app.html(), /streaming-message/);
  assert.equal(app.html().split(answer).length - 1, 1);
  app.emit("result", result);
  app.emit("done", {});
  app.close();
  await flush();
  assert.equal(app.run("state.sending"), true);
  assert.equal(app.run("state.replyPending"), false);
  assert.doesNotMatch(app.html(), /streaming-message|thinking-row/);
  assert.equal(app.html().split(answer).length - 1, 1);
  await app.send();
  assert.equal(app.requests.length, 1);
  release([]);
  await pending;
  assert.equal(app.run("state.sending"), false);
  assert.equal(app.run("state.messages.filter(item => item.role === 'assistant').length"), 1);
  assert.ok(app.paints.every((html) => html.split(answer).length - 1 <= 1), "任何一帧都不能出现两份答案");
});

test("连续两轮回复即使文字相同也分别保留，不按文本粗暴去重", async () => {
  const app = harness();
  // 两轮使用独立响应；每一轮的两个相同 delta 也都属于正文，不应被过滤。
  const content = "重复片段重复片段";
  app.context.fetch = async () => new Response(
    `event: answer_delta\ndata: {"delta":"重复片段"}\n\n` +
    `event: answer_delta\ndata: {"delta":"重复片段"}\n\n` +
    `event: result\ndata: ${JSON.stringify({...result, assistant_message: {...result.assistant_message, content}})}\n\n`,
  );
  await app.send();
  await app.send();
  assert.equal(app.run("state.messages.filter(item => item.role === 'assistant').length"), 2);
  assert.equal(app.html().split(content).length - 1, 2);
  assert.doesNotMatch(app.html(), /streaming-message|thinking-row/);
  app.close();
});

for (const scenario of ["error", "cancelled", "missing_result", "disconnect"]) {
  test(`${scenario} 后清除临时回答、恢复输入，历史消息不受影响`, async () => {
    const app = harness();
    app.run('state.messages = [{id: "old", role: "assistant", content: "上轮回答"}]');
    const pending = app.send();
    app.emit("answer_delta", {delta: answer});
    await flush();
    if (scenario === "disconnect") {
      app.run("state.activeStreamController.abort()");
    } else if (scenario !== "missing_result") {
      app.emit(scenario, {message: "测试中止", status: 500});
    }
    if (scenario !== "disconnect") app.close();
    await pending;
    assert.equal(app.run("state.sending || state.replyPending"), false);
    assert.equal(app.run("state.streamedAnswer"), "");
    assert.equal(app.run("elements.messageInput.value"), "解释压力测试");
    assert.match(app.html(), /上轮回答/);
    assert.ok(!app.html().includes(answer));
    assert.doesNotMatch(app.html(), /streaming-message|thinking-row/);
    assert.equal(app.notices.length, 1);
    if (scenario === "disconnect") assert.match(app.notices[0][0], /本次运行已停止/);
  });
}

test("回答保存后的会话列表刷新失败不提示重发，不残留预览", async () => {
  const app = harness();
  app.run('loadConversations = async () => {throw new Error("列表读取失败");}');
  const pending = app.send();
  app.emit("answer_delta", {delta: answer});
  app.emit("result", result);
  app.close();
  await pending;
  assert.equal(app.html().split(answer).length - 1, 1);
  assert.doesNotMatch(app.html(), /streaming-message|thinking-row/);
  assert.equal(app.run("elements.messageInput.value"), "");
  assert.equal(app.run("state.retryMessage"), "");
  assert.equal(app.notices.length, 1);
  assert.match(app.notices[0][0], /回答已保存，但会话列表刷新失败/);
});

test("基金详情只跳转统一投研入口，不建立第二个流式回答组件", () => {
  const source = fs.readFileSync(path.join(__dirname, "../../app/static/terminal.js"), "utf8");
  assert.doesNotMatch(source, /id="terminal-ai-form"|function renderAI\(/);
  assert.match(source, /actions\.openResearch\(subjectContext\(state\)\)/);
});
