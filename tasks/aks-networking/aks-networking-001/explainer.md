# aks-networking-001 — explainer

## How to investigate

Start with the widest signal: what kind of names fail, and what kind succeed.
`fixtures/nslookup-from-pod.txt:5` shows `api.stripe.com` timing out from
inside a pod, while `fixtures/nslookup-from-pod.txt:17-18` shows
`orders-api.shop.svc.cluster.local` resolving to a cluster IP from the same
pod. That split — external names dead, in-cluster names healthy — points at
the upstream resolver path, not at the cluster DNS namespace or kube-dns
service itself.

Next read the CoreDNS logs, because they name the upstream every failed query
went to: `fixtures/kubectl-logs-coredns.txt:5` shows
`read udp 10.244.0.121:44318->10.20.1.53:53: i/o timeout` for
`api.stripe.com`, and the same timeout to `10.20.1.53` repeats for
`graph.microsoft.com`, `shopacr00000001.azurecr.io`, and
`login.microsoftonline.com` through line 16. Every external lookup is being
forwarded to one unreachable on-prem address, `10.20.1.53`. (In-cluster names
in lines 8 and 13 answer instantly from the managed zone, consistent with the
nslookup split.)

Now find who tells CoreDNS to do that: `fixtures/kubectl-get-cm-coredns-custom.yaml:21-29`
is the `.:53` server block in the `coredns-custom` ConfigMap, and line 24 is
the smoking gun — `forward . 10.20.1.53` sends **every** query for the root
zone to the on-prem DNS server. The comment on line 19 explains it was added
for a legacy ERP integration, but a `.` server block is a catch-all: it
overrides resolution for the entire internet, not just ERP zones. Lines 27–28
(`reload`, `loadbalance`) confirm the block is live and was picked up — the
log in `fixtures/kubectl-logs-coredns.txt:3` shows CoreDNS reloaded a new
configuration SHA at 08:59:12, matching the "since around 09:00" in the
symptom.

## Why the red herring misleads

Two fixtures tempt you away from the ConfigMap. First, "the VNet's custom DNS
servers were changed" is the classic Azure DNS story, and
`fixtures/az-network-vnet-show.json:7-9` is where you'd check it — but
`dhcpOptions.dnsServers` is empty, meaning the VNet uses Azure-provided DNS,
and the nodes confirmed healthy over SSH. Second, "CoreDNS itself is broken"
feels plausible when everything is `Running` — and
`fixtures/kubectl-get-pods-kube-system.txt:4-5` shows both CoreDNS pods
healthy with zero restarts. Both red herrings are ruled out by the same fact:
the failures are selective (external only) and the CoreDNS log names a
specific dead upstream, which is behavior of a working CoreDNS faithfully
following a bad configuration, not a sick one.

## Root cause

The `coredns-custom` ConfigMap in kube-system contains a `.:53` server block
that forwards all queries to the on-prem DNS server `10.20.1.53`
(`fixtures/kubectl-get-cm-coredns-custom.yaml:21-24`), which stopped
responding. CoreDNS — having reloaded that config at 08:59
(`fixtures/kubectl-logs-coredns.txt:3`) — forwards every external lookup to
the dead server and times out (`fixtures/kubectl-logs-coredns.txt:5-16`),
while in-cluster names still resolve because the managed Kubernetes zone is
served before the catch-all block runs.

## Fix

Remove the `.:53` server block from the `coredns-custom` ConfigMap (or change
line 24 to a working upstream, or forward only the ERP zone instead of `.`):

```bash
kubectl edit cm coredns-custom -n kube-system
# delete the .:53 server block (lines 21–29 of the Corefile)
```

CoreDNS picks up the change automatically (`reload` is in the block, and AKS
reloads the corefile on ConfigMap change). If the ERP integration genuinely
needs on-prem names, replace the catch-all with a scoped zone:

```
erp.local:53 {
    forward . 10.20.1.53
}
```

This is safe and reversible: removing the block restores the Azure-provided
resolver (168.63.129.16) for external names and touches nothing else.

## Prevention

- **Change-review guardrail:** a CI/policy check that reads the
  `coredns-custom` ConfigMap (or its Helm/IaC source) and rejects any
  `forward .` (root-zone forward) — root forwards in custom Corefiles should
  be zone-scoped, e.g. `forward erp.local .`. The change that broke prod
  (CHG0001432, per the ConfigMap annotation) shipped a root forward no one
  would approve deliberately.
- **Alert on upstream health:** a synthetic check that resolves a known
  public name through the cluster DNS every minute and alerts on failure —
  this existed as `egress-dns-failure` here, but a matching probe *against
  the configured upstream* (dig @10.20.1.53) would catch the dead server
  before pods notice.

## Further reading

- [Custom CoreDNS configuration on AKS](https://learn.microsoft.com/azure/aks/coredns-custom)
- [Kubernetes DNS for Services and Pods (cluster resolution order)](https://kubernetes.io/docs/concepts/services-networking/dns-pod-service/)
- [Name resolution failure — Troubleshoot AKS](https://learn.microsoft.com/troubleshoot/azure/azure-kubernetes/connectivity/troubleshoot-name-resolution-failures)
