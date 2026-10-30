"""Postmortem-track tests: sections, word window, facts, forbidden claims (issue #148).

The issue's acceptance criteria — keyword pre-checks (required sections,
300-1500 words), a per-task rubric of required facts and forbidden claims,
a judge prompt, mocked tests — are each covered below, including the
dispatcher, validator and runner integration the other tracks get.
"""

from __future__ import annotations

import json

import yaml

from bench.preincident import KIND_PRE_INCIDENT_REVIEW
from bench.remediation import grade_for_task
from bench.tasks import CANARY_GUID, inspect_task
from bench.postmortem import (
    FACTS_KEY,
    FORBIDDEN_CLAIM,
    FORBIDDEN_KEY,
    KIND_POSTMORTEM,
    MISSING_FACT,
    MISSING_SECTION,
    POSTMORTEM_INVALID,
    POSTMORTEM_KEY,
    POSTMORTEM_TOO_LONG,
    POSTMORTEM_TOO_SHORT,
    WORDS_MAX,
    WORDS_MIN,
    grade_postmortem,
    is_postmortem,
)

# A 4-fact rubric shaped like aks-identity-077's (issue #211).
RUBRIC = {
    FACTS_KEY: [
        [["47"]],
        [["subject"], ["default", "old namespace"]],
        [["system:serviceaccount:orders:orders-api", "orders:orders-api"], ["federated", "credential"]],
        [["runbook", "checklist", "iac"]],
    ],
    FORBIDDEN_KEY: [
        ["key vault outage", "keyvault outage", "vault misconfig"],
        ["rbac misconfig", "rbac was wrong", "wrong role assignment"],
        ["alex", "priya", "human error", "at fault"],
    ],
}

SECTIONS = (
    "# Summary\n",
    "## Impact\n",
    "## Timeline\n",
    "## Root Cause\n",
    "## Contributing Factors\n",
    "## Action Items\n",
)


def _document(blame=None, words=400) -> str:
    """A minimal-valid postmortem; parameters break it on purpose."""
    body = " ".join(
        [
            "orders-api moved namespaces and its federated credential subject stayed",
            "on the old namespace, so token exchange failed and Key Vault reads",
            "returned 403. Impact lasted 47 minutes with HTTP 500 on order endpoints.",
            "The fix added a federated credential for",
            "system:serviceaccount:orders:orders-api and restarted the rollout.",
            "Follow-up: the runbook gains a federated-credential step for namespace",
            "moves and the credential is managed as code.",
        ]
    )
    filler = "This sentence is filler for the word-count window. " * 40
    blame_sentence = {
        "kv": "The outage was a key vault outage, nothing else.\n",
        "rbac": "An rbac misconfig broke the data plane.\n",
        "person": "alex pushed the change and is at fault; human error.\n",
    }[blame] if blame else ""
    doc = "".join(SECTIONS) + body + "\n" + filler + blame_sentence
    doc_words = len(doc.split())
    if words > doc_words:
        doc += "More padding. " * (words - doc_words)
    return doc


def _answer(doc):
    return {POSTMORTEM_KEY: doc}


# --- the valid shape passes -----------------------------------------------


def test_well_formed_postmortem_passes():
    result = grade_postmortem(_answer(_document()), RUBRIC)
    assert result["passed"] is True
    assert result["score"] == 1.0
    assert result["detail"]["facts_matched"] == 4


def test_fact_conjunction_semantics():
    # Fact 2 needs BOTH a subject mention and an old-namespace mention.
    doc = _document().replace("stayed on the old namespace", "stayed put")
    result = grade_postmortem(_answer(doc), RUBRIC)
    assert result["passed"] is False
    missing = [reason for reason in result["detail"]["reasons"] if reason["reason"] == MISSING_FACT]
    assert len(missing) == 1 and missing[0]["fact"] == 1
    assert result["score"] == 0.75


# --- keyword pre-checks: sections and words -------------------------------


