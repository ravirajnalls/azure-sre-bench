"""Tests for `bench verify` — third-party result submission verification (issue #118)."""

from __future__ import annotations

import json
from pathlib import Path

from bench import cli
from bench.verify import validate_agent_metadata, verify_run

REPO_ROOT = Path(__file__).resolve().parent.parent
TEST_TASKS = REPO_ROOT / "tests" / "data" / "tasks"

VALID_AGENT_YAML = """\
name: test-agent
version: "1.0.0"
model: gpt-4o
provider: openai
framework: none
open-source: yes
command: python agents/test_agent.py
date: 2026-01-15
contact: agent@example.com
"""


def write_result(run_dir: Path, task_dir: Path, *, score=None, passed=None) -> Path:
    """Write one result record whose answer is the task's reference solution."""
    solution = json.loads((task_dir / "solution.json").read_text(encoding="utf-8"))
    record = {
        "task": task_dir.name,
        "elapsed_s": 0.1,
        "answer": solution,
        "stderr_tail": "",
        "score": 1.0 if score is None else score,
        "passed": True if passed is None else passed,
        "detail": {},
        "timed_out": False,
        "returncode": 0,
    }
    path = run_dir / f"{task_dir.name}.json"
    path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    return path


def make_run(tmp_path: Path, agent_yaml: str = VALID_AGENT_YAML) -> tuple[Path, Path]:
    """Create a run folder with valid agent.yaml and one honest result."""
    run_dir = tmp_path / "results" / "test-agent"
    run_dir.mkdir(parents=True)
    (run_dir / "agent.yaml").write_text(agent_yaml, encoding="utf-8")
    task_dir = TEST_TASKS / "monitoring" / "monitoring-001"
    write_result(run_dir, task_dir)
    return run_dir, task_dir


def test_verify_passes_on_honest_run(tmp_path):
    run_dir, _ = make_run(tmp_path)
    report = verify_run(run_dir, TEST_TASKS)
    assert report["checked"] == 1
    assert report["errors"] == []
    assert report["mismatches"] == []


def test_verify_catches_tampered_score(tmp_path):
    run_dir, task_dir = make_run(tmp_path)
    result_path = run_dir / f"{task_dir.name}.json"
    record = json.loads(result_path.read_text(encoding="utf-8"))
    record["score"] = 0.5  # honest score is 1.0
    result_path.write_text(json.dumps(record), encoding="utf-8")

    report = verify_run(run_dir, TEST_TASKS)
    assert len(report["mismatches"]) == 1
    assert "regraded score=1.0" in report["mismatches"][0]
    assert "recorded score=0.5" in report["mismatches"][0]


def test_verify_catches_tampered_passed_flag(tmp_path):
    run_dir, task_dir = make_run(tmp_path)
    result_path = run_dir / f"{task_dir.name}.json"
    record = json.loads(result_path.read_text(encoding="utf-8"))
    record["passed"] = False
    result_path.write_text(json.dumps(record), encoding="utf-8")

    report = verify_run(run_dir, TEST_TASKS)
    assert len(report["mismatches"]) == 1
    assert "passed=True" in report["mismatches"][0]


def test_verify_catches_scored_up_failed_answer(tmp_path):
    """A dumped answer that honestly scores 0 cannot be submitted as a pass."""
    run_dir, task_dir = make_run(tmp_path)
    result_path = run_dir / f"{task_dir.name}.json"
    record = json.loads(result_path.read_text(encoding="utf-8"))
    record["answer"] = {"root_cause": "DNS", "evidence": [], "fix": "fix DNS"}
    record["score"] = 1.0
    record["passed"] = True
    result_path.write_text(json.dumps(record), encoding="utf-8")

    report = verify_run(run_dir, TEST_TASKS)
    assert len(report["mismatches"]) == 1


def test_verify_missing_agent_yaml_fails(tmp_path):
    run_dir, _ = make_run(tmp_path)
    (run_dir / "agent.yaml").unlink()
    report = verify_run(run_dir, TEST_TASKS)
    assert any("missing agent.yaml" in error for error in report["errors"])


def test_verify_rejects_incomplete_agent_yaml(tmp_path):
    errors = validate_agent_metadata(
        {"name": "x", "version": "1.0.0"}  # every other field absent
    )
    assert any("contact" in error for error in errors)
    assert any("open-source" in error for error in errors)
    assert any("date" in error for error in errors)


def test_verify_rejects_bad_open_source_value():
    meta = dict.fromkeys(
        ("name", "version", "model", "provider", "framework", "command", "date",
         "contact"),
        "placeholder",
    )
    meta["open-source"] = "maybe"
    meta["date"] = "2026-01-15"
    errors = validate_agent_metadata(meta)
    assert any("open-source" in error for error in errors)


def test_verify_rejects_impossible_date():
    meta = dict.fromkeys(
        ("name", "version", "model", "provider", "framework", "command",
         "open-source", "contact"),
        "placeholder",
    )
    meta["date"] = "2026-02-30"
    errors = validate_agent_metadata(meta)
    assert any("calendar day" in error for error in errors)


def test_verify_unknown_task_is_an_error(tmp_path):
    run_dir, task_dir = make_run(tmp_path)
    result_path = run_dir / f"{task_dir.name}.json"
    record = json.loads(result_path.read_text(encoding="utf-8"))
    record["task"] = "monitoring-999"
    result_path.rename(run_dir / "monitoring-999.json")
    (run_dir / "monitoring-999.json").write_text(json.dumps(record), encoding="utf-8")

    report = verify_run(run_dir, TEST_TASKS)
    assert any("monitoring-999" in error for error in report["errors"])


def test_cli_verify_exit_codes(tmp_path):
    run_dir, _ = make_run(tmp_path)
    argv = ["verify", str(run_dir), "--tasks-root", str(TEST_TASKS)]
    assert cli.main(argv) == 0

    # Tamper one score: the same command must now fail.
    result_path = next(p for p in run_dir.glob("*.json"))
    record = json.loads(result_path.read_text(encoding="utf-8"))
    record["score"] = 0.0
    result_path.write_text(json.dumps(record), encoding="utf-8")
    assert cli.main(argv) == 1


def test_cli_verify_missing_run_dir(tmp_path):
    argv = ["verify", str(tmp_path / "nope"), "--tasks-root", str(TEST_TASKS)]
    assert cli.main(argv) == 1
