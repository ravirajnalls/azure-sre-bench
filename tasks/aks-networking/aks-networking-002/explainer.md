# aks-networking-002 — explainer

## How to investigate

Start at the layer that returns the error. The ingress controller log says
"502 with no upstream" far more precisely than the browser does:
`fixtures/ingress-nginx-controller.log:9` shows
`"No live upstreams while handling request" ... upstream="shop-storefront-80"`,
and line 11 confirms nginx served `/shop` with no upstream available. The same
log shows sibling paths healthy — line 17 routes `/cart` to a live endpoint
and line 20 routes `/account` to one — so the ingress controller, ingress
rules, and load balancer are all working; only the storefront service has
nothing behind it. That pushes the question one layer down: does the
`storefront` Service have endpoints?

Check that directly: `fixtures/kubectl-get-endpoints.txt:4` shows
`storefront <none>` while `account` (line 2) and `cart` (line 3) list real
pod IPs. A Service with no endpoints has exactly one cause class: its
selector matches zero pods.

So compare the selector against the pods. `fixtures/service.yaml:14-16`
selects `app: storefront, tier: web`, but `fixtures/deployment.yaml:29-31` —
this morning's pod template — labels pods `app: storefront, tier: frontend`.
`app` matches, `tier` does not. The change-cause annotation on
`fixtures/deployment.yaml:12-14` names it: release 2026.10.30 restructured
pod labels so `tier` reflects the frontend tier naming from the
service-catalog migration — the Service was never updated in the same
change. One label, both files, done.

## Why the red herring misleads

"All storefront pods Running and Ready" is stated right in the symptom, and
`fixtures/kubectl-get-pods.txt:2-4` confirms it: three healthy pods, zero
restarts, no crash loop. That genuinely rules out the usual suspects — a bad
release crashing pods, failing readiness probes, restart storms — and that is
why it misleads: it makes the problem look like it cannot be at the pod
layer, so investigators drift toward the ingress-nginx configuration
(`fixtures/ingress.yaml` reads clean, and the distractor list in
`expected.json` includes "nginx controller" for a reason). But "the pods are
healthy" and "the service can see the pods" are independent facts. The pods
are perfect; the Service just is not connected to them. The `no live
upstreams` warning is the tell: it is a Service-endpoint condition, not an
nginx misconfiguration.

## Root cause

The `storefront` Service selector pins `tier: web`
(`fixtures/service.yaml:14-16`) while the release 2026.10.30 pod template
labels pods `tier: frontend` (`fixtures/deployment.yaml:29-31`). The selector
matches zero pods, the Service has no endpoints
(`fixtures/kubectl-get-endpoints.txt:4`), and ingress-nginx logs "No live
upstreams" for `shop-storefront-80` and serves 502 for `/shop`
(`fixtures/ingress-nginx-controller.log:9-11`).

## Fix

Point the Service selector at the pod's current label:

```bash
kubectl patch svc storefront -n shop -p '{"spec":{"selector":{"tier":"frontend"}}}'
```

(or edit `service.yaml` line 16 from `tier: web` to `tier: frontend` and
re-apply). The change takes effect immediately — the endpoint controller
reconciles in seconds, endpoints repopulate, and the 502s stop without
touching the pods or the ingress. Verify with
`kubectl get endpoints storefront -n shop` showing the three pod IPs from
`fixtures/kubectl-get-pods.txt`.

Reversible and low risk: it widens the selector to match the pods that were
already meant to be served, and nothing else in the request path changes.

## Prevention

- **Ship the Service in the same PR as the pod label change.** The guardrail
  is a CI check (or `kubectl diff` in the deploy pipeline) that, for every
  Service in the manifests, resolves `spec.selector` against the pod
  templates being applied and fails when a selector matches zero templates —
  the exact gap that release 2026.10.30 shipped.
- **Alert on empty endpoints:** a controller/policy that watches Services
  which are backed by an Ingress and reports `endpoints == none` for more
  than a minute. `storefront <none>` sat observable in
  `kubectl get endpoints` from the moment the rollout finished; catching it
  before checkout traffic does turns a 502 incident into a deploy-time
  warning.

## Further reading

- [Service selectors and endpoints — Kubernetes Services](https://kubernetes.io/docs/concepts/services-networking/service/#defining-a-service)
- [Troubleshooting ingress-nginx — "No live upstreams"](https://kubernetes.github.io/ingress-nginx/troubleshooting/)
- [AKS ingress controller documentation](https://learn.microsoft.com/azure/aks/concepts-network-ingress)
