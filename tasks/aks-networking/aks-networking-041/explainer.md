# aks-networking-041 — explainer

## How to investigate

Start where the failure actually surfaces: `fixtures/batch-runner.log` shows
every POST to `orders.orders.svc.cluster.local:8080` dying with
`read: connection reset by peer`, five retries, five resets. "Connection reset
by peer" from a service that is up means the **server side closed the
connection mid-conversation** — the TCP connection was accepted and then torn
down. That is a different signature from a routing timeout (no connection ever
established) or a DNS failure (no attempt at all), and it is the signature of
a proxy rejecting the client at the protocol layer.

So go look at the server side. `fixtures/istio-proxy-orders.log` is the
orders sidecar's log, and it tells the same story from the other end: a burst
of `TLS handshake failed: SSL_ERROR_SSL ... wrong version number` from peer
`10.240.5.23`, interleaved with successful `TLSv1.3` handshakes from
`10.240.3.44` (payments). "Wrong version number" is Envoy-speak for *the
bytes arriving were not TLS at all* — a plaintext HTTP/1.1 request landed on a
listener that only speaks TLS.

Now connect the two IPs to infrastructure. `fixtures/kubectl-get-ns-labels.txt`
shows the batch namespace carries only `kubernetes.io/metadata.name=batch` —
no `istio.io/rev` label, so no pod in it was ever injected with a sidecar.
`10.240.5.23` is batch-runner (`fixtures/batch-runner.log` names the pod), so
batch-runner speaks plain HTTP to orders, and orders — behind its sidecar —
refuses it. Finally, `fixtures/peerauthentication.yaml` shows *why* the refusal
became fatal this week: a namespace-wide `PeerAuthentication` with
`mtls.mode: STRICT` on orders, part of the mesh rollout. STRICT means the
sidecar accepts only mutual TLS; before the rollout a stricter-on-paper
policy simply did not exist, so plaintext happened to work.

## Why the red herring misleads

`fixtures/destinationrule.yaml` is the trap, and it is a good one because it
*looks* like a TLS configuration mismatch: a `DestinationRule` pinning
`tls.mode: ISTIO_MUTUAL` for the orders host, sitting right next to a
plaintext client. The tempting story is "the client's TLS setting doesn't
match the destination's requirement" — change the DestinationRule and the
handshake should work.

It fails for a structural reason: a `DestinationRule` is consumed by the
**client's sidecar** to decide how to originate outbound traffic. batch-runner
has no sidecar — nothing in the batch namespace does
(`fixtures/kubectl-get-ns-labels.txt`) — so the DestinationRule is never read
by anyone on the sending side. It configures only mesh-internal callers, and
for them it is correct and healthy (payments connects fine). Rewriting it to
`tls.mode: DISABLED` would not help batch-runner at all and would silently
downgrade the mesh traffic that currently works. The rule "a sidecar-less
client is invisible to every Istio traffic object" is what argues the red
herring away: PeerAuthentication governs the *server* sidecar's acceptance;
DestinationRule governs a *client* sidecar's origination. Only one of those
exists here.

## Root cause

The orders namespace carries a namespace-wide `PeerAuthentication` named
`default` with `spec.mtls.mode: STRICT` (`fixtures/peerauthentication.yaml`),
introduced with the Istio add-on rollout. The legacy batch-runner pod
(`batch-runner-6c8f9d7b-x7q2p`, 10.240.5.23) lives in the batch namespace,
which has no `istio.io/rev` injection label (`fixtures/kubectl-get-ns-labels.txt`),
so it has no sidecar and sends plaintext HTTP. The orders sidecar therefore
rejects its handshake — repeated `TLS handshake failed ... wrong version
number` from peer 10.240.5.23 (`fixtures/istio-proxy-orders.log`) — which the
client experiences as connection resets on every POST
(`fixtures/batch-runner.log`). Mesh clients with sidecars, like payments,
negotiate mTLS and are unaffected.

## Fix

Two consistent directions; pick one:

1. **Mesh-enable the client** (the durable choice): label the batch namespace
   for injection and restart the workload, so batch-runner gets a sidecar that
   negotiates mTLS:

   ```bash
   kubectl label ns batch istio.io/rev=asm-1-23-5
   kubectl rollout restart deployment batch-runner -n batch
   ```

2. **Relax the policy for this caller**: set the namespace
   `PeerAuthentication` to `mtls.mode: PERMISSIVE`, or keep `STRICT` and add a
   port-level exception for the legacy path:

   ```yaml
   spec:
     mtls:
       mode: STRICT
     portLevelMtls:
       8080:
         mode: PERMISSIVE
   ```

Direction 1 keeps the zero-trust posture the rollout was bought for; direction
2 is faster but widens the namespace's attack surface (PERMISSIVE still
*accepts* plaintext forever — it is a migration state, not an end state). If
only one legacy workload needs the exception and nothing else will ever move
into batch, a `PeerAuthentication` scoped to the batch-runner workload
(via `selector`) with permissive mode on the **client** side is still wrong —
the deciding policy lives on the server, so the real scoped options are
`portLevelMtls` on orders or meshing the client. Do not touch
`fixtures/destinationrule.yaml`: it is correct for mesh callers and inert for
the sidecar-less one.

## Prevention

1. **Inventory non-mesh callers before any STRICT migration.** A namespace
   that opt-outs of injection is an implicit plaintext allowlist. Every
   mTLS rollout should ship with a query for sidecar-less workloads that
   call into STRICT namespaces — that list is the rollout's real blast
   radius.
2. **Treat PERMISSIVE as a timed migration state.** Put an expiry or follow-up
   ticket on every namespace left in PERMISSIVE, or the mesh quietly stops
   being mutual.
3. **Alert on the handshake signature.** Repeated Envoy `TLS handshake failed:
   wrong version number` from a stable peer IP means a non-mesh client found a
   STRICT listener — page on it before the nightly batch fails.

## Further reading

- [Istio — PeerAuthentication](https://istio.io/latest/docs/reference/config/security/peer_authentication/)
- [Istio — mutual TLS migration (PERMISSIVE as a stepping stone)](https://istio.io/latest/docs/ops/configuration/traffic-management/tls-migration/)
- [Istio — DestinationRule TLS settings and which proxy consumes them](https://istio.io/latest/docs/reference/config/networking/destination-rule/)
- [AKS — Istio add-on revision-based injection labels](https://learn.microsoft.com/azure/aks/istio-deploy-addon)
