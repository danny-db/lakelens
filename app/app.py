"""
LakeLens (formerly Databricks Cost Optimiser) — FastAPI backend.

A self-service layer over the `weekly-cost-hunt` notebook. It runs the SAME
system-table queries the notebook runs — against the CUSTOMER'S OWN
system.* tables — and presents them as guided "hunts" (find waste),
"tune" guidance (use Databricks cost-effectively), and "measures"
(prove the saving).

Auth model: on-behalf-of-user (OBO). Each request builds a WorkspaceClient
from the `x-forwarded-access-token` header, so every query runs as the
signed-in user and needs that user's own SELECT on the system schemas.
Locally (no forwarded token) it falls back to the ambient client.

Timeout model: Databricks Apps cut a single HTTP request at ~120s (hard).
Some hunts scan 14 days of query history. So every query is submitted with
a short server-side wait, then POLLED to completion via get_statement — no
single request is ever held open long enough to be cut.
"""
import os
import re
import logging
import threading
from pathlib import Path
from contextlib import asynccontextmanager
from typing import Optional, Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel

from databricks.sdk import WorkspaceClient
from databricks.sdk.core import Config
from databricks.sdk.errors import DatabricksError, NotFound, PermissionDenied, Unauthenticated
from databricks.sdk.service.sql import StatementState, ExecuteStatementRequestOnWaitTimeout

import hunts as catalogue
import tune as tune_content
import tokenomics as token_content
import scan as scan_engine
import store_delta
import tagging as tagging_content
import boards as boards_content
import genie_cost
import models as chat_catalogue
import pricing

logger = logging.getLogger("uvicorn.error")

# ── Config — every value comes from env (set by the DAB bundle / app.yaml), so the
# app is fully portable: no workspace-specific value is hardcoded here. ──────────
# REQUIRED: the SQL warehouse the app runs every query against (set by the bundle).
WAREHOUSE_ID = os.getenv("DATABRICKS_WAREHOUSE_ID", "")
# The model-serving endpoint for the plain-English explainers. A cheap tier is plenty.
AI_ENDPOINT = os.getenv("AI_ENDPOINT", "databricks-gpt-5-4-mini")
# Published DBU-per-1M-token rate (input, output) for the explainer endpoint,
# used to price the scan's own ai_query cost from the rate card rather than from
# this shared workspace's noisy billing. Default = a conservative small-model
# ("mini") rate; override via env if you point AI_ENDPOINT at a pricier model.
AI_ENDPOINT_DBU = tuple(float(x) for x in os.getenv("AI_ENDPOINT_DBU", "7.5,30").split(","))
# Where scan results are persisted (shared, durable). The app
# writes here AS THE LOGGED-IN USER, so they need CREATE on this catalog/schema.
# REQUIRED: set RESULTS_CATALOG to a catalog the app's users can write to (e.g. a
# dedicated `lakelens` catalog, or `main`). No default — must be set per workspace so
# we never silently write to someone else's catalog. If unset, persistence is skipped
# (the app still runs; scans just aren't shared across users/restarts).
RESULTS_CATALOG = os.getenv("RESULTS_CATALOG", "")
RESULTS_SCHEMA = os.getenv("RESULTS_SCHEMA", "lakelens")
# How long the SQL engine may run before we start polling (seconds, 5-50 allowed).
FIRST_WAIT = "30s"
# How many times we poll after that, and the gap between polls (seconds).
POLL_TRIES = 40
POLL_GAP_S = 3

# Ambient client (the app's own service principal in the Apps runtime, or a
# CLI profile locally). Used as a fallback and to read app config.
sp_client: Optional[WorkspaceClient] = None
HOST = os.getenv("DATABRICKS_HOST", "")
# The Databricks Apps runtime sets DATABRICKS_APP_NAME. There, a request without a
# user token must not fall back to the app's own service principal.
IN_APPS_RUNTIME = bool(os.getenv("DATABRICKS_APP_NAME"))
# The Apps runtime sets the workspace id; locally it is read once from the ambient client.
WORKSPACE_ID = os.getenv("DATABRICKS_WORKSPACE_ID", "")


def _normalize_host(h: Optional[str]) -> str:
    """Return the workspace host as a full https:// URL with no trailing slash.
    The SDK/env host is sometimes bare (`foo.cloud.databricks.com`); without the
    scheme, a browser treats a deep link as a RELATIVE path and it loops back to
    the app. So always prepend https:// when missing."""
    h = (h or "").strip().rstrip("/")
    if not h:
        return ""
    if not h.startswith(("http://", "https://")):
        h = "https://" + h
    return h


@asynccontextmanager
async def lifespan(app: FastAPI):
    global sp_client, HOST
    sp_client = WorkspaceClient()  # ambient SP OAuth in the Apps runtime (or local profile)
    HOST = HOST or sp_client.config.host
    logger.info(f"LakeLens started | WH={WAREHOUSE_ID} | AI={AI_ENDPOINT}")
    yield


app = FastAPI(title="LakeLens — FinOps for the Lakehouse", lifespan=lifespan)


def _client_for(request: Request) -> WorkspaceClient:
    """Return the client that should run this request's queries.

    Cost and billing data is account-wide, so we run every query ON BEHALF OF
    THE LOGGED-IN USER (OBO): the app forwards the user's token in the
    `x-forwarded-access-token` header, and the warehouse enforces that user's
    own governance server-side. This means each user sees exactly the system-
    table data they are entitled to — no blanket service-principal grant, and
    no account-admin dependency to stand the app up.

    Locally (no forwarded header) we fall back to the ambient client, which
    is the developer's CLI profile — so `uvicorn` dev runs still work. In the
    Apps runtime a missing token is a 401, never the service principal.
    """
    tok = request.headers.get("x-forwarded-access-token")
    if tok:
        # auth_type="pat" is REQUIRED: without it the SDK sees the ambient
        # DATABRICKS_CLIENT_ID/SECRET too and errors "more than one auth method".
        return WorkspaceClient(config=Config(host=HOST, token=tok, auth_type="pat"))
    if IN_APPS_RUNTIME:
        raise HTTPException(401, "No user token on this request. Open the app from its Databricks URL "
                                 "and accept the consent screen.")
    return sp_client


# ── SQL helpers ──────────────────────────────────────────────────────────────

# The UI only ever shows the top ~100 rows of a table, so by default we ship
# just the first slice to the browser and report the true total separately.
# A big customer workspace can return 150k+ rows for some hunts — transferring
# all of them is pure waste. Chart measures pass max_rows=None (they need every
# row to build the series) and are the only unbounded callers.
DEFAULT_MAX_ROWS = 200


def _params(items) -> Optional[list]:
    """Build SDK StatementParameterListItem objects from [{name,value,type?}] dicts."""
    if not items:
        return None
    from databricks.sdk.service.sql import StatementParameterListItem
    out = []
    for p in items:
        out.append(StatementParameterListItem(
            name=p["name"], value=p.get("value"), type=p.get("type")))
    return out


