# aks-ops-043 — explainer

## How to investigate

The symptom is a scheduling dead-end: GPU pods are Pending and node
auto-provisioning never reacts. Start with the pod itself —
`fixtures/kubectl-describe-pod.txt` shows `llm-inference-5d8f9b7c4d-k2vwj`
requesting `nvidia.com/gpu: 1`, and, crucially, the events are not the usual
"insufficient quota" flavor: the Karpenter `Unschedulable` events say the
NodePool's requirements *exclude all instance types* that offer GPUs, and name
the offender — `karpenter.azure.com/sku-family In [D]`.

Confirm it from the controller's side: `fixtures/karpenter-controller.log`
shows instance-type resolution evaluating **zero** compatible instance types
for the NodePool, with the same incompatible-requirements error, and
`fixtures/kubectl-get-nodeclaims.txt` confirms the end state — zero
NodeClaims. Nothing was ever created, which is why the node count never
changed.

Then read the configuration: `fixtures/nodepool.yaml` is the only NodePool in
the cluster, and its requirements pin
`karpenter.azure.com/sku-family: In ["D"]` — the general-purpose CPU family.
Karpenter can only pick instance types that satisfy *both* the NodePool
requirements and the pods' requests; a D-series VM has no GPU, so the
intersection is empty by construction.

## Why the red herring misleads

`fixtures/az-vm-quota.json` looks like the classic GPU-blocking problem: when
GPU pods don't schedule, the first instinct is "quota". But read the numbers —
`standardNCSv3FamilyClusterVirtualCPUs` and
`standardNCASv4FamilyClusterVirtualCPUs` both show `currentValue: 0` against
limits of 24 and 48. Not a single GPU vCPU is in use, so nothing is being
throttled; the subscription would happily grant the capacity if the controller
ever asked for it. The failure happens *before* any VM request is made — at
the requirement-matching stage inside Karpenter.

## Root cause

The single Karpenter NodePool `general-purpose`
(`fixtures/nodepool.yaml`) restricts `karpenter.azure.com/sku-family` to
`In ["D"]`, while the Pending inference pods request `nvidia.com/gpu: 1`
(`fixtures/kubectl-describe-pod.txt`). No D-family instance type has GPUs, so
the set of instance types satisfying both constraints is empty —
`fixtures/karpenter-controller.log` records "incompatible requirements" and
zero instance types evaluated — and no NodeClaim is ever created
(`fixtures/kubectl-get-nodeclaims.txt`).

## Fix

Give GPU pods a satisfiable NodePool:

1. **Add a GPU NodePool** (preferred): create a second NodePool, e.g.
   `gpu-inference`, with `karpenter.azure.com/sku-family: In ["N"]`
   (NCSv3/NCASv4) referencing the same or a dedicated `AKSNodeClass`, so pods
   requesting `nvidia.com/gpu` land on N-series VMs. Keep the general-purpose
   pool for CPU workloads.
2. **Or widen the existing pool**: drop or broaden the `sku-family`
   requirement in `fixtures/nodepool.yaml` (e.g. `In ["D", "N"]`) if one pool
   should serve both shapes.
3. Watch the pending pods: within seconds of a satisfiable NodePool existing,
   Karpenter should create NodeClaims for GPU instances and the
   `llm-inference` pods should schedule.

## Prevention

- **Pair NodePools to workload classes**: when enabling node
  auto-provisioning, define a NodePool per workload class (CPU general,
  GPU inference, spot batch) instead of one restrictive pool — requirement
  sets copied from a CPU-only example are the most common Karpenter-on-AKS
  rollout mistake.
- **Alert on unschedulable GPU pods**: alert on
  `kube_pod_status_pending` for pods with GPU resources, and on Karpenter's
  "incompatible requirements" log/error, which is emitted well before anyone
  notices the rollout stalled.
- **Validate requirements at deploy time**: a CI check that cross-references
  NodePool requirements against the resource requests of the workloads they
  are meant to schedule catches empty intersections before rollout.

## Further reading

- [Karpenter: NodePool requirements and instance type selection](https://karpenter.sh/docs/concepts/nodepools/)
- [AKS: Node auto-provisioning (Karpenter)](https://learn.microsoft.com/azure/aks/node-auto-provisioning)
- [AKS: GPU workloads on AKS](https://learn.microsoft.com/azure/aks/gpu-cluster)
