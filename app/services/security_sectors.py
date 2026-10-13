"""为基金季报重仓证券补充可审阅的细分行业标签。

基金季报行业配置通常只给出“制造业”这类大类，无法回答“白酒、半导体或电池到底
占多少”。第一版使用版本化 JSON 映射当前演示数据；未知证券保持未分类并进入覆盖率，
不会因为模型知道某家公司就临时猜一个行业。将来接入申万等正式数据源时，只需替换
这个 Service 的数据来源，事件归因算法和 API 不需要改写。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SECURITY_SECTOR_PATH = (
    PROJECT_ROOT / "data" / "fund_fixtures" / "security_sectors.json"
)


class SecuritySectorError(RuntimeError):
    """版本化行业词典缺失或格式错误。"""


class SecuritySectorService:
    """按证券代码读取细分行业，并聚合同一行业的组合暴露。"""

    def __init__(self, fixture_path: Path = DEFAULT_SECURITY_SECTOR_PATH) -> None:
        self.fixture_path = fixture_path
        self._payload: dict[str, Any] | None = None
        self._profiles: dict[str, dict[str, Any]] | None = None

    def metadata(self) -> dict[str, str]:
        """返回版本和口径，让前端知道细分行业不是盘中行情字段。"""

        payload = self._load()[0]
        return {
            "taxonomy_version": str(payload["taxonomy_version"]),
            "generated_at": str(payload["generated_at"]),
            "source": str(payload["source"]),
            "methodology": str(payload["methodology"]),
        }

    def get(self, *, security_code: str, security_name: str = "") -> dict[str, Any] | None:
        """返回一只证券的细分标签；名称只用于校验和对外说明，不参与模糊猜测。"""

        _, profiles = self._load()
        profile = profiles.get(str(security_code).strip())
        if profile is None:
            return None
        return {
            **profile,
            "security_name": profile.get("security_name") or security_name,
        }

    def vocabulary(self) -> list[str]:
        """返回行业名和别名词典，供基金名称与市场事件做透明关键词关联。"""

        _, profiles = self._load()
        terms = {
            str(term).strip()
            for profile in profiles.values()
            for term in [profile["sector_name"], *profile.get("aliases", [])]
            if str(term).strip()
        }
        # 长词优先只是为了让调试输出更具体，不改变“是否命中”的判断。
        return sorted(terms, key=lambda term: (-len(term), term))

    def keyword_groups(self) -> list[list[str]]:
        """按细分行业返回同义词组，例如“半导体、芯片、集成电路”。"""

        _, profiles = self._load()
        groups: list[list[str]] = []
        seen: set[tuple[str, ...]] = set()
        for profile in profiles.values():
            group = tuple(
                dict.fromkeys(
                    [profile["sector_name"], *profile.get("aliases", [])]
                )
            )
            if group and group not in seen:
                seen.add(group)
                groups.append(list(group))
        return groups

    def classify_exposures(
        self,
        security_exposures: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """把股票重仓按细分行业聚合，并显式计算已分类和未分类覆盖。"""

        sectors: dict[str, dict[str, Any]] = {}
        classified_exposure = 0.0
        unclassified_exposure = 0.0
        unclassified_codes: list[str] = []

        for item in security_exposures:
            if item.get("asset_type") != "stock":
                continue
            exposure = float(item.get("portfolio_exposure_pct") or 0)
            profile = self.get(
                security_code=str(item.get("security_code") or ""),
                security_name=str(item.get("security_name") or ""),
            )
            if profile is None:
                unclassified_exposure += exposure
                unclassified_codes.append(str(item.get("security_code") or ""))
                continue

            classified_exposure += exposure
            target = sectors.setdefault(
                profile["sector_name"],
                {
                    "sector_name": profile["sector_name"],
                    "portfolio_exposure_pct": 0.0,
                    "security_count": 0,
                    "security_codes": [],
                    "security_names": [],
                    "aliases": set(),
                },
            )
            target["portfolio_exposure_pct"] += exposure
            target["security_count"] += 1
            target["security_codes"].append(item["security_code"])
            target["security_names"].append(item["security_name"])
            target["aliases"].update(profile.get("aliases") or [])

        public_sectors = [
            {
                **item,
                "portfolio_exposure_pct": round(item["portfolio_exposure_pct"], 4),
                "aliases": sorted(item["aliases"]),
            }
            for item in sectors.values()
        ]
        public_sectors.sort(
            key=lambda item: (-item["portfolio_exposure_pct"], item["sector_name"])
        )
        return {
            "sector_exposures": public_sectors,
            "classified_stock_exposure_pct": round(classified_exposure, 4),
            "unclassified_stock_exposure_pct": round(unclassified_exposure, 4),
            "unclassified_security_codes": sorted(set(unclassified_codes)),
            **self.metadata(),
        }

    def _load(self) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
        """首次读取后缓存；文件是随代码发布的版本化静态资产。"""

        if self._payload is not None and self._profiles is not None:
            return self._payload, self._profiles
        try:
            payload = json.loads(self.fixture_path.read_text(encoding="utf-8"))
            required = {
                "taxonomy_version",
                "generated_at",
                "source",
                "methodology",
                "securities",
            }
            if not required.issubset(payload) or not isinstance(payload["securities"], list):
                raise ValueError("missing taxonomy fields")
            profiles: dict[str, dict[str, Any]] = {}
            for item in payload["securities"]:
                code = str(item["security_code"]).strip()
                sector_name = str(item["sector_name"]).strip()
                if not code or not sector_name or code in profiles:
                    raise ValueError(f"invalid or duplicate security: {code}")
                profiles[code] = {
                    "security_code": code,
                    "security_name": str(item.get("security_name") or "").strip(),
                    "sector_name": sector_name,
                    "aliases": list(
                        dict.fromkeys(
                            str(value).strip()
                            for value in item.get("aliases", [])
                            if str(value).strip()
                        )
                    ),
                }
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise SecuritySectorError("细分行业词典格式错误或不可读取") from exc

        self._payload = payload
        self._profiles = profiles
        return payload, profiles


security_sector_service = SecuritySectorService()
