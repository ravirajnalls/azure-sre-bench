# aks-identity-011 — explainer

## How to investigate

Read the mount failure in full — it states the tenant it could not use:
`fixtures/kubectl-describe-pod.txt:58` shows `FailedMount` for volume
`secrets-store-inline` with
`failed to get key vault secret reports-db-connection-string from key vault
kv-reports-prod: failed to acquire token: AADSTS90002: Tenant
'00000000-0000-0000-0000-000000000009' not found`. Two things follow: the
driver is reaching Key Vault (it named the vault and the secret — this is
not networking), and something on the cluster is configured with tenant
`...0009`, which does not exist.

Find where that tenant ID is pinned. The pod's volume carries the pointer —
`fixtures/kubectl-describe-pod.txt:44` shows
`secretProviderClass: azure-reports-kv` — so read that object:
`fixtures/secretproviderclass.yaml:25` sets
`tenantId: 00000000-0000-0000-0000-000000000009` for `keyvaultName:
kv-reports-prod` (line 16). There is the stray tenant, pinned in Terraform
(managed-by label, line 11) when this SecretProviderClass was created this
morning at 05:39 (line 8) — minutes before the first stuck pod.

Decide which tenant is right by asking the vault:
`fixtures/az-keyvault-show.json:17` shows kv-reports-prod lives in tenant
`00000000-0000-0000-0000-000000000001`, and
`fixtures/az-account-show.json:3,10` confirms your own session is in that
same tenant. The SecretProviderClass points at a tenant nobody is in.

Also clear the RBAC theory in passing: `fixtures/az-role-assignment-list.json:9-13`
shows `Key Vault Secrets User` correctly assigned (2026-09-12) to the
workload identity on kv-reports-prod. If permissions were the problem, the
driver would get a 403 from the vault — not an AADSTS90002 at token
acquisition, which happens before the vault is ever asked for a secret.

## Why the red herring misleads

The strongest decoy is "Key Vault denies the identity" — the mount failure
names the vault, and the natural reflex is a missing `Key Vault Secrets
User` role or an access-policy gap. `fixtures/az-keyvault-show.json:6` even
shows `accessPolicies: []`, which looks damning until you read line 8:
`enableRbacAuthorization: true`, meaning the vault deliberately uses Azure
RBAC instead of access policies — and the role assignment exists
(`fixtures/az-role-assignment-list.json:12`). The error itself is the
giveaway: AADSTS90002 is a *tenant lookup* failure at token issuance, not a
403 from vault authorization; the request never gets far enough for
permissions to matter. The second decoy is the workload identity: the
clientID `00000000-0000-0000-0000-000000000003` on
`fixtures/secretproviderclass.yaml:15` invites a "workload identity is
broken" detour. But the clientID is consistent with the assigned principal
and the failure is in the *authority* the token is requested from, not the
principal asking. Only the tenantId on line 25 disagrees with the rest of
the environment — one field, wrong tenant.

## Root cause

The SecretProviderClass `azure-reports-kv` pins
`tenantId: 00000000-0000-0000-0000-000000000009`
(`fixtures/secretproviderclass.yaml:25`), while its target vault
kv-reports-prod lives in tenant `00000000-0000-0000-0000-000000000001`
(`fixtures/az-keyvault-show.json:17`). The Secrets Store CSI driver requests
its token from the nonexistent tenant authority, gets AADSTS90002
(`fixtures/kubectl-describe-pod.txt:58`), and every newly scheduled pod
stays in ContainerCreating because `secrets-store-inline` cannot mount.

## Fix

Correct the tenant in the SecretProviderClass to the vault's tenant:

```bash
kubectl patch secretproviderclass azure-reports-kv -n reports \
  --type merge -p '{"spec":{"parameters":{"tenantId":"00000000-0000-0000-0000-000000000001"}}}'
```

(and fix the Terraform source — the managed-by label on
`fixtures/secretproviderclass.yaml:11` says this object is Terraform-owned,
so a `kubectl patch` alone gets reverted). Then delete the stuck pods so
they schedule fresh and the driver mounts with the corrected class. Safe and
reversible: one field changes, pointing the driver at the tenant the vault
actually belongs to; nothing else in the mount path moves.

## Prevention

- **IaC template validation:** the Terraform module that renders the
  SecretProviderClass should take the vault's tenant as an output of the
  `azurerm_key_vault` resource (`data`-pulled `tenant_id`), not a hand-typed
  literal — the wrong value here (`fixtures/secretproviderclass.yaml:25`)
  was almost certainly transcribed, and a module reference makes the wrong
  tenant unrepresentable. A CI `terraform validate`/plan check that asserts
  `parameters.tenantId == azurerm_key_vault.<vault>.tenant_id` catches any
  drift before apply.
- **Pre-flight mount check in the deploy pipeline:** after applying
  SecretProviderClass changes, run a canary pod with the same
  secretProviderClass and fail the deploy if it does not reach Running
  within a mount timeout — the AADSTS90002 failures in
  `fixtures/kubectl-describe-pod.txt:58-59` would then stop the rollout,
  not the reports worker.

## Further reading

- [Provision secrets from Key Vault with the Secrets Store CSI driver on AKS](https://learn.microsoft.com/azure/aks/csi-secrets-store-driver)
- [AADSTS90002 — tenant not found (Microsoft identity platform error reference)](https://learn.microsoft.com/entra/identity-platform/reference-error-codes)
- [SecretProviderClass parameters for the Azure provider](https://learn.microsoft.com/azure/key-vault/secrets/secrets-store-csi-driver-configuration)
