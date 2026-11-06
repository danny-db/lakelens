"""Board anomaly detectors, observed through boards.compute_anomalies. Run from the repo root:
    python3 -m unittest discover tests

The detectors must read an account of any size the same way, so most tests run one shape
at a small and a large scale and expect the same callouts.
"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "app"))

import boards  # noqa: E402
import scan  # noqa: E402

SMALL, LARGE = 1.0, 1000.0   # a $5K-a-week account and a $5M-a-week one
WEEKS = [f"2026-09-{d:02d}" for d in (1, 8, 15, 22, 29)]


def callouts(spec, cols, rows):
    return boards.compute_anomalies({"anomaly": spec}, cols, rows)


def severities(spec, cols, rows):
    return [a["severity"] for a in callouts(spec, cols, rows)]


def weekly(values, scale):
    return [[w, v * scale] for w, v in zip(WEEKS, values)]


def sliced(groups, scale):
    return [[w, g, v * scale] for g, vals in groups.items() for w, v in zip(WEEKS, vals)]


TOTAL = {"type": "ts_spike", "time_col": "week", "value_col": "usd", "unit": "usd"}
SLICES = {"type": "ts_spike", "time_col": "week", "group_col": "product", "value_col": "usd", "unit": "usd"}


class TsSpike(unittest.TestCase):
    def test_grades_by_the_app_wide_multiples(self):
        cols = ["week", "usd"]
        for scale in (SMALL, LARGE):
            self.assertEqual(severities(TOTAL, cols, weekly([5000] * 4 + [5000 * scan.ANOMALY_HIGH_X], scale)), ["high"])
            self.assertEqual(severities(TOTAL, cols, weekly([5000] * 4 + [5000 * scan.ANOMALY_MEDIUM_X], scale)), ["medium"])
            self.assertEqual(severities(TOTAL, cols, weekly([5000] * 4 + [5000 * 1.5], scale)), [])

    def test_same_shape_same_callout_at_any_scale(self):
        shape = [4800, 5100, 4950, 5050, 16000]
        small = callouts(TOTAL, ["week", "usd"], weekly(shape, SMALL))
        large = callouts(TOTAL, ["week", "usd"], weekly(shape, LARGE))
        self.assertEqual([a["severity"] for a in small], [a["severity"] for a in large])
        self.assertIn("3.2× its normal", small[0]["headline"])
        self.assertIn("3.2× its normal", large[0]["headline"])

    def test_tiny_slice_tripling_is_floored_at_the_typical_slice(self):
        groups = {"SQL": [3000] * 5, "JOBS": [1500] * 5, "APPS": [800] * 5, "NETWORKING": [5, 5, 5, 5, 15]}
        for scale in (SMALL, LARGE):
            self.assertEqual(callouts(SLICES, ["week", "product", "usd"], sliced(groups, scale)), [])

    def test_typical_slice_tripling_fires_at_any_scale(self):
        groups = {"SQL": [3000] * 5, "JOBS": [1500] * 5, "APPS": [1000] * 4 + [3000], "DLT": [600] * 5,
                  "NETWORKING": [5] * 5}
        for scale in (SMALL, LARGE):
            out = callouts(SLICES, ["week", "product", "usd"], sliced(groups, scale))
            self.assertEqual([a["severity"] for a in out], ["high"])
            self.assertTrue(out[0]["headline"].startswith("“APPS”"))

    def test_small_slice_is_judged_against_the_floor_and_says_so(self):
        groups = {"SQL": [3000] * 5, "JOBS": [1500] * 5, "APPS": [800] * 4 + [2600], "NETWORKING": [5] * 5}
        out = callouts(SLICES, ["week", "product", "usd"], sliced(groups, SMALL))
        self.assertEqual([a["severity"] for a in out], ["medium"])
        self.assertIn("2.3× its normal", out[0]["headline"])
        self.assertIn("floored at the typical product's week ($1,150)", out[0]["detail"])

    def test_independent_metrics_are_not_floored_by_each_other(self):
        spec = {"type": "ts_spike", "time_col": "week", "unit": "usd",
                "value_cols": ["cost per query", "cost per weekly active user"]}
        rows = [[w, q, u] for w, q, u in zip(WEEKS, [0.03, 0.03, 0.031, 0.029, 0.07], [75, 76, 74, 75, 77])]
        out = callouts(spec, ["week", "cost per query", "cost per weekly active user"], rows)
        self.assertEqual([a["severity"] for a in out], ["medium"])
        self.assertIn("“cost per query”", out[0]["headline"])
        self.assertIn("$0.0700", out[0]["headline"])

    def test_needs_three_prior_periods(self):
        self.assertEqual(callouts(TOTAL, ["week", "usd"], [[WEEKS[0], 10], [WEEKS[1], 10], [WEEKS[2], 90]]), [])


class RankOutlier(unittest.TestCase):
    SPEC = {"type": "rank_outlier", "value_col": "usd", "label_col": "workspace", "unit": "usd"}

    def test_long_tail_with_no_single_leader_is_quiet(self):
        for scale in (SMALL, LARGE):
            rows = [[f"ws{i}", v * scale] for i, v in enumerate([3358, 1920, 1681, 1653, 99, 50, 10, 1])]
            self.assertEqual(callouts(self.SPEC, ["workspace", "usd"], rows), [])

    def test_one_item_several_times_the_runner_up_fires(self):
        for scale in (SMALL, LARGE):
            rows = [[f"ws{i}", v * scale] for i, v in enumerate([307, 81, 50, 30])]
            out = callouts(self.SPEC, ["workspace", "usd"], rows)
            self.assertEqual([a["severity"] for a in out], ["high"])
            self.assertIn("3.8× the next-largest", out[0]["headline"])


class VsTypical(unittest.TestCase):
    SPEC = boards.find_panel("jobs", "cpu_efficiency")[1]["anomaly"]
    COLS = ["job_name", "avg_cpu_pct", "typical_cpu_pct", "times_below_typical", "node_minutes"]

    def test_counts_rows_at_the_medium_multiple_and_grades_the_worst(self):
        rows = [["a", 1.0, 25, 25.0, 900], ["b", 12.0, 25, 2.1, 900], ["c", 20.0, 25, 1.3, 900]]
        out = callouts(self.SPEC, self.COLS, rows)
        self.assertEqual([a["severity"] for a in out], ["high"])
        self.assertTrue(out[0]["headline"].startswith("2 of these jobs"))
        self.assertIn("“a” at 1.0%", out[0]["detail"])

    def test_nothing_unusual_is_quiet(self):
        self.assertEqual(callouts(self.SPEC, self.COLS, [["a", 20.0, 25, 1.3, 900]]), [])


class DecompDriver(unittest.TestCase):
    COLS = ["delta_usd", "new_workloads_usd", "retired_workloads_usd", "volume_growth_usd",
            "rate_mix_usd", "typical_change_usd"]

    def row(self, delta, typical, scale):
        return [[delta * scale, delta * scale, 0, 0, 0, typical * scale]]

    def test_change_graded_against_the_typical_weekly_change(self):
        spec = {"type": "decomp_driver"}
        for scale in (SMALL, LARGE):
            self.assertEqual(severities(spec, self.COLS, self.row(1000, 400, scale)), ["medium"])
            self.assertEqual(severities(spec, self.COLS, self.row(1300, 400, scale)), ["high"])
            self.assertEqual(severities(spec, self.COLS, self.row(700, 400, scale)), [])


class UnitEconomicsTable(unittest.TestCase):
    COLS = ["metric", "kind", "value", "coverage", "definition", "has_account_prices"]

    def result(self, priced):
        flag = "true" if priced else "false"
        return [[k.metric, k.kind, "v", "c", k.definition, flag] for k in boards.ECON_KPIS]

    def shown(self, priced):
        _, panel = boards.find_panel("econ", "kpis")
        return boards.present(panel, self.COLS, self.result(priced))

    def test_the_four_unit_costs_are_tiles_and_the_rest_is_the_table(self):
        cols, rows, extra = self.shown(priced=False)
        self.assertEqual([t["metric"] for t in extra["tiles"]],
                         ["Cost per query", "Cost per MAU, all identities", "Cost per MAU, humans",
                          "Cost per TiB of data under management, per month"])
        self.assertEqual(cols, ["metric", "value", "coverage", "definition"])
        self.assertNotIn("Cost per query", [r[0] for r in rows])
        self.assertTrue(all(t["definition"] for t in extra["tiles"]))

    def test_without_account_prices_figures_are_list_price_and_the_discount_row_goes(self):
        _, rows, extra = self.shown(priced=False)
        self.assertEqual(extra["price_basis"], "list price")
        self.assertNotIn("Effective discount", [r[0] for r in rows])

    def test_with_account_prices_the_effective_discount_stays(self):
        _, rows, extra = self.shown(priced=True)
        self.assertIn("Effective discount", [r[0] for r in rows])
        self.assertEqual(extra["price_basis"], "account price where set, list otherwise")

    def test_a_one_week_storage_series_says_why_it_is_a_dot(self):
        _, panel = boards.find_panel("econ", "trend")
        cols = ["week", "cost per query", "cost per TiB managed, per month", "cost per weekly active user"]
        rows = [["2026-10-28", "0.03", None, "68.7"], ["2026-11-04", "0.04", "5975.52", "67.3"]]
        self.assertIn("one week so far", boards.present(panel, cols, rows)[2]["notice"])
        self.assertIn("can't read", boards.present(panel, cols, rows, storage=False)[2]["notice"])
        rows[0][2] = "6000.00"
        self.assertEqual(boards.present(panel, cols, rows)[2], {})

    def test_other_panels_pass_through(self):
        self.assertEqual(boards.present({}, ["week", "usd"], [["w", "1"]]), (["week", "usd"], [["w", "1"]], {}))


class Catalogue(unittest.TestCase):
    def test_generated_skill_matches_the_board_sql(self):
        sys.path.insert(0, os.path.join(ROOT, "tools"))
        import gen_unit_economics_skill as gen
        with open(gen.OUT) as f:
            self.assertEqual(f.read(), gen.render(), "run tools/gen_unit_economics_skill.py")

    def test_no_detector_spec_carries_a_number(self):
        for b in boards.BOARDS:
            for p in b["panels"]:
                for k, v in (p.get("anomaly") or {}).items():
                    self.assertNotIsInstance(v, (int, float), f"{b['id']}/{p['id']} detector.{k}")


if __name__ == "__main__":
    unittest.main()
