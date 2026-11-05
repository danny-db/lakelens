"""GET /api/models lists the workspace's chat models as the user. A sign-in without the
model-serving scope is listed by the app's own identity, and when both fail only the configured
default is offered, with the reason. Needs the app's requirements (FastAPI, Databricks SDK). Run
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
LUNA = {"endpoints": [{"name": "databricks-gpt-6-luna", "task": "llm/v1/chat",
                       "config": {"served_entities": [{"foundation_model": {"display_name": "GPT-6 Luna"}}]}}]}


def client(do):
    return SimpleNamespace(api_client=SimpleNamespace(do=do))


@unittest.skipUnless(HAS_APP_DEPS, "needs the app's requirements")
class ModelListing(unittest.TestCase):
    def setUp(self):
        import app
        self.app, self.ambient, self.client_for = app, app.sp_client, app._client_for

    def tearDown(self):
        self.app.sp_client, self.app._client_for = self.ambient, self.client_for

    def signed_in(self, user_do, app_do):
        self.app._client_for = lambda request: client(user_do)
        self.app.sp_client = client(app_do)
        return self.app.list_models(NO_TOKEN)

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

    def test_a_sign_in_without_the_model_serving_scope_is_listed_by_the_app(self):
        from databricks.sdk.errors import PermissionDenied

        def user(method, path):
            raise PermissionDenied("Provided OAuth token does not have required scopes: model-serving")
        d = self.signed_in(user, lambda method, path: LUNA)
        self.assertEqual([m["id"] for m in d["models"]], ["databricks-gpt-6-luna"])
        self.assertEqual(d["source"], "app")
        self.assertIsNone(d["error"])

    def test_a_user_who_can_list_is_listed_as_themselves(self):
        def app_identity(method, path):
            raise AssertionError("the app's identity must not list for a user who can")
        d = self.signed_in(lambda method, path: LUNA, app_identity)
        self.assertEqual(d["source"], "you")
        self.assertEqual(d["models"][0]["name"], "GPT-6 Luna")

    def test_when_both_fail_the_default_is_offered_with_the_users_reason(self):
        from databricks.sdk.errors import DatabricksError, PermissionDenied

        def user(method, path):
            raise PermissionDenied("Provided OAuth token does not have required scopes: model-serving")

        def app_identity(method, path):
            raise DatabricksError("unable to parse response. Request log:```GET /api/2.0/serving-endpoints\n< 502 Bad Gateway```")
        d = self.signed_in(user, app_identity)
        self.assertEqual([m["id"] for m in d["models"]], [self.app.AI_ENDPOINT])
        self.assertIn("model-serving", d["error"])


if __name__ == "__main__":
    unittest.main()
