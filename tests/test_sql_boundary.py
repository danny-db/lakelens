"""The SQL boundary: a system-table read that races a file change runs once more, and a panel
that reads system.storage runs without it where the account can't read that table. Needs the
app's requirements (FastAPI, Databricks SDK). Run from the repo root:
    python3 -m unittest discover tests
"""
import importlib.util
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))

HAS_APP_DEPS = all(importlib.util.find_spec(m) for m in ("fastapi", "databricks"))
RACED = ("SQL FAILED: [FAILED_READ_FILE.FILE_NOT_EXIST] Error while reading file delta-sharing-log:/uc-deltasharing"
         "%3A%2F%2Fsystem.query.history/_delta_log/00000000000000000000.json. File does not exist.")
NO_STORAGE = "SQL FAILED: [TABLE_OR_VIEW_NOT_FOUND] The table or view `system`.`storage`.`table_metrics_history` cannot be found."
ROWS = {"columns": ["n"], "rows": [["1"]], "total_rows": 1}


@unittest.skipUnless(HAS_APP_DEPS, "needs the app's requirements")
class SqlBoundary(unittest.TestCase):
    def setUp(self):
        import app
        self.app, self.saved = app, (app._execute_sql, app._run_sql, app.WAREHOUSE_ID)
        app.WAREHOUSE_ID = "wh"

    def tearDown(self):
        self.app._execute_sql, self.app._run_sql, self.app.WAREHOUSE_ID = self.saved

    def executing(self, *outcomes):
        from fastapi import HTTPException
        calls = []

        def execute(sql, client, max_rows, params):
            calls.append(sql)
            outcome = outcomes[len(calls) - 1]
            if isinstance(outcome, str):
                raise HTTPException(500, outcome)
            return outcome
        self.app._execute_sql = execute
        return calls

    def test_a_read_that_races_a_file_change_runs_once_more(self):
        calls = self.executing(RACED, ROWS)
        self.assertEqual(self.app._run_sql("SELECT 1", None), ROWS)
        self.assertEqual(len(calls), 2)

    def test_any_other_failure_is_not_retried(self):
        from fastapi import HTTPException
        calls = self.executing(NO_STORAGE)
        with self.assertRaises(HTTPException):
            self.app._run_sql("SELECT 1", None)
        self.assertEqual(len(calls), 1)

    def test_a_panel_runs_without_storage_when_the_account_cannot_read_it(self):
        calls = self.executing(NO_STORAGE, ROWS)
        panel = {"sql": "with storage", "without_storage": "without storage"}
        self.assertEqual(self.app._panel_rows(panel, None, "econ/kpis"), (ROWS, False))
        self.assertEqual(calls, ["with storage", "without storage"])

    def test_a_panel_without_a_storage_variant_reports_the_failure(self):
        from fastapi import HTTPException
        self.executing(NO_STORAGE)
        with self.assertRaises(HTTPException):
            self.app._panel_rows({"sql": "with storage"}, None, "usage/trend")


if __name__ == "__main__":
    unittest.main()
