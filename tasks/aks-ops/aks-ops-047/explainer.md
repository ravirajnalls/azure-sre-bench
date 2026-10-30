# aks-ops-047 — explainer

## How to investigate

The signature to spot: secret reads/writes fail cluster-wide with an
`InternalError` naming the **KMS plugin**, while running pods are fine — they
already have their secrets decrypted in memory; only *new* secret operations
fail. `fixtures/kubectl-error.txt` shows exactly that: `failed to list
secrets ... kms plugin: decrypt failed ... key is currently disabled`,
`CreateContainerConfigError` for pods that mount Secrets, and the API server
rejecting new secret writes on encrypt.

The error already names the provider, so read the configuration and the key:
`fixtures/az-aks-show.json` shows `securityProfile.azureKeyVaultKms.enabled:
true` with `keyId` pointing at `kv-akskms0002/keys/aks-etcd-key`, and
`fixtures/az-keyvault-key-show.json` shows that same key with
`attributes.enabled: false`. That mismatch — cluster expects to use the key,
the key refuses to be used — is the whole story.

Then find *who* disabled it: `fixtures/keyvault-audit.csv` records a
`KeyUpdate (Disable key)` by `sec-automation-sp` at 03:12Z under policy
`KV-Key-Rotation-Cleanup-2026`, and from 03:15Z onward the cluster's KMS
identity gets `Forbidden` on every `KeyDecrypt`/`KeyEncrypt` with error
"key is disabled".

## Why the red herring misleads

`fixtures/az-role-assignment-list.json` is planted for the reflex "Key Vault
says Forbidden, so permissions must be broken". Read it properly: the
cluster's system-assigned identity holds **Key Vault Crypto User** on the
vault and at subscription scope — exactly the role KMS encryption needs, and
nothing changed overnight. The audit log confirms it: the KMS caller
authenticated fine (its requests reached the key and were rejected because
the *key* is disabled — a key-state error, not an authorization error). RBAC
cannot be the cause when the same principal worked for months and the denial
message names the key's state.

## Root cause

KMS etcd encryption is enabled on the cluster
(`fixtures/az-aks-show.json`, `securityProfile.azureKeyVaultKms`), but the
Key Vault key it uses — `aks-etcd-key` in `kv-akskms0002` — was disabled
overnight: `attributes.enabled: false`
(`fixtures/az-keyvault-key-show.json`), set by the security automation
`sec-automation-sp` under policy `KV-Key-Rotation-Cleanup-2026`
(`fixtures/keyvault-audit.csv`). With the key unusable, the API server's KMS
plugin cannot encrypt or decrypt secret content, so `kubectl` secret
operations fail with InternalError and pods referencing Secrets cannot start
(`fixtures/kubectl-error.txt`).

## Fix

Restore the key's usable state — minutes, not hours:

1. **Re-enable the exact key**: the cluster references a specific key version;
   `az keyvault key update --vault-name kv-akskms0002 --name aks-etcd-key
   --enabled true` (or have `sec-automation-sp` roll back its change). The
   key is `Recoverable`, so nothing is lost — a disabled key is a state
   change, not a deletion.
2. **If the automation was rotating**, the correct path is to enable the new
   key version and update the cluster's KMS profile to it:
   `az aks update --resource-group rg-prod-01 --name aks-prod-eu
   --azure-keyvault-kms ...` with the new `keyId`.
3. **Verify recovery**: `kubectl get secrets -n default` succeeds, and the
   `CreateContainerConfigError` pods in `fixtures/kubectl-error.txt` start
   and mount their Secrets.
4. **Exempt the key from the cleanup policy** so `KV-Key-Rotation-Cleanup-2026`
   cannot disable it again.

## Prevention

- **Scope key-cleanup automation**: any automation that disables "stale" keys
  must carry an explicit exclusion list (or tag) for keys referenced by KMS,
  disk encryption, and CMK configurations — the blast radius of disabling
  them is cluster-wide.
- **Alert on KMS failures at the source**: the API server emits
  `kms plugin` errors and Key Vault records `Forbidden` decrypt/encrypt
  immediately at 03:15 — alert on Key Vault denial spikes for encryption
  operations rather than waiting for kubectl failures.
- **Monitor key state**: alert when any key referenced by an AKS KMS profile
  transitions `enabled` to `false` (Event Grid on Key Vault key events) —
  this incident was fully visible in the audit trail three hours before
  on-call noticed.

## Further reading

- [AKS: KMS etcd encryption with Key Vault](https://learn.microsoft.com/azure/aks/use-kms-etcd-encryption)
- [Azure Key Vault: Key attributes and enabled state](https://learn.microsoft.com/azure/key-vault/keys/about-keys#key-attributes)
- [Kubernetes: KMS provider v2](https://kubernetes.io/docs/tasks/administer-cluster/encrypt-data/#kms-provider-v2)
