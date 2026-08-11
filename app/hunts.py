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

HUNTS = [
    # ── H0 · AI ──────────────────────────────────────────────────────────────
    {
        "id": "H0",
        "category": "ai",
        "title": "Runaway AI spend",
        "eli5": "Finds AI model calls that are spending fast right now — within the "
                "hour, before the daily bill even updates.",
        "what": "Each row is one hour of AI usage for a model endpoint and the identity "
                "that called it. If a single (endpoint, caller) pair suddenly spends "
                "10× more than its neighbours, that is your runaway. "
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
            "a single (endpoint, requester) pair whose input_tokens or est_usd_rough "
            "jumps an order of magnitude above its neighbours is the runaway. "
            "requester_type = service principal means an automated pipeline. "
            "est_usd_rough is a rate-blended estimate (this window's tokens x the "
            "account's own 7-day effective $/1M-token rate), read it relative, not as "
            "an invoice. source = AI_QUERY marks a SQL ai_query() call."
        ),
        "sql": """
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
)
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
ORDER BY est_usd_rough DESC NULLS LAST
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
        "what": "A read-to-write ratio in the thousands is the red flag. If the target "
                "table is small but the reads are huge, the cost is upstream (the source "
                "query has no date filter). If reads are about twice the target size, the "
                "merge is scanning the whole target every run.",
        "action": "Bound the source with an incremental watermark so it only reads new "
                  "rows; bound the target with incremental_predicates on a date column; "
                  "cluster the target on that same column so old data can be skipped; and "
                  "update only the rows that changed.",
        "measure": "M4",
        "explain_hunt_desc": "MERGE statements that read far more than they write (read amplification)",
        "explain_rubric": (
            "read_write_ratio in the thousands is the red flag. Then compare read_tib per "
            "run against the target's actual size (DESCRIBE DETAIL <target> -> sizeInBytes, "
            "see H15): if the target is small but reads are huge, the cost is upstream (the "
            "source query); if reads are approximately 2x the target size, the merge scans "
            "the whole target in both its match and rewrite phases."
        ),
        "sql": """
SELECT statement_id, executed_by,
       read_bytes/POW(1024,4)               AS read_tib,
       written_bytes/POW(1024,3)            AS written_gib,
       CAST(read_bytes AS DOUBLE)/NULLIF(written_bytes,0) AS read_write_ratio,
       total_duration_ms/60000.0            AS mins
FROM system.query.history
WHERE statement_type = 'MERGE'
  AND start_time >= current_date() - INTERVAL 7 DAYS
ORDER BY read_bytes DESC
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
        "what": "Compare spill to the input size. Spill much LARGER than the input (we have "
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
            "compare spill_tib to read_gib. Spill far larger than the input (we saw 46x) is a "
            "query-shape problem: re-used CTEs recomputed per reference, deduplication after a "
            "join instead of before, unbounded lookback windows. Spill roughly proportional to "
            "input on a well-shaped query is a sizing problem."
        ),
        "sql": """
SELECT statement_id, start_time, executed_by,
       spilled_local_bytes/POW(1024,4)      AS spill_tib,
       read_bytes/POW(1024,3)               AS read_gib,
       total_duration_ms/60000.0            AS mins
FROM system.query.history
WHERE spilled_local_bytes > 0
  AND start_time >= current_date() - INTERVAL 7 DAYS
ORDER BY spilled_local_bytes DESC
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
                "means the tool opens a fresh connection for every statement. Tens of thousands "
                "of these a week from one client is the pattern that keeps a warehouse billing 24/7.",
        "action": "Fix the tool first — e.g. a BI tool's refresh schedule left at its "
                  "aggressive default, or a monitoring tool that should have its own small "
                  "warehouse. Then move always-on tools onto a small serverless warehouse where "
                  "a short auto-stop is practical.",
        "measure": "M3",
        "explain_hunt_desc": "chatty clients issuing tiny zero-read statements that keep warehouses awake",
        "explain_rubric": (
            "client_application and executed_by name the tool - it's configuration, not "
            "guesswork. stmts_per_session near 1 means the client opens a fresh connection per "
            "statement (pool churn). Tens of thousands of rows per week from one client is the pattern."
        ),
        "sql": """
