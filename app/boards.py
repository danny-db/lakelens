"""
boards.py — the Observability section: four "boards" (Usage, Jobs, DBSQL,
Model Serving), each a set of panels. A panel is one query rendered as a
chart or table.

The point of this section (per the brief) is NOT to draw pretty graphs — it
is to SPOT THE ANOMALY. So every panel can carry an `anomaly` spec, and the
backend runs a deterministic detector over the panel's own rows and returns
plain-English callouts that render ABOVE the chart. The rule is the same one
the Alerts engine uses: code detects (deterministic, explainable, no
hallucinated numbers); the AI only ever narrates what the rule found.

All SQL is grounded in columns probed live on the workspace (see the schema-gotchas
memory): clusters uses dbr_version / worker_count / min|max_autoscale_workers
(NOT spark_version/num_workers); node_timeline uses mem_used_percent and
cpu_user_percent+cpu_system_percent; result_state ∈ ERROR/FAILED/TIMED_OUT/
CANCELLED; model-serving type comes from product_features.serving_type +
model_serving.offering_type + usage_type.

Same verbatim-when-possible rule as hunts.py. Where a public source existed
(model-serving = tiantan32 dashboard; DBSQL = the cost-per-query PrPr; usage/
forecast = Account Usage Dashboard v2) the core logic is transcribed and only
de-parameterised (the dashboards are parameter-bound; the app is not).
"""
from typing import NamedTuple, Optional

from names import JOB, WORKSPACE, ctes, ref
from scan import ANOMALY_HIGH_X, ANOMALY_MEDIUM_X

# ── Anomaly detectors ─────────────────────────────────────────────────────────
# Each is pure Python over the panel's returned rows. They return a list of
# callout dicts: {severity, headline, detail}. No detector invents a number the
# query didn't produce — it only compares values already in the result.
# Every detector reduces a row to "how many times its normal" and grades that
# multiple with the same cut-offs as the Alerts engine, so no detector carries
# a dollar, count or percentage of its own and an account of any size reads
# the same way.

def _severity(x: Optional[float]) -> Optional[str]:
    if x is None:
        return None
    if x >= ANOMALY_HIGH_X:
        return "high"
    if x >= ANOMALY_MEDIUM_X:
        return "medium"
    return None


def _f(x) -> Optional[float]:
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _median(xs):
    s = sorted(xs)
    n = len(s)
    if not n:
        return None
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2.0


def _detect(spec: dict, cols: list, rows: list) -> list:
    """Dispatch to the right detector. Returns [] on any shape mismatch."""
    if not spec or not rows:
        return []
    ci = {c: i for i, c in enumerate(cols)}
    kind = spec.get("type")
    try:
        if kind == "ts_spike":
            return _ts_spike(spec, ci, rows)
        if kind == "rank_outlier":
            return _rank_outlier(spec, ci, rows)
        if kind == "new_entity":
            return _new_entity(spec, ci, rows)
        if kind == "vs_typical":
            return _vs_typical(spec, ci, rows)
        if kind == "decomp_driver":
            return _decomp_driver(spec, ci, rows)
    except Exception:  # a detector must never break a panel
        return []
    return []


def _decomp_driver(spec, ci, rows):
    """Read the ΔCost decomposition (one row) and lead with the DOMINANT driver of the
    week-over-week change — so the callout says WHY, not just how much. It speaks only
    when the change is unusual for this account: its size against typical_change_usd,
    the median absolute weekly change before it."""
    r = rows[0]
    def g(c): return _f(r[ci[c]]) if c in ci else None
    delta, typical = g("delta_usd"), g("typical_change_usd")
    if delta is None or not typical:
        return []
    comps = [("new workloads", g("new_workloads_usd")),
             ("retired workloads", g("retired_workloads_usd")),
             ("volume growth (existing workloads doing more)", g("volume_growth_usd")),
             ("rate / mix (pricier compute or SKU mix)", g("rate_mix_usd"))]
    comps = [(n, v) for n, v in comps if v is not None]
    x = abs(delta) / typical
    sev = _severity(x)
    if not comps or not sev:
        return []
    # dominant driver = the component with the largest ABSOLUTE contribution
    name, val = max(comps, key=lambda c: abs(c[1]))
    up = delta >= 0
    share = (abs(val) / abs(delta) * 100) if delta else 0
    breakdown = ", ".join(f"{n.split(' (')[0]} {_fmt(v, 'usd')}" for n, v in comps)
    return [{
        "severity": sev,
        "headline": f"Cost {'rose' if up else 'fell'} {_fmt(abs(delta), 'usd')} week over week, "
                    f"{x:.1f}× a typical weekly change of {_fmt(typical, 'usd')}. Mostly {name} "
                    f"({_fmt(val, 'usd')}, {share:.0f}% of the change)",
        "detail": f"Breakdown: {breakdown}. These four sum to the total change exactly. "
                  f"{'A rise driven by NEW workloads is expected growth — attribute and budget it, not cut. ' if up and 'new' in name else ''}"
                  f"{'A rise from RATE/MIX means the same work got pricier — check compute type / SKU. ' if up and 'rate' in name else ''}"
                  f"{'A rise from VOLUME means existing workloads are doing more — check for runaway growth. ' if up and 'volume' in name else ''}",
    }]


def _ts_spike(spec, ci, rows):
    """The latest period against each series' own normal: the median of its prior
    periods. The row shape says what a series is:
      - (period, group, value) with `group_col`: the groups are slices of one bill,
        so a slice's normal is floored at the typical slice-period (the median over
        every group's prior periods). A big multiple on a tiny line item is not a
        cost problem (accuracy over noise), and the floor scales with the account.
      - (period, value) with `value_col`, or (period, a, b, ...) with `value_cols`:
        each column is its own account-level series, judged only against itself.
    Callouts are ranked by absolute movement, so the biggest mover leads."""
    tcol, gcol, unit = spec["time_col"], spec.get("group_col"), spec.get("unit", "usd")
    vcols = spec.get("value_cols") or [spec["value_col"]]
    if tcol not in ci or (gcol and gcol not in ci) or any(c not in ci for c in vcols):
        return []
    series = {}
    for r in rows:
        for vc in vcols:
            v = _f(r[ci[vc]])
            if v is not None:
                key = r[ci[gcol]] if gcol else (vc if len(vcols) > 1 else None)
                series.setdefault(key, []).append((str(r[ci[tcol]]), v))
    periods = sorted({t for pts in series.values() for t, _ in pts})
    if len(periods) < 2:
        return []
    latest = periods[-1]
    peer = _median([v for pts in series.values() for t, v in pts if t != latest]) if gcol else None
    outs = []
    for g, pts in series.items():
        pts.sort(key=lambda p: p[0])
        hist = [v for t, v in pts if t != latest]
        # Three prior periods at least, so one partial first period can't be the median.
        if pts[-1][0] != latest or len(hist) < 3:
            continue
        own = _median(hist)
        normal = max(own, peer) if peer is not None else own
        last_v = pts[-1][1]
        x = last_v / normal if normal > 0 else None
        sev = _severity(x)
        if not sev:
            continue
        gl = "" if g is None else f"“{g}” "
        basis = (f"floored at the typical {gcol}'s {tcol} ({_fmt(peer, unit)}) because its own "
                 f"median is only {_fmt(own, unit)}" if normal != own else
                 f"the median of its {len(hist)} prior {tcol}s")
        outs.append({
            "severity": sev,
            "_delta": last_v - normal,   # sort key (dropped before returning)
            "headline": f"{gl}spiked to {_fmt(last_v, unit)}, {x:.1f}× its normal "
                        f"(up {_fmt(last_v - normal, unit)})",
            "detail": f"Latest period {latest}: {_fmt(last_v, unit)} against a normal of "
                      f"{_fmt(normal, unit)}, {basis}. Worth checking what changed.",
        })
    outs.sort(key=lambda o: -o["_delta"])
    for o in outs:
        o.pop("_delta", None)
    return outs[:6]


def _rank_outlier(spec, ci, rows):
    """The largest row against the next-largest. Cost spreads are long-tailed, so the
    top item always dwarfs the median of the rest; one item several times the
    runner-up is the single dominant outlier worth a callout."""
    vcol, lcol = spec["value_col"], spec.get("label_col")
    unit = spec.get("unit", "usd")
    if vcol not in ci:
        return []
    vals = [(_f(r[ci[vcol]]), (r[ci[lcol]] if lcol and lcol in ci else "top item")) for r in rows]
    vals = sorted(((v, l) for v, l in vals if v is not None), key=lambda p: -p[0])
    if len(vals) < 2 or vals[1][0] <= 0:
        return []
    (top_v, top_l), (next_v, next_l) = vals[0], vals[1]
    x = top_v / next_v
    sev = _severity(x)
    if not sev:
        return []
    share = top_v / sum(v for v, _ in vals) * 100
    return [{
        "severity": sev,
        "headline": f"“{top_l}” is {x:.1f}× the next-largest, a dominant cost outlier",
        "detail": f"Top item {_fmt(top_v, unit)} against {_fmt(next_v, unit)} for “{next_l}”, "
                  f"{share:.0f}% of the rows shown. Concentrated cost is the highest-leverage "
                  f"place to look first.",
    }]


def _new_entity(spec, ci, rows):
    """Entities present in the latest period but absent in prior ones — new cost
    that wasn't there before (expected growth, or a surprise to attribute)."""
    tcol, gcol, vcol = spec["time_col"], spec["group_col"], spec["value_col"]
    unit = spec.get("unit", "usd")
    min_abs = spec.get("min_abs", 0)
    if any(c not in ci for c in (tcol, gcol, vcol)):
        return []
    periods = sorted({str(r[ci[tcol]]) for r in rows})
    if len(periods) < 2:
        return []
    latest = periods[-1]
    prior = set()
    latest_vals = {}
    for r in rows:
        t = str(r[ci[tcol]])
        g = r[ci[gcol]]
        v = _f(r[ci[vcol]]) or 0
        if t == latest:
            latest_vals[g] = latest_vals.get(g, 0) + v
        else:
            prior.add(g)
    new = [(g, v) for g, v in latest_vals.items() if g not in prior and v >= min_abs and g is not None]
    new.sort(key=lambda x: -x[1])
    if not new:
        return []
    top = new[:4]
    names = ", ".join(f"“{g}” ({_fmt(v, unit)})" for g, v in top)
    return [{
        "severity": "medium",
        "headline": f"{len(new)} new item{'s' if len(new) != 1 else ''} appeared this period",
        "detail": f"Not present in prior periods: {names}. New workloads are expected growth — "
                  f"just make sure each has an owner and a budget.",
    }]


