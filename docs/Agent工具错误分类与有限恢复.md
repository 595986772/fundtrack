# Agent 工具错误分类与有限恢复

更新时间：2026-09-30。接续[循环进展与完成检查](Agent循环进展与完成检查.md)和[聊天共享时限](Agent聊天共享时限与超时收尾.md)。

## 通俗理解

同样是“没有拿到结果”，原因可能不同：数据源暂时忙、基金代码写错、没有净值记录，或者根本没有权限。不能对这些情况统一重试，更不能把“没查到”解释为“没有风险”。

例如比较两只基金：A 已成功，B 临时连接失败。后端只重试 B 一次，然后把各自的结果交给模型。B 仍失败就留下明确缺口，不重复查询 A，不生成假装完整的比较。

## 实际流程

```text
模型发出 tool_call
  -> 注入可信用户/当前组合 -> Hook 与任务授权
  -> 检查参数与本轮失败记录
  -> 调用工具 -> 成功：原结果返回
             -> 失败：分类
                 -> 获准的临时故障：短暂等待、再次检查权限和时间、重试一次
                 -> 其他失败：结构化错误返回，不自动重试
  -> PostToolUse 如实记为成功或失败
  -> 模型读取结果，修参/追问/说明缺口/使用已成功资料
```

## 错误类型与处理

| error_code | 含义 | 后端行为 / 模型下一步 |
| --- | --- | --- |
| `invalid_arguments` | 不符合参数 Schema 或明确的参数规则 | 返回字段及格式提示；允许一次改参；再次失败后请求用户确认 |
| `resource_not_found` | 例如基金尚未导入、组合不存在或不可见 | 核对对象，不猜代码，不自动导入 |
| `data_unavailable` | 缺少净值、持仓、流水或事件快照 | 说明缺口，不能当成零值，不自行触发刷新 |
| `source_timeout` / `source_unavailable` | 类型明确的网络超时、断连、部分 HTTP 暂时故障或数据库连接暂不可用 | 仅获准只读工具可自动重试一次 |
| `source_rate_limited` | 数据源返回 HTTP 429 | 尊重 Retry-After；等待过长或剩余时间不足时不重试 |
| `permission_denied` | 工具、资源或来源拒绝访问 | 停止访问，不换工具绕过 |
| `business_error` | 旧工具返回的未细分业务失败 | 如实报错，不从错误文字猜测可以重试 |
| `internal_error` | 未分类的代码/服务异常 | 不重试，返回有限错误说明供排查 |

举例，风险工具缺少净值时会返回：

```json
{
  "ok": false,
  "fund_code": "110020",
  "error": "当前区间净值点不足，至少需要 3 条记录",
  "error_code": "data_unavailable",
  "retryable": false,
  "suggested_action": "report_data_gap"
}
```

`retryable=false` 表示模型不应继续重放相同查询。自动重试已在后端完成或被政策拒绝，不把是否重试交给模型自行决定。`correct_arguments` 允许提交修正后的参数，不允许重复相同的错误参数。

成功查询返回空事件列表仍是成功：它表示“本次条件在当前来源中未命中”，不是来源崩溃，也不能推出全市场没有相关新闻。

## 重试边界

