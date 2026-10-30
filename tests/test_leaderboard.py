"""Tests for the leaderboard command and README updater (issue #41)."""

import json
from pathlib import Path

import pytest

from bench.cli import main
from bench.leaderboard import (
    END_MARKER,
    START_MARKER,
    collect_records,
    leaderboard_markdown,
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
    runs = {
        "slow-expert": [record("t1", 1.0, True), record("t2", 0.5, False)],
        "sweep": [record("t1", 0.9, True), record("t2", 0.9, True)],
        "sharp": [record("t1", 1.0, True), record("t2", 0.2, False)],
        "empty": [],
    }
    order = [row["name"] for row in rank_rows(runs)]
    # sweep: 2 passed; slow-expert and sharp: 1 passed each, broken on mean
    # (0.75 vs 0.6); a run with no records ranks last.
    assert order == ["sweep", "slow-expert", "sharp", "empty"]


def test_rank_rows_explicit_order():
    rows = rank_rows(
        {
            "a": [record("t1", 1.0, True)],
            "b": [record("t1", 0.5, True), record("t2", 0.5, True)],
            "c": [record("t1", 0.0, False), record("t2", 0.0, False)],
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


def test_cli_leaderboard_prints_table(tmp_path, capsys):
    seed_run(tmp_path, "naive", [record("t1", 1.0, True)])
    assert main(["leaderboard", "--results-root", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "| 1 | naive | 1/1" in out


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
