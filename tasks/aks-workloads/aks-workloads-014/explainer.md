# aks-workloads-014 — explainer

## How to investigate

Start at the scheduler's own verdict: `fixtures/kubectl-describe-pod.txt`
shows `FailedScheduling — 0/4 nodes are available: 4 Insufficient memory`,
repeating for over an hour. That message is precise: the scheduler evaluated
every node in the cluster and every node lacked enough memory for this pod.

Two numbers make the failure permanent. `fixtures/deployment.yaml` requests
`memory: 30Gi` (and `cpu: "4"`) per pod.
`fixtures/kubectl-describe-node-allocatable.txt` shows the pool runs
Standard_D8s_v5 nodes whose Allocatable memory is `28278784Ki` — about
27.6Gi — already below the pod's request before anything else on the node is
counted. No node that can exist in this pool can ever fit this pod.

Then read the autoscaler's side of it: `fixtures/cluster-autoscaler-status.txt`
reports `ScaleUp: NoActivity` with the reason `pod didn't trigger scale-up: 1
insufficient memory`. The autoscaler did evaluate the pending pod and concluded
that a scale-up could not help — another D8s_v5 has the same ~27.6Gi
allocatable — so staying idle is the correct behavior, not a malfunction.

## Why the red herring misleads

The symptom's "cluster autoscaler is enabled and reports healthy, yet no
scale-up has happened" frames this as an autoscaler bug, and
`fixtures/kubectl-top-nodes.txt` (nodes at 5–7% memory used) makes "there is
plenty of free memory" feel like proof. But free memory on existing nodes
cannot help a pod that no node in the pool can fit — utilization being low is
a consequence of the impossible request, not evidence against it. Likewise,
"Azure quota was not changed recently" points at a quota gate that was never
the binding constraint: the autoscaler refuses to scale because the pod could
not be scheduled even with more identical nodes.

## Root cause

The ml-scorer memory request of 30Gi (`fixtures/deployment.yaml`) exceeds the
~27.6Gi allocatable on every Standard_D8s_v5 node
(`fixtures/kubectl-describe-node-allocatable.txt`), so all four nodes are
rejected with `Insufficient memory` (`fixtures/kubectl-describe-pod.txt`) and
the cluster autoscaler reports `NoActivity` because adding more nodes of the
same size cannot ever fit the pod (`fixtures/cluster-autoscaler-status.txt`).

## Fix

Either shrink the pod or grow the node — one of the two must change:

- Reduce the memory request below node allocatable, e.g. by splitting the
  model across more, smaller pods, or
- Move the pool (or add a new pool) to a larger VM size whose allocatable
  memory clears 30Gi, e.g. Standard_D16s_v5, and repoint the workload at it.

After the change, confirm the pods schedule: `kubectl get pods -n ml` shows
Running, and `fixtures/cluster-autoscaler-status.txt` stays `NoActivity` only
because no scale-up is needed anymore.

## Prevention

Gate manifests on schedulability: in CI, compare every container's resource
requests against the allocatable capacity of the node pools the workload
targets (a server-side dry-run against a cluster with matching node sizes, or
a policy check, catches an un-schedulable request before rollout). When
introducing a workload with an unusually large per-pod footprint, size a
dedicated node pool in the same change rather than assuming the autoscaler
will make room.

## Further reading

- [Resource management for pods and containers](https://kubernetes.io/docs/concepts/configuration/manage-resources-containers/)
- [Cluster autoscaler on Azure Kubernetes Service](https://learn.microsoft.com/azure/aks/use-cluster-autoscaler)
