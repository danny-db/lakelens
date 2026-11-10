"""Spot calls a hunt a reference view exactly when the scan doesn't grade it, so its status line agrees
with its "judged" line. Needs the app's requirements (FastAPI, Databricks SDK). Run from the repo root:
    python3 -m unittest discover tests
"""
import importlib.util
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))

import hunts  # noqa: E402

HAS_APP_DEPS = all(importlib.util.find_spec(m) for m in ("fastapi", "databricks"))


@unittest.skipUnless(HAS_APP_DEPS, "needs the app's requirements")
class ReferenceViews(unittest.TestCase):
    def test_a_reference_view_is_a_hunt_the_scan_does_not_grade(self):
        import app
        cards = {c["id"]: c for c in app.get_meta()["hunts"]}
        self.assertEqual({i for i, c in cards.items() if c["inventory"]},
                         {h["id"] for h in hunts.HUNTS if h["id"] not in hunts.ALERT_SPECS})
        self.assertFalse(cards["H8"]["inventory"], "H8 raises alerts, so Spot must not call it a reference view")
        self.assertEqual(cards["H10"]["judged_short"], "Not graded")
        self.assertTrue(cards["H10"]["inventory"])


if __name__ == "__main__":
    unittest.main()
