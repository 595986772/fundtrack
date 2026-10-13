"""本地 UI 验证夹具：只提供假报告及四个静态文件，不连接业务数据库或模型。

运行 python -m tests.preview_fund_answer 后在 127.0.0.1:8767 查看。
这是独立测试服务器，不给正式产品添加测试入口；Ctrl+C 结束。
"""

import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from app.services.fund_answer import FundAnswerCheck
from app.services.investment_context import InvestmentContext, InvestmentFact, investment_context_scope
from tests.test_fund_answer import brief, valid_draft


STATIC = Path(__file__).resolve().parents[1] / "app" / "static"


class PreviewHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        path = urlsplit(self.path).path
        if path == "/":
            payload = brief()
            draft = valid_draft(payload)
            # 展示数值和长字段，并保留与正式渲染器相同的成功/降级两种状态。
            draft["facts"] = [{key: fact[key] for key in ("evidence_id", "fund_code", "value", "unit", "period")}
                              for fact in payload["answer_facts"][:2]]
            with investment_context_scope(InvestmentContext(facts={"horizon": InvestmentFact(
                text="这笔钱三年不用", source_turn="preview-only", updated_at="2026-09-06",
            )})):
                checker = FundAnswerCheck(payload)
            counter = next(fact for fact in payload["answer_facts"] if ".top_securities." in fact["source_path"])
            draft["judgments"][0]["counter_evidence"] = [{"evidence_id": counter["evidence_id"], "explanation": "主题持仓可能集中暴露于同一行业，需要留意分散程度。"}]
            draft["judgments"][0]["counter_evidence_gap"] = ""
            draft["judgments"][0]["conditions"] = [
                {"basis": "user_context", "text": "你的持有安排提供了较长观察期。", "background_fields": ["horizon"]},
                {"basis": "assumption", "text": "假设期间没有新的用款安排。", "background_fields": []},
                {"basis": "missing_information", "text": "还需了解你能接受的阶段性亏损。", "background_fields": ["loss_tolerance"]},
            ]
            checker.gaps = ["可承受亏损尚待你补充。"]
            good = checker.finish(json.dumps(draft), attempts=1, initial_issues=["原草稿数值不一致"])
            limited = checker.finish("格式不正确", attempts=1, initial_issues=[])
            data = json.dumps([good.model_dump(), limited.model_dump()], ensure_ascii=False).replace("<", "\\u003c")
            body = ("""<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>单基金回答 · 离线 UI 测试</title><link rel="stylesheet" href="/static/styles.css">
<link rel="stylesheet" href="/static/design-system.css"><link rel="stylesheet" href="/static/terminal.css">
<style>body{overflow:auto;background:var(--canvas)}main{max-width:760px;padding:24px 16px;margin:auto}
.preview-label{color:var(--muted);font-size:12px}.message-content{margin-bottom:32px}</style>
<body class="terminal-experience"><main><p class="preview-label">离线测试数据，不是实际基金研究结果</p>
<article class="terminal-ai-message" data-role="assistant" style="max-width:304px">
<div class="markdown-body" id="good"></div></article><div class="message-content" id="limited"></div></main>
<script src="/static/fund-answer.js"></script><script type="application/json" id="reports">REPLACE_DATA</script>
<script>const reports=JSON.parse(document.getElementById('reports').textContent);
document.getElementById('good').innerHTML=FundScopeFundAnswer.render(reports[0]);
document.getElementById('limited').innerHTML=FundScopeFundAnswer.render(reports[1]);</script></html>""").replace("REPLACE_DATA", data).encode()
            content_type = "text/html; charset=utf-8"
        elif path in ("/static/styles.css", "/static/design-system.css", "/static/terminal.css", "/static/fund-answer.js"):
            body = (STATIC / Path(path).name).read_bytes()
            content_type = "text/css" if path.endswith(".css") else "text/javascript"
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    HTTPServer(("127.0.0.1", 8767), PreviewHandler).serve_forever()
