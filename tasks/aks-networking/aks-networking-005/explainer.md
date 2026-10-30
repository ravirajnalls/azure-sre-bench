# aks-networking-005 — explainer

## How to investigate

Work from the pod outward. `kubectl describe` on a stuck pod splits the
failure precisely: `fixtures/kubectl-describe-pod.txt:53` shows the
application image from ACR **pulled successfully**, while line 55 shows the
init container's image from MCR failing —
`dial tcp 20.220.130.99:443: i/o timeout` against
`mcr.microsoft.com/oss/istio/proxyv2:1.22.0`. One registry works, the other
times out; that is a path/firewall distinction, not a node or kubelet
problem (the ACR pull over the same node proves the egress stack itself can
move bytes).

Establish how egress leaves the cluster: `fixtures/az-aks-show.json:15`
shows `outboundType: userDefinedRouting`, so node egress follows the route
table — and `fixtures/az-network-route-table-show.json:15-21` names it:
route `DefaultRouteToFirewall` sends `0.0.0.0/0` to the firewall appliance
at `10.20.1.4`. Every outbound connection from the AKS subnet now ends at
Azure Firewall, which only passes what a rule allows.

Then compare what the firewall allows against what the nodes need.
`fixtures/az-network-firewall-policy-rule-collection.json:16-45` lists the
entire `ProdAllowInternet` collection: `login.microsoftonline.com` and
`management.azure.com` (lines 22–25), `*.blob.core.windows.net` (line 34),
and the private ACR FQDN (line 43). There is **no** `AzureKubernetesService`
FQDN tag and no `mcr.microsoft.com` anywhere in the file. The firewall log
makes the consequence undeniable:
`fixtures/azfw-application-rule-log.kql.csv:2-8` shows repeated `Deny,
Default Action` rows — no rule matched — for `mcr.microsoft.com` and
`data.mcr.microsoft.com`, while lines 9–11 show the *allowed* entries for
`login.microsoftonline.com`, `management.azure.com`, and blob storage. That
allow/deny split is exactly the symptom's split: management-plane calls and
blob traffic succeed while image pulls from MCR time out, and the denied
pulled images leave nodes without the images they need to stay healthy —
the intermittent NotReady auto-repair alerts.

## Why the red herring misleads

The private ACR is the loudest decoy: the pod description shows an ACR image
and an MCR image, `fixtures/az-network-firewall-policy-rule-collection.json:38-44`
includes an explicit allow for `acrpaymentsprod01.azurecr.io`, and
`fixtures/kubectl-describe-pod.txt:52-53` shows that pull **succeeding** —
so "the registry is unreachable / we forgot ACR in the firewall" is a story
the evidence actively supports right up until you notice the successful pull
in the same events list. The ACR works *because* it has a rule; MCR fails
because it doesn't. The second distractor is the workload's own mesh: the
failing image is `oss/istio/proxyv2` (`fixtures/kubectl-describe-pod.txt:15`),
which invites an "istio sidecar is broken" detour. But the failure is a
TCP dial timeout to the registry at line 55 — the sidecar never runs, so
istio cannot be the cause of its own image being unpullable. Both decoys
lose to the firewall log: denied rows for exactly the FQDNs that time out,
allowed rows for exactly the traffic the symptom says works.

## Root cause

The AKS subnet's UDR routes all egress to Azure Firewall
(`fixtures/az-network-route-table-show.json:15-21`), but the firewall
policy's application rules were written without the `AzureKubernetesService`
FQDN tag or any `mcr.microsoft.com` / `*.data.mcr.microsoft.com` entries
(`fixtures/az-network-firewall-policy-rule-collection.json:9-48`). The
firewall denies MCR connections (`fixtures/azfw-application-rule-log.kql.csv:2-8`),
node image pulls time out (`fixtures/kubectl-describe-pod.txt:55`), pods
enter ImagePullBackOff, and nodes intermittently go NotReady.

## Fix

Add MCR to the firewall policy's application rules — preferably via the FQDN
tag that tracks Microsoft's published AKS requirements:

```bash
az network firewall policy rule-collection-group collection add \
  --resource-group rg-prod-01 \
  --policy-name azfw-policy-prod-01 \
  --collection-name AKSRequiredEgress \
  --priority 110 \
  --action Allow \
  --rule-name AllowAKSMCR \
  --type ApplicationRule \
  --target-fqdn-tags AzureKubernetesService \
  --protocols Https=443 \
  --source-addresses 10.20.0.0/16
```

(or add explicit ApplicationRules for `mcr.microsoft.com` and
`*.data.mcr.microsoft.com` alongside the existing collection). The change is
additive — it only widens what the firewall accepts — takes effect within
seconds to minutes, and the backoff loop retries pulls automatically; the
ACR and management-plane rules are untouched.

## Prevention

- **Egress policy as tested code:** keep the firewall policy in Terraform/
  Bicep, and add a CI check that fails whenever a cluster with
  `outboundType: userDefinedRouting` is applied without a rule collection
  containing the `AzureKubernetesService` FQDN tag — the exact gap in
  `fixtures/az-network-firewall-policy-rule-collection.json:9-48`.
- **Alert on firewall denials for cluster subnets:** a scheduled KQL query
  against `AzureDiagnostics | where Category endswith "ApplicationRule" and
  Action == "Deny"` scoped to AKS subnet sources — the
  `Deny, Default Action` rows in
  `fixtures/azfw-application-rule-log.kql.csv:2-8` existed from 08:02, before
  the first auto-repair page, and paging on them converts this from a
  NotReady incident into a pre-incident notification.

## Further reading

- [Control egress for AKS clusters — required FQDNs and the AzureKubernetesService tag](https://learn.microsoft.com/azure/aks/limit-egress-traffic)
- [Azure Firewall FQDN tags documentation](https://learn.microsoft.com/azure/firewall/fqdn-tags)
- [Azure Firewall logs and metrics (application rule log fields)](https://learn.microsoft.com/azure/firewall/log-analytics-samples)
