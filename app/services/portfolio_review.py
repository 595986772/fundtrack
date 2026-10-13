"""持仓后的只读汇总用例。复用既有计算，不调用模型、不抓取数据、不写账本。"""

from app.services.portfolios import portfolio_service
from app.services.portfolio_performance import portfolio_performance_service, PortfolioPerformanceError
from app.services.portfolio_assessment import portfolio_assessment_service, PortfolioAssessmentError
from app.services.portfolio_research_audit import portfolio_research_audit_service


class PortfolioReviewService:
    def __init__(self, portfolios=portfolio_service, performance=portfolio_performance_service,
                 assessment=portfolio_assessment_service, research=portfolio_research_audit_service):
        self.portfolios = portfolios
        self.performance = performance
        self.assessment = assessment
        self.research = research

    def read(self, *, user_id: str, portfolio_id: str, months: int = 12) -> dict:
        # 必须先验证归属。不能把“无权读取账本”包装成一个空的分析结果。
        portfolio = self.portfolios.get(user_id=user_id, portfolio_id=portfolio_id)
        result = {"portfolio": portfolio, "period_months": months, "performance": None,
                  "assessment": None, "gaps": [], "latest_report": None}
        # 新记账可能尚无足够净值。某一项业务数据不足不应挡住其他已知事实。
        for name, reader, error_type in (
            ("performance", self.performance.analyze, PortfolioPerformanceError),
            ("assessment", self.assessment.assess, PortfolioAssessmentError),
        ):
            try:
                result[name] = reader(user_id=user_id, portfolio_id=portfolio_id, months=months)
            except error_type as exc:
                result["gaps"].append({"section": name, "message": str(exc)})
        history = self.research.list_runs(user_id=user_id, portfolio_id=portfolio_id, months=months, limit=1)
        result["latest_report"] = history["runs"][0] if history["runs"] else None
        # 首页只传摘要，完整曲线与全部证据仍由详情接口负责。两个前端保持同一口径。
        if result["performance"] is not None:
            result["performance"] = {key: result["performance"].get(key) for key in (
                "performance_label", "position_mode", "period_start", "period_end", "observation_count",
                "twr_pct", "max_drawdown", "annualized_volatility_pct", "warnings")}
        if result["assessment"] is not None:
            assessment = result["assessment"]
            result["assessment"] = {key: assessment.get(key) for key in (
                "state_label", "summary", "assessment_date", "evidence_count")}
            result["assessment"]["risk_evidence"] = (assessment.get("risk_evidence") or [])[:3]
        return result


portfolio_review_service = PortfolioReviewService()
