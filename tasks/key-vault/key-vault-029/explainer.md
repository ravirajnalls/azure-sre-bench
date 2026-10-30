# key-vault-029 — explainer

## How to investigate

Start at the error, which already names its class: `fixtures/function.log`.
Every `ReportGen` run fetches `reporting-db-conn` from `kv-reports` and gets
`Operation returned an invalid status code 'Forbidden'` with the inner
error code `ForbiddenByRbac` — and the log even links the Azure RBAC
permission model doc. Two more lines rule out identity plumbing: the token
acquisition succeeded (`Managed identity token acquired successfully …
principal id 00000000-0000-0000-0000-000000000003`), and the failure is
instant and deterministic, not throttling. This is an authorization denial
under the RBAC model, not a network or authentication problem.

Now look at the vault's permission model: `fixtures/az-keyvault-show.json`
shows `enableRbacAuthorization: true` on `kv-reports`. That is the pivotal
fact — under the RBAC permission model, **access policies on the vault are
not evaluated at all**. The `accessPolicyEntries` you can see in the same
JSON are inert configuration.

Confirm the policy is exactly the mirage it appears to be:
`fixtures/az-keyvault-access-policy.json` shows the vault's access policy
granting objectId `…0003` (the function app) `Get` and `List` on secrets —
perfect-looking, and completely ignored. Then check the RBAC side:
`fixtures/az-role-assignment-list.json` shows only one role assignment on
the vault scope — `Key Vault Administrator` for a **User** principal
(`…0005`). The function app's managed identity (objectId `…0003`) has no
role assignment anywhere on the vault.

Verify the principal matches what the log presented:
`fixtures/az-functionapp-identity-show.json` shows func-reports-gen's
SystemAssigned identity is principalId `00000000-0000-0000-0000-000000000003`
— the same id the log shows being denied. The identity is real; it just
has no RBAC role on the vault.

## Why the red herring misleads

The symptom statement hands you the trap: "the vault has an access policy
that grants the function app's managed identity Get and List on secrets."
`fixtures/az-keyvault-access-policy.json` shows exactly that — a textbook,
correct-looking access policy. If you assume access policies govern the
vault, the only remaining explanations are networking or a broken managed
identity, and `fixtures/az-keyvault-show.json` defuses both
(`networkAcls.defaultAction: Allow`, `publicNetworkAccess: Enabled`) while
`fixtures/az-functionapp-identity-show.json` confirms the identity exists.
The one field that dissolves the paradox is `enableRbacAuthorization: true`
in the same vault JSON: the moment the vault moved to the RBAC permission
model, its access policies became dead text, and only role assignments —
where the function app has none — decide who reads secrets.

## Root cause

The vault `kv-reports` uses the Azure RBAC permission model
(`fixtures/az-keyvault-show.json`: `enableRbacAuthorization: true`), so the
access policy granting the function app's identity Get/List on secrets
(`fixtures/az-keyvault-access-policy.json`) is never evaluated. The
identity (`fixtures/az-functionapp-identity-show.json`, principalId
`…0003`) holds no role assignment on the vault —
`fixtures/az-role-assignment-list.json` shows only a Key Vault Administrator
assignment for a different, User principal — so every secrets get is denied
with `ForbiddenByRbac` (`fixtures/function.log`).

## Fix

Grant the managed identity the **Key Vault Secrets User** role on the vault
— the RBAC-model equivalent of the dead access policy:

```bash
az role assignment create \
  --role "Key Vault Secrets User" \
  --assignee-object-id 00000000-0000-0000-0000-000000000003 \
  --assignee-principal-type ServicePrincipal \
  --scope /subscriptions/00000000-0000-0000-0000-000000000002/resourceGroups/rg-reports-prod/providers/Microsoft.KeyVault/vaults/kv-reports
```

Safe and least-privileged: Key Vault Secrets User grants only get/list on
secrets — the exact permissions the old access policy intended. Takes
effect within a minute (role assignments can take a few minutes to
propagate); no app restart needed. Optionally delete the now-ignored access
policy so the next reader is not misled again.

## Prevention

Pick one permission model per vault and enforce it:

1. **Ban the mixed state in IaC.** In Terraform/Bicep, a vault with
   `enableRbacAuthorization = true` should have no `access_policy` blocks
   at all — add a `checkov`/`az policy` rule (or a PR-time grep) that fails
   any vault declaring both, because the policies are unverifiable no-ops
   that mislead exactly like this incident.
2. **Alert on the denial signature.** A Log Analytics alert on Key Vault
   diagnostic logs for `ForbiddenByRbac` results (like the 6 failures in
   5 minutes that tripped `FunctionReportGenFailures` in
   `fixtures/function.log`) pages the mismatch on the first denied read
   instead of after the vault's callers have failed all morning.
3. **Pre-deploy smoke test.** A CI check that resolves each secret
   reference with the deployed function app's identity before go-live
   would have failed this morning's launch at deploy time, not in
   production.

## Further reading

- [Azure role-based access control for Key Vault (enableRbacAuthorization)](https://learn.microsoft.com/azure/key-vault/general/rbac-access-policy)
- [Key Vault Secrets User role definition](https://learn.microsoft.com/azure/role-based-access-control/built-in-roles/security#key-vault-secrets-user)
- [Use a managed identity for Azure Functions to read Key Vault secrets](https://learn.microsoft.com/azure/app-service/app-service-key-vault-references)
