"""使用真实净值序列计算组合层量化风险。

这个模块是项目与 Riskfolio-Lib 之间的适配层。业务代码只调用
``PortfolioRiskEngine.analyze``，不需要知道第三方库要求的 DataFrame、权重向量和
协方差矩阵格式。以后替换风险库时，LangGraph、FastAPI 和前端都不需要跟着重写。

第一版只回答“当前目标权重在历史上表现出什么风险”，不会自动优化权重，也不会生成
买卖建议。组合收益按固定目标权重每日再平衡近似，这是常见的历史风险测算口径，但不
等同于用户真实账户收益。
"""

from __future__ import annotations

import math
from importlib.metadata import PackageNotFoundError, version
from typing import Any, Protocol

import numpy as np
import pandas as pd

from app.services.funds import fund_catalog_service
from app.services.fund_returns import (
    FundReturnSeriesError,
    FundReturnSeriesService,
    fund_return_series_service,
)
from app.services.portfolios import portfolio_service


SUPPORTED_PORTFOLIO_RISK_MONTHS = {6, 12, 36}
TRADING_DAYS_PER_YEAR = 252
MIN_RETURN_OBSERVATIONS = 20
CVAR_ALPHA = 0.05
SCENARIO_INPUT_SCHEMA_VERSION = "portfolio-scenario-input-v1"


class PortfolioRiskEngineError(RuntimeError):
    """组合净值无法对齐，或第三方风险计算无法完成。"""


class PortfolioReader(Protocol):
    """风险引擎读取组合时真正需要的最小接口，便于测试注入 Fake。"""

    def get(self, *, user_id: str, portfolio_id: str) -> dict[str, Any]: ...


class FundReader(Protocol):
    """风险引擎只要求基金目录能按代码返回净值明细。"""

    def get_fund(self, fund_code: str) -> dict[str, Any]: ...


