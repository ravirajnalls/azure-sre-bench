# aks-ops-069 — explainer

## How to investigate

Change attribution starts from the onset time, not from the change list. The
metrics export (`fixtures/az-monitor-checkout-errors.csv`) brackets it: the
error rate sits at 0.55% through 14:00, jumps to 7.7% in the 14:05 bucket and
35% by 14:10. The logs (`fixtures/kubectl-logs-checkout-api.txt`) sharpen it
to a second — the first `TimeoutError: unable to acquire a connection within
30s (pool size 8, 8 in use, 14 waiters)` lands at 14:06:11 UTC, right after
the pool saturates at 8/8 with two waiters at 14:05:20. Anything that caused
this must have been in effect at 14:05-14:06.

Now read the change log (`fixtures/changes.json`) against that window:

- **CHG-1042 — applied 14:05 UTC**, `env DB_POOL_MAX: 40 -> 8` on
  checkout-api, via deploy pipeline checkout/cd-3120. The only change to
  checkout-api's runtime configuration before onset. The live manifest
  (`fixtures/kubectl-get-deploy-checkout-api.yaml`) confirms the fleet runs
  `DB_POOL_MAX: "8"` right now, and the pods listing
  (`fixtures/kubectl-get-pods-checkout.txt`) shows six checkout-api replicas —
  each capped at 8 database connections. The log line says the mechanism
  outright: pool size 8, saturated, waiters climbing until the 30-second
  acquire timeout fires and the gateway answers 502.
- **CHG-1007 — applied Oct 28**: HPA minimum replicas 4 → 6. Capacity
  headroom, not a fault; the six replicas it predicts are exactly what the
  pods listing shows.
- **CHG-0991 — applied Oct 29 16:20**: a patch deploy of a different service
  (recommendation-svc). Its calls return `status=200` throughout the incident
  log, and its pods have been up 19h.

The remaining two need timing, not just plausibility:

- **CHG-1038 — applied 09:12 UTC**, node-image work on the apps pool. The
  checkout-api pods are all ~5h old — rescheduled during that maintenance —
  and served a 0.5% error rate from ~10:00 until 14:06. Infrastructure that
  ran clean for four hours before onset did not start the incident.
- **CHG-1043 — applied 14:40 UTC**, the configmap flag change. It landed 34
  minutes **after** the first pool-exhaustion error. It may have made the
  incident noisier, but a change cannot cause an error wave that predates it.

## Why the red herring misleads

The symptom names three recent changes, and the most suggestive one is this
morning's node-pool infrastructure work: "something changed on the nodes" is
the reflex when pods restart. The pods listing is the counter-evidence —
every checkout-api pod restarted at ~10:00 (during the maintenance drain) and
then served clean traffic for four hours, including the 14:00-14:05 buckets
in the metrics export. A node-image fault would show up in those pods
immediately after rescheduling, not five hours later with a saturated
application-level connection pool. The recommendation-svc deploy from
yesterday is a different service whose 200s are interleaved through the
incident window, and the afternoon configmap change is the classic
post-hoc trap: it is the *latest* change, it touched checkout-api, and it is
guilty-looking — but the timeline exonerates it, because the errors started
34 minutes before it was applied.

## Root cause

CHG-1042 (applied 2026-10-30T14:05:00Z, deploy pipeline checkout/cd-3120)
changed `DB_POOL_MAX` from 40 to 8 on the checkout-api deployment as a
"config-drift cleanup." Six replicas × 8 connections cannot serve peak
checkout traffic: the pool saturates within a minute of the change, waiters
queue, every acquire times out after 30s, and the error rate jumps from
0.55% to ~35% — 14:05 is the change's applied_at to the minute.

## Fix

Roll back CHG-1042: restore `DB_POOL_MAX` to 40 on the checkout-api
deployment (`kubectl set env` or the helm values) and let the existing pods
drain their queues. Verify recovery on the metrics export — the error rate
should drop to baseline within one pool-drain cycle. If the 8-connection
baseline was intentional, it needs a load test across all six replicas before
it ships, plus a HPA-aware pool budget; do not re-apply it during an
incident.

## Prevention

Gate config-drift cleanups behind the same change review as feature deploys —
CHG-1042 shipped via the deploy pipeline with no canary or error-rate burn
alert attached. Add a deployment-time guard that diffs application-critical
env variables (pool sizes, timeouts, ports) against the running manifest and
requires a human approval when they shrink. Finally, alert on connection-pool
saturation (`in use 8/8, waiters > 0` sustained) so the next pool change that
is too small pages before checkout traffic does.

## Further reading

- [PostgreSQL wiki: pgbouncer pooling modes](https://www.pgbouncer.org/faq.html)
- [Kubernetes: Manage Resources — application-level connection budgeting](https://kubernetes.io/docs/concepts/configuration/manage-resources-containers/)
- [Google SRE Book: Alerting on SLOs — burn-rate alerts for error waves](https://sre.google/sre-book/alerting-on-slo/)
