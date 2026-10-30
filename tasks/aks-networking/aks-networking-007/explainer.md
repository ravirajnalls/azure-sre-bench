# aks-networking-007 — explainer

## How to investigate

The symptom splits the world in two — everything works from inside the AKS
VNet, `kubectl` fails from the hub — so start with the *shape* of the failure in
`fixtures/kubectl-error.txt`. Every command dies with `lookup
00000000-0000-0000-0000-000000000006.hcp.eastus.azmk8s.io on 10.20.0.4:53: no
such host`. That is not a connection refused or a timeout: kubectl never got far
enough to open a connection. The hub's DNS resolver has **no answer** for the
cluster's private API FQDN.

Confirm the resolver itself is healthy with
`fixtures/nslookup-from-jumpbox.txt`: the private FQDN returns NXDOMAIN while
`management.azure.com` resolves normally through the same resolver
(`10.20.0.4`). The resolver works — it just has no source of records for the
`azmk8s.io` name.

Now establish what the cluster is: `fixtures/az-aks-show.json` shows
`enablePrivateCluster: true` with `enablePrivateClusterPublicFqdn: false` and
`privateFqdn: 00000000-0000-0000-0000-000000000006.hcp.eastus.azmk8s.io`. A
private cluster publishes that FQDN as an A record in a **private DNS zone**
(`privatelink.eastus.azmk8s.io` for the `system` zone mode shown in
`privateDnsZone: "system"`). So the question becomes: which VNets are linked to
that zone? `fixtures/az-network-private-dns-link-list.json` answers it — the
zone has exactly **one** virtual network link, `vnet-aks-spoke-link`, pointing
at `vnet-aks-spoke`. Nothing links `vnet-hub`, so the hub resolver can never see
the zone's records. Peering is irrelevant to that: it carries traffic, not name
resolution.

## Why the red herring misleads

"After last night's network change window" plus a peering fixture begs you to
blame the peering — and `fixtures/az-network-vnet-peering-list.json` shows
`peer-aks-spoke-to-hub` as `Connected` and `FullyInSync`, which **rules the
peering out**, not in: routing is fine. The failure mode also proves it. A
routing or firewall problem produces timeouts (`i/o timeout`) or resets, not
`no such host` — nothing in the symptom mentions latency or flakiness from
inside the spoke, and traffic from the hub could not be "partially" failing DNS.
The second trap is the `resolv.conf` captured in `fixtures/kubectl-error.txt`
(`search internal.cloudapp.net`, `nameserver 10.20.0.4`) — it looks suspicious,
but the same resolver resolves `management.azure.com` fine in
`fixtures/nslookup-from-jumpbox.txt`. The resolver is healthy; it is missing a
zone link, not configuration.

## Root cause

The private DNS zone `privatelink.eastus.azmk8s.io` that backs the cluster's
private API FQDN is linked **only** to `vnet-aks-spoke`
(`fixtures/az-network-private-dns-link-list.json`), while the jumpbox resolves
through the hub DNS at `10.20.0.4` in `vnet-hub`
(`fixtures/kubectl-error.txt`, with the private FQDN from
`fixtures/az-aks-show.json`). Without a virtual network link to `vnet-hub`, the
hub resolver returns NXDOMAIN for the API name, and every `kubectl` invocation
from the jumpbox fails before any connection is attempted.

## Fix

Create a virtual network link from the zone to the hub:

```bash
az network private-dns link vnet create \
  --resource-group rg-aks-prod \
  --zone-name privatelink.eastus.azmk8s.io \
  --name vnet-hub-link \
  --virtual-network \
    /subscriptions/00000000-0000-0000-0000-000000000002/resourceGroups/rg-hub/providers/Microsoft.Network/virtualNetworks/vnet-hub \
  --registration-enabled false
```

(`--registration-enabled false` — the hub only needs to *resolve* the zone, not
auto-register its own VM records into it.) Verify from the jumpbox: `nslookup
00000000-0000-0000-0000-000000000006.hcp.eastus.azmk8s.io 10.20.0.4` returns the
zone's A record, and `kubectl version` connects. The link takes effect within
minutes, and it is additive and reversible (`az network private-dns link vnet
delete`) — no cluster or peering state changes. If the hub VNet uses custom DNS
servers instead of Azure-provided resolution, the equivalent fix is a
conditional forwarder for `azmk8s.io` pointed at a resolver that *is* linked to
the zone.

## Prevention

1. **Make zone-link coverage part of the network change checklist.** Before
   closing any hub/spoke change window, query Private DNS zones' virtual
   network links (Azure Resource Graph or a small `az network private-dns link
   vnet list` loop) and assert every zone backing shared services — private
   clusters, SQL, storage — lists a link for every VNet that needs to resolve
   it. Last night's change added or touched hub networking without re-checking
   zone links; the checklist item makes that gap mechanical rather than
   tribal.
2. **Add a hub-side smoke test to the post-change runbook.** `kubectl version`
   from the jumpbox fails in seconds on DNS breakage; the current runbook only
   verified connectivity from inside the spoke, where resolution always works.

## Further reading

- [Create and use a private AKS cluster — hub and spoke DNS considerations](https://learn.microsoft.com/azure/aks/private-clusters)
- [Azure Private DNS — virtual network links](https://learn.microsoft.com/azure/dns/private-dns-virtual-network-links)
