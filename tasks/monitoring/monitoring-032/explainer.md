# monitoring-032 — explainer

## How to investigate

When an alert "never fired," the question is which link in the chain broke:
the signal, the rule's condition, or the notification path. Check them in
that order.

First confirm the signal existed and the notification path was clean.
`fixtures/kql-5xx-per-5min.csv` shows the burst plainly: the 14:35, 14:40
and 14:45 bins have 402, 511 and 468 server errors against a normal of
~26–44 — roughly 1,400 failures in fifteen minutes. And
`fixtures/az-monitor-action-group-show.json` shows `ag-prod-oncall` enabled
with every receiver (email, SMS, webhook) `Enabled`. A silent pager with a
healthy signal and a healthy action group points squarely at the rule's
condition.

Then read the rule itself: `fixtures/az-monitor-scheduled-query-show.json`
shows `high-5xx-rate` is `enabled: true`, evaluated every 15 minutes,
`operator: GreaterThan`, `threshold: 50`, `timeAggregation: Average`, with
one failing period enough to alert. The rule exists and is wired up — but
look closely at the units it evaluates in.

Now the decisive comparison: what does the query actually produce, and can
it ever beat the threshold? `fixtures/alert-query.kql` ends with
`| summarize avg(is5xx)` over a one-hour window, where
`is5xx = iff(toint(resultCode) >= 500, 1, 0)` — a per-request 0/1 flag.
The average of a 0/1 column is a ratio bounded by 1.0. The condition
compares that average against `GreaterThan 50`. Even a perfect outage
(one thousand percent of requests failing) evaluates to 1.0 — seventy
times below the threshold. The condition is mathematically unsatisfiable;
the rule can never fire, on this spike or any other.

Finally verify the "never fired" claim rather than trusting the pager:
`fixtures/alert-history.json` queries AlertsManagementResources over the
incident window and returns **zero rows** — no fired, no muted, no failed
evaluation to blame. The rule simply never satisfied its condition, exactly
as the units mismatch predicts: during the spike the hour-window average
was about 0.055 while the condition demanded more than 50.

## Why the red herring misleads

Every classic excuse for a silent alert is available here, and each fixture
politely eliminates it. "The action group was misconfigured" —
`fixtures/az-monitor-action-group-show.json` shows all receivers enabled.
"The alert fired but was muted or auto-mitigated" — `fixtures/alert-history.json`
returns zero rows; there is nothing in the history at all. "The evaluation
frequency was too slow to catch a 15-minute spike" — the rule runs every
15 minutes (`evaluationFrequency: PT15M`), it would have seen the spike
twice. And "the query was wrong and returned nothing" — it returns
something, just in the wrong units. The trap is that
`fixtures/az-monitor-scheduled-query-show.json` looks healthy in every
field an on-call usually checks (enabled, scoped, actioned, frequent);
the defect hides in the arithmetic between `avg(is5xx)` in
`fixtures/alert-query.kql` and `threshold: 50` — a count-shaped threshold
applied to an average-shaped signal.

## Root cause

The "High 5xx rate" scheduled query rule compares apples to oranges:
its query computes `avg(is5xx)` — a per-request 0/1 average bounded by
1.0 over a one-hour window (`fixtures/alert-query.kql`) — but its
condition requires `Average > 50` (`fixtures/az-monitor-scheduled-query-show.json`),
which is count semantics. The threshold of 50 (matching the ~402–511
counts per 5-minute bin in `fixtures/kql-5xx-per-5min.csv`) can never be
exceeded by a value capped at 1.0, so the condition is unsatisfiable and
the rule can never fire — confirmed by the empty incident-window history
in `fixtures/alert-history.json`.

## Fix

Make the query's aggregation and the threshold use the same units. Two
equally good shapes:

**Count semantics (matches the existing threshold of 50):**

```kql
requests
| where cloud_RoleName == "api-gateway"
| where TimeGenerated > ago(30m)
| summarize total = count(), errors = countif(toint(resultCode) >= 500)
| extend error_count = errors
| project error_count
```

with `timeAggregation: Average` over the projected `error_count`, still
compared to `GreaterThan 50`.

**Percentage semantics (keeps the current query's spirit):**

```kql
requests
| where cloud_RoleName == "api-gateway"
| where TimeGenerated > ago(30m)
| summarize avg(is5xx) * 100
```

with the threshold set to 0.5 (i.e. alert above 0.5% errors).

Either way, verify the condition with a synthetic backfill: replay the
14:35–14:50 spike through the new rule and confirm it fires, then file the
rule change through normal alert-as-code review so the threshold's units
are visible in the diff.

## Prevention

Unit mismatches in alert rules are invisible until the one day you need
the alert. Three concrete guardrails:

1. **Alert-as-code review with a lint rule.** Keep scheduled query rules in
   Terraform/Bicep and add a CI check that parses each rule's query and
   flags a `threshold` of large magnitude paired with an `avg(…)` over a
   0/1 (boolean-derived) column — or more generally, any rule whose query
   output domain (ratio vs count) disagrees with the threshold's. This
   exact incident fails the lint at PR time.
2. **Synthetic alert firing in staging.** A weekly job that injects a
   small batch of 5xx rows into the Log Analytics workspace and asserts a
   fired alert appears in the AlertsManagement history within one
   evaluation period. An alert that has not demonstrably fired (or been
   tested) in the last quarter is not a monitoring control.
3. **Coverage reconciliation.** Compare incident postmortems against alert
   history: any severity-seeming user-visible failure (like the ~1,400
   5xxs here) with an empty alert history for its affected resource should
   auto-open a follow-up task — the missing alert gets noticed in days,
   not discovered during the next outage.

## Further reading

- [Scheduled query alerts in Azure Monitor (timeAggregation and threshold semantics)](https://learn.microsoft.com/azure/azure-monitor/alerts/alerts-shared-query)
- [Kusto summarize operator and aggregation functions](https://learn.microsoft.com/azure/data-explorer/kusto/query/summarizeoperator)
- [Action groups and alert processing in Azure Monitor](https://learn.microsoft.com/azure/azure-monitor/alerts/action-groups)
