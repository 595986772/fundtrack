# FundScope Agent Evals

`fund_agent_v2.json` 是当前产品的版本化业务回归集。它不保存一段唯一的标准回答，
而是保存更稳定的检查条件：预期路由、工具、工具 JSON 事实、回答要点和证据要求。

## 为什么固定数据

基金净值、市场状态和新闻每天变化。评测运行时，`eval_context.py` 只对当前协程启用
固定数据模式，让以下文件成为权威输入：

- `data/fund_fixtures/fund_profiles.json`
- `data/fund_fixtures/fund_nav.csv`
- `data/market_fixtures/market_snapshot.json`
- `data/market_fixtures/market_events.json`

这四个文件的内容会生成 SHA-256 短指纹。只有 `suite_id`、case 集合和数据指纹全部
相同的两次成绩才适合直接比较；单题、冒烟集和完整集各自拥有不同 `case_set_id`。

## 场景字段

- `expected_route`：Python 请求路由应该选择的领域。
- `required_tools`：模型至少应调用的工具。
- `forbidden_tools`：本场景不应重复或错误调用的工具。
- `tool_result_assertions`：直接检查 Tool JSON 中的固定事实。
- `required_answer_groups`：最终回答必须覆盖的业务要点。
- `require_evidence_reference`：回答必须引用工具真实返回的证据 ID。
- `tags`：`smoke` 表示适合日常快速回归。

支持的事实断言操作符为 `equals`、`not_equals`、`contains`、`one_of`、`gte`、
`lte`、`between` 和 `exists`。字段路径只允许 `a.b.0.c` 这种简单形式，不执行脚本。

## 运行方式

Loop 入口验收使用独立的 `agent_loop_acceptance_v1.json`，不是在线付费套件。
运行 `python scripts/eval_agent_loop_acceptance.py` 可在临时数据库中检查 13 个固定场景，
不调用真实模型。它复用本目录的评分及报告结构，新增精确的 `expected_tool_errors`
和 `expected_stop_reasons` 契约；未声明错误的旧场景仍然要求工具全部成功。
详见 [Agent 循环产品入口验收](../../docs/Agent循环产品入口验收.md)。

不调用模型的评测框架测试：

```bash
conda run --no-capture-output -n dl \
  python -m unittest tests.test_agent_evals -v
```

真实运行一个场景：

```bash
curl -X POST http://127.0.0.1:8000/api/v1/evals/runs \
  -H 'Content-Type: application/json' \
  -d '{"user_id":"chen","case_ids":["assessment-index"]}'
```

真实运行会调用已配置的大模型。评测不会自动调优；先看失败维度，再修改对应代码，
最后使用同一个数据指纹重跑并比较分数。

## 从真实反馈生成草稿

聊天回答收到“没帮助”后，会先进入工作台右侧“评测 -> 待审核”。人工点击“保留候选”
后，它才会出现在“已保留”列表。此时可以创建草稿并填写预期路由、工具、回答关键事实
和证据要求。

草稿写入 `data/evals/drafts/`，不会被评测运行器加载，也不会修改
`fund_agent_v2.json`。草稿可能包含真实问题、原回答和用户修正，因此 JSON 默认被 Git
忽略。合并前需要人工完成以下检查：

1. 确认问题可以用固定 fixture 稳定复现。
2. 为关键工具结果补充 `tool_result_assertions`。
3. 检查答案断言不是只匹配某一种措辞。
4. 将草稿中的 `scenario` 复制到正式套件，并提升套件版本。
5. 先单题运行，再运行完整回归集。

## 2026-08-29 调优验收

同一套 20 条场景、同一数据指纹 `sha256:2c2caeb39399efd0` 的本地结果：

| 阶段 | 严格通过 | 得分 | Tool Calls | Token |
|---|---:|---:|---:|---:|
| 初始基线 | 13/20 | 96.8 | 26 | 91,002 |
| 调优验收 | 20/20 | 100.0 | 20 | 62,967 |

本轮修复包括市场子路由、事件与 Assessment 有界 DTO、多值参数归一化、组合证据提示、
窄路由完成条件、Grounding Guard，以及千分位数字和否定语境的评分纠正。模型输出仍
可能存在随机性，所以验收重点同时观察事实维度、工具错误、重复调用和 Token，而不只
看总分。
