"""Tests for the failure taxonomy (issue #163)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bench.cli import main
from bench.failures import (
    CATEGORIES,
    classify_failure,
    failure_counts,
    failure_examples,
    failures_report,
)
from bench.leaderboard import collect_records

REPO_ROOT = Path(__file__).resolve().parent.parent


def failed(**overrides: object) -> dict:
    """A minimal failed record in the shape `bench run` writes."""
    record: dict = {
        "task": "monitoring-901",
        "passed": False,
        "timed_out": False,
        "answer": {"root_cause": "RBAC role binding", "fix": "grant the role"},
        "detail": {
            "root_cause": [{"group": ["RBAC role binding"], "matched": None}],
            "reasons": [],
            "safety_violations": [],
        },
    }
    record.update(overrides)
    return record


def test_passing_records_are_never_classified():
    assert classify_failure(failed(passed=True)) is None


def test_every_category_is_representable():
    # One record per category, built from the real grader signals.
    cases = [
        failed(timed_out=True),  # timeout
        failed(answer={}),  # no_answer
        failed(
            detail={
                "root_cause": [{"group": ["x"], "matched": "x"}],
                "reasons": [{"reason": "named_distractor", "field": "root_cause"}],
            }
        ),
        failed(
            answer={"root_cause": "x", "fix": "kubectl delete ns kube-system"},
            detail={
                "root_cause": [{"group": ["x"], "matched": "x"}],
                "reasons": [{"reason": "unsafe_fix", "rule": "r"}],
            },
        ),
        failed(
            detail={
                "root_cause": [{"group": ["x"], "matched": None}],
                "reasons": [{"reason": "answer_too_long", "field": "root_cause"}],
            }
        ),
        failed(
            detail={
                "root_cause": [{"group": ["x"], "matched": "x"}],
                "reasons": [],
            }
        ),  # right_cause_wrong_fix
        failed(),  # wrong_layer fallback
    ]
    got = {classify_failure(record) for record in cases}
    assert got == set(CATEGORIES)


def test_precedence_when_signals_overlap():
    # Timeout beats everything: there is no graded attempt to analyze.
    assert classify_failure(failed(timed_out=True, answer={})) == "timeout"
    # A named distractor outranks an unsafe fix — the diagnosis is the
    # more fundamental error.
    distractor_and_unsafe = failed(
        detail={
            "root_cause": [{"group": ["x"], "matched": "x"}],
            "reasons": [
                {"reason": "named_distractor"},
                {"reason": "unsafe_fix", "rule": "r"},
            ],
        }
    )
    assert classify_failure(distractor_and_unsafe) == "named_distractor"
    # Too-long outranks the diagnosis match: over-cap answers earn nothing,
    # so their partial matches say nothing.
    too_long_and_matched = failed(
        detail={
            "root_cause": [{"group": ["x"], "matched": "x"}],
            "reasons": [
                {"reason": "answer_too_long", "field": "root_cause"},
            ],
        }
    )
    assert classify_failure(too_long_and_matched) == "too_long"


def test_safety_violations_without_a_reason_still_classify_unsafe():
    record = failed(
        detail={
            "root_cause": [{"group": ["x"], "matched": "x"}],
            "reasons": [],
            "safety_violations": [{"rule": "no-delete-namespace"}],
        }
    )
    assert classify_failure(record) == "unsafe_fix"


def test_malformed_records_fall_back_to_wrong_layer():
    assert classify_failure({"passed": False}) == "wrong_layer"
    assert classify_failure("not a dict") is None


def test_failure_counts_zero_fills_canonical_order():
    records = [failed(passed=True), failed(), failed(timed_out=True)]
    counts = failure_counts(records)
    assert list(counts) == list(CATEGORIES)
    assert counts["wrong_layer"] == 1
    assert counts["timeout"] == 1
    assert sum(counts.values()) == 2


def test_failure_examples_name_the_first_task_per_category():
    records = [failed(task="first-001"), failed(task="second-001", timed_out=True)]
    assert failure_examples(records) == {
        "wrong_layer": "first-001",
        "timeout": "second-001",
    }


def test_failures_report_renders_header_table_and_shares():
    records = [failed(), failed(), failed(timed_out=True, task="other-001")]
    report = failures_report("naive", records)
    assert report.splitlines()[0] == "3 failed results across 3 for naive:"
    assert "| wrong_layer | 2 | 66.7% | monitoring-901 |" in report
    assert "| timeout | 1 | 33.3% | other-001 |" in report
    # Every category appears as a row, zeros included.
    for category in CATEGORIES:
        assert f"| {category} |" in report


def test_failures_report_with_no_failures_shows_zero_counts():
    report = failures_report("clean", [failed(passed=True)])
    assert report.splitlines()[0] == "0 failed results across 1 for clean:"
    assert "| wrong_layer | 0 | — | — |" in report


def test_cli_failures_prints_the_table(tmp_path, capsys):
    run_dir = tmp_path / "naive"
    run_dir.mkdir()
    (run_dir / "monitoring-901.json").write_text(
        json.dumps(failed()), encoding="utf-8"
    )
    exit_code = main(["failures", "naive", "--results-root", str(tmp_path)])
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "1 failed result across 1 for naive:" in out
    assert "| wrong_layer | 1 | 100.0% | monitoring-901 |" in out


def test_cli_failures_names_unknown_runs(tmp_path, capsys):
    assert main(["failures", "ghost", "--results-root", str(tmp_path)]) == 2
    assert "no run named 'ghost'" in capsys.readouterr().err


def test_cli_failures_reads_real_run_layout(tmp_path, capsys):
    # Round-trip through collect_records so the command and the storage
    # contract stay honest with each other.
    run_dir = tmp_path / "naive"
    run_dir.mkdir()
    (run_dir / "monitoring-901.0.json").write_text(
        json.dumps(failed(timed_out=True)), encoding="utf-8"
    )
    runs = collect_records(tmp_path)
    assert runs["naive"][0]["timed_out"] is True
    report = failures_report("naive", runs["naive"])
    assert "| timeout | 1 | 100.0% | monitoring-901 |" in report
