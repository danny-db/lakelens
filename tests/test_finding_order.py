"""The alert queue must sequence identical findings identically on every path:
the live poll (scan_view, insertion order) and the saved scan (scan_latest, whatever
order the store returns). Today both sort by (severity, -impact_usd) only, so the
many findings that tie (no $ impact) keep their input order — and the queue
reshuffles when the finished scan swaps to the saved copy. Run from the repo root:
    python3 -m unittest discover tests
"""
import os
import random
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))

import scan  # noqa: E402


def mk(hunt_id, severity="medium", impact=None, metric=100.0):
    return {"hunt_id": hunt_id, "severity": severity, "impact_usd": impact,
            "metric_num": metric, "title": hunt_id, "summary": "", "metric_label": "",
            "metric_value": "", "total_rows": 1, "evidence": {"columns": [], "rows": []}}


class FindingOrder(unittest.TestCase):
    def test_order_is_identical_regardless_of_input_order(self):
        """Live insertion order and an arbitrary storage order must render the same queue."""
        live_order = [mk(f"H{i}") for i in (1, 2, 3, 5, 6, 7, 8, 16, 18, 19, 20, 21, 23)]
        storage_order = live_order[:]
        random.Random(7).shuffle(storage_order)
        live = sorted(live_order, key=scan.finding_sort_key)
        saved = sorted(storage_order, key=scan.finding_sort_key)
        self.assertEqual([f["hunt_id"] for f in live], [f["hunt_id"] for f in saved])

    def test_worst_first_severity_dollars_metric_then_id(self):
        findings = [
            mk("H9", metric=50.0),                            # medium, no $, smaller metric
            mk("H2", impact=900.0),                           # medium, most $
            mk("H5", impact=900.0),                           # medium, same $, id breaks the tie
            mk("H1", severity="high", metric=1.0),            # high beats every medium
            mk("H7", metric=5000.0),                          # medium, no $, biggest metric
        ]
        ordered = [f["hunt_id"] for f in sorted(findings, key=scan.finding_sort_key)]
        self.assertEqual(ordered, ["H1", "H2", "H5", "H7", "H9"])

    def test_scan_view_applies_the_shared_rule(self):
        sid, created = scan.STORE.start_scan({"status": "running", "total": 3, "done": 0,
                                              "clean": 0, "findings": []})
        self.assertTrue(created)
        try:
            for hid in ("H3", "H1", "H2"):
                scan.STORE.append_finding(sid, mk(hid))
            view = scan.scan_view(sid)
            self.assertEqual([f["hunt_id"] for f in view["findings"]], ["H1", "H2", "H3"])
        finally:
            # a "running" scan would make later start_scan calls idempotently reuse this id
            scan.STORE.update_scan(sid, status="complete")


if __name__ == "__main__":
    unittest.main()
