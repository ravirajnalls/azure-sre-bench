# aks-workloads-042 — explainer

## How to investigate

Start where the scaling decision is made. KEDA scales `invoice-worker` through
the HPA it manages, so the first question is why the autoscaler never fires.
`fixtures/kubectl-get-hpa.txt` shows `keda-hpa-invoice-worker` with
`TARGETS <unknown>/5` and `REPLICAS 0`: the HPA is not saying "no load", it is
saying "I have no idea what the load is". A `<unknown>` target means the
metrics pipeline behind the scaler is broken, which is very different from a
healthy queue that simply has no work.

The ScaledObject wiring is the next stop. `fixtures/scaledobject.yaml` shows a
plain `azure-servicebus` trigger on `invoice-queue` with
`authenticationRef: invoice-worker-auth`, and
`fixtures/triggerauthentication.yaml` shows what that name resolves to — a
TriggerAuthentication whose entire `spec.workloadIdentity` block is **empty**.
No `identityId` is declared. The team assumed that "workload identity" means
KEDA authenticates as the app's identity; in fact, with no `identityId`, the
KEDA operator fetches metrics with its own bound identity.

The operator log settles whose token is failing.
`fixtures/keda-operator.log:1` shows the webhook injected client-id
`00000000-0000-0000-0000-000000000001` into the keda-operator pod, and line 3
shows tokens being acquired for that same client. Every subsequent poll of
`invoice-queue` then fails with `401 InvalidAuthorizationToken` — the caller
`00000000-0000-0000-0000-000000000001` "does not have a 'Listen' claim" on the
queue. So the identity actually doing the metric fetch is unauthorized.

Whose fault is the missing claim? `fixtures/az-role-assignment-list.json` lists
exactly one `Azure Service Bus Data Receiver` assignment on
`sb-invoices-prod`, and its `principalId` is
`00000000-0000-0000-0000-000000000002`. Cross-reference the identity names in
`fixtures/az-identity-list.json`: client
`00000000-0000-0000-0000-000000000001` is `keda-operator-mi` and client
`00000000-0000-0000-0000-000000000002` is `invoice-worker-mi`. The role went
to the **app** identity, but the **operator's** identity is the one polling —
hence 401 on every poll, `<unknown>` on the HPA, and 0 replicas forever.

## Why the red herring misleads

The queue itself is the attractive wrong answer: 40,112 messages sitting in
`invoice-queue` looks like a messaging outage, and "the queue is stuck /
backlogged" is what a fast investigator writes. But
`fixtures/az-servicebus-queue-show.json` shows a perfectly healthy queue:
`status: Active`, zero dead-lettered messages, `maxDeliveryCount: 10`,
generous size limits, and a large but ordinary
`activeMessageCount: 40112`. A queue is passive storage — it does not need to
"work" for a scaler to see it; the messages being present is exactly why the
deployment *should* be at max replicas. The real break is upstream of the
queue, in the identity that is allowed (or not allowed) to read its message
count. The `order-archiver` HPA in `fixtures/kubectl-get-hpa.txt` working
normally (`2/5 (avg)`) is the tell: only the trigger that depends on this
TriggerAuthentication is blind.

## Root cause

The `invoice-worker-auth` TriggerAuthentication declares
`spec.workloadIdentity: {}` with no `identityId`
(`fixtures/triggerauthentication.yaml`), so KEDA's operator polls Service Bus
with its own `keda-operator-mi` identity (client
`00000000-0000-0000-0000-000000000001`, per
`fixtures/keda-operator.log`). The only `Azure Service Bus Data Receiver`
role assignment on `sb-invoices-prod` was granted to the app identity
`invoice-worker-mi` (client `00000000-0000-0000-0000-000000000002`,
`fixtures/az-role-assignment-list.json` with
`fixtures/az-identity-list.json`). The operator's identity therefore has no
read access, every metric fetch returns 401, the HPA target stays
`<unknown>`, and `invoice-worker` never scales out of 0 replicas.

## Fix

Pick one of two consistent endings — either make the operator's identity
capable, or make KEDA use the identity that already is:

1. Grant the role to the identity that polls:

   ```bash
   az role assignment create \
     --role "Azure Service Bus Data Receiver" \
     --assignee-object-id 00000000-0000-0000-0000-000000000001 \
     --scope /subscriptions/00000000-0000-0000-0000-000000000002/resourceGroups/rg-invoices-prod/providers/Microsoft.ServiceBus/namespaces/sb-invoices-prod
   ```

2. Or point the TriggerAuthentication at the identity that already holds the
   role:

   ```yaml
   spec:
     workloadIdentity:
       identityId: 00000000-0000-0000-0000-000000000002  # invoice-worker-mi
   ```

After either change, the trigger polls succeed within one `pollingInterval`
(30 s in `fixtures/scaledobject.yaml`), the HPA target becomes a number, and
the deployment scales toward the queue depth. Reversible: remove the role
assignment or the `identityId` to return to the broken state.

## Prevention

- **Grant the role to whoever actually polls.** With KEDA workload identity,
  the operator is the caller unless the TriggerAuthentication names a
  different `identityId`. Bake both halves into the module that renders these
  manifests: `identityId` set from the same variable as the role assignment,
  so they can never drift apart.
- **Alert on `<unknown>` HPA targets.** A deployment pinned at 0 replicas
  with a full queue for hours should page someone; an alert on
  `kube_horizontalpodautoscaler_status_target_metric` being unknown (and on
  KEDA operator 401 log rate) turns this from a silent billing outage into a
  five-minute fix.
- **Canary scaling in CI.** A synthetic queue-depth check after any
  TriggerAuthentication change — scale a scratch deployment from 0 and assert
  it scales out — would have caught the missing role before the invoice run.

## Further reading

- [KEDA TriggerAuthentication — workload identity](https://keda.sh/docs/latest/authentication-forms/workload-identity/)
- [KEDA azure-servicebus scaler](https://keda.sh/docs/latest/scalers/azure-service-bus/)
- [Azure workload identity for KEDA on AKS](https://learn.microsoft.com/azure/aks/workload-identity-deploy-cluster#keda)
- [Azure Service Bus built-in roles](https://learn.microsoft.com/azure/service-bus-messaging/service-bus-managed-identities)
