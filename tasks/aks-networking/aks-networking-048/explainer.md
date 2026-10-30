# aks-networking-048 — explainer

## How to investigate

A 502 from a load balancer in front of pods that are **Ready** is the
signature to sit with for a moment. Kubernetes readiness (kubelet probes) and
load-balancer health (App Gateway probes) are two *separate* systems asking
separate questions. If readiness says 1/1 and the LB still says 502, the two
systems disagree — so the question is not "is the app up?" but "what is the
load balancer asking, and what does the app answer to *that*?"

Ask the load balancer first. `fixtures/az-network-application-gateway-show-backend-health.json`
answers it directly: both catalog backend addresses report `httpStatus: 404`
under `default-probe` — and the probe list shows that probe's `path: "/"`.
404, not timeout, not connection refused: the gateway **reached** the pods and
the pods replied "no such route." That already kills a whole class of
hypotheses (NSGs, routing, wrong ports would never produce a crisp HTTP 404).

Then ask the app what it serves. `fixtures/catalog.log` shows the whole
picture in one place: `GET /` from user-agent
`Azure-Application-Gateway/HealthProbe` returning 404, `GET /healthz` from
`kube-probe/1.29` returning 200, and real API traffic returning 200. The app
is alive and healthy — it simply has no route at `/`, which is exactly where
Application Gateway's **default** probe looks when nobody tells it otherwise.
The last piece is the negative: `fixtures/ingress.yaml` carries no
`appgw.ingress.kubernetes.io/health-probe-path` annotation — nothing ever told
AGIC where the health endpoint lives, so it defaulted to `/`.

## Why the red herring misleads

`fixtures/nsg-rules.json` shows the subnet NSG with an allow rule for App
Gateway → backends on 8080 plus the usual internet 80/443 entries. The
symptom even hands you the hook: "the security review of the new path came
back clean" — which sounds like a reason to suspect the security layer, and a
blocked probe is a classic 502 cause in general. But a blocked probe looks
like a **timeout or connection refused** in backend health, never a clean
HTTP 404 with a status code. The 404 proves the probe traversed the network,
reached the pod, was answered by the app, and was rejected by *application
routing*. The NSG is permissive, and the review was right. What failed is the
probe's *path*, a detail no security tooling looks at — which is precisely why
a clean security review and a broken migration can both be true.

## Root cause

Application Gateway's backend health probe for the catalog pool still uses the
default probe path `/` — `fixtures/az-network-application-gateway-show-backend-health.json`
shows `default-probe` with `path: "/"` returning HTTP 404 for both backend
addresses (10.0.2.21, 10.0.2.22) — while the catalog app 404s on `/` and
exposes health at `/healthz` (`fixtures/catalog.log`). The ingress
(`fixtures/ingress.yaml`) sets no `appgw.ingress.kubernetes.io/health-probe-path`
annotation, so AGIC never learned the real health endpoint. With both backends
failing the probe, Application Gateway marks the pool Unhealthy and answers
502 to every external request even though the pods are Ready and healthy.

## Fix

Teach AGIC where the health endpoint is, on the catalog ingress:

```yaml
metadata:
  annotations:
    kubernetes.io/ingress.class: azure/application-gateway
    appgw.ingress.kubernetes.io/health-probe-path: "/healthz"
    appgw.ingress.kubernetes.io/health-probe-statuscode: "200"  # optional — default success codes are 200-399
```

Apply and verify at the gateway, not in Kubernetes:

```bash
az network application-gateway show-backend-health \
  -g rg-aks-prod --name appgw-prod
```

Both `10.0.2.2x` hosts should flip to `httpStatus: 200` within a probe
interval or two (the default interval is 30 s, with `unhealthyThreshold: 3`),
and the 502s stop as soon as the pool goes healthy. Worth adopting in the
same change: a probe on a real lightweight endpoint (`/healthz`) rather than
`/` is the durable convention — the default probe only ever works by
accident, for apps that happen to serve something at `/`.

## Prevention

1. **Make the probe annotation part of the migration template.** Any
   service moved onto AGIC/App Gateway gets `health-probe-path` set in the
   same PR — the migration checklist item that was missed here.
2. **Watch backend health as a first-class signal.** A 502 spike with
   Ready pods is diagnosable in one `show-backend-health` call; alerting on
   `UnhealthyHosts > 0` pages before customers notice.
3. **Probe the app's declared health endpoint everywhere.** Kubelet
   readiness already knows the answer (`/healthz`, 200 in the pod spec);
   the LB probe should point at the same route, not a guessed one.

## Further reading

- [AGIC — health probe annotations](https://azure.github.io/application-gateway-kubernetes-ingress/annotations/health-probe/)
- [Application Gateway — backend health overview](https://learn.microsoft.com/azure/application-gateway/application-gateway-probe-overview)
- [Diagnose backend health with az CLI](https://learn.microsoft.com/cli/azure/network/application-gateway#az-network-application-gateway-show-backend-health)
- [AKS — Application Gateway Ingress Controller](https://learn.microsoft.com/azure/aks/app-gateway-ingress-controller)
