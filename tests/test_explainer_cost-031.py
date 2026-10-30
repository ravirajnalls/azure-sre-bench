"""Explainer check for cost-031 (issue #239).

Explainers are optional for task IDs below 041, so the generic corpus test
cannot assert this one exists — this file pins it: the shipped explainer must
be present and pass the validator's heading and fixture-reference checks.

Deliberately named per-task (not a shared ``tests/test_explainers.py``) so
parallel explainer-authoring PRs never collide on the same test path.
"""

from pathlib import Path

from bench.tasks import EXPLAINER_FILENAME, inspect_task

TASK_DIR = Path("tasks") / "cost" / "cost-031"


def test_cost_031_ships_a_valid_explainer():
    result = inspect_task(TASK_DIR)
    assert (TASK_DIR / EXPLAINER_FILENAME).is_file(), "explainer.md is missing"
    assert not any("explainer" in error for error in result.errors), result.errors
