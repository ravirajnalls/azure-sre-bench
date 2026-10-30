"""Smoke tests for the scaffold CLI (issue #1).

`validate`/`list` are covered in tests/test_tasks.py (issue #2), `grade` in
tests/test_grader.py (issue #3), and `run` in tests/test_run.py (issue #4).
"""

import subprocess
import sys

from bench.cli import IMPLEMENTED, SUBCOMMANDS, main


def test_help_via_subprocess():
    proc = subprocess.run(
        [sys.executable, "-m", "bench", "--help"], capture_output=True, text=True
    )
    assert proc.returncode == 0
    assert "Azure SRE Bench" in proc.stdout
    assert "validate" in proc.stdout


def test_every_advertised_subcommand_is_implemented():
    # All subcommands (issues #2-#4 and #40-#42) are implemented; the advertised
    # surface and the dispatchable set must never drift apart.
    assert set(SUBCOMMANDS) == set(IMPLEMENTED)


# --- bench explain (issue #149) ----------------------------------------------

import shutil
from pathlib import Path

DATA_TASK = Path(__file__).parent / "data" / "tasks" / "monitoring" / "monitoring-001"

EXPLAINER = """\
# monitoring-001 — explainer

## How to investigate

Read `fixtures/kubectl-get-pods.txt`.

## Why the red herring misleads

The metrics dip predates the incident.

## Root cause

Wrong backend path.

## Fix

Correct it.

## Prevention

Manifest lint.

## Further reading

- docs
"""


def make_task(tmp_path: Path) -> Path:
    dest = tmp_path / "tasks" / "monitoring" / "monitoring-001"
    shutil.copytree(DATA_TASK, dest)
    return dest


def test_explain_prints_explainer(tmp_path, capsys):
    task_dir = make_task(tmp_path)
    (task_dir / "explainer.md").write_text(EXPLAINER, encoding="utf-8")

    exit_code = main(
        ["explain", "--task", "monitoring-001", "--tasks-root", str(tmp_path / "tasks")]
    )
    assert exit_code == 0
    assert "## How to investigate" in capsys.readouterr().out


def test_explain_missing_explainer_errors(tmp_path, capsys):
    make_task(tmp_path)
    exit_code = main(
        ["explain", "--task", "monitoring-001", "--tasks-root", str(tmp_path / "tasks")]
    )
    assert exit_code == 2
    assert "error:" in capsys.readouterr().err


def test_explain_unknown_task_errors(tmp_path, capsys):
    exit_code = main(
        ["explain", "--task", "no-such-999", "--tasks-root", str(tmp_path / "tasks")]
    )
    assert exit_code == 2
    assert "error:" in capsys.readouterr().err