WITH wh AS (SELECT warehouse_id, warehouse_name FROM system.compute.warehouses QUALIFY ROW_NUMBER() OVER (PARTITION BY warehouse_id ORDER BY change_time DESC)=1 AND delete_time IS NULL)
SELECT coalesce(wh.warehouse_name, q.warehouse_id) AS warehouse, q.statement_type, q.executed_by,
       q.client_application, q.client_driver, q.stmts, q.sessions, q.stmts_per_session
FROM (
  SELECT compute.warehouse_id, statement_type, executed_by, client_application, client_driver,
         count(*) AS stmts, count(DISTINCT session_id) AS sessions,
         round(count(*)/count(DISTINCT session_id),1) AS stmts_per_session
  FROM system.query.history
  WHERE read_bytes = 0 AND total_duration_ms < 30000
    AND start_time >= current_timestamp() - INTERVAL 7 DAYS
  GROUP BY 1,2,3,4,5
) q LEFT JOIN wh ON q.warehouse_id = wh.warehouse_id
ORDER BY q.stmts DESC
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
        "what": "The split tells you which of three problems you have. Long execution time "
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
            "long exec_min + huge read_gib = a genuine heavy scan (see H6). Long exec_min + tiny "
            "reads = a hung statement (we found two that executed 21.7 h while reading 6 MB - "
            "check execution_status and the query profile). Large queued_min = a capacity problem (see H8)."
        ),
        "sql": """
SELECT statement_id, start_time,
       total_duration_ms/60000.0               AS total_min,
       waiting_at_capacity_duration_ms/60000.0 AS queued_min,
       waiting_for_compute_duration_ms/60000.0 AS provisioning_min,
       execution_duration_ms/60000.0           AS exec_min,
       read_files, read_bytes/POW(1024,3)      AS read_gib
FROM system.query.history
WHERE total_duration_ms > 4*3600*1000
  AND start_time >= current_date() - INTERVAL 7 DAYS
ORDER BY total_duration_ms DESC
""",
    },

    # ── H6 · reads ─────────────────────────────────────────────────────────────
    {
        "id": "H6",
        "category": "reads",
        "title": "The scan paid five times",
        "eli5": "Finds several multi-terabyte scans by the same person within one hour — "
                "the signature of an investigation re-scanning a raw table over and over.",
        "what": "Three or more heavy scans in one hour from one person is almost always a "
                "single incident hunt — someone searching a raw event table with "
                "‘LIKE %some-id%’ and re-paying the full scan on every refinement. "
                "Open the statements in Query History to confirm the shape.",
        "action": "Give investigators a two-line template: first, time-bound the opening scan "
                  "on the table's date column; second, stage that first pass into a temp table "
                  "and run every refinement against the small staged copy — five scans become "
                  "one. Longer term, promote the searched fields to real clustered columns, or "
                  "evaluate a full-text search index.",
        "measure": "M1",
        "explain_hunt_desc": "repeated multi-TiB scans by one principal in an hour (a re-paid investigation scan)",
        "explain_rubric": (
            "3+ heavy scans in an hour from one principal is almost always one incident hunt. "
            "Open the statements in the Query History UI to confirm the shape."
        ),
        "sql": """
SELECT executed_by, date_trunc('HOUR', start_time) AS hour,
       count(*)                          AS scans,
       sum(read_bytes)/POW(1024,4)       AS read_tib,
       max(total_duration_ms)/3600000.0  AS longest_hours
FROM system.query.history
WHERE statement_type = 'SELECT'
  AND read_bytes > POW(1024,4)
  AND start_time >= current_date() - INTERVAL 14 DAYS
GROUP BY 1, 2
HAVING count(*) >= 3
ORDER BY read_tib DESC
""",
    },

    # ── H7 · longrun ─────────────────────────────────────────────────────────────
    {
        "id": "H7",
        "category": "longrun",
        "title": "Polling every 20 seconds",
        "eli5": "Finds one identity running the same short query thousands of times — "
                "usually an orchestrator checking metadata far more often than needed.",
        "what": "More than ~10,000 runs a week from one identity is a CANDIDATE poller, not a "
                "verdict — confirm before acting. ‘execution_hours’ is the total "
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
            "more than ~10K executions a week from one principal is a candidate poller, not a "
            "verdict - confirm before acting. p50_exec_ms shows how expensive each poll is; "
            "execution_hours is the aggregate execution weight (execution only, excluding "
            "queue/provisioning). When all query_source IDs are NULL the group is unattributed - "
            "unrelated short SELECTs can collapse into one row, so do not assert it is a poller."
        ),
        "sql": """
SELECT executed_by,
       CASE WHEN executed_by LIKE '%@%' THEN 'human' ELSE 'service_principal' END AS owner_type,
       client_application,
       query_source.sql_query_id            AS sql_query_id,
       query_source.dashboard_id            AS dashboard_id,
       query_source.job_info.job_id         AS job_id,
       count(*)                                       AS execs,
       approx_percentile(execution_duration_ms, 0.5)  AS p50_exec_ms,
       sum(execution_duration_ms)/3600000.0           AS execution_hours,
       any_value(statement_id)                        AS example_statement_id,
       slice(array_agg(statement_id), 1, 3)           AS more_example_ids,
       CASE WHEN coalesce(query_source.sql_query_id, query_source.dashboard_id,
                          query_source.job_info.job_id) IS NULL
            THEN 'unattributed — confirm before acting'
            ELSE 'candidate — confirm in Query History' END    AS status
FROM system.query.history
WHERE statement_type = 'SELECT' AND total_duration_ms < 60000
  AND start_time >= current_date() - INTERVAL 7 DAYS
GROUP BY 1, 2, 3, 4, 5, 6
HAVING count(*) > 10000
ORDER BY execs DESC
""",
    },

    # ── H8 · config ─────────────────────────────────────────────────────────────
    {
        "id": "H8",
        "category": "config",
        "title": "Queue pressure vs capacity",
        "eli5": "Shows, per warehouse, how long queries waited for capacity next to how long "
                "they actually ran — the objective signal for right-sizing.",
        "what": "High queued time relative to execution time means the warehouse is undersized "
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
            "high queued_hours relative to exec_hours means the warehouse is undersized or its "
            "max cluster count is too low for peaks. Zero queueing with low utilisation (H14 for "
            "clusters, warehouse monitoring for DBSQL) means there is room to downsize."
        ),
        "sql": """
WITH wh AS (SELECT warehouse_id, warehouse_name FROM system.compute.warehouses QUALIFY ROW_NUMBER() OVER (PARTITION BY warehouse_id ORDER BY change_time DESC)=1 AND delete_time IS NULL)
SELECT coalesce(wh.warehouse_name, q.warehouse_id) AS warehouse,
       q.stmts_queued_over_1m, q.queued_hours, q.exec_hours
FROM (
  SELECT compute.warehouse_id,
         count_if(waiting_at_capacity_duration_ms > 60000) AS stmts_queued_over_1m,
         sum(waiting_at_capacity_duration_ms)/3600000.0    AS queued_hours,
         sum(execution_duration_ms)/3600000.0              AS exec_hours
  FROM system.query.history
  WHERE start_time >= current_date() - INTERVAL 7 DAYS
  GROUP BY 1
) q LEFT JOIN wh ON q.warehouse_id = wh.warehouse_id
ORDER BY q.queued_hours DESC
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
                "cluster it ran on (cluster_source UI/API = interactive; JOB = proper jobs compute). "
                "The pct_saving column is the rate saving from moving it.",
        "action": "Move the job to Jobs Compute (an ephemeral cluster that spins up per run and "
                  "terminates after) or Serverless Jobs. That drops the rate to $0.15/DBU and removes "
                  "idle-between-runs billing. Interim: cut the cluster's auto-termination to 20–30 min.",
        "measure": "M2",
        "explain_hunt_desc": "automated jobs running on All-Purpose clusters instead of cheaper Jobs compute",
        "explain_rubric": (
            "each row is a job that ran on an ALL-PURPOSE cluster (cluster_source UI or API), which "
            "bills $0.55/DBU vs $0.15/DBU on Jobs compute — a 3.7x markup. run_count is how many times "
            "it ran that way. Name the jobs with the most runs on all-purpose as the migration targets; "
            "the fix is Jobs Compute / Serverless Jobs. Do not overstate — the saving is the rate delta "
            "on that job's DBUs, not the whole cluster."
        ),
        "sql": """
