"""Timeline-track tests: the Kendall-tau ordering grader (issue #146).

The issue's acceptance criteria name four order shapes — perfect, reversed,
partial and invalid — and each is covered below against a stable 6-event
reference order, alongside the dispatcher, validator and runner integration
that the other tracks get.
"""

from __future__ import annotations

import json

import pytest
import yaml

from bench.preincident import KIND_PRE_INCIDENT_REVIEW
from bench.remediation import grade_for_task
from bench.tasks import CANARY_GUID, inspect_task
from bench.timeline import (
    EVENTS_FILENAME,
    FIRST_EVENT_WRONG,
    KIND_TIMELINE,
    ORDER_INVALID,
    ORDER_KEY,
    TAU_BELOW_PASS,
    grade_timeline,
    is_timeline,
    kendall_tau,
    rubric_order,
)

# A stable 6-event reference order (the #209 upgrade-incident shape).
REFERENCE = ["ev-01", "ev-03", "ev-02", "ev-04", "ev-06", "ev-05"]


def _answer(order):
    return {ORDER_KEY: order}


# --- grade_timeline: the four required order shapes -----------------------


def test_perfect_order_passes_with_tau_one():
    result = grade_timeline(_answer(REFERENCE), {ORDER_KEY: REFERENCE})
    assert result["passed"] is True
    assert result["score"] == 1.0


def test_reversed_order_fails():
    result = grade_timeline(_answer(list(reversed(REFERENCE))), {ORDER_KEY: REFERENCE})
    assert result["passed"] is False
    assert result["score"] == -1.0
    reasons = [reason["reason"] for reason in result["detail"]["reasons"]]
    assert TAU_BELOW_PASS in reasons


def test_partial_order_above_threshold_and_first_correct_passes():
    # One adjacent slip at the tail of six events: 1 discordant pair of 15
    # -> tau 13/15 ≈ 0.867, above the 0.8 pass line (issue #146).
    slipped = ["ev-01", "ev-03", "ev-02", "ev-04", "ev-05", "ev-06"]
    assert slipped != REFERENCE
    result = grade_timeline(_answer(slipped), {ORDER_KEY: REFERENCE})
    assert result["passed"] is True
    assert result["score"] == round(13 / 15, 3)


def test_partial_order_below_threshold_fails():
    # The four-event tail scrambled: 12 of 15 pairs discordant, first event
    # still correct -> tau 0.2, well under the pass line.
    swapped = ["ev-01", "ev-03", "ev-05", "ev-06", "ev-04", "ev-02"]
    result = grade_timeline(_answer(swapped), {ORDER_KEY: REFERENCE})
    assert result["passed"] is False
    assert result["score"] == 0.2
    reasons = [reason["reason"] for reason in result["detail"]["reasons"]]
    assert TAU_BELOW_PASS in reasons
    assert FIRST_EVENT_WRONG not in reasons


def test_correct_tau_but_wrong_first_event_fails():
    # Swap the first two events of a 6-event answer: 1 discordant pair,
    # tau 13/15 >= 0.8, but the causal starting point is wrong.
    order = ["ev-03", "ev-01", "ev-02", "ev-04", "ev-06", "ev-05"]
    result = grade_timeline(_answer(order), {ORDER_KEY: REFERENCE})
    assert result["passed"] is False
    assert result["score"] == round(13 / 15, 3)
    reasons = [reason["reason"] for reason in result["detail"]["reasons"]]
    assert FIRST_EVENT_WRONG in reasons
    assert TAU_BELOW_PASS not in reasons


# --- grade_timeline: invalid orders ---------------------------------------


@pytest.mark.parametrize(
    "order",
    [
        [],  # empty
        ["ev-01", "ev-03", "ev-02", "ev-04", "ev-06"],  # missing one id
        ["ev-01", "ev-03", "ev-02", "ev-04", "ev-06", "ev-05", "ev-07"],  # unknown id
        ["ev-01", "ev-03", "ev-02", "ev-04", "ev-06", "ev-05", "ev-01"],  # duplicate
        ["ev-01", 2, "ev-02", "ev-04", "ev-06", "ev-05"],  # non-string id
        "ev-01,ev-03",  # not a list
        None,  # no order key at all
    ],
)
def test_invalid_orders_fail_with_reason(order):
    rubric = {ORDER_KEY: REFERENCE}
    answer = _answer(order) if not isinstance(order, str) else {ORDER_KEY: order}
    result = grade_timeline(answer, rubric)
    assert result["passed"] is False
    assert result["score"] == 0.0
    reasons = [reason["reason"] for reason in result["detail"]["reasons"]]
    assert reasons == [ORDER_INVALID]


def test_malformed_rubric_never_passes():
    rubrics = ({}, {ORDER_KEY: []}, {ORDER_KEY: "ev-01"}, {ORDER_KEY: ["", "x", "x"]})
    for rubric in rubrics:
        result = grade_timeline(_answer(REFERENCE), rubric)
        assert result["passed"] is False
        assert result["score"] == 0.0


# --- kendall_tau and rubric_order units -----------------------------------


def test_kendall_tau_known_values():
    assert kendall_tau(["a", "b", "c"], ["a", "b", "c"]) == 1.0
    assert kendall_tau(["a", "b", "c"], ["c", "b", "a"]) == -1.0
    assert kendall_tau(["a", "b", "c"], ["a", "c", "b"]) == pytest.approx(1 / 3)
    assert kendall_tau(["a"], ["a"]) == 1.0  # no pairs, trivially correct


