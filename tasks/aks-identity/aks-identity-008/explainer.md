# aks-identity-008 — explainer

## How to investigate

Start where the failure is visible: `fixtures/kubectl-logs-orders-api.txt`.
The pod starts fine and passes probes (lines 2–6), but every Key Vault read
dies with `AADSTS70021: No matching federated identity record found for presented
assertion` — line 10 names the error and line 12 names the subject the pod
presented: `system:serviceaccount:orders:orders-api`. That is a workload-identity token
exchange failure, not a network or Key Vault availability problem.

Next check what the identity actually trusts: `fixtures/az-identity-federated-credential-list.json`.
The federated identity credential `fic-orders-api-default` has the correct
issuer (line 7 matches the cluster's OIDC issuer URL, `fixtures/az-aks-show.json:41`)
but its subject on line 10 is `system:serviceaccount:default:orders-api` — the
**old** namespace. The pod presents `orders` (line 12 of the log), the trust record pins `default`;
no assertion can ever match.

Then confirm the pod side is configured correctly so you do not chase a ghost:
`fixtures/kubectl-get-sa-orders-api.yaml` shows the `orders` namespace
ServiceAccount annotated with the right client-id (line 10), and
`fixtures/kubectl-get-deploy-orders-api.yaml` shows the deployment using that
ServiceAccount (line 27) with the workload-identity label intact (line 25) —
plus the change-cause on line 12 naming the namespace move (PR ORD-2914).
And clear the RBAC reflex in passing: `fixtures/az-role-assignment-list.json:12-13`
shows `Key Vault Secrets User` is already assigned to the identity on
`kv-orders-prod`.

## Why the red herring misleads

The 500s on secret reads make "Key Vault denies the identity" feel right, and
`fixtures/az-role-assignment-list.json` feeds that instinct by listing
role assignments on `kv-orders-prod` — it is genuinely tempting to conclude a
missing `Key Vault Secrets User` role. But the file shows the role **is**
assigned to the identity (lines 12–13), and the failure is not a Key Vault 403: it is
`AADSTS70021` at token exchange (`fixtures/kubectl-logs-orders-api.txt:10`), before any vault authorization runs. RBAC on
the vault never gets a chance to matter when the identity proof itself fails.

## Root cause

The federated identity credential on `id-orders-api-prod` still pins the
subject to the old namespace — `system:serviceaccount:default:orders-api`
(`fixtures/az-identity-federated-credential-list.json:10`) —
while the moved ServiceAccount presents `system:serviceaccount:orders:orders-api`
(the AADSTS70021 assertion in `fixtures/kubectl-logs-orders-api.txt:12`). The namespace move
(`fixtures/kubectl-get-deploy-orders-api.yaml:12`)
invalidated the workload-identity trust binding.

## Fix

Update the federated credential subject to the new namespace, or add a new
federated credential for it:

```bash
az identity federated-credential update \
  --name fic-orders-api-default \
  --identity-name id-orders-api-prod \
  --resource-group rg-orders-prod \
  --subject system:serviceaccount:orders:orders-api
```

Safe and reversible: the change only widens which assertions the identity
accepts, takes effect on the next token exchange (restart the pods to retry
immediately), and the old subject can be kept during a migration window.

## Prevention

Treat a workload-identity trust binding as part of the namespace's identity,
not the workload's: whenever a ServiceAccount moves namespaces, update the
federated credential subject in the same change window — the namespace move
in `fixtures/kubectl-get-deploy-orders-api.yaml:12` shipped without one. A pre-deploy check (or
policy, e.g. Azure Policy / Kyverno) that compares the ServiceAccount namespace
against the `system:serviceaccount:` subject of every federated credential on
the referenced identity catches this before the first restart.

## Further reading

- [Azure AD Workload Identity — federation with external identity providers](https://learn.microsoft.com/entra/workload-id/workload-identity-federation)
- [AADSTS error AADSTS70021 — no matching federated identity record](https://learn.microsoft.com/entra/identity-platform/reference-error-codes)
- [Use the Azure Key Vault Secrets provider with workload identity on AKS](https://learn.microsoft.com/azure/aks/csi-secrets-store-identity)
