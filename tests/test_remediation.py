"""Tests for remediation-track grading — patch-file checks (issue #110).

Check engine semantics, the patched_files overlay, score composition, runner
integration, and the validator's authoring rules for `kind: remediation`.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from bench import run as runner
from bench.remediation import evaluate_checks, grade_for_task, run_check

REPO_ROOT = Path(__file__).resolve().parent.parent
REMEDIATION_TASKS = [
    REPO_ROOT / "tasks" / "aks-workloads" / "aks-workloads-017",
    REPO_ROOT / "tasks" / "aks-ops" / "aks-ops-022",
]

DEPLOYMENT_YAML = """\
apiVersion: apps/v1
kind: Deployment
spec:
  replicas: 3
  template:
    spec:
      containers:
        - name: app
          image: registry.io/app:1.4.2
          resources:
            requests:
              cpu: 500m
"""


def task_tree(tmp_path: Path) -> Path:
    """A minimal task dir with one YAML fixture; returns the task dir."""
    task_dir = tmp_path / "tasks" / "monitoring" / "monitoring-001"
    (task_dir / "fixtures").mkdir(parents=True)
    (task_dir / "fixtures" / "deployment.yaml").write_text(
        DEPLOYMENT_YAML, encoding="utf-8"
    )
    return task_dir


def make_check(tmp_path: Path, **overrides: object) -> tuple[Path, dict]:
    """Write the standard fixture and return (task_dir, check) against it."""
    task_dir = task_tree(tmp_path)
    check = {
        "file": "fixtures/deployment.yaml",
        "path": "spec.replicas",
        "op": "equals",
        "value": 3,
    }
    check.update(overrides)
    return task_dir, check


def reasons(result: dict) -> list[str]:
    return [reason["reason"] for reason in result["detail"]["reasons"]]


def check_reason(result: dict, which: str) -> dict:
    return next(
        reason
        for reason in result["detail"]["reasons"]
        if reason.get("reason") == which
    )


# --- run_check: op semantics ---------------------------------------------------


def test_equals_passes_on_yaml_scalar(tmp_path: Path):
    root, check = make_check(tmp_path)
    entry = run_check(check, root, {})
    assert entry["passed"] is True


def test_equals_failure_records_expected_and_actual(tmp_path: Path):
    root, check = make_check(tmp_path, value=5)
    entry = run_check(check, root, {})
    assert entry["passed"] is False
    assert entry["expected"] == 5
    assert entry["actual"] == 3


def test_equals_unquotes_yaml_strings(tmp_path: Path):
    """YAML-quoted scalars compare as their parsed value: "3.4" equals 3.4."""
    (tmp_path / "fixtures").mkdir(parents=True)
    (tmp_path / "fixtures" / "version.yaml").write_text('version: "3.4"\n')
    entry = run_check(
        {"file": "fixtures/version.yaml", "path": "version", "op": "equals",
         "value": "3.4"},
        tmp_path, {},
    )
    assert entry["passed"] is True


def test_equals_distinguishes_number_from_string(tmp_path: Path):
    root, check = make_check(tmp_path, value="3")
    assert run_check(check, root, {})["passed"] is False


def test_equals_distinguishes_bool_from_number(tmp_path: Path):
    """Strict typing: YAML `true` is not 1 and 1 is not true."""
    (tmp_path / "fixtures").mkdir(parents=True)
    (tmp_path / "fixtures" / "settings.yaml").write_text("autoscaled: true\n", encoding="utf-8")
    entry = run_check(
        {"file": "fixtures/settings.yaml", "path": "autoscaled", "op": "equals",
         "value": 1},
        tmp_path,
        {},
    )
    assert entry["passed"] is False
    assert run_check(
        {"file": "fixtures/settings.yaml", "path": "autoscaled", "op": "equals",
         "value": True},
        tmp_path,
        {},
    )["passed"] is True


def test_exists_requires_present_and_non_null(tmp_path: Path):
    (tmp_path / "fixtures").mkdir(parents=True)
    (tmp_path / "fixtures" / "svc.yaml").write_text("name: svc\nendpoint: null\n")
    for path, expected in (("name", True), ("endpoint", False), ("missing", False)):
        entry = run_check(
            {"file": "fixtures/svc.yaml", "path": path, "op": "exists"}, tmp_path, {}
        )
        assert entry["passed"] is expected, path


def test_absent_is_the_exact_complement_of_exists(tmp_path: Path):
    (tmp_path / "fixtures").mkdir(parents=True)
    (tmp_path / "fixtures" / "svc.yaml").write_text("name: svc\nendpoint: null\n")
    for path, expected in (("name", False), ("endpoint", True), ("missing", True)):
        entry = run_check(
            {"file": "fixtures/svc.yaml", "path": path, "op": "absent"}, tmp_path, {}
        )
        assert entry["passed"] is expected, path


def test_gte_lte_compare_numbers(tmp_path: Path):
    root, _ = make_check(tmp_path)
    assert run_check(
        {"file": "fixtures/deployment.yaml", "path": "spec.replicas", "op": "gte",
         "value": 2}, root, {},
    )["passed"] is True
    assert run_check(
        {"file": "fixtures/deployment.yaml", "path": "spec.replicas", "op": "lte",
         "value": 3}, root, {},
    )["passed"] is True
    assert run_check(
        {"file": "fixtures/deployment.yaml", "path": "spec.replicas", "op": "gte",
         "value": 4}, root, {},
    )["passed"] is False


def test_equals_matches_string_values(tmp_path: Path):
    root, _ = make_check(tmp_path)
    entry = run_check(
        {"file": "fixtures/deployment.yaml",
         "path": "spec.template.spec.containers.0.resources.requests.cpu",
         "op": "equals", "value": "500m"},
        root, {},
    )
    assert entry["passed"] is True


def test_gte_fails_on_non_numeric_scalar(tmp_path: Path):
    root, _ = make_check(tmp_path)
    entry = run_check(
        {"file": "fixtures/deployment.yaml",
         "path": "spec.template.spec.containers.0.image",
         "op": "gte", "value": 1},
        root, {},
    )
    assert entry["passed"] is False
    assert entry["actual"] == "registry.io/app:1.4.2"


def test_json_fixtures_are_supported(tmp_path: Path):
    (tmp_path / "fixtures").mkdir(parents=True)
    (tmp_path / "fixtures" / "quota.json").write_text('{"limits": {"pods": 110}}')
    entry = run_check(
        {"file": "fixtures/quota.json", "path": "limits.pods", "op": "gte",
         "value": 100},
        tmp_path, {},
    )
    assert entry["passed"] is True


# --- run_check: dotted paths ----------------------------------------------------


def test_dotted_path_list_index(tmp_path: Path):
    root, _ = make_check(tmp_path)
    entry = run_check(
        {"file": "fixtures/deployment.yaml",
         "path": "spec.template.spec.containers.0.image", "op": "equals",
         "value": "registry.io/app:1.4.2"},
        root, {},
    )
    assert entry["passed"] is True


def test_dotted_path_numeric_key(tmp_path: Path):
    (tmp_path / "fixtures").mkdir(parents=True)
    (tmp_path / "fixtures" / "data.yaml").write_text('labels: {"3": "app"}\n')
    entry = run_check(
        {"file": "fixtures/data.yaml", "path": "labels.3", "op": "equals",
         "value": "app"},
        tmp_path, {},
    )
    assert entry["passed"] is True


def test_dotted_path_dot_in_key_is_simply_not_found(tmp_path: Path):
    """Dotted paths cannot address keys containing dots (see AGENTS.md):
    traversal returns not-found rather than erroring."""
    (tmp_path / "fixtures").mkdir(parents=True)
    (tmp_path / "fixtures" / "data.yaml").write_text(
        "annotations: {kubernetes.io/ingress.class: nginx}\n"
    )
    entry = run_check(
        {"file": "fixtures/data.yaml", "path": "annotations.kubernetes",
         "op": "exists"},
        tmp_path, {},
    )
    assert entry["passed"] is False


# --- run_check: structural failures ---------------------------------------------


def test_check_outside_fixtures_is_invalid(tmp_path: Path):
    for bad in ("../app.py", "app.py", "fixtures/../secret.yaml", "/etc/passwd"):
        entry = run_check(
            {"file": bad, "path": "spec.replicas", "op": "exists"}, tmp_path, {}
        )
        assert entry["error_kind"] == "check_invalid", bad


def test_missing_file_is_target_missing(tmp_path: Path):
    root, check = make_check(tmp_path, file="fixtures/absent.yaml")
    entry = run_check(check, root, {})
    assert entry["error_kind"] == "check_target_missing"


def test_unparseable_file_is_target_unparseable(tmp_path: Path):
    (tmp_path / "fixtures").mkdir(parents=True)
    (tmp_path / "fixtures" / "broken.yaml").write_text("a: [unclosed\nb: {x\n")
    entry = run_check(
        {"file": "fixtures/broken.yaml", "path": "a", "op": "exists"},
        tmp_path, {},
    )
    assert entry["error_kind"] == "check_unparseable"


@pytest.mark.parametrize(
    "check,why",
    [
        ("just a string", "not an object"),
        ({"file": "f", "path": "a", "op": "noop"}, "unknown op"),
        ({"file": "f", "path": "a", "op": "equals", "value": []}, "non-scalar value"),
        ({"file": "f", "path": "a", "op": "exists", "value": 1}, "value on exists"),
        ({"file": "f", "path": "", "op": "exists"}, "empty path"),
        ({"file": "f", "path": "a..b", "op": "exists"}, "empty segment"),
        ({"file": "f", "op": "exists"}, "missing path key"),
        ({"file": "fixtures/x.yaml", "path": "a", "op": "equals", "value": 1,
          "note": "hi"}, "unknown key"),
    ],
)
def test_invalid_check_shapes_fail_as_check_invalid(
    tmp_path: Path, check: object, why: str
):
    (tmp_path / "fixtures").mkdir(parents=True)
    (tmp_path / "fixtures" / "x.yaml").write_text("a: 1\n")
    entry = run_check(check, tmp_path, {})
    assert entry["passed"] is False, why
    assert entry["error_kind"] == "check_invalid", why


# --- evaluate_checks ------------------------------------------------------------


def test_evaluate_runs_all_checks_and_reports_failures(tmp_path: Path):
    (tmp_path / "fixtures").mkdir(parents=True)
    (tmp_path / "fixtures" / "a.yaml").write_text("replicas: 3\n")
    (tmp_path / "fixtures" / "b.yaml").write_text("replicas: 9\n")
    entries, failed = evaluate_checks(
        [
            {"file": "fixtures/a.yaml", "path": "replicas", "op": "equals", "value": 3},
            {"file": "fixtures/b.yaml", "path": "replicas", "op": "equals", "value": 3},
        ],
        tmp_path,
    )
    assert [e["passed"] for e in entries] == [True, False]
    assert len(failed) == 1
    assert failed[0]["file"] == "fixtures/b.yaml"
    assert failed[0]["op"] == "equals"


def test_run_check_reuses_parse_cache_per_file(tmp_path: Path):
    (tmp_path / "fixtures").mkdir(parents=True)
    (tmp_path / "fixtures" / "a.yaml").write_text("replicas: 3\n")
    docs: dict = {}
    for check in (
        {"file": "fixtures/a.yaml", "path": "replicas", "op": "exists"},
        {"file": "fixtures/a.yaml", "path": "replicas", "op": "gte", "value": 1},
    ):
        run_check(check, tmp_path, docs)
    assert len(docs) == 1


# --- grade_for_task: the overlay and score model ---------------------------------


def rubric(**overrides: object) -> dict:
    base = {
        "root_cause": [["replica", "count"]],
        "fix": [["scale", "replicas"]],
        "checks": [
            {"file": "fixtures/deployment.yaml", "path": "spec.replicas",
             "op": "equals", "value": 5}
        ],
    }
    base.update(overrides)
    return base


PATCHED = {"fixtures/deployment.yaml": DEPLOYMENT_YAML.replace("replicas: 3", "replicas: 5")}
DIAGNOSIS = {"root_cause": "Replicas scaled down.", "evidence": ["spec: 3"],
             "fix": "scale to 5"}


def test_patched_files_satisfy_checks(tmp_path: Path):
    task_dir = task_tree(tmp_path)
    result = grade_for_task(
        {**DIAGNOSIS, "patched_files": PATCHED},
        rubric(), task_dir, {},
    )
    assert result["passed"] is True
    assert result["score"] == 1.0
    assert all(entry["passed"] for entry in result["detail"]["checks"])


def test_missing_patched_files_fails_checks_against_original(tmp_path: Path):
    """No patches: checks still run, against the original fixture content."""
    task_dir = task_tree(tmp_path)
    result = grade_for_task(DIAGNOSIS, rubric(), task_dir, {"kind": "remediation"})
    assert result["passed"] is False
    assert "check_failed" in reasons(result)
    assert check_reason(result, "check_failed")["actual"] == 3


def test_checks_trigger_remediation_even_without_kind(tmp_path: Path):
    """A rubric declaring checks is graded as remediation even unmarked:
    skipping the patch half would inflate the score."""
    task_dir = task_tree(tmp_path)
    result = grade_for_task(DIAGNOSIS, rubric(), task_dir, {})
    assert result["passed"] is False
    assert "check_failed" in reasons(result)


def test_plain_grade_ignores_unmarked_rubric(tmp_path: Path):
    task_dir = task_tree(tmp_path)
    result = grade_for_task(DIAGNOSIS, rubric(checks=None), task_dir, {})
    assert result["passed"] is True
    assert "checks" not in result["detail"]


def test_remediation_without_checks_fails_closed(tmp_path: Path):
    """kind: remediation but a checks-less rubric: the grader says so."""
    task_dir = task_tree(tmp_path)
    result = grade_for_task(
        DIAGNOSIS, rubric(checks=None), task_dir, {"kind": "remediation"}
    )
    assert result["passed"] is False
    assert "check_invalid" in reasons(result)


def test_patch_outside_fixtures_never_grades(tmp_path: Path):
    """A patch escaping fixtures/ aborts the overlay; checks do not run."""
    task_dir = task_tree(tmp_path)
    escaped = {"../deployment.yaml": DEPLOYMENT_YAML.replace("replicas: 3", "replicas: 5")}
    result = grade_for_task(
        {**DIAGNOSIS, "patched_files": escaped},
        rubric(), task_dir, {"kind": "remediation"},
    )
    assert result["passed"] is False
    assert "patch_outside_fixtures" in reasons(result)
    assert result["detail"]["checks"] == []


def test_patch_to_missing_file_fails(tmp_path: Path):
    task_dir = task_tree(tmp_path)
    result = grade_for_task(
        {**DIAGNOSIS, "patched_files": {"fixtures/absent.yaml": "name: x\n"}},
        rubric(), task_dir, {"kind": "remediation"},
    )
    assert result["passed"] is False
    assert "patch_missing_file" in reasons(result)


def test_checks_read_patched_tree_not_original(tmp_path: Path):
    """Checks evaluate the patched content: an ineffective patch still fails,
    observed against the patched tree."""
    task_dir = task_tree(tmp_path)
    ineffective = {"fixtures/deployment.yaml": DEPLOYMENT_YAML}  # replicas still 3
    result = grade_for_task(
        {**DIAGNOSIS, "patched_files": ineffective},
        rubric(), task_dir, {"kind": "remediation"},
    )
    assert result["passed"] is False
    failed = check_reason(result, "check_failed")
    assert failed["actual"] == 3
    assert failed["expected"] == 5


def test_malformed_patched_files_fail_without_crashing(tmp_path: Path):
    task_dir = task_tree(tmp_path)
    answer = {**DIAGNOSIS, "patched_files": {"fixtures/deployment.yaml": 42}}
    result = grade_for_task(answer, rubric(), task_dir, {"kind": "remediation"})
    assert result["passed"] is False
    assert "patched_files_invalid" in reasons(result)

    result = grade_for_task(
        {**DIAGNOSIS, "patched_files": "nope"}, rubric(), task_dir,
        {"kind": "remediation"},
    )
    assert result["passed"] is False
    assert "patched_files_invalid" in reasons(result)


def test_safety_scan_covers_patched_files(tmp_path: Path):
    task_dir = task_tree(tmp_path)
    unsafe_patch = {
        "fixtures/deployment.yaml": "roleRef:\n  name: cluster-admin\n"
    }
    result = grade_for_task(
        {**DIAGNOSIS, "patched_files": unsafe_patch},
        rubric(), task_dir, {"kind": "remediation"},
    )
    assert result["passed"] is False
    assert "unsafe_fix" in reasons(result)
    assert result["detail"]["safety_violations"]


def test_score_composition_checks_count_as_units(tmp_path: Path):
    """Additive units: 2 rubric groups + 1 check = 3 units; 2 matched = 2/3
    (scores round to three decimals)."""
    task_dir = task_tree(tmp_path)
    result = grade_for_task(DIAGNOSIS, rubric(), task_dir, {"kind": "remediation"})
    assert result["score"] == pytest.approx(round(2 / 3, 3))
    assert result["passed"] is False


# --- runner integration ----------------------------------------------------------


def test_run_task_end_to_end_with_reference_answer(tmp_path: Path):
    """Echo the reference solution through the real runner: must pass."""
    task_dir = REMEDIATION_TASKS[0]
    reference = json.loads((task_dir / "solution.json").read_text(encoding="utf-8"))
    record = runner.run_task(
        task_dir,
        f"{sys.executable} -c 'import json;print(json.dumps(json.load(open(\"{task_dir / 'solution.json'}\"))))'",
        results_root=tmp_path,
    )
    assert record["task"] == "aks-workloads-017"
    assert record["passed"] is True
    assert record["answer"] == reference
    assert all(entry["passed"] for entry in record["detail"]["checks"])


@pytest.mark.parametrize("task_dir", REMEDIATION_TASKS, ids=lambda t: t.name)
def test_workspace_hint_names_patched_files(task_dir: Path, tmp_path: Path):
    workspace = tmp_path / "workspace"
    runner.prepare_workspace(task_dir, workspace)
    symptom = (workspace / "symptom.md").read_text(encoding="utf-8")
    assert "patched_files" in symptom
    assert "fixtures/" in symptom


def test_workspace_hint_absent_for_plain_tasks(tmp_path: Path):
    task_dir = tmp_path / "tasks" / "monitoring" / "monitoring-001"
    (task_dir / "fixtures").mkdir(parents=True)
    (task_dir / "fixtures" / "note.txt").write_text("evidence")
    (task_dir / "task.yaml").write_text(
        "id: monitoring-001\ntitle: t\ncategory: monitoring\ndifficulty: easy\n"
        "tags: [monitoring]\nsymptom: s\n"
    )
    workspace = tmp_path / "workspace"
    runner.prepare_workspace(task_dir, workspace)
    assert "patched_files" not in (workspace / "symptom.md").read_text(encoding="utf-8")


# --- validator -------------------------------------------------------------------


def remediation_task(tmp_path: Path) -> Path:
    """A valid plain task dir with one fixture, ready to be marked remediation."""
    task_dir = tmp_path / "tasks" / "monitoring" / "monitoring-001"
    (task_dir / "fixtures").mkdir(parents=True)
    (task_dir / "fixtures" / "deployment.yaml").write_text(DEPLOYMENT_YAML)
    (task_dir / "fixtures" / "events.txt").write_text("evidence")
    (task_dir / "task.yaml").write_text(
        "id: monitoring-001\n"
        "canary: 00000000-0000-0000-0000-000000000114  # Canary for training-corpus detection — DO NOT REMOVE (see README, License section).\n"
        "title: t\ncategory: monitoring\ndifficulty: easy\n"
        "tags: [monitoring]\nsymptom: s\n"
    )
    (task_dir / "expected.json").write_text(json.dumps(
        {"root_cause": [["replica"]], "fix": [["scale"]],
         "checks": [{"file": "fixtures/deployment.yaml", "path": "spec.replicas",
                     "op": "equals", "value": 3}]}
    ))
    (task_dir / "solution.json").write_text(json.dumps(
        {"root_cause": "replicas", "evidence": ["x"], "fix": "scale",
         "evidence_files": ["deployment.yaml"],
         "patched_files": {"fixtures/deployment.yaml": DEPLOYMENT_YAML}}
    ))
    return task_dir


def load_errors(task_dir: Path) -> list[str]:
    from bench.tasks import inspect_task

    return inspect_task(task_dir).errors


def test_validator_accepts_valid_remediation_task(tmp_path: Path):
    task_dir = remediation_task(tmp_path)
    (task_dir / "task.yaml").write_text(
        (task_dir / "task.yaml").read_text() + "kind: remediation\n"
    )
    assert load_errors(task_dir) == []


def test_validator_rejects_remediation_without_checks(tmp_path: Path):
    task_dir = remediation_task(tmp_path)
    (task_dir / "task.yaml").write_text(
        (task_dir / "task.yaml").read_text() + "kind: remediation\n"
    )
    expected = json.loads((task_dir / "expected.json").read_text())
    del expected["checks"]
    (task_dir / "expected.json").write_text(json.dumps(expected))
    assert any("requires a non-empty 'checks' list" in e for e in load_errors(task_dir))


def test_validator_rejects_checks_without_kind(tmp_path: Path):
    task_dir = remediation_task(tmp_path)  # no kind: remediation
    assert any("'checks' requires task.yaml 'kind: remediation'" in e
               for e in load_errors(task_dir))


def test_validator_rejects_unknown_kind(tmp_path: Path):
    task_dir = remediation_task(tmp_path)
    (task_dir / "task.yaml").write_text(
        (task_dir / "task.yaml").read_text() + "kind: widget\n"
    )
    assert any("kind must be one of" in e for e in load_errors(task_dir))


@pytest.mark.parametrize(
    "check,fragment",
    [
        ({"file": "fixtures/deployment.yaml", "path": "spec.replicas",
          "op": "equals", "value": 3, "extra": 1}, "unknown key(s) extra"),
        ({"file": "fixtures/deployment.yaml", "path": "spec.replicas",
          "op": "before"}, "'op' must be one of"),
        ({"file": "fixtures/deployment.yaml", "path": "spec.replicas",
          "op": "equals"}, "requires a 'value'"),
        ({"file": "fixtures/deployment.yaml", "path": "spec.replicas",
          "op": "equals", "value": [1]}, "must be a string, number, or boolean"),
        ({"file": "fixtures/deployment.yaml", "path": "spec.replicas",
          "op": "exists", "value": 1}, "must not declare a 'value'"),
        ({"file": "app.yaml", "path": "spec.replicas", "op": "equals",
          "value": 3}, "path under fixtures/"),
        ({"file": "fixtures/absent.yaml", "path": "spec.replicas", "op": "equals",
          "value": 3}, "not found in fixtures/"),
        ({"file": "fixtures/deployment.yaml", "path": "spec..replicas",
          "op": "equals", "value": 3}, "non-empty segments"),
        ({"file": "fixtures/deployment.yaml", "path": "a..",
          "op": "equals", "value": 3}, "non-empty segments"),
    ],
)
def test_validator_rejects_malformed_check_entries(
    tmp_path: Path, check: dict, fragment: str
):
    task_dir = remediation_task(tmp_path)
    (task_dir / "task.yaml").write_text(
        (task_dir / "task.yaml").read_text() + "kind: remediation\n"
    )
    expected = json.loads((task_dir / "expected.json").read_text())
    expected["checks"] = [check]
    (task_dir / "expected.json").write_text(json.dumps(expected))
    assert any(fragment in e for e in load_errors(task_dir)), fragment


@pytest.mark.parametrize("task_dir", REMEDIATION_TASKS, ids=lambda t: t.name)
def test_converted_real_tasks_validate_cleanly(task_dir: Path):
    from bench.tasks import inspect_task

    result = inspect_task(task_dir)
    assert result.errors == []
    assert result.warnings == []
