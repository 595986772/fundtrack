# AI 调用与费用控制

这轮解决的是：多人同时分析基金时，一个用户不能占满模型资源；调用前检查额度，
调用后有账可查。不是新增一种 Agent，也不是重新实现模型网关。

## 工作流程

```text
网页 / 小程序 -> 登录身份 -> 聊天入口频率限制
    -> 幂等查询（已有结果直接返回，不再次调用模型）
    -> Agent / 投研工作流
    -> 模型请求频率检查 -> Redis 执行槽位或有限等待
    -> SQL 同时预留“今日额度”和“本任务额度”
    -> 标记请求已发出 -> 模型响应
    -> usage 结算 / 未知用量待确认 -> 释放执行槽位

后台任务：数据库队列准入 -> Celery / local Worker -> 恢复 Job 的用户与任务 ID
    -> 同一套模型治理 -> 任务结果及用量分别持久化
```

额度以服务端登录身份为准，不接受大模型或前端指定的“扣费用户”。本地原有
`AUTH_REQUIRED=false` 仍是兼容模式，不等同于安全的多用户部署；对外使用必须设为
`true`。所有副本要使用相同的数据库、Redis、前缀和限制配置。

## 当前默认限制

| 配置 | 默认值 | 含义 |
| --- | ---: | --- |
| `CHAT_RATE_LIMIT_PER_MINUTE` | 20 | 每用户每分钟聊天入口请求数 |
| `AI_MODEL_REQUESTS_PER_MINUTE` | 30 | 每用户每分钟模型入口尝试数，包含排队、拒绝与重试 |
| `AI_GLOBAL_CONCURRENCY` | 4 | 所有用户同时执行的模型请求上限 |
| `AI_USER_CONCURRENCY` | 2 | 单个用户同时执行的模型请求上限 |
| `AI_GLOBAL_QUEUE_SIZE` | 20 | 等待模型槽位的请求上限 |
| `AI_USER_QUEUE_SIZE` | 4 | 每用户等待模型槽位的请求上限 |
| `AI_QUEUE_WAIT_SECONDS` | 15 | 最多等待槽位的秒数，也占原工作流时间预算 |
| `AI_SLOT_LEASE_SECONDS` | 120 | 槽位租约，活跃调用会续租，崩溃后过期回收 |
| `AI_DAILY_TOKEN_LIMIT` | 2000000 | 每用户每天的 Token 额度，北京时间零点换日 |
| `AI_RUN_TOKEN_LIMIT` | 500000 | 一个聊天请求或后台 Job 的累计 Token 额度 |
| `AI_MAX_OUTPUT_TOKENS` | 16384 | 单次模型请求输出上限，可能影响长推理/长回答 |
| `AI_GLOBAL_PENDING_JOBS` | 32 | 所有 queued/running 后台 Job 上限 |
| `AI_USER_PENDING_JOBS` | 6 | 单用户 queued/running 后台 Job 上限 |

模型并发限制不等同于“整个 Agent 只能有四个”。多个工作流可以同时计算 Python
指标，但进入昂贵模型请求时必须拿到槽位。等待队列是有界轮询，不承诺严格 FIFO。
后台任务准入包含数据同步等所有 Job，防止 local 模式无限创建线程；历史终态和
`waiting_input` 不占准入容量，人工确认后重新排队需要再次检查容量。

## 额度如何记账

- `reserved`：预留成功，但还没有发出请求；此时取消可以退回预留。
- `in_flight`：准备发给供应商；崩溃或超时不能确定是否产生费用。
- `settled`：收到完整 usage，按供应商计数记账，释放多预留的部分。
- `unknown`：请求可能已执行但没有完整 usage，保留原预留为待确认额度。
- `released`：确定尚未发出的预留已经退回。

预留数 = 请求 JSON 的 UTF-8 字节数 + 协议余量 + 输出 Token 上限。这是**偏保守的
额度预估，不是精确 tokenizer，也不是供应商账单**。包含工具描述和结构化 schema；
实际用量高于预估时也如实入账，后续调用会被余额检查挡住，不伪造“不超支”的数字。

当前范围是通过 `get_chat_model()` 创建的文本模型调用，包括路由、工具后续回答、
结构化研判、子 Agent 与后台研究。Embedding、图片模型、第三方数据服务费用不在
此账本中。没有按人民币收费，也不把 input/output/cache 的不同单价当成同一种价格。

SDK 内部重试关闭，原允许的重试次数由治理层逐次执行，每次都生成独立调用明细。
备用供应商也使用同一个用户和任务额度。结构化解析在原始生成之后，解析失败仍会
记录已经返回的 usage。额度不足/队列满属于资源拒绝，不触发供应商重试或故障切换。

聊天幂等重放不再请求模型。失败后用同一幂等键重试、同一 Job 恢复，都继续用原
任务额度；这不能保证供应商 exactly-once 计费，已经发出的超时调用可能实际已计费。

## 配置与运行

本地不启 Redis 时，默认 `auto + local` 使用单进程 memory 控制，账本仍写 SQLite。
本轮已为当前 Mac 配置专属 Redis：

