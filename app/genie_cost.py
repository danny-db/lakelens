"""
genie_cost.py — the "Ask" tab: natural-language cost questions through Genie One.

Genie One is the Unity AI Gateway MCP service `system.ai.genie_one_mcp`. Nothing is
created in the customer's workspace: every question is a stateless JSON-RPC
`tools/call` to the gateway, posted with the logged-in user's (OBO) client, so
Genie only sees tables that user can already read.

Genie One searches all of the user's data, so an unprompted question can wander off
to the wrong source. We prepend a grounding preamble built from SEED_TABLES and
INSTRUCTIONS, and show the user only their own question.
"""
import re
import time
from typing import Literal, Optional

from typing_extensions import TypedDict

# The cost system tables Genie should answer from, each with a short description so
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


MCP_PATH = "/ai-gateway/mcp-services/system.ai.genie_one_mcp"
_HEADERS = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}


def grounded_question(question: str) -> str:
    """The question Genie One actually receives: a preamble naming the cost tables
    and the list-price rule, then the user's own words."""
    tables = "\n".join(f"- {ident}: {desc}" for ident, desc in SEED_TABLES)
    return (f"{INSTRUCTIONS}\nAnswer only from these Databricks system tables:\n{tables}\n\n"
            f"Question: {question}")


class GenieError(Exception):
    """A Genie One failure with the HTTP status the API route should return."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def call_tool(client, name: str, args: dict) -> dict:
    """Call one Genie One MCP tool as the client's user. Returns the tool's
    structured result, or raises GenieError for protocol and tool errors."""
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": name, "arguments": args}}
    out = client.api_client.do("POST", MCP_PATH, body=body, headers=_HEADERS)
    if out.get("error"):
        raise GenieError(502, f"Genie One rejected the call: {out['error'].get('message')}")
    res = out.get("result") or {}
    if res.get("isError"):
        text = " ".join(c.get("text", "") for c in res.get("content") or []).strip()
        m = re.search(r"HTTP (\d{3})", text)
        raise GenieError(int(m.group(1)) if m else 502, text or "Genie One returned an error.")
    sc = res.get("structuredContent")
    return sc if sc is not None else res


def poll_response(client, conversation_id: str, response_id: str) -> dict:
    """genie_poll_response, riding out the few seconds after a follow-up is sent
    when Genie One 404s the response_id it just issued."""
    args = {"conversation_id": conversation_id, "response_id": response_id}
    for wait in (1.5, 1.5, 1.5):
        try:
            return call_tool(client, "genie_poll_response", args)
        except GenieError as e:
            if e.status != 404:
                raise
            time.sleep(wait)
    return call_tool(client, "genie_poll_response", args)


Status = Literal["in_progress", "completed", "failed", "cancelled"]


class GenieQuery(TypedDict):
    item_id: str
    sql: str


class GenieTurn(TypedDict):
    """One Genie One answer as the UI sees it. The UI branches on `status` only."""
    status: Status
    conversation_id: Optional[str]
    response_id: Optional[str]
    text: Optional[str]
    queries: list[GenieQuery]
    deep_link: Optional[str]
    error: Optional[str]


_STATUSES = {"in_progress", "completed", "failed", "cancelled"}


def to_turn(payload: dict) -> GenieTurn:
    """Map a genie_ask / genie_poll_response payload to a GenieTurn. Unknown
    upstream statuses become "failed" with the raw status in `error`."""
    raw = str(payload.get("status") or "")
    status = raw if raw in _STATUSES else "failed"
    error = payload.get("error")
    if status != raw:
        error = f"Genie One returned an unexpected status: {raw or 'none'}"
    return GenieTurn(
        status=status,
        conversation_id=payload.get("conversation_id"),
        response_id=payload.get("response_id"),
        text=payload.get("final_answer"),
        queries=[GenieQuery(item_id=q["item_id"], sql=q.get("sql") or "")
                 for q in payload.get("query_items") or [] if q.get("item_id")],
        deep_link=payload.get("deep_link"),
        error=str(error) if error else None,
    )
