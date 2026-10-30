# aks-ops-020 — explainer

## How to investigate

Start with the autoscaler's own self-report: `fixtures/cluster-autoscaler-status.txt`.
The cluster-wide conditions are `Healthy: True` and `AllNodesRegistered: True`
— the autoscaler is running and sees nothing wrong with itself. But the
`userpool1` node group shows `cloudProviderInfo: {minSize: 3, maxSize: 10}`
with ten nodes listed, all `Ready`. The autoscaler is healthy but already at
its configured ceiling: it cannot scale a pool beyond its maximum, no matter
how many pods are pending.

Cross-check the pool configuration from the Azure side:
`fixtures/az-aks-nodepool-list.json` shows `userpool1` with
`enableAutoScaling: true`, `minCount: 3`, `maxCount: 10`, and — the important
part — `nodeCount: 10`. The Azure config agrees with the autoscaler status:
the pool is running at exactly its `maxCount`.

Confirm the node count independently: `fixtures/kubectl-get-nodes.txt` lists
ten `aks-userpool1-*` nodes, all `Ready` and with normal ages. There is no
`NotReady` node wasting capacity and no missing node — the pool is simply full.

Check what the Pending pods are waiting for: `fixtures/kubectl-get-pods-pending.txt`
shows 15 pods `Pending` for 20–25 minutes across `checkout`, `payments`,
`catalogue`, and `shop-web` — consistent with "no node has capacity for a long
time", not a single bad deployment.

Then rule out the obvious external blocker: `fixtures/az-vm-quota.json` shows
`Standard DADv5 Family vCPUs` at 88 of 640 and `Total Regional vCPUs` at 88 of
625 — quota has enormous headroom. Azure would happily create more VMs; the
autoscaler just is not asking.

## Why the red herring misleads

`fixtures/az-vm-quota.json` is planted to suggest "quota exhausted": a sale
overload plus a scaling failure naturally reads as a cloud capacity problem,
and quota errors are the classic cause of autoscaler silence. But the numbers
point the other way — 88 of 640 family vCPUs and 88 of 625 regional vCPUs is
nowhere near any limit, and no quota error appears anywhere in the evidence.
The real limiter is mundane: `maxCount: 10` in
`fixtures/az-aks-nodepool-list.json` (mirrored as `maxSize: 10` in
`fixtures/cluster-autoscaler-status.txt`) against exactly 10 running nodes
(`fixtures/kubectl-get-nodes.txt`). The autoscaler obeys its configured
maximum perfectly — the configuration is the problem.

## Root cause

The `userpool1` node pool is autoscaling-enabled with `maxCount: 10`
(`fixtures/az-aks-nodepool-list.json`) and is already running 10 nodes
(`fixtures/kubectl-get-nodes.txt`; `maxSize: 10` with 10 Ready nodes in
`fixtures/cluster-autoscaler-status.txt`). The pool has reached its configured
ceiling, so the cluster autoscaler cannot scale out during the Black Friday
load, and 15 pods across checkout, payments, catalogue, and shop-web stay
Pending (`fixtures/kubectl-get-pods-pending.txt`). Azure vCPU quota has ample
headroom (`fixtures/az-vm-quota.json`) and is not the blocker.

## Fix

Raise the autoscaler ceiling for the pool so it can scale out again:

```bash
az aks nodepool update \
  --resource-group rg-prod-01 \
  --cluster-name aks-prod-eus-01 \
  --name userpool1 \
  --max-count 15
```

The subscription's quota already covers 15 × 16 vCPUs
(`fixtures/az-vm-quota.json`), and the change takes effect without restarting
existing nodes — the autoscaler adds nodes on its next unschedulable-pod
evaluation (~10–15 s scale-up evaluation interval). For a same-day fix,
offloading some work to another pool or trimming replica requests also unblocks
pods, but raising `max-count` is the actual correction.

## Prevention

- **Capacity review for peak events:** before a known traffic peak, check
  `az aks nodepool list` for each pool's `maxCount` against expected load and
  pre-raise ceilings (or pre-provision a burst pool). A pool at its ceiling is
  invisible until the first overload.
- **Alert on the autoscaler ceiling:** alert when a node group's node count
  equals its `maxSize` while unschedulable pods exist — the autoscaler status
  configmap and `kube_node_status` / `kube_pod_unschedulable`-style metrics
  both expose the ingredients.
- **Right-size quotas too:** keep vCPU quota comfortably above the sum of all
  pool `maxCount`s so the two limiters never trade places.

## Further reading

- [AKS: Cluster autoscaler](https://learn.microsoft.com/azure/aks/cluster-autoscaler)
- [Kubernetes: Cluster Autoscaler FAQ — node group limits](https://github.com/kubernetes/autoscaler/blob/master/cluster-autoscaler/FAQ.md)
- [Azure: Check vCPU quota](https://learn.microsoft.com/azure/quotas/quotas)
