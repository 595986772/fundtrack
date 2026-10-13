# Agent 循环产品入口验收

这轮不再增加一层 Agent 或新中间件，而是把之前三轮能力放到一起验收：
**进展与完成检查、共享时限、工具错误分类与有限恢复。**

## 通俗理解

之前分别检查每个模块，这次从用户实际调用的聊天接口发起请求，检查一路执行到答案
返回、记录保存、资源释放是否一致。来源超时、数据不足和取消不是同一种结果，不能
统统写成“失败”，也不能只要有文字就算“成功”。

例如比较两只基金时，第二个来源第一次超时：

1. 模型发出两次工具调用。
2. 第一只正常返回，第二只只重试一次后返回。
3. 来源实际执行三次，而不是把两只重新查一遍。
4. SSE 只交付一个最终答案，会话只保存一组问答。
5. 相同幂等键重放不再调用来源、模型，也不重复写入消息。

## 跑的是哪条链路

```text
内存 HTTP 客户端
  -> /api/v1/conversations 创建会话
  -> /api/v1/agent/chat/stream
  -> 请求限流、可信身份注入
  -> AgentChatCoordinator：共享时限、会话锁、幂等记录
  -> ComprehensiveAgentService：历史、业务上下文
  -> ToolCallingAgentService：规则路由、Loop、完成检查
  -> 工具执行器：校验、缓存、重试、错误分类
  -> ModelRecovery + GovernedChatOpenAI
  -> SQL Token 预留/结算、内存执行槽位
  -> SSE 终态、会话保存、资源清理
```

模型响应通过 `httpx.MockTransport` 模拟供应商协议，**没有绕开治理器或模型恢复器**。
工具来源使用固定的合成风险数据和故障注入。真正的停止场景调用现有 DELETE 停止接口，
并不是直接取消一个假的 Agent 方法。

## 怎么运行

在项目根目录、已经安装依赖的 conda 环境中运行：

```bash
conda activate dl
python scripts/eval_agent_loop_acceptance.py
```

不需要启动网页、Redis、Worker 或真实模型服务。脚本在导入应用之前把数据库地址切到
临时目录，只影响当前脚本进程，**不修改 `.env`、持仓或线上数据库**。没有后台调度器。

默认输出：

- `.cache/agent-loop-acceptance/report.md`：可读表格和失败检查项。
- `.cache/agent-loop-acceptance/report.json`：完整检查、SSE 事件类型、工具恢复轨迹、用量状态。

只排查某一组问题：

```bash
python scripts/eval_agent_loop_acceptance.py --case recover_source --case deadline --case cancel
```

保留前后两份报告时分别传入不同目录：

```bash
python scripts/eval_agent_loop_acceptance.py --output .cache/loop-before
python scripts/eval_agent_loop_acceptance.py --output .cache/loop-after
```

先对照失败检查、来源实际执行次数和终态；只有数据指纹、场景集合相同才适合比较。
脚本全部通过返回退出码 `0`，任意场景失败返回 `1`，可以作为发布前回归步骤。

## 13 个固定场景

| 场景 | 检查重点 |
| --- | --- |
| 双基金证据完整 | 同区间证据、正确引用、持久化、同键重放 |
| 单来源暂时超时 | 只重试失败来源，保留另一只结果 |
| 重复读取 | 3 个工具意图只执行 2 次来源读取 |
| 无进展循环 | 重复请求提前停止，不耗尽 10 轮 |
| 净值不足 | 拦住“已全部核验”的脚本草稿，明确缺项 |
| 缺经理原文 | 没有季度证据不能完成策略比较 |
| 重试用尽 | 换 tool_call_id 不能重新获得次数 |
| 参数纠正成功 | 坏参数不访问来源，纠正后访问一次 |
| 参数反复错误 | 二次错误后禁止继续试探；连续无进展收尾并追问 |
| 权限拒绝 | 产品入口不能读开发者文件，不发送模型请求 |
| 额度不足 | SQL 预留拒绝后不向供应商发送请求 |
| 共享时限到期 | 保存已返回资料和缺口，超时阶段入库，可幂等重放 |
| 用户停止 | 通过停止接口取消，不保存半轮问答，回收锁和槽位 |

正常结果记为 `completed`；正确拒绝、追问、缺口或超时收尾记为 `handled_limitation`；
任一必要检查未通过记为 `system_error`，此处表示“验收失败”，不专指服务器 500。
**正确处理限制不等于业务分析已经完成。**

取消或超时时，已经发送但没有返回 usage 的模型请求仍必须为 `unknown`，不能当免费调用。
报告中的已知 Token 不包含这部分未确认用量，JSON 另有 `ledger_statuses` 和
`uncertain_tokens`。这里所有用量都是合成计数，仅用于验证记账流程。

## 和旧评测如何复用

- `AgentEvalService.grade_case` 继续检查工具选择、答案、引用和事实断言。
- 新场景可以声明精确的 `expected_tool_errors`：工具名、错误码、次数必须完全一致。
- **没有声明的旧评测仍要求工具全部成功**，没有加入“忽略错误”的全局开关。
- `AgentEvalCaseResult` 增加可选的 `outcome` 和 `physical_tool_attempts`。
  旧记录默认未知；工具没有恢复轨迹时也不会假装执行了零次。
