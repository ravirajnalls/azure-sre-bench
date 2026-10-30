# Manual protocol for closed agents (issue #124)

Some capable agents cannot be launched as a subprocess by `bench run` —
ChatGPT, Copilot chat, an internal copilot behind a web UI. This protocol lets
a human operate such a **closed agent** against any task and record the result
on the same leaderboard, graded by the same rubric, clearly marked `manual`.

## 1. Pick a task

Choose a task ID, e.g. from `docs/TASKS.md`. The rest of this document uses
`aks-identity-008`.

## 2. Generate the exact prompt

```bash
python3 -m bench prompt --task aks-identity-008
```

(`python3 -m bench prompt` prints a paste-ready prompt; see `bench prompt
--help`.) The prompt contains:

- the task **symptom** verbatim,
- the **complete contents of every fixture file** in `tasks/**/fixtures/`
  (including red herrings — the agent must not be told which those are),
- the **answer instruction**: respond with only a single JSON object.

A system prompt is included in the output for agents that accept one.

## 3. Run the closed agent

Paste the prompt into the closed agent. Do not let the agent browse the task
corpus or the benchmark repository: it must work only from the pasted symptom
and fixtures, exactly like a `bench run` agent sees only its workspace.

## 4. The answer contract

The agent's final message must be a single JSON object with exactly these
keys — the same contract every benchmark agent uses:

```json
{
  "root_cause": "the specific technical cause of the incident",
  "evidence": ["fixture fact the diagnosis rests on", "..."],
  "fix": "concrete remediation steps"
}
```

- `root_cause` — string, required.
- `evidence` — list of strings, required.
- `fix` — string, required.

If the agent wraps the JSON in prose, extract the JSON object yourself and save
only the object to a file, e.g. `answer-aks-identity-008.json`.

## 5. Submit

```bash
bench submit --task aks-identity-008 --agent chatgpt-frontier \
  --answer answer-aks-identity-008.json
```

This grades the answer against the task's hidden `expected.json` and writes
`results/<agent>/<task-id>.json` — the same location and schema `bench run`
uses, plus:

- `"mode": "manual"`,
- `"agent": "<name>"`,
- `"elapsed_s": null` and `"returncode": null` (no subprocess was run).

Answers missing required contract keys are rejected without grading.

## 6. How manual results appear

- The **markdown leaderboard** (`bench leaderboard`, README block) renders a
  **Mode** column: runs containing manual submissions are marked `manual`,
  subprocess runs `runner`.
- Manual results are graded identically to automated runs — no special
  scoring, no excluded tasks. The mode column exists so readers know a human
  relayed the interaction.
- To publish manual results on the site, run `bench site` as usual; the run
  folder name (the `--agent` value) is the leaderboard identity, so use a
  name your readers will recognize, e.g. `chatgpt-frontier` or
  `copilot-web-2026-05`.
