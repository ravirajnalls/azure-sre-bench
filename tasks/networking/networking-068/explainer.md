# networking-068 — explainer

## How to investigate

"Stopped resolving publicly, works internally" is the signature of a
*delegation* problem: the internal resolver bypasses the public
hierarchy (it answers from cache or a hosts entry), while public
resolvers must walk it — root servers down to `.com`, then
`contoso.com`, then the `eu` delegation. So the walk, not the zone
contents, is the suspect. Run the walk yourself:
`fixtures/dig-trace.txt` shows public resolution reaching the
`contoso.com` servers, being handed to `ns1-01.azure-dns.com.` and
friends, and **those** servers answering REFUSED for a name in a zone
they no longer host. The delegation is pointing at servers that
disown the zone.

Now compare the two ends of the delegation. The recreated zone's
actual servers are in `fixtures/az-network-dns-zone-show-eu.json`:
`ns1-03.azure-dns.com.` etc. The servers the parent hands out are in
`fixtures/az-network-dns-record-set-ns-parent.json`: `ns1-01...` —
the pre-recreation assignment. Azure DNS re-picks its four name
servers whenever a zone is deleted and recreated; the `01`-suffix
servers stopped hosting the zone the moment it was recreated, and the
parent's NS record set was never re-pointed. `fixtures/activity-log.json`
supplies the timeline: delete at 14:05:32, re-create at 14:06:19 on
2026-10-29 — public resolution broke "yesterday," exactly.

## Why the red herring misleads

The app team's ask — "fix the record" — and the artifact they attach,
`fixtures/az-network-dns-record-set-a-api.json`, both aim at the api
A record. And to be clear, the record *is* fine: `api.eu.contoso.com`
→ the right address, a healthy TTL, sitting in a zone that answers
correctly when you query its **current** servers directly. Editing it
changes nothing, because the resolution path never reaches it — the
walk dies one level up, at the stale delegation. The lesson generalizes:
in DNS, a record can be perfect and resolution still fail, because
resolution is the *path* to the record, and this failure is in the
path.

## Root cause

Deleting and recreating `eu.contoso.com` caused Azure DNS to assign
new name servers (`ns1-03…`–`ns4-03…`) to the recreated zone
(`fixtures/az-network-dns-zone-show-eu.json`). The NS record set for
`eu` in the parent `contoso.com` zone still lists the old assignment
(`ns1-01…`–`ns4-01…`, `fixtures/az-network-dns-record-set-ns-parent.json`).
Public resolvers follow the delegation, query servers that no longer
host the zone, get REFUSED, and return SERVFAIL — while the corporate
resolver, which never walks the public delegation, keeps answering
from cache. The zone was recreated at 14:06 UTC on 2026-10-29
(`fixtures/activity-log.json`); public resolution has been broken
since that moment.

## Fix

Re-point the delegation: update the `eu` NS record set in the parent
`contoso.com` zone to the recreated zone's current name servers —

```
az network dns record-set ns set -g rg-dns -z contoso.com -n eu \
  --record-set-type NS \
  --record ns1-03.azure-dns.com. --record ns2-03.azure-dns.net. \
  --record ns3-03.azure-dns.org. --record ns4-03.azure-dns.info.
```

Then respect the caching reality: the old delegation carries a
172800-second TTL (48 hours), so resolvers that cached it keep asking
the dead servers until the TTL expires — worst case two days, not
instant. Verify with `dig +trace` from a public vantage point that the
`eu.contoso.com` referral now lists the `-03` servers and that
`api.eu.contoso.com` resolves end to end. For resolvers with very long
cache tails, the zone's servers can also be temporarily configured to
answer for the old hostnames, but the durable fix is the updated
delegation.

## Prevention

- Make "zone recreated" a trigger for "delegation re-checked": any
  delete + create of a DNS zone must end with a diff of the zone's
  assigned name servers against every delegation that references it,
  parent zones and registrar glue alike.
- Lower delegation TTLs for zones that get churned: 172800s is a sane
  default for a stable zone and a liability for one under active
  cleanup — reconcile the TTL with the change cadence.
- Monitor from outside: an external availability probe from a public
  resolver caught this in hours, but a synthetic `dig +trace` that
  asserts the delegation path (not just the final answer) would have
  flagged the stale referral the moment the zone was recreated,
  before any user noticed.

## Further reading

- [Azure DNS zones — delegating with name servers](https://learn.microsoft.com/azure/dns/dns-domain-delegation)
- [NS records and delegations in Azure DNS](https://learn.microsoft.com/azure/dns/dns-zones-records)
- [dig +trace and how resolution walks the hierarchy](https://www.rfc-editor.org/rfc/rfc1034)
