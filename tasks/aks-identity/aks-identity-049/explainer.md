# aks-identity-049 — explainer

## How to investigate

Read the error's shape before suspecting the network: "PermissionError:
[Errno 13]" from `open()` is an operating-system permission failure, not a
timeout, not an IOException from a lost mount, and not an access-denied from
a blob endpoint. `fixtures/media-processor.log:3-6` makes the split sharp —
the job **reads** `/mnt/media/inbound/clip_8841.mov` fine at 03:00:22 and
**writes** fail at 03:00:41, for every job, starting with the storage
hardening change. Read-yes/write-no on a mounted POSIX-ish filesystem means
the process is running without the write bit: look at ownership and mode.

Find what set them. `fixtures/deployment.yaml:17-20` shows the pod's
`securityContext` — `runAsUser: 1000, runAsGroup: 1000`, with `fsGroup: 1000`
on line 21 — so the process is a non-root uid. Then read the StorageClass
the PVC uses (`fixtures/kubectl-describe-pvc.txt:3` names
`azure-file-media`): `fixtures/storageclass.yaml:11-15` shows the
hardening change added `mountOptions: uid=0, gid=0, file_mode=0755,
dir_mode=0755`. The azure-file CSI driver passes these straight to the SMB
mount, which means **every file on the share is owned by uid 0 (root)** and
mode 0755 grants group/other only `r-x`. uid 1000 can list and read but has
no write bit anywhere — exactly the observed symptom. (The comment at
`fixtures/deployment.yaml:29-30` explains why `fsGroup: 1000` does not save
you: the azure-file CSI driver does not apply fsGroup ownership changes to
SMB mounts; the StorageClass mountOptions win.)

## Why the red herring misleads

The storage-account firewall is the reflex: "permission denied" plus a
recent *storage* hardening change sends most people to network controls.
`fixtures/az-storage-account-show.json` defuses it in four lines —
`networkRuleSet.defaultAction: Allow`, empty `virtualNetworkRules` and
`ipRules`, and `bypass: AzureServices` — and the pod's volume **mounted**
and reads successfully, which a firewall drop could not permit. A network
failure also produces timeouts and connection resets in the log, not clean
`Errno 13` from the writer. The second weak signal is
`fixtures/kubectl-describe-pvc.txt`: a freshly `Bound` PVC might suggest a
provisioning problem, but `ProvisioningSucceeded` plus a working read side
shows provisioning and mounting are fine — only the mode bits are wrong.

## Root cause

The 2026-10-28 hardening change added `mountOptions` to StorageClass
`azure-file-media` pinning `uid=0, gid=0, file_mode=0755, dir_mode=0755`
(`fixtures/storageclass.yaml:11-15`), making all files on the share
root-owned and non-writable by others. media-processor runs as uid 1000
(`fixtures/deployment.yaml:17-20`) and the azure-file CSI driver does not
apply `fsGroup` to SMB mounts, so its writes to `/mnt/media` get Errno 13
(`fixtures/media-processor.log:4`) while its reads succeed — the 0755 mode
grants read/execute to everyone, write to root only.

## Fix

Give the workload's uid the ownership and write bits, in the StorageClass
mountOptions:

```yaml
mountOptions:
  - uid=1000
  - gid=1000
  - file_mode=0777   # or 0775 with a shared group
  - dir_mode=0777    # or 0775
  - mfsymlinks
  - cache=strict
  - noserverino
```

Then delete and recreate the PVC (or roll the pods after re-creating the
PV) — SMB mount options are applied at mount time, so existing mounts keep
the old modes until they are remounted. The alternative shape is to drop
the `uid`/`gid` options and let the pod's `fsGroup` own the files where the
driver supports it. Both are safe, reversible manifest changes; nothing on
the storage account or the cluster changes. Do not "fix" it by running the
container as root — that trades a mount-option bug for a pod-security
violation.

## Prevention

- **Pair-runAsUser check:** lint any StorageClass that sets `mountOptions:
  uid=...` against the workloads that mount it and fail CI when no
  workload's `runAsUser` matches — the hardening change would have been
  rejected before merge.
- **Write-probe in CI:** stage a canary pod (same image, same
  securityContext) that mounts the class and touches a file in the target
  directory before rollout completes; Errno 13 fails the deploy in
  seconds instead of in production transcoding.
- **Hardening review checklist:** require that "hardening" changes to
  storage classes list every consuming workload and its uid, since mount
  options change effective permissions for all of them at once.

## Further reading

- [Azure Files CSI driver mount options — uid, gid, file_mode, dir_mode](https://learn.microsoft.com/azure/storage/files/storage-files-identity-authentication?toc=%2Fazure%2Fstorage%2Fblobs%2Ftoc.json&bc=%2Fazure%2Fstorage%2Fblobs%2Fbreadcrumb%2Ftoc.json)
- [Dynamically provision an Azure Files volume with the CSI driver on AKS](https://learn.microsoft.com/azure/aks/azure-files-dynamic-pv)
- [Configure a SecurityContext for a pod — fsGroup](https://kubernetes.io/docs/tasks/configure-pod-container/security-context/)
- [csi-driver-azure-file — known limitations on fsGroup with SMB](https://github.com/kubernetes-sigs/azurefile-csi-driver/blob/master/docs/known-issues.md)
