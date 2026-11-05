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
    """CTE `acct` from a CTE of (wk, v), the account metric per window. The spine
    is the current window plus the four 7-day windows of the prior 28 days; a
    window with no rows counts as 0, so a quiet week pulls the median down honestly."""
    return f"""acct AS (
  SELECT max(CASE WHEN wk = 0 THEN v END)                AS account_value,
         percentile(CASE WHEN wk > 0 THEN v END, 0.5)    AS normal
  FROM (
    SELECT s.wk, coalesce(a.v, 0) AS v
    FROM (SELECT posexplode(sequence(current_date() - INTERVAL 28 DAYS, current_date(), INTERVAL 7 DAYS)) AS (wk, d)) s
    LEFT JOIN {weekly} a USING (wk)
  )
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
        "eli5": "Watches the account's AI token rate hour by hour, so a runaway shows up the same "
                "day, before the daily bill even updates.",
        "what": "The account's busiest AI hour this week (every endpoint and caller together), set "
                "against its usual busiest hour: the median of its busiest hour in each of the prior "
                "four weeks. vs_normal is how many times that normal this week's peak is, and that "
                "sets the alert. Each row is one (endpoint, caller) pair inside that peak hour with "
                "its share of the hour's tokens, so the top row is who drove the spike. "
                "“est_usd_rough” is an estimate built from your own recent bill; read it as a "
                "relative signal (“who spiked”), not an exact invoice.",
        "action": "Layer three controls: (1) a SQL alert on the hourly rate so a spike is "
                  "caught fast; (2) a daily billing-dollar alert as the reconciling truth; "
                  "(3) a monthly budget as a coarse net. Prevent repeats by not granting "
                  "expensive models (Sonnet/Opus-class) to every pipeline by default — "
                  "default to a cheaper tier (e.g. Haiku), cap max_tokens, and only run the "
                  "model over NEW rows each refresh, never the whole table again.",
        "measure": "M2",
        "explain_hunt_desc": "the account's busiest AI token hour this week versus its usual busiest "
                             "hour, and the (endpoint, caller) pairs that drove it",
        "explain_rubric": (
            "account_value is the account's tokens in its busiest hour this week, across every "
            "endpoint and caller; baseline is the median of the account's busiest hour in each of the "
            "prior four weeks; vs_normal = account_value / baseline (NULL = no prior AI usage). Say "
            "first whether the account is running above its normal. Each row is one (endpoint, "
            "requester) pair inside that peak hour; share_pct is its share of the hour's tokens, so "
            "the top rows are who drove it. requester_type = service principal means an automated "
            "pipeline. est_usd_rough is a rate-blended estimate (the pair's tokens x the account's "
            "own 7-day effective $/1M-token rate, NULL when there is no rate for that endpoint); read it "
            "relative, not as an invoice. source = AI_QUERY marks a SQL ai_query() call."
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
  WHERE g.tok > 0
),
hourly AS (
  SELECT *, {_wk("hr")} AS wk, in_tok + out_tok AS tokens
  FROM (
    SELECT date_trunc('hour', event_time) AS hr, invocation_metadata.source AS source,
           endpoint_name, requester, requester_type,
           coalesce(sum(input_tokens), 0) AS in_tok, coalesce(sum(output_tokens), 0) AS out_tok
    FROM system.ai_gateway.usage
    WHERE event_time >= current_timestamp() - INTERVAL 35 DAYS
    GROUP BY 1, 2, 3, 4, 5
  )
),
account_hours AS (SELECT wk, hr, sum(tokens) AS v FROM hourly WHERE wk BETWEEN 0 AND 4 GROUP BY 1, 2),
weekly AS (SELECT wk, max(v) AS v FROM account_hours GROUP BY 1),
peak AS (SELECT max_by(hr, v) AS hr FROM account_hours WHERE wk = 0),
{_account_normal("weekly")},
contrib AS (
  SELECT h.*, round(h.tokens / 1e6 * r.usd_1m, 2) AS est_usd_rough
  FROM hourly h JOIN peak p ON h.hr = p.hr LEFT JOIN rate r ON h.endpoint_name = r.model
)
SELECT hr, source, endpoint_name, requester, requester_type, in_tok, out_tok, est_usd_rough,
       endpoint_name || ' / ' || coalesce(requester, 'unknown caller') AS caller,
       tokens,
       {_vs_account("tokens")}
FROM contrib CROSS JOIN acct
ORDER BY tokens DESC
LIMIT 20
""",
    },

    # ── H1 · reads ─────────────────────────────────────────────────────────────
    {
        "id": "H1",
        "category": "reads",
        "title": "Read amplification",
        "eli5": "Checks how much data the account's MERGE statements read this week against its usual "
                "week, then names the MERGE targets behind it; an “incremental” job quietly "
                "re-scanning everything shows up here.",
        "what": "The TiB every MERGE in the account read this week, set against its usual week (the "
                "median of the prior four weeks). vs_normal is how many times that normal this week "
                "is, and that sets the alert. Each row is one MERGE target (or the job, notebook or "
                "person behind it) with its runs, the TiB it read this week and its share of the "
                "account's MERGE reads, so the top row is where the extra reading went. A high "
                "read-to-write ratio says the MERGE reads far more than it changes. If the target "
                "table is small but the reads are huge, the cost is upstream (the source query has no "
                "date filter). If each run reads about twice the target's size, the merge is scanning "
                "the whole target in both its match and rewrite phases.",
        "action": "Bound the source with an incremental watermark so it only reads new "
                  "rows; bound the target with incremental_predicates on a date column; "
                  "cluster the target on that same column so old data can be skipped; and "
                  "update only the rows that changed.",
        "measure": "M4",
        "explain_hunt_desc": "the account's MERGE read volume this week versus its usual week, and the "
                             "MERGE targets that drove it (read amplification)",
        "explain_rubric": (
            "account_value is the TiB all MERGE statements in the account read this week; baseline is "
            "the median of the prior four weekly totals; vs_normal = account_value / baseline (NULL = "
            "no prior MERGE history). Say first whether the account is reading more than its normal. "
            "Each row is one MERGE target this week; share_pct is its share of the account's MERGE "
            "reads, and read_write_ratio shows how much it reads per byte written. Compare read_tib "
            "per run against the target's actual size (DESCRIBE DETAIL <target> -> sizeInBytes, see "
            "H15): if the target is small but reads are huge, the cost is upstream (the source "
            "query); if reads per run are about twice the target size, the merge scans the whole "
            "target in both its match and rewrite phases."
        ),
        "sql": f"""
WITH merges AS (
  SELECT statement_id, executed_by, read_bytes, written_bytes, total_duration_ms,
         read_bytes/POW(1024,4) AS read_tib, {_wk("start_time")} AS wk,
         coalesce(nullif(regexp_extract(statement_text, '(?i)MERGE\\\\s+INTO\\\\s+([`\\\\w.]+)', 1), ''),
                  'job ' || query_source.job_info.job_id, 'notebook ' || query_source.notebook_id,
                  executed_by) AS merge_target
  FROM system.query.history
  WHERE statement_type = 'MERGE' AND read_bytes > 0
    AND start_time >= current_timestamp() - INTERVAL 35 DAYS
),
weekly AS (SELECT wk, sum(read_tib) AS v FROM merges WHERE wk BETWEEN 0 AND 4 GROUP BY 1),
{_account_normal("weekly")},
contrib AS (
  SELECT merge_target, count(*) AS runs, sum(read_tib) AS read_tib,
         sum(written_bytes)/POW(1024,3)                         AS written_gib,
         CAST(sum(read_bytes) AS DOUBLE)/NULLIF(sum(written_bytes),0) AS read_write_ratio,
         sum(total_duration_ms)/60000.0                         AS mins,
         max_by(statement_id, read_bytes)                       AS heaviest_statement_id,
         max_by(executed_by, read_bytes)                        AS executed_by
  FROM merges WHERE wk = 0
  GROUP BY 1
)
SELECT merge_target, executed_by, heaviest_statement_id, runs, read_tib, written_gib,
       read_write_ratio, mins,
       {_vs_account("read_tib")}
FROM contrib CROSS JOIN acct
ORDER BY read_tib DESC
LIMIT 20
""",
    },

    # ── H2 · reads ─────────────────────────────────────────────────────────────
    {
        "id": "H2",
        "category": "reads",
        "title": "Disk spill",
        "eli5": "Checks how much working data the account's queries spilled to disk this week (they "
                "ran out of memory, which is slower and you pay for the extra time) against its usual "
                "week, then names the sources behind it.",
        "what": "The TiB every statement in the account spilled to disk this week, set against its "
                "usual week (the median of the prior four weeks). vs_normal is how many times that "
                "normal this week is, and that sets the alert. Each row is one source of spilling "
                "statements (a job, notebook, saved query, dashboard or person) with its spill and "
                "share of the account's spill. Compare spill to the input size. Spill much LARGER "
                "than the input is a query-shape problem: a CTE recomputed many times, de-duplication "
                "done after a join instead of before, or an unbounded lookback. Spill roughly equal "
                "to the input is a sizing problem.",
        "action": "Rewrite first — shorten lookbacks, stage a re-used CTE into a temp "
                  "table, and de-duplicate the small side before joins. Only if a clean query "
                  "still spills should you increase the warehouse/cluster size. Open the worst "
                  "offender's query profile to see which step spills.",
        "measure": "M4",
        "explain_hunt_desc": "the account's disk spill this week versus its usual week, and the sources "
                             "that drove it (memory pressure)",
        "explain_rubric": (
            "account_value is the TiB the whole account spilled to local disk this week; baseline is "
            "the median of the prior four weekly totals; vs_normal = account_value / baseline (NULL = "
            "no prior spill history). Say first whether the account is spilling more than its normal. "
            "Each row is one source this week; share_pct is its share of the account's spill. Compare "
            "spill_tib to read_gib. Spill far larger than the input is a query-shape problem: re-used "
            "CTEs recomputed per reference, deduplication after a join instead of before, unbounded "
            "lookback windows. Spill roughly proportional to input on a well-shaped query is a sizing "
            "problem."
        ),
        "sql": f"""
WITH spills AS (
  SELECT statement_id, executed_by, read_bytes, total_duration_ms,
         spilled_local_bytes/POW(1024,4) AS spill_tib, {_wk("start_time")} AS wk,
         coalesce('job ' || query_source.job_info.job_id, 'notebook ' || query_source.notebook_id,
                  'query ' || query_source.sql_query_id, 'dashboard ' || query_source.dashboard_id,
                  executed_by) AS source
  FROM system.query.history
  WHERE spilled_local_bytes > 0
    AND start_time >= current_timestamp() - INTERVAL 35 DAYS
),
weekly AS (SELECT wk, sum(spill_tib) AS v FROM spills WHERE wk BETWEEN 0 AND 4 GROUP BY 1),
{_account_normal("weekly")},
contrib AS (
  SELECT source, count(*) AS statements, sum(spill_tib) AS spill_tib,
         sum(read_bytes)/POW(1024,3)          AS read_gib,
         sum(total_duration_ms)/60000.0       AS mins,
         max_by(statement_id, spill_tib)      AS worst_statement_id,
         max_by(executed_by, spill_tib)       AS executed_by
  FROM spills WHERE wk = 0
  GROUP BY 1
)
SELECT source, executed_by, worst_statement_id, statements, spill_tib, read_gib, mins,
       {_vs_account("spill_tib")}
FROM contrib CROSS JOIN acct
ORDER BY spill_tib DESC
LIMIT 20
""",
    },

    # ── H3 · idle ──────────────────────────────────────────────────────────────
    {
        "id": "H3",
        "category": "idle",
        "title": "Chatter keeps warehouses awake",
        "eli5": "Checks how many do-nothing statements (ones that read no data) the account fired this "
                "week against its usual week; each one resets a warehouse's auto-stop timer, so the "
                "warehouse never sleeps. Then names the tools firing them.",
        "what": "The number of statements across the account that read no data this week, set against "
                "its usual week (the median of the prior four weeks). vs_normal is how many times that "
                "normal this week is, and that sets the alert. Each row is one client with its share of "
                "that chatter. “client_application” and “executed_by” name the tool exactly: "
                "this is configuration, not guesswork. “stmts_per_session near 1” means the tool "
                "opens a fresh connection for every statement.",
        "action": "Fix the tool first — e.g. a BI tool's refresh schedule left at its "
                  "aggressive default, or a monitoring tool that should have its own small "
                  "warehouse. Then move always-on tools onto a small serverless warehouse where "
                  "a short auto-stop is practical.",
        "measure": "M3",
        "explain_hunt_desc": "the account's zero-read statements this week versus its usual week, and the "
                             "chatty clients keeping warehouses awake",
        "explain_rubric": (
            "account_value is the number of statements across the account that read no data this week; "
            "baseline is the median of the prior four weekly totals; vs_normal = account_value / "
            "baseline (NULL = no prior history). Say first whether the account's chatter is above its "
            "normal. Each row is one client; share_pct is its share of the chatter. client_application "
            "and executed_by name the tool - it's configuration, not guesswork. stmts_per_session near 1 "
            "means the client opens a fresh connection per statement (pool churn)."
        ),
        "sql": f"""
WITH wh AS (SELECT warehouse_id, warehouse_name FROM system.compute.warehouses QUALIFY ROW_NUMBER() OVER (PARTITION BY warehouse_id ORDER BY change_time DESC)=1 AND delete_time IS NULL),
chatter AS (
  SELECT compute.warehouse_id AS warehouse_id, statement_type, executed_by, client_application,
         client_driver, session_id, {_wk("start_time")} AS wk
  FROM system.query.history
  WHERE read_bytes = 0
    AND start_time >= current_timestamp() - INTERVAL 35 DAYS
),
weekly AS (SELECT wk, count(*) AS v FROM chatter WHERE wk BETWEEN 0 AND 4 GROUP BY 1),
{_account_normal("weekly")},
contrib AS (
  SELECT warehouse_id, statement_type, executed_by, client_application, client_driver,
         count(*) AS stmts, count(DISTINCT session_id) AS sessions,
         round(count(*)/count(DISTINCT session_id),1) AS stmts_per_session
  FROM chatter WHERE wk = 0
  GROUP BY 1, 2, 3, 4, 5
)
SELECT coalesce(wh.warehouse_name, c.warehouse_id) AS warehouse, c.statement_type, c.executed_by,
       c.client_application, c.client_driver, c.stmts, c.sessions, c.stmts_per_session,
       coalesce(c.client_application, c.client_driver, 'unknown client') || ' (' || c.executed_by
         || ') on ' || coalesce(wh.warehouse_name, c.warehouse_id, 'a cluster') AS client,
       {_vs_account("c.stmts")}
FROM contrib c LEFT JOIN wh ON c.warehouse_id = wh.warehouse_id CROSS JOIN acct
ORDER BY c.stmts DESC
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
        "title": "Statements far longer than the account's usual",
        "eli5": "Checks the account's longest statement this week against the longest it usually "
                "runs in a week, and splits that time into waiting-for-capacity vs starting-up vs "
                "actually-running.",
        "what": "The account's longest statement this week, set against its usual longest: the "
                "median of the longest statement in each of the prior four weeks. vs_normal is how "
                "many times that normal this week's longest ran, and that sets the alert. Each row is "
                "one source (job, notebook, saved query, dashboard or person) with its longest "
                "statement this week; share_pct is that statement's length as a % of the account's "
                "longest, so the top row is the culprit. The split tells you which of three problems "
                "you have. Long execution time plus a huge read = a genuine heavy scan. Long execution "
                "time plus tiny reads = a HUNG statement (check execution_status and the query "
                "profile). A large queued time = a capacity problem (see Queue Pressure).",
        "action": "Set a workspace-wide statement timeout so runaways stop themselves; the platform "
                  "default is TWO DAYS. A few times the account's usual longest statement (the normal "
                  "this check shows) is a sensible starting point, and long ETL can raise its own "
                  "limit per session. Note: changing the workspace setting restarts running "
                  "warehouses, so schedule it.",
        "measure": "M5",
        "explain_hunt_desc": "the account's longest statement this week versus its usual longest, "
                             "split into queued vs executing, and the sources behind it",
        "explain_rubric": (
            "account_value is the account's longest statement this week in minutes; baseline is the "
            "median of the account's longest statement in each of the prior four weeks; vs_normal = "
            "account_value / baseline (NULL = no prior history). Say first whether the account's "
            "longest is above its normal. Each row is one source's longest statement this week; "
            "share_pct is its length as a % of the account's longest. Long exec_min + huge read_gib = "
            "a genuine heavy scan (see H6). Long exec_min + tiny reads = a hung statement (check "
            "execution_status and the query profile). Large queued_min = a capacity problem (see H8)."
        ),
        "sql": f"""
WITH stmts AS (
  SELECT statement_id, start_time, {_wk("start_time")} AS wk,
         total_duration_ms/60000.0               AS total_min,
         waiting_at_capacity_duration_ms/60000.0 AS queued_min,
         waiting_for_compute_duration_ms/60000.0 AS provisioning_min,
         execution_duration_ms/60000.0           AS exec_min,
         read_files, read_bytes/POW(1024,3)      AS read_gib,
         coalesce('job ' || query_source.job_info.job_id, 'notebook ' || query_source.notebook_id,
                  'query ' || query_source.sql_query_id, 'dashboard ' || query_source.dashboard_id,
                  executed_by) AS source
  FROM system.query.history
  WHERE start_time >= current_timestamp() - INTERVAL 35 DAYS
),
weekly AS (SELECT wk, max(total_min) AS v FROM stmts WHERE wk BETWEEN 0 AND 4 GROUP BY 1),
{_account_normal("weekly")},
contrib AS (
  SELECT * FROM stmts WHERE wk = 0
  QUALIFY row_number() OVER (PARTITION BY source ORDER BY total_min DESC) = 1
)
SELECT statement_id, start_time, source, total_min, queued_min, provisioning_min, exec_min,
       read_files, read_gib,
       {_vs_account("total_min")}
FROM contrib CROSS JOIN acct
ORDER BY total_min DESC
LIMIT 20
""",
    },

    # ── H6 · reads ─────────────────────────────────────────────────────────────
    {
        "id": "H6",
        "category": "reads",
        "title": "Unusually heavy scan hours across the account",
        "eli5": "Checks the account's heaviest scanning hour this week against its usual heaviest "
                "hour, then names who did the scanning; often one investigation re-scanning a raw "
                "table over and over.",
        "what": "The account's busiest scan hour this week (bytes read by everyone's SELECTs), set "
                "against its usual busiest hour: the median of its busiest hour in each of the prior "
                "four weeks. vs_normal is how many times that normal this week's peak is, and that "
                "sets the alert. Each row is one person who scanned in that hour, with their share of "
                "its bytes. One person with a large share and many scans (the scans column) is "
                "usually a single incident hunt: someone searching a raw event table with "
                "‘LIKE %some-id%’ and re-paying the full scan on every refinement. Open the "
                "statements in Query History to confirm the shape.",
        "action": "Give investigators a two-line template: first, time-bound the opening scan "
                  "on the table's date column; second, stage that first pass into a temp table "
                  "and run every refinement against the small staged copy, so the repeated scans "
                  "become one. Longer term, promote the searched fields to real clustered columns, or "
                  "evaluate a full-text search index.",
        "measure": "M1",
        "explain_hunt_desc": "the account's heaviest scan hour this week versus its usual heaviest "
                             "hour, and the people who scanned in it (often a re-paid investigation scan)",
        "explain_rubric": (
            "account_value is the TiB the whole account's SELECTs read in its busiest hour this week; "
            "baseline is the median of the account's busiest hour in each of the prior four weeks; "
            "vs_normal = account_value / baseline (NULL = no prior history). Say first whether the "
            "account's peak is above its normal. Each row is one principal who scanned in that hour; "
            "share_pct is their share of its bytes. A large share with many scans is usually one "
            "incident hunt re-paying a full scan per refinement. Open the statements in the Query "
            "History UI to confirm the shape."
        ),
        "sql": f"""
WITH hourly AS (
  SELECT *, {_wk("hour")} AS wk
  FROM (
    SELECT executed_by, date_trunc('HOUR', start_time) AS hour,
           count(*)                          AS scans,
           sum(read_bytes)/POW(1024,4)       AS read_tib,
           max(total_duration_ms)/3600000.0  AS longest_hours
    FROM system.query.history
    WHERE statement_type = 'SELECT' AND read_bytes > 0
      AND start_time >= current_timestamp() - INTERVAL 35 DAYS
    GROUP BY 1, 2
  )
),
account_hours AS (SELECT wk, hour, sum(read_tib) AS v FROM hourly WHERE wk BETWEEN 0 AND 4 GROUP BY 1, 2),
weekly AS (SELECT wk, max(v) AS v FROM account_hours GROUP BY 1),
peak AS (SELECT max_by(hour, v) AS hour FROM account_hours WHERE wk = 0),
{_account_normal("weekly")},
contrib AS (SELECT h.* FROM hourly h JOIN peak p ON h.hour = p.hour)
SELECT executed_by, hour, scans, read_tib, longest_hours,
       {_vs_account("read_tib")}
FROM contrib CROSS JOIN acct
ORDER BY read_tib DESC
LIMIT 50
""",
    },

    # ── H7 · longrun ─────────────────────────────────────────────────────────────
    {
        "id": "H7",
        "category": "longrun",
        "title": "Repeated short queries (polling)",
        "eli5": "Checks how many short queries the account ran this week against its usual week, then "
                "names the identities and sources behind them, typically an orchestrator checking "
                "metadata more often than needed.",
        "what": "“Short” means at or under the account's own typical SELECT duration (the median "
                "over the prior four weeks), so the definition fits any account. The number of short "
                "SELECTs across the account this week is set against its usual week (the median of the "
                "prior four weeks). vs_normal is how many times that normal this week is, and that sets "
                "the alert. Each row is one identity and query source with its share of those "
                "queries. A group with a large share is a CANDIDATE poller, not a verdict; confirm "
                "before acting. ‘execution_hours’ is the total compute weight. The result names the "
                "culprit (a saved query, dashboard, or job) even when the SQL text is hidden. When all "
                "three are blank the row is labelled ‘unattributed, confirm before acting’ because "
                "unrelated short queries can pile into one row.",
        "action": "Go event-driven — use a table-update trigger to run work only when the "
                  "source changes, instead of polling. Or write the freshness marker to a tiny "
                  "one-row table (a single-row read beats a metadata scan). Or simply reduce the "
                  "frequency to what downstream actually needs (minutes, not seconds).",
        "measure": "M3",
        "explain_hunt_desc": ("the account's short SELECTs this week versus its usual week, and the "
                              "identities behind them (candidate polling; unattributed groups need "
                              "confirmation, do not assert waste)"),
        "explain_rubric": (
            "a short SELECT is one at or under the account's median SELECT duration over the prior four "
            "weeks. account_value is the number of short SELECTs across the account this week; "
            "baseline is the median of the prior four weekly totals; vs_normal = account_value / "
            "baseline (NULL = no prior history). Say first whether the account is polling more than its "
            "normal. Each row is one group; share_pct is its share. A group with a large share is a "
            "candidate poller, not a verdict - confirm before acting. p50_exec_ms shows how expensive "
            "each poll is; execution_hours is the aggregate execution weight (execution only, excluding "
            "queue/provisioning). When all query_source IDs are NULL the group is unattributed - "
            "unrelated short SELECTs can collapse into one row, so do not assert it is a poller."
        ),
        "sql": f"""
WITH selects AS (
  SELECT executed_by, client_application,
         query_source.sql_query_id            AS sql_query_id,
         query_source.dashboard_id            AS dashboard_id,
         query_source.job_info.job_id         AS job_id,
         statement_id, execution_duration_ms, total_duration_ms, {_wk("start_time")} AS wk
  FROM system.query.history
  WHERE statement_type = 'SELECT'
    AND start_time >= current_timestamp() - INTERVAL 35 DAYS
),
typical AS (SELECT percentile_approx(total_duration_ms, 0.5) AS short_ms FROM selects WHERE wk BETWEEN 1 AND 4),
polls AS (SELECT s.* FROM selects s CROSS JOIN typical t WHERE s.total_duration_ms <= t.short_ms),
weekly AS (SELECT wk, count(*) AS v FROM polls WHERE wk BETWEEN 0 AND 4 GROUP BY 1),
{_account_normal("weekly")},
contrib AS (
  SELECT executed_by,
         CASE WHEN executed_by LIKE '%@%' THEN 'human' ELSE 'service_principal' END AS owner_type,
         client_application, sql_query_id, dashboard_id, job_id,
         count(*)                                       AS execs,
         approx_percentile(execution_duration_ms, 0.5)  AS p50_exec_ms,
         sum(execution_duration_ms)/3600000.0           AS execution_hours,
         any_value(statement_id)                        AS example_statement_id,
         slice(array_agg(statement_id), 1, 3)           AS more_example_ids,
         CASE WHEN coalesce(sql_query_id, dashboard_id, job_id) IS NULL
              THEN 'unattributed, confirm before acting'
              ELSE 'candidate, confirm in Query History' END    AS status
  FROM polls WHERE wk = 0
  GROUP BY 1, 2, 3, 4, 5, 6
)
SELECT executed_by, owner_type, client_application, sql_query_id, dashboard_id, job_id,
       execs, p50_exec_ms, execution_hours, example_statement_id, more_example_ids, status,
       coalesce('query ' || sql_query_id, 'dashboard ' || dashboard_id, 'job ' || job_id,
                client_application, 'unattributed') || ' (' || executed_by || ')' AS poller,
       {_vs_account("execs")}
FROM contrib CROSS JOIN acct
ORDER BY execs DESC
LIMIT 50
""",
    },

    # ── H8 · config ─────────────────────────────────────────────────────────────
    {
        "id": "H8",
        "category": "config",
        "title": "Queue pressure vs capacity",
        "eli5": "Checks how long the account's queries waited for capacity this week against its usual "
                "week, then shows which warehouses the waiting happened on, next to how long they "
                "actually ran: the objective signal for right-sizing.",
        "what": "The hours statements across the account spent queued for capacity this week, set "
                "against its usual week (the median of the prior four weeks). vs_normal is how many "
                "times that normal this week is, and that sets the alert. Each row is one warehouse "
                "that queued this week, with its share of the account's queue wait. High queued time "
                "relative to execution time means the warehouse is undersized or its maximum cluster "
                "count is too low for peaks. Near-zero queueing with low utilisation means there is "
                "room to size DOWN. This is real demand, not perceived slowness.",
        "action": "Decide warehouse by warehouse, by load shape. Spiky, idle-heavy workloads "
                  "suit serverless (scales in seconds, stops in minutes). Steady, predictable "
                  "load can suit a right-sized classic warehouse with spot and committed-use "
                  "discounts. Keep different workload types on separate warehouses so one noisy "
                  "client can't queue everyone.",
        "measure": "M3",
        "explain_hunt_desc": "the account's queue wait this week versus its usual week, and the "
                             "warehouses it happened on",
        "inventory": True,  # ranks every warehouse — a reference view, not a waste list
        "explain_rubric": (
            "account_value is the hours statements across the account waited for capacity this week; "
            "baseline is the median of the prior four weekly totals; vs_normal = account_value / "
            "baseline (NULL = no prior queueing). Say first whether the account is queueing more than "
            "its normal. Each row is one warehouse; share_pct is its share of the queue wait. High "
            "queued_hours relative to exec_hours means the warehouse is undersized or its max cluster "
            "count is too low for peaks. Zero queueing with low utilisation (H14 for clusters, "
            "warehouse monitoring for DBSQL) means there is room to downsize."
        ),
        "sql": f"""
WITH wh AS (SELECT warehouse_id, warehouse_name FROM system.compute.warehouses QUALIFY ROW_NUMBER() OVER (PARTITION BY warehouse_id ORDER BY change_time DESC)=1 AND delete_time IS NULL),
stmts AS (
  SELECT compute.warehouse_id AS warehouse_id, {_wk("start_time")} AS wk,
         waiting_at_capacity_duration_ms, execution_duration_ms
  FROM system.query.history
  WHERE start_time >= current_timestamp() - INTERVAL 35 DAYS
),
weekly AS (SELECT wk, sum(waiting_at_capacity_duration_ms)/3600000.0 AS v FROM stmts WHERE wk BETWEEN 0 AND 4 GROUP BY 1),
{_account_normal("weekly")},
contrib AS (
  SELECT warehouse_id,
         count_if(waiting_at_capacity_duration_ms > 0)  AS stmts_queued,
         sum(waiting_at_capacity_duration_ms)/3600000.0 AS queued_hours,
         sum(execution_duration_ms)/3600000.0           AS exec_hours
  FROM stmts WHERE wk = 0
  GROUP BY 1
  HAVING sum(waiting_at_capacity_duration_ms) > 0
)
SELECT coalesce(wh.warehouse_name, c.warehouse_id, 'no warehouse') AS warehouse,
       c.stmts_queued, c.queued_hours, c.exec_hours,
       {_vs_account("c.queued_hours")}
FROM contrib c LEFT JOIN wh ON c.warehouse_id = wh.warehouse_id CROSS JOIN acct
ORDER BY c.queued_hours DESC
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
                "cost-per-table comparison. A pipeline that costs far more per table than its "
                "peers is usually explained by machine type, on-demand vs spot, or low CPU "
                "use (see Idle Cores). Check the pipeline's event log for WARN/ERROR messages too.",
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
                "Sustained CPU well below your other busy clusters on large nodes means idle "
                "cores you are paying for. Pick a cluster to drill into (in the notebook, query its hour-by-hour CPU "
                "and memory); low steady CPU with low memory = downsize, memory near full = "
                "memory-bound.",
        "action": "Right-size on this evidence: start with a small cluster and step up "
                  "only when the metrics say so. Use fewer, larger nodes for shuffle-heavy work "
                  "rather than many small ones.",
        "measure": "M2",
        "explain_hunt_desc": "busiest clusters over 7 days with average CPU (idle-core discovery)",
        "explain_rubric": (
            "each row is a cluster with node_minutes (how much it ran) and avg_cpu_pct. Sustained low "
            "avg_cpu_pct (well below the other listed clusters) on a cluster with high node_minutes = idle cores you are paying "
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
        "eli5": "Checks how many automated job runs the account put on All-Purpose (interactive) "
                "clusters this week against its usual week. All-Purpose bills a higher rate per DBU "
                "than Jobs compute for the same work, so these runs pay a premium.",
        "what": "All-Purpose compute is for interactive notebook work and bills a higher list rate "
                "than Jobs compute. A job pinned to an always-on All-Purpose cluster pays that premium "
                "AND the idle time between runs. The account's job runs on All-Purpose clusters "
                "(cluster_source UI/API) this week are set against its usual week (the median of the "
                "prior four weeks). vs_normal is how many times that normal this week is, and that "
                "sets the alert. Each row is one job with its share of those runs and the clusters it "
                "used. all_purpose_usd_dbu and jobs_usd_dbu are your own account's average list $/DBU "
                "for classic All-Purpose and Jobs compute over the last 30 days, and pct_saving is the "
                "rate saving those two imply.",
        "action": "Move the job to Jobs Compute (an ephemeral cluster that spins up per run and "
                  "terminates after) or Serverless Jobs. That drops it to the Jobs rate and removes "
                  "idle-between-runs billing. Interim: shorten the cluster's auto-termination so it "
                  "stops soon after the last run.",
        "measure": "M2",
        "explain_hunt_desc": "the account's job runs on All-Purpose clusters this week versus its usual "
                             "week, and the jobs behind them (they belong on cheaper Jobs compute)",
        "explain_rubric": (
            "account_value is the number of job runs across the account that used an ALL-PURPOSE "
            "cluster (cluster_source UI or API) this week; baseline is the median of the prior four "
            "weekly totals; vs_normal = account_value / baseline (NULL = no prior history). Say first "
            "whether the account is above its normal. Each row is one job; share_pct is its share of "
            "those runs. all_purpose_usd_dbu and jobs_usd_dbu are the account's own average list "
            "$/DBU for classic All-Purpose and Jobs compute over 30 days, and pct_saving is the rate "
            "saving they imply; quote those, never a fixed price. Name the jobs with the largest share "
            "as the migration targets; the fix is Jobs Compute / Serverless Jobs. Do not overstate - "
            "the saving is the rate difference on that job's DBUs, not the whole cluster."
        ),
        "sql": f"""
WITH latest_clusters AS (
  SELECT workspace_id, cluster_id, cluster_name, cluster_source
  FROM system.compute.clusters
  QUALIFY ROW_NUMBER() OVER (PARTITION BY workspace_id, cluster_id ORDER BY change_time DESC)=1
),
job_names AS (
  SELECT workspace_id, job_id, max(name) AS job_name FROM system.lakeflow.jobs GROUP BY 1,2
),
rates AS (
  SELECT try_divide(sum(CASE WHEN u.billing_origin_product = 'ALL_PURPOSE' THEN u.usage_quantity * p.pricing.effective_list.default END),
                    sum(CASE WHEN u.billing_origin_product = 'ALL_PURPOSE' THEN u.usage_quantity END)) AS all_purpose_usd_dbu,
         try_divide(sum(CASE WHEN u.billing_origin_product = 'JOBS' THEN u.usage_quantity * p.pricing.effective_list.default END),
                    sum(CASE WHEN u.billing_origin_product = 'JOBS' THEN u.usage_quantity END))        AS jobs_usd_dbu
  FROM system.billing.usage u
  JOIN system.billing.list_prices p ON u.sku_name = p.sku_name AND u.cloud = p.cloud
   AND u.usage_start_time >= p.price_start_time
   AND (p.price_end_time IS NULL OR u.usage_start_time < p.price_end_time)
  WHERE u.billing_origin_product IN ('ALL_PURPOSE', 'JOBS')
    AND NOT coalesce(u.product_features.is_serverless, false)
    AND u.usage_date >= current_date() - INTERVAL 30 DAYS
),
task_clusters AS (
  SELECT t.workspace_id, t.job_id, t.run_id, t.period_start_time, exploded.cluster_id
  FROM system.lakeflow.job_task_run_timeline t
  LATERAL VIEW explode(t.compute_ids) exploded AS cluster_id
  WHERE t.period_start_time >= current_timestamp() - INTERVAL 35 DAYS
),
runs AS (
  SELECT t.workspace_id, t.job_id, t.run_id, {_wk("min(t.period_start_time)")} AS wk,
         collect_set(coalesce(c.cluster_name, c.cluster_id)) AS clusters
  FROM task_clusters t
  JOIN latest_clusters c ON t.workspace_id = c.workspace_id AND t.cluster_id = c.cluster_id
  WHERE c.cluster_source IN ('UI','API')                     -- all-purpose only
  GROUP BY 1, 2, 3
),
weekly AS (SELECT wk, count(*) AS v FROM runs WHERE wk BETWEEN 0 AND 4 GROUP BY 1),
{_account_normal("weekly")},
contrib AS (
  SELECT workspace_id, job_id, count(*) AS run_count,
         concat_ws(', ', array_distinct(flatten(collect_list(clusters)))) AS clusters
  FROM runs WHERE wk = 0
  GROUP BY 1, 2
)
SELECT c.workspace_id,
       coalesce(jn.job_name, CAST(c.job_id AS STRING)) AS job_name,
       c.clusters, c.run_count,
       round(r.all_purpose_usd_dbu, 3) AS all_purpose_usd_dbu, round(r.jobs_usd_dbu, 3) AS jobs_usd_dbu,
       round(100 * (1 - try_divide(r.jobs_usd_dbu, r.all_purpose_usd_dbu))) AS pct_saving,
       {_vs_account("c.run_count")}
FROM contrib c
LEFT JOIN job_names jn ON c.workspace_id=jn.workspace_id AND c.job_id=jn.job_id
CROSS JOIN rates r CROSS JOIN acct
ORDER BY c.run_count DESC
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
        "action": "Set auto_stop_minutes to a short value that fits the workload's idle gaps so "
                  "it suspends when idle, and drop min_clusters to 1 unless sustained concurrency needs "
                  "more. For genuinely bursty BI, a serverless warehouse auto-stops in minutes.",
        "measure": "M3",
        "explain_hunt_desc": "classic SQL warehouses with auto-stop disabled (billing 24/7)",
        "explain_rubric": (
            "each row is a classic warehouse with auto_stop_minutes=0 — it never suspends, so it bills "
            "continuously. warehouse_size and min_clusters set how expensive that is (a large size "
            "with several min_clusters held 24/7 is a lot). Flag the largest / highest-min_clusters ones first; "
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
        "title": "Idle cores on interactive clusters",
        "eli5": "Checks how much of the account's interactive (All-Purpose) cluster capacity sat idle "
                "this week against its usual week, then names the clusters that idled most: big "
                "machines you pay for that mostly do nothing (the ds_highmem pattern).",
        "what": "Idle capacity is node-minutes weighted by how much of the CPU went unused: a node at "
                "a quarter of its CPU for an hour counts as three quarters of an hour idle. The "
                "account's idle node-minutes on interactive clusters this week are set against its "
                "usual week (the median of the prior four weeks). vs_normal is how many times that "
                "normal this week is, and that sets the alert: idle capacity growing far beyond the "
                "account's usual is the thing to catch. Each row is one interactive cluster with its "
                "idle node-minutes, its share of the account's, and its average and peak CPU. A "
                "cluster with a large share and a low average, especially a high-memory one, is "
                "paying for cores that do nothing.",
        "action": "Right-size on this evidence: fewer / smaller nodes, or move the scheduled work to "
                  "Jobs compute (which terminates per run). Shorten auto-termination so it can't sit "
                  "idle for hours. For notebook work, serverless notebooks scale to zero.",
        "measure": "M2",
        "explain_hunt_desc": "the account's idle interactive-cluster capacity this week versus its usual "
                             "week, and the clusters that idled most (idle cores billed)",
        "explain_rubric": (
            "account_value is the account's idle node-minutes on interactive (all-purpose) clusters "
            "this week: node-minutes weighted by the unused share of CPU. baseline is the median of "
            "the prior four weekly totals; vs_normal = account_value / baseline (NULL = no prior "
            "history). Say first whether the account is idling more than its normal. Each row is one "
            "cluster; share_pct is its share of the idle node-minutes, and avg_cpu_pct / peak_cpu_pct "
            "show how busy it was. A large share with a low average CPU = idle cores you are paying "
            "for; a high-mem node makes it worse. Name the worst and recommend right-sizing or moving "
            "to Jobs compute. Confirm the cluster isn't a deliberately-warm interactive one before acting."
        ),
        "sql": f"""
WITH latest_clusters AS (
  SELECT workspace_id, cluster_id, cluster_name, cluster_source, auto_termination_minutes
  FROM system.compute.clusters
  QUALIFY ROW_NUMBER() OVER (PARTITION BY workspace_id, cluster_id ORDER BY change_time DESC)=1
),
samples AS (
  SELECT n.workspace_id, n.cluster_id, c.cluster_name, c.cluster_source, c.auto_termination_minutes,
         {_wk("n.start_time")} AS wk,
         n.cpu_user_percent + n.cpu_system_percent AS cpu
  FROM system.compute.node_timeline n
  JOIN latest_clusters c USING (workspace_id, cluster_id)
  WHERE n.start_time >= current_timestamp() - INTERVAL 35 DAYS
    AND c.cluster_source IN ('UI','API')
),
weekly AS (SELECT wk, sum(greatest(100 - cpu, 0)) / 100 AS v FROM samples WHERE wk BETWEEN 0 AND 4 GROUP BY 1),
{_account_normal("weekly")},
contrib AS (
  SELECT workspace_id, cluster_id, cluster_name, cluster_source, auto_termination_minutes,
         sum(greatest(100 - cpu, 0)) / 100 AS idle_node_minutes,
         count(*)                          AS node_minutes,
         round(avg(cpu), 1)                AS avg_cpu_pct,
         round(max(cpu), 1)                AS peak_cpu_pct
  FROM samples WHERE wk = 0
  GROUP BY 1, 2, 3, 4, 5
)
SELECT coalesce(cluster_name, cluster_id) AS cluster, cluster_source, auto_termination_minutes,
       round(idle_node_minutes, 1) AS idle_node_minutes, node_minutes, avg_cpu_pct, peak_cpu_pct,
       {_vs_account("idle_node_minutes")}
FROM contrib CROSS JOIN acct
ORDER BY idle_node_minutes DESC
LIMIT 50
""",
    },

    # ── H19 · idle (always-on Apps) ───────────────────────────────────────────
    {
        "id": "H19",
        "category": "idle",
        "title": "Databricks Apps compute",
        "eli5": "Checks how much compute the account's Databricks Apps billed this week against its "
                "usual week. An app bills while it runs, whether or not anyone is using it.",
        "what": "A Databricks App bills DBUs while deployed and running, regardless of traffic. The "
                "DBUs every app in the account billed this week (complete days) are set against its "
                "usual week (the median of the prior four weeks). vs_normal is how many times that "
                "normal this week is, and that sets the alert, so apps that scaled up or started "
                "burning more stand out. Each row is one app with its DBUs, its share of the account's "
                "app DBUs and how many days of the week it billed; an app billed every day is "
                "effectively always-on. High DBU + always-on + little real traffic = a "
                "stop-or-consolidate candidate.",
        "action": "Audit per-app traffic (Apps UI Insights, or OAuth-consent as a breadth signal); stop "
                  "or undeploy apps no one uses, and consolidate per-workspace/per-env duplicates. Apps "
                  "that only need business-hours availability don't need to run overnight.",
        "measure": "M2",
        "explain_hunt_desc": "the account's Databricks Apps DBUs this week versus its usual week, and "
                             "the apps behind them (apps bill while running, regardless of traffic)",
        "explain_rubric": (
            "account_value is the DBUs every Databricks App in the account billed this week (complete "
            "days); baseline is the median of the prior four weekly totals; vs_normal = account_value / "
            "baseline (NULL = no prior history). Say first whether the account is above its normal. "
            "Each row is one app; share_pct is its share, and days_billed_7d shows how many days of the "
            "week it billed (every day = effectively always-on). Name the apps with the largest share "
            "as stop/consolidate candidates - but note billing shows it's RUNNING, not whether it's "
            "USED, so recommend confirming traffic (Apps Insights) before stopping."
        ),
        "sql": f"""
WITH daily AS (
  SELECT coalesce(usage_metadata.app_name, 'unnamed app') AS app_name, workspace_id, usage_date,
         {_wk_day("usage_date")} AS wk, sum(usage_quantity) AS dbus
  FROM system.billing.usage
  WHERE billing_origin_product = 'APPS'
    AND usage_date >= current_date() - INTERVAL 35 DAYS
    AND usage_date < current_date()
  GROUP BY 1, 2, 3
),
weekly AS (SELECT wk, sum(dbus) AS v FROM daily WHERE wk BETWEEN 0 AND 4 GROUP BY 1),
{_account_normal("weekly")},
contrib AS (
  SELECT app_name, workspace_id,
         sum(dbus)              AS dbus_7d,
         count(*)               AS days_billed_7d,
         round(avg(dbus), 2)    AS avg_daily_dbus,
         max(usage_date)        AS last_billed
  FROM daily WHERE wk = 0
  GROUP BY 1, 2
)
SELECT app_name, workspace_id, round(dbus_7d, 1) AS dbus_7d, days_billed_7d, avg_daily_dbus, last_billed,
       {_vs_account("dbus_7d")}
FROM contrib CROSS JOIN acct
ORDER BY dbus_7d DESC
LIMIT 50
""",
    },

    # ── H20 · spend (failing jobs) ────────────────────────────────────────────
    {
        "id": "H20",
        "category": "spend",
        "title": "Frequently-failing jobs",
        "eli5": "Checks how much compute the account burned on job runs that failed this week against "
                "its usual week; every failed run still pays for setup and partial execution before it "
                "dies. Then names the jobs behind it.",
        "what": "A job that fails wastes the DBUs of every failed attempt (startup + however far it "
                "got). The hours the account spent on runs that ended in failure this week are set "
                "against its usual week (the median of the prior four weeks). vs_normal is how many "
                "times that normal this week is, and that sets the alert. Each row is one job that "
                "failed this week with its run count, failed-run count, failure rate, wasted hours and "
                "share of the account's wasted hours. A high failure rate on a frequent job is both a "
                "reliability problem and a steady cost leak.",
        "action": "Root-cause the recurring failures (read the termination reason and the run logs); "
                  "add an early-abort timeout so a doomed run dies fast instead of burning a full "
                  "window; and pause or fix broken schedules that never succeed.",
        "measure": "M2",
        "explain_hunt_desc": "the account's compute wasted on failed job runs this week versus its usual "
                             "week, and the jobs behind it",
        "explain_rubric": (
            "account_value is the hours of compute the account spent on runs that ended in "
            "ERROR/FAILED/TIMED_OUT/CANCELLED this week; baseline is the median of the prior four "
            "weekly totals; vs_normal = account_value / baseline (NULL = no prior failures). Say first "
            "whether the account is wasting more than its normal. Each row is one job; share_pct is "
            "its share of the wasted hours, with total_runs, failed_runs and failure_rate_pct. A high "
            "failure_rate_pct with a large share = a reliability-and-cost problem. Name the worst "
            "offenders; the fix is root-causing the failures and adding early-abort timeouts. A few "
            "cancellations are normal - focus on the chronic high-rate ones."
        ),
        "sql": f"""
WITH job_names AS (SELECT workspace_id, job_id, max(name) AS job_name FROM system.lakeflow.jobs GROUP BY 1,2),
runs AS (
  SELECT workspace_id, job_id, {_wk("period_start_time")} AS wk,
         result_state IN ('ERROR','FAILED','TIMED_OUT','CANCELLED') AS failed,
         timestampdiff(SECOND, period_start_time, period_end_time) / 3600.0 AS hours
  FROM system.lakeflow.job_run_timeline
  WHERE period_start_time >= current_timestamp() - INTERVAL 35 DAYS
),
weekly AS (SELECT wk, sum(CASE WHEN failed THEN hours END) AS v FROM runs WHERE wk BETWEEN 0 AND 4 GROUP BY 1),
{_account_normal("weekly")},
contrib AS (
  SELECT workspace_id, job_id,
         count(*)                                       AS total_runs,
         count_if(failed)                               AS failed_runs,
         round(count_if(failed) * 100.0 / count(*), 1)  AS failure_rate_pct,
         sum(CASE WHEN failed THEN hours END)           AS wasted_hours
  FROM runs WHERE wk = 0
  GROUP BY 1, 2
  HAVING count_if(failed) > 0
)
SELECT c.workspace_id,
       coalesce(jn.job_name, CAST(c.job_id AS STRING)) AS job_name,
       c.total_runs, c.failed_runs, c.failure_rate_pct, round(c.wasted_hours, 1) AS wasted_hours,
       {_vs_account("c.wasted_hours")}
FROM contrib c LEFT JOIN job_names jn ON c.workspace_id=jn.workspace_id AND c.job_id=jn.job_id
CROSS JOIN acct
ORDER BY c.wasted_hours DESC
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
                "snappy to zstd; zstd typically stores the same data in less space.",
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
            "= more potential storage saving from recompressing to zstd (zstd files are typically smaller). Recommend confirming "
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
        "eli5": "Checks how much the account spent on serverless jobs in Performance-optimized mode this "
                "week against its usual week. That mode starts and runs faster but burns more DBUs; "
                "worth it for time-critical pipelines, but non-urgent jobs can switch to Standard mode "
                "and pay for fewer DBUs.",
        "what": "Serverless jobs have two performance modes. Both bill at the SAME $/DBU rate; the "
                "difference is DBU consumption. Performance-optimized (the default) prioritises fast "
                "startup and runs on quicker capacity, so it consumes more DBUs; Standard tolerates a "
                "slower cold start and uses fewer DBUs for the same work. The account's list-price "
                "spend in Performance-optimized mode this week (complete days) is set against its "
                "usual week (the median of the prior four weeks). vs_normal is how many times that "
                "normal this week is, and that sets the alert. Each row is one job with its spend, "
                "DBUs and share of the account's spend in this mode. usd_7d is the spend exposed to a "
                "Standard-mode switch, not a guaranteed saving.",
        "action": "For latency-tolerant jobs — scheduled batch, backfills, overnight ETL, anything with "
                  "no downstream deadline — turn OFF ‘Performance optimization’ on the job's serverless "
                  "compute (Job → task → Compute → Serverless → Performance optimized). That accepts a "
                  "slower cold start in exchange for fewer DBUs. Keep Performance-optimized only where a "
                  "run has a real SLA or a waiting consumer. Prove the change with M6 (the job's "
                  "Performance-optimized line should fall, replaced by a smaller Standard line).",
        "measure": "M6",
        "explain_hunt_desc": "the account's serverless-job spend in Performance-optimized mode this week "
                             "versus its usual week, and the jobs behind it (faster start, more DBUs)",
        "explain_rubric": (
            "account_value is the account's list-price $ on serverless jobs in PERFORMANCE_OPTIMIZED "
            "mode this week (complete days); baseline is the median of the prior four weekly totals; "
            "vs_normal = account_value / baseline (NULL = no prior spend in this mode). Say first "
            "whether the account is above its normal. Ground the read on the real mechanic: BOTH modes "
            "bill at the same $/DBU rate - performance-optimized just consumes MORE DBUs to start and "
            "run faster (Standard tolerates a slower cold start for fewer DBUs). Each row is one job; "
            "usd_7d is its spend this week, share_pct its share, dbus_7d the volume and active_days how "
            "regularly it ran. Name the jobs with the largest share as the candidates to move to "
            "Standard, and note that a frequently-run job compounds the saving. DO NOT quote a fixed % "
            "saving - the realised saving depends on the workload, and the tradeoff is a slower start; "
            "flag that genuinely time-critical / SLA jobs should stay Performance-optimized. The job "
            "name often hints at urgency (a frequent ‘Refresh’ may be deliberately fast; a nightly "
            "‘datagen’ or ‘backfill’ usually is not)."
        ),
        "sql": f"""
WITH price AS (
  SELECT sku_name, cloud, price_start_time, price_end_time,
         pricing.effective_list.default AS unit_usd FROM system.billing.list_prices
),
job_name AS (SELECT workspace_id, job_id, max(name) AS name FROM system.lakeflow.jobs GROUP BY 1, 2),
daily AS (
  SELECT u.workspace_id, u.usage_metadata.job_id AS job_id, u.usage_date,
         {_wk_day("u.usage_date")}           AS wk,
         sum(u.usage_quantity * pr.unit_usd) AS usd,
         sum(u.usage_quantity)               AS dbus
  FROM system.billing.usage u
  JOIN price pr ON u.sku_name = pr.sku_name AND u.cloud = pr.cloud
    AND u.usage_start_time >= pr.price_start_time
    AND (pr.price_end_time IS NULL OR u.usage_start_time < pr.price_end_time)
  WHERE u.billing_origin_product = 'JOBS'
    AND u.product_features.performance_target = 'PERFORMANCE_OPTIMIZED'
    AND u.usage_date >= current_date() - INTERVAL 35 DAYS
    AND u.usage_date < current_date()
  GROUP BY 1, 2, 3
),
weekly AS (SELECT wk, sum(usd) AS v FROM daily WHERE wk BETWEEN 0 AND 4 GROUP BY 1),
{_account_normal("weekly")},
contrib AS (
  SELECT workspace_id, job_id,
         sum(usd)                    AS usd_7d,
         sum(dbus)                   AS dbus_7d,
         count(DISTINCT usage_date)  AS active_days
  FROM daily WHERE wk = 0
  GROUP BY 1, 2
)
SELECT c.workspace_id,
       coalesce(jn.name, CAST(c.job_id AS STRING), 'unknown job') AS job_name,
       c.job_id, round(c.usd_7d, 2) AS usd_7d, round(c.dbus_7d, 1) AS dbus_7d, c.active_days,
       {_vs_account("c.usd_7d")}
FROM contrib c
LEFT JOIN job_name jn ON c.workspace_id = jn.workspace_id AND c.job_id = jn.job_id
CROSS JOIN acct
ORDER BY c.usd_7d DESC
LIMIT 50
""",
    },

    # ── H23 · spend ──────────────────────────────────────────────────────────
    {
        "id": "H23",
        "category": "spend",
        "title": "Predictive Optimization cost drivers",
        "eli5": "Checks the account's Predictive Optimization DBUs over the last 7 complete days against "
                "its usual week, then names the tables whose auto-maintenance (compaction, clustering, "
                "vacuum) drove it.",
        "what": "Predictive Optimization is the automatic maintenance Databricks runs on your Unity "
                "Catalog managed tables: OPTIMIZE/compaction, liquid clustering, VACUUM and stats. You "
                "pay per DBU of that work, so the bill tracks how much data is being written and "
                "reorganised. The account's PO DBUs in the last 7 complete days are set against its "
                "usual week (the median of the four 7-day periods before). vs_normal is how many times "
                "that normal this week is, and that sets the alert. Each row is one table + operation "
                "type with its PO DBUs this period and the one before, the change, and its share of "
                "the account's PO DBUs. dbu_increase is in DBUs, not dollars; multiply by your PO SKU "
                "rate for an approximate cost. (The windows are trailing complete 7-day periods, not "
                "calendar weeks, so today's partial day never distorts the comparison.)",
        "action": "Trace the top risers to a cause: (1) more ingestion / writes into that table → "
                  "expected if it is genuine new data; (2) a table or whole schema newly brought under "
                  "PO → confirm you actually want PO managing it; (3) a move to liquid clustering or a "
                  "clustering-key change → a one-off re-cluster that settles after a week or two. Then "
                  "right-size the SCOPE: enabling PO across prod usually makes sense, but for "
                  "dev/staging/lower environments decide per use-case — you can leave PO off there and "
                  "instead run table maintenance (OPTIMIZE/VACUUM) on a schedule, less often. Re-run this "
                  "check next week; a one-off re-cluster or a narrowed PO scope should pull the top "
                  "table's dbu_increase back down.",
        "explain_hunt_desc": "the account's Predictive Optimization DBUs over the last 7 complete days "
                             "versus its usual week, and the tables and operations that drove them",
        "explain_rubric": (
            "account_value is the account's Predictive Optimization DBUs (ESTIMATED_DBU, the unit PO "
            "bills in) over the last 7 complete days; baseline is the median of the four 7-day periods "
            "before; vs_normal = account_value / baseline (NULL = no prior PO usage). Say first whether "
            "the account is above its normal. Each row is one (table, operation_type); share_pct is its "
            "share, and dbu_increase = last-7-days minus the 7 days before. operation_type tells you "
            "WHICH maintenance grew: COMPACTION = many small files being written, so more bin-packing; "
            "CLUSTERING / AUTO_CLUSTERING_COLUMN_SELECTION = liquid-clustering re-organisation, often a "
            "one-off after clustering is enabled or keys change; VACUUM = garbage-collecting deleted "
            "files; ANALYZE = statistics. A large rise usually means one of: higher ingestion volume "
            "into that table, a table/schema newly brought under PO, or a clustering-key change forcing "
            "a re-cluster. Name the top tables and their operation types as the drivers. Values are "
            "DBUs, NOT dollars - do not fabricate a cost; the bill is DBUs x the PO SKU rate. If the "
            "top operation is CLUSTERING right after enablement, call it a likely one-off that settles."
        ),
        "sql": f"""
WITH ops AS (
  SELECT catalog_name, schema_name, table_name, operation_type, usage_quantity,
         {_wk_day("start_time")} AS wk
  FROM system.storage.predictive_optimization_operations_history
  WHERE usage_unit = 'ESTIMATED_DBU'
    AND start_time >= current_date() - INTERVAL 35 DAYS
    AND start_time <  current_date()
),
weekly AS (SELECT wk, sum(usage_quantity) AS v FROM ops WHERE wk BETWEEN 0 AND 4 GROUP BY 1),
{_account_normal("weekly")},
contrib AS (
  SELECT catalog_name, schema_name, table_name, operation_type,
         coalesce(sum(CASE WHEN wk = 1 THEN usage_quantity END), 0) AS dbus_prev_7d,
         coalesce(sum(CASE WHEN wk = 0 THEN usage_quantity END), 0) AS dbus_last_7d
  FROM ops WHERE wk <= 1
  GROUP BY 1, 2, 3, 4
  HAVING sum(CASE WHEN wk = 0 THEN usage_quantity END) > 0
)
SELECT catalog_name, schema_name, table_name, operation_type,
       round(dbus_prev_7d, 1) AS dbus_prev_7d, round(dbus_last_7d, 1) AS dbus_last_7d,
       round(dbus_last_7d - dbus_prev_7d, 1) AS dbu_increase,
       concat_ws('.', catalog_name, schema_name, table_name) || ' (' || operation_type || ')' AS po_target,
       {_vs_account("dbus_last_7d")}
FROM contrib CROSS JOIN acct
ORDER BY dbus_last_7d DESC
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
    "H0": {"kind": "anomaly", "aggregate": "peak", "entity_col": "caller", "metric_col": "tokens",
           "metric_label": "Account tokens in its busiest hour this week", "unit": "count",
           "baseline": "median of the account's busiest hour in each of the prior four weeks",
           "summary": "Account used {account} AI tokens in its busiest hour this week, {x} its usual (normal ~{baseline}).",
           "new_summary": "Account has no prior history of AI token use yet ({account} tokens in its busiest hour this week).",
           "usd_col": None},
    "H1": {"kind": "anomaly", "aggregate": "additive", "entity_col": "merge_target", "metric_col": "read_tib",
           "metric_label": "Account MERGE reads this week", "unit": "tib",
           "baseline": "median of the account's prior four weekly MERGE read totals",
           "summary": "Account's MERGE statements read {account} this week, {x} its usual (normal ~{baseline}).",
           "new_summary": "Account has no prior history of MERGE reads yet ({account} this week).",
           "usd_col": None},
    "H2": {"kind": "anomaly", "aggregate": "additive", "entity_col": "source", "metric_col": "spill_tib",
           "metric_label": "Account disk spill this week", "unit": "tib",
           "baseline": "median of the account's prior four weekly spill totals",
           "summary": "Account spilled {account} to disk this week, {x} its usual (normal ~{baseline}).",
           "new_summary": "Account has no prior history of disk spill yet ({account} this week).",
           "usd_col": None},
    "H3": {"kind": "anomaly", "aggregate": "additive", "entity_col": "client", "metric_col": "stmts",
           "metric_label": "Account zero-read statements this week", "unit": "count",
           "baseline": "median of the account's prior four weekly totals",
           "summary": "Account fired {account} statements that read no data this week, {x} its usual (normal ~{baseline}).",
           "new_summary": "Account has no prior history of zero-read statements yet ({account} this week).",
           "usd_col": None},
    "H5": {"kind": "anomaly", "aggregate": "peak", "entity_col": "source", "metric_col": "total_min",
           "metric_label": "Account's longest statement this week", "unit": "minutes",
           "baseline": "median of the account's longest statement in each of the prior four weeks",
           "summary": "Account's longest statement this week ran {account}, {x} its usual longest (normal ~{baseline}).",
           "new_summary": "Account has no prior history of statement run times yet (longest this week {account}).",
           "usd_col": None},
    "H6": {"kind": "anomaly", "aggregate": "peak", "entity_col": "executed_by", "metric_col": "read_tib",
           "metric_label": "Account's busiest scan hour this week", "unit": "tib",
           "baseline": "median of the account's busiest scan hour in each of the prior four weeks",
           "summary": "Account scanned {account} in its busiest hour this week, {x} its usual (normal ~{baseline}).",
           "new_summary": "Account has no prior history of SELECT scans yet ({account} in its busiest hour this week).",
           "usd_col": None},
    "H7": {"kind": "anomaly", "aggregate": "additive", "entity_col": "poller", "metric_col": "execs",
           "metric_label": "Account short SELECTs this week", "unit": "count",
           "baseline": "median of the account's prior four weekly totals of SELECTs at or under its "
                       "median SELECT duration",
           "summary": "Account ran {account} short queries this week, {x} its usual (normal ~{baseline}).",
           "new_summary": "Account has no prior history of short queries yet ({account} this week).",
           "usd_col": None},
    "H8": {"kind": "anomaly", "aggregate": "additive", "entity_col": "warehouse", "metric_col": "queued_hours",
           "metric_label": "Account queue wait this week", "unit": "hours",
           "baseline": "median of the account's prior four weekly queue-wait totals",
           "summary": "Account's statements waited {account} for capacity this week, {x} its usual (normal ~{baseline}).",
           "new_summary": "Account has no prior history of queueing yet ({account} waiting this week).",
           "usd_col": None},
    # ── H16–H20 (Aug 2026 thread additions) ──
    "H16": {"kind": "anomaly", "aggregate": "additive", "entity_col": "job_name", "metric_col": "run_count",
            "metric_label": "Account job runs on All-Purpose compute this week", "unit": "count",
            "baseline": "median of the account's prior four weekly totals",
            "summary": "Account ran {account} jobs on All-Purpose compute this week, {x} its usual (normal ~{baseline}).",
            "new_summary": "Account has no prior history of jobs on All-Purpose compute yet ({account} runs this week).",
            "usd_col": None},
    "H17": {"kind": "presence", "entity_col": "warehouse_name",
            "metric_label": "Classic warehouses with auto-stop off", "usd_col": None,
            "summary": "{n} classic warehouse(s) never auto-stop, including {entity}."},
    "H18": {"kind": "anomaly", "aggregate": "additive", "entity_col": "cluster", "metric_col": "idle_node_minutes",
            "metric_label": "Account idle node-minutes on interactive clusters this week", "unit": "count",
            "baseline": "median of the account's prior four weekly totals",
            "summary": "Account's interactive clusters idled {account} node-minutes this week, {x} its usual (normal ~{baseline}).",
            "new_summary": "Account has no prior history of interactive clusters yet ({account} idle node-minutes this week).",
            "usd_col": None},
    "H19": {"kind": "anomaly", "aggregate": "additive", "entity_col": "app_name", "metric_col": "dbus_7d",
            "metric_label": "Account Databricks Apps DBUs this week", "unit": "count",
            "baseline": "median of the account's prior four weekly totals (complete days)",
            "summary": "Account's Databricks Apps billed {account} DBUs this week, {x} its usual (normal ~{baseline}).",
            "new_summary": "Account has no prior history of Databricks Apps billing yet ({account} DBUs this week).",
            "usd_col": None},
    "H20": {"kind": "anomaly", "aggregate": "additive", "entity_col": "job_name", "metric_col": "wasted_hours",
            "metric_label": "Account compute on failed job runs this week", "unit": "hours",
            "baseline": "median of the account's prior four weekly totals",
            "summary": "Account spent {account} on failed job runs this week, {x} its usual (normal ~{baseline}).",
            "new_summary": "Account has no prior history of failed job runs yet ({account} on failed runs this week).",
            "usd_col": None},
    "H21": {"kind": "presence", "entity_col": "schema_name",
            "metric_label": "Schemas with likely-snappy Delta tables", "usd_col": None,
            "summary": "{n} schema(s) hold Delta tables that predate the zstd default, including {entity}."},
    "H22": {"kind": "anomaly", "aggregate": "additive", "entity_col": "job_name", "metric_col": "usd_7d",
            "metric_label": "Account Performance-optimized serverless job spend this week", "unit": "usd",
            "baseline": "median of the account's prior four weekly totals (complete days)",
            "summary": "Account spent {account} on Performance-optimized serverless jobs this week, {x} its usual (normal ~{baseline}).",
            "new_summary": "Account has no prior history of Performance-optimized serverless jobs yet ({account} this week).",
            "usd_col": "account_value"},
    "H23": {"kind": "anomaly", "aggregate": "additive", "entity_col": "po_target", "metric_col": "dbus_last_7d",
            "metric_label": "Account Predictive Optimization DBUs, last 7 complete days", "unit": "count",
            "baseline": "median of the account's four prior 7-day totals",
            "summary": "Account's Predictive Optimization used {account} DBUs in the last 7 days, {x} its usual (normal ~{baseline}).",
            "new_summary": "Account has no prior history of Predictive Optimization yet ({account} DBUs in the last 7 days).",
            "usd_col": None},
}

