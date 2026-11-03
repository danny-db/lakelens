"""
scan.py — the FinOps workflow engine behind the Alerts tab.

This is what turns the app from "a prettier notebook" into a workflow:
one full scan runs every waste-signal hunt, and each result becomes a
pre-triaged ALERT — severity, a plain-English explanation, the recommended
fix, and which measure proves the before/after — dropped into a queue a
FinOps admin works (acknowledge → in progress → resolved / dismissed).

Design notes:
- **Severity is deterministic** (rule-based, from each hunt's own magnitude
  metric vs the thresholds in hunts.ALERT_SPECS). The AI only narrates; it
  never sets severity. We never invent a dollar figure a query didn't produce.
- **Async**: a scan runs on a background thread so no single HTTP request is
  held past the Databricks Apps ~120s cap. The frontend polls and streams
  findings in as each hunt completes.
- **State is in-memory** (demo-scoped): scans by id, and triage status keyed by
  hunt id so an admin's decisions survive a re-scan. The STORE object is the
  only stateful thing — swap it for a Delta/Lakebase table to persist.
"""
import threading
import traceback
from datetime import datetime, timezone
from typing import Optional, Callable

import hunts as catalogue


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

# Severity order for sorting / summarising.
SEV_RANK = {"high": 0, "medium": 1, "low": 2, "info": 3}
TRIAGE_STATES = ("new", "acknowledged", "in_progress", "resolved", "dismissed")


class Store:
    """In-memory scan + triage store. Thread-safe for the app's needs.
    Swap this class for a Delta/Lakebase-backed one to persist across restarts."""

    def __init__(self):
        self._lock = threading.Lock()
        self.scans: dict[str, dict] = {}          # scan_id -> scan record
        self.triage: dict[str, dict] = {}         # hunt_id -> {status, note, updated_by, updated_ts}
        self._counter = 0

    def new_scan_id(self) -> str:
        # Globally unique (uuid) — Databricks Apps runs MULTIPLE worker processes,
        # each with its own STORE, so a per-process counter collides (two workers
        # both mint 'scan-1') and a poll can hit a worker that never saw the id.
        # The uuid makes the id unique; the frontend also falls back to the shared
        # Delta-backed /api/scan/latest for the completed view (cross-worker safe).
        import uuid
        return "scan-" + uuid.uuid4().hex[:12]

    def _legacy_new_scan_id(self) -> str:
        with self._lock:
            self._counter += 1
            return f"scan-{self._counter}"

    def put_scan(self, scan_id: str, record: dict):
        with self._lock:
            self.scans[scan_id] = record

    def get_scan(self, scan_id: str) -> Optional[dict]:
        with self._lock:
            rec = self.scans.get(scan_id)
            return dict(rec) if rec else None   # shallow copy for safe read

    def update_scan(self, scan_id: str, **changes):
        with self._lock:
            if scan_id in self.scans:
                self.scans[scan_id].update(changes)

    def append_finding(self, scan_id: str, finding: dict):
        with self._lock:
            if scan_id in self.scans:
                self.scans[scan_id]["findings"].append(finding)
                self.scans[scan_id]["done"] += 1

    def bump_progress(self, scan_id: str):
        """Count a hunt that finished with no alert (clean)."""
        with self._lock:
            if scan_id in self.scans:
                self.scans[scan_id]["done"] += 1
                self.scans[scan_id]["clean"] += 1

    def set_triage(self, hunt_id: str, status: str, note: str = "", who: str = "") -> dict:
        with self._lock:
            rec = {"status": status, "note": note, "updated_by": who}
            self.triage[hunt_id] = rec
            return dict(rec)

    def get_triage(self, hunt_id: str) -> dict:
        with self._lock:
            return dict(self.triage.get(hunt_id, {"status": "new", "note": "", "updated_by": ""}))


STORE = Store()


def _add_wall(scan_id: str, ms: float):
    """Accumulate wall-clock ms across all of a scan's queries (for instant self-cost)."""
    with STORE._lock:
        if scan_id in STORE.scans:
            STORE.scans[scan_id]["wall_ms"] = STORE.scans[scan_id].get("wall_ms", 0.0) + (ms or 0.0)


# ── severity + formatting ─────────────────────────────────────────────────────

def _severity(value: float, spec: dict) -> str:
    if value is None:
        return "info"
    if value >= spec["high"]:
        return "high"
    if value >= spec["medium"]:
        return "medium"
    return "low"


def _fmt_metric(value: float, unit: str) -> str:
    """Render a metric value for a headline, by unit."""
    if value is None:
        return "—"
    try:
        v = float(value)
    except (TypeError, ValueError):
        return str(value)
    if unit == "usd":
        return f"${v:,.0f}" if v >= 1 else f"${v:,.2f}"
    if unit == "ratio":
        return f"{v:,.0f}:1"
    if unit == "count":
        return f"{v:,.0f}"
    if unit == "tib":
        return f"{v:,.2f} TiB"
    if unit == "hours":
        return f"{v:,.1f} h"
    if unit == "minutes":
        h = v / 60.0
        return f"{v:,.0f} min ({h:,.1f} h)" if v >= 90 else f"{v:,.0f} min"
    return f"{v:,.2f}"


