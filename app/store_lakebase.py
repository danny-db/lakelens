"""
store_lakebase.py — Lakebase (managed Postgres) persistence for scan runs, findings, triage.

Drop-in replacement for store_delta.DeltaStore (same method signatures), chosen by
app.py when LAKEBASE_INSTANCE is set. Why Lakebase over Delta for this data:
  - Point reads/writes of a few hundred rows: Postgres answers in milliseconds, a
    warehouse needs seconds (and a cold one much longer — the "Finalising…" stall).
  - Real transactions: a scan's findings + run row commit atomically.
  - Triage upserts can't conflict the way concurrent Delta MERGEs did.

The app's OWN identity (service principal when deployed, CLI profile locally) owns
the tables — one shared history for every user, with no per-user Lakebase grants.
Postgres passwords are short-lived OAuth tokens from generate_database_credential
(~1h), so every operation opens a fresh connection instead of pooling.

Everything degrades gracefully like the Delta store: on any failure the method
returns False/[]/None, last_error is set, and the caller falls back to memory.
"""
import json
import logging
from typing import Callable, Optional

import psycopg

from store_delta import _f, _with_current_text   # shared row shaping with the Delta store

logger = logging.getLogger("uvicorn.error")

# Mirrors the Delta schema (store_delta.py), Postgres types. TIMESTAMP WITHOUT TIME
# ZONE: the app writes naive UTC strings ("2026-11-19 11:54:07") and reads them back
# unchanged, exactly like the Delta store.
_DDL = """
CREATE SCHEMA IF NOT EXISTS {schema};
CREATE TABLE IF NOT EXISTS {schema}.scan_runs (
  scan_id TEXT PRIMARY KEY,
  started_at TIMESTAMP, ended_at TIMESTAMP, started_by TEXT,
  total INTEGER, findings INTEGER, high INTEGER, medium INTEGER, clean INTEGER,
  scan_seconds DOUBLE PRECISION,
  dbsql_usd DOUBLE PRECISION, ai_usd DOUBLE PRECISION, total_usd DOUBLE PRECISION
);
CREATE TABLE IF NOT EXISTS {schema}.scan_findings (
  scan_id TEXT, started_at TIMESTAMP, hunt_id TEXT, title TEXT, category TEXT,
  severity TEXT, summary TEXT, metric_label TEXT, metric_value TEXT,
  metric_num DOUBLE PRECISION, impact_usd DOUBLE PRECISION, total_rows BIGINT,
  measure TEXT, action TEXT, explanation TEXT, evidence_json TEXT,
  PRIMARY KEY (scan_id, hunt_id)
);
CREATE TABLE IF NOT EXISTS {schema}.finding_triage (
  hunt_id TEXT PRIMARY KEY, status TEXT, note TEXT, updated_by TEXT,
  updated_at TIMESTAMP
);
"""


