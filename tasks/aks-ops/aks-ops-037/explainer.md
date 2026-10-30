# aks-ops-037 — explainer

## How to investigate

An expired cert with a "healthy" issuer is a broken-renewal question: the
renewal path is `Certificate → ACME order → HTTP-01 challenge → some ingress
controller serving the challenge path`, and the failure lives at exactly one
hop. Walk the chain and let it rule out the obvious decoy early.

**Clear the decoy first: is cert-manager itself sick?** The symptom volunteers
that "the pods in the issuer's namespace are all running" — verify it and move
on. `fixtures/kubectl-get-pods-cert-manager.txt` shows `cert-manager`,
`cert-manager-cainjector`, and `cert-manager-webhook` all 1/1 Running with zero
restarts, and the warning-events query returns nothing. A crashed webhook or
cainjector would show restarts, not-ready containers, or warning events —
none here. The controller is also demonstrably *working*: it acquired its
lease, ran its controllers, and registered the ACME account
(`fixtures/cert-manager.log`). The renewal machinery is alive; what it is
doing is failing.

**Read the failure, not the symptom.** The Certificate
(`fixtures/kubectl-get-certificate.txt`) says it outright: renewal was due
2026-09-10, `Not After` was 2026-10-10, and issuance has been failing for 8
days ("certificate is past its Not After date and the ACME order never
completed"). The Challenge (`fixtures/kubectl-describe-challenge.txt`) has
been self-check-failing ~19,000 times over those 8 days: the challenge is
*Presented* (the solver ingress exists) but the self-check hits
`http://shop.contoso.com/.well-known/acme-challenge/<token>` and gets
**404, expected 200**. So: cert-manager created the solver, someone should be
serving the challenge path, but the traffic dies before reaching it.

**Find who was supposed to serve it.** Two artifacts close the case. The
ClusterIssuer (`fixtures/clusterissuer.yaml`) declares the http01 solver with
`ingressClassName: nginx`. But `fixtures/kubectl-get-ingressclass.txt` shows
the cluster's only IngressClass is `webapprouting.kubernetes.azure.com` — the
Azure web app routing add-on. There is no `nginx` class, so no controller
reconciles cert-manager's `cm-http-solver-*` ingress, and every request to the
challenge path 404s at the webapprouting controller. cert-manager logs the
same 404s while its ACME account registration succeeds — proving the account
and API are fine and only the challenge propagation is broken.

**Sanity-check the timeline.** Challenge created 2026-10-05 (8 days of
retries), cert expired 2026-10-10 — consistent with renewal due 09-10,
issuance stuck, expiry overtaking the retry loop.

## Why the red herring misleads

The distractor group is `cert-manager-webhook` / `cert-manager-cainjector` /
"cert-manager down". It hooks the strongest prior in cert-manager
troubleshooting: most issuances failures *are* webhook timeouts or cainjector
issues, and the symptom's phrasing ("the pods in the issuer's namespace are
all running") reads like reassurance you should second-guess. Three things
break the spell:

- `fixtures/kubectl-get-pods-cert-manager.txt` shows all three pods 1/1
  Running, 0 restarts, and **zero warning events** in 24h.
- `fixtures/cert-manager.log` shows a controller fully in charge of its
  domain: lease acquired, challenges controller running, ACME account
  registered. A dead component doesn't produce clean structured logs.
- The failure signature is *deterministic propagation 404*, not
  intermittent webhook errors or "no pending CertificateRequest" — the
  component that fails on a missing webhook behaves differently.

The 404 self-check failure is the signature of *nobody serving the path*, and
"nobody serving" is an ingress-controller/class problem, not a cert-manager
problem. The healthy-pods evidence is not a red herring to work around — it
is the exclusion proof.

## Root cause

The cluster's only ingress controller class is
`webapprouting.kubernetes.azure.com` (the webapprouting add-on,
`fixtures/kubectl-get-ingressclass.txt`), but the letsencrypt-prod
ClusterIssuer's http01 block declares `ingressClassName: nginx`
(`fixtures/clusterissuer.yaml`). That ingress class does not exist, so no
controller handles cert-manager's solver ingress and the ACME HTTP-01
challenge self-check gets a 404 on the challenge path
(`fixtures/kubectl-describe-challenge.txt`, `fixtures/cert-manager.log`). The
challenge never becomes valid, the ACME order never completes, and the
certificate expired on 10 October (`fixtures/kubectl-get-certificate.txt`);
the cert-manager pods are healthy and unrelated to the failure
(`fixtures/kubectl-get-pods-cert-manager.txt`).

## Fix

Point the solver's challenge ingress at the class that actually exists:

1. **Edit the ClusterIssuer:** change
   `spec.acme.solvers[].http01.ingressClassName` from `nginx` to
   `webapprouting.kubernetes.azure.com`
   (`kubectl edit clusterissuer letsencrypt-prod`). Alternatively, remove the
   `ingressClassName` field entirely to fall back to the cluster's default
   class — both satisfy the rubric; the explicit class is the safer of the
   two.
2. **Let cert-manager retry:** delete the pending Challenge
   (`kubectl -n shop delete challenge shop-contoso-tls-1-3847261917`) so a
   fresh order starts with the corrected solver ingress; the webapprouting
   controller will now serve the challenge path and the self-check returns
   200.
3. **Verify end to end:** the Certificate's Ready condition flips to True and
   the `shop-tls` secret is re-issued, then confirm
   `https://shop.contoso.com` serves the new cert — `Not After` at least 90
   days out — before tomorrow's promotion.

## Prevention

- **Alert on Certificate age, not on issuer errors:** alert when a
  Certificate's Ready condition is False or `Renewal Time` passes — cert-manager
  gives a 30-day renewal window and this failed silently through all of it
  because nothing watched the Certificate, only (nothing) from the issuer.
- **Validate ingress classes in the issuer config:** a policy/CI check that
  every `http01.ingressClassName` in ClusterIssuers matches an existing
  IngressClass catches this at apply time — the class reference is checked by
  nothing in the cluster today.
- **Pin the class in task/app issues:** the webhook solves cert-manager
  failures reflexively; chart the *class* of failure (404 on the challenge
  path = controller/ingress problem; webhook/timeout errors = cert-manager
  problem) in the runbook so the decoy costs minutes, not days.

## Further reading

- [cert-manager: ACME HTTP-01 challenges](https://cert-manager.io/docs/configuration/acme/http01/)
- [cert-manager: Troubleshooting issuing ACME certificates](https://cert-manager.io/docs/troubleshooting/solving-issues-with-acme/)
- [Azure AKS Web App Routing add-on](https://learn.microsoft.com/azure/aks/app-routing)
- [Kubernetes: IngressClass](https://kubernetes.io/docs/concepts/services-networking/ingress/#ingress-class)
