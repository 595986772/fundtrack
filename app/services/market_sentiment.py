"""把市场数据库快照或离线快照转换成可解释、可测试的市场状态。

这一层不调用大模型。它只负责读取原始值、执行固定公式并给每个维度贴上透明标签。
REST API、前端和 Agent Tool 都复用这个 service，避免出现三套不同的“市场情绪”。
"""

from __future__ import annotations

import json
import math
import statistics
from pathlib import Path
from typing import Any

from app.services.funds import (
    FundCatalogError,
    FundCatalogService,
    fund_catalog_service,
)
from app.services.eval_context import frozen_eval_data_enabled
from app.services.market_data_sync import (
    MarketSnapshotRepository,
    market_snapshot_repository,
)
from app.services.market_indices import (
    MarketIndexAnalysisService,
    MarketIndexError,
    market_index_analysis_service,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MARKET_FIXTURE = (
    PROJECT_ROOT / "data" / "market_fixtures" / "market_snapshot.json"
)
TRADING_DAYS_PER_YEAR = 252
THREE_MONTH_TRADING_DAYS = 60


class MarketSentimentError(RuntimeError):
    """市场快照缺失、格式错误或观察值不足。"""


class MarketSentimentService:
    """根据四个可追溯维度生成市场环境，而不是制造一个神秘总分。"""

    def __init__(
        self,
        fixture_path: Path = DEFAULT_MARKET_FIXTURE,
        catalog: FundCatalogService = fund_catalog_service,
        snapshot_repository: MarketSnapshotRepository | None = None,
        index_analysis_service: MarketIndexAnalysisService | None = None,
    ) -> None:
        # 测试实例默认只读传入的 fixture；全局产品实例在文件末尾显式注入数据库。
        self.fixture_path = fixture_path
        self.catalog = catalog
        self.snapshot_repository = snapshot_repository
        self.index_analysis_service = index_analysis_service

    def analyze(self) -> dict[str, Any]:
        """返回趋势、波动、市场宽度、成交活跃度和综合状态。"""

        snapshot = self._load_snapshot()
        data_date = snapshot["data_date"]
        data_mode = snapshot.get("_data_mode", "fixture")
        data_updated_at = snapshot.get("_data_updated_at")
        proxy = snapshot["broad_market_proxy"]

        try:
            detail = self.catalog.get_fund(proxy["fund_code"])
        except FundCatalogError as exc:
            raise MarketSentimentError("宽基代理基金数据不可用") from exc

        # 只使用市场快照日期及以前的净值，防止未来刷新基金文件后发生数据穿越。
        points = [
            point for point in detail["nav_points"] if point["nav_date"] <= data_date
        ]
        if len(points) < 61:
            raise MarketSentimentError("宽基趋势至少需要 61 个净值点")
        proxy_data_date = points[-1]["nav_date"]

        closes = [float(point["unit_nav"]) for point in points]
        daily_returns = [
            current / previous - 1
            for previous, current in zip(closes, closes[1:])
        ]
        return_20d = (closes[-1] / closes[-21] - 1) * 100
        return_30d = (closes[-1] / closes[-31] - 1) * 100
        return_60d = (
            closes[-1] / closes[-(THREE_MONTH_TRADING_DAYS + 1)] - 1
        ) * 100
        # 页面所说的“近 3 个月”统一按 60 个交易日近似，避免自然月长度波动。
        return_3m = return_60d
        volatility_20d = (
            statistics.stdev(daily_returns[-20:])
            * math.sqrt(TRADING_DAYS_PER_YEAR)
            * 100
        )

        breadth = snapshot["breadth"]
        traded_count = (
            breadth["advancing_count"]
            + breadth["declining_count"]
            + breadth["flat_count"]
        )
        if traded_count <= 0:
            raise MarketSentimentError("市场涨跌家数合计必须大于 0")
        advance_ratio = breadth["advancing_count"] / traded_count * 100

        turnover = snapshot["turnover_proxy"]
        amount_points = sorted(
            turnover["daily_amounts"], key=lambda item: item["date"]
        )
        if len(amount_points) < 5:
            raise MarketSentimentError("成交额代理至少需要 5 个交易日")
        latest_amount = float(amount_points[-1]["amount_cny"])
        five_day_average = statistics.fmean(
            float(item["amount_cny"]) for item in amount_points[-5:]
        )
        latest_vs_average = latest_amount / five_day_average * 100

        trend_label = self._trend_label(return_20d, return_60d)
        volatility_label = self._volatility_label(volatility_20d)
        breadth_label = self._breadth_label(advance_ratio)
        activity_label = self._activity_label(latest_vs_average)
        state_code, state_label = self._overall_state(
            trend_label=trend_label,
            breadth_label=breadth_label,
            volatility_label=volatility_label,
            activity_label=activity_label,
        )

        # summary 由确定性标签拼接，不调用模型，也不包含收益预测。
        summary = (
            f"宽基趋势{trend_label}，当日市场宽度{breadth_label}，"
            f"20 日波动{volatility_label}，成交代理{activity_label}，"
            f"整体呈现{state_label}。"
        )

        trend = {
            "proxy_code": proxy["fund_code"],
            "proxy_name": proxy["name"],
            "tracking_target": proxy["tracking_target"],
            "return_20d_pct": round(return_20d, 2),
            "return_30d_pct": round(return_30d, 2),
            "return_60d_pct": round(return_60d, 2),
            "return_3m_pct": round(return_3m, 2),
            "label": trend_label,
        }
        volatility = {
            "window_days": 20,
            "annualized_volatility_pct": round(volatility_20d, 2),
            "annualization_trading_days": TRADING_DAYS_PER_YEAR,
            "label": volatility_label,
        }
        breadth_result = {
            "advancing_count": breadth["advancing_count"],
            "declining_count": breadth["declining_count"],
            "flat_count": breadth["flat_count"],
            "suspended_count": breadth["suspended_count"],
            "limit_up_count": breadth["limit_up_count"],
            "limit_down_count": breadth["limit_down_count"],
            "advance_ratio_pct": round(advance_ratio, 2),
            "market_activity_pct": breadth["market_activity_pct"],
            "label": breadth_label,
        }
        activity = {
            "benchmark_code": turnover["benchmark_code"],
            "benchmark_name": turnover["benchmark_name"],
            "latest_amount_cny": round(latest_amount, 2),
            "five_day_average_amount_cny": round(five_day_average, 2),
            "latest_vs_five_day_average_pct": round(latest_vs_average, 2),
            "label": activity_label,
        }

        # 指数分区读取另一张原始行情表。评测模式不读取不断变化的数据库，保证同一
        # 测试样本在不同日期仍得到相同结果。
        index_overview = None
        index_analysis_error = ""
        if self.index_analysis_service is not None and not frozen_eval_data_enabled():
            try:
                index_overview = self.index_analysis_service.analyze()
            except MarketIndexError as exc:
                # 单个辅助分区异常不能让原有市场总览整体不可用，但限制中必须说明。
                index_analysis_error = str(exc)

        return {
            "data_kind": (
                "database_market_snapshot"
                if data_mode == "database"
                else "frozen_market_snapshot"
            ),
            "data_mode": data_mode,
            "data_updated_at": data_updated_at,
            "data_date": data_date,
            "state_code": state_code,
            "state_label": state_label,
            # 使用 medium 是因为数据本身真实可追溯，但趋势和成交额仍是代理指标。
            "confidence": "medium",
            "summary": summary,
            "trend": trend,
            "volatility": volatility,
            "breadth": breadth_result,
            "activity": activity,
            "index_overview": index_overview,
            "signals": self._signals(
                trend=trend,
                volatility=volatility,
                breadth=breadth_result,
                activity=activity,
            ),
            "evidence_ids": [
                f"broad_proxy_{proxy['fund_code']}_{proxy_data_date}",
                f"market_breadth_{data_date}",
                f"index_amount_{turnover['benchmark_code']}_{data_date}",
            ],
            "sources": [
                {
                    "name": detail.get("source", proxy["source"]),
                    "source_level": "B",
                    "data_date": proxy_data_date,
                    "source_url": (
                        detail.get("profile", {}).get("source_url")
                        or proxy["source_url"]
                    ),
                },
                {
                    "name": breadth["source"],
                    "source_level": "B",
                    "data_date": data_date,
                    "source_url": breadth["source_url"],
                },
                {
                    "name": turnover["source"],
                    "source_level": "B",
                    "data_date": data_date,
                    "source_url": turnover["source_url"],
                },
            ],
            "limitations": [
                "市场宽度是单日截面，不代表未来涨跌。",
                "总览趋势使用沪深300联接基金净值作为宽基代理；指数分区则直接使用指数行情。",
                "成交活跃度使用沪深300指数成交额，不等同于全 A 股成交额。",
                "本接口只衡量量化市场状态；新闻、政策和公告由独立市场事件接口提供。",
            ]
            + (
                [f"宽基指数分区暂不可用：{index_analysis_error}"]
                if index_analysis_error
                else []
            ),
            "methodology": (
                "趋势使用宽基代理基金 20/30/60 个交易日收益，近 3 个月按 60 个"
                "交易日近似；波动率使用最近 20 个日收益的样本标准差乘 sqrt(252)；"
                "市场宽度使用上涨家数占上涨、下跌和平盘家数之和；成交活跃度比较"
                "最新成交额与最近 5 日均值。"
            ),
            "disclaimer": (
                "每日同步数据库快照，不是盘中实时行情；仅用于学习和研究，不构成投资建议。"
                if data_mode == "database"
                else "本地固定历史快照，仅用于学习和研究，不构成投资建议。"
            ),
        }

    def _load_snapshot(self) -> dict[str, Any]:
        """优先读取数据库最新快照，没有数据时再读取离线 JSON。"""

        # 评测上下文跳过数据库最新行情，确保不同日期运行仍使用同一市场截面。
        if self.snapshot_repository is not None and not frozen_eval_data_enabled():
            database_snapshot = self.snapshot_repository.latest()
            if database_snapshot is not None:
                return database_snapshot

        if not self.fixture_path.is_file():
            raise MarketSentimentError("市场状态快照不存在")
        try:
            payload = json.loads(self.fixture_path.read_text(encoding="utf-8"))
            # 提前访问核心字段，让缺少字段的快照在 service 层得到明确错误。
            payload["data_date"]
            payload["broad_market_proxy"]["fund_code"]
            payload["breadth"]["advancing_count"]
            payload["turnover_proxy"]["daily_amounts"]
            payload["_data_mode"] = "fixture"
            payload["_data_updated_at"] = payload.get("generated_at")
            return payload
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise MarketSentimentError("市场状态快照格式错误") from exc

    @staticmethod
    def _trend_label(return_20d: float, return_60d: float) -> str:
        if return_20d >= 3 and return_60d >= 3:
            return "上行"
        if return_20d <= -3 and return_60d <= -3:
            return "下行"
        if return_60d >= 3 and return_20d > -3:
            return "中期偏强"
        if return_60d <= -3 and return_20d < 3:
            return "中期偏弱"
        if return_20d >= 3 or return_20d <= -3:
            return "短中期分化"
        return "震荡"

    @staticmethod
    def _volatility_label(volatility_pct: float) -> str:
        if volatility_pct <= 15:
            return "偏低"
        if volatility_pct <= 25:
            return "中等"
        return "偏高"

    @staticmethod
    def _breadth_label(advance_ratio_pct: float) -> str:
        if advance_ratio_pct >= 60:
            return "上涨占优"
        if advance_ratio_pct <= 40:
            return "下跌占优"
        return "涨跌均衡"

    @staticmethod
    def _activity_label(latest_vs_average_pct: float) -> str:
        if latest_vs_average_pct >= 110:
            return "放量"
        if latest_vs_average_pct <= 90:
            return "缩量"
        return "常态"

    @staticmethod
    def _overall_state(
        *,
        trend_label: str,
        breadth_label: str,
        volatility_label: str,
        activity_label: str,
    ) -> tuple[str, str]:
        trend_positive = trend_label in {"上行", "中期偏强"}
        trend_negative = trend_label in {"下行", "中期偏弱"}

        if trend_negative and breadth_label == "上涨占优":
            return "mixed_recovery", "修复分化"
        if trend_positive and breadth_label == "下跌占优":
            return "mixed_weakening", "强势分化"
        if trend_positive and breadth_label == "上涨占优" and activity_label != "缩量":
            return "constructive", "偏积极"
        if trend_negative and breadth_label == "下跌占优":
            return "cautious", "偏谨慎"
        if volatility_label == "偏高":
            return "high_volatility", "波动偏高"
        return "neutral", "中性震荡"

    @staticmethod
    def _signals(
        *,
        trend: dict[str, Any],
        volatility: dict[str, Any],
        breadth: dict[str, Any],
        activity: dict[str, Any],
    ) -> list[dict[str, str]]:
        """把四个维度整理成 Agent 可逐条引用的证据。"""

        trend_direction = (
            "positive"
            if trend["label"] in {"上行", "中期偏强"}
            else "negative"
            if trend["label"] in {"下行", "中期偏弱"}
            else "neutral"
        )
        breadth_direction = (
            "positive"
            if breadth["label"] == "上涨占优"
            else "negative"
            if breadth["label"] == "下跌占优"
            else "neutral"
        )
        return [
            {
                "key": "trend",
                "label": trend["label"],
                "direction": trend_direction,
                "value_text": (
                    f"20 日 {trend['return_20d_pct']:+.2f}% / "
                    f"30 日 {trend['return_30d_pct']:+.2f}% / "
                    f"近 3 个月 {trend['return_3m_pct']:+.2f}%"
                ),
                "evidence": f"{trend['proxy_name']}净值作为{trend['tracking_target']}代理",
            },
            {
                "key": "volatility",
                "label": volatility["label"],
                "direction": (
                    "caution" if volatility["label"] == "偏高" else "neutral"
                ),
                "value_text": f"20 日年化 {volatility['annualized_volatility_pct']:.2f}%",
                "evidence": "最近 20 个交易日日收益率样本标准差年化",
            },
            {
                "key": "breadth",
                "label": breadth["label"],
                "direction": breadth_direction,
                "value_text": (
                    f"上涨 {breadth['advancing_count']} / 下跌 "
                    f"{breadth['declining_count']}"
                ),
                "evidence": f"上涨家数占比 {breadth['advance_ratio_pct']:.2f}%",
            },
            {
                "key": "activity",
                "label": activity["label"],
                "direction": (
                    "caution" if activity["label"] == "缩量" else "neutral"
                ),
                "value_text": (
                    f"最新为 5 日均值的 "
                    f"{activity['latest_vs_five_day_average_pct']:.2f}%"
                ),
                "evidence": f"{activity['benchmark_name']}成交额代理",
            },
        ]


market_sentiment_service = MarketSentimentService(
    snapshot_repository=market_snapshot_repository,
    index_analysis_service=market_index_analysis_service,
)