class LakebaseStore:
    """Persists scans to a Lakebase instance. `ws_factory` builds the WorkspaceClient
    used for host discovery + credential generation (the app's own identity). The
    `client` argument every method takes is ignored — it exists for interface parity
    with DeltaStore, whose SQL runs as the calling user."""

    def __init__(self, instance: str, database: str, schema: str, ws_factory: Callable):
        self.instance = instance
        self.database = database
        self.schema = schema
        self._ws_factory = ws_factory
        self._ws = None
        self._host: Optional[str] = None
        self._user: Optional[str] = None
        self.ready = False
        self.last_error: Optional[str] = None

    # ── connection ────────────────────────────────────────────────────────────
    def _client(self):
        if self._ws is None:
            self._ws = self._ws_factory()
        return self._ws

    def _connect(self):
        """Fresh connection per operation: the credential is an OAuth token that
        expires (~1h), so nothing here is cached except the host and the role name."""
        w = self._client()
        if not self._host:
            self._host = w.database.get_database_instance(self.instance).read_write_dns
        if not self._user:
            # Identities auto-provision a Postgres role named after their Databricks
            # identity (email for users, the SP's name for service principals).
            self._user = w.current_user.me().user_name
        cred = w.database.generate_database_credential(instance_names=[self.instance])
        return psycopg.connect(host=self._host, dbname=self.database, user=self._user,
                               password=cred.token, sslmode="require", connect_timeout=15)

    # ── schema bootstrap ──────────────────────────────────────────────────────
    def ensure_tables(self, client=None) -> bool:
        """Create the schema + 3 tables if absent. Returns True if writable."""
        if not self.instance:
            self.last_error = "LAKEBASE_INSTANCE not set"
            return False
        try:
            with self._connect() as conn:
                with conn.cursor() as cur:
                    for stmt in _DDL.format(schema=self.schema).split(";"):
                        if stmt.strip():
                            cur.execute(stmt)
                conn.commit()
            self.ready = True
            return True
        except Exception as e:  # noqa: BLE001
            self.last_error = str(e)[:300]
            logger.warning(f"LakebaseStore.ensure_tables failed (falling back to memory): {e}")
            self.ready = False
            return False

    # ── persist a completed scan ──────────────────────────────────────────────
    def save_scan(self, client, view: dict) -> bool:
        """Write one scan's finding rows + run row in ONE transaction — readers can
        never see a half-written run (the Delta store orders writes for this instead)."""
        if not self.ensure_tables():
            return False
        try:
            counts = view.get("counts", {})
            with self._connect() as conn:
                with conn.cursor() as cur:
                    for f in view.get("findings", []):
                        cur.execute(f"""
                            INSERT INTO {self.schema}.scan_findings VALUES
                            (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                            ON CONFLICT (scan_id, hunt_id) DO NOTHING""",
                            (view["scan_id"], view.get("started_at"), f["hunt_id"],
                             f.get("title", ""), f.get("category", ""), f.get("severity", ""),
                             f.get("summary", ""), f.get("metric_label", ""), f.get("metric_value", ""),
                             f.get("metric_num"), f.get("impact_usd"), f.get("total_rows", 0),
                             f.get("measure") or "", f.get("action", ""), f.get("explanation") or "",
                             json.dumps({**f.get("evidence", {}), "vs": f.get("vs"),
                                        "lens": f.get("lens"), "why": f.get("why")})[:60000]))
                    cur.execute(f"""
                        INSERT INTO {self.schema}.scan_runs VALUES
                        (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (scan_id) DO NOTHING""",
                        (view["scan_id"], view.get("started_at"), view.get("ended_at"),
                         view.get("started_by") or "", view.get("total", 0),
                         len(view.get("findings", [])), counts.get("high", 0),
                         counts.get("medium", 0), view.get("clean", 0),
                         round(float(view.get("wall_ms") or 0) / 1000.0, 1),
                         None, None, None))
                conn.commit()
            logger.info(f"LakebaseStore: persisted scan {view['scan_id']} ({len(view.get('findings',[]))} findings)")
            return True
        except Exception as e:  # noqa: BLE001
            self.last_error = str(e)[:300]
            logger.error(f"LakebaseStore.save_scan failed: {e}")
            return False

    def save_cost(self, client, scan_id: str, cost: dict) -> bool:
        """Attach a saved scan's self-cost (dbsql_usd, ai_usd, total_usd) to its run row."""
        try:
            with self._connect() as conn:
                with conn.cursor() as cur:
                    cur.execute(f"""
                        UPDATE {self.schema}.scan_runs
                        SET dbsql_usd = %s, ai_usd = %s, total_usd = %s
                        WHERE scan_id = %s""",
                        (cost.get("dbsql_usd"), cost.get("ai_usd"), cost.get("total_usd"), scan_id))
                conn.commit()
            return True
        except Exception as e:  # noqa: BLE001
            self.last_error = str(e)[:300]
            logger.warning(f"LakebaseStore.save_cost failed: {e}")
            return False

    # ── read the latest persisted scan (+ the one before, for comparison) ──────
    def latest_two_run_ids(self, client) -> list[str]:
        if not self.ensure_tables():
            return []
        try:
            with self._connect() as conn:
                with conn.cursor() as cur:
                    cur.execute(f"SELECT scan_id FROM {self.schema}.scan_runs ORDER BY started_at DESC LIMIT 2")
                    return [r[0] for r in cur.fetchall()]
        except Exception as e:  # noqa: BLE001
            logger.warning(f"LakebaseStore.latest_two_run_ids failed: {e}")
            return []

    def load_run(self, client, scan_id: str) -> Optional[dict]:
        """Rebuild a scan_view()-shaped dict from Lakebase for a given run."""
        try:
            with self._connect() as conn:
                with conn.cursor() as cur:
                    cur.execute(f"""
                        SELECT scan_id, started_at, ended_at, started_by, total, findings,
                               high, medium, clean, scan_seconds, dbsql_usd, ai_usd, total_usd
                        FROM {self.schema}.scan_runs WHERE scan_id = %s""", (scan_id,))
                    r = cur.fetchone()
                    if not r:
                        return None
                    cur.execute(f"""
                        SELECT hunt_id, title, category, severity, summary, metric_label,
                               metric_value, impact_usd, total_rows, measure, action,
                               explanation, evidence_json, metric_num
                        FROM {self.schema}.scan_findings WHERE scan_id = %s""", (scan_id,))
                    rows = cur.fetchall()
            findings = []
            for x in rows:
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
            ts = lambda v: v.strftime("%Y-%m-%d %H:%M:%S") if v else None   # the string shape the UI parses
            return {
                "scan_id": r[0], "started_at": ts(r[1]), "ended_at": ts(r[2]),
                "started_by": r[3], "total": int(r[4] or 0),
                "counts": {"high": int(r[6] or 0), "medium": int(r[7] or 0)},
                "clean": int(r[8] or 0), "scan_seconds": _f(r[9]),
                "cost": {"dbsql_usd": _f(r[10]), "ai_usd": _f(r[11]), "total_usd": _f(r[12])},
                "findings": findings, "persisted": True,
            }
        except Exception as e:  # noqa: BLE001
            logger.warning(f"LakebaseStore.load_run failed: {e}")
            return None

    # ── shared triage ──────────────────────────────────────────────────────────
    def set_triage(self, client, hunt_id: str, status: str, note: str, who: str) -> bool:
        """Upsert this hunt's triage. One atomic statement, and unlike Delta there's
        no concurrent-write conflict to retry."""
        if not self.ensure_tables():
            return False
        try:
            with self._connect() as conn:
                with conn.cursor() as cur:
                    cur.execute(f"""
                        INSERT INTO {self.schema}.finding_triage (hunt_id, status, note, updated_by, updated_at)
                        VALUES (%s, %s, %s, %s, now()::timestamp)
                        ON CONFLICT (hunt_id) DO UPDATE
                        SET status = EXCLUDED.status, note = EXCLUDED.note,
                            updated_by = EXCLUDED.updated_by, updated_at = EXCLUDED.updated_at""",
                        (hunt_id, status, note or "", who or ""))
                conn.commit()
            return True
        except Exception as e:  # noqa: BLE001
            self.last_error = str(e)[:300]
            logger.warning(f"LakebaseStore.set_triage failed: {e}")
            return False

    def all_triage(self, client) -> dict[str, dict]:
        if not self.ensure_tables():
            return {}
        try:
            with self._connect() as conn:
                with conn.cursor() as cur:
                    cur.execute(f"SELECT hunt_id, status, note, updated_by FROM {self.schema}.finding_triage")
                    return {r[0]: {"status": r[1], "note": r[2] or "", "updated_by": r[3] or ""}
                            for r in cur.fetchall()}
        except Exception:  # noqa: BLE001
            return {}
