# Unit economics

The **Unit economics** board (Observability) answers what a query, a terabyte read and an active
user cost this account, and what discount it gets. It reads system tables only, so it works in any
account with system tables enabled. The same SQL also ships as a Genie One skill,
[`docs/skills/unit-economics/SKILL.md`](skills/unit-economics/SKILL.md), which
`tools/gen_unit_economics_skill.py` writes from `app/boards.py`. A test fails when the two drift.

## The window

Every query uses one window of 28 full days. `system.billing.usage` is still loading its newest
day, so the window ends the day before `max(usage_date)` and starts 27 days earlier. Query history
(`start_time`) and audit (`event_date`) use the same dates. Ending on `current_date()` instead would
count a partial day, and every unit cost would look cheaper than it is.

The weekly trend uses twelve 7-day buckets that end on the same day.

## Prices

| Column | Meaning |
|---|---|
| `value` | List price: `usage_quantity × list_prices.pricing.effective_list.default`, the same as the rest of LakeLens. |
| `discounted` | The account's own price from `system.billing.account_prices` (`pricing.default`), joined on SKU, cloud, usage unit, currency and price window. |

When the account has no rows in `account_prices`, every discounted figure and the effective
discount read **unavailable: no account prices**. When only some SKUs have a price, those rows use
it, the rest stay at list, and the figure is marked **(partial)**. The effective discount row shows
how much of list spend has an account price. LakeLens never invents a discount for a SKU without a
price. Account prices in a different currency from list are not compared.

## Coverage

Billing sees every workspace in the account. Query history and audit see fewer: they are regional,
and they only hold workspaces with activity. Dividing all spend by queries that only some workspaces
report would overstate every unit cost. So each ratio divides only the spend of the workspaces its
denominator can see, and the `coverage` column says what share of spend that is.

On one large demo account, a third of SQL warehouse spend sat in workspaces that query history does
not cover. Without the coverage rule, DBSQL cost per TiB read would have read 1.5 times too high.

| Workspace set | Used by | Rule |
|---|---|---|
| Query workspaces | Cost per query, cost per TiB read | At least one `FINISHED` query in `system.query.history` in the window |
| Audit workspaces | Cost per MAU (all and human) | At least one event in `system.access.audit` in the window |
| DBSQL workspaces | DBSQL cost per TiB read | At least one finished query on a SQL warehouse (`compute.warehouse_id` is set); only `SQL` product spend counts |

## Metrics

| Metric | Formula | Caveat |
|---|---|---|
| a. Cost per query | Spend of query workspaces / finished queries | Counts every finished query, including tiny metadata reads, so it is a fleet average. |
| b. Cost per MAU, all | Spend of audit workspaces / distinct audit identities | People and service principals both count. |
| c. Cost per MAU, human | Spend of audit workspaces / identities that are emails | An email-like identity is a person; a UUID is a service principal. |
| d. Cost per TiB read | Spend of query workspaces / TiB read by finished queries | `read_bytes` is summed as DOUBLE because the BIGINT sum overflows. |
| e. DBSQL cost per TiB read | SQL spend of DBSQL workspaces / TiB read on SQL warehouses | Warehouse queries only. |
| Effective discount | 1 - account-priced spend / list spend, on SKUs with an account price | Unavailable without account prices; partial with some. |

Raw inputs in the same table: total spend (with the number of workspaces it covers), average daily
spend over the last 7 days, active products, MAU, human MAU, WAU and DAU (all `approx_count_distinct`, so they are
approximate), finished queries, data read, DBSQL queries and DBSQL data read.

The other three panels split the same 28 days of spend by billing product, by serverless or classic
(from the billing row's `is_serverless` flag, with unflagged products such as Model Serving and
storage in their own bar), and DBSQL spend by warehouse tier (`sql_tier` is empty on serverless
warehouses, so serverless comes from `is_serverless`).

## The weekly trend and its alerts

The trend plots cost per query, cost per TiB read and cost per weekly active user over twelve
non-overlapping weeks, on a logarithmic axis because the three sit orders of magnitude apart. Weekly
active users stand in for MAU there: a rolling 28-day count would spread one bad week across four
and hide the jump.

The detector compares the latest week of each cost with the median of its earlier weeks. A week at
`ANOMALY_MEDIUM_X` times that normal is medium and at `ANOMALY_HIGH_X` times is high: the same
multiples the Alerts tab uses, set once as bundle variables. No dollar threshold is involved, so a
small account and a large one are read the same way.

## For maintainers

- Change the SQL in `app/boards.py`, then run `python3 tools/gen_unit_economics_skill.py`. The
  generator refuses output with account ids, workspace URLs or long dashes.
- Keep the window's scalar subqueries in `WHERE` clauses. Inside an expression they push Photon onto
  a slow path. On a large account the audit part of the KPI query took 101 seconds that way, against
  25 seconds with `days_back` columns.
- `account_prices` is a single CTE so a test can swap in synthetic prices and check the discount
  maths against an independent query.
