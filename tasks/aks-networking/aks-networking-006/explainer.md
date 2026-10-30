# aks-networking-006 — explainer

## How to investigate

Start with what the scanner actually claims: `fixtures/security-scan-finding.json`.
Finding SEC-2026-1042 flags public IP `20.162.44.128` (`a3f7c1d9-public-lb-pip`)
as reachable from the internet while the related workload is classified as
VNet-only — `finance/ledger-internal` on cluster `aks-finops-prod`. Then
corroborate it from the Kubernetes side: `fixtures/kubectl-get-svc.txt` shows
`ledger-internal` as `type: LoadBalancer` with a **public** `EXTERNAL-IP` of
`20.162.44.128`. The exposure is real, not a scanner false positive.

The decisive artifact is `fixtures/service.yaml`. Its single annotation reads:

```yaml
service.beta.kubernetes.io/azure-load-balancer-internal-only: "true"
```

The annotation the Azure cloud provider recognizes for an internal service is
`service.beta.kubernetes.io/azure-load-balancer-internal`. The stray `-only`
suffix makes the key unknown, and the provider **silently ignores unknown
annotations** — the API server accepts any `service.beta.kubernetes.io/*` key —
so the service reconciled as a plain public LoadBalancer.

Cross-check on the Azure side with `fixtures/az-network-lb-list.json`: the
public `kubernetes` load balancer owns the frontend carrying `20.162.44.128`,
while the internal `kubernetes-internal` load balancer (private frontend
`10.240.0.100`) has no frontend for this service. The service landed on the
wrong load balancer.

## Why the red herring misleads

`fixtures/nsg-rules.json` shows `aks-nsg` with `AllowVNetInBound` followed by
`DenyInternetInBound`, which invites two wrong conclusions: "the NSG is broken
and must have changed" or "the NSG denies internet anyway, so the scan is a
false positive." Both fail. An NSG governs what traffic can **reach** the
subnet; it says nothing about whether a service was handed a public frontend on
the load balancer — the exposure *is* the public IP existing at all. And the
symptom itself confirms the rules are fine ("the network team confirms no
inbound rules have changed this month", and the scan finding says no NSG change
in 30 days). Likewise, the ledger-posting batch jobs still completing only
proves internal reachability works; it tells you nothing about public
reachability, which is exactly the problem.

## Root cause

The `ledger-internal` Service's annotation is misspelled —
`service.beta.kubernetes.io/azure-load-balancer-internal-only` instead of
`service.beta.kubernetes.io/azure-load-balancer-internal` — in
`fixtures/service.yaml`. The cloud provider never saw the service as internal,
provisioned it on the **public** load balancer, and exposed it on
`20.162.44.128` (`fixtures/kubectl-get-svc.txt`,
`fixtures/az-network-lb-list.json`). The NSG never had a chance to matter:
`DenyInternetInBound` filters traffic, but the public frontend should never
have existed.

## Fix

Correct the annotation to the exact key the provider recognizes, and re-apply:

```bash
kubectl -n finance annotate service ledger-internal \
  service.beta.kubernetes.io/azure-load-balancer-internal- \
  --overwrite
kubectl -n finance annotate service ledger-internal \
  service.beta.kubernetes.io/azure-load-balancer-internal="true"
```

(or edit `service.yaml` and `kubectl apply -f service.yaml`). The provider then
reconciles the service onto the internal load balancer and releases the public
IP. Verify with `kubectl get svc -n finance ledger-internal` — `EXTERNAL-IP`
should become the private `10.240.x.x` — and confirm
`a3f7c1d9-public-lb-pip` disappears from the resource group. Safe and
reversible: it is one annotation, and reverting restores the previous state.
One operational note: the service's frontend IP **changes** during the
migration, so any VNet client that hardcoded `20.162.44.128` must switch to the
new private IP.

## Prevention

Two guardrails, one for the typo and one for the exposure:

1. **Catch unknown Azure LB annotations at deploy time.** A Kyverno policy (or
   a CI manifest lint) that fails any `LoadBalancer` Service carrying a
   `service.beta.kubernetes.io/azure-load-balancer-*` annotation the provider
   does not recognize — and any internal-only workload missing the exact
   `azure-load-balancer-internal` key — turns this typo into a rejected
   manifest. Unknown annotations pass `kubectl apply` silently, so CI is the
   only place they can be caught.
2. **Alert on new public IPs in protected resource groups** rather than
   waiting for the weekly scan. SEC-2026-1042 worked — the 48-hour SLA was
   honored — but a near-real-time alert on public IP creation in
   `rg-finops-prod` would have closed the exposure in minutes.

## Further reading

- [Use an internal load balancer with Azure Kubernetes Service (AKS)](https://learn.microsoft.com/azure/aks/internal-load-balancer)
- [AKS load balancer Service annotations reference](https://learn.microsoft.com/azure/aks/load-balancer-standard)
- [Kubernetes documentation: Service types](https://kubernetes.io/docs/concepts/services-networking/service/#publishing-services-service-types)
