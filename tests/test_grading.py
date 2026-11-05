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
ANOMALY = {"kind": "anomaly", "aggregate": "additive", "entity_col": "job", "metric_col": "runs",
           "metric_label": "All-Purpose job runs this week", "unit": "count",
           "baseline": "median of the account's prior four weekly totals",
           "summary": "Account ran {account} jobs on All-Purpose compute this week, {x} its usual "
                      "(normal ~{baseline}).",
           "new_summary": "Account has no prior history for All-Purpose job runs yet ({account} this week).",
           "usd_col": None}
PRESENCE = {"kind": "presence", "entity_col": "wh", "metric_label": "Warehouses",
            "summary": "{n} warehouse(s) never stop, including {entity}.", "usd_col": None}
COLS = ["job", "runs", "share_pct", "account_value", "baseline", "vs_normal"]


def finding(spec, rows, cols=COLS):
    hunts.ALERT_SPECS["T1"] = spec
    try:
        return scan.build_finding(HUNT, {"columns": cols, "rows": rows, "total_rows": len(rows)})
    finally:
        del hunts.ALERT_SPECS["T1"]


def contributors(account, baseline, x, *parts):
    """Rows the way an anomaly hunt returns them: each contributor plus the same account columns."""
    return [[name, v, round(100 * v / account, 1), account, baseline, x] for name, v in parts]


class AnomalyGrading(unittest.TestCase):
    def test_high_at_three_times_the_account_normal(self):
        f = finding(ANOMALY, contributors(2340, 980, 2.39 * 1.4, ("nightly_etl", 725), ("adhoc", 300)))
        self.assertEqual(f["severity"], "high")
        self.assertEqual(f["summary"], "Account ran 2,340 jobs on All-Purpose compute this week, 3.3x its "
                                       "usual (normal ~980). Top contributor: nightly_etl (31%).")
        self.assertEqual(f["vs"], {"reading": "anomaly", "x": 2.39 * 1.4, "baseline": "980"})
        self.assertEqual((f["metric_value"], f["metric_num"]), ("2,340", 2340))

    def test_medium_between_two_and_three(self):
        f = finding(ANOMALY, contributors(2340, 980, 2.39, ("nightly_etl", 725)))
        self.assertEqual(f["severity"], "medium")
        self.assertIn("2.4x its usual", f["summary"])

    def test_small_share_keeps_a_decimal(self):
        f = finding(ANOMALY, contributors(1000, 100, 10.0, ("arnaud", 56)))
        self.assertTrue(f["summary"].endswith("Top contributor: arnaud (5.6%)."), f["summary"])
        self.assertIn("10x its usual", f["summary"])

    def test_account_within_normal_raises_no_alert_even_if_one_contributor_is_huge(self):
        self.assertIsNone(finding(ANOMALY, contributors(1000, 900, 1.11, ("one_big_job", 990))))

    def test_null_vs_normal_is_new_and_medium(self):
        f = finding(ANOMALY, contributors(40, None, None, ("fresh_job", 40)))
        self.assertEqual(f["severity"], "medium")
        self.assertEqual(f["summary"], "Account has no prior history for All-Purpose job runs yet "
                                       "(40 this week). Top contributor: fresh_job (100%).")
        self.assertEqual(f["vs"], {"reading": "new", "x": None, "baseline": None})

    def test_missing_contract_columns_raise_no_alert(self):
        self.assertIsNone(finding(ANOMALY, [["a", 51, 12, 4.25]], cols=["job", "runs", "baseline", "vs_normal"]))

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

    def test_every_anomaly_spec_names_its_aggregate(self):
        for hid, spec in hunts.ALERT_SPECS.items():
            if spec["kind"] == "anomaly":
                self.assertIn(spec.get("aggregate"), ("additive", "peak"), hid)


if __name__ == "__main__":
    unittest.main()
