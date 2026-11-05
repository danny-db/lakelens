"""
hunts.py — the Cost Optimiser's catalogue of "hunts" and "measures".

Every SQL string here is transcribed VERBATIM from the live-validated
`weekly-cost-hunt` notebook  The notebook
runs entirely on the customer's OWN system.* tables — so does this app.
Do not "improve" the SQL: it is byte-for-byte the validated version, and
the notebook is the source of truth. If a hunt needs changing, change it
in the notebook first, re-validate, then copy it here.

Each hunt is one card in the app. The prose fields (eli5, what, action)
are plain-English explanations written for a reader who has never seen a
system table — no jargon, no analogies, just what it means.

`AI_ENDPOINT` is substituted into the explainer prompt at request time.
"""
from typing import Literal, Optional, Union

from typing_extensions import TypedDict


# The six plain-English categories the hunts are grouped into on the Spot page.
# label = what the customer sees; blurb = one ELI5 line under the category header.
CATEGORIES = [
    {
        "id": "ai",
        "label": "AI & Model Serving",
        "icon": "\U0001F916",  # robot
        "blurb": "AI calls (ai_query, model endpoints) can spend faster than the daily "
                 "bill updates. This category watches them by the hour so a runaway "
                 "gets caught the same day, not on next month's invoice.",
    },
    {
        "id": "idle",
        "label": "Idle Compute",
        "icon": "\U0001F4A4",  # zzz
        "blurb": "Compute you are paying for while it does little or no work — warehouses "
                 "that never switch off, clusters running at a fraction of their capacity, "
                 "or background tools that keep the lights on around the clock.",
    },
    {
        "id": "reads",
        "label": "Heavy Reads & Spill",
        "icon": "\U0001F4D6",  # open book
        "blurb": "Queries that read far more data than they need, or run out of memory and "
                 "spill to disk. Both make jobs slower and more expensive than they should be.",
    },
    {
        "id": "longrun",
        "label": "Long-Running & Repetitive",
        "icon": "⏱️",  # stopwatch
        "blurb": "Statements that run for hours, or the same small query fired thousands of "
                 "times. Individually easy to miss; together they add up to real money.",
    },
    {
        "id": "config",
        "label": "Right-Sizing & Config",
        "icon": "\U0001F4CF",  # ruler
        "blurb": "Are your warehouses and clusters the right size and shape for the work? "
                 "This category surfaces the settings — size, auto-stop, cluster count, "
                 "queue pressure — so you can match capacity to demand.",
    },
    {
        "id": "spend",
        "label": "Spend Trends & Attribution",
        "icon": "\U0001F4C8",  # chart up
        "blurb": "Where the money goes and which way it is trending — by product, by job, "
                 "by pipeline — so a rising cost is spotted early and traced to an owner.",
    },
]

# ── The hunts (Spot page) ────────────────────────────────────────────────────
# Fields:
#   id, category, title  — identity + grouping
#   eli5                 — one plain sentence: what this hunt looks for
#   what                 — what a row means / how to read the result
#   action               — the recommended fix, in plain English
#   measure              — which Prove-page measure proves the fix worked
#   sql                  — the exact hunt query (verbatim from the notebook)
#   explain_rubric       — the interpretation rubric fed to the AI explainer
#   explain_hunt_desc    — short description of the hunt for the AI prompt
#   caution              — optional warning banner (e.g. H15 reads customer data)

# ── The "versus its own normal" columns every anomaly hunt returns ───────────
# Each anomaly hunt computes, per row, `own_normal` (the entity's statistic over
# the prior 28 days, NULL when it has none) and `ws_normal` (the same statistic
# across every entity: the workspace's own floor, so a tiny normal can't turn
# noise into a huge multiple). These two helpers turn that into the contract
# ALERT_SPECS grades on. An entity with no history is NEW (vs_normal NULL) only
# when it is at least the workspace normal; a smaller newcomer is scored against
# the workspace normal instead, so it can't raise an alert on its own.
def _vs_normal(metric: str) -> str:
    new = f"(own_normal IS NULL AND {metric} > 0 AND {metric} >= coalesce(ws_normal, 0))"
    base = "CASE WHEN own_normal IS NOT NULL THEN greatest(own_normal, ws_normal) ELSE ws_normal END"
    return (f"CASE WHEN {new} THEN NULL ELSE {base} END AS baseline,\n"
            f"       CASE WHEN {new} THEN NULL\n"
            f"            ELSE coalesce(round(try_divide({metric}, {base}), 2), 0) END AS vs_normal,\n"
            f"       CASE WHEN {new} THEN 'none (new)'\n"
            f"            WHEN own_normal IS NULL THEN 'workspace (no own history)'\n"
            f"            WHEN own_normal >= ws_normal THEN 'own' ELSE 'workspace floor' END AS baseline_from")


def _most_unusual(metric: str) -> str:
    """ORDER BY key: most times its normal first; a NEW row ranks by its multiple of
    the workspace normal."""
    return f"coalesce(vs_normal, try_divide({metric}, ws_normal)) DESC NULLS LAST"


# ── The account-versus-its-own-normal columns every anomaly hunt returns ──────
# Windows are trailing 7-day periods counted back from now, so the current window
# (wk 0) and the four before it (wk 1-4, the prior 28 days) are the same length
# and none is a partial calendar week.
def _wk(ts: str) -> str:
    """Window index for a timestamp: 0 = the last 7 x 24 hours, 1-4 = the 28 days before."""
    return f"floor((unix_timestamp(current_timestamp()) - unix_timestamp({ts})) / 604800)"


def _wk_day(day: str) -> str:
    """Window index for a daily column, over complete days only (filter `{day} < current_date()`)."""
    return f"floor((datediff(current_date(), {day}) - 1) / 7)"


def _account_normal(weekly: str) -> str:
    """CTE `acct` from a CTE of (wk, v), the account metric per window. A window
    with no rows counts as 0, so a quiet week pulls the median down honestly."""
    return f"""acct AS (
  SELECT coalesce(max(CASE WHEN s.wk = 0 THEN a.v END), 0)      AS account_value,
         percentile(CASE WHEN s.wk > 0 THEN coalesce(a.v, 0) END, 0.5) AS normal
  FROM (SELECT explode(sequence(0, 4)) AS wk) s LEFT JOIN {weekly} a USING (wk)
)"""


def _vs_account(metric: str) -> str:
    """SELECT columns: the contributor's share plus the account columns (FROM ... CROSS JOIN acct)."""
    return (f"round(100 * {metric} / nullif(acct.account_value, 0), 1) AS share_pct,\n"
            f"       acct.account_value,\n"
            f"       nullif(acct.normal, 0) AS baseline,\n"
            f"       round(try_divide(acct.account_value, nullif(acct.normal, 0)), 2) AS vs_normal")


