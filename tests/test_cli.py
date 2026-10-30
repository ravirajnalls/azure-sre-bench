"""Smoke tests for the scaffold CLI (issue #1).

`validate`/`list` are covered in tests/test_tasks.py (issue #2) and `grade`
in tests/test_grader.py (issue #3); runner tests arrive with issue #4.
"""

import subprocess
import sys

from bench.cli import main


def test_help_via_subprocess():
    proc = subprocess.run(
        [sys.executable, "-m", "bench", "--help"], capture_output=True, text=True
    )
    assert proc.returncode == 0
    assert "Azure SRE Bench" in proc.stdout
    assert "validate" in proc.stdout


def test_unimplemented_subcommand_exits_nonzero():
    assert main(["run"]) == 2
