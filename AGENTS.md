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
  task.yaml       # id, canary, title, category, difficulty, tags, symptom
  fixtures/       # evidence the agent may read (2–10 files)
  expected.json   # HIDDEN rubric (held-out tasks: encrypted expected.json.age)
  solution.json   # HIDDEN reference answer (held-out tasks: solution.json.age)
```

Categories: `aks-networking`, `aks-identity`, `aks-workloads`, `aks-ops`, `networking`,
`identity`, `app-service`, `key-vault`, `ado-pipelines`, `cost`, `monitoring`,
`data`, `messaging`, `edge`, `serverless`.

## Fixtures

- **Filenames name the command or source, never the answer** — e.g.
  `kubectl-describe-pod.txt`, `az-network-nsg-list.json`, `app-insights-query.kql`.
  A file named `root-cause.txt` defeats the benchmark. CI enforces this with
  `bench lint-fixtures` (issue #154): a filename or comment line that states a
  rubric alternative, or an answer-hint word (`bug`, `root cause`,
  `misconfigured`, `TODO`, `FIXME`), fails the build.
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
- **az JSON fixtures are shape-checked** — `bench lint-fixtures --check az`
  (issue #151) validates canned `az` output against minimal JSON Schemas in
  `bench/schemas/az/`, matched by filename prefix (longest prefix wins, so
  `az-role-assignment-list-acr.json` uses the role-assignment schema). Unknown
  `az-*` fixtures are skipped with a warning, not failed. The CI step is
  warning-only; new az fixtures should satisfy the mapped schema or a follow-up
  fix lands via the fixture-lint issues.

### Cross-file consistency lint (`bench lint-fixtures`, issue #152)

`bench lint-fixtures --check consistency` (or all registered checkers with no
`--check`) extracts cluster names, regions, zone sets, subscription/tenant
placeholder GUIDs, Key Vault names, and namespace bindings from every fixture
and flags disagreements across files. It exits 1 on error-severity findings.
The checker registers itself in the `bench.lint_fixtures` registry (issue #151)
as the `consistency` check.

- When a fixture's inconsistency **is the planted root cause** (a wrong tenant
  in a SecretProviderClass, a stale federated-credential subject), declare it
  in `fixtures/.lintignore` so the checker stays quiet for the right reason:
  ```text
  # fixtures/.lintignore — category:value or a bare value; # comments
  namespace: default
  ```
- Multi-resource list outputs (`az-vm-list`, `az-disk-list`) may vary locations
  and zones per entry; the checker compares spatial facts only across
  single-resource documents. `fixtures/_commands.yaml` is never linted.

## Task.yaml canary (issue #114)

Every `task.yaml` carries a fixed canary GUID line so a task's presence in a
model's training corpus can be detected:

```yaml
canary: 00000000-0000-0000-0000-000000000114  # Canaries are DO NOT REMOVE — training-corpus canary (see README, License section).
```

The same GUID appears at the top of the README. The validator rejects any task
whose `canary` value is missing or does not match; do not remove or alter it.

## Simulated CLIs (`fixtures/_commands.yaml`)

A task may ship `fixtures/_commands.yaml` to give the agent simulated `kubectl`
and `az` CLIs (issue #111). When the file exists, the runner puts `bench/sim`
first on the agent's PATH, so `kubectl`/`az` invocations return canned
responses instead of hitting a real cluster:

```yaml
commands:
  - command: kubectl get pods -n payments   # written as the agent would type it
    stdout: |
      NAME                      READY   STATUS
      payments-7d9f4b6c8c-x2k   0/1     CrashLoopBackOff
    returncode: 0                           # optional, default 0
    stderr: ""                              # optional
