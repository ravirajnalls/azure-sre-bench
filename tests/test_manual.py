"""Tests for the closed-agent manual protocol (issue #124)."""

import json
from pathlib import Path

import pytest

from bench.cli import main
from bench.leaderboard import leaderboard_markdown, rank_rows
from bench.manual import (
    ANSWER_CONTRACT,
    SYSTEM_PROMPT,
    build_prompt,
    load_task_prompt,
    submit_answer,
)

TASK_ID = "aks-identity-008"


def passing_answer() -> dict:
    # The reference solution for aks-identity-008, matching its rubric groups.
    return {
        "root_cause": (
            "The orders-api service account in the new namespace lost its Azure "
            "workload identity federated credential because the federated "
            "credential is bound to the old namespace service account"
        ),
        "evidence": [
            "federated credential lists the default namespace service account"
        ],
        "fix": "recreate the federated credential for the orders namespace service account",
    }


@pytest.fixture()
def task_dir() -> Path:
    matches = sorted(Path("tasks").rglob(TASK_ID))
    return next(m for m in matches if (m / "expected.json").is_file())


def test_contract_has_three_required_fields():
    assert set(ANSWER_CONTRACT) == {"root_cause", "evidence", "fix"}


def test_build_prompt_includes_symptom_and_contract():
    prompt = build_prompt("pods are failing", {"a.txt": "hello"})
    assert "pods are failing" in prompt
    assert "hello" in prompt
    assert "root_cause" in prompt and "evidence" in prompt and "fix" in prompt


def test_build_prompt_lists_fixtures_sorted():
    prompt = build_prompt("s", {"z.txt": "1", "a.txt": "2"})
    assert prompt.index("### a.txt") < prompt.index("### z.txt")


def test_load_task_prompt_from_disk(task_dir):
    prompt = load_task_prompt(task_dir)
    assert "Key Vault" in prompt or "kv-orders-prod" in prompt
    for fixture in sorted((task_dir / "fixtures").iterdir()):
        assert f"### {fixture.name}" in prompt


def test_load_task_prompt_mentions_patched_files_for_remediation():
    """Remediation tasks (issue #110) extend the manual prompt with the
    patched_files contract, matching the `bench run` workspace hint."""
    matches = sorted(Path("tasks").rglob("aks-workloads-017"))
    prompt = load_task_prompt(matches[0])
    assert "patched_files" in prompt


def test_load_task_prompt_omits_remediation_block_for_diagnosis(task_dir):
    prompt = load_task_prompt(task_dir)
    assert "patched_files" not in prompt


def test_submit_answer_records_manual_mode(tmp_path, task_dir):
    record = submit_answer(
        TASK_ID, passing_answer(), "chatgpt-frontier", results_root=tmp_path
    )
    assert record["mode"] == "manual"
    assert record["agent"] == "chatgpt-frontier"
    assert record["returncode"] is None and record["elapsed_s"] is None
    stored = json.loads(
        (tmp_path / "chatgpt-frontier" / f"{TASK_ID}.json").read_text(encoding="utf-8")
    )
    assert stored == record


def test_submit_answer_missing_contract_key_rejected(tmp_path, task_dir):
    with pytest.raises(ValueError, match="root_cause"):
        submit_answer(
            TASK_ID, {"fix": "restart"}, "x", results_root=tmp_path
        )


def test_submit_answer_unknown_task_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        submit_answer("no-such-task-999", passing_answer(), "x", results_root=tmp_path)


def test_leaderboard_marks_manual_run(tmp_path):
    submit_answer(TASK_ID, passing_answer(), "chatgpt-frontier", results_root=tmp_path)
    table = leaderboard_markdown(tmp_path)
    assert "| Rank | Agent | Mode |" in table
    assert "chatgpt-frontier" in table
    assert "manual" in table


def test_leaderboard_runner_mode_unchanged(tmp_path):
    run_dir = tmp_path / "naive"
    run_dir.mkdir()
    (run_dir / "t.json").write_text(
        json.dumps({"task": "t", "score": 1.0, "passed": True}), encoding="utf-8"
    )
    rows = rank_rows({"naive": [{"task": "t", "score": 1.0, "passed": True}]})
    assert rows[0]["mode"] == "runner"
    assert "runner" in leaderboard_markdown(tmp_path)


def test_cli_submit_end_to_end(tmp_path, capsys):
    answer_path = tmp_path / "answer.json"
    answer_path.write_text(json.dumps(passing_answer()), encoding="utf-8")
    results_root = tmp_path / "results"
    code = main(
        [
            "submit",
            "--task",
            TASK_ID,
            "--agent",
            "chatgpt-frontier",
            "--answer",
            str(answer_path),
            "--results-root",
            str(results_root),
        ]
    )
    assert code == 0
    record = json.loads(
        (results_root / "chatgpt-frontier" / f"{TASK_ID}.json").read_text(
            encoding="utf-8"
        )
    )
    assert record["mode"] == "manual"


def test_cli_submit_rejects_incomplete_answer(tmp_path, capsys):
    answer_path = tmp_path / "answer.json"
    answer_path.write_text(json.dumps({"fix": "restart"}), encoding="utf-8")
    code = main(
        [
            "submit",
            "--task",
            TASK_ID,
            "--agent",
            "x",
            "--answer",
            str(answer_path),
            "--results-root",
            str(tmp_path / "results"),
        ]
    )
    assert code == 2
    assert "root_cause" in capsys.readouterr().err


def test_cli_prompt_prints_system_and_user(task_dir, capsys):
    code = main(["prompt", "--task", TASK_ID])
    out = capsys.readouterr().out
    assert code == 0
    assert "# system" in out
    assert SYSTEM_PROMPT in out
    assert "### kubectl-logs-orders-api.txt" in out


# --- result-schema versioning fields (issue #95, D3) --------------------------


def test_submit_record_carries_versioning_fields(tmp_path, task_dir):
    from bench import GRADER_VERSION, RESULT_SCHEMA_VERSION
    from bench.grader import rubric_sha256

    answer = {**passing_answer(), "usage": {"input_tokens": 10, "output_tokens": 5}}
    record = submit_answer(
        TASK_ID, answer, "chatgpt-frontier", results_root=tmp_path
    )
    assert record["schema_version"] == RESULT_SCHEMA_VERSION
    assert record["grader_version"] == GRADER_VERSION
    rubric = json.loads((task_dir / "expected.json").read_text(encoding="utf-8"))
    assert record["rubric_sha256"] == rubric_sha256(rubric)
    assert record["commands"] == []
    assert record["usage"] == {"input_tokens": 10, "output_tokens": 5}
