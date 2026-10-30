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
