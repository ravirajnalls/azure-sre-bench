# aks-networking-052 — explainer

## How to investigate

"Slow but zero errors" plus "CoreDNS running hot" is a pairing to take
literally: if the pod experienced real failures you'd see errors, and if only
the pod were slow, CoreDNS wouldn't care. A resolver that quietly generates
extra work on every lookup taxes both at once. So the two probes that pay off
are the pod's resolver configuration and CoreDNS's view of the traffic.

Pod side first. `fixtures/kubectl-exec-cat-resolv-conf.txt` shows what the
kernel actually uses: four search domains (`notify.svc.cluster.local`,
`svc.cluster.local`, `cluster.local`, and the node's Azure domain) and
`options ndots:5`. The rule ndots encodes: a name with **fewer than 5 dots**
is not absolute — the resolver tries it *with each search suffix appended
first*, and only falls back to the bare name after all suffix attempts fail.
Count the dots in `api.sendgrid.com`: two. So every lookup becomes five
questions, four of them doomed.

CoreDNS's log confirms the mechanics: `fixtures/coredns-query-log.txt` shows
the notifier (`10.244.7.19`) asking
`api.sendgrid.com.notify.svc.cluster.local.`, `api.sendgrid.com.svc.cluster.local.`,
`api.sendgrid.com.cluster.local.`, and the Azure node domain — NXDOMAIN every
time — before finally asking the bare `api.sendgrid.com.` and getting NOERROR.
With both A and AAAA that is 8 wasted queries per real lookup. The volume
signature in `fixtures/coredns-metrics.csv` matches: query rate jumps from
~310/s to ~5300/s, cache hit ratio collapses from 0.91 to 0.22 (NXDOMAIN
answers mostly aren't cached), and CoreDNS CPU blows past its 500m limit on
exactly the day the namespace change landed. Four doomed attempts × ~50ms
round-trips ≈ the ~200ms the callers feel.

## Why the red herring misleads

`fixtures/sendgrid-status.json` shows every SendGrid component operational —
which reads as *evidence against* the provider only if someone accused it,
and as *evidence for* the provider's blame if the investigation started from
"external calls are slow, so the external service must be slow." That is the
trap: the status page is green because the provider is fine, and it was
always going to be green, because the latency lives **before** the request
ever leaves the cluster — in the resolver, trying four suffixes that cannot
exist. The same logic rules out a SendGrid incident even without the status
page: the successful bare-name lookup returns NOERROR in ~38ms
(`fixtures/coredns-query-log.txt`), so once the resolver gets to the real
question, the answer is fast. Slow lookups that end in a fast successful
answer are a local storm, not a remote one.

## Root cause

The notifier pod resolves with `options ndots:5` — set explicitly in its
`dnsConfig` (`fixtures/deployment.yaml`, `fixtures/kubectl-exec-cat-resolv-conf.txt`)
— so the two-dot name `api.sendgrid.com` is tried against all four search
domains before the bare name. `fixtures/coredns-query-log.txt` shows those
suffixed attempts returning NXDOMAIN (plus one slow forwarded miss against the
Azure node domain) on every lookup. The result is ~8 wasted queries and
~200ms of added latency per external call, and a cluster-wide DNS load spike
— ~5300 queries/s, cache hit ratio 0.22, CoreDNS CPU over limit
(`fixtures/coredns-metrics.csv`) — because every pod behind the same
configuration does the same dance.

## Fix

Stop generating the doomed suffix attempts. The durable fix is lowering
ndots on the pod:

```yaml
# in fixtures/deployment.yaml's spec.template
dnsConfig:
  options:
    - name: ndots
      value: "2"
```

This resolves `api.sendgrid.com` (2 dots < 5 but ≥ ndots threshold... more
precisely, with `ndots:2` the bare name is tried as absolute immediately)
while keeping in-cluster service lookups (`orders.store.svc.cluster.local`,
3 dots) still suffix-expanded correctly. The zero-config-change alternative
is to make the client use a fully-qualified name — `api.sendgrid.com.` with a
trailing dot — which the resolver treats as absolute and looks up directly.
Prefer the `dnsConfig` change when you own the manifest: it fixes every
external hostname the pod uses, not just this one, and needs no client code
change. Verify by re-reading the CoreDNS log: the suffixed NXDOMAIN queries
for `api.sendgrid.com.*` disappear, query rate and cache hit ratio return to
baseline, and the ~200ms vanishes.

## Prevention

1. **Set `ndots` deliberately on any workload that calls external APIs.** The
   Kubernetes default of 5 is tuned for in-cluster service discovery and is
   pathological for pods that resolve more external than internal names.
2. **Watch CoreDNS query composition, not just totals.** A spike in NXDOMAIN
   answers against known search suffixes is the ndots signature; alerting on
   `coredns_dns_responses_total{rcode="NXDOMAIN"}` catches it cluster-wide.
3. **Treat a namespace move as a DNS event.** Search domains change with the
   namespace (`notify.svc.cluster.local` in this cluster), so lookups that
   were previously well-behaved can cross the ndots threshold after a move —
   check resolv.conf behavior in the canary namespace before the fleet rolls.

## Further reading

- [Kubernetes — DNS for Services and Pods (dnsConfig, ndots)](https://kubernetes.io/docs/concepts/services-networking/dns-pod-service/)
- [glibc resolver — ndots and search list behavior](https://man7.org/linux/man-pages/man5/resolv.conf.5.html)
- [CoreDNS — plugin/log and rcode metrics](https://coredns.io/plugins/log/)
- [AKS — CoreDNS scaling and troubleshooting](https://learn.microsoft.com/troubleshoot/azure/aks/coredns-troubleshooting-and-scale)
