# serverless-065 — explainer

## How to investigate

An intermittent, session-sticky wrong value that survives restarts is a routing problem, not a compute problem. The support log pins it: `fixtures/support-tickets.log` shows the old shipping fee on roughly one order in four, unchanged by the 06:00Z maintenance restarts — a stable ratio means stable traffic weights.

Read the ingress, not the revision list's `active` flags. `fixtures/az-containerapp-show.json` shows the live split: `ingress.traffic` routes 75% to `checkout-api--sv-3` (v2.4.1, latest) and **25% to `checkout-api--sv-1`** — which `fixtures/revision-list.json` reveals is still running image `v2.3.9`, the old pricing engine. The 1-in-4 rate matches the 25% weight exactly.

Explain how the weight got there: `fixtures/deploy-history.csv` shows the blue-green cutover aborted at 50/50 and pinned by hand on Oct 29. When v2.4.1 deployed minutes later, the traffic update targeted only the previous latest revision (sv-2 → 0) and never touched sv-1's pinned 25%. In multiple-revisions mode, Container Apps keeps splitting by weight forever — nothing auto-drains an old revision.

## Why the red herring misleads

The deploy history records two v2.4.1 redeploys "to rule out image pull caching" — a plausible theory because pods *look* correct (`PRICING_VERSION 2026.10-v2` everywhere per ticket #48122) yet wrong values keep appearing. It is a trap: redeploys create a *new* revision; they cannot change where the *existing* ingress weights point. The restarts and redeploy both left the ratio untouched, which is itself the disproof. Any fix at the image layer would never drain sv-1.

## Root cause

Revision `checkout-api--sv-1` (v2.3.9) still receives 25% of ingress traffic because the aborted blue-green cutover's manually pinned weights were never drained; the v2.4.1 deploy zeroed sv-2's weight but left sv-1 at 25 (`fixtures/az-containerapp-show.json`, `fixtures/revision-list.json`). A quarter of checkout sessions are still routed to the old pricing engine.

## Fix

Drain the old revision and complete the cutover:

```bash
az containerapp ingress traffic update \
  --name checkout-api -g rg-shop-prod \
  --revision-weight checkout-api--sv-3=100
```

Then deactivate `checkout-api--sv-1` so it stops consuming the maintenance window. Verify the fee ratio disappears from new sessions. Longer term, make this class of leak impossible: the deploy tooling should zero **all** non-latest revision weights on release (not just the previous latest), and a manually pinned traffic split should carry an expiry or an owner tag so an aborted cutover cannot silently persist in production.

## Prevention

- **Cutover completion gate:** a deploy that changes revisions must assert the resulting ingress split is a single 100% target (or an explicitly requested split); a residual non-latest weight above 0 is a failed deploy.
- **Expiry on pinned weights:** manual `--revision-weight` pinning gets a TTL and an owner; the alert fires when a non-latest revision has carried traffic for more than an hour.

## Further reading

- [az containerapp ingress traffic update](https://learn.microsoft.com/cli/azure/containerapp/ingress/traffic#az-containerapp-ingress-traffic-update)
- [Azure Container Apps — blue-green deployment](https://learn.microsoft.com/azure/container-apps/blue-green-deployment-overview)
- [Azure Container Apps — revisions](https://learn.microsoft.com/azure/container-apps/revisions)
