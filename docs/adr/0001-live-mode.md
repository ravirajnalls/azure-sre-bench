# ADR 0001: Live mode — kind in CI now, opt-in AKS later

- **Status:** Accepted
- **Date:** 2026-10-30
- **Deciders:** Raviraj Nallasivam, Phase 2 build (spec decision D6)
- **References:** [issue #126](https://github.com/ravirajnalls/azure-sre-bench/issues/126), Phase 2 spec `art_TRhJWSwQ`, scenario: `live/scenarios/service-selector-mismatch/`

## Context

The benchmark grades agents on pre-rendered fixture files. That measures
diagnosis from evidence, but not investigation against a real cluster: no
`kubectl` loop, no discovery of what to look at, no consequences of running
commands in the wrong order. Issue #126 asks for a **live track** prototype —
fault injection on a real cluster — for faults that are not Azure-specific.

Constraints that shape the decision:

- **The sandbox has no docker** (verified during phase-2 research); GitHub
  CI runners do. Whatever is built must be verifiable offline for structure
  and by CI for behavior.
- **Phase 2 has no cloud credentials** for anything (LLM endpoints were
  declined twice), and no Azure spend is approved.
- **Agents are untrusted code paths.** An agent under evaluation must not be
  able to mutate the system it is diagnosing.
- Fixture-corpus discipline applies: the symptom must not name the root
  cause, grading must be reproducible, and everything applied to the cluster
  should be reviewable as plain manifests.

## Decision drivers

1. Cost must be ~$0 per run and require no cloud credentials.
2. Every run must be reproducible in CI and locally with the same scripts.
3. The agent's access must be read-only, enforced by the platform (RBAC), not
   by agent good behavior.
4. Teardown must be guaranteed even when earlier steps fail.
5. The grading semantics must match the fixture benchmark so results are
   comparable.

## Considered options

**A. Stay fixture-only.** No live track. Rejected for the spike: it forgoes
the investigation loop entirely, which is the dimension issue #126 exists to
explore.

**B. kind cluster in CI, fault injected by manifests — CHOSEN.** kind
(Kubernetes-in-Docker) runs on GitHub-hosted runners at zero Azure cost and
zero credentials. Faults are declarative manifests applied by a setup script.
The same scripts run locally with any reachable cluster.

**C. AKS by default.** Realistic (LoadBalancer, CNI, Azure-specific signals)
but rejected as the default: per-run cost, credential distribution, IP/
resource cleanup risk, and slower, flakier CI. It also couples the benchmark
to Azure for faults that are Kubernetes-generic.

**D. AKS opt-in (deferred).** The right home for Azure-specific faults, kept
as a documented future extension, not built in phase 2. It needs a profile
(creds, region, teardown billing checks) and its own ADR when scheduled.

## Decision

Live mode ships as: **kind in CI as the execution substrate, one scenario,
declarative fault injection, read-only agent access via RBAC, guaranteed
teardown, and grader-side-only remediation.** AKS is explicitly out of scope
for phase 2 (spec D6: no AKS dependency).

### Fault injection

`setup.sh` applies versioned manifests (`app.yaml` = release state,
`fault.yaml` = the injected fault) — no imperative chaos tooling. The fault
is a stale Service selector (`tier: web` against pods relabeled
`tier: frontend`), mirroring fixture task `aks-networking-002`. `setup.sh`
verifies the fault took effect (storefront endpoints empty, cart endpoints
populated) before the agent is given access, and prints the symptom — which,
per corpus discipline, does not name the root cause.

### Read-only kubeconfig for the agent

`setup.sh` applies `agent-rbac.yaml`: a `ServiceAccount` in the scenario
namespace bound to a `Role` granting only `get`/`list`/`watch` on
pods (+`pods/log`), services, endpoints, events, deployments, and ingresses.
The agent kubeconfig is minted from a short-lived bound token
(`kubectl create token`, TTL 4h), written mode 0600, and gitignored.
`setup.sh` asserts the boundary in both directions — reads succeed, a
mutating verb (`delete pod`) is Forbidden — and CI re-probes it as a named
step. Cluster-scoped visibility is intentionally excluded.

### Remediation is grader-side only

The agent submits a proposed Service manifest; `remediate.sh` (running as the
grader, with admin kubectl) validates it — single document, `kind: Service`,
name `storefront`, namespace `shop` — refuses everything else, and applies
it. `check.sh remediation` then grades live state: endpoints repopulated and
the selector matching Running pods. This preserves the read-only boundary
while still letting the benchmark score a fix on the cluster.

### Grading semantics

`grade_diagnosis.py` mirrors `bench/grader.py`: lowercase text, keyword
groups matching on case-insensitive substring, evidence joined into the
root-cause text, `{"passed": bool, "reasons": [...]}` output with a reason
per unmatched group. The fixture grader stays the authority; the copy exists
because the live CI job never installs the harness. Both the diagnosis half
(text rubric) and the remediation half (cluster state) report reasons.

### Teardown

`teardown.sh` is best-effort by design (`set -uo pipefail`, every step `||`
logged, always exits 0): delete the namespace, remove the agent credential,
and — when `DELETE_CLUSTER=1`, which CI sets — delete the kind cluster. The
workflow runs it under `if: always()`, so a failed scenario can never leave
an orphaned cluster or credential behind.

### Cost and safety

- **Cost:** GitHub-hosted runner minutes only. No Azure resources, no
  ingress controller, no metrics-server. Node image is pinned
  (`kindest/node:v1.31.2`); job timeout 20 minutes.
- **Safety:** no cloud credentials exist in the job at all; the only
  credential minted is the agent's own read-only namespace-scoped token,
  TTL-bound, file mode 0600, gitignored, and deleted at teardown. The
  grader-side patch gate prevents an agent from turning "submit a fix" into
  "apply arbitrary YAML to the cluster".
- **CI surface:** `.github/workflows/live-mode.yml` is path-gated to
  `live/**` (plus `workflow_dispatch`), so the optional job only executes
  when live mode changes. It runs: shellcheck + `bash -n` → offline
  `--self-test` → kind setup → read-only probe → diagnosis grading both
  ways → remediation both ways → teardown.

## Consequences

**Positive**

- The investigation loop (discover → query → diagnose) is now benchmarkable
  with zero infrastructure cost and zero credentials.
- Everything the cluster runs is a committed manifest — faults are reviewable
  artifacts, not scripts of side effects.
- The read-only boundary is platform-enforced and CI-proven, not aspirational.

**Negative / accepted trade-offs**

- kind is not AKS: no LoadBalancer services, different CNI, no Azure
  identity surface. Live scores measure the Kubernetes investigation loop,
  not Azure semantics.
- One scenario today; conversion of the fixture-task candidates in
  `live/README.md` is manual per-task work (some need extra components —
  Calico for NetworkPolicy, metrics-server for HPA).
- `grade_diagnosis.py` duplicates grader semantics; drift is possible until
  issue #95's grader versioning makes the live grader carry a version too.

## Future work

- Opt-in AKS profile for Azure-specific live faults (separate ADR, needs
  credentials and a teardown/billing review).
- Convert the candidate fixture tasks listed in `live/README.md`.
- Wire live results into the leaderboard with an explicit
  `mode: live` marker so they never mix with fixture runs.