def _sdk_message(e: Exception) -> str:
    """The SDK's own words, without the request id and Config dump it appends."""
    return re.split(r"\s*\[ReqId|\. Config:", str(e))[0]


def _run_sql(sql: str, client: WorkspaceClient, max_rows: Optional[int] = DEFAULT_MAX_ROWS,
             params: Optional[list] = None) -> dict[str, Any]:
    """The SQL boundary. Every failure leaves as an HTTPException with a plain
    message, so routes can pass it straight through."""
    if not WAREHOUSE_ID:
        raise HTTPException(503, "No warehouse configured. Redeploy with --var=\"warehouse_id=...\".")
    try:
        return _execute_sql(sql, client, max_rows, params)
    except Unauthenticated:
        raise HTTPException(401, "Databricks rejected your sign-in token. Reload the app to sign in again.")
    except PermissionDenied as e:
        raise HTTPException(403, f"Permission denied: {_sdk_message(e)}")
    except DatabricksError as e:
        raise HTTPException(502, f"SQL request failed: {_sdk_message(e)}")


def _execute_sql(sql: str, client: WorkspaceClient, max_rows: Optional[int],
                 params: Optional[list]) -> dict[str, Any]:
    """Execute SQL and return {columns, rows, total_rows, truncated, wall_ms}. Submits
    with a short wait, then polls to completion so we never hold one HTTP request past
    the app's 120s cap. Stops fetching result chunks once max_rows is reached, so
    a 150k-row result never fully crosses the wire when the UI shows only 100.

    `params` are named SQL parameters ([{name,value,type?}]) — used for safe writes
    of free text (AI explanations etc.) into the Delta result tables.
    `wall_ms` is the wall-clock time this call took — used by the scan to give an
    instant self-cost estimate without waiting for billing tables to catch up."""
    import time
    _t0 = time.monotonic()

    stmt = client.statement_execution.execute_statement(
        warehouse_id=WAREHOUSE_ID,
        statement=sql,
        wait_timeout=FIRST_WAIT,
        on_wait_timeout=ExecuteStatementRequestOnWaitTimeout.CONTINUE,
        parameters=_params(params),
    )
    state = stmt.status.state
    statement_id = stmt.statement_id

    # Poll until terminal, if it didn't finish inside the first wait.
    tries = 0
    while state in (StatementState.PENDING, StatementState.RUNNING) and tries < POLL_TRIES:
        time.sleep(POLL_GAP_S)
        stmt = client.statement_execution.get_statement(statement_id)
        state = stmt.status.state
        tries += 1

    if state != StatementState.SUCCEEDED:
        msg = "query did not finish in time (still running)"
        if stmt.status and stmt.status.error:
            msg = stmt.status.error.message or msg
        if msg.startswith("[AI_FUNCTION"):
            name = re.search(r"'([^']+)' does not exist", msg)
            raise HTTPException(502, f"AI endpoint '{name.group(1)}' is unavailable. Pick another model."
                                if name else "The AI endpoint is unavailable. Pick another model.")
        raise HTTPException(500, f"SQL {state.value if state else 'FAILED'}: {msg}")

    cols = [c.name for c in stmt.manifest.schema.columns] if stmt.manifest and stmt.manifest.schema else []
    total = stmt.manifest.total_row_count if stmt.manifest else None

    # Result may arrive inline, or as chunks we must fetch (large results). Stop
    # once we have max_rows — no point pulling further chunks the UI won't show.
    rows: list[list] = []

    def _enough() -> bool:
        return max_rows is not None and len(rows) >= max_rows

    if stmt.result and stmt.result.data_array:
        rows.extend(stmt.result.data_array)
    next_idx = stmt.result.next_chunk_index if stmt.result else None
    while next_idx is not None and not _enough():
        chunk = client.statement_execution.get_statement_result_chunk_n(statement_id, next_idx)
        if chunk.data_array:
            rows.extend(chunk.data_array)
        next_idx = chunk.next_chunk_index

    if max_rows is not None and len(rows) > max_rows:
        rows = rows[:max_rows]

    return {
        "statement_id": statement_id,   # so a caller can attribute this query's own cost (M1)
        "wall_ms": (time.monotonic() - _t0) * 1000.0,   # wall-clock for instant self-cost
        "columns": cols,
        "rows": rows,
        "total_rows": total if total is not None else len(rows),
        "truncated": bool(total is not None and total > len(rows)),
    }


def _run_scalar(sql: str, client: WorkspaceClient) -> Optional[str]:
    """Run a one-cell query and return the single value (or None)."""
    out = _run_sql(sql, client)
    if out["rows"] and out["rows"][0]:
        return out["rows"][0][0]
    return None


def _sql_for_store(sql: str, client, params=None) -> dict[str, Any]:
    """Adapter for store_delta reads: (sql, client, params) → {rows, columns}."""
    return _run_sql(sql, client, max_rows=5000, params=params)


def _write_for_store(sql: str, client, params=None) -> None:
    """Adapter for store_delta writes (INSERT/DDL/DELETE): run and discard result."""
    _run_sql(sql, client, max_rows=1, params=params)


# Delta-backed shared store (created per-request with the OBO client). One instance
# is cheap; it holds no state itself — the OBO client is passed into each call.
DELTA = store_delta.DeltaStore(RESULTS_CATALOG, RESULTS_SCHEMA, _sql_for_store, _write_for_store)


def _bounded(sql: str, n: int = 10) -> str:
    """Return the hunt SQL capped to its top-n rows for the explainer prompt.
    Strips any trailing LIMIT (so we don't double it) and appends LIMIT n
    directly after the query's own ORDER BY — the same top-n the notebook's
    explainer cells used, and small enough to stay well under the model's
    input-token limit even on a large customer workspace."""
    s = sql.strip().rstrip(";").rstrip()
    s = re.sub(r"(?is)\s+limit\s+\d+\s*$", "", s)  # drop a trailing LIMIT if present
    return s + f"\nLIMIT {n}"


def _explainer_model(model: Optional[str]) -> str:
    """Resolve the explainer endpoint: a caller override if it's a known FMAPI
    endpoint (guards against injection into the ai_query() literal), else the
    configured default. Only 'databricks-*' names are accepted."""
    if model and chat_catalogue.ENDPOINT_NAME.fullmatch(model):
        return model
    return AI_ENDPOINT


def _ai_explain(rubric: str, hunt_desc: str, rows_sql: str, client: WorkspaceClient,
                model: Optional[str] = None) -> str:
    """Ask the model for a neutral, cost-aware gloss over a hunt's top rows.
    Mirrors the notebook's explainer: deterministic rubric injected, model only
    narrates. Runs ai_query via SQL so it works under the `sql` scope. `model`
    optionally overrides the endpoint (the UI model picker)."""
    ep = _explainer_model(model)
    prompt = (
        "You are a Databricks cost analyst. Interpret the result using THIS rubric: \""
        + rubric
        + "\" In 2-3 neutral sentences, explain what the result shows and whether it "
        "looks like waste worth investigating. Cite the figures; do NOT overstate cost. "
        "If the row list is empty, say the hunt found nothing notable this period. Hunt = "
        + hunt_desc + ". Rows: "
    )
    esc_prompt = prompt.replace("\\", "\\\\").replace("'", "''")
    wrapped = f"""
SELECT ai_query('{ep}',
  CONCAT('{esc_prompt}', coalesce(to_json(collect_list(struct(t.*))), '[]'))) AS explanation
FROM ( {rows_sql} ) t
"""
    return _run_scalar(wrapped, client) or "(no explanation returned)"