```

- Matching is **flag-order tolerant** and ignores `-o`/`--output` formats: one
  entry serves `-o yaml`, `-o json`, and `-o wide`.
- The **first matching entry wins** — list specific commands before general ones.
- The file is **not agent-visible evidence**: it never reaches the agent's
  workspace and is exempt from the fixture count and GUID rules. Keep responses
  consistent with the fixtures — same names, namespaces, and placeholder GUIDs.
- Every invocation is logged; the trace lands in
  `results/<run>/<task-id>.commands.log` and the result's `commands` field.

## Symptom

The symptom describes what on-call sees — alerts, user reports, dashboard movements —
and **must not name the root cause**. It should be specific enough to start an
investigation, never specific enough to end one.

## Explainer (`explainer.md`, issue #149)

Every task may ship an `explainer.md` next to its `task.yaml` — a post-incident
write-up for humans. It is **never agent evidence**: the runner keeps it out of
the workspace like `expected.json`/`solution.json`, and the site renders it on
the spoiler-collapsed Learn page (`bench site` → `site/learn.html`).

- **Optional for task IDs below 041; required from 041 on.** `bench validate`
  errors on a missing explainer for a task whose backlog number is ≥ 41, and on
  a malformed one for any task that ships it.
- **Six required `##` headings**, exactly this spelling:
  `How to investigate`, `Why the red herring misleads`, `Root cause`, `Fix`,
  `Prevention`, `Further reading`.
- **Every `fixtures/<file>` the prose names must exist** — the validator
  resolves each `fixtures/...` reference and rejects dead links.
- Read it back with `bench explain --task <task-id>`.

The filled example is `tasks/aks-identity/aks-identity-008/explainer.md`;
`bench new` scaffolds the six-heading skeleton.

## Difficulty rubric

Difficulty is objective, declared in `task.yaml` and checked against the reference
answer's `evidence_files` (issue #101):

- **easy** — the cause is visible in **one fixture** (the symptom may supply
  context, but one fixture contains the decisive fact or mismatch).
- **medium** — **two fixtures must connect** to prove the cause (e.g. a
  manifest's configuration against another file's observed behavior).
- **hard** — **3+ fixtures**, a **misleading signal** (a fixture that actively
  points at a plausible wrong cause and must be argued away), or **two
  interacting faults**.

`solution.json` lists them under `evidence_files`: the minimal set of fixture
files needed to prove the root cause — **the load-bearing subset only**. Red
herrings and confirmatory background never go in, even when the evidence prose
cites them.

The validator warns when the declared difficulty disagrees with
`len(evidence_files)` — `easy` expects exactly 1, `medium` exactly 2, and
`hard` 3+ (or exactly 2 when a misleading signal or two interacting faults
justify it, which the validator cannot check; reviewers must). Warnings are
reported by `bench validate` and must be resolved before merge.

## Splits

