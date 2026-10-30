# aks-ops-056 — explainer

## How to investigate

Read the pod's events before touching the pipeline: the pod never got as
far as pulling. `fixtures/kubectl-describe-pod.txt:13` shows
`FailedCreate` from the **deployment-controller** with an admission
webhook denial — `denypolicy.gateddeployment.security.microsoft.com`
denied the request, naming image `billing-ui:v2026.10.30-rc4` and gate
`security-vulnerability-gate`. An admission denial happens in the API
server before any scheduling or image fetch, so the cluster never even
tried to pull the image. The pipeline re-runs change nothing because the
pipeline is not the failing component.

Connect the gate to its data source: the webhook is Defender for
Containers' gated deployment policy, so read Defender's findings.
`fixtures/defender-vulnerability-findings.json:5-14` shows the new tag
`v2026.10.30-rc4` carrying `CVE-2026-33412`, classification **Critical**,
CVSS 9.8, first observed 06:12 — three minutes before the 06:15 rollout
stuck. The comparison finding is lines 16-21: the currently serving
`v2026.10.23-rc2` has only a **Low** finding, which is why old replicas
run and new ones are denied. Close the loop on *why* this release has a
critical finding when the app did not change: `fixtures/Dockerfile.diff`
shows the only diff is the base-image digest — a vendor security rebuild
of `ubuntu:24.04` that itself bundles a vulnerable OpenSSL.

## Why the red herring misleads

The registry is the reflex target: a stuck rollout plus a private registry
reads as a pull problem, and `fixtures/az-acr-show.json` looks like the
place to look. It clears on every axis — Premium SKU, `defaultAction:
Allow` with no blocking network rules, healthy private endpoint, and no
quarantine policy. More decisively, the describe output at
`fixtures/kubectl-describe-pod.txt:14` says `Container image already
present on machine` — the image is cached on the node, and the pod still
never starts, so nothing about transport or the registry can explain the
denial. The admission webhook denied pod *creation*; a registry problem
produces `ImagePullBackOff` on *running* pods, a different event from a
different actor. (This is also why re-running the release pipeline could
never help — it re-pushes an image the policy will deny again.)

## Root cause

Defender for Containers' gated deployment policy blocks the new image:
`fixtures/defender-vulnerability-findings.json:5-14` reports critical
`CVE-2026-33412` (CVSS 9.8) on `v2026.10.30-rc4`, introduced by this
release's base-image digest bump (`fixtures/Dockerfile.diff`), and the
Defender admission webhook denies every ReplicaSet that references the
tag (`fixtures/kubectl-describe-pod.txt:13`). Old replicas on
`v2026.10.23-rc2` predate the vulnerable digest, so they keep serving
while the rollout is stuck.

## Fix

Rebuild on a patched base and roll that out:

```bash
docker build --pull -t retailacr000a.azurecr.io/billing-ui:v2026.10.30-rc5 apps/billing-ui
docker push retailacr000a.azurecr.io/billing-ui:v2026.10.30-rc5
# confirm the Critical finding clears for the new digest, then:
kubectl -n billing set image deployment/billing-ui billing-ui=retailacr000a.azurecr.io/billing-ui:v2026.10.30-rc5
```

The vendor's fixed base rebuild patches CVE-2026-33412, the Defender
finding for the new digest clears, and the gate admits the rollout —
the correct, permanent resolution. If the release genuinely cannot wait
for the rebuild, the fast path is a **scoped, time-boxed exemption** for
the specific image digest in the Defender gated deployment policy, with
the rebuild tracked to a deadline — never a policy-wide disable, which
would un-gate every workload in the cluster.

## Prevention

- **Scan before promote:** run Defender vulnerability assessment (or an
  equivalent scanner gate) as a pipeline stage before the image tag is
  promoted to the release manifest — the 06:12 finding predates the 06:15
  rollout and would have stopped it in CI instead of in the cluster.
- **Pin and update base images deliberately:** rebuild-and-rescan on
  every base-image digest change (the bump in `fixtures/Dockerfile.diff`
  shipped unscanned); keep a feed of vendor fixed digests so app images
  inherit patched bases automatically.
- **Exemption hygiene:** if policy exemptions are used, require an
  expiry and an owner on each one, and alert on any exemption that
  outlives its rebuild ticket.

## Further reading

- [Microsoft Defender for Containers — gated deployment policies](https://learn.microsoft.com/azure/defender-for-cloud/gated-deployment)
- [Defender for Containers vulnerability assessment and image findings](https://learn.microsoft.com/azure/defender-for-cloud/container-image-assessment)
- [Kubernetes admission webhooks](https://kubernetes.io/docs/reference/access-authn-authz/extensible-admission-controllers/)
- [Azure Container Registry content trust and image quarantine](https://learn.microsoft.com/azure/container-registry/container-registry-content-trust)
