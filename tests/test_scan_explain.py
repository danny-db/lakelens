"""A scan explains each finding from the rows it already fetched, so the warehouse runs every hunt
once. Needs the app's requirements (FastAPI, Databricks SDK). Run from the repo root:
    python3 -m unittest discover tests
"""
import importlib.util
import json
import os
import sys
import threading
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import hunts  # noqa: E402
from test_peers import COLS, SPEC, field  # noqa: E402

HAS_APP_DEPS = all(importlib.util.find_spec(m) for m in ("fastapi", "databricks"))
HUNT_SQL = "SELECT * FROM t1_hunt ORDER BY runs DESC LIMIT 50"
HUNT = {"id": "T1", "title": "Jobs on All-Purpose compute", "category": "jobs", "action": "fix it",
        "measure": "M2", "eli5": "test", "sql": HUNT_SQL,
        "explain_rubric": "rubric", "explain_hunt_desc": "jobs on All-Purpose compute"}
# A flat account with one job far above its 96 peers, in 12 rows so the explainer's top 10 is a cut.
ROWS = field(1000, 900, 1.11, 97, 1.0, 1.0, 4.0, ("nightly-loans", 400), *[(f"job{i}", 5) for i in range(11)])


@unittest.skipUnless(HAS_APP_DEPS, "needs the app's requirements")
class ScanRunsEachHuntOnce(unittest.TestCase):
    def test_the_explanation_reads_the_top_rows_the_scan_fetched(self):
        import app
        statements, captured, handed_over = [], {}, threading.Event()

        def run_sql(sql, client, max_rows=None, params=None):
            statements.append((sql, {p["name"]: p["value"] for p in params or []}))
            if "ai_query" in sql:
                return {"columns": ["explanation"], "rows": [["narrative"]], "total_rows": 1}
            return {"columns": COLS, "rows": ROWS, "total_rows": 97}

        def hand_over(*args, **kwargs):
            captured["args"] = args
            handed_over.set()

        run_scan = app.scan_engine.run_scan
        with mock.patch.object(app, "_run_sql", run_sql), mock.patch.object(app.scan_engine, "run_scan", hand_over), \
                mock.patch.object(hunts, "HUNTS", [HUNT]), mock.patch.dict(hunts.ALERT_SPECS, {"T1": SPEC}, clear=True):
            sid = app.scan_start(SimpleNamespace(headers={}), None)["scan_id"]
            try:
                self.assertTrue(handed_over.wait(5), "the route starts the scan")
                run_scan(*captured["args"])   # the route's own wiring, without the save and the self-cost
            finally:
                app.scan_engine.STORE.update_scan(sid, status="complete")
        finding = app.scan_engine.scan_view(sid)["findings"][0]

        self.assertEqual([s for s, _ in statements if "t1_hunt" in s], [HUNT_SQL], "the hunt runs once")
        explain = [p for s, p in statements if "ai_query" in s]
        self.assertEqual(json.loads(explain[0]["rows"]), [dict(zip(COLS, r)) for r in ROWS[:10]])
        self.assertEqual(finding["explanation"], "narrative")


if __name__ == "__main__":
    unittest.main()
