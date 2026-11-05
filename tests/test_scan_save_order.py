"""A finished scan is saved before its self-cost is computed, so the page shows the saved scan
without waiting on the billing queries behind the cost, which then attaches to the saved run.
Needs the app's requirements (FastAPI, Databricks SDK). Run from the repo root:
    python3 -m unittest discover tests
"""
import importlib.util
import os
import sys
import time
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))

HAS_APP_DEPS = all(importlib.util.find_spec(m) for m in ("fastapi", "databricks"))
COST = {"total_usd": 0.31, "dbsql_usd": 0.3, "ai_usd": 0.01}


@unittest.skipUnless(HAS_APP_DEPS, "needs the app's requirements")
class SaveBeforeCost(unittest.TestCase):
    def setUp(self):
        import app
        self.app, self.events, self.saved_while_pricing = app, [], "never priced"
        self.real = (app._run_sql, app._compute_scan_cost, app.DELTA)
        app._run_sql = lambda sql, client, max_rows=None, params=None: {"columns": [], "rows": [], "total_rows": 0}
        app._compute_scan_cost = self.compute_cost
        app.DELTA = SimpleNamespace(save_scan=self.save_scan, save_cost=self.save_cost, last_error=None)

    def tearDown(self):
        self.app._run_sql, self.app._compute_scan_cost, self.app.DELTA = self.real

    def save_scan(self, client, view, *cost):
        self.events.append("save")
        return True

    def save_cost(self, client, scan_id, cost):
        self.events.append("cost attached to the saved run")
        return True

    def compute_cost(self, view, client):
        self.events.append("cost")
        self.saved_while_pricing = self.app.scan_engine.scan_view(view["scan_id"])["saved"]
        return COST

    def finished(self):
        sid = self.app.scan_start(SimpleNamespace(headers={}), None)["scan_id"]
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            v = self.app.scan_engine.scan_view(sid)
            if v["saved"] is not None and (v.get("cost") or v.get("cost_error")):
                return v
            time.sleep(0.02)
        return self.app.scan_engine.scan_view(sid)

    def test_a_finished_scan_is_saved_before_its_self_cost_is_computed(self):
        v = self.finished()
        self.assertEqual(self.events, ["save", "cost", "cost attached to the saved run"])
        self.assertIs(self.saved_while_pricing, True)
        self.assertEqual((v["status"], v["saved"], v["cost"]), ("complete", True, COST))


if __name__ == "__main__":
    unittest.main()
