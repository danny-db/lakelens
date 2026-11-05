"""
names.py: the one label every result shows for a workspace, warehouse, job, pipeline
or cluster, so a hunt, a measure and a board name an entity the same way.

A label is the entity's latest name. When another entity of the same kind in the
account has that name, the label adds its workspace, and when that workspace holds
two of them it adds the id too, so a label never stands for two entities. An entity
no system table names is labelled by its kind and id, and a warehouse by a short id
and its workspace. Results keep the raw id in a column of its own for lookups.

A query opens its WITH clause with ctes(...) and labels each id with ref(), after
it has aggregated:

    job = ref("job", "c.job_id", "c.workspace_id")
    f"WITH {ctes('job')}, ... SELECT {job.label} AS job_name, c.job_id FROM contrib c {job.join}"

Every names CTE holds one row per (workspace_id, id), the key the join uses, so a
label never changes a row count or a metric.
"""
from typing import NamedTuple


class Source(NamedTuple):
    table: str
    id_col: str
    name_col: str
    latest_by: str  # the newest row per id holds the current name


SOURCES = {
    "warehouse": Source("system.compute.warehouses", "warehouse_id", "warehouse_name", "change_time"),
    "job": Source("system.lakeflow.jobs", "job_id", "name", "change_time"),
    "pipeline": Source("system.lakeflow.pipelines", "pipeline_id", "name", "change_time"),
    "cluster": Source("system.compute.clusters", "cluster_id", "cluster_name", "change_time"),
}

# system.lakeflow.jobs lists only some of the jobs that run (on one large account,
# 7,519 of the 24,853 that query history names), while billing names every job it
# bills, so a job the jobs table misses takes its latest billed name.
_BILLED_JOBS = """
    UNION ALL
    SELECT workspace_id, usage_metadata.job_id, trim(max_by(usage_metadata.job_name, usage_end_time)),
           1, max(usage_end_time)
    FROM system.billing.usage
    WHERE usage_metadata.job_id IS NOT NULL AND usage_metadata.job_name IS NOT NULL
      AND usage_date >= current_date() - INTERVAL 8 WEEKS
    GROUP BY 1, 2"""

_WORKSPACES = """workspace_names AS (
  SELECT workspace_id,
         CASE WHEN count(*) OVER (PARTITION BY name) = 1 THEN name ELSE name || ' · ' || workspace_id END AS label
  FROM (SELECT workspace_id, trim(workspace_name) AS name FROM system.access.workspaces_latest
        WHERE trim(workspace_name) <> ''
        QUALIFY row_number() OVER (PARTITION BY workspace_id ORDER BY create_time DESC) = 1)
)"""


def _workspace(label: str, ws_expr: str) -> str:
    return f"coalesce({label}, 'workspace ' || {ws_expr})"


def _cte(kind: str) -> str:
    s = SOURCES[kind]
    rows = (f"SELECT workspace_id, {s.id_col} AS id, trim({s.name_col}) AS name, 0 AS pref, "
            f"{s.latest_by} AS seen FROM {s.table}" + (_BILLED_JOBS if kind == "job" else ""))
    ws = _workspace("w.label", "n.workspace_id")
    return f"""{kind}_names AS (
  SELECT n.workspace_id, n.id,
         CASE WHEN count(*) OVER (PARTITION BY n.name) = 1 THEN n.name
              WHEN count(*) OVER (PARTITION BY n.name, n.workspace_id) = 1 THEN n.name || ' · ' || {ws}
              ELSE n.name || ' · ' || {ws} || ' · ' || n.id END AS label
  FROM (SELECT workspace_id, id, name FROM ({rows})
        WHERE name <> ''
        QUALIFY row_number() OVER (PARTITION BY workspace_id, id ORDER BY pref, seen DESC) = 1) n
  LEFT JOIN workspace_names w ON w.workspace_id = n.workspace_id
)"""


def ctes(*kinds: str) -> str:
    """The CTEs that label these kinds, for a WITH clause. workspace_names always
    comes first: every other label uses it."""
    unknown = set(kinds) - set(SOURCES) - {"workspace"}
    if unknown:
        raise ValueError(f"no names for {sorted(unknown)}")
    return ",\n".join([_WORKSPACES] + [_cte(k) for k in SOURCES if k in kinds])


class Ref(NamedTuple):
    join: str    # LEFT JOIN clause(s) to add after the FROM that holds the id
    label: str   # the label; NULL only when the id is NULL
    tagged: str  # the label led by its kind ("job nightly_etl"), for a column that mixes kinds


def _short(expr: str) -> str:
    return f"left({expr}, 4) || '…' || right({expr}, 4)"


def ref(kind: str, id_expr: str, ws_expr: str = "", alias: str = "") -> Ref:
    """Label one id. ws_expr is the row's workspace_id (a workspace is its own);
    alias names the joins when one query labels two columns of the same kind."""
    a = alias or f"{kind}_n"
    if kind == "workspace":
        label = _workspace(f"{a}.label", id_expr)
        return Ref(f"LEFT JOIN workspace_names {a} ON {a}.workspace_id = {id_expr}", label, label)
    if kind not in SOURCES or not ws_expr:
        raise ValueError(f"ref({kind!r}) needs a known kind and the row's workspace_id")
    join = f"LEFT JOIN {kind}_names {a} ON {a}.workspace_id = {ws_expr} AND {a}.id = {id_expr}"
    fallback = f"'{kind} ' || {id_expr}"
    if kind == "warehouse":
        join += f"\nLEFT JOIN workspace_names {a}_ws ON {a}_ws.workspace_id = {ws_expr}"
        fallback = (f"CASE WHEN {id_expr} IS NOT NULL THEN concat_ws(' · ', 'warehouse ' || {_short(id_expr)}, "
                    f"{_workspace(f'{a}_ws.label', ws_expr)}) END")
    return Ref(join, f"coalesce({a}.label, {fallback})", f"coalesce('{kind} ' || {a}.label, {fallback})")