def _ai_explain_trend(instruction: str, rows_sql: str, client: WorkspaceClient,
                      model: Optional[str] = None) -> str:
    """Explainer variant for trend hunts (e.g. H10) — a full instruction string,
    not the row rubric."""
    ep = _explainer_model(model)
    esc = instruction.replace("\\", "\\\\").replace("'", "''")
    wrapped = f"""
SELECT ai_query('{ep}',
  CONCAT('{esc}', coalesce(to_json(collect_list(struct(t.*))), '[]'))) AS explanation
FROM ( {rows_sql} ) t
"""
    return _run_scalar(wrapped, client) or "(no explanation returned)"


# ── API: metadata (drives the UI, no warehouse needed) ───────────────────────

def _failure_reason(e: Exception) -> str:
    """A short reason for the UI. An error body the SDK can't parse carries its request log,
    whose '< 502 Bad Gateway' line names the HTTP status."""
    if isinstance(e, HTTPException):
        return str(e.detail)
    status = re.search(r"^< (\d{3}\b.*)$", str(e), re.M)
    return f"HTTP {status.group(1).strip()}" if status else _sdk_message(e)[:200]


@app.get("/api/models")
def list_models(request: Request):
    """The chat models this workspace serves, for the explainer picker, by display name and
    group. Read straight from the REST API and parsed for the few fields used, so a change in
    the SDK's endpoint model can't break it. If listing fails, only the configured default is
    offered, with the reason."""
    try:
        raw = _client_for(request).api_client.do("GET", "/api/2.0/serving-endpoints")
        listed = [m._asdict() for m in chat_catalogue.chat_models(raw)]
        return {"models": listed, "groups": chat_catalogue.GROUPS, "default": AI_ENDPOINT, "error": None}
    except Exception as e:  # noqa: BLE001
        logger.warning(f"list_models failed: {e}")
        return {"models": [{"id": AI_ENDPOINT, "name": AI_ENDPOINT, "group": chat_catalogue.group_of(AI_ENDPOINT)}],
                "groups": chat_catalogue.GROUPS, "default": AI_ENDPOINT,
                "error": f"Couldn't list this workspace's models ({_failure_reason(e)}). "
                         "Showing the configured default only."}


@app.get("/api/whoami")
def whoami(request: Request):
    """Diagnostic: confirm the on-behalf-of-user (OBO) path is working.
    Reports whether this request carried a forwarded user token, and who the
    SQL path resolves to. Safe, read-only — used to validate OBO after deploy."""
    has_obo = bool(request.headers.get("x-forwarded-access-token"))
    fwd_email = request.headers.get("x-forwarded-email") or request.headers.get("x-forwarded-user")
    out: dict[str, Any] = {
        "obo_token_present": has_obo,
        "forwarded_email": fwd_email,
        "auth_mode": "on-behalf-of-user (OBO)" if has_obo else "service principal (fallback)",
    }
    try:
        client = _client_for(request)
        who = _run_scalar("SELECT current_user()", client)
        out["sql_runs_as"] = who
        out["sql_ok"] = True
    except Exception as e:
        out["sql_ok"] = False
        out["sql_error"] = str(e)[:300]
    return out


@app.get("/api/meta")
def get_meta():
    """Everything the frontend needs to render the pages, minus live query results."""
    def hunt_card(h):
        return {
            "id": h["id"], "category": h["category"], "title": h["title"],
            "eli5": h["eli5"], "what": h["what"], "action": h["action"],
            "measure": h.get("measure"),
            "has_explainer": bool(h.get("explain_rubric") or h.get("explain_is_trend")),
            "caution": h.get("caution"),
            "inventory": bool(h.get("inventory")),
        }

    def measure_card(m):
        return {"id": m["id"], "title": m["title"], "eli5": m["eli5"],
                "what": m["what"], "chart": m.get("chart", "table"),
                "group": m.get("group", "other"), "order": m.get("order", 99),
                "use_when": m.get("use_when")}

    return {
        "categories": catalogue.CATEGORIES,
        "hunts": [hunt_card(h) for h in catalogue.HUNTS],
        "measures": [measure_card(m) for m in catalogue.MEASURES],
        "maturity": tune_content.MATURITY,
        "practices": tune_content.PRACTICES,
        "tokenomics": token_content.public_meta(),
        "alert_hunts": list(catalogue.ALERT_SPECS.keys()),
        "triage_states": list(scan_engine.TRIAGE_STATES),
        "tagging": tagging_content.public_meta(),
        "boards": boards_content.all_meta(),
        "ai_endpoint": AI_ENDPOINT,
        "warehouse_configured": bool(WAREHOUSE_ID),
        # Workspace URL so the frontend can deep-link a statement_id / dashboard / job
        # back to this workspace's UI (Query History etc.). MUST include the https://
        # scheme — without it the browser treats the value as a relative path and the
        # link loops back to the app. Trailing slash stripped.
        "workspace_host": _normalize_host(HOST),
    }


# ── API: KPIs for the Overview page ──────────────────────────────────────────

