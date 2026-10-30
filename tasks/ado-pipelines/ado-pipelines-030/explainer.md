# ado-pipelines-030 — explainer

## How to investigate

The pipeline log already names the failure mode: `fixtures/pipeline-log.txt:17`
is `Error: Error acquiring the state lock`, and lines 19–20 say exactly what
holds it — `a blob lease is held on the state blob. Lease ID: ...0003`. The
pattern on lines 14–15 (`Acquiring state lock` repeated at 06:02 and 06:07)
with the 300s lock-timeout expiry on line 27 shows Terraform retrying and
giving up; the state was never corrupted, it was simply locked. The error
message even pre-writes the diagnosis: "If the lease was left by a previous
run that no longer exists, break the lease manually."

Confirm the lease on the blob itself:
`fixtures/az-storage-blob-show.json:32-34` shows `leaseDuration: infinite`,
`leaseState: leased`, `leaseStatus: locked` on `prod.terraform.tfstate`
(line 13) in the `tfstate` container of `stprodtfstate001` — the exact
backend from `fixtures/backend.tf:2-8`. `infinite` lease duration means the
lease will never expire on its own; this state does not heal with waiting.

Identify who left it: `fixtures/pipeline-run-history.json:49-55` shows run
7842 of infra-prod was **canceled** at `2026-10-29T08:41:27.3100385Z`, and
`fixtures/az-storage-blob-show.json:31` shows the blob's `lastModified` is
`2026-10-29T08:41:27+00:00` — the same second. The cancel killed the agent
mid-apply; Terraform never got to release the lease, and it has been
blocking every run since (7842 is the last non-failed run; 7850 and 7851
both failed on the lock). Cancel time matching the lease timestamp to the
second closes the case.

## Why the red herring misleads

The storage account's network configuration is the standing suspect for
anything that "suddenly can't reach state":
`fixtures/az-storage-account-network-rules.json:21-27` is where a firewall
story would live — but it shows `defaultAction: Allow` with empty `ipRules`
and `virtualNetworkRules`, and `publicNetworkAccess: Enabled` (line 33). More
decisively, a network problem would fail `terraform init` (which reads the
backend) — and `fixtures/pipeline-log.txt:11` shows init **succeeding**
("Terraform has been successfully initialized!"). The failure only appears
when plan tries to take the *write lock*. Network rules can't be the cause
of a failure that starts after authentication succeeds and only on the
locking operation.

## Root cause

A cancelled pipeline run (7842, canceled 2026-10-29T08:41:27Z,
`fixtures/pipeline-run-history.json:49-55`) left an orphaned infinite blob
lease on the `prod.terraform.tfstate` state blob
(`fixtures/az-storage-blob-show.json:31-34`) in the backend configured by
`fixtures/backend.tf`. Every subsequent `terraform plan` hangs on the state
lock and times out after 300s
(`fixtures/pipeline-log.txt:14-17,27`).

## Fix

Break the stale lease, then re-run — no Terraform code changes:

```bash
az storage blob lease break \
  --account-name stprodtfstate001 \
  --container-name tfstate \
  --name prod.terraform.tfstate
```

(Equivalently, `terraform force-unlock -force 00000000-0000-0000-0000-000000000003`
using the lease ID from `fixtures/pipeline-log.txt:20`.) The break is
immediate and safe **after you confirm no other apply is actually running**
— which the run history proves here: 7842 finished canceled yesterday, and
7850/7851 failed without ever acquiring the lock. Then re-run the infra-prod
pipeline; plan acquires a fresh lease and completes normally. The risk to
check before breaking any lease is a genuinely concurrent apply — verify
against the run history or Azure activity log first, since breaking a live
lease could allow two writers to the state file.

## Prevention

- **Clean up on cancel:** a pipeline final step (or an Azure DevOps
  cancellation hook / scheduled function) that checks the state blob's
  `leaseState` after a canceled or failed apply and breaks any lease older
  than a grace window. Run 7842's lease was detectable the moment it was
  canceled (`fixtures/az-storage-blob-show.json:33`); automation would have
  cleared it yesterday.
- **Alert before the queue does:** a monitor on the tfstate container that
  pages when `leaseState == 'leased'` persists beyond, say, 30 minutes with
  no run in `inProgress` (`fixtures/pipeline-run-history.json` shows none
  after 7842's cancel) — a stuck lease then surfaces as its own alert
  instead of as N consecutive pipeline failures.

## Further reading

- [Terraform azurerm backend — state lock and lease errors](https://learn.microsoft.com/azure/developer/terraform/backend-azure-blob-storage)
- [Blob leases — break a lease on a blob](https://learn.microsoft.com/rest/api/storageservices/lease-blob#break-lease)
- [Azure Pipelines — control run cancellation behavior](https://learn.microsoft.com/azure/devops/pipelines/process/phases)
