# aks-ops-018 — explainer

## How to investigate

Start where the upgrade itself is complaining: `fixtures/aks-activity-log.json`.
The activity log shows the `upgradeNodes` operation failing repeatedly with
status `Conflict`, and the message names the exact blocker — the drain of
`aks-npuser-31214664-vmss000000` failed on attempt 14 with
`Cannot evict pod as it would violate the pod's disruption budget` for the
`finance/ledger-writer` pods. So the upgrade is not hanging for an
infrastructure reason; the eviction API is refusing to move two specific pods.

Next confirm which budget is refusing: `fixtures/kubectl-get-pdb.txt` shows the
`ledger-writer` PDB in the `finance` namespace with `MIN AVAILABLE 2` and
`ALLOWED DISRUPTIONS 0`. A budget that allows zero disruptions can never be
satisfied by an eviction, so the drain waits forever. Compare with the healthy
`ledger-ui` and `coredns-pdb` rows, which each allow one disruption.

Then find out why the budget allows zero: `fixtures/pdb.yaml` sets
`minAvailable: 2` while `fixtures/deployment.yaml` shows the `ledger-writer`
Deployment running exactly `replicas: 2`. With two replicas and a requirement
to keep two available, every pod is load-bearing — the eviction API has no
slack to grant.

Sanity-check the node itself so you do not chase hardware ghosts:
`fixtures/kubectl-get-nodes.txt` shows
`aks-npuser-31214664-vmss000000` as `Ready,SchedulingDisabled` (cordoned by the
upgrader, otherwise healthy) and every other node `Ready`. Nothing is
`NotReady`, `MemoryPressure`, or `DiskPressure` — the only thing blocked is the
eviction.

Finally, place the blockage in the upgrade's overall progress:
`fixtures/az-aks-show.json` shows the cluster `provisioningState: Upgrading`
with the `npuser` pool still on `currentOrchestratorVersion: 1.30.6` while the
system pool is already on 1.31.1 — the wave stopped at the first user node.

## Why the red herring misleads

Two details invite wrong conclusions. First, `fixtures/deployment.yaml` shows
the Deployment's rolling-update strategy with `maxUnavailable: 0`, which reads
like "zero disruptions allowed". But that knob only governs the Deployment's
own rolling updates; node drains go through the eviction API, which consults
the PodDisruptionBudget — the Deployment strategy is irrelevant here. Second,
`fixtures/az-aks-show.json` shows `networkPolicy: calico`, and a stuck upgrade
naturally suggests a networking fault. Nothing in the evidence shows a network
problem: pods are serving, nodes are `Ready`, and the activity log explicitly
names the disruption budget.

## Root cause

The `ledger-writer` PodDisruptionBudget (`fixtures/pdb.yaml`) sets
`minAvailable: 2` over a Deployment of exactly 2 replicas
(`fixtures/deployment.yaml`), so the budget permits zero disruptions
(`fixtures/kubectl-get-pdb.txt`: `ALLOWED DISRUPTIONS 0`). The AKS node drain
during the 1.30.6 → 1.31.1 upgrade cannot evict either `ledger-writer` pod
(`fixtures/aks-activity-log.json`: "Cannot evict pod as it would violate the
pod's disruption budget"), so the drain of the first cordoned user node never
completes and the cluster-wide upgrade stalls. The cordoned node itself is
healthy (`fixtures/kubectl-get-nodes.txt`).

## Fix

Give the eviction API slack — either add headroom to the workload or relax the
budget. The safer production choice is to scale the Deployment above
`minAvailable`:

```bash
kubectl -n finance scale deployment/ledger-writer --replicas=3
```

or, if the workload genuinely cannot grow, relax the PDB to allow one
disruption:

```yaml
# pdb.yaml
spec:
  minAvailable: 1        # or: maxUnavailable: 1
```

Once `kubectl get pdb -n finance` reports `ALLOWED DISRUPTIONS 1`, AKS retries
the drain automatically (the activity log shows it already retrying), and the
1.31.1 upgrade proceeds through the remaining `npuser` nodes.

## Prevention

Treat `ALLOWED DISRUPTIONS 0` on a PDB that protects a node-drain-dependent
workload as an actionable alert, not a normal state. Two concrete guardrails:

- **Alert:** kube-state-metrics exposes
  `kube_poddisruptionbudget_status_allowed_disruptions`; alert when it has been
  0 for a pod that is not intentionally locked (or, more surgically, alert when
  a node is cordoned and a 0-disruption PDB matches pods on it).
- **CI/policy check:** reject manifests where a PDB's `minAvailable` is greater
  than or equal to the targeted Deployment's `replicas` (e.g. a Kyverno/OPA
  policy or a lint step that parses the pair). A budget that already spends
  every replica is misconfigured the moment it is written, and it will block
  the next upgrade or node drain.

Also run `kubectl get pdb -A` as part of the pre-upgrade checklist; AKS support
guidance recommends confirming disruption budgets allow drains before starting
a cluster upgrade.

## Further reading

- [AKS: Upgrade a Kubernetes cluster](https://learn.microsoft.com/azure/aks/upgrade-cluster)
- [Kubernetes: Pod Disruption Budgets](https://kubernetes.io/docs/concepts/workloads/pod-disruption-budget/)
- [Kubernetes: The Eviction API](https://kubernetes.io/docs/concepts/scheduling-eviction/api-eviction/)
