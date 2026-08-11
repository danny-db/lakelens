"""
genie_cost.py — the "Ask" tab: an embedded Genie space for natural-language
cost questions.

Why a space (not "Genie One"): the Conversations API is namespaced under a
space_id — there is NO space-less REST endpoint (Genie One is MCP-only, Beta).
And this app runs in the CUSTOMER's workspace, so we can't hardcode a space_id.
So: a one-time "Create cost Genie space" button provisions a space seeded with
the cost system tables, persists the returned space_id to a small Delta config
table, and all subsequent chat routes through the Conversations API. The
space_id is discovered at runtime, never hardcoded — portable across workspaces.

Everything runs on-behalf-of-user (OBO): the space is created and queried as the
logged-in user, so Genie only ever sees tables that user can already read.
"""
import json
from typing import Optional, Any, Callable

# The cost system tables to seed the space with, each with a short description so
# Genie knows what it is. Kept to the tables the rest of LakeLens already uses.
SEED_TABLES = [
    ("system.billing.usage",
     "Per-record billing usage. Key cols: usage_date, sku_name, usage_quantity (DBUs), "
     "billing_origin_product, usage_metadata (job_id/warehouse_id/dlt_pipeline_id/endpoint_name), "
     "custom_tags, workspace_id, product_features."),
    ("system.billing.list_prices",
     "List price per SKU over time. Join usage to this on sku_name+cloud within "
     "price_start_time/price_end_time; dollars = usage_quantity * pricing.effective_list.default."),
    ("system.compute.warehouses",
     "SQL warehouse config history (size, auto_stop_minutes, min/max clusters). Latest row per "
     "warehouse_id by change_time."),
    ("system.compute.clusters",
     "Cluster config history: cluster_source (UI/API=all-purpose, JOB=jobs compute), dbr_version, "
     "worker_count, min/max_autoscale_workers, worker_node_type."),
    ("system.query.history",
     "Per-statement SQL history: executed_by, statement_type, execution_duration_ms, read_bytes, "
     "spilled_local_bytes, compute.warehouse_id, query_source (job/dashboard/notebook/genie ids)."),
    ("system.lakeflow.job_run_timeline",
     "Job run outcomes over time: result_state (SUCCEEDED/ERROR/FAILED/TIMED_OUT/CANCELLED), "
     "period_start_time/period_end_time, job_id, workspace_id."),
]

SAMPLE_QUESTIONS = [
    "What did we spend by product over the last 30 days?",
    "Which warehouse cost the most last week?",
    "Show daily model serving spend for the last 2 weeks",
    "Which jobs failed the most in the last 30 days?",
    "What is our total DBU cost this month vs last month?",
]

INSTRUCTIONS = (
    "You answer Databricks cost/FinOps questions from the system tables. Dollars are LIST-PRICE: "
    "compute them by joining system.billing.usage to system.billing.list_prices on sku_name and cloud "
    "where usage_start_time is between price_start_time and coalesce(price_end_time, now()), then "
    "sum(usage_quantity * pricing.effective_list.default). These are pre-discount, pre-cloud-infra list "
    "costs — say so. Prefer recent windows (last 7/30 days) unless asked otherwise. When you show a cost, "
    "state the time window."
)


def build_serialized_space() -> str:
    """The serialized_space JSON create_space expects — seed tables + instructions + samples."""
    return json.dumps({
        "version": 1,
        # ids must be 32-hex UUIDs without hyphens (API rejects 'q0'/'i1'); tables sorted by identifier.
        "config": {"sample_questions": [{"id": _hexid(f"sample:{q}"), "question": [q]}
                                        for q in SAMPLE_QUESTIONS]},
        "data_sources": {"tables": [{"identifier": ident, "description": [desc]}
                                    for ident, desc in sorted(SEED_TABLES, key=lambda t: t[0])]},
        "instructions": {"text_instructions": [{"id": _hexid("instr:cost"), "content": [INSTRUCTIONS]}]},
    })


def _hexid(seed: str) -> str:
    """Stable lowercase 32-hex id (no hyphens) — required by the Genie export proto."""
    import hashlib
    return hashlib.md5(seed.encode()).hexdigest()


# ── space-id persistence (tiny Delta config table, reuse the app's sql adapters) ──

class GenieConfig:
    """Persists the created space_id so the button is one-time (per workspace).
    Uses the same catalog.schema as scan results; a 1-col config table keyed by name."""

    def __init__(self, catalog: str, schema: str, run_sql: Callable, run_write: Callable, logger):
        self.fq = f"{catalog}.{schema}"
        self.tbl = f"{self.fq}.app_config"
        self._run_sql = run_sql
        self._run_write = run_write
        self.log = logger

    def _ensure(self, client) -> bool:
        if not self.fq.split(".")[0]:   # no RESULTS_CATALOG → can't persist the space id
            return False
        try:
            self._run_write(f"CREATE SCHEMA IF NOT EXISTS {self.fq}", client)
            self._run_write(f"CREATE TABLE IF NOT EXISTS {self.tbl} "
                            f"(key STRING, value STRING, updated_by STRING, updated_at TIMESTAMP)", client)
            return True
        except Exception as e:  # noqa: BLE001
            self.log.warning(f"GenieConfig.ensure failed: {e}")
            return False

    def get_space_id(self, client) -> Optional[str]:
        if not self._ensure(client):
            return None
        try:
            out = self._run_sql(
                f"SELECT value FROM {self.tbl} WHERE key='genie_space_id' "
                f"ORDER BY updated_at DESC LIMIT 1", client)
            rows = out.get("rows") or []
            return rows[0][0] if rows and rows[0] and rows[0][0] else None
        except Exception as e:  # noqa: BLE001
            self.log.warning(f"GenieConfig.get failed: {e}")
            return None

    def set_space_id(self, client, space_id: str, who: str) -> None:
        if not self._ensure(client):
            return
        from databricks.sdk.service.sql import StatementParameterListItem  # noqa
        # parameterised insert (space_id/who are safe values, but keep the pattern consistent)
        self._run_write(
            f"INSERT INTO {self.tbl} VALUES (:k, :v, :w, current_timestamp())", client,
            params=[{"name": "k", "value": "genie_space_id"},
                    {"name": "v", "value": space_id},
                    {"name": "w", "value": who or "unknown"}])


def message_to_view(msg) -> dict:
    """Flatten a GenieMessage into a frontend-friendly view: the text answer, the SQL
    Genie ran (explainability!), any suggested follow-ups, and ids to fetch the result."""
    view: dict[str, Any] = {"status": str(getattr(msg, "status", "") or ""),
                            "text": None, "sql": None, "sql_title": None,
                            "attachment_id": None, "conversation_id": getattr(msg, "conversation_id", None),
                            "message_id": getattr(msg, "message_id", None),
                            "error": None, "suggestions": []}
    err = getattr(msg, "error", None)
    if err:
        view["error"] = getattr(err, "error", None) or str(err)
    for att in (getattr(msg, "attachments", None) or []):
        if getattr(att, "text", None) and getattr(att.text, "content", None):
            view["text"] = att.text.content
        if getattr(att, "suggested_questions", None):
            sq = att.suggested_questions
            view["suggestions"] = getattr(sq, "suggested_questions", None) or []
        q = getattr(att, "query", None)
        if q is not None:
            view["sql"] = getattr(q, "query", None)
            view["sql_title"] = getattr(q, "description", None) or getattr(q, "title", None)
            view["attachment_id"] = getattr(att, "attachment_id", None)
    return view
