# messaging-076 — explainer

> **Spoiler for humans** — this file is the Learn-page write-up. Agents never see it: the runner keeps it out of the workspace like `expected.json`.

## How to investigate

1. `fixtures/events.json` — ten events from seven sources (ci-cd, app-logs,
   service-bus, pagerduty, batch-job, support, github), shuffled. Eight of
   the ten sort cleanly by timestamp.
2. The trap: `cd-233` (rollback complete) and `job-88` (DLQ resubmission
   started) share `14:19:02`. Read the summaries for causality: the
   resubmission job "started automatically **on deploy completion**" — the
   rollback completing is the cause, the replay job the effect. A stable
   sort that leaves them in file order has a coin-flip chance; the correct
   order is `cd-233` before `job-88`.
3. The full story: the v2.4.0 deserializer deploy (13:58:12) fails to parse
   events missing `shipmentId` (14:01:33); Service Bus dead-letters them and
   the DLQ crosses 5,000 (14:06:00), tripping the P2 alert a second later
   (14:06:01); the rollback is initiated (14:12:45) and completes
   (14:19:02); the auto-triggered resubmission replays 5,813 messages
   (14:19:02); customers receive duplicate confirmations (14:47:20) because
   the replay plus in-flight redeliveries broke at-least-once assumptions;
   the idempotency fix merges (15:30:11) and ships (16:02:37).

## Why the red herring misleads

Shared-timestamp events make timestamp sort ambiguous, and the file order
in the export is a plausible-looking decoy (`cd-233` appears after
`job-88`). Only the summaries' causal language resolves the tie — which is
the point: incident timelines are reconstructed from causality, not clock
ties.

## Root cause

A contract-changing deploy without a schema-gate dead-lettered thousands of
order events; the rollback restored consumption, and the automatic DLQ
replay — combined with normal redeliveries — produced duplicate side
effects because the consumer had no idempotency guard. The durable fix was
the idempotent consumer (v2.5.0).

## Fix

- Gate deserializer/contract changes behind a schema-compatibility check in
  CI (old consumers must parse new payloads, or fail the deploy).
- Make consumers idempotent before any replay: dedupe on message-id (or a
  business key) before side effects.

## Prevention

- Alert on DLQ *rate of growth*, not just absolute depth, and page before
  thousands accumulate.
- Run DLQ replays in a capped, monitored mode (batch size, poison-pill
  cutoff) with an idempotency preflight check on the consumer.

## Further reading

- Service Bus dead-letter queues:
  https://learn.microsoft.com/azure/service-bus-messaging/service-bus-dead-letter-queues
- Idempotent message processing:
  https://learn.microsoft.com/azure/architecture/best-practices/idempotency
- Kendall tau rank correlation (grading shape):
  https://en.wikipedia.org/wiki/Kendall_rank_correlation_coefficient
