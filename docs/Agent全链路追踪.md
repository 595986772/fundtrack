# Agent 全链路追踪

## 它解决什么问题

用户说“分析很慢”，现在可以用一次执行的 `trace_id` 查出：任务等了多久、读取哪路数据最慢、哪个 Agent 调用了几次模型、是否在生成后反复修正、最后是否保存成功。

Trace 是整次执行的编号，Span 是其中一个计时步骤。这里接入的是 **OpenTelemetry SDK + OTLP HTTP 导出**，可以直接发送到 Langfuse，不需要把现有 Agent 重写成另一套框架。业务 Job、SQL 账本和 LangGraph Checkpoint 仍负责自己的状态，追踪只是观测。

## 覆盖的路径

```text
HTTP 请求（返回 X-Trace-ID，SSE 则覆盖整个响应流）
└─ job.create（保存 Job 和父追踪上下文）
   ├─ job.dispatch（Outbox 向执行器投递）
   └─ job.execute（Worker 每次领取对应一个新步骤）
      ├─ job.authorize
      ├─ research.restore_checkpoint
      ├─ research.workflow
      │  ├─ research.snapshot
      │  │  └─ research.retrieve.*（风险、业绩、市场等五路实际读取）
      │  ├─ research.load_portfolio / calculate_* 等图节点
      │  ├─ research.interpret_with_llm 或 committee_* 多角色节点
      │  │  ├─ ai.call
      │  │  │  ├─ ai.admission（限流、排队和额度预留）
      │  │  │  ├─ ai.generate（供应商调用）
      │  │  │  └─ ai.settle（SQL 结算和执行槽位释放）
      │  │  └─ research.validate（引用、数值等校验）
      │  └─ research.compose_report
      └─ job.save_result
```

普通聊天也覆盖 `agent.chat → agent.tool → agent.authorize_tool / rag.retrieve / ai.call → 回答校验`。具体出现哪些步骤取决于实际执行路径，不会为了看起来完整而生成没执行的步骤。

图节点消费的是冻结快照，真正的数据读取发生在图执行之前，因此把快照采集也单独计时。并行步骤的耗时有重叠，**不能把所有 Span 耗时相加当作用户等待时间**。

## 如何跨进程关联

1. API 创建 Job 时，将服务端生成的 W3C `traceparent` 与少量受控元数据写入内部 `payload._trace_context`，随 Job 和 Outbox 一起提交。
2. Outbox 发送者和 Worker 都从 SQL 恢复这个父上下文，不依赖 HTTP 进程里的变量，也不需要把用户问题塞进消息头。
3. 同一任务的每次执行、重试或接管生成新的 `job.execute` Span，但仍属于原来的 Trace。`job.attempt`、`job.execution_version` 区分不同执行。
4. LangGraph 通过运行时 Context 显式把父上下文传给各节点，快照线程使用 Context 副本。追踪字段不加入冻结金融数据或其内容哈希。

投递与执行是 `job.create` 下的兄弟步骤，不是假装 Worker 一定由某次“已确认成功的投递”直接触发。重复消息可能出现，业务依旧由 SQL 领取和租约校验防止重复执行。幂等重复提交会返回原 Job 的 `trace_id`，不会覆盖它；这次 HTTP 响应头仍是当前请求的编号。

## 本地怎么用

更新依赖后，**重启 API 和 Worker**。默认 `TRACE_ENABLED=true`、`TRACE_LOCAL_ENABLED=true`，记录写入 `data/traces/`，没有配置远程入口就不会上传。

照常在页面创建一次组合分析。任务状态接口的 `job.trace_id`、报告的 `graph_trace.trace_id` 可用于定位；普通请求也可在浏览器网络面板查看 `X-Trace-ID`。旧任务没有追踪编号，不会伪造过去的执行记录。

在项目目录、现有 Conda 环境下运行：

```bash
# 列出最近 20 条已落盘链路
python -m scripts.inspect_agent_trace

# 查看指定链路的步骤树，把编号替换成实际 trace_id
python -m scripts.inspect_agent_trace --trace-id 你的追踪编号
```

导出是异步的，任务完成后通常需要等待约一秒。`UNSET` 是 OpenTelemetry 的“未设置错误状态”，不代表模型结论正确。失败步骤显示错误类型；模型没有返回完整 Token 计数时显示 `ai.usage_known=False`，不会记成零费用。