WITH latest_clusters AS (
  SELECT workspace_id, cluster_id, cluster_name, cluster_source, owned_by
  FROM system.compute.clusters
  QUALIFY ROW_NUMBER() OVER (PARTITION BY workspace_id, cluster_id ORDER BY change_time DESC)=1
),
job_names AS (
  SELECT workspace_id, job_id, max(name) AS job_name FROM system.lakeflow.jobs GROUP BY 1,2
),
runs_on_cluster AS (
  SELECT t.workspace_id, t.job_id, exploded.cluster_id, count(DISTINCT t.run_id) AS run_count
  FROM system.lakeflow.job_task_run_timeline t
  LATERAL VIEW explode(t.compute_ids) exploded AS cluster_id
  WHERE t.period_start_time >= current_date() - INTERVAL 30 DAYS
    AND t.compute_ids IS NOT NULL AND array_size(t.compute_ids) > 0
  GROUP BY 1,2,3
)
SELECT r.workspace_id,
       coalesce(jn.job_name, CAST(r.job_id AS STRING)) AS job_name,
       c.cluster_name,
       c.cluster_source,                                   -- UI / API = all-purpose
       r.run_count,
       0.55 AS all_purpose_usd_dbu, 0.15 AS jobs_usd_dbu,
       '73% cheaper on Jobs' AS pct_saving
