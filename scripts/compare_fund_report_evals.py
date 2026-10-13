"""对照两次已保存的季报测评，不调用付费API；输出JSON供复盘或后续展示。"""

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.fund_report_eval_comparison import compare_report_runs
from app.services.observability import agent_observability_service


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--before", required=True)
    parser.add_argument("--after", required=True)
    parser.add_argument("--user-id", default="chen")
    args = parser.parse_args()
    # 沿用观测服务的用户隔离，不能只拿一个run_id跨用户读取。
    runs = [agent_observability_service.get_eval_run(user_id=args.user_id, run_id=run_id)
        for run_id in (args.before, args.after)]
    result = compare_report_runs(*runs)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["comparable"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
