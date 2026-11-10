"""
store_delta.py — Delta-backed persistence for scan runs, findings, and triage.

Why: the in-memory Store is per-container and evaporates on restart, so different
users (and the same user after a redeploy) saw different / empty views. Persisting
to Delta gives ONE shared, durable view — and unlocks run-over-run comparison
(is this finding better or worse than last scan?).

Tables (in a configurable catalog.schema, default main.cost_optimiser):
  scan_runs      — one row per completed scan (counts, self-cost, who, when)
  scan_findings  — one row per finding per run (severity, metric, $, explanation…)
  finding_triage — latest triage state per hunt (survives re-scans, shared)

All writes use PARAMETERIZED statements (verified working under OBO) so free-text
like AI explanations can't break the SQL. Everything degrades gracefully: if the
tables aren't writable (permissions, wrong cloud), the caller falls back to the
in-memory Store and the app still works — just not shared/durable.
"""
import json
import logging
import random
import time
from typing import Optional, Callable

import hunts as catalogue

logger = logging.getLogger("uvicorn.error")


class DeltaStore:
    """Persists scans to Delta. `run_sql` and `run_write` are injected from app.py
    (they carry the OBO client), so this module has no Databricks SDK import."""

    def __init__(self, catalog: str, schema: str,
                 run_sql: Callable, run_write: Callable):
        self.catalog = catalog
        self.schema = schema
        self.fq = f"{catalog}.{schema}"
        self._run_sql = run_sql       # run_sql(sql, client, params=None) -> {rows, columns}
        self._run_write = run_write   # run_write(sql, client, params=None) -> None (INSERT/DDL)
        self.ready = False
        self.last_error: Optional[str] = None

    # ── schema bootstrap ──────────────────────────────────────────────────────
    def ensure_tables(self, client) -> bool:
        """Create the schema + 3 tables if absent. Returns True if writable.
        Called once per scan-persist attempt; cheap (IF NOT EXISTS)."""
        if not self.catalog:   # no RESULTS_CATALOG set → persistence off (app still runs in-memory)
            self.last_error = "RESULTS_CATALOG not set"
            return False
        try:
            self._run_write(f"CREATE SCHEMA IF NOT EXISTS {self.fq}", client)
            self._run_write(f"""
                CREATE TABLE IF NOT EXISTS {self.fq}.scan_runs (
                  scan_id STRING, started_at TIMESTAMP, ended_at TIMESTAMP,
                  started_by STRING, total INT, findings INT, high INT, medium INT, clean INT,
                  scan_seconds DOUBLE, dbsql_usd DOUBLE, ai_usd DOUBLE, total_usd DOUBLE
                ) USING DELTA""", client)
            self._run_write(f"""
                CREATE TABLE IF NOT EXISTS {self.fq}.scan_findings (
                  scan_id STRING, started_at TIMESTAMP, hunt_id STRING, title STRING,
                  category STRING, severity STRING, summary STRING,
                  metric_label STRING, metric_value STRING, metric_num DOUBLE,
                  impact_usd DOUBLE, total_rows BIGINT, measure STRING,
                  action STRING, explanation STRING, evidence_json STRING
                ) USING DELTA""", client)
            self._run_write(f"""
                CREATE TABLE IF NOT EXISTS {self.fq}.finding_triage (
                  hunt_id STRING, status STRING, note STRING, updated_by STRING, updated_at TIMESTAMP
                ) USING DELTA""", client)
            self.ready = True
            return True
        except Exception as e:  # noqa: BLE001
            self.last_error = str(e)[:300]
            logger.warning(f"DeltaStore.ensure_tables failed (falling back to memory): {e}")
            self.ready = False
            return False

    # ── persist a completed scan ──────────────────────────────────────────────
    def save_scan(self, client, view: dict) -> bool:
        """Write one scan's run row + all its finding rows. `view` = scan_view(). The run's
        self-cost columns stay NULL until save_cost attaches the cost."""
        if not self.ensure_tables(client):
            return False
        try:
            counts = view.get("counts", {})
            # Write the FINDINGS first, then the run row LAST. The run row is the
            # commit marker: latest_two_run_ids() only sees runs whose scan_runs row
            # exists, so a reader can never catch a half-written run (all findings
            # are already in place by the time the run row appears).
            for f in view.get("findings", []):
                self._run_write(f"""
                    INSERT INTO {self.fq}.scan_findings VALUES
                    (:scan_id, :started_at::timestamp, :hunt_id, :title, :category, :severity,
                     :summary, :metric_label, :metric_value, :metric_num::double,
                     :impact_usd::double, :total_rows::bigint, :measure, :action, :explanation, :evidence_json)
                """, client, [
                    {"name": "scan_id", "value": view["scan_id"]},
                    {"name": "started_at", "value": view.get("started_at")},
                    {"name": "hunt_id", "value": f["hunt_id"]},
                    {"name": "title", "value": f.get("title", "")},
                    {"name": "category", "value": f.get("category", "")},
                    {"name": "severity", "value": f.get("severity", "")},
                    {"name": "summary", "value": f.get("summary", "")},
                    {"name": "metric_label", "value": f.get("metric_label", "")},
                    {"name": "metric_value", "value": f.get("metric_value", "")},
                    {"name": "metric_num", "value": str(f.get("metric_num")) if f.get("metric_num") is not None else None},
                    {"name": "impact_usd", "value": str(f["impact_usd"]) if f.get("impact_usd") is not None else None},
                    {"name": "total_rows", "value": str(f.get("total_rows", 0))},
                    {"name": "measure", "value": f.get("measure") or ""},
                    {"name": "action", "value": f.get("action", "")},
                    {"name": "explanation", "value": f.get("explanation") or ""},
                    {"name": "evidence_json", "value": json.dumps({**f.get("evidence", {}), "vs": f.get("vs"),
                                                                "lens": f.get("lens"), "why": f.get("why")})[:60000]},
                ])
            # Run row LAST — the commit marker.
            self._run_write(f"""
                INSERT INTO {self.fq}.scan_runs VALUES
                (:scan_id, :started_at::timestamp, :ended_at::timestamp, :started_by,
                 :total::int, :findings::int, :high::int, :medium::int, :clean::int,
                 :scan_seconds::double, :dbsql_usd::double, :ai_usd::double, :total_usd::double)
            """, client, [
                {"name": "scan_id", "value": view["scan_id"]},
                {"name": "started_at", "value": view.get("started_at")},
                {"name": "ended_at", "value": view.get("ended_at")},
                {"name": "started_by", "value": view.get("started_by") or ""},
                {"name": "total", "value": str(view.get("total", 0))},
                {"name": "findings", "value": str(len(view.get("findings", [])))},
                {"name": "high", "value": str(counts.get("high", 0))},
                {"name": "medium", "value": str(counts.get("medium", 0))},
                {"name": "clean", "value": str(view.get("clean", 0))},
                {"name": "scan_seconds", "value": str(round(float(view.get("wall_ms") or 0) / 1000.0, 1))},
                {"name": "dbsql_usd", "value": None},
                {"name": "ai_usd", "value": None},
                {"name": "total_usd", "value": None},
            ])
            logger.info(f"DeltaStore: persisted scan {view['scan_id']} ({len(view.get('findings',[]))} findings)")
            return True
        except Exception as e:  # noqa: BLE001
            self.last_error = str(e)[:300]
            logger.error(f"DeltaStore.save_scan failed: {e}")
            return False

    def save_cost(self, client, scan_id: str, cost: dict) -> bool:
        """Attach a saved scan's self-cost (dbsql_usd, ai_usd, total_usd) to its run row."""
        try:
            self._run_write(f"""
                UPDATE {self.fq}.scan_runs
                SET dbsql_usd = :dbsql_usd::double, ai_usd = :ai_usd::double, total_usd = :total_usd::double
                WHERE scan_id = :scan_id""", client,
                [{"name": "scan_id", "value": scan_id}] +
                [{"name": k, "value": None if cost.get(k) is None else str(cost[k])}
                 for k in ("dbsql_usd", "ai_usd", "total_usd")])
            return True
        except Exception as e:  # noqa: BLE001
            self.last_error = str(e)[:300]
            logger.warning(f"DeltaStore.save_cost failed: {e}")
            return False

    # ── read the latest persisted scan (+ the one before, for comparison) ──────
    def latest_two_run_ids(self, client) -> list[str]:
        if not self.ensure_tables(client):
            return []
        try:
            out = self._run_sql(
                f"SELECT scan_id FROM {self.fq}.scan_runs ORDER BY started_at DESC LIMIT 2", client)
            return [r[0] for r in out["rows"]]
        except Exception as e:  # noqa: BLE001
            logger.warning(f"DeltaStore.latest_two_run_ids failed: {e}")
            return []

    def load_run(self, client, scan_id: str) -> Optional[dict]:
        """Rebuild a scan_view()-shaped dict from Delta for a given run."""
        try:
            runs = self._run_sql(
                f"SELECT scan_id, started_at, ended_at, started_by, total, findings, high, medium, clean, "
                f"scan_seconds, dbsql_usd, ai_usd, total_usd FROM {self.fq}.scan_runs WHERE scan_id=:sid",
                client, [{"name": "sid", "value": scan_id}])
            if not runs["rows"]:
                return None
            r = runs["rows"][0]
            fnd = self._run_sql(
                f"SELECT hunt_id, title, category, severity, summary, metric_label, metric_value, "
                f"impact_usd, total_rows, measure, action, explanation, evidence_json, metric_num "
                f"FROM {self.fq}.scan_findings WHERE scan_id=:sid", client,
                [{"name": "sid", "value": scan_id}])
            findings = []
            for x in fnd["rows"]:
                try:
                    ev = json.loads(x[12]) if x[12] else {}
                except Exception:
                    ev = {}
                findings.append(_with_current_text({
                    "hunt_id": x[0], "title": x[1], "category": x[2], "severity": x[3],
                    "summary": x[4], "metric_label": x[5], "metric_value": x[6],
                    "impact_usd": _f(x[7]), "total_rows": int(x[8] or 0), "measure": x[9] or None,
                    "action": x[10], "explanation": x[11] or None, "vs": ev.pop("vs", None),
                    "lens": ev.pop("lens", None), "why": ev.pop("why", None), "evidence": ev,
                    "metric_num": _f(x[13]),
                }))
            return {
                "scan_id": r[0], "started_at": str(r[1]), "ended_at": str(r[2]),
                "started_by": r[3], "total": int(r[4] or 0),
                "counts": {"high": int(r[6] or 0), "medium": int(r[7] or 0)},
                "clean": int(r[8] or 0), "scan_seconds": _f(r[9]),
                "cost": {"dbsql_usd": _f(r[10]), "ai_usd": _f(r[11]), "total_usd": _f(r[12])},
                "findings": findings, "persisted": True,
            }
        except Exception as e:  # noqa: BLE001
            logger.warning(f"DeltaStore.load_run failed: {e}")
            return None

    # ── shared triage ──────────────────────────────────────────────────────────
    def set_triage(self, client, hunt_id: str, status: str, note: str, who: str) -> bool:
        """Upsert this hunt's triage in one atomic MERGE, so a failed write leaves the
        prior state and a repeat converges to the same single row."""
        if not self.ensure_tables(client):
            return False
        for attempt in range(6):
            try:
                return self._merge_triage(client, hunt_id, status, note, who)
            except Exception as e:  # noqa: BLE001
                # Two users triaging at once conflict in Delta; the MERGE is safe to repeat.
                if "DELTA_CONCURRENT" in str(e) and attempt < 5:
                    time.sleep(random.uniform(0.2, 1.0))
                    continue
                self.last_error = str(e)[:300]
                logger.warning(f"DeltaStore.set_triage failed: {e}")
                return False
        return False

    def _merge_triage(self, client, hunt_id: str, status: str, note: str, who: str) -> bool:
        self._run_write(f"""
                MERGE INTO {self.fq}.finding_triage t
                USING (SELECT :h AS hunt_id, :s AS status, :n AS note, :w AS updated_by,
                              current_timestamp() AS updated_at) s
                ON t.hunt_id = s.hunt_id
                WHEN MATCHED THEN UPDATE SET *
                WHEN NOT MATCHED THEN INSERT *""", client,
            [{"name": "h", "value": hunt_id}, {"name": "s", "value": status},
             {"name": "n", "value": note or ""}, {"name": "w", "value": who or ""}])
        return True

    def all_triage(self, client) -> dict[str, dict]:
        if not self.ensure_tables(client):
            return {}
        try:
            out = self._run_sql(
                f"SELECT hunt_id, status, note, updated_by FROM {self.fq}.finding_triage", client)
            return {r[0]: {"status": r[1], "note": r[2] or "", "updated_by": r[3] or ""} for r in out["rows"]}
        except Exception:  # noqa: BLE001
            return {}


