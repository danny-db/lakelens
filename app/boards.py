"""
boards.py — the Observability section: four "boards" (Usage, Jobs, DBSQL,
Model Serving), each a set of panels. A panel is one query rendered as a
chart or table.

The point of this section (per the brief) is NOT to draw pretty graphs — it
is to SPOT THE ANOMALY. So every panel can carry an `anomaly` spec, and the
backend runs a deterministic detector over the panel's own rows and returns
plain-English callouts that render ABOVE the chart. The rule is the same one
the Alerts engine uses: code detects (deterministic, explainable, no
hallucinated numbers); the AI only ever narrates what the rule found.

All SQL is grounded in columns probed live on the workspace (see the schema-gotchas
memory): clusters uses dbr_version / worker_count / min|max_autoscale_workers
(NOT spark_version/num_workers); node_timeline uses mem_used_percent and
cpu_user_percent+cpu_system_percent; result_state ∈ ERROR/FAILED/TIMED_OUT/
CANCELLED; model-serving type comes from product_features.serving_type +
model_serving.offering_type + usage_type.

Same verbatim-when-possible rule as hunts.py. Where a public source existed
(model-serving = tiantan32 dashboard; DBSQL = the cost-per-query PrPr; usage/
forecast = Account Usage Dashboard v2) the core logic is transcribed and only
de-parameterised (the dashboards are parameter-bound; the app is not).
"""
from typing import Optional

# ── Anomaly detectors ─────────────────────────────────────────────────────────
# Each is pure Python over the panel's returned rows. They return a list of
# callout dicts: {severity, headline, detail}. No detector invents a number the
# query didn't produce — it only compares values already in the result.

def _f(x) -> Optional[float]:
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _median(xs):
    s = sorted(xs)
    n = len(s)
    if not n:
        return None
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2.0


def _detect(spec: dict, cols: list, rows: list) -> list:
    """Dispatch to the right detector. Returns [] on any shape mismatch."""
    if not spec or not rows:
        return []
    ci = {c: i for i, c in enumerate(cols)}
    kind = spec.get("type")
    try:
        if kind == "ts_spike":
            return _ts_spike(spec, ci, rows)
        if kind == "rank_outlier":
            return _rank_outlier(spec, ci, rows)
        if kind == "new_entity":
            return _new_entity(spec, ci, rows)
        if kind == "threshold":
            return _threshold(spec, ci, rows)
        if kind == "decomp_driver":
            return _decomp_driver(spec, ci, rows)
    except Exception:  # a detector must never break a panel
        return []
    return []


def _decomp_driver(spec, ci, rows):
    """Read the ΔCost decomposition (one row) and lead with the DOMINANT driver of the
    week-over-week change — so the callout says WHY, not just how much."""
    r = rows[0]
    def g(c): return _f(r[ci[c]]) if c in ci else None
    delta = g("delta_usd")
    if delta is None:
        return []
    comps = [("new workloads", g("new_workloads_usd")),
             ("retired workloads", g("retired_workloads_usd")),
             ("volume growth (existing workloads doing more)", g("volume_growth_usd")),
             ("rate / mix (pricier compute or SKU mix)", g("rate_mix_usd"))]
    comps = [(n, v) for n, v in comps if v is not None]
    if not comps or abs(delta) < 1000:   # a trivial weekly change isn't worth a callout
        return []
    # dominant driver = the component with the largest ABSOLUTE contribution
    name, val = max(comps, key=lambda c: abs(c[1]))
    up = delta >= 0
    sev = "high" if abs(delta) >= 250000 else "medium"
    share = (abs(val) / abs(delta) * 100) if delta else 0
    breakdown = ", ".join(f"{n.split(' (')[0]} {_fmt(v, 'usd')}" for n, v in comps)
    return [{
        "severity": sev,
        "headline": f"Cost {'rose' if up else 'fell'} {_fmt(abs(delta), 'usd')} week-over-week — "
                    f"mostly {name} ({_fmt(val, 'usd')}, {share:.0f}% of the change)",
        "detail": f"Breakdown: {breakdown}. These four sum to the total change exactly. "
                  f"{'A rise driven by NEW workloads is expected growth — attribute and budget it, not cut. ' if up and 'new' in name else ''}"
                  f"{'A rise from RATE/MIX means the same work got pricier — check compute type / SKU. ' if up and 'rate' in name else ''}"
                  f"{'A rise from VOLUME means existing workloads are doing more — check for runaway growth. ' if up and 'volume' in name else ''}",
    }]


def _ts_spike(spec, ci, rows):
    """Latest period vs the mean+std of prior periods, per group. Flags a group
    whose most recent value spikes above its own history.

    ACCURACY over noise (Danny's #1 rule): a big % on a tiny base is not a cost
    problem. So a spike must clear BOTH a statistical bar (z >= k) AND a material
    dollar bar:
      - min_abs      : the group's latest value must be at least this ($ floor to
                       even consider it) — keeps sub-$k products out entirely.
      - min_delta    : the increase (last - mean) must be at least this to fire.
      - min_delta_high: 'high' severity requires the $ delta to also clear this;
                       otherwise it's at most 'medium' no matter how large the %.
    Callouts are ranked by ABSOLUTE $ DELTA (biggest mover first), not z-score, so
    the finding a FinOps admin should chase leads."""
    tcol, vcol = spec["time_col"], spec["value_col"]
    gcol = spec.get("group_col")
    k = spec.get("k", 2.5)
    min_abs = spec.get("min_abs", 0)
    # Default the $ materiality bars off min_abs when a panel doesn't set them,
    # so every ts_spike panel is protected even without extra config.
    min_delta = spec.get("min_delta", max(min_abs * 0.5, 1000))
    min_delta_high = spec.get("min_delta_high", max(min_abs * 3, 20000))
    unit = spec.get("unit", "usd")
    if tcol not in ci or vcol not in ci:
        return []
    series = {}
    for r in rows:
        g = r[ci[gcol]] if gcol and gcol in ci else "total"
        t = r[ci[tcol]]
        v = _f(r[ci[vcol]])
        if v is None:
            continue
        series.setdefault(g, []).append((str(t), v))
    outs = []
    for g, pts in series.items():
        pts.sort(key=lambda p: p[0])
        if len(pts) < 4:
            continue
        hist = [v for _, v in pts[:-1]]
        last_t, last_v = pts[-1]
        mean = sum(hist) / len(hist)
        var = sum((v - mean) ** 2 for v in hist) / len(hist)
        std = var ** 0.5
        delta = last_v - mean
        # Gate on BOTH statistical and dollar materiality.
        if last_v < min_abs or std == 0 or delta < min_delta:
            continue
        z = delta / std
        if z < k or last_v <= mean:
            continue
        pct = (last_v / mean - 1) * 100 if mean else 0
        gl = "" if g == "total" else f"“{g}” "
        # 'high' needs a material dollar delta too — a huge % on a small base is 'medium'.
        sev = "high" if (z >= k + 1.5 and delta >= min_delta_high) else "medium"
        outs.append({
            "severity": sev,
            "_delta": delta,   # sort key (dropped before returning)
            "headline": f"{gl}spiked to {_fmt(last_v, unit)} — up {_fmt(delta, unit)} "
                        f"({pct:.0f}%) on its recent average",
            "detail": f"Latest period {last_t}: {_fmt(last_v, unit)} vs a prior mean of "
                      f"{_fmt(mean, unit)} — a {_fmt(delta, unit)} increase (z={z:.1f}). "
                      f"Worth checking what changed.",
        })
    # Rank by absolute $ movement — biggest dollar impact leads, not biggest %.
    outs.sort(key=lambda o: -o["_delta"])
    for o in outs:
        o.pop("_delta", None)
    return outs[:6]


