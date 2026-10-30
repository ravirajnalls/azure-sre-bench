# networking-067 — explainer

## How to investigate

Archive pipelines fail in one of three places: nothing is produced to
send, the send is misconfigured, or the destination refuses the write.
The symptom hands you the first two cheaply — the diagnostic setting
looks healthy, and nothing changed in the cluster — so start at the
destination. `fixtures/storage-log.kql.csv` is the telling artifact:
the storage account's *own* log shows `AppendBlock` calls from
`20.42.65.92` (an Azure Monitor address, and the user agent says so
too) arriving daily and being answered **403**. So the write is
arriving at the account's front door and being refused — and the 403s
start on 2026-10-25, one day after the activity log's lockdown entry.

Now read the destination's config: `fixtures/az-storage-account-show.json`
shows `networkAcls.defaultAction: Deny` with the corporate subnet and
two corporate IPs allowed — and `"bypass": []`. The bypass list is the
storage firewall's carve-out for first-party (trusted) Azure services;
emptied, even Azure Monitor's platform identity is subject to the
network rules, and its calls come from Microsoft-owned addresses that
match neither the subnet nor the IP allowlist. Cross-check the timeline:
`fixtures/activity-log.json` shows the storage account write that
replaced `networkAcls` on 2026-10-24 with exactly this shape — the
lockdown and the first failure are the same event.

## Why the red herring misleads

403 plus a service principal writes "permissions problem" into every
SRE's reflex, and `fixtures/az-role-assignment-list.json` feeds it: a
`Storage Blob Data Contributor` assignment for the monitoring writer
sits right there, so the first instinct is to audit role scopes and
re-grant access. But re-granting is already done — the assignment
exists at the account scope — and `fixtures/az-monitor-diagnostic-settings-show.json`
shows the sender side is healthy too. When both ends of a pipeline are
provably configured and the failure is 403 at a fixed network boundary,
the middle — the network rules — is the remaining suspect. The storage
log even agrees: a missing *role* produces the same 403 text, but a
lockdown that coincided to the day with the failures is not
coincidence.

## Root cause

The 2026-10-24 lockdown set `defaultAction: Deny` on `stauditarchive`
and emptied `networkRuleSet.bypass` in the same update
(`fixtures/az-storage-account-show.json`). Azure Monitor's diagnostic
pipeline is a trusted first-party service: with
`bypass: ["AzureServices"]`, its writes skip the network rules; with
the list empty, its `AppendBlock` calls must satisfy the IP/subnet
rules, and they come from Microsoft-owned addresses that cannot. Every
archive write since 2026-10-25 is refused with 403
(`fixtures/storage-log.kql.csv`), and the archive container ages while
the diagnostic setting reports healthy — the failure is invisible from
the monitoring side.

## Fix

Restore the trusted-service exemption without undoing the lockdown:

```
az storage account update -g rg-observability -n stauditarchive \
  --default-action Deny --bypass AzureServices
```

The subnet and IP rules stay exactly as the security team set them;
only the bypass list changes, and it exempts first-party Microsoft
services (Azure Monitor among them) from the network rules while the
Deny default keeps blocking everything else. Confirm recovery by
watching the next daily archive append return 200/201 in the storage
log and the compliance archive-age alert clear. If the security
mandate forbids the trusted-services bypass, the alternative is a
Private Endpoint for the account plus private-link ingestion — a
larger change that needs the same verification.

## Prevention

- Treat a storage account's `networkAcls` as a contract with its
  *writers*, not just its readers: any Deny-by-default change must be
  reviewed against every service that writes to the account, and
  Azure Monitor archival is the most common forgotten one.
- Canary the lockdown: apply the ruleset to a non-production twin with
  the same diagnostic settings first, or verify within 24 hours that
  each writer still succeeds — a firewall change that silently breaks
  log archival hides future incidents, not just past ones.
- Alert on pipeline *age* (time since last archive blob) rather than
  only on archive container emptiness — an aging alert fired this
  incident after five silent days; a 24-hour staleness alert would
  have caught it the first morning.

## Further reading

- [Configure Azure Storage firewalls and virtual networks](https://learn.microsoft.com/azure/storage/common/storage-network-security)
- [Trusted access for resources registered in your subscription (bypass AzureServices)](https://learn.microsoft.com/azure/storage/common/storage-network-security#grant-access-to-trusted-azure-services)
- [Azure Monitor diagnostic settings — archive to a storage account](https://learn.microsoft.com/azure/azure-monitor/essentials/diagnostic-settings)