def _vs_typical(spec, ci, rows):
    """Rows the SQL scored against the account's typical item: `x_col` is how many
    times worse than typical each row is, and `typical_col` the typical value. The
    headline and detail templates come from the panel, which knows what worse means."""
    xcol, tcol, lcol, vcol = spec["x_col"], spec["typical_col"], spec["label_col"], spec["value_col"]
    unit = spec.get("unit", "pct")
    if any(c not in ci for c in (xcol, tcol, lcol, vcol)):
        return []
    scored = [(_f(r[ci[xcol]]), r) for r in rows]
    flagged = sorted(((x, r) for x, r in scored if _severity(x)), key=lambda p: -p[0])
    if not flagged:
        return []
    x, worst = flagged[0]
    fill = {"n": len(flagged), "frac": f"1/{ANOMALY_MEDIUM_X:g}", "x": f"{x:.1f}×",
            "typical": _fmt(_f(worst[ci[tcol]]), unit), "worst": worst[ci[lcol]],
            "value": _fmt(_f(worst[ci[vcol]]), unit)}
    return [{
        "severity": _severity(x),
        "headline": spec["headline"].format(**fill),
        "detail": spec["detail"].format(**fill),
    }]


def _fmt(v, unit):
    if v is None:
        return "—"
    if unit == "usd":
        # unit costs (per query) live in fractions of a cent; two decimals would hide them
        return f"${v:,.0f}" if abs(v) >= 1 else f"${v:,.2f}" if abs(v) >= 0.1 else f"${v:,.4f}"
    if unit == "pct":
        return f"{v:,.1f}%"
    if unit == "tokens":
        return f"{v:,.0f} tok"
    if unit == "dbu":
        return f"{v:,.0f} DBU"
    return f"{v:,.0f}"


def compute_anomalies(panel: dict, columns: list, rows: list) -> list:
    return _detect(panel.get("anomaly"), columns, rows)


# ── Unit economics: shared SQL ───────────────────────────────────────────────
# Every unit-economics query reads one window. The newest billing day is still
# loading, so the window ends the day before it; query history and audit use the
# same dates. A row is priced at the account's own price when one exists for its
# SKU, otherwise at list, so a missing price never turns into an invented discount.
# account_prices is one CTE so a test can swap in synthetic prices. The window
# bounds appear as scalar subqueries only in WHERE (they still prune there); inside
# an expression they cost Photon a slow path, so rows carry days_back instead.

def _econ_with(days: int) -> str:
    return f"""WITH bounds AS (SELECT date_sub(max(usage_date), 1) AS end_date FROM system.billing.usage),
win AS (SELECT date_sub(end_date, {days - 1}) AS start_date, end_date FROM bounds),
account_prices AS (
  SELECT sku_name, cloud, currency_code, usage_unit, price_start_time, price_end_time,
         pricing.default AS unit_price
  FROM system.billing.account_prices
),
priced AS (
  SELECT u.workspace_id, u.billing_origin_product AS product,
         datediff(w.end_date, u.usage_date) AS days_back,
         u.product_features.is_serverless AS is_serverless,
         u.product_features.sql_tier AS sql_tier,
         u.usage_quantity * lp.pricing.effective_list.default AS list_usd,
         u.usage_quantity * ap.unit_price AS acct_usd
  FROM system.billing.usage u
  CROSS JOIN win w
  LEFT JOIN system.billing.list_prices lp
    ON u.sku_name = lp.sku_name AND u.cloud = lp.cloud AND u.usage_unit = lp.usage_unit
   AND u.usage_start_time >= lp.price_start_time
   AND (lp.price_end_time IS NULL OR u.usage_start_time < lp.price_end_time)
  LEFT JOIN account_prices ap
    ON u.sku_name = ap.sku_name AND u.cloud = ap.cloud AND u.usage_unit = ap.usage_unit
   AND ap.currency_code = lp.currency_code
   AND u.usage_start_time >= ap.price_start_time
   AND (ap.price_end_time IS NULL OR u.usage_start_time < ap.price_end_time)
  WHERE u.usage_date BETWEEN (SELECT start_date FROM win) AND (SELECT end_date FROM win)
)"""


_ECON_ACTIVITY = """,
queries AS (   -- finished queries per workspace and day; read_bytes summed as DOUBLE (the BIGINT sum overflows)
  SELECT q.workspace_id, datediff(w.end_date, to_date(q.start_time)) AS days_back,
         count(*) AS queries, sum(CAST(q.read_bytes AS DOUBLE)) AS read_bytes,
         count(q.compute.warehouse_id) AS sql_queries,
         sum(CASE WHEN q.compute.warehouse_id IS NOT NULL THEN CAST(q.read_bytes AS DOUBLE) END) AS sql_read_bytes
  FROM system.query.history q CROSS JOIN win w
  WHERE q.execution_status = 'FINISHED'
    AND q.start_time >= (SELECT start_date FROM win) AND q.start_time < (SELECT date_add(end_date, 1) FROM win)
  GROUP BY 1, 2
),
ids AS (   -- each audit identity once per workspace and day; an email is a person, a UUID a service principal
  SELECT a.workspace_id, datediff(w.end_date, a.event_date) AS days_back, a.user_identity.email AS identity
  FROM system.access.audit a CROSS JOIN win w
  WHERE a.event_date BETWEEN (SELECT start_date FROM win) AND (SELECT end_date FROM win)
    AND a.user_identity.email IS NOT NULL
  GROUP BY 1, 2, 3
)"""


class Kpi(NamedTuple):
    """One row of the unit-economics table, as SQL over the one-row `m` CTE."""
    metric: str
    value: str        # at list price, or the raw count
    discounted: str   # at the account's own prices; '' where price doesn't apply
    coverage: str     # which workspaces the figure rests on
    definition: str


def _usd(expr: str, dp: int = 0) -> str:
    return f"concat('$', format_number({expr}, {dp}))"


def _at_account_prices(expr: str, dp: int = 0) -> str:
    return (f"CASE WHEN m.priced_list_usd IS NULL THEN 'unavailable: no account prices' "
            f"ELSE concat({_usd(expr, dp)}, CASE WHEN m.unpriced_rows > 0 THEN ' (partial)' ELSE '' END) END")


def _share(part: str, whole: str, of: str) -> str:
    return f"concat(format_number(100 * {part} / {whole}, 1), '% of {of}')"


def _count(expr: str, suffix: str = "") -> str:
    return f"concat(format_number({expr}, 0), '{suffix}')"


ECON_KPIS = [
    Kpi("Window", "concat(m.start_date, ' to ', m.end_date)", "''", "''",
        "28 full days before the newest billing day"),
    Kpi("Total spend", _usd("m.list_usd"), _at_account_prices("m.net_usd"),
        _count("m.workspaces", " workspaces"), "Usage × price, every workspace"),
    Kpi("Effective discount", "''",
        "CASE WHEN m.priced_list_usd IS NULL THEN 'unavailable: no account prices' ELSE concat("
        "format_number(100 * (1 - m.priced_acct_usd / m.priced_list_usd), 1), '%', "
        "CASE WHEN m.unpriced_rows > 0 THEN ' (partial)' ELSE '' END) END",
        _share("coalesce(m.priced_list_usd, 0)", "m.list_usd", "list spend priced"),
        "1 - account $ / list $, on priced SKUs"),
    Kpi("Avg daily spend, last 7 days", _usd("m.daily_list_usd"), _at_account_prices("m.daily_net_usd"),
        "''", "Spend in the last 7 days of the window / 7"),
    Kpi("Active products", _count("m.products"), "''", "''", "Billing products with spend"),
    Kpi("MAU, all identities (approx)", _count("m.mau"), "''", _count("m.a_workspaces", " workspaces in audit"),
        "Distinct audit identities, 28 days"),
    Kpi("MAU, humans (approx)", _count("m.mau_human"), "''", _count("m.a_workspaces", " workspaces in audit"),
        "Audit identities that are emails"),
    Kpi("WAU (approx)", _count("m.wau"), "''", "''", "Distinct audit identities, last 7 days"),
    Kpi("DAU (approx)", _count("m.dau"), "''", "''", "Distinct audit identities, last day"),
    Kpi("Finished queries", _count("m.queries"), "''", _count("m.q_workspaces", " workspaces with queries"),
        "query.history rows, status FINISHED"),
    Kpi("Data read", _count("m.read_tib", " TiB"), "''", "''", "read_bytes of finished queries"),
    Kpi("DBSQL queries", _count("m.sql_queries"), "''", _count("m.sql_workspaces", " workspaces with DBSQL"),
        "Finished queries on a SQL warehouse"),
    Kpi("DBSQL data read", _count("m.sql_read_tib", " TiB"), "''", "''", "read_bytes of those queries"),
    Kpi("a. Cost per query", _usd("m.q_list_usd / m.queries", 4), _at_account_prices("m.q_net_usd / m.queries", 4),
        _share("m.q_list_usd", "m.list_usd", "spend"), "Query-workspace spend / queries"),
    Kpi("b. Cost per MAU, all", _usd("m.a_list_usd / m.mau", 2), _at_account_prices("m.a_net_usd / m.mau", 2),
        _share("m.a_list_usd", "m.list_usd", "spend"), "Audit-workspace spend / MAU"),
    Kpi("c. Cost per MAU, human", _usd("m.a_list_usd / m.mau_human", 2),
        _at_account_prices("m.a_net_usd / m.mau_human", 2),
        _share("m.a_list_usd", "m.list_usd", "spend"), "Audit-workspace spend / human MAU"),
    Kpi("d. Cost per TiB read", _usd("m.q_list_usd / m.read_tib", 2), _at_account_prices("m.q_net_usd / m.read_tib", 2),
        _share("m.q_list_usd", "m.list_usd", "spend"), "Query-workspace spend / TiB read"),
    Kpi("e. DBSQL cost per TiB read", _usd("m.dbsql_list_usd / m.sql_read_tib", 2),
        _at_account_prices("m.dbsql_net_usd / m.sql_read_tib", 2),
        _share("m.dbsql_list_usd", "m.sql_list_usd", "SQL spend"), "DBSQL-workspace SQL spend / TiB read"),
]


def _sql_str(s: str) -> str:
    assert "'" not in s, s   # Spark SQL has no '' escape; keep KPI text free of quotes
    return f"'{s}'"


