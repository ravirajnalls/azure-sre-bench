
"""Runner tests for `bench run` and the naive baseline agent (issue #4).

Uses a synthetic task under tests/data/ and throwaway agent scripts so the real
runner code path (workspace setup, subprocess, parsing, grading, results) is
exercised end to end.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest

from bench import grader
from bench import run as runner
from bench.grader import grade

REPO_ROOT = Path(__file__).resolve().parent.parent
TEST_TASK = REPO_ROOT / "tests" / "data" / "monitoring" / "run-test-001"
NAIVE_AGENT = "agents/naive.py"  # relative: the runner must resolve it


def write_agent(tmp_path: Path, body: str) -> str:
    """Write a throwaway agent script; returns an absolute command string."""
    script = tmp_path / "agent.py"
    script.write_text(body, encoding="utf-8")
    return f"{sys.executable} {script}"


LISTING_AGENT = """
import json, os
print(json.dumps({
    "root_cause": " ".join(sorted(os.listdir(os.environ["TASK_DIR"]))),
    "evidence": [os.environ.get("TASK_ID", "")],
    "fix": ""
}))
"""


def test_workspace_never_contains_hidden_files(tmp_path):
    """Information boundary: expected.json / solution.json never reach the agent."""
    cmd = write_agent(tmp_path, LISTING_AGENT)
    record = runner.run_task(TEST_TASK, cmd, results_root=tmp_path)

    listing = record["answer"]["root_cause"]
    assert "fixtures" in listing
    assert "symptom.md" in listing
    assert "expected.json" not in listing
    assert "solution.json" not in listing


def test_timeout_yields_fail_result_not_crash(tmp_path):
    slow_cmd = write_agent(tmp_path, "import time; time.sleep(30)")
    record = runner.run_task(TEST_TASK, slow_cmd, timeout=1, results_root=tmp_path)

    assert record["timed_out"] is True
    assert record["passed"] is False
    assert record["answer"] == {}
    assert record["returncode"] is None


def test_log_lines_before_json_still_grade(tmp_path):
    chatty_cmd = write_agent(
        tmp_path,
        "import json\n"
        "print('INFO: starting investigation')\n"
        "print('WARNING: reading fixtures')\n"
        "print(json.dumps({"
        '"root_cause": "Expired connection string secret in the probe deployment",'
        ' "evidence": ["app-insights-query.kql: 401 spike"],'
        ' "fix": "rotate the secret"}))\n',
    )
    record = runner.run_task(TEST_TASK, chatty_cmd, results_root=tmp_path)

    assert record["passed"] is True
    assert record["score"] == 1.0


def test_stored_evidence_is_capped(tmp_path):
    """Issue #93: result storage caps evidence at 8 items x 300 chars — overflow
    is ignored, never an error, and never an error channel into grading."""
    dump_cmd = write_agent(
        tmp_path,
        "import json\n"
        "print(json.dumps({"
        '"root_cause": "Expired connection string secret",'
        ' "evidence": [f"item-{i}: " + "x" * 400 for i in range(12)],'
        ' "fix": "rotate the secret"}))\n',
    )
    record = runner.run_task(TEST_TASK, dump_cmd, results_root=tmp_path)

    stored = record["answer"]["evidence"]
    assert len(stored) == grader.MAX_EVIDENCE_ITEMS
    assert all(len(item) <= grader.MAX_EVIDENCE_ITEM_CHARS for item in stored)
    assert stored[0].startswith("item-0:")


def test_naive_agent_scores_zero(tmp_path):
    """Non-triviality: the generic guess must fail a specific rubric."""
    record = runner.run_task(TEST_TASK, NAIVE_AGENT, results_root=tmp_path)

    assert record["passed"] is False
    assert record["score"] == 0.0
    assert record["answer"]["fix"] == "restart and monitor"


def test_relative_agent_path_resolved_absolute(tmp_path):
    """NAIVE_AGENT is relative and the agent's cwd is the workspace — it must
    still run because the runner resolves it against the caller's cwd."""
    record = runner.run_task(TEST_TASK, NAIVE_AGENT, results_root=tmp_path)
    assert "task" in record  # the subprocess ran and produced a record


