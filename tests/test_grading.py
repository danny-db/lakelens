"""Alert grading, observed through scan.build_finding. Run from the repo root:
    python3 -m unittest discover tests
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))

import hunts  # noqa: E402
import scan  # noqa: E402

HUNT = {"id": "T1", "title": "Test hunt", "category": "spend", "action": "fix it",
        "measure": "M2", "eli5": "test"}
ANOMALY = {"kind": "anomaly", "entity_col": "job", "metric_col": "runs", "metric_label": "Runs this week",
           "unit": "count", "baseline": "median of prior weeks",
           "summary": "{entity}: {x} its usual runs (normal ~{baseline}/week)",
           "new_summary": "NEW: {entity} first seen this window ({metric} runs)", "usd_col": None}
PRESENCE = {"kind": "presence", "entity_col": "wh", "metric_label": "Warehouses",
            "summary": "{n} warehouse(s) never stop, including {entity}.", "usd_col": None}
COLS = ["job", "runs", "baseline", "vs_normal"]


def finding(spec, rows, cols=COLS):
    hunts.ALERT_SPECS["T1"] = spec
    try:
        return scan.build_finding(HUNT, {"columns": cols, "rows": rows, "total_rows": len(rows)})
    finally:
        del hunts.ALERT_SPECS["T1"]


class AnomalyGrading(unittest.TestCase):
    def test_high_at_three_times_normal(self):
        f = finding(ANOMALY, [["nightly_etl", 51, 12, 4.25]])
        self.assertEqual(f["severity"], "high")
        self.assertEqual(f["summary"], "nightly_etl: 4.2x its usual runs (normal ~12/week)")
        self.assertEqual(f["vs"], {"reading": "anomaly", "x": 4.25, "baseline": "12"})

    def test_medium_between_two_and_three(self):
        self.assertEqual(finding(ANOMALY, [["a", 25, 10, 2.5]])["severity"], "medium")

    def test_below_medium_raises_no_alert(self):
        self.assertIsNone(finding(ANOMALY, [["a", 15, 10, 1.5], ["b", 9, 10, 0.9]]))

    def test_null_vs_normal_is_new_and_medium(self):
        f = finding(ANOMALY, [["fresh_job", 40, None, None]])
        self.assertEqual(f["severity"], "medium")
        self.assertEqual(f["summary"], "NEW: fresh_job first seen this window (40 runs)")
        self.assertEqual(f["vs"]["reading"], "new")

    def test_high_row_outranks_new_row_listed_first(self):
        f = finding(ANOMALY, [["fresh_job", 40, None, None], ["spiky", 90, 10, 9.0]])
        self.assertEqual(f["severity"], "high")
        self.assertTrue(f["summary"].startswith("spiky: 9.0x"))

    def test_first_of_equal_severity_rows_wins(self):
        f = finding(ANOMALY, [["first", 25, 10, 2.5], ["second", 28, 10, 2.8]])
        self.assertTrue(f["summary"].startswith("first:"))

    def test_missing_contract_columns_raise_no_alert(self):
        self.assertIsNone(finding(ANOMALY, [["a", 51]], cols=["job", "runs"]))

    def test_no_rows_raise_no_alert(self):
        self.assertIsNone(finding(ANOMALY, []))


class PresenceGrading(unittest.TestCase):
    def test_any_row_is_medium_without_a_magnitude(self):
        f = finding(PRESENCE, [["cube_serving"], ["bi_wh"]], cols=["wh"])
        self.assertEqual(f["severity"], "medium")
        self.assertEqual(f["summary"], "2 warehouse(s) never stop, including cube_serving.")
        self.assertEqual(f["vs"]["reading"], "presence")
        self.assertIsNone(f["metric_num"])

    def test_no_rows_raise_no_alert(self):
        self.assertIsNone(finding(PRESENCE, [], cols=["wh"]))


class SpecCatalogue(unittest.TestCase):
    def test_every_alert_spec_has_a_grader(self):
        for hid, spec in hunts.ALERT_SPECS.items():
            self.assertIn(spec["kind"], scan.GRADERS, hid)


if __name__ == "__main__":
    unittest.main()
