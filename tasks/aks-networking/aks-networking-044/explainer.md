# aks-networking-044 — explainer

## How to investigate

A **403 that is path-scoped** is the loudest clue in the symptom: the same
client, the same service, the same port — one URL works, its sibling doesn't.
Connectivity problems (routing, DNS, security groups, ports) are path-blind;
anything that can tell `/api/v2` from `/api/v1` is operating at the HTTP
layer. On a Cilium cluster (confirmed by `fixtures/az-aks-show.json`:
`networkDataplane: cilium`), that means an L7 policy is the first suspect, and
Hubble is the tool that adjudicates it.

`fixtures/hubble-observe.txt` settles it in six lines. The dropped flows read:

```
GET /api/v2/orders VERDICT=DROPPED rule=CiliumNetworkPolicy/store/orders-api-l7
```

— while the very same source pod, destination pod, and port forwards:

```
GET /api/v1/orders VERDICT=FORWARDED rule=CiliumNetworkPolicy/store/orders-api-l7
```

The rule attribution matters: the drop names the *policy that did it*, and
that policy is a Cilium one, not the k8s one. `fixtures/client.log` then
corroborates: the bff sees exactly 403 on v2 and 200 on v1. Read the policy
itself next: `fixtures/ciliumnetworkpolicy.yaml` has exactly one L7 http rule
— `method: GET, path: /api/v1/.*`. There is no v2 entry because there is no
v2 entry anywhere.

## Why the red herring misleads

`fixtures/kubectl-get-networkpolicy.yaml` shows a standard Kubernetes
`NetworkPolicy` (`allow-frontend-to-api`) allowing the bff to reach orders-api
on port 8080 — the intuitive first stop, because `kubectl get networkpolicy`
is what most people check first. It is *healthy and irrelevant*: a k8s
NetworkPolicy has **no path awareness at all**. It selects pods and ports;
HTTP paths are invisible to it. It cannot block `/api/v2` while allowing
`/api/v1` on the same port, so no reading of that file, however suspicious,
can explain a path-scoped 403. The symmetric mistake — concluding the k8s
policy is the culprit because it exists and mentions bff — is exactly the
"stop at the first plausible policy" failure. The L3/L4 layer in this cluster
is clean; the action is one layer up, in the Cilium L7 rules.

## Root cause

The `CiliumNetworkPolicy` `orders-api-l7` in the store namespace
(`fixtures/ciliumnetworkpolicy.yaml`) allows ingress from the bff pods on port
8080, but its L7 http rules whitelist a single path: `GET /api/v1/.*`. The new
`/api/v2/orders` route was never added. Cilium's L7 enforcement therefore
drops every `GET /api/v2/orders` from bff —
`VERDICT=DROPPED rule=CiliumNetworkPolicy/store/orders-api-l7` in
`fixtures/hubble-observe.txt` — and returns 403 to the client
(`fixtures/client.log`), while `/api/v1/orders` flows through the same policy
unimpeded. The 403 is generated at Cilium's proxy layer; orders-api never
sees the request, which is why the pods are Ready and serve v2 fine from an
unpoliced debug pod.

## Fix

Add the v2 path to the policy's L7 rules:

```yaml
rules:
  http:
    - method: GET
      path: /api/v1/.*
    - method: GET
      path: /api/v2/.*
```

(or a single regex `/api/v[12]/.*` if version families move together). Apply,
then verify from the data plane rather than the manifest:

```bash
hubble observe --namespace store --last 20 --output compact
```

`GET /api/v2/orders` should flip from `VERDICT=DROPPED` to
`VERDICT=FORWARDED`, and the bff's checkout flow should see 200s again. Note
the general shape: an L7 policy is a **positive allowlist** — when it exists,
*anything not listed is denied*, so every new route a service ships must land
in the policy in the same rollout, or the policy rollout must include a
diff of routes-in-code versus routes-in-policy.

## Prevention

1. **Ship policy changes with route changes.** Any PR that adds an API path
   to a service under L7 policy should touch the CiliumNetworkPolicy in the
   same change; CI can enforce this by diffing route registrations against
   policy paths.
2. **Test policies through Hubble, not just kubectl.** `kubectl get cnp -o
   yaml` proves intent, not behavior; a synthetic check that hits each
   route and asserts FORWARDED verdicts catches a missing path in seconds.
3. **Alert on path-scoped drop patterns.** Hubble drops where the
   source/destination pair forwards other paths on the same port is a
   missing-allowlist-entry signature, distinct from a client-auth 403.

## Further reading

- [Cilium — L7 policy](https://docs.cilium.io/en/latest/security/policy/language/#layer-7-policy)
- [Cilium — Hubble observe](https://docs.cilium.io/en/latest/observability/hubble/)
- [AKS — Cilium dataplane](https://learn.microsoft.com/azure/aks/concepts-cilium)
- [Kubernetes NetworkPolicy — what it cannot express (paths, methods)](https://kubernetes.io/docs/concepts/services-networking/network-policies/)