def test_rubric_order_defensive():
    assert rubric_order({ORDER_KEY: REFERENCE}) == REFERENCE
    assert rubric_order({}) == []
    assert rubric_order({ORDER_KEY: "nope"}) == []
    assert rubric_order({ORDER_KEY: ["a", "a"]}) == []  # duplicates
    assert rubric_order({ORDER_KEY: ["a", ""]}) == []  # blank id


# --- dispatcher integration (grade_for_task) ------------------------------


def _write_timeline_task(tmp_path):
    task_dir = tmp_path / "tasks" / "aks-ops" / "aks-ops-075"
    fixtures = task_dir / "fixtures"
    fixtures.mkdir(parents=True)
    (fixtures / EVENTS_FILENAME).write_text(
        json.dumps(
            {
                "events": [
                    {"id": "ev-01", "timestamp": None, "summary": "first", "source": "portal"},
                    {"id": "ev-03", "timestamp": "2026-10-30T05:14:02Z", "summary": "third", "source": "chat"},
                    {"id": "ev-02", "timestamp": "2026-10-30T05:14:09Z", "summary": "second", "source": "activity-log"},
                ]
            }
        ),
        encoding="utf-8",
    )
    config = {
        "id": "aks-ops-075",
        "canary": CANARY_GUID,
        "kind": KIND_TIMELINE,
        "title": "Order the events",
        "category": "aks-ops",
        "difficulty": "easy",
        "tags": ["demo"],
        "symptom": "s",
    }
    (task_dir / "task.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")
    (task_dir / "expected.json").write_text(
        json.dumps({ORDER_KEY: ["ev-01", "ev-03", "ev-02"]}), encoding="utf-8"
    )
    (task_dir / "solution.json").write_text(
        json.dumps({ORDER_KEY: ["ev-01", "ev-03", "ev-02"]}), encoding="utf-8"
    )
    (task_dir / "explainer.md").write_text(
        "# aks-ops-075\n\n"
        "## How to investigate\n\nTrace the events.\n\n"
        "## Root cause\n\nThe second event actually came first.\n\n"
        "## Why the red herring misleads\n\nTimestamps alone misorder a tie.\n\n"
        "## Fix\n\nCorrect the order.\n\n"
        "## Prevention\n\nShip clocks that agree.\n\n"
        "## Further reading\n\nNone.\n",
        encoding="utf-8",
    )
    return task_dir, config


def test_task_declaring_kind_timeline_dispatches_to_order_grader(tmp_path):
    task_dir, config = _write_timeline_task(tmp_path)
    rubric = json.loads((task_dir / "expected.json").read_text(encoding="utf-8"))
    assert is_timeline(config) is True
    assert grade_for_task(
        {ORDER_KEY: ["ev-01", "ev-03", "ev-02"]}, rubric, task_dir, config
    )["passed"] is True
    # A diagnosis-shaped answer does not satisfy an ordering rubric.
    assert (
        grade_for_task(
            {"root_cause": ["ev-01 first"], "fix": ["reorder"]}, rubric, task_dir, config
        )["passed"]
        is False
    )


def test_rubric_shape_fallback_grades_timeline_without_config(tmp_path):
    task_dir, _ = _write_timeline_task(tmp_path)
    rubric = json.loads((task_dir / "expected.json").read_text(encoding="utf-8"))
    assert grade_for_task({ORDER_KEY: ["ev-01", "ev-03", "ev-02"]}, rubric, task_dir, None)[
        "passed"
    ] is True


# --- validator integration (bench validate) -------------------------------


def test_validator_accepts_well_formed_timeline_task(tmp_path):
    task_dir, _ = _write_timeline_task(tmp_path)
    assert inspect_task(task_dir).errors == []


def test_validator_rejects_timeline_task_without_events_fixture(tmp_path):
    task_dir, _ = _write_timeline_task(tmp_path)
    (task_dir / "fixtures" / EVENTS_FILENAME).unlink()
    assert any(EVENTS_FILENAME in error for error in inspect_task(task_dir).errors)


def test_validator_rejects_reference_ids_missing_from_events(tmp_path):
    task_dir, _ = _write_timeline_task(tmp_path)
    events = json.loads((task_dir / "fixtures" / EVENTS_FILENAME).read_text())
    events["events"] = events["events"][:2]  # drop ev-02, which the rubric references
    (task_dir / "fixtures" / EVENTS_FILENAME).write_text(json.dumps(events), encoding="utf-8")
    assert any("ev-02" in error for error in inspect_task(task_dir).errors)


def test_validator_rejects_timeline_rubric_without_order(tmp_path):
    task_dir, _ = _write_timeline_task(tmp_path)
    (task_dir / "expected.json").write_text(json.dumps({"order": []}), encoding="utf-8")
    assert any(ORDER_KEY in error for error in inspect_task(task_dir).errors)


# --- runner integration (symptom.md carries the answer contract) ----------


def test_runner_instructions_explain_the_order_contract(tmp_path):
    task_dir, _ = _write_timeline_task(tmp_path)
    from bench.run import prepare_workspace

    workspace = tmp_path / "ws"
    prepare_workspace(task_dir, workspace)
    symptom = (workspace / "symptom.md").read_text(encoding="utf-8")
    assert "timeline" in symptom
    assert "`order`" in symptom


def test_diagnosis_kind_is_untouched():
    assert is_timeline({"kind": "diagnosis"}) is False
    assert is_timeline({"kind": KIND_PRE_INCIDENT_REVIEW}) is False
    assert is_timeline(None) is False
