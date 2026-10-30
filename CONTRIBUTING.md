# Contributing to Azure SRE Bench

Thank you for helping grow the benchmark. This guide walks through the full
contribution pipeline: propose a task, scaffold it, fill in the real content,
and open a reviewable PR. The authoring rules live in
[AGENTS.md](AGENTS.md) — read it before your first task.

## 1. Propose the task (issue form)

Open a GitHub issue using the **New task** template
(`.github/ISSUE_TEMPLATE/new-task.yml`). It asks for the category, symptom,
hidden root cause, fixture plan, red herring, rubric, and difficulty. One task
per issue. A maintainer signs off on the design before you start writing
fixtures — this keeps hidden rubrics from being designed around an answer the
issue text leaks.

## 2. Scaffold the task folder

Once the issue is approved, from the repository root:

```bash
python -m bench new <category> --title "One-line symptom title"
```

For example:

```bash
python -m bench new aks-networking --title "Ingress returns 502 after certificate rotation"
```

This creates `tasks/<category>/<category>-<nnn>/` with the next free backlog
number and placeholder files:

```
tasks/aks-networking/aks-networking-008/
  task.yaml       # id, title, category, difficulty, tags, symptom placeholders
  expected.json   # rubric with <REPLACE-ME-...> keyword groups
  solution.json   # reference answer with matching placeholders
  fixtures/       # two placeholder fixture files — replace both
```

`bench new` always scaffolds the next free backlog number, so re-running is
safe — it never touches an existing folder.

## 3. Fill in the real content

Replace **every** `<REPLACE-ME-...>` placeholder, following AGENTS.md:

- **Fixtures** — 2–10 realistic files named after the command or source
  (`kubectl-describe-pod.txt`, `az-network-nsg-list.json`), never the answer.
  Cross-file consistency matters: names, namespaces, zones, and timestamps
  must agree. Include at least one red herring. Only placeholder GUIDs
  (`00000000-0000-0000-0000-00000000000X`) — no real IDs or secrets.
- **Symptom** — what on-call sees; never names the root cause.
- **Rubric** (`expected.json`) — keyword groups specific enough that a generic
  guess fails.
- **Reference answer** (`solution.json`) — must pass its own task.

The scaffold is built so it passes `bench validate` as generated *and* after
you fill in the placeholders — but never commit an unfilled scaffold.

## 4. Verify locally

```bash
python -m bench validate        # structural checks over the whole corpus
pytest -q                       # includes per-task integrity checks
python -m bench catalog         # regenerate docs/TASKS.md; commit the diff
```

## 5. Open the PR

One task per PR, branch `task/<task-id>`, touching only that task's folder.
The PR template carries the full definition-of-done checklist — work through
it. CI enforces the mechanical rules (validation, catalog freshness, test
suite with integrity checks); reviewers hold the rest.

## Other contributions

Infrastructure, grader, runner, agent, and site contributions follow the same
flow minus the task sections of the checklist. Open an issue first for
anything that changes the scoring contract or the result schema — those are
load-bearing for result comparability.

## Licensing

Code is Apache-2.0. Task data (`tasks/`) is contributed under CC-BY-4.0.
