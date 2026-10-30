# aks-workloads-053 — explainer

## How to investigate

The symptom gives you two clocks ticking together: replica count changes and
resource-request changes on nearly the same cadence. When two system
properties oscillate in lockstep, ask what mechanism can move both. Start
with `fixtures/kubectl-get-hpa-watch.txt`: the HPA's cpu target column swings
between roughly 26% and 151% of the 60% target while REPLICAS bounces between
3 and 9 — ten samples, five full reversals of direction. An HPA only behaves
this erratically when its input signal is unstable, and for a
`Resource`/`Utilization` metric the signal is a *ratio*: pod cpu usage divided
by the pod's cpu **request**. If the denominator moves, the ratio moves even
when usage is flat.

So the next question is: what is moving the requests?
`fixtures/vpa-recommender.log` answers it — the recommender rewrites the
recommendations-api cpu request repeatedly (`250m -> 900m -> 1.1 -> 350m ->
1.2`) and the updater evicts pods to apply each change
(`updateMode=Auto`). `fixtures/kubectl-get-events.txt` shows the two
mechanisms interleaved on the same timestamps: `VPA evicted pod to apply
resource recommendation` lines alternating with `Required duration exceeded,
scaling up` HPA events.

Finally, confirm the configuration makes the loop closed rather than
coincidental. `fixtures/vpa.yaml` sets `updateMode: Auto` with
`controlledResources: [cpu, memory]`, and `fixtures/hpa.yaml` scales the
**same** Deployment on `Resource cpu Utilization` at 60%. Same pods, same cpu
requests, two controllers writing and reading them — the loop is structural,
not an accident of load.

## Why the red herring misleads

`fixtures/node-pressure.txt` invites you to blame the node layer: the
recommendations-api pods are being evicted every few minutes, and node
evictions are the classic cause of replica churn. Read the file, though, and
every pressure condition is `False` on all six nodes, with kubelet eviction
thresholds at defaults and nothing fired in the past 7 days. The eviction
events themselves also name a different actor: `kubectl-get-events.txt` says
"VPA evicted pod to apply resource recommendation" — the evictions are the
VPA updater doing its job, not the kubelet defending a node. Node-pressure
would also not explain the second oscillation (the `cpu` target percentage)
that is the actual killer here.

## Root cause

The VPA (`updateMode: Auto`, `controlledResources` including cpu,
`fixtures/vpa.yaml`) continuously rewrites recommendations-api's CPU requests
and evicts pods to apply them (`fixtures/vpa-recommender.log`). The HPA
(`fixtures/hpa.yaml`) scales replicas on CPU **utilization** — usage divided
by those same CPU requests — at a 60% target. Each VPA request change swings
the utilization percentage (`fixtures/kubectl-get-hpa-watch.txt` shows 26%→
151% swings), the HPA reacts by scaling replicas in the opposite direction of
the recommendation, and the changed replica set feeds the VPA new usage
statistics to recommend from. The two controllers form a feedback loop over
the same field, and p99 spikes during every VPA eviction that lands mid-swing.

## Fix

Break the loop by giving the two autoscalers disjoint signals — any one of:

1. Restrict the VPA to memory so the HPA's input stops moving:

   ```yaml
   resourcePolicy:
     containerPolicies:
       - containerName: recommendations-api
         controlledResources: [memory]
   ```

2. Set `updatePolicy.updateMode: "Off"` (recommender-only mode), size cpu
   requests manually, and re-enable VPA updates only after load testing.

3. Keep the VPA on cpu and re-point the HPA at a custom metric that does not
   depend on the pods' requests — for example requests-per-second or
   connection count — so the two controllers no longer share a field.

Option 1 is the smallest change and keeps both controllers active. After the
change, `kubectl get hpa -w` should show a stable target percentage with
replicas moving only under real load.

## Prevention

- **Policy-check autoscaler combinations.** A CI/CD lint that rejects a VPA
  with `updateMode: Auto` controlling cpu in the same namespace/Deployment
  as an HPA on `Resource cpu Utilization` would have blocked this at review
  time; the combination is a known anti-pattern, not a subtle bug.
- **Change-window soak test.** The platform team's Monday "autoscaling
  reconciliation" landed both manifests at once. Run a 24-hour soak with
  synthetic load after any autoscaler change and alert on target-percentage
  variance — the watch output here shows the failure within minutes.
- **Tune HPA stabilization.** HPA `behavior.stabilizationWindowSeconds`
  smooths reactions to input noise; it would damp, but not fix, this loop —
  document it as mitigation only.

## Further reading

- [Kubernetes VPA — known limitations with HPA](https://github.com/kubernetes/autoscaler/blob/master/vertical-pod-autoscaler/pkg/README.md#known-limitations)
- [HPA — Resource metric with utilization target](https://kubernetes.io/docs/tasks/run-application/horizontal-pod-autoscale/)
- [VPA resourcePolicy — controlledResources](https://github.com/kubernetes/autoscaler/blob/master/vertical-pod-autoscaler/pkg/README.md#resource-policy)
- [KEDA and VPA coexistence guidance](https://keda.sh/docs/latest/operate/#cluster-considerations)
