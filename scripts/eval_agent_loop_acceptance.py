"""运行免费的产品聊天入口验收并导出报告；必须作为独立进程运行。"""

import argparse
import asyncio
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory


def main():
    parser = argparse.ArgumentParser(description="Agent Loop 离线入口验收，不调用真实模型")
    parser.add_argument("--case", action="append", dest="case_ids", help="只跑指定场景，可重复")
    parser.add_argument("--output", type=Path, default=Path(".cache/agent-loop-acceptance"))
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root))
    with TemporaryDirectory(prefix="fundscope-offline-bootstrap-") as bootstrap:
        # 必须在导入 app 前隔离：一些现有全局服务在模块加载时会初始化数据库。
        # 覆盖仅影响本脚本子进程，不修改 .env，更不写入线上数据库。
        os.environ.update(DATABASE_URL=f"sqlite:///{bootstrap}/bootstrap.db",
            DATABASE_AUTO_CREATE_SCHEMA="true", AI_CONTROL_BACKEND="memory",
            JOB_EXECUTION_MODE="local", TRACE_ENABLED="false", AGENT_TASK_AUTHORIZATION_ENABLED="false",
            LANGGRAPH_CHECKPOINT_DATABASE_URL=f"sqlite:///{bootstrap}/checkpoint.db")
        os.environ.setdefault("MPLCONFIGDIR", str(root / ".cache/matplotlib"))
        from tests.agent_loop_acceptance import run_acceptance_suite

        report = asyncio.run(run_acceptance_suite(case_ids=args.case_ids,
            progress_callback=lambda result: print(
                f"{'PASS' if result.passed else 'FAIL'} {result.case_id}", flush=True)))
        output = args.output.resolve()
        output.mkdir(parents=True, exist_ok=True)
        (output / "report.json").write_text(report.model_dump_json(indent=2), encoding="utf-8")
        labels = {"completed": "完整完成", "handled_limitation": "正确处理限制", "system_error": "验收失败"}
        lines = ["# Agent Loop 产品入口验收", "",
            "> 固定行情、脚本模型、临时 SQLite、内存槽位。Token 为合成用量，耗时为本地测试时间。",
            "> 不代表真实模型回答质量、云端性能或 PostgreSQL/Redis 多进程验证。", "",
            f"通过：{report.passed_cases}/{report.total_cases}；数据指纹：`{report.data_fingerprint}`", "",
            "| 场景 | 结果 | 模型工具意图 | 来源实际执行 | 本地耗时 ms | 合成 Token（已知） |",
            "| --- | --- | ---: | ---: | ---: | ---: |"]
        for result in report.results:
            lines.append(f"| {result.name} | {labels[result.outcome]} | {result.tool_call_count} | "
                f"{result.physical_tool_attempts if result.physical_tool_attempts is not None else '未知'} | "
                f"{result.duration_ms:.1f} | {result.usage.total_tokens} |")
        lines.extend(["", "## 失败详情", ""])
        failed = [row for row in report.results if not row.passed]
        if not failed:
            lines.append("全部场景通过。正确拒绝、追问、缺证据和超时收尾不等于业务分析已完整完成。")
        for result in failed:
            lines.append(f"### {result.name}")
            if result.error:
                lines.append(result.error)
            lines.extend(f"- {check.name}：{check.detail}" for check in result.checks if not check.passed)
        (output / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"通过 {report.passed_cases}/{report.total_cases}；报告：{output / 'report.md'}")
        for result in failed:
            print(f"FAIL {result.case_id}: {result.error or [(c.name, c.detail) for c in result.checks if not c.passed]}")
        return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
