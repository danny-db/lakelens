#!/usr/bin/env python3
"""
probe_schema.py — verify every column / struct-field / feature our new
Observability + Jobs + DBSQL + Model-Serving SQL will depend on ACTUALLY
exists on your workspace before we write the real queries. (Lesson from H20: docs and
dashboards drift from live system tables — probe, don't assume.)

Each probe is a tiny SELECT that references the exact column/field. SUCCEEDED
== it exists & is queryable on this warehouse. We also capture a few enum
value lists (usage_type, serving_type, result_state) and test AI_FORECAST().

Usage: python3 probe_schema.py
"""
import json, subprocess, time

PROFILE = "DEFAULT"
WAREHOUSE = "<your-sql-warehouse-id>"


def run(sql, timeout=120):
    payload = {"warehouse_id": WAREHOUSE, "statement": sql,
               "wait_timeout": "30s", "on_wait_timeout": "CONTINUE"}
    p = subprocess.run(["databricks", "api", "post", "/api/2.0/sql/statements",
                        "-p", PROFILE, "--json", json.dumps(payload)],
                       capture_output=True, text=True)
    if p.returncode != 0:
        return {"state": "CLI_ERROR", "error": p.stderr[:300], "rows": []}
    d = json.loads(p.stdout)
    sid = d.get("statement_id")
    state = d.get("status", {}).get("state")
    waited = 0
    while state in ("PENDING", "RUNNING") and waited < timeout:
        time.sleep(3); waited += 3
        pg = subprocess.run(["databricks", "api", "get",
                             f"/api/2.0/sql/statements/{sid}", "-p", PROFILE],
                            capture_output=True, text=True)
        d = json.loads(pg.stdout)
        state = d.get("status", {}).get("state")
    if state != "SUCCEEDED":
        return {"state": state,
                "error": d.get("status", {}).get("error", {}).get("message", "?")[:300],
                "rows": []}
    return {"state": "SUCCEEDED",
            "rows": d.get("result", {}).get("data_array", []) or []}


