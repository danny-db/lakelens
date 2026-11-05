"""
What a model's tokens cost on this workspace.

A model's DBU per 1M tokens is Databricks' published pay-per-token rate (rate_card.json, which
tools/gen_rate_card.py generates from the public pricing pages). The $ per DBU depends on the
cloud, the pricing tier and, for open models, the region, so it comes from this workspace's own
billing: the SKU each model bills here, priced from system.billing.list_prices.
"""
import json
import os
from typing import NamedTuple, Optional

with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "rate_card.json"), encoding="utf-8") as f:
    CARD = json.load(f)
RATES = {m["id"]: m for m in CARD["models"]}
SRTI = "SERVERLESS_REAL_TIME_INFERENCE"
FAMILIES = sorted({m["sku_family"] for m in CARD["models"]})
TIERS = ("ENTERPRISE", "PREMIUM", "STANDARD")

# This workspace's DBU usage for 30 days (the SKUs it bills, and per model-serving endpoint), and
# the current list price of every model-serving SKU on its cloud. system.billing.usage holds every
# workspace in the account, so the workspace filter is what makes the SKUs this workspace's own.
PRICES_SQL = """
WITH mine AS (
  SELECT cloud, sku_name,
         CASE WHEN billing_origin_product = 'MODEL_SERVING' THEN usage_metadata.endpoint_name END AS endpoint,
         max(usage_date) AS last_day, sum(usage_quantity) AS dbus
  FROM system.billing.usage
  WHERE workspace_id = :workspace_id AND usage_unit = 'DBU' AND usage_date >= current_date() - INTERVAL 30 DAYS
  GROUP BY ALL
)
SELECT 'billed' AS kind, cloud, sku_name, endpoint, CAST(last_day AS STRING) AS last_day, dbus AS amount FROM mine
UNION ALL
SELECT 'list_price', cloud, sku_name, NULL, NULL, pricing.effective_list.default
FROM system.billing.list_prices
WHERE cloud IN (SELECT cloud FROM mine) AND usage_unit = 'DBU'
  AND (sku_name LIKE '%MODEL_SERVING%' OR sku_name LIKE '%SERVERLESS_REAL_TIME_INFERENCE%')
  AND price_start_time <= current_timestamp()
  AND (price_end_time IS NULL OR price_end_time > current_timestamp())
"""


class SkuPrice(NamedTuple):
    sku: str            # e.g. ENTERPRISE_OPENAI_MODEL_SERVING
    usd_per_dbu: float
    billed: bool        # this workspace billed it in the window; else named from its tier and region


class Prices(NamedTuple):
    cloud: Optional[str]
    tier: Optional[str]
    region: Optional[str]
    families: dict      # SKU family -> SkuPrice
    endpoints: dict     # endpoint billed here -> SkuPrice (wins over its family)
    missing: dict       # SKU family -> why it has no price here


def _tier(sku: str) -> Optional[str]:
    return next((t for t in TIERS if sku.startswith(t + "_")), None)


def _family(sku: str) -> Optional[str]:
    rest = sku[len(_tier(sku)) + 1:] if _tier(sku) else sku
    if rest in FAMILIES:
        return rest
    return SRTI if rest.startswith(SRTI + "_") else None


def workspace_prices(rows: list) -> Prices:
    """Rows of PRICES_SQL -> what each SKU family and billed endpoint costs per DBU here.
    A family this workspace hasn't billed is named from its tier and region, then priced if
    that SKU is on the cloud's price list."""
    billed = [(r[1], r[2], r[3], r[4] or "", float(r[5] or 0)) for r in rows if r[0] == "billed"]
    if not billed:
        why = "no DBU usage for this workspace in the last 30 days, or no access to system.billing.usage"
        return Prices(None, None, None, {}, {}, {f: why for f in FAMILIES})
    cloud = max(billed, key=lambda b: b[4])[0]
    listed = {r[2]: float(r[5]) for r in rows if r[0] == "list_price" and r[1] == cloud and r[5] is not None}

    def latest(items):
        """The SKU billed most recently, then most: a model's SKU can change (Kimi K3 moved off SRTI)."""
        return max(items, key=lambda b: (b[3], b[4]))[1] if items else None

    endpoints = {}
    for ep in {b[2] for b in billed if b[2]}:
        sku = latest([b for b in billed if b[2] == ep and _family(b[1]) and b[1] in listed])
        if sku:
            endpoints[ep] = SkuPrice(sku, listed[sku], True)
    serving = [b for b in billed if _family(b[1])]
    tier = _tier(latest(serving) or latest([b for b in billed if _tier(b[1])]) or "")
    weight = {}
    for s in listed:
        if SRTI + "_" in s:
            r = s.split(SRTI + "_", 1)[1]
            weight[r] = sum(b[4] for b in billed if b[1].endswith("_" + r))
    region = max((r for r, w in weight.items() if w > 0), key=lambda r: weight[r], default=None)

    families, missing = {}, {}
    for f in FAMILIES:
        sku = latest([b for b in serving if _family(b[1]) == f and b[1] in listed])
        if sku:
            families[f] = SkuPrice(sku, listed[sku], True)
            continue
        if not tier:
            missing[f] = "this workspace's pricing tier isn't in its billing"
            continue
        if f == SRTI and not region:
            missing[f] = "this workspace's region isn't in its billing"
            continue
        sku = f"{tier}_{f}_{region}" if f == SRTI else f"{tier}_{f}"
        if sku in listed:
            families[f] = SkuPrice(sku, listed[sku], False)
        else:
            missing[f] = f"{sku} has no current list price on {cloud}"
    return Prices(cloud, tier, region, families, endpoints, missing)


def model_cost(model_id: str, prices: Prices, in_tokens: float, out_tokens: float) -> dict:
    """List-price $ for tokens on one model here, or the reason it can't be priced. Never falls
    back to another model's rate."""
    rate = RATES.get(model_id)
    if not rate:
        return {"model": model_id, "usd": None,
                "unpriced": f"{model_id} has no published pay-per-token rate on the rate card"}
    sku = prices.endpoints.get(model_id) or prices.families.get(rate["sku_family"])
    if not sku:
        return {"model": model_id, "model_name": rate["name"], "usd": None,
                "unpriced": prices.missing.get(rate["sku_family"], "no $ per DBU for its SKU")}
    dbus = in_tokens / 1e6 * rate["dbu_in"] + out_tokens / 1e6 * rate["dbu_out"]
    return {"model": model_id, "model_name": rate["name"], "usd": round(dbus * sku.usd_per_dbu, 6),
            "dbu_in": rate["dbu_in"], "dbu_out": rate["dbu_out"], "sku": sku.sku,
            "usd_per_dbu": sku.usd_per_dbu, "sku_billed_here": sku.billed}


def as_json(prices: Prices) -> dict:
    return {"cloud": prices.cloud, "tier": prices.tier, "region": prices.region,
            "families": {f: p._asdict() for f, p in prices.families.items()},
            "endpoints": {e: p._asdict() for e, p in prices.endpoints.items()},
            "missing": prices.missing}


def card_meta() -> dict:
    """The rate card as the Tokenomics tab reads it: source, retrieval date, notes, rows."""
    return {k: CARD[k] for k in ("retrieved", "sources", "basis", "notes", "models")}