@app.get("/api/kpis")
def get_kpis(request: Request):
    """Four headline numbers for the top of the app. Each is a small, fast query;
    any that fails degrades to null rather than failing the whole page."""
    client = _client_for(request)
    kpis: dict[str, Any] = {}

    # 30-day total list-price DBU $ across the account (region-scoped system tables).
    try:
        kpis["spend_30d"] = _run_scalar("""
            WITH price AS (SELECT sku_name, cloud, price_start_time, price_end_time,
                                  pricing.effective_list.default AS unit_usd
                           FROM system.billing.list_prices)
            SELECT round(sum(u.usage_quantity * pr.unit_usd), 0)
            FROM system.billing.usage u
            LEFT JOIN price pr ON u.sku_name = pr.sku_name AND u.cloud = pr.cloud
              AND u.usage_start_time >= pr.price_start_time
              AND (pr.price_end_time IS NULL OR u.usage_start_time < pr.price_end_time)
            WHERE u.usage_date >= current_date() - INTERVAL 30 DAYS
        """, client)
    except Exception as e:
        logger.warning(f"kpi spend_30d failed: {e}")
        kpis["spend_30d"] = None

    # Top product (SKU) by 30-day $.
    try:
        top = _run_sql("""
            WITH price AS (SELECT sku_name, cloud, price_start_time, price_end_time,
                                  pricing.effective_list.default AS unit_usd
                           FROM system.billing.list_prices)
            SELECT u.sku_name, round(sum(u.usage_quantity * pr.unit_usd), 0) AS usd
            FROM system.billing.usage u
            LEFT JOIN price pr ON u.sku_name = pr.sku_name AND u.cloud = pr.cloud
              AND u.usage_start_time >= pr.price_start_time
              AND (pr.price_end_time IS NULL OR u.usage_start_time < pr.price_end_time)
            WHERE u.usage_date >= current_date() - INTERVAL 30 DAYS
            GROUP BY 1 ORDER BY usd DESC NULLS LAST LIMIT 1
        """, client)
        kpis["top_sku"] = {"name": top["rows"][0][0], "usd": top["rows"][0][1]} if top["rows"] else None
    except Exception as e:
        logger.warning(f"kpi top_sku failed: {e}")
        kpis["top_sku"] = None

    # Total unallocated (idle) warehouse $ over 14d — the M1b headline, summed.
    try:
        kpis["idle_14d"] = _run_scalar("""
            WITH wh_hour AS (
              SELECT warehouse_id, hour, sum(usage_quantity * unit_usd) AS hour_usd FROM (
                SELECT u.usage_metadata.warehouse_id AS warehouse_id,
                       date_trunc('HOUR', u.usage_start_time) AS hour,
                       u.usage_quantity, lp.pricing.effective_list.default AS unit_usd
                FROM system.billing.usage u JOIN system.billing.list_prices lp
                  ON u.sku_name=lp.sku_name AND u.cloud=lp.cloud
                 AND u.usage_start_time>=lp.price_start_time AND (lp.price_end_time IS NULL OR u.usage_start_time<lp.price_end_time)
                WHERE u.usage_metadata.warehouse_id IS NOT NULL AND u.usage_date >= current_date() - INTERVAL 14 DAYS
                QUALIFY ROW_NUMBER() OVER (PARTITION BY u.record_id ORDER BY lp.price_start_time DESC) = 1
              ) GROUP BY 1,2),
            cfg_hist AS (SELECT warehouse_id, max_clusters, change_time AS valid_from,
                           coalesce(lead(change_time) OVER (PARTITION BY warehouse_id ORDER BY change_time), TIMESTAMP '9999-12-31') AS valid_to
                         FROM system.compute.warehouses),
            busy AS (SELECT compute.warehouse_id AS warehouse_id, date_trunc('HOUR',start_time) AS hour,
                            sum(execution_duration_ms) AS busy_ms
                     FROM system.query.history
                     WHERE start_time >= current_date() - INTERVAL 14 DAYS AND execution_duration_ms>0 GROUP BY 1,2),
            hour_alloc AS (SELECT w.warehouse_id, w.hour, w.hour_usd, coalesce(b.busy_ms,0) AS busy_ms,
                             GREATEST(coalesce(c.max_clusters,1)*3600000.0, coalesce(b.busy_ms,0)) AS capacity_ms
                           FROM wh_hour w LEFT JOIN cfg_hist c ON c.warehouse_id=w.warehouse_id
                             AND w.hour>=c.valid_from AND w.hour<c.valid_to
                           LEFT JOIN busy b USING (warehouse_id, hour))
            SELECT round(sum(hour_usd * (1 - busy_ms / capacity_ms)), 0) FROM hour_alloc
        """, client)
    except Exception as e:
        logger.warning(f"kpi idle_14d failed: {e}")
        kpis["idle_14d"] = None

    kpis["hunt_count"] = len(catalogue.HUNTS)
    return kpis


# ── API: run one hunt ────────────────────────────────────────────────────────

@app.get("/api/hunt/{hunt_id}")
def run_hunt(hunt_id: str, request: Request):
    h = next((x for x in catalogue.HUNTS if x["id"] == hunt_id), None)
    if not h:
        raise HTTPException(404, f"unknown hunt {hunt_id}")
    try:
        out = _run_sql(h["sql"], _client_for(request))
        return {"id": hunt_id, "columns": out["columns"], "rows": out["rows"],
                "total_rows": out["total_rows"], "truncated": out["truncated"]}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"hunt {hunt_id} failed: {e}")
        raise HTTPException(500, f"hunt failed: {e}")


# ── API: Observability boards (Usage / Jobs / DBSQL / Model Serving) ─────────
#
# One generic endpoint serves every board panel. It runs the panel's SQL and
# then runs a DETERMINISTIC anomaly detector over the returned rows (boards.py):
# the callouts it returns are what render above the chart. The rule detects; the
# optional AI pass only narrates the rule's finding — never invents a number.

@app.get("/api/board/{board_id}/{panel_id}")
def run_panel(board_id: str, panel_id: str, request: Request):
    b, p = boards_content.find_panel(board_id, panel_id)
    if not p:
        raise HTTPException(404, f"unknown panel {board_id}/{panel_id}")
    try:
        # Charts need every row to build the series, so this caller is unbounded
        # (like the Prove measures). Panels are pre-aggregated in SQL (weeks,
        # top-N), so the row counts are small.
        out = _run_sql(p["sql"], _client_for(request), max_rows=None)
        anomalies = boards_content.compute_anomalies(p, out["columns"], out["rows"])
        return {"board": board_id, "panel": panel_id, "chart": p["chart"],
                "columns": out["columns"], "rows": out["rows"],
                "total_rows": out["total_rows"], "anomalies": anomalies}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"panel {board_id}/{panel_id} failed: {e}")
        raise HTTPException(500, f"panel failed: {e}")


@app.get("/api/board/{board_id}/{panel_id}/narrate")
def narrate_panel(board_id: str, panel_id: str, request: Request, model: Optional[str] = None):
    """Optional plain-English narration of a panel's detected anomalies. The
    deterministic detector has already found and worded the anomaly; this asks
    the model to summarise what to DO about it in one or two sentences. If there
    are no anomalies it says so — it never manufactures a concern."""
    b, p = boards_content.find_panel(board_id, panel_id)
    if not p:
        raise HTTPException(404, f"unknown panel {board_id}/{panel_id}")
    client = _client_for(request)
    try:
        out = _run_sql(p["sql"], client, max_rows=None)
        anomalies = boards_content.compute_anomalies(p, out["columns"], out["rows"])
        if not anomalies:
            return {"board": board_id, "panel": panel_id, "anomalies": [],
                    "narration": "No anomalies detected in this view for the current window."}
        # Feed the detector's own callouts (facts) to the model; it only advises.
        import json as _json
        facts = _json.dumps(anomalies)[:4000]
        instruction = (
            "You are a Databricks FinOps analyst. A deterministic detector found these "
            "cost anomalies in the '" + p["title"] + "' view (facts, already quantified): "
            + facts + ". In 2-3 neutral sentences, explain what likely causes this and the "
            "first thing a FinOps admin should check. Do NOT invent new numbers; only use the "
            "figures given. Be concrete and calm, not alarmist."
        )
        esc = instruction.replace("\\", "\\\\").replace("'", "''")
        wrapped = f"SELECT ai_query('{_explainer_model(model)}', '{esc}') AS narration"
        text = _run_scalar(wrapped, client) or "(no narration returned)"
        return {"board": board_id, "panel": panel_id, "anomalies": anomalies,
                "narration": text, "model": _explainer_model(model)}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"narrate {board_id}/{panel_id} failed: {e}")
        raise HTTPException(500, f"narrate failed: {e}")


