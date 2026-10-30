# Baseline sweep results (issue #125)

This file records the baseline sweep protocol and, once a real sweep runs, its
findings. The harness shipped before the numbers: the sweep is fully built and
mock-tested, but the LLM entries need credentials that are not configured
(declined during phase 1), so no real model numbers exist yet.

**Credential gap:** entries `llm-agent-foundry-gpt-4o`, `llm-agent-llama-70b`,
`react-agent-foundry-gpt-4o`, and `foundry-agent-gpt-4o` in
`sweeps/baseline.yaml` declare `requires_env` for
`LLM_BASE_URL` / `LLM_API_KEY` / `LLM_MODEL` (and the `FOUNDRY_*` equivalents).
Until those variables are present, `bench sweep` skips them and records
`status: skipped, reason: "missing-env: ..."` in the sweep report — the gap is
explicit and machine-readable, not silently empty. When credentials are
approved, re-running the sweep fills them in; no code changes are needed.

## Running the sweep

```bash
# Inspect the plan without running anything (shows what would be skipped):
python -m bench sweep --config sweeps/baseline.yaml --dry-run

# Run it. Rule-based agents run anywhere; credential-gated entries skip
# themselves when their env is absent.
python -m bench sweep --config sweeps/baseline.yaml
```

Each agent entry produces `results/<agent-name>/` — one graded result record
per task plus an `agent.yaml` descriptor (issue #118 schema), so every
sweep-produced run folder is a valid submission shape (`python -m bench verify
results/<agent-name>`). The sweep refuses to write into a run folder that
already exists (a committed baseline is never silently clobbered); remove the
folder or set `overwrite: true` in the config to refresh it deliberately. The
sweep summary lands at `results/<sweep-name>.sweep.json`, with per-agent pass
counts, skip reasons, and an `adversarial_clean` verdict.

## Acceptance criterion: adversarial agents score 0

The sweep reports `gameable: true` for any `adversarial: true` entry that
passed at least one task, and prints a warning with a pointer to
`tests/known_gameable.txt`. Post-#93, fixture-dump and minimal-dump agents
already fail every task; keyword-soup still passes the corpus until the rubric
audits (#96–#99) close it. A sweep with `"adversarial_clean": true` is the
issue's "adversarial agents score 0" criterion, satisfied in CI by the
gameability gate once the audits land.

## Findings template (fill after a real run)

- **Scores by category and difficulty** — per-agent pass rate broken down by
  category (`aks-networking`, `aks-identity`, `aks-ops`, `aks-workloads`,
  `monitoring`, `cost`, ...) and difficulty.
- **Where agents fail** — red herrings (distractor groups matched instead of
  the root cause), multi-hop tasks (3+ evidence files), and hard tasks.
- **Cost and time per task** — from the `usage` object and `elapsed_s` in the
  result records (#112 fields).
- **Cross-model comparison** — the three models in the config: one Foundry
  deployment, one open-weights model, one hosted frontier model.
