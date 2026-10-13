"""FundScope 基金档案与净值的统一读取服务。

本地 fixture 继续提供稳定的历史基线；每日同步成功后，数据库记录按日期覆盖或追加到
基线上。REST、风险计算和 Agent Tool 都只依赖这个入口，因此不需要分别判断数据来源。
"""

from __future__ import annotations

import csv
import json
from collections import defaultdict
from datetime import date
from pathlib import Path
from typing import Any

from app.services.fund_data_sync import (
    FundNavSnapshotRepository,
    fund_nav_snapshot_repository,
)
from app.services.fund_corporate_actions import (
    FundCorporateActionRepository,
    fund_corporate_action_repository,
)
from app.services.fund_catalog import (
    FundCatalogRepository,
    fund_catalog_repository,
)
from app.services.eval_context import frozen_eval_data_enabled
from app.services.fund_chart_data import fund_period_summary
from app.services.errors import ServiceError


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_FIXTURE_DIR = PROJECT_ROOT / "data" / "fund_fixtures"


class FundCatalogError(ServiceError):
    """基金样本文件缺失或无法解析。"""


class FundNotFoundError(FundCatalogError):
    """请求的基金尚未导入当前工作台。"""

    code = "resource_not_found"


class FundCatalogService:
    """把基金档案、本地历史和数据库最新快照合并成统一目录。"""

    def __init__(
        self,
        fixture_dir: Path = DEFAULT_FIXTURE_DIR,
        snapshot_repository: FundNavSnapshotRepository = fund_nav_snapshot_repository,
        catalog_repository: FundCatalogRepository = fund_catalog_repository,
        action_repository: FundCorporateActionRepository | None = None,
    ) -> None:
        self.fixture_dir = fixture_dir
        self.snapshot_repository = snapshot_repository
        self.catalog_repository = catalog_repository
        # 自定义净值 Repository（例如临时测试数据库）应自动配套同一个数据库的公司
        # 行为 Repository，避免净值来自测试库、分红却误读运行时数据库。
        if action_repository is not None:
            self.action_repository = action_repository
        elif hasattr(snapshot_repository, "database"):
            self.action_repository = FundCorporateActionRepository(
                snapshot_repository.database
            )
        else:
            # 部分纯单元测试只注入一个没有 SQL 连接的轻量净值 Reader。冻结评测不会
            # 读取数据库公司行为；离开冻结上下文后仍使用应用默认只读 Repository。
            self.action_repository = fund_corporate_action_repository

    def list_funds(self) -> dict[str, Any]:
        """返回 fixture 基线和用户已导入基金的最新净值。"""
        profile_payload, nav_by_fund, metadata_by_fund = self._load_catalog()
        funds = []

        # 使用 JSON 中的顺序，让前端每次展示顺序一致。
        for profile in profile_payload["funds"]:
            fund_code = profile["fund_code"]
            nav_points = nav_by_fund.get(fund_code, [])
            if not nav_points:
                raise FundCatalogError(f"基金 {fund_code} 没有净值数据", code="data_unavailable")
            latest = nav_points[-1]
            action_context = self._load_corporate_action_context(fund_code=fund_code)
            period_summary = fund_period_summary({
                "nav_points": nav_points,
                "corporate_actions": action_context["actions"],
                "corporate_action_coverage": action_context["coverage"],
            })
            funds.append(
                {
                    "fund_code": fund_code,
                    "short_name": profile["short_name"],
                    "fund_type": profile["fund_type"],
                    "sample_role": profile["sample_role"],
                    "latest_nav": latest["unit_nav"],
                    "latest_nav_date": latest["nav_date"],
                    "daily_growth_rate_pct": latest["daily_growth_rate_pct"],
                    "nav_record_count": len(nav_points),
                    **period_summary,
                }
            )

        all_points = [point for points in nav_by_fund.values() for point in points]
        database_metadata = [
            metadata
            for metadata in metadata_by_fund.values()
            if metadata["data_mode"] == "database"
        ]
        return {
            "count": len(funds),
            "snapshot_start": min(point["nav_date"] for point in all_points),
            "snapshot_end": max(point["nav_date"] for point in all_points),
            "source": (
                "AKShare / Eastmoney Fund · SQL snapshot"
                if database_metadata
                else profile_payload["source"]
            ),
            "data_mode": "database" if database_metadata else "fixture",
            "data_updated_at": self._latest_updated_at(database_metadata),
            "funds": funds,
        }

    def list_profiles(self) -> list[dict[str, Any]]:
        """返回当前工作台可用的完整基金档案。

        事件关联层需要基金全名、跟踪标的等研究字段，但不需要整段净值历史。
        这里复用统一目录的合并规则，避免它绕过 Service 直接读取 fixture 或数据库。
        """

        profile_payload, _, _ = self._load_catalog()
        return [dict(profile) for profile in profile_payload["funds"]]

    def get_fund(self, fund_code: str) -> dict[str, Any]:
        """返回单只基金档案和用于画图的净值点。"""
        profile_payload, nav_by_fund, metadata_by_fund = self._load_catalog()
        profile = next(
            (
                item
                for item in profile_payload["funds"]
                if item["fund_code"] == fund_code
            ),
            None,
        )
        if profile is None:
            raise FundNotFoundError(f"基金 {fund_code} 尚未导入当前工作台")

        nav_points = nav_by_fund.get(fund_code, [])
        if not nav_points:
            raise FundCatalogError(f"基金 {fund_code} 没有净值数据", code="data_unavailable")
        latest = nav_points[-1]
        metadata = metadata_by_fund[fund_code]
        action_context = self._load_corporate_action_context(fund_code=fund_code)

        return {
            "profile": profile,
            "latest_nav": latest["unit_nav"],
            "latest_nav_date": latest["nav_date"],
            "daily_growth_rate_pct": latest["daily_growth_rate_pct"],
            "nav_record_count": len(nav_points),
            "snapshot_start": nav_points[0]["nav_date"],
            "snapshot_end": nav_points[-1]["nav_date"],
            "source": metadata["source"],
            "data_mode": metadata["data_mode"],
            "data_updated_at": metadata["data_updated_at"],
            "nav_points": nav_points,
            "corporate_actions": action_context["actions"],
            "corporate_action_coverage": action_context["coverage"],
        }

    def _load_catalog(
        self,
    ) -> tuple[
        dict[str, Any],
        dict[str, list[dict[str, Any]]],
        dict[str, dict[str, Any]],
    ]:
        """读取 fixture，并使用数据库中同日期记录覆盖它。"""

        profile_payload, fixture_nav = self._load_fixture()
        # 自动评测必须只看冻结样本；普通请求则把 SQL 中 ready 的完整档案追加进目录。
        if not frozen_eval_data_enabled():
            profiles = list(profile_payload["funds"])
            fixture_codes = {item["fund_code"] for item in profiles}
            profiles.extend(
                profile
                for profile in self.catalog_repository.list_ready_profiles()
                if profile and profile.get("fund_code") not in fixture_codes
            )
            profile_payload = {**profile_payload, "funds": profiles}
        merged_by_fund: dict[str, list[dict[str, Any]]] = {}
        metadata_by_fund: dict[str, dict[str, Any]] = {}

        for profile in profile_payload["funds"]:
            fund_code = profile["fund_code"]
            # 自动评测必须永远读同一份 fixture，不能被今天刚同步的净值改变答案。
            # ContextVar 只作用于当前评测调用，不影响同时发生的真实用户请求。
            database_points = (
                []
                if frozen_eval_data_enabled()
                else self.snapshot_repository.list_points(fund_code=fund_code)
            )
            # 日期是业务唯一键。数据库来自最近同步，因此同日期时优先使用数据库值。
            points_by_date = {
                point["nav_date"]: point
                for point in fixture_nav.get(fund_code, [])
            }
            for point in database_points:
                points_by_date[point["nav_date"]] = {
                    "nav_date": point["nav_date"],
                    "unit_nav": point["unit_nav"],
                    "daily_growth_rate_pct": point["daily_growth_rate_pct"],
                }
            merged_by_fund[fund_code] = sorted(
                points_by_date.values(),
                key=lambda item: item["nav_date"],
            )
            metadata_by_fund[fund_code] = {
                "data_mode": "database" if database_points else "fixture",
                "data_updated_at": (
                    max(point["fetched_at"] for point in database_points)
                    if database_points
                    else None
                ),
                "source": (
                    database_points[-1]["source"] + " · SQL snapshot"
                    if database_points
                    else profile_payload["source"]
                ),
            }

        return profile_payload, merged_by_fund, metadata_by_fund

    def _load_fixture(self) -> tuple[dict[str, Any], dict[str, list[dict[str, Any]]]]:
        """读取并组合基金档案与净值 CSV。"""
        profile_path = self.fixture_dir / "fund_profiles.json"
        nav_path = self.fixture_dir / "fund_nav.csv"
        if not profile_path.is_file() or not nav_path.is_file():
            raise FundCatalogError("基金样本数据不完整，请先运行冻结脚本", code="data_unavailable")

        try:
            profile_payload = json.loads(profile_path.read_text(encoding="utf-8"))
            nav_by_fund: dict[str, list[dict[str, Any]]] = defaultdict(list)
            with nav_path.open(encoding="utf-8", newline="") as nav_file:
                for row in csv.DictReader(nav_file):
                    nav_by_fund[row["fund_code"]].append(
                        {
                            "nav_date": row["nav_date"],
                            "unit_nav": float(row["unit_nav"]),
                            "daily_growth_rate_pct": (
                                float(row["daily_growth_rate_pct"])
                                if row["daily_growth_rate_pct"]
                                else None
                            ),
                        }
                    )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise FundCatalogError("基金样本数据格式错误") from exc

        for nav_points in nav_by_fund.values():
            nav_points.sort(key=lambda item: item["nav_date"])
        return profile_payload, dict(nav_by_fund)

    def _load_corporate_action_context(self, *, fund_code: str) -> dict[str, Any]:
        """合并冻结样例与 SQL 公司行为，并保留真实连续覆盖区间。

        覆盖区间不能只取最早和最晚日期；中间若存在未同步空档，就不能声称整段数据
        完整。因此这里先合并有交集的区间，收益服务再选择真正覆盖其统计窗口的一段。
        """

        fixture_payload = self._load_corporate_action_fixture()
        fixture_entry = next(
            (
                item
                for item in fixture_payload.get("funds", [])
                if item.get("fund_code") == fund_code
            ),
            None,
        )
        actions_by_key: dict[tuple[str, str], dict[str, Any]] = {}
        intervals: list[dict[str, Any]] = []
        sources: list[str] = []

        if fixture_entry is not None:
            for action in fixture_entry.get("actions", []):
                normalized = {
                    **action,
                    "fund_code": fund_code,
                    "source": fixture_payload.get("source"),
                    "source_url": fixture_payload.get("source_url"),
                    "fetched_at": fixture_payload.get("generated_at"),
                }
                actions_by_key[
                    (normalized["action_date"], normalized["action_type"])
                ] = normalized
            intervals.append(
                {
                    "coverage_start": fixture_payload["coverage_start"],
                    "coverage_end": fixture_payload["coverage_end"],
                }
            )
            sources.append(str(fixture_payload.get("source") or "冻结样例"))

        if not frozen_eval_data_enabled():
            database_actions = self.action_repository.list_actions(
                fund_code=fund_code
            )
            database_coverage = self.action_repository.get_coverage(
                fund_code=fund_code
            )
            for action in database_actions:
                actions_by_key[(action["action_date"], action["action_type"])] = action
            if database_coverage is not None:
                intervals.append(
                    {
                        "coverage_start": database_coverage["coverage_start"],
                        "coverage_end": database_coverage["coverage_end"],
                    }
                )
                sources.append(str(database_coverage["source"]))

        merged_intervals = self._merge_coverage_intervals(intervals)
        return {
            "actions": sorted(
                actions_by_key.values(),
                key=lambda item: (item["action_date"], item["action_type"]),
            ),
            "coverage": {
                "coverage_start": (
                    merged_intervals[0]["coverage_start"]
                    if len(merged_intervals) == 1
                    else None
                ),
                "coverage_end": (
                    merged_intervals[0]["coverage_end"]
                    if len(merged_intervals) == 1
                    else None
                ),
                "intervals": merged_intervals,
                "source": " · ".join(dict.fromkeys(sources)) or None,
            },
        }

    def _load_corporate_action_fixture(self) -> dict[str, Any]:
        """读取离线公司行为样例；缺失时拒绝假装“没有分红”。"""

        path = self.fixture_dir / "fund_corporate_actions.json"
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            required = {"coverage_start", "coverage_end", "funds"}
            if not required.issubset(payload):
                raise ValueError("缺少公司行为覆盖字段")
            return payload
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise FundCatalogError("基金公司行为样本数据不完整") from exc

    @staticmethod
    def _merge_coverage_intervals(
        intervals: list[dict[str, Any]],
    ) -> list[dict[str, str]]:
        """只合并相交或首尾相接的覆盖段，保留真正的数据空档。"""

        normalized = sorted(
            (
                date.fromisoformat(str(item["coverage_start"])),
                date.fromisoformat(str(item["coverage_end"])),
            )
            for item in intervals
        )
        merged: list[list[date]] = []
        for start, end in normalized:
            if end < start:
                raise FundCatalogError("公司行为覆盖结束日早于开始日")
            if not merged or start > merged[-1][1]:
                merged.append([start, end])
                continue
            merged[-1][1] = max(merged[-1][1], end)
        return [
            {
                "coverage_start": start.isoformat(),
                "coverage_end": end.isoformat(),
            }
            for start, end in merged
        ]

    @staticmethod
    def _latest_updated_at(metadata: list[dict[str, Any]]) -> str | None:
        values = [item["data_updated_at"] for item in metadata if item["data_updated_at"]]
        return max(values) if values else None


fund_catalog_service = FundCatalogService()