HUNTS = [
    # ── H0 · AI ──────────────────────────────────────────────────────────────
    {
        "id": "H0",
        "category": "ai",
        "title": "Runaway AI spend",
        "eli5": "Finds AI model calls that are spending fast right now — within the "
                "hour, before the daily bill even updates.",
        "what": "Each row is one hour of AI usage for a model endpoint and the identity "
                "that called it, set against that same (endpoint, caller) pair's own busy hour: "
                "the 95th percentile of its active hours over the prior 28 days. vs_normal is how "
                "many times that normal this hour's tokens are, and that sets the alert. A pair "
                "running several times its own busy hour is your runaway. "
                "“est_usd_rough” is an estimate built from your own recent bill "
                "— read it as a relative signal (“who spiked”), not an exact invoice.",
        "action": "Layer three controls: (1) a SQL alert on the hourly rate so a spike is "
                  "caught fast; (2) a daily billing-dollar alert as the reconciling truth; "
                  "(3) a monthly budget as a coarse net. Prevent repeats by not granting "
                  "expensive models (Sonnet/Opus-class) to every pipeline by default — "
                  "default to a cheaper tier (e.g. Haiku), cap max_tokens, and only run the "
                  "model over NEW rows each refresh, never the whole table again.",
        "measure": "M2",
        "explain_hunt_desc": "hourly token-rate leaders for FMAPI / ai_query() calls "
                             "(runaway-spend early warning)",
        "explain_rubric": (
            "each row is one hour for an (endpoint, requester) pair. vs_normal = tokens / baseline, "
            "where baseline is that pair's 95th-percentile active hour over the prior 28 days "
            "(floored at the workspace's 95th-percentile pair-hour; baseline_from says which). A pair "
            "several times its own busy hour is the runaway; NULL vs_normal = a pair first seen in "
            "this window. "
            "requester_type = service principal means an automated pipeline. "
            "est_usd_rough is a rate-blended estimate (this window's tokens x the "
            "account's own 7-day effective $/1M-token rate), read it relative, not as "
            "an invoice. source = AI_QUERY marks a SQL ai_query() call."
        ),
        "sql": f"""
WITH billing_dollars AS (
  SELECT u.usage_metadata.endpoint_name AS model,
         sum(u.usage_quantity * p.pricing.default) AS usd
  FROM system.billing.usage u
  JOIN system.billing.list_prices p
    ON u.sku_name = p.sku_name
   AND u.usage_end_time BETWEEN p.price_start_time AND coalesce(p.price_end_time, now())
  WHERE u.billing_origin_product = 'MODEL_SERVING'
    AND u.usage_type = 'TOKEN'
    AND u.usage_date >= current_date() - 7
  GROUP BY 1
),
gateway_tokens AS (
  SELECT endpoint_name AS model, sum(total_tokens) AS tok
  FROM system.ai_gateway.usage
  WHERE event_time >= now() - INTERVAL 7 DAYS
  GROUP BY 1
),
rate AS (
  SELECT b.model, b.usd / (g.tok / 1e6) AS usd_1m
  FROM billing_dollars b JOIN gateway_tokens g USING (model)
  WHERE g.tok > 50e6
),
prior AS (
  SELECT endpoint_name, requester, date_trunc('hour', event_time) AS hr,
         sum(input_tokens + output_tokens) AS tok
  FROM system.ai_gateway.usage
  WHERE event_time >= now() - INTERVAL 28 DAYS - INTERVAL 3 HOURS
    AND event_time <  now() - INTERVAL 3 HOURS
  GROUP BY 1, 2, 3
),
own AS (SELECT endpoint_name, requester, percentile_approx(tok, 0.95) AS own_normal FROM prior GROUP BY 1, 2),
ws AS (SELECT percentile_approx(tok, 0.95) AS ws_normal FROM prior),
cur AS (
  SELECT date_trunc('hour', u.event_time)        AS hr,
         u.invocation_metadata.source            AS source,
         u.endpoint_name, u.requester, u.requester_type,
         sum(u.input_tokens)                      AS in_tok,
         sum(u.output_tokens)                     AS out_tok,
         round(sum(u.input_tokens + u.output_tokens) / 1e6 * max(r.usd_1m), 2) AS est_usd_rough
  FROM system.ai_gateway.usage u
  LEFT JOIN rate r ON u.endpoint_name = r.model
  WHERE u.event_time >= now() - INTERVAL 3 HOURS
  GROUP BY 1,2,3,4,5
),
graded AS (
  SELECT c.*, c.in_tok + c.out_tok AS tokens, o.own_normal, w.ws_normal
  FROM cur c LEFT JOIN own o ON c.endpoint_name <=> o.endpoint_name AND c.requester <=> o.requester
  CROSS JOIN ws w
)
SELECT hr, source, endpoint_name, requester, requester_type, in_tok, out_tok, est_usd_rough,
       endpoint_name || ' / ' || coalesce(requester, 'unknown caller') AS caller,
       tokens,
       {_vs_normal("tokens")}
FROM graded
ORDER BY {_most_unusual("tokens")}
LIMIT 20
""",
    },

    # ── H1 · reads ─────────────────────────────────────────────────────────────
    {
        "id": "H1",
        "category": "reads",
        "title": "Read amplification",
        "eli5": "Finds MERGE statements that read enormously more data than they write "
                "— an “incremental” job quietly re-scanning everything.",
        "what": "Each row is one MERGE target (or the job, notebook or person behind it) "
                "with its heaviest run this week, set against the bytes a run of that same "
                "MERGE usually reads. vs_normal is how many times its usual this run read, "
                "and that sets the alert: an incremental MERGE that suddenly reads several "
                "times its normal has started re-scanning. A high read-to-write ratio says "
                "the same thing in another way. If the target table is small but the reads "
                "are huge, the cost is upstream (the source query has no date filter). If "
                "reads are about twice the target size, the merge is scanning the whole "
                "target every run.",
        "action": "Bound the source with an incremental watermark so it only reads new "
                  "rows; bound the target with incremental_predicates on a date column; "
                  "cluster the target on that same column so old data can be skipped; and "
                  "update only the rows that changed.",
        "measure": "M4",
        "explain_hunt_desc": "MERGE statements that read far more than they write (read amplification)",
        "explain_rubric": (
            "each row is a MERGE target's heaviest run this week. vs_normal = read_tib / baseline, "
            "where baseline is the median bytes a run of the same MERGE read over the prior 28 days "
            "(floored at the workspace's 95th-percentile MERGE; baseline_from says which). A NULL vs_normal "
            "means a MERGE first seen this week. A run several times its own normal has started "
            "re-scanning; read_write_ratio corroborates it. Then compare read_tib per "
            "run against the target's actual size (DESCRIBE DETAIL <target> -> sizeInBytes, "
            "see H15): if the target is small but reads are huge, the cost is upstream (the "
            "source query); if reads are approximately 2x the target size, the merge scans "
            "the whole target in both its match and rewrite phases."
        ),
        "sql": f"""
WITH merges AS (
  SELECT statement_id, executed_by, start_time, read_bytes, written_bytes, total_duration_ms,
         read_bytes/POW(1024,4) AS read_tib,
         coalesce(nullif(regexp_extract(statement_text, '(?i)MERGE\\\\s+INTO\\\\s+([`\\\\w.]+)', 1), ''),
                  'job ' || query_source.job_info.job_id, 'notebook ' || query_source.notebook_id,
                  executed_by) AS merge_target
  FROM system.query.history
  WHERE statement_type = 'MERGE' AND read_bytes > 0
    AND start_time >= current_date() - INTERVAL 35 DAYS
),
prior AS (SELECT * FROM merges WHERE start_time < current_date() - INTERVAL 7 DAYS),
own AS (SELECT merge_target, percentile_approx(read_tib, 0.5) AS own_normal FROM prior GROUP BY 1),
ws AS (SELECT percentile_approx(read_tib, 0.95) AS ws_normal FROM prior),
cur AS (
  SELECT m.*, o.own_normal, w.ws_normal
  FROM merges m LEFT JOIN own o USING (merge_target) CROSS JOIN ws w
  WHERE m.start_time >= current_date() - INTERVAL 7 DAYS
  QUALIFY row_number() OVER (PARTITION BY m.merge_target ORDER BY m.read_bytes DESC) = 1
)
SELECT statement_id, executed_by, merge_target, read_tib,
       written_bytes/POW(1024,3)            AS written_gib,
       CAST(read_bytes AS DOUBLE)/NULLIF(written_bytes,0) AS read_write_ratio,
       total_duration_ms/60000.0            AS mins,
       {_vs_normal("read_tib")}
FROM cur
ORDER BY {_most_unusual("read_tib")}
LIMIT 20
""",
    },

    # ── H2 · reads ─────────────────────────────────────────────────────────────
    {
        "id": "H2",
        "category": "reads",
        "title": "Disk spill",
        "eli5": "Finds queries that ran out of memory and had to write working data to "
                "disk — far slower, and you pay for the extra time.",
        "what": "Each row is one source of spilling statements (a job, notebook, saved query, "
                "dashboard or person) with its worst spill this week, set against how much a "
                "spilling statement from that source usually spills. vs_normal is how many times "
                "its usual, and that sets the alert. "
                "Compare spill to the input size. Spill much LARGER than the input (we have "
                "seen 46×) is a query-shape problem — a CTE recomputed many times, "
                "de-duplication done after a join instead of before, or an unbounded lookback. "
                "Spill roughly equal to the input is a sizing problem.",
        "action": "Rewrite first — shorten lookbacks, stage a re-used CTE into a temp "
                  "table, and de-duplicate the small side before joins. Only if a clean query "
                  "still spills should you increase the warehouse/cluster size. Open the worst "
                  "offender's query profile to see which step spills.",
        "measure": "M4",
        "explain_hunt_desc": "statements spilling intermediate data to disk (memory pressure)",
        "explain_rubric": (
            "each row is a source's worst-spilling statement this week. vs_normal = spill_tib / "
            "baseline, where baseline is the source's median spill per spilling statement over the "
            "prior 28 days (floored at the workspace median; baseline_from says which). NULL "
            "vs_normal = a source first seen spilling this week. "
            "compare spill_tib to read_gib. Spill far larger than the input (we saw 46x) is a "
            "query-shape problem: re-used CTEs recomputed per reference, deduplication after a "
            "join instead of before, unbounded lookback windows. Spill roughly proportional to "
            "input on a well-shaped query is a sizing problem."
        ),
        "sql": f"""
WITH spills AS (
  SELECT statement_id, start_time, executed_by, read_bytes, total_duration_ms,
         spilled_local_bytes/POW(1024,4) AS spill_tib,
         coalesce('job ' || query_source.job_info.job_id, 'notebook ' || query_source.notebook_id,
                  'query ' || query_source.sql_query_id, 'dashboard ' || query_source.dashboard_id,
                  executed_by) AS source
  FROM system.query.history
  WHERE spilled_local_bytes > 0
    AND start_time >= current_date() - INTERVAL 35 DAYS
),
prior AS (SELECT * FROM spills WHERE start_time < current_date() - INTERVAL 7 DAYS),
own AS (SELECT source, percentile_approx(spill_tib, 0.5) AS own_normal FROM prior GROUP BY 1),
ws AS (SELECT percentile_approx(spill_tib, 0.5) AS ws_normal FROM prior),
cur AS (
  SELECT s.*, o.own_normal, w.ws_normal
  FROM spills s LEFT JOIN own o USING (source) CROSS JOIN ws w
  WHERE s.start_time >= current_date() - INTERVAL 7 DAYS
  QUALIFY row_number() OVER (PARTITION BY s.source ORDER BY s.spill_tib DESC) = 1
)
SELECT statement_id, start_time, executed_by, source, spill_tib,
       read_bytes/POW(1024,3)               AS read_gib,
       total_duration_ms/60000.0            AS mins,
       {_vs_normal("spill_tib")}
FROM cur
ORDER BY {_most_unusual("spill_tib")}
LIMIT 20
""",
    },

    # ── H3 · idle ──────────────────────────────────────────────────────────────
    {
        "id": "H3",
        "category": "idle",
        "title": "Chatter keeps warehouses awake",
        "eli5": "Finds tools that fire tiny, do-nothing statements around the clock — "
                "each one resets the auto-stop timer so the warehouse never sleeps.",
        "what": "“client_application” and “executed_by” name the tool exactly "
                "— this is configuration, not guesswork. “stmts_per_session near 1” "
                "means the tool opens a fresh connection for every statement. Each row is one "
                "client this week, set against its own usual week (the median of its prior four "
                "weeks); vs_normal is how many times that usual it fired, and that sets the alert. "
                "A client that suddenly fires several times its normal chatter is the new cause of "
                "a warehouse that won't sleep.",
        "action": "Fix the tool first — e.g. a BI tool's refresh schedule left at its "
                  "aggressive default, or a monitoring tool that should have its own small "
                  "warehouse. Then move always-on tools onto a small serverless warehouse where "
                  "a short auto-stop is practical.",
        "measure": "M3",
        "explain_hunt_desc": "chatty clients issuing tiny zero-read statements that keep warehouses awake",
        "explain_rubric": (
            "client_application and executed_by name the tool - it's configuration, not "
            "guesswork. stmts_per_session near 1 means the client opens a fresh connection per "
            "statement (pool churn). vs_normal = stmts / baseline, where baseline is the client's "
            "median week over the prior four weeks (floored at the workspace's 95th-percentile client-week; "
            "baseline_from says which). A client several times its own normal is the pattern; NULL "
            "vs_normal = a client first seen this week."
        ),
        "sql": f"""
WITH wh AS (SELECT warehouse_id, warehouse_name FROM system.compute.warehouses QUALIFY ROW_NUMBER() OVER (PARTITION BY warehouse_id ORDER BY change_time DESC)=1 AND delete_time IS NULL),
weekly AS (
  SELECT compute.warehouse_id, statement_type, executed_by, client_application, client_driver,
         concat_ws('|', compute.warehouse_id, statement_type, executed_by,
                   coalesce(client_application, ''), coalesce(client_driver, '')) AS k,
         floor((unix_timestamp(current_timestamp()) - unix_timestamp(start_time)) / 604800) AS wk,
         count(*) AS stmts, count(DISTINCT session_id) AS sessions,
         round(count(*)/count(DISTINCT session_id),1) AS stmts_per_session
  FROM system.query.history
  WHERE read_bytes = 0 AND total_duration_ms < 30000
    AND start_time >= current_timestamp() - INTERVAL 35 DAYS
  GROUP BY 1,2,3,4,5,6,7
),
own AS (SELECT k, percentile_approx(stmts, 0.5) AS own_normal FROM weekly WHERE wk BETWEEN 1 AND 4 GROUP BY 1),
ws AS (SELECT percentile_approx(stmts, 0.95) AS ws_normal FROM weekly WHERE wk BETWEEN 1 AND 4),
q AS (
  SELECT c.*, o.own_normal, w.ws_normal
  FROM weekly c LEFT JOIN own o USING (k) CROSS JOIN ws w
  WHERE c.wk = 0
)
SELECT coalesce(wh.warehouse_name, q.warehouse_id) AS warehouse, q.statement_type, q.executed_by,
       q.client_application, q.client_driver, q.stmts, q.sessions, q.stmts_per_session,
       coalesce(q.client_application, q.client_driver, 'unknown client') || ' (' || q.executed_by
         || ') on ' || coalesce(wh.warehouse_name, q.warehouse_id, 'a cluster') AS client,
       {_vs_normal("stmts")}
FROM q LEFT JOIN wh ON q.warehouse_id = wh.warehouse_id
ORDER BY {_most_unusual("stmts")}
LIMIT 50
""",
    },

    # ── H4 · idle ──────────────────────────────────────────────────────────────
    {
        "id": "H4",
        "category": "idle",
        "title": "Warehouses that never sleep",
        "eli5": "Shows which warehouses have no ‘stopping’ events — a signal "
                "they may be billing continuously instead of auto-stopping when idle.",
        "what": "For each warehouse, look for the presence and recency of STOPPING / STOPPED "
                "events. No STOPPING over two weeks is a signal to investigate — NOT proof "
                "of waste. It might genuinely run 24/7 for real load. Confirm with its actual "
                "billed usage (M3) before acting.",
        "action": "If a warehouse never stops AND a chatty client is hitting it (see the "
                  "Chatter hunt), fix the client or move it to its own small serverless "
                  "warehouse. If it never stops because of genuine round-the-clock load, that "
                  "is fine — just confirm the size matches the load (see Queue Pressure).",
        "measure": "M3",
        "explain_hunt_desc": "warehouse auto-stop events over 14 days (warehouses that may never sleep)",
        "explain_rubric": (
            "each row is a warehouse + an event_type with a count and last_seen. A warehouse "
            "with STARTING/RUNNING events but NO STOPPING/STOPPED rows may be billing continuously "
            "- that is a signal to investigate, NOT proof of waste (it may run 24/7 for real load). "
            "Call out warehouses that appear to start but never stop; do not assert waste - recommend "
            "confirming against actual billed usage."
        ),
        "inventory": True,  # lists all warehouses' events — a reference view, not a waste list
        "sql": """
WITH wh AS (SELECT warehouse_id, warehouse_name FROM system.compute.warehouses QUALIFY ROW_NUMBER() OVER (PARTITION BY warehouse_id ORDER BY change_time DESC)=1 AND delete_time IS NULL)
SELECT coalesce(wh.warehouse_name, e.warehouse_id) AS warehouse, e.event_type,
       count(*) AS events, max(e.event_time) AS last_seen
FROM system.compute.warehouse_events e
LEFT JOIN wh ON e.warehouse_id = wh.warehouse_id
WHERE e.event_time >= current_date() - INTERVAL 14 DAYS
GROUP BY 1, 2
ORDER BY 1, 2
""",
    },

    # ── H5 · longrun ─────────────────────────────────────────────────────────────
    {
        "id": "H5",
        "category": "longrun",
        "title": "Runaway statements (over 4 hours)",
        "eli5": "Finds any statement that ran longer than four hours, and splits its time "
                "into waiting-for-capacity vs starting-up vs actually-running.",
        "what": "Each row is a statement that ran over four hours this week, set against the "
                "longest statement its source (job, notebook, saved query, or person) usually runs "
                "in a day. vs_normal is how many times that usual length this one ran, and that "
                "sets the alert: a nightly six-hour job is normal for itself, while a four-hour "
                "statement from a source that usually finishes in minutes is not. "
                "The split tells you which of three problems you have. Long execution time "
                "plus a huge read = a genuine heavy scan. Long execution time plus tiny reads "
                "= a HUNG statement (we found two that ‘ran’ 21.7 hours while reading "
                "6 MB). A large queued time = a capacity problem (see Queue Pressure).",
        "action": "Set a workspace-wide statement timeout (say 2–4 hours) so runaways stop "
                  "themselves — the platform default is TWO DAYS. Long ETL can raise its own "
                  "limit per session. Note: changing the workspace setting restarts running "
                  "warehouses, so schedule it.",
        "measure": "M5",
        "explain_hunt_desc": "statements running over 4 hours, split into queued vs executing",
        "explain_rubric": (
            "vs_normal = total_min / baseline, where baseline is the source's median daily longest "
            "statement over the prior 28 days (floored at the workspace's 95th percentile; baseline_from says "
            "which). A statement near 1x is that source's normal long run; a high multiple is a "
            "runaway or a hang. NULL vs_normal = a source first seen this week. "
            "long exec_min + huge read_gib = a genuine heavy scan (see H6). Long exec_min + tiny "
            "reads = a hung statement (we found two that executed 21.7 h while reading 6 MB - "
            "check execution_status and the query profile). Large queued_min = a capacity problem (see H8)."
        ),
        "sql": f"""
WITH stmts AS (
  SELECT statement_id, start_time, executed_by, total_duration_ms, waiting_at_capacity_duration_ms,
         waiting_for_compute_duration_ms, execution_duration_ms, read_files, read_bytes,
         coalesce('job ' || query_source.job_info.job_id, 'notebook ' || query_source.notebook_id,
                  'query ' || query_source.sql_query_id, 'dashboard ' || query_source.dashboard_id,
                  executed_by) AS source
  FROM system.query.history
  WHERE start_time >= current_date() - INTERVAL 35 DAYS
),
daily AS (
  SELECT source, date(start_time) AS day, max(total_duration_ms)/60000.0 AS longest_min
  FROM stmts WHERE start_time < current_date() - INTERVAL 7 DAYS
  GROUP BY 1, 2
),
own AS (SELECT source, percentile_approx(longest_min, 0.5) AS own_normal FROM daily GROUP BY 1),
ws AS (SELECT percentile_approx(longest_min, 0.95) AS ws_normal FROM daily),
cur AS (
  SELECT s.*, s.total_duration_ms/60000.0 AS total_min, o.own_normal, w.ws_normal
  FROM stmts s LEFT JOIN own o USING (source) CROSS JOIN ws w
  WHERE s.total_duration_ms > 4*3600*1000
    AND s.start_time >= current_date() - INTERVAL 7 DAYS
)
SELECT statement_id, start_time, source,
       total_min,
       waiting_at_capacity_duration_ms/60000.0 AS queued_min,
       waiting_for_compute_duration_ms/60000.0 AS provisioning_min,
       execution_duration_ms/60000.0           AS exec_min,
       read_files, read_bytes/POW(1024,3)      AS read_gib,
       {_vs_normal("total_min")}
FROM cur
ORDER BY {_most_unusual("total_min")}
""",
    },

    # ── H6 · reads ─────────────────────────────────────────────────────────────
    {
        "id": "H6",
        "category": "reads",
        "title": "Unusually heavy scan hours by one person",
        "eli5": "Finds an hour in which one person scanned far more data than they usually do "
                "in a busy hour, often an investigation re-scanning a raw table over and over.",
        "what": "Each row is one person's heaviest scanning hour this week, set against their "
                "own busy hour (the 95th percentile of their active hours over the prior 28 "
                "days). vs_normal is how many times that normal this hour read, and that sets "
                "the alert. Many scans in that hour (the scans column) is usually a single "
                "incident hunt: someone searching a raw event table with ‘LIKE %some-id%’ "
                "and re-paying the full scan on every refinement. Open the statements in Query "
                "History to confirm the shape.",
        "action": "Give investigators a two-line template: first, time-bound the opening scan "
                  "on the table's date column; second, stage that first pass into a temp table "
                  "and run every refinement against the small staged copy — five scans become "
                  "one. Longer term, promote the searched fields to real clustered columns, or "
                  "evaluate a full-text search index.",
        "measure": "M1",
        "explain_hunt_desc": "hours in which one principal scanned far more than their own normal busy hour (often a re-paid investigation scan)",
        "explain_rubric": (
            "each row is one principal's heaviest scanning hour this week. vs_normal = read_tib / "
            "baseline, where baseline is that principal's 95th-percentile active hour over the prior "
            "28 days (floored at the workspace's 95th-percentile person-hour; baseline_from says "
            "which). NULL vs_normal = someone first seen scanning this week. A high multiple with "
            "many scans in the hour is usually one incident hunt re-paying a full scan per "
            "refinement. Open the statements in the Query History UI to confirm the shape."
        ),
        "sql": f"""
WITH hourly AS (
  SELECT executed_by, date_trunc('HOUR', start_time) AS hour,
         count(*)                          AS scans,
         sum(read_bytes)/POW(1024,4)       AS read_tib,
         max(total_duration_ms)/3600000.0  AS longest_hours
  FROM system.query.history
  WHERE statement_type = 'SELECT' AND read_bytes > 0
    AND start_time >= current_date() - INTERVAL 35 DAYS
  GROUP BY 1, 2
),
prior AS (SELECT * FROM hourly WHERE hour < current_date() - INTERVAL 7 DAYS),
own AS (SELECT executed_by, percentile_approx(read_tib, 0.95) AS own_normal FROM prior GROUP BY 1),
ws AS (SELECT percentile_approx(read_tib, 0.95) AS ws_normal FROM prior),
cur AS (
  SELECT h.*, o.own_normal, w.ws_normal
  FROM hourly h LEFT JOIN own o USING (executed_by) CROSS JOIN ws w
  WHERE h.hour >= current_date() - INTERVAL 7 DAYS
  QUALIFY row_number() OVER (PARTITION BY h.executed_by ORDER BY h.read_tib DESC) = 1
)
SELECT executed_by, hour, scans, read_tib, longest_hours,
       {_vs_normal("read_tib")}
FROM cur
ORDER BY {_most_unusual("read_tib")}
LIMIT 50
""",
    },

    # ── H7 · longrun ─────────────────────────────────────────────────────────────
    {
        "id": "H7",
        "category": "longrun",
        "title": "Polling every 20 seconds",
        "eli5": "Finds one identity running the same short query far more often than it usually "
                "does, typically an orchestrator checking metadata more often than needed.",
        "what": "Each row is one identity and query source this week, set against its own usual "
                "week (the median of its prior four weeks). vs_normal is how many times that usual "
                "it ran, and that sets the alert. A group running several times its normal is a "
                "CANDIDATE poller, not a verdict — confirm before acting. ‘execution_hours’ is the total "
                "compute weight. The result names the culprit (a saved query, dashboard, or job) "
                "even when the SQL text is hidden. When all three are blank the row is labelled "
                "‘unattributed — confirm before acting’ because unrelated short "
                "queries can pile into one row.",
        "action": "Go event-driven — use a table-update trigger to run work only when the "
                  "source changes, instead of polling. Or write the freshness marker to a tiny "
                  "one-row table (a single-row read beats a metadata scan). Or simply reduce the "
                  "frequency to what downstream actually needs (minutes, not seconds).",
        "measure": "M3",
        "explain_hunt_desc": ("one principal running thousands of short SELECTs (candidate polling - "
                              "unattributed groups need confirmation, do not assert waste)"),
        "explain_rubric": (
            "vs_normal = execs / baseline, where baseline is the group's median week over the prior "
            "four weeks (floored at the workspace's 95th-percentile group-week; baseline_from says "
            "which). A group several times its own normal is a candidate poller, not a verdict - "
            "confirm before acting; NULL vs_normal = a group first seen this week. p50_exec_ms shows how expensive each poll is; "
            "execution_hours is the aggregate execution weight (execution only, excluding "
            "queue/provisioning). When all query_source IDs are NULL the group is unattributed - "
            "unrelated short SELECTs can collapse into one row, so do not assert it is a poller."
        ),
        "sql": f"""
WITH polls AS (
  SELECT executed_by, client_application,
         query_source.sql_query_id            AS sql_query_id,
         query_source.dashboard_id            AS dashboard_id,
         query_source.job_info.job_id         AS job_id,
         concat_ws('|', executed_by, coalesce(client_application, ''), coalesce(query_source.sql_query_id, ''),
                   coalesce(query_source.dashboard_id, ''), coalesce(query_source.job_info.job_id, '')) AS k,
         statement_id, execution_duration_ms, start_time
  FROM system.query.history
  WHERE statement_type = 'SELECT' AND total_duration_ms < 60000
    AND start_time >= current_timestamp() - INTERVAL 35 DAYS
),
prior AS (
  SELECT k, floor((unix_timestamp(current_timestamp()) - unix_timestamp(start_time)) / 604800) AS wk,
         count(*) AS execs
  FROM polls WHERE start_time < current_timestamp() - INTERVAL 7 DAYS
  GROUP BY 1, 2
),
own AS (SELECT k, percentile_approx(execs, 0.5) AS own_normal FROM prior GROUP BY 1),
ws AS (SELECT percentile_approx(execs, 0.95) AS ws_normal FROM prior),
cur AS (
  SELECT executed_by,
         CASE WHEN executed_by LIKE '%@%' THEN 'human' ELSE 'service_principal' END AS owner_type,
         client_application, sql_query_id, dashboard_id, job_id, k,
         count(*)                                       AS execs,
         approx_percentile(execution_duration_ms, 0.5)  AS p50_exec_ms,
         sum(execution_duration_ms)/3600000.0           AS execution_hours,
         any_value(statement_id)                        AS example_statement_id,
         slice(array_agg(statement_id), 1, 3)           AS more_example_ids,
         CASE WHEN coalesce(sql_query_id, dashboard_id, job_id) IS NULL
              THEN 'unattributed — confirm before acting'
              ELSE 'candidate — confirm in Query History' END    AS status
  FROM polls
  WHERE start_time >= current_timestamp() - INTERVAL 7 DAYS
  GROUP BY 1, 2, 3, 4, 5, 6, 7
),
graded AS (
  SELECT c.*, o.own_normal, w.ws_normal
  FROM cur c LEFT JOIN own o USING (k) CROSS JOIN ws w
)
SELECT executed_by, owner_type, client_application, sql_query_id, dashboard_id, job_id,
       execs, p50_exec_ms, execution_hours, example_statement_id, more_example_ids, status,
       coalesce('query ' || sql_query_id, 'dashboard ' || dashboard_id, 'job ' || job_id,
                client_application, 'unattributed') || ' (' || executed_by || ')' AS poller,
       {_vs_normal("execs")}
FROM graded
ORDER BY {_most_unusual("execs")}
LIMIT 50
""",
    },

    # ── H8 · config ─────────────────────────────────────────────────────────────
    {
        "id": "H8",
        "category": "config",
        "title": "Queue pressure vs capacity",
        "eli5": "Shows, per warehouse, how long queries waited for capacity next to how long "
                "they actually ran — the objective signal for right-sizing.",
        "what": "Each row is one warehouse this week with its queue wait set against its own "
                "usual week (the median of its prior four weeks that had any queueing). vs_normal "
                "is how many times that usual it waited, and that sets the alert. "
                "High queued time relative to execution time means the warehouse is undersized "
                "or its maximum cluster count is too low for peaks. Near-zero queueing with low "
                "utilisation means there is room to size DOWN. This is real demand, not perceived "
                "slowness.",
        "action": "Decide warehouse by warehouse, by load shape. Spiky, idle-heavy workloads "
                  "suit serverless (scales in seconds, stops in minutes). Steady, predictable "
                  "load can suit a right-sized classic warehouse with spot and committed-use "
                  "discounts. Keep different workload types on separate warehouses so one noisy "
                  "client can't queue everyone.",
        "measure": "M3",
        "explain_hunt_desc": "warehouses ranked by queue pressure vs execution time",
        "inventory": True,  # ranks every warehouse — a reference view, not a waste list
        "explain_rubric": (
            "vs_normal = queued_hours / baseline, where baseline is the warehouse's median queueing "
            "week over the prior four (floored at the workspace's 95th-percentile queueing warehouse-week; "
            "baseline_from says which). A warehouse several times its own normal queue has outgrown "
            "its size; NULL vs_normal = first time it has queued. "
            "high queued_hours relative to exec_hours means the warehouse is undersized or its "
            "max cluster count is too low for peaks. Zero queueing with low utilisation (H14 for "
            "clusters, warehouse monitoring for DBSQL) means there is room to downsize."
        ),
        "sql": f"""
WITH wh AS (SELECT warehouse_id, warehouse_name FROM system.compute.warehouses QUALIFY ROW_NUMBER() OVER (PARTITION BY warehouse_id ORDER BY change_time DESC)=1 AND delete_time IS NULL),
weekly AS (
  SELECT compute.warehouse_id,
         floor((unix_timestamp(current_timestamp()) - unix_timestamp(start_time)) / 604800) AS wk,
         count_if(waiting_at_capacity_duration_ms > 60000) AS stmts_queued_over_1m,
         sum(waiting_at_capacity_duration_ms)/3600000.0    AS queued_hours,
         sum(execution_duration_ms)/3600000.0              AS exec_hours
  FROM system.query.history
  WHERE start_time >= current_timestamp() - INTERVAL 35 DAYS
  GROUP BY 1, 2
),
prior AS (SELECT * FROM weekly WHERE wk BETWEEN 1 AND 4 AND queued_hours > 0),
own AS (SELECT warehouse_id, percentile_approx(queued_hours, 0.5) AS own_normal FROM prior GROUP BY 1),
ws AS (SELECT percentile_approx(queued_hours, 0.95) AS ws_normal FROM prior),
q AS (
  SELECT c.*, o.own_normal, w.ws_normal
  FROM weekly c LEFT JOIN own o ON c.warehouse_id <=> o.warehouse_id CROSS JOIN ws w
  WHERE c.wk = 0
)
SELECT coalesce(wh.warehouse_name, q.warehouse_id) AS warehouse,
       q.stmts_queued_over_1m, q.queued_hours, q.exec_hours,
       {_vs_normal("queued_hours")}
FROM q LEFT JOIN wh ON q.warehouse_id = wh.warehouse_id
ORDER BY {_most_unusual("queued_hours")}, queued_hours DESC
""",
    },

    # ── H9 · config ─────────────────────────────────────────────────────────────
    {
        "id": "H9",
        "category": "config",
        "title": "Warehouse config audit",
        "eli5": "Lists the current settings of every warehouse — type, size, cluster "
                "range, auto-stop — with the last time each one changed.",
        "what": "One row per warehouse. Look for a long auto-stop on interactive workloads, "
                "‘max_clusters = 1’ on a shared BI warehouse (no room to burst), and "
                "recent unexplained change times — the sign of config drift (a warehouse "
                "resized by hand with no audit trail).",
        "action": "Agree a baseline for each warehouse (type, size, auto-stop) and re-run this "
                  "audit weekly. Any difference from the baseline is a conversation, not a "
                  "surprise.",
        "measure": "M3",
        "explain_hunt_desc": "current warehouse configuration audit (type/size/clusters/auto-stop)",
        "explain_rubric": (
            "each row is one warehouse's live config. Flag the config smells: a long "
            "auto_stop_minutes on interactive workloads (money burned while idle), max_clusters=1 on a "
            "shared warehouse (no burst headroom), and very recent change_time values (possible "
            "undocumented config drift). Name the notable outliers; this is a reference audit, so "
            "frame findings as 'worth a conversation', not proven waste."
        ),
        "inventory": True,  # lists every warehouse's config — a reference view, not a waste list
        "sql": """
SELECT warehouse_id, warehouse_name, warehouse_type, warehouse_size,
       min_clusters, max_clusters, auto_stop_minutes, change_time
FROM system.compute.warehouses
QUALIFY ROW_NUMBER() OVER (PARTITION BY warehouse_id ORDER BY change_time DESC) = 1
   AND delete_time IS NULL
""",
    },

    # ── H10 · spend ─────────────────────────────────────────────────────────────
    {
        "id": "H10",
        "category": "spend",
        "title": "Spend by product (SKU)",
        "eli5": "Shows daily spend and usage for every Databricks product, so a rising "
                "cost trend is visible early.",
        "what": "Scan for products that appear or accelerate week over week. Usage (DBUs) and "
                "dollars can rank DIFFERENTLY — a high-usage product on a cheap rate can sit "
                "below a lower-usage product on an expensive one, so sort by dollars to see what "
                "actually moves the bill. Genuinely free products keep a blank dollar figure but "
                "stay visible, so nothing can hide.",
        "action": "Put budgets behind the trends — account-wide or scoped by workspace/tag, "
                  "with up to four alert thresholds each. For AI features like Genie, per-user "
                  "budgets add thresholds and an optional block switch.",
        "measure": "M2",
        "explain_hunt_desc": "weekly list-price $ (and DBUs) per billing SKU over the last ~4 weeks",
        "explain_rubric": (
            "The rows are weekly list-price $ (and DBUs) per billing SKU over the last ~4 weeks. "
            "In 3-4 neutral sentences: name the 2-3 SKUs that dominate list-price $, call out any "
            "SKU whose weekly list_usd is clearly ACCELERATING week over week (the early warning), "
            "and note that DBUs and $ can rank differently so a fix should target the $-heavy SKUs. "
            "Cite the figures; do NOT overstate cost; these are list-price DBU costs (pre-discount, "
            "pre-infra), read them relative."
        ),
        "explain_is_trend": True,  # this explainer takes a weekly roll-up prompt, not the row rubric
        "sql": """
WITH price AS (
  SELECT sku_name, cloud, price_start_time, price_end_time,
         pricing.effective_list.default AS unit_usd
  FROM system.billing.list_prices
)
SELECT u.usage_date, u.sku_name,
       sum(u.usage_quantity)                          AS dbus,
       round(sum(u.usage_quantity * pr.unit_usd), 2)  AS list_usd
FROM system.billing.usage u
LEFT JOIN price pr ON u.sku_name = pr.sku_name AND u.cloud = pr.cloud
  AND u.usage_start_time >= pr.price_start_time
  AND (pr.price_end_time IS NULL OR u.usage_start_time < pr.price_end_time)
WHERE u.usage_date >= current_date() - INTERVAL 30 DAYS
GROUP BY 1, 2
ORDER BY 1 DESC, list_usd DESC NULLS LAST
""",
        # explainer feeds a weekly roll-up (verbatim from notebook H10 explainer subquery)
        "explain_sql": """
WITH price AS (
  SELECT sku_name, cloud, price_start_time, price_end_time,
         pricing.effective_list.default AS unit_usd
  FROM system.billing.list_prices
)
SELECT date_trunc('WEEK', u.usage_date)              AS week,
       u.sku_name,
       round(sum(u.usage_quantity), 0)               AS dbus,
       round(sum(u.usage_quantity * pr.unit_usd), 2) AS list_usd
FROM system.billing.usage u
LEFT JOIN price pr ON u.sku_name = pr.sku_name AND u.cloud = pr.cloud
  AND u.usage_start_time >= pr.price_start_time
  AND (pr.price_end_time IS NULL OR u.usage_start_time < pr.price_end_time)
WHERE u.usage_date >= current_date() - INTERVAL 28 DAYS
GROUP BY 1, 2
HAVING sum(u.usage_quantity * pr.unit_usd) > 0
ORDER BY week DESC, list_usd DESC NULLS LAST
LIMIT 40
""",
    },

    # ── H11 · spend ─────────────────────────────────────────────────────────────
    {
        "id": "H11",
        "category": "spend",
        "title": "Jobs — weekly movers",
        "eli5": "Shows week-over-week usage per job, so you can tell a genuinely new "
                "workload apart from a cost that crept up with no story.",
        "what": "Rank the current week, then compare each mover's trajectory. A brand-new "
                "job appearing is expected growth — forecast it. An existing job trending "
                "up with no new work behind it is the optimisation target. A doubling bill is "
                "only a problem if it isn't new work.",
        "action": "Tag jobs consistently (team / domain / environment) so cost has an owner; "
                  "turn the daily thresholds you already watch into budget alerts; and review "
                  "the top movers monthly, applying read-amplification and spill analysis inside them.",
        "measure": "M2",
        "explain_hunt_desc": "weekly DBUs per job (spend movers)",
        "explain_rubric": (
            "rank the current week, then compare each mover's trajectory. usage_metadata also "
            "carries job_run_id and the run-as identity; custom_tags attribute to teams once jobs are tagged."
        ),
        "sql": """
SELECT workspace_id,
       usage_metadata.job_id,
       any_value(usage_metadata.job_name) AS job_name,
       date_trunc('WEEK', usage_date)     AS week,
       sum(usage_quantity)                AS dbus
FROM system.billing.usage
WHERE billing_origin_product = 'JOBS'
  AND usage_date >= current_date() - INTERVAL 8 WEEKS
GROUP BY workspace_id, usage_metadata.job_id, date_trunc('WEEK', usage_date)
ORDER BY week DESC, dbus DESC
LIMIT 100
""",
    },

    # ── H12 · spend ─────────────────────────────────────────────────────────────
    {
        "id": "H12",
        "category": "spend",
        "title": "Jobs — who got slower?",
        "eli5": "Shows average run time per job per week, so you can spot a job that is "
                "quietly getting slower over time.",
        "what": "A job whose duration creeps up while its input grows only linearly is "
                "accumulating an inefficiency — growing scans, spill, or the wrong compute "
                "size. Cross-check the risers against the weekly movers (are they also the cost "
                "movers?) and check their clusters for idle cores.",
        "action": "For each riser, look one level deeper: cluster utilisation during runs, then "
                  "the shuffle/spill per stage in the Spark UI. Right-size on evidence. Spot "
                  "instances suit retryable batch jobs — but not continuous pipelines, where "
                  "an eviction restarts work and costs more than it saves.",
        "measure": "M2",
        "explain_hunt_desc": "weekly p50/p95 job run-duration (jobs getting slower over time)",
        "explain_rubric": (
            "each row is a (workspace, job) with weekly run count and p50/p95 minutes. Look for a job "
            "whose p50/p95 duration creeps UP week over week - that is an accumulating inefficiency "
            "worth a look (growing scans, spill, or wrong compute size). A high run count alone is not "
            "waste. Name any clear risers; suggest cross-checking them against the weekly-movers hunt "
            "and idle-cores hunt. This is a reference trend, not proven waste."
        ),
        "inventory": True,  # lists all jobs by run count — a reference view, not a waste list
        "sql": """
WITH runs AS (
  SELECT workspace_id, job_id, run_id,
         min(period_start_time)                                            AS run_start,
         sum(timestampdiff(SECOND, period_start_time, period_end_time))    AS duration_s
  FROM system.lakeflow.job_run_timeline
  WHERE period_start_time >= current_date() - INTERVAL 8 WEEKS
  GROUP BY workspace_id, job_id, run_id
)
SELECT workspace_id, job_id,
       date_trunc('WEEK', run_start)                 AS week,
       count(*)                                      AS runs,
       round(percentile_approx(duration_s, 0.50)/60, 1) AS p50_min,
       round(percentile_approx(duration_s, 0.95)/60, 1) AS p95_min
FROM runs
GROUP BY workspace_id, job_id, date_trunc('WEEK', run_start)
ORDER BY runs DESC
""",
    },

    # ── H13 · spend ─────────────────────────────────────────────────────────────
    {
        "id": "H13",
        "category": "spend",
        "title": "Pipelines — cost per pipeline",
        "eli5": "Shows weekly usage per Lakeflow (DLT) pipeline, so pipelines of similar "
                "size can be compared and the expensive outlier found.",
        "what": "Rank by usage; for the expensive ones, divide by table count for a rough "
                "cost-per-table comparison. In our review one pipeline cost 3.3× its peer "
                "— mostly because of machine type and on-demand vs spot, while running at "
                "about 25% CPU. Check the pipeline's event log for WARN/ERROR messages too.",
        "action": "Prefer fewer, larger workers (less shuffle) and a bigger driver when one "
                  "pipeline drives many tables; start small and step up on evidence; avoid spot "
                  "for continuous change-data pipelines; and consider splitting a mega-pipeline. "
                  "For merge-heavy targets: turn on Photon and deletion vectors, keep the real "
                  "business key as the merge key and put the time column in the ordering/clustering, "
                  "never in the key.",
        "measure": "M2",
        "explain_hunt_desc": "weekly cost per pipeline",
        "explain_rubric": (
            "rank by dbus; for the expensive ones, divide by table count for a rough $/table "
            "comparison, then check utilisation with H14 and warnings with the pipeline event log."
        ),
        "sql": """
SELECT usage_metadata.dlt_pipeline_id AS pipeline_id,
       date_trunc('WEEK', usage_date) AS week,
       sum(usage_quantity)            AS dbus
FROM system.billing.usage
WHERE billing_origin_product = 'DLT'
  AND usage_date >= current_date() - INTERVAL 8 WEEKS
GROUP BY 1, 2
ORDER BY dbus DESC
""",
    },

    # ── H14 · idle ─────────────────────────────────────────────────────────────
    {
        "id": "H14",
        "category": "idle",
        "title": "Idle cores",
        "eli5": "Shows the busiest clusters and their average CPU — sustained low CPU "
                "on big machines means you are paying for cores that sit idle.",
        "what": "This lists the busiest clusters over the last 7 days with their average CPU. "
                "Sustained CPU around 20–30% on large nodes means idle cores you are paying "
                "for. Pick a cluster to drill into (in the notebook, query its hour-by-hour CPU "
                "and memory); low steady CPU with low memory = downsize, memory near 100% = "
                "memory-bound.",
        "action": "Right-size on this evidence — start small (2–3 nodes) and step up "
                  "only when the metrics say so. Use fewer, larger nodes for shuffle-heavy work "
                  "rather than many small ones.",
        "measure": "M2",
        "explain_hunt_desc": "busiest clusters over 7 days with average CPU (idle-core discovery)",
        "explain_rubric": (
            "each row is a cluster with node_minutes (how much it ran) and avg_cpu_pct. Sustained low "
            "avg_cpu_pct (around 20-30%) on a cluster with high node_minutes = idle cores you are paying "
            "for - a downsize candidate. High CPU means it's well-used. Name the low-CPU, high-runtime "
            "clusters as the ones to drill into (per-node utilisation) and right-size. This is a "
            "discovery list; recommend confirming a cluster's hour-by-hour utilisation before resizing."
        ),
        "inventory": True,  # busiest-clusters discovery list — a reference view, not a waste list
        "sql": """
SELECT cluster_id, count(*) AS node_minutes,
       round(avg(cpu_user_percent + cpu_system_percent), 1) AS avg_cpu_pct
FROM system.compute.node_timeline
WHERE start_time >= current_date() - INTERVAL 7 DAYS
GROUP BY 1
ORDER BY node_minutes DESC
LIMIT 10
""",
    },

    # ══════════════════════════════════════════════════════════════════════════
    # H16–H20 · additional hunts from an internal cost-analysis exercise.
    # Not in the original weekly-cost-hunt notebook — the detection SQL is
    # grounded in DBRA (official system-table docs) and corrected against live
    # live data (e.g. real result_state values are ERROR / TIMED_OUT / CANCELLED,
    # not the doc's FAILED/TIMEDOUT/CANCELED). They run in the scan alongside H0–H14.
    # ══════════════════════════════════════════════════════════════════════════

    # ── H16 · idle (compute-type migration) ──────────────────────────────────
    {
        "id": "H16",
        "category": "idle",
        "title": "Jobs running on All-Purpose compute",
        "eli5": "Finds automated jobs running on All-Purpose (interactive) clusters, which cost "
                "$0.55/DBU — versus $0.15/DBU on Jobs compute for the same work.",
        "what": "All-Purpose compute is for interactive notebook work and bills ~3.7× more than Jobs "
                "compute ($0.55 vs $0.15 per DBU). A job pinned to an always-on All-Purpose cluster "
                "pays that premium AND the idle time between runs. Each row is a job + the All-Purpose "
                "cluster it ran on (cluster_source UI/API = interactive; JOB = proper jobs compute), with "
                "its runs this week set against its usual week (the median of its prior four). vs_normal "
                "is how many times that usual it ran, and that sets the alert, so a job newly moved onto "
                "an All-Purpose cluster or suddenly running there far more often stands out. "
                "The pct_saving column is the rate saving from moving it.",
        "action": "Move the job to Jobs Compute (an ephemeral cluster that spins up per run and "
                  "terminates after) or Serverless Jobs. That drops the rate to $0.15/DBU and removes "
                  "idle-between-runs billing. Interim: cut the cluster's auto-termination to 20–30 min.",
        "measure": "M2",
        "explain_hunt_desc": "automated jobs running on All-Purpose clusters instead of cheaper Jobs compute",
        "explain_rubric": (
            "each row is a job that ran on an ALL-PURPOSE cluster (cluster_source UI or API), which "
            "bills $0.55/DBU vs $0.15/DBU on Jobs compute — a 3.7x markup. run_count is how many times "
            "it ran that way this week; vs_normal = run_count / baseline, where baseline is the median of "
            "its prior four weeks on that cluster (floored at the workspace's median job-week on all-purpose; "
            "baseline_from says which), and NULL vs_normal = a job first seen on all-purpose this week. Name "
            "the jobs furthest above their own normal, then the most runs, as the migration targets; "
            "the fix is Jobs Compute / Serverless Jobs. Do not overstate — the saving is the rate delta "
            "on that job's DBUs, not the whole cluster."
        ),
        "sql": f"""
WITH latest_clusters AS (
  SELECT workspace_id, cluster_id, cluster_name, cluster_source, owned_by
  FROM system.compute.clusters
  QUALIFY ROW_NUMBER() OVER (PARTITION BY workspace_id, cluster_id ORDER BY change_time DESC)=1
),
job_names AS (
  SELECT workspace_id, job_id, max(name) AS job_name FROM system.lakeflow.jobs GROUP BY 1,2
),
runs_on_cluster AS (
  SELECT t.workspace_id, t.job_id, exploded.cluster_id,
         floor((unix_timestamp(current_timestamp()) - unix_timestamp(t.period_start_time)) / 604800) AS wk,
         count(DISTINCT t.run_id) AS run_count
  FROM system.lakeflow.job_task_run_timeline t
  LATERAL VIEW explode(t.compute_ids) exploded AS cluster_id
  WHERE t.period_start_time >= current_timestamp() - INTERVAL 35 DAYS
    AND t.compute_ids IS NOT NULL AND array_size(t.compute_ids) > 0
  GROUP BY 1,2,3,4
),
all_purpose AS (
  SELECT r.*, c.cluster_name, c.cluster_source
  FROM runs_on_cluster r
  JOIN latest_clusters c ON r.workspace_id=c.workspace_id AND r.cluster_id=c.cluster_id
  WHERE c.cluster_source IN ('UI','API')                     -- all-purpose only
),
own AS (SELECT workspace_id, job_id, cluster_id, percentile_approx(run_count, 0.5) AS own_normal
        FROM all_purpose WHERE wk BETWEEN 1 AND 4 GROUP BY 1,2,3),
ws AS (SELECT percentile_approx(run_count, 0.5) AS ws_normal FROM all_purpose WHERE wk BETWEEN 1 AND 4),
cur AS (
  SELECT a.*, o.own_normal, w.ws_normal
  FROM all_purpose a LEFT JOIN own o USING (workspace_id, job_id, cluster_id) CROSS JOIN ws w
  WHERE a.wk = 0
)
SELECT r.workspace_id,
       coalesce(jn.job_name, CAST(r.job_id AS STRING)) AS job_name,
       r.cluster_name,
       r.cluster_source,                                   -- UI / API = all-purpose
       r.run_count,
       0.55 AS all_purpose_usd_dbu, 0.15 AS jobs_usd_dbu,
       '73% cheaper on Jobs' AS pct_saving,
       {_vs_normal("run_count")}
FROM cur r
LEFT JOIN job_names jn ON r.workspace_id=jn.workspace_id AND r.job_id=jn.job_id
ORDER BY {_most_unusual("run_count")}, run_count DESC
LIMIT 50
""",
    },

    # ── H17 · config (always-on classic warehouse) ───────────────────────────
    {
        "id": "H17",
        "category": "config",
        "title": "Warehouses with auto-stop disabled",
        "eli5": "Finds classic SQL warehouses set to never auto-stop (auto_stop_minutes = 0) — "
                "they bill 24/7 even when no queries are running.",
        "what": "A classic warehouse with auto_stop_minutes = 0 never suspends, so it bills around the "
                "clock regardless of use. min_clusters > 1 multiplies that. This is the cube_serving "
                "pattern — clusters held warm continuously. Each row is such a warehouse and its size.",
        "action": "Set auto_stop_minutes to a sensible value (10–60 min depending on the workload) so "
                  "it suspends when idle, and drop min_clusters to 1 unless sustained concurrency needs "
                  "more. For genuinely bursty BI, a serverless warehouse auto-stops in minutes.",
        "measure": "M3",
        "explain_hunt_desc": "classic SQL warehouses with auto-stop disabled (billing 24/7)",
        "explain_rubric": (
            "each row is a classic warehouse with auto_stop_minutes=0 — it never suspends, so it bills "
            "continuously. warehouse_size and min_clusters set how expensive that is (a MEDIUM with "
            "min_clusters=3 held 24/7 is a lot). Flag the largest / highest-min_clusters ones first; "
            "the fix is to set an auto-stop. This is a config signal — confirm the warehouse isn't "
            "deliberately kept warm for a real 24/7 SLA before acting."
        ),
        "sql": """
SELECT warehouse_id, warehouse_name, warehouse_type, warehouse_size,
       min_clusters, max_clusters, auto_stop_minutes, change_time
FROM system.compute.warehouses
QUALIFY ROW_NUMBER() OVER (PARTITION BY warehouse_id ORDER BY change_time DESC)=1
   AND delete_time IS NULL
   AND auto_stop_minutes = 0
   AND warehouse_type = 'CLASSIC'
ORDER BY min_clusters DESC, warehouse_size DESC
""",
    },

    # ── H18 · idle (idle high-mem cluster) ────────────────────────────────────
    {
        "id": "H18",
        "category": "idle",
        "title": "Idle interactive clusters (low CPU)",
        "eli5": "Finds persistent All-Purpose clusters sitting at low CPU most of the time — "
                "you're paying for big machines that mostly idle (the ds_highmem pattern).",
        "what": "Each row is an interactive cluster that averaged under 20% CPU this week, with its "
                "node-minutes set against its usual week (the median of its prior four). vs_normal is how "
                "many times that usual it ran, and that sets the alert: a cluster left running idle far "
                "longer than it normally runs is the one to catch. "
                "Over the last 7 days this measures each interactive cluster's average CPU and how much "
                "of the time it sat under 10% (effectively idle). A cluster running for days at sub-20% "
                "average CPU — especially a high-memory one — is paying for cores that do nothing. "
                "avg_cpu_pct and pct_time_idle are the read; verdict flags the worst.",
        "action": "Right-size on this evidence: fewer / smaller nodes, or move the scheduled work to "
                  "Jobs compute (which terminates per run). Cut auto-termination to 20–30 min so it "
                  "can't sit idle for hours. For notebook work, serverless notebooks scale to zero.",
        "measure": "M2",
        "explain_hunt_desc": "persistent interactive clusters sitting at low CPU (idle cores billed)",
        "explain_rubric": (
            "each row is an interactive (all-purpose) cluster with its 7-day avg CPU and the % of time "
            "it was effectively idle (<10% CPU). Sustained low avg_cpu_pct with high total runtime = "
            "idle cores you are paying for; a high-mem node makes it worse. verdict flags "
            "severely-underutilised / underutilised. vs_normal = node_minutes / baseline, where baseline is "
            "the cluster's median week over the prior four (floored at the workspace's median "
            "interactive cluster-week; baseline_from says which); NULL vs_normal = a cluster first seen "
            "this week. Name the worst and recommend right-sizing or "
            "moving to Jobs compute. Confirm the cluster isn't a deliberately-warm interactive one before acting."
        ),
        "sql": f"""
WITH latest_clusters AS (
  SELECT workspace_id, cluster_id, cluster_name, cluster_source, owned_by, auto_termination_minutes, delete_time
  FROM system.compute.clusters
  QUALIFY ROW_NUMBER() OVER (PARTITION BY workspace_id, cluster_id ORDER BY change_time DESC)=1
),
cpu AS (
  SELECT workspace_id, cluster_id,
         floor((unix_timestamp(current_timestamp()) - unix_timestamp(start_time)) / 604800) AS wk,
         count(*) AS samples,
         round(avg(cpu_user_percent + cpu_system_percent), 1) AS avg_cpu_pct,
         round(max(cpu_user_percent + cpu_system_percent), 1) AS peak_cpu_pct,
         round(sum(CASE WHEN (cpu_user_percent + cpu_system_percent) < 10 THEN 1 ELSE 0 END)*100.0/count(*), 1) AS pct_time_idle
  FROM system.compute.node_timeline
  WHERE start_time >= current_timestamp() - INTERVAL 35 DAYS
  GROUP BY 1,2,3
),
interactive AS (
  SELECT cpu.*, c.cluster_name, c.cluster_source, c.auto_termination_minutes
  FROM cpu JOIN latest_clusters c USING (workspace_id, cluster_id)
  WHERE c.cluster_source IN ('UI','API') AND c.delete_time IS NULL
),
own AS (SELECT workspace_id, cluster_id, percentile_approx(samples, 0.5) AS own_normal
        FROM interactive WHERE wk BETWEEN 1 AND 4 GROUP BY 1,2),
ws AS (SELECT percentile_approx(samples, 0.5) AS ws_normal FROM interactive WHERE wk BETWEEN 1 AND 4),
cur AS (
  SELECT i.*, i.samples AS node_minutes, o.own_normal, w.ws_normal
  FROM interactive i LEFT JOIN own o USING (workspace_id, cluster_id) CROSS JOIN ws w
  WHERE i.wk = 0 AND i.avg_cpu_pct < 20
)
SELECT coalesce(cluster_name, cluster_id) AS cluster,
       cluster_source, auto_termination_minutes,
       avg_cpu_pct, peak_cpu_pct, pct_time_idle, node_minutes,
       CASE WHEN avg_cpu_pct < 10 AND peak_cpu_pct < 30 THEN 'severely-underutilised'
            WHEN avg_cpu_pct < 20 THEN 'underutilised' ELSE 'ok' END AS verdict,
       {_vs_normal("node_minutes")}
FROM cur
ORDER BY {_most_unusual("node_minutes")}, node_minutes DESC
LIMIT 50
""",
    },

    # ── H19 · idle (always-on Apps) ───────────────────────────────────────────
    {
        "id": "H19",
        "category": "idle",
        "title": "Always-on Databricks Apps",
        "eli5": "Finds Databricks Apps that bill compute nearly every day — running continuously "
                "whether or not anyone is using them.",
        "what": "A Databricks App bills DBUs while deployed and running, regardless of traffic. An app "
                "that shows usage on 25+ of the last 30 days is effectively always-on. Each row is an "
                "app with its days billed and total/average daily DBUs, plus its DBUs this week set "
                "against its usual week (the median of its prior four). vs_normal is how many times that "
                "usual it burned, and that sets the alert, so an app that scaled up or started burning "
                "more stands out. High DBU + always-on + little real traffic = a stop-or-consolidate candidate.",
        "action": "Audit per-app traffic (Apps UI Insights, or OAuth-consent as a breadth signal); stop "
                  "or undeploy apps no one uses, and consolidate per-workspace/per-env duplicates. Apps "
                  "that only need business-hours availability don't need to run overnight.",
        "measure": "M2",
        "explain_hunt_desc": "Databricks Apps billing nearly every day (always-on regardless of traffic)",
        "explain_rubric": (
            "each row is a Databricks App with days_billed (of last 30) and its DBUs. days_billed >= 25 "
            "means it's effectively always-on; high total_dbus makes it costly. vs_normal = dbus_7d / "
            "baseline, where baseline is the app's median week over the prior four (floored at the "
            "workspace's median app-week; baseline_from says which). Name the apps furthest above their "
            "own normal first, then the biggest "
            "always-on apps as stop/consolidate candidates — but note billing shows it's RUNNING, not "
            "whether it's USED, so recommend confirming traffic (Apps Insights) before stopping."
        ),
        "sql": f"""
WITH daily AS (
  SELECT usage_metadata.app_name AS app_name, workspace_id, usage_date,
         sum(usage_quantity) AS dbus
  FROM system.billing.usage
  WHERE billing_origin_product = 'APPS'
    AND usage_date >= current_date() - INTERVAL 34 DAYS
    AND usage_metadata.app_name IS NOT NULL
  GROUP BY 1, 2, 3
),
weekly AS (
  SELECT app_name, workspace_id, floor(datediff(current_date(), usage_date) / 7) AS wk, sum(dbus) AS dbus
  FROM daily GROUP BY 1, 2, 3
),
own AS (SELECT app_name, workspace_id, percentile_approx(dbus, 0.5) AS own_normal
        FROM weekly WHERE wk BETWEEN 1 AND 4 GROUP BY 1, 2),
ws AS (SELECT percentile_approx(dbus, 0.5) AS ws_normal FROM weekly WHERE wk BETWEEN 1 AND 4),
apps AS (
  SELECT app_name, workspace_id,
         count(*)                                       AS days_billed_30d,
         round(sum(dbus), 1)                            AS total_dbus_30d,
         round(sum(dbus)/count(*), 2)                   AS avg_daily_dbus,
         max(usage_date)                                AS last_billed,
         round(sum(CASE WHEN usage_date > current_date() - INTERVAL 7 DAYS THEN dbus ELSE 0 END), 1) AS dbus_7d
  FROM daily
  WHERE usage_date >= current_date() - INTERVAL 30 DAYS
  GROUP BY 1, 2
  HAVING count(*) >= 25
),
graded AS (
  SELECT a.*, o.own_normal, w.ws_normal
  FROM apps a LEFT JOIN own o USING (app_name, workspace_id) CROSS JOIN ws w
)
SELECT app_name, workspace_id, days_billed_30d, total_dbus_30d, avg_daily_dbus, last_billed, dbus_7d,
       {_vs_normal("dbus_7d")}
FROM graded
ORDER BY {_most_unusual("dbus_7d")}, total_dbus_30d DESC
LIMIT 50
""",
    },

    # ── H20 · spend (failing jobs) ────────────────────────────────────────────
    {
        "id": "H20",
        "category": "spend",
        "title": "Frequently-failing jobs",
        "eli5": "Finds jobs that fail repeatedly — every failed run still burns compute on "
                "setup and partial execution before it dies.",
        "what": "A job that fails often wastes the DBUs of every failed attempt (startup + however far "
                "it got). Each row is a job that failed this week with its run count, failed-run count, "
                "failure rate, and the hours of compute spent on runs that failed, set against its usual "
                "failing week (the median of its prior four weeks that had failures). vs_normal is how many "
                "times that usual it wasted, and that sets the alert; a job with no failures in the prior "
                "four weeks shows as NEW. A high failure rate on a frequent job is both a reliability "
                "problem and a steady cost leak.",
        "action": "Root-cause the recurring failures (read the termination reason and the run logs); "
                  "add an early-abort timeout so a doomed run dies fast instead of burning a full "
                  "window; and pause or fix broken schedules that never succeed.",
        "measure": "M2",
        "explain_hunt_desc": "jobs failing frequently (compute wasted on failed runs)",
        "explain_rubric": (
            "each row is a job with total_runs, failed_runs, failure_rate_pct and wasted_hours (compute "
            "spent on runs that ended in ERROR/FAILED/TIMED_OUT/CANCELLED this week). vs_normal = "
            "wasted_hours / baseline, where baseline is the job's median failing week over the prior four "
            "(floored at the workspace's 95th-percentile failing job-week; baseline_from says which); NULL "
            "vs_normal = a job that started failing this week. High "
            "failure_rate_pct with meaningful wasted_hours = a reliability-and-cost problem. Name the "
            "worst offenders; the fix is root-causing the failures and adding early-abort timeouts. A "
            "few cancellations are normal — focus on the chronic high-rate ones."
        ),
        "sql": f"""
WITH job_names AS (SELECT workspace_id, job_id, max(name) AS job_name FROM system.lakeflow.jobs GROUP BY 1,2),
weekly AS (
  SELECT workspace_id, job_id,
         floor((unix_timestamp(current_timestamp()) - unix_timestamp(period_start_time)) / 604800) AS wk,
         count(*) AS total_runs,
         count_if(result_state IN ('ERROR','FAILED','TIMED_OUT','CANCELLED')) AS failed_runs,
         round(count_if(result_state IN ('ERROR','FAILED','TIMED_OUT','CANCELLED'))*100.0/count(*), 1) AS failure_rate_pct,
         sum(CASE WHEN result_state IN ('ERROR','FAILED','TIMED_OUT','CANCELLED')
                  THEN timestampdiff(SECOND, period_start_time, period_end_time) ELSE 0 END)/3600.0 AS wasted_hours
  FROM system.lakeflow.job_run_timeline
  WHERE period_start_time >= current_timestamp() - INTERVAL 35 DAYS
  GROUP BY 1, 2, 3
),
own AS (SELECT workspace_id, job_id, percentile_approx(wasted_hours, 0.5) AS own_normal
        FROM weekly WHERE wk BETWEEN 1 AND 4 AND wasted_hours > 0 GROUP BY 1, 2),
ws AS (SELECT percentile_approx(wasted_hours, 0.95) AS ws_normal
       FROM weekly WHERE wk BETWEEN 1 AND 4 AND wasted_hours > 0),
cur AS (
  SELECT s.*, o.own_normal, w.ws_normal
  FROM weekly s LEFT JOIN own o USING (workspace_id, job_id) CROSS JOIN ws w
  WHERE s.wk = 0 AND s.failed_runs > 0
)
SELECT s.workspace_id,
       coalesce(jn.job_name, CAST(s.job_id AS STRING)) AS job_name,
       s.total_runs, s.failed_runs, s.failure_rate_pct, round(s.wasted_hours, 1) AS wasted_hours,
       {_vs_normal("s.wasted_hours")}
FROM cur s LEFT JOIN job_names jn ON s.workspace_id=jn.workspace_id AND s.job_id=jn.job_id
ORDER BY {_most_unusual("s.wasted_hours")}, s.failure_rate_pct DESC
LIMIT 50
""",
    },

    # ── H21 · reads (compression: snappy → zstd) ──────────────────────────────
    # NOTE: the parquet compression codec is NOT exposed in any system table or
    # information_schema view (confirmed via DBRA). So this hunt can't read the
    # codec directly — it flags Delta tables created BEFORE the Oct-2025 default
    # flip (snappy→zstd) as CANDIDATES, and the action explains how to confirm
    # (SHOW TBLPROPERTIES / the .snappy.parquet file extension) and fix. Honest
    # by construction: it's a "worth checking" list, framed as such.
    {
        "id": "H21",
        "category": "reads",
        "title": "Tables likely still on snappy (not zstd)",
        "eli5": "Finds Delta tables created before Databricks switched the default compression from "
                "snappy to zstd — zstd typically stores the same data in ~10–40% less space.",
        "what": "The compression codec isn't in any system table, so this can't read it directly. Instead "
                "it lists Delta tables created BEFORE the Oct-2025 default flip (snappy → zstd) per schema "
                "— these are the likely-snappy CANDIDATES. A table created after that, or one whose codec was "
                "set explicitly, may already be zstd. This is a 'worth checking' list, not proof.",
        "action": "Confirm a candidate's codec with SHOW TBLPROPERTIES <tbl> ('delta.parquet.compression.codec') "
                  "or by checking whether its files end in .snappy.parquet. To switch: set the table property "
                  "delta.parquet.compression.codec = 'zstd', then rewrite the data so the new codec actually "
                  "applies (OPTIMIZE, or for a full rewrite REORG TABLE) — setting the property alone only "
                  "affects newly-written files. Prioritise the largest, most-scanned tables.",
        "measure": "M2",
        "explain_hunt_desc": "Delta tables created before the Oct-2025 snappy→zstd default flip (candidates to recompress)",
        "explain_rubric": (
            "each row is a schema (catalog.schema) with a count of Delta tables created before 2025-10-15 — "
            "the date Databricks flipped the default parquet codec from snappy to zstd. These are CANDIDATES "
            "likely still on snappy, NOT confirmed (the codec isn't in system tables). More tables in a schema "
            "= more potential storage saving from recompressing to zstd (~10-40% smaller). Recommend confirming "
            "with SHOW TBLPROPERTIES before acting, and prioritising the largest / most-scanned tables. Do not "
            "assert waste — frame as 'worth checking'."
        ),
        "sql": """
SELECT table_catalog || '.' || table_schema        AS schema_name,
       count(*)                                     AS candidate_delta_tables,
       min(created)                                 AS oldest_created,
       max(last_altered)                            AS newest_change
FROM system.information_schema.tables
WHERE data_source_format = 'DELTA'
  AND table_type IN ('MANAGED','EXTERNAL')
  AND created < '2025-10-15'          -- before the snappy→zstd default flip
GROUP BY 1
ORDER BY candidate_delta_tables DESC
LIMIT 50
""",
    },

    # ── H22 · config (serverless jobs in performance-optimized mode) ──────────
    {
        "id": "H22",
        "category": "config",
        "title": "Serverless jobs in Performance-optimized mode",
        "eli5": "Finds serverless jobs running in Performance-optimized mode. That mode starts and "
                "runs faster but burns more DBUs — worth it for time-critical pipelines, but "
                "non-urgent jobs can switch to Standard mode and pay for fewer DBUs.",
        "what": "Serverless jobs have two performance modes. Both bill at the SAME $/DBU rate — the "
                "difference is DBU consumption: Performance-optimized (the default) prioritises fast "
                "startup and runs on quicker capacity, so it consumes more DBUs; Standard tolerates a "
                "slower cold start (Databricks quotes ~4–6 minutes) and uses fewer DBUs for the same "
                "work. Each row is a serverless job currently billing in Performance-optimized mode, "
                "with its list-price cost and DBUs over the last 30 days, plus its spend this week set "
                "against its usual week (the median of its prior four). vs_normal is how many times that "
                "usual it spent, and that sets the alert. perf_optimized_usd is the "
                "spend exposed to a Standard-mode switch — not a guaranteed saving.",
        "action": "For latency-tolerant jobs — scheduled batch, backfills, overnight ETL, anything with "
                  "no downstream deadline — turn OFF ‘Performance optimization’ on the job's serverless "
                  "compute (Job → task → Compute → Serverless → Performance optimized). That accepts a "
                  "slower cold start in exchange for fewer DBUs. Keep Performance-optimized only where a "
                  "run has a real SLA or a waiting consumer. Prove the change with M6 (the job's "
                  "Performance-optimized line should fall, replaced by a smaller Standard line).",
        "measure": "M6",
        "explain_hunt_desc": "serverless jobs running in Performance-optimized mode (faster start, more DBUs) that may not need it",
        "explain_rubric": (
            "each row is a serverless job billing in PERFORMANCE_OPTIMIZED mode over the last 30 days. "
            "Ground the read on the real mechanic: BOTH modes bill at the same $/DBU rate — "
            "performance-optimized just consumes MORE DBUs to start and run faster (Standard tolerates a "
            "~4–6 min slower cold start for fewer DBUs). perf_optimized_usd is the 30-day list-price spend "
            "in this mode; dbus is the volume; active_days shows how regularly it runs. vs_normal = usd_7d / "
            "baseline, where baseline is the job's median week of this spend over the prior four (floored "
            "at the workspace's 95th-percentile job-week; baseline_from says which); NULL vs_normal = a job first "
            "seen in this mode this week. Name the jobs furthest above their own normal and the biggest-$ "
            "jobs as the candidates to move to Standard, and note that a frequently-run job (high active_days) "
            "compounds the saving. DO NOT quote a fixed % saving — the realised saving depends on the "
            "workload, and the tradeoff is a slower start; flag that genuinely time-critical / SLA jobs "
            "should stay Performance-optimized. The job name often hints at urgency (e.g. ‘15min Refresh’ "
            "may be deliberately fast; a nightly ‘datagen’ or ‘backfill’ usually is not)."
        ),
        "sql": f"""
WITH price AS (
  SELECT sku_name, cloud, price_start_time, price_end_time,
         pricing.effective_list.default AS unit_usd FROM system.billing.list_prices
),
job_name AS (SELECT workspace_id, job_id, max(name) AS name FROM system.lakeflow.jobs GROUP BY 1, 2),
daily AS (
  SELECT u.workspace_id, u.usage_metadata.job_id AS job_id, u.usage_date,
         sum(u.usage_quantity * pr.unit_usd) AS usd,
         sum(u.usage_quantity)               AS dbus
  FROM system.billing.usage u
  JOIN price pr ON u.sku_name = pr.sku_name AND u.cloud = pr.cloud
    AND u.usage_start_time >= pr.price_start_time
    AND (pr.price_end_time IS NULL OR u.usage_start_time < pr.price_end_time)
  WHERE u.billing_origin_product = 'JOBS'
    AND u.product_features.performance_target = 'PERFORMANCE_OPTIMIZED'
    AND u.usage_metadata.job_id IS NOT NULL
    AND u.usage_date >= current_date() - INTERVAL 34 DAYS
  GROUP BY 1, 2, 3
),
weekly AS (
  SELECT workspace_id, job_id, floor(datediff(current_date(), usage_date) / 7) AS wk, sum(usd) AS usd
  FROM daily GROUP BY 1, 2, 3
),
own AS (SELECT workspace_id, job_id, percentile_approx(usd, 0.5) AS own_normal
        FROM weekly WHERE wk BETWEEN 1 AND 4 AND usd > 0 GROUP BY 1, 2),
ws AS (SELECT percentile_approx(usd, 0.95) AS ws_normal FROM weekly WHERE wk BETWEEN 1 AND 4 AND usd > 0),
jobs AS (
  SELECT workspace_id, job_id,
         round(sum(CASE WHEN usage_date >= current_date() - INTERVAL 30 DAYS THEN usd END), 2) AS perf_optimized_usd,
         round(sum(CASE WHEN usage_date >= current_date() - INTERVAL 30 DAYS THEN dbus END), 1) AS dbus,
         count(DISTINCT CASE WHEN usage_date >= current_date() - INTERVAL 30 DAYS THEN usage_date END) AS active_days,
         round(sum(CASE WHEN usage_date > current_date() - INTERVAL 7 DAYS THEN usd ELSE 0 END), 2) AS usd_7d
  FROM daily GROUP BY 1, 2
  HAVING perf_optimized_usd > 0
),
graded AS (
  SELECT j.*, o.own_normal, w.ws_normal
  FROM jobs j LEFT JOIN own o USING (workspace_id, job_id) CROSS JOIN ws w
)
SELECT g.workspace_id,
       coalesce(jn.name, CAST(g.job_id AS STRING)) AS job_name,
       g.job_id, g.perf_optimized_usd, g.dbus, g.active_days, g.usd_7d,
       {_vs_normal("g.usd_7d")}
FROM graded g
LEFT JOIN job_name jn ON g.workspace_id = jn.workspace_id AND g.job_id = jn.job_id
ORDER BY {_most_unusual("g.usd_7d")}, g.perf_optimized_usd DESC
LIMIT 50
""",
    },

    # ── H23 · spend ──────────────────────────────────────────────────────────
    {
        "id": "H23",
        "category": "spend",
        "title": "Predictive Optimization cost drivers",
        "eli5": "Finds the tables driving your Predictive Optimization bill up — the ones whose "
                "auto-maintenance (compaction, clustering, vacuum) burned far more DBUs in the last 7 "
                "days than they usually do in a week.",
        "what": "Predictive Optimization is the automatic maintenance Databricks runs on your Unity "
                "Catalog managed tables — OPTIMIZE/compaction, liquid clustering, VACUUM and stats. You "
                "pay per DBU of that work, so the bill tracks how much data is being written and "
                "reorganised. Each row is one table + operation type, with its PO DBUs in the last 7 "
                "complete days, the prior 7, and the change. vs_normal sets this week against the "
                "table's usual week (the median of its prior four), and that sets the alert; rows are "
                "sorted by it, so the tables furthest above their own normal sit at the top. dbu_increase is in DBUs, not dollars — multiply by "
                "your PO SKU rate for an approximate cost. (The two windows are trailing complete 7-day "
                "periods, not calendar weeks, so today's partial day never distorts the comparison.)",
        "action": "Trace the top risers to a cause: (1) more ingestion / writes into that table → "
                  "expected if it is genuine new data; (2) a table or whole schema newly brought under "
                  "PO → confirm you actually want PO managing it; (3) a move to liquid clustering or a "
                  "clustering-key change → a one-off re-cluster that settles after a week or two. Then "
                  "right-size the SCOPE: enabling PO across prod usually makes sense, but for "
                  "dev/staging/lower environments decide per use-case — you can leave PO off there and "
                  "instead run table maintenance (OPTIMIZE/VACUUM) on a schedule, less often. Re-run this "
                  "check next week; a one-off re-cluster or a narrowed PO scope should pull the top "
                  "table's dbu_increase back down.",
        "explain_hunt_desc": "tables whose Predictive Optimization DBU consumption rose most over the "
                             "last 7 days vs the prior 7 — the driver of a PO cost increase",
        "explain_rubric": (
            "each row is a (table, operation_type) whose Predictive Optimization work changed "
            "week-over-week; dbu_increase = last-7-days minus prior-7-days in ESTIMATED_DBUs (the unit "
            "PO bills in). vs_normal = dbus_last_7d / baseline, where baseline is the table's median week "
            "over the prior four (floored at the workspace's 95th-percentile table-week; baseline_from says which); "
            "NULL vs_normal = a table first seen under PO this week. operation_type tells you WHICH maintenance grew: COMPACTION = many small "
            "files being written, so more bin-packing; CLUSTERING / AUTO_CLUSTERING_COLUMN_SELECTION = "
            "liquid-clustering re-organisation, often a one-off after clustering is enabled or keys "
            "change; VACUUM = garbage-collecting deleted files; ANALYZE = statistics. A large rise "
            "usually means one of: higher ingestion volume into that table, a table/schema newly brought "
            "under PO, or a clustering-key change forcing a re-cluster. Name the top tables and their "
            "operation types as the drivers. Values are DBUs, NOT dollars — do not fabricate a cost; the "
            "bill is DBUs x the PO SKU rate. If the top operation is CLUSTERING right after enablement, "
            "call it a likely one-off that settles."
        ),
        "sql": f"""
WITH ops AS (
  SELECT catalog_name, schema_name, table_name, operation_type, usage_quantity,
         floor((datediff(current_date(), start_time) - 1) / 7) AS wk
  FROM system.storage.predictive_optimization_operations_history
  WHERE usage_unit = 'ESTIMATED_DBU'
    AND start_time >= current_date() - INTERVAL 35 DAYS
    AND start_time <  current_date()
),
weekly AS (
  SELECT catalog_name, schema_name, table_name, operation_type, wk, sum(usage_quantity) AS dbus
  FROM ops GROUP BY 1, 2, 3, 4, 5
),
own AS (
  SELECT catalog_name, schema_name, table_name, operation_type, percentile_approx(dbus, 0.5) AS own_normal
  FROM weekly WHERE wk BETWEEN 1 AND 4 AND dbus > 0 GROUP BY 1, 2, 3, 4
),
ws AS (SELECT percentile_approx(dbus, 0.95) AS ws_normal FROM weekly WHERE wk BETWEEN 1 AND 4 AND dbus > 0),
agg AS (
  SELECT catalog_name, schema_name, table_name, operation_type,
         round(sum(CASE WHEN wk = 1 THEN dbus END), 1) AS dbus_prev_7d,
         round(sum(CASE WHEN wk = 0 THEN dbus END), 1) AS dbus_last_7d
  FROM weekly WHERE wk <= 1
  GROUP BY 1, 2, 3, 4
),
graded AS (
  SELECT a.catalog_name, a.schema_name, a.table_name, a.operation_type,
         coalesce(a.dbus_prev_7d, 0) AS dbus_prev_7d,
         coalesce(a.dbus_last_7d, 0) AS dbus_last_7d,
         o.own_normal, w.ws_normal
  FROM agg a
  LEFT JOIN own o ON a.catalog_name <=> o.catalog_name AND a.schema_name <=> o.schema_name
                 AND a.table_name <=> o.table_name AND a.operation_type <=> o.operation_type
  CROSS JOIN ws w
  WHERE coalesce(a.dbus_last_7d, 0) + coalesce(a.dbus_prev_7d, 0) > 0
)
SELECT catalog_name, schema_name, table_name, operation_type,
       dbus_prev_7d, dbus_last_7d,
       round(dbus_last_7d - dbus_prev_7d, 1) AS dbu_increase,
       concat_ws('.', catalog_name, schema_name, table_name) || ' (' || operation_type || ')' AS po_target,
       {_vs_normal("dbus_last_7d")}
FROM graded
ORDER BY {_most_unusual("dbus_last_7d")}, dbu_increase DESC
LIMIT 50
""",
    },
]