def _rank_outlier(spec, ci, rows):
    """Top-ranked row vs the median of the rest — flags a dominant outlier."""
    vcol, lcol = spec["value_col"], spec.get("label_col")
    k = spec.get("k", 4.0)
    unit = spec.get("unit", "usd")
    if vcol not in ci:
        return []
    vals = [(_f(r[ci[vcol]]), (r[ci[lcol]] if lcol and lcol in ci else "top item")) for r in rows]
    vals = [(v, l) for v, l in vals if v is not None]
    if len(vals) < 3:
        return []
    top_v, top_l = vals[0]
    rest = [v for v, _ in vals[1:]]
    med = _median(rest)
    if not med or med <= 0:
        return []
    ratio = top_v / med
    if ratio >= k:
        return [{
            "severity": "high" if ratio >= k * 2 else "medium",
            "headline": f"“{top_l}” is {ratio:.0f}× the median — a dominant cost outlier",
            "detail": f"Top item {_fmt(top_v, unit)} vs a median of {_fmt(med, unit)} across the rest. "
                      f"Concentrated cost is the highest-leverage place to look first.",
        }]
    return []


def _new_entity(spec, ci, rows):
    """Entities present in the latest period but absent in prior ones — new cost
    that wasn't there before (expected growth, or a surprise to attribute)."""
    tcol, gcol, vcol = spec["time_col"], spec["group_col"], spec["value_col"]
    unit = spec.get("unit", "usd")
    min_abs = spec.get("min_abs", 0)
    if any(c not in ci for c in (tcol, gcol, vcol)):
        return []
    periods = sorted({str(r[ci[tcol]]) for r in rows})
    if len(periods) < 2:
        return []
    latest = periods[-1]
    prior = set()
    latest_vals = {}
    for r in rows:
        t = str(r[ci[tcol]])
        g = r[ci[gcol]]
        v = _f(r[ci[vcol]]) or 0
        if t == latest:
            latest_vals[g] = latest_vals.get(g, 0) + v
        else:
            prior.add(g)
    new = [(g, v) for g, v in latest_vals.items() if g not in prior and v >= min_abs and g is not None]
    new.sort(key=lambda x: -x[1])
    if not new:
        return []
    top = new[:4]
    names = ", ".join(f"“{g}” ({_fmt(v, unit)})" for g, v in top)
    return [{
        "severity": "medium",
        "headline": f"{len(new)} new item{'s' if len(new) != 1 else ''} appeared this period",
        "detail": f"Not present in prior periods: {names}. New workloads are expected growth — "
                  f"just make sure each has an owner and a budget.",
    }]


def _threshold(spec, ci, rows):
    """Count rows whose metric crosses a bar (e.g. clusters under 10% CPU)."""
    vcol = spec["value_col"]
    op = spec.get("op", "lt")
    bar = spec["bar"]
    unit = spec.get("unit", "count")
    label = spec.get("label", "items")
    if vcol not in ci:
        return []
    n = 0
    for r in rows:
        v = _f(r[ci[vcol]])
        if v is None:
            continue
        if (op == "lt" and v < bar) or (op == "gt" and v > bar):
            n += 1
    if n == 0:
        return []
    return [{
        "severity": "medium",
        "headline": f"{n} {label} crossed the {'below' if op == 'lt' else 'above'}-{_fmt(bar, unit)} line",
        "detail": "These are the rows worth acting on first.",
    }]


def _fmt(v, unit):
    if v is None:
        return "—"
    if unit == "usd":
        return f"${v:,.0f}" if abs(v) >= 1 else f"${v:,.2f}"
    if unit == "pct":
        return f"{v:,.1f}%"
    if unit == "tokens":
        return f"{v:,.0f} tok"
    if unit == "dbu":
        return f"{v:,.0f} DBU"
    return f"{v:,.0f}"


def compute_anomalies(panel: dict, columns: list, rows: list) -> list:
    return _detect(panel.get("anomaly"), columns, rows)


# ── Boards & panels ───────────────────────────────────────────────────────────
# chart: "line" | "bar" | "table" | "forecast" | "stacked_bar"
# For line/bar/stacked: x = first column, series/group = second (if categorical),
# value = last numeric column — the frontend infers from `chart` + column types.

