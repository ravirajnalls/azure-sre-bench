"""Tests for the alert-triage track (issue #145).

Root-alert exact matching, the derived symptom set, classification scoring
with the 0.8 pass gate, the volume attack, fail-closed fixture handling,
dispatcher routing, and the validator's authoring rules for
`kind: alert-triage`.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from bench.remediation import grade_for_task
from bench.run import prepare_workspace
from bench.tasks import inspect_task
from bench.triage import (
    KIND_TRIAGE,
    SYMPTOM_PASS_FRACTION,
    grade_triage,
    read_alerts,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
TRIAGE_TASKS = [
    REPO_ROOT / "tasks" / "aks-ops" / "aks-ops-071",
    REPO_ROOT / "tasks" / "data" / "data-072",
]

ALERTS = {
    "alerts": [
        {"alert_id": "ALT-01", "name": "cert-expiry", "state": "firing"},
        {"alert_id": "ALT-02", "name": "gateway-5xx", "state": "firing"},
        {"alert_id": "ALT-03", "name": "latency-high", "state": "firing"},
        {"alert_id": "ALT-04", "name": "sync-job-failed", "state": "firing"},
    ]
}

RUBRIC = {"root_alert": "ALT-01"}

GOOD_ANSWER = {
    "root_alert": "ALT-01",
    "symptom_alerts": ["ALT-02", "ALT-03", "ALT-04"],
    "reasoning": "The expired certificate broke the sync; everything else is fallout.",
}


def alerts_file(task_dir: Path) -> Path:
    return task_dir / "fixtures" / "alerts.json"


def task_tree(tmp_path: Path, **overrides: object) -> Path:
    """A minimal alert-triage task dir; `overrides` patch the rubric,
    `rubric_<key>=None` drops the key, `alerts=<value>` replaces the fixture."""
    task_dir = tmp_path / "tasks" / "data" / "data-072"
    (task_dir / "fixtures").mkdir(parents=True)
    (task_dir / "fixtures" / "alerts.json").write_text(
        json.dumps(overrides.pop("alerts", ALERTS)), encoding="utf-8"
    )
    rubric = {"root_alert": "ALT-01"}
    rubric.update(overrides)
    for key, value in list(rubric.items()):
        if value is None:
            rubric.pop(key)  # type: ignore[misc]
    (task_dir / "expected.json").write_text(json.dumps(rubric), encoding="utf-8")
    (task_dir / "task.yaml").write_text(
        yaml.safe_dump(
            {
                "id": "data-072",
                "title": "t",
                "category": "data",
                "difficulty": "easy",
                "tags": ["cosmosdb"],
                "symptom": "Several alerts fired after a certificate expired.",
                "kind": KIND_TRIAGE,
            }
        ),
        encoding="utf-8",
    )
    return task_dir


def reasons(result: dict) -> list[str]:
    return [reason["reason"] for reason in result["detail"]["reasons"]]


# --- read_alerts: the fixture loader -----------------------------------------------


def test_read_alerts_collects_ids_in_order(tmp_path: Path):
    path = tmp_path / "alerts.json"
    path.write_text(json.dumps(ALERTS), encoding="utf-8")
    ids, duplicates, error = read_alerts(path)
    assert ids == ["ALT-01", "ALT-02", "ALT-03", "ALT-04"]
    assert duplicates == []
    assert error is None


def test_read_alerts_reports_duplicates(tmp_path: Path):
    path = tmp_path / "alerts.json"
    path.write_text(
        json.dumps(
            {"alerts": [{"alert_id": "ALT-01"}, {"alert_id": "ALT-01"}]}
        ),
        encoding="utf-8",
    )
    ids, duplicates, _ = read_alerts(path)
    assert ids == ["ALT-01"]
    assert duplicates == ["ALT-01"]


def test_read_alerts_fails_closed_on_bad_files(tmp_path: Path):
    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    assert read_alerts(broken)[2] is not None
    assert read_alerts(tmp_path / "absent.json")[2] is not None
    empty = tmp_path / "empty.json"
    empty.write_text("[]", encoding="utf-8")
    assert read_alerts(empty)[2] is not None


# --- grade_triage: the root unit ----------------------------------------------------


def test_correct_answer_passes(tmp_path_factory):
    task_dir = task_tree(tmp_path_factory.mktemp("t"))
    result = grade_triage(GOOD_ANSWER, RUBRIC, task_dir)
    assert result["passed"] is True
    assert result["score"] == 1.0
    assert reasons(result) == []


def test_root_matches_after_normalization(tmp_path_factory):
    task_dir = task_tree(tmp_path_factory.mktemp("t"))
    result = grade_triage({**GOOD_ANSWER, "root_alert": " alt-01 "}, RUBRIC, task_dir)
    assert result["passed"] is True


def test_wrong_root_fails_even_with_correct_symptom_labels(tmp_path_factory):
    """The claimed root is a true symptom, so one label becomes misclassified:
    hits=2, misclassified=1 of 3 symptoms → fraction 1/3, score (0+1/3)/2."""
    task_dir = task_tree(tmp_path_factory.mktemp("t"))
    result = grade_triage({**GOOD_ANSWER, "root_alert": "ALT-02"}, RUBRIC, task_dir)
    assert result["passed"] is False
    assert "root_alert_wrong" in reasons(result)
    assert result["score"] == pytest.approx(1 / 6)


def test_missing_root_fails(tmp_path_factory):
    task_dir = task_tree(tmp_path_factory.mktemp("t"))
    result = grade_triage({"symptom_alerts": ["ALT-02"]}, RUBRIC, task_dir)
    assert result["passed"] is False
    assert "root_alert_wrong" in reasons(result)


# --- classification scoring ----------------------------------------------------------


def test_one_miss_of_three_symptoms_fails_the_gate(tmp_path_factory):
    """3 true symptoms, 2 labeled → 2/3 < 0.8: small storms tolerate no miss."""
    task_dir = task_tree(tmp_path_factory.mktemp("t"))
    result = grade_triage(
        {"root_alert": "ALT-01", "symptom_alerts": ["ALT-02", "ALT-03"]},
        RUBRIC,
        task_dir,
    )
    assert SYMPTOM_PASS_FRACTION == 0.8
    assert result["detail"]["symptom_classification"]["fraction"] == pytest.approx(2 / 3)
    assert result["passed"] is False


def test_one_miss_of_five_symptoms_exactly_meets_the_gate(tmp_path_factory):
    """5 true symptoms, 4 labeled → 4/5 = 0.8 ≥ gate: larger storms forgive
    one miss, pinning the >= in '>=80% classification' (issue #145)."""
    task_dir = task_tree(
        tmp_path_factory.mktemp("t"),
        alerts={
            "alerts": ALERTS["alerts"]
            + [{"alert_id": "ALT-05"}, {"alert_id": "ALT-06"}]
        },
    )
    answer = {
        "root_alert": "ALT-01",
        "symptom_alerts": ["ALT-02", "ALT-03", "ALT-04", "ALT-05"],
    }
    result = grade_triage(answer, RUBRIC, task_dir)
    assert result["detail"]["symptom_classification"]["fraction"] == pytest.approx(0.8)
    assert result["passed"] is True
    assert result["score"] == pytest.approx(0.9)


def test_volume_attack_fails(tmp_path_factory):
    """Listing every alert (plus junk) subtracts into failure, not coverage.
    entries=[01,02,03,04,99]: hits=3 (02-04), misclassified=2 (root-as-symptom
    and the unknown id) of 3 symptoms → fraction 1/3, score (0+1/3)/2 = 1/6."""
    task_dir = task_tree(tmp_path_factory.mktemp("t"))
    answer = {
        "root_alert": "ALT-01",
        "symptom_alerts": ["ALT-01", "ALT-02", "ALT-03", "ALT-04", "ALT-99"],
        "reasoning": "shotgun",
    }
    result = grade_triage(answer, RUBRIC, task_dir)
    assert result["passed"] is False
    assert result["score"] == pytest.approx(1 / 6)
    assert "root_classified_as_symptom" in reasons(result)
    assert "symptom_fraction_below_threshold" in reasons(result)


def test_unknown_ids_are_misclassifications(tmp_path_factory):
    task_dir = task_tree(tmp_path_factory.mktemp("t"))
    answer = {
        "root_alert": "ALT-01",
        "symptom_alerts": ["ALT-02", "ALT-GHOST"],
    }
    result = grade_triage(answer, RUBRIC, task_dir)
    detail = result["detail"]["symptom_classification"]
    assert detail["unknown_ids"] == ["ALT-GHOST"]
    assert detail["fraction"] == 0.0


def test_duplicate_labels_are_deduplicated(tmp_path_factory):
    task_dir = task_tree(tmp_path_factory.mktemp("t"))
    answer = {
        "root_alert": "ALT-01",
        "symptom_alerts": ["ALT-02", "ALT-02", "alt-03"],
    }
    result = grade_triage(answer, RUBRIC, task_dir)
    detail = result["detail"]["symptom_classification"]
    assert detail["duplicates_ignored"] == 1
    assert detail["hits"] == 2


def test_non_string_labels_are_ignored(tmp_path_factory):
    task_dir = task_tree(tmp_path_factory.mktemp("t"))
    result = grade_triage(
        {"root_alert": "ALT-01", "symptom_alerts": [42, None, "ALT-02"]},
        RUBRIC,
        task_dir,
    )
    assert result["detail"]["symptom_classification"]["hits"] == 1


def test_symptom_alerts_non_list_fails_closed(tmp_path_factory):
    task_dir = task_tree(tmp_path_factory.mktemp("t"))
    result = grade_triage(
        {"root_alert": "ALT-01", "symptom_alerts": "ALT-02 ALT-03"}, RUBRIC, task_dir
    )
    assert result["passed"] is False


# --- fail-closed fixture and rubric handling ------------------------------------------


def test_missing_alerts_fixture_fails_closed(tmp_path: Path):
    task_dir = task_tree(tmp_path)
    alerts_file(task_dir).unlink()
    result = grade_triage(GOOD_ANSWER, RUBRIC, task_dir)
    assert result["passed"] is False
    assert "symptom_set_unavailable" in reasons(result)


def test_malformed_rubric_never_passes(tmp_path: Path):
    task_dir = task_tree(tmp_path)
    for rubric in ({}, {"root_alert": ""}, {"root_alert": None}):
        result = grade_triage(GOOD_ANSWER, rubric, task_dir)  # type: ignore[arg-type]
        assert result["passed"] is False
        assert "triage_rubric_invalid" in reasons(result)


def test_no_length_cap_violation_scoring_without_reasoning(tmp_path: Path):
    """`reasoning` is optional content-wise; an over-cap one fails via #93."""
    task_dir = task_tree(tmp_path)
    result = grade_triage(
        {**GOOD_ANSWER, "reasoning": "x" * 1000}, RUBRIC, task_dir
    )
    assert result["passed"] is False
    assert any(
        r["reason"] == "answer_too_long" and r["field"] == "reasoning"
        for r in result["detail"]["reasons"]
    )


# --- dispatcher routing -----------------------------------------------------------------


def test_grade_for_task_routes_triage_kind(tmp_path: Path):
    task_dir = task_tree(tmp_path)
    task = {"kind": KIND_TRIAGE}
    result = grade_for_task(GOOD_ANSWER, RUBRIC, task_dir, task)
    assert result["passed"] is True


def test_grade_for_task_sniffs_triage_rubric_without_config(tmp_path: Path):
    task_dir = task_tree(tmp_path)
    result = grade_for_task(GOOD_ANSWER, RUBRIC, task_dir, None)
    assert result["passed"] is True


# --- real task, runner surface, validator ------------------------------------------------


@pytest.mark.parametrize("task_dir", TRIAGE_TASKS)
def test_real_triage_task_answers_under_its_contract(task_dir: Path):
    rubric = json.loads((task_dir / "expected.json").read_text(encoding="utf-8"))
    solution = json.loads((task_dir / "solution.json").read_text(encoding="utf-8"))
    result = grade_triage(solution, rubric, task_dir)
    assert result["passed"] is True, result["detail"]
    ids, _, _ = read_alerts(alerts_file(task_dir))
    wrong_root = next(alert_id for alert_id in ids if alert_id != rubric["root_alert"])
    wrong = grade_triage(
        {**solution, "root_alert": wrong_root, "symptom_alerts": ids}, rubric, task_dir
    )
    assert wrong["passed"] is False


def test_prepare_workspace_states_the_triage_contract(tmp_path: Path):
    task_dir = task_tree(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    prepare_workspace(task_dir, workspace)
    symptom_md = (workspace / "symptom.md").read_text(encoding="utf-8")
    assert "alert-triage" in symptom_md
    assert '"root_alert"' in symptom_md


def test_validator_accepts_well_formed_triage_task(tmp_path: Path):
    task_dir = task_tree(tmp_path)
    assert not [e for e in inspect_task(task_dir).errors if "expected.json" in e]


def test_validator_requires_root_alert_in_fixture(tmp_path: Path):
    task_dir = task_tree(tmp_path, root_alert="ALT-99")
    errors = inspect_task(task_dir).errors
    assert any("is not an alert id" in e for e in errors)


def test_validator_requires_well_formed_alerts_fixture(tmp_path: Path):
    task_dir = task_tree(tmp_path)
    alerts_file(task_dir).write_text("{broken", encoding="utf-8")
    errors = inspect_task(task_dir).errors
    assert any("fixtures/alerts.json" in e for e in errors)


def test_validator_flags_duplicate_alert_ids(tmp_path: Path):
    task_dir = task_tree(
        tmp_path,
        alerts={"alerts": [{"alert_id": "ALT-01"}, {"alert_id": "ALT-01"}]},
    )
    errors = inspect_task(task_dir).errors
    assert any("duplicate alert id" in e for e in errors)


def test_validator_rejects_distractors_on_triage(tmp_path: Path):
    task_dir = task_tree(tmp_path, distractors=[["ALT-02"]])
    errors = inspect_task(task_dir).errors
    assert any("no channel" in e for e in errors)


def test_validator_triage_needs_no_distractor_warning(tmp_path: Path):
    task_dir = task_tree(tmp_path)
    assert not any("distractors" in w for w in inspect_task(task_dir).warnings)
