# monitoring-040 — explainer

## How to investigate

The symptom already narrows the shape: ingestion stops mid-afternoon and
resumes right at midnight UTC. A daily rhythm synchronized to UTC midnight is
a workspace quota clock, not an agent or network fault — midnight is when Log
Analytics resets its daily cap. But confirm the cap hypothesis with data
before trusting the pattern.

Start with the workspace configuration: `fixtures/az-monitor-log-analytics-workspace-show.json`
shows `workspaceCapping.dailyQuotaGb = 5` on `law-prod-eus-0001` — a 5 GB
daily cap exists, so it *can* be hit.

Then verify it *is* being hit, at the right hour: `fixtures/kql-usage-by-hour.csv`
shows Oct 29's hourly ingestion bins accumulating to exactly 5.00 GB at the
14:00 UTC bin, with every later hour at 0.00 GB — and a note marking the daily
quota reached at 14:00 UTC. The 00:00 UTC rows resume, matching the morning
recovery in the symptom.

Cap hit ≠ cap deserved, so find what changed: `fixtures/kql-ingestion-by-container.csv`
shows `payments-api` at 3.15 GB for Oct 29 against a 0.35 GB Oct 20–26 median —
roughly 9×, and 63% of the day's total ingestion — while every other container
is flat versus its median. Something on payments-api is emitting far more than
before.

The culprit is in the manifest: `fixtures/kubectl-get-deploy-payments-api.yaml`
shows the payments-api deployment running with `LOG_LEVEL=debug` — the debug
firehose behind the ~2.8 GB/day jump.

Finally confirm the stops, not just the volumes: `fixtures/kql-operation-table.csv`
records "daily cap reached" administrative events at Oct 27 14:41, Oct 28
14:07, and Oct 29 14:00:33 UTC — three days, drifting slightly earlier each
day as debug volume grows — each followed by ingestion resuming at the 00:00
UTC reset.

## Why the red herring misleads

The symptom opens with "the Azure Monitor Agent pods were checked first and are
all Running with zero restarts" — which reads like reassurance, but the
expected.json distractor group is the agent: "ama-logs crashed / stopped
shipping." It is worth stating why the agent is genuinely ruled out, not just
unplausible: `fixtures/ama-logs-pods.txt` shows the `ama-logs` DaemonSet pods
Running with zero restarts on every node, and the ingestion that exists each
morning proves the shipping pipeline works end-to-end — data stops *at the
cap boundary*, not when agents restart. An agent fault would also produce
gaps at arbitrary times, not a clean 14:00-to-midnight UTC window that recurs
daily. No ingestion-latency alerts firing fits too: the workspace is not
slow, it is refusing rows by policy.

## Root cause

The workspace `law-prod-eus-0001` hits its 5 GB daily cap
(`workspaceCapping.dailyQuotaGb = 5`, `fixtures/az-monitor-log-analytics-workspace-show.json`)
each early afternoon: Oct 29's hourly ingestion accumulates to exactly 5.00 GB
at 14:00 UTC (`fixtures/kql-usage-by-hour.csv`), and the Operation log records
a "daily cap reached" event on Oct 27, 28, and 29 (`fixtures/kql-operation-table.csv`)
before ingestion is refused until the 00:00 UTC reset. The cap started being
hit because debug logging on payments-api floods the workspace —
`LOG_LEVEL=debug` (`fixtures/kubectl-get-deploy-payments-api.yaml`) drove
3.15 GB on Oct 29 versus a 0.35 GB median (`fixtures/kql-ingestion-by-container.csv`),
about 9× and 63% of all ingestion, exhausting the quota by early afternoon.

## Fix

Turn off the debug firehose and give the workspace headroom. Set the
payments-api log level back to `info`:

```bash
kubectl -n shop set env deployment/payments-api LOG_LEVEL=info
```

Removing ~2.8 GB/day of debug noise drops daily volume to roughly 2.2 GB,
comfortably under the 5 GB cap. If debug output must be kept, the alternatives
are a data collection rule transformation that drops debug-level rows from
ContainerLogV2 at ingestion, or raising the workspace daily quota (for example
`dailyQuotaGb` to 20) — accepting the added ingestion cost. The change is safe
and reversible: the log level is one env var, the cap setting is one
`az monitor log-analytics workspace update --daily-quota-gb` call, and neither
touches data already ingested.

## Prevention

Budget ingestion like the capacity it is: alert on daily volume trending
toward the cap (a query over the `Usage` table projected against
`dailyQuotaGb` would have fired on Oct 27, two days before the dashboards
went fully blank), and treat any single container exceeding a few multiples
of its own 7-day median as an anomaly worth paging on. Gate log-level changes
like config changes — a `LOG_LEVEL=debug` that reaches prod unreviewed caused
this. And when a workspace is genuinely near its ceiling, prefer ingestion-time
transformations that drop low-value verbosity over quota increases, so the cap
keeps protecting cost instead of hiding noise behind a higher number.

## Further reading

- [Set the daily cap on a Log Analytics workspace](https://learn.microsoft.com/azure/azure-monitor/logs/daily-cap)
- [Monitor Log Analytics workspace ingestion volume — Usage table](https://learn.microsoft.com/azure/azure-monitor/logs/analyze-usage)
- [Azure Monitor Agent overview and health](https://learn.microsoft.com/azure/azure-monitor/agents/agents-overview)
- [Data collection rule transformations in Azure Monitor](https://learn.microsoft.com/azure/azure-monitor/essentials/data-collection-transformations)