BOARDS = [
    # ══════════════════════════════════════════════════════════════════════════
    # USAGE — total spend, by SKU, by workspace, and an AI forecast.
    # Core logic de-parameterised from Account Usage Dashboard v2 (labs sandbox).
    # ══════════════════════════════════════════════════════════════════════════
    {
        "id": "usage",
        "label": "Usage",
        "icon": "\U0001F4C8",
        "blurb": "Total spend and where it goes — over time, by product, by workspace — "
                 "with week-over-week spikes and brand-new cost flagged for you.",
        "panels": [
            {
                "id": "why_changed",
                "title": "Why did cost change? (this week vs last)",
                "eli5": "Breaks the week-over-week change in total spend into WHY it moved: new "
                        "workloads, retired workloads, volume growth (existing things doing more), "
                        "and rate/mix (pricier compute or SKU mix). The four add up to the total "
                        "change exactly — so you fix the right driver instead of cutting blindly.",
                "chart": "decomp",
                "note": "ΔCost = new − retired + volume + rate/mix, decomposed per workload "
                        "(job/pipeline/warehouse/endpoint/app) then summed — reconciles to the total "
                        "change by construction. Volume vs rate/mix split uses each workload's own "
                        "$/DBU. Compares the last COMPLETE week to the one before.",
                "anomaly": {"type": "decomp_driver"},
                "sql": r"""
WITH price AS (
  SELECT sku_name, cloud, price_start_time, price_end_time, pricing.effective_list.default AS unit_usd
  FROM system.billing.list_prices
),
usage AS (
  SELECT coalesce('job:'||u.usage_metadata.job_id, 'pipeline:'||u.usage_metadata.dlt_pipeline_id,
                  'warehouse:'||u.usage_metadata.warehouse_id, 'endpoint:'||u.usage_metadata.endpoint_name,
                  'app:'||u.usage_metadata.app_name, 'product:'||u.billing_origin_product) AS workload_key,
         u.usage_date,
         CAST(u.usage_quantity AS DOUBLE) AS dbus,
         CAST(u.usage_quantity * pr.unit_usd AS DOUBLE) AS est_cost
  FROM system.billing.usage u
  LEFT JOIN price pr ON u.sku_name=pr.sku_name AND u.cloud=pr.cloud
    AND u.usage_start_time>=pr.price_start_time AND (pr.price_end_time IS NULL OR u.usage_start_time<pr.price_end_time)
  WHERE u.usage_date >= date_trunc('WEEK', current_date()) - INTERVAL 2 WEEKS
    AND u.usage_date <  date_trunc('WEEK', current_date())
),
per_workload AS (
  SELECT workload_key,
    CAST(SUM(CASE WHEN usage_date >= date_trunc('WEEK', current_date()) - INTERVAL 1 WEEKS THEN est_cost ELSE 0 END) AS DOUBLE) AS cur_cost,
    CAST(SUM(CASE WHEN usage_date <  date_trunc('WEEK', current_date()) - INTERVAL 1 WEEKS THEN est_cost ELSE 0 END) AS DOUBLE) AS prev_cost,
    CAST(SUM(CASE WHEN usage_date >= date_trunc('WEEK', current_date()) - INTERVAL 1 WEEKS THEN dbus END) AS DOUBLE) AS cur_dbus,
    CAST(SUM(CASE WHEN usage_date <  date_trunc('WEEK', current_date()) - INTERVAL 1 WEEKS THEN dbus END) AS DOUBLE) AS prev_dbus
  FROM usage GROUP BY workload_key
),
classed AS (
  SELECT *,
    CASE WHEN prev_cost=0 AND cur_cost>0 THEN 'NEW' WHEN cur_cost=0 AND prev_cost>0 THEN 'RETIRED' ELSE 'EXISTING' END AS status,
    -- volume vs rate/mix split for EXISTING workloads (DBU-metered): volume = ΔDBU at last week's rate;
    -- rate/mix = the remainder (price/SKU-mix). Non-DBU workloads attribute their whole delta to volume.
    CASE WHEN prev_cost>0 AND cur_cost>0 AND coalesce(prev_dbus,0)>0 AND coalesce(cur_dbus,0)>0 THEN (cur_dbus-prev_dbus)*(prev_cost/prev_dbus)
         WHEN prev_cost>0 AND cur_cost>0 THEN cur_cost-prev_cost ELSE 0 END AS volume_effect,
    CASE WHEN prev_cost>0 AND cur_cost>0 AND coalesce(prev_dbus,0)>0 AND coalesce(cur_dbus,0)>0 THEN cur_dbus*((cur_cost/cur_dbus)-(prev_cost/prev_dbus))
         ELSE 0 END AS rate_mix_effect
  FROM per_workload
)
SELECT round(sum(prev_cost),2)                                              AS prev_week_usd,
       round(sum(cur_cost),2)                                               AS this_week_usd,
       round(sum(cur_cost)-sum(prev_cost),2)                                AS delta_usd,
       round(sum(CASE WHEN status='NEW' THEN cur_cost ELSE 0 END),2)        AS new_workloads_usd,
       round(-sum(CASE WHEN status='RETIRED' THEN prev_cost ELSE 0 END),2)  AS retired_workloads_usd,
       round(sum(volume_effect),2)                                          AS volume_growth_usd,
       round(sum(rate_mix_effect),2)                                        AS rate_mix_usd,
       count(CASE WHEN status='NEW' THEN 1 END)                             AS new_count,
       count(CASE WHEN status='RETIRED' THEN 1 END)                         AS retired_count
FROM classed
""",
            },
            {
                "id": "trend",
                "title": "Spend trend (weekly, all products)",
                "eli5": "Total list-price $ per week across the account. A step-change up is the "
                        "first thing to explain.",
                "chart": "line",
                # Total weekly spend is large; only flag a materially large weekly jump.
                "anomaly": {"type": "ts_spike", "time_col": "week", "value_col": "list_usd",
                            "k": 2.0, "min_abs": 10000, "min_delta": 50000,
                            "min_delta_high": 250000, "unit": "usd"},
                "sql": """
WITH price AS (
  SELECT sku_name, cloud, price_start_time, price_end_time,
         pricing.effective_list.default AS unit_usd
  FROM system.billing.list_prices
)
SELECT date_trunc('WEEK', u.usage_date)              AS week,
       round(sum(u.usage_quantity * pr.unit_usd), 2) AS list_usd
FROM system.billing.usage u
LEFT JOIN price pr ON u.sku_name = pr.sku_name AND u.cloud = pr.cloud
  AND u.usage_start_time >= pr.price_start_time
  AND (pr.price_end_time IS NULL OR u.usage_start_time < pr.price_end_time)
WHERE u.usage_date >= current_date() - INTERVAL 12 WEEKS
  AND u.usage_date < date_trunc('WEEK', current_date())   -- complete weeks only (drop partial current week)
GROUP BY 1 ORDER BY 1
""",
            },
            {
                "id": "by_sku",
                "title": "Spend by product (weekly)",
                "eli5": "Weekly $ per billing product. Watch for a SKU accelerating week over week.",
                "chart": "stacked_bar",
                # Materiality floors so a big % on a tiny product never fires (accuracy > noise):
                # a product must bill >= $10k in the latest week, move up >= $5k to flag, and
                # >= $50k to be 'high'. Ranked by $ delta so the biggest mover leads.
                "anomaly": {"type": "ts_spike", "time_col": "week", "value_col": "list_usd",
                            "group_col": "product", "k": 2.0, "min_abs": 10000,
                            "min_delta": 5000, "min_delta_high": 50000, "unit": "usd"},
                "sql": """
WITH price AS (
  SELECT sku_name, cloud, price_start_time, price_end_time,
         pricing.effective_list.default AS unit_usd
  FROM system.billing.list_prices
)
SELECT date_trunc('WEEK', u.usage_date)              AS week,
       u.billing_origin_product                      AS product,
       round(sum(u.usage_quantity * pr.unit_usd), 2) AS list_usd
FROM system.billing.usage u
LEFT JOIN price pr ON u.sku_name = pr.sku_name AND u.cloud = pr.cloud
  AND u.usage_start_time >= pr.price_start_time
  AND (pr.price_end_time IS NULL OR u.usage_start_time < pr.price_end_time)
WHERE u.usage_date >= current_date() - INTERVAL 8 WEEKS
  AND u.usage_date < date_trunc('WEEK', current_date())   -- complete weeks only
GROUP BY 1, 2 HAVING sum(u.usage_quantity * pr.unit_usd) > 0
ORDER BY 1, list_usd DESC
""",
            },
            {
                "id": "by_workspace",
                "title": "Spend by workspace (30 days)",
                "eli5": "Which workspace is spending the most. A single dominant workspace is where "
                        "attribution effort pays off first.",
                "chart": "bar",
                "anomaly": {"type": "rank_outlier", "value_col": "list_usd",
                            "label_col": "workspace", "k": 4.0, "unit": "usd"},
                "sql": """
WITH price AS (
  SELECT sku_name, cloud, price_start_time, price_end_time,
         pricing.effective_list.default AS unit_usd
  FROM system.billing.list_prices
),
ws AS (SELECT workspace_id, max(workspace_name) AS workspace_name
       FROM system.access.workspaces_latest GROUP BY 1)
SELECT coalesce(w.workspace_name, concat('id:', u.workspace_id)) AS workspace,
       round(sum(u.usage_quantity * pr.unit_usd), 2)             AS list_usd
FROM system.billing.usage u
LEFT JOIN price pr ON u.sku_name = pr.sku_name AND u.cloud = pr.cloud
  AND u.usage_start_time >= pr.price_start_time
  AND (pr.price_end_time IS NULL OR u.usage_start_time < pr.price_end_time)
LEFT JOIN ws w ON u.workspace_id = w.workspace_id
WHERE u.usage_date >= current_date() - INTERVAL 30 DAYS
GROUP BY 1 ORDER BY list_usd DESC NULLS LAST LIMIT 20
""",
            },
            {
                "id": "forecast",
                "title": "Spend forecast (next 4 weeks)",
                "eli5": "A predictive forecast of weekly spend using Databricks' built-in AI_FORECAST — "
                        "actuals plus a projected line with a confidence band.",
                "chart": "forecast",
                "anomaly": None,   # the forecast IS the forward-looking signal
                "sql": """
WITH price AS (
  SELECT sku_name, cloud, price_start_time, price_end_time,
         pricing.effective_list.default AS unit_usd
  FROM system.billing.list_prices
),
hist AS (
  SELECT date_trunc('WEEK', u.usage_date)::timestamp      AS week,
         round(sum(u.usage_quantity * pr.unit_usd), 2)    AS list_usd
  FROM system.billing.usage u
  LEFT JOIN price pr ON u.sku_name = pr.sku_name AND u.cloud = pr.cloud
    AND u.usage_start_time >= pr.price_start_time
    AND (pr.price_end_time IS NULL OR u.usage_start_time < pr.price_end_time)
  WHERE u.usage_date >= current_date() - INTERVAL 26 WEEKS
    AND u.usage_date < date_trunc('WEEK', current_date())   -- complete weeks only (no partial-week cliff)
  GROUP BY 1
),
fc AS (
  SELECT week, usage_forecast, usage_upper, usage_lower
  FROM AI_FORECAST(
    TABLE(SELECT week, list_usd AS usage FROM hist),
    horizon => date_trunc('WEEK', current_date()) + INTERVAL 4 WEEKS,
    time_col => 'week',
    value_col => 'usage',
    prediction_interval_width => 0.9
  )
)
-- actuals + forecast as separate rows; the frontend merges by week and bridges the
-- last actual point into the forecast series (so the dashed line connects, no gap).
SELECT week, list_usd AS actual_usd, CAST(NULL AS DOUBLE) AS forecast_usd,
       CAST(NULL AS DOUBLE) AS upper_usd, CAST(NULL AS DOUBLE) AS lower_usd
FROM hist
UNION ALL
SELECT week, CAST(NULL AS DOUBLE), round(usage_forecast,2), round(usage_upper,2), round(usage_lower,2)
FROM fc
ORDER BY week
""",
            },
        ],
    },

    # ══════════════════════════════════════════════════════════════════════════
    # JOBS — potential savings, CPU/mem efficiency, fixed workers, outdated DBR.
    # Docs lacked these five, so built from compute tables (probed columns).
    # ══════════════════════════════════════════════════════════════════════════
    {
        "id": "jobs",
        "label": "Jobs",
        "icon": "⚙️",
        "blurb": "Job-level efficiency: where the money could be saved, which jobs waste "
                 "cores or memory, and which run on fixed-size or outdated compute.",
        "panels": [
            {
                "id": "potential_savings",
                "title": "Top 25 jobs by potential monthly saving",
                "eli5": "Jobs running on All-Purpose compute ($0.55/DBU) that would cost far less on "
                        "Jobs compute ($0.15/DBU). The saving is the rate difference on their own DBUs.",
                "chart": "table",
                "anomaly": {"type": "rank_outlier", "value_col": "est_monthly_saving_usd",
                            "label_col": "job_name", "k": 4.0, "unit": "usd"},
                "sql": """
WITH price AS (
  SELECT sku_name, cloud, price_start_time, price_end_time,
         pricing.effective_list.default AS unit_usd FROM system.billing.list_prices
),
job_names AS (SELECT workspace_id, job_id, max(name) AS job_name FROM system.lakeflow.jobs GROUP BY 1,2),
ap_clusters AS (   -- all-purpose clusters (interactive), latest config
  SELECT workspace_id, cluster_id FROM (
    SELECT workspace_id, cluster_id, cluster_source
    FROM system.compute.clusters
    QUALIFY ROW_NUMBER() OVER (PARTITION BY workspace_id, cluster_id ORDER BY change_time DESC)=1
  ) WHERE cluster_source IN ('UI','API')
),
job_ap_runs AS (  -- job runs that used an all-purpose cluster
  SELECT t.workspace_id, t.job_id, exploded.cluster_id
  FROM system.lakeflow.job_task_run_timeline t
  LATERAL VIEW explode(t.compute_ids) exploded AS cluster_id
  WHERE t.period_start_time >= current_date() - INTERVAL 30 DAYS
    AND t.compute_ids IS NOT NULL AND array_size(t.compute_ids) > 0
),
ap_job_ids AS (
  SELECT DISTINCT r.workspace_id, r.job_id
  FROM job_ap_runs r JOIN ap_clusters c
    ON r.workspace_id=c.workspace_id AND r.cluster_id=c.cluster_id
),
job_spend AS (  -- 30-day all-purpose $ for those jobs
  SELECT u.workspace_id, u.usage_metadata.job_id AS job_id,
         sum(u.usage_quantity * pr.unit_usd) AS ap_usd_30d
  FROM system.billing.usage u
  JOIN price pr ON u.sku_name=pr.sku_name AND u.cloud=pr.cloud
    AND u.usage_start_time >= pr.price_start_time
    AND (pr.price_end_time IS NULL OR u.usage_start_time < pr.price_end_time)
  WHERE u.billing_origin_product='JOBS' AND u.usage_date >= current_date() - INTERVAL 30 DAYS
    AND u.usage_metadata.job_id IS NOT NULL
  GROUP BY 1,2
)
SELECT coalesce(jn.job_name, CAST(s.job_id AS STRING)) AS job_name,
       round(s.ap_usd_30d, 2)                          AS all_purpose_usd_30d,
       round(s.ap_usd_30d * (0.55-0.15)/0.55, 2)       AS est_monthly_saving_usd
FROM job_spend s
JOIN ap_job_ids a ON s.workspace_id=a.workspace_id AND s.job_id=a.job_id
LEFT JOIN job_names jn ON s.workspace_id=jn.workspace_id AND s.job_id=jn.job_id
WHERE s.ap_usd_30d > 0
ORDER BY est_monthly_saving_usd DESC LIMIT 25
""",
            },
            {
                "id": "cpu_efficiency",
                "title": "Top 10 jobs with lowest avg CPU",
                "eli5": "Job clusters running at low average CPU — cores you pay for that sit idle. "
                        "A downsize candidate.",
                "chart": "table",
                "anomaly": {"type": "threshold", "value_col": "avg_cpu_pct", "op": "lt",
                            "bar": 20, "unit": "pct", "label": "job clusters under 20% CPU"},
                "sql": """
WITH job_names AS (SELECT workspace_id, job_id, max(name) AS job_name FROM system.lakeflow.jobs GROUP BY 1,2),
job_clusters AS (
  SELECT DISTINCT t.workspace_id, t.job_id, exploded.cluster_id
  FROM system.lakeflow.job_task_run_timeline t
  LATERAL VIEW explode(t.compute_ids) exploded AS cluster_id
  WHERE t.period_start_time >= current_date() - INTERVAL 30 DAYS
    AND t.compute_ids IS NOT NULL AND array_size(t.compute_ids) > 0
),
cpu AS (
  SELECT cluster_id, count(*) AS node_minutes,
         round(avg(cpu_user_percent + cpu_system_percent), 1) AS avg_cpu_pct
  FROM system.compute.node_timeline
  WHERE start_time >= current_date() - INTERVAL 30 DAYS AND driver = false
  GROUP BY 1 HAVING count(*) > 200
)
SELECT coalesce(jn.job_name, CAST(jc.job_id AS STRING)) AS job_name,
       cpu.avg_cpu_pct, cpu.node_minutes
FROM job_clusters jc
JOIN cpu ON jc.cluster_id = cpu.cluster_id
LEFT JOIN job_names jn ON jc.workspace_id=jn.workspace_id AND jc.job_id=jn.job_id
ORDER BY cpu.avg_cpu_pct ASC, cpu.node_minutes DESC LIMIT 10
""",
            },
            {
                "id": "memory_allocation",
                "title": "Top 10 jobs with highest avg memory",
                "eli5": "Job clusters running near their memory ceiling — memory-bound work that may "
                        "need a different node type (more RAM), not more nodes.",
                "chart": "table",
                "anomaly": {"type": "threshold", "value_col": "avg_mem_pct", "op": "gt",
                            "bar": 85, "unit": "pct", "label": "job clusters over 85% memory"},
                "sql": """
WITH job_names AS (SELECT workspace_id, job_id, max(name) AS job_name FROM system.lakeflow.jobs GROUP BY 1,2),
job_clusters AS (
  SELECT DISTINCT t.workspace_id, t.job_id, exploded.cluster_id
  FROM system.lakeflow.job_task_run_timeline t
  LATERAL VIEW explode(t.compute_ids) exploded AS cluster_id
  WHERE t.period_start_time >= current_date() - INTERVAL 30 DAYS
    AND t.compute_ids IS NOT NULL AND array_size(t.compute_ids) > 0
),
mem AS (
  SELECT cluster_id, count(*) AS node_minutes,
         round(avg(mem_used_percent), 1) AS avg_mem_pct,
         round(max(mem_used_percent), 1) AS peak_mem_pct
  FROM system.compute.node_timeline
  WHERE start_time >= current_date() - INTERVAL 30 DAYS AND driver = false
  GROUP BY 1 HAVING count(*) > 200
)
SELECT coalesce(jn.job_name, CAST(jc.job_id AS STRING)) AS job_name,
       mem.avg_mem_pct, mem.peak_mem_pct, mem.node_minutes
FROM job_clusters jc
JOIN mem ON jc.cluster_id = mem.cluster_id
LEFT JOIN job_names jn ON jc.workspace_id=jn.workspace_id AND jc.job_id=jn.job_id
ORDER BY mem.avg_mem_pct DESC LIMIT 10
""",
            },
            {
                "id": "fixed_workers",
                "title": "Jobs on fixed-size clusters (last 30 days)",
                "eli5": "Job clusters with a fixed worker count and no autoscaling — they can't shrink "
                        "for light runs or grow for heavy ones. Autoscaling usually saves money.",
                "chart": "table",
                "anomaly": None,
                "sql": """
WITH job_names AS (SELECT workspace_id, job_id, max(name) AS job_name FROM system.lakeflow.jobs GROUP BY 1,2),
job_clusters AS (
  SELECT DISTINCT t.workspace_id, t.job_id, exploded.cluster_id
  FROM system.lakeflow.job_task_run_timeline t
  LATERAL VIEW explode(t.compute_ids) exploded AS cluster_id
  WHERE t.period_start_time >= current_date() - INTERVAL 30 DAYS
    AND t.compute_ids IS NOT NULL AND array_size(t.compute_ids) > 0
),
cfg AS (
  SELECT workspace_id, cluster_id, cluster_name, worker_count,
         min_autoscale_workers, max_autoscale_workers, worker_node_type
  FROM system.compute.clusters
  QUALIFY ROW_NUMBER() OVER (PARTITION BY workspace_id, cluster_id ORDER BY change_time DESC)=1
)
SELECT coalesce(jn.job_name, CAST(jc.job_id AS STRING)) AS job_name,
       coalesce(cfg.cluster_name, jc.cluster_id)        AS cluster_name,
       cfg.worker_count, cfg.worker_node_type
FROM job_clusters jc
JOIN cfg ON jc.workspace_id=cfg.workspace_id AND jc.cluster_id=cfg.cluster_id
LEFT JOIN job_names jn ON jc.workspace_id=jn.workspace_id AND jc.job_id=jn.job_id
WHERE cfg.min_autoscale_workers IS NULL AND cfg.max_autoscale_workers IS NULL
  AND cfg.worker_count >= 1
ORDER BY cfg.worker_count DESC LIMIT 50
""",
            },
            {
                "id": "outdated_dbr",
                "title": "Jobs on outdated DBR (last 30 days)",
                "eli5": "Job clusters on old Databricks Runtime versions (below the current LTS). Newer "
                        "runtimes are faster and get Photon/security fixes — upgrading is often free speed.",
                "chart": "table",
                "anomaly": None,
                "sql": """
WITH job_names AS (SELECT workspace_id, job_id, max(name) AS job_name FROM system.lakeflow.jobs GROUP BY 1,2),
job_clusters AS (
  SELECT DISTINCT t.workspace_id, t.job_id, exploded.cluster_id
  FROM system.lakeflow.job_task_run_timeline t
  LATERAL VIEW explode(t.compute_ids) exploded AS cluster_id
  WHERE t.period_start_time >= current_date() - INTERVAL 30 DAYS
    AND t.compute_ids IS NOT NULL AND array_size(t.compute_ids) > 0
),
cfg AS (
  SELECT workspace_id, cluster_id, cluster_name, dbr_version,
         try_cast(regexp_extract(dbr_version, '^(\\\\d+)\\\\.', 1) AS INT) AS major
  FROM system.compute.clusters
  QUALIFY ROW_NUMBER() OVER (PARTITION BY workspace_id, cluster_id ORDER BY change_time DESC)=1
)
SELECT coalesce(jn.job_name, CAST(jc.job_id AS STRING)) AS job_name,
       coalesce(cfg.cluster_name, jc.cluster_id)        AS cluster_name,
       cfg.dbr_version
FROM job_clusters jc
JOIN cfg ON jc.workspace_id=cfg.workspace_id AND jc.cluster_id=cfg.cluster_id
LEFT JOIN job_names jn ON jc.workspace_id=jn.workspace_id AND jc.job_id=jn.job_id
WHERE cfg.major IS NOT NULL AND cfg.major < 15   -- below DBR 15.x LTS-era
ORDER BY cfg.major ASC, job_name LIMIT 50
""",
            },
        ],
    },

    # ══════════════════════════════════════════════════════════════════════════
    # DBSQL — per-query cost attributed to user / source / query, over time.
    # Allocation is the app's own M1 method (execution-time share of the
    # warehouse-hour $), de-scoped from the cost-per-query PrPr MV so it needs
    # no PrPr table. Source-type CASE is transcribed from that MV.
    # ══════════════════════════════════════════════════════════════════════════
    {
        "id": "dbsql",
        "label": "DBSQL",
        "icon": "\U0001F5C2️",
        "blurb": "Databricks SQL cost attributed down to the user, the source (job, notebook, "
                 "dashboard, Genie), and the individual query — plus the trend.",
        "panels": [
            {
                "id": "by_user",
                "title": "Attributed cost by user (14 days)",
                "eli5": "Each query gets a slice of its warehouse-hour cost by execution-time share; "
                        "this rolls that up per user. A single heavy user is where a chat starts.",
                "chart": "bar",
                "anomaly": {"type": "rank_outlier", "value_col": "query_usd",
                            "label_col": "executed_by", "k": 4.0, "unit": "usd"},
                "sql": """
WITH wh_hour AS (
  SELECT warehouse_id, hour, sum(usage_quantity * unit_usd) AS hour_usd FROM (
    SELECT u.usage_metadata.warehouse_id AS warehouse_id,
           date_trunc('HOUR', u.usage_start_time) AS hour,
           u.usage_quantity, lp.pricing.effective_list.default AS unit_usd
    FROM system.billing.usage u JOIN system.billing.list_prices lp
      ON u.sku_name=lp.sku_name AND u.cloud=lp.cloud
     AND u.usage_start_time>=lp.price_start_time AND (lp.price_end_time IS NULL OR u.usage_start_time<lp.price_end_time)
    WHERE u.usage_metadata.warehouse_id IS NOT NULL AND u.usage_date >= current_date() - INTERVAL 14 DAYS
    QUALIFY ROW_NUMBER() OVER (PARTITION BY u.record_id ORDER BY lp.price_start_time DESC)=1
  ) GROUP BY 1,2
),
cfg AS (SELECT warehouse_id, max_clusters, change_time AS vf,
          coalesce(lead(change_time) OVER (PARTITION BY warehouse_id ORDER BY change_time), TIMESTAMP '9999-12-31') AS vt
        FROM system.compute.warehouses),
q AS (
  SELECT compute.warehouse_id AS warehouse_id, date_trunc('HOUR', start_time) AS hour,
         executed_by, execution_duration_ms,
         sum(execution_duration_ms) OVER (PARTITION BY compute.warehouse_id, date_trunc('HOUR', start_time)) AS busy_ms
  FROM system.query.history
  WHERE start_time >= current_date() - INTERVAL 14 DAYS AND execution_duration_ms > 0
),
alloc AS (
  SELECT q.*, w.hour_usd,
         GREATEST(coalesce(c.max_clusters,1)*3600000.0, q.busy_ms) AS capacity_ms
  FROM q JOIN wh_hour w USING (warehouse_id, hour)
         LEFT JOIN cfg c ON c.warehouse_id=q.warehouse_id AND q.hour>=c.vf AND q.hour<c.vt
)
SELECT executed_by,
       round(sum(hour_usd * execution_duration_ms / capacity_ms), 2) AS query_usd,
       count(*) AS statements
FROM alloc GROUP BY 1 ORDER BY query_usd DESC NULLS LAST LIMIT 20
""",
            },
            {
                "id": "by_source",
                "title": "Attributed cost by source type (14 days)",
                "eli5": "Where the queries come from — Job, Notebook, SQL Query, AI/BI Dashboard, "
                        "Genie, Alert, or Other (ad-hoc / driver). Tells you which surface to tune.",
                "chart": "bar",
                "anomaly": None,
                "sql": """
WITH wh_hour AS (
  SELECT warehouse_id, hour, sum(usage_quantity * unit_usd) AS hour_usd FROM (
    SELECT u.usage_metadata.warehouse_id AS warehouse_id,
           date_trunc('HOUR', u.usage_start_time) AS hour,
           u.usage_quantity, lp.pricing.effective_list.default AS unit_usd
    FROM system.billing.usage u JOIN system.billing.list_prices lp
      ON u.sku_name=lp.sku_name AND u.cloud=lp.cloud
     AND u.usage_start_time>=lp.price_start_time AND (lp.price_end_time IS NULL OR u.usage_start_time<lp.price_end_time)
    WHERE u.usage_metadata.warehouse_id IS NOT NULL AND u.usage_date >= current_date() - INTERVAL 14 DAYS
    QUALIFY ROW_NUMBER() OVER (PARTITION BY u.record_id ORDER BY lp.price_start_time DESC)=1
  ) GROUP BY 1,2
),
cfg AS (SELECT warehouse_id, max_clusters, change_time AS vf,
          coalesce(lead(change_time) OVER (PARTITION BY warehouse_id ORDER BY change_time), TIMESTAMP '9999-12-31') AS vt
        FROM system.compute.warehouses),
q AS (
  SELECT compute.warehouse_id AS warehouse_id, date_trunc('HOUR', start_time) AS hour,
         CASE
           WHEN query_source.job_info.job_id IS NOT NULL THEN 'Job'
           WHEN query_source.legacy_dashboard_id IS NOT NULL THEN 'Legacy dashboard'
           WHEN query_source.dashboard_id IS NOT NULL THEN 'AI/BI dashboard'
           WHEN query_source.alert_id IS NOT NULL THEN 'Alert'
           WHEN query_source.notebook_id IS NOT NULL THEN 'Notebook'
           WHEN query_source.sql_query_id IS NOT NULL THEN 'SQL query'
           WHEN query_source.genie_space_id IS NOT NULL THEN 'Genie'
           ELSE 'Other (ad-hoc / driver)' END AS source_type,
         execution_duration_ms,
         sum(execution_duration_ms) OVER (PARTITION BY compute.warehouse_id, date_trunc('HOUR', start_time)) AS busy_ms
  FROM system.query.history
  WHERE start_time >= current_date() - INTERVAL 14 DAYS AND execution_duration_ms > 0
),
alloc AS (
  SELECT q.*, w.hour_usd,
         GREATEST(coalesce(c.max_clusters,1)*3600000.0, q.busy_ms) AS capacity_ms
  FROM q JOIN wh_hour w USING (warehouse_id, hour)
         LEFT JOIN cfg c ON c.warehouse_id=q.warehouse_id AND q.hour>=c.vf AND q.hour<c.vt
)
SELECT source_type,
       round(sum(hour_usd * execution_duration_ms / capacity_ms), 2) AS query_usd,
       count(*) AS statements
FROM alloc GROUP BY 1 ORDER BY query_usd DESC NULLS LAST
""",
            },
            {
                "id": "top_queries",
                "title": "Most expensive queries (14 days)",
                "eli5": "The individual statements that cost the most, each with who ran it, the "
                        "warehouse it ran on, and where it came from. Click a statement to open it in "
                        "Query History.",
                "chart": "table",
                "note": "Cost = the query's share of its warehouse-hour $, by execution-time. Warehouses "
                        "bill by size, so a longer query on a small warehouse can cost LESS than a shorter "
                        "one on a 2X-Large — that's why exec_min and query_usd don't always rank together. "
                        "The warehouse column shows which one ran it.",
                "anomaly": {"type": "rank_outlier", "value_col": "query_usd",
                            "label_col": "executed_by", "k": 5.0, "unit": "usd"},
                "sql": """
WITH wh_hour AS (
  SELECT warehouse_id, hour, sum(usage_quantity * unit_usd) AS hour_usd FROM (
    SELECT u.usage_metadata.warehouse_id AS warehouse_id,
           date_trunc('HOUR', u.usage_start_time) AS hour,
           u.usage_quantity, lp.pricing.effective_list.default AS unit_usd
    FROM system.billing.usage u JOIN system.billing.list_prices lp
      ON u.sku_name=lp.sku_name AND u.cloud=lp.cloud
     AND u.usage_start_time>=lp.price_start_time AND (lp.price_end_time IS NULL OR u.usage_start_time<lp.price_end_time)
    WHERE u.usage_metadata.warehouse_id IS NOT NULL AND u.usage_date >= current_date() - INTERVAL 14 DAYS
    QUALIFY ROW_NUMBER() OVER (PARTITION BY u.record_id ORDER BY lp.price_start_time DESC)=1
  ) GROUP BY 1,2
),
cfg AS (SELECT warehouse_id, max_clusters, change_time AS vf,
          coalesce(lead(change_time) OVER (PARTITION BY warehouse_id ORDER BY change_time), TIMESTAMP '9999-12-31') AS vt
        FROM system.compute.warehouses),
wh_name AS (SELECT warehouse_id, warehouse_name FROM system.compute.warehouses
            QUALIFY ROW_NUMBER() OVER (PARTITION BY warehouse_id ORDER BY change_time DESC)=1),
ws AS (SELECT workspace_id, max(workspace_url) AS workspace_url FROM system.access.workspaces_latest GROUP BY 1),
q AS (
  SELECT statement_id, workspace_id, compute.warehouse_id AS warehouse_id, date_trunc('HOUR', start_time) AS hour,
         executed_by, statement_type,
         CASE
           WHEN query_source.job_info.job_id IS NOT NULL THEN 'Job'
           WHEN query_source.dashboard_id IS NOT NULL THEN 'AI/BI dashboard'
           WHEN query_source.notebook_id IS NOT NULL THEN 'Notebook'
           WHEN query_source.sql_query_id IS NOT NULL THEN 'SQL query'
           WHEN query_source.genie_space_id IS NOT NULL THEN 'Genie'
           ELSE 'Other' END AS source_type,
         -- the id behind the source (so "which dashboard/job?" is answerable + deep-linkable)
         coalesce(query_source.job_info.job_id, query_source.dashboard_id, query_source.legacy_dashboard_id,
                  query_source.notebook_id, query_source.sql_query_id, query_source.genie_space_id) AS source_id,
         execution_duration_ms,
         sum(execution_duration_ms) OVER (PARTITION BY compute.warehouse_id, date_trunc('HOUR', start_time)) AS busy_ms
  FROM system.query.history
  WHERE start_time >= current_date() - INTERVAL 14 DAYS AND execution_duration_ms > 0
),
alloc AS (
  SELECT q.*, w.hour_usd,
         GREATEST(coalesce(c.max_clusters,1)*3600000.0, q.busy_ms) AS capacity_ms
  FROM q JOIN wh_hour w USING (warehouse_id, hour)
         LEFT JOIN cfg c ON c.warehouse_id=q.warehouse_id AND q.hour>=c.vf AND q.hour<c.vt
)
SELECT a.statement_id, a.executed_by,
       coalesce(wn.warehouse_name, a.warehouse_id)              AS warehouse,
       a.source_type, a.source_id, a.statement_type,
       round(a.execution_duration_ms/60000.0, 2)                    AS exec_min,
       round(a.hour_usd * a.execution_duration_ms / a.capacity_ms, 2) AS query_usd,
       ws.workspace_url   -- the workspace this statement actually ran in (system tables span the whole account)
FROM alloc a LEFT JOIN wh_name wn ON a.warehouse_id = wn.warehouse_id
             LEFT JOIN ws ON a.workspace_id = ws.workspace_id
ORDER BY query_usd DESC NULLS LAST LIMIT 50
""",
            },
            {
                "id": "trend",
                "title": "Attributed DBSQL cost over time (daily)",
                "eli5": "The attributed query cost per day — the trend line behind the per-user and "
                        "per-source views. A jump here is a day to investigate.",
                "chart": "line",
                "anomaly": {"type": "ts_spike", "time_col": "day", "value_col": "query_usd",
                            "k": 2.0, "min_abs": 200, "min_delta": 100,
                            "min_delta_high": 1000, "unit": "usd"},
                "sql": """
WITH wh_hour AS (
  SELECT warehouse_id, hour, sum(usage_quantity * unit_usd) AS hour_usd FROM (
    SELECT u.usage_metadata.warehouse_id AS warehouse_id,
           date_trunc('HOUR', u.usage_start_time) AS hour,
           u.usage_quantity, lp.pricing.effective_list.default AS unit_usd
    FROM system.billing.usage u JOIN system.billing.list_prices lp
      ON u.sku_name=lp.sku_name AND u.cloud=lp.cloud
     AND u.usage_start_time>=lp.price_start_time AND (lp.price_end_time IS NULL OR u.usage_start_time<lp.price_end_time)
    WHERE u.usage_metadata.warehouse_id IS NOT NULL AND u.usage_date >= current_date() - INTERVAL 28 DAYS
    QUALIFY ROW_NUMBER() OVER (PARTITION BY u.record_id ORDER BY lp.price_start_time DESC)=1
  ) GROUP BY 1,2
),
cfg AS (SELECT warehouse_id, max_clusters, change_time AS vf,
          coalesce(lead(change_time) OVER (PARTITION BY warehouse_id ORDER BY change_time), TIMESTAMP '9999-12-31') AS vt
        FROM system.compute.warehouses),
q AS (
  SELECT compute.warehouse_id AS warehouse_id, date_trunc('HOUR', start_time) AS hour,
         date_trunc('DAY', start_time) AS day, execution_duration_ms,
         sum(execution_duration_ms) OVER (PARTITION BY compute.warehouse_id, date_trunc('HOUR', start_time)) AS busy_ms
  FROM system.query.history
  WHERE start_time >= current_date() - INTERVAL 28 DAYS AND execution_duration_ms > 0
    AND start_time < current_date()   -- complete days only (drop today's partial day)
),
alloc AS (
  SELECT q.*, w.hour_usd,
         GREATEST(coalesce(c.max_clusters,1)*3600000.0, q.busy_ms) AS capacity_ms
  FROM q JOIN wh_hour w USING (warehouse_id, hour)
         LEFT JOIN cfg c ON c.warehouse_id=q.warehouse_id AND q.hour>=c.vf AND q.hour<c.vt
)
SELECT day, round(sum(hour_usd * execution_duration_ms / capacity_ms), 2) AS query_usd
FROM alloc GROUP BY 1 ORDER BY 1
""",
            },
        ],
    },

    # ══════════════════════════════════════════════════════════════════════════
    # MODEL SERVING — batch inference, pay-per-token, provisioned throughput,
    # granular by endpoint. Transcribed from the tiantan32 cost-attribution
    # dashboard; serving-type CASE verified against live product_features.
    # ══════════════════════════════════════════════════════════════════════════
    {
        "id": "serving",
        "label": "Model Serving",
        "icon": "\U0001F9E0",
        "blurb": "AI/ML serving spend split the way it actually bills: batch inference, "
                 "pay-per-token foundation models, and provisioned-throughput endpoints.",
        "panels": [
            {
                "id": "by_type",
                "title": "Serving spend by type (weekly)",
                "eli5": "Weekly $ split into Batch Inference, Pay-per-token, Provisioned throughput, "
                        "and custom models — the three foundation-model modes bill very differently.",
                "chart": "stacked_bar",
                # Serving spend is smaller than total spend, so lower floors than the by-product panel
                # but still material: >= $2k latest, >= $1k move to flag, >= $10k to be 'high'.
                "anomaly": {"type": "ts_spike", "time_col": "week", "value_col": "list_usd",
                            "group_col": "serving_type", "k": 2.0, "min_abs": 2000,
                            "min_delta": 1000, "min_delta_high": 10000, "unit": "usd"},
                "sql": """
WITH price AS (
  SELECT sku_name, cloud, price_start_time, price_end_time,
         pricing.effective_list.default AS unit_usd FROM system.billing.list_prices
)
SELECT date_trunc('WEEK', u.usage_date) AS week,
       CASE
         WHEN u.product_features.model_serving.offering_type = 'BATCH_INFERENCE' THEN 'Batch inference'
         WHEN u.product_features.serving_type = 'FOUNDATION_MODEL' AND u.usage_type = 'TOKEN' THEN 'Pay-per-token'
         WHEN u.product_features.serving_type = 'FOUNDATION_MODEL' AND u.usage_type = 'COMPUTE_TIME' THEN 'Provisioned throughput'
         WHEN u.product_features.serving_type IN ('MODEL','GPU_MODEL') THEN 'Custom model'
         WHEN u.product_features.serving_type = 'FEATURE' THEN 'Feature serving'
         ELSE coalesce(u.product_features.serving_type, 'Other') END AS serving_type,
       round(sum(u.usage_quantity * pr.unit_usd), 2) AS list_usd
FROM system.billing.usage u
JOIN price pr ON u.sku_name=pr.sku_name AND u.cloud=pr.cloud
  AND u.usage_start_time >= pr.price_start_time
  AND (pr.price_end_time IS NULL OR u.usage_start_time < pr.price_end_time)
WHERE u.billing_origin_product = 'MODEL_SERVING'
  AND u.usage_date >= current_date() - INTERVAL 8 WEEKS
  AND u.usage_date < date_trunc('WEEK', current_date())   -- complete weeks only
GROUP BY 1, 2 HAVING sum(u.usage_quantity * pr.unit_usd) > 0
ORDER BY 1, list_usd DESC
""",
            },
            {
                "id": "by_endpoint",
                "title": "Spend by endpoint (30 days)",
                "eli5": "Which serving endpoint costs the most. A single dominant endpoint is the first "
                        "place to check batch size, caching, and whether it needs to be always-on.",
                "chart": "bar",
                "anomaly": {"type": "rank_outlier", "value_col": "list_usd",
                            "label_col": "endpoint", "k": 4.0, "unit": "usd"},
                "sql": """
WITH price AS (
  SELECT sku_name, cloud, price_start_time, price_end_time,
         pricing.effective_list.default AS unit_usd FROM system.billing.list_prices
)
SELECT coalesce(u.usage_metadata.endpoint_name, 'not populated') AS endpoint,
       round(sum(u.usage_quantity * pr.unit_usd), 2)             AS list_usd
FROM system.billing.usage u
JOIN price pr ON u.sku_name=pr.sku_name AND u.cloud=pr.cloud
  AND u.usage_start_time >= pr.price_start_time
  AND (pr.price_end_time IS NULL OR u.usage_start_time < pr.price_end_time)
WHERE u.billing_origin_product = 'MODEL_SERVING'
  AND u.usage_date >= current_date() - INTERVAL 30 DAYS
GROUP BY 1 ORDER BY list_usd DESC NULLS LAST LIMIT 20
""",
            },
            {
                "id": "pay_per_token",
                "title": "Pay-per-token usage by endpoint (30 days)",
                "eli5": "Foundation-model endpoints billed by the token — input vs output tokens and the "
                        "requests behind them. Output tokens usually cost several × input.",
                "chart": "table",
                "anomaly": {"type": "rank_outlier", "value_col": "total_tokens",
                            "label_col": "endpoint_name", "k": 4.0, "unit": "tokens"},
                "sql": """
WITH ent AS (   -- served_entities carries the endpoint name; endpoint_usage only has served_entity_id
  SELECT served_entity_id, max(endpoint_name) AS endpoint_name
  FROM system.serving.served_entities GROUP BY 1
)
SELECT coalesce(e.endpoint_name, 'unknown')          AS endpoint_name,
       count(*)                                      AS requests,
       sum(u.input_token_count)                      AS input_tokens,
       sum(u.output_token_count)                     AS output_tokens,
       sum(u.input_token_count + u.output_token_count) AS total_tokens
FROM system.serving.endpoint_usage u
LEFT JOIN ent e ON u.served_entity_id = e.served_entity_id
WHERE u.request_time >= current_date() - INTERVAL 30 DAYS
GROUP BY 1 ORDER BY total_tokens DESC NULLS LAST LIMIT 25
""",
            },
            {
                "id": "provisioned",
                "title": "Provisioned-throughput endpoints (weekly $)",
                "eli5": "Dedicated endpoints that bill for reserved compute-time whether or not they're "
                        "busy — the model-serving equivalent of an always-on warehouse.",
                "chart": "line",
                "anomaly": {"type": "ts_spike", "time_col": "week", "value_col": "list_usd",
                            "group_col": "endpoint", "k": 2.0, "min_abs": 2000,
                            "min_delta": 1000, "min_delta_high": 10000, "unit": "usd"},
                "sql": """
WITH price AS (
  SELECT sku_name, cloud, price_start_time, price_end_time,
         pricing.effective_list.default AS unit_usd FROM system.billing.list_prices
)
SELECT date_trunc('WEEK', u.usage_date)                          AS week,
       coalesce(u.usage_metadata.endpoint_name, 'not populated') AS endpoint,
       round(sum(u.usage_quantity * pr.unit_usd), 2)             AS list_usd
FROM system.billing.usage u
JOIN price pr ON u.sku_name=pr.sku_name AND u.cloud=pr.cloud
  AND u.usage_start_time >= pr.price_start_time
  AND (pr.price_end_time IS NULL OR u.usage_start_time < pr.price_end_time)
WHERE u.billing_origin_product = 'MODEL_SERVING'
  AND u.product_features.serving_type = 'FOUNDATION_MODEL'
  AND u.usage_type = 'COMPUTE_TIME'
  AND u.product_features.model_serving.offering_type <=> 'PROVISIONED_THROUGHPUT'
  AND u.usage_date >= current_date() - INTERVAL 8 WEEKS
  AND u.usage_date < date_trunc('WEEK', current_date())   -- complete weeks only
GROUP BY 1, 2 HAVING sum(u.usage_quantity * pr.unit_usd) > 0
ORDER BY 1, list_usd DESC
""",
            },
            {
                "id": "ai_query_cost",
                "title": "Top 10 most expensive ai_query() calls (14 days)",
                "eli5": "The individual SQL ai_query() statements that cost the most — who ran each, "
                        "which model it called, and its attributed cost. Click a statement to open it "
                        "in Query History.",
                "chart": "table",
                "note": "Model is parsed from the ai_query() call's first argument. Cost = the "
                        "statement's share of its warehouse-hour $, by execution-time (same method as "
                        "the DBSQL board) — it's the SQL-compute cost of running the call, not a "
                        "separate model-serving token charge.",
                "anomaly": {"type": "rank_outlier", "value_col": "query_usd",
                            "label_col": "model_endpoint", "k": 4.0, "unit": "usd"},
                "sql": r"""
WITH wh_hour AS (
  SELECT warehouse_id, hour, sum(usage_quantity * unit_usd) AS hour_usd FROM (
    SELECT u.usage_metadata.warehouse_id AS warehouse_id,
           date_trunc('HOUR', u.usage_start_time) AS hour,
           u.usage_quantity, lp.pricing.effective_list.default AS unit_usd
    FROM system.billing.usage u JOIN system.billing.list_prices lp
      ON u.sku_name=lp.sku_name AND u.cloud=lp.cloud
     AND u.usage_start_time>=lp.price_start_time AND (lp.price_end_time IS NULL OR u.usage_start_time<lp.price_end_time)
    WHERE u.usage_metadata.warehouse_id IS NOT NULL AND u.usage_date >= current_date() - INTERVAL 14 DAYS
    QUALIFY ROW_NUMBER() OVER (PARTITION BY u.record_id ORDER BY lp.price_start_time DESC)=1
  ) GROUP BY 1,2
),
cfg AS (SELECT warehouse_id, max_clusters, change_time AS vf,
          coalesce(lead(change_time) OVER (PARTITION BY warehouse_id ORDER BY change_time), TIMESTAMP '9999-12-31') AS vt
        FROM system.compute.warehouses),
ws AS (SELECT workspace_id, max(workspace_url) AS workspace_url FROM system.access.workspaces_latest GROUP BY 1),
q AS (
  SELECT statement_id, workspace_id, compute.warehouse_id AS warehouse_id, date_trunc('HOUR', start_time) AS hour, executed_by,
         lower(regexp_extract(statement_text, "ai_query\\(\\s*'([^']+)'", 1)) AS model_endpoint,
         execution_duration_ms,
         sum(execution_duration_ms) OVER (PARTITION BY compute.warehouse_id, date_trunc('HOUR', start_time)) AS busy_ms
  FROM system.query.history
  WHERE start_time >= current_date() - INTERVAL 14 DAYS AND execution_duration_ms > 0
    AND statement_text ILIKE '%ai_query(%'
),
alloc AS (
  SELECT q.*, w.hour_usd, GREATEST(coalesce(c.max_clusters,1)*3600000.0, q.busy_ms) AS capacity_ms
  FROM q JOIN wh_hour w USING (warehouse_id, hour)
         LEFT JOIN cfg c ON c.warehouse_id=q.warehouse_id AND q.hour>=c.vf AND q.hour<c.vt
)
SELECT a.statement_id, a.executed_by,
       CASE WHEN a.model_endpoint = '' OR a.model_endpoint IS NULL THEN '(not detected)' ELSE a.model_endpoint END AS model_endpoint,
       round(a.execution_duration_ms/60000.0, 2)                      AS exec_min,
       round(a.hour_usd * a.execution_duration_ms / a.capacity_ms, 2)     AS query_usd,
       ws.workspace_url   -- the workspace this statement actually ran in
FROM alloc a LEFT JOIN ws ON a.workspace_id = ws.workspace_id
ORDER BY query_usd DESC NULLS LAST LIMIT 10
""",
            },
        ],
    },
]


def board_meta(b: dict) -> dict:
    """Public board descriptor (no SQL) for the frontend."""
    return {
        "id": b["id"], "label": b["label"], "icon": b["icon"], "blurb": b["blurb"],
        "panels": [{"id": p["id"], "title": p["title"], "eli5": p["eli5"],
                    "chart": p["chart"], "has_anomaly": bool(p.get("anomaly")),
                    "note": p.get("note")}
                   for p in b["panels"]],
    }


def all_meta() -> list:
    return [board_meta(b) for b in BOARDS]


def find_panel(board_id: str, panel_id: str):
    b = next((x for x in BOARDS if x["id"] == board_id), None)
    if not b:
        return None, None
    p = next((x for x in b["panels"] if x["id"] == panel_id), None)
    return b, p