# ── The measures (Prove page) ────────────────────────────────────────────────
# These prove a fix worked. Same verbatim-SQL rule as the hunts.
MEASURES = [
    {
        "id": "M1",
        "group": "warehouse", "order": 3,
        "use_when": "you want to price one specific expensive query (e.g. an investigation scan) "
                    "and show a per-query before/after.",
        "title": "Per-query attributed cost (shared warehouses)",
        "eli5": "Prices individual queries on a shared warehouse by allocating each a slice of "
                "the warehouse-hour's cost in proportion to its execution time.",
        "what": "Warehouses bill by uptime, not by query, so per-query cost is an ALLOCATION. Each "
                "query gets a share of its warehouse-hour's cost against the warehouse's cluster-hours "
                "of capacity — so the shares never exceed the real cost. Use it to price a specific "
                "expensive query (e.g. an investigation scan) and to show a per-query before/after.",
        "chart": "table",
        "sql": """
WITH wh_hour AS (
  SELECT warehouse_id, hour, sum(usage_quantity * unit_usd) AS hour_usd
  FROM (
    SELECT u.usage_metadata.warehouse_id          AS warehouse_id,
           date_trunc('HOUR', u.usage_start_time) AS hour,
           u.usage_quantity,
           lp.pricing.effective_list.default       AS unit_usd
    FROM system.billing.usage u
    JOIN system.billing.list_prices lp
      ON  u.sku_name = lp.sku_name AND u.cloud = lp.cloud
      AND u.usage_start_time >= lp.price_start_time
      AND (lp.price_end_time IS NULL OR u.usage_start_time < lp.price_end_time)
    WHERE u.usage_metadata.warehouse_id IS NOT NULL
      AND u.usage_date >= current_date() - INTERVAL 14 DAYS
    QUALIFY ROW_NUMBER() OVER (PARTITION BY u.record_id ORDER BY lp.price_start_time DESC) = 1
  )
  GROUP BY 1, 2
),
cfg_hist AS (
  SELECT warehouse_id, max_clusters, change_time AS valid_from,
         coalesce(lead(change_time) OVER (PARTITION BY warehouse_id ORDER BY change_time),
                  TIMESTAMP '9999-12-31') AS valid_to
  FROM system.compute.warehouses
),
q AS (
  SELECT compute.warehouse_id AS warehouse_id, date_trunc('HOUR', start_time) AS hour,
         statement_id, executed_by, execution_duration_ms,
         sum(execution_duration_ms) OVER (
           PARTITION BY compute.warehouse_id, date_trunc('HOUR', start_time)) AS busy_ms
  FROM system.query.history
  WHERE start_time >= current_date() - INTERVAL 14 DAYS AND execution_duration_ms > 0
),
alloc AS (
  SELECT q.*, w.hour_usd,
         GREATEST(coalesce(c.max_clusters, 1) * 3600000.0, q.busy_ms) AS capacity_ms
  FROM q JOIN wh_hour w USING (warehouse_id, hour)
         LEFT JOIN cfg_hist c
           ON  c.warehouse_id = q.warehouse_id
           AND q.hour >= c.valid_from AND q.hour < c.valid_to
)
SELECT statement_id, executed_by,
       round(execution_duration_ms/60000.0, 2)     AS exec_min,
       round(capacity_ms/3600000.0, 2)             AS capacity_cluster_hours,
       round(hour_usd * execution_duration_ms / capacity_ms, 2) AS query_usd
FROM alloc
ORDER BY query_usd DESC
LIMIT 50
""",
    },
    {
        "id": "M3",
        "group": "warehouse", "order": 1,
        "use_when": "you changed a warehouse's size, auto-stop, or moved a workload — the headline "
                    "proof. Watch its weekly line step down.",
        "title": "The “After” Scoreboard — weekly $ per warehouse",
        "eli5": "The headline proof. Weekly spend per warehouse over eight weeks — fix "
                "something, then watch this line step down.",
        "what": "Every warehouse-level fix (chatter, auto-stop, placement, downsizing) lands "
                "here. Compare weeks of the same-shape workload. Remember classic warehouses "
                "carry a separate cloud-infrastructure bill next to this usage line.",
        "chart": "line_by_warehouse",  # frontend renders this as a multi-line weekly chart
        "sql": """
SELECT u.usage_metadata.warehouse_id     AS warehouse_id,
       date_trunc('WEEK', u.usage_date)  AS week,
       sum(u.usage_quantity)             AS dbus,
       round(sum(u.usage_quantity * lp.pricing.effective_list.default), 2) AS list_usd
FROM system.billing.usage u
JOIN system.billing.list_prices lp
  ON  u.sku_name = lp.sku_name AND u.cloud = lp.cloud
  AND u.usage_start_time >= lp.price_start_time
  AND (lp.price_end_time IS NULL OR u.usage_start_time < lp.price_end_time)
WHERE u.usage_metadata.warehouse_id IS NOT NULL
  AND u.usage_date >= current_date() - INTERVAL 8 WEEKS
GROUP BY 1, 2
ORDER BY 1, 2
""",
    },
    {
        "id": "M1b",
        "group": "warehouse", "order": 2,
        "use_when": "you suspect a warehouse is billing while idle — shows the idle $ and utilisation "
                    "so you can prove an auto-stop / right-sizing fix.",
        "title": "Warehouse idle cost — the residual",
        "eli5": "For each warehouse: total cost, the part that did actual work, and the "
                "unallocated (idle) remainder — plus average utilisation.",
        "what": "High idle cost with low utilisation points to an auto-stop or right-sizing "
                "conversation, not a query problem. This is an ALLOCATION PROXY, not measured "
                "idle: it uses each warehouse's cluster-hours of capacity as the denominator, "
                "a sound but simplified floor. The dollars are Databricks list-price DBU cost "
                "— Finance applies your contract rate and (for classic) the separate cloud bill.",
        "chart": "table",
        "sql": """
WITH wh_hour AS (
  SELECT warehouse_id, hour, sum(usage_quantity * unit_usd) AS hour_usd
  FROM (
    SELECT u.usage_metadata.warehouse_id AS warehouse_id,
           date_trunc('HOUR', u.usage_start_time) AS hour,
           u.usage_quantity, lp.pricing.effective_list.default AS unit_usd
    FROM system.billing.usage u JOIN system.billing.list_prices lp
      ON u.sku_name=lp.sku_name AND u.cloud=lp.cloud
     AND u.usage_start_time>=lp.price_start_time AND (lp.price_end_time IS NULL OR u.usage_start_time<lp.price_end_time)
    WHERE u.usage_metadata.warehouse_id IS NOT NULL AND u.usage_date >= current_date() - INTERVAL 14 DAYS
    QUALIFY ROW_NUMBER() OVER (PARTITION BY u.record_id ORDER BY lp.price_start_time DESC) = 1
  )
  GROUP BY 1,2
),
cfg_hist AS (
  SELECT warehouse_id, max_clusters,
         change_time AS valid_from,
         coalesce(lead(change_time) OVER (PARTITION BY warehouse_id ORDER BY change_time),
                  TIMESTAMP '9999-12-31') AS valid_to
  FROM system.compute.warehouses
),
busy AS (
  SELECT compute.warehouse_id AS warehouse_id, date_trunc('HOUR',start_time) AS hour,
         sum(execution_duration_ms) AS busy_ms
  FROM system.query.history
  WHERE start_time >= current_date() - INTERVAL 14 DAYS AND execution_duration_ms>0
  GROUP BY 1,2
),
hour_alloc AS (
  SELECT w.warehouse_id, w.hour, w.hour_usd,
         coalesce(b.busy_ms, 0) AS busy_ms,
         GREATEST(coalesce(c.max_clusters,1)*3600000.0, coalesce(b.busy_ms,0)) AS capacity_ms
  FROM wh_hour w
  LEFT JOIN cfg_hist c
    ON  c.warehouse_id = w.warehouse_id
    AND w.hour >= c.valid_from AND w.hour < c.valid_to
  LEFT JOIN busy b USING (warehouse_id, hour)
)
SELECT warehouse_id,
       round(sum(hour_usd), 2)                                AS total_usd,
       round(sum(hour_usd * busy_ms / capacity_ms), 2)        AS attributed_usd,
       round(sum(hour_usd * (1 - busy_ms / capacity_ms)), 2)  AS idle_usd,
       round(avg(busy_ms / capacity_ms) * 100, 1)             AS avg_util_pct
FROM hour_alloc
GROUP BY 1 ORDER BY idle_usd DESC
""",
    },
    {
        "id": "M2",
        "group": "job", "order": 1,
        "use_when": "you fixed a job or pipeline (moved to Jobs compute, right-sized, cut failures) — "
                    "the before/after is two rows of this table.",
        "title": "Exact $ per job and per pipeline",
        "eli5": "Exact weekly dollars for each job and pipeline — the before/after for "
                "any job or pipeline fix is two rows of this table.",
        "what": "Because usage carries the entity ID, this is DIRECTLY attributed (not an "
                "allocation) wherever the ID is populated. Usage with no job/pipeline ID (some "
                "shared or serverless products) shows up as its own ‘unattributed’ row "
                "rather than being dropped, so the dollars reconcile.",
        "chart": "table",
        "sql": """
WITH price AS (
  SELECT sku_name, cloud, price_start_time, price_end_time,
         pricing.effective_list.default AS unit_usd FROM system.billing.list_prices
),
pipe_name AS (SELECT pipeline_id, max(name) AS name FROM system.lakeflow.pipelines GROUP BY 1),
job_name  AS (SELECT workspace_id, job_id, max(name) AS name FROM system.lakeflow.jobs GROUP BY workspace_id, job_id)
SELECT u.billing_origin_product                                            AS product,
       u.workspace_id,
       coalesce(u.usage_metadata.job_id, u.usage_metadata.dlt_pipeline_id,
                '(unattributed — no entity id)')                           AS entity_id,
       coalesce(jn.name, pn.name,
                CASE WHEN u.usage_metadata.job_id IS NULL
                      AND u.usage_metadata.dlt_pipeline_id IS NULL
                     THEN '(unattributed — no entity id)'
                     ELSE '(name unavailable)' END)                        AS entity_name,
       date_trunc('WEEK', u.usage_date)                                    AS week,
       sum(u.usage_quantity)                                               AS dbus,
       round(sum(u.usage_quantity * pr.unit_usd), 2)                       AS list_usd
FROM system.billing.usage u
JOIN price pr ON u.sku_name = pr.sku_name AND u.cloud = pr.cloud
  AND u.usage_start_time >= pr.price_start_time
  AND (pr.price_end_time IS NULL OR u.usage_start_time < pr.price_end_time)
LEFT JOIN pipe_name pn ON u.usage_metadata.dlt_pipeline_id = pn.pipeline_id
LEFT JOIN job_name  jn ON u.usage_metadata.job_id = jn.job_id
                      AND u.workspace_id          = jn.workspace_id
WHERE u.billing_origin_product IN ('JOBS', 'DLT')
  AND u.usage_date >= current_date() - INTERVAL 8 WEEKS
GROUP BY 1, 2, 3, 4, 5
ORDER BY list_usd DESC
LIMIT 200
""",
    },
    {
        "id": "M6",
        "group": "job", "order": 2,
        "use_when": "you switched a serverless job from Performance-optimized to Standard mode (H22) — "
                    "watch that job's Performance-optimized column fall, replaced by a smaller Standard column.",
        "title": "Serverless job cost by performance mode",
        "eli5": "For the top serverless jobs: weekly dollars split into Performance-optimized vs "
                "Standard mode — the direct before/after for a performance-mode change.",
        "what": "Both modes bill at the same $/DBU rate, so this splits each job's weekly serverless "
                "spend by performance_target to make a mode change visible. Before a change, a job's "
                "spend sits entirely in the Performance-optimized column; after you disable performance "
                "optimization, subsequent weeks shift into the Standard column at a lower DBU total for "
                "the same work. Limited to the top jobs by Performance-optimized spend so the table "
                "stays legible. List-price DBU cost, directly attributed by job_id.",
        "chart": "table",
        "sql": """
WITH price AS (
  SELECT sku_name, cloud, price_start_time, price_end_time,
         pricing.effective_list.default AS unit_usd FROM system.billing.list_prices
),
job_name AS (SELECT workspace_id, job_id, max(name) AS name FROM system.lakeflow.jobs GROUP BY 1, 2),
sj AS (
  SELECT u.workspace_id,
         u.usage_metadata.job_id                    AS job_id,
         u.product_features.performance_target      AS mode,
         date_trunc('WEEK', u.usage_date)           AS week,
         u.usage_quantity                           AS dbus,
         u.usage_quantity * pr.unit_usd             AS usd
  FROM system.billing.usage u
  JOIN price pr ON u.sku_name = pr.sku_name AND u.cloud = pr.cloud
    AND u.usage_start_time >= pr.price_start_time
    AND (pr.price_end_time IS NULL OR u.usage_start_time < pr.price_end_time)
  WHERE u.billing_origin_product = 'JOBS'
    AND u.product_features.performance_target IS NOT NULL   -- serverless jobs only
    AND u.usage_metadata.job_id IS NOT NULL
    AND u.usage_date >= current_date() - INTERVAL 8 WEEKS
),
top_jobs AS (
  SELECT job_id FROM sj WHERE mode = 'PERFORMANCE_OPTIMIZED'
  GROUP BY 1 ORDER BY sum(usd) DESC LIMIT 12
)
SELECT coalesce(jn.name, CAST(sj.job_id AS STRING))                              AS job_name,
       sj.week,
       round(sum(CASE WHEN sj.mode = 'PERFORMANCE_OPTIMIZED' THEN sj.usd ELSE 0 END), 2) AS perf_optimized_usd,
       round(sum(CASE WHEN sj.mode = 'STANDARD'              THEN sj.usd ELSE 0 END), 2) AS standard_usd,
       round(sum(sj.dbus), 1)                                                    AS dbus
FROM sj JOIN top_jobs USING (job_id)
LEFT JOIN job_name jn ON sj.workspace_id = jn.workspace_id AND sj.job_id = jn.job_id
GROUP BY 1, 2
ORDER BY job_name, sj.week
""",
    },
    {
        "id": "M5",
        "group": "warehouse", "order": 4,
        "use_when": "you set (or plan to set) a statement timeout — baseline the over-cap runners now, "
                    "confirm zero after.",
        "title": "Timeout guardrail scoreboard",
        "eli5": "Counts statements per warehouse that ran longer than a one-hour cap — "
                "your baseline before setting timeouts, and zero after.",
        "what": "A statement timeout caps EXECUTION time (time actually running), not total "
                "elapsed time (which includes queueing and fetching results), so this tests "
                "execution time — what a timeout would really have killed. Baseline now "
                "(expect the multi-hour offenders); confirm zero after the caps go in.",
        "chart": "table",
        "sql": """
SELECT compute.warehouse_id,
       date_trunc('WEEK', start_time)                          AS week,
       count_if(execution_duration_ms > 3600*1000)             AS stmts_over_cap,
       round(sum(CASE WHEN execution_duration_ms > 3600*1000
                      THEN execution_duration_ms ELSE 0 END)/3600000.0, 1) AS over_cap_exec_hours,
       round(max(execution_duration_ms)/3600000.0, 1)          AS longest_exec_hours
FROM system.query.history
WHERE start_time >= current_date() - INTERVAL 8 WEEKS
GROUP BY 1, 2 ORDER BY over_cap_exec_hours DESC
""",
    },
    {
        "id": "M4",
        "group": "benchmark", "order": 1,
        "use_when": "you fixed read-amplification or spill and want to prove work got cheaper PER RUN "
                    "(not just quieter) — watch the averages fall while run count holds.",
        "title": "Benchmark a fix — per-statement efficiency, week over week",
        "eli5": "For the busiest warehouses, average read / spill / duration per statement each "
                "week — so after a fix you can see the per-run numbers fall.",
        "what": "This is the before/after benchmark. Pick the warehouse behind a fix and read down "
                "its weekly rows: average bytes read, spilled, and execution minutes per statement. "
                "A successful read-amplification or spill fix shows the averages step DOWN in the "
                "weeks after you ship it, while the row count (runs) holds — proof the work got "
                "cheaper per run, not just quieter.",
        "chart": "table",
        "sql": """
WITH wh AS (SELECT warehouse_id, warehouse_name FROM system.compute.warehouses
            QUALIFY ROW_NUMBER() OVER (PARTITION BY warehouse_id ORDER BY change_time DESC)=1 AND delete_time IS NULL),
top_wh AS (   -- limit to the busiest warehouses so the table stays legible
  SELECT compute.warehouse_id AS warehouse_id
  FROM system.query.history
  WHERE start_time >= current_date() - INTERVAL 8 WEEKS AND execution_duration_ms > 0
  GROUP BY 1 ORDER BY sum(execution_duration_ms) DESC LIMIT 8
)
SELECT coalesce(wh.warehouse_name, q.warehouse_id)   AS warehouse,
       date_trunc('WEEK', q.start_time)              AS week,
       count(*)                                      AS runs,
       round(avg(q.read_bytes)/POW(1024,3), 1)       AS avg_read_gib,
       round(avg(q.spilled_local_bytes)/POW(1024,3), 2) AS avg_spill_gib,
       round(avg(q.execution_duration_ms)/60000.0, 2)   AS avg_exec_min
FROM (
  SELECT compute.warehouse_id AS warehouse_id, start_time, read_bytes, spilled_local_bytes, execution_duration_ms
  FROM system.query.history
  WHERE start_time >= current_date() - INTERVAL 8 WEEKS AND execution_duration_ms > 0
) q
JOIN top_wh USING (warehouse_id)
LEFT JOIN wh ON q.warehouse_id = wh.warehouse_id
GROUP BY 1, 2
ORDER BY 1, 2
""",
    },
]