def _num(x) -> Optional[float]:
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def build_finding(hunt: dict, result: dict) -> Optional[dict]:
    """Turn one hunt's query result into an alert finding, or None if nothing
    crossed the medium threshold (a clean hunt raises no alert).

    `result` = {columns, rows, total_rows} from _run_sql (top rows already
    sorted worst-first by the hunt's own ORDER BY)."""
    spec = catalogue.ALERT_SPECS.get(hunt["id"])
    if not spec:
        return None
    cols, rows = result["columns"], result["rows"]
    if not rows:
        return None

    ci = {c: i for i, c in enumerate(cols)}
    mcol = spec["metric_col"]
    if mcol not in ci:
        return None

    top_val = _num(rows[0][ci[mcol]])
    sev = _severity(top_val, spec)
    if sev in ("low", "info"):
        # Below the medium bar → not worth an admin's queue. Clean-ish.
        return None

    total = result.get("total_rows", len(rows))
    metric_str = _fmt_metric(top_val, spec["unit"])
    summary = spec["summary"].format(n=total, metric=metric_str)

    # Real dollars only if the hunt genuinely has a $ column (else None — we show
    # the magnitude metric instead, never a fabricated cost).
    impact_usd = None
    if spec.get("usd_col") and spec["usd_col"] in ci:
        impact_usd = _num(rows[0][ci[spec["usd_col"]]])

    # Keep the top-5 rows as evidence (the queue card shows these; the full
    # result stays available on the Spot tab).
    evidence = {"columns": cols, "rows": rows[:5]}

    return {
        "hunt_id": hunt["id"],
        "title": hunt["title"],
        "category": hunt["category"],
        "severity": sev,
        "summary": summary,
        "metric_label": spec["metric_label"],
        "metric_value": metric_str,
        "metric_num": top_val,          # raw numeric — for reliable run-over-run comparison
        "impact_usd": impact_usd,
        "total_rows": total,
        "action": hunt["action"],          # the recommended fix (static, from the notebook)
        "measure": hunt.get("measure"),     # which Prove-tab measure shows before/after
        "eli5": hunt["eli5"],
        "evidence": evidence,
        "explanation": None,                # filled in by the AI pass during the scan
    }


def run_scan(scan_id: str, client, run_sql: Callable, ai_explain: Callable, bounded: Callable,
             logger, on_complete: Optional[Callable] = None) -> None:
    """Execute every alerting hunt, build findings, attach AI explanations.
    Runs on a background thread. Streams results into STORE as it goes so the
    frontend can render the queue progressively.

      run_sql(sql, client) -> {columns, rows, total_rows}
      ai_explain(rubric, hunt_desc, rows_sql, client) -> str
      bounded(sql, n) -> sql capped to top-n
      on_complete(scan_id, client) -> called after the scan finishes (persist to Delta)
    """
    alert_hunts = [h for h in catalogue.HUNTS if h["id"] in catalogue.ALERT_SPECS]
    # Record the scan's own start time (UTC) so we can attribute what THIS scan
    # cost afterwards (self-cost): every query it runs falls in [started_at, now).
    STORE.update_scan(scan_id, total=len(alert_hunts), status="running",
                      started_at=_utc_now_iso(), n_explains=0, wall_ms=0.0)

    import time as _time
    for h in alert_hunts:
        try:
            result = run_sql(h["sql"], client)
            _add_wall(scan_id, result.get("wall_ms", 0.0))
            finding = build_finding(h, result)
            if finding is None:
                STORE.bump_progress(scan_id)
                continue
            # Attach the plain-English explanation (bounded input, same as the
            # per-hunt explainer). If it fails, the finding still lands.
            try:
                if h.get("explain_rubric") and h.get("explain_hunt_desc"):
                    rows_sql = h.get("explain_sql") or bounded(h["sql"], 10)
                    _e0 = _time.monotonic()
                    finding["explanation"] = ai_explain(
                        h["explain_rubric"], h["explain_hunt_desc"], rows_sql, client)
                    _add_wall(scan_id, (_time.monotonic() - _e0) * 1000.0)
                    with STORE._lock:
                        if scan_id in STORE.scans:
                            STORE.scans[scan_id]["n_explains"] = STORE.scans[scan_id].get("n_explains", 0) + 1
            except Exception as e:  # noqa: BLE001
                logger.warning(f"scan {scan_id}: explain for {h['id']} failed: {e}")
                finding["explanation"] = None
            # Merge any existing triage decision so the queue reflects prior actions.
            finding["triage"] = STORE.get_triage(h["id"])
            STORE.append_finding(scan_id, finding)
        except Exception as e:  # noqa: BLE001
            logger.error(f"scan {scan_id}: hunt {h['id']} failed: {e}\n{traceback.format_exc()}")
            STORE.bump_progress(scan_id)  # count it so the scan can complete

    STORE.update_scan(scan_id, status="complete", ended_at=_utc_now_iso())
    logger.info(f"scan {scan_id} complete")
    # Persist to the shared Delta store (on this thread — it holds the OBO client).
    if on_complete:
        try:
            on_complete(scan_id, client)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"scan {scan_id}: on_complete (persist) failed: {e}")


def scan_view(scan_id: str) -> Optional[dict]:
    """The frontend-facing view of a scan's progress + findings, sorted by severity."""
    rec = STORE.get_scan(scan_id)
    if not rec:
        return None
    findings = sorted(
        rec["findings"],
        key=lambda f: (SEV_RANK.get(f["severity"], 9), -(f["impact_usd"] or 0)),
    )
    # refresh triage on each finding (an admin may have actioned mid-poll)
    for f in findings:
        f["triage"] = STORE.get_triage(f["hunt_id"])
    counts = {"high": 0, "medium": 0}
    for f in findings:
        counts[f["severity"]] = counts.get(f["severity"], 0) + 1
    return {
        "scan_id": scan_id,
        "status": rec["status"],
        "total": rec["total"],
        "done": rec["done"],
        "clean": rec["clean"],
        "findings": findings,
        "counts": counts,
        "started_by": rec.get("started_by", ""),
        "started_at": rec.get("started_at"),
        "ended_at": rec.get("ended_at"),
        "n_explains": rec.get("n_explains", 0),
        "wall_ms": rec.get("wall_ms", 0.0),
    }