_HUNTS_BY_ID = {h["id"]: h for h in catalogue.HUNTS}


def _with_current_text(f: dict) -> dict:
    """A saved finding shows the hunt as the catalogue describes it now (title, category,
    fix, measure, eli5), so a renamed hunt never reads under its old name. The stored text
    is the fallback for a hunt that no longer exists. summary, metric_label and
    metric_value stay as saved: they describe the number that scan measured."""
    h = _HUNTS_BY_ID.get(f["hunt_id"])
    if h:
        f.update(title=h["title"], category=h["category"], action=h["action"],
                 measure=h.get("measure"), eli5=h["eli5"])
    return f


def _f(x) -> Optional[float]:
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def compare_runs(current: dict, previous: Optional[dict]) -> dict:
    """Annotate the current run's findings with run-over-run trend vs the previous
    run, and compute a run-level summary. Pure function — no I/O.

    A finding's saved metric_label names what its number measures, so two runs are
    compared only when the labels match. A hunt whose definition changed between them
    gets trend 'changed' and no previous value.

    Adds to each finding: trend ('new'|'better'|'worse'|'flat'|'changed'), prev_metric_value.
    Adds top-level: resolved_since (findings present last run, gone now), deltas."""
    prev_by_hunt = {}
    if previous:
        for f in previous.get("findings", []):
            prev_by_hunt[f["hunt_id"]] = f

    def metric_num(f):
        v = f.get("metric_num")
        if v is not None:
            return v
        # fall back to parsing the display string
        import re
        m = re.search(r"[-+]?\d[\d,]*\.?\d*", str(f.get("metric_value", "")))
        return float(m.group().replace(",", "")) if m else None

    seen_now = set()
    for f in current.get("findings", []):
        seen_now.add(f["hunt_id"])
        p = prev_by_hunt.get(f["hunt_id"])
        if not p:
            f["trend"] = "new"
            f["prev_metric_value"] = None
            continue
        if p.get("metric_label") != f.get("metric_label"):
            f["trend"] = "changed"
            f["prev_metric_value"] = None
            continue
        f["prev_metric_value"] = p.get("metric_value")
        cur_n, prev_n = metric_num(f), metric_num(p)
        if cur_n is None or prev_n is None:
            f["trend"] = "flat"
        elif prev_n == 0:
            f["trend"] = "worse" if cur_n > 0 else "flat"
        else:
            change = (cur_n - prev_n) / abs(prev_n)
            # For every alerting metric here, LOWER is better (less waste/spend/wait).
            if change <= -0.10:
                f["trend"] = "better"
            elif change >= 0.10:
                f["trend"] = "worse"
            else:
                f["trend"] = "flat"

    # resolved = present last run, absent now (dropped below threshold or fixed), under the
    # definition the hunt still measures; an alert raised under an older one has not cleared.
    resolved = []
    if previous:
        for hid, p in prev_by_hunt.items():
            if hid not in seen_now and p.get("metric_label") == catalogue.ALERT_SPECS.get(hid, {}).get("metric_label"):
                resolved.append({"hunt_id": hid, "title": p.get("title"),
                                 "prev_severity": p.get("severity"),
                                 "prev_metric_value": p.get("metric_value")})
    current["resolved_since"] = resolved
    if previous:
        current["prev_run"] = {
            "scan_id": previous.get("scan_id"), "started_at": previous.get("started_at"),
            "findings": len(previous.get("findings", [])),
            "high": previous.get("counts", {}).get("high", 0),
            "total_usd": (previous.get("cost") or {}).get("total_usd"),
        }
    return current
