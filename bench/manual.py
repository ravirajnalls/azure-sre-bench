"""Manual protocol for closed agents that cannot run as commands (issue #124).

Some frontier agents — ChatGPT, Copilot chat, internal copilots behind a web
UI — cannot be launched as a subprocess by `bench run`. This module is the
protocol surface for them: it builds the exact prompt a human pastes into the
closed agent, validates the answer against the same JSON contract every other
agent uses, and records a graded result marked `"mode": "manual"` so the
leaderboard can flag the entry.

The grading itself is deliberately identical to `bench run`: a manual result is
scored by the same rubric and is never special-cased.
"""

from __future__ import annotations

import json
from pathlib import Path

from bench import GRADER_VERSION, RESULT_SCHEMA_VERSION
from bench.attribution import is_attribution
from bench.grader import cap_evidence, extract_usage, rubric_sha256
from bench.heldout import ENCRYPTED_SUFFIX, HeldoutKeyError, load_hidden_json
from bench.remediation import grade_for_task, is_remediation
from bench.triage import is_triage

ANSWER_CONTRACT = {
    "root_cause": "str (required) — the specific technical cause of the incident",
    "evidence": "list[str] (required) — fixture facts the diagnosis rests on",
    "fix": "str (required) — the concrete remediation steps",
}

PROMPT_TEMPLATE = """You are an Azure SRE diagnosing a production incident.

## Symptom

{symptom}

## Investigation

You cannot run commands. Below is the complete contents of the diagnostic
fixtures collected from the cluster and subscription at the time of the
incident. Read them carefully; some may be red herrings.

{fixture_block}

## Answer

Respond with ONLY a single JSON object, no prose before or after, exactly this
shape:

{{"root_cause": "<the specific technical cause>", "evidence": ["<fixture fact>", ...], "fix": "<concrete remediation steps>"}}
"""

SYSTEM_PROMPT = (
    "You are an expert Azure/AKS site reliability engineer. Diagnose incidents "
    "from the symptom and diagnostic fixtures you are given, then answer with a "
    "single JSON object with exactly the keys root_cause (string), evidence "
    "(list of strings), and fix (string). Output nothing else."
)


def fixture_block(fixture_files: dict[str, str]) -> str:
    """Render fixture file contents as fenced blocks for the pasted prompt."""
    parts = []
    for name in sorted(fixture_files):
        parts.append(f"### {name}\n\n```\n{fixture_files[name].rstrip()}\n```")
    return "\n\n".join(parts)


def build_prompt(symptom: str, fixtures: dict[str, str]) -> str:
    """The exact user prompt a human pastes into the closed agent."""
    return PROMPT_TEMPLATE.format(
        symptom=symptom.strip(), fixture_block=fixture_block(fixtures)
    )


REMEDIATION_BLOCK = """

## Remediation

This is a remediation task: also include a `patched_files` object in the JSON
answer, mapping `fixtures/<file>` paths to the full new file content that
implements your fix. Every patched file is checked against task assertions and
scanned for unsafe changes.
"""

ATTRIBUTION_BLOCK = """

## Change attribution

This is a change-attribution task: answer with `culprit` (exactly one change
id from the change log), `reasoning` (why that change caused the incident and
no other change did), and `fix` (the remediation that undoes it).
"""

TRIAGE_BLOCK = """

## Alert triage

This is an alert-triage task: answer with `root_alert` (exactly one alert id
from fixtures/alerts.json — the one alert whose firing caused all the
others), `symptom_alerts` (every other active alert the root explains), and
`reasoning` (why the root caused the rest).
"""


def load_task_prompt(task_dir: Path) -> str:
    """Build the paste-ready prompt from a task directory on disk."""
    from bench.run import load_task  # deferred: avoids a circular import

    task_dir = Path(task_dir)
    task = load_task(task_dir)
    fixtures = {
        path.name: path.read_text(encoding="utf-8")
        for path in sorted((task_dir / "fixtures").iterdir())
        if path.is_file()
    } if (task_dir / "fixtures").is_dir() else {}
    prompt = build_prompt(str(task.get("symptom", "")), fixtures)
    if is_remediation(task):
        # Same contract the `bench run` workspace hint gives agents (issue #110).
        prompt += REMEDIATION_BLOCK
    elif is_attribution(task):
        # Same contract the `bench run` workspace hint gives agents (issue #144).
        prompt += ATTRIBUTION_BLOCK
    elif is_triage(task):
        # Same contract the `bench run` workspace hint gives agents (issue #145).
        prompt += TRIAGE_BLOCK
    return prompt


def submit_answer(
    task_id: str,
    answer: dict,
    agent: str,
    tasks_root: Path | str = "tasks",
    results_root: Path | str = "results",
) -> dict:
    """Grade a closed agent's answer and record it marked `mode: manual`.

    Writes `results/<agent>/<task-id>.json` — the same path `bench run` uses —
    so the leaderboard aggregates manual and automated runs together.
    """
    task_dir = _find_task_dir(task_id, Path(tasks_root))
    missing = [key for key in ANSWER_CONTRACT if key not in answer]
    if missing:
        raise ValueError(
            f"answer for {task_id} is missing required contract keys: "
            f"{', '.join(missing)}"
        )
    from bench.run import load_task  # deferred: avoids a circular import

    # Held-out rubrics decrypt only when HELDOUT_KEY is present (issue #115).
    task = load_task(task_dir) if (task_dir / "task.yaml").is_file() else {}
    rubric = load_hidden_json(task_dir, "expected.json", config=task)
    result = grade_for_task(answer, rubric, task_dir, task)
    record = {
        "task": task_id,
        # Same result-schema extension as `bench run` (issue #95): a manual
        # record self-describes its schema, grader, and rubric exactly like a
        # runner record, so renderers treat both uniformly.
        "schema_version": RESULT_SCHEMA_VERSION,
        "grader_version": GRADER_VERSION,
        "rubric_sha256": rubric_sha256(rubric),
        "mode": "manual",
        "agent": agent,
        "elapsed_s": None,
        "answer": cap_evidence(answer),
        "stderr_tail": "",
        "score": result["score"],
        "passed": result["passed"],
        "detail": result["detail"],
        "timed_out": False,
        "returncode": None,
        # The manual protocol observes no simulated-CLI trace; an explicit
        # empty list keeps the schema uniform with runner records.
        "commands": [],
    }
    usage = extract_usage(answer)
    if usage is not None:
        record["usage"] = usage
    out_dir = Path(results_root) / agent
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"{task_id}.json").write_text(
        json.dumps(record, indent=2) + "\n", encoding="utf-8"
    )
    return record


def _find_task_dir(task_id: str, tasks_root: Path) -> Path:
    for candidate in sorted(tasks_root.rglob(task_id)):
        if candidate.is_dir() and (
            (candidate / "expected.json").is_file()
            or (candidate / f"expected.json{ENCRYPTED_SUFFIX}").is_file()
        ):
            return candidate
    raise FileNotFoundError(
        f"no task directory named '{task_id}' with expected.json under {tasks_root}/"
    )
