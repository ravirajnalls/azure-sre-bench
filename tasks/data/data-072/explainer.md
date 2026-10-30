# data-072 — explainer

## How to investigate

Triage a cascade by ordering, then corroborate with the evidence. The alert
list (`fixtures/alerts.json`) has seven active alerts — and exactly one of
them fired long before today's incident: **ALT-06**, the Key Vault
certificate alert, fired **2026-10-23 at 09:20 UTC**, seven days before the
other six, and has stayed firing ever since. Every other alert appeared
between 09:17 and 09:23 this morning. A root cause cannot postdate its own
symptom, and an alert that predates the storm by a week and never cleared
is either the cause or a dead end worth checking — read it first.

Test the ordering story against the evidence:

- **The certificate** (`fixtures/keyvault-certificate-show.json`):
  `app-mtls-client` in `kv-prod-shared-01` has
  `attributes.expires = 2026-10-30T09:15:00+00:00` — expired this morning.
  Its policy names **only an `EmailContacts` lifetime action at 7 days
  before expiry** with a **self issuer**: no auto-renewal was ever
  configured, so the seven-day warning email was the entire renewal
  pipeline. The `pending.error` block confirms no renewal action ran.
- **The application log** (`fixtures/api.log`): the pool warm-up at
  09:15:02 still reports `redis=ok sql=ok`; from **09:15:47** — 47 seconds
  after the cert's expiry instant — the log fills with
  `certificate has expired` TLS handshake failures to the Redis endpoint
  and `TLS client authentication rejected` on the SQL login, followed by
  5xx responses, the circuit opening, and consumers going idle.
- **The edge** (`fixtures/appgw-backend-health.json`): both servers in
  `api-backend-pool` are `Unhealthy` with
  `CERT_HAS_EXPIRED` on the HTTPS probe — the app gateway presents the
  same `app-mtls-client` authentication certificate when it probes the
  backends, so it fails for the same reason.

Every other alert is downstream of the expired client certificate:

- **ALT-07 (AppGwBackendUnhealthy)** — the first fresh alert (09:17:41):
  probes present the expired client cert and fail the handshake.
- **ALT-03 (RedisTlsHandshakeErrors)** — Redis requires mutual TLS; every
  reconnecting client presents the expired cert and fails before AUTH.
- **ALT-02 (SqlConnectionFailureRateHigh)** — the SQL logical server is
  configured for certificate-based client authentication only; logins fail
  at the TLS handshake, not at the credential check.
- **ALT-01 (Api5xxRateHigh)** — the order API cannot reach its session
  cache or its database, so data-tier endpoints return 500/503.
- **ALT-04 (QueueBacklogGrowing)** — checkout consumers abort on the
  failing Redis session lookup before they consume, so the backlog climbs.
- **ALT-05 (SyntheticEndToEndFailure)** — the synthetic journey's first
  data-tier step fails from all probe regions; it is a symptom, not a
  source.

## Why the red herring misleads

The loudest candidates are the freshest alerts. **ALT-07
(AppGwBackendUnhealthy)** fires first this morning, and an edge outage
*looks* like a root: if the app gateway cannot reach backends, everything
behind it seems down. But the probe log names the exact failure —
`CERT_HAS_EXPIRED` — and the same cert error appears in `api.log` for
Redis and SQL connections that never touch the app gateway. A shared
failure across three independent TLS consumers points at the one thing
they share: the client certificate. **ALT-02 and ALT-03** are equally
traps: a 100% SQL login failure rate reads like an outage of the database,
and the alert summary deliberately notes "clients report TLS handshake
errors, not wrong credentials" — credential and service-health
investigations waste the night. The genuine discipline in this task is
checking the alert that fired **seven days ago**: most on-call attention
flows to the last five minutes, and a warning that has been quiet and
firing for a week is invisible in exactly the way that matters.

## Root cause

**ALT-06 — KeyVaultCertificateNearExpiry.** The `app-mtls-client`
certificate used by orders-api for mutual TLS to Redis, SQL, and the App
Gateway backends expired at 2026-10-30T09:15:00Z. The Key Vault alert had
fired continuously since 2026-10-23 (the policy's 7-day-before-expiry
trigger), but the policy's only lifetime action is `EmailContacts` on a
self-issued certificate — no auto-renewal — so the warning was never
acted on. From 09:15:47 the app presents an expired client cert on every
mTLS handshake, and the six downstream alerts follow within eight minutes.

## Fix

Rotate the certificate, not the services:

1. Renew `app-mtls-client` in `kv-prod-shared-01` — issue a new cert (or
   configure a real issuer and trigger renewal), then re-sync the
   certificate to the consumers (the app reads it at startup, so restart
   orders-api pods after the new cert lands).
2. Confirm recovery in order: `api.log` TLS handshakes succeed → App
   Gateway backends go `Healthy` → SQL logins and Redis AUTH succeed →
   5xx rate returns to baseline → the queue drains → the synthetic journey
   passes end to end.
3. Clear the stale warning: ALT-06 resolves only when the cert is renewed;
   do not silence it while the old cert is still deployed anywhere.

Do not roll back the platform, rotate database credentials, or rebuild the
app gateway — the database and cache are healthy, the credentials are
valid, and the edge is misfiring only because the probes inherit the
expired client cert.

## Prevention

- Configure **auto-renewal or an issuer with automation** for every
  production certificate; an email-only lifetime action on a self-issued
  cert is a reminder, not a renewal pipeline. Treat "certificate expires
  in N days" alerts as page-worthy at N=7 and verify a renewal job exists.
- **Monitor expiry on the consumers too**: deploy an admission or config
  check that fails (or alerts) when a workload's mounted client certificate
  is within 14 days of expiry — the certificate store is not the only
  place expiry matters.
- Alert on **renewal failures explicitly**: the `pending.error` state in
  the Key Vault certificate is machine-readable; a tiny watcher on it
  catches "nobody renewed the cert" weeks before the outage.
- Test the **renewal-to-consumer path** end to end in drills: new cert →
  vault sync → app restart → mTLS handshake, because each hop can go stale
  independently.

The naive triage — "the SQL database is down" or "the app gateway is
broken" — chases the loudest fresh alert and never explains why Redis,
SQL, and the probes fail identically at the same second. The only alert
that orders the causality is the one that fired seven days ago and never
cleared; the fix is one certificate rotation, not six service recoveries.

## Further reading

- [Azure Key Vault best practices](https://learn.microsoft.com/azure/key-vault/general/best-practices) —
  renewal policy, lifetime actions, and the gap between notification and
  automation.
- [Key Vault Certificate Near Expiry metric](https://learn.microsoft.com/azure/key-vault/certificates/certificate-life-cycle) —
  what the near-expiry signal covers and when it fires.
- [Application Gateway end-to-end TLS with authentication certificates](https://learn.microsoft.com/azure/application-gateway/configuration-overview) —
  why the gateway presents a client certificate to backends and how probes
  inherit it.
- [Azure Cache for Redis TLS configuration](https://learn.microsoft.com/azure/azure-cache-for-redis/cache-configure#tls-configuration) —
  server-side TLS requirements and client handshake behavior.