# ── API: Ask (Genie One) — natural-language cost Q&A ─────────────────────────
#
# Genie One is a Unity AI Gateway MCP service, so there is no space to create.
# Every call runs on-behalf-of-user. Ask returns at once with status in_progress
# and the UI polls; no route ever blocks on Genie.

def _genie(request: Request, tool: str, args: dict) -> dict:
    """Call a Genie One tool as the user, turning failures into clean JSON errors."""
    client = _client_for(request)
    try:
        if tool == "genie_poll_response":
            return genie_cost.poll_response(client, **args)
        return genie_cost.call_tool(client, tool, args)
    except genie_cost.GenieError as e:
        raise HTTPException(e.status, str(e))
    except (PermissionDenied, Unauthenticated) as e:
        logger.warning(f"genie {tool} denied: {e}")
        raise HTTPException(403, "Genie One refused this user. The app needs the ai-gateway "
                                 "scope (re-consent after a redeploy) and the user needs EXECUTE on "
                                 "system.ai.genie_one_mcp.")
    except NotFound as e:
        logger.warning(f"genie {tool} not found: {e}")
        raise HTTPException(404, "Genie One isn't available in this workspace. It needs Unity AI Gateway.")
    except Exception as e:  # noqa: BLE001
        logger.error(f"genie {tool} failed: {e}")
        raise HTTPException(502, f"Genie One call failed: {str(e).split(' [ReqId')[0][:300]}")


@app.get("/api/genie/status")
def genie_status():
    """Genie One needs no setup, so the Ask tab is always ready."""
    return {"status": "ready", "sample_questions": genie_cost.SAMPLE_QUESTIONS}


class GenieAskBody(BaseModel):
    question: str
    conversation_id: Optional[str] = None


@app.post("/api/genie/ask")
def genie_ask(body: GenieAskBody, request: Request) -> genie_cost.GenieTurn:
    """Send a question (grounded on the cost tables) and return the in-progress
    turn at once. Pass conversation_id to ask a follow-up."""
    q = (body.question or "").strip()
    if not q:
        raise HTTPException(400, "Empty question.")
    args = {"question": genie_cost.grounded_question(q)}
    if body.conversation_id:
        args["conversation_id"] = body.conversation_id
    return genie_cost.to_turn(_genie(request, "genie_ask", args))


@app.get("/api/genie/poll")
def genie_poll(request: Request, conversation_id: str, response_id: str) -> genie_cost.GenieTurn:
    """Current state of one answer. The UI calls this until status != in_progress."""
    return genie_cost.to_turn(_genie(request, "genie_poll_response",
                                     {"conversation_id": conversation_id, "response_id": response_id}))


@app.get("/api/genie/result")
def genie_result(request: Request, conversation_id: str, response_id: str, item_id: str):
    """The result rows behind one of the SQL queries Genie ran."""
    res = _genie(request, "genie_get_query_result", {"conversation_id": conversation_id,
                                                     "response_id": response_id, "item_id": item_id})
    cols = [c.get("name") if isinstance(c, dict) else c for c in res.get("columns") or []]
    rows = (res.get("rows") or [])[:200]
    return {"columns": cols, "rows": rows, "total_rows": res.get("total_row_count", len(rows))}


# ── API: AI explainer for a hunt ─────────────────────────────────────────────

@app.get("/api/explain/{hunt_id}")
def explain_hunt(hunt_id: str, request: Request, model: Optional[str] = None):
    h = next((x for x in catalogue.HUNTS if x["id"] == hunt_id), None)
    if not h:
        raise HTTPException(404, f"unknown hunt {hunt_id}")
    if not (h.get("explain_rubric") or h.get("explain_is_trend")):
        raise HTTPException(400, f"hunt {hunt_id} has no explainer")
    client = _client_for(request)
    try:
        if h.get("explain_is_trend"):
            # trend explainer uses its purpose-built weekly roll-up (bounded in the SQL)
            text = _ai_explain_trend(h["explain_rubric"], h.get("explain_sql", h["sql"]), client, model)
        else:
            # explainer runs over the top-10 of the hunt query — use an explicit
            # explain_sql if provided, else bound the hunt SQL to its top 10 rows
            rows_sql = h.get("explain_sql") or _bounded(h["sql"], 10)
            text = _ai_explain(h["explain_rubric"], h["explain_hunt_desc"], rows_sql, client, model)
        return {"id": hunt_id, "explanation": text, "model": _explainer_model(model)}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"explain {hunt_id} failed: {e}")
        raise HTTPException(500, f"explain failed: {e}")


# ── API: run one measure ─────────────────────────────────────────────────────

@app.get("/api/measure/{measure_id}")
def run_measure(measure_id: str, request: Request):
    m = next((x for x in catalogue.MEASURES if x["id"] == measure_id), None)
    if not m:
        raise HTTPException(404, f"unknown measure {measure_id}")
    try:
        # Chart measures need every row to build the series; table measures only
        # show the top slice, so cap those the same way as hunts.
        is_chart = m.get("chart", "table") != "table"
        out = _run_sql(m["sql"], _client_for(request), max_rows=None if is_chart else DEFAULT_MAX_ROWS)
        return {"id": measure_id, "columns": out["columns"], "rows": out["rows"],
                "total_rows": out["total_rows"], "truncated": out["truncated"],
                "chart": m.get("chart", "table")}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"measure {measure_id} failed: {e}")
        raise HTTPException(500, f"measure failed: {e}")


# ── API: Full cost scan → alert queue (the FinOps workflow) ──────────────────

@app.post("/api/scan/start")
def scan_start(request: Request, model: Optional[str] = None):
    """Kick off a full cost scan on a background thread and return a scan_id
    immediately. Every alerting hunt runs, each result becomes a pre-triaged
    finding with a severity, an AI explanation, the fix, and the before/after
    measure — streamed into the queue as the scan progresses. `model` optionally
    overrides the explainer endpoint (the UI model picker).

    We capture the request's OBO client HERE (on the request thread) and hand it
    to the worker, so the background queries still run as the logged-in user."""
    client = _client_for(request)
    who = request.headers.get("x-forwarded-email") or request.headers.get("x-forwarded-user") or ""
    # explainer bound to the chosen model (falls back to default inside _ai_explain)
    def _explain_bound(rubric, desc, rows_sql, cli):
        return _ai_explain(rubric, desc, rows_sql, cli, model)
    scan_id, created = scan_engine.STORE.start_scan({
        "status": "starting", "total": 0, "done": 0, "clean": 0,
        "findings": [], "started_by": who,
    })
    if not created:
        return {"scan_id": scan_id, "status": "running"}

    def _persist(sid: str, cli: WorkspaceClient) -> Optional[str]:
        """Called on the scan thread when it finishes: compute self-cost, then
        write the run + findings to the shared Delta store (best-effort).
        Returns None once saved, else why it wasn't."""
        view = scan_engine.scan_view(sid)
        if not view:
            return "scan not found"
        try:
            cost = _compute_scan_cost(view, cli)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"persist: cost compute failed: {e}")
            cost = None
        return None if DELTA.save_scan(cli, view, cost) else DELTA.last_error

    t = threading.Thread(
        target=scan_engine.run_scan,
        args=(scan_id, client, _run_sql, _explain_bound, _bounded, logger),
        kwargs={"on_complete": _persist},
        daemon=True,
    )
    t.start()
    return {"scan_id": scan_id, "status": "starting"}


