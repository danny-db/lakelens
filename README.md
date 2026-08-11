# LakeLens — FinOps for the Lakehouse

*See every dollar. Spot every saving. (formerly "LakeLedger" / "Databricks Cost Optimiser")*

A self-service Databricks App that turns the `weekly-cost-hunt` notebook into a
guided, plain-English cost review. It runs the **customer's own** `system.*`
tables — no data leaves the workspace, and it never changes a setting.

**Runs entirely in your own workspace** — see **[Deploying LakeLens in your workspace](#deploying-lakelens-in-your-workspace-databricks-asset-bundle)** below.

## What it does

The app is organised into four sections: **① Optimise** (the audit→spot→tune→prove
workflow), **② Observability** (spot the anomaly across Usage, Jobs, DBSQL, and
Model Serving), **③ Attribute** (tagging), and **④ Estimate** (tokenomics) — plus
an **Ask** tab (embedded Genie) for natural-language cost questions.

### Ask (embedded Genie)
A natural-language assistant over the customer's own cost system tables. Because
the app runs in the customer's workspace, it can't hardcode a Genie `space_id`
(and there is no space-less Genie REST API), so a **one-time "Create cost Genie
space" button** provisions a space (seeded with the billing/compute/query/lakeflow
system tables), persists the returned `space_id` to a Delta config table, and
routes all chat through the Conversations API — everything on-behalf-of-user.
Every answer shows **the SQL Genie ran** and the result table, so numbers are
verifiable. Requires the app's `genie` + `sql` user API scopes (adding a scope to
a live app needs the user to **re-authorize** to mint a token with it).

The front door is a **workflow**, not a notebook: one scan builds a triaged alert
queue for the FinOps team. The Spot / Tune / Prove / Tokenomics tabs are the
reference + drill-down behind it.

