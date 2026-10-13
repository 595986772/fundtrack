# Agent 任务级授权

## 解决什么问题

你同时拥有组合 A 和 B，但这次只选择 A 让 Agent 分析。用户拥有 B，并不代表
这次 Agent 可以查看 B。模型即使被新闻中的指令诱导，或者主动生成 B 的工具参数，
后端也会拒绝。普通用户在页面查看自己的 B 不受这张任务授权单影响。

本轮接入的是真实 OpenFGA HTTP 服务，不是用 Python 字典模拟权限引擎。
业务数据库负责授权的身份、生命周期和审计，OpenFGA 负责资源与操作之间的关系。
两层都允许才执行。

## 数据怎么走

```text
登录身份 + 页面选中的组合 / 已验证的报告引用
  -> 后端创建任务授权（用户、run_id、agent_id、资源、能力、到期时间）
  -> SQL 写 pending -> OpenFGA 写关系 -> SQL 改 active
  -> ExecutionContext 绑定 grant_id / run_id / agent_id
  -> 模型产生 tool_call（这些参数仍不可信）
  -> 工具执行器检查身份、任务范围、OpenFGA 判定
  -> 业务 Service 再核对真实资源归属
  -> 执行并检查授权是否仍有效 -> 将结果送回模型
```

例如 OpenFGA 保存：`grant:grant_x` 对 `portfolio:portfolio_a` 具有 `reader` 关系。
查询时问的不是“用户 chen 能看什么”，而是“本次 grant_x 能否读取 portfolio_a”。
数据库中的授权还绑定 chen、这次运行和这个 Agent，知道 grant_id 本身不能获得权限。

### 两类组合操作

| 能力 | 用途 |
| --- | --- |
| `portfolio:read` | 查询组合、历史压力记录、已保存报告和决策资料 |
| `portfolio:research-run` | 创建组合研究任务、运行压力测试、生成约束草案 |

分析授权不包含真实资产写入、交易确认或执行调仓。自然语言约束草案仍须通过原有人机
确认流程，Agent 不能代替用户确认。公共基金、市场、知识检索和计算能力继续使用现有
工具能力表；本轮没有把所有文档、文件和 MCP 资源都建模进 OpenFGA。

## 已接入的边界

- 产品聊天：根据可信页面选择授权；没有选择组合时不默认授权全部组合。
- 工具执行：工具能被发现、Schema 已加载，不等于可以访问任意组合。
- 数据服务：压力记录、草案、快照、报告引用等先查实际关联组合，再检查范围。
- 历史与缓存：重新检查授权与请求范围，幂等重放不能复活已过期或撤销的授权。
- 子 Agent：只能派生父范围的子集，不能增加能力或延长到期时间；父授权失效影响后代。
- 后台任务：保存授权引用，Worker 恢复原授权；重试与 checkpoint 不重新发放权限。
- LangGraph 并行节点：显式传递执行上下文，线程切换不能清除任务边界。
- 模型调用：调用前、返回后和流式检查点检查授权生命周期。
- 审计：保存身份、操作、资源、允许/拒绝及原因，不复制完整 Prompt 或持仓正文。

默认有效期 30 分钟。对话下一次独立请求可以创建新的任务授权；同一任务的重试只恢复
原授权，不自动续期。授权仍有效不代表任务仍在运行，聊天结束后派生的后台工作可能还在执行。

## 启动

### 本机：现有 Conda + 独立 OpenFGA

在项目根目录，确保 Docker Desktop 正在运行：

```bash
conda activate dl
docker compose -f docker-compose.openfga.yml up -d --wait
python -m scripts.setup_openfga
fastapi dev main.py
```

OpenFGA 只监听本机 `127.0.0.1:8091`，使用独立持久化 SQLite 卷。初始化程序写入
`data/openfga.json`，其中只有 URL、store_id 和 model_id，不含模型 API Key。
重复初始化会核对已有模型，不在断网时偷偷创建另一个授权库。

关闭 OpenFGA 可以执行 `docker compose -f docker-compose.openfga.yml stop`。
不要给 `down` 加 `-v`，否则会删除授权数据库。服务不可用时组合访问返回失败，不降级放行。

### Docker 工程模式

这是本机独立 OpenFGA 的替代部署，不要让两套服务同时占用默认 8091 端口。
正式环境通过安全的环境变量或密钥管理注入 `OPENFGA_TOKEN` 和
`OPENFGA_POSTGRES_PASSWORD`，密码须适合连接 URI，不要写入版本库。

```bash
docker compose --profile openfga up -d --wait openfga
python -m scripts.setup_openfga --config data/openfga-docker.json
OPENFGA_CONFIG_PATH=/app/data/openfga-docker.json docker compose --profile openfga up --build -d
```

初始化脚本读取进程环境中的 Token；它不会读取或改写 `.env`。在主机运行初始化时
使用本机端口，API/Worker 在容器内使用 `http://openfga:8080`。不同 OpenFGA 数据库
的 store/model ID 不能混用，因此这里使用独立配置文件。

此部署在原有 API、Worker、Redis、业务 PostgreSQL 之外，添加 OpenFGA、一次性迁移
容器和独立的 OpenFGA PostgreSQL。数据库只暴露在内部网络，不使用业务数据库账号。
升级业务数据库仍使用 `alembic upgrade head`，Docker API 启动命令会先执行它。