@app.get("/api/scan/latest")
def scan_latest(request: Request):
    """The latest persisted scan from the shared Delta store, WITH run-over-run
    comparison against the prior run. This is what the Alerts tab loads on open, so
    every user sees the same durable view (survives restarts). Returns
    {available: false} if nothing is persisted yet (or the store isn't writable)."""
    client = _client_for(request)
    ids = DELTA.latest_two_run_ids(client)
    if not ids:
        return {"available": False, "store_error": DELTA.last_error}
    current = DELTA.load_run(client, ids[0])
    if not current:
        return {"available": False, "store_error": DELTA.last_error}
    previous = DELTA.load_run(client, ids[1]) if len(ids) > 1 else None
    # merge shared triage + sort + compare
    triage = DELTA.all_triage(client)
    for f in current["findings"]:
        f["triage"] = triage.get(f["hunt_id"], {"status": "new", "note": "", "updated_by": ""})
    current["findings"].sort(key=lambda f: (scan_engine.SEV_RANK.get(f["severity"], 9),
                                            -(f["impact_usd"] or 0)))
    store_delta.compare_runs(current, previous)
    current["available"] = True
    current["status"] = "complete"
    return current


@app.get("/api/scan/{scan_id}")
def scan_status(scan_id: str):
    """Poll a scan: progress (done/total/clean), severity counts, and the
    findings collected so far (sorted worst-first). The frontend calls this on
    an interval and renders the queue as it fills."""
    view = scan_engine.scan_view(scan_id)
    if not view:
        raise HTTPException(404, f"unknown scan {scan_id}")
    return view


@app.get("/api/scan/{scan_id}/cost")
def scan_cost(scan_id: str, request: Request):
    """What did THIS scan cost to run? Answers the catch-22 head-on: a tool that
    hunts for waste is itself a cost, so we attribute it honestly, in two parts —
    (1) DBSQL compute for the scan's own queries, via the M1 duration-weighted
    method, and (2) the ai_query() explanations, priced from the account's own
    effective token rate. All list-price, read-only, and tiny — the point is to
    reassure a FinOps admin that clicking Re-run costs cents and changes nothing."""
    view = scan_engine.scan_view(scan_id)
    if not view:
        raise HTTPException(404, f"unknown scan {scan_id}")
    if not view.get("started_at"):
        raise HTTPException(400, "scan has not started")
    return _compute_scan_cost(view, _client_for(request))