- 结果复用 `AgentEvalRun`、`save_eval_run` 和原有读回逻辑，保存在临时数据库的现有 JSON
  字段中，再导出报告，无需数据库迁移。不会把合成评测混入用户日常质量统计。
- 测试还会故意去掉 SSE `done`、绕过缓存，确认验收器确实能发现退化。

## 哪些还不能证明

这是一套**离线产品入口集成验收**，不是浏览器端或云端全链路验收。

- 固定模型脚本证明控制逻辑，不证明真实模型的推理、选工具和语义理解准确率。
- 不验证完整 Prompt、语义路由、长期记忆、真实行情或季报解析；这些由各自专项覆盖。
- 测试身份由依赖注入提供；验证产品/开发者工具边界，但不验证登录系统或 OpenFGA 服务。
- 临时 SQLite 和内存槽位不等于 PostgreSQL/Redis 多进程验证，也不覆盖 Celery Worker 接管。
- 内存 ASGI 不经过 Nginx、TLS、网络断线或前端渲染，不能当公网 SSE 交付测试。
- 合成 Token 和本机耗时不能用作简历中的降本比例、真实 P95 或模型质量提升数据。

验收通过后，应在现有真实模型评测中选少量代表题目检查实际效果；云端部署再单独验证
代理下的 SSE 和跨进程治理，不需要为了这套验收增加新技术栈。

## 首轮真实模型验收（2026-09-30）

经用户授权，只跑 `risk_comparison`，使用真实 `deepseek-v4-pro` 和固定基金数据。
该脚本仍使用隔离 SQLite、内存执行槽位和注入的测试身份，不访问用户实际持仓。

```bash
python scripts/eval_agent_loop_live.py --allow-paid-api --case risk_comparison \
  --max-model-calls 3 --token-limit 40000 --max-output-tokens 2048
```

这是付费命令，不应作为普通离线回归自动运行。追加 `--prepare-only` 只验证隔离数据，
不会发送模型请求。上限包含辅助模型调用和重试；禁用备用供应商，不自动扩大预算或重跑。
Token 额度使用保守预留，并非人民币费用保证；实际费用以供应商账单为准。

- 原始结果：`.cache/live-loop-acceptance/20260930-201751/risk_comparison.json`。
- 接口耗时约 9.63 秒，HTTP 200；3 次模型请求均已结算，共 5898 Token，无待确认用量。
- 主 Loop 用量为 5249 Token，另有投资背景提取调用 649 Token，账本统计包含两者。
- 自动检查 8/9 通过，**此场景未完全通过**；未执行其余四个真实模型场景。
- 两次风险工具调用成功；SSE 唯一终态、正文和会话保存检查通过。
- 人工对照原始工具结果：两基金统计区间一致，回答中的波动率 17.30% / 3.25%、
  最大回撤 9.26% / 2.28% 与工具结果相符。这只是固定数据验收，不是当前行情结论。

唯一失败项是“引用真实工具证据”。`get_fund_risk_tool` 的结果有来源、日期和数值，
但没有 `evidence_id`；前端元数据通过 `extract_tool_evidence_records` 在回答后生成
`tool:` 来源记录，而验收器要求最终答案引用工具原先返回的证据 ID，两者尚未对齐。
所以不能把这项失败解释为 DeepSeek 连接失败，也不能把来源卡片直接算作逐条引用通过。
后续应明确并统一风险工具的引用协议，再做回归；本次不修改业务代码或降低评分条件。

## 修复后联合验收（2026-09-30）

本节是后续实测结果，不覆盖上面的失败记录。结论：**本轮 RAG / Loop 验收范围内，
七个代表性真实模型场景已通过自动检查；不是整个系统、所有问题或云端部署均已验收。**

### 本轮修复

1. `app/services/fund_agent_tools.py` 的风险工具补充波动率、最大回撤证据，编号包含基金代码
   和实际统计起止日，数值直接取同次风险计算。最终答案及证据面板使用同一份工具证据，
   不再依靠回答结束后才生成的来源占位编号。
2. 实际持仓评估曾错误返回“模拟目标权重”，导致模型照着工具解释错。
   `portfolio_assessment.py`、`portfolio_agent_tools.py` 与 API Schema 现在明确
   `position_mode` / `weight_basis`：实际份额市值与目标配置分别处理。
   业绩工具另加 `valuation_note`，说明按现金流和分红再投资总回报推进的模型价值，
   不等同于当前份额乘净值的账本市值；本轮不改动收益核算算法。
3. 真实验收脚本支持隔离 Chroma 索引与 `--retrieval-mode hybrid`，建库、查询 Embedding
   也进入相同的调用上限和 SQL 额度账本。`--prepare-only` 始终不发付费请求。
   季报使用原有专项评分器核对原文、页码、基金/版本、金标数字和真实检索模式，
   不要求用户可读的 PDF 链接变成机器证据 ID，也不把只有链接当作回答完成。

