# aks-identity-039 — explainer

## How to investigate

Start at the failing pull itself: `fixtures/kubectl-describe-pod.txt`. The
event is not a auth-secret problem — it is an HTTP 403 from the registry's
**data endpoint**:
`https://acrcontosoprod.eastus.data.azurecr.io/v2/shop/checkout-api/blobs/sha256:...`
with `403 Forbidden, public network access disabled for registry
acrcontosoprod`. Two facts fall out of that URL: image *authentication* works
(the pull got far enough to request a blob), and the layer download is going
through `...eastus.data.azurecr.io`, not the login server the symptom talks
about.

Now ask where that data endpoint resolves. `fixtures/nslookup-from-node.txt`
is the pivot: `acrcontosoprod.azurecr.io` resolves to private **10.224.0.10**
(the symptom's claim checks out), but
`acrcontosoprod.eastus.data.azurecr.io` resolves to **public 20.62.130.44**.
The login endpoint is on private link; the data endpoint is not.

Confirm the DNS zone is the reason: `fixtures/az-network-private-dns-record-set-list.json`
shows the `privatelink.azurecr.io` zone holds an A record for the login server
`acrcontosoprod` and **no record set for `acrcontosoprod.eastus.data`** — the
data endpoint simply was never added to the zone.

Then rule out the remaining suspects. `fixtures/az-acr-show.json`: the registry
has `publicNetworkAccess: Disabled` while `dataEndpointEnabled: true` — so
layer downloads *must* use the data endpoint, and the public path is hard-refused.
`fixtures/az-role-assignment-list-acr.json`: the pull identity holds `AcrPull`
on the registry, so RBAC is fine — and it must be, since authentication already
succeeded in step one.

## Why the red herring misleads

The symptom steers you at the registry's "access policy" — and
`fixtures/az-role-assignment-list-acr.json` looks like exactly the place where
a missing role assignment would hide. It is tempting to declare "someone
removed AcrPull" or "the kubelet identity lost access when public access was
disabled." But the describe event shows the pull reaching a blob URL and
getting an HTTP-level 403 *after* token authentication succeeded, and the role
list shows `AcrPull` is present. A 403 body that literally says "public network
access disabled" is a network-edge rejection, not an RBAC denial — an RBAC
failure would be a 401 from the token endpoint or a different denied message.
The kubelet identity never changed; the route the layer traffic takes did.

## Root cause

The `privatelink.azurecr.io` private DNS zone linked to the cluster VNet has
an A record for the registry's login endpoint
(`acrcontosoprod.azurecr.io` → 10.224.0.10) but is missing the A record for
the regional data endpoint `acrcontosoprod.eastus.data.azurecr.io`
(`fixtures/az-network-private-dns-record-set-list.json`). Nodes therefore
resolve the data endpoint to its public IP 20.62.130.44
(`fixtures/nslookup-from-node.txt`), and because the registry now has
`publicNetworkAccess: Disabled` with `dataEndpointEnabled: true`
(`fixtures/az-acr-show.json`), every blob/layer download hits the refused
public endpoint and fails with 403 (`fixtures/kubectl-describe-pod.txt`) —
even though login against the privately-resolved endpoint succeeds. That also
explains the timing: pods that pulled their image before the change still run;
only fresh pulls need the data endpoint.

## Fix

Add the missing A record for the regional data endpoint in the
`privatelink.azurecr.io` private DNS zone linked to the cluster VNet:

```bash
az network private-dns record-set a add-record \
  --resource-group rg-network-prod \
  --zone-name privatelink.azurecr.io \
  --record-set-name acrcontosoprod.eastus.data \
  --ipv4-address 10.224.0.10
```

Then restart the affected pods so the pulls retry
(`kubectl -n shop rollout restart deployment/checkout-api`). Safe and
reversible: adding a DNS record only reroutes traffic through the private
endpoint, it grants nothing new; remove the record to revert. On registries
with the dedicated data-endpoint feature, Azure's documented
`Privatelink.[region].azurecr.io` data-zone records achieve the same result.
The registry-side access policy and role assignments need no changes — they
were never wrong.

## Prevention

When you disable public network access on an ACR that uses regional data
endpoints, treat the private DNS zone as part of the change: enumerate every
FQDN the registry serves (login server plus `<region>.data` and any replication
endpoints) and verify each resolves to a private IP from a cluster node —
a one-line `nslookup` per FQDN in the runbook. Better, automate it: a
connectivity test that pulls a scratch image from inside the cluster after any
registry-network change catches a half-covered zone before it breaks a deploy.
Verify-from-the-node, not from the laptop — the symptom here was only visible
from the VNet's DNS view.

## Further reading

- [Azure Container Registry networking — private link and data endpoints](https://learn.microsoft.com/azure/container-registry/container-registry-private-link)
- [Configure the regional data endpoint for Azure Container Registry](https://learn.microsoft.com/azure/container-registry/container-registry-data-endpoint)
- [Azure private DNS zone records — privatelink.azurecr.io](https://learn.microsoft.com/azure/container-registry/container-registry-private-link#dns-configuration-options)
- [Troubleshoot ACR image pull failures on AKS](https://learn.microsoft.com/troubleshoot/azure/azure-kubernetes/aks/aks-should-pull-image-from-acr)
