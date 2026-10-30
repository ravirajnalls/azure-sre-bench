# aks-ops-046 — explainer

## How to investigate

A weekly, clock-regular eviction wave that recovers on its own is a schedule,
not an outage. Anchor on the timing first: every Tuesday around 10:00 local.
Then read the node events — `fixtures/kubectl-get-events.txt` shows nodes
being **drained for "automatic node OS image upgrade"**, going `NodeNotReady`,
and pods like `storefront-web` and `catalog-api` getting `Evicted`, with
matches 3 and 10 days back — the same weekday pattern the symptom describes.

Now find who started it: `fixtures/aks-activity-log.json` shows
`Microsoft.ContainerService/managedClusters/upgrade/action` operations,
caller "Microsoft Container Service (auto-upgrade)", property
`channel: NodeImage`, at 08:55Z (09:55 local) on two consecutive Tuesdays —
exactly the eviction window, on exactly the weekly cadence.

Finally, connect the two halves of the cause in the cluster configuration:
`fixtures/az-aks-show.json` shows `autoUpgradeProfile.nodeOSUpgradeChannel:
NodeImage` — automatic node OS image upgrades are on — while
`fixtures/az-aks-maintenanceconfiguration-list.json` shows the **only**
maintenance configuration is `default` (Sunday 04:00–08:00), which governs
*cluster* upgrades. No `aksManagedNodeOSUpgradeSchedule` exists, so node OS
image upgrades are not confined to any window and run when AKS chooses —
Tuesday mornings, in practice.

## Why the red herring misleads

`fixtures/node-memory-kql.csv` shows node memory around 57–61% across the
exact Tuesday windows — someone glancing at it alongside the reboots may
theorize memory pressure, OOM, or a leak crashing the kubelet. But the numbers
are flat and unremarkable: nothing approaches eviction thresholds, and the
events never mention memory conditions — they say *drained for automatic node
OS image upgrade*, in so many words. The CSV exists to be ruled out; the
drain/evict events name the actual mechanism.

## Root cause

The `NodeImage` node OS upgrade channel (`fixtures/az-aks-show.json`,
`autoUpgradeProfile.nodeOSUpgradeChannel`) upgrades node OS images
automatically, and with no `aksManagedNodeOSUpgradeSchedule` maintenance
configuration (`fixtures/az-aks-maintenanceconfiguration-list.json` lists only
the cluster-upgrade `default` window), AKS runs those upgrades in its default
window — Tuesdays around 10:00 local for this cluster
(`fixtures/aks-activity-log.json` confirms the Tuesday 08:55Z auto-upgrade
starts on channel `NodeImage`). Each upgrade drains nodes
(`fixtures/kubectl-get-events.txt`), evicting pods and firing the weekly
alerts.

## Fix

Create a maintenance configuration for the node OS upgrade channel with an
off-hours window:

```
az aks maintenanceconfiguration add \
  --resource-group rg-prod-01 --cluster-name aks-prod-eu \
  --name aksManagedNodeOSUpgradeSchedule \
  --schedule-type Weekly --day-of-week Sunday \
  --hours 1 --duration 4 --utc-offset +00:00
```

(any nightly/weekly window that avoids business hours). Do **not** rely on the
existing `default` configuration — it applies to cluster auto-upgrades, not to
`NodeImage` node OS upgrades; the node-OS schedule must exist by its own name.
Until the window is in place, every new node image will keep draining nodes
mid-morning on Tuesdays. Optionally, pair the schedule with
PodDisruptionBudgets so drains respect availability during any unavoidable
window.

## Prevention

- **Set maintenance windows when enabling auto-upgrade**: an upgrade channel
  without a matching `aksManagedNodeOSUpgradeSchedule` / `aksManagedBackup...`
  style schedule means AKS picks the time — for production, always pair the
  channel with an explicit window at rollout.
- **Alert on drain events, not only pod evictions**: a
  `kubectl get events`-based alert on "drained: automatic node OS image
  upgrade" during business hours identifies the pattern within one week
  instead of three.
- **Budget for drains**: PodDisruptionBudgets and multiple replicas on the
  user pools make any node OS upgrade transparent regardless of when it runs.

## Further reading

- [AKS: Automatic node OS image upgrades](https://learn.microsoft.com/azure/aks/auto-upgrade-node-os-image)
- [AKS: Maintenance configuration windows](https://learn.microsoft.com/azure/aks/planned-maintenance)
- [Kubernetes: PodDisruptionBudgets and voluntary disruptions](https://kubernetes.io/docs/concepts/workloads/pods/disruptions/)