### 实际结果

| 验收层 | 结果 | 范围 |
| --- | --- | --- |
| Python 回归 | 322/322 | RAG、引用修正、跨轮记录、Loop、超时、重试、取消、额度、组合工具 |
| 前端交互回归 | 26/26 | 正式答案替换预览、错误与取消、季报切换、旧请求隔离、评测页面 |
| 离线产品入口 | 13/13 | 实际 ASGI 路由、SSE、幂等、故障注入、资源释放 |
| 固定 PDF 检索 | 40/40 标注证据进入完整结果 | 20 题、23 次检索，含三道追问；不是模型回答准确率 |
| 真实模型代表题 | 七个场景均取得通过记录 | 下表；包含一次预算阻断后的单题补验 |
| 本机就绪检查 | ready | database / ai_control / authorization 均为 ok |

| 真实模型场景 | 最终结果与补充核对 |
| --- | --- |
| 双基金风险比较 | 同区间数值、来源日期、四条风险引用一致 |
| 债券基金两季久期策略 | 两期实际 hybrid；区分 Q1 利率下行与 Q2 经济数据下行，不混淆因果条件 |
| 实际持仓收益和风险 | 修复后复验通过；工具明确份额市值权重，回答标明期末模型价值和模型盈亏 |
| 多轮投资背景与切换基金 | 三年期限和 15% 容忍度保留，研究对象切到 110007 |
| 基金不存在 | 明确 999999 数据缺失，停止于 insufficient_evidence，不编造比较 |
| 商业航天经理主题 | 两期实际 hybrid；覆盖 IPO、发射、火箭和终端，原文展示含手机直连等具体场景 |
| 资产表横杠/缺失 | 两期实际 hybrid；0.69% 对 null，变化仍为 null，不补零、不认定清仓 |

所有成功轮次均检查了 SSE 唯一 result/done、流正文与入库正文一致、会话问答完整。
主题题通过了一次有限修正，原始诊断保留 `corrected`，不伪装成首次生成就通过。
本轮另逐条对照了上述真实回答、工具数字与引文。专项报告的人审字段仍为 `pending`，
未冒充项目所有者签字；语义蕴含、主题是否完全覆盖和数据源真实性不由自动分认证。

### 失败记录与用量

- `.cache/live-loop-acceptance/20260930-203624/`：首次五题自动检查通过；随后阅读原始回答
  发现组合权重的旧口径问题，因此不能只凭这批的自动绿灯作为最终验收。
- `.cache/live-loop-acceptance/20260930-204224/`：组合修复复验、商业航天主题通过。
  最后一题触发本地 `token_budget_exceeded`。当时已用 51479 Token，剩余 48521，
  下一请求保守预留需 53389，故没有向供应商发送该次生成请求。这不是欠费或超时。
- `.cache/live-loop-acceptance/20260930-204516/`：只补验被预算阻断的缺失值题，通过。
  使用新的隔离验收账号，最多 8 次准入、100000 Token、单次输出 3072；没有更改产品额度。
- 三批实际请求合计 45 次：DeepSeek 27 次、Qwen Embedding 18 次，共 154354 Token。
  其中 DeepSeek 104713、Embedding 49641；SQL 账本均已结算，无残留预留或待确认用量。
  这些数字包含建索引、辅助调用、修正和失败批次的已完成请求，不是某一次用户提问成本。
- 免费检索结果：`.cache/rag-loop-acceptance-20260930/retrieval.json`。
- 免费入口报告：`.cache/rag-loop-acceptance-20260930/loop/report.md` 与 `report.json`。

### 复验入口

免费入口验收仍使用上面的 `eval_agent_loop_acceptance.py`。
真实模型命令会收费，先准备再执行：

```bash
python scripts/eval_agent_loop_live.py --allow-paid-api --retrieval-mode hybrid --prepare-only
python scripts/eval_agent_loop_live.py --allow-paid-api --retrieval-mode hybrid \
  --max-model-calls 32 --token-limit 120000 --max-output-tokens 3072
```

默认跑五个主场景。单独选择已有单轮季报金标题可以用 `--report-case report-active-missing`；
不会默认追加其他题目。带追问的季报专项题必须使用原有 `FundReportEvalService.run_suite`，
不能只跑第一轮然后对整题评分。每批写入独立目录，已有数据库的目录拒绝覆盖。

### 明确未覆盖

真实主模型、Qwen Embedding、Chroma 向量写入查询与 BM25 融合已经实测。
**当前未配置精排 URL，因此真实 rerank 不在通过项中**；精排协议、降级和用量逻辑只有
隔离 HTTP 回归。网页交互测试使用 DOM/网络替身，不等于手机真机或真实浏览器端到端验证。
本轮也不验证微信网络、公网代理、Celery/PostgreSQL/Redis 多进程、OpenFGA 远程服务、
实时行情抓取或所有季报题的真实模型效果，不能把本次成绩扩写成这些能力全部正常。
