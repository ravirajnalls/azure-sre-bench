# aks-workloads-013 — explainer

## How to investigate

Start with the restart forensics in `fixtures/kubectl-describe-pod.txt`:
`Last State: Terminated, Reason: OOMKilled, Exit Code: 137, Restart Count: 14`.
Exit code 137 under the OOMKilled reason is the kernel's OOM killer terminating
the process because the container exceeded its memory limit — this is not an
application crash.

Check what that limit is versus what the process is configured to use:
`fixtures/deployment.yaml` sets `resources.limits.memory: 512Mi`, but the same
container passes `JAVA_TOOL_OPTIONS: -Xms256m -Xmx768m` — the JVM's maximum
heap alone is 1.5× the container limit, before counting any off-heap memory.
`fixtures/kubectl-top-pod.txt` shows both indexer pods pinned at 511Mi / 510Mi
against the 512Mi cap: they ride the ceiling, and peak-load indexing pushes
them over.

Correlate the death to be sure: `fixtures/search-indexer.log` ends cleanly at
`09:40:56,112 — Batch 4445 committed` with no error or warning, and
`fixtures/kubectl-describe-pod.txt` shows the previous container's OOMKilled
`Finished: 09:40:56`. The log doesn't report the crash because the process
never got the chance to — that instant log-stop is the OOM signature.

## Why the red herring misleads

Two things pull attention away. "Node CPU graphs spike in the same window"
suggests node-level pressure, but `fixtures/node-cpu-kql.csv` peaks around 68%
— busy, not contended — and only search-indexer restarts while every other
service rides through the same peak; the CPU spikes are the peak traffic doing
its job. And "no errors in the application logs" reads as "the app didn't
fail", when in fact it is evidence *for* the OOM story: SIGKILL gives the JVM
no chance to log anything, so a healthy log that stops mid-line is exactly what
an OOM kill leaves behind.

## Root cause

The container's memory limit (512Mi in `fixtures/deployment.yaml`) is smaller
than the JVM's configured maximum heap (`-Xmx768m` via `JAVA_TOOL_OPTIONS`).
During peak indexing the heap grows past what the cgroup allows and the kernel
kills the process — OOMKilled, exit code 137, Restart Count 14 in
`fixtures/kubectl-describe-pod.txt` — with memory pinned at the limit in
`fixtures/kubectl-top-pod.txt` and a clean log that simply stops at the kill
instant (`fixtures/search-indexer.log`).

## Fix

Make the container limit larger than the heap plus off-heap overhead — raise
the limit to 1Gi and keep `-Xmx768m`, leaving ~25% headroom for metaspace,
threads, and direct buffers:

```yaml
resources:
  limits:
    memory: 1Gi
```

The alternative — capping the heap to fit the existing limit
(`-Xmx384m`) — works but costs indexer throughput. The limit raise is the
safer direction here: the process was truncated while healthy, not leaking.

## Prevention

Keep JVM heap sizing and container limits in one decision: heap max plus
~25–50% off-heap headroom must fit the container limit. Prefer
`-XX:MaxRAMPercentage` (e.g. 75) over a fixed `-Xmx` so the JVM derives its
heap from the cgroup limit automatically and a future limit change cannot
re-introduce the mismatch. Alert when a pod's memory sits within a few percent
of its limit — pinned-at-the-ceiling is the early-warning state for the next
OOM kill.

## Further reading

- [Assign memory resources to containers and pods](https://kubernetes.io/docs/tasks/configure-pod-container/assign-memory-resource/)
- [Resource management for pods and containers](https://kubernetes.io/docs/concepts/configuration/manage-resources-containers/)
