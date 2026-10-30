# identity-027 — explainer

## How to investigate

Start at the exact failure: `fixtures/pipeline-log.txt`. The service
connection acquires its Azure DevOps federated token fine
(`Acquired federated token from Azure DevOps OIDC endpoint`), then dies at
`az login` with `AADSTS700213: No matching federated identity record found
for presented assertion` — and Entra prints the assertion's identity:
subject `sc://contoso/PlatformEng/sc-prod-deploy`, issuer
`https://vstoken.dev.azure.com/00000000-0000-0000-0000-00000000000B/`. This
is a token-exchange rejection: Entra saw the token, found no federated
credential whose subject matches.

So check what the app registration actually trusts:
`fixtures/az-ad-app-federated-credential-list.json` shows exactly one
federated credential, with the **correct** issuer and audience — but its
subject is `sc://contoso/Platform/sc-prod-deploy`. The token says
`PlatformEng`; the trust record says `Platform`. No match is possible.
The credential's description even says it was created for "project
Platform".

Now find out when and why the name changed: `fixtures/project-rename-audit.json`
records `Project.RenamedProject` at 2026-10-29 21:44 UTC with
`{"oldProjectName":"Platform","newProjectName":"PlatformEng"}` — one day
before the failures. Azure DevOps builds the OIDC subject from the
**current** project name, so the rename silently changed the assertion
identity while the federated credential kept the old string.

Rule out the two usual suspects so you don't chase ghosts:
`fixtures/service-connection.json` shows `sc-prod-deploy` on
`WorkloadIdentityFederation`, state Ready, pointing at the same service
principal the login uses — the connection itself is intact. And
`fixtures/az-role-assignment-list.json` shows the service principal holding
Contributor on `rg-platform-prod` — RBAC is irrelevant anyway, because the
login fails before any resource call.

## Why the red herring misleads

The rename audit invites a wrong read: "the project was renamed, so the
service connection must have broken — recreate or re-authorize it."
`fixtures/service-connection.json` looks reassuring or worrying depending on
framing, but it is healthy: scheme, principal, tenant, and state are all
fine, and the pipeline log proves ADO issues the token successfully. The
other trap is RBAC — `fixtures/az-role-assignment-list.json` shows real
Contributor assignments, and "permissions were lost in the rename" is a
plausible story. But `AADSTS700213` is not a 403 from a resource; it is
Entra refusing the token exchange before authorization ever runs. The break
is one string in one federated credential, not the connection or its roles.

## Root cause

The federated credential on the app registration still pins the subject to
the pre-rename project — `sc://contoso/Platform/sc-prod-deploy`
(`fixtures/az-ad-app-federated-credential-list.json`) — while the renamed
project's token now asserts `sc://contoso/PlatformEng/sc-prod-deploy`
(`fixtures/pipeline-log.txt`). After the ADO project rename recorded in
`fixtures/project-rename-audit.json`, no credential subject matches the
assertion, so every `az login` through `sc-prod-deploy` fails with
AADSTS700213.

## Fix

Update the federated credential's subject to the new project name:

```bash
az ad app federated-credential update \
  --id <app-object-id> \
  --credential-name sc-prod-deploy-00000000-0000-0000-0000-00000000000B \
  --parameters subject="sc://contoso/PlatformEng/sc-prod-deploy"
```

Safe and reversible: the issuer and audience are unchanged, only the
subject string moves, and the old value can be restored by the same
command. The next pipeline run exchanges tokens successfully — no restarts
or re-creation of the service connection needed.

## Prevention

Treat a project rename as an identity change, not a cosmetic one:

1. **Runbook step for renames.** Any ADO project rename checklist must
   include "enumerate service connections in the project and update every
   federated credential subject from `sc://<org>/<old>/…` to
   `sc://<org>/<new>/…`." The ADO REST API can list service connections by
   project; a small script makes the step mechanical.
2. **Detect drift proactively.** A scheduled CI job that runs a cheap
   `az login` through each production service connection (or requests a
   token and inspects the presented subject against
   `az ad app federated-credential list`) catches subject drift within a
   day — including the same class of breakage from org renames, connection
   renames, or a manually recreated connection.

## Further reading

- [Azure DevOps service connections with workload identity federation (subject format)](https://learn.microsoft.com/azure/devops/pipelines/library/connect-to-azure)

- [AADSTS700213 — no matching federated identity record](https://learn.microsoft.com/entra/identity-platform/reference-error-codes)
- [Application federation: configure federated identity credentials on an app registration](https://learn.microsoft.com/entra/workload-id/workload-identity-federation)
