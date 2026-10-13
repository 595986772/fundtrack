"""真实模型小规模验收。独立进程、隔离数据库；必须显式允许付费调用。

不替换模型、正式 Prompt、业务工具和规则/语义路由。仅隔离用户身份、数据和工作区，
保留原始回答与失败记录，不自动重跑直到成功。使用现有固定净值和季报 PDF。
"""

from __future__ import annotations

import argparse
import asyncio
from contextlib import ExitStack
from datetime import date, datetime, timezone
import json
import os
from pathlib import Path
import sys
from threading import Lock
from time import perf_counter
from unittest.mock import patch
from uuid import uuid4


ROOT = Path(__file__).resolve().parents[1]
CASE_IDS = ("risk_comparison", "quarterly_strategy", "portfolio", "conversation", "missing_data")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--allow-paid-api", action="store_true", help="明确允许调用 .env 配置的真实模型")
    parser.add_argument("--case", action="append", choices=CASE_IDS, dest="case_ids")
    parser.add_argument("--report-case", action="append", default=[],
        help="指定已有单轮季报金标题 ID；可以与 --case 组合选择，不默认追加其他题目")
    parser.add_argument("--prepare-only", action="store_true", help="只检查隔离数据，不发送模型请求")
    parser.add_argument("--retrieval-mode", choices=("keyword", "hybrid"), default="keyword",
        help="hybrid 会为固定季报建立隔离 Chroma 索引，并实际调用 Embedding/精排")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--max-model-calls", type=int, default=24, help="整批模型请求准入次数上限，包含重试")
    parser.add_argument("--token-limit", type=int, default=200000, help="隔离账号的整批 Token 额度")
    parser.add_argument("--max-output-tokens", type=int, default=8192, help="单次模型输出 Token 上限")
    args = parser.parse_args()
    if not args.allow_paid_api:
        parser.error("本脚本会产生 API 用量，请显式传入 --allow-paid-api")
    if min(args.max_model_calls, args.token_limit, args.max_output_tokens) <= 0:
        parser.error("请求次数和 Token 上限必须大于零")
    output = (args.output or ROOT / ".cache/live-loop-acceptance" /
        datetime.now().strftime("%Y%m%d-%H%M%S")).resolve()
    output.mkdir(parents=True, exist_ok=True)
    if (output / "runtime.db").exists():
        parser.error("输出目录已有验收数据库，请换一个目录以保留原始结果")
    sys.path.insert(0, str(ROOT))
    # 全局业务服务在导入时绑定数据库，必须先改子进程环境，不能导入后才试图隔离。
    # 不读取或输出凭据，也不修改项目 .env。所有模拟持仓、用量、会话都保留在本次目录。
    os.environ.update(DATABASE_URL=f"sqlite:///{output / 'runtime.db'}",
        DATABASE_AUTO_CREATE_SCHEMA="true", AI_CONTROL_BACKEND="memory", JOB_EXECUTION_MODE="local",
        TRACE_ENABLED="false", AGENT_TASK_AUTHORIZATION_ENABLED="false",
        LANGGRAPH_CHECKPOINT_DATABASE_URL=f"sqlite:///{output / 'checkpoint.db'}",
        AI_GOVERNANCE_ENABLED="true", AI_DAILY_TOKEN_LIMIT=str(args.token_limit),
        AI_RUN_TOKEN_LIMIT=str(min(args.token_limit, 100000)),
        AI_MAX_OUTPUT_TOKENS=str(args.max_output_tokens),
        # 用户授权的是 DeepSeek 验收；失败时不能自动向其他付费供应商发送数据。
        LLM_FALLBACK_MODEL="", LLM_FALLBACK_API_KEY="")
    os.environ.setdefault("MPLCONFIGDIR", str(ROOT / ".cache/matplotlib"))
    return asyncio.run(run(args, output))