_ECON_KPI_SQL = _econ_with(28) + _ECON_ACTIVITY + """,
spend AS (   -- per workspace
  SELECT workspace_id,
         sum(list_usd) AS list_usd,
         sum(coalesce(acct_usd, list_usd)) AS net_usd,
         sum(CASE WHEN acct_usd IS NOT NULL THEN list_usd END) AS priced_list_usd,
         sum(acct_usd) AS priced_acct_usd,
         count(CASE WHEN acct_usd IS NULL AND list_usd > 0 THEN 1 END) AS unpriced_rows,
         sum(CASE WHEN product = 'SQL' THEN list_usd END) AS sql_list_usd,
         sum(CASE WHEN product = 'SQL' THEN coalesce(acct_usd, list_usd) END) AS sql_net_usd,
         sum(CASE WHEN floor(days_back / 7) = 0 THEN list_usd END) AS week_list_usd,
         sum(CASE WHEN floor(days_back / 7) = 0 THEN coalesce(acct_usd, list_usd) END) AS week_net_usd,
         collect_set(CASE WHEN list_usd > 0 THEN product END) AS products
  FROM priced GROUP BY 1
),
q AS (   -- per workspace
  SELECT workspace_id, sum(queries) AS queries, sum(read_bytes) AS read_bytes,
         sum(sql_queries) AS sql_queries, sum(sql_read_bytes) AS sql_read_bytes
  FROM queries GROUP BY 1
),
users AS (
  SELECT approx_count_distinct(identity) AS mau,
         approx_count_distinct(CASE WHEN identity LIKE '%@%' THEN identity END) AS mau_human,
         approx_count_distinct(CASE WHEN floor(days_back / 7) = 0 THEN identity END) AS wau,
         approx_count_distinct(CASE WHEN days_back = 0 THEN identity END) AS dau,
         collect_set(workspace_id) AS audit_workspaces
  FROM ids
),
m AS (   -- one row; each ratio's spend is limited to the workspaces its denominator can see
  SELECT max(w.start_date) AS start_date, max(w.end_date) AS end_date,
         sum(s.list_usd) AS list_usd, sum(s.net_usd) AS net_usd,
         sum(s.priced_list_usd) AS priced_list_usd, sum(s.priced_acct_usd) AS priced_acct_usd,
         sum(s.unpriced_rows) AS unpriced_rows,
         count(CASE WHEN s.list_usd > 0 THEN 1 END) AS workspaces,
         size(array_distinct(flatten(collect_list(s.products)))) AS products,
         sum(s.week_list_usd) / 7 AS daily_list_usd, sum(s.week_net_usd) / 7 AS daily_net_usd,
         sum(q.queries) AS queries, sum(q.read_bytes) / POW(1024, 4) AS read_tib,
         count(q.workspace_id) AS q_workspaces,
         sum(CASE WHEN q.queries > 0 THEN s.list_usd END) AS q_list_usd,
         sum(CASE WHEN q.queries > 0 THEN s.net_usd END) AS q_net_usd,
         sum(q.sql_queries) AS sql_queries, sum(q.sql_read_bytes) / POW(1024, 4) AS sql_read_tib,
         count(CASE WHEN q.sql_queries > 0 THEN 1 END) AS sql_workspaces,
         sum(s.sql_list_usd) AS sql_list_usd,
         sum(CASE WHEN q.sql_queries > 0 THEN s.sql_list_usd END) AS dbsql_list_usd,
         sum(CASE WHEN q.sql_queries > 0 THEN s.sql_net_usd END) AS dbsql_net_usd,
         max(u.mau) AS mau, max(u.mau_human) AS mau_human, max(u.wau) AS wau, max(u.dau) AS dau,
         max(size(u.audit_workspaces)) AS a_workspaces,
         sum(CASE WHEN array_contains(u.audit_workspaces, s.workspace_id) THEN s.list_usd END) AS a_list_usd,
         sum(CASE WHEN array_contains(u.audit_workspaces, s.workspace_id) THEN s.net_usd END) AS a_net_usd
  FROM spend s
  FULL OUTER JOIN q ON s.workspace_id = q.workspace_id
  CROSS JOIN users u
  CROSS JOIN win w
)
SELECT inline(array(
""" + ",\n".join(
    f"  named_struct('metric', {_sql_str(k.metric)}, 'value', {k.value}, 'discounted', {k.discounted},\n"
    f"               'coverage', {k.coverage}, 'definition', {_sql_str(k.definition)})"
    for k in ECON_KPIS) + """
)) FROM m
"""

_ECON_SPLIT_SQL = _econ_with(28) + """
SELECT {label} AS {label_as},
       round(sum(coalesce(acct_usd, list_usd)), 2) AS spend_usd,
       round(100 * sum(coalesce(acct_usd, list_usd)) / sum(sum(coalesce(acct_usd, list_usd))) OVER (), 1) AS {pct_as}
FROM priced {where}
GROUP BY 1 HAVING sum(coalesce(acct_usd, list_usd)) > 0
ORDER BY spend_usd DESC
"""

_ECON_TREND_SQL = _econ_with(84) + _ECON_ACTIVITY + """,
spend AS (SELECT workspace_id, floor(days_back / 7) AS wk, sum(coalesce(acct_usd, list_usd)) AS usd
          FROM priced GROUP BY 1, 2),
q AS (SELECT workspace_id, floor(days_back / 7) AS wk, sum(queries) AS queries, sum(read_bytes) AS read_bytes
      FROM queries GROUP BY 1, 2),
users AS (SELECT floor(days_back / 7) AS wk, approx_count_distinct(identity) AS wau,
                 collect_set(workspace_id) AS audit_workspaces
          FROM ids GROUP BY 1)
SELECT date_sub(max(w.end_date), CAST(s.wk AS INT) * 7) AS week,
       round(sum(CASE WHEN q.queries > 0 THEN s.usd END) / sum(q.queries), 4) AS `cost per query`,
       round(sum(CASE WHEN q.queries > 0 THEN s.usd END) / (sum(q.read_bytes) / POW(1024, 4)), 2)
         AS `cost per TiB read`,
       round(sum(CASE WHEN array_contains(u.audit_workspaces, s.workspace_id) THEN s.usd END) / max(u.wau), 2)
         AS `cost per weekly active user`
FROM spend s
LEFT JOIN q ON s.workspace_id = q.workspace_id AND s.wk = q.wk
LEFT JOIN users u ON s.wk = u.wk
CROSS JOIN win w
GROUP BY s.wk ORDER BY week
"""

# ── Boards & panels ───────────────────────────────────────────────────────────
# chart: "line" | "lines" | "bar" | "table" | "forecast" | "stacked_bar" | "decomp"
# For line/bar/stacked: x = first column, series/group = second (if categorical),
# value = last numeric column — the frontend infers from `chart` + column types.
# "lines" draws one line per column after the first, on a log axis.

# The job-cluster panels label from job_clusters jc, the most expensive queries from alloc a (names.py).
JOB_JC, CLUSTER_JC = ref("job", "jc.job_id", "jc.workspace_id"), ref("cluster", "jc.cluster_id", "jc.workspace_id")
WAREHOUSE_A = ref("warehouse", "a.warehouse_id", "a.workspace_id")

