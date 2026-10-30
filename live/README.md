# Live mode

Live scenarios grade an agent's investigation of a **real cluster with a real
injected fault**, instead of pre-rendered fixture files. Everything here runs
on kind (Kubernetes-in-Docker) in GitHub CI; no Azure resources and no cloud
credentials are involved. Design rationale lives in
[docs/adr/0001-live-mode.md](../docs/adr/0001-live-mode.md).

## Scenario contract

A live scenario is a directory under `live/scenarios/<name>/` with:

| File | Required | Role |
| --- | --- | --- |
| `setup.sh` | yes | Creates the cluster if needed, injects the fault, mints the read-only agent kubeconfig, verifies the fault is active, prints the symptom |
| `teardown.sh` | yes | Best-effort cleanup; CI runs it under `if: always()` |
| `check.sh` | yes | Grades the run: `diagnosis` (text rubric) and `remediation` (live cluster state) |
| `rubric.json` | yes | Keyword-group rubric, same shape as `expected.json` in the fixture corpus |
| `grade_diagnosis.py` | yes | Standalone matcher mirroring `bench/grader.py` semantics (the CI job has no harness install) |
| `remediate.sh` | if the scenario has a remediation track | Grader-side patch application, scoped to the faulted resource |
| `app.yaml` / fault manifests | yes | Declarative fault injection — applied by `setup.sh`, inspectable in review |
| `agent-rbac.yaml` | yes | The agent's read-only identity |
| `samples/` | yes | Correct/wrong diagnosis answers and good/bad patches, used by `--self-test` and CI |

Rules every live scenario must keep:

1. **The agent never mutates the cluster.** Investigation happens through a
   read-only kubeconfig; the only mutation path is the grader-side
   `remediate.sh`, scoped to the faulted resource.
2. **The symptom never names the root cause** — same information boundary as
   the fixture corpus.
3. **Faults are declarative.** `setup.sh` applies manifests; no imperative
   chaos tooling. What CI applies is what a reviewer reads.
4. **Teardown always runs**, never aborts the workflow on partial failure,
   and removes the agent credential.

## Running in CI

`.github/workflows/live-mode.yml` runs each scenario end to end on kind:
lint → offline self-test → setup → read-only enforcement probe → diagnosis
grading (correct passes, wrong fails) → remediation (bad patch refused, good
patch restores endpoints) → teardown. The workflow is path-gated to `live/**`
plus `workflow_dispatch`, so it only executes when live mode changes.

## Candidate tasks for conversion

From the current 32-task corpus, these fixture tasks are Kubernetes-generic
(not Azure-specific) and could become live scenarios next:

| Task | Fault | Kind compatibility note |
| --- | --- | --- |
| `aks-networking-004` | NetworkPolicy blocks postgres traffic | Requires a kind CNI that enforces NetworkPolicy (e.g. Calico) — `kind create cluster --config` with a networking patch |
| `aks-workloads-012` | Bad liveness probe → CrashLoopBackOff | No extra components |
| `aks-workloads-013` | Memory limit too low → OOMKilled | No extra components |
| `aks-workloads-014` | nodeSelector matches no nodes → Pending | No extra components |
| `aks-workloads-016` | Readiness probe fails after image bump | No extra components |
| `aks-workloads-017` | HPA never scales out (metrics mismatch) | Needs metrics-server installed on kind |
| `aks-ops-018` | PDB blocks node drain during upgrade | No extra components; drain simulation on kind nodes |

Tasks that are inherently Azure-specific (private clusters, Azure RBAC,
Workload Identity, ACR, Log Analytics, cost) are not conversion candidates —
their decisive evidence does not exist on kind.