- 白名单只有七个工具：基金档案、净值摘要、基金风险、组合研判、组合业绩、组合穿透、已保存事件查询，具体名称在 `tool_recovery.py` 的 `REPLAY_SAFE_READS`。
- 不自动重试交易/文件写入、后台建单、模型分析、研究补查、RAG 和 MCP 工具。名字叫 `get_*` 或 MCP 标记 `readOnly`，都不自动获得重试资格。
- 只对有明确异常类型的临时故障重试；工具正常返回 `ok:false` 时不追加通用重试，避免与内部业务恢复叠加。即使回执自带 `retryable:true` 也不能改变后端政策。
- 每次物理调用前检查取消和共享时限，重试前重新执行 Hook 和任务授权。额度、执行资格、总时限等控制异常向上层传播，沿用现有治理，不能当作 HTTP 429 自动重试。
- 默认退避 0.2 秒，接受 Retry-After 秒数或日期。要求等待超过 2 秒，或工作时间不足以容纳等待时，直接返回失败；不会缩短供应商要求的等待再请求。
- 同一 Agent 调用内，相同规范化参数的失败只保留一份回执。换 tool_call_id、显式填写默认参数、增加 Schema 忽略的字段，都不能重新获得重试次数。锁合并并发的同一失败查询。
- 参数修正按工具和可信身份限一次。第二组参数仍不合法，本请求内该工具停止继续执行；用户确认后可发起新请求。
- 缓存仍由第一轮 LoopGuard 管理，失败不进入成功缓存。每次进入 `ToolCallingAgentService.chat()` 都新建状态，嵌套调用也不复用父状态；普通聊天恢复仍受共享时限限制，后台 Job 沿用自己的时限。
- 本轮接入主 Agent 的公共工具执行器。旧的 s06 Subagent/Teammate 有独立文件与 RAG 工具执行管线，本轮不迁移那条管线，也不声称所有子流程已具备同样的错误协议。
- 非白名单工具发生超时，返回 `verify_operation_state`，提示核对既有操作状态。该提示不代表系统已经完成状态核验，更不代表可以安全再次提交。

## 诊断保存在哪里

每个 `run.tool_calls[]` 新增 `recovery_trace`，记录实际尝试次数、耗时、错误代码、退避、重复调用拦截及停止原因。内部重试不算作新的模型轮次，也不重复生成 ToolMessage。

这些信息同时进入 PostToolUse Hook、助手消息的 `run_metadata.tool_calls` 和现有运行观测表的工具记录，不新增数据库表。诊断不保存传输异常中的完整 URL、供应商请求体或凭证，也不代替 Token 费用账本。

原来某些工具正常返回 `ok:false` 会被记录为成功，现在日志、ToolMessage 中的 JSON 和 Loop 覆盖判断保持一致。长错误被 Context Compact 压缩时也保留失败代码及处理建议，不会变成 `ok:true`。

## 如何测试

保持已有 conda 环境，在项目根目录执行：

```bash
AI_CONTROL_BACKEND=memory TRACE_ENABLED=false python -m unittest tests.test_tool_recovery tests.test_agent_loop_guard tests.test_chat_execution_budget -v
```

测试使用假模型、假来源、MockTransport 和临时数据库，不消耗收费模型，不修改真实持仓。覆盖临时失败后成功、重试耗尽、并发重复、修参上限、权限撤销、Retry-After、额度拒绝、用户取消、部分成功、多基金覆盖检查和错误压缩。

页面继续使用原来的 AI 助手，无新增按钮、依赖或环境变量。重启后端即可启用。不要为了验证故障主动破坏真实数据源；自动化测试更容易精确复现这些分支。

## 源码入口

| 文件 | 职责 |
| --- | --- |
| `app/services/tool_recovery.py` | 分类、结构化回执、重试白名单、失败去重、修参上限 |
| `app/services/agent_tool_executor.py` | 可信参数、权限重查、结果审计 |
| `app/services/tool_agent.py`、`prompt_builder.py` | 绑定独立恢复状态，把错误协议加入实际提示词和 Prompt Trace |
| `app/services/errors.py` | 业务错误代码基类，保留原 RuntimeError 兼容性 |
| `funds.py`、`fund_risk.py`、`portfolio_performance.py` 等 | 在真正发现缺数据的分支给出稳定代码 |
| `context_compactor.py`、`observability.py`、`comprehensive_agent.py` | 保留错误身份与恢复诊断 |
| `tests/test_tool_recovery.py` | 本轮确定性验收 |

本轮不承诺模型对所有错误都能解释正确，也不强杀已开始的同步线程或撤回已发出的外部请求。已有轮数、进展检查、权限和用量治理继续负责最终边界。
