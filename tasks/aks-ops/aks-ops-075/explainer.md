# aks-ops-075 — explainer

> **Spoiler for humans** — this file is the Learn-page write-up. Agents never see it: the runner keeps it out of the workspace like `expected.json`.

## How to investigate

1. `fixtures/sources.md` — four sources merge into `events.json`: the
   upgrade pipeline (`pl-*`, second precision), kubectl events (`kc-*`),
   the Activity Log export (`al-*`), and the on-call chat (`chat-*`).
   The Activity Log export's timestamps run **7 minutes ahead** — the
   workbook-documented skew. Every `al-*` timestamp must be shifted back
   before comparing with the other sources.
2. `fixtures/events.json` — the export is shuffled. Sort by corrected time,
   then sanity-check with content: the chat confirms a stuck drain before
   the pipeline's timeout, and the second eviction block (`kc-003`) lands
   between the retry acceptance and the PDB edit.
3. The corrected sequence: pipeline starts (05:07:12) → upgrade accepted
   (`al-118` shown 05:14:22 = 05:07:22 true) → surge node joins (05:08:05)
   → drain starts (05:09:47) → eviction blocked by the PDB (05:10:02) →
   on-call notices (05:11:03) → drain times out after 20m (05:29:59) →
   retry accepted (`al-119` shown 05:41:44 = 05:34:44 true) → blocked again
   (05:34:58) → platform team paged (05:36:40) → PDB relaxed to
   maxUnavailable=1 (05:39:12) → drain succeeds and the upgrade completes
   (05:52:40).

## Why the red herring misleads

Sorting `events.json` by raw timestamp puts `al-118` in the middle of the
story (its skewed 05:14:22 lands between the chat and the timeout) and
`al-119` at the very end (05:41:44, after the "completed" line). Both
Activity Log entries actually belong early in their sequence — the skew,
not the content, moves them. Any order built on uncorrected clocks fails.

## Root cause

The upgrade itself was healthy; the PDB was not: `payments-api-pdb`
declared `maxUnavailable: 0`, so no drain could evict the payments-api
pod. The pipeline retried for 20 minutes, timed out, retried again, and
the on-call relaxed the PDB to `maxUnavailable: 1` for the window, after
which the drain and the upgrade completed.

## Fix

- Before any planned upgrade, check `kubectl get pdb -A` for budgets with
  zero allowed disruptions and agree a temporary `maxUnavailable` (or a
  runbook override) for the window.
- Correct clock skew at the export boundary (or in the workbook), so merged
  incident timelines from `al-*` sources sort correctly without manual
  adjustment.

## Prevention

- Alert on `PodDisruptionBudget` budgets with `ALLOWED_DISRUPTIONS 0` that
  block more than X minutes during planned maintenance.
- Export jobs should stamp their clock offset in the export header; the
  incident-review tooling should normalize timestamps before merge.

## Further reading

- `kubectl drain` and PodDisruptionBudgets:
  https://kubernetes.io/docs/concepts/scheduling-eviction/api-eviction/
- AKS upgrade considerations (surge, drains):
  https://learn.microsoft.com/azure/aks/upgrade-cluster
- Azure Activity Log: https://learn.microsoft.com/azure/azure-monitor/essentials/platform-logs-overview
