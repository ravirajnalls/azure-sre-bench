# Record sources

The export in `events.json` merges six record sources. None of them is
skewed in this incident — every timestamp is directly comparable — which
is exactly why the shared-timestamp pair matters: with honest clocks the
only way to order those two events is causality.

| Source | Prefix | What it records |
|---|---|---|
| Orders consumer deploy log | `cd-*` | CI deploy events for the orders consumer (version bumps) |
| Service Bus metrics | `sb-*` | Queue and DLQ depth metric samples |
| PagerDuty | `pd-*` | Alert fired / resolved transitions |
| GitHub | `gh-*` | PR merge events |
| CRM tickets | `crm-*` | Customer-reported symptoms |
| Batch job log | `job-*` | DLQ resubmission batch runs |
