# aks-ops-045 — explainer

## How to investigate

The symptom is an admission denial during release, not an image build failure —
the pipeline log proves the build and push succeeded before the failure. Read
`fixtures/pipeline-log.txt` first: the `kubectl apply` line is the only failure,
and it names the mechanism — `Admission webhook "validation.gatekeeper.sh"
denied the request` and `Denied by constraint "k8sazurev2containerallowedimages"
assigned by Azure Policy`.

That constraint is a Gatekeeper constraint, so read it directly:
`fixtures/kubectl-get-constraints.txt` shows `k8sazurev2containerallowedimages`
in `deny` mode with 3 violations in the `reporting` namespace. The decisive
pairing: `fixtures/deployment.yaml` deploys
`docker.io/contoso/reporting-api:1.4.2`, while
`fixtures/k8sazurev2containerallowedimages.yaml` allows only
`benchacr0001.azurecr.io` and `mcr.microsoft.com` in its `allowedRepoRegexes`.
`docker.io/contoso/...` matches no regex — the deny is deterministic, not
flaky. `fixtures/gatekeeper-audit.log` corroborates with audit-time violations
quoting the exact image.

## Why the red herring misleads

`fixtures/az-role-assignment-list.json` invites an RBAC theory: the error above
the webhook noise says `Forbidden`, and "forbidden" reads like a permissions
problem. But the denial comes from `validation.gatekeeper.sh`, not from
Kubernetes RBAC — and the role assignments for the release service principal
`sp-reporting-release` are all present and healthy: AcrPull on the registry,
Key Vault Secrets User on the vault, AKS Cluster User on the cluster. RBAC
controls what the *caller* may do; a Gatekeeper constraint denies the *object*
regardless of who submits it. The same manifest also worked on staging — which
differently has no policy assignment — reinforcing that identity was never the
variable.

## Root cause

Azure Policy (Gatekeeper) denies the deployment. The assignment
"Kubernetes cluster containers should only use allowed images" is enforced
with `enforcementAction: deny`
(`fixtures/k8sazurev2containerallowedimages.yaml`), and its
`allowedRepoRegexes` admit only `benchacr0001.azurecr.io` and
`mcr.microsoft.com`. The pipeline deploys
`docker.io/contoso/reporting-api:1.4.2`
(`fixtures/deployment.yaml`), so the `validation.gatekeeper.sh` webhook rejects
the apply (`fixtures/pipeline-log.txt`), and the audit log records the
violations (`fixtures/gatekeeper-audit.log`).

## Fix

Get the image into an approved registry — pick one:

1. **Import into ACR (fastest unblock):**
   `az acr import --registry benchacr0001 --source docker.io/contoso/reporting-api:1.4.2 -n reporting-api:1.4.2`,
   then update `fixtures/deployment.yaml` to
   `benchacr0001.azurecr.io/contoso/reporting-api:1.4.2` and re-run the
   pipeline.
2. **Change the pipeline to build and push to ACR directly** so release images
   never leave the approved registry.
3. **Ask the platform team to widen the policy** (add a `docker.io/contoso`
   regex to `allowedRepoRegexes`) only if Docker Hub is genuinely an approved
   source for this cluster — that is a governance decision, not a pipeline
   change.

## Prevention

- **Publish the allowed-registry list to developers** and to pipeline
  templates, so release images target an approved registry from the start
  instead of discovering the policy at deploy time.
- **Test policy in audit mode first**: new Azure Policy assignments should
  start with `enforcementAction: audit`, so violations surface as
  `kubectl get constraints` warnings before anything is denied in production.
- **Make the deny readable**: an Azure Policy assignment message links the
  constraint; a small runbook or pipeline step that runs
  `kubectl get k8sazurev2containerallowedimages -o yaml` on failure turns the
  wall of JSON into the two lines that matter (allowed regexes vs actual image).

## Further reading

- [AKS: Azure Policy for Kubernetes (Gatekeeper)](https://learn.microsoft.com/azure/governance/policy/concepts/policy-for-kubernetes)
- [Azure Policy built-ins: Kubernetes cluster containers should only use allowed images](https://learn.microsoft.com/azure/governance/policy/samples/built-in-policy#kubernetes)
- [Azure Container Registry: az acr import](https://learn.microsoft.com/azure/container-registry/container-registry-import-images)