# (id, description, sql). Column-existence probes select the exact field with LIMIT 1.
PROBES = [
    # ── Observability / Usage (Dashboard v2 core deps) ──
    ("usage.product_features", "is_serverless / is_photon flags",
     "SELECT product_features.is_serverless, product_features.is_photon FROM system.billing.usage LIMIT 1"),
    ("usage.custom_tags", "custom_tags map on usage",
     "SELECT custom_tags FROM system.billing.usage WHERE custom_tags IS NOT NULL LIMIT 1"),
    ("workspaces_latest", "workspace id+name lookup",
     "SELECT workspace_id, workspace_name FROM system.access.workspaces_latest LIMIT 1"),
    ("list_prices.effective_list", "pricing.effective_list.default",
     "SELECT pricing.effective_list.default FROM system.billing.list_prices LIMIT 1"),
    ("AI_FORECAST", "AI_FORECAST TVF runs on this warehouse",
     """WITH h AS (SELECT usage_date::timestamp AS ds, sum(usage_quantity) AS y
                   FROM system.billing.usage WHERE usage_date >= current_date() - INTERVAL 40 DAYS
                   GROUP BY 1)
        SELECT ds, y_forecast FROM AI_FORECAST(TABLE(h), horizon => current_date() + INTERVAL 14 DAYS,
                  time_col => 'ds', value_col => 'y') LIMIT 3"""),

    # ── Jobs (built from compute tables; docs lacked these) ──
    ("node_timeline.cpu", "cpu_user_percent + cpu_system_percent",
     "SELECT cpu_user_percent, cpu_system_percent FROM system.compute.node_timeline LIMIT 1"),
    ("node_timeline.mem", "memory columns (probe several names)",
     "SELECT * FROM system.compute.node_timeline LIMIT 1"),
    ("clusters.sizing", "spark_version, num_workers, autoscale, worker_node_type",
     "SELECT spark_version, num_workers, autoscale, worker_node_type_id, driver_node_type_id FROM system.compute.clusters LIMIT 1"),
    ("job_run_timeline.state", "result_state / termination present",
     "SELECT DISTINCT result_state FROM system.lakeflow.job_run_timeline WHERE period_start_time >= current_date() - INTERVAL 30 DAYS LIMIT 25"),
    ("job_task_run_timeline.compute", "compute_ids array on task timeline",
     "SELECT compute_ids FROM system.lakeflow.job_task_run_timeline WHERE compute_ids IS NOT NULL LIMIT 1"),
    ("jobs.meta", "system.lakeflow.jobs name",
     "SELECT job_id, name FROM system.lakeflow.jobs LIMIT 1"),

    # ── DBSQL (query_source struct fields) ──
    ("query.query_source", "all query_source id fields",
     """SELECT query_source.job_info.job_id, query_source.notebook_id, query_source.sql_query_id,
               query_source.dashboard_id, query_source.legacy_dashboard_id, query_source.alert_id,
               query_source.genie_space_id
        FROM system.query.history LIMIT 1"""),
    ("query.durations", "duration component columns",
     """SELECT execution_duration_ms, compilation_duration_ms, total_task_duration_ms,
               result_fetch_duration_ms, waiting_at_capacity_duration_ms, waiting_for_compute_duration_ms
        FROM system.query.history LIMIT 1"""),
    ("query.source_type_dist", "distribution of derived source types (30d)",
     """SELECT CASE
          WHEN query_source.job_info.job_id IS NOT NULL THEN 'JOB'
          WHEN query_source.legacy_dashboard_id IS NOT NULL THEN 'LEGACY DASHBOARD'
          WHEN query_source.dashboard_id IS NOT NULL THEN 'AI/BI DASHBOARD'
          WHEN query_source.alert_id IS NOT NULL THEN 'ALERT'
          WHEN query_source.notebook_id IS NOT NULL THEN 'NOTEBOOK'
          WHEN query_source.sql_query_id IS NOT NULL THEN 'SQL QUERY'
          WHEN query_source.genie_space_id IS NOT NULL THEN 'GENIE SPACE'
          ELSE 'OTHER' END AS src, count(*) AS n
        FROM system.query.history WHERE start_time >= current_date() - INTERVAL 30 DAYS GROUP BY 1 ORDER BY n DESC"""),

    # ── Model Serving (tiantan32 dashboard deps) ──
    ("usage.serving_type", "serving_type + offering_type + usage_type distribution",
     """SELECT product_features.serving_type AS serving_type,
               product_features.model_serving.offering_type AS offering_type,
               usage_type, count(*) AS n
        FROM system.billing.usage
        WHERE billing_origin_product = 'MODEL_SERVING' AND usage_date >= current_date() - INTERVAL 30 DAYS
        GROUP BY 1,2,3 ORDER BY n DESC"""),
    ("usage.endpoint_meta", "usage_metadata.endpoint_name / endpoint_id",
     "SELECT usage_metadata.endpoint_name, usage_metadata.endpoint_id FROM system.billing.usage WHERE billing_origin_product='MODEL_SERVING' LIMIT 1"),
    ("serving.endpoint_usage", "token columns on serving.endpoint_usage",
     "SELECT input_token_count, output_token_count, request_time, requester, served_entity_id FROM system.serving.endpoint_usage LIMIT 1"),
    ("serving.served_entities", "served_entities metadata",
     "SELECT served_entity_id, served_entity_name, endpoint_name, endpoint_id, entity_type FROM system.serving.served_entities LIMIT 1"),
    ("ai_gateway.usage", "ai_gateway.usage token columns (fallback path)",
     "SELECT endpoint_name, input_tokens, output_tokens, total_tokens, event_time FROM system.ai_gateway.usage LIMIT 1"),

    # ── Snappy→zstd (codec is NOT in system tables — confirm the fallback path) ──
    ("information_schema.tables", "data_source_format, table_type, created, storage_path",
     "SELECT table_catalog, table_schema, table_name, data_source_format, table_type, created FROM system.information_schema.tables WHERE data_source_format='DELTA' LIMIT 1"),
    ("delta_tables.count", "count DELTA tables created before Oct-2025 zstd flip",
     "SELECT count(*) FROM system.information_schema.tables WHERE data_source_format='DELTA' AND table_type IN ('MANAGED','EXTERNAL') AND created < '2025-10-15'"),
]


def main():
    print(f"Probing {len(PROBES)} schema assumptions on {PROFILE} / {WAREHOUSE}\n")
    ok, bad = [], []
    for pid, desc, sql in PROBES:
        r = run(sql)
        if r["state"] == "SUCCEEDED":
            ok.append(pid)
            extra = ""
            # For the enum/distribution probes, echo the values (they drive CASE logic)
            if pid in ("job_run_timeline.state", "query.source_type_dist",
                       "usage.serving_type", "delta_tables.count") and r["rows"]:
                extra = " -> " + json.dumps(r["rows"][:25])
            print(f"  OK   {pid:28} {desc}{extra}")
        else:
            bad.append((pid, r.get("error", r["state"])))
            print(f"  FAIL {pid:28} {desc}\n         {r['state']}: {r.get('error','')[:200]}")
    print(f"\n=== {len(ok)} ok / {len(bad)} failed ===")
    for pid, err in bad:
        print(f"  x {pid}: {err[:180]}")


if __name__ == "__main__":
    main()
