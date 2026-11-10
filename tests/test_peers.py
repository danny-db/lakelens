"""Outliers against peers, and the words that explain every flag. Run from the repo root:
    python3 -m unittest discover tests
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))

import hunts  # noqa: E402
import scan  # noqa: E402

HUNT = {"id": "T1", "title": "Jobs on All-Purpose compute", "category": "jobs", "action": "fix it",
        "measure": "M2", "eli5": "test"}
SPEC = {"kind": "anomaly", "aggregate": "additive", "entity_col": "job", "metric_col": "runs",
        "metric_label": "All-Purpose job runs this week", "unit": "count",
        "entity_noun": "job", "entity_nouns": "jobs", "metric_noun": "runs on All-Purpose compute",
        "baseline": "median of the account's prior four weekly totals",
        "summary": "Account ran {account} jobs on All-Purpose compute this week, {x} its usual (normal ~{baseline}).",
        "new_summary": "Account has no prior history for All-Purpose job runs yet ({account} this week).",
        "usd_col": None}
COLS = ["job", "runs", "share_pct", "account_value", "baseline", "vs_normal",
        "vs_peers", "peer_n", "peer_median", "peer_q1", "peer_q3"]
RULE = "the box-plot rule on a log scale, never below 3x the median"


def field(account, baseline, x, n, median, q1, q3, *parts):
    """Rows the way a wrapped anomaly hunt returns them: largest first, each with the account columns
    and the same peer spread."""
    return [[name, v, round(100 * v / account, 1), account, baseline, x, round(v / median, 1), n, median, q1, q3]
            for name, v in parts]


def finding(rows, spec=SPEC, cols=COLS, total=None):
    hunts.ALERT_SPECS["T1"] = spec
    try:
        return scan.build_finding(HUNT, {"columns": cols, "rows": rows, "total_rows": total or len(rows)})
    finally:
        del hunts.ALERT_SPECS["T1"]


def spot(rows, spec=SPEC, cols=COLS, total=None):
    hunts.ALERT_SPECS["T1"] = spec
    try:
        return scan.judge_result(HUNT, {"columns": cols, "rows": rows, "total_rows": total or len(rows)})
    finally:
        del hunts.ALERT_SPECS["T1"]


# 97 jobs, median 1 run, quartiles 1 and 4: the fence is 4 x (4 / 1) ^ 1.5 = 32, above the 3x floor.
FLAT_ACCOUNT_ONE_BIG_JOB = field(1000, 900, 1.11, 97, 1.0, 1.0, 4.0, ("nightly-loans", 400), ("b", 20), ("c", 10))


class StandingOutliers(unittest.TestCase):
    def test_a_flat_account_with_a_job_far_above_its_peers_is_a_medium_standing_outlier(self):
        f = finding(FLAT_ACCOUNT_ONE_BIG_JOB, total=97)
        self.assertEqual((f["severity"], f["lens"], f["vs"]["reading"]), ("medium", "peers", "outlier"))
        self.assertEqual(f["summary"], "nightly-loans stands far above the other 96 jobs this week: 400 runs on "
                                       "All-Purpose compute, 40% of the account's total, against a median of 1.")
        self.assertEqual(f["why"]["basis"],
                         "Judged against its peers this week. Against the account's own normal, the median of the "
                         "account's prior four weekly totals (900), this week is 1.1x that: below the 2x medium "
                         "cut-off, so the trend alone raises nothing.")
        self.assertEqual(f["why"]["peers"], f"1 of the 97 jobs is above the outlier fence of 32 ({RULE}).")

    def test_a_flat_field_uses_three_times_the_median_as_the_floor(self):
        # Quartiles equal the median, so the box-plot fence collapses to 84; the floor is 3 x 84 = 252.
        rows = field(10000, 9000, 1.0, 50, 84.0, 84.0, 84.0, ("big", 2520), ("mid", 200), ("small", 84))
        f = finding(rows, total=50)
        self.assertEqual(f["why"]["peers"], f"1 of the 50 jobs is above the outlier fence of 252 ({RULE}).")
        self.assertEqual(f["evidence"]["rows"][1][-1], "", "200 is under the floor, so not an outlier")

    def test_a_wide_spread_has_no_outlier_and_raises_nothing(self):
        # Quartiles 0.01 and 100: the fence is 100 x 10,000 ^ 1.5 = 100,000,000, far above any job.
        rows = field(60000, 60000, 1.0, 40, 1.0, 0.01, 100.0, ("big", 50000), ("b", 900))
        self.assertIsNone(finding(rows, total=40))
        _, _, why = spot(rows, total=40)
        self.assertEqual(why["peers"], f"No job stands apart from the other 39: none is above the outlier fence "
                                       f"of 100,000,000 ({RULE}).")

    def test_fewer_than_five_peers_never_makes_an_outlier(self):
        rows = field(500, 500, 1.0, 4, 1.0, 1.0, 1.0, ("big", 497), ("a", 1), ("b", 1), ("c", 1))
        self.assertIsNone(finding(rows))
        _, _, why = spot(rows)
        self.assertEqual(why["peers"], "Only 4 jobs this week, too few to compare with peers (it takes 5).")

    def test_a_spike_keeps_its_own_normal_lens_and_still_names_the_outlier(self):
        rows = field(1000, 300, 3.33, 97, 1.0, 1.0, 4.0, ("nightly-loans", 400), ("b", 20))
        f = finding(rows, total=97)
        self.assertEqual((f["severity"], f["lens"]), ("high", "normal"))
        self.assertEqual(f["why"]["basis"],
                         "Judged against the account's own normal, the median of the account's prior four weekly "
                         "totals (300). This week is 3.3x that: medium from 2x, high from 3x.")
        self.assertEqual(f["why"]["peers"],
                         "nightly-loans stands far above the other 96 jobs: 400 runs on All-Purpose compute, 40% of the "
                         f"account's total, against a median of 1. 1 of the 97 jobs is above the outlier fence of 32 ({RULE}).")

    def test_peak_hunts_do_not_claim_a_share_of_a_total(self):
        f = finding(FLAT_ACCOUNT_ONE_BIG_JOB, spec={**SPEC, "aggregate": "peak"}, total=97)
        self.assertEqual(f["summary"], "nightly-loans stands far above the other 96 jobs this week: 400 runs on "
                                       "All-Purpose compute, against a median of 1.")

    def test_more_outliers_than_returned_rows_says_at_least(self):
        rows = field(5000, 5000, 1.0, 97, 1.0, 1.0, 4.0, ("a", 400), ("b", 300))
        f = finding(rows, total=500)
        self.assertEqual(f["why"]["peers"], f"At least 2 of the 97 jobs are above the outlier fence of 32 ({RULE}).")

    def test_cut_offs_and_floor_follow_the_bundle_settings(self):
        with mock.patch.object(scan, "ANOMALY_HIGH_X", 4.0), mock.patch.object(scan, "ANOMALY_MEDIUM_X", 1.5):
            words = scan.judging(SPEC)
            self.assertIn("medium from 1.5x that, high from 4x", words)
            self.assertIn("never below 4x the median job", words)
            # A flat field at 84: the floor is 4 x 84 = 336, so 300 is no longer an outlier.
            rows = field(10000, 9000, 1.0, 50, 84.0, 84.0, 84.0, ("near", 300), ("small", 84))
            self.assertIsNone(finding(rows, total=50))

    def test_results_without_peer_stats_grade_as_before(self):
        old_cols = COLS[:6]
        rows = [r[:6] for r in FLAT_ACCOUNT_ONE_BIG_JOB]
        self.assertIsNone(finding(rows, cols=old_cols))

    def test_evidence_drops_the_spread_and_marks_the_outliers(self):
        f = finding(FLAT_ACCOUNT_ONE_BIG_JOB, total=97)
        self.assertEqual(f["evidence"]["columns"],
                         ["job", "runs", "share_pct", "account_value", "baseline", "vs_normal", "vs_peers", "peer_outlier"])
        self.assertEqual([r[-1] for r in f["evidence"]["rows"]], ["outlier", "", ""])


class SpotView(unittest.TestCase):
    def test_spot_shows_the_rows_marked_and_says_why(self):
        cols, rows, why = spot(FLAT_ACCOUNT_ONE_BIG_JOB, total=97)
        self.assertNotIn("peer_q1", cols)
        self.assertEqual([r[-1] for r in rows], ["outlier", "", ""])
        self.assertEqual((why["lens"], why["severity"]), ("peers", "medium"))
        self.assertTrue(why["peers"].startswith("nightly-loans stands far above the other 96 jobs: 400 runs"), why["peers"])

    def test_a_hunt_that_doesnt_alert_has_no_why(self):
        cols, rows, why = scan.judge_result({"id": "H10"}, {"columns": ["a"], "rows": [[1]], "total_rows": 1})
        self.assertIsNone(why)


class Words(unittest.TestCase):
    def test_how_each_kind_is_judged(self):
        self.assertEqual(scan.judging({"kind": "presence", "entity_noun": "warehouse"}),
                         "Judged by presence: every warehouse in this state raises a medium alert, whatever its history.")
        self.assertEqual(scan.judging(None), "Not graded: a reference view that raises no alert.")
        words = scan.judging(SPEC)
        for part in ("the median of the account's prior four weekly totals", "medium from 2x that, high from 3x",
                     "against its peers this week", "box-plot rule on a log scale"):
            self.assertIn(part, words)

    def test_the_article_fits_the_noun(self):
        self.assertIn("an app above the outlier fence", scan.judging({**SPEC, "entity_noun": "app"}))
        self.assertIn("a user above the outlier fence", scan.judging({**SPEC, "entity_noun": "user"}))

    def test_a_tiny_median_stays_readable(self):
        self.assertEqual(scan._fmt_small(3.4e-7, "tib"), "0.36 MiB")
        self.assertEqual(scan._fmt_small(0.12, "minutes"), "7 s")
        self.assertEqual(scan._fmt_small(0.3, "count"), "0.3")


class Catalogue(unittest.TestCase):
    def test_every_alert_spec_has_the_nouns_its_sentences_need(self):
        for hid, spec in hunts.ALERT_SPECS.items():
            needed = ("entity_noun", "entity_nouns") + (("metric_noun",) if spec["kind"] == "anomaly" else ())
            for key in needed:
                self.assertTrue(spec.get(key), f"{hid} has no {key}")

    def test_every_anomaly_hunt_returns_its_peer_spread(self):
        for h in hunts.HUNTS:
            spec = hunts.ALERT_SPECS.get(h["id"])
            if spec and spec["kind"] == "anomaly":
                for col in ("vs_peers",) + scan.PEER_COLS:
                    self.assertIn(f" AS {col}", h["sql"], f"{h['id']} misses {col}")


if __name__ == "__main__":
    unittest.main()