FROM runs_on_cluster r
JOIN latest_clusters c ON r.workspace_id=c.workspace_id AND r.cluster_id=c.cluster_id
LEFT JOIN job_names jn ON r.workspace_id=jn.workspace_id AND r.job_id=jn.job_id
WHERE c.cluster_source IN ('UI','API')                     -- all-purpose only
ORDER BY r.run_count DESC
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
        "what": "Over the last 7 days this measures each interactive cluster's average CPU and how much "
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
            "severely-underutilised / underutilised. Name the worst and recommend right-sizing or "
            "moving to Jobs compute. Confirm the cluster isn't a deliberately-warm interactive one before acting."
        ),
        "sql": """
WITH latest_clusters AS (
  SELECT workspace_id, cluster_id, cluster_name, cluster_source, owned_by, auto_termination_minutes, delete_time
  FROM system.compute.clusters
  QUALIFY ROW_NUMBER() OVER (PARTITION BY workspace_id, cluster_id ORDER BY change_time DESC)=1
),
cpu AS (
  SELECT workspace_id, cluster_id,
         count(*) AS samples,
         round(avg(cpu_user_percent + cpu_system_percent), 1) AS avg_cpu_pct,
         round(max(cpu_user_percent + cpu_system_percent), 1) AS peak_cpu_pct,
         round(sum(CASE WHEN (cpu_user_percent + cpu_system_percent) < 10 THEN 1 ELSE 0 END)*100.0/count(*), 1) AS pct_time_idle
  FROM system.compute.node_timeline
  WHERE start_time >= current_date() - INTERVAL 7 DAYS
  GROUP BY 1,2
)
SELECT coalesce(c.cluster_name, c.cluster_id) AS cluster,
       c.cluster_source, c.auto_termination_minutes,
       cpu.avg_cpu_pct, cpu.peak_cpu_pct, cpu.pct_time_idle, cpu.samples AS node_minutes,
       CASE WHEN cpu.avg_cpu_pct < 10 AND cpu.peak_cpu_pct < 30 THEN 'severely-underutilised'
            WHEN cpu.avg_cpu_pct < 20 THEN 'underutilised' ELSE 'ok' END AS verdict
FROM cpu JOIN latest_clusters c USING (workspace_id, cluster_id)
WHERE c.cluster_source IN ('UI','API') AND c.delete_time IS NULL
  AND cpu.avg_cpu_pct < 20 AND cpu.samples > 500
ORDER BY cpu.samples DESC
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
                "app with its days billed and total/average daily DBUs. High DBU + always-on + little "
                "real traffic = a stop-or-consolidate candidate.",
        "action": "Audit per-app traffic (Apps UI Insights, or OAuth-consent as a breadth signal); stop "
                  "or undeploy apps no one uses, and consolidate per-workspace/per-env duplicates. Apps "
                  "that only need business-hours availability don't need to run overnight.",
        "measure": "M2",
        "explain_hunt_desc": "Databricks Apps billing nearly every day (always-on regardless of traffic)",
        "explain_rubric": (
            "each row is a Databricks App with days_billed (of last 30) and its DBUs. days_billed >= 25 "
            "means it's effectively always-on; high total_dbus makes it costly. Name the biggest "
            "always-on apps as stop/consolidate candidates — but note billing shows it's RUNNING, not "
            "whether it's USED, so recommend confirming traffic (Apps Insights) before stopping."
        ),
        "sql": """
