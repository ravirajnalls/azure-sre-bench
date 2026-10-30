"""Unit tests for the keyword-group grader (issue #3)."""

import json

import pytest

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


def test_evidence_only_match_for_root_cause():
    # root_cause says nothing; only the evidence citation carries a group keyword.
    answer = {"root_cause": "", "evidence": ["logs show the fic error"], "fix": "x"}
    result = grade(answer, RUBRIC)
    matched = [entry for entry in result["detail"]["root_cause"] if entry["matched"]]
    assert [entry["matched"] for entry in matched] == ["fic"]


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
        "detail": {"root_cause": [], "fix": []},
    }


def test_normalize():
    assert normalize("  Mixed\tCASE\nText  ") == "mixed case text"


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
