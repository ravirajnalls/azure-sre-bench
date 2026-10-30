# aks-ops-051 — explainer

## How to investigate

Separate authentication from reachability first, because the symptom smells
like the former and is the latter. `fixtures/pipeline-log.txt:6` shows
kubectl from `selfhosted-azdo-egress-0` dying with
`dial tcp 20.53.10.7:443: i/o timeout` — a TCP connect timeout, not a 401 or
403. An auth failure gets an HTTP status; a connect timeout on a public API
server is a drop before the handshake, and the classic Azure reason is the
API server's authorized-IP allowlist.

Read the allowlist: `fixtures/az-aks-show.json:8-11` shows
`apiServerAccessProfile.authorizedIpRanges` containing exactly two CIDRs —
`203.0.113.10/32` and `198.51.100.0/24`. Then answer: what IP does the
failing agent pool egress from? `fixtures/vmss-agents.json` says the
`vmss-selfhosted-azdo` scale set has no instance public IPs and SNATs all
egress through its subnet's NAT gateway, and
`fixtures/az-network-public-ip-show-natgw.json:6` gives that gateway's
address as `192.0.2.55` — which is in neither allowed range. That is the
whole failure: the allowlist predates the pool.

The corroborating asymmetry closes it: `fixtures/pipeline-log.txt:9` notes
the same pipeline succeeded minutes later from a laptop on the VPN —
`203.0.113.10/32` is the VPN egress, which *is* allowlisted. One path is
allowed, the other is dropped; the difference is the source IP.

## Why the red herring misleads

The service connection is the strongest decoy because pipelines fail and
someone always suspects credentials: `fixtures/service-connection.json` names
`sp-retail-prod-deploy`, a principal with a tenant ID, an expiration date
over a year out, and — the line that looks alarming — a `lastUsed` timestamp
from the failed run. But read the verification summary: the connection
validated successfully at 02:00, minutes before the failure, and it is an
ARM connection for the resource group, not the API server path. A broken
service connection produces an ARM token error before kubectl ever runs; a
credential-expiry worry dies on `credentialExpiration: 2027`. The run never
authenticated because it never connected — the drop is at the control
plane's edge, before identity is evaluated.

## Root cause

The API server authorized IP ranges on retail-prod-aks
(`fixtures/az-aks-show.json:8-11`) were set when only the VPN existed and do
not include `192.0.2.55/32`, the NAT gateway public IP the new
retail-deploy-agents pool egresses through
(`fixtures/az-network-public-ip-show-natgw.json:6`,
`fixtures/vmss-agents.json`). Every API server connection from the pool is
dropped at the control plane, so deploys from the pool i/o-time-out
(`fixtures/pipeline-log.txt:6`) while VPN laptops succeed.

## Fix

Add the agent pool's egress IP to the authorized ranges, keeping the
existing ones:

```bash
az aks update -g rg-retail-prod -n retail-prod-aks \
  --api-server-authorized-ip-ranges 203.0.113.10/32,198.51.100.0/24,192.0.2.55/32
```

(or a stable /24 covering the NAT gateway's reservation instead of the /32,
if the pool's egress IP may rotate). This is safe and reversible: it widens
an allowlist by one well-known static address, changes nothing inside the
cluster, and the next pipeline run connects immediately. Re-run the failing
deployment after the update — no agent or pipeline changes are needed.

## Prevention

- **Provision the range with the pool:** when the NAT gateway and agent pool
  were created, nothing added their egress IP to the cluster's authorized
  ranges. Make the Terraform/module that builds a deploy-agent subnet take
  the target cluster as an input and append `natGateway.publicIPAddress`
  to `api_server_access_profile.authorized_ip_ranges` in the same apply.
- **Pipeline reachability check:** run a one-second
  `kubectl version --request-timeout=5s` from the agent pool as the first
  pipeline step — it fails in 5s with the same timeout instead of after a
  30s kubectl retry loop, and makes the drop unmistakably a network failure.
- **Change-review for `apiServerAccessProfile`:** any edit to authorized
  ranges or to agent egress paths should require a cross-check against the
  inventory of deploy pools — the two changed independently here.

## Further reading

- [Secure access to the AKS API server with authorized IP ranges](https://learn.microsoft.com/azure/aks/api-server-authorized-ip-ranges)
- [SNAT with Azure NAT Gateway for outbound connectivity](https://learn.microsoft.com/azure/nat-gateway/nat-overview)
- [az aks update — API server authorized IP ranges](https://learn.microsoft.com/cli/azure/aks#az-aks-update)
