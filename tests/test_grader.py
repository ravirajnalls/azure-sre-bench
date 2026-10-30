"""Unit tests for the keyword-group grader (issue #3)."""

import json

import pytest

from bench import grader
from bench.grader import grade, normalize

RUBRIC = {
    "root_cause": [
        ["federated credential", "federated identity", "fic"],
        ["subject"],
        ["namespace", "default", "orders"],
    ],
    "fix": [["system:serviceaccount:orders:orders-api"], ["update the federated"]],
}


def full_answer() -> dict:
    return {
        "root_cause": (
            "Stale Federated Identity Credential subject pinned to the "
            "default namespace."
        ),
        "evidence": [
            "az-identity-federated-credential-list.json: subject "
            "system:serviceaccount:default:orders-api",
            "kubectl-logs-orders-api.txt: AADSTS70021",
        ],
        "fix": "Update the federated credential subject to "
        "system:serviceaccount:orders:orders-api.",
    }


def test_full_match_passes():
    result = grade(full_answer(), RUBRIC)
    assert result["passed"] is True
    assert result["score"] == 1.0
    assert all(entry["matched"] is not None for field in result["detail"].values()
               for entry in field)


def test_partial_match_scores_matched_fraction():
    answer = full_answer()
    answer["fix"] = "Not sure what to change."
    result = grade(answer, RUBRIC)
    assert result["passed"] is False
    assert result["score"] == 0.6  # 3 of 5 groups matched
    assert result["detail"]["fix"] == [
        {"group": ["system:serviceaccount:orders:orders-api"], "matched": None},
        {"group": ["update the federated"], "matched": None},
    ]


def test_synonym_match():
    answer = full_answer()
    # No alternative in group 1 verbatim; the synonym "fic" matches instead.
    answer["root_cause"] = "The fic for orders-api points at the wrong namespace."
    answer["evidence"] = []
    result = grade(answer, RUBRIC)
    assert result["detail"]["root_cause"][0]["matched"] == "fic"
    assert result["passed"] is False
    assert result["score"] == 0.8  # root_cause groups 1 (fic) and 3 (orders); both fix groups


def test_case_and_whitespace_insensitivity():
    answer = full_answer()
    answer["root_cause"] = "  STALE   federated\nCREDENTIAL\tsubject   "
    result = grade(answer, RUBRIC)
    assert result["detail"]["root_cause"][0]["matched"] is not None
    assert result["detail"]["root_cause"][1]["matched"] == "subject"


def test_empty_answer_scores_zero_without_crashing():
    result = grade({}, RUBRIC)
    assert result["score"] == 0.0
    assert result["passed"] is False
    assert len(result["detail"]["root_cause"]) == 3
    assert all(entry["matched"] is None for field in result["detail"].values()
               for entry in field)


def test_missing_and_nonstring_fields_count_as_empty():
    answer = {"root_cause": None, "evidence": "not a list", "fix": 42}
    result = grade(answer, RUBRIC)  # must not raise
    assert result["score"] == 0.0
    assert result["passed"] is False


def test_evidence_alone_never_satisfies_root_cause():
    # Issue #93: evidence no longer participates in root-cause matching — the
    # diagnosis must be stated in the field itself, not merely cited.
    answer = {"root_cause": "", "evidence": ["logs show the fic error"], "fix": "x"}
    result = grade(answer, RUBRIC)
    assert all(
        entry["matched"] is None for entry in result["detail"]["root_cause"]
    )
    assert result["passed"] is False


def test_fix_is_never_matched_from_evidence():
    answer = full_answer()
    answer["fix"] = "Restart the pod."
    result = grade(answer, RUBRIC)
    assert all(
        entry["matched"] is None for entry in result["detail"]["fix"]
    ), "evidence must not satisfy fix groups"


def test_score_rounds_to_three_decimals():
    rubric = {"root_cause": [["alpha"], ["beta"], ["gamma"]], "fix": []}
    result = grade({"root_cause": "alpha", "fix": ""}, rubric)
    assert result["score"] == 0.333  # 1 of 3, rounded to 3 dp


def test_empty_rubric_never_passes():
    assert grade({"root_cause": "x", "fix": "y"}, {}) == {
        "score": 0.0,
        "passed": False,
        "detail": {
            "root_cause": [],
            "fix": [],
            "reasons": [],
            "safety_violations": [],
        },
    }


# --- Volume caps and failure reasons (issue #93) -----------------------------


