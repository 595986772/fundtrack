"""基金分红、份额折算的采集、清洗与持久化。

风险计算不能只看单位净值：现金分红会让单位净值除息，份额折算会同时改变净值和份额。
本模块把 AKShare 的中文 DataFrame 清洗成稳定结构，并用覆盖记录区分“没有公司行为”与
“尚未抓取”。它只处理事实数据，不调用大模型。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
import math
import re
from typing import Any

from sqlalchemy import delete, func, select

from app.database import Database, runtime_database
from app.models import FundCorporateActionModel, FundCorporateActionSyncModel


CORPORATE_ACTION_SOURCE = "AKShare / Eastmoney Fund Corporate Actions"
DIVIDEND_TEXT_PATTERN = re.compile(
    r"每\s*(?P<units>\d+(?:\.\d+)?)?\s*份.*?现金\s*(?P<amount>\d+(?:\.\d+)?)\s*元"
)
SPLIT_RATIO_PATTERN = re.compile(
    r"(?P<old>\d+(?:\.\d+)?)\s*:\s*(?P<new>\d+(?:\.\d+)?)"
)


class FundCorporateActionError(RuntimeError):
    """公司行为字段无法解析或数据库写入失败。"""


@dataclass(frozen=True)
class FundCorporateAction:
    """Provider 与 Repository 之间使用的稳定公司行为结构。"""

    fund_code: str
    action_date: date
    action_type: str
    cash_dividend_per_unit: float | None = None
    split_ratio: float | None = None
    description: str = ""

    def as_dict(self) -> dict[str, Any]:
        """转换成 Catalog 和收益序列服务都能直接读取的字典。"""

        return {
            "fund_code": self.fund_code,
            "action_date": self.action_date.isoformat(),
            "action_type": self.action_type,
            "cash_dividend_per_unit": self.cash_dividend_per_unit,
            "split_ratio": self.split_ratio,
            "description": self.description,
        }


class AkshareFundCorporateActionProvider:
    """从东方财富基金详情读取全部历史分红和拆分记录。"""

    source = CORPORATE_ACTION_SOURCE

    def fetch_actions(
        self,
        *,
        fund_code: str,
        date_from: date,
        date_to: date,
    ) -> list[FundCorporateAction]:
        """抓取统计区间内的公司行为；空列表也表示一次成功的完整查询。"""

        try:
            import akshare as ak
            import pandas as pd
        except ModuleNotFoundError as exc:
            raise FundCorporateActionError(
                "请先安装 requirements.txt 中的 akshare"
            ) from exc

        try:
            dividend_frame = ak.fund_open_fund_info_em(
                symbol=fund_code,
                indicator="分红送配详情",
            )
            split_frame = ak.fund_open_fund_info_em(
                symbol=fund_code,
                indicator="拆分详情",
            )
        except Exception as exc:
            raise FundCorporateActionError(
                f"AKShare 获取基金 {fund_code} 公司行为失败：{exc}"
            ) from exc

        actions = [
            *self._parse_dividends(
                frame=dividend_frame,
                fund_code=fund_code,
                date_from=date_from,
                date_to=date_to,
                pandas=pd,
            ),
            *self._parse_splits(
                frame=split_frame,
                fund_code=fund_code,
                date_from=date_from,
                date_to=date_to,
                pandas=pd,
            ),
        ]

        # 上游偶尔可能重复同一行。业务唯一键相同时保留最后一条清洗结果。
        unique = {
            (item.action_date, item.action_type): item
            for item in actions
        }
        return sorted(
            unique.values(),
            key=lambda item: (item.action_date, item.action_type),
        )

    def _parse_dividends(
        self,
        *,
        frame: Any,
        fund_code: str,
        date_from: date,
        date_to: date,
        pandas: Any,
    ) -> list[FundCorporateAction]:
        """兼容“每份分红”和“每10份分红”两种上游列名。"""

        if frame.empty:
            return []
        if "除息日" not in frame.columns:
            raise FundCorporateActionError(f"基金 {fund_code} 分红记录缺少除息日")

        dividend_columns = [name for name in frame.columns if "分红" in str(name)]
        if not dividend_columns:
            raise FundCorporateActionError(f"基金 {fund_code} 分红记录缺少金额")
        dividend_column = dividend_columns[0]

        actions: list[FundCorporateAction] = []
        for raw in frame.to_dict(orient="records"):
            action_date = self._parse_date(
                raw.get("除息日"),
                field="除息日",
                fund_code=fund_code,
                pandas=pandas,
            )
            if not date_from <= action_date <= date_to:
                continue
            raw_description = str(raw.get(dividend_column) or "").strip()
            per_unit = self._parse_dividend_per_unit(
                raw_description,
                column_name=str(dividend_column),
                fund_code=fund_code,
            )
            actions.append(
                FundCorporateAction(
                    fund_code=fund_code,
                    action_date=action_date,
                    action_type="cash_dividend",
                    cash_dividend_per_unit=per_unit,
                    description=raw_description,
                )
            )
        return actions

    def _parse_splits(
        self,
        *,
        frame: Any,
        fund_code: str,
        date_from: date,
        date_to: date,
        pandas: Any,
    ) -> list[FundCorporateAction]:
        """把 ``1:1.4982`` 转成每份旧份额对应 1.4982 份新份额。"""

        if frame.empty:
            return []
        required = {"拆分折算日", "拆分折算比例"}
        if not required.issubset(frame.columns):
            raise FundCorporateActionError(f"基金 {fund_code} 拆分记录字段不完整")

        actions: list[FundCorporateAction] = []
        for raw in frame.to_dict(orient="records"):
            action_date = self._parse_date(
                raw.get("拆分折算日"),
                field="拆分折算日",
                fund_code=fund_code,
                pandas=pandas,
            )
            if not date_from <= action_date <= date_to:
                continue
            raw_ratio = str(raw.get("拆分折算比例") or "").strip()
            ratio = self._parse_split_ratio(raw_ratio, fund_code=fund_code)
            split_type = str(raw.get("拆分类型") or "份额折算").strip()
            actions.append(
                FundCorporateAction(
                    fund_code=fund_code,
                    action_date=action_date,
                    action_type="share_split",
                    split_ratio=ratio,
                    description=f"{split_type} {raw_ratio}".strip(),
                )
            )
        return actions

    @staticmethod
    def _parse_date(
        raw_value: Any,
        *,
        field: str,
        fund_code: str,
        pandas: Any,
    ) -> date:
        try:
            value = pandas.to_datetime(raw_value).date()
        except (TypeError, ValueError) as exc:
            raise FundCorporateActionError(
                f"基金 {fund_code} 的{field}无法解析"
            ) from exc
        return value

    @staticmethod
    def _parse_dividend_per_unit(
        text: str,
        *,
        column_name: str,
        fund_code: str,
    ) -> float:
        match = DIVIDEND_TEXT_PATTERN.search(text)
        if match is None:
            raise FundCorporateActionError(
                f"基金 {fund_code} 的分红金额无法解析：{text}"
            )

        # “每份派现金”省略数字时按 1 份；上游也可能只在列名写“每10份”。
        units_text = match.group("units")
        if not units_text:
            column_match = re.search(r"每\s*(\d+(?:\.\d+)?)?\s*份", column_name)
            units_text = column_match.group(1) if column_match else None
        units = float(units_text or 1)
        amount = float(match.group("amount"))
        per_unit = amount / units
        if not math.isfinite(per_unit) or per_unit < 0:
            raise FundCorporateActionError(f"基金 {fund_code} 的每份分红非法")
        return per_unit

    @staticmethod
    def _parse_split_ratio(text: str, *, fund_code: str) -> float:
        match = SPLIT_RATIO_PATTERN.search(text)
        if match is None:
            raise FundCorporateActionError(
                f"基金 {fund_code} 的拆分比例无法解析：{text}"
            )
        old_units = float(match.group("old"))
        new_units = float(match.group("new"))
        ratio = new_units / old_units if old_units else 0.0
        if not math.isfinite(ratio) or ratio <= 0:
            raise FundCorporateActionError(f"基金 {fund_code} 的拆分比例非法")
        return ratio


class FundCorporateActionRepository:
    """公司行为明细和覆盖状态的 SQLAlchemy Repository。"""

    def __init__(self, database: Database = runtime_database) -> None:
        self.database = database
        self.database.create_schema()

    def replace_coverage(
        self,
        *,
        fund_code: str,
        actions: list[FundCorporateAction],
        coverage_start: date,
        coverage_end: date,
        source: str,
        source_url: str,
        fetched_at: datetime,
    ) -> dict[str, Any]:
        """原子替换已抓取区间，空列表也会写入“已确认无事件”的覆盖记录。"""

        if coverage_end < coverage_start:
            raise FundCorporateActionError("公司行为覆盖结束日不能早于开始日")

        with self.database.session() as session, session.begin():
            session.execute(
                delete(FundCorporateActionModel).where(
                    FundCorporateActionModel.fund_code == fund_code,
                    FundCorporateActionModel.action_date >= coverage_start,
                    FundCorporateActionModel.action_date <= coverage_end,
                )
            )
            for action in actions:
                session.add(
                    FundCorporateActionModel(
                        fund_code=fund_code,
                        action_date=action.action_date,
                        action_type=action.action_type,
                        cash_dividend_per_unit=action.cash_dividend_per_unit,
                        split_ratio=action.split_ratio,
                        description=action.description,
                        source=source,
                        source_url=source_url,
                        fetched_at=fetched_at,
                    )
                )

            sync = session.get(FundCorporateActionSyncModel, fund_code)
            if sync is None:
                sync = FundCorporateActionSyncModel(
                    fund_code=fund_code,
                    coverage_start=coverage_start,
                    coverage_end=coverage_end,
                    action_count=len(actions),
                    source=source,
                    source_url=source_url,
                    fetched_at=fetched_at,
                )
                session.add(sync)
            else:
                sync.coverage_start = coverage_start
                sync.coverage_end = coverage_end
                sync.action_count = len(actions)
                sync.source = source
                sync.source_url = source_url
                sync.fetched_at = fetched_at

        return {
            "action_count": len(actions),
            "coverage_start": coverage_start.isoformat(),
            "coverage_end": coverage_end.isoformat(),
        }

    def list_actions(self, *, fund_code: str) -> list[dict[str, Any]]:
        statement = (
            select(FundCorporateActionModel)
            .where(FundCorporateActionModel.fund_code == fund_code)
            .order_by(
                FundCorporateActionModel.action_date.asc(),
                FundCorporateActionModel.action_type.asc(),
            )
        )
        with self.database.session() as session:
            rows = list(session.scalars(statement))
        return [
            {
                "fund_code": row.fund_code,
                "action_date": row.action_date.isoformat(),
                "action_type": row.action_type,
                "cash_dividend_per_unit": row.cash_dividend_per_unit,
                "split_ratio": row.split_ratio,
                "description": row.description,
                "source": row.source,
                "source_url": row.source_url,
                "fetched_at": self._as_utc(row.fetched_at).isoformat(),
            }
            for row in rows
        ]

    def get_coverage(self, *, fund_code: str) -> dict[str, Any] | None:
        with self.database.session() as session:
            row = session.get(FundCorporateActionSyncModel, fund_code)
        if row is None:
            return None
        return {
            "fund_code": row.fund_code,
            "coverage_start": row.coverage_start.isoformat(),
            "coverage_end": row.coverage_end.isoformat(),
            "action_count": row.action_count,
            "source": row.source,
            "source_url": row.source_url,
            "fetched_at": self._as_utc(row.fetched_at).isoformat(),
        }

    def status(self) -> dict[str, Any]:
        """汇总公司行为表的覆盖数量，供数据状态接口和运维检查。"""

        with self.database.session() as session:
            # 两张表没有 join 关系，必须分别聚合；放进同一个 SELECT 会形成笛卡尔积，
            # 让公司行为条数和基金数互相相乘。
            action_count = session.scalar(
                select(func.count(FundCorporateActionModel.fund_code))
            )
            fund_count, coverage_end, fetched_at = session.execute(
                select(
                    func.count(FundCorporateActionSyncModel.fund_code),
                    func.max(FundCorporateActionSyncModel.coverage_end),
                    func.max(FundCorporateActionSyncModel.fetched_at),
                )
            ).one()
        return {
            "action_count": int(action_count or 0),
            "fund_count": int(fund_count or 0),
            "coverage_end": coverage_end.isoformat() if coverage_end else None,
            "last_successful_sync_at": (
                self._as_utc(fetched_at).isoformat() if fetched_at else None
            ),
        }

    @staticmethod
    def _as_utc(value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)


fund_corporate_action_repository = FundCorporateActionRepository(runtime_database)
fund_corporate_action_provider = AkshareFundCorporateActionProvider()
