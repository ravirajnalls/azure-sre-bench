# networking-074 — explainer

> **Spoiler for humans** — this file is the Learn-page write-up. Agents never see it: the runner keeps it out of the workspace like `expected.json`.

## How to investigate

1. `pr.diff` — the change has two real parts: a new route table
   (`rt-payments-egress-lockdown`) whose single route sends `0.0.0.0/0` to
   `VirtualAppliance 10.0.100.4`, and a `subnet_route_table_association`
   binding it to the cluster subnet. A variable rename rides along.
2. `network.tf` — confirm the route is the classic "force-tunnel everything"
   pattern: `address_prefix = "0.0.0.0/0"`, `next_hop_type = "VirtualAppliance"`,
   `next_hop_in_ip_address = var.firewall_private_ip`, and the association
   targets `snet-payments-aks` (the data source pins it).
3. `firewall.tf` — the destination firewall `af-payments-egress` allow-lists
   only internal tooling FQDNs (`time.windows.com`,
   `*.internal.contoso.com`) and corp RFC1918 ranges. There is no
   `Azure AKS FQDN tag` application rule: nothing for `mcr.microsoft.com`
   (image pulls), AzureMonitor, or the AAD/login endpoints the node and
   control plane need. The firewall's default deny drops everything else.
4. `az-aks-show.json` — `networkProfile.outboundType: "loadBalancer"` with
   `networkPlugin: "azure"`: the cluster was built expecting the default
   system route (via the load balancer), not a UDR default route to a
   firewall. The moment the association applies, node egress (image pulls,
   tunnel health, telemetry) is forced to the firewall and fails.

## Why the red herring misleads

The variable rename (`aks_subnets` → `cluster_subnets`) is the loudest
non-functional change in the diff and the easiest thing to flag — but it only
changes a variable's name and default's shape; nothing consumes the old name
in the shown files, and renaming a variable cannot affect routing. The real
hazard is the route the PR quietly adds.

## Root cause

The default route (`0.0.0.0/0 -> VirtualAppliance 10.0.100.4`,
network.tf line 43) plus its subnet association captures all cluster egress
into `af-payments-egress`, whose rules cover internal tooling only — with the
cluster on `outboundType: loadBalancer` there is no AKS FQDN allow-list, so
image pulls (mcr.microsoft.com) and control-plane/monitoring callbacks are
dropped and nodes go NotReady once the route applies.

## Fix

Before merging, give the firewall an application rule collection with the
`Azure AKS FQDN tag` scoped to the cluster subnet's source range (or switch
the cluster to `outboundType: userDefinedRouting` as part of the same
lockdown), and only then apply the route-table association. The variable
rename can ship independently.

## Prevention

Pair every `0.0.0.0/0` UDR on an AKS subnet with a required FQDN-tag review:
add a policy/plan check ("route table touching an AKS subnet must reference a
firewall with the Azure AKS FQDN tag") to the pipeline, and treat
`outboundType` as part of the change's blast radius — lockdown PRs must state
which outbound type they assume.

## Further reading

- AKS egress overview and outbound types:
  https://learn.microsoft.com/azure/aks/egress
- Use Azure Firewall to protect AKS egress (FQDN tag):
  https://learn.microsoft.com/azure/architecture/reference-architectures/containers/aks/aks-egress
- Azure Firewall FQDN tags:
  https://learn.microsoft.com/azure/firewall/fqdn-tags
