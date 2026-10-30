---
name: New task
about: Propose a benchmark task for Azure SRE Bench
labels: task
---

<!--
One task per issue. The issue captures the design before any code is written;
the implementation PR must touch only `tasks/<category>/<task-id>/` and follow
[AGENTS.md](AGENTS.md). Note: the task ID backlog number (`<category>-<nnn>`) is
not the GitHub issue number.
-->

**Title:** `Task <category>-<nnn> — <one-line symptom>`

**Depends on:** #<issue numbers this task depends on, if any>

**Symptom** — What on-call sees: alerts, user reports, dashboard movements. It
must not name the root cause — specific enough to start an investigation, never
specific enough to end one.

**Hidden root cause** — The underlying cause the agent must pin down. Keep it
concrete and diagnosable from the fixtures alone.

**Fixtures** — 3–7 files, each under ~150 lines, named after the command or
source (`kubectl-describe-pod.txt`, `az-network-nsg-list.json`), never the
answer. At least one **red herring**: a plausible but healthy signal an
investigator would check and rule out. Names, namespaces, and timestamps must
agree across files. Use only placeholder GUIDs
(`00000000-0000-0000-0000-00000000000X`) — no real IDs or secrets.

**Rubric** — Keyword groups for `expected.json`:
- `root_cause`: `[concept A | alternate phrasing] + [specific detail]`
- `fix`: `[acceptable remediation 1 | 2 | 3]`

**Difficulty** — easy / medium / hard
