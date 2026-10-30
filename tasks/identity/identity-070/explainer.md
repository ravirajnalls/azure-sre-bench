# identity-070 — explainer

## How to investigate

Start from the error, not the change list. Every failure in the sign-in log
(`fixtures/signin-logs.kql.csv`) carries `AADSTS53003` and names the same
policy: **"Require compliant device - All cloud apps (v2)"**. The first
failure is at 09:12:37Z; four different service principals (`-pipelines-prod`,
`-release-infra`, `-package-read`) fail identically, while a human sign-in at
09:15:02Z from an Intune-compliant laptop succeeds. Whatever did this applies
to workload-identity sign-ins but not to compliant interactive devices.

Now anchor that policy in the change log (`fixtures/changes.json`):

- **CHG-0815 — applied 08:45 UTC** (27 minutes before the first failure):
  expanded the "Require compliant device" Conditional Access policy from one
  legacy CRM app to **All cloud apps**, and left
  `clientApplications/excludeServicePrincipals` empty — workload-identity
  sign-ins were never exempted. The live policy export
  (`fixtures/ca-policy.json`) corroborates every element: `includeApplications:
  ["All"]`, `builtInControls: ["compliantDevice", "approvedApplication"]`,
  `modifiedDateTime 2026-10-30T08:45:00Z`, `lastModifiedByChange: CHG-0815`.
  The mechanism matches the error exactly: a compliant-device grant control
  applied to a service-principal sign-in that has no device at all.
- **CHG-0821 — applied 08:55 UTC** (ADO project settings): retention and badge
  visibility. It is the *latest* change and touches the ADO project, but
  nothing in it can alter authentication.
- **CHG-0809 — applied 07:20 UTC**: the yaml refactor ran **green in staging
  at 07:41** and kept the AzureCLI@2 login steps; it cannot explain failures
  that start 90 minutes later across unrelated projects.
- **CHG-0789 / CHG-0795**: an owner rotation is directory metadata; the Key
  Vault firewall change targets `kv-infra-secrets`, while pipelines read from
  `kv-prod-shared` — and the failure is at token issuance, long before any
  secret is read.

## Why the red herring misleads

The federated-credential edit (**CHG-0802**, 01:30 UTC) is the trap the
symptom hands you: the identity team "checked the credential", the failure is
an authentication error, and a credential edit is the most guilty-looking
entry in the log. The pipeline log
(`fixtures/pipeline-log.txt`) is the counter-evidence — the OIDC client
assertion is **accepted** immediately before every AADSTS53003 block,
including the 09:31 re-run *after* on-call re-synced the credential. A broken
federated credential fails at the token exchange with AADSTS70021 or
AADSTS501057 (untrusted issuer/subject); it does not produce a Conditional
Access block at sign-in. The credential works; the *policy* refuses to issue
a token to an identity with no compliant device.

## Root cause

CHG-0815 (applied 2026-10-30T08:45:00Z, device-trust rollout SEC-2209)
expanded the "Require compliant device" Conditional Access policy to **All
cloud apps** without exempting workload identities. Service principals have
no device state, so every sign-in that the policy now covers — all four
pipeline identities — is denied with AADSTS53003 from 09:12:37Z, when the
first pipeline of the morning ran.

## Fix

Add the pipeline app registrations' appIds to the policy's
`excludeServicePrincipals` (Graph: PATCH the `ca-00000000-0000-0000-0000-000000000001` policy), or scope
`includeApplications` back down until workload identities are supported.
Re-run a pipeline and confirm the sign-in log shows a success for
`-pipelines-prod`. Longer term, device-trust rollouts need a
workload-identity exclusion list reviewed as part of the change, because
"require compliant device" is un-satisfiable for any service principal.

## Prevention

Treat Conditional Access scope expansions as auth-breaking changes for
non-interactive identities: stage them with a report-only state, alert on
CA-denied sign-in spikes (a `ConditionalAccessStatus == "failure"` alert on
service-principal sign-ins would have caught this in one minute), and
document the workload-identity exemption review in the device-trust runbook
so SEC tickets cannot ship with an empty `excludeServicePrincipals` list.

## Further reading

- [Microsoft Entra: Workload identity and Conditional Access](https://learn.microsoft.com/entra/identity/conditional-access/workload-identity)
- [Microsoft Entra: AADSTS error codes reference](https://learn.microsoft.com/entra/identity-platform/reference-error-codes)
- [Google SRE Book: Postmortem culture — learning from failure timelines](https://sre.google/sre-book/postmortem-culture/)