def _compute_scan_cost(view: dict, client: WorkspaceClient) -> dict[str, Any]:
    """Attribute what a scan cost to run (DBSQL wall-time estimate + ai_query
    published-rate estimate). Shared by the /cost endpoint and the Delta persist."""
    scan_id = view["scan_id"]
    started, ended = view.get("started_at"), view.get("ended_at")
    end_expr = f"TIMESTAMP '{ended}'" if ended else "now()"
    wall_ms = float(view.get("wall_ms", 0.0))
    n_explains = view.get("n_explains", 0)

    out: dict[str, Any] = {"scan_id": scan_id, "n_explains": n_explains,
                           "scan_seconds": round(wall_ms / 1000.0, 1)}

    # ── (1) DBSQL compute — INSTANT estimate that never waits on billing tables.
    #   The scan runs its queries sequentially on ONE warehouse, so it holds that
    #   warehouse busy for ~its execution wall-time. We price that at the
    #   warehouse's current list $/hour (its recent DBU/hr × $/DBU from billing —
    #   which lands fast). This is a conservative UPPER bound (the warehouse may
    #   have been running anyway), which is the right direction for reassurance.
    #   If billing has caught up we ALSO return the precise M1-attributed figure. ──
    try:
        rate = _run_sql(f"""
          WITH price AS (SELECT sku_name, cloud, price_start_time, price_end_time,
                                pricing.effective_list.default AS unit_usd
                         FROM system.billing.list_prices)
          SELECT round(sum(u.usage_quantity * pr.unit_usd)
                       / (sum(timestampdiff(SECOND, u.usage_start_time, u.usage_end_time))/3600.0), 4) AS usd_per_hour
          FROM system.billing.usage u
          LEFT JOIN price pr ON u.sku_name=pr.sku_name AND u.cloud=pr.cloud
            AND u.usage_start_time>=pr.price_start_time AND (pr.price_end_time IS NULL OR u.usage_start_time<pr.price_end_time)
          WHERE u.usage_metadata.warehouse_id = '{WAREHOUSE_ID}'
            AND u.usage_date >= current_date() - 7
        """, client)
        usd_per_hr = scan_engine._num(rate["rows"][0][0]) if rate["rows"] else None
        est_usd = round((wall_ms / 3_600_000.0) * usd_per_hr, 4) if usd_per_hr else None
        out["dbsql"] = {"basis": "estimate", "scan_seconds": round(wall_ms/1000.0, 1),
                        "warehouse_usd_per_hour": usd_per_hr, "usd": est_usd}
    except Exception as e:
        logger.warning(f"scan cost dbsql estimate failed: {e}")
        out["dbsql"] = {"error": str(e)[:200]}

    # Bonus: precise M1-attributed DBSQL $ IF query.history has caught up (lags
    # a few minutes). Silently omitted while pending — the estimate above stands.
    try:
        m1 = _run_sql(f"""
        WITH wh_hour AS (
          SELECT warehouse_id, hour, sum(usage_quantity*unit_usd) AS hour_usd FROM (
            SELECT u.usage_metadata.warehouse_id AS warehouse_id, date_trunc('HOUR',u.usage_start_time) AS hour,
                   u.usage_quantity, lp.pricing.effective_list.default AS unit_usd
            FROM system.billing.usage u JOIN system.billing.list_prices lp
              ON u.sku_name=lp.sku_name AND u.cloud=lp.cloud
             AND u.usage_start_time>=lp.price_start_time AND (lp.price_end_time IS NULL OR u.usage_start_time<lp.price_end_time)
            WHERE u.usage_metadata.warehouse_id='{WAREHOUSE_ID}' AND u.usage_date >= current_date()-1
            QUALIFY ROW_NUMBER() OVER (PARTITION BY u.record_id ORDER BY lp.price_start_time DESC)=1
          ) GROUP BY 1,2),
        cfg AS (SELECT warehouse_id, max_clusters, change_time AS vf,
                  coalesce(lead(change_time) OVER (PARTITION BY warehouse_id ORDER BY change_time), TIMESTAMP '9999-12-31') AS vt
                FROM system.compute.warehouses),
        q AS (SELECT compute.warehouse_id AS warehouse_id, date_trunc('HOUR',start_time) AS hour, execution_duration_ms,
                     sum(execution_duration_ms) OVER (PARTITION BY compute.warehouse_id, date_trunc('HOUR',start_time)) AS busy_ms
              FROM system.query.history
              WHERE compute.warehouse_id='{WAREHOUSE_ID}' AND executed_by=current_user()
                AND start_time >= TIMESTAMP '{started}' AND start_time < {end_expr} AND execution_duration_ms>0),
        alloc AS (SELECT q.*, w.hour_usd, GREATEST(coalesce(c.max_clusters,1)*3600000.0, q.busy_ms) AS capacity_ms
                  FROM q JOIN wh_hour w USING (warehouse_id, hour)
                  LEFT JOIN cfg c ON c.warehouse_id=q.warehouse_id AND q.hour>=c.vf AND q.hour<c.vt)
        SELECT count(*) AS scan_queries, round(sum(hour_usd*execution_duration_ms/capacity_ms),4) AS usd FROM alloc
        """, client)
        if m1["rows"] and scan_engine._num(m1["rows"][0][0]) and int(m1["rows"][0][0]) > 0:
            out["dbsql_precise"] = {"basis": "M1 (query.history)", "scan_queries": m1["rows"][0][0],
                                    "usd": scan_engine._num(m1["rows"][0][1])}
    except Exception as e:
        logger.warning(f"scan cost M1 precise failed: {e}")

    # ── (2) ai_query() explanations. INSTANT estimate: n_explains × a small
    #   assumed token profile, priced at this endpoint's rate. Refined with real
    #   tokens from ai_gateway.usage when it has caught up. ──
    # Price the explainer tokens against the PUBLISHED rate card (the honest,
    # workspace-independent way — deriving a $/token from this shared demo
    # workspace's messy billing↔gateway ratio produces nonsense unit rates).
    # We use a conservative proprietary anchor: $0.070/DBU × published DBU/1M,
    # input & output separately. AI_ENDPOINT_DBU below is the explainer model's
    # published rate; default to a small-model rate and cap the estimate as "≤".
    try:
        scan_mix = _run_sql(f"""
          SELECT coalesce(sum(input_tokens),0) AS in_s, coalesce(sum(output_tokens),0) AS out_s,
                 count(*) AS calls
          FROM system.ai_gateway.usage
          WHERE endpoint_name='{AI_ENDPOINT}' AND invocation_metadata.source='AI_QUERY'
            AND event_time >= TIMESTAMP '{started}' AND event_time < {end_expr}
        """, client)
        r = scan_mix["rows"][0] if scan_mix["rows"] else [0, 0, 0]
        in_s = int(scan_engine._num(r[0]) or 0); out_s = int(scan_engine._num(r[1]) or 0)
        measured = (in_s + out_s) > 0
        # published rate for the explainer endpoint (DBU/1M in, out) × $/DBU.
        dbu_in, dbu_out = AI_ENDPOINT_DBU
        usd_dbu = 0.070
        if measured:
            ai_usd = round((in_s/1e6*dbu_in + out_s/1e6*dbu_out) * usd_dbu, 4)
            out["ai_query"] = {"basis": "measured", "ai_calls": n_explains,
                               "tokens": in_s + out_s, "usd": ai_usd}
        else:  # pre-gateway estimate: input-heavy profile per explainer (~1200 in / 120 out)
            e_in, e_out = n_explains * 1200, n_explains * 120
            ai_usd = round((e_in/1e6*dbu_in + e_out/1e6*dbu_out) * usd_dbu, 4)
            out["ai_query"] = {"basis": "estimate", "ai_calls": n_explains, "tokens": e_in + e_out, "usd": ai_usd}
    except Exception as e:
        logger.warning(f"scan cost ai_query failed: {e}")
        out["ai_query"] = {"error": str(e)[:200]}

    parts = [out.get("dbsql", {}).get("usd"), out.get("ai_query", {}).get("usd")]
    out["total_usd"] = round(sum(p for p in parts if isinstance(p, (int, float))), 4)
    return out


TRIAGE_NOTE_MAX = 2000


class TriageUpdate(BaseModel):
    status: str
    note: str = ""


@app.post("/api/finding/{hunt_id}/triage")
def finding_triage(hunt_id: str, body: TriageUpdate, request: Request):
    """Move a finding through the workflow: new → acknowledged → in_progress →
    resolved / dismissed. Keyed by hunt id so the decision survives a re-scan."""
    if not any(h["id"] == hunt_id for h in catalogue.HUNTS):
        raise HTTPException(404, f"unknown hunt {hunt_id}")
    if body.status not in scan_engine.TRIAGE_STATES:
        raise HTTPException(400, f"status must be one of {scan_engine.TRIAGE_STATES}")
    if len(body.note) > TRIAGE_NOTE_MAX:
        raise HTTPException(400, f"note is longer than {TRIAGE_NOTE_MAX} characters")
    who = request.headers.get("x-forwarded-email") or request.headers.get("x-forwarded-user") or ""
    client = _client_for(request)
    rec = scan_engine.STORE.set_triage(hunt_id, body.status, body.note, who)  # in-memory (fast path)
    persisted = DELTA.set_triage(client, hunt_id, body.status, body.note, who)  # shared/durable
    return {"hunt_id": hunt_id, **rec, "persisted": persisted,
            "store_error": None if persisted else DELTA.last_error}


# ── API: Tokenomics live SQL (this workspace's prices + actual spend) ────────

def _workspace_prices(client: WorkspaceClient) -> pricing.Prices:
    """What each model's SKU costs per DBU on this workspace, from its own billing."""
    global WORKSPACE_ID
    if not WORKSPACE_ID:
        try:
            WORKSPACE_ID = str(sp_client.get_workspace_id())
        except DatabricksError as e:
            raise HTTPException(502, f"Couldn't read this workspace's id: {_sdk_message(e)}")
    out = _run_sql(pricing.PRICES_SQL, client, max_rows=None,
                   params=[{"name": "workspace_id", "value": WORKSPACE_ID}])
    return pricing.workspace_prices(out["rows"])


@app.get("/api/token/prices")
def token_prices(request: Request):
    """The cloud, tier and region this workspace bills in, and the $ per DBU of each model SKU,
    for the Tokenomics calculator and rate card."""
    prices = _workspace_prices(_client_for(request))
    return {"workspace_id": WORKSPACE_ID, **pricing.as_json(prices)}


