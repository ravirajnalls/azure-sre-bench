# aks-ops-071 — explainer

## How to investigate

Triage a storm by ordering, not by noise. The alert list
(`fixtures/alerts.json`) spans six minutes (04:12–04:17), and exactly one
alert fired first: **ALT-09**, the Azure Service Health event, at 04:12:11 —
every cluster alert follows it within five minutes. Its own summary carries
the second clue: impact started at **04:08 UTC**, four minutes before the
first cluster alert, so whatever it describes was already in progress while
the cluster still looked healthy.

Read what the Service Health event claims and test it against the cluster:

- **The claim** (`fixtures/service-health.json`): degraded network
  performance for VMs and managed disks in **Availability Zone 2 of West
  Europe** — packet loss, connectivity latency, and disk attach/detach
  timeouts. Status Active.
- **The zone check** (`fixtures/kubectl-get-nodes.txt`): the three NotReady
  nodes (`vmss0000a0`, `a1`, `a2`) are all in zone **2**, while the zone-1
  and zone-3 nodes — including a zone-2 spot node that stayed Ready — are
  fine. A total zone outage would evict everything in zone 2; a partial
  network/degradation event explains why three of four zone-2 app nodes
  lost their kubelet while one survived.
- **The disk symptom**: Service Health names managed-disk attach/detach
  timeouts. The events fixture shows `FailedAttachVolume` on a rescheduled
  checkout-api pod with a **detach timeout** from the previous (NotReady)
  node — the exact failure mode the platform alert predicted.

Every other alert is downstream of the zone-2 degradation:

- **ALT-01/02/03 (KubeNodeNotReady ×3)** — the zone-2 app nodes lose their
  control-plane connectivity; kubelet stops posting status.
- **ALT-04 (KubePodCrashLooping)** — the taint manager evicts payments-api
  off a NotReady node; the replacement CrashLoopBackOffs during reschedule.
- **ALT-05 (Ingress5xxRate)** — with a third of the app capacity unreachable
  and pods rescheduling, upstream connections time out into 502/504s.
- **ALT-06 (PVCAttachFailed)** — disks cannot detach from the dead zone-2
  nodes fast enough for the rescheduled pods; Multi-Attach errors.
- **ALT-07 (HPAAtMaxReplicas)** — the autoscaler fights FailedScheduling
  (0/8 nodes available) and pins at max with pods Pending.
- **ALT-08 (LogAnalyticsIngestionDrop)** — ama-logs is DaemonSet-bound to
  the same three NotReady nodes; its eviction takes a third of ingestion
  with it. (This one is easy to misread as a Log Analytics outage: the
  workspace is fine, the *agents* are not.)

## Why the red herring misleads

The loudest trap is **ALT-08 (LogAnalyticsIngestionDrop)**: it looks like a
standalone telemetry outage with its own workspace and its own baseline, and
it is the alert an on-call engineer is most tempted to treat as the root —
but the ingestion drop is a 33% *fraction*, and a third of the fleet is
exactly the three NotReady zone-2 nodes. The workspace stays healthy; the
agents on the dead nodes do not. The second trap is treating the three
KubeNodeNotReady alerts as the root cause — they fired at 04:13, one minute
*after* the Service Health event, and a cause cannot postdate its own
symptom; rolling the node pool would have churned healthy zones while the
platform incident continued. The Service Health alert's 04:08 impact start
and the zone-2 concentration of exactly the failed nodes are what order the
causality.

## Root cause

**ALT-09 — ServiceHealthZoneDegraded.** Azure Service Health reported
degraded network performance for VMs and managed disks in Availability Zone
2 of West Europe, active from 04:08 UTC, four minutes before any cluster
alert fired. It is the only alert that fired first, and the only one whose
evidence (service-health.json, kubectl-get-nodes.txt) explains the shape of
the storm: exactly the zone-2 app nodes went NotReady, disks timed out on
attach/detach, and a zone-2 spot node that stayed Ready shows the blast
radius matches a network degradation, not a total zonal outage.

## Fix

The remediation is platform-side, not cluster-side: subscribe to the
Service Health tracking event (4K1Z-W8T) and stop treating this as a
cluster failure. Concretely: fail over zone-2 app traffic by relying on the
zone-1/zone-3 replicas (topology spread already keeps PDBs satisfiable), let
the rescheduled pods settle, then verify attach/detach behavior recovers as
Azure mitigates (`impactMitigationTime` flips from null). Afterward, clear
the fallout in order: unstick the HPA by confirming scheduling succeeds,
confirm ama-logs reports from all three nodes again, and watch the ingress
5xx rate return to baseline. Do not roll the node pool or open a Log
Analytics support ticket — both treat symptoms.

## Prevention

- Treat **Azure Service Health as the first signal** for every multi-alert
  storm: alert on Service Health events per region and availability zone, so
  the platform view arrives with the storm, not five minutes into it.
- Keep **zonal spread honest**: topology spread constraints across three
  zones, PDBs sized so a degraded zone still leaves serving capacity, and a
  spot pool deliberately spread across zones (the Ready zone-2 spot node in
  the fixture is what kept capacity judgment honest).
- Make **observability agents zonal-outage-aware**: dashboards should show
  per-node ama-logs health next to node status, so an ingestion drop is
  read as "agents down" versus "pipeline down".
- Run **attach/detach failure drills**: rehearse what a disk-detach timeout
  does to rescheduled StatefulSet-like pods (checkout-api's PVC) so the
  Multi-Attach signature is recognized in minutes.
- In on-call runbooks, make **"which alert fired first, and what does the
  platform say"** the mandatory first two questions of any storm triage.

The naive triage — "three nodes died, roll the node pool" or "Log Analytics
is down, open a support ticket" — treats symptoms as causes and would have
on-call paging the wrong team all morning. The only alert that explains the
order, the zone concentration, and the disk-attach signature is **ALT-09**;
the fix is not in the cluster at all, which is the point of listening to
the platform first.

## Further reading

- [Azure Service Health documentation](https://learn.microsoft.com/azure/azure-monitor/az-monitor-overview#service-health) —
  platform-level incident and impact notifications, including resource-type
  and availability-zone scoping.
- [Availability Zones in Azure Kubernetes Service](https://learn.microsoft.com/azure/aks/availability-zones) —
  how AKS node pools spread across zones and what survives a zonal
  degradation.
- [Kubernetes eviction and node conditions](https://kubernetes.io/docs/concepts/scheduling-eviction/taints-and-tolerations/) —
  how the taint manager reacts to `NotReady`, and why rescheduled pods pile
  into CrashLoopBackOff and FailedScheduling.
- [Azure managed disk attach semantics](https://learn.microsoft.com/troubleshoot/azure/virtual-machines/windows/multi-attach-disks) —
  why a disk still attached to a dead node blocks attach elsewhere, and how
  detach timeouts surface as `FailedAttachVolume`.
