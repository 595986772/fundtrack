"""投研终端的图表数据适配层，不采集行情，也不调用模型。

收益口径复用 fund_returns；这里仅补齐展示所需的日期、短周期摘要和基准曲线。
把计算放在后端，避免浏览器用净值相除而误把分红当作下跌。
"""

from calendar import monthrange
from datetime import date, timedelta
from typing import Any

from app.services.fund_returns import FundReturnSeriesError, FundReturnSeriesService


def chart_period_start(end: date, period: str) -> date:
    """30 日使用自然日，月份回退会处理月末，而不是固定减去 90 天。"""
    if period == "30d":
        return end - timedelta(days=30)
    months = int(period[:-1])
    month_index = end.year * 12 + end.month - 1 - months
    year, month_zero = divmod(month_index, 12)
    month = month_zero + 1
    return date(year, month, min(end.day, monthrange(year, month)[1]))


def fund_period_summary(detail: dict[str, Any]) -> dict[str, Any]:
    """列表只携带短摘要，不把每只基金的三年历史重复塞进列表接口。"""
    try:
        series = FundReturnSeriesService().build(detail=detail)
    except FundReturnSeriesError as exc:
        return {"return_30d_pct": None, "return_3m_pct": None,
                "return_basis_label": "收益口径暂不可用", "chart_warnings": [str(exc)]}
    points = series.index_points
    end = date.fromisoformat(points[-1]["nav_date"])
    result: dict[str, Any] = {"return_basis_label": series.return_basis_label,
                             "chart_warnings": series.data_quality_warnings}
    for period, key in [("30d", "return_30d_pct"), ("3m", "return_3m_pct")]:
        cutoff = chart_period_start(end, period).isoformat()
        # 非交易日使用截止日之前最近一个净值点；历史不足时返回空值，不冒充全区间收益。
        baseline = next((point for point in reversed(points) if point["nav_date"] <= cutoff), None)
        result[key] = round((points[-1]["index_value"] / baseline["index_value"] - 1) * 100, 2) if baseline else None
    return result


def build_fund_chart_data(detail: dict[str, Any], benchmark: dict[str, Any] | None = None) -> dict[str, Any]:
    """输出日期对齐的净值与收益指数。基准缺失时留空，不拿沪深300冒充基金基准。"""
    series = FundReturnSeriesService().build(detail=detail)
    navs = {point["nav_date"]: point["unit_nav"] for point in detail["nav_points"]}
    points = [{"date": point["nav_date"], "value": point["index_value"],
               "nav": navs[point["nav_date"]]} for point in series.index_points]
    benchmark_points = []
    # 基准指数按自己的交易日连续复利，前端比较时再截取共同日期、统一起点。
    # 不填充缺失日期，不把缺少覆盖的区间绘制成“完整基准”。
    level = 1.0
    if benchmark and benchmark.get("benchmark_status") != "unavailable":
        for point in benchmark.get("points", []):
            level *= 1 + point["daily_return"]
            benchmark_points.append({"date": point["benchmark_date"], "value": level})
    return {
        "fund_code": detail["profile"]["fund_code"],
        "short_name": detail["profile"]["short_name"],
        "source": detail["source"], "data_mode": detail["data_mode"],
        "data_updated_at": detail.get("data_updated_at"),
        "points": points, **series.metadata(),
        "benchmark_name": (benchmark or {}).get("benchmark_name") or detail["profile"].get("benchmark") or "未披露基准",
        "benchmark_status": (benchmark or {}).get("benchmark_status", "unavailable"),
        "benchmark_points": benchmark_points,
        "benchmark_warnings": (benchmark or {}).get("warnings", []),
        "benchmark_source_urls": (benchmark or {}).get("source_urls", []),
    }