`job.initial_queue_ms` 仅在首次执行记录，表示建单到领取后的等待时间，包含 Outbox 投递和执行资源等待，并非精确的 Redis 排队时长。后续执行记录 `job.age_ms`，不把任务的全部存活时间冒充本轮排队时间。

## Langfuse 可视化

项目已经提供导出接口，但**没有替你创建 Langfuse 项目或部署 Langfuse 服务**。可以使用已有的自托管实例或云端项目；将它提供的项目凭据配置在私有环境文件，不要提交 Git：

```dotenv
TRACE_ENABLED=true
TRACE_SAMPLE_RATE=1.0
TRACE_RELEASE=你部署的代码版本
LANGFUSE_BASE_URL=https://你的-langfuse-地址
LANGFUSE_PUBLIC_KEY=项目提供的公钥
LANGFUSE_SECRET_KEY=项目提供的密钥
```

API 与 Worker 必须使用相同配置。正式 Compose 已透传这些变量，`TRACE_RELEASE` 使用镜像标签。SDK 向 `/api/public/otel/v1/traces` 发送 HTTP/protobuf，使用项目公私钥组成的 Basic 认证，并设置 Langfuse v4 ingestion 标记。也可只设置完整的 `TRACE_OTLP_ENDPOINT`，发送到自己的 OTLP Collector；该设置优先于 Langfuse，两个出口不会重复投递。

在 Langfuse 按 Trace ID 或 `job_id` 查询，可以看到模型名、输入输出 Token、角色节点、重试和耗时。综合分析与多角色分析携带各自的 Prompt 版本，版本变更后可进行对照。没有上传输入输出正文，所以界面中的提示词/回答内容为空是有意的。

生产使用远程存储后，可将 `TRACE_LOCAL_ENABLED=false`。追踪平台账户只向运维人员开放，不给普通产品用户开放查询全库权限。

## 隐私、采样与故障边界

- 只记录路由模板、受控工具名、模型名、业务任务编号、版本、耗时、用量和错误类型；不记录问题、模型回答、新闻、持仓、Token 凭据、工具参数、异常正文或堆栈。
- 公网请求的 `traceparent` 和 `baggage` 不被信任，不能用它们跨用户串接执行或控制采样。追踪编号不是访问凭证，不影响现有授权检查。
- 每个进程使用有界异步导出队列。远程超时、文件写入失败不会改变业务返回、触发模型重试或阻止 SQL 结算。队列满或进程被强杀可能丢失观测，因此它不替代审计和费用账本。
- 本地单个进程最多轮转三份约 5 MiB 的文件，不同容器/启动批次文件名不冲突。**所有历史启动文件并没有全局容量上限**，正式运行应关闭本地副本或另设日志保留/清理策略。
- `TRACE_SAMPLE_RATE` 是新根链路的采样比例，子步骤继承原选择。降低采样后有些 Trace ID 没有记录；它不是“错误必保留”的尾部采样，也不能据此得到全量调用或费用总数。
- 这轮没有引入指标告警、备份恢复或完整 SQL 自动探针，也没有改动业务重试策略。

## 验证

```bash
python -m unittest tests.test_tracing -v
```

该测试使用临时 SQL 数据库、第二个真实 Python Worker 进程和确定性数据，验证跨进程父子关联、Outbox、幂等提交、并行图节点、SSE 生命周期、模型重试、用量缺失、脱敏、父级采样与导出失败。模型 HTTP 与远程 OTLP 接收端使用测试替身，不消耗 API 余额，不冒充已经跑通真实远程服务。

完整服务器验收还需要：重建并启动新镜像，执行一条实际业务任务，确认 API 与 Celery Worker 的步骤出现在同一条 Langfuse Trace 中，再模拟关闭追踪接收端，确认业务仍能完成。故障注入请在测试环境做。

## 源码入口

- `app/services/tracing.py`：Provider、脱敏 Span、采样、导出和 ASGI 中间件。
- `app/services/background_jobs.py`、`job_delivery.py`、`job_runtime.py`：任务上下文持久化、投递和执行。
- `app/services/portfolio_research_graph.py`：快照采集、并行节点、恢复与报告关联。
- `app/services/governed_model.py`：调用前等待、真实生成、Token 用量和结算观察。
- `app/services/agent_tool_executor.py`、`fund_reports.py`：工具边界、授权和季报检索。

参考：[OpenTelemetry Python](https://opentelemetry.io/docs/languages/python/instrumentation/)、[Langfuse 原生 OpenTelemetry 接入](https://langfuse.com/integrations/native/opentelemetry)。