def test_unspawnable_agent_yields_fail_result(tmp_path):
    """A non-executable agent file is a graded FAIL, not a runner crash."""
    bad_script = tmp_path / "not-executable.py"
    bad_script.write_text("print('never runs')\n", encoding="utf-8")
    bad_script.chmod(0o644)

    record = runner.run_task(TEST_TASK, str(bad_script), results_root=tmp_path)

    assert record["passed"] is False
    assert record["answer"] == {}
    assert record["returncode"] is None
    assert "Permission denied" in record["stderr_tail"]


def test_result_file_written_with_contract_fields(tmp_path):
    cmd = write_agent(
        tmp_path,
        'import json; print(json.dumps({"root_cause": "x", "evidence": [], "fix": "y"}))',
    )
    record = runner.run_task(TEST_TASK, cmd, name="ci-check", results_root=tmp_path)

    result_file = tmp_path / "ci-check" / "monitoring-901.json"
    assert result_file.exists()
    stored = json.loads(result_file.read_text())
    for key in (
        "task",
        "elapsed_s",
        "answer",
        "stderr_tail",
        "score",
        "passed",
        "detail",
    ):
        assert key in stored
    assert stored["task"] == "monitoring-901"
    assert record["score"] == stored["score"]


def test_stderr_tail_capped_at_2kb(tmp_path):
    noisy_cmd = write_agent(tmp_path, "import sys; sys.stderr.write('x' * 5000)")
    record = runner.run_task(TEST_TASK, noisy_cmd, results_root=tmp_path)

    assert len(record["stderr_tail"]) == 2048


def test_symptom_piped_on_stdin(tmp_path):
    reader_cmd = write_agent(
        tmp_path,
        "import sys, json\n"
        "symptom = sys.stdin.read()\n"
        'print(json.dumps({"root_cause": symptom, "evidence": [], "fix": "x"}))\n',
    )
    record = runner.run_task(TEST_TASK, reader_cmd, results_root=tmp_path)
    # The symptom itself (not an error) reached the agent via stdin.
    assert "Synthetically generated symptom" in record["answer"]["root_cause"]


