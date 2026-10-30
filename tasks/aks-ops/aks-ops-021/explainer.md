# aks-ops-021 — explainer

## How to investigate

The symptom says the node flaps NotReady while CPU and memory look healthy —
so ask what condition makes a kubelet go NotReady without compute contention.
Read the node itself first: `fixtures/kubectl-describe-node.txt` shows
`DiskPressure: True` alongside `Ready: False`, and the taint list includes
`node.kubernetes.io/disk-pressure:NoSchedule`. That answers the "what" — the
node is out of disk — and `MemoryPressure`/`PIDPressure` are both `False`.

Confirm what fills the disk: `fixtures/node-disk-usage.txt` shows the OS
volume at 95% used and `/var/log/audit-debug` holding **58G** of
`audit-debug-*.log` files, a fresh ~1.7G one every ~17 minutes with no rotation
in sight. Nothing else on `/var` comes close (docker 3.1G, containerd 1.4G).
Find the writer: `fixtures/daemonset-legacy-audit-agent.yaml` mounts that exact
`/var/log/audit-debug` hostPath (`DirectoryOrCreate`), runs with
`DEBUG_LOG_LEVEL: trace`, and has no log-rotation configuration. A
`kubectl get events` view — `fixtures/kubectl-get-events.txt` — ties it to the
symptom: repeated `Evicted` events for payments pods citing "low on resource:
ephemeral-storage", plus the kubelet evicting even the audit agent itself and
re-creating it (it tolerates the disk-pressure taint, so it comes right back).

Finally, check the compute story to close it out: `fixtures/kubectl-top-node.txt`
shows every node, including the flapping one, at ≤15% CPU and ≤24% memory —
the flat dashboards are real, and the leak is on disk, not in memory.

## Why the red herring misleads

Two fixture families are planted to misdirect:

- **Container runtime bloat.** `fixtures/node-disk-usage.txt` deliberately
  shows `/var/lib/docker` and `/var/lib/containerd` sizes (3.1G, 1.4G) and the
  task mentions "no upgrade" — inviting a "stale images / image bloat / docker
  vs containerd" theory. But both directories are an order of magnitude
  smaller than `/var/log/audit-debug`, and the disk-usage capture includes
  them precisely so you can compare and rule them out.
- **Compute contention.** The evictions and NotReady flaps suggest a hot node,
  but `fixtures/kubectl-top-node.txt` shows the affected node at 11% CPU and
  17% memory — and the node's own conditions in
  `fixtures/kubectl-describe-node.txt` say `MemoryPressure: False`. Evictions
  happen for ephemeral-storage pressure, which is a disk condition, not a
  compute one.

## Root cause

Disk pressure caused by a logging leak. The kube-system DaemonSet
`legacy-audit-agent` (`fixtures/daemonset-legacy-audit-agent.yaml`) writes
unrotated trace-level debug logs to the hostPath `/var/log/audit-debug`, which
has grown to 58G and pushed the node's OS disk to 95% used
(`fixtures/node-disk-usage.txt`). The kubelet reports `DiskPressure=True`,
taints the node with `node.kubernetes.io/disk-pressure:NoSchedule`, evicts pods
for low ephemeral-storage (`fixtures/kubectl-get-events.txt`), and flaps
`Ready: False` (`fixtures/kubectl-describe-node.txt`). The agent tolerates the
disk-pressure taint, so it survives its own evictions and keeps filling the
disk. CPU and memory are normal on every node (`fixtures/kubectl-top-node.txt`).

## Fix

Stop the leak at the source, then release the disk:

1. **Fix the DaemonSet:** remove the `/var/log/audit-debug` hostPath mount
   from `fixtures/daemonset-legacy-audit-agent.yaml`, or ship the agent with
   log rotation and a size cap (and drop `DEBUG_LOG_LEVEL: trace` in
   production) so debug output goes to a bounded location instead of the node's
   OS volume.
2. **Reclaim the disk on the affected node:** remove the accumulated files,
   e.g. `sudo rm -f /var/log/audit-debug/audit-debug-*.log` (or archive them
   off-node first, since this is audit evidence — see the compliance
   annotation on the DaemonSet).
3. **Recover the node:** once free space is back above the kubelet's eviction
   threshold, the `disk-pressure` taint clears; `kubectl uncordon
   aks-npuser-27489013-vmss000001` if it did not auto-clear, and let the
   evicted payments pods reschedule.

## Prevention

- **Block unbounded hostPath writes:** use an admission/OPA policy to reject
  workloads (especially in `kube-system`-adjacent compliance tooling) that
  mount a writable hostPath without a rotation/size guarantee — the same
  policy can flag `privileged: true` DaemonSets for owner review.
- **Alert on node disk and ephemeral storage:** alert when any node's OS disk
  exceeds ~85% or when `kubelet_volume_stats`/ephemeral-storage metrics climb
  toward the eviction threshold — minutes matter at a 1.7 G / 17 min write
  rate.
- **Watch the watcher:** exclude compliance agents from no-notice eviction
  only deliberately — tolerating `node.kubernetes.io/disk-pressure` let this
  agent keep writing while everything else was evicted. Audit
  `tolerations` on system DaemonSets.

## Further reading

- [Kubernetes: Resource management — node-level evictions](https://kubernetes.io/docs/concepts/scheduling-eviction/node-pressure-eviction/)
- [AKS: Node health and NotReady states](https://learn.microsoft.com/troubleshoot/azure/aks/availability-performance/node-not-ready-common-issues)
- [Kubernetes: Taints and tolerations](https://kubernetes.io/docs/concepts/scheduling-eviction/taint-and-toleration/)
