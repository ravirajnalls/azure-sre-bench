# aks-networking-038 — explainer

## How to investigate

The error message in `fixtures/app.log` is the whole story if you read it
precisely: `error code 40615` — "Client with IP address '10.240.2.15' is not
allowed to access the server." That rejection comes from the SQL server's own
access controls (its firewall and virtual network rules), not from a network
path problem: the connection **reached** the server, and the server refused
that client IP. A routing or NSG failure would look like a connect timeout with
no server-side verdict at all.

Connect that IP to the infrastructure. `fixtures/kubectl-get-pods-wide.txt`
shows the failing pool2 pods sitting at `10.240.2.x` addresses — `10.240.2.15`
is exactly the rejected client IP — while the working pool1 pods live at
`10.240.1.x`. Then `fixtures/az-aks-nodepool-list.json` maps the pools to
subnets: `nodepool1` → `snet-aks-pool1`, `pool2` → `snet-aks-pool2`. "Same
pods, same image, same connection string, works on pool1 and fails on pool2"
translates to: **authorized from one subnet, unauthorized from the other.**

Check the server's allowlist: `fixtures/az-sql-server-vnet-rule-list.json` —
`sql-ledger` has exactly one virtual network rule, `allow-snet-aks-pool1`,
pointing at `snet-aks-pool1`. The pool2 subnet was never added. And check the
prerequisite on the subnet side, because a SQL virtual network rule requires a
`Microsoft.Sql` service endpoint on the subnet it authorizes:
`fixtures/az-network-vnet-subnet-show-pool2.json` shows
`"serviceEndpoints": []` — the brand-new subnet has no service endpoints at
all, so the fix is two steps, not one.

## Why the red herring misleads

The NSG is the trap. `fixtures/nsg-rules.json` shows `nsg-aks-spoke` — attached
to **both** subnets — with `allow-outbound-sql` (TCP 1433 to `Sql.Eastus`)
among its allow rules, which matches the symptom's "Azure's NSG audit came back
clean." Everything the NSG says is *true*, and it is still irrelevant: an NSG
governs what traffic is allowed to **leave** the subnet. Error 40615 means the
traffic left, traversed Azure's backbone, and was rejected by the server's
access control. "NSG permissive" and "server refusing" are both true at once,
and only one of them is a fault. "Nobody has touched the firewall in weeks" is
consistent too — nothing on the server changed yesterday; the *new subnet*
simply arrived without being granted access. That asymmetry — an allowlist that
predates a new entrant — is the shape of almost every "added infrastructure,
now the old path's authorization is missing" failure.

## Root cause

pool2's pods run in the new subnet `snet-aks-pool2` (10.240.2.0/24)
(`fixtures/az-aks-nodepool-list.json`), but the `sql-ledger` server's virtual
network rules cover only `snet-aks-pool1` — the single rule
`allow-snet-aks-pool1` (`fixtures/az-sql-server-vnet-rule-list.json`) — and
`snet-aks-pool2` has no `Microsoft.Sql` service endpoint
(`fixtures/az-network-vnet-subnet-show-pool2.json`). Connections from pool2 pod
IPs such as `10.240.2.15` therefore reach the server's access controls and are
rejected with error 40615 (`fixtures/app.log`), while pool1 pods (10.240.1.x)
still match the existing allow rule.

## Fix

Enable the service endpoint on the pool2 subnet, then add the matching virtual
network rule — in that order:

```bash
# 1. Give snet-aks-pool2 the Microsoft.Sql service endpoint
az network vnet subnet update \
  -g rg-aks-prod --vnet-name vnet-aks-spoke \
  -n snet-aks-pool2 \
  --service-endpoints Microsoft.Sql

# 2. Authorize the subnet on the sql-ledger server
az sql server vnet-rule create \
  -g rg-sql-ledger -s sql-ledger \
  -n allow-snet-aks-pool2 \
  --vnet-name vnet-aks-spoke \
  --subnet snet-aks-pool2
```

The order matters: with no `Microsoft.Sql` endpoint on `snet-aks-pool2`,
creating the vnet rule (with `ignoreMissingVnetServiceEndpoint: false`, the
safe default — and what the existing `allow-snet-aks-pool1` rule uses) would
fail its precondition. Note that enabling a service endpoint on a subnet can
briefly interrupt existing connections from that subnet's nodes, so schedule it
in a low-traffic window. Once the rule lands, running pods recover without
restarting. An alternative worth considering at the platform level: put
`sql-ledger` behind a **private endpoint** in `vnet-aks-spoke` and authorize by
VNet connectivity — then future node pools and subnets inherit access instead
of each one needing a new per-subnet rule.

## Prevention

1. **Make per-subnet authorizations part of the "add a node pool" change
   template.** Anything that grants access by subnet — SQL virtual network
   rules, storage firewalls, Key Vault network ACLs, Redis — must list the new
   subnet in the same change window that creates the pool. A Terraform/module
   shape that declares node pools and their subnet authorizations together
   makes the omission impossible to merge rather than easy to forget.
2. **Prefer VNet-wide authorization when more pools are expected.** A private
   endpoint reachable from `vnet-aks-spoke` authorizes by connectivity, not
   subnet membership — pool3, pool4, and every future pool stop being
   individual grant opportunities.
3. **Alert on error 40615 in application logs.** It is always an
   authorization gap after a topology change, it identifies the exact client IP
   and server in the message, and it pages before "every request times out"
   becomes the user report.

## Further reading

- [Azure SQL Database — virtual network rules and service endpoints](https://learn.microsoft.com/azure/azure-sql/database/vnet-rules)
- [Virtual network service endpoints](https://learn.microsoft.com/azure/virtual-network/virtual-network-service-endpoints)
- [Azure SQL Database — private endpoint (alternative approach)](https://learn.microsoft.com/azure/azure-sql/database/private-endpoint-overview)
