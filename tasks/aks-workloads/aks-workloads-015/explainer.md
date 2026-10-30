# aks-workloads-015 — explainer

## How to investigate

The scheduler already names both halves of the failure in
`fixtures/kubectl-describe-pod.txt`: `3 node(s) didn't match Pod's node
affinity/selector, 1 node(s) had untolerated taint {sku: gpu}`. The one GPU
node is refused for a taint, and the three regular nodes are refused by the
pod's own nodeSelector — so the only node that could host the pod is blocking
it.

Confirm the taint's origin: `fixtures/az-aks-nodepool-show-gpu01.json` lists
`nodeTaints: ["sku=gpu:NoSchedule"]` on gpu01. Then confirm the pod side:
`fixtures/deployment.yaml` sets `nodeSelector:
kubernetes.azure.com/agentpool: gpu01` and has no `tolerations` block.
`fixtures/kubectl-get-nodes-show-labels.txt` shows the single gpu01 node Ready
and carrying the matching `kubernetes.azure.com/agentpool: gpu01` label — the
selector is satisfiable; only the taint blocks placement.

## Why the red herring misleads

The symptom foregrounds the NVIDIA device plugin — it is running with clean
logs, which sounds like the thing to verify before anything else.
`fixtures/nvidia-device-plugin.log` indeed shows `device nvidia.com/gpu/0
marked healthy` on every periodic check: the GPU hardware and plugin are fine,
and they were never the question. Scheduling is refused on the
taint/selector match, which happens before GPU counting would ever matter —
a healthy device plugin cannot rescue a pod the scheduler will not place.

## Root cause

The gpu01 pool was created with the taint `sku=gpu:NoSchedule`
(`fixtures/az-aks-nodepool-show-gpu01.json`) and the video-transcoder pods
carry no matching toleration (`fixtures/deployment.yaml`), so the scheduler
rejects the only node that satisfies the pod's nodeSelector —
`1 node(s) had untolerated taint {sku: gpu}` in
`fixtures/kubectl-describe-pod.txt`.

## Fix

Add the matching toleration to the video-transcoder pod spec:

```yaml
tolerations:
  - key: sku
    operator: Equal
    value: gpu
    effect: NoSchedule
```

Roll the deployment; both pods schedule onto gpu01 immediately. Key the
toleration exactly to the taint (`key: sku`, `value: gpu`, `effect:
NoSchedule`) — a wider `operator: Exists` toleration would also admit future,
unrelated taints on the pool.

## Prevention

Treat a node pool's taint and the workloads' tolerations as one contract: when
a tainted pool is created, ship the toleration in the same change that
repoints workloads at it, and never rely on the pool staying untainted. A
policy check (e.g. Kyverno or OPA Gatekeeper) can reject workloads whose
nodeSelector targets a tainted pool without a matching toleration before they
ever reach the cluster.

## Further reading

- [Taints and tolerations](https://kubernetes.io/docs/concepts/scheduling-eviction/taints-and-tolerations/)
- [Assign pods to nodes](https://kubernetes.io/docs/concepts/scheduling-eviction/assign-pod-node/)
