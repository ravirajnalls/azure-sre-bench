# networking-026 — explainer

## How to investigate

Start with what the app itself says: `fixtures/app-portal.log`. SQL
connections fail instantly with `SqlException … Error: 47073` — "Server
'sql-contoso-prod' has public network access set to 'Disabled'. Connections
over the public endpoint are not permitted. Use a private endpoint to
connect." That error text is unusually generous: the server received the
connection attempt and rejected it because it arrived over the **public**
endpoint. So the app is reaching SQL, but not through the private path.
Static pages and `/healthz` keep returning 200 because nothing but the
database is affected.

Now prove where the hostname resolves from inside the app's network:
`fixtures/nslookup-from-kudu.txt` was run from app-portal's own Kudu
console. `sql-contoso-prod.database.windows.net` resolves to **20.42.65.92**
— a public IP — via the Azure public resolver `168.63.129.16`. The control
query in the same session (`app-portal.azurewebsites.net`) resolves fine,
so DNS itself works; it is this one name that has no private answer. A
private-path setup would resolve to the private endpoint's IP, not a
public one.

Check the private endpoint is real and healthy before blaming DNS:
`fixtures/az-network-private-endpoint-show.json` shows `pe-sql-contoso-prod`
provisioned, connection **Approved**, with private IP `10.60.1.5` in
`snet-pe` on vnet-sql-prod — and its `customDnsConfigs` even records that
`sql-contoso-prod.database.windows.net` should map to `10.60.1.5`. The
server side of private access is done.

So why doesn't the app's VNet resolve that name privately? DNS zones only
answer VNets linked to them: `fixtures/az-network-private-dns-link-list.json`
lists the virtual network links on `privatelink.database.windows.net` and
shows exactly one — `link-sql-vnet` to `vnet-sql-prod`. There is **no link
for `vnet-app-prod`**, the VNet app-portal is integrated into. The app's
resolver falls back to public DNS, gets the public IP, and SQL (correctly)
refuses the public connection.

Finally, dismiss the routing theory: `fixtures/az-webapp-vnet-integration.json`
shows app-portal's integration to `snet-app-integration` on vnet-app-prod
is configured with `resyncRequired: false` — the network team is right,
routing/integration is healthy. The missing piece was never routing; it is
name resolution inside the app's VNet.

## Why the red herring misleads

"VNet integration shows healthy, so routing was verified" is stated as fact
in the handoff — and it is true, which is what makes it misleading.
`fixtures/az-webapp-vnet-integration.json` confirms the integration subnet
is configured and not pending resync, but VNet integration moves **traffic**;
it does nothing about **DNS**. The private endpoint
(`fixtures/az-network-private-endpoint-show.json`) is equally healthy and
equally insufficient on its own: a private endpoint only helps callers who
resolve the server's FQDN to its private IP, and that mapping exists only
in the `privatelink.database.windows.net` zone — linked here to
vnet-sql-prod only (`fixtures/az-network-private-dns-link-list.json`).
The evidence even invites you to conclude "the network team already checked
the network," when the actual gap sits one layer higher, in name
resolution.

## Root cause

The `privatelink.database.windows.net` private DNS zone has no virtual
network link for `vnet-app-prod`, the App Service's integration VNet
(`fixtures/az-network-private-dns-link-list.json`: the only link targets
`vnet-sql-prod`). So `sql-contoso-prod.database.windows.net` resolves from
app-portal to its public IP 20.42.65.92 (`fixtures/nslookup-from-kudu.txt`)
instead of the private endpoint IP 10.60.1.5
(`fixtures/az-network-private-endpoint-show.json`), and once public network
access was disabled on sql-contoso-prod, those public-endpoint connections
are rejected with error 47073 (`fixtures/app-portal.log`).

## Fix

Link the app VNet to the private DNS zone:

```bash
az network private-dns link vnet create \
  --resource-group rg-sql-prod \
  --zone-name privatelink.database.windows.net \
  --name link-app-vnet \
  --virtual-network /subscriptions/00000000-0000-0000-0000-000000000003/resourceGroups/rg-app-prod/providers/Microsoft.Network/virtualNetworks/vnet-app-prod \
  --registration-enabled false
```

Safe and effectively instant: adding a VNet link is a control-plane change
with no downtime; the app's next DNS lookup resolves to 10.60.1.5 and
connections succeed (an app restart flushes cached lookups faster). No
firewall, endpoint, or SQL configuration changes are needed — they were
already correct.

## Prevention

Make "every VNet that must reach a private resource is linked to its
private DNS zone" a checked invariant, not a hope:

1. **IaC pairing rule.** Wherever the private endpoint is declared, declare
   the DNS zone links in the same module and parameterize the list of
   consuming VNets — and add a policy/CI check (e.g. `az network private-dns
   link vnet list` diffed in a nightly job) that fails when a VNet containing
   resources whose connection strings target `*.database.windows.net` has
   no link on the matching `privatelink.*` zone. This incident is exactly
   what that diff surfaces.
2. **Post-hardening smoke test.** Any change that disables public network
   access on a shared data service should gate on a connectivity probe from
   each consumer VNet (a Kudu `nslookup` + `sqlcmd` check, automated) — the
   same nslookup in `fixtures/nslookup-from-kudu.txt`, run before the
   hardening change closes, would have shown the public-IP resolution
   immediately.
3. **Alert on the rejection signature.** A log alert on `SqlException`
   error 47073 / "public network access set to 'Disabled'" pages the
   private-DNS gap with its cause named in the first minute, instead of
   routing through a generic db-connectivity page.

## Further reading

- [Azure Private Endpoint DNS configuration (privatelink zones and VNet links)](https://learn.microsoft.com/azure/private-link/private-endpoint-dns)
- [Integrate your App Service with a virtual network](https://learn.microsoft.com/azure/app-service/overview-vnet-integration)
- [Connect privately to Azure SQL Database using a private endpoint](https://learn.microsoft.com/azure/azure-sql/database/private-endpoint-overview)
