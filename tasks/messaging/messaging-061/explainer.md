# messaging-061 — explainer

## How to investigate

The headline is a failed scale-out: instance count went 4 → 16 and the lag
curve did not move (`fixtures/consumer-lag.csv` — lag grows monotonically on
every partition straight through the scaling window). When scaling out does
nothing, the bottleneck is a parallelism cap somewhere below the instance
count. For Event Hubs, the degree of parallelism is the **partition count**.

Check the hub itself first. `fixtures/az-eventhubs-eventhub-show.json` shows
`partitionCount: 4` (partitionIds 0–3). Event Hubs' consumption model is
strict: within one consumer group, each partition is owned by exactly one
processor instance at a time. So this hub can keep at most 4 instances busy
in a group — the 16-instance pool is structurally unable to help.

The processor's own log confirms the arithmetic. `fixtures/processor.log`
shows instances `-004`, `-005`, `-007`, `-008`, `-012`, `-013`, `-015` and
others repeating "no partitions assigned; waiting for ownership changes",
while exactly four instances (one per partition, e.g. `-000` on partition 0)
log event-batch processing. The ownership protocol is working perfectly —
there is simply nothing for twelve of the sixteen instances to own. The
checkpoint store agrees: `fixtures/checkpoint-blobs.json` has exactly four
checkpoint blobs, all advancing, one per partition.

Ruling out the alternative cap is quick. The symptom already says the
namespace is not rate limited, and `fixtures/throughput-units.json` shows
ingress at 38% and egress at 51% of the 4-TU allocation with zero throttled
requests — the hub is nowhere near its quota, and the processors that own
partitions keep up with their partitions' pace. The binding constraint is
partition count, not throughput units and not consumer health.

## Why the red herring misleads

`fixtures/throughput-units.json` is the trap: a "lag" incident on Event Hubs
usually *is* a throughput-units story, and the file even shows the SKU is
Standard with 4 TUs — low enough to look suspicious. But the utilisation
lines disprove it: ingress 38%, egress 51%, `throttledRequestsLast24h: 0`.
Throttling also has a signature this incident lacks — retry errors and
stuttering throughput in the processor logs, and lag that flattens when
traffic dips — while here the four partition-owning processors process
smoothly and the lag grows evenly on all four partitions. Adding
throughput units would raise the ceiling the hub is nowhere near touching.

## Root cause

The telemetry hub has `partitionCount: 4`
(`fixtures/az-eventhubs-eventhub-show.json`), and within the
telemetry-processors consumer group each partition can be owned by at most
one processor instance. Scaling the processor pool from 4 to 16 instances
therefore changed nothing: at most 4 instances ever own partitions and the
rest log "no partitions assigned; waiting for ownership changes"
(`fixtures/processor.log`). Total event-processing parallelism is capped at
4, the ingress rate exceeds what 4 partitions can drain
(`fixtures/consumer-lag.csv` — lag rises monotonically on all partitions),
and dashboards fall hours behind.

## Fix

Increase the hub's degree of parallelism, in whichever of these forms the
platform allows:

1. **New hub with more partitions** — Standard-tier partition count is fixed
   at creation, so create `telemetry` with 16 or 32 partitions, switch
   producers, drain the old hub, and repoint the processor. Plan the
   cutover; a brief gap or replay bridge may be needed.
2. **Premium/Dedicated namespace** — Premium and Dedicated support
   increasing partition count in place, making this a resize rather than a
   migration.
3. **Re-shard across hubs** — split telemetry into multiple hubs (e.g. by
   device region) so total partitions exceed the processor instance count.

Do **not** add processor instances or throughput units first: neither moves
the cap that is binding here.

## Prevention

- **Size partitions to the fleet, not the fleet to partitions.** When
  provisioning an Event Hub, set partition count to the maximum expected
  consumer parallelism (it cannot be lowered or raised in place on
  Standard), not to today's instance count.
- **Alert on idle consumers.** Instances in a consumer group with zero owned
  partitions for more than a few minutes ("no partitions assigned" rate)
  indicate parallelism is capped by the hub — alert on it the same way you
  alert on lag itself.
- **Partition-count review in change process.** The 4 → 16 scale-out was a
  plausible and correctly-executed response; a pre-change checklist item
  ("verify hub parallelism ≥ target instance count before scaling
  consumers") catches it before spend, not after.

## Further reading

- [Event Hubs — partitions](https://learn.microsoft.com/azure/event-hubs/event-hubs-features#partitions)
- [Event Hubs — availability and consistency (consumer group ownership)](https://learn.microsoft.com/azure/event-hubs/event-hubs-availability-and-consistency)
- [Azure Event Hubs Premium — resize partitions](https://learn.microsoft.com/azure/event-hubs/elastic-premium-partition-scaling)
- [Create an event hub with more partitions](https://learn.microsoft.com/azure/event-hubs/event-hubs-create)
