# aks-identity-008 — explainer

## How to investigate

Start where the failure is visible: `fixtures/kubectl-logs-orders-api.txt`. The
pod starts fine and passes probes, but every Key Vault read dies with
`AADSTS70021: No matching federated identity record found for presented
assertion` — and the log names the subject the pod presented:
`system:serviceaccount:orders:orders-api`. That is a workload-identity token
exchange failure, not a network or Key Vault availability problem.

Next check what the identity actually trusts: `fixtures/az-identity-federated-credential-list.json`.
The federated identity credential `fic-orders-api-default` has the correct
issuer but its subject is `system:serviceaccount:default:orders-api` — the
**old** namespace. The pod presents `orders`, the trust record pins `default`;
no assertion can ever match.

Then confirm the pod side is configured correctly so you do not chase a ghost:
`fixtures/kubectl-get-sa-orders-api.yaml` shows the `orders` namespace
ServiceAccount annotated with the right client-id, and
`fixtures/kubectl-get-deploy-orders-api.yaml` shows the deployment using that
ServiceAccount with the workload-identity labels intact.

## Why the red herring misleads

The 500s on secret reads make "Key Vault denies the identity" feel right, and
`fixtures/az-role-assignment-list.json` feeds that instinct by listing
role assignments on `kv-orders-prod` — it is genuinely tempting to conclude a
missing `Key Vault Secrets User` role. But the file shows the role **is**
assigned to `id-orders-api-prod`, and the failure is not a Key Vault 403: it is
`AADSTS70021` at token exchange, before any vault authorization runs. RBAC on
the vault never gets a chance to matter when the identity proof itself fails.

## Root cause

The federated identity credential on `id-orders-api-prod` still pins the
subject to the old namespace — `system:serviceaccount:default:orders-api` —
while the moved ServiceAccount presents `system:serviceaccount:orders:orders-api`
(`fixtures/az-identity-federated-credential-list.json` against the AADSTS70021
assertion in `fixtures/kubectl-logs-orders-api.txt`). The namespace move
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
federated credential subject in the same change window. A pre-deploy check (or
policy, e.g. Azure Policy / Kyverno) that compares the ServiceAccount namespace
against the `system:serviceaccount:` subject of every federated credential on
the referenced identity catches this before the first restart.

## Further reading

- [Azure AD Workload Identity — federation with external identity providers](https://learn.microsoft.com/entra/workload-id/workload-identity-federation)
- [AADSTS error AADSTS70021 — no matching federated identity record](https://learn.microsoft.com/entra/identity-platform/reference-error-codes)
- [Use the Azure Key Vault Secrets provider with workload identity on AKS](https://learn.microsoft.com/azure/aks/csi-secrets-store-identity)
