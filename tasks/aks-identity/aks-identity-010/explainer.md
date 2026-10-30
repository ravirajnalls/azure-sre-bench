# aks-identity-010 — explainer

## How to investigate

Establish which authorization path the cluster actually uses, because that
determines whether a RoleBinding can matter at all:
`fixtures/az-aks-show.json:2-4` shows `aadProfile.enableAzureRbac: true`.
That single flag changes the rules of the game: Entra identities are
authorized by **Azure role assignments** on the cluster, and native
Kubernetes RBAC objects (Roles, RoleBindings) are ignored for them. Any
theory that starts with "fix the RoleBinding" is already dead.

Confirm the user is who they claim, so group membership isn't the issue:
`fixtures/kubectl-error.txt:5` shows the Forbidden carrying
`User: 00000000-0000-0000-0000-000000000004`, and
`fixtures/az-ad-group-member-check.json:4` answers
`"value": true` — that object id is a member of grp-sre-oncall. The group
identity chain is intact; the failure is purely about grants.

Now enumerate what the group actually has at the Azure layer:
`fixtures/az-role-assignment-list.json` is the complete list on the cluster.
Assignment 1 (lines 6–15): `Azure Kubernetes Service Cluster User Role` for
grp-platform-admins — that only grants `az aks get-credentials`-level
access, not in-cluster API verbs. Assignment 2 (lines 21–30):
`Azure Kubernetes Service RBAC Writer` for grp-payments-deploy, scoped to
`.../namespaces/payments` (line 28) — the right *kind* of role at the right
*scope*, but for a different group. There is no assignment for
grp-sre-oncall (`00000000-0000-0000-0000-000000000003`) at any scope. No
Azure RBAC role + Azure RBAC enabled = Forbidden on everything, exactly as
observed across pods, deployments, and logs in
`fixtures/kubectl-error.txt:4-11`.

## Why the red herring misleads

The RoleBinding is a genuinely good-looking object:
`fixtures/kubectl-get-rolebinding.yaml:13-20` binds grp-sre-oncall to the
`view` ClusterRole in the payments namespace, and the on-call team has
"double-checked" it. That is precisely why it misleads — the YAML is
correct, and correctness in Kubernetes RBAC is irrelevant when
`enableAzureRbac` bypasses it for Entra identities. Editing, recreating, or
re-validating this RoleBinding can never fix the Forbidden because the API
server never consults it for this user. The mistake this task trains is
sequencing: check the cluster's authorization mode (`fixtures/az-aks-show.json:3`)
*before* grading any RBAC object, because the mode decides which objects are
even in scope. "No other team has problems" fits too — the other teams'
access comes from Azure role assignments
(`fixtures/az-role-assignment-list.json:12,27`), which is exactly why they
work.

## Root cause

The cluster has Azure RBAC for Kubernetes enabled
(`fixtures/az-aks-show.json:3`), so grp-sre-oncall's access must come from
Azure role assignments — and the assignment list
(`fixtures/az-role-assignment-list.json`) contains none for the group: only
Cluster User Role for grp-platform-admins and a payments-scoped RBAC Writer
for grp-payments-deploy. The correct-looking RoleBinding
(`fixtures/kubectl-get-rolebinding.yaml:13-20`) is ignored for Entra
identities, so every kubectl verb in payments returns Forbidden
(`fixtures/kubectl-error.txt:5`).

## Fix

Grant the group the appropriate Azure Kubernetes Service RBAC role at the
payments namespace scope:

```bash
az role assignment create \
  --assignee-object-id 00000000-0000-0000-0000-000000000003 \
  --assignee-principal-type Group \
  --role "Azure Kubernetes Service RBAC Reader" \
  --scope "/subscriptions/00000000-0000-0000-0000-000000000002/resourcegroups/rg-payments-prod/providers/Microsoft.ContainerService/managedClusters/aks-payments-prod/namespaces/payments"
```

Use `Azure Kubernetes Service RBAC Writer` instead if the group needs write
access. The Kubernetes RoleBinding needs **no** change — leave it alone.
Assignment propagates in minutes; the user re-runs `az aks get-credentials`
(or just retries) and the Forbidden clears. Reversible by deleting the one
assignment.

## Prevention

- **Access reviews against the active authorization mode:** a scheduled
  script (or Azure Policy / Entitlement-management check) that, for each
  cluster with `enableAzureRbac: true`, diffs the groups with Kubernetes
  RoleBindings against the groups with AKS RBAC role assignments and flags
  any binding with no corresponding Azure assignment. grp-sre-oncall is
  exactly the orphan this catches — a binding that has been a no-op since
  the cutover.
- **Migration checklist guardrail:** when flipping `enableAzureRbac` on a
  cluster (the cutover last week), run the diff *at migration time* and
  require every pre-existing RoleBinding group to get a mapped Azure role
  before the flag flips. A post-cutover synthetic check —
  `kubectl auth can-i list pods -n payments --as=<group member>` — in the
  pipeline fails the migration, not the next on-call rotation.

## Further reading

- [Azure RBAC for Kubernetes authorization on AKS](https://learn.microsoft.com/azure/aks/manage-azure-rbac)
- [AKS RBAC role definitions (Reader/Writer/Cluster Admin)](https://learn.microsoft.com/azure/role-based-access-control/built-in-roles#azure-kubernetes-service-rbac-writer)
- [Kubernetes RBAC authorization concepts](https://kubernetes.io/docs/reference/access-authn-authz/rbac/)
