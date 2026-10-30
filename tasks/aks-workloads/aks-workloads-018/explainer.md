# aks-workloads-018 — explainer

## How to investigate

Latency with healthy-looking CPU is the signature of a workload that is being
*suspended*, not starved, so measure the suspension directly. The Grafana
comment block in `fixtures/prometheus-throttling.csv` explains the arithmetic:
the CFS scheduler enforces the container's cpu limit in 100 ms periods — 10
periods per second per pod — and the panel divides
`container_cpu_cfs_throttled_periods_total` by `container_cpu_cfs_periods_total`.
At 13:00 the pods throttle 19–21% of periods; by 13:30, 65–68%; through the
13:45–14:15 peak, 63–71%. Seven of every ten scheduling periods find the pod's
quota spent and the cgroup frozen until the next period.

`fixtures/deployment.yaml` names the quota being enforced: the pricing-api
container sets `limits.cpu: 500m` (request 250m) and runs a JVM
(`JAVA_TOOL_OPTIONS: -XX:MaxRAMPercentage=75.0 -XX:+UseG1GC`) whose request
threads all wake onto the same quota. `fixtures/prometheus-cpu-usage.csv`
reconciles the paradox — mean usage is 208–233 mCPU, 42–47% of the 500 m limit.
Bursts, not averages, exhaust a quota: eight Tomcat threads each want a full
core at the same instant, so the pool burns its entire 50 ms of per-period CPU
budget (500 m in a 100 ms period) within the first ~7 ms, and the remaining
~93 ms of every period is suspension. Confirm the autoscaler
is not the lever: `fixtures/kubectl-get-hpa.txt` shows four scale-ups ending at
maxReplicas 12 with targets still 91%/70% — replicas doubled, per-container
quota unchanged. Then rule out the two alternates the dashboards suggest:
`fixtures/pricing-api.log` shows quote latency climbing 512 ms → 3 s while the
10-minute GC summaries hold under 11 ms longest pause, and
`fixtures/kubectl-top-pod.txt` shows memory steady at ~540 Mi against a 1 Gi
limit.

## Why the red herring misleads

Two of them. The Grafana CPU panel reads ~45% — a healthy-looking average that
argues "there is headroom, the app must be broken elsewhere." But CFS
throttling is invisible to averages: the pod burns its quota in short bursts
and is suspended for the rest of each period, so the same 500 m limit reads 45%
mean *and* 70% throttled simultaneously. The scale-out story pulls the same
way: the HPA is doing its job (4 → 12 replicas, `fixtures/kubectl-get-hpa.txt`),
so "it stopped scaling" or "maxReplicas is too low" feels live — yet a
per-container quota is reproduced in full on every new pod, and
`fixtures/kubectl-top-pod.txt` makes the memory-pressure reading easy to
over-trust when its memory column is the largest number on screen at 540+ Mi
per pod, against a 1 Gi limit and flat across the incident.

## Root cause

The pricing-api container's `limits.cpu: 500m`
(`fixtures/deployment.yaml`) is a hard per-container ceiling enforced by the
CFS scheduler, and the multi-threaded JVM bursts past it: 65–71% of CFS
scheduling periods are throttled at peak (`fixtures/prometheus-throttling.csv`)
while mean usage sits at 42–47% of the limit
(`fixtures/prometheus-cpu-usage.csv`). Quote latency triples
(`fixtures/pricing-api.log`) and doubling replicas via the HPA
(`fixtures/kubectl-get-hpa.txt`) cannot relieve a constraint each pod carries
individually.

## Fix

Raise or remove the limit in `fixtures/deployment.yaml` — the burst demand, not
the 45% average, sizes it (e.g. 1500 m), or drop the cpu limit so the JVM is
bounded only by node capacity. Keep the 250 m *request* so the HPA's
utilization percentage still has a denominator. A JVM note worth checking while
you are in the manifest: without an explicit `-XX:ActiveProcessorCount`, the JVM
sizes its thread pools from the *node's* CPU count, which is exactly what makes
8 threads arrive on a half-core quota.

## Prevention

Alert on the throttling ratio, not on CPU average: fire when
`container_cpu_cfs_throttled_periods_total / container_cpu_cfs_periods_total`
exceeds ~10% for five minutes across any pod — this incident sat at 70% for
over an hour with zero alerts. Add a CI check for deployments that pair a
Resource-metric HPA with a cpu limit less than 2× the request, which is the
configuration that turns ordinary burstiness into throttling. And in load
tests, chart the throttled-periods panel next to p99 — latency regressions show
up there several load levels before they show in the SLA.

## Further reading

- [Resource management for pods and containers](https://kubernetes.io/docs/concepts/configuration/manage-resources-containers/)
- [AKS best practices for resource management — application developers](https://learn.microsoft.com/en-us/azure/aks/developer-best-practices-resource-management)
