# aks-identity-009 — explainer

## How to investigate

Start with the kubelet's own account of the failure: `kubectl describe` on a
stuck pod. `fixtures/kubectl-describe-pod.txt:41` is the decisive event —
`Failed to pull image "acrcontosoprod.azurecr.io/payments-api:2.7.1" ...
unexpected status 401 Unauthorized ... unauthorized: authentication required`.
A 401 is an *authentication/authorization* failure: the registry heard the
request and refused the credentials, so this is an identity problem, not a
network, DNS, or registry-availability problem. (Line 42–43 confirm the
kubelet is backing off repeatedly — persistent, not a blip.)

Rule out the boring explanations before chasing roles. The tag: `az acr
repository show-tags` on `payments-api` — `fixtures/az-acr-repository-show-tags.json:9-12`
— lists `2.7.1` created `2026-10-20T14:33:02Z`, so the image exists and the
reference is right. The registry: `fixtures/az-acr-show.json:44-47` shows
`displayStatus: Healthy` at 06:24Z, minutes after the failures started.
Neither missing-image nor registry-outage survives these two files.

Now check who is authorized to pull. The cluster's kubelet identity is in
`fixtures/az-aks-show.json:31-36`: `identityProfile.kubeletidentity.objectId`
is `00000000-0000-0000-0000-00000000000c`, and line 22 shows the cluster was
**created 2026-10-29T23:41:18Z** — last night, matching the "recreated" in
the symptom. The registry's role assignments are in
`fixtures/az-role-assignment-list-acr.json:5-11`: exactly one `AcrPull`
assignment, `principalId 00000000-0000-0000-0000-000000000009`, created
2026-06-11. That principal is not the current kubelet identity — it is the
kubelet identity of the *deleted* cluster. The new identity
(`...000c`) authenticates fine but is authorized for nothing, which is what
a 401 from ACR means for a managed identity. Old cluster's grant, new
cluster's kubelet.

## Why the red herring misleads

`fixtures/az-acr-show.json:2` shows `adminUserEnabled: false`, and "the admin
user is disabled" is a tempting quick fix story — but kubelet image pulls
never use the admin user on a normal AKS cluster; they use the kubelet
managed identity. The admin flag being off is configuration hygiene, not the
cause, and enabling it would be the wrong fix (credential-based auth where
identity-based is the design). The second decoy is the error text itself:
`ImagePullBackOff` plus a 401 makes "the image is missing" feel plausible,
and 401s are also what ACR returns to unauthenticated *anonymous* probes.
Both dissolve the same way: `fixtures/az-acr-repository-show-tags.json:12`
proves the tag exists, and the 401 arrives on a token request
(`fixtures/kubectl-describe-pod.txt:41`,
`oauth2/token?scope=repository%3Apayments-api%3Apull`) — a scope grant
refusal for a specific identity, not a generic reachability failure.

## Root cause

The recreated cluster's kubelet identity
(`00000000-0000-0000-0000-00000000000c`, created with the cluster 2026-10-29,
`fixtures/az-aks-show.json:22,34`) has no `AcrPull` role on
`acrcontosoprod`: the only AcrPull assignment on the registry
(`fixtures/az-role-assignment-list-acr.json:5-11`) points at principal
`00000000-0000-0000-0000-000000000009` — the kubelet identity of the deleted
cluster. Every pull from ACR therefore returns 401 Unauthorized
(`fixtures/kubectl-describe-pod.txt:41`).

## Fix

Grant the new kubelet identity AcrPull on the registry. The one-liner:

```bash
az aks update --resource-group rg-contoso-prod --name aks-contoso-prod \
  --attach-acr acrcontosoprod
```

or manually, against the objectId from `fixtures/az-aks-show.json:34`:

```bash
az role assignment create \
  --assignee-object-id 00000000-0000-0000-0000-00000000000c \
  --assignee-principal-type ServicePrincipal \
  --role AcrPull \
  --scope /subscriptions/00000000-0000-0000-0000-000000000002/resourcegroups/rg-contoso-prod/providers/Microsoft.ContainerRegistry/registries/acrcontosoprod
```

Role assignments propagate within a few minutes; the kubelet's image-pull
backoff retries on its own schedule (or delete the pods to force an
immediate retry). Safe and additive: it grants read-only pull rights to one
identity on one registry, and no existing assignment changes.

## Prevention

- **Attach ACR in the same IaC change that creates the cluster.** The
  recreate (network migration) rebuilt the cluster but not the role binding.
  Guardrail: the Terraform/Bicep AKS module should depend on the
  `--attach-acr` equivalent (a `Microsoft.Authorization/roleAssignments`
  resource scoped to the registry, keyed to the kubelet identity), so a
  cluster recreation automatically re-establishes the grant — and a CI
  `az role assignment list` assertion fails the pipeline when the kubelet
  identity has no AcrPull on the registries its workloads use.
- **Canary pull after any cluster lifecycle event:** a post-provision smoke
  test that runs `kubectl run` of a dummy pod pulling one ACR image — a 401
  on a fresh cluster in CI fails the deploy, not the morning image pulls.

## Further reading

- [Authenticate with a managed identity for image pulls (AKS)](https://learn.microsoft.com/azure/aks/use-managed-identity)
- [az aks update --attach-acr reference](https://learn.microsoft.com/cli/azure/aks#az-aks-update)
- [ACR authentication errors — troubleshoot 401 Unauthorized](https://learn.microsoft.com/azure/container-registry/troubleshoot-login)
