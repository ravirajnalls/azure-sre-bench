# aks-ops-025 — explainer

## How to investigate

A doubled bill with flat traffic is a "what changed" question, and cost changes
come with meters. Start where the money is:

**Split the bill by meter.** `fixtures/cost-by-meter.csv` is the decisive
artifact: in September the systempool VMSS billed as `Standard_D4s_v5`
(414.72 USD); in October the same VMSS bills as `Standard_D16s_v5` at
4,571.14 USD — an 11× jump on that meter. Meanwhile `npuser`'s `Standard_D8s_v5`
meter was essentially flat (3,317.76 → 3,429.89 USD), and Storage (Managed
Disks ~38→39 USD) and Bandwidth are rounding errors. The doubling is the
system pool's VM line.

**Find who changed it.** `fixtures/aks-activity-log.json` records an
`agentpools/write` on `systempool` at 2025-10-01 09:14 UTC by
`priya.raman@contoso.com` with requestbody `vmSize: Standard_D16s_v5,
minCount: 8, maxCount: 12` — and the previous write, 2025-06-12, shows the old
shape (`Standard_D4s_v5`, `minCount: 3`). So on October 1 the system pool's
autoscaler floor went from 3 small nodes to 8 large ones.

**Confirm the waste.** `fixtures/az-aks-nodepool-list.json` shows systempool
live: autoscaling enabled, minCount 8 / maxCount 12, 8 nodes of
`Standard_D16s_v5` running. `fixtures/kubectl-top-nodes.txt` shows all eight
sitting at 7–9% CPU and 6% memory — always-on 16-vCPU nodes the autoscaler can
never scale down because its own floor is 8.

**Kill the alternatives.** `fixtures/log-analytics-ingestion.csv`: ingestion
grew only ~6% month over month (September weeks ~27.5 GB vs October weeks
~28.5–30.2 GB) — real, but nowhere near a doubling of the cluster bill.

## Why the red herring misleads

Two decoys, both plausible at a glance and both priced by the fixtures:

- **"Log Analytics ingestion spike" (ContainerLog).** The Monitor and Log
  Analytics meter is the only other visibly growing line, and "logs cost more"
  is a familiar story. `fixtures/log-analytics-ingestion.csv` prices it out:
  ContainerLog moved 18.40 → 20.30 GB/week (~10%), total ingestion ~6% — tens
  of dollars on a 62 USD baseline, against a 4,156 USD VM-meter increase in
  `fixtures/cost-by-meter.csv`. An order of magnitude too small.
- **"Managed disks / storage cost / the npuser pool."** Storage Managed Disks
  held at ~38–39 USD per pool, and npuser's VM cost was flat
  (`fixtures/cost-by-meter.csv`), with npuser nodes running a steady 11–15%
  CPU in `fixtures/kubectl-top-nodes.txt`. The cost moved exactly once — on
  the systempool VM meter — and the resource IDs in the CSV put that meter on
  `aks-systempool-…-vmss`, not npuser.

## Root cause

The system node pool (`systempool`) autoscaler minimum was raised on
2025-10-01 from 3 nodes of Standard_D4s_v5 to a floor of 8 nodes of
Standard_D16s_v5 (`fixtures/aks-activity-log.json`, agentpools/write by
priya.raman@contoso.com). The pool now runs 16-vCPU VMs with minCount 8
(`fixtures/az-aks-nodepool-list.json`) while utilization sits near 8% CPU
(`fixtures/kubectl-top-nodes.txt`), so the autoscaler never scales down and
eight always-on large nodes doubled the cluster's VM spend
(`fixtures/cost-by-meter.csv`: 414.72 → 4,571.14 USD on the systempool meter).
The small rise in Log Analytics ingestion
(`fixtures/log-analytics-ingestion.csv`) is far too small to explain the jump.

## Fix

Give the autoscaler its floor back, and right-size the floor's VM:

1. **Lower the systempool autoscaler `minCount`** back to the original floor
   of 3 (`az aks nodepool update --cluster-name aks-contoso-dev
   --resource-group rg-contoso-dev --name systempool --min-count 3`), so the
   autoscaler can scale the pool down outside peak hours — the eight
   underutilized Standard_D16s_v5 nodes are what doubled the bill.
2. **Optionally resize the system pool to a smaller VM** such as
   `Standard_D4s_v5`, matching the pre-change shape, if the June configuration
   was already sufficient for system workloads.
3. **Confirm npuser absorbs any overflow:** npuser has minCount 3 / maxCount 15
   with headroom (`fixtures/az-aks-nodepool-list.json`), so system pods evicted
   from a shrinking systempool can reschedule there.

## Prevention

- **Review autoscaler floors as cost decisions:** a `minCount` bump from 3 to 8
  plus a 4× VM-size increase is an 11× committed-spend change that should need
  a cost estimate in the change request, not a silent `agentpools/write`.
- **Alert on cost-by-meter deltas:** a weekly anomaly check on the top VM
  meters (here: +4,156 USD month over month) flags the change within days
  instead of at the next Finance review.
- **Right-size alerts on idle pools:** alert when a pool runs below ~20% CPU
  at its autoscaler floor for sustained periods — always-on capacity the
  autoscaler cannot release is exactly the condition minCount creates.

## Further reading

- [AKS: Cluster autoscaler profiles and node pool scaling](https://learn.microsoft.com/azure/aks/cluster-autoscaler)
- [AKS: Cost fundamentals and best practices](https://learn.microsoft.com/azure/aks/best-practices-cost)
- [Azure: Monitor cost analysis by meter](https://learn.microsoft.com/azure/cost-management-billing/costs/cost-analysis-common-uses)
