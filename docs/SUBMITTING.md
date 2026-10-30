# Submitting results to Azure SRE Bench

Anyone can benchmark their agent on the public corpus and submit the run to the
leaderboard. This document describes the process, what we verify, and what we
reject.

## The short version

1. Run your agent on every task:

   ```bash
   python -m bench run --agent "<your agent command>" --name <your-agent>
   ```

   This writes `results/<your-agent>/<task-id>.json` — one record per task with
   the raw answer your agent produced and the score the grader gave it.

2. Add a descriptor at `results/<your-agent>/agent.yaml` (schema below).

3. Verify your own run before opening the PR:

   ```bash
   python -m bench verify results/<your-agent>
   ```

4. Stage the run folder (`git add -f results/<your-agent>/` — the `-f` is
   intentional: the ignore rules keep local harness runs out of git unless
   someone deliberately commits them) and open one PR per agent, adding
   **only** that run folder. GitHub starts with the
   [submission template](.github/PULL_REQUEST_TEMPLATE/submission.md)
   (`?template=submission`). CI re-verifies the run; a reviewer confirms the
   agent is what it says it is, then the run joins the leaderboard.

## `agent.yaml` schema

Every run folder must carry this descriptor. All fields are required; a missing
or invalid field fails `bench verify`.

| Field | Type | Meaning |
| --- | --- | --- |
| `name` | string | Run/agent name; must match the folder name. |
| `version` | string | Version of the agent you ran (free-form). |
| `model` | string | Model ID, or `none` for rule-based agents. |
| `provider` | string | Model provider, or `none`. |
| `framework` | string | Agent framework used, or `none`. |
| `open-source` | yes/no | Whether the agent's source is publicly available. |
| `command` | string | The exact command a reproducer would run. |
| `date` | date | Run date, `YYYY-MM-DD`. |
| `contact` | string | Email or GitHub handle for questions about the run. |
| `repository` | string, optional | http(s) URL of the agent's public repo. The leaderboard site links the agent's name to it (issue #119); anything that is not an http/https URL is ignored. |

Example:

```yaml
name: my-react-agent
version: "0.3.1"
model: gpt-4o
provider: openai
framework: langchain
open-source: yes
command: python -m my_agent.bench_runner
date: 2026-01-15
contact: you@example.com
repository: https://github.com/you/my-agent
```

## Result record schema (version 1)

Each `results/<your-agent>/<task-id>.json` is one JSON object written by
`bench run` (or `bench submit` for the manual protocol). The shape is versioned:
`schema_version` in every record identifies it, and `bench/__init__.py` carries
the matching `RESULT_SCHEMA_VERSION`.

| Field | Type | Meaning |
| --- | --- | --- |
| `task` | string | Task ID the record grades. |
| `schema_version` | int | Result-record schema version (`1`). |
| `grader_version` | int | Version of the grading semantics that produced `score`/`passed`. |
| `rubric_sha256` | string | Content hash of the task rubric at grading time. |
| `agent` | string | Run/agent name; matches the folder name. |
| `score` | number | Matched concept groups over total groups (3 decimal places). |
| `passed` | bool | Whether every group matched and no violation fired. |
| `detail` | object | Per-field match results plus the failure `reasons` list. |
| `answer` | object | The raw answer the agent produced, evidence-capped. |
| `elapsed_s` | number | Wall-clock seconds the agent ran. |
| `stderr_tail` | string | Last 2 KB of the agent's stderr. |
| `timed_out` / `returncode` | bool / int | Process outcome (`returncode` null on timeout). |
| `commands` | list[string] | Simulated-CLI invocations, in order (empty without sim CLIs). |
| `mode` | string | `"manual"` for pasted-answer runs; absent for `bench run`. |
| `usage` | object | Optional token/cost telemetry the agent printed with its answer: `input_tokens`, `output_tokens`, `cost_usd`. |
| `efficiency` | object | Optional investigation cost: `files_read`, `commands_run`. |
| `judge` | object | Reserved for LLM-judge grading metadata (issue #100). |
| `release` | string | Present only when the run was started with `bench run --release <v>` (issue #116); names the frozen release the run was verified against. |

The leaderboard and site rank only records whose `grader_version` matches the
current grader and whose `rubric_sha256` matches the current rubric. A record
graded under a different grader version or an older rubric renders **stale**;
a record written before these fields existed renders **legacy** — both stay
visible but never blend into a ranking with current-version numbers.

## Efficiency and cost metrics (issue #112)

The leaderboard and site also render **Median time** (median `elapsed_s`),
**Median cmds** (median `efficiency.commands_run`), and **Cost** (sum of
`usage.cost_usd`) — each computed over current-version records only, and each
rendered as an em dash (`—`) when no record in the run reported the metric.

The telemetry channels have different sources:

- `commands_run` — counted by the runner from its own simulated-CLI trace; you
  report nothing.
- `usage` — extracted from a `usage` object you print with your answer
  (`input_tokens`, `output_tokens`, `cost_usd`; numbers only).
- `files_read` — invisible to the runner, so report it in the answer: either a
  list of paths (unique entries are counted, so reading one file twice counts
  once) or a plain positive count. The bundled agents do this: the ReAct agent
  lists the files its `read_file` tool actually served, the evidence-dumping
  baseline lists the files it packed into its prompt.

## What `bench verify` checks

`bench verify results/<your-agent>` re-grades every stored answer with the
current grader and corpus and fails when:

- `agent.yaml` is missing, unparseable, or missing/invalidating a field
- a result file is missing the `task`, `answer`, `score`, or `passed` field
- the recorded task ID does not exist in the corpus
- the recorded `score` or `passed` does not match a fresh re-grade of the
  stored answer

The last case is the integrity guarantee: you cannot submit a score the stored
answers did not earn. Verify proves consistency, not ability — and it cannot
prove your agent is honestly what `agent.yaml` claims; the run command,
open-source flag, and model claims are reviewer-checked.

## Rules for submissions

- **One agent per PR**, touching only `results/<your-agent>/`. Never edit
  another run folder; those are other submitters' records.
- **Submit raw output.** Do not tune answers against the rubric (it is hidden,
  but keyword groups make spray-and-pray answers detectable), do not post-edit
  answers after the run, and never hand-edit scores — `bench verify` catches it.
- **Closed-source agents are welcome**; mark `open-source: no` and describe the
  invocation in `command` (see `docs/` for the closed-agent protocol as it
  lands).
- **Disclose the model and framework honestly.** Misattribution discovered
  later gets the run removed and repeated offenders blocked.

## Reviewer checklist

- [ ] `python -m bench verify results/<agent>` exits 0 (CI runs this too)
- [ ] `agent.yaml` names match the folder and honestly describe the agent
- [ ] PR touches only the new run folder
- [ ] Contact reachable if the run looks anomalous
