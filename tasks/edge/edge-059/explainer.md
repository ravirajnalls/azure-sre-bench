# edge-059 — explainer

## How to investigate

The symptom statement already splits the world in two: same token,
through APIM it fails, direct to the backend it succeeds. So the token
is in scope and the backend is in scope, but the *path between them*
is the prime suspect. Confirm the token half cheaply:
`fixtures/token-claims.json` shows signature valid, `expired: false`,
audience and scopes matched — an auth *issuer* problem would not
produce "works directly, fails through the gateway."

Now look at what the gateway does to the request. `fixtures/apim-trace.json`
is the highest-value artifact: the inbound request carries
`Authorization: Bearer …`, the policy trace lists `set-header
Authorization delete` among the applied inbound policies, and the
request actually sent to the backend has **no** Authorization header —
the backend answers 401. `fixtures/backend.log` agrees from the other
side: gateway-sourced calls fail with "missing Authorization header"
while the peered internal path (which never passes through APIM)
returns 201. Finally, `fixtures/apim-policy.xml` shows the line that
does it, added in the revision the symptom timeframe points at —
tucked under a comment about header normalization.

## Why the red herring misleads

`fixtures/apim-product-policy.xml` contains a `rate-limit-by-key`
policy, and "APIs failing after a policy change" primes a throttling
diagnosis. Two observations argue it away. Rate limiting answers 429
with `Retry-After`; every failure here is 401 with
`WWW-Authenticate: Bearer`. And the limit is 100 successful calls per
minute per client IP — the failing calls return errors, and errors
don't count against the limit (`increment-condition="success"`), so
the throttle could not starve traffic in the observed pattern. The
product policy is real, it is doing its job, and it is not the fault.

## Root cause

The 2026-10-29 API-level policy revision added
`<set-header name="Authorization" exists-action="delete" />` to the
inbound section (`fixtures/apim-policy.xml`). APIM obediently strips
the caller's bearer token from every forwarded request, the backend's
auth middleware sees no credentials, and every gateway call returns
401 — regardless of token validity. The change note assumed the
backend would mint its own session from the subscription key, but the
backend still expects a bearer token, and the two beliefs never met
before the deploy.

## Fix

Remove the `set-header Authorization exists-action="delete"` line from
the API-level inbound policy and redeploy. If the intent was "APIM
normalizes auth," the correct mechanism is
`<set-header name="Authorization" exists-action="override">` with an
explicitly minted value — not a delete. After redeploying, confirm
with a trace (`fixtures/apim-trace.json` shape) that the forwarded
backend request carries the Authorization header and the backend
answers 200/201 through the gateway.

## Prevention

- Gate APIM policy changes with a canary subscription: one synthetic
  call per API through the gateway in CI, asserting the backend's
  response — a policy that deletes auth fails in seconds, not after
  partner integrations page on-call.
- Require the change ticket to name the *contract* being changed:
  "backend mints its own session" was an assumption, not an
  agreement with the orders-api team.
- Alert on 401-rate-per-route as a deploy-health signal for
  auth-bearing APIs; a policy regression moves 401s, throttling moves
  429s — track them separately.

## Further reading

- [APIM set-header policy](https://learn.microsoft.com/azure/api-management/set-header-policy)
- [APIM policy expressions and context](https://learn.microsoft.com/azure/api-management/api-management-policy-expressions)
- [APIM tracing / debug](https://learn.microsoft.com/azure/api-management/api-management-howto-app-insights)