| Section | Goal | Contents |
|---|---|---|
| **🚨 Alerts** | **The workflow** | One **"Run full cost scan"** button runs every waste-signal hunt asynchronously. Each result comes back as a pre-triaged **alert**: a deterministic **severity**, a plain-English **explanation** (from your chosen model), the **recommended fix**, and **which measure proves the before/after**. A FinOps admin works the queue: **acknowledge → in progress → resolve / dismiss**. Results **persist to Delta** (shared across users, survive restarts) and every run is **compared to the previous one** — each finding shows a NEW / ▲ WORSE / ▼ BETTER / = FLAT badge and its prior value, plus a "cleared since last scan" section. **"What did this scan cost?"** self-attributes the scan's own DBSQL + ai_query() cost. |
| **📊 Observability** | **Spot the anomaly, not just the graph** | Four boards — **Usage** (spend trend / by-SKU / by-workspace / **AI_FORECAST**), **Jobs** (potential savings, low-CPU, high-memory, fixed-workers, outdated-DBR), **DBSQL** (cost by user / source-type / query / trend), **Model Serving** (batch inference / pay-per-token / provisioned-throughput / by-endpoint). Every panel runs a **deterministic anomaly detector** (spike vs history, dominant outlier, new entity, threshold) and leads with plain-English callouts *above* the chart; an optional AI pass narrates what to do. Grounded in the labs Account-Usage-Dashboard-v2, the DBSQL cost-per-query PrPr, and the model-serving cost-attribution dashboard. |
| **🏷️ Tagging** | Cost attribution | Finds **untagged** warehouses, clusters, and spend-by-product (read-only) — the foundation of attribution (deck Step 1). **Set a tag** right from the UI (the one action that writes to the workspace: explicit, confirmed, runs as you so it's permission-gated). |
| **Overview** | Orient | A scan CTA + live KPIs (30-day spend, idle warehouse cost, top product, # checks) + the Spot/Tune/Prove loop + the 4-step maturity model. |
| **① Spot** | Find the waste | 15 "hunts" (H0–H14) grouped into 6 plain-English categories: AI & Model Serving · Idle Compute · Heavy Reads & Spill · Long-Running & Repetitive · Right-Sizing & Config · Spend Trends & Attribution. Each card = ELI5 summary + live results + an optional AI "explain in plain English". |
| **② Tune** | Use Databricks well | 12 best-practice cards (Job-vs-All-Purpose 2–3×, Serverless 25%+, Photon caveat, Liquid Clustering/PO/Zstd, statement timeouts, compute policies, spot, AI guardrails, tagging, budgets, billing admin), grouped by the maturity model. |
| **③ Prove** | Measure the saving | The M-queries: M3 "After Scoreboard" (weekly $/warehouse chart), M1b idle cost, M2 exact $/job+pipeline, M5 timeout scoreboard — with the honest three-currencies framing. |
| **💠 Tokenomics** | Estimate AI cost *ahead* of the bill | Interactive calculator (model / tokens / volume / cache / discount → cost per task/day/month + a live tokens→DBU→$ breakdown), model comparison on your task, workload menu, rate card with a live `list_prices` verify button, actual 30-day model-serving spend per endpoint, and the caching + AI-Gateway pricing explainers. Ported from an internal token cost-estimator notebook. |

Every hunt (H0–H14) has an "✨ Explain" button — a neutral, cost-aware plain-English
gloss over its own result, via `ai_query()`. (`ai_query` is the right tool for a text
gloss: it runs in the SQL engine with the rows inline and needs no extra scope. The
Tokenomics tab's live-call proof is the one place FMAPI-direct would be used instead,
because it needs the token counts back — which `ai_query` strips.)

The hunt/measure SQL is transcribed **verbatim** from the live-validated notebook
(a live-validated `weekly-cost-hunt` notebook). H15
(the one hunt that reads customer data) is intentionally excluded — the app is
system-tables-only.

## Architecture

- **FastAPI** backend (`app/app.py`) + single self-contained `static/index.html`
  + vendored Chart.js. No build step.
- **Persistence & sharing** (`app/store_delta.py`): completed scans, findings, and
  triage are written to Delta tables in `RESULTS_CATALOG.RESULTS_SCHEMA` (set per
  workspace via the bundle; schema default `lakelens`) as the logged-in user. `/api/scan/latest` reads the
  most recent run back with run-over-run comparison, so the queue is **shared** and
  **survives restarts/redeploys**. Findings are written first and the run row last (a
  commit marker) so a reader never sees a half-written run. Falls back to in-memory if
  the schema isn't writable.
- **Multi-worker note:** Databricks Apps runs several worker processes; scan IDs are
  UUIDs (not a per-process counter) and the frontend falls back to the shared
  `/api/scan/latest` when a `/scan/{id}` poll lands on a worker that didn't own the run.
- **The scan workflow** (`app/scan.py`): `POST /api/scan/start` captures the OBO
  client on the request thread, spawns a **background thread** that runs every
  alerting hunt, and returns a `scan_id` instantly. The browser polls
  `GET /api/scan/{id}` and streams findings in as they complete — so no single
  request is ever held past the ~120s app cap. Severity is **deterministic**
  (rule-based, in `hunts.ALERT_SPECS`); the AI only narrates. Triage
  (`POST /api/finding/{hunt_id}/triage`) is keyed by hunt id and stored in an
  in-memory `Store` (demo-scoped — swap for Delta/Lakebase to persist).
- **Auth = on-behalf-of-user (OBO).** Every query runs as the logged-in user via
  the `x-forwarded-access-token` header, so each user sees exactly the cost data
  their own governance allows — no service-principal grant, no account-admin
  dependency. Requires the app's `sql` user API scope. Falls back to the ambient
  client locally so `uvicorn` dev runs work.
- **No held-open requests.** Queries submit with a short wait then poll to
  completion, so nothing hits the Databricks Apps ~120s request cap.
- **Row cap.** The backend ships only the top ~200 rows to the browser (the UI
  shows 100) but reports the true total — a hunt returning 150k rows never fully
  crosses the wire. Chart measures are the only unbounded callers.
- `/api/whoami` is a diagnostic that confirms OBO is working (auth mode + who the
  SQL path resolves to).

---

# Deploying LakeLens in your workspace (Databricks Asset Bundle)

LakeLens ships as a **Databricks Asset Bundle** (`databricks.yml`), so you can
deploy it into your own workspace with two commands. Nothing workspace-specific
is hardcoded — you pass your warehouse and catalog as variables.

## Prerequisites

1. **Databricks CLI ≥ 0.246.0** (`databricks --version`). Earlier versions can't set
   the app's OBO scopes from the bundle. Install/upgrade:
   `brew install databricks` or see the [CLI docs](https://docs.databricks.com/dev-tools/cli/install).
2. **Authenticate** the CLI to your workspace:
   `databricks auth login --host https://<your-workspace>.cloud.databricks.com`
   (creates a profile you pass with `-p <profile>`).
3. **A SQL warehouse** the app will run queries on — get its id from
   `databricks warehouses list` (Serverless is ideal). This is `warehouse_id`.
4. **A catalog you can `CREATE` in** — LakeLens persists scan results + the Genie
   space-id here (e.g. `main`, or a dedicated `lakelens` catalog). This is
   `results_catalog`. Skip it and the app still runs, just without shared/persisted
   scans.
5. **Permission to create Databricks Apps** in the workspace (the CLI user, or the
   bundle's run-as principal).

## Two-command deploy

```bash
# from the repo root (where databricks.yml lives)

# 1. Deploy the bundle: uploads the app source + creates/updates the app resource,
#    wired to your warehouse + catalog and with the OBO scopes (sql, genie) set.
databricks bundle deploy -t prod -p <profile> \
  --var="warehouse_id=<your-sql-warehouse-id>" \
  --var="results_catalog=<a-catalog-you-can-write-to>"

# 2. Start (or restart) the app with the deployed code.
databricks bundle run lakelens -t prod -p <profile>
```

`bundle run` prints the app URL (`https://lakelens-<workspace-id>.aws.databricksapps.com`).
That's it — no separate `apps create` / `sync` / `apps deploy` needed; the bundle
does all of it.

> **Tip:** instead of `--var` each time, edit the `prod` target in `databricks.yml`
> to pin your `host`, `warehouse_id`, and `results_catalog`, then just
> `databricks bundle deploy -t prod`.

## Configuration surface

All config is bundle **variables** (in `databricks.yml`, override with `--var` or per
target). The bundle passes them to the app as env vars — you never edit code.

| Variable | Required | Default | What it is |
|---|---|---|---|
| `warehouse_id` | **yes** | — | SQL warehouse the app runs every query on |
| `results_catalog` | **yes*** | — | Catalog for persisted scans + Genie space-id (*app runs without it, just no persistence) |
| `results_schema` | no | `lakelens` | Schema inside that catalog |
| `ai_endpoint` | no | `databricks-gpt-5-4-mini` | Model-serving endpoint for the plain-English explainers |
| `ai_endpoint_dbu` | no | `7.5,30` | Published DBU/1M-token rate (in,out) of `ai_endpoint`, to price the scan's own AI cost |

## Permissions your users need (OBO model)

LakeLens runs **every query as the signed-in user** (on-behalf-of-user), so it needs
no blanket service-principal grants. Each user who opens the app needs, in their own
right:

- **`SELECT` on the `system` schemas** it reads: `system.billing`, `system.compute`,
  `system.query`, `system.lakeflow`, `system.access`. (Grant with
  `GRANT USE SCHEMA ON SCHEMA system.billing TO \`<group>\`` etc., or enable the
  system schemas for the account.)
- **`CAN USE`** on the SQL warehouse (the bundle grants the *app* this; users also
  need it for their own token).
- **`CREATE`** on `results_catalog` (only for persistence — first scan creates the
  schema + tables).
- For the **Ask (Genie)** tab: the `genie` + `dashboards.genie` scopes are set by the
  bundle; the user also needs permission to create a Genie space (first-run button).

## First run

1. Open the app URL, complete SSO — you'll see a **consent screen** listing the
   scopes (SQL, Genie). Click **Accept**.
2. Hit **`/api/whoami`** (append to the URL) to confirm OBO: expect
   `"sql_ok": true` and your email as `sql_runs_as`.
3. Go to **Alerts → Run full cost scan** to populate the queue.
4. Go to **Ask** → click **"Create cost Genie space"** once (provisions the Genie
   space, seeded with the cost system tables). Subsequent visits skip straight to chat.

### ⚠️ If you change the app's scopes later
Adding a `user_api_scope` to an already-authorized app does **not** update an existing
user's consent — their token keeps the old scopes (clearing cookies won't fix it,
because the workspace SSO session silently re-issues the old grant). To force
re-consent, an admin deletes the stale grant:
```bash
CLIENT_ID=$(databricks apps get lakelens -p <profile> | jq -r .oauth2_app_client_id)
databricks api delete "/api/2.0/oauth-app-integrations/$CLIENT_ID/user-consent/me" -p <profile>
```
then the user reopens the app in a **fresh/incognito** window and re-accepts. (Setting
all scopes at first deploy — as this bundle does — avoids this entirely.)

## Local development

```bash
cd app
export DATABRICKS_CONFIG_PROFILE=<profile>
export DATABRICKS_WAREHOUSE_ID=<warehouse_id>
export RESULTS_CATALOG=<catalog>
export AI_ENDPOINT=databricks-gpt-5-4-mini
python3 -m uvicorn app:app --host 127.0.0.1 --port 8771
```
Without a forwarded user token the app runs queries as your CLI profile, so local
runs behave like the deployed app minus per-user OBO.

## Validate the SQL

`python3 validate.py` runs every hunt/explainer/measure and `python3 validate_boards.py`
runs every Observability panel — against the configured warehouse, failing loudly on any
error. Run after editing any SQL. (Both use a `PROFILE`/`WAREHOUSE` constant near the top
— point them at your workspace.)

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| App shows but scans return an auth error | User lacks `SELECT` on a `system.*` schema, or `CAN USE` on the warehouse. Check `/api/whoami`. |
| "No warehouse configured" | `warehouse_id` variable not set — redeploy with `--var="warehouse_id=…"`. |
| Scans run but don't persist / not shared | `results_catalog` unset, or the user lacks `CREATE` on it. App still works in-memory. |
| Ask tab: "token does not have required scopes: genie" | Stale consent grant — see the re-consent box above. |
| Genie space create fails | User lacks permission to create a Genie space, or the `genie` scope isn't consented yet. |
| `bundle deploy` rejects `user_api_scopes` | CLI < 0.246.0 — upgrade the Databricks CLI. |
