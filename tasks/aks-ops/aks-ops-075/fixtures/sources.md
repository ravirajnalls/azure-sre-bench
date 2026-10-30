# Event sources

Four sources recorded the upgrade incident of 2026-10-30 on payments-prod-aks.
The `events.json` export merges them; the `source` field on each event says
where it came from.

## upgrade-pipeline (`pl-*`)

Second-precision step log from the CI runner that drove
`az aks nodepool upgrade`. Clock: NTP-synced. Times are UTC.

## kubectl-events (`kc-*`)

`kubectl get events -A` captured from the ops bastion. Clock: NTP-synced.
Times are UTC.

## activity-log (`al-*`)

Export of the cluster's Azure Activity Log resource-write entries.

> **Known offset:** this export's timestamps run **7 minutes ahead** of the
> cluster's actual event time (the export job's clock skew, documented in the
> subscription's log-analytics workbook). Subtract 7 minutes to line an
> `al-*` event up with the other sources.

## chat (`chat-*`)

On-call incident channel. Minute precision; clock NTP-synced. Human
observations and actions, not machine events.
