"""同题对照季报整理模式。会调用已配置的模型，产生真实API用量。

固定PDF、题目、混合检索和评分器保持不变。模式只在本进程作用，结果写入
既有评测库，人工验收状态仍为pending，不因自动分高就判定解释正确。
"""

import argparse
import asyncio
import json
from pathlib import Path
import sys


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.fund_report_evals import fund_report_eval_service
from app.services.fund_report_runtime import report_synthesis_scope


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=["standard", "evidence", "balanced"], required=True)
    parser.add_argument("--user-id", default="chen")
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument("--case", action="append", dest="cases")
    selection.add_argument("--all", action="store_true", help="运行现有完整固定题集，不自动重跑失败题")
    args = parser.parse_args()
    # 顺序执行、无自动重跑；不要挑选偶然成功的一次覆盖之前的失败结果。
    with report_synthesis_scope(args.mode):
        run = await fund_report_eval_service.run_suite(user_id=args.user_id, case_ids=args.cases,
            retrieval_mode="hybrid", progress_callback=lambda done, total, label: print(label, flush=True))
    print(json.dumps({"id": run.id, "mode": args.mode, "passed": run.passed_cases,
        "total": run.total_cases, "duration_ms": run.duration_ms, "tokens": run.total_tokens,
        "cases": [{"id": row.case_id, "passed": row.passed, "duration_ms": row.duration_ms,
            # 新评分已检查完成状态；单独输出状态便于与旧版成绩对照，不能只看总分。
            "answer_status": (row.report_quality.get("answer_alignment") or {}).get("status"),
            "tokens": row.usage.total_tokens, "error": row.error, "failure": row.execution_diagnostics,
            "turns": [turn.get("execution") for turn in row.report_quality.get("turns", [])]}
            for row in run.results]}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    asyncio.run(main())