async def run(args, output):
    import httpx
    from fastapi import FastAPI
    from app.config import settings
    from app.database import runtime_database
    from app.routers import workspace
    from app.schemas import ComprehensiveAgentChatResponse
    from app.services.agent_execution_context import (AgentExecutionContext, FUND_READ, PORTFOLIO_READ,
        MARKET_READ, CALCULATOR_USE, TOOL_DISCOVERY)
    from app.services.agent_evals import AgentEvalService, DEFAULT_FIXTURE_PATHS
    from app.services.ai_governance_context import AIControlError, ai_execution_scope
    from app.services.ai_usage import get_ai_usage_service
    from app.services.auth import CurrentPrincipal, get_current_principal
    from app.services.eval_context import use_frozen_eval_data, fixture_fingerprint
    from app.services.fund_report_evals import FundReportEvalService, EvaluationWorkspace
    from app.services.fund_report_eval_grading import grade_report_case, tool_payloads
    from app.services.fund_reports import fund_report_reader_scope
    from app.services.governed_model import ModelPermit
    from app.services.llm import chat_model_provider_service
    from app.services.portfolios import portfolio_service

    if not settings.deepseek_api_key:
        raise RuntimeError("未配置主模型凭据；未发送 API 请求")
    providers = chat_model_provider_service.describe()
    print(json.dumps({"models": providers, "output": str(output), "max_model_admissions": args.max_model_calls,
        "daily_token_limit": settings.ai_daily_token_limit,
        "max_output_tokens": settings.ai_max_output_tokens}, ensure_ascii=False), flush=True)
    user_id = f"live-loop-{uuid4().hex[:12]}"
    principal = CurrentPrincipal(user_id=user_id, display_name="真实模型验收", role="user", authenticated=True)
    app = FastAPI()
    app.include_router(workspace.router)
    app.dependency_overrides[get_current_principal] = lambda: principal
    # 这轮只验证读取与解释，不允许模型创建后台任务、调仓草案或联网刷新。
    access = AgentExecutionContext(user_id=user_id, role="user", mode="product", authenticated=True,
        capabilities=frozenset({FUND_READ, PORTFOLIO_READ, MARKET_READ, CALCULATOR_USE, TOOL_DISCOVERY}),
        source="live-acceptance")
    admissions, gate = [0], Lock()
    original_rate = ModelPermit.rate

    def bounded_admission(permit):
        original_rate(permit)
        with gate:
            if admissions[0] >= args.max_model_calls:
                raise AIControlError("acceptance_call_limit", "本批验收模型调用次数已到上限")
            admissions[0] += 1
            # 这里只显示计数和预留量，不输出 Prompt 或凭据；准入不代表一定已发送。
            print(f"ADMISSION {admissions[0]}/{args.max_model_calls} "
                f"reserved_token_estimate={permit.tokens}", flush=True)

    # 只输出安全字段；供应商异常或工具结果意外包含配置值时，也先去除再写报告。
    secrets = [value for name in dir(settings) if any(word in name for word in ("api_key", "secret", "password"))
        and isinstance((value := getattr(settings, name)), str) and len(value) > 8]

    def save(name, value):
        raw = json.dumps(value, ensure_ascii=False, indent=2, default=str)
        for secret in secrets:
            raw = raw.replace(secret, "[REDACTED]")
        (output / name).write_text(raw, encoding="utf-8")

    suite = FundReportEvalService()
    docs = suite.validate_fixture()
    # 先免费核对 PDF。付费建索引必须放在下方调用计数和用户额度的作用域内，
    # prepare-only 即使指定 hybrid 也绝不发送 Embedding 请求。
    reader = suite._prepare_reader(runtime_database, docs, lambda _: None, "keyword")
    reader.chroma_directory = output / "chroma"
    if args.retrieval_mode == "keyword":
        reader.reranker = None
    chosen_docs = [doc["id"] for doc in docs if doc["fund_code"] == "110007"]
    report_case = suite._resolve_scenario(next(case for case in suite._load_suite()["scenarios"]
        if case["id"] == "report-bond-duration"), portfolio_ids={})
    report_cases = {case["id"]: suite._resolve_scenario(case, portfolio_ids={})
        for case in suite._load_suite()["scenarios"]}
    if any(key not in report_cases for key in args.report_case):
        raise ValueError("未知季报金标题 ID；未发送模型请求")
    if any(report_cases[key].get("follow_up") for key in args.report_case):
        raise ValueError("季报跨轮题请使用专项 run_suite；本入口不省略后续轮次来评分")
    grader = AgentEvalService()
    results = []
    with ExitStack() as stack:
        stack.enter_context(use_frozen_eval_data())
        stack.enter_context(fund_report_reader_scope(reader))
        stack.enter_context(patch.object(workspace, "_execution_context_for_route", return_value=access))
        stack.enter_context(patch.object(workspace.agent_chat_coordinator.agent, "runtime_state", EvaluationWorkspace()))
        stack.enter_context(patch.object(ModelPermit, "rate", new=bounded_admission))
        # 与生产相同的正式 Prompt、工具和路由不替换；只对总等待时间设测试上界。
        stack.enter_context(patch.object(settings, "agent_chat_timeout_seconds", min(settings.agent_chat_timeout_seconds, 120)))
        portfolio = portfolio_service.create(user_id=user_id, name="隔离验收组合", holdings=[
            {"fund_code": "110020", "units": 1000, "cost_amount": 1500, "purchase_date": date(2025, 8, 25)},
            {"fund_code": "110007", "units": 1000, "cost_amount": 1800, "purchase_date": date(2025, 8, 25)},
        ])
        cases = [
            {"id": "risk_comparison", "name": "两只基金风险比较", "message":
                "根据已保存数据，比较110020和110007近12个月的波动与最大回撤。使用相同区间，给出数据日期和证据，300字以内。不要刷新数据。",
                "required_tools": ["get_fund_risk_tool"], "require_evidence_reference": True,
                "expected_stop_reasons": ["model_finished"],
                "required_answer_groups": [{"terms": ["110020", "110007", "回撤"], "match": "all"}]},
            {"id": "quarterly_strategy", "name": "两季经理观点比较", "fund_code": "110007", "fund_report_ids": chosen_docs,
                "report_evaluation_id": report_case["id"],
                "message": report_case["message"] + "各引用一处原文和页码，600字以内。",
                # 季报产品渲染的是原文页码链接，不能拿机器 ID 检查误判；下面用专用
                # grader 同时检查固定金标召回、版本、结构化结论和原文链接，不降低标准。
                "required_tools": ["get_fund_report_research_tool"], "require_evidence_reference": False,
                "expected_stop_reasons": ["model_finished"],
                "required_answer_groups": [{"terms": ["一季度", "Q1", "2026-03-31"]},
                    {"terms": ["二季度", "Q2", "2026-06-30"]}]},
            {"id": "portfolio", "name": "实际持仓收益与风险", "portfolio_id": portfolio["id"], "message":
                "根据已保存数据解释当前组合的收益与风险：实际盈亏和历史模拟回测要区分，说明主要风险来自哪只基金，给出数据日期和证据，500字以内。不要创建后台研究任务，也不刷新来源。",
                "required_tools": ["get_portfolio_performance_tool", "get_portfolio_assessment_tool"],
                "tool_result_assertions": [{"tool": "get_portfolio_assessment_tool", "path": "weight_basis",
                    "expected": "current_position_market_value", "name": "实际持仓权重口径正确"},
                    {"tool": "get_portfolio_performance_tool", "path": "valuation_note", "operator": "exists"}],
                "require_evidence_reference": True, "expected_stop_reasons": ["model_finished"]},
            {"id": "conversation", "name": "投资背景跨轮沿用并切换基金", "fund_code": "110007",
                "setup_message": "这笔钱三年不用，可以接受的阶段性最大亏损是15%。先记住这些条件，不用分析。",
                "message": "现在切换到110007。根据刚才的期限和可接受亏损，读取它的历史风险并解释，历史回撤是否超出我的容忍范围？不要刷新数据，300字以内。",
                "required_tools": ["get_fund_risk_tool"], "expected_stop_reasons": ["model_finished"],
                "require_evidence_reference": True,
                "required_answer_groups": [{"terms": ["三年", "3年"]}, {"terms": ["15%", "15％"]}, {"terms": ["110007"]}]},
            {"id": "missing_data", "name": "缺少基金数据不编造比较", "message":
                "比较110020和999999近12个月的风险，缺数据请说明，不要刷新或尝试联网补全，300字以内。",
                "required_tools": ["get_fund_risk_tool"], "expected_stop_reasons": ["insufficient_evidence"],
                "expected_tool_errors": [{"tool": "get_fund_risk_tool", "error_code": "resource_not_found", "count": 1}],
                "required_answer_groups": [{"terms": ["999999"]}, {"terms": ["缺", "不足", "无法", "没有", "未取得"]}]},
        ]
        if args.case_ids or args.report_case:
            cases = [case for case in cases if case["id"] in (args.case_ids or [])]
        for key in dict.fromkeys(args.report_case):
            report = report_cases[key]
            cases.append({**report, "report_evaluation_id": key, "fund_report_ids": report["document_ids"],
                "require_evidence_reference": False, "expected_stop_reasons": ["model_finished"]})
        fingerprint = fixture_fingerprint([*DEFAULT_FIXTURE_PATHS, suite.data_dir / "documents.json",
            *[suite.data_dir / doc["pdf_path"] for doc in docs]])
        save("manifest.json", {"user_id": user_id, "models": providers, "data_fingerprint": fingerprint,
            "cases": cases, "portfolio": portfolio, "limits": {"model_admissions": args.max_model_calls,
                "daily_tokens": settings.ai_daily_token_limit, "run_tokens": settings.ai_run_token_limit,
                "output_tokens": settings.ai_max_output_tokens, "tool_rounds": 4,
                "chat_seconds": settings.agent_chat_timeout_seconds, "provider_fallback": False},
            "retrieval": args.retrieval_mode,
            "excluded": ["浏览器/反向代理", "OpenFGA", "Redis/PostgreSQL多进程", "真实行情源"]
                + (["Embedding与精排"] if args.retrieval_mode == "keyword" else
                    (["精排接口未配置"] if not reader.reranker.available else [])),
            "started_at": datetime.now(timezone.utc).isoformat()})

        if args.prepare_only:
            print("READY: 固定数据与隔离数据库检查完成，模型调用为 0。", flush=True)
            return 0

        if args.retrieval_mode == "hybrid" and any(case.get("report_evaluation_id") for case in cases):
            # 只索引本轮用到的固定 PDF，不访问真实用户文档；索引费用也计入同一额度。
            selected_ids = {key for case in cases for key in case.get("fund_report_ids", [])}
            with ai_execution_scope(user_id, "live-acceptance-index", "acceptance"):
                try:
                    for doc in docs:
                        if doc["id"] in selected_ids:
                            reader.index_document(doc, lambda message: print(message, flush=True))
                except Exception as exc:
                    save("preparation_error.json", {"error": f"{type(exc).__name__}: {exc}"})
                    save("usage.json", get_ai_usage_service().summary(user_id=user_id))
                    print("FAIL: 混合检索准备失败，未退回关键词模式冒充成功。", flush=True)
                    return 1

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://live-acceptance") as client:
            async def chat(case, conversation_id, *, setup=False):
                request = {"user_id": user_id, "conversation_id": conversation_id,
                    "idempotency_key": f"{case['id']}:{'setup' if setup else 'answer'}",
                    "message": case["setup_message"] if setup else case["message"], "max_tool_rounds": 4,
                    "research_refresh": False, **({"fund_code": "110020"} if setup else
                        {key: case[key] for key in ("fund_code", "fund_report_ids", "portfolio_id") if key in case})}
                started = perf_counter()
                response = await asyncio.wait_for(client.post("/api/v1/agent/chat/stream", json=request),
                    timeout=settings.agent_chat_timeout_seconds + 45)
                elapsed = (perf_counter() - started) * 1000
                frames = []
                for block in response.text.split("\n\n"):
                    fields = dict(line.split(": ", 1) for line in block.splitlines() if ": " in line)
                    if "data" in fields:
                        frames.append({"event": fields.get("event"), "data": json.loads(fields["data"])})
                result_data = next((frame["data"] for frame in frames if frame["event"] == "result"), None)
                return {"request": request, "http_status": response.status_code, "duration_ms": elapsed,
                    "frames": frames, "response": result_data}

            for case in cases:
                print(f"START {case['id']}", flush=True)
                created = await client.post("/api/v1/conversations", json={"user_id": user_id})
                created.raise_for_status()
                conversation_id = created.json()["id"]
                record = {"id": case["id"], "name": case["name"], "turns": [], "checks": [], "passed": False}
                try:
                    if case.get("setup_message"):
                        record["turns"].append(await chat(case, conversation_id, setup=True))
                    record["turns"].append(await chat(case, conversation_id))
                    response_data = record["turns"][-1]["response"]
                    if response_data:
                        response = ComprehensiveAgentChatResponse.model_validate(response_data)
                        grade = grader.grade_case(scenario=case, run=response.run,
                            duration_ms=sum(turn["duration_ms"] for turn in record["turns"]),
                            data_profile="live-model-frozen-data", data_fingerprint=fingerprint)
                        record["grade"] = grade.model_dump(mode="json")
                        record["checks"] = [check.model_dump(mode="json") for check in grade.checks]
                        if case.get("report_evaluation_id"):
                            report_checks, quality = grade_report_case(report_cases[case["report_evaluation_id"]], response.run,
                                [doc for doc in docs if doc["id"] in case["fund_report_ids"]],
                                {"retrieval_mode": args.retrieval_mode})
                            record["report_quality"] = quality
                            record["checks"].extend(check.model_dump(mode="json") for check in report_checks)
                            if args.retrieval_mode == "hybrid" and reader.reranker.available:
                                diagnostics = [item.get("retrieval_diagnostics", {}) for item in tool_payloads(response.run)]
                                reranks = [item.get("rerank", {}).get("status") for item in diagnostics]
                                record["checks"].append({"name": "真实精排执行且未降级",
                                    "passed": bool(reranks) and all(status == "applied" or
                                        (status == "not_needed" and item.get("query_mode") == "allocation")
                                        for status, item in zip(reranks, diagnostics)),
                                    "detail": reranks})
                    else:
                        record["checks"].append({"name": "取得结构化最终结果", "passed": False,
                            "detail": "错误事件或请求结束但未返回 result；查看原始 SSE"})
                    detail = (await client.get(f"/api/v1/conversations/{conversation_id}")).json()
                    record["conversation"] = detail
                    for turn in record["turns"]:
                        events = [item["event"] for item in turn["frames"]]
                        delta = "".join(item["data"].get("delta", "") for item in turn["frames"])
                        returned = turn["response"]
                        record["checks"].append({"name": "SSE唯一终态且正文完整", "passed": bool(returned)
                            and events.count("result") == 1 and events.count("done") == 1
                            and not any(event in events for event in ("error", "cancelled"))
                            and delta == returned["assistant_message"]["content"], "detail": events})
                    record["checks"].append({"name": "会话保存完整问答", "passed":
                        len(detail["messages"]) == 2 * len(record["turns"]), "detail": len(detail["messages"])})
                    if case["id"] == "conversation" and response_data:
                        background = response_data["assistant_message"]["run_metadata"].get("investment_context", {})
                        facts = background.get("facts", {})
                        record["checks"].append({"name": "结构化背景沿用且对象已切换", "passed":
                            background.get("fund_code") == "110007" and all(key in facts for key in ("horizon", "loss_tolerance"))
                            and not background.get("needs_confirmation"), "detail": background})
                    record["passed"] = all(check["passed"] for check in record["checks"])
                except Exception as exc:
                    record["error"] = f"{type(exc).__name__}: {exc}"
                record["usage_snapshot"] = get_ai_usage_service().summary(user_id=user_id)
                results.append(record)
                save(f"{case['id']}.json", record)
                save("results.json", results)
                print(f"{'PASS' if record['passed'] else 'FAIL'} {case['id']} "
                    f"used_tokens={record['usage_snapshot']['used_tokens']} "
                    f"admissions={admissions[0]}", flush=True)
                if record["usage_snapshot"]["remaining_tokens"] < 15000:
                    print("STOP: 剩余额度不足，未自动增加预算或重跑。", flush=True)
                    break
    save("usage.json", get_ai_usage_service().summary(user_id=user_id))
    passed = sum(row["passed"] for row in results)
    print(f"DONE automated={passed}/{len(results)} requested={len(cases)}；原始结果 {output}", flush=True)
    return 0 if passed == len(cases) else 1


if __name__ == "__main__":
    raise SystemExit(main())
