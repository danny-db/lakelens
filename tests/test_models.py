"""The AI model picker lists chat models by display name, fast ones first, each family's newest
model first. Run from the repo root:
    python3 -m unittest discover tests
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))

import models  # noqa: E402


def endpoint(name, display, task="llm/v1/chat"):
    return {"name": name, "task": task,
            "config": {"served_entities": [{"foundation_model": {"display_name": display}}]}}


RAW = {"endpoints": [
    endpoint("databricks-claude-sonnet-5-5", "Claude Sonnet 5.5"),
    endpoint("databricks-claude-haiku-4-5", "Claude Haiku 4.5"),
    endpoint("databricks-gpt-5-6-luna", "GPT-5.6 Luna"),
    endpoint("databricks-gpt-oss-120b", "GPT OSS 120B"),
    endpoint("databricks-claude-haiku-5-5", "Claude Haiku 5.5"),
    endpoint("databricks-gemini-3-1-flash-lite", "Gemini 3.1 Flash Lite"),
    endpoint("databricks-gpt-6-luna", "GPT-6 Luna"),
    endpoint("databricks-deepseek-v4-flash-0731", "DeepSeek V4 Flash (0731)"),
    endpoint("databricks-deepseek-v4-1-flash", "DeepSeek V4.1 Flash"),
    endpoint("databricks-claude-sonnet-4-5", "Claude Sonnet 4.5"),
    endpoint("databricks-gte-large-en", "GTE Large (En)", task="llm/v1/embeddings"),
    endpoint("databricks-openjev-qwen35-4b", "OpenJev (Qwen 3.5 4B)", task="llm/v1/systemone"),
    endpoint("my-team-chat-bot", "Team bot"),
    {"name": "mas-39e61b2a-endpoint", "task": "agent/v1/responses"},
]}


class ModelPicker(unittest.TestCase):
    def setUp(self):
        self.listed = models.chat_models(RAW)
        self.names = [m.name for m in self.listed]

    def test_only_databricks_chat_endpoints_are_listed(self):
        self.assertNotIn("GTE Large (En)", self.names)
        self.assertNotIn("OpenJev (Qwen 3.5 4B)", self.names)
        self.assertNotIn("Team bot", self.names)
        self.assertEqual(len(self.listed), 10)

    def test_fast_models_come_first_and_the_latest_of_each_family_leads(self):
        self.assertEqual(self.names, [
            "Claude Haiku 5.5", "DeepSeek V4.1 Flash", "Gemini 3.1 Flash Lite", "GPT-6 Luna",
            "Claude Haiku 4.5", "DeepSeek V4 Flash (0731)", "GPT-5.6 Luna",
            "Claude Sonnet 5.5", "GPT OSS 120B", "Claude Sonnet 4.5"])

    def test_each_model_carries_its_endpoint_display_name_and_group(self):
        luna = next(m for m in self.listed if m.id == "databricks-gpt-6-luna")
        self.assertEqual((luna.name, luna.group), ("GPT-6 Luna", "fast"))
        sonnet = next(m for m in self.listed if m.id == "databricks-claude-sonnet-5-5")
        self.assertEqual(sonnet.group, "capable")
        self.assertEqual(models.GROUPS[sonnet.group], "More capable")

    def test_versions_compare_as_numbers_not_text(self):
        raw = {"endpoints": [endpoint("databricks-gpt-5-9-mini", "GPT-5.9 Mini"),
                             endpoint("databricks-gpt-5-10-mini", "GPT-5.10 Mini")]}
        self.assertEqual([m.name for m in models.chat_models(raw)], ["GPT-5.10 Mini", "GPT-5.9 Mini"])

    def test_a_newer_snapshot_of_the_same_version_leads(self):
        raw = {"endpoints": [endpoint("databricks-deepseek-v4-1-flash", "DeepSeek V4.1 Flash (0910)"),
                             endpoint("databricks-deepseek-v4-1-flash-1017", "DeepSeek V4.1 Flash (1017)")]}
        self.assertEqual([m.id for m in models.chat_models(raw)],
                         ["databricks-deepseek-v4-1-flash-1017", "databricks-deepseek-v4-1-flash"])

    def test_an_endpoint_without_a_display_name_shows_its_name(self):
        raw = {"endpoints": [{"name": "databricks-new-model", "task": "llm/v1/chat", "config": {}}]}
        self.assertEqual(models.chat_models(raw)[0].name, "databricks-new-model")

    def test_an_empty_or_partial_response_lists_nothing(self):
        self.assertEqual(models.chat_models({}), [])
        self.assertEqual(models.chat_models({"endpoints": None}), [])


if __name__ == "__main__":
    unittest.main()
