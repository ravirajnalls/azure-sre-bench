# data-063 — explainer

## How to investigate

The symptom pins the change window: reports-api timeouts "since Monday",
and `fixtures/dtu-usage.csv` shows DTU leaping from ~30% to 96.4% in the
02:15Z slot on 2026-10-26 and never recovering — including off-peak hours,
which rules out a traffic-driven cause. Something structural changed at
exactly that minute. Monday 02:15Z is when
`fixtures/migration.sql` ran: line 14 is
`DROP INDEX IF EXISTS IX_Orders_CustomerId ON dbo.Orders` — deleted as
"old reporting indexes" in a cleanup change.

Confirm the query is the victim. `fixtures/query-store-top-queries.csv`:
the top query `q-9f21` (`WHERE CustomerId = @CustomerId`) flips from
`plan_4471` to `plan_9523` in the very next slot after the migration, and
its numbers move by four orders of magnitude — avg_logical_reads 309 →
8,873,001, avg_duration 47 ms → 18.4 s. The neighboring invoice query
`q-3a04`, which kept its plan, still runs at 38–41 ms.

Name the bad plan: `fixtures/execution-plan.xml` shows `plan_9523` is a
`Clustered Index Scan` on `PK_Orders` with `Scanning="Full scan"` against
a 1.2-billion-row table, feeding a sort that spills to tempdb. With no
nonclustered index on `CustomerId` left to seek, SQL Server reads the
entire table per customer lookup.

## Why the red herring misleads

`fixtures/connection-pool.log` is dramatic — wait queues at 138 with
2.4 s waits, `in_use=200/200 — saturated` — and "connection pool exhaustion"
is a tempting root cause. Two things give it away as a symptom: the
saturation **starts and ends with the slow-query window** (the log's own
dba-thread line on 10-29 says so), and no leak was found — connections
are always returned to the pool. A 24-second query holds its connection
for 24 seconds; with enough concurrent report requests the pool drains
naturally. Treating the pool as the cause would lead to raising
`max_size`, which adds concurrent 9-million-read scans and makes the DTU
saturation worse.

## Root cause

The Monday migration (`fixtures/migration.sql`, 2026-10-26 02:15Z) dropped
the nonclustered index `IX_Orders_CustomerId` on `dbo.Orders`. The
reports-api top query (`q-9f21`, `WHERE CustomerId = @CustomerId`) lost
its seek and regressed to a full clustered index scan of the 1.2B-row
table (`fixtures/execution-plan.xml`), with logical reads up ~30,000x and
duration up ~500x (`fixtures/query-store-top-queries.csv`) — which pegs
DTU at ~100% (`fixtures/dtu-usage.csv`) and exhausts the API's
connection pool as a downstream symptom.

## Fix

Recreate the index:

```sql
CREATE NONCLUSTERED INDEX IX_Orders_CustomerId
    ON dbo.Orders (CustomerId)
    INCLUDE (CreatedAtUtc, TotalAmount)
    WITH (ONLINE = ON);
```

Including `CreatedAtUtc` and `TotalAmount` covers the query's sort and
select list so the plan is a pure seek with no key lookup. `ONLINE = ON`
matters on an 840 GB, 1.2B-row table. Verify in Query Store that `q-9f21`
binds to a seek-based plan and logical reads drop back to the ~310 range,
then revert the 30s→60s command-timeout stopgap. Do **not** raise the
connection-pool max size — that schedules more concurrent full scans.

## Prevention

- **Index-drop review gate:** any `DROP INDEX` in a migration must list
  Query Store usage stats for the index in the change ticket; "last
  surveyed in 2023" is not evidence an index is unused.
- **Plan-regression alert:** page when a top query's `plan_id` flips and
  avg_logical_reads rises by >10x in the same slot — Query Store had the
  regression on day one; the DTU alarm was the third-order effect.

## Further reading

- [Query Store — tune performance by finding plan regressions](https://learn.microsoft.com/sql/relational-databases/performance/monitoring-performance-by-using-the-query-store)
- [Showplan XML — Clustered Index Scan](https://learn.microsoft.com/sql/relational-databases/showplan-xml)
- [CREATE INDEX — ONLINE option](https://learn.microsoft.com/sql/t-sql/statements/create-index-transact-sql)
- [Connection pooling in Azure SQL](https://learn.microsoft.com/azure/azure-sql/database/connect-query-dotnet-core)
