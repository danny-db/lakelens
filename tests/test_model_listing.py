"""GET /api/models lists the workspace's chat models, and when listing fails it offers only the
configured default with the reason. Needs the app's requirements (FastAPI, Databricks SDK). Run
from the repo root:
    python3 -m unittest discover tests
"""
import importlib.util
import os
import sys
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))

HAS_APP_DEPS = all(importlib.util.find_spec(m) for m in ("fastapi", "databricks"))
NO_TOKEN = SimpleNamespace(headers={})


@unittest.skipUnless(HAS_APP_DEPS, "needs the app's requirements")
class ModelListing(unittest.TestCase):
    def setUp(self):
        import app
        self.app, self.ambient = app, app.sp_client

    def tearDown(self):
        self.app.sp_client = self.ambient

    def listing(self, do):
        self.app.sp_client = SimpleNamespace(api_client=SimpleNamespace(do=do))
        return self.app.list_models(NO_TOKEN)

    def test_the_listing_carries_display_names_and_groups(self):
        def do(method, path):
            self.assertEqual((method, path), ("GET", "/api/2.0/serving-endpoints"))
            return {"endpoints": [{"name": "databricks-gpt-6-luna", "task": "llm/v1/chat",
                                   "config": {"served_entities": [{"foundation_model": {"display_name": "GPT-6 Luna"}}]}}]}
        d = self.listing(do)
        self.assertEqual(d["models"], [{"id": "databricks-gpt-6-luna", "name": "GPT-6 Luna", "group": "fast"}])
        self.assertEqual(d["groups"]["fast"], "Fast, for simple tasks")
        self.assertIsNone(d["error"])

    def test_a_failed_listing_offers_only_the_default_and_names_the_http_status(self):
        from databricks.sdk.errors import DatabricksError

        def do(method, path):
            raise DatabricksError("unable to parse response. This is likely a bug in the Databricks SDK. "
                                  "Request log:```GET /api/2.0/serving-endpoints\n< 502 Bad Gateway\n< <html>```")
        d = self.listing(do)
        self.assertEqual([m["id"] for m in d["models"]], [self.app.AI_ENDPOINT])
        self.assertEqual(d["default"], self.app.AI_ENDPOINT)
        self.assertIn("HTTP 502 Bad Gateway", d["error"])


if __name__ == "__main__":
    unittest.main()
