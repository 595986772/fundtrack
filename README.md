# FundTrack

基金持仓管理与 Agent 分析平台。先把持仓和收益算清楚，再把行情、事件和基金季报放在一起分析，回答“赚亏来自哪里、风险集中在哪、哪些结论有依据”。支持 Web 和微信小程序。

## 能做什么

- **管理持仓**：按实际份额、成交净值和手续费记账，区分浮动盈亏与已实现盈亏。买卖申请、确认、撤销和更正保留记录，重复提交不会重复入账。
- **分析当前配置**：从持仓发起分析，查看收益、回撤、集中度和历史压力测试；报告生成后可继续追问，沿用当前组合和会话背景。
- **查看市场变化**：按板块查看指数、成交额、热点事件和公开资金线索，关联到已有持仓。数据注明来源与时间，手动刷新；交易日简报需要用户主动开启。
- **核对分析依据**：季报问答返回文档版本、原文和页码。经理观点走文本检索，比例与金额优先核对解析事实，不让模型直接编数字。
- **跟踪长任务**：组合分析在后台执行，通过 SSE 展示进度；保存节点状态，支持中断后的阶段恢复与报告回查。

这是个人资产记录与辅助分析工具，不连接券商或基金销售平台下单。行情、估值和季报都有各自的延迟，历史压力测试不代表未来收益。

## 关键实现

```text
Web / 微信小程序
      │ HTTP、SSE
      ▼
FastAPI ── 登录、用户隔离、任务授权、调用额度
      ├─ 持仓账本 → 交易流水 → 成本与收益核算
      ├─ 对话 Agent → 按需发现工具 → 执行与完成检查
      └─ 后台分析 → Celery Worker → LangGraph → 报告
                                   │
                      冻结证据 → 多角色分析 → 复核
                                   │
                季报 RAG / 行情 / 风险计算 / 市场事件

PostgreSQL：业务状态、账本、任务、用量、Checkpoint
Redis：消息队列、共享限流、并发控制
Chroma：文档向量索引    OpenFGA：任务级资源授权
```

**Agent 不是固定问答模板。** 对话入口由模型选择工具，执行器约束权限、时限和重复调用；需要更多能力时再发现工具。组合报告使用 LangGraph 编排独立分析与复核节点，不把多角色意见一致当成正确性证明。数值计算留在业务服务，模型负责解释并关联证据。

**RAG 区分“找原文”和“查数字”。** 中文 BM25 与向量检索融合候选，按配置重排，保留季度与来源边界。回答后检查引用、数值口径和证据覆盖；缺数据时说明缺口，不补造结论。向量化通过配置的 Embedding API 完成。

**耗时任务与 HTTP 请求解耦。** 数据库记录任务和投递意图，Outbox 补偿漏投，执行租约与版本校验约束接管。Checkpoint 恢复已完成阶段；它不保证外部模型调用恰好执行一次。调用前预留 Token，完成后结算，未知用量单独保留。

**账本可以重算。** 同一事务内写入流水并重建持仓，幂等键与唯一约束防止重复记账。更正保留历史关联；恢复备份后可逐项核对份额、成本与收益。

技术栈：Python 3.12、FastAPI、LangChain、LangGraph、SQLAlchemy、PostgreSQL、Redis、Celery、Chroma、OpenFGA、Docker。Web 使用原生 JavaScript 与 ECharts；小程序使用 TypeScript、WXML、WXSS。代码及部分文档沿用早期名称 FundScope。

## 本地启动

推荐 macOS / Linux、Python 3.12；OpenFGA 需要 Docker。Windows 可使用 WSL2。