# ── Alert specs — what turns a hunt result into a triage-queue alert ──────────
#
# Only the WASTE-SIGNAL hunts raise alerts. The inventory/reference hunts
# (H4/H9/H12/H14) are excluded — they list every warehouse/job by design, so a
# row is not a finding. The full-scan engine runs each hunt below and grades it
# with the rule for its `kind` (scan.GRADERS). We never invent a dollar figure
# the query didn't produce.
#
# kind "anomaly": is the ACCOUNT behaving abnormally, and who caused it? The hunt
#   SQL computes one account-level metric (everything visible in the system
#   tables) for the current window and for each of the 4 equivalent windows before
#   it (28 days), then returns the current window's TOP CONTRIBUTORS, largest
#   first. Every row carries the same account columns:
#     <metric_col>   this contributor's value in the current window
#     share_pct      that value as a % of account_value
#     account_value  the account's metric in the current window
#     baseline       the account's normal: the median of the 4 prior windows
#                    (a total for additive metrics, the window's peak for peak
#                    metrics, see `aggregate`); NULL when that median is 0
#     vs_normal      account_value / baseline; NULL when there is no normal (NEW)
#   vs_normal >= ANOMALY_HIGH_X is high, >= ANOMALY_MEDIUM_X is medium, NEW is
#   medium, anything else is low (no alert).
# kind "presence": a config state with no time series. Any row is a medium alert.


