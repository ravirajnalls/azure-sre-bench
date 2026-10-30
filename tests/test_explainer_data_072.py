"""Explainer check for data-072 (issue #149).

Explainers are required from task ID 041 on; this per-task file pins that the
shipped explainer exists and passes the validator's heading and
fixture-reference checks.

Deliberately named per-task (not a shared ``tests/test_explainers.py``) so
parallel explainer-authoring PRs never collide on the same test path.
"""

from pathlib import Path

from bench.tasks import EXPLAINER_FILENAME, inspect_task

TASK_DIR = Path("tasks") / "data" / "data-072"


def test_data_072_ships_a_valid_explainer():
    result = inspect_task(TASK_DIR)
    assert (TASK_DIR / EXPLAINER_FILENAME).is_file(), "explainer.md is missing"
    assert not any("explainer" in error for error in result.errors), result.errors
