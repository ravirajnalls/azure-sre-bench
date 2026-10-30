# serverless-066 — explainer

## How to investigate

The metrics rule out most of the usual suspects. `fixtures/scale-metrics.csv` shows instances growing 3 → 5 as the lunch peak builds, then **freezing at exactly 5** for an hour while the service-bus backlog climbs from 4,290 to 49,980 and 503s accrue. Per-instance execution time stays ~150 ms — each instance is healthy; there just aren't more of them. The reference rows from the same window a week earlier scale to 14, so nothing about the workload changed. A clean plateau at an exact number is not controller indecision — it is a configured ceiling.

Find who set it: `fixtures/appsettings.json` shows `WEBSITE_MAX_DYNAMIC_APPLICATION_SCALE_OUT=5` on the prod app. `fixtures/infra-changes.csv` explains when and why: the cost-guardrails terraform module v0.9 applied on Oct 28 set that cap — meant for sandbox slots — but its `rg-shop-*` resource-group wildcard caught prod apps. The module author's own PR comment (02:19Z, two days before the peak) says exactly that.

## Why the red herring misleads

The same terraform apply also raised `host.json` function concurrency 16 → 32, and a redeploy-plus-restart on Oct 30 "suspected a stuck scale controller / concurrency regression." Both are dead ends the fixtures themselves refute: the Oct 30 restart produced **no change** — a stuck controller doesn't hold a stable 5-instance plateau through an hour of growing backlog, and higher concurrency reduces the number of instances needed, it doesn't cap them. The plateau-at-5 signature points past both to the cap.

## Root cause

The cost-guardrails terraform module (v0.9, Oct 28) set `WEBSITE_MAX_DYNAMIC_APPLICATION_SCALE_OUT=5` on the prod Functions app via an `rg-shop-*` wildcard (`fixtures/infra-changes.csv`). The Consumption-plan scale controller honors the cap, so the app plateaus at 5 instances during peak while the backlog and 503s grow (`fixtures/scale-metrics.csv`).

## Fix

Restore real headroom on the prod app:

```bash
az functionapp config appsettings update \
  --name func-order-webhook-prod -g rg-shop-prod \
  --settings WEBSITE_MAX_DYNAMIC_APPLICATION_SCALE_OUT=30
```

(or remove the setting entirely to fall back to the plan default) and verify instances scale past 5 with the backlog draining. Then close the hole in the module: the sandbox cap must not wildcard-match prod resource groups — scope guardrails by explicit resource-group list, and treat any scale-out cap on a `*-prod*` group as a review block.

## Prevention

- **Scope guardrails explicitly:** a "cost guardrail" module should never fan out via wildcard; the rg-shop-* catch in module defaults is the same shape as the incident.
- **Alert on plateau-plus-backlog:** instance count frozen at a constant while queue depth grows monotonically is a configured-ceiling signature; it distinguishes instantly from controller thrash (which oscillates) and code regressions (which raise avg_exec_ms).

## Further reading

- [WEBSITE_MAX_DYNAMIC_APPLICATION_SCALE_OUT](https://learn.microsoft.com/azure/azure-functions/functions-scale#limit-scale-out)
- [Azure Functions scale controller — Consumption plan](https://learn.microsoft.com/azure/azure-functions/event-driven-scaling)
- [az functionapp config appsettings update](https://learn.microsoft.com/cli/azure/functionapp/config/appsettings#az-functionapp-config-appsettings-update)
