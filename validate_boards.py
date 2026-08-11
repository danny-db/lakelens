#!/usr/bin/env python3
"""
validate_boards.py — run every Observability board panel SQL against your workspace,
exactly as the app will. Fails loudly per panel. Mirrors validate.py.

Usage: python3 validate_boards.py
"""
import sys, json, subprocess, time

sys.path.insert(0, "app")
import boards  # noqa

PROFILE = "DEFAULT"
WAREHOUSE = "<your-sql-warehouse-id>"


def run_sql(sql, timeout=180):
    payload = {"warehouse_id": WAREHOUSE, "statement": sql,
               "wait_timeout": "30s", "on_wait_timeout": "CONTINUE"}
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
        pg = subprocess.run(["databricks", "api", "get",
                             f"/api/2.0/sql/statements/{sid}", "-p", PROFILE],
                            capture_output=True, text=True)
        d = json.loads(pg.stdout)
        state = d.get("status", {}).get("state")
    if state != "SUCCEEDED":
        return {"state": state,
                "error": d.get("status", {}).get("error", {}).get("message", "?")}
    n = len(d.get("result", {}).get("data_array", []) or [])
    return {"state": "SUCCEEDED", "rows": n}


def main():
    passed, failed = [], []
    for b in boards.BOARDS:
        for p in b["panels"]:
            ident = f"{b['id']}/{p['id']}"
            print(f"[{ident:28}] {p['title'][:40]:40} … ", end="", flush=True)
            r = run_sql(p["sql"])
            if r["state"] == "SUCCEEDED":
                print(f"OK ({r.get('rows',0)} rows)")
                passed.append(ident)
            else:
                print(f"FAIL {r['state']}: {r.get('error','')[:160]}")
                failed.append((ident, r.get("error", r["state"])))
    print(f"\n=== {len(passed)} passed / {len(failed)} failed ===")
    for ident, err in failed:
        print(f"  x {ident}: {str(err)[:220]}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