def saturated_root_cause() -> str:
    """Over the 600-char cap while containing every root_cause keyword."""
    return ("federated identity credential subject default namespace " * 20).strip()


def test_root_cause_over_cap_fails_with_answer_too_long():
    answer = full_answer()
    answer["root_cause"] = saturated_root_cause()
    assert len(answer["root_cause"]) > grader.DEFAULT_MAX_CHARS

    result = grade(answer, RUBRIC)
    assert result["passed"] is False
    assert result["detail"]["reasons"] == [
        {
            "reason": "answer_too_long",
            "field": "root_cause",
            "length": len(answer["root_cause"]),
            "limit": grader.DEFAULT_MAX_CHARS,
        }
    ]
    # An over-cap field earns no credit, even saturated with the keywords.
    assert all(entry["matched"] is None for entry in result["detail"]["root_cause"])
    # The legal field is still graded: 2 of 5 groups match.
    assert result["score"] == 0.4


def test_fix_over_cap_fails_with_answer_too_long():
    answer = full_answer()
    answer["fix"] = "restart everything and monitor closely " * 20
    result = grade(answer, RUBRIC)
    assert result["passed"] is False
    assert result["detail"]["reasons"][0]["field"] == "fix"
    assert result["detail"]["reasons"][0]["reason"] == "answer_too_long"


def test_field_exactly_at_the_cap_is_legal():
    answer = full_answer()
    prefix = "stale federated identity credential subject pinned to the default namespace"
    answer["root_cause"] = (
        prefix + " " + "x" * (grader.DEFAULT_MAX_CHARS - len(prefix) - 1)
    )
    assert len(answer["root_cause"]) == grader.DEFAULT_MAX_CHARS

    result = grade(answer, RUBRIC)
    assert result["passed"] is True
    assert result["detail"]["reasons"] == []


def test_max_chars_override_raises_the_cap():
    rubric = {**RUBRIC, "max_chars": 800}
    answer = full_answer()
    answer["root_cause"] = "federated identity subject namespace " + "x" * 700
    assert grader.DEFAULT_MAX_CHARS < len(answer["root_cause"]) <= 800

    result = grade(answer, rubric)
    assert result["passed"] is True
    assert result["detail"]["reasons"] == []


def test_max_chars_override_limit_recorded_in_reason():
    rubric = {**RUBRIC, "max_chars": 700}
    answer = full_answer()
    answer["root_cause"] = saturated_root_cause()

    result = grade(answer, rubric)
    assert result["detail"]["reasons"][0]["limit"] == 700


def test_invalid_max_chars_falls_back_to_default():
    for bad in (0, -5, "800", None, True, 12.5):
        assert grader.max_chars_for({"max_chars": bad}) == grader.DEFAULT_MAX_CHARS


def test_over_cap_field_fails_task_even_when_all_groups_match():
    # The issue's rule: over 600 characters the task fails — a matching field
    # cannot rescue an answer that violates the cap on the other field.
    rubric = {"fix": [["restart"]]}
    answer = {
        "root_cause": "x" * 601,
        "evidence": [],
        "fix": "restart the deployment",
    }
    result = grade(answer, rubric)
    assert result["score"] == 1.0
    assert result["passed"] is False
    assert result["detail"]["reasons"][0]["reason"] == "answer_too_long"


def test_cap_evidence_truncates_count_and_item_length():
    answer = {"evidence": [f"item-{i}: " + "y" * 400 for i in range(12)]}
    capped = grader.cap_evidence(answer)
    assert len(capped["evidence"]) == grader.MAX_EVIDENCE_ITEMS
    assert all(
        len(item) == grader.MAX_EVIDENCE_ITEM_CHARS for item in capped["evidence"]
    )
    assert capped["evidence"][0].startswith("item-0:")
    assert capped["evidence"][7].startswith("item-7:")


def test_cap_evidence_leaves_legal_and_absent_evidence_unchanged():
    legal = {"root_cause": "r", "evidence": ["a", "b"], "fix": "f"}
    assert grader.cap_evidence(legal) == legal
    assert grader.cap_evidence({"root_cause": "r"}) == {"root_cause": "r"}
    assert grader.cap_evidence({"evidence": "not a list"}) == {
        "evidence": "not a list"
    }
    assert grader.cap_evidence({}) == {}


def test_normalize():
    assert normalize("  Mixed\tCASE\nText  ") == "mixed case text"


# --- Distractor groups (issue #94) -------------------------------------------

DISTRACTOR_RUBRIC = {
    **RUBRIC,
    "distractors": [["missing role assignment", "rbac misconfiguration"]],
}


