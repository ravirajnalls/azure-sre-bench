# aks-ops-023 — explainer

## How to investigate

An outage window with a known external trigger — "Azure Advisor shows spot
eviction events at that time" — makes the timeline the first artifact to pin
down. `fixtures/aks-activity-log.json` does that: four
`virtualMachineScaleSets/virtualMachines/delete` operations on
`aks-npspot-00000001` between 02:07:41 and 02:08:03 UTC, caller
`Azure Platform Infrastructure`, subStatus `Evicted`, message "capacity was
needed elsewhere." All four spot VMs died within 22 seconds.

Then ask the outage-defining question: why did losing the spot pool take the
*whole service* down? `fixtures/kubectl-get-pods-wide-history.txt` answers
it — in the pre-incident snapshot all four payments-api replicas sit on
`aks-npspot-…-vmss000000` through `000003`, one per spot node. Mid-outage they
are all `Evicted` and their replacements go `Pending`; in the recovery snapshot
they return — to npspot nodes again.

Explain why the scheduler packed them there: `fixtures/deployment.yaml`
tolerates `kubernetes.azure.com/scaleSetPriority=Spot:NoSchedule` and — the key
line, visible in the change-cause annotation — release 2026.10.28 "drop[ped]
node affinity to npuser so the scheduler can bin-pack freely." The spec now has
no `nodeAffinity` and no `topologySpreadConstraints`. `fixtures/az-aks-nodepool-list.json`
confirms the pool layout: `npspot` is `scaleSetPriority: Spot` with
eviction policy `Delete` and the matching taint; `npuser` and `systempool` are
regular.

Finally, close the loop on the protection that should have limited the blast
radius: `fixtures/kubectl-get-pdb.txt` shows the payments-api PDB at
`Allowed disruptions: 0` with 4 healthy pods. Yet all four were evicted. That
is not a contradiction — it is the lesson: a PDB constrains *voluntary*
disruptions, and platform-initiated spot evictions are not voluntary.

## Why the red herring misleads

**The PDB.** `fixtures/kubectl-get-pdb.txt` was captured during the review
precisely because a dashboard flagged evictions next to a budget showing
`Allowed disruptions: 0`. The natural misread is "the PDB blocked rescheduling"
or "the PDB prevented the eviction from being handled" — a zero-allowed-
disruptions budget looks like a smoking gun. It fails two ways: the pods *were*
all evicted (a PDB cannot block what does not consult it), and the replacements
went Pending because their nodes were deleted, not because a budget held them
back. Spot eviction bypasses the eviction API entirely — no PDB, however
configured, applies.

## Root cause

All payments-api replicas ran on the spot node pool, and nothing held any
replica back. The deployment tolerates the spot taint
(`kubernetes.azure.com/scaleSetPriority=Spot:NoSchedule`) and, since release
2026.10.28 removed its node affinity to `npuser`
(`fixtures/deployment.yaml`), has no node affinity or topology spread
constraint keeping any replica on the regular pool — so the scheduler packed
every replica onto `npspot` nodes (`fixtures/kubectl-get-pods-wide-history.txt`;
`fixtures/az-aks-nodepool-list.json`). When Azure evicted all four spot VMs
around 02:07–02:08 UTC (`fixtures/aks-activity-log.json`), the entire service
went down at once; the PDB could not help because spot evictions are not
voluntary disruptions and ignore disruption budgets
(`fixtures/kubectl-get-pdb.txt`).

## Fix

Keep the workload off spot — or at least keep it from ever being fully on spot:

1. **Remove the spot toleration** from `fixtures/deployment.yaml`, or add
   `nodeAffinity`/`nodeSelector` requiring the regular pool, so payments-api
   only lands on `npuser`.
2. **Add `topologySpreadConstraints`** (on `kubernetes.io/hostname` and zone)
   so replicas never concentrate in a single failure domain — the pre-incident
   snapshot shows one replica per node but zero spread enforcement; nothing
   would have stopped two replicas on one node.
3. **If spot is desired for cost,** run a mixed strategy: pin a minimum number
   of replicas to the regular pool (separate deployment or pool-pinning
   affinity) and let only the surplus burst onto spot. Note the PDB cannot
   prevent spot evictions either way.

## Prevention

- **Gate spot tolerations by workload tier:** a policy/CI check that
  user-facing, latency-sensitive services do not tolerate
  `kubernetes.azure.com/scaleSetPriority=Spot` — or that they carry a
  regular-pool pin when they do.
- **Alert on placement concentration:** alert when all replicas of a
  multi-replica service share a node pool, spot or otherwise; the
  bin-packing release changed placement with no placement test.
- **Review "scheduler freedom" changes:** release 2026.10.28 dropped an
  affinity to let the scheduler bin-pack — a cost optimization with an
  availability tail risk. Require that placement-affecting manifest changes
  list their failure-domain impact in the change cause and pass a
  topology-spread check.

## Further reading

- [AKS: Spot node pools](https://learn.microsoft.com/azure/aks/spot-node-pool)
- [Kubernetes: Taints and tolerations](https://kubernetes.io/docs/concepts/scheduling-eviction/taint-and-toleration/)
- [Kubernetes: Pod Disruption Budgets and voluntary disruptions](https://kubernetes.io/docs/concepts/workloads/pods/disruptions/)
- [Kubernetes: Topology Spread Constraints](https://kubernetes.io/docs/concepts/scheduling-eviction/topology-spread-constraints/)
