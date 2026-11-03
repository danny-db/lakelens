"""
scan.py — the FinOps workflow engine behind the Alerts tab.

This is what turns the app from "a prettier notebook" into a workflow:
one full scan runs every waste-signal hunt, and each result becomes a
pre-triaged ALERT — severity, a plain-English explanation, the recommended
fix, and which measure proves the before/after — dropped into a queue a
FinOps admin works (acknowledge → in progress → resolved / dismissed).

Design notes:
- **Severity is deterministic**: each row is graded by the rule for its spec's
  `kind` (see GRADERS and hunts.ALERT_SPECS), mostly "how many times this
  entity's own normal". The AI only narrates; it never sets severity. We never
  invent a dollar figure a query didn't produce.
- **Async**: a scan runs on a background thread so no single HTTP request is
  held past the Databricks Apps ~120s cap. The frontend polls and streams
  findings in as each hunt completes.
- **State is in-memory** (demo-scoped): scans by id, and triage status keyed by
  hunt id so an admin's decisions survive a re-scan. The STORE object is the
  only stateful thing — swap it for a Delta/Lakebase table to persist.
"""
import os
import threading
import traceback
from datetime import datetime, timezone
from typing import NamedTuple, Optional, Callable

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

    def new_scan_id(self) -> str:
        # Globally unique (uuid) — Databricks Apps runs MULTIPLE worker processes,
        # each with its own STORE, so a per-process counter collides (two workers
        # both mint 'scan-1') and a poll can hit a worker that never saw the id.
        # The uuid makes the id unique; the frontend also falls back to the shared
        # Delta-backed /api/scan/latest for the completed view (cross-worker safe).
        import uuid
        return "scan-" + uuid.uuid4().hex[:12]

    MAX_SCANS = 20

    def start_scan(self, record: dict) -> tuple[str, bool]:
        """Register a new scan and return (scan_id, True). If one is already
        starting or running, return (its id, False) instead: overlapping scans
        inflate each other's self-cost windows. Keeps the newest MAX_SCANS."""
        with self._lock:
            for sid, rec in self.scans.items():
                if rec["status"] in ("starting", "running"):
                    return sid, False
            sid = self.new_scan_id()
            self.scans[sid] = record
            while len(self.scans) > self.MAX_SCANS:
                del self.scans[next(iter(self.scans))]
            return sid, True

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

# An anomaly row is high at ANOMALY_HIGH_X times its entity's normal and medium at
# ANOMALY_MEDIUM_X. Read once at import; set them as bundle variables.
ANOMALY_HIGH_X = float(os.getenv("ANOMALY_HIGH_X", "3"))
ANOMALY_MEDIUM_X = float(os.getenv("ANOMALY_MEDIUM_X", "2"))


class Grade(NamedTuple):
    severity: str           # high | medium | low
    reading: str            # anomaly | new | presence | threshold
    x: Optional[float]      # vs_normal, for anomaly rows


def _grade_anomaly(spec: dict, row: list, ci: dict) -> Grade:
    x = _num(row[ci["vs_normal"]])
    if x is None:
        return Grade("medium", "new", None)
    if x >= ANOMALY_HIGH_X:
        return Grade("high", "anomaly", x)
    if x >= ANOMALY_MEDIUM_X:
        return Grade("medium", "anomaly", x)
    return Grade("low", "anomaly", x)


def _grade_presence(spec: dict, row: list, ci: dict) -> Grade:
    return Grade("medium", "presence", None)


def _grade_threshold(spec: dict, row: list, ci: dict) -> Grade:
    v = _num(row[ci[spec["metric_col"]]])
    if v is not None and v >= spec["high"]:
        return Grade("high", "threshold", None)
    if v is not None and v >= spec["medium"]:
        return Grade("medium", "threshold", None)
    return Grade("low", "threshold", None)


GRADERS: dict[str, Callable[[dict, list, dict], Grade]] = {
    "anomaly": _grade_anomaly,
    "presence": _grade_presence,
    "threshold": _grade_threshold,
}

# Columns each kind needs in the hunt result, beyond the spec's own column names.
REQUIRED_COLS = {"anomaly": ("vs_normal", "baseline"), "presence": (), "threshold": ()}


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


def _headline(spec: dict, g: Grade, row: list, ci: dict, total: int, metric_str: str,
              baseline_str: Optional[str]) -> str:
    entity = str(row[ci[spec["entity_col"]]]) if spec.get("entity_col") in ci else ""
    if g.reading == "new":
        return spec["new_summary"].format(entity=entity, metric=metric_str, n=total)
    x = f"{g.x:,.1f}x" if g.x is not None else ""
    return spec["summary"].format(entity=entity, x=x, baseline=baseline_str, metric=metric_str, n=total)


def build_finding(hunt: dict, result: dict) -> Optional[dict]:
    """Turn one hunt's query result into an alert finding, or None if no row
    grades medium or worse (a clean hunt raises no alert).

    `result` = {columns, rows, total_rows} from _run_sql. Every row is graded;
    the headline row is the first row with the worst severity, so the hunt's
    ORDER BY breaks ties."""
    spec = catalogue.ALERT_SPECS.get(hunt["id"])
    if not spec:
        return None
    cols, rows = result["columns"], result["rows"]
    if not rows:
        return None

    ci = {c: i for i, c in enumerate(cols)}
    needed = [spec.get("metric_col"), spec.get("entity_col"), *REQUIRED_COLS[spec["kind"]]]
    if any(c and c not in ci for c in needed):
        return None

    grader = GRADERS[spec["kind"]]
    graded = [grader(spec, r, ci) for r in rows]
    top_i = min(range(len(rows)), key=lambda i: SEV_RANK[graded[i].severity])
    top, g = rows[top_i], graded[top_i]
    if g.severity == "low":
        return None

    total = result.get("total_rows", len(rows))
    mcol = spec.get("metric_col")
    top_val = _num(top[ci[mcol]]) if mcol else None
    metric_str = _fmt_metric(top_val, spec["unit"]) if mcol else ""
    baseline_str = (_fmt_metric(_num(top[ci["baseline"]]), spec["unit"])
                    if g.reading == "anomaly" else None)
    summary = _headline(spec, g, top, ci, total, metric_str, baseline_str)

    # Real dollars only if the hunt genuinely has a $ column (else None — we show
    # the magnitude metric instead, never a fabricated cost).
    impact_usd = None
    if spec.get("usd_col") and spec["usd_col"] in ci:
        impact_usd = _num(top[ci[spec["usd_col"]]])

    # Keep the top-5 rows as evidence (the queue card shows these; the full
    # result stays available on the Spot tab).
    evidence = {"columns": cols, "rows": rows[:5]}

    return {
        "hunt_id": hunt["id"],
        "title": hunt["title"],
        "category": hunt["category"],
        "severity": g.severity,
        "summary": summary,
        "metric_label": spec["metric_label"],
        "metric_value": metric_str,
        "metric_num": top_val,          # raw numeric — for reliable run-over-run comparison
        # How the headline row compares with its own normal (the card's badge).
        "vs": {"reading": g.reading, "x": g.x, "baseline": baseline_str},
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
