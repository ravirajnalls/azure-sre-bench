# data-062 — explainer

## How to investigate

Start where the pain is: `fixtures/cart-api.log` shows `CosmosException 429`
with substatus 3200 (throttled) hitting `POST /cart/add` in bursts from
18:32Z onward — and every sampled 429 carries `partitionKeyRegion=eastus`,
while reads for `westeurope` and `australiaeast` carts complete in 6–9 ms
(lines 5, 6, 8, 15). One region's requests throttle; the others sail
through. That asymmetry is the tell: a container-wide capacity problem
would throttle everything roughly evenly.

Quantify it per partition. `fixtures/normalized-ru-by-partition.csv` is the
decisive file: from 18:00Z to 19:00Z, physical partition 0 climbs from 38%
normalized RU consumption to 100% at 18:40Z and stays pinned in the 90s,
while partitions 1 and 2 never leave the low teens. Normalized RU
consumption is per-partition: 100% means *that partition* has zero spare
capacity and every excess request earns a 429, no matter what the container
total is doing.

What is the key? `fixtures/az-cosmosdb-sql-container-show.json:15-20` shows
`partitionKey.paths: ["/region"]` — three storefront regions, one hash key.
`fixtures/sample-documents.json` closes the loop: 82% of documents are
`region=eastus`, so the sale-event traffic concentrates on one logical
partition, which is exactly the partition pinned at 100%.

## Why the red herring misleads

`fixtures/total-ru.csv` shows container-wide consumption peaking at ~31% of
the 30,000 RU/s provisioned — which is why the dashboard "looked fine" and
why the tempting conclusion is "we have headroom, so the 429s must be a
burst/accounting artifact." But Cosmos DB does not pool throughput across
partitions: each physical partition gets provisioned÷N, and a request can
only be served by the partition that owns its key. Total utilization is
arithmetic across a mostly-idle container; throttling is decided per
partition. The proposal in `fixtures/cart-api.log:17` (raise to 60,000
RU/s or autoscale) buys more capacity on partitions that are already idle —
the eastus partition's normalized consumption would stay pinned, because
provisioned-per-partition rises only proportionally with demand. Headroom
on the average is not headroom on the max.

## Root cause

A hot partition. The `cartitems` container is partitioned by `/region`
(`fixtures/az-cosmosdb-sql-container-show.json:15-20`), 82% of documents
are `eastus` (`fixtures/sample-documents.json`), and during peak that
single logical/physical partition saturates — 100% normalized RU
consumption (`fixtures/normalized-ru-by-partition.csv`) — while the
container overall runs at ~30% of provisioned (`fixtures/total-ru.csv`).
Per-partition throughput enforcement turns that imbalance into 429
substatus 3200 on cart-api.

## Fix

Repartition onto a higher-cardinality key — `partition key` changes require
a new container and data copy, so plan it as a migration:

1. Create a new container with a partition key that spreads the sale
   traffic: `/customerId` alone, a synthetic key like
   `CONCAT(region, '-', customerId)`, or hierarchical partition keys
   `["/region", "/customerId"]` (keeps region-local reads efficient while
   fanning out the write load).
2. Copy data across (Azure Cosmos DB data-migration tooling / Spark
   connector / change-feed), update `cart-api` connection strings and any
   cross-partition queries, and cut over.
3. Verify per-partition normalized RU consumption spreads flat across
   partitions under the same load profile.

Do **not** raise RU/s or flip to autoscale — `fixtures/total-ru.csv` proves
the container overall is ~70% idle; only the one partition saturates, and
per-partition allocation scales proportionally. The skew, not the budget,
is the constraint.

## Prevention

- **Cardinality review at design time:** a partition key should have
  enough distinct values — and enough demand distribution across them —
  to keep any single logical partition under ~1–2% of traffic. `region`
  with three storefronts was knowably low-cardinality before the sale.
- **Per-partition alerting:** page on `normalized_ru_consumption` p99 by
  physical partition (the metric behind
  `fixtures/normalized-ru-by-partition.csv`) breaching ~80%, not just on
  aggregate 429 counts or container-wide RU — the aggregate will look
  healthy right up until checkout dies.

## Further reading

- [Cosmos DB — partitioning and horizontal scaling](https://learn.microsoft.com/azure/cosmos-db/partitioning-overview)
- [Cosmos DB — diagnose and troubleshoot 429 request rate too large](https://learn.microsoft.com/azure/cosmos-db/troubleshoot-request-rate-too-large)
- [Cosmos DB — normalized RU consumption metric](https://learn.microsoft.com/azure/cosmos-db/monitor-normalized-request-charges)
- [Hierarchical partition keys](https://learn.microsoft.com/azure/cosmos-db/hierarchical-partition-keys)
