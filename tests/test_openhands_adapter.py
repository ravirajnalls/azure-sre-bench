"""End-to-end tests for the OpenHands adapter (issue #170).

No real OpenHands install: the fake "OpenHands" script below does what the
real headless CLI would — runs in the workspace it was pointed at, reads the
task prompt from argv, and writes `answer.json`. That exercises the full chain
— harness -> adapter -> OpenHands -> answer file -> stdout -> grading — with no
network, no credentials, and no model.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

from agents import openhands_adapter
from bench import run as runner
from bench.cli import main as cli_main

REPO_ROOT = Path(__file__).resolve().parent.parent
ADAPTER = REPO_ROOT / "agents" / "openhands_adapter.py"
TEST_TASK = REPO_ROOT / "tests" / "data" / "monitoring" / "monitoring-902"
TESTS_DATA_ROOT = REPO_ROOT / "tests" / "data"

ANSWER = {
    "root_cause": "Redis connection pool exhausted in billing-api",
    "evidence": ["kubectl-get-pods.txt: CrashLoopBackOff"],
    "fix": "raise the connection limit on the redis pool",
}

# A fake OpenHands: proves the headless prompt arrived and the workspace is the
# cwd, then writes the answer file where the instruction says to.
FAKE_OPENHANDS = """
import json, os, sys

assert "--headless" in sys.argv, sys.argv
task_text = sys.argv[sys.argv.index("-t") + 1]
assert "submit_answer" not in task_text  # OpenHands has no MCP tools: answer.json is the contract

with open(
    os.path.join(os.environ["WORKSPACE_BASE"], {answer_filename!r}), "w", encoding="utf-8"
) as fh:
    json.dump(
        {{
            "root_cause": {root_cause!r},
            "evidence": [
                "answer.json written in the workspace: "
                + str(os.path.realpath(os.getcwd()) == os.path.realpath(os.environ["WORKSPACE_BASE"])),
                "prompt carried the symptom: " + str("fixtures" in task_text),
            ],
            "fix": {fix!r},
        }},
        fh,
    )
"""


def fake_openhands_command(tmp_path: Path) -> str:
    script = tmp_path / "fake_openhands.py"
    script.write_text(
        FAKE_OPENHANDS.format(
            answer_filename=openhands_adapter.ANSWER_FILENAME,
            root_cause=ANSWER["root_cause"],
            fix=ANSWER["fix"],
        ),
        encoding="utf-8",
    )
    return f"{sys.executable} {script}"


def adapter_command(openhands_command: str) -> str:
    return f"{sys.executable} {ADAPTER} --command {shlex.quote(openhands_command)}"


# --- unit pieces -------------------------------------------------------------


def test_build_prompt_names_the_symptom_and_the_answer_file():
    prompt = openhands_adapter.build_prompt("pod crash-looping in checkout")

    assert "pod crash-looping in checkout" in prompt
    assert "answer.json" in prompt


def test_build_command_is_headless_with_task_text():
    command = openhands_adapter.build_command("python -m openhands.core.main", "investigate")

    assert command == [
        "python",
        "-m",
        "openhands.core.main",
        "--headless",
        "-t",
        "investigate",
    ]


def test_collect_answer_missing_or_invalid_file_is_empty(tmp_path):
    assert openhands_adapter.collect_answer(tmp_path) == {}
    (tmp_path / openhands_adapter.ANSWER_FILENAME).write_text("not json", encoding="utf-8")
    assert openhands_adapter.collect_answer(tmp_path) == {}


# --- mocked end-to-end runs -----------------------------------------------------


def test_end_to_end_subprocess_through_adapter_entrypoint(tmp_path):
    """The real entrypoint (as `bench run` calls it) maps answer.json to stdout."""
    # A throwaway copy of the task: the adapter points the agent's workspace at
    # TASK_DIR, so the run must never write answer.json into the repo's corpus.
    task_copy = tmp_path / "task"
    shutil.copytree(TEST_TASK, task_copy, ignore=shutil.ignore_patterns("expected*", "solution*"))
    env = dict(
        os.environ,
        TASK_DIR=str(task_copy),
        WORKSPACE_BASE="",
    )
    proc = subprocess.run(
        shlex.split(adapter_command(fake_openhands_command(tmp_path))),
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
    )

    assert proc.returncode == 0, proc.stderr
    answer = json.loads(proc.stdout)
    assert answer["root_cause"] == ANSWER["root_cause"]
    assert answer["evidence"] == [
        "answer.json written in the workspace: True",
        "prompt carried the symptom: True",
    ]
    assert not (TEST_TASK / openhands_adapter.ANSWER_FILENAME).exists()


def test_openhands_without_answer_file_prints_empty_object_and_exits_zero(tmp_path):
    """No answer.json: {} on stdout, exit 0 — a graded FAIL, not a crash."""
    idle_openhands = tmp_path / "idle_openhands.py"
    idle_openhands.write_text("print('worked, never answered')", encoding="utf-8")
    env = dict(os.environ, TASK_DIR=str(TEST_TASK))
    proc = subprocess.run(
        shlex.split(adapter_command(f"{sys.executable} {idle_openhands}")),
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
    )

    assert proc.returncode == 0
    assert json.loads(proc.stdout) == {}
    assert "answer.json" in proc.stderr


def test_end_to_end_mocked_benchmark_run(tmp_path):
    """The full chain: harness -> adapter -> OpenHands -> answer file -> grading."""
    record = runner.run_task(
        TEST_TASK,
        adapter_command(fake_openhands_command(tmp_path)),
        results_root=tmp_path,
    )

    assert record["passed"] is True, record["stderr_tail"]
    assert record["score"] == 1.0
    assert record["answer"]["root_cause"] == ANSWER["root_cause"]
    assert record["timed_out"] is False


def test_adapter_is_wired_through_the_bench_cli(tmp_path, monkeypatch, capsys):
    """`bench run --agent "<adapter> ..."` grades the OpenHands run like any other."""
    monkeypatch.setattr(runner, "RESULTS_DIR", tmp_path / "results")
    exit_code = cli_main(
        [
            "run",
            "--agent",
            adapter_command(fake_openhands_command(tmp_path)),
            "--task",
            "monitoring-902",
            "--name",
            "cli-openhands",
            "--tasks-root",
            "tests/data",
        ]
    )

    out = capsys.readouterr().out
    assert exit_code == 0
    assert out.startswith("PASS monitoring-902 score=1.0 ")
    assert out.endswith("cli-openhands: 1/1 passed\n")
    stored = json.loads(
        (tmp_path / "results" / "cli-openhands" / "monitoring-902.json").read_text()
    )
    assert stored["passed"] is True
