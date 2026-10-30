# aks-networking-004 — explainer

## How to investigate

The symptom hands you the change window: a "security hardening change" hit the
billing namespace about twenty minutes before the timeouts. That is the first
place to look — what exactly landed? `kubectl get networkpolicy -n billing`
is the fastest read:
`fixtures/kubectl-get-networkpolicy.yaml:10-19` shows the namespace's **only**
NetworkPolicy: `default-deny-all`, created `2026-10-30T09:18:44Z` (line 10)
— minutes before billing-api started timing out — with `podSelector: {}`
(line 17, every pod in the namespace) and `policyTypes: [Ingress]` (lines
18–19, deny all inbound traffic to all pods unless another policy allows it).
The list contains nothing else: no allow policy for postgres exists, so every
ingress connection to postgres on 5432 is dropped.

Confirm the client side matches that story. `fixtures/billing-api.log:2` shows
`Connection to postgres.billing.svc.cluster.local:5432 timed out`, and two
lines deeper in the log the same process proves what is *not* broken:
line 20 shows DNS resolving the service name fine
(`postgres.billing.svc.cluster.local -> 10.244.1.88, 10.244.2.37`). DNS good
+ connect-timeout = packets being silently dropped, which is what a default
deny looks like from the client.

Then rule out the pods and the Service so the policy is the only remaining
explanation: `fixtures/kubectl-get-pods-show-labels.txt:2-6` shows all
billing-api and postgres pods Running, Ready, zero restarts — with the
labels you'll need for the fix (`app=billing-api` on the clients, `app=postgres`
on the database). `fixtures/kubectl-get-svc-postgres.yaml:20-26` shows the
postgres Service exposing port 5432 with a correct `app: postgres` selector.
Service, DNS, pods: all healthy. Only the policy layer stands between them.

## Why the red herring misleads

The postgres Service is the attractive suspect — a hardening change *could*
have touched it, and a wrong selector would produce exactly these symptoms.
But `fixtures/kubectl-get-svc-postgres.yaml:25-26` shows the selector is
`app: postgres`, matching both pods' labels
(`fixtures/kubectl-get-pods-show-labels.txt:5-6`), and the DNS line in
`fixtures/billing-api.log:20` resolves two real pod IPs through it — a broken
selector usually breaks DNS-to-endpoint resolution, not the TCP connect that
follows. The second red herring is postgres itself: a "Running and Ready"
statefulset that logs no errors can be read as "postgres is refusing
connections", but `fixtures/postgres.log:1-8` shows routine checkpoints and
analyzes — and, tellingly, **no connection attempts at all**. Postgres never
sees the traffic; if postgres were crashing or rejecting, the client would
get connection refused or auth errors, not the silent connect timeouts in
`fixtures/billing-api.log:7`. A drop with no server-side trace is the
signature of a NetworkPolicy, and the deny is standing in front of the
database, not inside it.

## Root cause

The hardening wave applied a default-deny ingress NetworkPolicy to the
billing namespace (`podSelector: {}`, `policyTypes: [Ingress]`,
`fixtures/kubectl-get-networkpolicy.yaml:16-19`) without adding an allow rule
for postgres traffic. Billing-api's connections to postgres on 5432 are
dropped by policy — hence connect timeouts (`fixtures/billing-api.log:2`)
while DNS, the Service, and both pods remain healthy
(`fixtures/billing-api.log:20`, `fixtures/kubectl-get-svc-postgres.yaml`,
`fixtures/kubectl-get-pods-show-labels.txt`).

## Fix

Add an allow policy that pokes exactly the needed hole in the default deny:

```yaml
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: allow-billing-api-to-postgres
  namespace: billing
spec:
  podSelector:
    matchLabels:
      app: postgres
  policyTypes:
  - Ingress
  ingress:
  - from:
    - podSelector:
        matchLabels:
          app: billing-api
    ports:
    - protocol: TCP
      port: 5432
```

Applied with `kubectl apply -f`, this takes effect in seconds — policies are
additive, so the default-deny stays in force for everything else and the
invoice batch's retries succeed on the next flush. Fully reversible: delete
the allow policy and you are back to the deny-all posture.

## Prevention

- **Baseline policies ship with their exceptions.** The NS-4b baseline (per
  the change-cause annotation on
  `fixtures/kubectl-get-networkpolicy.yaml:7-9`) defined the deny but not the
  allow policies for the namespace's known flows. Guardrail: the IaC module
  or CI check that applies a default-deny to a namespace must fail unless
  every Service in that namespace has a matching allow policy — deny-first
  hardening without allowlist coverage is a pre-broken namespace.
- **Post-hardening smoke test:** after any NetworkPolicy change, run a
  synthetic connect from each tier-1 client to its backend port (here:
  `psql` from a billing-api pod to `postgres.billing:5432`) in the deploy
  pipeline. A policy mistake surfaces in CI, not in the invoice batch at
  09:40.

## Further reading

- [Kubernetes NetworkPolicy — default deny and allow rules](https://kubernetes.io/docs/concepts/services-networking/network-policies/)
- [Azure CNI network policies on AKS](https://learn.microsoft.com/azure/aks/use-network-policies)
- [Declare Network Policy (Kubernetes task walkthrough)](https://kubernetes.io/docs/tasks/administer-cluster/declare-network-policy/)
