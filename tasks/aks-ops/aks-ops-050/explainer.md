# aks-ops-050 — explainer

## How to investigate

Start with the exact verb that fails, because the error text points at two
different layers. `fixtures/kubectl-error.txt:7` shows `kubectl logs` dying
with `dial tcp 10.240.0.35:10250: i/o timeout` — a kubelet-port address —
yet `fixtures/kubectl-error.txt:2-4` shows `kubectl get` returning instantly.
The API server accepts your requests; only the verbs that require the API
server to open a stream *through* the cluster's tunnel fail. That asymmetry
says the API server cannot reach the kubelets, and on a modern AKS cluster
that stream is the konnectivity tunnel.

Check the tunnel itself next: `fixtures/kubectl-get-pods-kube-system.txt:6-7`
shows both `konnectivity-agent` pods in CrashLoopBackOff with dozens of
restarts, while every other kube-system pod is Running. Read the agent's own
log — `fixtures/konnectivity-agent.log:1-2` shows it dialing the API server
endpoint `retail-prod-06ab0cfb.hcp.eastus2.azmk8s.io:443` and timing out on
its **own egress connection**, and line 5 shows it giving up with "all
retries exhausted". The agents are not crashing on a bug; they cannot get
out.

Ask what stands between the node and the internet. The firewall log answers
it: `fixtures/azfw-application-rule-log.kql.csv:2` shows repeated **Deny**
hits from the `AKS-Egress` group's `Unapproved-Endpoints` collection against
the cluster's `hcp.eastus2.azmk8s.io` FQDN on 443, starting 01:30 — the exact
time on-call noticed the first failure. Confirm the endpoint is really that
FQDN and that egress is forced through the firewall:
`fixtures/az-aks-show.json:6` gives the cluster `fqdn` as
`retail-prod-06ab0cfb.hcp.eastus2.azmk8s.io` and line 16 shows
`outboundType: userDefinedRouting`, which sends all pod egress to the Azure
Firewall.

## Why the red herring misleads

The kubelet is the obvious suspect — the failing verbs print a
`10.240.0.35:10250` address, which looks like a kubelet refusing to talk.
`fixtures/kubelet-status.txt` is the disproof: the kubelet is active,
ready probes on the very pods you cannot exec into pass, and there are no
auth or port errors anywhere in its log. The 10250 timeout is what the
API server *reports* when it cannot open the proxied stream — the failure
is upstream in the tunnel, at the konnectivity-agent, not in the kubelet.
The second mild decoy is `kubectl-get-pods-kube-system.txt` itself: with
two kube-system pods CrashLooping it is tempting to stop at "control-plane
components are down" without asking *why* they are down — the firewall log
is what turns a symptom into a cause.

## Root cause

A hardened Azure Firewall application rule collection (`Unapproved-Endpoints`,
`fixtures/azfw-application-rule-log.kql.csv:2-7`) denies egress to
`*.hcp.eastus2.azmk8s.io` on 443. Because the cluster routes egress through
the firewall (`fixtures/az-aks-show.json:16`), the konnectivity-agent pods
can no longer reach the API server tunnel endpoint
(`fixtures/konnectivity-agent.log:2`), crash-loop
(`fixtures/kubectl-get-pods-kube-system.txt:6-7`), and every
tunnel-proxied verb — `logs`, `exec`, `attach`, `port-forward` — times out
with the kubelet-side `dial tcp 10.240.0.35:10250` error the operator sees.

## Fix

Allow the cluster's API server FQDN through the firewall. Add an application
rule for `*.hcp.eastus2.azmk8s.io` on port 443 (or the
`AzureKubernetesService` FQDN tag for the cluster's region and resource
group, which AKS documents for exactly this purpose) to the `AKS-Egress`
rule collection group's allow list, in place of the blanket
`Unapproved-Endpoints` deny:

```bash
az network firewall application-rule create \
  --firewall-name prod-fw -g rg-network \
  --collection-name AKS-Egress-Allow \
  --name allow-aks-hcp --protocols HTTPS=443 \
  --target-fqdns '*.hcp.eastus2.azmk8s.io' --action Allow
```

No cluster restart is needed: once the rule lands, the konnectivity-agent
Deployment's CrashLoopBackOff recovers on its own and the tunnel re-opens.
Safe and reversible — one allow rule for a required Microsoft-managed
endpoint; nothing on the cluster changes.

## Prevention

- **Change windows for firewall rule collections:** the deny collection
  landed overnight and took the tunnel with it. Require a `az network
  firewall` plan review that enumerates the AKS required-FQDN list against
  the cluster's outboundType before any rule collection change merges —
  `userDefinedRouting` clusters depend on it for management-plane traffic.
- **Synthetic tunnel probe:** alert when `kubectl logs` on a canary pod
  fails cluster-wide (or when konnectivity-agent restarts exceed N per
  hour). The CrashLoop in `fixtures/kubectl-get-pods-kube-system.txt:6-7`
  was visible for an hour before anyone correlated it with the firewall.
- **Tag-based egress config:** prefer the `AzureKubernetesService` FQDN tag
  over hand-listed `*.azmk8s.io` hostnames so regional updates to the
  required endpoints are picked up automatically.

## Further reading

- [Control network egress traffic for AKS cluster nodes](https://learn.microsoft.com/azure/aks/concepts-egress)
- [Outbound type userDefinedRouting in AKS](https://learn.microsoft.com/azure/aks/egress-udr)
- [AKS cluster FQDN requirements — *.hcp.<region>.azmk8s.io and tunnelfront](https://learn.microsoft.com/azure/aks/concepts-egress#azure-kubernetes-service-egress-fqdn-tags)
- [Konnectivity service in AKS](https://learn.microsoft.com/azure/aks/concepts-network-traffic)
