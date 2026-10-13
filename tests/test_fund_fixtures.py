"""FundScope 冻结数据的最小一致性测试。"""

from __future__ import annotations

import csv
import json
import unittest
from collections import Counter
from pathlib import Path


FIXTURE_DIR = Path(__file__).resolve().parents[1] / "data" / "fund_fixtures"
EXPECTED_FUND_CODES = {"110020", "161725", "110007"}


class FundFixtureTests(unittest.TestCase):
    def test_profiles_and_nav_cover_same_sample_funds(self) -> None:
        """基金档案和净值必须覆盖同一组样本，且无重复日期。"""
        profile_payload = json.loads(
            (FIXTURE_DIR / "fund_profiles.json").read_text(encoding="utf-8")
        )
        profile_codes = {
            item["fund_code"] for item in profile_payload.get("funds", [])
        }

        with (FIXTURE_DIR / "fund_nav.csv").open(
            encoding="utf-8", newline=""
        ) as nav_file:
            nav_rows = list(csv.DictReader(nav_file))

        nav_codes = {row["fund_code"] for row in nav_rows}
        pair_counts = Counter((row["fund_code"], row["nav_date"]) for row in nav_rows)

        self.assertEqual(profile_codes, EXPECTED_FUND_CODES)
        self.assertEqual(nav_codes, EXPECTED_FUND_CODES)
        self.assertTrue(all(count == 1 for count in pair_counts.values()))
        self.assertTrue(all(float(row["unit_nav"]) > 0 for row in nav_rows))


if __name__ == "__main__":
    unittest.main()
