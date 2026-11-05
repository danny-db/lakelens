"""Model token costs use the published DBU rate and the $ per DBU of the SKU the workspace bills,
for its own cloud, tier and region. Run from the repo root:
    python3 -m unittest discover tests
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))

import pricing  # noqa: E402

LUNA, GLM, GROK, KIMI = "databricks-gpt-6-luna", "databricks-glm-5-3", "databricks-grok-4-7", "databricks-kimi-k3"


def billed(cloud, sku, endpoint=None, day="2026-11-04", dbus=10.0):
    return ["billed", cloud, sku, endpoint, day, str(dbus)]


def listed(cloud, sku, usd):
    return ["list_price", cloud, sku, None, None, str(usd)]


def prices_list(cloud, tier, regions, vendors=("ANTHROPIC", "OPENAI", "GEMINI", "XAI", "MOONSHOT")):
    rows = [listed(cloud, f"{tier}_{v}_MODEL_SERVING", 0.07) for v in vendors]
    return rows + [listed(cloud, f"{tier}_SERVERLESS_REAL_TIME_INFERENCE_{r}", usd) for r, usd in regions.items()]


class WorkspacePrices(unittest.TestCase):
    def test_an_aws_workspace_prices_from_the_skus_it_bills(self):
        p = pricing.workspace_prices([
            billed("AWS", "ENTERPRISE_OPENAI_MODEL_SERVING", LUNA),
            billed("AWS", "ENTERPRISE_SERVERLESS_REAL_TIME_INFERENCE_US_EAST_N_VIRGINIA", GLM),
            billed("AWS", "ENTERPRISE_SERVERLESS_SQL_COMPUTE_US_EAST_N_VIRGINIA", dbus=500),
        ] + prices_list("AWS", "ENTERPRISE", {"US_EAST_N_VIRGINIA": 0.07, "AP_SYDNEY": 0.088}))
        self.assertEqual((p.cloud, p.tier, p.region), ("AWS", "ENTERPRISE", "US_EAST_N_VIRGINIA"))
        self.assertTrue(p.families["OPENAI_MODEL_SERVING"].billed)
        self.assertEqual(p.families["SERVERLESS_REAL_TIME_INFERENCE"].usd_per_dbu, 0.07)
        cost = pricing.model_cost(LUNA, p, 1e6, 1e6)
        self.assertEqual((cost["sku"], cost["usd_per_dbu"]), ("ENTERPRISE_OPENAI_MODEL_SERVING", 0.07))
        rate = pricing.RATES[LUNA]
        self.assertAlmostEqual(cost["usd"], (rate["dbu_in"] + rate["dbu_out"]) * 0.07, places=6)

    def test_a_family_not_billed_yet_is_named_from_the_workspace_tier(self):
        p = pricing.workspace_prices([billed("AWS", "ENTERPRISE_OPENAI_MODEL_SERVING", LUNA)]
                                     + prices_list("AWS", "ENTERPRISE", {"US_EAST_OHIO": 0.07}))
        grok = p.families["XAI_MODEL_SERVING"]
        self.assertEqual((grok.sku, grok.billed), ("ENTERPRISE_XAI_MODEL_SERVING", False))
        self.assertEqual(pricing.model_cost(GROK, p, 1e6, 0)["sku"], "ENTERPRISE_XAI_MODEL_SERVING")

    def test_open_models_take_the_workspace_region_rate(self):
        p = pricing.workspace_prices([
            billed("GCP", "ENTERPRISE_SERVERLESS_SQL_COMPUTE_ASIA_SINGAPORE", dbus=80),
            billed("GCP", "ENTERPRISE_JOBS_SERVERLESS_COMPUTE_ASIA_SINGAPORE", dbus=20),
        ] + prices_list("GCP", "ENTERPRISE", {"ASIA_SINGAPORE": 0.088, "US_OREGON": 0.07}))
        self.assertEqual((p.cloud, p.region), ("GCP", "ASIA_SINGAPORE"))
        glm = pricing.model_cost(GLM, p, 1e6, 0)
        self.assertEqual((glm["sku"], glm["usd_per_dbu"], glm["sku_billed_here"]),
                         ("ENTERPRISE_SERVERLESS_REAL_TIME_INFERENCE_ASIA_SINGAPORE", 0.088, False))
        self.assertAlmostEqual(glm["usd"], pricing.RATES[GLM]["dbu_in"] * 0.088, places=6)

    def test_an_sku_missing_from_the_cloud_price_list_leaves_its_models_unpriced(self):
        p = pricing.workspace_prices([billed("AZURE", "PREMIUM_SERVERLESS_SQL_COMPUTE_EUROPE_WEST", dbus=40)]
                                     + prices_list("AZURE", "PREMIUM", {"EUROPE_WEST": 0.091},
                                                   vendors=("ANTHROPIC", "OPENAI", "GEMINI")))
        self.assertEqual(p.missing["XAI_MODEL_SERVING"], "PREMIUM_XAI_MODEL_SERVING has no current list price on AZURE")
        grok = pricing.model_cost(GROK, p, 1e6, 1e6)
        self.assertIsNone(grok["usd"])
        self.assertIn("PREMIUM_XAI_MODEL_SERVING", grok["unpriced"])
        self.assertEqual(pricing.model_cost(GLM, p, 1e6, 0)["usd_per_dbu"], 0.091)

    def test_an_endpoint_takes_the_sku_it_billed_most_recently(self):
        p = pricing.workspace_prices([
            billed("AWS", "ENTERPRISE_SERVERLESS_REAL_TIME_INFERENCE_US_WEST_OREGON", KIMI, day="2026-08-27", dbus=900),
            billed("AWS", "ENTERPRISE_MOONSHOT_MODEL_SERVING", KIMI, day="2026-11-05", dbus=5),
        ] + prices_list("AWS", "ENTERPRISE", {"US_WEST_OREGON": 0.07}))
        self.assertEqual(pricing.model_cost(KIMI, p, 1e6, 0)["sku"], "ENTERPRISE_MOONSHOT_MODEL_SERVING")

    def test_prices_from_another_cloud_are_ignored(self):
        p = pricing.workspace_prices([billed("AWS", "ENTERPRISE_OPENAI_MODEL_SERVING", LUNA),
                                      listed("GCP", "ENTERPRISE_OPENAI_MODEL_SERVING", 0.5),
                                      listed("AWS", "ENTERPRISE_OPENAI_MODEL_SERVING", 0.07)])
        self.assertEqual(pricing.model_cost(LUNA, p, 1e6, 0)["usd_per_dbu"], 0.07)


class Unpriced(unittest.TestCase):
    def test_a_model_off_the_rate_card_is_unpriced_never_given_another_rate(self):
        p = pricing.workspace_prices([billed("AWS", "ENTERPRISE_OPENAI_MODEL_SERVING", LUNA)]
                                     + prices_list("AWS", "ENTERPRISE", {"US_EAST_OHIO": 0.07}))
        cost = pricing.model_cost("databricks-some-new-model", p, 1e6, 1e6)
        self.assertIsNone(cost["usd"])
        self.assertIn("no published pay-per-token rate", cost["unpriced"])

    def test_without_billing_rows_nothing_is_priced_and_the_reason_says_why(self):
        p = pricing.workspace_prices([])
        self.assertIsNone(p.cloud)
        cost = pricing.model_cost(LUNA, p, 1e6, 1e6)
        self.assertIsNone(cost["usd"])
        self.assertIn("system.billing.usage", cost["unpriced"])


class RateCard(unittest.TestCase):
    def test_the_card_cites_its_source_and_carries_the_fast_models(self):
        self.assertTrue(pricing.CARD["retrieved"])
        self.assertTrue(all(u.startswith("https://www.databricks.com/product/pricing/")
                            for u in pricing.CARD["sources"].values()))
        for model in ("databricks-gpt-6-luna", "databricks-gpt-5-6-luna", "databricks-claude-haiku-5-5"):
            self.assertIn(model, pricing.RATES)


if __name__ == "__main__":
    unittest.main()
