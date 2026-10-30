"""Smoke tests for the scaffold CLI (issue #1).

Full grader/runner/validator tests arrive with issues #2-#4.
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
    assert main(["list"]) == 2
