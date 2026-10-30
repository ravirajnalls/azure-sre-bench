# aks-identity-034 — explainer

## How to investigate

Start where the split is visible: `fixtures/kubectl-get-pods-show-labels.txt`.
Only 2 of 5 pods are failing, and the label columns split them cleanly — the
3 healthy pods carry `pod-template-hash=7d9f4b6c8c` **and**
`azure.workload.identity/use=true`, while the 2 crash-looping pods are all
`pod-template-hash=c5f8d2b4e` with **no** workload-identity label. A pod-by-pod
fault is almost never an identity-plane problem: the identity, the federated
credential, and Key Vault are shared by all 5 pods, so the difference must live
in the pods themselves.

Confirm what the failing pods are missing with
`fixtures/kubectl-describe-pod-new.txt`: the new pod has no `AZURE_CLIENT_ID`,
no `AZURE_FEDERATED_TOKEN_FILE` env, and no `azure-wi-token` projected volume —
the workload-identity mutating webhook skipped it entirely. Then prove the
webhook itself is fine by comparing `fixtures/kubectl-describe-pod-old.txt`: an
old pod shows the full injected set. Same node pool, same namespace, same
ServiceAccount — the only delta between the two describes is the pod template
label from the first step.

Now find where the label went: `fixtures/deployment.yaml` (revision 2) shows
`azure.workload.identity/use: "true"` sitting under the **Deployment's own**
`metadata.labels`, while `spec.template.metadata.labels` carries only
`app=inventory-svc`. The webhook keys on the pod template label, not the
Deployment label — the change-cause annotation ("label restructure per platform
policy") is the deploy that moved it.

Finally, `fixtures/kubectl-get-rs.txt` shows why the outage is partial: the new
ReplicaSet `inventory-svc-c5f8d2b4e` is stuck at 0/2 ready while the old
ReplicaSet `7d9f4b6c8c` still serves its 3 pods. The rollout is wedged
halfway, which is exactly why the symptom reads as "2 of 5 pods".

## Why the red herring misleads

The symptom says Key Vault calls fail with an authentication error, and
`fixtures/az-identity-federated-credential-list.json` invites you to audit the
federated credentials — the expected.json distractor group is right there. But
nothing about the identity changed today, and the credential list is intact:
correct issuer, correct subject binding for the `inventory` ServiceAccount. If
the trust relationship were broken, all 5 pods would fail token exchange, not
just the 2 newest ones. The old pods — same identity, same federated
credential — keep passing, which rules the identity plane out. The failure is
not *who the pod is* but *whether the pod ever got a token to present*: no
injected env/volume means no workload-identity token, and a Key Vault SDK with
no `AZURE_FEDERATED_TOKEN_FILE` fails at authentication before the vault is
ever contacted.

## Root cause

This morning's deploy restructured labels and moved
`azure.workload.identity/use: "true"` out of `spec.template.metadata.labels`
into the Deployment's top-level `metadata.labels`
(`fixtures/deployment.yaml`, revision 2). The workload-identity mutating
webhook only injects the `AZURE_FEDERATED_TOKEN_FILE` env and the
`azure-wi-token` projected volume onto pods whose **template** carries that
label, so the new ReplicaSet's pods (`c5f8d2b4e` in
`fixtures/kubectl-get-rs.txt`) get no identity token — visible as the missing
env/volume in `fixtures/kubectl-describe-pod-new.txt` versus the injected set
in `fixtures/kubectl-describe-pod-old.txt` — and every Key Vault call fails
authentication. The 3 old ReplicaSet pods still have the label and keep
serving.

## Fix

Move the label back into the pod template and roll the pods:

```yaml
spec:
  template:
    metadata:
      labels:
        app: inventory-svc
        azure.workload.identity/use: "true"
```

```bash
kubectl -n inventory rollout restart deployment/inventory-svc
kubectl -n inventory rollout status deployment/inventory-svc
```

Leaving `azure.workload.identity/use` on the Deployment metadata is harmless
but does nothing — the webhook only reads the pod template. Safe and
reversible: patching the template creates a new ReplicaSet with the label,
the webhook injects the token on the first pod start, and a rollback
(`kubectl rollout undo`) restores the previous template exactly.

## Prevention

Put the workload-identity label under template-only enforcement: an admission
policy (Kyverno / Azure Policy for AKS) that requires
`azure.workload.identity/use: "true"` on `spec.template.metadata.labels` of any
Deployment whose ServiceAccount is annotated with the workload-identity
client-id, and warns when the label appears only on Deployment metadata. Add a
CI lint for the same rule so the next "label restructure" cannot silently strip
it, and gate rollouts on the new ReplicaSet becoming ready — a stuck rollout
that leaves 3 healthy old pods is easy to miss in a dashboard until the old
pods are the only thing keeping the service alive.

## Further reading

- [Azure AD Workload Identity — architecture and label requirements](https://azure.github.io/azure-workload-identity/docs/topics/architecture.html)
- [Workload identity with AKS — deploy and configure](https://learn.microsoft.com/azure/aks/workload-identity-deploy-cluster)
- [Azure AD Workload Identity mutating webhook — label-based injection](https://azure.github.io/azure-workload-identity/docs/installation.html)
