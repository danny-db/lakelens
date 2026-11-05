"""A finished scan says whether it was saved, which the Alerts tab waits for before it shows the
finished state. Run from the repo root:
    python3 -m unittest discover tests
"""
import logging
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))

import scan  # noqa: E402


def finished_scan(on_complete):
    sid, _ = scan.STORE.start_scan({"status": "starting", "total": 0, "done": 0, "clean": 0,
                                    "findings": [], "started_by": ""})
    scan.run_scan(sid, None, lambda sql, client: {"columns": [], "rows": [], "total_rows": 0},
                  lambda *a: None, lambda sql, n: sql, logging.getLogger("test"), on_complete=on_complete)
    return scan.scan_view(sid)


def fails(sid, client):
    raise RuntimeError("warehouse unreachable")


class ScanSaveState(unittest.TestCase):
    def test_without_a_results_catalog_the_scan_finishes_unsaved_with_the_reason(self):
        v = finished_scan(lambda sid, client: "RESULTS_CATALOG not set")
        self.assertEqual((v["status"], v["saved"], v["save_error"]), ("complete", False, "RESULTS_CATALOG not set"))

    def test_a_saved_scan_finishes_saved(self):
        v = finished_scan(lambda sid, client: None)
        self.assertEqual((v["status"], v["saved"], v["save_error"]), ("complete", True, None))

    def test_a_save_that_raises_still_finishes_unsaved(self):
        v = finished_scan(fails)
        self.assertEqual((v["status"], v["saved"], v["save_error"]), ("complete", False, "warehouse unreachable"))


if __name__ == "__main__":
    unittest.main()
