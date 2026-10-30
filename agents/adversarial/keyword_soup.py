#!/usr/bin/env python3
"""Adversarial probe: the keyword-soup cheat (issue #92).

Builds its answer from the union of every rubric alternative in the task's
hidden `expected.json` — the answer an attacker writes after reading the
rubrics in the repo. It never reads fixtures, the symptom, or its workspace:
if this agent passes a task, the rubric is gameable by substring matching
alone, and no amount of investigation was required.

"Every rubric alternative" includes the rubric's distractor groups: the
mechanical cheat unions whatever the rubric contains into the diagnosis,
which is exactly the answer the distractor defense (issue #94) exists to
catch. A cheater that understood the rubric well enough to exclude trap
words while keeping the rest would effectively have to know the answer.
This is a probe for the CI gameability gate (`tests/test_gameability.py`),
not a baseline; its results land under `results/adversarial-*` and are
excluded from the leaderboard and site.
"""

from __future__ import annotations

import glob
import json
import os
import sys
from pathlib import Path

# The runner resolves the agent command to an absolute path before spawning,
# so __file__ is absolute even though the agent's cwd is a throwaway workspace.
REPO_ROOT = Path(__file__).resolve().parents[2]
TASKS_DIRNAME = "tasks"
RUBRIC_FILENAME = "expected.json"


def soup(groups: object) -> str:
    """Union of every alternative in every group, deduplicated, order kept.

    Substring matching means an answer containing every alternative matches
    every group; joining them into one string is the cheapest such answer.
    """
    alternatives = dict.fromkeys(
        alternative
        for group in (groups or [])
        if isinstance(group, list)
        for alternative in group
        if isinstance(alternative, str)
    )
    return " ".join(alternatives)


def soup_answer(rubric: dict) -> dict:
    """Contract-shaped answer that covers every alternative of every group.

    The diagnosis field unions the root-cause groups *and* the distractor
    groups — a mechanical rubric reader cannot tell trap words from real
    ones, which is how the distractor defense (issue #94) defeats this cheat.
    """
    return {
        "root_cause": " ".join(
            part
            for part in (
                soup(rubric.get("root_cause")),
                soup(rubric.get("distractors")),
            )
            if part
        ),
        "evidence": [],  # no fixture was read — there is nothing real to cite
        "fix": soup(rubric.get("fix")),
    }


def find_rubric(task_id: str) -> Path:
    """Locate the task's hidden rubric in the repo checkout."""
    pattern = f"*/{glob.escape(task_id)}/{RUBRIC_FILENAME}"
    matches = sorted((REPO_ROOT / TASKS_DIRNAME).glob(pattern))
    if not matches:
        raise FileNotFoundError(
            f"no {RUBRIC_FILENAME} for task '{task_id}' under "
            f"{REPO_ROOT / TASKS_DIRNAME}"
        )
    return matches[0]


def main() -> int:
    task_id = os.environ.get("TASK_ID", "")
    if not task_id:
        print("TASK_ID not set; run this agent through `bench run`", file=sys.stderr)
        return 1
    rubric = json.loads(find_rubric(task_id).read_text(encoding="utf-8"))
    print(json.dumps(soup_answer(rubric)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
