# aks-ops-019 — explainer

## How to investigate

Start at Flux itself, because that is what is failing: `fixtures/flux-kustomization-status.txt`
shows the `apps-edge-gateway` Kustomization with `READY False` and an apply
failure naming two errors — `no matches for kind "PodDisruptionBudget" in
version "policy/v1beta1"` and `no matches for kind
"HorizontalPodAutoscaler" in version "autoscaling/v2beta2"`. Every other
Kustomization in the same revision reconciles fine, so Flux, its credentials,
and the GitOps repo are healthy; only the manifests that pin removed API
versions are rejected.

Confirm the API server actually dropped those versions:
`fixtures/kubectl-api-versions.txt` lists what the upgraded cluster serves —
`policy/v1` and `autoscaling/v2` are present, and neither `policy/v1beta1` nor
`autoscaling/v2beta2` is. "No matches for kind" means exactly that: the
resource version no longer exists on the server.

Then check the offending manifests for yourself:
`fixtures/pdb.yaml` declares `apiVersion: policy/v1beta1` for the edge-gateway
PodDisruptionBudget and `fixtures/hpa.yaml` declares
`apiVersion: autoscaling/v2beta2` for the HorizontalPodAutoscaler. These are
the two objects Flux cannot create.

Finally, confirm the cluster side is not the problem:
`fixtures/az-aks-show.json` shows `kubernetesVersion: 1.29.7` with
`provisioningState: Succeeded` — the upgrade completed cleanly. The pods that
were running before the upgrade keep serving because their objects already
exist; only new applies fail.

## Why the red herring misleads

Both manifests look completely normal, and that is the trap. The HPA in
`fixtures/hpa.yaml` carries a plausible `averageUtilization: 70` CPU target and
a sensible `minReplicas: 2 / maxReplicas: 6`, so an "autoscaler
misconfiguration" reading feels natural — but no autoscaler is even running;
the apply is rejected before any controller sees the object. Likewise
`minAvailable: 1` in `fixtures/pdb.yaml` is a perfectly reasonable disruption
budget. The one anomalous field on each object is the `apiVersion` line, and
the Flux error message points straight at it. The healthy `provisioningState`
in `fixtures/az-aks-show.json` also rules out a failed upgrade — the upgrade
succeeded; the manifests did not.

## Root cause

The edge-gateway manifests are pinned to beta API versions that the upgraded
cluster no longer serves: `policy/v1beta1` for the PodDisruptionBudget
(`fixtures/pdb.yaml`) and `autoscaling/v2beta2` for the
HorizontalPodAutoscaler (`fixtures/hpa.yaml`). Both beta APIs were removed
before Kubernetes 1.29, and `fixtures/kubectl-api-versions.txt` confirms only
`policy/v1` and `autoscaling/v2` are served, so Flux's apply of the
`apps-edge-gateway` Kustomization fails with "no matches for kind"
(`fixtures/flux-kustomization-status.txt`). Running pods are unaffected because
their objects predate the upgrade.

## Fix

Migrate both manifests to the served versions and commit to the GitOps repo so
Flux can reconcile again:

```yaml
# pdb.yaml
apiVersion: policy/v1        # was policy/v1beta1; minAvailable carries over unchanged
```

```yaml
# hpa.yaml
apiVersion: autoscaling/v2   # was autoscaling/v2beta2; spec fields are compatible here
```

Watch `flux get kustomizations` until `apps-edge-gateway` reports
`READY True` — no cluster changes are needed, since the cluster itself
(`fixtures/az-aks-show.json`) is healthy.

## Prevention

- **Pre-upgrade API deprecation scan:** before scheduling an AKS upgrade, run
  the deprecation check against the GitOps repo — `kubectl-convert` (or the
  `pluto` detector, or `kubectl get --raw /deprecated`-style audits) flags
  every manifest pinned to an API version that the target version removes, as
  a CI step on the GitOps repo.
- **Alert on reconciliation failures:** alert on
  `gotk_reconcile_condition{type="Ready",status="False"}` (or Flux's
  `flux get kustomizations -A` in a scheduled check) so a broken apply is
  noticed in minutes, not on the next deploy.
- Catch the class early in dev clusters: upgrade a canary cluster first, where
  the same apply failure surfaces before production schedules its upgrade.

## Further reading

- [AKS: Upgrade a Kubernetes cluster](https://learn.microsoft.com/azure/aks/upgrade-cluster)
- [Kubernetes: Deprecated API Migration Guide](https://kubernetes.io/docs/reference/using-api/deprecation-guide/)
- [Flux: Kustomize controller reconciliation](https://fluxcd.io/flux/components/kustomize/)
