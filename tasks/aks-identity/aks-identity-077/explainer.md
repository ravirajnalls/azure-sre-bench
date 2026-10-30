# aks-identity-077 — explainer

> **Spoiler for humans** — this file is the Learn-page write-up. Agents never see it: the runner keeps it out of the workspace like `expected.json`.

## How to investigate

1. Start from the original diagnosis (aks-identity-008): pods Running with
   green probes, Key Vault reads failing with 403, vault metrics flat, the
   `Key Vault Secrets User` role assignment intact — then
   `az-identity-federated-credential-list.json` shows the only credential is
   `fic-orders-api-default` with subject
   `system:serviceaccount:default:orders-api`, while the deployment now runs
   in namespace `orders` with service account `orders-api`.
2. `resolution.log` gives the operator timestamps: impact window 22:16 →
   23:03 (**47 minutes**), diagnosis at 22:41, fix — adding
   `fic-orders-api-orders` for subject
   `system:serviceaccount:orders:orders-api` — at 22:58, verification by
   23:03, and the old credential left for a 48-hour rollback window.
3. `chat-timeline.txt` confirms the same facts in the on-call's words and
   sets the tone: "fix the runbook, not the people."

## What a passing write-up does

- Uses the six blameless-postmortem sections (Summary, Impact, Timeline,
  Root Cause, Contributing Factors, Action Items) in 300–1500 words.
- States every required fact: the 47-minute customer impact; that the
  federated credential's **subject stayed on the old namespace**; that the
  fix added a credential for `system:serviceaccount:orders:orders-api`; and
  a concrete action item closing the gap (runbook step, IaC-managed
  credentials, or a pre-move FIC checklist).
- Stays blameless: it names the missing process step as the cause, never
  Key Vault or RBAC (both verified healthy in the evidence) and never an
  individual.

## Common failure modes

- Blaming the vault ("Key Vault outage") or the RBAC configuration — the
  fixtures explicitly rule both out.
- Naming the on-call engineers from the chat instead of the process gap.
- Reciting the timeline without an action item — the postmortem's whole
  point is the follow-up.

## Root cause

The namespace move changed the service account's identity path while the
federated credential kept its subject pinned to
`system:serviceaccount:default:orders-api` — so Azure stopped matching the
presented `system:serviceaccount:orders:orders-api` subject and began
rejecting every exchange. Nothing in the move runbook covered federated
credentials, and IaC never managed them, so the stale subject survived the
planned change undetected until the pod's authentication failed for 47
minutes.

## Prevention

Manage federated credentials as infrastructure as code, deriving each
subject from the same manifest that declares the service account's
namespace — a namespace move then updates the subject in the same
commit. Add a federated-credential check to the move runbook, and alert
on a rising rate of failed token exchanges (`401`/`403` from the
identity provider) so the next drift is caught in minutes, not on the
first customer ticket.

## Further reading

- Blameless postmortems (Google SRE book):
  https://sre.google/sre-book/postmortem-culture/
- Azure workload identity federation:
  https://learn.microsoft.com/entra/workload-id/workload-identity-federation
- AKS workload identity:
  https://learn.microsoft.com/azure/aks/workload-identity-deploy-cluster