def test_distractor_match_fails_task_with_named_reason():
    answer = full_answer()
    answer["root_cause"] = (
        "A missing role assignment for the federated credential subject "
        "in the namespace."
    )
    result = grade(answer, DISTRACTOR_RUBRIC)
    assert result["passed"] is False
    # Distractors cost no score: every rubric group still matched.
    assert result["score"] == 1.0
    assert result["detail"]["reasons"] == [
        {
            "reason": "named_distractor",
            "field": "root_cause",
            "matched": "missing role assignment",
            "group": 0,
        }
    ]


def test_answer_that_never_names_the_distractor_passes():
    result = grade(full_answer(), DISTRACTOR_RUBRIC)
    assert result["passed"] is True
    assert result["detail"]["reasons"] == []


def test_fix_never_triggers_a_distractor():
    # The issue scopes distractor matching to the root_cause text: a fix may
    # legitimately mention restoring the distractor's component.
    answer = full_answer()
    answer["fix"] += " Also create the missing role assignment for the service account."
    result = grade(answer, DISTRACTOR_RUBRIC)
    assert result["passed"] is True
    assert result["detail"]["reasons"] == []


def test_evidence_never_triggers_a_distractor():
    answer = full_answer()
    answer["evidence"] = ["az-role-assignment-list.json: no missing role assignment found"]
    result = grade(answer, DISTRACTOR_RUBRIC)
    assert result["passed"] is True


def test_each_matched_distractor_group_reports_one_reason():
    rubric = {
        **RUBRIC,
        "distractors": [["rbac misconfiguration"], ["dns resolution failure"]],
    }
    answer = full_answer()
    answer["root_cause"] = (
        "A dns resolution failure and an rbac misconfiguration hit the "
        "federated credential subject in the namespace."
    )
    result = grade(answer, rubric)
    assert result["detail"]["reasons"] == [
        {"reason": "named_distractor", "field": "root_cause", "matched": "rbac misconfiguration", "group": 0},
        {"reason": "named_distractor", "field": "root_cause", "matched": "dns resolution failure", "group": 1},
    ]


def test_malformed_distractors_are_ignored_like_absent():
    clean = grade(full_answer(), RUBRIC)
    for bad in ("nope", 42, [], ["flat"], [["ok", 5]], [[""]], [[None]]):
        result = grade(full_answer(), {**RUBRIC, "distractors": bad})
        assert result["passed"] is True
        assert result["detail"]["reasons"] == []
    assert grader.distractor_groups({"distractors": bad}) == []
    assert clean == grade(full_answer(), RUBRIC)


def test_distractor_reasons_coexist_with_answer_too_long():
    answer = full_answer()
    answer["root_cause"] = "missing role assignment " * 80  # over the 600-char cap
    result = grade(answer, DISTRACTOR_RUBRIC)
    reason_kinds = [reason["reason"] for reason in result["detail"]["reasons"]]
    assert reason_kinds == ["answer_too_long", "named_distractor"]
    assert result["passed"] is False


# --- CLI: `bench grade --task <id> --answer answer.json` ---


@pytest.fixture
def task_tree(tmp_path, monkeypatch):
    """A minimal task directory with a rubric, plus an answer file beside it."""
    task_dir = tmp_path / "tasks" / "demo" / "demo-001"
    task_dir.mkdir(parents=True)
    (task_dir / "expected.json").write_text(json.dumps(RUBRIC))
    answer_path = tmp_path / "answer.json"
    answer_path.write_text(json.dumps(full_answer()))
    monkeypatch.chdir(tmp_path)
    return answer_path


def test_cli_grade_prints_result_json(task_tree, capsys):
    from bench.cli import main

    assert main(["grade", "--task", "demo-001", "--answer", str(task_tree)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["passed"] is True
    assert result["score"] == 1.0


def test_cli_grade_unknown_task_exits_nonzero(tmp_path, monkeypatch, capsys):
    from bench.cli import main

    monkeypatch.chdir(tmp_path)
    (tmp_path / "tasks").mkdir()
    assert main(["grade", "--task", "nope-999", "--answer", "answer.json"]) == 2
    assert "nope-999" in capsys.readouterr().err


def test_cli_grade_malformed_answer_exits_nonzero(task_tree, tmp_path, capsys):
    from bench.cli import main

    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    assert main(["grade", "--task", "demo-001", "--answer", str(bad)]) == 2
    assert "error:" in capsys.readouterr().err