def test_cli_run_end_to_end(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(REPO_ROOT)
    monkeypatch.setattr(runner, "RESULTS_DIR", tmp_path / "results")
    from bench.cli import main

    exit_code = main(
        [
            "run",
            "--agent",
            NAIVE_AGENT,
            "--task",
            "monitoring-901",
            "--name",
            "cli-test",
            "--tasks-root",
            "tests/data",
        ]
    )
    out = capsys.readouterr().out
    assert exit_code == 0
    assert out.startswith("FAIL monitoring-901 score=0.0 ")
    assert out.endswith("cli-test: 0/1 passed\n")


# --- unit coverage for the parsing / resolution seams -----------------------


def test_parse_answer_whole_output():
    assert runner.parse_answer('{"a": 1}') == {"a": 1}


def test_parse_answer_last_parseable_line():
    stdout = 'INFO boot\n{"a": 1}\nnot json\n{"b": 2}\n'
    assert runner.parse_answer(stdout) == {"b": 2}


def test_parse_answer_garbage_is_empty():
    assert runner.parse_answer("segmentation fault\n") == {}
    assert runner.parse_answer("") == {}


def test_parse_answer_non_object_json_ignored():
    assert runner.parse_answer("[1, 2, 3]") == {}
    assert runner.parse_answer('"just a string"') == {}


def test_resolve_command_makes_relative_paths_absolute():
    resolved = runner.resolve_command(NAIVE_AGENT)
    assert resolved == [str(REPO_ROOT / "agents" / "naive.py")]


def test_resolve_command_leaves_flags_and_missing_paths():
    resolved = runner.resolve_command("python -c 'print(1)' --flag /no/such/path")
    assert resolved == ["python", "-c", "print(1)", "--flag", "/no/such/path"]


def test_test_task_solution_passes_own_rubric():
    """Mini-solvability check: the fixture's own reference answer must pass."""
    solution = json.loads((TEST_TASK / "solution.json").read_text())
    rubric = json.loads((TEST_TASK / "expected.json").read_text())
    assert grade(solution, rubric)["passed"] is True


@pytest.mark.parametrize(
    "stdout,expected",
    [
        ('{"root_cause": "a", "fix": "b"}', {"root_cause": "a", "fix": "b"}),
    ],
)
def test_parse_answer_parametrized(stdout, expected):
    assert runner.parse_answer(stdout) == expected


# --- simulated kubectl/az CLIs (issue #111) ----------------------------------

SIM_COMMANDS_YAML = """\
commands:
  - command: kubectl get pods -n payments
    stdout: |
      payments-7d9f4b6c8c-x2k   0/1   CrashLoopBackOff
"""

KUBECTL_AGENT = """
import json, subprocess
proc = subprocess.run(
    ["kubectl", "-n", "payments", "get", "pods"], capture_output=True, text=True
)
print(json.dumps({
    "root_cause": proc.stdout.strip(),
    "evidence": [f"returncode={proc.returncode}"],
    "fix": ""
}))
"""


def sim_task(tmp_path: Path) -> Path:
    """The synthetic run-test task plus a `_commands.yaml` for the sim CLIs."""
    dest = tmp_path / "tasks" / "monitoring" / "run-test-001"
    shutil.copytree(TEST_TASK, dest)
    (dest / "fixtures" / "_commands.yaml").write_text(SIM_COMMANDS_YAML, encoding="utf-8")
    return dest


def test_sim_clis_reach_agent_and_commands_log_lands_in_results(tmp_path):
    cmd = write_agent(tmp_path, KUBECTL_AGENT)
    record = runner.run_task(
        sim_task(tmp_path), cmd, name="sim-run", results_root=tmp_path
    )

    # The agent invoked the shim with reordered flags and got the canned stdout.
    assert (
        record["answer"]["root_cause"]
        == "payments-7d9f4b6c8c-x2k   0/1   CrashLoopBackOff"
    )
    assert record["commands"] == ["kubectl -n payments get pods"]
    sidecar = tmp_path / "sim-run" / "monitoring-901.commands.log"
    assert sidecar.read_text() == "kubectl -n payments get pods\n"


def test_commands_log_captured_before_workspace_cleanup(tmp_path):
    """Even a timed-out run keeps its trace: the log is read before the finally."""
    body = (
        "import subprocess, time\n"
        "subprocess.run(['kubectl', 'get', 'pods'], capture_output=True, text=True)\n"
        "print('investigating...', flush=True)\n"
        "time.sleep(30)\n"
    )
    record = runner.run_task(
        sim_task(tmp_path), write_agent(tmp_path, body), timeout=2, results_root=tmp_path
    )

    assert record["timed_out"] is True
    assert record["commands"] == ["kubectl get pods"]


def test_workspace_never_contains_commands_file(tmp_path):
    body = (
        "import json, os\n"
        "print(json.dumps({'root_cause': ' '.join(sorted(os.listdir('fixtures'))),"
        " 'evidence': [], 'fix': ''}))\n"
    )
    record = runner.run_task(
        sim_task(tmp_path), write_agent(tmp_path, body), results_root=tmp_path
    )

    listing = record["answer"]["root_cause"]
    assert "app-insights-query.kql" in listing
    assert "_commands.yaml" not in listing


def test_task_without_commands_file_records_no_commands(tmp_path):
    cmd = write_agent(tmp_path, LISTING_AGENT)
    record = runner.run_task(TEST_TASK, cmd, name="plain", results_root=tmp_path)

    assert record["commands"] == []
    assert not (tmp_path / "plain" / "monitoring-901.commands.log").exists()


# --- result-schema versioning fields (issue #95, D3) --------------------------


def test_record_carries_versioning_fields(tmp_path):
    cmd = write_agent(tmp_path, LISTING_AGENT)
    record = runner.run_task(TEST_TASK, cmd, results_root=tmp_path)

    assert record["schema_version"] == runner.RESULT_SCHEMA_VERSION
    assert record["grader_version"] == runner.GRADER_VERSION
    assert (
        isinstance(record["rubric_sha256"], str)
        and len(record["rubric_sha256"]) == 64
    )
    # The rubric hash matches an independent re-hash of the task's expected.json.
    rubric = json.loads((TEST_TASK / "expected.json").read_text(encoding="utf-8"))
    assert record["rubric_sha256"] == grader.rubric_sha256(rubric)
    # run_task without an explicit name uses the default; a named run passes
    # the name through as the agent identity.
    assert record["agent"] == runner.DEFAULT_NAME
    named = runner.run_task(TEST_TASK, cmd, name="probe-agent", results_root=tmp_path)
    assert named["agent"] == "probe-agent"


def test_record_promotes_usage_and_derives_efficiency_from_sim_trace(tmp_path):
    agent = write_agent(
        tmp_path,
        "import json, subprocess\n"
        "proc = subprocess.run(\n"
        "    [\"kubectl\", \"-n\", \"payments\", \"get\", \"pods\"], capture_output=True, text=True\n"
        ")\n"
        "print(json.dumps({\n"
        "    \"root_cause\": proc.stdout.strip(),\n"
        "    \"evidence\": [],\n"
        "    \"fix\": \"\",\n"
        "    \"usage\": {\"input_tokens\": 120, \"output_tokens\": 45, \"cost_usd\": 0.001},\n"
        "}))\n",
    )
    record = runner.run_task(
        sim_task(tmp_path), agent, name="sim-run", results_root=tmp_path
    )

    assert record["usage"] == {
        "input_tokens": 120,
        "output_tokens": 45,
        "cost_usd": 0.001,
    }
    # Efficiency metrics (issue #112): the sim-CLI count derives from the
    # command trace; the agent's own files_read accounting merges in.
    assert record["efficiency"] == {"commands_run": 1}


def test_record_merges_agent_reported_files_read_into_efficiency(tmp_path):
    """A `files_read` list in the answer becomes a unique count in efficiency."""
    agent = write_agent(
        tmp_path,
        "import json\n"
        "print(json.dumps({\n"
        "    \"root_cause\": \"x\", \"evidence\": [], \"fix\": \"\",\n"
        "    \"files_read\": [\"symptom.md\", \"fixtures/a.log\", \"fixtures/a.log\"],\n"
        "}))\n",
    )
    record = runner.run_task(TEST_TASK, agent, results_root=tmp_path)
    assert record["efficiency"] == {"files_read": 2}


def test_record_merges_files_read_with_commands_run(tmp_path):
    """Both telemetry channels land in one efficiency object."""
    agent = write_agent(
        tmp_path,
        "import json, subprocess\n"
        "subprocess.run([\"kubectl\", \"get\", \"pods\"], capture_output=True)\n"
        "print(json.dumps({\n"
        "    \"root_cause\": \"x\", \"evidence\": [], \"fix\": \"\",\n"
        "    \"files_read\": 3,\n"
        "}))\n",
    )
    record = runner.run_task(
        sim_task(tmp_path), agent, name="sim-run", results_root=tmp_path
    )
    assert record["efficiency"] == {"commands_run": 1, "files_read": 3}


def test_record_ignores_bogus_files_read_shapes(tmp_path):
    """Empty lists, wrong types, and non-positive counts record nothing."""
    for bogus in ("[]", '""', "0", "-2", "[123]", "None"):
        agent = write_agent(
            tmp_path,
            "import json\n"
            f"print(json.dumps({{\"root_cause\": \"x\", \"evidence\": [], \"fix\": \"\", \"files_read\": {bogus}}}))\n",
        )
        record = runner.run_task(TEST_TASK, agent, results_root=tmp_path)
        assert "efficiency" not in record, bogus


def test_record_without_usage_or_commands_omits_both_keys(tmp_path):
    cmd = write_agent(tmp_path, LISTING_AGENT)
    record = runner.run_task(TEST_TASK, cmd, results_root=tmp_path)
    assert "usage" not in record
    assert "efficiency" not in record


def test_workspace_never_contains_explainer(tmp_path):
    """Issue #149: explainer.md names the root cause, so it is hidden from the
    agent exactly like expected.json/solution.json."""
    dest = tmp_path / "tasks" / "monitoring" / "run-test-001"
    shutil.copytree(TEST_TASK, dest)
    (dest / "explainer.md").write_text(
        "# explainer\n## Root cause\nspoiler-giveaway\n", encoding="utf-8"
    )

    record = runner.run_task(dest, write_agent(tmp_path, LISTING_AGENT), results_root=tmp_path)

    listing = record["answer"]["root_cause"]
    assert "fixtures" in listing
    assert "symptom.md" in listing
    assert "explainer.md" not in listing
    assert "spoiler-giveaway" not in record["answer"]["evidence"][0]
