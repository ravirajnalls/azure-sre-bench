# aks-workloads-017 — explainer

## How to investigate

Let the HPA state its own problem: `fixtures/kubectl-describe-hpa.txt` shows
`ScalingActive: False — FailedGetResourceMetric … failed to get cpu
utilization: missing request for cpu`, and the events repeat
`FailedComputeMetricsReplicas` (x24 over 22m). `fixtures/kubectl-get-hpa.txt`
renders the same fact in one token: `TARGETS <unknown>/70%`. Utilization is
usage divided by request — the HPA has the usage but no request to divide by,
so the target side reads `<unknown>`.

Rule out the metrics path, because the symptom points straight at it:
`fixtures/kubectl-top-pod.txt` shows live per-pod usage (~900m for every
checkout-api replica) and `fixtures/metrics-server.log` shows successful
scrapes (`Scrape successful … cpuNanoCoreSum=908107000`, `podsFailed=0`). The
pipeline works end to end. The missing half is in the manifest:
`fixtures/deployment.yaml` defines no `resources` block at all, while
`fixtures/hpa.yaml` asks for a Resource-metric target of 70% CPU utilization.

## Why the red herring misleads

"Metrics dashboards show live utilization data flowing" makes a metrics-server
outage feel like the live hypothesis, and an HPA pinned at minReplicas looks
like scaling is broken. But the HPA cannot turn raw usage into a percentage
without a request as the denominator — `<unknown>/70%` is the tell that the
pipeline is fine and the formula is missing an input. The pods really are
pegged (~900m per pod in `fixtures/kubectl-top-pod.txt`); the autoscaler is not
ignoring them, it is arithmetic-blocked.

## Root cause

The checkout-api container has no cpu resource request
(`fixtures/deployment.yaml`), so the HPA cannot compute CPU utilization —
`missing request for cpu` in `fixtures/kubectl-describe-hpa.txt`, echoed as
`<unknown>/70%` in `fixtures/kubectl-get-hpa.txt` — and it stays pinned at
minReplicas while the pods run hot (`fixtures/kubectl-top-pod.txt`) with
metrics flowing fine (`fixtures/metrics-server.log`).

## Fix

Give the container a cpu request so the utilization ratio exists (this is a
remediation task — the patched manifest is the graded artifact):

```yaml
resources:
  requests:
    cpu: "500m"
    memory: "512Mi"
```

Size the request against the behavior you want: with observed usage around
900m, a 500m request reads ~180% utilization and drives aggressive scale-out
toward `maxReplicas: 12` (`fixtures/hpa.yaml`); a 900m request would read ~100%
and scale more conservatively. Once the request exists, the HPA computes
utilization on its next sync and scales between min and max.

## Prevention

Treat a cpu/memory request as part of the definition of done for any
Deployment an HPA targets — by construction, a Resource-metric HPA is a
no-op without one. A policy check (e.g. Kyverno's require-resources rule, or a
CI manifest lint that walks `scaleTargetRef`s and asserts requests exist)
rejects the combination at review time instead of during the flash sale.

## Further reading

- [Horizontal Pod Autoscaler](https://kubernetes.io/docs/tasks/run-application/horizontal-pod-autoscale/)
- [Resource management for pods and containers](https://kubernetes.io/docs/concepts/configuration/manage-resources-containers/)
