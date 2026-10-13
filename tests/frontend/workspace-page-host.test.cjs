// DOM 替身记录节点身份和写入次数；测试不会请求真实行情、数据库或模型。
const test = require("node:test");
const assert = require("node:assert/strict");
const {create} = require("../../app/static/workspace-page-host.js");

function setup() {
  const document = {createElement: () => new Node()};
  class Node {
    constructor() { this.ownerDocument = document; this.dataset = {}; this.children = []; this.writes = 0; }
    set innerHTML(value) {
      this.html = value;
      this.writes++;
      if (value.includes("data-workspace-header")) this.children = [new Node(), new Node()];
    }
    get innerHTML() { return this.html; }
    querySelector(selector) { return this.children[selector === "[data-workspace-header]" ? 0 : 1]; }
    setAttribute() {}
    append(node) { this.children.push(node); }
  }
  const element = new Node();
  const renders = [];
  const data = {holdings: "交易表单", monitoring: "任务进度 0%", optimizer: "配置表单"};
  const host = create({element, pages: Object.keys(data).map(id => ({id, label: id})), render: id => {
    renders.push(id); return data[id];
  }});
  const update = (options = {}) => host.update({key: "chen:portfolio-a", activePage: "holdings", header: "组合 A", ...options});
  const panel = (id) => element.children[1].children.find(node => node.dataset.workspacePage === id);
  return {element, renders, data, update, panel};
}

test("首次只挂载当前页，切走再回来保留原节点和未提交输入", () => {
  const h = setup();
  h.update();
  assert.deepEqual(h.renders, ["holdings"]);
  const original = h.panel("holdings");
  original.draft = "未提交交易备注";
  h.update({activePage: "optimizer"});
  assert.equal(original.hidden, true);
  h.update();
  assert.equal(h.panel("holdings"), original);
  assert.equal(original.draft, "未提交交易备注");
  assert.equal(original.writes, 1);
  assert.equal(h.panel("optimizer").hidden, true);
});

test("监控进度只使监控页失效，不重绘正在输入的持仓页", () => {
  const h = setup();
  h.update();
  h.update({activePage: "monitoring"});
  h.update();
  h.data.monitoring = "任务进度 80%";
  h.update({changedPages: ["monitoring"]});
  assert.equal(h.panel("holdings").writes, 1);
  assert.equal(h.panel("monitoring").innerHTML, "任务进度 0%");
  h.update({activePage: "monitoring"});
  assert.equal(h.panel("monitoring").innerHTML, "任务进度 80%");
  assert.equal(h.panel("monitoring").writes, 2);
});

test("重复数据通知不销毁页面；离开整个组合工作区后不渲染隐藏页", () => {
  const h = setup();
  h.update();
  h.update({changedPages: null});
  assert.equal(h.panel("holdings").writes, 1);
  h.data.holdings = "更新后的交易表单";
  h.update({changedPages: ["holdings"], visible: false});
  assert.equal(h.panel("holdings").writes, 1);
  h.update();
  assert.equal(h.panel("holdings").innerHTML, "更新后的交易表单");
});

test("换组合或用户后创建独立页面树，不带入上一份编辑状态", () => {
  const h = setup();
  h.update();
  const original = h.panel("holdings");
  original.draft = "用户 A 的草稿";
  h.update({key: "other:portfolio-b", header: "组合 B"});
  assert.notEqual(h.panel("holdings"), original);
  assert.equal(h.panel("holdings").draft, undefined);
  assert.equal(h.element.children[0].innerHTML, "组合 B");
});
