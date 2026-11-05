#!/usr/bin/env python3
"""
validate.py — run every hunt + measure + explainer + KPI SQL against your workspace's
real system tables, exactly as the app would. Fails loudly on any SQL error.

Usage:  python3 validate.py            # run all
        python3 validate.py H0 M3      # run a subset
Set PROFILE / WAREHOUSE below to point at your workspace.
"""
import sys, time, json, subprocess, re

sys.path.insert(0, "app")
import hunts as cat  # noqa

PROFILE = "DEFAULT"
WAREHOUSE = "<your-sql-warehouse-id>"
AI_ENDPOINT = "databricks-gpt-6-luna"


def run_sql(sql: str, timeout=170):
    """Submit via CLI api post + poll — mirrors the app's _run_sql path."""
    payload = {"warehouse_id": WAREHOUSE, "statement": sql, "wait_timeout": "30s",
               "on_wait_timeout": "CONTINUE"}
    p = subprocess.run(["databricks", "api", "post", "/api/2.0/sql/statements",
                        "-p", PROFILE, "--json", json.dumps(payload)],
                       capture_output=True, text=True)
    if p.returncode != 0:
        return {"state": "CLI_ERROR", "error": p.stderr[:400]}
    d = json.loads(p.stdout)
    sid = d.get("statement_id")
    state = d.get("status", {}).get("state")
    waited = 0
    while state in ("PENDING", "RUNNING") and waited < timeout:
        time.sleep(3); waited += 3
        pg = subprocess.run(["databricks", "api", "get", f"/api/2.0/sql/statements/{sid}", "-p", PROFILE],
                            capture_output=True, text=True)
        d = json.loads(pg.stdout)
        state = d.get("status", {}).get("state")
    if state != "SUCCEEDED":
        err = d.get("status", {}).get("error", {}).get("message", "unknown")
        return {"state": state, "error": err}
    n = 0
    if d.get("result", {}).get("data_array"):
        n = len(d["result"]["data_array"])
    return {"state": "SUCCEEDED", "rows": n}


def bounded(sql, n=10):
    """Mirror app.py _bounded: cap hunt SQL to top-n for the explainer prompt."""
    s = sql.strip().rstrip(";").rstrip()
    s = re.sub(r"(?is)\s+limit\s+\d+\s*$", "", s)
    return s + f"\nLIMIT {n}"


def explain_wrap(rubric, hunt_desc, rows_sql, is_trend=False):
    """Rebuild the app's explainer SQL wrapper."""
    if is_trend:
        prompt = rubric
    else:
        prompt = (
            "You are a Databricks cost analyst. Interpret the result using THIS rubric: \""
            + rubric + "\" In 2-3 neutral sentences, explain what the result shows and whether "
            "it looks like waste worth investigating. Cite the figures; do NOT overstate cost. "
            "If the row list is empty, say the hunt found nothing notable this period. Hunt = "
            + hunt_desc + ". Rows: ")
    esc = prompt.replace("\\", "\\\\").replace("'", "''")
    return f"SELECT ai_query('{AI_ENDPOINT}', CONCAT('{esc}', coalesce(to_json(collect_list(struct(t.*))), '[]'))) AS explanation FROM ( {rows_sql} ) t"


def main():
    only = set(a.upper() for a in sys.argv[1:])
    passed, failed = [], []

    items = []
    for h in cat.HUNTS:
        items.append(("HUNT", h["id"], h["title"], h["sql"]))
        if h.get("explain_rubric") or h.get("explain_is_trend"):
            if h.get("explain_is_trend"):
                rs = h.get("explain_sql", h["sql"])          # trend rollup is pre-bounded
            else:
                rs = h.get("explain_sql") or bounded(h["sql"], 10)
            wrap = explain_wrap(h.get("explain_rubric"), h.get("explain_hunt_desc"), rs,
                                is_trend=bool(h.get("explain_is_trend")))
            items.append(("EXPLAIN", h["id"] + "·ai", h["title"] + " (explainer)", wrap))
    for m in cat.MEASURES:
        items.append(("MEASURE", m["id"], m["title"], m["sql"]))

    for kind, ident, title, sql in items:
        base = ident.split("·")[0]
        if only and base not in only and ident not in only:
            continue
        print(f"[{kind:8}] {ident:8} {title[:42]:42} … ", end="", flush=True)
        r = run_sql(sql)
        if r["state"] == "SUCCEEDED":
            print(f"OK  ({r.get('rows',0)} rows)")
            passed.append(ident)
        else:
            print(f"FAIL  {r['state']}: {r.get('error','')[:150]}")
            failed.append((ident, r.get("error", r["state"])))

    print(f"\n=== {len(passed)} passed / {len(failed)} failed ===")
    for ident, err in failed:
        print(f"  ✗ {ident}: {err[:200]}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
