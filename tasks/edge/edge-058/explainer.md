# edge-058 — explainer

## How to investigate

A 403 on one form while everything else works means the request is
being rejected by something in front of the app, not by the app. Read
`fixtures/app.log` first: page views arrive all morning, but the log
has **no** `POST /api/support/ticket` entries at all — the last ticket
creation in the file predates the weekend. An application error would
leave an application log line; silence means the request never made it
through the gateway layer.

So ask what inspects request bodies in front of the app. The WAF does.
`fixtures/waf-firewall-log.kql.csv` is unambiguous: every blocked entry
is `RuleId 942100` — "SQL Injection Attack Detected via libinjection"
— on `POST /api/support/ticket`, action `Blocked`, status 403. Then
`fixtures/az-network-application-gateway-waf-policy-show.json` supplies
the policy's posture: `mode: "Prevention"`, `requestBodyCheck: true`,
OWASP CRS 3.2 with an empty `ruleGroupOverrides` list — the rule
blocks, and nothing scopes it. Finally,
`fixtures/request-sample.txt` shows the trigger is not an attacker: a
real customer's free-text description contains `'1'='1'` and
quote-heavy prose ("name'123", "select the line item"), which is
exactly the token pattern libinjection scores as SQL injection. The
rule is doing precisely what it was written to do — to a form field
that was never a threat.

## Why the red herring misleads

`fixtures/backend-health.json` looks like the natural suspect when a
form fails: two backend servers, health probes, status codes. But both
members of `appgw-backend-pool` report `Healthy` with 200 probes, and
the app log shows traffic flowing to the same backends all morning.
When a backend is down, the gateway answers 502, not 403; and page
views share the same pool with the form. The 403 has a different
signature: it is generated at the WAF layer with a rule ID attached,
before any backend is chosen. Healthy probes and failing form posts
are both true — and only one of them is a fault.

## Root cause

`waf-policy-appgw` runs OWASP CRS 3.2 in Prevention mode with request
body inspection enabled (`fixtures/az-network-application-gateway-waf-policy-show.json`),
and managed rule 942100 (libinjection SQLi) blocks every
`POST /api/support/ticket` whose `description` field contains
quote patterns such as `'1'='1'` (`fixtures/waf-firewall-log.kql.csv`).
Legitimate customers quote things in free text; the rule cannot tell a
pasted invoice from an injection, so in Prevention mode the submission
dies at the gateway with 403 and never reaches the app.

## Fix

Tune the WAF, don't rewrite the app. The targeted fix is an exclusion
request that exempts the `description` field (RequestFormFieldSelect)
from rule 942100 for the `/api/support/ticket` route — the app still
parameterizes its SQL, so the rule is redundant *for that field*. A
scoped custom rule allowing the endpoint, or moving the policy to
Detection mode while the exclusion ships, are acceptable interim
postures. Avoid the blunt version — disabling 942100 globally —
because the same rule protects every other route through the gateway.
After the change, watch the firewall log: `Blocked` entries for
942100 on the form should drop to zero while scanner blocks continue.

## Prevention

- Run new WAF policies in Detection mode for a soak period before
  switching to Prevention, and review the log-only hits for your real
  endpoints before flipping the switch.
- Treat free-text fields as known false-positive sources: ship the
  exclusions for them as code, alongside the form itself, so a new
  form never lands without its WAF tuning.
- Alert on WAF `Block` rate per route: one route dominating the block
  counts for hours is a tuning problem, not an attack.

## Further reading

- [Web Application Firewall exclusions on Application Gateway](https://learn.microsoft.com/azure/web-application-firewall/application-gateway/waf-request-size-limits)
- [OWASP CRS rule 942100 — libinjection SQLi](https://github.com/coreruleset/coreruleset/blob/v3.2.0/rules/sql-injection-attacks.conf)
- [Application Gateway WAF policy modes](https://learn.microsoft.com/azure/web-application-firewall/ag/application-gateway-overview#waf-modes)
