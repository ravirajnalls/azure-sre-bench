"""Explainer check for identity-070 (issue #204).

Explainers are required from task ID 041 on; this per-task file pins that the
shipped explainer exists and passes the validator's heading and
fixture-reference checks.

Deliberately named per-task (not a shared ``tests/test_explainers.py``) so
parallel explainer-authoring PRs never collide on the same test path.
"""

from pathlib import Path

from bench.tasks import EXPLAINER_FILENAME, inspect_task

TASK_DIR = Path("tasks") / "identity" / "identity-070"


def test_identity_070_ships_a_valid_explainer():
    result = inspect_task(TASK_DIR)
    assert (TASK_DIR / EXPLAINER_FILENAME).is_file(), "explainer.md is missing"
    assert not any("explainer" in error for error in result.errors), result.errors
