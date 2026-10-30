# edge-057 — explainer

## How to investigate

The alert says the origin group is unhealthy, so start where health is
decided: the probe. `fixtures/az-afd-origin-group-show.json` shows
`og-www-prod` probing `Https` `GET /health` every 120 seconds. That is a
configuration claim; the next question is what actually happens when the
probe fires. `fixtures/afd-health-probe-log.kql.csv` answers it: every
single probe to `www-origin.contoso-apps.com` returns `404` with result
`OriginError`, back to back, for hours. An origin that fails 100% of
probes with 404s is not down — it is answering. A dead server times out
or refuses connections; a 404 is an application response, which means
the probe reaches a live web server that simply has no route at
`/health`.

`fixtures/origin-access-log.txt` closes the loop: the origin's own
access log shows `AzureFrontDoor-HealthProbe` requests to `/health`
failing with 404 while `azure-cluster-probe` requests to `/healthz`
succeed with 200 on the same box. The replatformed origin moved its
health endpoint to `/healthz` and nothing told Front Door. The route
itself is fine — `fixtures/az-afd-route-show.json` shows `route-www`
enabled and pointing at `og-www-prod` — so when Front Door declares all
origins unhealthy it has nowhere to send user traffic and serves 503
instead.

## Why the red herring misleads

`fixtures/waf-log.kql.csv` is full of `Block` actions against
`waf-policy-prod` in the same time window, which invites "the WAF is
blocking users." Three things argue it away. First, the blocked entries
are 403s against scanner noise — `/wp-login.php`, `/.env`,
`/xmlrpc.php` — from two external IPs, while the outage is 503s for
everyone. Second, WAF evaluates requests that Front Door is willing to
forward; an origin group at 0% healthy never forwards anything, so WAF
activity cannot explain the outage. Third, a scanner's blocked probes
continue through the whole window — the WAF is doing exactly what it
was configured to do. Correlation in the same log workspace is not
causation.

## Root cause

`og-www-prod`'s health probe targets `Https GET /health`
(`fixtures/az-afd-origin-group-show.json`), but the replatformed origin
serves health at `/healthz`. Every probe returns 404
(`fixtures/afd-health-probe-log.kql.csv`), so the origin never earns a
successful sample, the group reports 0% healthy, and Front Door has no
origin to route `www.contoso.com` to — hence the 503s. The origin is
healthy the whole time; Front Door just measures it at a URL that no
longer exists.

## Fix

Point the probe at the endpoint the new origin actually serves: update
the origin group's `healthProbeSettings.probePath` from `/health` to
`/healthz` (or, alternatively, add a `/health` route on the new origin
that reuses the `/healthz` handler — pick one owner for the contract
and document it). After the change, confirm in
`fixtures/afd-health-probe-log.kql.csv`-equivalent probe logs that
probes return 200 and the origin group's healthy-origin count recovers;
user-facing 503s stop as soon as the origin is marked healthy again.

## Prevention

- Treat the probe path as an API contract between CDN and origin:
  version it, and fail the origin's deploy pipeline if the health
  endpoint moves without a matching CDN change.
- Add a canary alert on probe *status codes*, not just health ratio:
  "probe 404 rate > 0" distinguishes "origin gone" from "origin moved"
  in one signal.
- After any origin replatform, run the health check from the CDN's
  vantage point (`curl https://www-origin.contoso-apps.com/health`
  with the probe's protocol and headers) before cutting traffic over.

## Further reading

- [Front Door health probes](https://learn.microsoft.com/azure/frontdoor/health-probes)
- [Azure Front Door — routing decision overview](https://learn.microsoft.com/azure/frontdoor/front-door-routing-architecture)
- [Front Door health probe log fields](https://learn.microsoft.com/azure/frontdoor/front-door-diagnostics)