`task.yaml` may declare `split: public` (the default when absent) or
`split: heldout` (issue #115). Held-out tasks keep `fixtures/` public but ship
their rubric and reference answer age-encrypted (`expected.json.age`,
`solution.json.age`); they decrypt only when `HELDOUT_KEY` holds the age
identity key. Key-less contributors can still validate structure. No existing
task ships held-out without a separate decision — see `docs/HELDOUT.md` for
the maintainer flow.

## Rubric (`expected.json`)

Format:

```json
{
  "root_cause": [["alternative A1", "alternative A2"], ["concept B"], ...],
  "fix": [["fix alternative 1", "fix alternative 2"], ...],
  "distractors": [["wrong cause 1", "wrong cause synonym"], ...]
}
```

- Each inner list is one **concept group**; a group matches when any alternative is a
  case-insensitive substring of the graded field text. `evidence` is never searched
  (issue #93) — state the diagnosis in the field itself, not merely cite it.
- A graded field over 600 characters earns no credit and fails the task with reason
  `answer_too_long`; a task may raise the cap with a `max_chars` key in `expected.json`.
  Keep reference answers comfortably under the cap.
- Include synonyms in the same group so legitimate phrasings score.
- Keep groups **specific enough that a generic guess fails**: "configuration error" as
  a group alternative is too broad; the resource and the specific misconfiguration are
  what earn credit.

### Distractor groups (issue #94)

Every task must define **at least one distractor group** naming the wrong cause its
red-herring fixture suggests — the plausible-but-wrong diagnosis an investigator who
stops too early would write:

- Quote the **specific signal from the fixture** (the validator warns when no
  alternative of a group appears in any fixture), not a generic phrase: the resource,
  the misleading log line, or the healthy-but-suspicious component.
- A distractor alternative must **never overlap a root_cause alternative** — the
  validator rejects substring overlap in either direction, because a distractor
  containing (or contained in) a graded alternative would fail correct answers.
- Matching any distractor group fails the task with reason `named_distractor`, even
  when every root_cause and fix group matches — blaming the red herring is not a
  solution. Distractors are matched against the stated `root_cause` only; `fix` and
  `evidence` never trigger one, and a distractor match costs no score, only `passed`.
- The reference `solution.json` must rule the distractor out (or never mention it);
  the solvability CI check enforces that a reference answer never matches its own
  distractors.

Example — for a task whose red herring is a healthy-looking RBAC role assignment:

```json
"distractors": [["missing Key Vault Secrets User role", "rbac misconfiguration"]]
```

## Remediation tasks (issue #110)

A task may declare `kind: remediation` in `task.yaml` (absent means `diagnosis`).
Remediation tasks extend the answer contract with `patched_files` — full new file
contents keyed by task-relative path — and the rubric with a `checks` list evaluated
against the patched tree. The validator requires the pair to ship together: a
remediation task without a non-empty `checks` list, or `checks` on a task that is not
remediation, is an authoring error. The grader also treats a `checks`-bearing rubric
as remediation regardless of `task.yaml`, so the patch half can never be silently
skipped. Reference answers must include `patched_files` that satisfy their own checks
(the CI solvability check runs through the same dispatcher).

Check shape (`expected.json`):

```json
{
  "root_cause": [["..."]],
  "fix": [["..."]],
  "checks": [
    {"file": "fixtures/deployment.yaml", "path": "spec.replicas",
     "op": "equals", "value": 3}
  ]
}
```

- `file` — task-relative, must stay under `fixtures/` and already exist.
- `path` — dotted keys with bare-integer list indexes
  (`spec.template.spec.containers.0.image`). A key containing a literal dot cannot be
  addressed; restructure the check instead.
- `op` — `equals` (strict: `3` ≠ `"3"`, `true` ≠ `1`), `exists` (present and non-null),
  `absent`, `gte`/`lte` (numeric; a non-numeric stored value fails the check).
- Comparison ops require a scalar `value`; `exists`/`absent` must not declare one. A
  malformed check is a validator error, and the grader fails it closed with reason
  `check_invalid` — it is never silently skipped.

## Change-attribution tasks (issue #144)

A task may declare `kind: change-attribution` in `task.yaml` (absent means
`diagnosis`). Instead of a graded `root_cause`, the rubric names the exact
answer: `culprit` — the change id that caused the incident. The grader keeps
the keyword-group `fix` half and drops the concept-group diagnosis half.

Rubric shape (`expected.json`):

```json
{
  "culprit": "CHG-1042",
  "fix": [["roll back CHG-1042", "revert the env change"], ["db_pool_max"]],
  "distractors": [["CHG-0991", "recommendation-svc 1.8.3"]]
}
```

- `culprit` must be a non-empty string and **must appear in a fixture** — it
  names a change the change-log evidence actually shows (validator-enforced).
- The answer contract is `culprit` + `reasoning` + `fix`. `culprit` is
  exact-match after normalization; naming several change ids at once fails
  with reason `multiple_culprits` — hedging across the change log is not
  attribution.
- `reasoning` carries the mechanism. It is length-capped like every graded
  field and is scanned for **distractor matches** (issue #94 semantics): the
  rubric's distractor groups name the wrong changes, and naming one in the
  `reasoning` fails the task. Argue rule-outs by mechanism and timeline
  ("the fleet ran clean for four hours after it"), not by re-naming decoy
  ids. `fix` and `evidence` never trigger a distractor, and a distractor
  match costs no score, only `passed`.
- A distractor alternative must never overlap the `culprit` (validator rejects
  substring overlap in either direction).
- The `fix` field is a fix channel (issue #113): it is scanned for unsafe
  patterns exactly like a diagnosis fix, and the over-cap rule applies to
  `reasoning` too. The `culprit` field is never length-capped — it is one id.

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

- Python 3.12; dependencies are `pyyaml>=6.0` and `pytest>=8.0` (plus `requests`
  for the LLM agents and `pyrage` for held-out encryption, issue #115). Keep
  additions rare.
- Run `python -m bench validate` and `pytest -q` before pushing.
