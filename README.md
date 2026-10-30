# Azure SRE Bench

**An open benchmark for AI SRE agents on Azure, built entirely by agents in one day.**

Azure SRE Bench evaluates AI agents on **Azure and AKS incident diagnosis**.
Agents receive a symptom and a folder of realistic evidence (kubectl output, `az` CLI
output, logs, KQL results) and must return a structured diagnosis. Scoring is done
against hidden keyword-group rubrics, so an agent cannot pass by guessing — it has to
read the evidence.

## Purpose

Real incident response is investigation: forming a hypothesis from a symptom, digging
through tool output, and pinning down a root cause before proposing a fix. Azure SRE
Bench measures whether an agent can do this. Each task plants a hidden root cause in a
corpus of realistic fixtures, includes at least one healthy red herring, and grades the
agent's answer on specific, evidence-dependent concepts.

## Quick start

```bash
pip install -r requirements.txt

# List available tasks
python -m bench list

# Validate the task corpus (folder contract, rubrics, fixtures)
python -m bench validate

# Regenerate docs/TASKS.md, the auto-generated task catalog (issue #42)
python -m bench catalog

# Run an agent on a task and grade the result
python -m bench run --agent agents/naive.py --task tasks/aks-networking/aks-networking-001
```

The CLI is invoked as a Python module: `python -m bench <subcommand>`. See
`python -m bench --help` for the full command list.

For a 2-minute walkthrough — task count, a live `bench run`, and the leaderboard —
see [docs/DEMO.md](docs/DEMO.md).

## Task layout

Tasks live under `tasks/`, grouped by category:

```
tasks/<category>/<task-id>/
  task.yaml       # id, title, category, difficulty, tags, symptom
  fixtures/       # evidence the agent may read (2–10 files)
  expected.json   # HIDDEN rubric
  solution.json   # HIDDEN reference answer
```

Categories: `aks-networking`, `aks-identity`, `aks-workloads`, `aks-ops`, `networking`,
`identity`, `app-service`, `key-vault`, `ado-pipelines`, `cost`, `monitoring`.

Task IDs are `<category>-<nnn>` where `nnn` is the zero-padded backlog number. The
folder name, the `id` field in `task.yaml`, and the parent directory must agree.

## Agent contract

An agent runs as a shell command with:

- **cwd** — a temp directory containing only `symptom.md` and `fixtures/`. The hidden
  `expected.json` and `solution.json` are never copied there.
- **Env** — `TASK_DIR` (path of the task's temp copy) and `TASK_ID`.
- **stdin** — the symptom text.
- **stdout** — exactly one JSON object:

```json
{
  "root_cause": "one-paragraph diagnosis of the underlying cause",
  "evidence": ["fixture-file: the specific line or field that supports it", "..."],
  "fix": "the concrete remediation"
}
```

The runner grades whatever the agent prints: the whole output as JSON, or the last
line that parses — so agents that log before answering still score. A timeout becomes
a FAIL result, not a crash.

## Scoring

`expected.json` is a keyword-group rubric:

```json
{
  "root_cause": [["federated credential", "federated identity", "fic"], ["subject"], ["namespace"]],
  "fix": [["update the federated credential", "set the correct subject"]]
}
```

- Each inner list is one **concept group**; the answer matches the group when any
  alternative appears as a case-insensitive substring of the field text (for
  `root_cause`, the joined `evidence` list is appended to the text searched).
- Score = matched groups ÷ total groups (partial credit). A task **passes** when every
  group matches.
- Synonyms live in the same group so alternative phrasings score; groups stay specific
  enough that a generic guess scores zero. Evidence alone never earns `fix` credit.

## Leaderboard

Regenerated from `results/` by `python scripts/update_leaderboard.py` after baseline
runs; ranked by passed tasks, then mean score.

<!-- leaderboard -->
| Rank | Agent | Passed | Tasks | Mean score |
| ---: | --- | ---: | ---: | ---: |
| 1 | naive | 0/32 | 32 | 0.000 |
<!-- /leaderboard -->

## Run an LLM baseline

`agents/llm_agent.py` reads the symptom and every fixture (truncated to 20 KB each),
sends one prompt to an OpenAI-compatible chat endpoint, and prints the contract JSON
for the runner to grade. Configure it entirely through the environment — no code
changes for a new provider:

```bash
export LLM_BASE_URL="https://<your-endpoint>/v1"   # e.g. an Azure AI Foundry deployment
export LLM_API_KEY="..."                           # bearer token for that endpoint
export LLM_MODEL="..."                             # model or deployment name

python -m bench run --agent agents/llm_agent.py --task tasks/aks-networking/aks-networking-001
```

If the call fails, the credentials are missing, or the reply isn't JSON, the agent
prints `{}` and exits 0 — the runner records that task as a graded FAIL rather than
crashing. The mocked-HTTP test (`tests/test_llm_agent.py`) proves the contract JSON
reaches stdout without a live provider.

## Contributing

The benchmark grows one task at a time, and every task follows the same conventions:

1. **Read [AGENTS.md](AGENTS.md)** — it is the authoritative authoring guide: PR
   rules, folder layout, fixture requirements, symptom rules, and rubric format.
   CI validates the mechanical rules; reviewers hold the rest.
2. **Open an issue first.** Use the [new task template](
   .github/ISSUE_TEMPLATE/new-task.md) — it captures the category, difficulty, the
   symptom, the hidden root cause, and the red herring before any code is written.
3. **One task per PR.** Branch `task/<task-id>`, touch only that task's folder, and
   run `python -m bench validate` before pushing.
