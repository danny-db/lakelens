"""
tokenomics.py — the "Tokenomics" tab content: how an AI task's cost is built.

Ported VERBATIM (rates + presets + explainers) from an internal token cost-estimator
notebook  That notebook is the source of truth for
the numbers; this module mirrors its §3 rate card and §8/§9 explainers so the app
answers "what will an AI task cost, and why?" — the estimate side, complementing
the other tabs which measure ACTUAL spend from system tables.

The cost arithmetic itself runs client-side in the browser (it's simple, and an
instant calculator is the point). This module just ships the reference data +
plain-English copy. The backend adds two live SQL touches: verify $/DBU against
system.billing.list_prices, and show actual 30-day model-serving spend.

Numbers are GCP Enterprise, LIST price, as of the date below. Re-confirm on the
public pricing page before quoting — they move.
"""

PRICING_DATE = "2026-07-29"

# ── $/DBU: converts DBUs to dollars (GCP Enterprise) ─────────────────────────
#   proprietary (Claude/OpenAI/Gemini) = FLAT GLOBAL $0.070 (does not vary by region)
#   open models via SRTI = REGIONALISED, follows the WORKSPACE region → SG $0.088
USD_PER_DBU = {
    "fmapi_proprietary": 0.070,
    "fmapi_open_sg": 0.088,
}

# Human labels for each billing basis (shown in the UI).
BASIS_LABEL = {
    "fmapi_proprietary": "FMAPI · proprietary",
    "fmapi_open_sg": "FMAPI · open (SRTI)",
    "external": "External (vendor $)",
}

# ── Per-model DBU per 1M tokens (input / output / cache-read) ─────────────────
# Verbatim from the notebook §3 rate card. "basis" drives how the calculator
# bills it. External models carry usd_* (vendor $/1M) instead of dbu_*.
MODELS = [
    {"name": "Claude Haiku 4.5",  "family": "Claude", "basis": "fmapi_proprietary",
     "dbu_in": 14.286, "dbu_out": 71.429,  "dbu_cache_read": 1.429},
    {"name": "Claude Sonnet 4.5", "family": "Claude", "basis": "fmapi_proprietary",
     "dbu_in": 42.857, "dbu_out": 214.286, "dbu_cache_read": 4.286},
    {"name": "Claude Opus 4.8",   "family": "Claude", "basis": "fmapi_proprietary",
     "dbu_in": 71.429, "dbu_out": 357.143, "dbu_cache_read": 7.143},
    {"name": "OpenAI GPT-5.4",    "family": "OpenAI", "basis": "fmapi_proprietary",
     "dbu_in": 35.714, "dbu_out": 214.286, "dbu_cache_read": 3.571},
    {"name": "Gemini 3.5 Flash",  "family": "Gemini", "basis": "fmapi_proprietary",
     "dbu_in": 26.786, "dbu_out": 160.714, "dbu_cache_read": 2.679},
    # GLM: US-served (a Singapore call is cross-geo) but bills at the WORKSPACE
    # region SRTI rate = SG $0.088. A data-residency consideration, not just price.
    {"name": "GLM 5.2",           "family": "GLM",    "basis": "fmapi_open_sg",
     "dbu_in": 20.000, "dbu_out": 62.857,  "dbu_cache_read": 3.714,
     "note": "US-served (cross-geo) · bills at SG SRTI $0.088/DBU"},
    # External example — billed by the vendor in $ directly (no DBU). Placeholder
    # rates; replace with a provider's published $/1M price.
    {"name": "External model (example)", "family": "External", "basis": "external",
     "usd_in": 2.50, "usd_out": 10.00, "usd_cache_read": 0.25,
     "note": "external vendor billing (not FMAPI · discounts don't apply)"},
]

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
    "On a Singapore workspace, Claude / OpenAI / Gemini bill at a flat $0.070/DBU (a global "
    "rate). Open models via SRTI bill at $0.088/DBU in Singapore.",
    "Output tokens cost ~5× input — controlling response length is an easy cost win.",
    "Model choice is the biggest lever: ~5× between the cheapest and priciest Claude for the "
    "identical task.",
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

# ── SKU map (notebook §10) — links the estimate to the ‘Spot’-tab actuals ─────
SKU_MAP = [
    {"family": "Claude", "sku": "ANTHROPIC_MODEL_SERVING", "usd_dbu": "$0.070 (flat global)"},
    {"family": "OpenAI", "sku": "OPENAI_MODEL_SERVING", "usd_dbu": "$0.070 (flat global)"},
    {"family": "Gemini", "sku": "GEMINI_MODEL_SERVING", "usd_dbu": "$0.070 (flat global)"},
    {"family": "Open models (GLM, Kimi…)", "sku": "SERVERLESS_REAL_TIME_INFERENCE",
     "usd_dbu": "$0.088 SG (regionalised: US $0.070 · SG/Sydney $0.088 · Tokyo $0.090)"},
]


def public_meta() -> dict:
    """Everything the Tokenomics tab needs to render + calculate client-side."""
    return {
        "pricing_date": PRICING_DATE,
        "usd_per_dbu": USD_PER_DBU,
        "basis_label": BASIS_LABEL,
        "models": MODELS,
        "tasks": TASKS,
        "takeaways": TAKEAWAYS,
        "cache_table": CACHE_TABLE,
        "cache_caveats": CACHE_CAVEATS,
        "gateway_table": GATEWAY_TABLE,
        "sku_map": SKU_MAP,
    }
