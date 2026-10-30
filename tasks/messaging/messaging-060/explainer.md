# messaging-060 — explainer

## How to investigate

Two facts in the symptom pattern the incident: the active backlog is nearly
**empty** while the **dead-letter** count climbs by thousands per hour, and
throughput to the database is zero. An empty active queue means consumers are
taking delivery of messages — the bottleneck is not consumption rate. The
question becomes: what do the consumers do with the messages they take?

Look at the queue first to bound the problem. `fixtures/az-servicebus-queue-show.json`
shows `activeMessageCount: 14`, `deadLetterMessageCount: 42318`, and —
importantly — `maxDeliveryCount: 3`. That is Service Bus telling you its
default poison-message policy is armed: a message delivered three times
without completion goes to the DLQ.

Confirm what the messages died of. `fixtures/dlq-peek.json` peeks the DLQ and
every message carries `deadLetterReason: MaxDeliveryCountExceeded` with a
`deadLetterErrorDescription` that names the disease: `failed to deserialize
OrderCreated payload — expected property 'amountCents' (number), found legacy
property 'amount' (string)`. The consumer's own log agrees —
`fixtures/consumer.log` shows the same exception at attempt 1/3, 2/3, 3/3,
then "message will be dead-lettered", while the health probe stays green
(the service is running fine; it just cannot read the mail).

The last question is what changed. Messages were fine until recently, so
diff the contract: `fixtures/deploy-history.json` shows orders-consumer
2.7.0 deployed 2026-10-28 — one day before the failures — with the note
"reader now requires 'amountCents'; legacy 'amount' no longer accepted". The
producers were not updated in the same window. New consumer, old messages:
every message in flight fails deserialization exactly three times, then
lands in the DLQ.

## Why the red herring misleads

`fixtures/namespace-metrics.csv` is the natural detour: throughput dropped to
zero and messages are dying, which smells like the namespace is throttling —
`ServerBusy` responses and dropped deliveries are a real Service Bus failure
mode. But the CSV shows `throttled_requests` and `server_busy_errors` at
zero for every hour of the incident window, with steady incoming/outgoing
traffic and stable connection counts. Throttling also fails delivery with
*retryable* errors — it would delay messages, not deterministically
dead-letter every single one with the same deserialization exception. The
metrics file is healthy, and that health is itself a clue: nothing at the
namespace layer is wrong, so the fault must live in the message handling.

## Root cause

The orders-consumer 2.7.0 deploy narrowed the message schema: OrderCreated
readers now require `amountCents` and reject the legacy `amount` field
(`fixtures/deploy-history.json`). Every message in flight fails
deserialization on receipt (`fixtures/consumer.log`), the consumer abandons
each delivery, and after the queue's `maxDeliveryCount: 3` attempts Service
Bus moves each message to the dead-letter queue with reason
`MaxDeliveryCountExceeded` (`fixtures/dlq-peek.json`). The main queue drains
into the DLQ — thousands per hour — while nothing reaches the order
database.

## Fix

1. Restore a compatible reader immediately: roll orders-consumer back to
   2.6.3 (which reads both `amount` and `amountCents`) or deploy a forward
   fix that accepts both fields. This stops the dead-lettering the moment
   it rolls out.
2. Resubmit the DLQ backlog back to the orders queue (Service Bus
   Explorer's "transfer DLQ → queue" or a small script using
   `ServiceBusReceivedMessage` relink) so the ~42k dead-lettered orders
   process.
3. Only then re-attempt the schema migration — with producers and consumers
   upgraded in the right order (reader accepts both formats first, writers
   switch second).

## Prevention

- **Tolerant-reader contracts.** Message schema changes must be
  backward-compatible for at least one release: a reader accepts both
  formats before a writer stops emitting the old one. The 2.7.0 note
  ("legacy 'amount' no longer accepted") is exactly the line a contract
  review should have blocked while producers still emitted it.
- **DLQ-depth alerting.** Alert on `deadLetterMessageCount` growth rate per
  queue (and on `MaxDeliveryCountExceeded` share of DLQ reasons). A DLQ
  growing by thousands/hour is an outage; here it was discovered by the
  finance report the next day.
- **Deserialization failures are not retries.** A payload that fails to
  parse will fail again forever — treat deserialization errors as
  non-retryable (dead-letter immediately or route to a repair topic) instead
  of burning three deliveries per message.

## Further reading

- [Service Bus dead-letter queues](https://learn.microsoft.com/azure/service-bus-messaging/service-bus-dead-letter-queues)
- [Service Bus MaxDeliveryCount and poison messages](https://learn.microsoft.com/azure/service-bus-messaging/service-bus-messages-headers-and-properties#maxdeliverycount)
- [Tolerant reader / versioned message contracts](https://learn.microsoft.com/azure/architecture/best-practices/message-encode#versioning)
- [az servicebus queue show reference](https://learn.microsoft.com/cli/azure/servicebus/queue)
