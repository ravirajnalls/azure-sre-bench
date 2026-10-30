"""Leaderboard aggregation and markdown rendering (issue #41).

`bench run` writes one JSON record per task under `results/<run-name>/`. This
module turns those folders into one aggregate row per run name and renders a
markdown table sorted by passed tasks, then mean score — the ranking the README
`<!-- leaderboard -->` block and the reporting layer share.
"""

from __future__ import annotations

import json
from pathlib import Path

RESULTS_DIR = Path("results")
START_MARKER = "<!-- leaderboard -->"
END_MARKER = "<!-- /leaderboard -->"


def collect_records(results_root: Path) -> dict[str, list[dict]]:
    """Map run name -> parsed result records from `<results_root>/<run>/*.json`.

    Unparsable or non-object JSON files are skipped; a run folder with no
    records still yields an entry, so a run that produced nothing stays visible.
    """
    runs: dict[str, list[dict]] = {}
    root = Path(results_root)
    if not root.is_dir():
        return runs
    for run_dir in sorted(root.iterdir()):
        if not run_dir.is_dir():
            continue
        records = runs.setdefault(run_dir.name, [])
        for path in sorted(run_dir.glob("*.json")):
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(record, dict):
                records.append(record)
    return runs


def aggregate_run(name: str, records: list[dict]) -> dict:
    """One aggregate row per run: passed count, task count, and mean score."""
    passed = sum(1 for record in records if record.get("passed") is True)
    scores = [
        record["score"]
        for record in records
        if isinstance(record.get("score"), (int, float))
    ]
    mean_score = sum(scores) / len(scores) if scores else 0.0
    return {
        "name": name,
        "passed": passed,
        "total": len(records),
        "mean_score": mean_score,
    }


def rank_rows(runs: dict[str, list[dict]]) -> list[dict]:
    """Aggregate every run and sort by passed desc, then mean score desc."""
    rows = [aggregate_run(name, records) for name, records in runs.items()]
    rows.sort(key=lambda row: (-row["passed"], -row["mean_score"], row["name"]))
    return rows


def markdown_table(rows: list[dict]) -> str:
    """Render ranked rows as a markdown table (headers only when empty)."""
    lines = [
        "| Rank | Agent | Passed | Tasks | Mean score |",
        "| ---: | --- | ---: | ---: | ---: |",
    ]
    for rank, row in enumerate(rows, start=1):
        lines.append(
            f"| {rank} | {row['name']} | {row['passed']}/{row['total']}"
            f" | {row['total']} | {row['mean_score']:.3f} |"
        )
    return "\n".join(lines)


def leaderboard_markdown(results_root: Path) -> str:
    """Full pipeline: results folder -> ranked markdown table."""
    return markdown_table(rank_rows(collect_records(results_root)))


def update_readme(readme_path: Path, table: str) -> None:
    """Replace the content between the leaderboard markers in `readme_path`.

    Raises ValueError when the markers are missing or inverted — the README
    block is edited only here, never appended to blindly.
    """
    text = Path(readme_path).read_text(encoding="utf-8")
    prefix, start_sep, rest = text.partition(START_MARKER)
    if not start_sep:
        raise ValueError(f"{readme_path}: missing {START_MARKER!r} block")
    _, end_sep, suffix = rest.partition(END_MARKER)
    if not end_sep:
        raise ValueError(f"{readme_path}: missing {END_MARKER!r} block")
    updated = f"{prefix}{START_MARKER}\n{table}\n{END_MARKER}{suffix}"
    Path(readme_path).write_text(updated, encoding="utf-8")
