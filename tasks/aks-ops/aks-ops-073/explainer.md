# aks-ops-073 — explainer

> **Spoiler for humans** — this file is the Learn-page write-up. Agents never see it: the runner keeps it out of the workspace like `expected.json`.

## How to investigate

1. `pr.diff` — the proposed change has three hunks: a Kubernetes version bump
   (1.30.4 → 1.30.7), a cost-center tag update (4470 → 4471), and
   `upgradeSettings.maxSurge` on `npworkload` going from `'33%'` to `'100%'`.
   A review asks: which of these can take the cluster down?
2. `az-aks-nodepool-list.json` — the proposed state snapshot: `npworkload`
   runs 20 nodes with `maxPods: 30`, Azure CNI (`networkPlugin: "azure"`), and
   `upgradeSettings.maxSurge: "100%"`. The system pool (`npsystem`, 3 nodes,
   `maxSurge: "33%"`) is the contrast — surge settings are per-pool.
3. `az-network-vnet-subnet-show.json` — `snet-orders-workload` is
   `10.240.16.0/25`: 128 addresses, 5 reserved by Azure, 123 usable. The
   20 node NICs hold `.4`–`.23`; the capacity report says roughly 60
   addresses are free after pod IP allocations.
4. `aks.bicep` — confirm the diff is the whole story: the pool's subnet and
   maxPods are unchanged; only the surge multiplier changes. With maxSurge at
   100%, the upgrade creates ~20 additional nodes, each needing 1 node IP and
   up to 30 pod IPs (Azure CNI allocates pod IPs from the same subnet) — about
   600 addresses against ~60 free.

## Why the red herring misleads

The version bump is the loudest change and the usual upgrade suspect — but
1.30.4 → 1.30.7 is a patch bump on a pool whose `orchestratorVersion` already
shows 1.30.7 as the target, and nothing in the snapshots (API versions,
`upgradeSettings` on other pools) suggests an API incompatibility. The
cost-center tag change touches billing metadata only. Neither affects address
allocation; both are healthy changes.

## Root cause

`maxSurge: '100%'` (aks.bicep line 36) on a 20-node pool needs ~600 pod IPs
(20 surge nodes x `maxPods` 30) plus 20 node IPs from `snet-orders-workload`
during the drain, and the /25 subnet has only ~60 free addresses — the surge
nodes will fail IP allocation and the upgrade will stall mid-rollout with the
pool partially drained.

## Fix

Before the window, either set `maxSurge` to a value the subnet can host
(`'10%'` — two surge nodes, ~62 addresses) or extend the subnet's address
space (or move the pool to a larger subnet). Then merge; the version bump and
tag change are safe to keep.

## Prevention

Gate cluster-maintenance PRs on a subnet capacity check: the surge node count
times (1 + maxPods) must fit in the pool subnet's free addresses. Add the
check to the platform pipeline (or a Bicep/PSRule lint rule) so a surge
setting can never exceed subnet headroom again, and keep per-pool surge
defaults conservative (`33%`) on Azure CNI with tight subnets.

## Further reading

- AKS upgrade options — max surge:
  https://learn.microsoft.com/azure/aks/upgrade-cluster#customize-node-surge-upgrade
- Azure CNI IP address planning:
  https://learn.microsoft.com/azure/aks/concepts-network-ip-address-planning
- Azure subnet address reservation (5 addresses per subnet):
  https://learn.microsoft.com/azure/virtual-network/virtual-networks-faq#are-there-any-restrictions-on-using-ip-addresses-within-these-subnets
