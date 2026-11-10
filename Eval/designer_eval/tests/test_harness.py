"""Smoke tests for the Designer evaluation harness.

Run from the repository root:

    python3 -m unittest discover -s test_validation/designer_eval/tests -v
"""

from __future__ import annotations

import json
import sys
import unittest
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))

from Eval.designer_eval.common import (  # noqa: E402
    contract_checks,
    normalize_path,
    read_csv,
    ret_has_oracle,
)
from Eval.designer_eval.analyze_designer_eval import enrich  # noqa: E402


class DatasetTests(unittest.TestCase):
    def test_diagnostic_balance(self) -> None:
        rows = read_csv(HERE / "queries" / "designer_queries.csv")
        counts = Counter(row["gold_category"] for row in rows)
        self.assertEqual(len(rows), 45)
        self.assertEqual(counts["OUT_OF_DOMAIN"], 15)
        self.assertEqual(counts["AMBIGUOUS"], 15)
        self.assertEqual(counts["INVALID"], 15)

    def test_smart_city_size(self) -> None:
        rows = read_csv(HERE / "queries" / "smart_city_100.csv")
        self.assertEqual(len(rows), 100)
        self.assertTrue(all(row["query"] for row in rows))


class CommonTests(unittest.TestCase):
    def test_normalize_path(self) -> None:
        self.assertEqual(
            normalize_path("http://host/bin/1?status=ok"), "/bin/1"
        )
        self.assertEqual(
            normalize_path("/albums/{id}/tracks"), "/albums/{}/tracks"
        )
        self.assertEqual(normalize_path("/bin/"), "/bin")

    def test_contract_checks(self) -> None:
        contract = {
            "service_id": "smart-pharmacy-mock",
            "suggested_endpoints": [
                {
                    "method": "GET",
                    "path": "/pharmacies",
                    "response_schema": "{id:str}",
                }
            ],
        }
        checks = contract_checks(contract, {"smart-x-mock"}, {"GET /bin"})
        self.assertTrue(checks["contract_hard_valid"])
        self.assertEqual(checks["contract_endpoint_count"], 1)
        self.assertFalse(checks["reuse_exact_any"])

        duplicate = contract_checks(
            contract, {"smart-pharmacy-mock"}, {"GET /bin"}
        )
        self.assertFalse(duplicate["contract_hard_valid"])
        self.assertTrue(duplicate["duplicate_service_id"])

    def test_ret_has_oracle(self) -> None:
        discovered = [{"capabilities": ["GET /bin", "POST /bin"]}]
        self.assertTrue(ret_has_oracle(discovered, "GET /bin"))
        self.assertFalse(ret_has_oracle(discovered, "GET /parking"))
        self.assertIsNone(ret_has_oracle(discovered, ""))


class AnalysisTests(unittest.TestCase):
    def test_reached_designer_string_zero_is_false(self) -> None:
        row = {
            "set": "diagnostic",
            "query_id": "D001",
            "gold_category": "OUT_OF_DOMAIN",
            "predicted_category": "",
            "reached_designer": "0",
            "planner_tasks_count": "2",
            "validated_tasks_count": "2",
            "tasks_after_count": "2",
            "contract_json": "",
            "discovered_json": "[]",
            "network_error": "",
            "ret_has_gold": "",
        }
        enriched = enrich([row], set(), set())
        self.assertEqual(enriched[0]["reached_designer"], 0)
        self.assertEqual(enriched[0]["tasks_before"], 2)

    def test_reached_designer_string_one_is_true(self) -> None:
        row = {
            "set": "diagnostic",
            "query_id": "D002",
            "gold_category": "AMBIGUOUS",
            "predicted_category": "AMBIGUOUS",
            "reached_designer": "1",
            "planner_tasks_count": "0",
            "validated_tasks_count": "0",
            "tasks_after_count": "0",
            "contract_json": "",
            "discovered_json": "[]",
            "network_error": "",
            "ret_has_gold": "",
        }
        enriched = enrich([row], set(), set())
        self.assertEqual(enriched[0]["reached_designer"], 1)
        self.assertEqual(enriched[0]["predicted_for_matrix"], "AMBIGUOUS")


if __name__ == "__main__":
    unittest.main()
