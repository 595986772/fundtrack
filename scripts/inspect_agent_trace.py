"""读取本地 Span 文件，按父子关系列出一次 Agent 执行；不访问业务库或模型。"""

import argparse
from collections import defaultdict
from datetime import datetime
import json
from pathlib import Path


def load_spans(directory, trace_id=None):
    """支持轮转文件；末行可能正在写入，只跳过不完整行，不修改原始日志。"""
    spans = {}
    for path in Path(directory).glob("spans-*.jsonl*"):
        with path.open(encoding="utf-8") as stream:
            for line in stream:
                try:
                    row = json.loads(line)
                    context = row["context"]
                    if trace_id and context["trace_id"].removeprefix("0x") != trace_id.removeprefix("0x"):
                        continue
                    spans[(context["trace_id"], context["span_id"])] = row
                except (ValueError, KeyError, TypeError):
                    continue
    return sorted(spans.values(), key=lambda row: row["start_time"])


def describe(spans):
    """并行节点耗时不能直接相加；树状输出保留它们的从属关系。"""
    ids = {row["context"]["span_id"] for row in spans}
    children = defaultdict(list)
    for row in spans:
        parent = row.get("parent_id")
        children[parent if parent in ids else None].append(row)
    lines = []
    def visit(row, depth):
        elapsed = (datetime.fromisoformat(row["end_time"]) - datetime.fromisoformat(row["start_time"])).total_seconds() * 1000
        attrs = row.get("attributes", {})
        details = []
        for key in ("job.attempt", "job.initial_queue_ms", "ai.attempt", "gen_ai.request.model",
                    "gen_ai.usage.input_tokens", "gen_ai.usage.output_tokens", "ai.usage_known",
                    "prompt.version", "tool.name", "research.result_status", "error.type"):
            if key in attrs:
                value = attrs[key]
                details.append(f"{key}={round(value, 2) if isinstance(value, float) else value}")
        state = row.get("status", {}).get("status_code", "UNSET")
        # UNSET 表示没有记录异常，不等于金融判断已被证实。
        lines.append(f"{'  ' * depth}{row['name']}  {elapsed:.1f}ms  {state}  {' '.join(details)}".rstrip())
        for child in children[row["context"]["span_id"]]:
            visit(child, depth + 1)
    for root in children[None]:
        visit(root, 0)
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="查看本地 Agent 执行链路；不带 ID 时列出最近链路")
    parser.add_argument("--trace-id")
    parser.add_argument("--directory", default="data/traces")
    args = parser.parse_args()
    spans = load_spans(args.directory, args.trace_id)
    if not spans:
        print("暂无记录：确认 API/Worker 已开启追踪，执行一次请求后等待约一秒。未采样或进程被强杀也可能缺少记录。")
        return
    if args.trace_id:
        print(describe(spans))
        return
    grouped = defaultdict(list)
    for row in spans:
        grouped[row["context"]["trace_id"]].append(row)
    for trace_id, rows in list(grouped.items())[-20:]:
        errors = sum(row.get("status", {}).get("status_code") == "ERROR" for row in rows)
        print(f"{trace_id.removeprefix('0x')}  steps={len(rows)}  errors={errors}  {rows[0]['name']}")


if __name__ == "__main__":
    main()
