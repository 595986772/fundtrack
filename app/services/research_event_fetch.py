"""受控补查公开事件。子进程只读固定来源，不带用户 Cookie、不写库、不调用模型。"""

from dataclasses import asdict
from datetime import date, timedelta
from pathlib import Path
import json
import re
import subprocess
import sys


def fetch_research_events(fund_codes, *, timeout_seconds=25):
    if not fund_codes or len(fund_codes) > 3 or any(not re.fullmatch(r"\d{6}", code) for code in fund_codes):
        raise ValueError("公开补查仅支持最多三只持仓基金")
    # AKShare 部分调用没有可控超时；进程超时真正结束抓取，不留下后台下载线程。
    try:
        result = subprocess.run([sys.executable, "-m", "app.services.research_event_fetch", *fund_codes],
            cwd=Path(__file__).resolve().parents[2], capture_output=True, text=True,
            timeout=timeout_seconds, check=False)
    except subprocess.TimeoutExpired as exc:
        raise TimeoutError("事件补查超时，已停止") from exc
    if result.returncode:
        raise RuntimeError("公开事件来源暂不可用")
    return json.loads(result.stdout)


def main():
    from contextlib import redirect_stdout
    from app.services.market_event_sync import AkshareMarketEventProvider
    with redirect_stdout(sys.stderr):
        today = date.today()
        batch = AkshareMarketEventProvider(fund_codes=tuple(sys.argv[1:])).fetch_events(
            since=today - timedelta(days=3), until=today)
        value = asdict(batch)
        value["events"] = value["events"][:30]
    print(json.dumps(value, ensure_ascii=False, default=lambda value: value.isoformat()))


if __name__ == "__main__":
    main()