class PortfolioRiskEngine:
    """把组合持仓和多只基金净值转换成可审计的量化风险快照。"""

    def __init__(
        self,
        *,
        portfolios: PortfolioReader = portfolio_service,
        funds: FundReader = fund_catalog_service,
        return_series: FundReturnSeriesService = fund_return_series_service,
    ) -> None:
        self.portfolios = portfolios
        self.funds = funds
        self.return_series = return_series

    def analyze(
        self,
        *,
        user_id: str,
        portfolio_id: str,
        months: int = 12,
    ) -> dict[str, Any]:
        """计算当前组合的收益、波动、回撤、CVaR、相关性和风险贡献。"""

        return self._analyze(
            user_id=user_id,
            portfolio_id=portfolio_id,
            months=months,
            include_scenario_inputs=False,
        )

    def analyze_for_research_snapshot(
        self,
        *,
        user_id: str,
        portfolio_id: str,
        months: int = 12,
    ) -> dict[str, Any]:
        """研究快照专用：在普通风险结果中附带不公开的冻结收益矩阵。"""

        return self._analyze(
            user_id=user_id,
            portfolio_id=portfolio_id,
            months=months,
            include_scenario_inputs=True,
        )

    def capture_stress_inputs(self, *, user_id: str, portfolio_id: str, months: int) -> dict[str, Any]:
        """复用净值复权及日期对齐，冻结压力测试输入，不计算不必要的年化指标。

        每行保存收益起止日期。某只基金缺日时，该跨日收益不能冒充一天参与连续窗口。
        共同缺失的日期无法凭净值来源识别，因此此处不是官方交易日历认证。
        """
        if months not in SUPPORTED_PORTFOLIO_RISK_MONTHS:
            raise PortfolioRiskEngineError("观察区间仅支持 6、12、36 个月")
        portfolio = self.portfolios.get(user_id=user_id, portfolio_id=portfolio_id)
        holdings = portfolio.get("holdings") or []
        if not holdings or len(holdings) > 20:
            raise PortfolioRiskEngineError("压力测试需要 1 至 20 只有净值的持仓基金")
        prices, sources = self._aligned_return_indexes(holdings=holdings)
        calendar = prices.attrs["observed_calendar"]
        positions = calendar.get_indexer(prices.index)
        returns = prices.pct_change(fill_method=None).iloc[1:]
        starts = prices.index[:-1]
        eligible = np.diff(positions) == 1
        cutoff = prices.index[-1] - pd.DateOffset(months=months)
        keep = starts >= cutoff
        returns, starts, eligible = returns.loc[keep], starts[keep], eligible[keep]
        weights = {str(row["fund_code"]): float(row["weight_pct"]) for row in holdings}
        # 真实持仓市值权重可能经过展示舍入。这里只消除舍入尾差，绝不改变候选权重。
        total = sum(weights.values())
        if not math.isfinite(total) or abs(total - 100) > 0.05 or any(w < 0 for w in weights.values()):
            raise PortfolioRiskEngineError("当前组合权重必须非负且合计接近 100%")
        weights = {code: value * 100 / total for code, value in weights.items()}
        return {
            "portfolio_id": portfolio_id, "portfolio_name": portfolio["name"], "months": months,
            "fund_codes": list(returns.columns),
            "short_names": {row["fund_code"]: row["short_name"] for row in holdings},
            "baseline_weights": weights,
            "dates": [day.date().isoformat() for day in returns.index],
            "start_dates": [day.date().isoformat() for day in starts],
            "eligible": [bool(value) for value in eligible],
            "daily_returns": returns.to_numpy().tolist(),
            "data_sources": sources,
            "calendar_note": "按已保存基金净值的共同日期观察，不是官方交易日历；来源间缺日造成的跨日收益不参与连续窗口。",
        }

    def _analyze(
        self,
        *,
        user_id: str,
        portfolio_id: str,
        months: int,
        include_scenario_inputs: bool,
    ) -> dict[str, Any]:
        """共享计算实现；是否冻结场景输入由调用入口明确决定。"""

        if months not in SUPPORTED_PORTFOLIO_RISK_MONTHS:
            raise PortfolioRiskEngineError("组合风险区间只支持 6、12 或 36 个月")

        portfolio = self.portfolios.get(
            user_id=user_id,
            portfolio_id=portfolio_id,
        )
        holdings = list(portfolio.get("holdings") or [])
        if not holdings:
            raise PortfolioRiskEngineError("组合中没有可计算的基金持仓")

        fund_codes = [str(item["fund_code"]) for item in holdings]
        short_names = {
            str(item["fund_code"]): str(item["short_name"])
            for item in holdings
        }
        weights = pd.Series(
            [float(item["weight_pct"]) / 100 for item in holdings],
            index=fund_codes,
            name="weight",
            dtype=float,
        )
        if not math.isclose(float(weights.sum()), 1.0, abs_tol=0.0001):
            raise PortfolioRiskEngineError("组合权重合计必须为 100%")

        # 不同基金可能缺少某些交易日。这里只保留所有基金都有净值的共同日期，避免
        # 用向前填充伪造某只基金当日收益为 0。
        return_indexes, source_metadata = self._aligned_return_indexes(
            holdings=holdings
        )
        latest_common_date = return_indexes.index[-1]
        cutoff = latest_common_date - pd.DateOffset(months=months)
        period_indexes = return_indexes.loc[
            return_indexes.index >= cutoff,
            fund_codes,
        ]
        returns = period_indexes.pct_change(fill_method=None).dropna(how="any")
        if len(returns) < MIN_RETURN_OBSERVATIONS:
            raise PortfolioRiskEngineError(
                f"共同交易日收益样本不足，至少需要 {MIN_RETURN_OBSERVATIONS} 条"
            )

        # 每日组合收益 = 每只基金当日收益 × 当前目标权重。固定权重意味着这里采用
        # “每日再平衡”的研究近似，不会修改数据库中用户保存的持仓。
        portfolio_returns = returns.mul(weights, axis="columns").sum(axis=1)
        covariance = returns.cov()
        annualized_volatility = (
            float(portfolio_returns.std(ddof=1))
            * math.sqrt(TRADING_DAYS_PER_YEAR)
            * 100
        )
        period_return = (float((1 + portfolio_returns).prod()) - 1) * 100

        # Riskfolio-Lib 在这里承担两件专业计算：历史 CVaR，以及基于协方差的边际
        # 风险贡献。其余简单指标保留在本层，公式更容易审阅和单元测试。
        cvar_95 = self._riskfolio_cvar(
            returns=returns,
            weights=weights,
            covariance=covariance,
        )
        risk_contributions = self._riskfolio_risk_contributions(
            returns=returns,
            weights=weights,
            covariance=covariance,
            short_names=short_names,
        )
        max_drawdown = self._max_drawdown(portfolio_returns)
        correlation = self._correlation_snapshot(returns)
        top_contributor = max(
            risk_contributions,
            key=lambda item: float(item["absolute_risk_share_pct"]),
        )
        basis_values = {item["return_basis"] for item in source_metadata}
        status_values = {item["return_basis_status"] for item in source_metadata}
        portfolio_basis = (
            next(iter(basis_values)) if len(basis_values) == 1 else "mixed"
        )
        portfolio_basis_status = (
            "limited"
            if "limited" in status_values
            else "provider_reported"
            if "provider_reported" in status_values
            else "verified"
        )
        basis_label = (
            source_metadata[0]["return_basis_label"]
            if len(basis_values) == 1
            else "混合收益口径"
        )
        data_quality_warnings = list(
            dict.fromkeys(
                warning
                for item in source_metadata
                for warning in item.get("data_quality_warnings", [])
            )
        )

        result = {
            "data_kind": "quantitative_portfolio_risk",
            "portfolio_id": portfolio_id,
            "portfolio_name": portfolio["name"],
            "period_months": months,
            "period_start": period_indexes.index[0].date().isoformat(),
            "period_end": period_indexes.index[-1].date().isoformat(),
            "price_observation_count": len(period_indexes),
            "return_observation_count": len(returns),
            "period_return_pct": round(period_return, 2),
            "annualized_volatility_pct": round(annualized_volatility, 2),
            "daily_cvar_95_pct": round(cvar_95 * 100, 2),
            "max_drawdown": max_drawdown,
            "risk_contributions": risk_contributions,
            "top_risk_contributor": top_contributor,
            "correlation": correlation,
            "return_basis": portfolio_basis,
            "return_basis_label": basis_label,
            "return_basis_status": portfolio_basis_status,
            "data_quality_warnings": data_quality_warnings,
            "engine": {
                "name": "Riskfolio-Lib",
                "version": self._riskfolio_version(),
                "risk_contribution_measure": "MV",
                "tail_risk_measure": "historical_CVaR_95",
            },
            "data_sources": source_metadata,
            "assumptions": [
                "各基金先独立重建总回报指数，再只保留共同净值日期，不做向前填充。",
                "组合每日收益按当前目标权重每日再平衡近似。",
                "年化波动率按每年 252 个交易日换算。",
            ],
            "limitations": [
                "历史风险不代表未来风险，也不包含申购赎回费、税费或真实成交价格。",
                "当前分析基金层净值相关性，尚未穿透到基金底层股票或债券持仓。",
                *data_quality_warnings,
            ],
            "methodology": (
                f"各基金收益口径汇总为{basis_label}；先重建基金回报指数并按共同交易日"
                "对齐，再计算日收益率矩阵；组合收益使用目标权重加权，最大回撤使用"
                "复利累计曲线；Riskfolio-Lib 使用 MV 口径"
                "分解波动率风险贡献，并以 5% 尾部概率计算历史 CVaR。"
            ),
            "disclaimer": "历史量化风险仅用于学习和研究，不构成投资建议。",
        }

        if include_scenario_inputs:
            # 这里保存的是已经完成分红/折算复权并按共同日期对齐后的日收益，不是
            # 原始行情。它只进入不可变研究快照，报告节点会在公开前删除此字段。
            result["_scenario_validation_inputs"] = {
                "schema_version": SCENARIO_INPUT_SCHEMA_VERSION,
                "fund_codes": fund_codes,
                "short_names": short_names,
                "dates": [value.date().isoformat() for value in returns.index],
                "daily_returns": [
                    [round(float(value), 12) for value in row]
                    for row in returns.to_numpy()
                ],
                "period_start": returns.index[0].date().isoformat(),
                "period_end": returns.index[-1].date().isoformat(),
                "return_observation_count": len(returns),
                "return_basis": portfolio_basis,
                "return_basis_label": basis_label,
                "return_basis_status": portfolio_basis_status,
                "data_quality_warnings": data_quality_warnings,
                "engine": result["engine"],
            }
        return result

    def evaluate_frozen_weights(
        self,
        *,
        scenario_inputs: dict[str, Any],
        weights_pct: dict[str, float],
    ) -> dict[str, Any]:
        """在冻结收益矩阵上重算一组候选权重，不读取当前数据库。"""

        if scenario_inputs.get("schema_version") != SCENARIO_INPUT_SCHEMA_VERSION:
            raise PortfolioRiskEngineError("场景输入版本不受支持")

        fund_codes = [str(value) for value in scenario_inputs.get("fund_codes") or []]
        dates = list(scenario_inputs.get("dates") or [])
        rows = list(scenario_inputs.get("daily_returns") or [])
        if not fund_codes or set(weights_pct) != set(fund_codes):
            raise PortfolioRiskEngineError("候选方案基金集合与冻结输入不一致")
        if len(dates) != len(rows) or len(rows) < MIN_RETURN_OBSERVATIONS:
            raise PortfolioRiskEngineError("冻结收益样本不足，无法比较候选方案")
        if any(not isinstance(row, list) or len(row) != len(fund_codes) for row in rows):
            raise PortfolioRiskEngineError("冻结收益矩阵结构已损坏")

        weights = pd.Series(
            [float(weights_pct[code]) / 100 for code in fund_codes],
            index=fund_codes,
            name="weight",
            dtype=float,
        )
        if bool((weights < 0).any()) or not math.isclose(
            float(weights.sum()),
            1.0,
            abs_tol=0.0001,
        ):
            raise PortfolioRiskEngineError("候选方案权重必须非负且合计为 100%")

        try:
            returns = pd.DataFrame(
                rows,
                index=pd.to_datetime(dates, errors="raise"),
                columns=fund_codes,
                dtype=float,
            )
        except (TypeError, ValueError) as exc:
            raise PortfolioRiskEngineError("冻结收益矩阵无法解析") from exc
        if not np.isfinite(returns.to_numpy()).all():
            raise PortfolioRiskEngineError("冻结收益矩阵包含非有限数值")

        portfolio_returns = returns.mul(weights, axis="columns").sum(axis=1)
        covariance = returns.cov()
        annualized_volatility = (
            float(portfolio_returns.std(ddof=1))
            * math.sqrt(TRADING_DAYS_PER_YEAR)
            * 100
        )
        period_return = (float((1 + portfolio_returns).prod()) - 1) * 100
        cvar_95 = self._riskfolio_cvar(
            returns=returns,
            weights=weights,
            covariance=covariance,
        )
        short_names = {
            code: str((scenario_inputs.get("short_names") or {}).get(code) or code)
            for code in fund_codes
        }
        risk_contributions = self._riskfolio_risk_contributions(
            returns=returns,
            weights=weights,
            covariance=covariance,
            short_names=short_names,
        )

        return {
            "data_kind": "frozen_portfolio_scenario_metrics",
            "schema_version": SCENARIO_INPUT_SCHEMA_VERSION,
            "period_start": str(scenario_inputs.get("period_start") or dates[0]),
            "period_end": str(scenario_inputs.get("period_end") or dates[-1]),
            "return_observation_count": len(returns),
            "period_return_pct": round(period_return, 2),
            "annualized_volatility_pct": round(annualized_volatility, 2),
            "daily_cvar_95_pct": round(cvar_95 * 100, 2),
            "max_drawdown": self._max_drawdown(portfolio_returns),
            "risk_contributions": risk_contributions,
            "top_risk_contributor": max(
                risk_contributions,
                key=lambda item: float(item["absolute_risk_share_pct"]),
            ),
            "return_basis": scenario_inputs.get("return_basis"),
            "return_basis_label": scenario_inputs.get("return_basis_label"),
            "return_basis_status": scenario_inputs.get("return_basis_status"),
            "data_quality_warnings": list(
                scenario_inputs.get("data_quality_warnings") or []
            ),
            "methodology": "使用研究开始时冻结的同一日收益矩阵，按候选权重每日再平衡重算。",
        }

    def _aligned_return_indexes(
        self,
        *,
        holdings: list[dict[str, Any]],
    ) -> tuple[pd.DataFrame, list[dict[str, Any]]]:
        """为每只基金重建回报指数，并用 inner join 保留共同日期。"""

        series_list: list[pd.Series] = []
        metadata: list[dict[str, Any]] = []
        for holding in holdings:
            fund_code = str(holding["fund_code"])
            detail = self.funds.get_fund(fund_code)
            nav_points = list(detail.get("nav_points") or [])
            if not nav_points:
                raise PortfolioRiskEngineError(f"基金 {fund_code} 没有可用净值")

            try:
                return_series = self.return_series.build(
                    detail=detail,
                    points=nav_points,
                )
            except FundReturnSeriesError as exc:
                raise PortfolioRiskEngineError(
                    f"基金 {fund_code} 无法构建收益序列：{exc}"
                ) from exc
            index = pd.to_datetime(
                [point["nav_date"] for point in return_series.index_points],
                errors="raise",
            )
            values = [
                float(point["index_value"])
                for point in return_series.index_points
            ]
            series = pd.Series(values, index=index, name=fund_code, dtype=float)
            # 同一来源理论上每天只有一个净值；若 fixture 和数据库合并后出现重复，
            # 与 FundCatalogService 的规则一致，保留最后一条。
            series = series.groupby(level=0).last().sort_index()
            if bool((series <= 0).any()):
                raise PortfolioRiskEngineError(f"基金 {fund_code} 存在非正回报指数")
            series_list.append(series)
            metadata.append(
                {
                    "fund_code": fund_code,
                    "source": detail.get("source"),
                    "data_mode": detail.get("data_mode", "fixture"),
                    "snapshot_end": self._date_text(detail.get("snapshot_end")),
                    **return_series.metadata(),
                }
            )

        prices = pd.concat(series_list, axis="columns", join="inner").dropna()
        prices = prices.sort_index()
        if len(prices) < MIN_RETURN_OBSERVATIONS + 1:
            raise PortfolioRiskEngineError("各基金共同净值日期不足，无法计算组合风险")
        # 附加原来源日期并集，供压力测试识别 inner join 隐藏的跨日间隔。
        calendar = series_list[0].index
        for series in series_list[1:]:
            calendar = calendar.union(series.index)
        prices.attrs["observed_calendar"] = calendar.sort_values()
        return prices, metadata

    @staticmethod
    def _riskfolio_cvar(
        *,
        returns: pd.DataFrame,
        weights: pd.Series,
        covariance: pd.DataFrame,
    ) -> float:
        """调用 Riskfolio-Lib 计算最差 5% 交易日的平均损失。"""

        rp = PortfolioRiskEngine._load_riskfolio()
        try:
            result = rp.Sharpe_Risk(
                returns=returns,
                w=weights.to_numpy().reshape(-1, 1),
                cov=covariance,
                rm="CVaR",
                alpha=CVAR_ALPHA,
            )
        except (TypeError, ValueError, ArithmeticError) as exc:
            raise PortfolioRiskEngineError(
                f"Riskfolio-Lib 无法计算组合 CVaR：{exc}"
            ) from exc
        return float(np.asarray(result).reshape(-1)[0])

    @staticmethod
    def _riskfolio_risk_contributions(
        *,
        returns: pd.DataFrame,
        weights: pd.Series,
        covariance: pd.DataFrame,
        short_names: dict[str, str],
    ) -> list[dict[str, Any]]:
        """把 Riskfolio-Lib 返回的日波动贡献转换成年化百分点和占比。"""

        rp = PortfolioRiskEngine._load_riskfolio()
        try:
            raw = rp.Risk_Contribution(
                w=weights.to_numpy().reshape(-1, 1),
                returns=returns,
                cov=covariance,
                rm="MV",
            )
        except (TypeError, ValueError, ArithmeticError) as exc:
            raise PortfolioRiskEngineError(
                f"Riskfolio-Lib 无法计算风险贡献：{exc}"
            ) from exc

        values = np.asarray(raw, dtype=float).reshape(-1)
        annualized = values * math.sqrt(TRADING_DAYS_PER_YEAR) * 100
        signed_total = float(annualized.sum())
        absolute_total = float(np.abs(annualized).sum())
        contributions: list[dict[str, Any]] = []
        for fund_code, annualized_value in zip(weights.index, annualized):
            signed_share = (
                float(annualized_value) / signed_total * 100
                if not math.isclose(signed_total, 0.0, abs_tol=1e-12)
                else 0.0
            )
            absolute_share = (
                abs(float(annualized_value)) / absolute_total * 100
                if not math.isclose(absolute_total, 0.0, abs_tol=1e-12)
                else 0.0
            )
            contributions.append(
                {
                    "fund_code": fund_code,
                    "short_name": short_names[fund_code],
                    "weight_pct": round(float(weights.loc[fund_code]) * 100, 2),
                    "annualized_risk_contribution_pct_points": round(
                        float(annualized_value),
                        4,
                    ),
                    "risk_share_pct": round(signed_share, 2),
                    "absolute_risk_share_pct": round(absolute_share, 2),
                }
            )
        return sorted(
            contributions,
            key=lambda item: item["absolute_risk_share_pct"],
            reverse=True,
        )

    @staticmethod
    def _max_drawdown(portfolio_returns: pd.Series) -> dict[str, Any]:
        """在组合复利净值曲线上寻找最大历史回撤及是否已经恢复。"""

        wealth = (1 + portfolio_returns).cumprod()
        running_peak = wealth.cummax()
        drawdowns = wealth / running_peak - 1
        trough_date = drawdowns.idxmin()
        peak_date = wealth.loc[:trough_date].idxmax()
        peak_value = float(wealth.loc[peak_date])
        recovery_candidates = wealth.loc[trough_date:]
        recovery_candidates = recovery_candidates[recovery_candidates >= peak_value]
        recovery_date = (
            recovery_candidates.index[0] if not recovery_candidates.empty else None
        )
        return {
            "drawdown_pct": round(abs(float(drawdowns.loc[trough_date])) * 100, 2),
            "peak_date": peak_date.date().isoformat(),
            "trough_date": trough_date.date().isoformat(),
            "recovery_date": (
                recovery_date.date().isoformat() if recovery_date is not None else None
            ),
        }

    @staticmethod
    def _correlation_snapshot(returns: pd.DataFrame) -> dict[str, Any]:
        """输出前端友好的相关系数矩阵，并标记绝对相关性最高的一对基金。"""

        matrix = returns.corr()
        strongest_pair: dict[str, Any] | None = None
        for left_index, left_code in enumerate(matrix.columns):
            for right_index in range(left_index + 1, len(matrix.columns)):
                right_code = matrix.columns[right_index]
                value = float(matrix.loc[left_code, right_code])
                candidate = {
                    "left_fund_code": str(left_code),
                    "right_fund_code": str(right_code),
                    "correlation": round(value, 4),
                }
                if strongest_pair is None or abs(value) > abs(
                    float(strongest_pair["correlation"])
                ):
                    strongest_pair = candidate

        return {
            "fund_codes": [str(code) for code in matrix.columns],
            "values": [
                [round(float(value), 4) for value in row]
                for row in matrix.to_numpy()
            ],
            "strongest_pair": strongest_pair,
        }

    @staticmethod
    def _load_riskfolio():
        """延迟导入较重的量化库，不拖慢普通聊天和健康检查启动。"""

        try:
            import riskfolio as rp
        except ImportError as exc:
            raise PortfolioRiskEngineError(
                "缺少 Riskfolio-Lib，请先执行 pip install -r requirements.txt"
            ) from exc
        return rp

    @staticmethod
    def _riskfolio_version() -> str:
        try:
            return version("riskfolio-lib")
        except PackageNotFoundError:
            return "unknown"

    @staticmethod
    def _date_text(value: Any) -> str | None:
        if value is None:
            return None
        if hasattr(value, "isoformat"):
            return str(value.isoformat())
        return str(value)


# 应用级实例由 LangGraph 节点复用；单元测试会构造注入 Fake 数据源的独立实例。
portfolio_risk_engine = PortfolioRiskEngine()
