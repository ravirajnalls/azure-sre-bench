#!/usr/bin/env bash
# replace-agents.sh — roll a new build agent image across the CI fleet in
# rg-buildagents. Recreates each VM in place, one at a time, so at least
# three agents stay available to the build queues. Adapted from the old VMSS
# replacement runbook that was retired when the fleet moved off the scale set.
set -euo pipefail

RG="rg-buildagents"
LOCATION="westus2"
SIZE="Standard_D2s_v5"
IMAGE="/subscriptions/00000000-0000-0000-0000-000000000001/resourceGroups/rg-images/providers/Microsoft.Compute/images/buildagent-linux-2026-10"
SUBNET="/subscriptions/00000000-0000-0000-0000-000000000001/resourceGroups/rg-buildagents/providers/Microsoft.Network/virtualNetworks/vnet-buildagents/subnets/snet-agents"
FLEET=(buildagent-01 buildagent-02 buildagent-03 buildagent-04)

for vm in "${FLEET[@]}"; do
  echo "== Rebuilding ${vm} on the new agent image =="

  # Drain the agent so the build queue stops dispatching to it.
  az vm run-command invoke \
    --resource-group "$RG" \
    --name "$vm" \
    --command-id RunShellScript \
    --scripts "sudo systemctl stop buildagent-runner && sudo systemctl disable buildagent-runner"

  az vm deallocate --resource-group "$RG" --name "$vm"

  # Delete the old VM; the recreate below rebuilds it with the same name.
  az vm delete --resource-group "$RG" --name "$vm" --yes

  az vm create \
    --resource-group "$RG" \
    --name "$vm" \
    --location "$LOCATION" \
    --size "$SIZE" \
    --image "$IMAGE" \
    --subnet "$SUBNET" \
    --admin-username azureuser \
    --ssh-key-value ~/.ssh/buildagents.pub \
    --os-disk-size-gb 128

  echo "== ${vm} rebuilt =="
done

echo "Fleet rebuild complete."
