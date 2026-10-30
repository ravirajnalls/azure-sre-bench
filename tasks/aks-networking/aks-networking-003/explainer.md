# aks-networking-003 — explainer

## How to investigate

Start with the pod that is stuck, because `kubectl describe` on a
`ContainerCreating` pod records the exact failure:
`fixtures/kubectl-describe-pod.txt:48` is the decisive line —
`FailedCreatePodSandBox ... failed to setup network for sandbox ... add cidr:
ip allocation failed: azure-cni: unable to allocate an IP address from subnet
"snet-aks-nodes": all IP addresses in the subnet are allocated`. Everything
you need is in that one event: the plugin (azure-cni), the subnet
(`snet-aks-nodes`), and the condition (all addresses allocated). Note line 47:
the image pull **succeeded**, so this is not a registry or kubelet problem.

Confirm the numbers behind "all IP addresses ... allocated". The subnet is a
`/24` — `fixtures/az-network-vnet-subnet-show.json:2` shows
`addressPrefix: 10.224.0.0/24` and line 4 reports `ipConfigurationsCount: 251`
— every usable address is taken. The demand side is in
`fixtures/az-aks-show.json:11` (`maxPods: 110`) and lines 36–39
(`networkPlugin: azure`, no overlay mode): with Azure CNI (no overlay) each
node pre-allocates maxPods+1 = 111 pod IPs, and
`fixtures/az-aks-show.json:20` pins the node pool to `snet-aks-nodes`
(`fixtures/kubectl-get-nodes.txt:2-4`, node IPs 10.224.0.4–.6 live in it).
3 nodes × 111 = 333 addresses demanded against 251 usable — the subnet was
exhausted long before this scale-out; the 2→6 replica bump and the new
checkout LoadBalancer were just the first requests with nothing left to take.

The new LoadBalancer's `<pending>` EXTERNAL-IP is the same shortage showing
up in a second place: `fixtures/kubectl-get-svc.txt:3` shows `checkout` with
`EXTERNAL-IP <pending>` — a standard-SKU LoadBalancer frontend also needs an
IP from the same depleted address space.

## Why the red herring misleads

The symptom pairs "new pods stuck" with "new LoadBalancer has no external
IP", and the natural reading is that the LoadBalancer is the problem — maybe
the standard load balancer failed to allocate a frontend IP, or the cloud
controller is wedged. `fixtures/kubectl-get-svc.txt:5` feeds that: the
long-running `partner-api` LoadBalancer is serving fine on
`203.0.113.42`, which tempts a comparison of working vs broken LBs. But
`partner-api` already holds its IP from 42 days ago; nothing about it proves
the LB path works *for new allocations*. The second distractor is the Service
CIDR: `fixtures/az-aks-show.json:40` shows `serviceCidr: 10.240.0.0/16`, and
Service CIDR exhaustion also presents as "no ClusterIP". But `checkout` has a
ClusterIP (`10.240.12.87`, `fixtures/kubectl-get-svc.txt:3`) — it got a
service address and is waiting on an *external* one, so the /16 is not the
binding constraint. The subnet exhaustion in the pod event explains both
symptoms with one cause; the distractors each explain only half of one.

## Root cause

The node subnet `snet-aks-nodes` (10.224.0.0/24, 251 usable addresses) is
exhausted. The cluster runs Azure CNI without overlay
(`fixtures/az-aks-show.json:36-39`) with `maxPods: 110`
(`fixtures/az-aks-show.json:11`), so each of the three nodes pre-allocates
111 pod IPs — 333 total against a /24's 251 usable
(`fixtures/az-network-vnet-subnet-show.json:2-4`). New pod sandboxes fail IP
allocation (`fixtures/kubectl-describe-pod.txt:48`) and the new LoadBalancer
frontend cannot get an address (`fixtures/kubectl-get-svc.txt:3`).

## Fix

Relieve the address space — pick one, in order of disruption:

- **Move the node pool to a larger subnet** (e.g. a /22) or a new node pool
  subnet — re-create the pool (`az aks nodepool add --mode System ...` on the
  new subnet, drain, delete the old pool). Most work, but also fixes the
  growth headroom problem permanently.
- **Switch to Azure CNI overlay**: pod IPs come from a separate overlay
  CIDR, freeing the node subnet entirely. Requires cluster/networking
  prerequisites and a maintenance window.
- **Reduce `maxPods`** on the pool so `nodes × (maxPods+1)` fits the
  existing /24 with headroom (e.g. maxPods 60 → 3×61 = 183 < 251). Fastest
  mitigation; it is a node-pool re-create on AKS, so schedule it like the
  first option.

Either way, this is safe to do in a maintenance window: existing pods keep
their IPs, and the pending sandboxes and LoadBalancer recover as soon as
addresses free up.

## Prevention

- **Capacity alert on subnet utilization:** an Azure Monitor metric alert on
  `IPConfigurations` count (or a scheduled `az network vnet subnet show`
  check in CI) per AKS node subnet, warning at 80% and paging at 95%. A
  subnet running Azure CNI at 251/251 did not get there this morning — it
  crept.
- **IaC guardrail:** the Terraform/Bicep module that creates the AKS subnet
  should compute usable addresses and fail plan-time when
  `nodeCountMax × (maxPods+1)` exceeds them. Here `maxCount: 6`
  (`fixtures/az-aks-show.json:10`) with maxPods 110 demanded 666 addresses
  from a /24 — the plan should have refused to apply it.

## Further reading

- [Azure CNI networking — IP address planning for AKS](https://learn.microsoft.com/azure/aks/concepts-network-aks-services)
- [Azure CNI Overlay networking](https://learn.microsoft.com/azure/aks/azure-cni-overlay)
- [Troubleshoot IP address exhaustion — Azure Kubernetes Service](https://learn.microsoft.com/troubleshoot/azure/azure-kubernetes/connectivity/ip-address-exhaustion)