def test_missing_section_fails_with_section_name():
    doc = _document().replace("## Contributing Factors\n", "")
    result = grade_postmortem(_answer(doc), RUBRIC)
    assert result["passed"] is False
    assert any(
        reason["reason"] == MISSING_SECTION and reason["section"] == "contributing factors"
        for reason in result["detail"]["reasons"]
    )


def test_document_too_short_fails():
    doc = " ".join(SECTIONS) + " " + "word " * (WORDS_MIN - 20)
    result = grade_postmortem(_answer(doc), RUBRIC)
    assert result["passed"] is False
    assert any(reason["reason"] == POSTMORTEM_TOO_SHORT for reason in result["detail"]["reasons"])


def test_document_too_long_fails():
    result = grade_postmortem(_answer(_document(words=WORDS_MAX + 50)), RUBRIC)
    assert result["passed"] is False
    assert any(reason["reason"] == POSTMORTEM_TOO_LONG for reason in result["detail"]["reasons"])


# --- required facts and forbidden claims ----------------------------------


def test_missing_fact_lowers_score_but_not_below_precheck():
    # Strike both fact-3 keyword groups from the document (no "federated" or
    # "credential" token may remain anywhere) while facts 1, 2 and 4 still
    # match.
    doc = (
        _document()
        .replace("federated credential", "cloud identity")
        .replace("federated-credential", "cloud identity")
        .replace("credential", "identity")
    )
    result = grade_postmortem(_answer(doc), RUBRIC)
    assert result["passed"] is False
    assert result["score"] == 0.75


def test_forbidden_claim_fails_without_changing_score():
    for blame in ("kv", "rbac", "person"):
        result = grade_postmortem(_answer(_document(blame=blame)), RUBRIC)
        assert result["passed"] is False, blame
        assert result["score"] == 1.0, blame
        assert any(reason["reason"] == FORBIDDEN_CLAIM for reason in result["detail"]["reasons"]), blame


# --- invalid answers and rubrics ------------------------------------------


def test_invalid_answers_fail_cleanly():
    for answer in ({}, {POSTMORTEM_KEY: None}, {POSTMORTEM_KEY: "   "}, {POSTMORTEM_KEY: 42}):
        result = grade_postmortem(answer, RUBRIC)
        assert result["passed"] is False
        assert result["score"] == 0.0
        assert result["detail"]["reasons"][0]["reason"] == POSTMORTEM_INVALID


def test_malformed_rubric_never_passes():
    for rubric in ({}, {FACTS_KEY: []}, {FACTS_KEY: "nope"}, {FACTS_KEY: [[["ok"]], "broken"]}):
        result = grade_postmortem(_answer(_document()), rubric)
        assert result["passed"] is False
        assert result["score"] == 0.0
        assert result["detail"]["reasons"][0]["reason"] == POSTMORTEM_INVALID


# --- judge hook (LLM-judge mode, issue #100) ------------------------------


def test_judge_decides_passed_when_prechecks_pass():
    result = grade_postmortem(
        _answer(_document()), RUBRIC, judge=lambda doc, rubric: {"passed": False, "reasons": [{"reason": "judge_no"}]}
    )
    assert result["passed"] is False
    assert result["detail"]["reasons"][-1]["reason"] == "judge_no"


def test_judge_cannot_rescue_shape_failures():
    # Strike every "impact" occurrence (heading and body) — the section
    # pre-check is substring-based, so the word must be gone entirely.
    doc = _document().replace("## Impact\n", "").replace("Impact lasted", "Downtime lasted")
    result = grade_postmortem(_answer(doc), RUBRIC, judge=lambda doc, rubric: {"passed": True})
    assert result["passed"] is False  # missing_section beats the judge


def test_judge_not_called_when_prechecks_fail():
    calls = []

    def judge(doc, rubric):
        calls.append(1)
        return {"passed": True}

    doc = _document().replace("## Timeline\n", "")
    result = grade_postmortem(_answer(doc), RUBRIC, judge=judge)
    assert calls == []
    assert result["passed"] is False


# --- dispatcher integration (grade_for_task) ------------------------------


