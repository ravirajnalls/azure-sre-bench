# aks-ops-022 — explainer

## How to investigate

This is a remediation task: the diagnosis ends in an actual manifest patch, and
grading verifies the patched `fixtures/storageclass.yaml` directly (the
`volumeBindingMode` must end up `WaitForFirstConsumer`).

Start where the symptom points — the scheduler's own words. The PVC is Bound,
nodes have spare capacity, and `fixtures/kubectl-describe-pod.txt` shows
`ledger-db-0` with `Scheduled: False` and a repeating event:
`0/4 nodes are available: 4 node(s) had volume node affinity conflict`. That is
a zone question, not a resource question: the scheduler is not saying "too much
CPU requested", it is saying the volume is anchored somewhere no node lives.

Find where the volume is anchored: `fixtures/kubectl-get-pv.yaml` shows the PV
`pvc-…0002` (claim `data-ledger-db-0`, phase Bound) with
`nodeAffinity.required` demanding
`topology.disk.csi.azure.com/zone In [eastus-3]`.

Check where the nodes actually are: `fixtures/kubectl-get-nodes-zones.txt` lists
every node in `eastus-1` or `eastus-2`, and
`fixtures/az-aks-nodepool-list.json` confirms both pools only span
availability zones `1` and `2`. Zone `eastus-3` exists on no node.

Finally, find out why the disk landed in the wrong zone:
`fixtures/storageclass.yaml` — the class named by the PV — declares
`volumeBindingMode: Immediate` with `zoned: "true"`. Immediate means the disk
is provisioned the moment the PVC exists, before any pod exists, so the CSI
provisioner picked a zone with no knowledge of where the pod would run.
`fixtures/kubectl-top-nodes.txt` closes the resource question: every node sits
at ≤13% CPU and ≤22% memory, so capacity was never the issue.

## Why the red herring misleads

The symptom notes "every node shows plenty of spare CPU and memory", but
`fixtures/kubectl-top-nodes.txt` and the four-node fleet still invite an
"insufficient resources / memory pressure" reading — a Pending pod plus a
capacity dashboard is a familiar pairing. It fails on the scheduler's own
message: `fixtures/kubectl-describe-pod.txt` reports zero failed-for-resources
events and only `volume node affinity conflict` — no "Insufficient cpu/memory"
phrase appears. The node pools in `fixtures/az-aks-nodepool-list.json` run
D-series VMs with 11 of 12 nodes idle-capacity and no node pool was changed;
the disk was created in a zone where the cluster has no nodes, and no amount of
spare CPU satisfies a node-affinity conflict.

## Root cause

The `managed-csi-premium-zonal` StorageClass uses
`volumeBindingMode: Immediate` (`fixtures/storageclass.yaml`), so the ledger-db
PVC's disk was provisioned immediately — before the pod was scheduled — and the
CSI driver placed it in `eastus-3`. The PV now carries a required node affinity
for `topology.disk.csi.azure.com/zone eastus-3` (`fixtures/kubectl-get-pv.yaml`),
but every node in the cluster runs in `eastus-1` or `eastus-2`
(`fixtures/kubectl-get-nodes-zones.txt`; `fixtures/az-aks-nodepool-list.json`),
so the scheduler rejects all four nodes with a volume node affinity conflict
(`fixtures/kubectl-describe-pod.txt`) and `ledger-db-0` stays Pending even
though the PVC is Bound and compute is idle (`fixtures/kubectl-top-nodes.txt`).

## Fix

Re-anchor the disk to the pod's zone by switching the binding mode and
recreating the claim:

1. **Patch the StorageClass** in `fixtures/storageclass.yaml` — the graded
   check reads exactly this field:

   ```yaml
   volumeBindingMode: WaitForFirstConsumer   # was: Immediate
   ```

   `WaitForFirstConsumer` defers disk creation until the first pod that uses it
   is scheduled, so the PV lands in the mounted pod's zone.

2. **Delete the Bound PVC** `data-ledger-db-0` (and the StatefulSet recreates
   it) — the existing PV's `eastus-3` affinity is baked in and cannot be
   edited; under `reclaimPolicy: Delete` the disk is released and re-provisioned
   in the correct zone.

3. **Let the StatefulSet recreate `ledger-db-0`**; alternatively (or
   additionally) add zone `3` to a node pool in
   `fixtures/az-aks-nodepool-list.json` so an `eastus-3` disk could actually be
   mounted — but the binding-mode fix is the root-cause correction.

## Prevention

- **Default StorageClasses to `WaitForFirstConsumer`:** for zoned AKS disks,
  `Immediate` only makes sense when provisioning ahead of a known zone; make
  `WaitForFirstConsumer` the org default so zone placement follows scheduling.
- **Guard zoned disks against zone-less pools:** a CI/pre-deploy check that a
  zoned StorageClass's allowed zones intersect the target node pools'
  `availabilityZones` (here: class allowed any zone, pools span 1–2 only).
- **Alert on `volume node affinity conflict` scheduling events** — this exact
  message indicates a placement mismatch that will never self-heal, and it
  surfaces within minutes of a StatefulSet rollout.

## Further reading

- [Kubernetes: Storage Classes — VolumeBindingMode](https://kubernetes.io/docs/concepts/storage/storage-classes/#volume-binding-mode)
- [AKS: Availability zones for AKS clusters](https://learn.microsoft.com/azure/aks/availability-zones)
- [Azure Disks CSI driver: zoned disks](https://github.com/kubernetes-sigs/azuredisk-csi-driver)