@app.get("/api/token/actuals")
def token_actuals(request: Request):
    """Actual 30-day model-serving spend per endpoint — ties the estimate back to
    reality (the estimator answers 'what will it cost'; this shows 'what it did')."""
    sql = """
        WITH price AS (
          SELECT sku_name, cloud, price_start_time, price_end_time,
                 pricing.effective_list.default AS unit_usd
          FROM system.billing.list_prices
        )
        SELECT coalesce(u.usage_metadata.endpoint_name, '(endpoint not attributed)') AS endpoint,
               sum(u.usage_quantity)                          AS dbus,
               round(sum(u.usage_quantity * pr.unit_usd), 2)  AS list_usd
        FROM system.billing.usage u
        LEFT JOIN price pr ON u.sku_name = pr.sku_name AND u.cloud = pr.cloud
          AND u.usage_start_time >= pr.price_start_time
          AND (pr.price_end_time IS NULL OR u.usage_start_time < pr.price_end_time)
        WHERE u.billing_origin_product = 'MODEL_SERVING'
          AND u.usage_type = 'TOKEN'
          AND u.usage_date >= current_date() - INTERVAL 30 DAYS
        GROUP BY 1
        ORDER BY list_usd DESC NULLS LAST
    """
    try:
        out = _run_sql(sql, _client_for(request))
        return {"columns": out["columns"], "rows": out["rows"], "total_rows": out["total_rows"]}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"token actuals failed: {e}")
        raise HTTPException(500, f"token actuals failed: {e}")


# ── API: Tagging — find untagged resources (read-only) + set a tag (write) ────

@app.get("/api/tagging/{what}")
def tagging_find(what: str, request: Request):
    """Read-only: surface tagging gaps. `what` ∈ {coverage, spend, warehouses, clusters}.
    Cost attribution depends on tags, so this shows what carries none."""
    sql_map = {
        "coverage": tagging_content.TAG_COVERAGE,
        "spend": tagging_content.UNTAGGED_SPEND_BY_PRODUCT,
        "warehouses": tagging_content.UNTAGGED_WAREHOUSES,
        "clusters": tagging_content.UNTAGGED_CLUSTERS,
    }
    if what not in sql_map:
        raise HTTPException(404, f"unknown tagging view {what}")
    try:
        out = _run_sql(sql_map[what], _client_for(request))
        return {"view": what, "columns": out["columns"], "rows": out["rows"],
                "total_rows": out["total_rows"]}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"tagging {what} failed: {e}")
        raise HTTPException(500, f"tagging {what} failed: {e}")


# Ids are interpolated into SDK URL paths, so each must match its type's real format.
TAG_ID_FORMAT = {
    "warehouse": re.compile(r"[0-9a-f]{16}"),
    "cluster": re.compile(r"[0-9]{4}-[0-9]{6}-[a-z0-9]{5,12}"),
}


class TagSet(BaseModel):
    resource_type: str   # 'warehouse' | 'cluster'
    resource_id: str
    key: str
    value: str


@app.post("/api/tagging/set")
def tagging_set(body: TagSet, request: Request):
    """WRITE: set one tag on a warehouse or cluster. This is the ONE place the app
    changes workspace state — it's explicit, per-resource, and confirmed in the UI.
    Uses the SDK edit APIs as the logged-in user, so it's gated by that user's own
    permissions (a viewer will get a clean permission error, not a silent no-op).
    Merges the new tag with existing tags rather than replacing them."""
    client = _client_for(request)
    key = (body.key or "").strip()
    val = (body.value or "").strip()
    if not key or not val:
        raise HTTPException(400, "tag key and value are required")
    # basic hygiene on the tag key/value
    if not re.fullmatch(r"[A-Za-z0-9_\-. ]{1,64}", key) or len(val) > 256:
        raise HTTPException(400, "invalid tag key or value")
    id_format = TAG_ID_FORMAT.get(body.resource_type)
    if not id_format:
        raise HTTPException(400, "resource_type must be 'warehouse' or 'cluster'")
    if not id_format.fullmatch(body.resource_id):
        raise HTTPException(400, f"'{body.resource_id[:40]}' is not a valid {body.resource_type} id")
    try:
        if body.resource_type == "warehouse":
            from databricks.sdk.service.sql import EndpointTags, EndpointTagPair
            cur = client.warehouses.get(body.resource_id)
            pairs = list(cur.tags.custom_tags) if (cur.tags and cur.tags.custom_tags) else []
            pairs = [p for p in pairs if p.key != key]  # replace same-key
            pairs.append(EndpointTagPair(key=key, value=val))
            client.warehouses.edit(id=body.resource_id, tags=EndpointTags(custom_tags=pairs))
            return {"ok": True, "resource_type": "warehouse", "resource_id": body.resource_id,
                    "set": {key: val}, "note": "Tag set. It appears in system tables on the next sync."}
        else:
            cur = client.clusters.get(body.resource_id)
            tags = dict(cur.custom_tags or {})
            tags[key] = val
            # clusters.edit needs the FULL core spec echoed back, and the sizing field
            # must match the cluster's mode: autoscale clusters use `autoscale`, fixed
            # clusters use `num_workers`. Passing the wrong one errors — the likely
            # cause of the "warehouses work, clusters don't" report. Echo whichever it has.
            kwargs = dict(cluster_id=body.resource_id, cluster_name=cur.cluster_name,
                          spark_version=cur.spark_version, node_type_id=cur.node_type_id,
                          custom_tags=tags)
            if cur.driver_node_type_id: kwargs["driver_node_type_id"] = cur.driver_node_type_id
            if cur.autoscale is not None:
                kwargs["autoscale"] = cur.autoscale
            else:
                kwargs["num_workers"] = cur.num_workers or 0
            client.clusters.edit(**kwargs)
            return {"ok": True, "resource_type": "cluster", "resource_id": body.resource_id,
                    "set": {key: val}, "note": "Tag set (takes effect on the cluster's next start)."}
    except NotFound:
        raise HTTPException(404, f"{body.resource_type} {body.resource_id} does not exist")
    except Exception as e:
        msg = _sdk_message(e)
        logger.error(f"tagging set failed ({body.resource_type} {body.resource_id}): {msg}")
        # translate the common failures into a plain-English reason for the UI
        low = msg.lower()
        if "permission" in low or "does not have" in low or "forbidden" in low or "403" in low:
            hint = "You don't have edit permission on this resource — ask its owner or a workspace admin."
        elif "terminated" in low or "state" in low or "not editable" in low:
            hint = "The resource must be in an editable state (a terminated cluster can be tagged from its config page)."
        else:
            hint = msg[:180]
        raise HTTPException(500, f"Couldn't set tag: {hint}")


# ── Static files & SPA fallback ──────────────────────────────────────────────

static_dir = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")


@app.get("/")
def root():
    return FileResponse(str(static_dir / "index.html"))


@app.get("/{full_path:path}")
def serve_spa(full_path: str):
    if full_path.startswith("api/"):
        raise HTTPException(404)
    return FileResponse(str(static_dir / "index.html"))
