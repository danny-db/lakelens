"""
The "Tokenomics" tab content: how an AI task's cost is built.

The model rates are the published rate card (pricing.py, rate_card.json), and the $ per DBU is
this workspace's own list price for each model's SKU, read live (GET /api/token/prices). The
cost arithmetic runs in the browser, since an instant calculator is the point. This module
ships the rate card, the plain-English copy, the cache and gateway explainers, and the
illustrative task sizes. The task sizes and the cache and gateway tables come from an internal
token cost-estimator notebook.
"""
import pricing

# What each billing SKU family is, for the "which bill line" table.
FAMILY_LABEL = {
    "ANTHROPIC_MODEL_SERVING": "Claude (Anthropic)",
    "OPENAI_MODEL_SERVING": "GPT (OpenAI)",
    "GEMINI_MODEL_SERVING": "Gemini (Google)",
    "XAI_MODEL_SERVING": "Grok (xAI)",
    "MOONSHOT_MODEL_SERVING": "Kimi (Moonshot AI)",
    "SERVERLESS_REAL_TIME_INFERENCE": "Open models (Llama, GLM, DeepSeek, Qwen and others)",
}

# ── Example workload menu — ILLUSTRATIVE INDUSTRY NORMS, not measured real-world usage ─
TASKS = [
    {"name": "Classification / tagging",      "in": 800,   "out": 50,
     "note": "Short label from short text (e.g. ticket / transaction tagging)"},
    {"name": "Text classification (longer)",  "in": 3000,  "out": 150,
     "note": "Category from a longer document or transcript"},
    {"name": "Summarisation",                 "in": 5000,  "out": 600,
     "note": "Condense a conversation or document"},
    {"name": "Structured extraction",         "in": 4000,  "out": 800,
     "note": "Pull structured fields from unstructured text"},
    {"name": "Long-context analysis",         "in": 20000, "out": 2000,
     "note": "Reason over a long log, trace, or document set"},
]

# ── "If you read nothing else" — the exec takeaways (notebook top box) ────────
TAKEAWAYS = [
    "Cost = tokens × the model's DBU rate × $/DBU. DBUs are just the meter in between.",
    "Claude, GPT, Gemini, Grok and Kimi bill one $/DBU for your cloud and pricing tier. Open "
    "models hosted by Databricks bill your workspace region's rate. This page reads both from "
    "your workspace's own list prices.",
    "Output tokens cost several times more than input tokens, so capping response length is an "
    "easy saving.",
    "Model choice is the biggest lever. The comparison below shows the spread for your task.",
    "All figures are list price; your contracted rate may differ (use the discount control).",
]

# ── §8 caching table ─────────────────────────────────────────────────────────
CACHE_TABLE = [
    {"event": "Cache write (5-min retention)", "rate": "1.25× base input",
     "meaning": "First time a prefix is cached — a small premium"},
    {"event": "Cache write (1-hour retention)", "rate": "2.0× base input",
     "meaning": "Longer retention costs more to write"},
    {"event": "Cache read (a “hit”)", "rate": "0.10× base input",
     "meaning": "Reusing a cached prefix — a ~90% saving on those tokens"},
]
CACHE_CAVEATS = [
    "Prompt caching does NOT apply to ai_query() batch jobs — every row is billed at the full "
    "input rate. Set cache-hit to 0 for batch workloads.",
    "The calculator models cache READS (the saving), not the one-time write premium (negligible "
    "when a prefix is reused many times).",
    "The 0.10× / ~90% figure is Claude's cache-read ratio; each model applies its own rate from "
    "the rate card (GLM's is ~0.19× of its input rate, for example).",
]

# ── §9 gateway pricing ───────────────────────────────────────────────────────
GATEWAY_TABLE = [
    {"cap": "Routing / proxy", "cost": "Free", "kind": "free"},
    {"cap": "Permissions & access control", "cost": "Free", "kind": "free"},
    {"cap": "Rate limiting", "cost": "Free", "kind": "free"},
    {"cap": "Fallbacks", "cost": "Free", "kind": "free"},
    {"cap": "Prompt (prefix) caching", "cost": "Free", "kind": "free"},
    {"cap": "Guardrails (PII / safety)", "cost": "Not billed today", "kind": "free"},
    {"cap": "Usage tracking", "cost": "$0.10 / GB payload", "kind": "paid"},
    {"cap": "Inference tables (payload logging)", "cost": "$0.50 / GB payload", "kind": "paid"},
    {"cap": "External-model routing", "cost": "No Databricks fee", "kind": "free"},
]

def public_meta() -> dict:
    """Everything the Tokenomics tab needs to render and calculate in the browser, except the
    $ per DBU, which /api/token/prices reads for this workspace."""
    return {
        "card": pricing.card_meta(),
        "family_label": FAMILY_LABEL,
        "tasks": TASKS,
        "takeaways": TAKEAWAYS,
        "cache_table": CACHE_TABLE,
        "cache_caveats": CACHE_CAVEATS,
        "gateway_table": GATEWAY_TABLE,
    }
