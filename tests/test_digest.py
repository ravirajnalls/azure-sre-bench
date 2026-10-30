"""Tests for the weekly results digest (issue #166).

The digest's pure core lives in `bench.digest` — snapshot, diff, render,
parse — and that is everything the weekly workflow depends on. The `gh`
wrapper (`scripts/results_digest.py`) is deliberately untested here: its only
job is shelling out to the GitHub CLI.
"""

import json
from pathlib import Path

from bench import GRADER_VERSION
from bench.digest import (
    build_snapshot,
    diff_state,
    parse_state,
    render_digest,
)


def record(task: str, score: float, passed: bool) -> dict:
    return {
        "task": task,
        "score": score,
        "passed": passed,
        "grader_version": GRADER_VERSION,
        "rubric_sha256": "a" * 64,
    }


def write_run(results_root: Path, run_name: str, records: list[dict]) -> None:
    run_dir = results_root / run_name
    run_dir.mkdir(parents=True, exist_ok=True)
    for rec in records:
        (run_dir / f"{rec['task']}.json").write_text(
            json.dumps(rec), encoding="utf-8"
        )


def test_build_snapshot_aggregates_agents_and_tasks(tmp_path: Path):
    write_run(
        tmp_path,
        "naive",
        [record("monitoring-001", 1.0, True), record("cost-001", 0.5, False)],
    )
    snapshot = build_snapshot(tmp_path)
    assert snapshot["tasks"] == ["cost-001", "monitoring-001"]
    assert snapshot["agents"]["naive"] == {
        "passed": 1,
        "total": 2,
        "mean_score": 0.75,
    }


def test_build_snapshot_missing_root_is_empty(tmp_path: Path):
    assert build_snapshot(tmp_path / "nope") == {"agents": {}, "tasks": []}


def test_diff_state_reports_new_agents_and_tasks():
    old = {"agents": {"a": {"passed": 1, "total": 2, "mean_score": 0.5}}, "tasks": ["t1"]}
    new = {
        "agents": {
            "a": {"passed": 2, "total": 2, "mean_score": 1.0},
            "b": {"passed": 0, "total": 2, "mean_score": 0.0},
        },
        "tasks": ["t1", "t2"],
    }
    diff = diff_state(old, new)
    assert diff["new_agents"] == ["b"]
    assert diff["new_tasks"] == ["t2"]
    assert diff["score_changes"] == [
        {
            "agent": "a",
            "before": {"passed": 1, "total": 2, "mean_score": 0.5},
            "after": {"passed": 2, "total": 2, "mean_score": 1.0},
        }
    ]


def test_diff_state_unchanged_is_quiet():
    state = {"agents": {"a": {"passed": 1, "total": 2, "mean_score": 0.5}}, "tasks": ["t1"]}
    assert diff_state(state, state) == {
        "new_agents": [],
        "new_tasks": [],
        "score_changes": [],
    }


def test_diff_state_first_run_everything_is_new():
    new = {"agents": {"a": {"passed": 1, "total": 2, "mean_score": 0.5}}, "tasks": ["t1"]}
    diff = diff_state(None, new)
    assert diff["new_agents"] == ["a"]
    assert diff["new_tasks"] == ["t1"]
    assert diff["score_changes"] == []


def test_render_parse_roundtrip(tmp_path: Path):
    state = build_snapshot(tmp_path)  # empty state round-trips too
    body = render_digest(state, diff_state(None, state), "2026-10-30")
    assert parse_state(body) == state


def test_parse_state_returns_none_for_plain_body():
    assert parse_state("no snapshot here") is None
    assert parse_state(None) is None
    assert parse_state("<!-- digest-state: not json -->") is None


def test_render_digest_summarizes_changes():
    state = {
        "agents": {
            "naive": {"passed": 2, "total": 4, "mean_score": 0.5},
            "lite|llm": {"passed": 0, "total": 4, "mean_score": 0.0},
        },
        "tasks": ["aks-ops-025"],
    }
    diff = {
        "new_agents": ["lite|llm"],
        "new_tasks": ["aks-ops-025"],
        "score_changes": [
            {
                "agent": "naive",
                "before": {"passed": 0, "total": 4, "mean_score": 0.0},
                "after": {"passed": 2, "total": 4, "mean_score": 0.5},
            }
        ],
    }
    body = render_digest(state, diff, "2026-10-30")
    assert "# Results digest — 2026-10-30" in body
    assert "- lite\\|llm" in body  # pipes escaped so the table survives
    assert "- `aks-ops-025`" in body
    assert "| naive | 0/4 (0.000) | 2/4 (0.500) |" in body


def test_render_digest_empty_state():
    body = render_digest(
        {"agents": {}, "tasks": []},
        {"new_agents": [], "new_tasks": [], "score_changes": []},
        "2026-10-30",
    )
    assert "No results yet" in body