```bash
git clone https://github.com/595986772/fundtrack.git
cd fundtrack
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

按需在 `.env` 填写 `DEEPSEEK_API_KEY`、`DEEPSEEK_MODEL` 和 `DASHSCOPE_API_KEY`。密钥不随仓库提供；模型名称以自己的供应商账号实际支持为准。未配置时仍可运行账本和样例行情，AI 分析及向量化不可用。

```bash
docker compose -f docker-compose.openfga.yml up -d --wait
python -m scripts.setup_openfga
fastapi dev main.py --host 127.0.0.1 --port 8000
```

打开 <http://127.0.0.1:8000/>，注册本地账号后创建组合、录入持仓。API 文档在 `/docs`。本地默认 SQLite 和进程内任务执行，用于单机体验；跨进程限流、队列和数据库并发应在 PostgreSQL + Redis + Celery 配置下验证。

仓库只带冻结的公开样例，日期见 `data/fund_fixtures/README.md`，不代表最新行情。没有预置账号密码，也不包含开发者的持仓或浏览器登录态。示例配置关闭自动行情同步；数据源刷新、模型调用和收费服务需要自行配置并承担费用。

### 小程序与手机预览

安装 Node.js 22 与 pnpm 后：

```bash
cd miniprogram
pnpm install --frozen-lockfile
pnpm build
```

回到运行中的服务，访问 `/mini` 查看浏览器适配预览。微信开发者工具导入 `miniprogram`，把 `project.config.json` 的游客 AppID 改为自己的 AppID。真机网络设置与两种预览的区别见 [小程序说明](miniprogram/README.md)。

### 正式运行

公网部署使用独立的 `docker-compose.prod.yml`，不要直接把开发配置暴露到公网。Nginx 提供 HTTPS，数据库与 Redis 不开放宿主机端口；迁移、授权初始化、健康检查和 SSE 代理配置见 [部署说明](docs/正式运行与健康检查.md)。备份与隔离恢复见 [恢复指南](docs/备份恢复与账务核对.md)。

## 检查与测试

不需要模型密钥的核心回归：

```bash
python -m scripts.check_release
pnpm --dir miniprogram install --frozen-lockfile
node --test tests/frontend/*.test.cjs
cd miniprogram
pnpm build
pnpm test
```

`check_release` 在临时数据库运行账本、Agent Loop、RAG 排序、任务恢复和授权等确定性测试，屏蔽外部网络调用。它不能替代真实模型质量评测或 PostgreSQL / Redis / OpenFGA 集成验收；后者需要隔离环境显式开启，入口见 [测试说明](docs/测试与验收.md)。

季报原文件不直接分发。需要运行固定季报评测时，先从原来源下载并验证文件哈希：

```bash
python -m scripts.prepare_report_fixtures
python -m unittest tests.test_fund_report_query tests.test_fund_report_evals
```

下载脚本不调用模型。在线分析、Embedding 和模型评测会产生供应商费用，不在默认 CI 中执行。

## 代码入口

| 目录 | 内容 |
| --- | --- |
| `app/routers/` | HTTP 接口、鉴权、SSE |
| `app/services/` | Agent 执行、检索、账务、报告与任务服务 |
| `app/models.py`、`alembic/` | SQL 模型与数据库迁移 |
| `app/static/` | Web 页面与图表 |
| `miniprogram/` | 小程序源码、构建与浏览器适配 |
| `tests/`、`scripts/` | 回归测试、评测、数据准备及运维 |
| `infra/`、`docker-compose*.yml` | 反向代理、授权模型与容器配置 |

进一步阅读：[Agent 循环](docs/Agent循环进展与完成检查.md) · [工具发现](docs/按需工具发现.md) · [季报 RAG](docs/基金季报RAG.md) · [多角色分析](docs/多角色投研.md) · [可靠执行](docs/组合投研可靠执行.md) · [任务授权](docs/Agent任务级授权.md)

## 使用边界

- 估值不等于最终净值；没有取得的数据不会按零处理。基金交易确认以外部平台回执为准。
- 社媒采集依赖平台许可与登录状态，不提供绕过风控的保证；云端默认关闭依赖本机浏览器的采集。
- 当前生产配置以单机、单 API 进程为范围，不宣称多机高可用或银行级合规。
- 第三方组件保留原许可证，数据来源与分发范围见 [THIRD_PARTY.md](THIRD_PARTY.md)。安全与隐私注意事项见 [SECURITY.md](SECURITY.md)。
