# AGENTS.md — Authoring Conventions

Rules for contributing a task to Azure SRE Bench. Every task PR must follow all of
them; CI validates the mechanical ones and reviewers hold the rest.

## PR rules

- **One task per PR.** Branch `task/<task-id>` and touch **only that task's folder**.
- Task ID is `<category>-<nnn>` where `nnn` is the backlog number, zero-padded
  (e.g. `aks-networking-001`). The folder name, the `id` field in `task.yaml`, and the
  parent directory must agree. Note: the backlog number is not the GitHub issue number
  (e.g. task `aks-networking-001` is issue #9).

## Layout

```
tasks/<category>/<task-id>/
  task.yaml       # id, title, category, difficulty, tags, symptom
  fixtures/       # evidence the agent may read (2–10 files)
  expected.json   # HIDDEN rubric
  solution.json   # HIDDEN reference answer
```

Categories: `aks-networking`, `aks-identity`, `aks-workloads`, `aks-ops`, `networking`,
`identity`, `app-service`, `key-vault`, `ado-pipelines`, `cost`, `monitoring`.

## Fixtures

- **Filenames name the command or source, never the answer** — e.g.
  `kubectl-describe-pod.txt`, `az-network-nsg-list.json`, `app-insights-query.kql`.
  A file named `root-cause.txt` defeats the benchmark.
- **Use real tool output formats** — actual `kubectl`, `az`, KQL, and log formats, with
  plausible apiVersions, field names, and schema shapes.
- **Cross-file consistency** — names, namespaces, zones, and timestamps must agree
  across every file in the task. A pod referenced in one file must exist with the same
  name and namespace in the others.
- **At least one red herring**: a plausible but healthy signal an investigator would
  check and rule out.
- **3–7 fixture files**, each under ~150 lines.
- Only placeholder GUIDs of the form `00000000-0000-0000-0000-00000000000X` (last hex
  digit varies). **No real subscription IDs, tenant IDs, resource IDs, or secrets.**

## Symptom

The symptom describes what on-call sees — alerts, user reports, dashboard movements —
and **must not name the root cause**. It should be specific enough to start an
investigation, never specific enough to end one.

## Rubric (`expected.json`)

Format:

```json
{
  "root_cause": [["alternative A1", "alternative A2"], ["concept B"], ...],
  "fix": [["fix alternative 1", "fix alternative 2"], ...]
}
```

- Each inner list is one **concept group**; a group matches when any alternative is a
  case-insensitive substring of the graded field text (for `root_cause`, the joined
  `evidence` list is appended to the searched text).
- Include synonyms in the same group so legitimate phrasings score.
- Keep groups **specific enough that a generic guess fails**: "configuration error" as
  a group alternative is too broad; the resource and the specific misconfiguration are
  what earn credit.

## Reference answer (`solution.json`)

The reference answer must score `passed == true` on its own task when run through the
grader — this is enforced in CI (solvability check). Write it in the same
`{"root_cause": str, "evidence": [str], "fix": str}` shape the agent contract requires.

## Integrity invariants (CI-enforced)

1. **Information boundary** — `expected.json` and `solution.json` never reach the
   agent's temp dir; a test asserts it.
2. **Solvability** — the reference `solution.json` passes its own task.
3. **Non-triviality** — the `naive` agent, run through the real runner, must not pass.
4. **No leaks** — grading the symptom text alone must not match every root-cause group.
5. **Gameability** — the adversarial probes in `agents/adversarial/` (issue #92), run
   through the real runner against every task, must not pass any task outside
   `tests/known_gameable.txt`, and every entry in that list must still be gameable —
   the list is enforced in both directions and must shrink to empty as grader
   hardening (#93/#94) and rubric audits (#96-#99) land. Probe results land under
   `results/adversarial-*/` and never rank on the leaderboard or site.

## Tooling

- Python 3.12; dependencies are `pyyaml>=6.0` and `pytest>=8.0` only (plus `requests`
  for the LLM agents). Keep it that way.
- Run `python -m bench validate` and `pytest -q` before pushing.
