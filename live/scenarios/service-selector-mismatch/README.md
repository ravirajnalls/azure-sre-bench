# Scenario: Service selector mismatch

A live reproduction of the `aks-networking-002` incident: this morning's
release relabeled the storefront pods (`tier: web` → `tier: frontend`) but the
`storefront` Service was never updated, so its selector matches zero pods and
ingress-nginx returns 502 for `/shop`. The `cart` workload is healthy and
provides the contrast signal.

## Layout

| File | Role |
| --- | --- |
| `app.yaml` | The release state: storefront + cart Deployments (new pod labels) and the healthy `cart` Service |
| `fault.yaml` | THE FAULT: the `storefront` Service still selecting `tier: web` |
| `agent-rbac.yaml` | Read-only `ServiceAccount` (`sre-agent`) the agent investigates with |
| `rubric.json` | Keyword-group rubric, same shape as `expected.json` in the fixture corpus |
| `setup.sh` | Creates cluster if needed, injects the fault, mints the read-only kubeconfig, verifies the fault is active |
| `remediate.sh` | Grader-side: applies the agent's proposed patch (Service `shop/storefront` only) |
| `check.sh` | Grades the diagnosis (`diagnosis`) or the live cluster state (`remediation`) |
| `teardown.sh` | Best-effort: deletes the namespace, removes the kubeconfig, optionally the kind cluster |
| `grade_diagnosis.py` | The diagnosis grader — mirrors `bench/grader.py` keyword-group semantics |
| `samples/` | Committed fixtures used by `--self-test` and CI: correct/wrong diagnoses, good/bad patches |

## Run it

```bash
# Any cluster reachable by kubectl works; with none, setup creates a kind
# cluster (requires docker + kind on PATH).
live/scenarios/service-selector-mismatch/setup.sh

# The agent investigates with the read-only kubeconfig:
export KUBECONFIG="$PWD/live/scenarios/service-selector-mismatch/agent.kubeconfig"
kubectl get pods,svc,endpoints -n shop

# Grade (grader-side, admin kubectl):
live/scenarios/service-selector-mismatch/check.sh diagnosis \
  --answer live/scenarios/service-selector-mismatch/samples/diagnosis-correct.json
live/scenarios/service-selector-mismatch/remediate.sh \
  --patch live/scenarios/service-selector-mismatch/samples/patch-good.yaml
live/scenarios/service-selector-mismatch/check.sh remediation

live/scenarios/service-selector-mismatch/teardown.sh   # add DELETE_CLUSTER=1 to drop kind too
```

Offline (no cluster): `check.sh --self-test` grades the committed samples
through the same matcher, and `bash -n`/`shellcheck` lint the scripts.

## Agent contract

- **Inputs:** the symptom block printed by `setup.sh` (what on-call sees — it
  does not name the root cause) and a read-only kubeconfig. No fixture files.
- **Investigation:** any read-only `kubectl` against the `shop` namespace.
  Mutating verbs are RBAC-forbidden; `setup.sh` verifies both directions
  before handing the scenario over.
- **Diagnosis submission:** `answer.json` with the same
  `{"root_cause": str, "evidence": [str], "fix": str}` shape the fixture
  benchmark requires. Graded by `check.sh diagnosis`.
- **Remediation submission (optional track):** a single-document Service
  manifest for `shop/storefront`. `remediate.sh` refuses anything else;
  `check.sh remediation` then asserts the live state — endpoints populated
  and the selector matching Running pods.

## Scope notes

- The incident is reproduced at the Service/endpoints layer; no ingress
  controller runs in the cluster — the 502 and ingress-nginx log entries are
  the incident narrative, not something the agent must reproduce.
- The symptom text is adapted from `aks-networking-002` (the `/account`
  service is omitted — only `cart` exists here).
- Environment knobs: `CLUSTER_NAME`, `KIND_NODE_IMAGE`, `AGENT_KUBECONFIG`,
  `TOKEN_TTL`, `AGENT_ID`, `WAIT_TIMEOUT`.
