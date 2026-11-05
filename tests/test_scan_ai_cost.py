"""A scan's AI self-cost uses the published rate of the model the scan used, at the $ per DBU of
that model's SKU here, and a model with no published rate is left unpriced, never given another
model's rate. Needs the app's requirements (FastAPI, Databricks SDK). Run from the repo root:
    python3 -m unittest discover tests
"""
import importlib.util
import os
import sys
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))

HAS_APP_DEPS = all(importlib.util.find_spec(m) for m in ("fastapi", "databricks"))
HAIKU, OPUS = "databricks-claude-haiku-5-5", "databricks-claude-opus-5-5"


def aws_prices(pricing):
    rows = [["billed", "AWS", "ENTERPRISE_ANTHROPIC_MODEL_SERVING", HAIKU, "2026-11-05", "3"],
            ["list_price", "AWS", "ENTERPRISE_ANTHROPIC_MODEL_SERVING", None, None, "0.07"]]
    return pricing.workspace_prices(rows)


@unittest.skipUnless(HAS_APP_DEPS, "needs the app's requirements")
class ScanAiCost(unittest.TestCase):
    def setUp(self):
        import app
        import pricing
        self.app, self.pricing, self.gateway_params = app, pricing, []
        self.saved = (app._run_sql, app._workspace_prices, app.WORKSPACE_ID)

        def run_sql(sql, client, max_rows=None, params=None):
            if "system.ai_gateway.usage" in sql:
                self.gateway_params.append({p["name"]: p["value"] for p in params})
                return {"rows": [["0", "0"]]}
            if "usd_per_hour" in sql:
                return {"rows": [["3.6"]]}
            return {"rows": [["0", None]]}
        app._run_sql, app._workspace_prices, app.WORKSPACE_ID = run_sql, lambda client: aws_prices(pricing), "ws-1"

    def tearDown(self):
        self.app._run_sql, self.app._workspace_prices, self.app.WORKSPACE_ID = self.saved

    def cost(self, model):
        return self.app._compute_scan_cost({"scan_id": "scan-t", "started_at": "2026-11-05 01:00:00",
                                            "ended_at": "2026-11-05 01:06:00", "wall_ms": 360000.0,
                                            "n_explains": 5, "model": model}, None)

    def expected(self, model):
        rate = self.pricing.RATES[model]
        return (5 * 1200 / 1e6 * rate["dbu_in"] + 5 * 120 / 1e6 * rate["dbu_out"]) * 0.07

    def test_the_ai_cost_uses_the_rate_of_the_model_the_scan_used(self):
        ai = self.cost(HAIKU)["ai_query"]
        self.assertEqual((ai["model"], ai["sku"], ai["basis"]), (HAIKU, "ENTERPRISE_ANTHROPIC_MODEL_SERVING", "estimate"))
        self.assertAlmostEqual(ai["usd"], self.expected(HAIKU), places=6)

    def test_two_models_with_different_rates_price_differently(self):
        self.assertAlmostEqual(self.cost(OPUS)["ai_query"]["usd"], self.expected(OPUS), places=6)
        self.assertGreater(self.cost(OPUS)["ai_query"]["usd"], self.cost(HAIKU)["ai_query"]["usd"])

    def test_a_model_without_a_published_rate_is_left_unpriced(self):
        cost = self.cost("databricks-team-finetune")
        self.assertIsNone(cost["ai_query"]["usd"])
        self.assertIn("no published pay-per-token rate", cost["ai_query"]["unpriced"])
        self.assertEqual(cost["total_usd"], cost["dbsql"]["usd"])

    def test_measured_tokens_are_read_for_that_model_on_this_workspace(self):
        self.cost(OPUS)
        self.assertEqual((self.gateway_params[-1]["model"], self.gateway_params[-1]["ws"]), (OPUS, "ws-1"))


@unittest.skipUnless(HAS_APP_DEPS, "needs the app's requirements")
class ScanRecordsItsModel(unittest.TestCase):
    def setUp(self):
        import app
        self.app, self.run_scan = app, app.scan_engine.run_scan
        app.scan_engine.run_scan = lambda *a, **k: None

    def tearDown(self):
        self.app.scan_engine.run_scan = self.run_scan

    def started_model(self, model):
        sid = self.app.scan_start(SimpleNamespace(headers={}), model)["scan_id"]
        try:
            return self.app.scan_engine.scan_view(sid)["model"]
        finally:
            self.app.scan_engine.STORE.update_scan(sid, status="complete")

    def test_a_scan_records_the_model_its_explanations_use(self):
        self.assertEqual(self.started_model(HAIKU), HAIKU)
        self.assertEqual(self.started_model(None), self.app.AI_ENDPOINT)
        self.assertEqual(self.started_model("not-an-endpoint'); DROP"), self.app.AI_ENDPOINT)


if __name__ == "__main__":
    unittest.main()
