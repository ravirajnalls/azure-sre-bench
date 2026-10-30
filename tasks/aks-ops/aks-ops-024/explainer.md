# aks-ops-024 — explainer

## How to investigate

Zero rows in one table, from one cluster, after one known event (the rebuild)
— structure the investigation as: is the data arriving at all, and if not,
which link in the chain is broken?

**Pin the timeline.** `fixtures/kql-containerlogv2-count.csv` shows
ContainerLogV2 at ~50k rows/day through 2026-10-22, a partial day (12,093) on
10-23, and exactly 0 from 10-24 onward — a hard cutoff, not a decay. A hard
zero means the pipeline is severed at some discrete point, not that agents are
struggling or the workspace is throttling.

**Rule out the endpoints.** Ingestion destination: `fixtures/az-aks-show.json`
shows the `omsagent` addon enabled and pointing at `law-prod-01` — the same
workspace other clusters still write to, and `fixtures/az-monitor-dcr-show.json`
shows `dcr-container-insights` healthy (`provisioningState: Succeeded`) with a
data flow for `Microsoft-ContainerLogV2` to that same `law-prod-01` workspace.
Collection source: `fixtures/kubectl-get-pods-ama-logs.txt` shows every
`ama-logs` pod 3/3 Running with zero restarts for 6 days. Both ends of the
pipeline are green while zero rows flow — so the break is between them.

**Check the join.** The link between a cluster and a data collection rule is
the data collection rule association (DCRA).
`fixtures/az-monitor-dcr-association-list.json` — the association list scoped
to the aks-prod-eus-02 cluster resource — is `[]`. Zero associations. The rule
exists, the workspace exists, the agents run, but nothing tells the rule to
apply to this cluster. And the timeline explains when it broke: a DCRA
references the cluster's immutable resource ID, and the rebuild on 2026-10-23
created a *new* resource — the old association died with the old ID and was
never re-created.

## Why the red herring misleads

Two traps, one per endpoint you might blame too early:

- **"The agent is broken" (`ama-logs`, CrashLoopBackOff).** The symptom
  volunteers that agent pods report Running, but the pull to check agents
  first is strong. `fixtures/kubectl-get-pods-ama-logs.txt` confirms it: all
  `ama-logs` pods are 3/3 Running, 0 restarts, 6 days old. An agent-side
  failure would show restarts or not-ready containers, and would typically
  produce gaps or errors — not a clean zero beginning the day after a rebuild.
- **"Wrong workspace" (`law-prod-01`).** The workspace name appears in two
  fixtures, inviting a "the cluster is pointing at the wrong LAW" theory.
  It fails on cross-checking: the addon config in
  `fixtures/az-aks-show.json` and the DCR destination in
  `fixtures/az-monitor-dcr-show.json` both name the same `law-prod-01`, and
  `fixtures/kql-containerlogv2-count.csv` shows this cluster wrote ~50k rows/day
  to the pipeline until the rebuild. The workspace was never wrong; the link to
  the cluster was lost.

## Root cause

The container-logs data collection rule exists and is configured correctly,
but it has no data collection rule association with the new cluster resource.
The association was lost when aks-prod-eus-02 was rebuilt on 2026-10-23: a
DCRA references the immutable cluster resource ID, and the rebuilt resource is
a new one (`fixtures/az-aks-show.json`), so no rule links the cluster to the
workspace (`fixtures/az-monitor-dcr-association-list.json` is empty) and
ContainerLogV2 stays empty (`fixtures/kql-containerlogv2-count.csv`) even
though the rule (`fixtures/az-monitor-dcr-show.json`), the workspace, and the
agents (`fixtures/kubectl-get-pods-ama-logs.txt`) are all healthy.

## Fix

Re-create the missing association that links `dcr-container-insights` to the
rebuilt cluster resource:

```bash
az monitor data-collection rule association create \
  --dcr-name dcr-container-insights \
  --resource /subscriptions/00000000-0000-0000-0000-000000000001/resourcegroups/rg-prod-01/providers/Microsoft.ContainerService/managedClusters/aks-prod-eus-02
```

Then verify flow resumes: new ContainerLogV2 rows appear within minutes of the
association landing (the DCR's log collection interval is 1m). Note the
association is idempotent-safe to re-run and needs no agent or workspace
change — the rule and addon were never wrong.

## Prevention

- **Rebuild runbooks must include DCRA re-creation:** any AKS rebuild/infra
  pipeline that recreates the cluster resource must re-run
  `az monitor data-collection rule association create` (or re-enable the
  Container Insights addon config) as a post step — the association is
  resource-ID-bound and does not survive a rebuild.
- **Alert on ingest silence per cluster:** alert when a cluster's
  ContainerLogV2/heartbeat rows drop to zero for >1h, rather than relying on
  dashboard absence — six days of zero logs went unnoticed here.
- **Track associations as config:** export DCR associations into the same
  IaC/policy-as-code as the rules themselves, so a rule without an association
  (or an orphaned association) is flagged in review.

## Further reading

- [Azure Monitor: Data collection rule associations](https://learn.microsoft.com/azure/azure-monitor/data-collection-rule-associations)
- [Container Insights: DCR-based log collection](https://learn.microsoft.com/azure/azure-monitor/containers/container-insights-dcr)
- [AKS: Rebuilding clusters and resource IDs](https://learn.microsoft.com/azure/aks/)
