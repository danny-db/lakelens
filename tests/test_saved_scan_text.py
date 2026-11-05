"""Saved scans read back with the current catalogue's hunt text. Run from the repo root:
    python3 -m unittest discover tests
"""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))

import hunts  # noqa: E402
import store_delta  # noqa: E402

H7 = next(h for h in hunts.HUNTS if h["id"] == "H7")
RUN = ["scan-1", "2026-11-03 11:43:36", "2026-11-03 11:50:00", "danny", 16, 2, 1, 1, 14, 40.0, 0.1, 0.0, 0.1]


def stored(hunt_id, title):
    return [hunt_id, title, "old-category", "medium", "saved summary", "Short SELECTs this week", "1,234",
            None, 3, "M9", "old action", "", json.dumps({"columns": [], "rows": []}), 1234.0]


def store_with(findings):
    def run_sql(sql, client, params=None):
        return {"rows": [RUN] if "FROM " + "c.s.scan_runs" in sql else findings, "columns": []}
    return store_delta.DeltaStore("c", "s", run_sql, lambda *a, **k: None)


class SavedScanText(unittest.TestCase):
    def test_existing_hunt_takes_current_title_and_fix(self):
        run = store_with([stored("H7", "Polling every 20 seconds")]).load_run(None, "scan-1")
        f = run["findings"][0]
        self.assertEqual((f["title"], f["action"], f["measure"], f["category"], f["eli5"]),
                         (H7["title"], H7["action"], H7["measure"], H7["category"], H7["eli5"]))

    def test_measured_text_stays_as_saved(self):
        f = store_with([stored("H7", "Polling every 20 seconds")]).load_run(None, "scan-1")["findings"][0]
        self.assertEqual((f["summary"], f["metric_label"], f["metric_value"]),
                         ("saved summary", "Short SELECTs this week", "1,234"))

    def test_retired_hunt_keeps_its_stored_title(self):
        f = store_with([stored("H99", "A hunt that was removed")]).load_run(None, "scan-1")["findings"][0]
        self.assertEqual((f["title"], f["action"]), ("A hunt that was removed", "old action"))

    def test_cleared_since_last_scan_uses_current_titles(self):
        previous = store_with([stored("H7", "Polling every 20 seconds")]).load_run(None, "scan-0")
        current = store_delta.compare_runs({"findings": []}, previous)
        self.assertEqual(current["resolved_since"][0]["title"], H7["title"])


if __name__ == "__main__":
    unittest.main()