### 配置项

| 配置 | 默认值/含义 |
| --- | --- |
| `AGENT_TASK_AUTHORIZATION_ENABLED` | `true`，为产品新任务建立授权 |
| `AGENT_TASK_GRANT_SECONDS` | `1800`，授权有效期 |
| `OPENFGA_URL` | 本机 `http://127.0.0.1:8091` |
| `OPENFGA_CONFIG_PATH` | `data/openfga.json`，保存初始化 ID |
| `OPENFGA_STORE_ID` / `OPENFGA_MODEL_ID` | 可用环境变量覆盖文件中的 ID |
| `OPENFGA_TOKEN` | 正式部署的服务间凭证，不是用户授权单 |
| `OPENFGA_TIMEOUT_SECONDS` | `3`，权限服务请求超时 |

关闭功能开关只用于隔离旧教学测试或明确回退，不能当作授权服务故障的自动兜底。
已有授权的生命周期校验不会因为关闭开关而被跳过。

## 如何测试

### 页面体验

1. 打开 Web 的 AI 投研，选择一个组合，发起分析。
2. 点击聊天头部“任务授权”，查看组合范围、允许操作和到期时间。
3. 展开“只读审计”，查看访问是否被允许及原因。
4. 点击撤销并确认。本次任务及其子任务后续校验应失败；已生成的内容仍保留。
5. 新建一次分析是重新授权，不等于旧任务复活。另一组合必须通过可信页面选择进入新任务。

页面分析会产生正常模型费用。下面的自动化测试使用临时业务数据库和模拟模型，不请求付费模型。

```bash
python -m unittest tests.test_agent_task_authorization tests.test_task_authorization_api \
  tests.test_task_authorization_resources tests.test_task_authorization_chat \
  tests.test_task_authorization_jobs tests.test_task_authorization_job_api tests.test_openfga_authorization -v
```

使用已启动的真实 OpenFGA 验证 HTTP 协议、读写关系及撤销效果：

```bash
OPENFGA_INTEGRATION=1 RUN_OPENFGA_INTEGRATION=1 python -m unittest \
  tests.test_openfga_authorization tests.test_task_authorization_openfga \
  tests.test_task_authorization_job_openfga -v
node --test tests/frontend/agent-authorizations.test.cjs
python scripts/check_agent_authorization_ui.py
```

主要反例包括：同属一个用户却越界读取 B、伪造其他用户、空范围当全量、子任务扩权、
过期后重试、撤销后缓存重放、模型执行期间撤销、权限服务不可用，以及恢复旧 checkpoint。

## 代码入口

| 文件 | 职责 |
| --- | --- |
| `app/services/agent_authorization.py` | 授权单、父子继承、范围检查、撤销和审计 |
| `app/services/openfga_authorization.py` | 有界超时的真实 OpenFGA REST 客户端 |
| `infra/openfga-model.json` | grant、portfolio 和 reader/researcher 关系模型 |
| `app/services/agent_execution_context.py` | 后端可信身份与 ContextVar 作用域 |
| `app/services/comprehensive_agent.py` | 产品聊天入口创建/继承授权、范围内历史 |
| `app/services/agent_chat_runtime.py` | 请求幂等和授权感知的结果复用 |
| `app/services/agent_tool_executor.py` | 模型工具调用前检查，返回后再检查生命周期 |
| `app/services/background_jobs.py` / `job_runtime.py` | 保存授权引用与 Worker 恢复 |
| `app/services/portfolio_research_graph.py` | 图节点、并行节点与恢复路径的上下文 |
| `app/routers/agent_authorizations.py` | 当前用户查询和撤销接口，不开放任意创建授权 |
| `app/static/agent-authorizations.js` | 查看授权、审计和确认撤销 |
| `alembic/versions/c6a2f840d911_add_agent_task_authorization.py` | 业务授权表与审计表迁移 |

## 能力边界

1. **本地兼容模式不是正式多用户认证。** `AUTH_REQUIRED=false` 仍接受本地自报用户 ID。
   上线必须开启严格登录、HTTPS 和服务间凭证；OpenFGA 不替代身份认证。
2. **撤销不等于时间倒流。** SQL 是撤销的权威记录，不依赖权限缓存或 OpenFGA 删除成功。
   尚未到达下一检查点的外部请求可能已经发出；已产生费用、已看到的数据不会被撤回。
3. **OpenFGA 没有保存整套业务逻辑。** 用户归属、授权期限和父链生命周期仍由 SQL 校验；
   资源关系使用 `HIGHER_CONSISTENCY` 检查。当前未添加历史关系的后台清理任务。
4. **旧任务不补造权限。** 开启功能后，缺少授权引用的旧组合研究/调整/监控任务不能直接
   恢复执行，需要重新发起。用户本人查看已有报告沿用原有归属检查。
5. **这不是代码沙箱或数据库行级安全。** 保护的是接入任务上下文的产品 Agent 执行链。
   管理员开发入口、插件自带凭证以及未来新增资源仍须分别设计权限。

官方参考：[OpenFGA Docker 部署](https://openfga.dev/docs/getting-started/setup-openfga/docker)、
[关系写入](https://openfga.dev/docs/getting-started/update-tuples)、
[读取一致性](https://openfga.dev/docs/interacting/consistency)。
