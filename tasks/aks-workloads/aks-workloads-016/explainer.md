# aks-workloads-016 — explainer

## How to investigate

Start with what the pod itself says: `fixtures/kubectl-describe-pod.txt` shows
the new replica `Ready: False` with repeated `Readiness probe failed: Get
"http://10.244.3.71:3000/healthz": dial tcp 10.244.3.71:3000: connect:
connection refused`. "Connection refused" is a precise signal — the pod is up
and reachable, but nothing is listening on port 3000.

Ask what the new image actually listens on: `fixtures/profile-svc.log` shows
2.4.0 starting cleanly with `HTTP server listening on http://0.0.0.0:8080`,
while `fixtures/deployment.yaml` still sends the readiness probe to `port:
3000`. The probe and the server disagree about the port. For when that
changed, `fixtures/Dockerfile.diff` is the receipt: alongside the base image
bump, `EXPOSE` moves from 3000 to 8080.

Finally, `fixtures/kubectl-rollout-status.txt` shows the downstream symptom —
the rollout exceeds its progress deadline because the new replica can never
turn Ready and the old replicas wait for it.

## Why the red herring misleads

The symptom blames the base image bump, and the diff's most eye-catching line
is `node:20.11.1-slim` → `node:20.15.0-slim`. A patch release of the base
image is very unlikely to change how the server binds — and the log proves the
server starts fine. The load-bearing line in the same diff is `EXPOSE 8080`:
the release changed the listen port, and the Deployment's probes and
`containerPort` were never updated. The old replicas keep serving on 3000,
which is exactly why the service stayed healthy while the rollout wedged —
nothing user-visible broke, so nothing pointed at the port.

## Root cause

The readiness probe targets port 3000 (`fixtures/deployment.yaml`) but the
2.4.0 image listens on 8080 (`fixtures/profile-svc.log`; `EXPOSE 8080` in
`fixtures/Dockerfile.diff`), so every readiness check is refused and the new
replica can never become Ready (`fixtures/kubectl-describe-pod.txt`), stalling
the rollout past its progress deadline (`fixtures/kubectl-rollout-status.txt`).

## Fix

Update the Deployment to match the image's listen port and roll again:

```yaml
ports:
  - name: http
    containerPort: 8080
readinessProbe:
  httpGet:
    path: /healthz
    port: 8080
```

Check the Service's `targetPort` too — if it names `http` it follows
automatically; if it hard-codes 3000 it needs the same bump. Then
`kubectl rollout status` should reach Ready with zero restarts; if anything
else still probes 3000, it will show up as the same `connection refused`.

## Prevention

Treat the listen port as part of the image's published contract: when a
release changes it, `containerPort`, the probes, and the Service `targetPort`
change in the same PR — the `EXPOSE` line alone updates nothing at runtime. A
pre-deploy smoke check that runs the new image under the production probe
configuration and asserts Ready within the progress deadline catches the
mismatch before the rollout starts instead of stalling it.

## Further reading

- [Configure liveness, readiness and startup probes](https://kubernetes.io/docs/tasks/configure-pod-container/configure-liveness-readiness-startup-probes/)
- [Deployments — progress deadline](https://kubernetes.io/docs/concepts/workloads/controllers/deployment/#progress-deadline-seconds)
