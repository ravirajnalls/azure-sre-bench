# aks-ops-055 — explainer

## How to investigate

Let the timing speak first. Recurring 02:00–04:00 incidents that self-heal in
~40 minutes are not storage decay and not deploys — they are something
*scheduled*. `fixtures/zookeeper.log:2-5` shows the ensemble falling apart
from the inside at 02:20: replica `zookeeper-0` cannot reach the other two
members' election ports, logs `only 1 of 3 ensemble members reachable`, and
exits on quorum loss. Two replicas vanished at the same moment; find where
they went.

The autoscaler log answers it: `fixtures/cluster-autoscaler.log:2-6` shows
the nightly scale-down choosing node `vmss00000c` at 02:20:05 and evicting
`streaming/zookeeper-1` **and** `streaming/zookeeper-2` in the same second —
both replicas were co-located on the one node picked for removal. Nothing
stopped that double eviction, and the reason is visible in two places:
`fixtures/kubectl-get-pdb.txt:2-4` shows no PodDisruptionBudget anywhere in
the streaming namespace, and `fixtures/statefulset.yaml:35-36` notes the pod
template sets no `cluster-autoscaler.kubernetes.io/safe-to-evict` annotation
and no anti-affinity to keep replicas apart. The autoscaler is doing exactly
what it was told: consolidate, then delete the emptiest node, evicting
everything on it. Quorum of 3 needs 2; evict 2 at once and the ensemble is
down until the evicted pods reschedule and rejoin — the ~40 minute gap in
the zookeeper log.

## Why the red herring misleads

The disk theory is seductive: slow overnight I/O causing election timeouts
fits "always between 02:00 and 04:00", and the zookeeper log's latency line
invites a look at storage. `fixtures/disk-latency.csv` kills it: p99 read
and write latency is flat between 1.7 and 2.4 ms all night — including
through 02:20, when the quorum actually failed — and utilization never
exceeds 14%. A latency-induced election timeout shows latency spikes at the
failure moment; there are none. The second weak signal is the node pool
itself: `fixtures/az-aks-nodepool-list.json` shows the user pool is
`scaleSetPriority: Spot`, which reads as "spot reclamation" — plausible, but
the autoscaler log shows the pods were *evicted by scale-down*, not preempted
by Azure, and the missing budget, not the spot pricing, is what let two
replicas be disrupted together.

## Root cause

The cluster autoscaler's nightly scale-down of the spot user pool evicts
every pod on the node it removes in one pass
(`fixtures/cluster-autoscaler.log:4-5`); with no PodDisruptionBudget in the
namespace (`fixtures/kubectl-get-pdb.txt`) and no
`safe-to-evict`/anti-affinity on the pod template
(`fixtures/statefulset.yaml:35-36`), two of the three zookeeper replicas are
disrupted simultaneously. The surviving replica loses quorum and exits
(`fixtures/zookeeper.log:4-5`), repeatedly, whenever the scale-down happens
to pick a node hosting two members — which is why the pattern recurs on
10/18, 10/22, and 10/25, always overnight when utilization is lowest.

## Fix

Cap simultaneous disruption and opt the ensemble out of best-effort
eviction:

```yaml
apiVersion: policy/v1
kind: PodDisruptionBudget
metadata:
  name: zookeeper
  namespace: streaming
spec:
  minAvailable: 2
  selector:
    matchLabels:
      app: zookeeper
```

plus, on the StatefulSet pod template:

```yaml
annotations:
  cluster-autoscaler.kubernetes.io/safe-to-evict: "false"
```

With the PDB in place, the autoscaler's eviction API calls for the second
zookeeper replica are refused, and it either picks a different node or skips
the scale-down; `safe-to-evict: "false"` stops the pod being treated as
best-effort removable. Both changes are additive config on the StatefulSet
namespace — no restart required, no data touched. (Add pod anti-affinity so
replicas stop sharing a removable node; without it the PDB only slows the
next double placement down.)

## Prevention

- **PDB for every quorum service:** make "quorum-based StatefulSet ⇒
  PodDisruptionBudget with minAvailable = quorum size" a lint rule in the
  manifest repo — the absence in `fixtures/kubectl-get-pdb.txt` would have
  failed CI before the first outage.
- **Scale-down simulation in staging:** run the autoscaler in dry-run mode
  against the staging cluster with production-shaped replica placement and
  assert no two members of a quorum set are ever evicted in one drain.
- **Alert on `safe-to-evict` omissions:** flag workloads with
  `stateful` labels or named headless services that lack the annotation, so
  new StatefulSets cannot silently inherit best-effort eviction behavior.

## Further reading

- [Pod Disruption Budgets — Kubernetes](https://kubernetes.io/docs/concepts/workloads/pods/disruptions/)
- [Cluster autoscaler: how scale-down works and safe-to-evict](https://github.com/kubernetes/autoscaler/blob/master/cluster-autoscaler/FAQ.md#how-does-scale-down-work)
- [Reserve compute resources / eviction annotations](https://kubernetes.io/docs/concepts/scheduling-eviction/api-eviction/)
- [AKS cluster autoscaler](https://learn.microsoft.com/azure/aks/cluster-autoscaler)
