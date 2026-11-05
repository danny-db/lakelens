"""
tagging.py — the Tagging tab: find untagged resources (the foundation of cost
attribution), and — explicitly, on request — set a tag from the UI.

Per the FinOps deck's Step 1 (Attribute): you can't attribute or budget spend you
can't split, and you can't split it without tags. This tab surfaces the resources
that carry no tags (or are missing a key attribution tag like `domain`/`team`),
so the gap is visible and closeable.

IMPORTANT — read vs write. FINDING untagged resources is read-only (system tables).
SETTING a tag is a genuine WRITE to the workspace (the one place this app changes
anything), so it is a separate, explicit, confirmed action in app.py — never
automatic. The queries here are all read-only.

`custom_tags` on system.billing.usage is a MAP; `tags` on system.compute.* is a MAP.
An empty map (or missing the expected key) = a tagging gap.
"""
from boards import full_days


# Which tag keys "count" as attribution. A resource with none of these is untagged
# for cost purposes even if it has an incidental tag. Overridable per customer.
ATTRIBUTION_KEYS = ["domain", "team", "project", "cost_center", "env", "owner"]


# Untagged SQL WAREHOUSES — the biggest, most visible attribution gap.
UNTAGGED_WAREHOUSES = """
SELECT warehouse_id, warehouse_name, warehouse_type, warehouse_size,
       CASE WHEN tags IS NULL OR size(map_keys(tags)) = 0 THEN 'no tags'
            ELSE concat(size(map_keys(tags)), ' tag(s): ', array_join(map_keys(tags), ', ')) END AS current_tags
FROM system.compute.warehouses
QUALIFY ROW_NUMBER() OVER (PARTITION BY warehouse_id ORDER BY change_time DESC) = 1
   AND delete_time IS NULL
   AND (tags IS NULL OR size(map_keys(tags)) = 0)
ORDER BY warehouse_size DESC, warehouse_name
LIMIT 200
"""

# Untagged all-purpose / interactive CLUSTERS.
UNTAGGED_CLUSTERS = """
SELECT cluster_id, cluster_name, cluster_source, owned_by,
       CASE WHEN tags IS NULL OR size(map_keys(tags)) = 0 THEN 'no tags'
            ELSE concat(size(map_keys(tags)), ' tag(s)') END AS current_tags
FROM system.compute.clusters
QUALIFY ROW_NUMBER() OVER (PARTITION BY cluster_id ORDER BY change_time DESC) = 1
   AND delete_time IS NULL
   AND cluster_source IN ('UI', 'API')
   AND (tags IS NULL OR size(map_keys(tags)) = 0)
ORDER BY cluster_name
LIMIT 200
"""

# The COST of the gap: spend over the last 30 full days that carries no custom_tags, by product.
# This is the "how much of the bill can't be attributed?" headline.
UNTAGGED_SPEND_BY_PRODUCT = f"""
WITH {full_days(30)},
price AS (
  SELECT sku_name, cloud, price_start_time, price_end_time,
         pricing.effective_list.default AS unit_usd
  FROM system.billing.list_prices
)
SELECT u.billing_origin_product AS product,
       round(sum(CASE WHEN u.custom_tags IS NULL OR size(map_keys(u.custom_tags)) = 0
                      THEN u.usage_quantity * pr.unit_usd ELSE 0 END), 0)          AS untagged_usd,
       round(sum(u.usage_quantity * pr.unit_usd), 0)                              AS total_usd,
       round(sum(CASE WHEN u.custom_tags IS NULL OR size(map_keys(u.custom_tags)) = 0
                      THEN u.usage_quantity * pr.unit_usd ELSE 0 END)
             * 100.0 / NULLIF(sum(u.usage_quantity * pr.unit_usd), 0), 1)         AS pct_untagged
FROM system.billing.usage u
LEFT JOIN price pr ON u.sku_name = pr.sku_name AND u.cloud = pr.cloud
  AND u.usage_start_time >= pr.price_start_time
  AND (pr.price_end_time IS NULL OR u.usage_start_time < pr.price_end_time)
WHERE u.usage_date BETWEEN (SELECT start_date FROM win) AND (SELECT end_date FROM win)
GROUP BY 1
HAVING sum(u.usage_quantity * pr.unit_usd) > 0
ORDER BY untagged_usd DESC
"""

# Coverage headline: overall tagged vs untagged $ over the last 30 full days (the Overview's window).
TAG_COVERAGE = f"""
WITH {full_days(30)},
price AS (
  SELECT sku_name, cloud, price_start_time, price_end_time,
         pricing.effective_list.default AS unit_usd
  FROM system.billing.list_prices
)
SELECT round(sum(u.usage_quantity * pr.unit_usd), 0)                               AS total_usd,
       round(sum(CASE WHEN u.custom_tags IS NULL OR size(map_keys(u.custom_tags)) = 0
                      THEN u.usage_quantity * pr.unit_usd ELSE 0 END), 0)          AS untagged_usd
FROM system.billing.usage u
LEFT JOIN price pr ON u.sku_name = pr.sku_name AND u.cloud = pr.cloud
  AND u.usage_start_time >= pr.price_start_time
  AND (pr.price_end_time IS NULL OR u.usage_start_time < pr.price_end_time)
WHERE u.usage_date BETWEEN (SELECT start_date FROM win) AND (SELECT end_date FROM win)
"""


def public_meta() -> dict:
    return {"attribution_keys": ATTRIBUTION_KEYS}
