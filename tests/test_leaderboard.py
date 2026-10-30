"""Tests for the leaderboard command and README updater (issue #41)."""

import json
from pathlib import Path

import pytest

from bench import GRADER_VERSION
from bench.cli import main
from bench.leaderboard import (
    END_MARKER,
    START_MARKER,
    STATUS_CURRENT,
    STATUS_LEGACY,
    STATUS_STALE,
    aggregate_run,
    classify_record,
    collect_records,
    leaderboard_markdown,
    markdown_table,
    median,
    rank_rows,
    update_readme,
)


def record(task: str, score: float, passed: bool) -> dict:
    return {"task": task, "score": score, "passed": passed}


def seed_run(results_root: Path, name: str, records: list[dict]) -> None:
    run_dir = results_root / name
    run_dir.mkdir(parents=True)
    for index, rec in enumerate(records):
        (run_dir / f"task-{index:03d}.json").write_text(
            json.dumps(rec), encoding="utf-8"
        )


def test_rank_rows_sorts_by_passed_then_mean_score():
    # Ranking math (issue #95) counts current-version records only, so the
    # comparable runs here carry the current schema; a run with no records
    # renders legacy and ranks last.
    runs = {
        "slow-expert": [versioned("t1"), versioned("t2", passed=False, score=0.5)],
        "sweep": [versioned("t1"), versioned("t2")],
        "sharp": [versioned("t1"), versioned("t2", passed=False, score=0.2)],
        "empty": [],
    }
    order = [row["name"] for row in rank_rows(runs)]
    # sweep: 2 passed; slow-expert and sharp: 1 passed each, broken on mean
    # (0.75 vs 0.6); a run with no records ranks last.
    assert order == ["sweep", "slow-expert", "sharp", "empty"]


def test_rank_rows_explicit_order():
    rows = rank_rows(
        {
            "a": [versioned("t1")],
            "b": [versioned("t1", passed=True, score=0.5), versioned("t2", passed=True, score=0.5)],
            "c": [versioned("t1", passed=False, score=0.0), versioned("t2", passed=False, score=0.0)],
        }
    )
    assert [row["name"] for row in rows] == ["b", "a", "c"]
    assert rows[0]["passed"] == 2
    assert rows[0]["mean_score"] == pytest.approx(0.5)


def test_markdown_table_empty_root_is_headers_only():
    table = leaderboard_markdown(Path("no-such-results-dir"))
    assert table.splitlines()[0].startswith("| Rank |")
    assert len(table.splitlines()) == 2


def test_collect_records_skips_malformed_and_non_dirs(tmp_path):
    seed_run(tmp_path, "good", [record("t1", 1.0, True)])
    (tmp_path / "bad.json").write_text("not json", encoding="utf-8")
    (tmp_path / "notes.txt").write_text("ignore", encoding="utf-8")
    broken = tmp_path / "broken"
    broken.mkdir()
    (broken / "x.json").write_text("{oops", encoding="utf-8")

    runs = collect_records(tmp_path)
    assert list(runs) == ["broken", "good"]
    assert runs["good"][0]["task"] == "t1"
    assert runs["broken"] == []


def test_collect_records_skips_adversarial_runs(tmp_path):
    """Adversarial probe runs (issue #92) are gate diagnostics, never entries."""
    seed_run(tmp_path, "adversarial-keyword-soup", [record("t1", 1.0, True)])
    seed_run(tmp_path, "naive", [record("t1", 0.0, False)])

    runs = collect_records(tmp_path)
    assert list(runs) == ["naive"]


