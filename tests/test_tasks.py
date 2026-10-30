"""Unit tests for the task loader and validator (issue #2), plus the per-task
integrity checks from issue #5.

Each loader test copies the valid fixture task from ``tests/data/`` into a tmp
dir and breaks exactly one rule, so every validation rule is exercised in
isolation. The integrity tests at the bottom are parametrized over every task
discovered in the real ``tasks/`` tree and enforce the benchmark's integrity
invariants: validity, solvability, non-triviality, and no symptom leaks. With
no tasks authored yet they report an empty parameter set and activate
automatically as task folders land.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from bench.attribution import KIND_ATTRIBUTION
from bench.triage import KIND_TRIAGE
from bench.grader import grade, normalize
from bench.heldout import SPLIT_HELDOUT, heldout_key, load_hidden_json, split_of
from bench.remediation import grade_for_task
from bench.run import run_task
from bench.tasks import CANARY_GUID, inspect_task, load_tasks

DATA_TASKS = Path(__file__).parent / "data" / "tasks"
BASE_TASK = DATA_TASKS / "monitoring" / "monitoring-001"

REAL_GUID = "12345678-1234-1234-1234-123456789012"
PLACEHOLDER_GUID = "00000000-0000-0000-0000-00000000000a"


def copy_task(tmp_path: Path, name: str = "monitoring-001") -> Path:
    """Copy the valid fixture task into tmp_path under its category."""
    dest = tmp_path / "tasks" / "monitoring" / name
    shutil.copytree(BASE_TASK, dest)
    return dest


def patch_yaml(task_dir: Path, **changes: object) -> None:
    """Rewrite task.yaml with the given field changes (None deletes the key)."""
    yaml_path = task_dir / "task.yaml"
    config = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
    for key, value in changes.items():
        if value is None:
            config.pop(key, None)
        else:
            config[key] = value
    yaml_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")


def load_one(tmp_path: Path):
    results = load_tasks(tmp_path / "tasks")
    assert len(results) == 1
    return results[0]


def test_valid_fixture_task_has_no_errors(tmp_path: Path):
    result = inspect_task(copy_task(tmp_path))
    assert result.errors == []
    assert result.config is not None
    assert result.config["id"] == "monitoring-001"


# --- task.yaml required keys and field types -------------------------------


def test_missing_required_key(tmp_path: Path):
    task_dir = copy_task(tmp_path)
    patch_yaml(task_dir, symptom=None)
    result = load_one(tmp_path)
    assert "task.yaml: missing required key 'symptom'" in result.errors


def test_empty_title(tmp_path: Path):
    task_dir = copy_task(tmp_path)
    patch_yaml(task_dir, title="   ")
    result = load_one(tmp_path)
    assert "task.yaml: 'title' must be a non-empty string" in result.errors


def test_empty_symptom(tmp_path: Path):
    task_dir = copy_task(tmp_path)
    patch_yaml(task_dir, symptom="")
    result = load_one(tmp_path)
    assert "task.yaml: 'symptom' must be a non-empty string" in result.errors


def test_unparseable_task_yaml(tmp_path: Path):
    task_dir = copy_task(tmp_path)
    (task_dir / "task.yaml").write_text("id: [unclosed\n", encoding="utf-8")
    result = load_one(tmp_path)
    assert any(error.startswith("task.yaml does not parse") for error in result.errors)


def test_task_yaml_not_a_mapping(tmp_path: Path):
    task_dir = copy_task(tmp_path)
    (task_dir / "task.yaml").write_text("- just\n- a\n- list\n", encoding="utf-8")
    result = load_one(tmp_path)
    assert "task.yaml must contain a mapping" in result.errors


def test_missing_task_yaml(tmp_path: Path):
    task_dir = copy_task(tmp_path)
    (task_dir / "task.yaml").unlink()
    result = load_one(tmp_path)
    assert any(error.startswith("missing task.yaml") for error in result.errors)


# --- canary (issue #114) -----------------------------------------------------


def test_missing_canary_fails(tmp_path: Path):
    task_dir = copy_task(tmp_path)
    patch_yaml(task_dir, canary=None)
    result = load_one(tmp_path)
    assert any("missing or incorrect canary" in error for error in result.errors)


def test_wrong_canary_value_fails(tmp_path: Path):
    task_dir = copy_task(tmp_path)
    patch_yaml(task_dir, canary="00000000-0000-0000-0000-999999999999")
    result = load_one(tmp_path)
    assert any("missing or incorrect canary" in error for error in result.errors)


# --- id / category / folder agreement --------------------------------------


def test_id_folder_mismatch(tmp_path: Path):
    task_dir = copy_task(tmp_path)
    patch_yaml(task_dir, id="monitoring-002")
    result = load_one(tmp_path)
    assert "id 'monitoring-002' does not match folder name 'monitoring-001'" in result.errors


def test_parent_directory_must_equal_category(tmp_path: Path):
    task_dir = copy_task(tmp_path)
    moved = task_dir.parent.parent / "networking" / task_dir.name
    moved.parent.mkdir()
    task_dir.rename(moved)
    result = load_one(tmp_path)
    assert (
        "task folder sits under 'networking' but category is 'monitoring'" in result.errors
    )


def test_id_not_matching_category_nnn_pattern(tmp_path: Path):
    task_dir = copy_task(tmp_path, name="monitoring-1")
    patch_yaml(task_dir, id="monitoring-1")
    result = load_one(tmp_path)
    assert any("does not match '<category>-<nnn>'" in error for error in result.errors)


def test_unknown_category(tmp_path: Path):
    task_dir = copy_task(tmp_path)
    patch_yaml(task_dir, category="kubernetes")
    result = load_one(tmp_path)
    assert "task.yaml: unknown category 'kubernetes'" in result.errors


def test_invalid_difficulty(tmp_path: Path):
    task_dir = copy_task(tmp_path)
    patch_yaml(task_dir, difficulty="trivial")
    result = load_one(tmp_path)
    assert any("difficulty must be one of" in error for error in result.errors)


def test_tags_must_be_non_empty_list(tmp_path: Path):
    task_dir = copy_task(tmp_path)
    patch_yaml(task_dir, tags=[])
    result = load_one(tmp_path)
    assert "task.yaml: 'tags' must be a non-empty list" in result.errors


def test_tags_must_be_strings(tmp_path: Path):
    task_dir = copy_task(tmp_path)
    patch_yaml(task_dir, tags=["monitoring", 42])
    result = load_one(tmp_path)
    assert "task.yaml: 'tags' must contain only non-empty strings" in result.errors


# --- fixtures ---------------------------------------------------------------


def test_too_few_fixtures(tmp_path: Path):
    task_dir = copy_task(tmp_path)
    (task_dir / "fixtures" / "kubectl-logs-ingress.txt").unlink()
    result = load_one(tmp_path)
    assert "fixtures/ must contain 2-10 files, found 1" in result.errors


def test_too_many_fixtures(tmp_path: Path):
    task_dir = copy_task(tmp_path)
    for index in range(9):
        (task_dir / "fixtures" / f"extra-{index}.txt").write_text("noise", encoding="utf-8")
    result = load_one(tmp_path)
    assert "fixtures/ must contain 2-10 files, found 11" in result.errors


def test_missing_fixtures_directory(tmp_path: Path):
    task_dir = copy_task(tmp_path)
    shutil.rmtree(task_dir / "fixtures")
    result = load_one(tmp_path)
    assert any(error.startswith("missing fixtures/ directory") for error in result.errors)


def test_fixture_bounds_are_inclusive(tmp_path: Path):
    """2 and 10 fixtures are both inside the allowed 2-10 range."""
    task_dir = copy_task(tmp_path)
    for index in range(8):
        (task_dir / "fixtures" / f"extra-{index}.txt").write_text("noise", encoding="utf-8")
    assert load_one(tmp_path).errors == []


def test_non_placeholder_guid_in_fixture(tmp_path: Path):
    task_dir = copy_task(tmp_path)
    fixture = task_dir / "fixtures" / "kubectl-logs-ingress.txt"
    fixture.write_text(
        f"pod uid {REAL_GUID} rejected upstream request\n", encoding="utf-8"
    )
    result = load_one(tmp_path)
    assert any(
        f"non-placeholder GUID '{REAL_GUID}'" in error for error in result.errors
    )


def test_placeholder_guid_is_accepted(tmp_path: Path):
    task_dir = copy_task(tmp_path)
    fixture = task_dir / "fixtures" / "kubectl-logs-ingress.txt"
    fixture.write_text(f"pod uid {PLACEHOLDER_GUID} rejected upstream request\n", encoding="utf-8")
    assert load_one(tmp_path).errors == []


# --- expected.json / solution.json rubric shapes ----------------------------


def test_missing_expected_json(tmp_path: Path):
    task_dir = copy_task(tmp_path)
    (task_dir / "expected.json").unlink()
    result = load_one(tmp_path)
    assert "missing expected.json" in result.errors


def test_malformed_expected_json(tmp_path: Path):
    task_dir = copy_task(tmp_path)
    (task_dir / "expected.json").write_text("{not json", encoding="utf-8")
    result = load_one(tmp_path)
    assert any(
        error.startswith("expected.json does not parse") for error in result.errors
    )


def test_missing_rubric_field(tmp_path: Path):
    task_dir = copy_task(tmp_path)
    data = json.loads((task_dir / "expected.json").read_text(encoding="utf-8"))
    del data["fix"]
    (task_dir / "expected.json").write_text(json.dumps(data), encoding="utf-8")
    result = load_one(tmp_path)
    assert (
        "expected.json: 'fix' must be a non-empty list of non-empty string lists"
        in result.errors
    )


def test_empty_rubric_field(tmp_path: Path):
    task_dir = copy_task(tmp_path)
    data = json.loads((task_dir / "expected.json").read_text(encoding="utf-8"))
    data["root_cause"] = []
    (task_dir / "expected.json").write_text(json.dumps(data), encoding="utf-8")
    result = load_one(tmp_path)
    assert (
        "expected.json: 'root_cause' must be a non-empty list of non-empty string lists"
        in result.errors
    )


def test_rubric_group_must_be_non_empty_string_list(tmp_path: Path):
    task_dir = copy_task(tmp_path)
    data = json.loads((task_dir / "expected.json").read_text(encoding="utf-8"))
    data["fix"] = [["scale the ingress controller"], []]
    (task_dir / "expected.json").write_text(json.dumps(data), encoding="utf-8")
    result = load_one(tmp_path)
    assert (
        "expected.json: 'fix' must be a non-empty list of non-empty string lists"
        in result.errors
    )


def test_valid_max_chars_override_is_accepted(tmp_path: Path):
    task_dir = copy_task(tmp_path)
    data = json.loads((task_dir / "expected.json").read_text(encoding="utf-8"))
    data["max_chars"] = 800
    (task_dir / "expected.json").write_text(json.dumps(data), encoding="utf-8")
    result = load_one(tmp_path)
    assert result.errors == []


@pytest.mark.parametrize("bad", [0, -5, "800", True, 2.5, [600]])
def test_invalid_max_chars_is_rejected(tmp_path: Path, bad):
    task_dir = copy_task(tmp_path)
    data = json.loads((task_dir / "expected.json").read_text(encoding="utf-8"))
    data["max_chars"] = bad
    (task_dir / "expected.json").write_text(json.dumps(data), encoding="utf-8")
    result = load_one(tmp_path)
    assert "expected.json: 'max_chars' must be a positive integer" in result.errors


# --- distractor groups (issue #94) -------------------------------------------


def patch_expected(task_dir: Path, **changes: object) -> None:
    """Rewrite expected.json with the given field changes (None deletes the key)."""
    path = task_dir / "expected.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    for key, value in changes.items():
        if value is None:
            data.pop(key, None)
        else:
            data[key] = value
    path.write_text(json.dumps(data), encoding="utf-8")


def test_fixture_task_has_grounded_distractors(tmp_path: Path):
    """The data task models the distractor shape: no errors, no warnings."""
    copy_task(tmp_path)
    result = load_one(tmp_path)
    assert result.errors == []
    assert result.warnings == []


def test_missing_distractors_warns(tmp_path: Path):
    task_dir = copy_task(tmp_path)
    patch_expected(task_dir, distractors=None)
    result = load_one(tmp_path)
    assert result.errors == []
    assert any(
        "no 'distractors' groups" in warning for warning in result.warnings
    )


@pytest.mark.parametrize(
    "bad", ["nope", 42, [], ["flat"], [["ok"], []], [[5]], [[""]], [[None]]]
)
def test_malformed_distractors_are_rejected(tmp_path: Path, bad):
    task_dir = copy_task(tmp_path)
    patch_expected(task_dir, distractors=bad)
    result = load_one(tmp_path)
    assert (
        "expected.json: 'distractors' must be a non-empty list of "
        "non-empty string lists" in result.errors
    )


def test_distractor_equal_to_root_cause_alternative_rejected(tmp_path: Path):
    task_dir = copy_task(tmp_path)
    patch_expected(task_dir, distractors=[["traffic spike"]])
    result = load_one(tmp_path)
    assert any("overlaps" in error for error in result.errors)


def test_distractor_containing_root_cause_alternative_rejected(tmp_path: Path):
    # Substring overlap in either direction: "peak traffic spike" contains the
    # graded "peak traffic" and would drag correct answers into the trap.
    task_dir = copy_task(tmp_path)
    patch_expected(task_dir, distractors=[["peak traffic spike"]])
    result = load_one(tmp_path)
    assert any(
        "overlaps" in error and "peak traffic" in error for error in result.errors
    )


def test_root_cause_contained_in_distractor_rejected(tmp_path: Path):
    task_dir = copy_task(tmp_path)
    patch_expected(task_dir, distractors=[["appgw and ingress saturation"]])
    result = load_one(tmp_path)
    assert any(
        "overlaps" in error and "ingress saturation" in error
        for error in result.errors
    )


def test_ungrounded_distractor_warns(tmp_path: Path):
    task_dir = copy_task(tmp_path)
    patch_expected(task_dir, distractors=[["dns misconfiguration"]])
    result = load_one(tmp_path)
    assert result.errors == []
    assert any(
        "distractor group 0 has no alternative found in any fixture" in warning
        for warning in result.warnings
    )


def test_each_grounded_distractor_group_is_accepted(tmp_path: Path):
    task_dir = copy_task(tmp_path)
    # "appgw" appears in the metrics fixture, "upstream" in the log fixture.
    patch_expected(task_dir, distractors=[["appgw"], ["upstream-connection-failed"]])
    result = load_one(tmp_path)
    assert result.errors == []
    assert not any("distractor" in warning for warning in result.warnings)


def test_distractor_rules_do_not_shadow_expected_json_errors(tmp_path: Path):
    task_dir = copy_task(tmp_path)
    patch_expected(task_dir, root_cause=[], distractors="nope")
    result = load_one(tmp_path)
    assert any("'root_cause' must be" in error for error in result.errors)
    assert any("'distractors' must be" in error for error in result.errors)


def test_missing_solution_json(tmp_path: Path):
    task_dir = copy_task(tmp_path)
    (task_dir / "solution.json").unlink()
    result = load_one(tmp_path)
    assert "missing solution.json" in result.errors


def test_malformed_solution_json(tmp_path: Path):
    task_dir = copy_task(tmp_path)
    (task_dir / "solution.json").write_text("[", encoding="utf-8")
    result = load_one(tmp_path)
    assert any(
        error.startswith("solution.json does not parse") for error in result.errors
    )


# --- solution.json evidence_files and difficulty agreement (issue #101) -------


def patch_solution(task_dir: Path, **changes: object) -> None:
    """Rewrite solution.json with the given field changes (None deletes the key)."""
    path = task_dir / "solution.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    for key, value in changes.items():
        if value is None:
            data.pop(key, None)
        else:
            data[key] = value
    path.write_text(json.dumps(data), encoding="utf-8")


def test_valid_fixture_task_has_no_warnings(tmp_path: Path):
    result = inspect_task(copy_task(tmp_path))
    assert result.warnings == []


def test_missing_evidence_files_warns(tmp_path: Path):
    task_dir = copy_task(tmp_path)
    patch_solution(task_dir, evidence_files=None)
    result = load_one(tmp_path)
    assert result.errors == []
    assert any(
        "'evidence_files' must be a non-empty list" in warning
        for warning in result.warnings
    )


def test_evidence_files_entry_not_in_fixtures_warns(tmp_path: Path):
    task_dir = copy_task(tmp_path)
    patch_solution(task_dir, evidence_files=["kubectl-logs-ingress.txt", "ghost.txt"])
    result = load_one(tmp_path)
    assert result.errors == []
    assert any(
        "evidence_files not found in fixtures/: ghost.txt" in warning
        for warning in result.warnings
    )


def test_easy_with_two_evidence_files_warns(tmp_path: Path):
    task_dir = copy_task(tmp_path)
    patch_solution(
        task_dir,
        evidence_files=["kubectl-logs-ingress.txt", "az-monitor-metrics.json"],
    )
    result = load_one(tmp_path)
    assert result.errors == []
    assert any(
        "difficulty 'easy' expects 1 evidence_files, found 2" in warning
        for warning in result.warnings
    )


def test_medium_with_one_evidence_file_warns(tmp_path: Path):
    task_dir = copy_task(tmp_path)
    patch_yaml(task_dir, difficulty="medium")
    result = load_one(tmp_path)
    assert result.errors == []
    assert any(
        "difficulty 'medium' expects 2 evidence_files, found 1" in warning
        for warning in result.warnings
    )


def test_hard_with_two_evidence_files_is_accepted(tmp_path: Path):
    """Hard may rest on two files when a misleading signal justifies it — the
    validator cannot check the justification, so it must not warn (issue #101)."""
    task_dir = copy_task(tmp_path)
    patch_yaml(task_dir, difficulty="hard")
    patch_solution(
        task_dir,
        evidence_files=["kubectl-logs-ingress.txt", "az-monitor-metrics.json"],
    )
    result = load_one(tmp_path)
    assert result.errors == []
    assert result.warnings == []


def test_hard_with_one_evidence_file_warns(tmp_path: Path):
    task_dir = copy_task(tmp_path)
    patch_yaml(task_dir, difficulty="hard")
    result = load_one(tmp_path)
    assert result.errors == []
    assert any(
        warning.startswith("difficulty 'hard' expects 3+ evidence_files")
        for warning in result.warnings
    )


def test_warnings_do_not_fail_validation(tmp_path: Path):
    """Warnings are corpus-quality signals: exit stays 0 while they surface."""
    task_dir = copy_task(tmp_path)
    patch_solution(task_dir, evidence_files=None)
    proc = run_cli("validate", "tasks", cwd=tmp_path)
    assert proc.returncode == 0
    assert f"WARN tasks/monitoring/{task_dir.name}" in proc.stdout
    assert "1/1 tasks valid, 1 warning" in proc.stdout


# --- duplicate ids across the repo ------------------------------------------


def test_duplicate_ids_detected(tmp_path: Path):
    copy_task(tmp_path)
    second = tmp_path / "tasks" / "cost" / "monitoring-001"
    second.parent.mkdir()
    shutil.copytree(BASE_TASK, second)
    results = load_tasks(tmp_path / "tasks")
    assert len(results) == 2
    for result in results:
        assert any(
            error.startswith("duplicate id 'monitoring-001'") for error in result.errors
        )


# --- CLI: validate -----------------------------------------------------------


def run_cli(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "PYTHONPATH": str(Path(__file__).resolve().parents[1]),
    }
    return subprocess.run(
        [sys.executable, "-m", "bench", *args],
        capture_output=True,
        text=True,
        cwd=cwd,
        env=env,
    )


def test_validate_success_exit_zero(tmp_path: Path):
    copy_task(tmp_path)
    proc = run_cli("validate", "tasks", cwd=tmp_path)
    assert proc.returncode == 0
    assert "1/1 tasks valid, 0 warnings" in proc.stdout
    assert "FAIL" not in proc.stdout
    assert "WARN" not in proc.stdout


def test_validate_failure_exit_nonzero_with_fail_lines(tmp_path: Path):
    task_dir = copy_task(tmp_path)
    patch_yaml(task_dir, difficulty="trivial")
    proc = run_cli("validate", "tasks", cwd=tmp_path)
    assert proc.returncode == 1
    assert f"FAIL tasks/monitoring/{task_dir.name}" in proc.stdout
    assert "difficulty must be one of" in proc.stdout
    assert "0/1 tasks valid" in proc.stdout


def test_validate_zero_tasks_exits_nonzero(tmp_path: Path):
    (tmp_path / "tasks").mkdir()
    proc = run_cli("validate", "tasks", cwd=tmp_path)
    assert proc.returncode == 1
    assert "0/0 tasks valid" in proc.stdout


# --- CLI: list ---------------------------------------------------------------


def test_list_prints_id_difficulty_title(tmp_path: Path):
    copy_task(tmp_path)
    proc = run_cli("list", "tasks", cwd=tmp_path)
    assert proc.returncode == 0
    assert (
        "monitoring-001  easy  Intermittent 503s from orders-api behind the AKS ingress"
        in proc.stdout
    )


def test_list_on_empty_root_is_quiet_success(tmp_path: Path):
    (tmp_path / "tasks").mkdir()
    proc = run_cli("list", "tasks", cwd=tmp_path)
    assert proc.returncode == 0
    assert proc.stdout == ""


def test_list_stats_prints_distribution(tmp_path: Path):
    """Issue #101: the difficulty distribution is printed by `bench list --stats`."""
    copy_task(tmp_path)
    proc = run_cli("list", "--stats", "tasks", cwd=tmp_path)
    assert proc.returncode == 0
    assert "1 tasks: 1 easy, 0 medium, 0 hard" in proc.stdout

# --- Per-task integrity checks (issue #5) ------------------------------------
#
# Parametrized over every task discovered in the real `tasks/` tree. These are
# the CI-enforced invariants from AGENTS.md: every task validates clean, its
# reference solution passes its own rubric, the naive guessing agent fails it,
# and the symptom alone does not give the root cause away.

TASKS_ROOT = Path(__file__).resolve().parent.parent / "tasks"
NAIVE_AGENT_CMD = (
    f"{sys.executable} {Path(__file__).resolve().parent.parent / 'agents' / 'naive.py'}"
)


def _discovered_task_dirs() -> list[Path]:
    if not TASKS_ROOT.is_dir():
        return []
    return sorted(
        path for path in TASKS_ROOT.glob("*/*") if (path / "task.yaml").is_file()
    )


def _task_label(task_dir: Path) -> str:
    """Unique, readable parametrize id: <category>/<task-id>."""
    return f"{task_dir.parent.name}/{task_dir.name}"


def _load_json_file(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _config_of(task_dir: Path) -> dict:
    return yaml.safe_load((task_dir / "task.yaml").read_text(encoding="utf-8")) or {}


def _load_rubric_pair(task_dir: Path) -> tuple[dict, dict]:
    """Decrypt-or-skip: a task's (solution, expected) pair (issue #115, spec D4).

    Public tasks read the plaintext files as before. Held-out tasks decrypt
    with HELDOUT_KEY when it is set and skip otherwise — structure is still
    validated key-free by test_task_validates_clean.
    """
    config = _config_of(task_dir)
    if split_of(config) != SPLIT_HELDOUT:
        return (
            _load_json_file(task_dir / "solution.json"),
            _load_json_file(task_dir / "expected.json"),
        )
    if not heldout_key():
        pytest.skip(
            f"{_task_label(task_dir)} is held-out; set HELDOUT_KEY to check its rubric"
        )
    return (
        load_hidden_json(task_dir, "solution.json", config=config),
        load_hidden_json(task_dir, "expected.json", config=config),
    )


@pytest.mark.parametrize("task_dir", _discovered_task_dirs(), ids=_task_label)
def test_task_validates_clean(task_dir: Path):
    """Integrity invariant: `validate(task) == []`."""
    result = inspect_task(task_dir)
    assert result.errors == [], (
        f"{_task_label(task_dir)} failed validation:\n"
        + "\n".join(f"  - {error}" for error in result.errors)
    )


@pytest.mark.parametrize("task_dir", _discovered_task_dirs(), ids=_task_label)
def test_reference_solution_passes_own_task(task_dir: Path):
    """Integrity invariant (solvability): solution.json scores passed == True.

    Graded through the shared dispatcher (issue #110) so a remediation task's
    reference must satisfy its own patch checks, not just the diagnosis.
    """
    solution, expected = _load_rubric_pair(task_dir)
    result = grade_for_task(solution, expected, task_dir, _config_of(task_dir))
    assert result["passed"], (
        f"reference solution for {_task_label(task_dir)} does not pass its own "
        f"rubric (score={result['score']}):\n{json.dumps(result['detail'], indent=2)}"
    )


@pytest.mark.parametrize("task_dir", _discovered_task_dirs(), ids=_task_label)
def test_naive_agent_does_not_pass(task_dir: Path, tmp_path: Path):
    """Integrity invariant (non-triviality): the naive guess agent must fail."""
    config = _config_of(task_dir)
    if split_of(config) == SPLIT_HELDOUT and not heldout_key():
        pytest.skip(
            f"{_task_label(task_dir)} is held-out; set HELDOUT_KEY to check its rubric"
        )
    record = run_task(
        task_dir, NAIVE_AGENT_CMD, name="naive-check", results_root=tmp_path
    )
    assert not record["passed"], (
        f"naive agent PASSED {_task_label(task_dir)} (score={record['score']}) — "
        f"the rubric is guessable:\n{json.dumps(record['detail'], indent=2)}"
    )


@pytest.mark.parametrize("task_dir", _discovered_task_dirs(), ids=_task_label)
def test_symptom_does_not_leak_root_cause(task_dir: Path):
    """Integrity invariant (no leaks): the symptom alone must not match every
    root_cause group — nor name the exact-match answer on change-attribution
    tasks (issue #144): a symptom that contains the culprit change id hands
    over the answer verbatim."""
    config = _config_of(task_dir)
    symptom_text = str(config.get("symptom", ""))
    kind = config.get("kind") if isinstance(config, dict) else None
    _, rubric = _load_rubric_pair(task_dir)
    if kind == KIND_ATTRIBUTION:
        culprit = rubric.get("culprit")
        assert not (
            isinstance(culprit, str)
            and culprit.strip()
            and normalize(culprit) in normalize(symptom_text)
        ), (
            f"symptom of {_task_label(task_dir)} contains the culprit change id "
            f"'{culprit}' — it names the answer"
        )
        return
    if kind == KIND_TRIAGE:
        root_alert = rubric.get("root_alert")
        assert not (
            isinstance(root_alert, str)
            and root_alert.strip()
            and normalize(root_alert) in normalize(symptom_text)
        ), (
            f"symptom of {_task_label(task_dir)} contains the root alert id "
            f"'{root_alert}' — it names the answer"
        )
        return
    if not (isinstance(rubric.get("root_cause"), list) and rubric["root_cause"]):
        # Non-diagnosis kinds (timeline, postmortem, pre-incident review) grade
        # different contracts with no root_cause groups to leak; their own
        # contract tests cover the equivalent check.
        pytest.skip("rubric has no root_cause groups (non-diagnosis kind)")
    result = grade({"root_cause": symptom_text}, rubric)
    entries = result["detail"]["root_cause"]
    matched = [entry["matched"] for entry in entries if entry["matched"] is not None]
    assert len(matched) < len(entries), (
        f"symptom of {_task_label(task_dir)} matches every root_cause group "
        f"({matched}) — it names the answer"
    )


# --- canary (issue #114) -----------------------------------------------------


@pytest.mark.parametrize("task_dir", _discovered_task_dirs(), ids=_task_label)
def test_real_task_carries_canary(task_dir: Path):
    """Every task.yaml must carry the fixed training-corpus canary (issue #114)."""
    config = yaml.safe_load((task_dir / "task.yaml").read_text(encoding="utf-8"))
    assert config.get("canary") == CANARY_GUID, (
        "task.yaml must carry the fixed training-corpus canary (issue #114)"
    )


def test_readme_carries_canary():
    """The README must carry the fixed training-corpus canary (issue #114)."""
    readme = Path(__file__).parent.parent / "README.md"
    assert CANARY_GUID in readme.read_text(encoding="utf-8")


# --- simulated-CLI commands file (issue #111) --------------------------------

VALID_COMMANDS_YAML = (
    "commands:\n"
    "  - command: kubectl get pods -n web\n"
    "    stdout: 'pod-a'\n"
)


def write_commands_file(task_dir: Path, content: str) -> None:
    (task_dir / "fixtures" / "_commands.yaml").write_text(content, encoding="utf-8")


def test_commands_file_is_exempt_from_guid_rule(tmp_path: Path):
    """A real GUID in the sim config is configuration, not a leaked secret."""
    task_dir = copy_task(tmp_path)
    write_commands_file(
        task_dir, f"commands:\n  - command: kubectl get pod\n    stdout: '{REAL_GUID}'\n"
    )
    result = load_one(tmp_path)
    assert result.errors == []


def test_commands_file_does_not_count_toward_fixture_minimum(tmp_path: Path):
    """Only evidence files count: 1 fixture + _commands.yaml is still too few."""
    task_dir = copy_task(tmp_path)
    (task_dir / "fixtures" / "az-monitor-metrics.json").unlink()
    write_commands_file(task_dir, VALID_COMMANDS_YAML)
    result = load_one(tmp_path)
    assert any("found 1" in error for error in result.errors)


def test_valid_commands_file_has_no_errors(tmp_path: Path):
    task_dir = copy_task(tmp_path)
    write_commands_file(task_dir, VALID_COMMANDS_YAML)
    result = load_one(tmp_path)
    assert result.errors == []


def test_commands_file_shape_violations_are_reported(tmp_path: Path):
    task_dir = copy_task(tmp_path)
    write_commands_file(task_dir, "commands:\n  - stdout: 'no command key'\n")
    result = load_one(tmp_path)
    assert (
        "fixtures/_commands.yaml: command #0 must have a non-empty 'command' string"
        in result.errors
    )


def test_commands_file_invalid_yaml_is_reported(tmp_path: Path):
    task_dir = copy_task(tmp_path)
    write_commands_file(task_dir, "commands: [unclosed\n")
    result = load_one(tmp_path)
    assert any(
        error.startswith("fixtures/_commands.yaml: invalid YAML")
        for error in result.errors
    )


# --- explainer.md (issue #149) ------------------------------------------------

VALID_EXPLAINER = """\
# {task_id} — explainer

## How to investigate

Open `fixtures/kubectl-logs-ingress.txt` first: the 502s start at the ingress.

## Why the red herring misleads

`fixtures/az-monitor-metrics.json` shows a dip, but the timestamps predate the
incident — it is background, not cause.

## Root cause

The wrong backend path in the ingress.

## Fix

Correct the path.

## Prevention

Add a manifest lint.

## Further reading

- ingress docs
"""


def write_explainer(task_dir: Path, text: str, task_id: str = "monitoring-001") -> None:
    (task_dir / "explainer.md").write_text(
        text.format(task_id=task_id), encoding="utf-8"
    )


def test_explainer_optional_below_041(tmp_path: Path):
    """Existing-corpus tasks (< 041) stay valid without an explainer."""
    copy_task(tmp_path)
    result = load_one(tmp_path)
    assert not any("explainer" in error for error in result.errors)


def test_explainer_required_from_041(tmp_path: Path):
    task_dir = copy_task(tmp_path)
    renamed = task_dir.parent / "monitoring-041"
    task_dir.rename(renamed)
    patch_yaml(renamed, id="monitoring-041")
    result = load_one(tmp_path)
    assert any(
        error.startswith("missing explainer.md") for error in result.errors
    )


def test_explainer_required_from_041_satisfied(tmp_path: Path):
    task_dir = copy_task(tmp_path)
    renamed = task_dir.parent / "monitoring-041"
    task_dir.rename(renamed)
    patch_yaml(renamed, id="monitoring-041")
    write_explainer(renamed, VALID_EXPLAINER, "monitoring-041")
    result = load_one(tmp_path)
    assert not any("explainer" in error for error in result.errors)


@pytest.mark.parametrize("heading", [
    "How to investigate",
    "Why the red herring misleads",
    "Root cause",
    "Fix",
    "Prevention",
    "Further reading",
])
def test_explainer_missing_heading_is_reported(tmp_path: Path, heading: str):
    task_dir = copy_task(tmp_path)
    write_explainer(task_dir, VALID_EXPLAINER)
    text = (task_dir / "explainer.md").read_text(encoding="utf-8")
    (task_dir / "explainer.md").write_text(
        text.replace(f"## {heading}", "## Something else"), encoding="utf-8"
    )
    result = load_one(tmp_path)
    assert any(
        f"missing required '## {heading}'" in error for error in result.errors
    )


def test_explainer_dead_fixture_reference_is_reported(tmp_path: Path):
    task_dir = copy_task(tmp_path)
    write_explainer(task_dir, VALID_EXPLAINER)
    (task_dir / "explainer.md").write_text(
        (task_dir / "explainer.md").read_text(encoding="utf-8")
        + "\nSee `fixtures/kubectl-describe-node.txt`.\n",
        encoding="utf-8",
    )
    result = load_one(tmp_path)
    assert any(
        "references 'fixtures/kubectl-describe-node.txt' but it does not"
        in error
        for error in result.errors
    )


def test_explainer_valid_has_no_errors(tmp_path: Path):
    task_dir = copy_task(tmp_path)
    write_explainer(task_dir, VALID_EXPLAINER)
    result = load_one(tmp_path)
    assert not any("explainer" in error for error in result.errors)


def test_real_corpus_explainers_are_valid():
    """Every explainer in the real corpus (including the 008 example) validates."""
    for result in load_tasks(Path("tasks")):
        assert not any("explainer" in error for error in result.errors), (
            f"{result.path}: {result.errors}"
        )
