"""Tests for the pre-incident-review track (issue #147).

Finding matching (file + line ±3 + issue groups), the precision budget
(false findings), distractors on `issue` text, safety and length caps, the
`grade_for_task` dispatch, and the validator's authoring rules.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bench import run as runner
from bench.preincident import (
    FINDINGS_INVALID,
    FINDINGS_OVERFLOW,
    KIND_PRE_INCIDENT_REVIEW,
    LINE_TOLERANCE,
    MAX_FALSE_FINDINGS,
    MAX_FINDINGS,
    MISSING_REQUIRED_FINDING,
    TOO_MANY_FALSE_FINDINGS,
    grade_pre_incident_review,
)

BICEP = """\
param clusterName string = 'orders-prod'
param kubernetesVersion string = '1.30.4'

resource cluster 'Microsoft.ContainerService/managedClusters@2024-03-02-preview' = {
  name: clusterName
  properties: {
    kubernetesVersion: kubernetesVersion
    tags: { environment: 'prod', costcenter: '4471' }
  }
}
"""


def task_tree(tmp_path: Path) -> Path:
    """A minimal task dir with one reviewed-file fixture; returns the task dir."""
    task_dir = tmp_path / "tasks" / "aks-ops" / "aks-ops-073"
    (task_dir / "fixtures").mkdir(parents=True)
    (task_dir / "fixtures" / "aks.bicep").write_text(BICEP, encoding="utf-8")
    # A second fixture keeps the validator's 2-10 fixture rule happy and gives
    # the diff context a real review would see.
    (task_dir / "fixtures" / "pr.diff").write_text(
        "--- a/aks.bicep\n+++ b/aks.bicep\n@@ -7,6 +7,7 @@\n"
        "     properties:\n       kubernetesVersion: kubernetesVersion\n"
        "+    agentPoolProfiles: [{ name: 'pool1', maxSurge: '100%' }]\n",
        encoding="utf-8",
    )
    return task_dir


def rubric(**overrides: object) -> dict:
    base = {
        "findings": [
            {
                "file": "aks.bicep",
                "line": 4,
                "issue": [["maxsurge", "surge"], ["subnet", "ip"]],
            }
        ],
        "distractors": [["kubernetes version", "costcenter"]],
    }
    base.update(overrides)
    return base


def finding(**overrides: object) -> dict:
    base = {
        "file": "aks.bicep",
        "line": 4,
        "issue": "maxSurge 100% doubles the pool; the subnet cannot allocate the pod IPs",
        "fix": "Set maxSurge to 33% or resize the subnet before the upgrade window.",
    }
    base.update(overrides)
    return base


def reasons(result: dict) -> list[str]:
    return [reason["reason"] for reason in result["detail"]["reasons"]]


def reason_of(result: dict, which: str) -> dict:
    return next(
        reason
        for reason in result["detail"]["reasons"]
        if reason.get("reason") == which
    )


# --- grading: the required finding ----------------------------------------------


def test_perfect_finding_passes():
    result = grade_pre_incident_review({"findings": [finding()]}, rubric())
    assert result["passed"] is True
    assert result["score"] == 1.0


def test_fixtures_prefix_is_accepted():
    answer = {"findings": [finding(file="fixtures/aks.bicep")]}
    assert grade_pre_incident_review(answer, rubric())["passed"] is True


@pytest.mark.parametrize("offset", [-3, -1, 0, 1, 3])
def test_line_within_tolerance_passes(offset: int):
    answer = {"findings": [finding(line=4 + offset)]}
    assert grade_pre_incident_review(answer, rubric())["passed"] is True


@pytest.mark.parametrize("offset", [-4, 4])
def test_line_beyond_tolerance_fails(offset: int):
    assert abs(offset) > LINE_TOLERANCE
    answer = {"findings": [finding(line=4 + offset)]}
    result = grade_pre_incident_review(answer, rubric())
    assert result["passed"] is False
    assert MISSING_REQUIRED_FINDING in reasons(result)


def test_wrong_file_fails():
    answer = {"findings": [finding(file="network.tf")]}
    result = grade_pre_incident_review(answer, rubric())
    assert MISSING_REQUIRED_FINDING in reasons(result)


def test_missing_issue_group_fails():
    # Second group ([subnet | ip]) never matched — a vague issue earns nothing.
    answer = {"findings": [finding(issue="the maxsurge change will break the upgrade")]}
    result = grade_pre_incident_review(answer, rubric())
    assert result["passed"] is False
    assert MISSING_REQUIRED_FINDING in reasons(result)


def test_synonym_alternatives_match():
    answer = {
        "findings": [
            finding(issue="max surge will exhaust subnet addresses mid-upgrade")
        ]
    }
    assert grade_pre_incident_review(answer, rubric())["passed"] is True


# --- grading: the precision budget ----------------------------------------------


def test_two_false_findings_are_tolerated():
    answer = {
        "findings": [
            finding(),
            finding(file="pr.diff", line=1, issue="a comment line", fix="none"),
            finding(file="pr.diff", line=2, issue="another comment", fix="none"),
        ]
    }
    result = grade_pre_incident_review(answer, rubric())
    assert result["passed"] is True
    assert result["detail"]["false_findings"] == 2


def test_three_false_findings_fail():
    answer = {
        "findings": [
            finding(),
            *[
                finding(file="pr.diff", line=n, issue=f"noise {n}", fix="none")
                for n in range(3)
            ],
        ]
    }
    result = grade_pre_incident_review(answer, rubric())
    assert result["passed"] is False
    reason = reason_of(result, TOO_MANY_FALSE_FINDINGS)
    assert reason["count"] == 3
    assert reason["limit"] == MAX_FALSE_FINDINGS


def test_more_than_five_findings_overflows():
    answer = {"findings": [finding(), *[finding(file="pr.diff", line=n, issue="x", fix="y") for n in range(5)]]}
    result = grade_pre_incident_review(answer, rubric())
    assert FINDINGS_OVERFLOW in reasons(result)
    assert reason_of(result, FINDINGS_OVERFLOW)["limit"] == MAX_FINDINGS


# --- grading: distractors, safety, caps -----------------------------------------


def test_distractor_in_issue_text_fails_even_with_correct_finding():
    answer = {
        "findings": [
            finding(),
            finding(
                file="aks.bicep",
                line=2,
                issue="the kubernetes version bump will break the cluster",
            ),
        ]
    }
    result = grade_pre_incident_review(answer, rubric())
    assert result["passed"] is False
    assert result["score"] == 1.0  # distractors cost score nothing, only `passed`
    reason = reason_of(result, "named_distractor")
    assert reason["matched"] == "kubernetes version"


def test_distractor_never_matches_fix_text():
    answer = {
        "findings": [
            finding(fix="keep the kubernetes version pinned; set maxSurge to 33%")
        ]
    }
    result = grade_pre_incident_review(answer, rubric())
    assert result["passed"] is True


def test_unsafe_fix_text_fails():
    answer = {"findings": [finding(fix="grant cluster-admin to the pod service account")]}
    result = grade_pre_incident_review(answer, rubric())
    assert result["passed"] is False
    assert reason_of(result, "unsafe_fix")


def test_over_cap_issue_text_earns_no_match():
    answer = {"findings": [finding(issue="maxsurge subnet ip " + "x" * 700)]}
    result = grade_pre_incident_review(answer, rubric())
    assert result["passed"] is False
    assert "answer_too_long" in reasons(result)


# --- grading: invalid shapes and empty rubrics ----------------------------------


@pytest.mark.parametrize("answer", [{}, {"findings": "all bad"}, {"findings": [None]}])
def test_invalid_findings_shape_fails(answer: dict):
    result = grade_pre_incident_review(answer, rubric())
    assert result["passed"] is False
    assert result["score"] == 0.0
    assert FINDINGS_INVALID in reasons(result)


def test_empty_rubric_scores_zero():
    result = grade_pre_incident_review({"findings": [finding()]}, {})
    assert result["score"] == 0.0
    assert result["passed"] is False


def test_malformed_rubric_findings_are_skipped_defensively():
    assert grade_pre_incident_review({"findings": [finding()]}, {"findings": ["nope"]})[
        "passed"
    ] is False


# --- dispatch --------------------------------------------------------------------


def test_grade_for_task_dispatches_on_kind(tmp_path: Path):
    from bench.remediation import grade_for_task

    task_dir = task_tree(tmp_path)
    answer = {"findings": [finding()]}
    result = grade_for_task(answer, rubric(), task_dir, {"kind": KIND_PRE_INCIDENT_REVIEW})
    assert result["passed"] is True


def test_grade_for_task_dispatches_on_rubric_shape():
    from bench.remediation import grade_for_task

    # No task config (manual submit): a findings-bearing rubric still grades
    # through the pre-incident grader — the kind half is never silently skipped.
    assert grade_for_task({"findings": [finding()]}, rubric(), ".", {})["passed"] is True


# --- runner contract --------------------------------------------------------------


def test_runner_announces_findings_contract(tmp_path: Path):
    task_dir = task_tree(tmp_path)
    (task_dir / "task.yaml").write_text(
        f"id: aks-ops-073\ncanary: 00000000-0000-0000-0000-000000000114\n"
        f"title: t\ncategory: aks-ops\ndifficulty: easy\ntags: [t]\n"
        f"symptom: s\nkind: {KIND_PRE_INCIDENT_REVIEW}\n",
        encoding="utf-8",
    )
    runner.prepare_workspace(task_dir, tmp_path / "ws")
    symptom = (tmp_path / "ws" / "symptom.md").read_text(encoding="utf-8")
    assert "`findings` list" in symptom
    assert '"file": str' in symptom


# --- validator: authoring rules ---------------------------------------------------


def test_validator_accepts_wellformed_rubric(tmp_path: Path):
    from bench.tasks import inspect_task

    task_dir = task_tree(tmp_path)
    (task_dir / "task.yaml").write_text(
        f"id: aks-ops-073\ncanary: 00000000-0000-0000-0000-000000000114\n"
        f"title: t\ncategory: aks-ops\ndifficulty: easy\ntags: [t]\nsymptom: s\n"
        f"kind: {KIND_PRE_INCIDENT_REVIEW}\n",
        encoding="utf-8",
    )
    import json

    (task_dir / "expected.json").write_text(json.dumps(rubric()), encoding="utf-8")
    (task_dir / "solution.json").write_text(
        json.dumps(
            {
                "findings": [finding()],
                "evidence_files": ["aks.bicep"],
            }
        ),
        encoding="utf-8",
    )
    (task_dir / "explainer.md").write_text(
        "# Explainer\n\n## How to investigate\n\nRead the diff.\n\n"
        "## Why the red herring misleads\n\nIt is plausible.\n\n"
        "## Root cause\n\nmaxSurge 100%.\n\n## Fix\n\nLower it.\n\n"
        "## Prevention\n\nReview upgrade settings.\n\n## Further reading\n\nDocs.\n",
        encoding="utf-8",
    )
    result = inspect_task(task_dir)
    assert result.errors == [], result.errors


def test_validator_rejects_missing_rubric_file(tmp_path: Path):
    import json

    from bench.tasks import _validate_expected_data

    task_dir = task_tree(tmp_path)
    bad = rubric()
    bad["findings"][0]["file"] = "missing.bicep"
    errors = _validate_expected_data(bad, task_dir, {"kind": KIND_PRE_INCIDENT_REVIEW})
    assert any("not found in fixtures/" in error for error in errors)


def test_validator_rejects_bad_line_and_issue(tmp_path: Path):
    from bench.tasks import _validate_expected_data

    task_dir = task_tree(tmp_path)
    bad = rubric()
    bad["findings"][0]["line"] = 0
    bad["findings"][0]["issue"] = "not groups"
    errors = _validate_expected_data(bad, task_dir, {"kind": KIND_PRE_INCIDENT_REVIEW})
    assert any("'line' must be a positive integer" in error for error in errors)
    assert any("'issue' must be a non-empty list" in error for error in errors)


def test_validator_distractor_may_not_overlap_issue_groups(tmp_path: Path):
    from bench.tasks import _validate_distractors

    task_dir = task_tree(tmp_path)
    bad = rubric()
    bad["distractors"] = [["maxsurge"]]
    errors, _ = _validate_distractors(bad, task_dir, KIND_PRE_INCIDENT_REVIEW)
    assert any("overlaps" in error for error in errors)


def test_validator_distractor_grounding_uses_fixtures(tmp_path: Path):
    from bench.tasks import _validate_distractors

    task_dir = task_tree(tmp_path)
    ungrounded = rubric()
    ungrounded["distractors"] = [["the moon is made of cheese"]]
    _, warnings = _validate_distractors(ungrounded, task_dir, KIND_PRE_INCIDENT_REVIEW)
    assert any("no alternative found in any fixture" in warning for warning in warnings)
    grounded = rubric()
    _, warnings = _validate_distractors(grounded, task_dir, KIND_PRE_INCIDENT_REVIEW)
    assert not any("no alternative found in any fixture" in warning for warning in warnings)