def test_cli_leaderboard_prints_table(tmp_path, capsys):
    seed_run(tmp_path, "naive", [record("t1", 1.0, True)])
    assert main(["leaderboard", "--results-root", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "| 1 | naive | runner | legacy | 0/0" in out


def test_update_readme_splices_only_the_block(tmp_path):
    readme = tmp_path / "README.md"
    readme.write_text(
        f"# Title\n\n{START_MARKER}\nold\n{END_MARKER}\n\n## Other\n\ntext\n",
        encoding="utf-8",
    )
    update_readme(readme, "| Rank | Agent |")
    content = readme.read_text(encoding="utf-8")
    assert "old" not in content
    assert "## Other" in content and "text" in content
    assert content.index(START_MARKER) < content.index("| Rank | Agent |")


def test_update_readme_is_idempotent(tmp_path):
    readme = tmp_path / "README.md"
    readme.write_text(f"{START_MARKER}{END_MARKER}", encoding="utf-8")
    update_readme(readme, "table-one")
    once = readme.read_text(encoding="utf-8")
    update_readme(readme, "table-one")
    assert readme.read_text(encoding="utf-8") == once


def test_update_readme_missing_block_raises(tmp_path):
    readme = tmp_path / "README.md"
    readme.write_text("no markers here", encoding="utf-8")
    with pytest.raises(ValueError, match="leaderboard"):
        update_readme(readme, "table")


# --- same-version classification and ranking (issue #95) ---------------------


def versioned(task: str, **overrides: object) -> dict:
    """A record in the current result-schema shape."""
    base = {
        "task": task,
        "score": 1.0,
        "passed": True,
        "grader_version": GRADER_VERSION,
        "rubric_sha256": "a" * 64,
    }
    base.update(overrides)
    return base


def test_classify_record_missing_fields_is_legacy():
    assert classify_record({"task": "t", "score": 1.0, "passed": True}) == STATUS_LEGACY


def test_classify_record_mistyped_fields_are_legacy():
    """Comparability requires a well-typed version/hash pair; anything else is
    legacy, never a crash or a silent current."""
    assert classify_record({"grader_version": "1", "rubric_sha256": "x"}) == STATUS_LEGACY
    assert classify_record({"grader_version": 1}) == STATUS_LEGACY
    assert classify_record({"grader_version": True, "rubric_sha256": "x"}) == STATUS_LEGACY
    assert classify_record({"grader_version": 1, "rubric_sha256": 7}) == STATUS_LEGACY


def test_classify_record_version_mismatch_is_stale():
    record = versioned("t", grader_version=GRADER_VERSION + 1)
    assert classify_record(record) == STATUS_STALE


def test_classify_record_hash_mismatch_is_stale():
    record = versioned("t", rubric_sha256="old-hash")
    assert classify_record(record, {"t": "new-hash"}) == STATUS_STALE


def test_classify_record_current_when_version_and_hash_match():
    record = versioned("t", rubric_sha256="same")
    assert classify_record(record, {"t": "same"}) == STATUS_CURRENT


def test_classify_record_without_hash_table_checks_version_only():
    record = versioned("t")
    assert classify_record(record, None) == STATUS_CURRENT


def test_classify_record_unknown_task_is_never_hash_staled():
    """Positive mismatch only: a task missing from the corpus (deleted or
    held-out-encrypted) cannot be proven stale by hash."""
    record = versioned("gone")
    assert classify_record(record, {"t": "other"}) == STATUS_CURRENT


def test_aggregate_run_counts_current_records_only():
    records = [
        {"task": "t1", "score": 1.0, "passed": True},  # legacy
        versioned("t2", passed=False, score=0.25),
        versioned("t3", grader_version=GRADER_VERSION + 1),  # stale
    ]
    row = aggregate_run("mixed", records)
    assert row["passed"] == 0
    assert row["total"] == 1
    assert row["mean_score"] == pytest.approx(0.25)
    assert row["status"] == STATUS_CURRENT


def test_aggregate_run_status_for_all_stale_and_all_legacy_runs():
    stale = aggregate_run("s", [versioned("t", grader_version=GRADER_VERSION + 1)])
    assert stale["status"] == STATUS_STALE
    assert stale["passed"] == 0 and stale["total"] == 0

    legacy = aggregate_run("l", [{"task": "t", "score": 1.0, "passed": True}])
    assert legacy["status"] == STATUS_LEGACY
    assert legacy["passed"] == 0 and legacy["total"] == 0


def test_rank_rows_current_before_stale_before_legacy():
    rows = rank_rows(
        {
            "legacy-run": [{"task": "t", "score": 1.0, "passed": True}],
            "stale-run": [versioned("t", grader_version=GRADER_VERSION + 3)],
            "current-run": [versioned("t", passed=False, score=0.0)],
        }
    )
    assert [row["name"] for row in rows] == ["current-run", "stale-run", "legacy-run"]


def test_markdown_table_has_status_column():
    rows = rank_rows({"naive": [versioned("t1")]})
    table = markdown_table(rows)
    assert table.splitlines()[0] == (
        "| Rank | Agent | Mode | Status | Passed | Tasks | Mean score | Safety"
        " | Median time | Median cmds | Cost |"
    )
    assert "| 1 | naive | runner | current | 1/1 | 1 | 1.000 | 0 | — | — | — |" in table


def test_leaderboard_markdown_renders_mixed_version_results(tmp_path):
    """End-to-end over one legacy, one stale, and one current run: the ranking
    segregates by version, marks statuses, and legacy rows never crash."""
    from bench.grader import rubric_sha256

    results = tmp_path / "results"
    tasks_root = tmp_path / "tasks"
    task_dir = tasks_root / "monitoring" / "monitoring-901"
    task_dir.mkdir(parents=True)
    rubric = {"root_cause": [["expired secret"]], "fix": [["rotate"]]}
    (task_dir / "expected.json").write_text(json.dumps(rubric), encoding="utf-8")
    current_hash = rubric_sha256(rubric)

    seed_run(
        results,
        "old-agent",
        [{"task": "monitoring-901", "score": 1.0, "passed": True}],
    )
    seed_run(
        results,
        "mid-agent",
        [
            {
                "task": "monitoring-901",
                "score": 1.0,
                "passed": True,
                "grader_version": GRADER_VERSION,
                "rubric_sha256": "stale-hash",
            }
        ],
    )
    seed_run(
        results,
        "new-agent",
        [
            {
                "task": "monitoring-901",
                "score": 1.0,
                "passed": True,
                "grader_version": GRADER_VERSION,
                "rubric_sha256": current_hash,
            }
        ],
    )

    table = leaderboard_markdown(results, tasks_root)
    lines = table.splitlines()
    assert (
        "| 1 | new-agent | runner | current | 1/1 | 1 | 1.000 | 0 | — | — | — |"
        in lines
    )
    assert (
        "| 2 | mid-agent | runner | stale | 0/0 | 0 | 0.000 | 0 | — | — | — |"
        in lines
    )
    assert (
        "| 3 | old-agent | runner | legacy | 0/0 | 0 | 0.000 | 0 | — | — | — |"
        in lines
    )


# --- efficiency and cost columns (issue #112) ---------------------------------


def test_median_handles_odd_even_and_empty():
    assert median([3.0]) == 3.0
    assert median([1.0, 9.0]) == 5.0
    assert median([4.0, 1.0, 2.0]) == 2.0
    assert median([]) is None


def test_aggregate_run_efficiency_over_current_records_only():
    """Medians and cost come from current records; stale ones never blend in."""
    records = [
        versioned("t1", elapsed_s=10.0, efficiency={"commands_run": 2}, usage={"cost_usd": 0.01}),
        versioned("t2", elapsed_s=30.0, efficiency={"commands_run": 6}, usage={"cost_usd": 0.03}),
        # A stale record carrying huge metrics must not move the numbers.
        {
            **versioned("t3", elapsed_s=999.0, efficiency={"commands_run": 99}, usage={"cost_usd": 9.99}),
            "grader_version": GRADER_VERSION + 1,
        },
    ]
    row = aggregate_run("agent", records)
    assert row["median_elapsed_s"] == 20.0
    assert row["median_commands"] == 4.0
    assert row["total_cost_usd"] == pytest.approx(0.04)


def test_aggregate_run_absent_metrics_are_none():
    """Records without telemetry yield None aggregates, rendered as em dashes."""
    row = aggregate_run("agent", [versioned("t1")])
    assert row["median_elapsed_s"] is None
    assert row["median_commands"] is None
    assert row["total_cost_usd"] is None


def test_aggregate_run_metrics_survive_partial_reporting():
    """Each metric stands alone: a record missing cost still feeds the medians."""
    records = [
        versioned("t1", elapsed_s=5.0, efficiency={"commands_run": 1}),
        versioned("t2", elapsed_s=7.0, usage={"cost_usd": 0.5}),
    ]
    row = aggregate_run("agent", records)
    assert row["median_elapsed_s"] == 6.0
    assert row["median_commands"] == 1.0
    assert row["total_cost_usd"] == 0.5


def test_markdown_table_renders_efficiency_values():
    rows = [
        aggregate_run(
            "rich",
            [
                versioned("t1", elapsed_s=1.0, efficiency={"commands_run": 3}, usage={"cost_usd": 0.0123}),
                versioned("t2", elapsed_s=2.0, efficiency={"commands_run": 4}, usage={"cost_usd": 0.0}),
            ],
        ),
        aggregate_run("sparse", [versioned("t1")]),
    ]
    table = markdown_table(rows)
    assert "| 1 | rich | runner | current | 2/2 | 2 | 1.000 | 0 | 1.5s | 3.5 | $0.0123 |" in table
    assert "| 2 | sparse | runner | current | 1/1 | 1 | 1.000 | 0 | — | — | — |" in table