BOARDS = [
    # ══════════════════════════════════════════════════════════════════════════
    # USAGE — total spend, by SKU, by workspace, and an AI forecast.
    # Core logic de-parameterised from Account Usage Dashboard v2 (labs sandbox).
    # ══════════════════════════════════════════════════════════════════════════
    {
        "id": "usage",
        "label": "Usage",
        "icon": "\U0001F4C8",
        "blurb": "Total spend and where it goes — over time, by product, by workspace — "
                 "with week-over-week spikes and brand-new cost flagged for you.",
        "panels": [
            {
                "id": "why_changed",
                "title": "Why did cost change? (this week vs last)",
                "eli5": "Breaks the week-over-week change in total spend into WHY it moved: new "
                        "workloads, retired workloads, volume growth (existing things doing more), "
                        "and rate/mix (pricier compute or SKU mix). The four add up to the total "
                        "change exactly — so you fix the right driver instead of cutting blindly.",
                "chart": "decomp",
                "note": "ΔCost = new − retired + volume + rate/mix, decomposed per workload "
                        "(job/pipeline/warehouse/endpoint/app) then summed — reconciles to the total "
                        "change by construction. Volume vs rate/mix split uses each workload's own "
                        "$/DBU. Compares the last COMPLETE week to the one before.",
                "anomaly": {"type": "decomp_driver"},
                "sql": r"""
WITH price AS (
  SELECT sku_name, cloud, price_start_time, price_end_time, pricing.effective_list.default AS unit_usd
  FROM system.billing.list_prices
),
usage AS (
  SELECT coalesce('job:'||u.usage_metadata.job_id, 'pipeline:'||u.usage_metadata.dlt_pipeline_id,
                  'warehouse:'||u.usage_metadata.warehouse_id, 'endpoint:'||u.usage_metadata.endpoint_name,
                  'app:'||u.usage_metadata.app_name, 'product:'||u.billing_origin_product) AS workload_key,
         u.usage_date,
         CAST(u.usage_quantity AS DOUBLE) AS dbus,
         CAST(u.usage_quantity * pr.unit_usd AS DOUBLE) AS est_cost
  FROM system.billing.usage u
  LEFT JOIN price pr ON u.sku_name=pr.sku_name AND u.cloud=pr.cloud
    AND u.usage_start_time>=pr.price_start_time AND (pr.price_end_time IS NULL OR u.usage_start_time<pr.price_end_time)
  WHERE u.usage_date >= date_trunc('WEEK', current_date()) - INTERVAL 2 WEEKS
    AND u.usage_date <  date_trunc('WEEK', current_date())
),
per_workload AS (
  SELECT workload_key,
    CAST(SUM(CASE WHEN usage_date >= date_trunc('WEEK', current_date()) - INTERVAL 1 WEEKS THEN est_cost ELSE 0 END) AS DOUBLE) AS cur_cost,
    CAST(SUM(CASE WHEN usage_date <  date_trunc('WEEK', current_date()) - INTERVAL 1 WEEKS THEN est_cost ELSE 0 END) AS DOUBLE) AS prev_cost,
    CAST(SUM(CASE WHEN usage_date >= date_trunc('WEEK', current_date()) - INTERVAL 1 WEEKS THEN dbus END) AS DOUBLE) AS cur_dbus,
    CAST(SUM(CASE WHEN usage_date <  date_trunc('WEEK', current_date()) - INTERVAL 1 WEEKS THEN dbus END) AS DOUBLE) AS prev_dbus
  FROM usage GROUP BY workload_key
),
classed AS (
  SELECT *,
    CASE WHEN prev_cost=0 AND cur_cost>0 THEN 'NEW' WHEN cur_cost=0 AND prev_cost>0 THEN 'RETIRED' ELSE 'EXISTING' END AS status,
    -- volume vs rate/mix split for EXISTING workloads (DBU-metered): volume = ΔDBU at last week's rate;
    -- rate/mix = the remainder (price/SKU-mix). Non-DBU workloads attribute their whole delta to volume.
    CASE WHEN prev_cost>0 AND cur_cost>0 AND coalesce(prev_dbus,0)>0 AND coalesce(cur_dbus,0)>0 THEN (cur_dbus-prev_dbus)*(prev_cost/prev_dbus)
         WHEN prev_cost>0 AND cur_cost>0 THEN cur_cost-prev_cost ELSE 0 END AS volume_effect,
    CASE WHEN prev_cost>0 AND cur_cost>0 AND coalesce(prev_dbus,0)>0 AND coalesce(cur_dbus,0)>0 THEN cur_dbus*((cur_cost/cur_dbus)-(prev_cost/prev_dbus))
         ELSE 0 END AS rate_mix_effect
  FROM per_workload
),
weekly AS (   -- the nine complete weeks before last week, for the account's usual weekly move
  SELECT date_trunc('WEEK', u.usage_date) AS week, sum(u.usage_quantity * pr.unit_usd) AS usd
  FROM system.billing.usage u
  LEFT JOIN price pr ON u.sku_name=pr.sku_name AND u.cloud=pr.cloud
    AND u.usage_start_time>=pr.price_start_time AND (pr.price_end_time IS NULL OR u.usage_start_time<pr.price_end_time)
  WHERE u.usage_date >= date_trunc('WEEK', current_date()) - INTERVAL 10 WEEKS
    AND u.usage_date <  date_trunc('WEEK', current_date()) - INTERVAL 1 WEEKS
  GROUP BY 1
),
typical AS (  -- median absolute week-over-week change across those eight week-pairs
  SELECT percentile(change_usd, 0.5) AS typical_change_usd
  FROM (SELECT abs(usd - lag(usd) OVER (ORDER BY week)) AS change_usd FROM weekly)
  WHERE change_usd IS NOT NULL
)
SELECT round(sum(prev_cost),2)                                              AS prev_week_usd,
       round(sum(cur_cost),2)                                               AS this_week_usd,
       round(sum(cur_cost)-sum(prev_cost),2)                                AS delta_usd,
       round(sum(CASE WHEN status='NEW' THEN cur_cost ELSE 0 END),2)        AS new_workloads_usd,
       round(-sum(CASE WHEN status='RETIRED' THEN prev_cost ELSE 0 END),2)  AS retired_workloads_usd,
       round(sum(volume_effect),2)                                          AS volume_growth_usd,
       round(sum(rate_mix_effect),2)                                        AS rate_mix_usd,
       count(CASE WHEN status='NEW' THEN 1 END)                             AS new_count,
       count(CASE WHEN status='RETIRED' THEN 1 END)                         AS retired_count,
       round(max(t.typical_change_usd),2)                                   AS typical_change_usd
FROM classed CROSS JOIN typical t
""",
            },
            {
                "id": "trend",
                "title": "Spend trend (weekly, all products)",
                "eli5": "Total list-price $ per week across the account. A step-change up is the "
                        "first thing to explain.",
                "chart": "line",
                "anomaly": {"type": "ts_spike", "time_col": "week", "value_col": "list_usd",
                            "unit": "usd"},
                "sql": """
WITH price AS (
  SELECT sku_name, cloud, price_start_time, price_end_time,
         pricing.effective_list.default AS unit_usd
  FROM system.billing.list_prices
)
SELECT date_trunc('WEEK', u.usage_date)              AS week,
       round(sum(u.usage_quantity * pr.unit_usd), 2) AS list_usd
FROM system.billing.usage u
LEFT JOIN price pr ON u.sku_name = pr.sku_name AND u.cloud = pr.cloud
  AND u.usage_start_time >= pr.price_start_time
  AND (pr.price_end_time IS NULL OR u.usage_start_time < pr.price_end_time)
WHERE u.usage_date >= current_date() - INTERVAL 12 WEEKS
  AND u.usage_date < date_trunc('WEEK', current_date())   -- complete weeks only (drop partial current week)
GROUP BY 1 ORDER BY 1
""",
            },
            {
                "id": "by_sku",
                "title": "Spend by product (weekly)",
                "eli5": "Weekly $ per billing product. Watch for a SKU accelerating week over week.",
                "chart": "stacked_bar",
                "anomaly": {"type": "ts_spike", "time_col": "week", "value_col": "list_usd",
                            "group_col": "product", "unit": "usd"},
                "sql": """
WITH price AS (
  SELECT sku_name, cloud, price_start_time, price_end_time,
         pricing.effective_list.default AS unit_usd
  FROM system.billing.list_prices
)
SELECT date_trunc('WEEK', u.usage_date)              AS week,
       u.billing_origin_product                      AS product,
       round(sum(u.usage_quantity * pr.unit_usd), 2) AS list_usd
FROM system.billing.usage u
LEFT JOIN price pr ON u.sku_name = pr.sku_name AND u.cloud = pr.cloud
  AND u.usage_start_time >= pr.price_start_time
  AND (pr.price_end_time IS NULL OR u.usage_start_time < pr.price_end_time)
WHERE u.usage_date >= current_date() - INTERVAL 8 WEEKS
  AND u.usage_date < date_trunc('WEEK', current_date())   -- complete weeks only
GROUP BY 1, 2 HAVING sum(u.usage_quantity * pr.unit_usd) > 0
ORDER BY 1, list_usd DESC
""",
            },
            {
                "id": "by_workspace",
                "title": "Spend by workspace (30 days)",
                "eli5": "Which workspace is spending the most. A single dominant workspace is where "
                        "attribution effort pays off first.",
                "chart": "bar",
                "anomaly": {"type": "rank_outlier", "value_col": "list_usd",
                            "label_col": "workspace", "unit": "usd"},
                "sql": f"""
WITH price AS (
  SELECT sku_name, cloud, price_start_time, price_end_time,
         pricing.effective_list.default AS unit_usd
  FROM system.billing.list_prices
),
{ctes("workspace")},
spend AS (
  SELECT u.workspace_id, round(sum(u.usage_quantity * pr.unit_usd), 2) AS list_usd
  FROM system.billing.usage u
  LEFT JOIN price pr ON u.sku_name = pr.sku_name AND u.cloud = pr.cloud
    AND u.usage_start_time >= pr.price_start_time
    AND (pr.price_end_time IS NULL OR u.usage_start_time < pr.price_end_time)
  WHERE u.usage_date >= current_date() - INTERVAL 30 DAYS
  GROUP BY 1
)
SELECT {WORKSPACE.label} AS workspace, c.list_usd, c.workspace_id
FROM spend c {WORKSPACE.join}
ORDER BY c.list_usd DESC NULLS LAST LIMIT 20
""",
            },
            {
                "id": "forecast",
                "title": "Spend forecast (next 4 weeks)",
                "eli5": "A predictive forecast of weekly spend using Databricks' built-in AI_FORECAST — "
                        "actuals plus a projected line with a confidence band.",
                "chart": "forecast",
                "anomaly": None,   # the forecast IS the forward-looking signal
                "sql": """
WITH price AS (
  SELECT sku_name, cloud, price_start_time, price_end_time,
         pricing.effective_list.default AS unit_usd
  FROM system.billing.list_prices
),
hist AS (
  SELECT date_trunc('WEEK', u.usage_date)::timestamp      AS week,
         round(sum(u.usage_quantity * pr.unit_usd), 2)    AS list_usd
  FROM system.billing.usage u
  LEFT JOIN price pr ON u.sku_name = pr.sku_name AND u.cloud = pr.cloud
    AND u.usage_start_time >= pr.price_start_time
    AND (pr.price_end_time IS NULL OR u.usage_start_time < pr.price_end_time)
  WHERE u.usage_date >= current_date() - INTERVAL 26 WEEKS
    AND u.usage_date < date_trunc('WEEK', current_date())   -- complete weeks only (no partial-week cliff)
  GROUP BY 1
),
fc AS (
  SELECT week, usage_forecast, usage_upper, usage_lower
  FROM AI_FORECAST(
    TABLE(SELECT week, list_usd AS usage FROM hist),
    horizon => date_trunc('WEEK', current_date()) + INTERVAL 4 WEEKS,
    time_col => 'week',
    value_col => 'usage',
    prediction_interval_width => 0.9
  )
)
-- actuals + forecast as separate rows; the frontend merges by week and bridges the
-- last actual point into the forecast series (so the dashed line connects, no gap).
SELECT week, list_usd AS actual_usd, CAST(NULL AS DOUBLE) AS forecast_usd,
       CAST(NULL AS DOUBLE) AS upper_usd, CAST(NULL AS DOUBLE) AS lower_usd
FROM hist
UNION ALL
SELECT week, CAST(NULL AS DOUBLE), round(usage_forecast,2), round(usage_upper,2), round(usage_lower,2)
FROM fc
ORDER BY week
""",
            },
        ],
    },

    # ══════════════════════════════════════════════════════════════════════════
    # JOBS — potential savings, CPU/mem efficiency, fixed workers, outdated DBR.
    # Docs lacked these five, so built from compute tables (probed columns).
    # ══════════════════════════════════════════════════════════════════════════
    {
        "id": "jobs",
        "label": "Jobs",
        "icon": "⚙️",
        "blurb": "Job-level efficiency: where the money could be saved, which jobs waste "
                 "cores or memory, and which run on fixed-size or outdated compute.",
        "panels": [
            {
                "id": "potential_savings",
                "title": "Top 25 jobs by potential monthly saving",
                "eli5": "Jobs running on All-Purpose compute that would cost less on Jobs compute. The "
                        "saving is each job's share of the all-purpose cluster time it used, priced at "
                        "your list price for that SKU minus the matching Jobs-compute SKU.",
                "chart": "table",
                "note": "Billing never tags all-purpose usage with a job, so a job's share of each "
                        "all-purpose cluster-hour is the time its tasks ran in that hour, split fairly "
                        "when jobs overlap. Idle time between runs is not counted, so the saving is a "
                        "floor.",
                "anomaly": {"type": "rank_outlier", "value_col": "est_monthly_saving_usd",
                            "label_col": "job_name", "unit": "usd"},
                "sql": f"""
WITH price AS (
  SELECT sku_name, cloud, usage_unit, price_start_time, price_end_time,
         pricing.effective_list.default AS unit_usd FROM system.billing.list_prices
),
{ctes("job")},
ap_clusters AS (   -- all-purpose clusters (interactive), latest config
  SELECT workspace_id, cluster_id FROM (
    SELECT workspace_id, cluster_id, cluster_source
    FROM system.compute.clusters
    QUALIFY ROW_NUMBER() OVER (PARTITION BY workspace_id, cluster_id ORDER BY change_time DESC)=1
  ) WHERE cluster_source IN ('UI','API')
),
job_ap_runs AS (  -- task time each job spent on an all-purpose cluster, per clock hour
  SELECT r.workspace_id, r.job_id, r.cluster_id, r.hour, sum(r.run_ms) AS run_ms
  FROM (
    SELECT t.workspace_id, t.job_id, exploded.cluster_id, date_trunc('HOUR', t.period_start_time) AS hour,
           unix_millis(t.period_end_time) - unix_millis(t.period_start_time) AS run_ms
    FROM system.lakeflow.job_task_run_timeline t
    LATERAL VIEW explode(t.compute_ids) exploded AS cluster_id
    WHERE t.period_start_time >= current_date() - INTERVAL 30 DAYS
      AND t.compute_ids IS NOT NULL AND array_size(t.compute_ids) > 0
  ) r
  JOIN ap_clusters c ON r.workspace_id=c.workspace_id AND r.cluster_id=c.cluster_id
  GROUP BY 1,2,3,4
),
ap_hour AS (  -- each all-purpose cluster-hour at its own list price and at the matching Jobs-compute price
  SELECT u.workspace_id, u.usage_metadata.cluster_id AS cluster_id,
         date_trunc('HOUR', u.usage_start_time) AS hour,
         sum(u.usage_quantity * ap.unit_usd) AS ap_usd,
         sum(u.usage_quantity * jp.unit_usd) AS jobs_usd
  FROM system.billing.usage u
  JOIN price ap ON u.sku_name=ap.sku_name AND u.cloud=ap.cloud AND u.usage_unit=ap.usage_unit
    AND u.usage_start_time >= ap.price_start_time
    AND (ap.price_end_time IS NULL OR u.usage_start_time < ap.price_end_time)
  JOIN price jp ON jp.sku_name=replace(u.sku_name, 'ALL_PURPOSE_COMPUTE', 'JOBS_COMPUTE')
    AND u.cloud=jp.cloud AND u.usage_unit=jp.usage_unit
    AND u.usage_start_time >= jp.price_start_time
    AND (jp.price_end_time IS NULL OR u.usage_start_time < jp.price_end_time)
  WHERE u.sku_name LIKE '%ALL_PURPOSE_COMPUTE%' AND u.usage_metadata.cluster_id IS NOT NULL
    AND u.usage_date >= current_date() - INTERVAL 30 DAYS
  GROUP BY 1,2,3
),
job_share AS (  -- the hour's busy time across every job decides each job's fair share of it
  SELECT j.*, h.ap_usd, h.jobs_usd,
         j.run_ms / greatest(sum(j.run_ms) OVER (PARTITION BY j.workspace_id, j.cluster_id, j.hour),
                             3600 * 1000.0) AS share
  FROM job_ap_runs j
  JOIN ap_hour h ON j.workspace_id=h.workspace_id AND j.cluster_id=h.cluster_id AND j.hour=h.hour
),
job_spend AS (
  SELECT workspace_id, job_id, sum(ap_usd * share) AS ap_usd_30d,
         sum((ap_usd - jobs_usd) * share) AS saving_usd_30d
  FROM job_share GROUP BY 1,2
)
SELECT {JOB.label}                                   AS job_name,
       round(c.ap_usd_30d, 2)                          AS all_purpose_usd_30d,
       round(c.saving_usd_30d, 2)                      AS est_monthly_saving_usd,
       c.job_id
FROM job_spend c {JOB.join}
WHERE c.saving_usd_30d > 0
ORDER BY est_monthly_saving_usd DESC LIMIT 25
""",
            },
            {
                "id": "cpu_efficiency",
                "title": "Top 10 jobs with lowest avg CPU",
                "eli5": "Job clusters running at low average CPU — cores you pay for that sit idle. "
                        "A downsize candidate. Only your largest tenth of job clusters by node-minutes "
                        "is listed, and each is set against your typical large job cluster.",
                "chart": "table",
                "anomaly": {"type": "vs_typical", "x_col": "times_below_typical",
                            "typical_col": "typical_cpu_pct", "label_col": "job_name",
                            "value_col": "avg_cpu_pct", "unit": "pct",
                            "headline": "{n} of these jobs use at most {frac} of the CPU of your "
                                        "typical large job cluster",
                            "detail": "Typical: {typical} CPU. Lowest: “{worst}” at {value}, {x} below "
                                      "typical. These are the rows worth acting on first."},
                "sql": f"""
WITH {ctes("job")},
job_clusters AS (
  SELECT DISTINCT t.workspace_id, t.job_id, exploded.cluster_id
  FROM system.lakeflow.job_task_run_timeline t
  LATERAL VIEW explode(t.compute_ids) exploded AS cluster_id
  WHERE t.period_start_time >= current_date() - INTERVAL 30 DAYS
    AND t.compute_ids IS NOT NULL AND array_size(t.compute_ids) > 0
),
cpu AS (
  SELECT cluster_id, count(*) AS node_minutes,
         avg(cpu_user_percent + cpu_system_percent) AS cpu_pct
  FROM system.compute.node_timeline
  WHERE start_time >= current_date() - INTERVAL 30 DAYS AND driver = false
    AND cluster_id IN (SELECT cluster_id FROM job_clusters)
  GROUP BY 1
),
large AS (   -- the largest tenth of job clusters by node-minutes, where idle cores cost real money
  SELECT * FROM cpu WHERE node_minutes >= (SELECT percentile_approx(node_minutes, 0.9) FROM cpu)
),
typical AS (SELECT percentile_approx(cpu_pct, 0.5) AS typical_cpu_pct FROM large)
SELECT {JOB_JC.label}                                AS job_name,
       round(l.cpu_pct, 1)                                  AS avg_cpu_pct,
       round(t.typical_cpu_pct, 1)                          AS typical_cpu_pct,
       round(try_divide(t.typical_cpu_pct, l.cpu_pct), 1)   AS times_below_typical,
       l.node_minutes, jc.job_id
FROM job_clusters jc
JOIN large l ON jc.cluster_id = l.cluster_id
CROSS JOIN typical t
{JOB_JC.join}
ORDER BY l.cpu_pct ASC, l.node_minutes DESC LIMIT 10
""",
            },
            {
                "id": "memory_allocation",
                "title": "Top 10 jobs with highest avg memory",
                "eli5": "Job clusters running near their memory ceiling — memory-bound work that may "
                        "need a different node type (more RAM), not more nodes. The ceiling is physical, "
                        "so each is judged on its free memory against your typical large job cluster.",
                "chart": "table",
                "anomaly": {"type": "vs_typical", "x_col": "times_less_headroom",
                            "typical_col": "typical_free_mem_pct", "label_col": "job_name",
                            "value_col": "free_mem_pct", "unit": "pct",
                            "headline": "{n} of these jobs keep at most {frac} of the free memory of "
                                        "your typical large job cluster",
                            "detail": "Typical: {typical} free. Tightest: “{worst}” at {value} free, "
                                      "{x} less headroom. These are the rows worth acting on first."},
                "sql": f"""
WITH {ctes("job")},
job_clusters AS (
  SELECT DISTINCT t.workspace_id, t.job_id, exploded.cluster_id
  FROM system.lakeflow.job_task_run_timeline t
  LATERAL VIEW explode(t.compute_ids) exploded AS cluster_id
  WHERE t.period_start_time >= current_date() - INTERVAL 30 DAYS
    AND t.compute_ids IS NOT NULL AND array_size(t.compute_ids) > 0
),
mem AS (
  SELECT cluster_id, count(*) AS node_minutes,
         avg(mem_used_percent) AS mem_pct, max(mem_used_percent) AS peak_mem_pct
  FROM system.compute.node_timeline
  WHERE start_time >= current_date() - INTERVAL 30 DAYS AND driver = false
    AND cluster_id IN (SELECT cluster_id FROM job_clusters)
  GROUP BY 1
),
large AS (   -- the largest tenth of job clusters by node-minutes
  SELECT * FROM mem WHERE node_minutes >= (SELECT percentile_approx(node_minutes, 0.9) FROM mem)
),
typical AS (SELECT percentile_approx(100 - mem_pct, 0.5) AS typical_free_mem_pct FROM large)
SELECT {JOB_JC.label}                                           AS job_name,
       round(l.mem_pct, 1)                                             AS avg_mem_pct,
       round(l.peak_mem_pct, 1)                                        AS peak_mem_pct,
       round(100 - l.mem_pct, 1)                                       AS free_mem_pct,
       round(t.typical_free_mem_pct, 1)                                AS typical_free_mem_pct,
       round(try_divide(t.typical_free_mem_pct, 100 - l.mem_pct), 1)   AS times_less_headroom,
       l.node_minutes, jc.job_id
FROM job_clusters jc
JOIN large l ON jc.cluster_id = l.cluster_id
CROSS JOIN typical t
{JOB_JC.join}
ORDER BY l.mem_pct DESC LIMIT 10
""",
            },
            {
                "id": "fixed_workers",
                "title": "Jobs on fixed-size clusters (last 30 days)",
                "eli5": "Job clusters with a fixed worker count and no autoscaling — they can't shrink "
                        "for light runs or grow for heavy ones. Autoscaling usually saves money.",
                "chart": "table",
                "anomaly": None,
                "sql": f"""
WITH {ctes("job", "cluster")},
job_clusters AS (
  SELECT DISTINCT t.workspace_id, t.job_id, exploded.cluster_id
  FROM system.lakeflow.job_task_run_timeline t
  LATERAL VIEW explode(t.compute_ids) exploded AS cluster_id
  WHERE t.period_start_time >= current_date() - INTERVAL 30 DAYS
    AND t.compute_ids IS NOT NULL AND array_size(t.compute_ids) > 0
),
cfg AS (
  SELECT workspace_id, cluster_id, worker_count,
         min_autoscale_workers, max_autoscale_workers, worker_node_type
  FROM system.compute.clusters
  QUALIFY ROW_NUMBER() OVER (PARTITION BY workspace_id, cluster_id ORDER BY change_time DESC)=1
)
SELECT {JOB_JC.label}                            AS job_name,
       {CLUSTER_JC.label}                        AS cluster_name,
       cfg.worker_count, cfg.worker_node_type, jc.job_id, jc.cluster_id
FROM job_clusters jc
JOIN cfg ON jc.workspace_id=cfg.workspace_id AND jc.cluster_id=cfg.cluster_id
{JOB_JC.join}
{CLUSTER_JC.join}
WHERE cfg.min_autoscale_workers IS NULL AND cfg.max_autoscale_workers IS NULL
  AND cfg.worker_count >= 1
ORDER BY cfg.worker_count DESC LIMIT 50
""",
            },
            {
                "id": "outdated_dbr",
                "title": "Jobs on outdated DBR (last 30 days)",
                "eli5": "Job clusters on an older Databricks Runtime major than your typical job cluster "
                        "runs (typical_major, the median across your job clusters). Newer runtimes are "
                        "faster and get Photon/security fixes — upgrading is often free speed.",
                "chart": "table",
                "anomaly": None,
                "sql": f"""
WITH {ctes("job", "cluster")},
job_clusters AS (
  SELECT DISTINCT t.workspace_id, t.job_id, exploded.cluster_id
  FROM system.lakeflow.job_task_run_timeline t
  LATERAL VIEW explode(t.compute_ids) exploded AS cluster_id
  WHERE t.period_start_time >= current_date() - INTERVAL 30 DAYS
    AND t.compute_ids IS NOT NULL AND array_size(t.compute_ids) > 0
),
cfg AS (
  SELECT workspace_id, cluster_id, dbr_version,
         try_cast(regexp_extract(dbr_version, '^(\\\\d+)\\\\.', 1) AS INT) AS major
  FROM system.compute.clusters
  QUALIFY ROW_NUMBER() OVER (PARTITION BY workspace_id, cluster_id ORDER BY change_time DESC)=1
),
fleet AS (   -- the major your typical job cluster runs: the median over the job clusters seen
  SELECT percentile_approx(cfg.major, 0.5) AS typical_major
  FROM (SELECT DISTINCT workspace_id, cluster_id FROM job_clusters) jc
  JOIN cfg ON jc.workspace_id=cfg.workspace_id AND jc.cluster_id=cfg.cluster_id
)
SELECT {JOB_JC.label}                            AS job_name,
       {CLUSTER_JC.label}                        AS cluster_name,
       cfg.dbr_version, f.typical_major, jc.job_id, jc.cluster_id
FROM job_clusters jc
JOIN cfg ON jc.workspace_id=cfg.workspace_id AND jc.cluster_id=cfg.cluster_id
CROSS JOIN fleet f
{JOB_JC.join}
{CLUSTER_JC.join}
WHERE cfg.major < f.typical_major
ORDER BY cfg.major ASC, job_name LIMIT 50
""",
            },
        ],
    },

    # ══════════════════════════════════════════════════════════════════════════
    # DBSQL — per-query cost attributed to user / source / query, over time.
    # Allocation is the app's own M1 method (execution-time share of the
    # warehouse-hour $), de-scoped from the cost-per-query PrPr MV so it needs
    # no PrPr table. Source-type CASE is transcribed from that MV.
    # ══════════════════════════════════════════════════════════════════════════
    {
        "id": "dbsql",
        "label": "DBSQL",
        "icon": "\U0001F5C2️",
        "blurb": "Databricks SQL cost attributed down to the user, the source (job, notebook, "
                 "dashboard, Genie), and the individual query — plus the trend.",
        "panels": [
            {
                "id": "by_user",
                "title": "Attributed cost by user (14 days)",
                "eli5": "Each query gets a slice of its warehouse-hour cost by execution-time share; "
                        "this rolls that up per user. A single heavy user is where a chat starts.",
                "chart": "bar",
                "anomaly": {"type": "rank_outlier", "value_col": "query_usd",
                            "label_col": "executed_by", "unit": "usd"},
                "sql": """
WITH wh_hour AS (
  SELECT warehouse_id, hour, sum(usage_quantity * unit_usd) AS hour_usd FROM (
    SELECT u.usage_metadata.warehouse_id AS warehouse_id,
           date_trunc('HOUR', u.usage_start_time) AS hour,
           u.usage_quantity, lp.pricing.effective_list.default AS unit_usd
    FROM system.billing.usage u JOIN system.billing.list_prices lp
      ON u.sku_name=lp.sku_name AND u.cloud=lp.cloud
     AND u.usage_start_time>=lp.price_start_time AND (lp.price_end_time IS NULL OR u.usage_start_time<lp.price_end_time)
    WHERE u.usage_metadata.warehouse_id IS NOT NULL AND u.usage_date >= current_date() - INTERVAL 14 DAYS
    QUALIFY ROW_NUMBER() OVER (PARTITION BY u.record_id ORDER BY lp.price_start_time DESC)=1
  ) GROUP BY 1,2
),
cfg AS (SELECT warehouse_id, max_clusters, change_time AS vf,
          coalesce(lead(change_time) OVER (PARTITION BY warehouse_id ORDER BY change_time), TIMESTAMP '9999-12-31') AS vt
        FROM system.compute.warehouses),
q AS (
  SELECT compute.warehouse_id AS warehouse_id, date_trunc('HOUR', start_time) AS hour,
         executed_by, execution_duration_ms,
         sum(execution_duration_ms) OVER (PARTITION BY compute.warehouse_id, date_trunc('HOUR', start_time)) AS busy_ms
  FROM system.query.history
  WHERE start_time >= current_date() - INTERVAL 14 DAYS AND execution_duration_ms > 0
),
alloc AS (
  SELECT q.*, w.hour_usd,
         GREATEST(coalesce(c.max_clusters,1)*3600000.0, q.busy_ms) AS capacity_ms
  FROM q JOIN wh_hour w USING (warehouse_id, hour)
         LEFT JOIN cfg c ON c.warehouse_id=q.warehouse_id AND q.hour>=c.vf AND q.hour<c.vt
)
SELECT executed_by,
       round(sum(hour_usd * execution_duration_ms / capacity_ms), 2) AS query_usd,
       count(*) AS statements
FROM alloc GROUP BY 1 ORDER BY query_usd DESC NULLS LAST LIMIT 20
""",
            },
            {
                "id": "by_source",
                "title": "Attributed cost by source type (14 days)",
                "eli5": "Where the queries come from — Job, Notebook, SQL Query, AI/BI Dashboard, "
                        "Genie, Alert, or Other (ad-hoc / driver). Tells you which surface to tune.",
                "chart": "bar",
                "anomaly": None,
                "sql": """
WITH wh_hour AS (
  SELECT warehouse_id, hour, sum(usage_quantity * unit_usd) AS hour_usd FROM (
    SELECT u.usage_metadata.warehouse_id AS warehouse_id,
           date_trunc('HOUR', u.usage_start_time) AS hour,
           u.usage_quantity, lp.pricing.effective_list.default AS unit_usd
    FROM system.billing.usage u JOIN system.billing.list_prices lp
      ON u.sku_name=lp.sku_name AND u.cloud=lp.cloud
     AND u.usage_start_time>=lp.price_start_time AND (lp.price_end_time IS NULL OR u.usage_start_time<lp.price_end_time)
    WHERE u.usage_metadata.warehouse_id IS NOT NULL AND u.usage_date >= current_date() - INTERVAL 14 DAYS
    QUALIFY ROW_NUMBER() OVER (PARTITION BY u.record_id ORDER BY lp.price_start_time DESC)=1
  ) GROUP BY 1,2
),
cfg AS (SELECT warehouse_id, max_clusters, change_time AS vf,
          coalesce(lead(change_time) OVER (PARTITION BY warehouse_id ORDER BY change_time), TIMESTAMP '9999-12-31') AS vt
        FROM system.compute.warehouses),
q AS (
  SELECT compute.warehouse_id AS warehouse_id, date_trunc('HOUR', start_time) AS hour,
         CASE
           WHEN query_source.job_info.job_id IS NOT NULL THEN 'Job'
           WHEN query_source.legacy_dashboard_id IS NOT NULL THEN 'Legacy dashboard'
           WHEN query_source.dashboard_id IS NOT NULL THEN 'AI/BI dashboard'
           WHEN query_source.alert_id IS NOT NULL THEN 'Alert'
           WHEN query_source.notebook_id IS NOT NULL THEN 'Notebook'
           WHEN query_source.sql_query_id IS NOT NULL THEN 'SQL query'
           WHEN query_source.genie_space_id IS NOT NULL THEN 'Genie'
           ELSE 'Other (ad-hoc / driver)' END AS source_type,
         execution_duration_ms,
         sum(execution_duration_ms) OVER (PARTITION BY compute.warehouse_id, date_trunc('HOUR', start_time)) AS busy_ms
  FROM system.query.history
  WHERE start_time >= current_date() - INTERVAL 14 DAYS AND execution_duration_ms > 0
),
alloc AS (
  SELECT q.*, w.hour_usd,
         GREATEST(coalesce(c.max_clusters,1)*3600000.0, q.busy_ms) AS capacity_ms
  FROM q JOIN wh_hour w USING (warehouse_id, hour)
         LEFT JOIN cfg c ON c.warehouse_id=q.warehouse_id AND q.hour>=c.vf AND q.hour<c.vt
)
SELECT source_type,
       round(sum(hour_usd * execution_duration_ms / capacity_ms), 2) AS query_usd,
       count(*) AS statements
FROM alloc GROUP BY 1 ORDER BY query_usd DESC NULLS LAST
""",
            },
            {
                "id": "top_queries",
                "title": "Most expensive queries (14 days)",
                "eli5": "The individual statements that cost the most, each with who ran it, the "
                        "warehouse it ran on, and where it came from. Click a statement to open it in "
                        "Query History.",
                "chart": "table",
                "note": "Cost = the query's share of its warehouse-hour $, by execution-time. Warehouses "
                        "bill by size, so a longer query on a small warehouse can cost LESS than a shorter "
                        "one on a much larger warehouse — that's why exec_min and query_usd don't always rank together. "
                        "The warehouse column shows which one ran it.",
                "anomaly": {"type": "rank_outlier", "value_col": "query_usd",
                            "label_col": "executed_by", "unit": "usd"},
                "sql": f"""
WITH {ctes("warehouse")},
wh_hour AS (
  SELECT warehouse_id, hour, sum(usage_quantity * unit_usd) AS hour_usd FROM (
    SELECT u.usage_metadata.warehouse_id AS warehouse_id,
           date_trunc('HOUR', u.usage_start_time) AS hour,
           u.usage_quantity, lp.pricing.effective_list.default AS unit_usd
    FROM system.billing.usage u JOIN system.billing.list_prices lp
      ON u.sku_name=lp.sku_name AND u.cloud=lp.cloud
     AND u.usage_start_time>=lp.price_start_time AND (lp.price_end_time IS NULL OR u.usage_start_time<lp.price_end_time)
    WHERE u.usage_metadata.warehouse_id IS NOT NULL AND u.usage_date >= current_date() - INTERVAL 14 DAYS
    QUALIFY ROW_NUMBER() OVER (PARTITION BY u.record_id ORDER BY lp.price_start_time DESC)=1
  ) GROUP BY 1,2
),
cfg AS (SELECT warehouse_id, max_clusters, change_time AS vf,
          coalesce(lead(change_time) OVER (PARTITION BY warehouse_id ORDER BY change_time), TIMESTAMP '9999-12-31') AS vt
        FROM system.compute.warehouses),
ws AS (SELECT workspace_id, max(workspace_url) AS workspace_url FROM system.access.workspaces_latest GROUP BY 1),
q AS (
  SELECT statement_id, workspace_id, compute.warehouse_id AS warehouse_id, date_trunc('HOUR', start_time) AS hour,
         executed_by, statement_type,
         CASE
           WHEN query_source.job_info.job_id IS NOT NULL THEN 'Job'
           WHEN query_source.dashboard_id IS NOT NULL THEN 'AI/BI dashboard'
           WHEN query_source.notebook_id IS NOT NULL THEN 'Notebook'
           WHEN query_source.sql_query_id IS NOT NULL THEN 'SQL query'
           WHEN query_source.genie_space_id IS NOT NULL THEN 'Genie'
           ELSE 'Other' END AS source_type,
         -- the id behind the source (so "which dashboard/job?" is answerable + deep-linkable)
         coalesce(query_source.job_info.job_id, query_source.dashboard_id, query_source.legacy_dashboard_id,
                  query_source.notebook_id, query_source.sql_query_id, query_source.genie_space_id) AS source_id,
         execution_duration_ms,
         sum(execution_duration_ms) OVER (PARTITION BY compute.warehouse_id, date_trunc('HOUR', start_time)) AS busy_ms
  FROM system.query.history
  WHERE start_time >= current_date() - INTERVAL 14 DAYS AND execution_duration_ms > 0
),
alloc AS (
  SELECT q.*, w.hour_usd,
         GREATEST(coalesce(c.max_clusters,1)*3600000.0, q.busy_ms) AS capacity_ms
  FROM q JOIN wh_hour w USING (warehouse_id, hour)
         LEFT JOIN cfg c ON c.warehouse_id=q.warehouse_id AND q.hour>=c.vf AND q.hour<c.vt
)
SELECT a.statement_id, a.executed_by,
       {WAREHOUSE_A.label}                                      AS warehouse, a.warehouse_id,
       a.source_type, a.source_id, a.statement_type,
       round(a.execution_duration_ms/60000.0, 2)                    AS exec_min,
       round(a.hour_usd * a.execution_duration_ms / a.capacity_ms, 2) AS query_usd,
       ws.workspace_url   -- the workspace this statement actually ran in (system tables span the whole account)
FROM alloc a {WAREHOUSE_A.join}
             LEFT JOIN ws ON a.workspace_id = ws.workspace_id
ORDER BY query_usd DESC NULLS LAST LIMIT 50
""",
            },
            {
                "id": "trend",
                "title": "Attributed DBSQL cost over time (daily)",
                "eli5": "The attributed query cost per day — the trend line behind the per-user and "
                        "per-source views. A jump here is a day to investigate.",
                "chart": "line",
                "anomaly": {"type": "ts_spike", "time_col": "day", "value_col": "query_usd",
                            "unit": "usd"},
                "sql": """
WITH wh_hour AS (
  SELECT warehouse_id, hour, sum(usage_quantity * unit_usd) AS hour_usd FROM (
    SELECT u.usage_metadata.warehouse_id AS warehouse_id,
           date_trunc('HOUR', u.usage_start_time) AS hour,
           u.usage_quantity, lp.pricing.effective_list.default AS unit_usd
    FROM system.billing.usage u JOIN system.billing.list_prices lp
      ON u.sku_name=lp.sku_name AND u.cloud=lp.cloud
     AND u.usage_start_time>=lp.price_start_time AND (lp.price_end_time IS NULL OR u.usage_start_time<lp.price_end_time)
    WHERE u.usage_metadata.warehouse_id IS NOT NULL AND u.usage_date >= current_date() - INTERVAL 28 DAYS
    QUALIFY ROW_NUMBER() OVER (PARTITION BY u.record_id ORDER BY lp.price_start_time DESC)=1
  ) GROUP BY 1,2
),
cfg AS (SELECT warehouse_id, max_clusters, change_time AS vf,
          coalesce(lead(change_time) OVER (PARTITION BY warehouse_id ORDER BY change_time), TIMESTAMP '9999-12-31') AS vt
        FROM system.compute.warehouses),
q AS (
  SELECT compute.warehouse_id AS warehouse_id, date_trunc('HOUR', start_time) AS hour,
         date_trunc('DAY', start_time) AS day, execution_duration_ms,
         sum(execution_duration_ms) OVER (PARTITION BY compute.warehouse_id, date_trunc('HOUR', start_time)) AS busy_ms
  FROM system.query.history
  WHERE start_time >= current_date() - INTERVAL 28 DAYS AND execution_duration_ms > 0
    AND start_time < current_date()   -- complete days only (drop today's partial day)
),
alloc AS (
  SELECT q.*, w.hour_usd,
         GREATEST(coalesce(c.max_clusters,1)*3600000.0, q.busy_ms) AS capacity_ms
  FROM q JOIN wh_hour w USING (warehouse_id, hour)
         LEFT JOIN cfg c ON c.warehouse_id=q.warehouse_id AND q.hour>=c.vf AND q.hour<c.vt
)
SELECT day, round(sum(hour_usd * execution_duration_ms / capacity_ms), 2) AS query_usd
FROM alloc GROUP BY 1 ORDER BY 1
""",
            },
        ],
    },

    # ══════════════════════════════════════════════════════════════════════════
    # MODEL SERVING — batch inference, pay-per-token, provisioned throughput,
    # granular by endpoint. Transcribed from the tiantan32 cost-attribution
    # dashboard; serving-type CASE verified against live product_features.
    # ══════════════════════════════════════════════════════════════════════════
    {
        "id": "serving",
        "label": "Model Serving",
        "icon": "\U0001F9E0",
        "blurb": "AI/ML serving spend split the way it actually bills: batch inference, "
                 "pay-per-token foundation models, and provisioned-throughput endpoints.",
        "panels": [
            {
                "id": "by_type",
                "title": "Serving spend by type (weekly)",
                "eli5": "Weekly $ split into Batch Inference, Pay-per-token, Provisioned throughput, "
                        "and custom models — the three foundation-model modes bill very differently.",
                "chart": "stacked_bar",
                "anomaly": {"type": "ts_spike", "time_col": "week", "value_col": "list_usd",
                            "group_col": "serving_type", "unit": "usd"},
                "sql": """
WITH price AS (
  SELECT sku_name, cloud, price_start_time, price_end_time,
         pricing.effective_list.default AS unit_usd FROM system.billing.list_prices
)
SELECT date_trunc('WEEK', u.usage_date) AS week,
       CASE
         WHEN u.product_features.model_serving.offering_type = 'BATCH_INFERENCE' THEN 'Batch inference'
         WHEN u.product_features.serving_type = 'FOUNDATION_MODEL' AND u.usage_type = 'TOKEN' THEN 'Pay-per-token'
         WHEN u.product_features.serving_type = 'FOUNDATION_MODEL' AND u.usage_type = 'COMPUTE_TIME' THEN 'Provisioned throughput'
         WHEN u.product_features.serving_type IN ('MODEL','GPU_MODEL') THEN 'Custom model'
         WHEN u.product_features.serving_type = 'FEATURE' THEN 'Feature serving'
         ELSE coalesce(u.product_features.serving_type, 'Other') END AS serving_type,
       round(sum(u.usage_quantity * pr.unit_usd), 2) AS list_usd
FROM system.billing.usage u
JOIN price pr ON u.sku_name=pr.sku_name AND u.cloud=pr.cloud
  AND u.usage_start_time >= pr.price_start_time
  AND (pr.price_end_time IS NULL OR u.usage_start_time < pr.price_end_time)
WHERE u.billing_origin_product = 'MODEL_SERVING'
  AND u.usage_date >= current_date() - INTERVAL 8 WEEKS
  AND u.usage_date < date_trunc('WEEK', current_date())   -- complete weeks only
GROUP BY 1, 2 HAVING sum(u.usage_quantity * pr.unit_usd) > 0
ORDER BY 1, list_usd DESC
""",
            },
            {
                "id": "by_endpoint",
                "title": "Spend by endpoint (30 days)",
                "eli5": "Which serving endpoint costs the most. A single dominant endpoint is the first "
                        "place to check batch size, caching, and whether it needs to be always-on.",
                "chart": "bar",
                "anomaly": {"type": "rank_outlier", "value_col": "list_usd",
                            "label_col": "endpoint", "unit": "usd"},
                "sql": """
WITH price AS (
  SELECT sku_name, cloud, price_start_time, price_end_time,
         pricing.effective_list.default AS unit_usd FROM system.billing.list_prices
)
SELECT coalesce(u.usage_metadata.endpoint_name, 'not populated') AS endpoint,
       round(sum(u.usage_quantity * pr.unit_usd), 2)             AS list_usd
FROM system.billing.usage u
JOIN price pr ON u.sku_name=pr.sku_name AND u.cloud=pr.cloud
  AND u.usage_start_time >= pr.price_start_time
  AND (pr.price_end_time IS NULL OR u.usage_start_time < pr.price_end_time)
WHERE u.billing_origin_product = 'MODEL_SERVING'
  AND u.usage_date >= current_date() - INTERVAL 30 DAYS
GROUP BY 1 ORDER BY list_usd DESC NULLS LAST LIMIT 20
""",
            },
            {
                "id": "pay_per_token",
                "title": "Pay-per-token usage by endpoint (30 days)",
                "eli5": "Foundation-model endpoints billed by the token — input vs output tokens and the "
                        "requests behind them. Output tokens usually cost several × input.",
                "chart": "table",
                "anomaly": {"type": "rank_outlier", "value_col": "total_tokens",
                            "label_col": "endpoint_name", "unit": "tokens"},
                "sql": """
WITH ent AS (   -- served_entities carries the endpoint name; endpoint_usage only has served_entity_id
  SELECT served_entity_id, max(endpoint_name) AS endpoint_name
  FROM system.serving.served_entities GROUP BY 1
)
SELECT coalesce(e.endpoint_name, 'unknown')          AS endpoint_name,
       count(*)                                      AS requests,
       sum(u.input_token_count)                      AS input_tokens,
       sum(u.output_token_count)                     AS output_tokens,
       sum(u.input_token_count + u.output_token_count) AS total_tokens
FROM system.serving.endpoint_usage u
LEFT JOIN ent e ON u.served_entity_id = e.served_entity_id
WHERE u.request_time >= current_date() - INTERVAL 30 DAYS
GROUP BY 1 ORDER BY total_tokens DESC NULLS LAST LIMIT 25
""",
            },
            {
                "id": "provisioned",
                "title": "Provisioned-throughput endpoints (weekly $)",
                "eli5": "Dedicated endpoints that bill for reserved compute-time whether or not they're "
                        "busy — the model-serving equivalent of an always-on warehouse.",
                "chart": "line",
                "anomaly": {"type": "ts_spike", "time_col": "week", "value_col": "list_usd",
                            "group_col": "endpoint", "unit": "usd"},
                "sql": """
WITH price AS (
  SELECT sku_name, cloud, price_start_time, price_end_time,
         pricing.effective_list.default AS unit_usd FROM system.billing.list_prices
)
SELECT date_trunc('WEEK', u.usage_date)                          AS week,
       coalesce(u.usage_metadata.endpoint_name, 'not populated') AS endpoint,
       round(sum(u.usage_quantity * pr.unit_usd), 2)             AS list_usd
FROM system.billing.usage u
JOIN price pr ON u.sku_name=pr.sku_name AND u.cloud=pr.cloud
  AND u.usage_start_time >= pr.price_start_time
  AND (pr.price_end_time IS NULL OR u.usage_start_time < pr.price_end_time)
WHERE u.billing_origin_product = 'MODEL_SERVING'
  AND u.product_features.serving_type = 'FOUNDATION_MODEL'
  AND u.usage_type = 'COMPUTE_TIME'
  AND u.product_features.model_serving.offering_type <=> 'PROVISIONED_THROUGHPUT'
  AND u.usage_date >= current_date() - INTERVAL 8 WEEKS
  AND u.usage_date < date_trunc('WEEK', current_date())   -- complete weeks only
GROUP BY 1, 2 HAVING sum(u.usage_quantity * pr.unit_usd) > 0
ORDER BY 1, list_usd DESC
""",
            },
            {
                "id": "ai_query_cost",
                "title": "Top 10 most expensive ai_query() calls (14 days)",
                "eli5": "The individual SQL ai_query() statements that cost the most — who ran each, "
                        "which model it called, and its attributed cost. Click a statement to open it "
                        "in Query History.",
                "chart": "table",
                "note": "Model is parsed from the ai_query() call's first argument. Cost = the "
                        "statement's share of its warehouse-hour $, by execution-time (same method as "
                        "the DBSQL board) — it's the SQL-compute cost of running the call, not a "
                        "separate model-serving token charge.",
                "anomaly": {"type": "rank_outlier", "value_col": "query_usd",
                            "label_col": "model_endpoint", "unit": "usd"},
                "sql": r"""
WITH wh_hour AS (
  SELECT warehouse_id, hour, sum(usage_quantity * unit_usd) AS hour_usd FROM (
    SELECT u.usage_metadata.warehouse_id AS warehouse_id,
           date_trunc('HOUR', u.usage_start_time) AS hour,
           u.usage_quantity, lp.pricing.effective_list.default AS unit_usd
    FROM system.billing.usage u JOIN system.billing.list_prices lp
      ON u.sku_name=lp.sku_name AND u.cloud=lp.cloud
     AND u.usage_start_time>=lp.price_start_time AND (lp.price_end_time IS NULL OR u.usage_start_time<lp.price_end_time)
    WHERE u.usage_metadata.warehouse_id IS NOT NULL AND u.usage_date >= current_date() - INTERVAL 14 DAYS
    QUALIFY ROW_NUMBER() OVER (PARTITION BY u.record_id ORDER BY lp.price_start_time DESC)=1
  ) GROUP BY 1,2
),
cfg AS (SELECT warehouse_id, max_clusters, change_time AS vf,
          coalesce(lead(change_time) OVER (PARTITION BY warehouse_id ORDER BY change_time), TIMESTAMP '9999-12-31') AS vt
        FROM system.compute.warehouses),
ws AS (SELECT workspace_id, max(workspace_url) AS workspace_url FROM system.access.workspaces_latest GROUP BY 1),
q AS (
  SELECT statement_id, workspace_id, compute.warehouse_id AS warehouse_id, date_trunc('HOUR', start_time) AS hour, executed_by,
         lower(regexp_extract(statement_text, "ai_query\\(\\s*'([^']+)'", 1)) AS model_endpoint,
         execution_duration_ms,
         sum(execution_duration_ms) OVER (PARTITION BY compute.warehouse_id, date_trunc('HOUR', start_time)) AS busy_ms
  FROM system.query.history
  WHERE start_time >= current_date() - INTERVAL 14 DAYS AND execution_duration_ms > 0
    AND statement_text ILIKE '%ai_query(%'
),
alloc AS (
  SELECT q.*, w.hour_usd, GREATEST(coalesce(c.max_clusters,1)*3600000.0, q.busy_ms) AS capacity_ms
  FROM q JOIN wh_hour w USING (warehouse_id, hour)
         LEFT JOIN cfg c ON c.warehouse_id=q.warehouse_id AND q.hour>=c.vf AND q.hour<c.vt
)
SELECT a.statement_id, a.executed_by,
       CASE WHEN a.model_endpoint = '' OR a.model_endpoint IS NULL THEN '(not detected)' ELSE a.model_endpoint END AS model_endpoint,
       round(a.execution_duration_ms/60000.0, 2)                      AS exec_min,
       round(a.hour_usd * a.execution_duration_ms / a.capacity_ms, 2)     AS query_usd,
       ws.workspace_url   -- the workspace this statement actually ran in
FROM alloc a LEFT JOIN ws ON a.workspace_id = ws.workspace_id
ORDER BY query_usd DESC NULLS LAST LIMIT 10
""",
            },
        ],
    },

    # ══════════════════════════════════════════════════════════════════════════
    # UNIT ECONOMICS — what a query, a TiB read and an active user cost.
    # Danny's Genie One skill, rebuilt on system tables only (it read internal
    # finance tables). tools/gen_unit_economics_skill.py writes the skill back
    # out from this SQL.
    # ══════════════════════════════════════════════════════════════════════════
    {
        "id": "econ",
        "label": "Unit economics",
        "icon": "\U0001F9EE",
        "blurb": "What a query, a terabyte read and an active user cost this account, from system "
                 "tables only, with the share of spend each ratio rests on.",
        "panels": [
            {
                "id": "kpis",
                "title": "Unit economics (28 days)",
                "eli5": "The raw inputs and the unit costs built from them, over one window. Value is "
                        "at list price; discounted uses your account prices where they exist. Coverage "
                        "says which workspaces each figure rests on.",
                "chart": "table",
                "note": "Billing sees every workspace, but query history and audit only list the "
                        "workspaces they cover, so each ratio divides only the spend of the workspaces "
                        "its denominator can see. User counts are approximate (approx_count_distinct). "
                        "With no rows in system.billing.account_prices, the discounted column and the "
                        "effective discount say unavailable rather than guess.",
                "anomaly": None,
                "sql": _ECON_KPI_SQL,
            },
            {
                "id": "trend",
                "title": "Weekly unit costs (12 weeks)",
                "eli5": "Cost per query, per TiB read and per weekly active user, one 7-day bucket at a "
                        "time, ending where the table above ends. A unit cost jumping above its own "
                        "normal means the same work got pricier.",
                "chart": "lines",
                "note": "Weekly buckets do not overlap, so a jump shows in full in the week it happens. "
                        "The user ratio uses weekly active users for that reason: a rolling monthly count "
                        "would spread one bad week across four.",
                "anomaly": {"type": "ts_spike", "time_col": "week", "unit": "usd",
                            "value_cols": ["cost per query", "cost per TiB read",
                                           "cost per weekly active user"]},
                "sql": _ECON_TREND_SQL,
            },
            {
                "id": "by_product",
                "title": "Spend by product (28 days)",
                "eli5": "Spend per billing product with its share of the total, at your account prices "
                        "where they exist and list price otherwise.",
                "chart": "bar",
                "anomaly": None,
                "sql": _ECON_SPLIT_SQL.format(label="product", label_as="product",
                                              pct_as="pct_of_total", where=""),
            },
            {
                "id": "serverless",
                "title": "Serverless vs classic (28 days)",
                "eli5": "How much of the spend runs on serverless compute, from the billing record's own "
                        "is_serverless flag. Model Serving, Vector Search, Lakebase, storage and networking "
                        "rows mostly carry no flag, so they get their own bar instead of a guess.",
                "chart": "bar",
                "anomaly": None,
                "sql": _ECON_SPLIT_SQL.format(
                    label="CASE WHEN is_serverless THEN 'Serverless' "
                          "WHEN NOT is_serverless THEN 'Classic' ELSE 'Not flagged in billing' END",
                    label_as="compute", pct_as="pct_of_total", where=""),
            },
            {
                "id": "dbsql_tier",
                "title": "DBSQL spend by warehouse tier (28 days)",
                "eli5": "SQL warehouse spend by tier. Billing leaves sql_tier empty on serverless "
                        "warehouses, so serverless is read from is_serverless.",
                "chart": "bar",
                "anomaly": None,
                "sql": _ECON_SPLIT_SQL.format(
                    label="CASE WHEN is_serverless THEN 'SERVERLESS' ELSE coalesce(sql_tier, 'UNKNOWN') END",
                    label_as="sql_tier", pct_as="pct_of_sql_spend", where="WHERE product = 'SQL'"),
            },
        ],
    },
]


def board_meta(b: dict) -> dict:
    """Public board descriptor (no SQL) for the frontend."""
    return {
        "id": b["id"], "label": b["label"], "icon": b["icon"], "blurb": b["blurb"],
        "panels": [{"id": p["id"], "title": p["title"], "eli5": p["eli5"],
                    "chart": p["chart"], "has_anomaly": bool(p.get("anomaly")),
                    "note": p.get("note")}
                   for p in b["panels"]],
    }


def all_meta() -> list:
    return [board_meta(b) for b in BOARDS]


def find_panel(board_id: str, panel_id: str):
    b = next((x for x in BOARDS if x["id"] == board_id), None)
    if not b:
        return None, None
    p = next((x for x in b["panels"] if x["id"] == panel_id), None)
    return b, p
