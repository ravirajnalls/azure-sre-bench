"""Tests for the change-attribution track (issue #144).

Culprit exact-match semantics, the multi-id guard, distractor scoping to the
`reasoning` field, length caps, the fix-channel safety scan, dispatcher
routing, and the validator's authoring rules for `kind: change-attribution`.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from bench.attribution import (
    ANSWER_CONTRACT_BLOCK,
    CULPRIT_WRONG,
    KIND_ATTRIBUTION,
    MULTIPLE_CULPRITS,
    change_ids_in,
    grade_attribution,
)
from bench.remediation import grade_for_task
from bench.run import prepare_workspace
from bench.tasks import inspect_task

REPO_ROOT = Path(__file__).resolve().parent.parent
ATTRIBUTION_TASKS = [
    REPO_ROOT / "tasks" / "aks-ops" / "aks-ops-069",
]

RUBRIC = {
    "culprit": "CHG-1042",
    "fix": [["roll back", "revert"], ["connection pool", "pool size"]],
    "distractors": [["CHG-0991", "config change 0991"]],
}

GOOD_ANSWER = {
    "culprit": "CHG-1042",
    "reasoning": "CHG-1042 raised the pool limit and exhausted sockets.",
    "fix": "Roll back CHG-1042 to restore the connection pool size.",
}


def reasons(result: dict) -> list[str]:
    return [reason["reason"] for reason in result["detail"]["reasons"]]


def culprit_detail(result: dict) -> dict:
    return result["detail"]["culprit"]


# --- grade_attribution: the culprit unit ---------------------------------------


def test_correct_answer_passes():
    result = grade_attribution(GOOD_ANSWER, RUBRIC)
    assert result["passed"] is True
    assert result["score"] == 1.0
    assert culprit_detail(result)["matched"] is True
    assert reasons(result) == []


def test_culprit_matches_after_normalization():
    result = grade_attribution(
        {**GOOD_ANSWER, "culprit": "  chg-1042 \n"}, RUBRIC
    )
    assert result["passed"] is True


def test_wrong_culprit_fails_and_earns_no_culprit_credit():
    result = grade_attribution({**GOOD_ANSWER, "culprit": "CHG-0991"}, RUBRIC)
    assert result["passed"] is False
    assert CULPRIT_WRONG in reasons(result)
    # 1 of 3 units (culprit + 2 fix groups) still matched (3-dp convention).
    assert result["score"] == pytest.approx(round(2 / 3, 3))
    assert culprit_detail(result)["matched"] is False


def test_missing_culprit_fails():
    result = grade_attribution(
        {"reasoning": "x", "fix": "roll back"}, RUBRIC
    )
    assert result["passed"] is False
    assert CULPRIT_WRONG in reasons(result)


def test_multiple_change_ids_in_culprit_fail_by_name():
    answer = {**GOOD_ANSWER, "culprit": "CHG-1042 and CHG-0991"}
    result = grade_attribution(answer, RUBRIC)
    assert result["passed"] is False
    assert MULTIPLE_CULPRITS in reasons(result)


def test_non_id_prose_culprit_is_only_a_wrong_culprit():
    """Prose without id-shaped tokens fails as a wrong culprit, not as hedging."""
    result = grade_attribution({**GOOD_ANSWER, "culprit": "the deploy"}, RUBRIC)
    assert reasons(result) == [CULPRIT_WRONG]


def test_change_ids_in_extracts_distinct_ids_in_order():
    assert change_ids_in("chg-2 or CHG-2 again, then dep-7") == ["CHG-2", "DEP-7"]
    assert change_ids_in(None) == []  # type: ignore[arg-type]


# --- fix groups -----------------------------------------------------------------


def test_partial_fix_matches_score_fractionally():
    result = grade_attribution(
        {**GOOD_ANSWER, "fix": "roll back the deployment"}, RUBRIC
    )
    assert result["passed"] is False
    assert result["score"] == pytest.approx(round(2 / 3, 3))


# --- distractors: scoped to the reasoning field (issue #94 semantics) -----------


def test_distractor_named_in_reasoning_fails_without_score_change():
    reasoning = (
        "CHG-0991 changed connection settings, and CHG-1042 raised the pool "
        "limit — CHG-1042 exhausted sockets."
    )
    result = grade_attribution({**GOOD_ANSWER, "reasoning": reasoning}, RUBRIC)
    assert result["passed"] is False
    reason = next(
        r for r in result["detail"]["reasons"] if r["reason"] == "named_distractor"
    )
    assert reason["field"] == "reasoning"
    assert result["score"] == 1.0


def test_distractor_mention_in_fix_or_evidence_does_not_fire():
    result = grade_attribution(
        {
            **GOOD_ANSWER,
            "fix": "Roll back CHG-1042 to restore the connection pool; CHG-0991 was not the cause.",
            "evidence": ["CHG-0991 touched the same namespace"],
        },
        RUBRIC,
    )
    assert result["passed"] is True
    assert "named_distractor" not in reasons(result)


# --- length caps (issue #93) ------------------------------------------------------


def test_over_cap_fix_earns_no_fix_credit():
    # Would match both fix groups if not over the 600-char default cap.
    long_fix = "roll back chg-1042 to restore the connection pool " * 13
    result = grade_attribution({**GOOD_ANSWER, "fix": long_fix}, RUBRIC)
    assert result["passed"] is False
    reason = next(
        r for r in result["detail"]["reasons"] if r["reason"] == "answer_too_long"
    )
    assert reason["field"] == "fix"
    assert result["score"] == pytest.approx(round(1 / 3, 3))


def test_over_cap_reasoning_fails():
    result = grade_attribution(
        {**GOOD_ANSWER, "reasoning": "story " * 200}, RUBRIC
    )
    assert result["passed"] is False
    assert any(
        r["reason"] == "answer_too_long" and r["field"] == "reasoning"
        for r in result["detail"]["reasons"]
    )


def test_culprit_field_is_never_length_capped():
    result = grade_attribution({**GOOD_ANSWER, "culprit": "C" * 2000}, RUBRIC)
    assert not any(
        r["reason"] == "answer_too_long" and r["field"] == "culprit"
        for r in result["detail"]["reasons"]
    )


# --- safety: the fix field is a fix channel (issue #113) --------------------------


def test_unsafe_fix_is_penalized_and_fails():
    result = grade_attribution(
        {
            **GOOD_ANSWER,
            "fix": "Roll back CHG-1042 and verify with curl -k https://checkout-api/healthz",
        },
        RUBRIC,
    )
    assert result["passed"] is False
    assert "unsafe_fix" in reasons(result)
    assert result["score"] < 1.0


def test_unsafe_language_in_reasoning_is_not_scanned():
    result = grade_attribution(
        {
            **GOOD_ANSWER,
            "reasoning": "Someone should just az group delete the whole pool setup.",
        },
        RUBRIC,
    )
    assert result["passed"] is True


# --- fail-closed rubric handling ---------------------------------------------------


def test_malformed_rubric_never_passes():
    for rubric in (
        {},
        {"culprit": "CHG-1042"},
        {"culprit": "CHG-1042", "fix": []},
        {"culprit": "", "fix": [["x"]]},
        {"culprit": "CHG-1042", "fix": "roll back"},
    ):
        result = grade_attribution(GOOD_ANSWER, rubric)  # type: ignore[arg-type]
        assert result["passed"] is False
        assert "attribution_rubric_invalid" in reasons(result)


def test_non_dict_answer_and_rubric_fail_closed():
    result = grade_attribution("nope", RUBRIC)  # type: ignore[arg-type]
    assert result["passed"] is False


# --- dispatcher routing ------------------------------------------------------------


def test_grade_for_task_routes_attribution_kind():
    task = {"kind": KIND_ATTRIBUTION}
    assert grade_for_task(GOOD_ANSWER, RUBRIC, Path("."), task)["passed"] is True


def test_grade_for_task_sniffs_attribution_rubric_without_config():
    assert grade_for_task(GOOD_ANSWER, RUBRIC, Path("."), None)["passed"] is True


def test_grade_for_task_sniffs_keeps_wrong_culprit_failing():
    result = grade_for_task({**GOOD_ANSWER, "culprit": "CHG-1"}, RUBRIC, Path("."), {})
    assert result["passed"] is False


# --- runner and manual contract surfaces -------------------------------------------


@pytest.mark.parametrize("task_dir", ATTRIBUTION_TASKS)
def test_real_attribution_task_answers_under_its_contract(task_dir: Path):
    """The real aks-ops-069 rubric passes its own reference answer and fails
    the natural wrong-culprit answer (the red-herring change)."""
    rubric = json.loads((task_dir / "expected.json").read_text(encoding="utf-8"))
    solution = json.loads((task_dir / "solution.json").read_text(encoding="utf-8"))
    result = grade_attribution(solution, rubric)
    assert result["passed"] is True, result["detail"]
    distractor_ids = [
        alt
        for group in rubric.get("distractors", [])
        for alt in group
        if alt != rubric["culprit"]
    ]
    for decoy in distractor_ids[:1]:
        wrong = grade_attribution({**solution, "culprit": decoy}, rubric)
        assert wrong["passed"] is False


def test_prepare_workspace_states_the_attribution_contract(tmp_path: Path):
    task_dir = tmp_path / "tasks" / "aks-ops" / "aks-ops-069"
    task_dir.mkdir(parents=True)
    (task_dir / "task.yaml").write_text(
        yaml.safe_dump(
            {"id": "aks-ops-069", "kind": "change-attribution", "symptom": "s"}
        ),
        encoding="utf-8",
    )
    prepare_workspace(task_dir, tmp_path)
    symptom_md = (tmp_path / "symptom.md").read_text(encoding="utf-8")
    assert "change-attribution" in symptom_md
    assert '"culprit"' in symptom_md
    assert ANSWER_CONTRACT_BLOCK.strip().splitlines()[0] in symptom_md


# --- validator authoring rules ------------------------------------------------------


def attribution_task_tree(tmp_path: Path, **rubric_overrides: object) -> Path:
    task_dir = tmp_path / "tasks" / "aks-ops" / "aks-ops-069"
    (task_dir / "fixtures").mkdir(parents=True)
    (task_dir / "fixtures" / "changes.json").write_text(
        json.dumps(
            [
                {"id": "CHG-1042", "summary": "raise pool size"},
                {"id": "CHG-0991", "summary": "bump image tag"},
            ]
        ),
        encoding="utf-8",
    )
    rubric = {
        "culprit": "CHG-1042",
        "fix": [["roll back", "revert"]],
        "distractors": [["CHG-0991", "image bump"]],
    }
    rubric.update(rubric_overrides)
    for key, value in list(rubric_overrides.items()):
        if value is None:
            rubric.pop(key)  # type: ignore[misc]
    (task_dir / "expected.json").write_text(json.dumps(rubric), encoding="utf-8")
    (task_dir / "task.yaml").write_text(
        yaml.safe_dump(
            {
                "id": "aks-ops-069",
                "title": "t",
                "category": "aks-ops",
                "difficulty": "easy",
                "tags": ["kubernetes"],
                "symptom": "CHG-0991 bumped the image and the pool settings "
                "changed; checkout fails.",
                "kind": KIND_ATTRIBUTION,
            }
        ),
        encoding="utf-8",
    )
    return task_dir


def validator_errors(task_dir: Path) -> list[str]:
    return inspect_task(task_dir).errors


def test_validator_accepts_well_formed_attribution_task(tmp_path: Path):
    task_dir = attribution_task_tree(tmp_path)
    assert not [
        e for e in validator_errors(task_dir) if "expected.json" in e
    ]


def test_validator_requires_culprit(tmp_path: Path):
    task_dir = attribution_task_tree(tmp_path, culprit=None)
    assert any("'culprit' must be a non-empty string" in e for e in validator_errors(task_dir))


def test_validator_requires_fix_groups(tmp_path: Path):
    task_dir = attribution_task_tree(tmp_path, fix=[])
    assert any("'fix' must be a non-empty list" in e for e in validator_errors(task_dir))


def test_validator_rejects_distractor_overlapping_culprit(tmp_path: Path):
    task_dir = attribution_task_tree(tmp_path, distractors=[["CHG-1042 roll"]])
    assert any("overlaps" in e for e in validator_errors(task_dir))


def test_validator_requires_culprit_grounded_in_fixtures(tmp_path: Path):
    task_dir = attribution_task_tree(tmp_path, culprit="CHG-7777")
    assert any("does not appear in any fixture" in e for e in validator_errors(task_dir))


def test_validator_attribution_ignores_missing_root_cause(tmp_path: Path):
    """The diagnosis-shaped rule must not fire on attribution rubrics."""
    task_dir = attribution_task_tree(tmp_path)
    assert not any("'root_cause'" in e for e in validator_errors(task_dir))


def test_attribution_task_with_diagnosis_rubric_fails_validation(tmp_path: Path):
    """kind: change-attribution with a root_cause-shaped rubric is an error,
    not a silently mis-graded diagnosis."""
    task_dir = attribution_task_tree(tmp_path)
    rubric = {"root_cause": [["pool"]], "fix": [["roll back"]]}
    (task_dir / "expected.json").write_text(json.dumps(rubric), encoding="utf-8")
    assert any("'culprit'" in e for e in validator_errors(task_dir))
