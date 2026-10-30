# aks-networking-033 — explainer

## How to investigate

Characterize the failure before touching Azure: `fixtures/payments-api.log`
shows `connect timeout after 10s` on attempt 1 — new connections never
establish — while successfully connected calls complete in ~110 ms with 200s.
So the partner is not slow; **new** outbound connections can't get out. The log
footer quantifies it: 187 connect timeouts on 4,102 partner calls (~4.6%) in the
14:00–15:00 window, and ledger (in-cluster) calls unaffected — the failure is
specific to egress at peak.

Rule the partner out first: `fixtures/partner-status.json` shows all systems
operational, no active incidents, last degraded-performance incident closed 41
days ago. A partner-side problem also wouldn't explain why a retry seconds
later succeeds (`req_id=6c03` fails attempt 1, then returns 200 on attempt 2) —
a degraded partner fails consistently, not per-new-connection.

Now look at how the cluster reaches the internet. `fixtures/az-aks-show.json`:
`outboundType: loadBalancer` with a `loadBalancerProfile` carrying **one**
managed outbound IP and `allocatedOutboundPorts: 0`. Zero means "use the
default": **1024 SNAT ports per node**, allocated from the outbound IP's 64k
port budget. `fixtures/kubectl-get-nodes.txt` shows 24 Ready nodes — so
24 × 1024 ≈ 24,576 SNAT ports, shared by every outbound flow from the cluster,
one port consumed per outbound connection. Why does that matter *now*?
`fixtures/app-config.yaml`: the partner-settlement client sets `keep-alive:
false` and `idle-connection-ttl: 0s` — every partner call opens a fresh TCP
connection, and each connection holds its SNAT port for up to the load
balancer's `idleTimeoutInMinutes: 4`. At peak, connections are opened faster
than ports are recycled, and the pool exhausts.

Confirm with the metrics: `fixtures/kql-snat-connection-count.csv` —
`SNATConnectionCount` with `State=Failed` jumps from single digits to 700+ at
14:00 and collapses back to single digits by 15:15, exactly matching the
symptom window. Failed SNAT allocations on the outbound load balancer is the
signature of SNAT port exhaustion. And `fixtures/node-cpu-kql.csv` shows node
CPU flat at 22–31% across the window — nothing is resource-starved on the nodes
themselves.

## Why the red herring misleads

Two plausible stories both die on the evidence. "The partner is having an
incident": the status page (`fixtures/partner-status.json`) says otherwise, the
failures confine themselves to one daily hour, and — decisively — a retry
succeeds 21 seconds later in `fixtures/payments-api.log` (`latency_ms=21047`
includes the 10 s timeout plus a fast success). A degraded partner API produces
consistent failures, not per-recovery windows. "The nodes are CPU-saturated at
peak": `fixtures/node-cpu-kql.csv` stays under ~31% across the peak window on
every sampled node — and CPU pressure wouldn't selectively produce *connect*
timeouts to one destination while request latency stays flat anyway.

## Root cause

SNAT port exhaustion on the cluster's outbound load balancer. The
`payments-api` client opens a fresh outbound connection for every partner call
(`keep-alive: false` in `fixtures/app-config.yaml`), and 24 nodes
(`fixtures/kubectl-get-nodes.txt`) share a single outbound IP with the default
1024 allocated SNAT ports per node (`allocatedOutboundPorts: 0` in
`fixtures/az-aks-show.json`). During the evening peak, connection churn
consumes the ~24.5k-port pool faster than the 4-minute idle timeout releases
ports, so new connections fail — visible as `State=Failed` SNAT spikes in
`fixtures/kql-snat-connection-count.csv` and connect timeouts on ~5% of partner
calls in `fixtures/payments-api.log`.

## Fix

Attack both sides of the ratio — ports available and ports demanded:

1. **More SNAT capacity.** Preferred: switch the cluster's outbound path to a
   managed NAT gateway, which allocates SNAT ports dynamically per flow instead
   of per-node up front:

   ```bash
   az aks update -g rg-aks-prod -n aks-prod \
     --outbound-type managedNATGateway \
     --nat-gateway-managed-outbound-ip-count 1
   ```

   (Requires the cluster to be on CNI networking with nodes in a
   customer-managed VNet, which `fixtures/az-aks-show.json` shows it is.) If
   the load balancer path must stay, raise `allocatedOutboundPorts` and add
   outbound IPs — note the arithmetic: total ports = IP count × 64k, so 24
   nodes × 4096 ports/node needs 2 IPs, not 1.
2. **Less churn.** Enable HTTP keep-alive with connection pooling in the
   partner-settlement client — the ledger client in the same
   `fixtures/app-config.yaml` already shows the intended shape:
   `keep-alive: true`, a real `max-connections-per-route`, a non-zero
   `idle-connection-ttl`. This is the change tracked (and unscheduled) as
   PAY-2291 in the config comment.

Do both: the client fix alone shrinks but doesn't bound the churn, and the NAT
gateway alone fixes capacity while leaving per-request connections as ongoing
waste. Both changes are reversible and can be staged (NAT gateway first for
immediate relief, client fix in the next release).

## Prevention

1. **Capacity-model egress for churny workloads.** With the load balancer
   outbound path, budget SNAT ports explicitly: ports/node =
   `allocatedOutboundPorts` (default 1024), total = outbound IPs × 64k. Any
   service that opens per-request outbound connections should trigger a capacity
   review against node count — node count multiplies demand against a fixed
   64k-port pool per outbound IP, so every added node (this cluster runs 24)
   brings the pool closer to exhaustion with the same per-connection client.
2. **Ban `keep-alive: false` in client config by default.** The
   `fixtures/app-config.yaml` comment shows the team *knew* about PAY-2291 and
   it sat unscheduled — make the healthy pooling settings the shared
   client-library default so a per-request client is a deliberate exception
   needing review, not a config default.
3. **Alert on SNAT failure metrics.** `SNATConnectionCount` with
   `State=Failed` > 0 sustained for 15 minutes pages before users notice a
   5% timeout rate — the data in `fixtures/kql-snat-connection-count.csv` was
   already flowing to the workspace; nothing was watching it.

## Further reading

- [Azure Load Balancer — outbound connections and SNAT port allocation](https://learn.microsoft.com/azure/load-balancer/load-balancer-outbound-connections)
- [Manage SNAT port exhaustion for Azure Load Balancer](https://learn.microsoft.com/azure/load-balancer/troubleshoot-snat-exhaustion)
- [Use a managed NAT gateway with Azure Kubernetes Service (AKS)](https://learn.microsoft.com/azure/aks/nat-gateway)
