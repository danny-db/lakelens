"""
tune.py — the "Tune" page content: how to use Databricks cost-effectively.

This is a static best-practice library distilled from the FinOps deck. It is
NOT query-driven — it is the reference knowledge that turns a spotted pattern
into a fix. Grouped by the deck's four-step cost-maturity model so a reader can
see where each practice fits.

Every number here is from the deck; each card explains it in plain English.
"""

# The four-step maturity model — the backbone of the Tune page and the Overview.
MATURITY = [
    {"step": 1, "name": "Attribute", "blurb":
        "Know whose cost is whose. Tag every resource and give finance a billing-admin "
        "view, so spend can be traced to a team, project, or environment."},
    {"step": 2, "name": "Report", "blurb":
        "See the spend. Use system tables and the built-in dashboards to watch usage, "
        "rank the top spenders, and forecast — then set budget alerts."},
    {"step": 3, "name": "Control", "blurb":
        "Set the guardrails. Compute policies cap runaway sizing, statement timeouts stop "
        "runaway queries, and rate limits protect model endpoints."},
    {"step": 4, "name": "Optimise", "blurb":
        "Tune the platform. Pick the right compute for each workload, and use features "
        "like serverless, Liquid Clustering, and compression to do the same work for less."},
]

# Best-practice cards. Each: id, step (maturity), title, eli5, detail (bullets), metric (headline).
PRACTICES = [
    # ── Step 4: Optimise (the highest-leverage, most concrete) ───────────────
    {
        "id": "job-vs-allpurpose", "step": 4,
        "title": "Use Job compute for jobs, not All-Purpose",
        "metric": "2–3× cheaper",
        "eli5": "All-Purpose clusters are for interactive notebook work. Running a scheduled "
                "job on one costs 2–3× more than running it on Job compute, which spins up for "
                "the job and shuts down after.",
        "detail": [
            "All-Purpose is billed at a higher DBU rate than Jobs compute for the same machine.",
            "A job pinned to an always-on All-Purpose cluster also pays for all the idle time between runs.",
            "Move scheduled work to Jobs Compute (ephemeral) or Serverless Jobs.",
            "Spot the offenders with the ‘Idle Compute’ hunts, then confirm the DBU moves to the "
            "jobs product after you switch.",
        ],
    },
    {
        "id": "serverless", "step": 4,
        "title": "Serverless for spiky and ad-hoc work",
        "metric": "25%+ savings",
        "eli5": "Serverless starts in under ten seconds and charges only while it is working — "
                "there is no idle cost. For bursty, unpredictable, or small workloads it is "
                "typically 25%+ cheaper than keeping a classic warehouse warm.",
        "detail": [
            "Sub-10-second startup means you don't keep compute running ‘just in case’.",
            "Zero idle cost — it stops the moment work finishes.",
            "Best fit: ad-hoc SQL, bursty BI, small or unpredictable jobs.",
            "Steady, predictable, high-utilisation load can still be cheaper on right-sized classic "
            "with committed-use discounts — decide by load shape (see the ‘Queue Pressure’ hunt).",
        ],
    },
    {
        "id": "photon", "step": 4,
        "title": "Photon: know the break-even",
        "metric": "2.9× DBU on jobs",
        "eli5": "Photon is a faster engine, but it bills more DBUs per hour — about 2.9× on job "
                "clusters and ~2× on All-Purpose. It only saves money if it cuts the runtime by "
                "MORE than that multiplier. Great for heavy SQL scans; wasteful on light work.",
        "detail": [
            "Photon speeds up scans, joins, and aggregations — CPU-heavy SQL work.",
            "It does NOT speed up UDFs, streaming state, or lightweight jobs — those pay the "
            "premium with no runtime win.",
            "Rule of thumb: enable it where it cuts runtime by more than the DBU multiplier; "
            "measure before/after with the ‘Prove’ page.",
            "On classic Jobs there is no Photon price premium (it bills the same) — there, Photon "
            "is a pure speed win.",
        ],
    },
    {
        "id": "storage", "step": 4,
        "title": "Liquid Clustering, Predictive Optimization & Zstd",
        "metric": "up to 40% storage",
        "eli5": "Three low-effort storage wins: Liquid Clustering keeps data laid out so queries "
                "skip what they don't need; Predictive Optimization runs the housekeeping for you; "
                "and Zstd compression can cut storage up to 40% with no maintenance.",
        "detail": [
            "Liquid Clustering: turn on for all new Delta tables — ~2.5× faster than the old "
            "Z-order approach, up to a 150 TB table size (as of Aug 2026).",
            "Predictive Optimization: hands-off compaction and clustering — but only on Unity "
            "Catalog MANAGED tables, and worth DISABLING on non-prod schemas that never get scanned.",
            "Zstd compression: up to 40% storage savings on UC managed tables, zero maintenance.",
        ],
    },
    # ── Step 3: Control ──────────────────────────────────────────────────────
    {
        "id": "timeout", "step": 3,
        "title": "Set a statement timeout",
        "metric": "default is 2 DAYS",
        "eli5": "Left alone, a query can run for TWO DAYS before Databricks stops it. A runaway "
                "or hung statement can burn compute that whole time. Set a sane workspace timeout "
                "(say 2–4 hours) so it stops itself.",
        "detail": [
            "The platform default STATEMENT_TIMEOUT is 2 days — almost never what you want for ad-hoc.",
            "Set it workspace-wide; long ETL can raise its own limit per session.",
            "It caps EXECUTION time (time actually running), not queue or fetch time.",
            "Changing the workspace value restarts running warehouses — schedule the change.",
            "Baseline the offenders and confirm zero afterwards with the ‘Timeout scoreboard’ measure.",
        ],
    },
    {
        "id": "compute-policies", "step": 3,
        "title": "Compute policies to stop sprawl",
        "metric": "cap DBU/hr & clusters",
        "eli5": "A compute policy sets the rules for what users can create — a maximum DBU-per-hour, "
                "how many clusters, which machine types, and which runtime. It stops one person from "
                "spinning up something enormous by accident.",
        "detail": [
            "Cap DBU/hr and max clusters per user to prevent runaway sizing.",
            "Allowlist instance types (or use regex) to keep people on sensible machine families.",
            "Enforce the latest long-term-support runtime for automatic security and performance updates.",
            "Enforce tagging through the policy so cost attribution can't be skipped.",
        ],
    },
    {
        "id": "spot", "step": 3,
        "title": "Spot VMs for retryable batch",
        "metric": "big infra discount",
        "eli5": "Spot (preemptible) VMs are much cheaper cloud machines that can be reclaimed at "
                "short notice. Perfect for batch jobs that can retry — but NOT for continuous "
                "pipelines, where a reclaim restarts work and can cost more than it saves.",
        "detail": [
            "Use PREEMPTIBLE_WITH_FALLBACK so the job falls back to on-demand if spot is unavailable.",
            "On GCP there is no ‘first N on-demand’ control that some other clouds have — plan for the driver.",
            "Great for retryable batch jobs; avoid for continuous change-data pipelines.",
        ],
    },
    {
        "id": "ai-controls", "step": 3,
        "title": "Guardrails for AI spend",
        "metric": "no real-time $ cap exists",
        "eli5": "There is no self-serve, real-time dollar hard-cap for ai_query today. So the "
                "controls are: don't hand expensive models to everyone (permissions are the "
                "kill-switch), alert on the hourly token rate, and default pipelines to a cheap model.",
        "detail": [
            "Governance gate: don't grant EXECUTE on Sonnet/Opus-class models to every pipeline "
            "by default — grant per team, on request, with a named owner.",
            "Default classification/extraction pipelines to a cheap model (e.g. Haiku); require "
            "justification for expensive models at batch scale.",
            "A rate limit of 0 will NOT cap a runaway batch job — use the token-rate alert (the "
            "‘Runaway AI spend’ hunt) as the fast substitute.",
            "For sustained workloads, Provisioned Throughput converts pay-per-token into a fixed, "
            "bounded cost.",
        ],
    },
    # ── Step 2: Report ─────────────────────────────────────────────────────────
    {
        "id": "dashboards", "step": 2,
        "title": "Built-in cost dashboards",
        "metric": "zero to build",
        "eli5": "Databricks ships ready-made dashboards for account usage, jobs, DBSQL, and model "
                "serving. They give you a top-N ranking, efficiency signals, and AI forecasts out "
                "of the box — no building required.",
        "detail": [
            "Account Usage dashboard: usage overview, top-N spenders, tag analysis, AI forecast.",
            "Jobs Monitoring dashboard: top jobs by potential saving, CPU/memory efficiency, outdated runtimes.",
            "DBSQL Monitoring dashboard: cost by user and by source (notebook / job / SQL / Genie), query drill-down.",
            "Model Serving dashboard: batch, pay-per-token, and provisioned-throughput cost attribution.",
        ],
    },
    {
        "id": "budgets", "step": 2,
        "title": "Budget alerts",
        "metric": "up to 4 thresholds",
        "eli5": "A budget watches spend — account-wide or scoped by workspace or tag — and emails "
                "you when it crosses a threshold. It is an alert, not a hard block (except Genie, "
                "which can block).",
        "detail": [
            "Set account-wide or scoped by workspace/tag, with up to four alert thresholds each.",
            "Budgets are based on list price, so read them relative to your contract.",
            "Genie budgets add per-user thresholds and an optional block switch — the one place "
            "spend can actually be stopped, not just alerted.",
        ],
    },
    # ── Step 1: Attribute ──────────────────────────────────────────────────────
    {
        "id": "tagging", "step": 1,
        "title": "Tag everything",
        "metric": "the foundation",
        "eli5": "Tags are labels on your compute — team, project, environment. They are the "
                "foundation of everything else: without them, spend is one big anonymous number "
                "you can't split, attribute, or budget by.",
        "detail": [
            "Three tiers of tag: general (team/owner), contextual (project), operational (environment).",
            "Classic compute is tagged in the cluster/job/warehouse definition; serverless is tagged "
            "through Budget Policies (which auto-apply on creation).",
            "A Budget Policy is different from a Budget — the policy applies tags; the budget alerts on spend.",
            "Enforce tags through compute policy so nothing untagged can be created, and run a "
            "compliance job to catch gaps.",
        ],
    },
    {
        "id": "billing-admin", "step": 1,
        "title": "Give finance the billing-admin role",
        "metric": "separation of duties",
        "eli5": "The billing-admin role gives finance a focused view of spend (the billing system "
                "tables and price list) without making them full platform admins. It separates "
                "‘who watches the money’ from ‘who runs the platform’.",
        "detail": [
            "Billing admins can read system.billing.usage and system.billing.list_prices.",
            "It's a narrower role than account admin — right for a finance/FinOps function.",
            "Workspace admins need the billing-admin role added to manage all cost policies.",
        ],
    },
]