class AnomalySpec(TypedDict):
    kind: Literal["anomaly"]
    aggregate: Literal["additive", "peak"]  # account = sum of contributors, or the window's peak
    entity_col: str        # column that names the contributor in the headline
    metric_col: str        # the contributor's value in the current window
    metric_label: str      # what the account value is
    unit: str              # "usd" | "ratio" | "count" | "tib" | "hours" | "minutes"
    baseline: str          # plain-English definition of the account's normal
    summary: str           # headline; may use {account} {x} {baseline}
    new_summary: str       # headline when the account has no normal yet; may use {account}
    usd_col: Optional[str]  # a real list-price $ column, or None


class PresenceSpec(TypedDict):
    kind: Literal["presence"]
    entity_col: str
    metric_label: str
    summary: str           # headline; may use {entity} {n}
    usd_col: Optional[str]


AlertSpec = Union[AnomalySpec, PresenceSpec]

ALERT_SPECS: dict[str, AlertSpec] = {
    "H0": {"kind": "anomaly", "entity_col": "caller", "metric_col": "tokens",
           "metric_label": "Tokens in one hour", "unit": "count",
           "baseline": "95th percentile of the (endpoint, caller) pair's active hours over the "
                       "prior 28 days, floored at the workspace's 95th-percentile pair-hour",
           "summary": "{entity}: {x} its usual busy-hour AI token rate ({metric} tokens in an hour, normal ~{baseline}).",
           "new_summary": "NEW: {entity} first seen calling AI in this window ({metric} tokens in an hour).",
           "usd_col": "est_usd_rough"},
    "H1": {"kind": "anomaly", "entity_col": "merge_target", "metric_col": "read_tib",
           "metric_label": "Heaviest MERGE run this week", "unit": "tib",
           "baseline": "median read of the same MERGE target's runs over the prior 28 days, "
                       "floored at the workspace's 95th-percentile MERGE",
           "summary": "{entity}: a MERGE read {x} its usual ({metric}, normal ~{baseline} per run).",
           "new_summary": "NEW: MERGE into {entity} first seen this window ({metric} read).",
           "usd_col": None},
    "H2": {"kind": "anomaly", "entity_col": "source", "metric_col": "spill_tib",
           "metric_label": "Worst spill this week", "unit": "tib",
           "baseline": "median spill per spilling statement from the same source over the prior "
                       "28 days, floored at the workspace median",
           "summary": "{entity}: a statement spilled {x} its usual ({metric}, normal ~{baseline}).",
           "new_summary": "NEW: {entity} first seen spilling this window ({metric}).",
           "usd_col": None},
    "H3": {"kind": "anomaly", "entity_col": "client", "metric_col": "stmts",
           "metric_label": "Tiny statements this week", "unit": "count",
           "baseline": "median of the client's prior four weeks, floored at the workspace's "
                       "95th-percentile client-week",
           "summary": "{entity}: {x} its usual tiny statements ({metric} this week, normal ~{baseline}/week).",
           "new_summary": "NEW: {entity} first seen this window ({metric} tiny statements this week).",
           "usd_col": None},
    "H5": {"kind": "anomaly", "entity_col": "source", "metric_col": "total_min",
           "metric_label": "Statement over 4 hours", "unit": "minutes",
           "baseline": "median of the source's longest statement per day over the prior 28 days, "
                       "floored at the workspace's 95th percentile",
           "summary": "{entity}: a statement ran {x} its usual longest ({metric}, normal ~{baseline}).",
           "new_summary": "NEW: {entity} first seen this window, with a {metric} statement.",
           "usd_col": None},
    "H6": {"kind": "anomaly", "entity_col": "executed_by", "metric_col": "read_tib",
           "metric_label": "Heaviest scan hour this week", "unit": "tib",
           "baseline": "95th percentile of the person's active hours over the prior 28 days, "
                       "floored at the workspace's 95th-percentile person-hour",
           "summary": "{entity}: {x} their usual busy-hour scan volume ({metric} in one hour, normal ~{baseline}).",
           "new_summary": "NEW: {entity} first seen scanning this window ({metric} in one hour).",
           "usd_col": None},
    "H7": {"kind": "anomaly", "entity_col": "poller", "metric_col": "execs",
           "metric_label": "Short SELECTs this week", "unit": "count",
           "baseline": "median of the group's prior four weeks, floored at the workspace's "
                       "95th-percentile group-week",
           "summary": "{entity}: {x} its usual short queries ({metric} this week, normal ~{baseline}/week).",
           "new_summary": "NEW: {entity} first seen polling this window ({metric} short queries this week).",
           "usd_col": None},
    "H8": {"kind": "anomaly", "entity_col": "warehouse", "metric_col": "queued_hours",
           "metric_label": "Queue wait this week", "unit": "hours",
           "baseline": "median of the warehouse's prior four weeks that had any queueing, floored "
                       "at the workspace's 95th-percentile queueing warehouse-week",
           "summary": "{entity}: queued {x} its usual ({metric} waiting this week, normal ~{baseline}/week).",
           "new_summary": "NEW: {entity} started queueing this window ({metric} waiting this week).",
           "usd_col": None},
    # ── H16–H20 (Aug 2026 thread additions) ──
    "H16": {"kind": "anomaly", "entity_col": "job_name", "metric_col": "run_count",
            "metric_label": "All-Purpose runs this week", "unit": "count",
            "baseline": "median of the job's prior four weeks on that cluster, floored at the "
                        "workspace's median job-week on All-Purpose",
            "summary": "{entity}: {x} its usual All-Purpose runs (normal ~{baseline}/week).",
            "new_summary": "NEW: {entity} first seen on All-Purpose compute this window ({metric} runs this week).",
            "usd_col": None},
    "H17": {"kind": "presence", "entity_col": "warehouse_name",
            "metric_label": "Classic warehouses with auto-stop off", "usd_col": None,
            "summary": "{n} classic warehouse(s) never auto-stop, including {entity}."},
    "H18": {"kind": "anomaly", "entity_col": "cluster", "metric_col": "node_minutes",
            "metric_label": "Low-CPU node-minutes this week", "unit": "count",
            "baseline": "median of the cluster's prior four weeks, floored at the workspace's median "
                        "interactive cluster-week",
            "summary": "{entity}: idled {x} its usual runtime ({metric} node-minutes this week, normal ~{baseline}/week).",
            "new_summary": "NEW: {entity} first seen this window, idling for {metric} node-minutes.",
            "usd_col": None},
    "H19": {"kind": "anomaly", "entity_col": "app_name", "metric_col": "dbus_7d",
            "metric_label": "Always-on App DBUs this week", "unit": "count",
            "baseline": "median of the app's prior four weeks, floored at the workspace's median app-week",
            "summary": "{entity}: {x} its usual DBUs ({metric} this week, normal ~{baseline}/week).",
            "new_summary": "NEW: {entity} first seen billing this window ({metric} DBUs this week).",
            "usd_col": None},
    "H20": {"kind": "anomaly", "entity_col": "job_name", "metric_col": "wasted_hours",
            "metric_label": "Compute on failed runs this week", "unit": "hours",
            "baseline": "median of the job's prior four weeks that had failures, floored at the "
                        "workspace's 95th-percentile failing job-week",
            "summary": "{entity}: wasted {x} its usual on failed runs ({metric} this week, normal ~{baseline}/week).",
            "new_summary": "NEW: {entity} started failing this window ({metric} of compute on failed runs).",
            "usd_col": None},
    "H21": {"kind": "presence", "entity_col": "schema_name",
            "metric_label": "Schemas with likely-snappy Delta tables", "usd_col": None,
            "summary": "{n} schema(s) hold Delta tables that predate the zstd default, including {entity}."},
    "H22": {"kind": "anomaly", "entity_col": "job_name", "metric_col": "usd_7d",
            "metric_label": "Performance-optimized spend this week", "unit": "usd",
            "baseline": "median of the job's prior four weeks in this mode, floored at the "
                        "workspace's 95th-percentile job-week",
            "summary": "{entity}: {x} its usual Performance-optimized spend ({metric} this week, normal ~{baseline}/week).",
            "new_summary": "NEW: {entity} first seen in Performance-optimized mode this window ({metric} this week).",
            "usd_col": "usd_7d"},
    "H23": {"kind": "anomaly", "entity_col": "po_target", "metric_col": "dbus_last_7d",
            "metric_label": "PO DBUs, last 7 complete days", "unit": "count",
            "baseline": "median of the table and operation's prior four weeks, floored at the "
                        "workspace's 95th-percentile table-week",
            "summary": "{entity}: {x} its usual Predictive Optimization DBUs ({metric} in the last 7 days, normal ~{baseline}/week).",
            "new_summary": "NEW: {entity} first seen under Predictive Optimization this window ({metric} DBUs).",
            "usd_col": None},
}

