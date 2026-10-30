# aks-workloads-012 — explainer

## How to investigate

Start where the loop is visible: `fixtures/kubectl-describe-pod.txt`. The events
list roughly 40 container starts in under ten minutes — `Created` and `Started`
at `x40 over 9m37s` — interleaved with `Liveness probe failed: HTTP probe
failed with statuscode: 500` (x120) and `Killing … container failed liveness
probe, will be restarted` (x38). The pods are being killed by the kubelet on a
schedule; they are not crashing on their own.

Read the deployment next: `fixtures/deployment.yaml`. The only health gate is
the liveness probe — `initialDelaySeconds: 10`, `periodSeconds: 10`,
`failureThreshold: 3` — so the first check fires 10 seconds after start and the
third failure lands around 40 seconds in. There is no `startupProbe`, so
liveness is enforced from the very beginning of the container's life.

Then time the death against the warm-up in `fixtures/catalog-api.log`: the
process starts, connects to products-db in ~21ms, and the warm-up crawls at
about 5% per 5 seconds — 3,214 skus needs roughly 90 seconds. `GET /healthz 500
— cache warm-up in progress` appears every 10 seconds until `SIGTERM received —
shutting down, warm-up aborted at 35%` at ~40 seconds after start (exit code
143). Every restart dies at the same fraction: the pod never survives long
enough to finish warming up.

## Why the red herring misleads

The symptom invites two wrong turns. First, "the warm-up reads from
products-db, so the database must be slow or failing" — but the log shows the
connection established in 21ms and the warm-up progressing steadily, and the
symptom itself says the database shows no errors or slow queries. Second,
`fixtures/kubectl-top-pod.txt` looks like an exculpatory dead end but is
actually the argument that closes the resource story: catalog-api sits at 46m
CPU / 213Mi memory, far inside its 1 CPU / 512Mi limits. There is no OOM, no
throttling, and no crash in the application itself — the only thing killing the
container is the kubelet's liveness check firing while `/healthz` is still
returning 500 by design.

## Root cause

The v2.7.1 release made the first ~90 seconds of pod life a
500-until-warm-up-completes period (`fixtures/catalog-api.log`), but the probe
configuration still assumes an almost-instant start: `fixtures/deployment.yaml`
gives the liveness probe a 10s initial delay and a 3-failure budget (~40s) and
defines no `startupProbe` to defer liveness enforcement. SIGTERM arrives at ~40s
on every cycle and `fixtures/kubectl-describe-pod.txt` shows the resulting
kill/restart loop. Kubernetes is working exactly as configured — the
configuration no longer matches the application's startup shape.

## Fix

Give the warm-up its own phase with a `startupProbe` on the catalog-api
container, so liveness is not enforced until startup completes:

```yaml
startupProbe:
  httpGet:
    path: /healthz
    port: 8080
  periodSeconds: 5
  failureThreshold: 30   # up to ~150s of startup budget
```

With the startupProbe in place, liveness checks (and their 10s/3-failure
budget) only begin after startup succeeds, while the existing liveness probe
keeps guarding steady state. The alternative — raising
`initialDelaySeconds` above the ~90s warm-up — works but permanently delays
detection of genuine start failures. Roll the deployment and confirm restart
counts stay at zero through a complete warm-up.

## Prevention

Treat "how long until this container is genuinely ready" as a release-visible
property: any change that adds startup work (cache warm-up, migrations, index
builds) needs its startup budget re-sized in the same PR. A startupProbe with a
generous `failureThreshold` is the cheap default — it costs nothing once the
pod is healthy. In CI, run new images under the production probe configuration
and assert the pod reaches Ready without restarts before promoting the rollout.

## Further reading

- [Configure liveness, readiness and startup probes](https://kubernetes.io/docs/tasks/configure-pod-container/configure-liveness-readiness-startup-probes/)
- [Pod lifecycle — container probes](https://kubernetes.io/docs/concepts/workloads/pods/pod-lifecycle/#container-probes)
