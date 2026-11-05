"""Run-over-run trend, observed the way /api/scan/latest builds it: two saved runs read back
with DeltaStore.load_run, then store_delta.compare_runs. Run from the repo root:
    python3 -m unittest discover tests
"""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))

import hunts  # noqa: E402
import store_delta  # noqa: E402

ACCOUNT_SPILL = hunts.ALERT_SPECS["H2"]["metric_label"]
ACCOUNT_QUEUE = hunts.ALERT_SPECS["H8"]["metric_label"]


def saved_run(*findings):
    """A run as Delta stores it; each finding is (hunt_id, metric_label, metric_value, metric_num)."""
    rows = [[hid, "saved title", "reads", "high", "saved summary", label, value, None, 20, "M4", "saved action", "",
             json.dumps({"columns": [], "rows": []}), num] for hid, label, value, num in findings]
    run = ["scan-x", "2026-11-05 00:31:51", "2026-11-05 00:40:00", "danny", 16, len(rows), len(rows), 0, 2,
           1.0, 0.1, 0.0, 0.1]

    def run_sql(sql, client, params=None):
        return {"rows": [run] if "scan_runs" in sql else rows, "columns": []}
    return store_delta.DeltaStore("c", "s", run_sql, lambda *a, **k: None).load_run(None, "scan-x")


def trends(current, previous):
    return {f["hunt_id"]: (f["trend"], f["prev_metric_value"])
            for f in store_delta.compare_runs(current, previous)["findings"]}


class TrendAcrossRuns(unittest.TestCase):
    def test_a_changed_metric_definition_is_not_compared(self):
        # scan-04db45bdcbcc saved H2 as one statement's spill; the account's weekly spill replaced it.
        previous = saved_run(("H2", "Worst spill this week", "0.99 TiB", 0.9851587785260563))
        current = saved_run(("H2", ACCOUNT_SPILL, "976.98 TiB", 976.98))
        self.assertEqual(trends(current, previous), {"H2": ("changed", None)})

    def test_the_same_metric_definition_is_compared(self):
        previous = saved_run(("H2", ACCOUNT_SPILL, "361.51 TiB", 361.51))
        current = saved_run(("H2", ACCOUNT_SPILL, "976.98 TiB", 976.98))
        self.assertEqual(trends(current, previous), {"H2": ("worse", "361.51 TiB")})

    def test_a_finding_raised_under_an_old_definition_is_not_reported_cleared(self):
        previous = saved_run(("H2", "Worst spill this week", "0.99 TiB", 0.9851587785260563),
                             ("H8", ACCOUNT_QUEUE, "1,915.9 h", 1915.9))
        cleared = store_delta.compare_runs(saved_run(), previous)["resolved_since"]
        self.assertEqual([r["hunt_id"] for r in cleared], ["H8"])


if __name__ == "__main__":
    unittest.main()