SELECT usage_metadata.app_name                              AS app_name,
       workspace_id,
       count(DISTINCT usage_date)                           AS days_billed_30d,
       round(sum(usage_quantity), 1)                        AS total_dbus_30d,
       round(sum(usage_quantity)/count(DISTINCT usage_date), 2) AS avg_daily_dbus,
       max(usage_date)                                      AS last_billed
FROM system.billing.usage
WHERE billing_origin_product = 'APPS'
  AND usage_date >= current_date() - INTERVAL 30 DAYS
  AND usage_metadata.app_name IS NOT NULL
GROUP BY 1, 2
HAVING count(DISTINCT usage_date) >= 25
ORDER BY total_dbus_30d DESC
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
                "it got). Each row is a job with its 30-day run count, failed-run count, failure rate, "
                "and the hours of compute spent on runs that failed. A high failure rate on a frequent "
                "job is both a reliability problem and a steady cost leak.",
        "action": "Root-cause the recurring failures (read the termination reason and the run logs); "
                  "add an early-abort timeout so a doomed run dies fast instead of burning a full "
                  "window; and pause or fix broken schedules that never succeed.",
        "measure": "M2",
        "explain_hunt_desc": "jobs failing frequently (compute wasted on failed runs)",
        "explain_rubric": (
            "each row is a job with total_runs, failed_runs, failure_rate_pct and wasted_hours (compute "
            "spent on runs that ended in ERROR/FAILED/TIMED_OUT/CANCELLED over 30 days). High "
            "failure_rate_pct with meaningful wasted_hours = a reliability-and-cost problem. Name the "
            "worst offenders; the fix is root-causing the failures and adding early-abort timeouts. A "
            "few cancellations are normal — focus on the chronic high-rate ones."
        ),
        "sql": """
WITH job_names AS (SELECT workspace_id, job_id, max(name) AS job_name FROM system.lakeflow.jobs GROUP BY 1,2),
stats AS (
  SELECT workspace_id, job_id,
         count(*) AS total_runs,
         count_if(result_state IN ('ERROR','FAILED','TIMED_OUT','CANCELLED')) AS failed_runs,
         round(count_if(result_state IN ('ERROR','FAILED','TIMED_OUT','CANCELLED'))*100.0/count(*), 1) AS failure_rate_pct,
         round(sum(CASE WHEN result_state IN ('ERROR','FAILED','TIMED_OUT','CANCELLED')
                        THEN timestampdiff(SECOND, period_start_time, period_end_time) ELSE 0 END)/3600.0, 1) AS wasted_hours
  FROM system.lakeflow.job_run_timeline
  WHERE period_start_time >= current_date() - INTERVAL 30 DAYS
  GROUP BY 1,2
  HAVING count_if(result_state IN ('ERROR','FAILED','TIMED_OUT','CANCELLED')) >= 5
)
SELECT s.workspace_id,
       coalesce(jn.job_name, CAST(s.job_id AS STRING)) AS job_name,
       s.total_runs, s.failed_runs, s.failure_rate_pct, s.wasted_hours
FROM stats s LEFT JOIN job_names jn ON s.workspace_id=jn.workspace_id AND s.job_id=jn.job_id
ORDER BY s.wasted_hours DESC, s.failure_rate_pct DESC
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
# row is not a finding. The full-scan engine runs each hunt below, and if the
# result's top row crosses `medium`, it raises ONE alert for that hunt with a
# deterministic severity (rule-based, from the hunt's own magnitude metric — we
# never invent a dollar figure the query didn't produce).
#
# Fields per hunt id:
#   metric_col   — the column whose top-row value sets severity (already the sort key)
#   metric_label — human name for that metric
#   unit         — how to render it: "usd" | "ratio" | "count" | "tib" | "hours" | "minutes"
#   high, medium — thresholds on metric_col's top-row value (>= high → high severity, etc.)
#   summary      — one-line finding headline (may reference {n} rows and {metric})
#   usd_col      — column holding a real list-price $ for this finding (None if the hunt
#                  has no honest $ — then the alert shows the metric, not a fabricated cost)
#   rank_by      — how to describe the ranking ("worst first" etc.), for the evidence table
ALERT_SPECS = {
    "H0": {"metric_col": "est_usd_rough", "metric_label": "Est. spend rate (rate-blended, 3h window)",
           "unit": "usd", "high": 100, "medium": 20, "usd_col": "est_usd_rough",
           "summary": "An AI caller is spending at an elevated rate — {metric} in the last 3 hours."},
    "H1": {"metric_col": "read_write_ratio", "metric_label": "Read-to-write ratio",
           "unit": "ratio", "high": 100000, "medium": 1000, "usd_col": None,
           "summary": "{n} MERGE statements read far more than they write (top ratio {metric})."},
    "H2": {"metric_col": "spill_tib", "metric_label": "Disk spill (top statement)",
           "unit": "tib", "high": 1.0, "medium": 0.1, "usd_col": None,
           "summary": "{n} statements spilled to disk — the worst spilled {metric}."},
    "H3": {"metric_col": "stmts", "metric_label": "Chatter statements / week (top client)",
           "unit": "count", "high": 50000, "medium": 10000, "usd_col": None,
           "summary": "Chatty clients are keeping warehouses awake — top client fired {metric} tiny statements."},
    "H5": {"metric_col": "total_min", "metric_label": "Longest statement",
           "unit": "minutes", "high": 480, "medium": 240, "usd_col": None,
           "summary": "{n} statements ran over 4 hours — the longest ran {metric}."},
    "H6": {"metric_col": "read_tib", "metric_label": "Data re-scanned in one hour",
           "unit": "tib", "high": 20, "medium": 3, "usd_col": None,
           "summary": "A repeated multi-TiB investigation scan — {metric} read by one person in an hour."},
    "H7": {"metric_col": "execution_hours", "metric_label": "Compute hours from polling (top principal)",
           "unit": "hours", "high": 100, "medium": 20, "usd_col": None,
           "summary": "A candidate poller ran thousands of short queries — {metric} of compute."},
    "H8": {"metric_col": "queued_hours", "metric_label": "Queue wait (top warehouse)",
           "unit": "hours", "high": 10, "medium": 2, "usd_col": None,
           "summary": "A warehouse is queue-pressured — {metric} spent waiting for capacity."},
    # ── H16–H20 (Aug 2026 thread additions) ──
    "H16": {"metric_col": "run_count", "metric_label": "Runs on All-Purpose (top job)",
            "unit": "count", "high": 100, "medium": 20, "usd_col": None,
            "summary": "{n} jobs ran on All-Purpose compute ($0.55/DBU vs $0.15 on Jobs) — top job {metric} runs."},
    "H17": {"metric_col": "min_clusters", "metric_label": "Min clusters held 24/7 (top warehouse)",
            "unit": "count", "high": 2, "medium": 1, "usd_col": None,
            "summary": "{n} classic warehouses never auto-stop — top holds {metric} cluster(s) warm 24/7."},
    "H18": {"metric_col": "node_minutes", "metric_label": "Idle-cluster runtime (top)",
            "unit": "count", "high": 5000, "medium": 1000, "usd_col": None,
            "summary": "{n} interactive clusters sit at low CPU — the busiest idled for {metric} node-minutes."},
    "H19": {"metric_col": "total_dbus_30d", "metric_label": "Always-on App DBUs (30d, top)",
            "unit": "count", "high": 500, "medium": 100, "usd_col": None,
            "summary": "{n} Databricks Apps run always-on — the top burned {metric} DBUs over 30 days."},
    "H20": {"metric_col": "wasted_hours", "metric_label": "Compute on failed runs (top job)",
            "unit": "hours", "high": 20, "medium": 5, "usd_col": None,
            "summary": "{n} jobs fail repeatedly — the worst wasted {metric} of compute on failed runs."},
    "H21": {"metric_col": "candidate_delta_tables", "metric_label": "Likely-snappy tables (top schema)",
            "unit": "count", "high": 50, "medium": 10, "usd_col": None,
            "summary": "{n} schemas hold Delta tables predating the zstd default — top schema has {metric} candidates to recompress."},
}

