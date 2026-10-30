# aks-workloads-054 — explainer

## How to investigate

The failing unit is a batch job that has not changed, so the first question is
what the *pull* itself is saying. `fixtures/kubectl-describe-pod.txt` is
unusually explicit: the error is `manifest unknown: manifest unknown` on
`finance/ledger-reconcile@sha256:a3f9c2e1...` — not `unauthorized`, not
`authentication required`, not a DNS or proxy failure. "Manifest unknown"
means the registry answered, the caller was allowed to ask, and the registry
has never seen (or no longer has) that exact content. When the image
reference is a digest, that means the digest is gone from the repository.

Where did it go? `fixtures/cronjob.yaml` confirms the CronJob pins the image
by digest rather than by tag — someone chose digests deliberately, which is
normally the *safe* choice precisely because digests are immutable. Unless
someone deletes them.

The registry hygiene program is the suspect. `fixtures/acr-task-purge.yaml`
shows a scheduled `acr purge` created 2026-10-20 that runs every Sunday at
03:00 over `finance/ledger-reconcile` with `--untagged --ago 30d` — delete
untagged manifests older than 30 days. Check what survives in
`fixtures/az-acr-repository-show-manifests.json`: the repository holds
`sha256:c7e2...` (tagged `prod`, created **2026-10-23**) and `sha256:e1b0...`
(tagged `v2.4.1`). The digest the CronJob pins is **absent**. Reconstructing
the timeline: the `prod` tag was re-pushed on 2026-10-23, which left the old
build (`a3f9...`) untagged; the first purge run on Sunday 2026-10-25 03:00
deleted it as untagged-and-old; the next nightly CronJob run after that
started failing — matching the symptom's "past week".

## Why the red herring misleads

`fixtures/az-role-assignment-list-acr.json` shows the kubelet identity holding
`AcrPull` on `crfinprod`, and it is the natural first check when pulls fail —
most ErrImagePulls *are* credential problems. But three signals point away
from it: the describe output shows no `unauthorized`/`authentication
required` error, only `manifest unknown`; the failure is exclusive to one
image reference while every other workload keeps pulling from the same
registry on the same credentials; and a missing role would break *all* images
from `crfinprod`, not one digest. Credentials gate who may pull; they do not
selectively delete one manifest's existence.

## Root cause

The CronJob pins `crfinprod.azurecr.io/finance/ledger-reconcile` by digest
(`sha256:a3f9c2e1...`, `fixtures/cronjob.yaml`). When `prod` was re-pushed on
2026-10-23 that digest lost its tag, and the weekly purge task
(`acr purge --untagged --ago 30d`, `fixtures/acr-task-purge.yaml`) deleted it
in its first run on 2026-10-25. The referenced digest no longer exists in the
repository (`fixtures/az-acr-repository-show-manifests.json` shows only newer,
differently-hashed manifests), so every pull resolves to `manifest unknown`
(`fixtures/kubectl-describe-pod.txt`) and the nightly job never starts. Pull
credentials were never involved — `AcrPull` on the node identity is intact.

## Fix

Get the job running again, then make the cleanup unable to repeat the
accident:

1. Re-push a current build and tag it:

   ```bash
   docker build -t crfinprod.azurecr.io/finance/ledger-reconcile:prod .
   docker push crfinprod.azurecr.io/finance/ledger-reconcile:prod
   ```

2. Move the CronJob to the tag instead of the deleted digest:

   ```yaml
   image: crfinprod.azurecr.io/finance/ledger-reconcile:prod
   ```

3. Reconcile the purge rule with reality — either add `--keep-tags` /
   drop `--untagged` so tagged manifests are never deleted, or exclude the
   tags the CronJob pins from the filter. If digest pinning must stay, the
   purge scope must know the pinned digest and never match it.

After step 2, the next scheduled run pulls successfully; `kubectl get jobs -n
finance` shows a fresh completed `ledger-reconcile-*` job that night.

## Prevention

- **Purge tasks must have a keep-list.** Any scheduled deletion job over a
  registry repository should require an explicit exclusion set (tags or
  digests that live workloads reference). Review the purge task together
  with the manifests that consume the repository — here, a simple grep of
  cluster manifests for `@sha256:` against the purge filter would have
  flagged the collision before the first Sunday run.
- **Tag the image the cluster runs.** Digest pinning in a CronJob means
  nothing re-pushes over it, but the *opposite* hazard applies: the digest
  survives only as long as some tag (or retention rule) anchors it. When a
  build is promoted, tag it and record the digest→tag mapping in the
  deployment notes.
- **Alert on pull failures by image, not just by pod.** A single
  `manifest unknown` on a nightly job should page after one occurrence — the
  job ran silently broken for a week.

## Further reading

- [Azure Container Registry — purge task](https://learn.microsoft.com/azure/container-registry/container-registry-auto-purge)
- [acr purge command reference](https://github.com/Azure/acr/blob/main/docs/acr-purge.md)
- [Pinning images by digest in Kubernetes](https://kubernetes.io/docs/concepts/containers/images/)
- [AKS — image pull errors](https://learn.microsoft.com/troubleshoot/azure/containers/aks/availability-performance/image-pull-errors)
