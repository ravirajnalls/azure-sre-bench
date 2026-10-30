# cost-031 — explainer

## How to investigate

Start with the bill, not the VMs: `fixtures/cost-by-resource.csv` splits
October's rg-buildagents spend by resource type. The four
`Microsoft.Compute/virtualMachines` rows total about $293, the one storage
account is $8.12 — but the 52 `Microsoft.Compute/disks` rows total roughly
$885. A fleet of four build VMs billing three times as much in disks as in
compute is the anomaly, and disks are what "storage spend" means here.

Next look at those disks directly: `fixtures/az-disk-list.json` lists 52
disks, and 48 of them report `diskState: Unattached` with `managedBy: null`.
Their `timeCreated` values fall on Mondays — four disks per week, every
week from 2026-08-03 through 2026-10-19. Four disks per weekly rollout
matches a four-VM fleet being replaced one VM at a time. Only the 4
current fleet disks are `Attached`.

Now find what creates a disk every Monday and leaves it behind:
`fixtures/cleanup-script.sh` is the weekly image rollout (`replace-agents.sh`).
For each VM it deallocates, then runs `az vm delete --yes`, then
`az vm create` from the new image. `az vm delete` does **not** delete the
OS disk the VM was using, and the script never references the old disk
again — every rollout orphans one Premium OS disk per VM, forever.

Confirm the fleet itself is not the story: `fixtures/az-vm-list.json` shows
exactly `buildagent-01` through `buildagent-04`, all running — no VM growth
to explain the creep. And `fixtures/az-storage-account-list.json` shows the
single artifact account (`stbldagentsart01`, Standard_LRS, Succeeded) at
about $8/month — blob storage is not the growth either. The stranded disks
are.

## Why the red herring misleads

"Storage cost" in a resource group with a storage account naturally points
at `stbldagentsart01` — someone assumes artifact uploads are ballooning.
But `fixtures/az-storage-account-list.json` shows one healthy
Standard_LRS account, and `fixtures/cost-by-resource.csv` prices it at
$8.12 for the whole month. The actual "storage" line items are managed
disks, which are easy to forget because they hide behind the VMs that own
them — until `az vm delete` orphans them and they start billing as
unattached Premium disks at ~$20/month each. The VM count staying flat
(`fixtures/az-vm-list.json`) makes the growth feel impossible, which is
exactly why it went unnoticed for three months.

## Root cause

`replace-agents.sh` (`fixtures/cleanup-script.sh`) rolls a new image across
the fleet by deleting each VM with `az vm delete` and recreating it with
`az vm create`, but never deletes the OS disk the old VM leaves behind —
`az vm delete` does not remove disks, and nothing else in the script cleans
them up. Twelve weekly rollouts since early August stranded 48 unattached
Premium_LRS OS disks (`fixtures/az-disk-list.json`: 48 × `diskState:
Unattached`, `managedBy: null`, four per weekly cadence), and
`fixtures/cost-by-resource.csv` shows their ~$885 of October spend is the
entire storage creep while the four-VM fleet stays constant.

## Fix

Two parts — clean up the backlog, then stop the leak:

```bash
# 1. Delete the stranded disks (verify nothing critical is unattached first)
for disk in $(az disk list --resource-group rg-buildagents \
    --query '[?diskState == `"Unattached"`].name' -o tsv); do
  az disk delete --resource-group rg-buildagents --name "$disk" --yes
done

# 2. Fix replace-agents.sh: delete the OS disk right after az vm delete
old_disk=$(az vm show --resource-group "$RG" --name "$vm" \
    --query "storageProfile.osDisk.name" -o tsv)
az vm delete --resource-group "$RG" --name "$vm" --yes
az disk delete --resource-group "$RG" --name "$old_disk" --yes
```

(Snapshot or export each disk first if the image history matters.)
Reversible in the sense that the change is a script edit plus one-time
deletes; the ~$885/month stops accruing immediately after cleanup.

## Prevention

Make orphaned disks impossible to accumulate. Two concrete guardrails:

1. **Tag-and-sweep policy.** Tag every disk created by the rollout script
   (e.g. `owner=buildagents-rollout`) and run a scheduled Azure CLI/Policy
   task that deletes tagged disks whose `diskState` is `Unattached` for
   more than a grace period (24–48 h). This catches any future leak —
   script bug, manual replacement, failed create — not just this script.
2. **Cost anomaly alert on unattached-disk spend.** A scheduled query over
   the cost export (or Azure Cost Management budget alert on the
   `Microsoft.Compute/disks` category in rg-buildagents) that fires when
   unattached-disk cost exceeds a small threshold would have flagged the
   first stranded disk in August, three months and ~$1,500 ago. A CI check
   that greps the rollout script for a disk-delete step after every
   `az vm delete` is a cheap third layer.

## Further reading

- [Delete a VM and its attached resources (az vm delete does not remove disks)](https://learn.microsoft.com/azure/virtual-machines/delete-resource-group)
- [Azure managed disk pricing (unattached disks still bill)](https://learn.microsoft.com/azure/virtual-machines/disks-types)
- [Azure Policy — tag enforcement for resource governance](https://learn.microsoft.com/azure/governance/policy/tutorials/govern-tags)