```bash
docker compose up -d redis
```

`.env` 使用：

```env
AI_GOVERNANCE_ENABLED=true
AI_CONTROL_BACKEND=redis
AI_REDIS_URL=redis://127.0.0.1:6380/2
```

修改 `.env` 后重启 FastAPI。Redis 不可用时模型入口返回 503，不偷偷回退到内存。
不使用模型的持仓浏览和普通数据读取仍可用。所有模型额度统一关闭可设置
`AI_GOVERNANCE_ENABLED=false`，这只用于开发排查，不建议作为线上兜底。

Docker Compose 的 API/Worker 已共享 `redis://redis:6379/2` 和 PostgreSQL。
`docker compose up` 的 API 启动命令会自动执行 Alembic。直接管理生产数据库时执行：

```bash
alembic upgrade head
```

本地 SQLite 的自动建表只新增两张表，不改已有持仓、报告或测评记录。

## 查看与测试

登录后查看 `/api/v1/ai/usage`，本地兼容模式可以传 `?user_id=chen`。
响应区分 `used_tokens`、`reserved_tokens`、`uncertain_tokens`、`remaining_tokens`，
并展示近 30 次调用和当前共享槽位数量。该接口不调用模型。

```bash
curl 'http://127.0.0.1:8000/api/v1/ai/usage?user_id=chen'
conda run -n dl python -m unittest tests.test_ai_governance tests.test_ai_governance_integration -v
TEST_AI_REDIS_URL=redis://127.0.0.1:6380/15 conda run -n dl python -m unittest tests.test_ai_governance.RedisCapacityTests -v
```

严格登录时 curl 需要自己的 Bearer Token 或 Cookie。不要把令牌、`.env` 或真实持仓
贴到公开日志。测试使用 MockTransport 返回模拟模型响应，不消耗 DeepSeek 余额。
Redis 测试使用随机前缀，只清理自己的键；其中有两个独立 Python 进程共享额度的测试。
`tests/test_ai_governance_postgres.py` 接受 `TEST_AI_POSTGRES_URL`，创建临时 schema
验证并发预留、后台准入、抢单与补结算，结束后只删除自己的 schema。

手动体验可临时调低 `AI_RUN_TOKEN_LIMIT`，重启后提问，应该收到“本次任务额度不足”，
账本没有实际供应商消耗。恢复原配置后再使用。不要用大量真实付费请求来压测限流。

管理员核对供应商账单后，可以通过
`POST /api/v1/ai/usage/calls/{call_id}/reconcile` 补录待确认请求的实际计数与备注。
该入口需要真实管理员登录，不能修改已结算结果，也不能清除仍在执行的预留。
不存在已确认账单时，保持待确认状态，不自动按零处理。

## 关键代码

| 文件 | 职责 |
| --- | --- |
| `app/services/ai_governance_context.py` | 可信用户、任务归属、明确的资源拒绝错误 |
| `app/services/ai_capacity.py` | Redis Lua 原子限流、执行槽位、等待队列与租约 |
| `app/services/ai_usage.py` | SQL 短事务、预留/结算/过期分类/管理员对账 |
| `app/services/governed_model.py` | 保留 ChatOpenAI 接口，约束每次实际生成 |
| `app/services/job_store.py` | 与任务 INSERT/重新排队同事务的容量检查 |
| `app/routers/ai_usage.py` | 只读用量与管理员对账 API |

Redis 脚本设计参考 [官方 Lua 原子执行说明](https://redis.io/docs/latest/develop/programmability/eval-intro/)。
业务账本以 SQL 为准，Redis 只保存短期控制状态，不在两种数据库之间做金额双写。

本轮未新建监控平台、CI 或费用订阅系统，也没有修改基金采集时机。新增治理上限会
影响运行条件，因此季报测评记录已包含治理配置；旧成绩不会被改写，新旧不同额度
不能直接宣称为“同预算提速”。极端网络分区时，租约不能强制终止已发到供应商的计算，
也不能追回该供应商产生的费用。

## 本轮验证记录

2026-09-16 分三组运行，均通过；各组存在重复覆盖，不把执行次数当成独立用例数：

- 61 项治理及对照测试：真实 Redis 跨进程限流、PostgreSQL 并发预留/抢单、
  身份隔离、取消、超时待确认、流式 usage、长请求续期和额度配置对照。
- 165 项原有业务回归：聊天、后台 Job、认证、错误恢复、组合研究、季报问答与测评。
- 86 项入口回归：补查组合研判/调整、市场事件和社媒分析中的资源拒绝传播，
  确认不会把“额度不足”改写为语义失败或触发内容修正重试。

临时 PostgreSQL 已验证迁移到 `53e8b19c7a20` 后清理。本机业务仍使用原来的 SQLite，
项目 Redis 保留运行；健康接口、小程序入口和用量查询均可用，用量接口确认使用 Redis。
模型测试全部使用模拟响应，没有调用真实 DeepSeek；这些测试不等于真实回答质量评测。
