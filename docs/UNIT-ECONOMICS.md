# Unit economics

The **Unit economics** board (Observability) answers what a query, an active user and a terabyte
under management cost this account, and what discount it gets. It reads system tables only, so it
works in any account with system tables enabled. The same SQL also ships as a Genie One skill,
[`docs/skills/unit-economics/SKILL.md`](skills/unit-economics/SKILL.md), which
`tools/gen_unit_economics_skill.py` writes from `app/boards.py`. A test fails when the two drift.

The board opens with four tiles: cost per query, cost per MAU (all identities), cost per MAU
(humans) and cost per TiB of data under management, per month. Each tile has an ⓘ that shows its definition on
hover and keeps it open on tap. The raw inputs they are built from sit in the table underneath.

## The window

Every query uses one window of 28 full days. Billing writes most of a day's records that day and the
rest the next day, so the newest usage day counts only once its usage runs to midnight and its
next-day records have landed. Otherwise the window ends the day before. On one large demo account,
89.8% of billing records landed the same day, 10.2% the next day and 0.03% later (mostly
corrections), and the newest usage day qualified once its next-day batch matched the day before's. Query history
(`start_time`), audit (`event_date`) and storage (`snapshot_date`) use the same dates. Ending on
`current_date()` instead would count a partial day, and every unit cost would look cheaper than it
is.

The weekly trend uses twelve 7-day buckets that end on the same day.

## Prices

Each figure uses the account's own price from `system.billing.account_prices` (`pricing.default`,
joined on SKU, cloud, usage unit, currency and price window) where one is set, and list price
(`list_prices.pricing.effective_list.default`, as in the rest of LakeLens) otherwise. LakeLens never
invents a discount for a SKU without a price.

With no rows in `account_prices`, every figure is at list price, the tiles say so, and the
effective discount row is left out because it has nothing to show. When only some SKUs have a price,
the effective discount is marked **(partial)** and its coverage says how much of list spend has an
account price. Account prices in a different currency from list are not compared.

## Coverage

Billing sees every workspace in the account. Query history, audit and table metrics see fewer: they
are regional, and query history and audit only hold workspaces with activity. Dividing all spend by
queries that only some workspaces report would overstate every unit cost. So each ratio divides only
the spend of the workspaces its denominator can see, and the coverage says what share of spend that
is.

| Workspace set | Used by | Rule |
|---|---|---|
| Query workspaces | Cost per query | At least one `FINISHED` query in `system.query.history` in the window |
| Audit workspaces | Cost per MAU (all and humans) | At least one event in `system.access.audit` in the window |
| Every workspace | Cost per TiB of data under management | Total spend, against the tables the storage history lists |

## Metrics

| Metric | Formula | Caveat |
|---|---|---|
| Cost per query | Spend of query workspaces / finished queries | Counts every finished query, including tiny metadata reads, so it is a fleet average. |
| Cost per MAU, all identities | Spend of audit workspaces / distinct audit identities | People and service principals both count. |
| Cost per MAU, humans | Spend of audit workspaces / identities that are emails | An email-like identity is a person; a UUID is a service principal. |
| Cost per TiB of data under management, per month | Total spend for a 30-day month (the window's spend / its days × 30) / total TiB of table data | See below. |
| Effective discount | 1 - account-priced spend / list spend, on SKUs with an account price | Shown only when the account has prices; partial with some. |

**Data under management** comes from `system.storage.table_metrics_history`: each table at its
newest snapshot in the window, unless it was dropped by the window's end, at its total size
(`total_bytes`, active plus vacuumable files) where the table reports one and its `active_bytes`
otherwise. Cost per TiB is a month of total spend over that volume. It sets all platform spend
(compute, AI, apps) against the data held, so it is not a storage price, and the tile says so. On an
account whose spend is mostly AI or apps and whose data is small, such as a demo account, it reads
very high; on a data-heavy production account it reads in the low hundreds of dollars per TiB. The
table lists only the current region's
metastore, while total spend covers every workspace, so on an account with workspaces in several
regions the ratio reads high. It is a new system table: on one large demo account its history began
a week before this was written and was still filling in, and `total_bytes` was empty, so the board
used active sizes there. Where the account can't read the table, the board runs a variant without
it: that tile and its input row say what is missing, and every other figure still shows.

Raw inputs in the table: the window, total spend (with the number of workspaces it covers), average
daily spend over the last 7 days, active products, MAU, human MAU, WAU and DAU (all
`approx_count_distinct`, so they are approximate), finished queries, data read, DBSQL queries, DBSQL
data read and data under management. `read_bytes` is summed as DOUBLE because the BIGINT sum
overflows.

The other three panels split the same 28 days of spend by billing product, by serverless or classic
(from the billing row's `is_serverless` flag, with unflagged products such as Model Serving and
storage in their own bar), and DBSQL spend by warehouse tier (`sql_tier` is empty on serverless
warehouses, so serverless comes from `is_serverless`).

## The weekly trend and its alerts

The trend plots cost per query, cost per TiB of data under management and cost per weekly active
user over twelve non-overlapping weeks, on a logarithmic axis because the three sit orders of
magnitude apart. Weekly active users stand in for MAU there: a rolling 28-day count would spread one
bad week across four and hide the jump. Cost per TiB under management is each week's total spend,
scaled to a 30-day month, over that week's data volume, so its last point matches the tile. Its line starts when the table metrics history does, and while it has fewer
than two weeks the panel says so, because one point can't draw a line.

The detector compares the latest week of each cost with the median of its earlier weeks. A week at
`ANOMALY_MEDIUM_X` times that normal is medium and at `ANOMALY_HIGH_X` times is high: the same
multiples the Alerts tab uses, set once as bundle variables. No dollar threshold is involved, so a
small account and a large one are read the same way.

## For maintainers

- Change the SQL in `app/boards.py`, then run `python3 tools/gen_unit_economics_skill.py`. The
  generator refuses output with account ids, workspace URLs or long dashes.
- A panel that reads `system.storage` carries a `without_storage` variant. The app runs it when the
  first run fails naming `system.storage`, so an account without that table loses one figure, not
  the board.
- Keep the window's scalar subqueries in `WHERE` clauses. Inside an expression they push Photon onto
  a slow path. On a large account the audit part of the KPI query took 101 seconds that way, against
  25 seconds with `days_back` columns.
- `account_prices` is a single CTE so a test can swap in synthetic prices and check the discount
  maths against an independent query.
