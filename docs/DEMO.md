# Demo: Azure SRE Bench in 2 minutes

A 2-minute walkthrough — from install to a graded result and the leaderboard.

## 1. What's here (10 seconds)

The corpus currently contains **32 tasks across 11 categories** — AKS networking,
identity, workloads and operations, networking, identity, App Service, Key Vault,
Azure DevOps pipelines, cost, and monitoring. Each task plants a hidden root cause
in realistic fixtures and grades the agent's answer against a hidden rubric.

```bash
python -m bench list
```

## 2. Run an agent on one task (60 seconds)

```bash
python -m bench run --agent agents/naive.py --task aks-networking-001
```

What happens:

1. The runner copies `symptom.md` and `fixtures/` into a clean temp directory — the
   hidden `expected.json` and `solution.json` are never copied there.
2. It invokes the agent (here, the naive control: prints a canned JSON answer without
   reading the evidence) with `TASK_DIR`/`TASK_ID` set and the symptom on stdin.
3. The grader scores the agent's JSON against the keyword-group rubric in
   `expected.json` and writes a record to `results/`.

An agent that guesses fails. The naive control scores 0/32 for exactly that reason.

## 3. The leaderboard (30 seconds)

After runs land in `results/`, regenerate and view the rankings:

```bash
python scripts/update_leaderboard.py
```

The README's [Leaderboard](README.md#leaderboard) section ranks agents by passed
tasks, then mean score. The naive baseline currently sits at 0/32 with a mean score
of 0.000 — the floor every real agent should beat.

---

Ready to go deeper? See the [Quick start](README.md#quick-start), run the LLM
baseline (`agents/llm_agent.py`, configured via environment variables), or read
[AGENTS.md](AGENTS.md) to contribute a task.
