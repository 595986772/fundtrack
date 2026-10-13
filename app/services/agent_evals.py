"""Agent Evals：版本化业务场景、固定数据和确定性评分。

评测解决的是“改了 Prompt、Tool 或 Agent Loop 以后，原来的基金能力有没有退化”。
每个场景都会运行真实 ``ToolCallingAgentService``，但基金净值、市场状态和新闻事件
被固定在仓库 fixture 上。Python 再检查路由、工具、事实、证据和回答边界，因此重复
运行不会因为线上数据变化而失去可比性，也不会让另一个模型给自己打分。
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timezone
import json
from pathlib import Path
import re
from time import perf_counter
from typing import Any
from uuid import uuid4

from app.schemas import (
    AgentChatRequest,
    AgentEvalCaseResult,
    AgentEvalCheck,
    AgentEvalDashboard,
    AgentEvalRun,
    AgentEvalScenarioInfo,
    AgentEvalSuiteInfo,
    AgentTokenUsage,
)
from app.services.eval_context import (
    DEFAULT_EVAL_DATA_PROFILE,
    eval_case_set_id,
    fixture_fingerprint,
    use_frozen_eval_data,
)
from app.services.observability import (
    AgentObservabilityService,
    agent_observability_service,
)
from app.services.portfolios import PortfolioService, portfolio_service
from app.services.tool_agent import ToolCallingAgentService, tool_calling_agent_service


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SUITE_PATH = PROJECT_ROOT / "data" / "evals" / "fund_agent_v2.json"
DEFAULT_FIXTURE_PATHS = [
    PROJECT_ROOT / "data" / "fund_fixtures" / "fund_profiles.json",
    PROJECT_ROOT / "data" / "fund_fixtures" / "fund_nav.csv",
    PROJECT_ROOT / "data" / "market_fixtures" / "market_snapshot.json",
    PROJECT_ROOT / "data" / "market_fixtures" / "market_events.json",
]
METRIC_DIMENSIONS = (
    "routing",
    "execution",
    "grounding",
    "answer_quality",
    "safety",
    "efficiency",
    "retrieval", "numeric", "citations", "conversation",
)
ASSERTION_OPERATORS = {
    "equals",
    "not_equals",
    "contains",
    "one_of",
    "gte",
    "lte",
    "between",
    "exists",
}
ABSOLUTE_CLAIM_PATTERNS = (
    "一定赚钱",
    "保证收益",
    "稳赚不赔",
    "必然上涨",
    "肯定上涨",
    "必然下跌",
)
ABSOLUTE_NEGATION_PREFIXES = (
    "并非",
    "不是",
    "不等于",
    "不代表",
    "不能视为",
    "不能当作",
    "不可视为",
    "不能因为",
)
ABSOLUTE_NEGATION_SUFFIXES = ("并不现实", "不是事实", "无法保证", "不能保证")


class AgentEvalError(RuntimeError):
    """评测配置、fixture 或请求不合法。"""


class AgentEvalService:
    """组织评测集加载、隔离数据、Agent 调用、规则评分和结果持久化。"""

    def __init__(
        self,
        *,
        agent: ToolCallingAgentService = tool_calling_agent_service,
        observability: AgentObservabilityService = agent_observability_service,
        portfolios: PortfolioService = portfolio_service,
        suite_path: Path = DEFAULT_SUITE_PATH,
        fixture_paths: list[Path] | None = None,
    ) -> None:
        # 依赖可注入，单元测试可以使用 FakeAgent 和临时数据库，不消耗模型额度。
        self.agent = agent
        self.observability = observability
        self.portfolios = portfolios
        self.suite_path = Path(suite_path)
        self.fixture_paths = list(fixture_paths or DEFAULT_FIXTURE_PATHS)

    def suite_info(self) -> AgentEvalSuiteInfo:
        """读取前端需要的公开说明，并附带固定数据内容指纹。"""

        suite = self._load_suite()
        category_counts = Counter(
            str(item.get("category") or "general") for item in suite["scenarios"]
        )
        return AgentEvalSuiteInfo(
            id=suite["id"],
            name=suite["name"],
            description=suite["description"],
            version=str(suite.get("version") or "1"),
            data_profile=str(
                suite.get("data_profile") or DEFAULT_EVAL_DATA_PROFILE
            ),
            data_fingerprint=self._data_fingerprint(),
            category_counts=dict(category_counts),
            scenarios=[
                AgentEvalScenarioInfo(
                    id=item["id"],
                    name=item["name"],
                    description=item["description"],
                    fund_code=str(item.get("fund_code") or ""),
                    message=item["message"],
                    category=str(item.get("category") or "general"),
                    tags=[str(tag) for tag in item.get("tags") or []],
                    expected_tools=[
                        str(name) for name in item.get("required_tools") or []
                    ],
                )
                for item in suite["scenarios"]
            ],
        )

    def dashboard(self, *, user_id: str) -> AgentEvalDashboard:
        """一次返回套件、最近两次成绩和全部运行观测摘要。"""

        suite = self.suite_info()
        # v1 和 v2 的案例、数据与权重不同，不能把两个总分当成连续趋势。
        all_recent = self.observability.recent_eval_runs(
            user_id=user_id,
            limit=30,
            suite_id=suite.id,
        )
        recent = all_recent[:6]
        latest = all_recent[0] if all_recent else None
        # 调试一条、冒烟九条、完整二十条是三种不同实验，不能混算趋势。
        previous = next(
            (
                item
                for item in all_recent[1:]
                if latest is not None
                and item.case_set_id == latest.case_set_id
                and item.data_fingerprint == latest.data_fingerprint
            ),
            None,
        )
        if latest is not None and previous is not None:
            latest = latest.model_copy(
                update={"score_delta": round(latest.score - previous.score, 1)}
            )
            recent[0] = latest
        return AgentEvalDashboard(
            suite=suite,
            latest_run=latest,
            previous_run=previous,
            recent_eval_runs=recent,
            observability=self.observability.summary(user_id=user_id),
        )

    async def run_suite(
        self,
        *,
        user_id: str,
        case_ids: list[str] | None = None,
        progress_callback=None,
    ) -> AgentEvalRun:
        """顺序执行场景，保存每个 case 的真实轨迹和整套质量报告。

        场景顺序执行是有意为之：它避免一次并发很多付费模型请求，也让延迟和 Token
        指标更容易解释。评测用户和临时组合均与真实用户数据隔离。
        """

        suite = self._load_suite()
        scenarios = self._select_scenarios(suite["scenarios"], case_ids or [])
        selected_case_ids = [str(item["id"]) for item in scenarios]
        current_case_set_id = eval_case_set_id(selected_case_ids)
        eval_run_id = f"eval_{uuid4().hex[:16]}"
        eval_user_id = f"eval-{eval_run_id[-8:]}"
        data_profile = str(
            suite.get("data_profile") or DEFAULT_EVAL_DATA_PROFILE
        )
        data_fingerprint = self._data_fingerprint()
        started = perf_counter()
        results: list[AgentEvalCaseResult] = []
        created_portfolio_ids: list[str] = []

        # ContextVar 跨 await 保持当前任务状态，退出 with 后自动恢复产品数据模式。
        with use_frozen_eval_data(data_profile):
            try:
                portfolio_ids = self._prepare_portfolio_fixtures(
                    suite=suite,
                    scenarios=scenarios,
                    user_id=eval_user_id,
                    created_ids=created_portfolio_ids,
                )
                for raw_scenario in scenarios:
                    if progress_callback:
                        progress_callback(len(results), len(scenarios), raw_scenario["name"])
                    scenario = self._resolve_scenario(
                        raw_scenario,
                        portfolio_ids=portfolio_ids,
                    )
                    result = await self._run_case(
                        scenario=scenario,
                        requested_by=user_id,
                        eval_user_id=eval_user_id,
                        eval_run_id=eval_run_id,
                        data_profile=data_profile,
                        data_fingerprint=data_fingerprint,
                    )
                    results.append(result)
                if progress_callback:
                    progress_callback(len(results), len(scenarios), "正在保存评测结果")
            finally:
                # 组合只是评测输入。无论模型调用是否失败，都不把它留在产品数据库。
                self._cleanup_portfolio_fixtures(
                    user_id=eval_user_id,
                    portfolio_ids=created_portfolio_ids,
                )

        total_cases = len(results)
        passed_cases = sum(1 for item in results if item.passed)
        total_tokens = sum(item.usage.total_tokens for item in results)
        previous_runs = self.observability.recent_eval_runs(
            user_id=user_id,
            limit=30,
            suite_id=suite["id"],
        )
        comparable_previous = next(
            (
                item
                for item in previous_runs
                if item.case_set_id == current_case_set_id
                and item.data_fingerprint == data_fingerprint
            ),
            None,
        )
        score = (
            round(sum(item.score for item in results) / total_cases, 1)
            if total_cases
            else 0.0
        )
        eval_run = AgentEvalRun(
            id=eval_run_id,
            user_id=user_id,
            suite_id=suite["id"],
            suite_name=suite["name"],
            status="completed",
            total_cases=total_cases,
            passed_cases=passed_cases,
            failed_cases=total_cases - passed_cases,
            score=score,
            duration_ms=round((perf_counter() - started) * 1000, 2),
            total_tokens=total_tokens,
            total_tool_calls=sum(item.tool_call_count for item in results),
            average_duration_ms=(
                round(sum(item.duration_ms for item in results) / total_cases, 2)
                if total_cases
                else 0.0
            ),
            average_tokens=(
                round(total_tokens / total_cases, 1) if total_cases else 0.0
            ),
            data_profile=data_profile,
            data_fingerprint=data_fingerprint,
            case_ids=selected_case_ids,
            case_set_id=current_case_set_id,
            metric_scores=self._aggregate_metric_scores(results),
            score_delta=(
                round(score - comparable_previous.score, 1)
                if comparable_previous is not None
                else None
            ),
            results=results,
            created_at=datetime.now(timezone.utc).isoformat(),
        )
        return self.observability.save_eval_run(eval_run)

    async def _run_case(
        self,
        *,
        scenario: dict[str, Any],
        requested_by: str,
        eval_user_id: str,
        eval_run_id: str,
        data_profile: str,
        data_fingerprint: str,
    ) -> AgentEvalCaseResult:
        """运行单个场景；失败会变成结果，不会中断整套回归。"""

        case_started = perf_counter()
        try:
            run = await self._execute_case(scenario, eval_user_id)
            duration_ms = (perf_counter() - case_started) * 1000
            result = self.grade_case(
                scenario=scenario,
                run=run,
                duration_ms=duration_ms,
                data_profile=data_profile,
                data_fingerprint=data_fingerprint,
            )
            self.observability.record_run(
                run=run,
                user_id=requested_by,
                duration_ms=duration_ms,
                source="eval",
                eval_run_id=eval_run_id,
                eval_case_id=scenario["id"],
                metrics={
                    "eval_passed": result.passed,
                    "eval_score": result.score,
                    "eval_category": result.category,
                    "eval_metric_scores": result.metric_scores,
                    "data_fingerprint": data_fingerprint,
                },
            )
            return result
        except Exception as exc:
            duration_ms = (perf_counter() - case_started) * 1000
            return AgentEvalCaseResult(
                case_id=scenario["id"],
                name=scenario["name"],
                category=str(scenario.get("category") or "general"),
                data_profile=data_profile,
                data_fingerprint=data_fingerprint,
                passed=False,
                score=0.0,
                duration_ms=round(duration_ms, 2),
                stop_reason="execution_error",
                checks=[
                    AgentEvalCheck(
                        name="场景可执行",
                        passed=False,
                        detail="Agent 调用失败，未进入规则评分。",
                        dimension="execution",
                        weight=2.0,
                    )
                ],
                metric_scores={"execution": 0.0},
                error=self._safe_error(exc),
                execution_diagnostics=getattr(exc, "execution_diagnostics", {}),
                usage=AgentTokenUsage(**getattr(exc, "execution_diagnostics", {}).get("usage", {})),
            )

    async def _execute_case(self, scenario, eval_user_id):
        """普通场景仍调用同一个 Agent；专项套件只替换输入组织，不复制评分/存储流程。"""
        request = AgentChatRequest(message=scenario["message"], user_id=eval_user_id,
            fund_code=scenario.get("fund_code") or None, portfolio_id=scenario.get("portfolio_id") or None,
            max_tool_rounds=int(scenario.get("max_tool_rounds") or 4))
        return await self.agent.chat(request, history=[])

    def grade_case(
        self,
        *,
        scenario: dict[str, Any],
        run: Any,
        duration_ms: float,
        data_profile: str = "",
        data_fingerprint: str = "",
    ) -> AgentEvalCaseResult:
        """使用声明式规则给一次真实 Agent 输出做确定性判分。"""

        tools_called = [call.tool_name for call in run.tool_calls]
        tool_payloads = self._tool_payloads(run.tool_calls)
        available_evidence = self.observability.extract_evidence_ids(run.tool_calls)
        cited_evidence = self.observability.extract_cited_evidence_ids(run.answer)
        checks: list[AgentEvalCheck] = []

        expected_route = str(scenario.get("expected_route") or "")
        if expected_route:
            actual_route = str(getattr(getattr(run, "route", None), "name", ""))
            self._add_check(
                checks,
                name="请求路由正确",
                passed=actual_route == expected_route,
                detail=f"预期 {expected_route}，实际 {actual_route or '未记录'}。",
                dimension="routing",
                weight=2.0,
            )

        required_tools = [str(name) for name in scenario.get("required_tools") or []]
        missing_tools = [name for name in required_tools if name not in tools_called]
        self._add_check(
            checks,
            name="选择预期工具",
            passed=not missing_tools,
            detail=(
                "已调用全部预期工具。"
                if not missing_tools
                else f"缺少工具：{', '.join(missing_tools)}"
            ),
            dimension="routing",
            weight=2.0,
        )

        forbidden_tools = {str(name) for name in scenario.get("forbidden_tools") or []}
        unexpected = [name for name in tools_called if name in forbidden_tools]
        self._add_check(
            checks,
            name="未调用禁用工具",
            passed=not unexpected,
            detail=(
                "没有禁用工具调用。"
                if not unexpected
                else f"出现禁用工具：{', '.join(unexpected)}"
            ),
            dimension="routing",
        )

        max_tool_calls = int(scenario.get("max_tool_calls") or 0)
        if max_tool_calls:
            within_limit = len(tools_called) <= max_tool_calls
            self._add_check(
                checks,
                name="工具调用数量合理",
                passed=within_limit,
                detail=(
                    f"共调用 {len(tools_called)} 次，未超过上限 {max_tool_calls}。"
                    if within_limit
                    else f"共调用 {len(tools_called)} 次，超过上限 {max_tool_calls}。"
                ),
                dimension="efficiency",
            )

        self._grade_tool_errors(checks, scenario, run.tool_calls)

        # 异常验收必须声明停止原因；只有命中预期错误码还不够，不能把崩溃算作拒绝成功。
        expected_stops = scenario.get("expected_stop_reasons")
        if expected_stops:
            self._add_check(checks, name="停止原因符合场景", dimension="execution",
                passed=run.stop_reason in expected_stops,
                detail=f"预期 {expected_stops}，实际 {run.stop_reason}。")

        # JSON 中声明事实断言，评分器只读取工具原始结果，不依赖模型是否换了一种说法。
        for assertion in scenario.get("tool_result_assertions") or []:
            self._grade_tool_assertion(
                checks=checks,
                assertion=assertion,
                tool_payloads=tool_payloads,
            )

        answer = str(run.answer or "")
        for group in scenario.get("required_answer_groups") or []:
            terms = [str(term) for term in group.get("terms") or []]
            matched = [term for term in terms if self._answer_contains_term(answer, term)]
            match_mode = str(group.get("match") or "any")
            passed = len(matched) == len(terms) if match_mode == "all" else bool(matched)
            self._add_check(
                checks,
                name=str(group.get("name") or "包含关键内容"),
                passed=passed,
                detail=(
                    f"命中：{', '.join(matched)}"
                    if passed
                    else f"未满足关键词要求：{', '.join(terms)}"
                ),
                dimension="answer_quality",
                weight=float(group.get("weight") or 1.0),
            )

        forbidden_answer_terms = [
            str(term) for term in scenario.get("forbidden_answer_terms") or []
        ]
        if forbidden_answer_terms:
            found_terms = [term for term in forbidden_answer_terms if term in answer]
            self._add_check(
                checks,
                name="未出现禁止事实",
                passed=not found_terms,
                detail=(
                    "没有出现与固定数据冲突的表述。"
                    if not found_terms
                    else f"发现冲突表述：{', '.join(found_terms)}"
                ),
                dimension="grounding",
                weight=2.0,
            )

        if scenario.get("require_evidence_reference"):
            valid_citations = sorted(set(cited_evidence) & set(available_evidence))
            invented_citations = sorted(set(cited_evidence) - set(available_evidence))
            self._add_check(
                checks,
                name="引用真实工具证据",
                passed=bool(valid_citations) and not invented_citations,
                detail=(
                    f"有效引用：{', '.join(valid_citations[:5])}"
                    if valid_citations and not invented_citations
                    else (
                        f"工具未返回这些证据 ID：{', '.join(invented_citations[:5])}"
                        if invented_citations
                        else "回答没有引用工具返回的 evidence_id / event_id。"
                    )
                ),
                dimension="grounding",
                weight=2.0,
            )

        if scenario.get("reject_absolute_claims", True):
            absolute_claims = self._unsupported_absolute_claims(answer)
            self._add_check(
                checks,
                name="无绝对化收益承诺",
                passed=not absolute_claims,
                detail=(
                    "未发现无依据的绝对化承诺。"
                    if not absolute_claims
                    else f"发现表述：{', '.join(absolute_claims)}"
                ),
                dimension="safety",
            )

        metric_scores = self._metric_scores(checks)
        total_weight = sum(check.weight for check in checks)
        passed_weight = sum(check.weight for check in checks if check.passed)
        score = round(passed_weight / total_weight * 100, 1) if total_weight else 0.0
        usage = getattr(run, "usage", AgentTokenUsage())
        return AgentEvalCaseResult(
            case_id=scenario["id"],
            name=scenario["name"],
            category=str(scenario.get("category") or "general"),
            data_profile=data_profile,
            data_fingerprint=data_fingerprint,
            passed=all(check.passed for check in checks if check.required),
            score=score,
            run_id=run.run_id,
            duration_ms=round(duration_ms, 2),
            stop_reason=run.stop_reason,
            tools_called=tools_called,
            tool_call_count=len(tools_called),
            physical_tool_attempts=self._physical_tool_attempts(run.tool_calls),
            evidence_ids=cited_evidence,
            available_evidence_ids=available_evidence,
            usage=usage,
            checks=checks,
            metric_scores=metric_scores,
            answer_preview=answer[:2400],
        )

    def _grade_tool_errors(self, checks, scenario, tool_calls):
        """异常场景逐工具、逐错误码精确匹配；没有声明的旧场景继续要求零错误。

        不提供宽泛的“忽略工具错误”开关。例如预期净值不足不能放过权限失败，
        预期一次参数纠错也不能放过模型反复提交十次坏参数。
        """
        from collections import Counter

        failed = [call for call in tool_calls if self._tool_call_failed(call)]
        contract = scenario.get("expected_tool_errors")
        if contract is None:
            self._add_check(checks, name="工具执行成功", dimension="execution", weight=2,
                passed=not failed, detail=("所有工具均成功返回。" if not failed
                    else f"工具失败：{', '.join(call.tool_name for call in failed)}"))
            return
        expected = Counter()
        for item in contract:
            if (not item.get("tool") or not item.get("error_code")
                    or type(item.get("count")) is not int or item["count"] < 1):
                raise ValueError("expected_tool_errors 必须指定工具、错误码和正整数次数")
            expected[(item["tool"], item["error_code"])] += item["count"]
        actual = Counter()
        for call in failed:
            try:
                payload = json.loads(call.tool_result)
            except (ValueError, TypeError):
                payload = None
            code = payload.get("error_code") if isinstance(payload, dict) else None
            actual[(call.tool_name, str(code or "unclassified"))] += 1
        self._add_check(checks, name="工具错误符合场景契约", dimension="execution", weight=2,
            passed=actual == expected, detail=f"预期 {dict(expected)}；实际 {dict(actual)}。")

    @staticmethod
    def _physical_tool_attempts(tool_calls):
        """只统计执行器记录的 attempt，不把一次自动重试误计为一次模型决策。"""
        count = 0
        for call in tool_calls:
            trace = getattr(call, "recovery_trace", [])
            if not trace and getattr(call, "execution_kind", "") != "cached":
                return None
            count += sum(item.get("action") == "attempt" for item in trace)
        return count

    def _grade_tool_assertion(
        self,
        *,
        checks: list[AgentEvalCheck],
        assertion: dict[str, Any],
        tool_payloads: dict[str, list[dict[str, Any]]],
    ) -> None:
        """读取一个 Tool JSONPath 风格字段并执行简单、可审计的比较。"""

        tool_name = str(assertion.get("tool") or "")
        path = str(assertion.get("path") or "")
        operator = str(assertion.get("operator") or "equals")
        expected = assertion.get("expected")
        actual_values: list[Any] = []
        for payload in tool_payloads.get(tool_name, []):
            exists, value = self._value_at_path(payload, path)
            if exists:
                actual_values.append(value)

        passed = any(
            self._compare_assertion(
                actual=value,
                expected=expected,
                operator=operator,
            )
            for value in actual_values
        )
        if operator == "exists":
            passed = bool(actual_values) == bool(expected if expected is not None else True)

        actual_preview = ", ".join(self._short_value(item) for item in actual_values[:3])
        self._add_check(
            checks,
            name=str(assertion.get("name") or f"工具事实：{path}"),
            passed=passed,
            detail=(
                f"{tool_name}.{path} = {actual_preview or '字段不存在'}；"
                f"期望 {operator} {self._short_value(expected)}。"
            ),
            dimension="grounding",
            weight=float(assertion.get("weight") or 1.5),
        )

    def _prepare_portfolio_fixtures(
        self,
        *,
        suite: dict[str, Any],
        scenarios: list[dict[str, Any]],
        user_id: str,
        created_ids: list[str],
    ) -> dict[str, str]:
        """为选中的组合场景创建短生命周期持仓，并返回 fixture key -> ID。"""

        required_keys = {
            str(item["portfolio_fixture"])
            for item in scenarios
            if item.get("portfolio_fixture")
        }
        definitions = (suite.get("fixtures") or {}).get("portfolios") or {}
        resolved: dict[str, str] = {}
        for key in sorted(required_keys):
            definition = definitions.get(key)
            if not isinstance(definition, dict):
                raise AgentEvalError(f"缺少组合 fixture：{key}")
            try:
                portfolio = self.portfolios.create(
                    user_id=user_id,
                    name=str(definition["name"]),
                    holdings=list(definition["holdings"]),
                )
            except Exception as exc:
                raise AgentEvalError(f"创建组合 fixture {key} 失败：{exc}") from exc
            resolved[key] = str(portfolio["id"])
            created_ids.append(str(portfolio["id"]))
        return resolved

    def _cleanup_portfolio_fixtures(
        self,
        *,
        user_id: str,
        portfolio_ids: list[str],
    ) -> None:
        """尽力删除临时组合；清理失败不覆盖已经生成的评测结果。"""

        for portfolio_id in portfolio_ids:
            try:
                self.portfolios.delete(
                    user_id=user_id,
                    portfolio_id=portfolio_id,
                )
            except Exception:
                # 唯一用户 ID 带有 eval- 前缀，残留时也不会被产品用户读取。
                continue

    @staticmethod
    def _resolve_scenario(
        scenario: dict[str, Any],
        *,
        portfolio_ids: dict[str, str],
    ) -> dict[str, Any]:
        """把动态组合 ID 注入请求上下文，静态 JSON 仍保持可提交和可审阅。"""

        resolved = dict(scenario)
        fixture_key = str(scenario.get("portfolio_fixture") or "")
        if fixture_key:
            portfolio_id = portfolio_ids[fixture_key]
            resolved["portfolio_id"] = portfolio_id
            resolved["message"] = str(resolved["message"]).replace(
                "{{portfolio_id}}",
                portfolio_id,
            )
        return resolved

    def _load_suite(self) -> dict[str, Any]:
        """加载并验证版本化 JSON，尽早暴露重复 ID 或断言拼写错误。"""

        if not self.suite_path.exists():
            raise AgentEvalError(f"找不到评测集：{self.suite_path}")
        try:
            payload = json.loads(self.suite_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise AgentEvalError(f"评测集读取失败：{exc}") from exc

        required = {"id", "name", "description", "scenarios"}
        if not required.issubset(payload) or not isinstance(payload["scenarios"], list):
            raise AgentEvalError("评测集缺少 id/name/description/scenarios。")
        scenario_ids = [str(item.get("id") or "") for item in payload["scenarios"]]
        if any(not item for item in scenario_ids) or len(set(scenario_ids)) != len(scenario_ids):
            raise AgentEvalError("评测场景 ID 不能为空或重复。")
        for scenario in payload["scenarios"]:
            missing = {
                key for key in ("name", "description", "message") if not scenario.get(key)
            }
            if missing:
                raise AgentEvalError(
                    f"场景 {scenario['id']} 缺少字段：{', '.join(sorted(missing))}"
                )
            for assertion in scenario.get("tool_result_assertions") or []:
                operator = str(assertion.get("operator") or "equals")
                if operator not in ASSERTION_OPERATORS:
                    raise AgentEvalError(
                        f"场景 {scenario['id']} 使用未知断言：{operator}"
                    )
        # 公共边界集中写在 defaults，单个场景只覆盖真正不同的部分，JSON 更易审阅。
        defaults = payload.get("defaults") or {}
        if not isinstance(defaults, dict):
            raise AgentEvalError("评测集 defaults 必须是对象。")
        payload["scenarios"] = [
            {**defaults, **scenario} for scenario in payload["scenarios"]
        ]
        return payload

    def _select_scenarios(
        self,
        scenarios: list[dict[str, Any]],
        case_ids: list[str],
    ) -> list[dict[str, Any]]:
        if not case_ids:
            return scenarios
        requested = set(case_ids)
        known = {item["id"] for item in scenarios}
        unknown = sorted(requested - known)
        if unknown:
            raise AgentEvalError(f"未知评测场景：{', '.join(unknown)}")
        return [item for item in scenarios if item["id"] in requested]

    def _data_fingerprint(self) -> str:
        try:
            return fixture_fingerprint(self.fixture_paths)
        except OSError as exc:
            raise AgentEvalError(str(exc)) from exc

    @staticmethod
    def _tool_payloads(tool_calls: list[Any]) -> dict[str, list[dict[str, Any]]]:
        """按工具名解析 JSON；无法解析的文本仍由执行成功检查处理。"""

        payloads: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for call in tool_calls:
            try:
                payload = json.loads(str(call.tool_result or ""))
            except (json.JSONDecodeError, TypeError):
                continue
            if isinstance(payload, dict):
                payloads[str(call.tool_name)].append(payload)
        return dict(payloads)

    @staticmethod
    def _tool_call_failed(call: Any) -> bool:
        """逐条判断执行结果，不能让同名工具的一次失败连累另一次成功调用。"""

        if bool(call.is_error):
            return True
        try:
            payload = json.loads(str(call.tool_result or ""))
        except (json.JSONDecodeError, TypeError):
            return False
        # 有些 Tool 捕获业务异常后返回 {ok: false}，LangChain 本身不会抛异常。
        return isinstance(payload, dict) and payload.get("ok") is False

    @staticmethod
    def _value_at_path(payload: Any, path: str) -> tuple[bool, Any]:
        """读取 ``a.b.0.c`` 形式的受限路径，不引入可执行 JSONPath 表达式。"""

        current = payload
        if not path:
            return True, current
        for part in path.split("."):
            if isinstance(current, dict) and part in current:
                current = current[part]
                continue
            if isinstance(current, list) and part.isdigit():
                index = int(part)
                if 0 <= index < len(current):
                    current = current[index]
                    continue
            return False, None
        return True, current

    @staticmethod
    def _compare_assertion(*, actual: Any, expected: Any, operator: str) -> bool:
        try:
            if operator == "equals":
                return actual == expected
            if operator == "not_equals":
                return actual != expected
            if operator == "contains":
                return expected in actual
            if operator == "one_of":
                return actual in expected
            if operator == "gte":
                return float(actual) >= float(expected)
            if operator == "lte":
                return float(actual) <= float(expected)
            if operator == "between":
                low, high = expected
                return float(low) <= float(actual) <= float(high)
            if operator == "exists":
                return True
        except (TypeError, ValueError, ZeroDivisionError):
            return False
        return False

    @staticmethod
    def _answer_contains_term(answer: str, term: str) -> bool:
        """匹配回答关键词，并把数字中的中英文千分位视为同一种写法。"""

        if term in answer:
            return True
        normalized_answer = re.sub(r"(?<=\d)[,，](?=\d)", "", answer)
        normalized_term = re.sub(r"(?<=\d)[,，](?=\d)", "", term)
        return normalized_term in normalized_answer

    @staticmethod
    def _unsupported_absolute_claims(answer: str) -> list[str]:
        """识别真正的绝对承诺，避免把“并非稳赚不赔”这样的警示句判错。"""

        found: list[str] = []
        for claim in ABSOLUTE_CLAIM_PATTERNS:
            for match in re.finditer(re.escape(claim), answer):
                prefix = answer[max(0, match.start() - 32) : match.start()]
                suffix = answer[match.end() : match.end() + 12]
                negated_before = any(
                    marker in prefix for marker in ABSOLUTE_NEGATION_PREFIXES
                )
                negated_after = any(
                    marker in suffix for marker in ABSOLUTE_NEGATION_SUFFIXES
                )
                if not negated_before and not negated_after:
                    found.append(claim)
                    break
        return found

    @staticmethod
    def _add_check(
        checks: list[AgentEvalCheck],
        *,
        name: str,
        passed: bool,
        detail: str,
        dimension: str,
        weight: float = 1.0,
        required: bool = True,
    ) -> None:
        checks.append(
            AgentEvalCheck(
                name=name,
                passed=passed,
                detail=detail,
                dimension=dimension,
                weight=weight,
                required=required,
            )
        )

    @staticmethod
    def _metric_scores(checks: list[AgentEvalCheck]) -> dict[str, float]:
        grouped: dict[str, list[AgentEvalCheck]] = defaultdict(list)
        for check in checks:
            grouped[check.dimension].append(check)
        scores: dict[str, float] = {}
        for dimension, items in grouped.items():
            total_weight = sum(item.weight for item in items)
            passed_weight = sum(item.weight for item in items if item.passed)
            scores[dimension] = (
                round(passed_weight / total_weight * 100, 1)
                if total_weight
                else 0.0
            )
        return scores

    @staticmethod
    def _aggregate_metric_scores(
        results: list[AgentEvalCaseResult],
    ) -> dict[str, float]:
        scores: dict[str, float] = {}
        for dimension in METRIC_DIMENSIONS:
            values = [
                item.metric_scores[dimension]
                for item in results
                if dimension in item.metric_scores
            ]
            if values:
                scores[dimension] = round(sum(values) / len(values), 1)
        return scores

    @staticmethod
    def _short_value(value: Any) -> str:
        text = json.dumps(value, ensure_ascii=False) if not isinstance(value, str) else value
        return text if len(text) <= 100 else f"{text[:97]}..."

    @staticmethod
    def _safe_error(error: Exception) -> str:
        # 不把模型 SDK 的完整异常写入前端，避免请求头中的密钥被意外带出。
        message = str(error).replace("\n", " ").strip()
        message = re.sub(r"\bsk-[A-Za-z0-9._-]{8,}\b", "[REDACTED_API_KEY]", message)
        return (message or error.__class__.__name__)[:500]


agent_eval_service = AgentEvalService()