def _write_postmortem_task(tmp_path):
    task_dir = tmp_path / "tasks" / "aks-identity" / "aks-identity-077"
    fixtures = task_dir / "fixtures"
    fixtures.mkdir(parents=True)
    (fixtures / "resolution.log").write_text("resolved; 47 minutes of impact\n", encoding="utf-8")
    (fixtures / "on-call-chat.txt").write_text("on-call: credential added, traffic green\n", encoding="utf-8")
    config = {
        "id": "aks-identity-077",
        "canary": CANARY_GUID,
        "kind": KIND_POSTMORTEM,
        "title": "Write the postmortem",
        "category": "aks-identity",
        "difficulty": "medium",
        "tags": ["demo"],
        "symptom": "s",
    }
    (task_dir / "task.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")
    (task_dir / "expected.json").write_text(json.dumps(RUBRIC), encoding="utf-8")
    (task_dir / "solution.json").write_text(
        json.dumps({POSTMORTEM_KEY: _document()}), encoding="utf-8"
    )
    (task_dir / "explainer.md").write_text(
        "# aks-identity-077\n\n"
        "## How to investigate\n\nRead the resolution log.\n\n"
        "## Root cause\n\nThe credential subject kept the old namespace.\n\n"
        "## Why the red herring misleads\n\nThe 403 looks like a vault outage.\n\n"
        "## Fix\n\nAdd the credential for the orders namespace.\n\n"
        "## Prevention\n\nManage credentials as code.\n\n"
        "## Further reading\n\nNone.\n",
        encoding="utf-8",
    )
    return task_dir, config


def test_task_declaring_kind_postmortem_dispatches_to_document_grader(tmp_path):
    task_dir, config = _write_postmortem_task(tmp_path)
    rubric = json.loads((task_dir / "expected.json").read_text(encoding="utf-8"))
    assert is_postmortem(config) is True
    assert grade_for_task(_answer(_document()), rubric, task_dir, config)["passed"] is True
    # A diagnosis-shaped answer does not satisfy a postmortem rubric.
    assert (
        grade_for_task(
            {"root_cause": ["47 minutes"], "fix": ["add credential"]},
            rubric,
            task_dir,
            config,
        )["passed"]
        is False
    )


# --- validator integration (bench validate) -------------------------------


def test_validator_accepts_well_formed_postmortem_task(tmp_path):
    task_dir, _ = _write_postmortem_task(tmp_path)
    assert inspect_task(task_dir).errors == []


def test_validator_rejects_postmortem_rubric_without_facts(tmp_path):
    task_dir, _ = _write_postmortem_task(tmp_path)
    (task_dir / "expected.json").write_text(json.dumps({FACTS_KEY: []}), encoding="utf-8")
    assert any(FACTS_KEY in error for error in inspect_task(task_dir).errors)


def test_validator_rejects_malformed_forbidden_claims(tmp_path):
    task_dir, _ = _write_postmortem_task(tmp_path)
    rubric = dict(RUBRIC)
    rubric[FORBIDDEN_KEY] = ["not a group list"]
    (task_dir / "expected.json").write_text(json.dumps(rubric), encoding="utf-8")
    assert any(FORBIDDEN_KEY in error for error in inspect_task(task_dir).errors)


# --- runner integration (symptom.md carries the answer contract) ----------


def test_runner_instructions_explain_the_document_contract(tmp_path):
    task_dir, _ = _write_postmortem_task(tmp_path)
    from bench.run import prepare_workspace

    workspace = tmp_path / "ws"
    prepare_workspace(task_dir, workspace)
    symptom = (workspace / "symptom.md").read_text(encoding="utf-8")
    assert "postmortem" in symptom
    assert '"postmortem"' in symptom
    assert "action items" in symptom


def test_diagnosis_kind_is_untouched():
    assert is_postmortem({"kind": "diagnosis"}) is False
    assert is_postmortem({"kind": KIND_PRE_INCIDENT_REVIEW}) is False
    assert is_postmortem(None) is False
