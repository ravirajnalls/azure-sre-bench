# aks-ops-026 — explainer

## How to investigate

Start from the failure itself, not from the workloads: the portal only says
"Provisioning failed", but the activity log records the real error.
`fixtures/aks-activity-log.json` shows the cluster upgrade being accepted at
23:41:07 and then two failed `Microsoft.Compute/virtualMachineScaleSets/write`
operations — at 00:04:33 (23 minutes in) and again at 00:26:19 after a retry —
each with `statusCode: BadRequest` and a `statusMessage` naming the actual
problem: `SubnetIsFull` — "Subnet 'app-subnet' (10.20.8.0/22) … has insufficient
free IP addresses for the requested virtual machines: 310 required, 118
available." That ~20-minute gap between an accepted upgrade and the first hard
failure is exactly the symptom window.

Now ask what wants 310 addresses. `fixtures/az-aks-nodepool-show.json`: app-pool
has `count: 30`, `maxPods: 30`, and `upgradeSettings.maxSurge: "33%"`. Thirty-three percent of 30 rounds up to 10 surge nodes, and under Azure CNI each surge node
reserves one NIC address plus one address per pod slot — 31 per node, 10 × 31 =
310. `fixtures/az-network-vnet-subnet-show.json` shows app-subnet is
10.20.8.0/22 (~1,019 usable addresses) and, critically, the nodepool fixture
points both `vnetSubnetId` and `podSubnetId` at that same subnet.
`fixtures/az-aks-show.json` confirms why pod addresses come from the subnet at
all: `networkPlugin: "azure"` with `networkPluginMode: null` and `podCidr:
null` — classic Azure CNI, not overlay. Only 118 addresses were free, so the
surge could never fit.

## Why the red herring misleads

The apps team had "already checked the pod disruption budgets", and the
narrative of an upgrade dying mid-flight invites a drain problem. But
`fixtures/kubectl-get-pdb.txt` shows every budget in every namespace with
allowed disruptions ≥ 1, and the capture itself notes that cordon/drain
completed for every node reached before the failure. Drains are not where the
upgrade stalls — node *creation* is. The workloads never got a chance to move,
because the replacement surge nodes never came into existence.

## Root cause

The surge budget of app-pool — `upgradeSettings.maxSurge: "33%"` on a 30-node,
`maxPods: 30` pool (`fixtures/az-aks-nodepool-show.json`) — requires 310 free
IP addresses (10 surge nodes × 31) from the Azure CNI pod subnet app-subnet
(`fixtures/az-network-vnet-subnet-show.json`), which has only 118 available.
VMSS creation for the surge nodes fails twice with `SubnetIsFull`
(`fixtures/aks-activity-log.json`), and AKS surfaces that as a generic
provisioning error roughly twenty minutes into every upgrade attempt.

## Fix

Either stop demanding 310 addresses at once, or create room for them:

1. **Lower the surge** — `az aks nodepool update --resource-group rg-prod
   --cluster-name prod-aks-eus1 --name app-pool --max-surge 1`. A fixed surge
   of 1 reserves only ~31 extra addresses and finishes a 30-node upgrade in
   more, smaller waves.
2. **Grow the subnet** — expand app-subnet's CIDR (e.g. to a /21) in the VNet
   while no overlapping allocation exists, so the 33% surge fits.
3. **Migrate the pool to Azure CNI Overlay** — pod IPs then come from a private
   pod CIDR and stop consuming VNet subnet addresses, which removes this entire
   failure class from future upgrades.

For tonight's change window, option 1 is the one-command path; options 2–3 are
the durable fixes.

## Prevention

Put the surge arithmetic in CI, where the human reviewer's "PDBs look fine"
check can never be: an IaC pre-flight that computes
`ceil(count × maxSurge) × (maxPods + 1)` for every node pool and asserts the
target subnet's free address count covers it before a pool manifest or cluster
update applies. Alongside it, alert on the subnet itself — an Azure Monitor
alert when available IPs in app-subnet drop below one surge increment
(`(maxPods + 1) × maxSurge`) turns a 3 a.m. upgrade failure into a daytime
capacity ticket. Pools sized this way also make a fixed small `maxSurge`
(1–3) the default for large pools, since percentage surges scale the IP budget
with the pool.

## Further reading

- [Upgrade options and recommendations for AKS clusters](https://learn.microsoft.com/en-us/azure/aks/upgrade-cluster)
- [Concepts — IP address planning in AKS](https://learn.microsoft.com/en-us/azure/aks/concepts-network-ip-address-planning)
- [Configure Azure CNI Overlay networking in AKS](https://learn.microsoft.com/en-us/azure/aks/azure-cni-overlay)
