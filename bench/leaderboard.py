"""Leaderboard aggregation and markdown rendering (issue #41).

`bench run` writes one JSON record per task under `results/<run-name>/`. This
module turns those folders into one aggregate row per run name and renders a
markdown table sorted by passed tasks, then mean score — the ranking the README
`<!-- leaderboard -->` block and the reporting layer share.

Run folders named `adversarial-*` are excluded: they hold the adversarial
probes' results (issue #92), which are gate diagnostics, never entries.

Same-version comparison (issue #95): ranking math only counts records whose
`grader_version` matches the current grader and whose `rubric_sha256` matches
the current rubric. Every other record renders `stale` (graded under a
different grader version or an older rubric) or `legacy` (written before
those fields existed); a run with no current-version records ranks after all
current-version runs and carries its status in the table.
"""

from __future__ import annotations

import json
from pathlib import Path

from bench import GRADER_VERSION
from bench.heldout import SPLIT_HELDOUT, SPLIT_PUBLIC
from bench.tasks import current_rubric_hashes

RESULTS_DIR = Path("results")
START_MARKER = "<!-- leaderboard -->"
END_MARKER = "<!-- /leaderboard -->"

# Run-name prefix reserved for the adversarial probes (issue #92). Their
# results measure grader gameability, not agent ability, so they never rank.
ADVERSARIAL_PREFIX = "adversarial-"

# Record classification against the current grader version and rubric (#95).
STATUS_CURRENT = "current"
STATUS_STALE = "stale"
STATUS_LEGACY = "legacy"

# Current-version runs rank first; stale, then legacy, follow in name order.
_STATUS_ORDER = {STATUS_CURRENT: 0, STATUS_STALE: 1, STATUS_LEGACY: 2}


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
        if run_dir.name.startswith(ADVERSARIAL_PREFIX):
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


def classify_record(record: dict, rubric_hashes: dict[str, str] | None = None) -> str:
    """One record vs the current grader and rubric: current, stale, or legacy.

    `legacy` — the record lacks a well-typed `grader_version`/`rubric_sha256`
    pair (pre-extension or malformed): comparability cannot be established.
    `stale` — the record was graded by a different grader version, or under a
    rubric whose current hash differs (a positive mismatch only: a task whose
    current rubric is unknown — deleted, held-out-encrypted — is never
    hash-staled). `current` otherwise.
    """
    version = record.get("grader_version")
    recorded_hash = record.get("rubric_sha256")
    if (
        not isinstance(version, int)
        or isinstance(version, bool)
        or not isinstance(recorded_hash, str)
    ):
        return STATUS_LEGACY
    if version != GRADER_VERSION:
        return STATUS_STALE
    if rubric_hashes is not None:
        current_hash = rubric_hashes.get(str(record.get("task", "")))
        if current_hash is not None and current_hash != recorded_hash:
            return STATUS_STALE
    return STATUS_CURRENT


def aggregate_run(
    name: str, records: list[dict], rubric_hashes: dict[str, str] | None = None
) -> dict:
    """One aggregate row per run: passed count, task count, mean score, mode.

    A run is `manual` when any record was submitted through the closed-agent
    protocol (issue #124) — the leaderboard flags those entries so scores
    produced by pasting into a chat UI are distinguishable from `bench run`
    subprocess runs.

    Counts cover current-version records only (issue #95): a run mixing
    grader versions or rubric hashes must not rank on a blended aggregate, so
    stale and legacy records drop out of the numbers. A run with no
    current-version records shows 0/0 and carries its `stale`/`legacy` status
    in the rendered table instead of a comparable score; a run with no records
    at all renders `legacy` and ranks last, as it always has.
    """
    classes = [classify_record(record, rubric_hashes) for record in records]
    current = [
        record
        for record, status in zip(records, classes)
        if status == STATUS_CURRENT
    ]
    passed = sum(1 for record in current if record.get("passed") is True)
    scores = [
        record["score"]
        for record in current
        if isinstance(record.get("score"), (int, float))
    ]
    mean_score = sum(scores) / len(scores) if scores else 0.0
    # Safety column (issue #113): total unsafe-fix matches, counted over
    # current-version records for the same reason as the other counts — a
    # stale record's violations must not blend into a current run's number.
    safety = sum(
        len(record["safety_violations"])
        for record in current
        if isinstance(record.get("safety_violations"), list)
    )
    if not records:
        status = STATUS_LEGACY
    elif STATUS_CURRENT in classes:
        status = STATUS_CURRENT
    elif STATUS_STALE in classes:
        status = STATUS_STALE
    else:
        status = STATUS_LEGACY
    return {
        "name": name,
        "passed": passed,
        "total": len(current),
        "mean_score": mean_score,
        "mode": "manual" if any(r.get("mode") == "manual" for r in records) else "runner",
        "safety_violations": safety,
        "status": status,
    }


def split_runs(runs: dict[str, list[dict]]) -> dict[str, dict[str, list[dict]]]:
    """Group run records by their task split (issue #115).

    Returns split -> run name -> records, so public and held-out scores rank
    separately; records written before the split existed default to public.
    """
    grouped: dict[str, dict[str, list[dict]]] = {}
    for name, records in runs.items():
        for record in records:
            split = str(record.get("split") or "")
            if split not in (SPLIT_PUBLIC, SPLIT_HELDOUT):
                split = SPLIT_PUBLIC
            grouped.setdefault(split, {}).setdefault(name, []).append(record)
    return grouped


def rank_rows(
    runs: dict[str, list[dict]], rubric_hashes: dict[str, str] | None = None
) -> list[dict]:
    """Aggregate every run; current-version runs rank first, then stale, then
    legacy, each group sorted by passed desc, then mean score desc."""
    rows = [aggregate_run(name, records, rubric_hashes) for name, records in runs.items()]
    rows.sort(
        key=lambda row: (
            _STATUS_ORDER.get(row["status"], 0),
            -row["passed"],
            -row["mean_score"],
            row["name"],
        )
    )
    return rows


def markdown_table(rows: list[dict]) -> str:
    """Render ranked rows as a markdown table (headers only when empty)."""
    lines = [
        "| Rank | Agent | Mode | Status | Passed | Tasks | Mean score | Safety |",
        "| ---: | --- | --- | --- | ---: | ---: | ---: | ---: |",
    ]
    for rank, row in enumerate(rows, start=1):
        lines.append(
            f"| {rank} | {row['name']} | {row.get('mode', 'runner')}"
            f" | {row.get('status', STATUS_CURRENT)}"
            f" | {row['passed']}/{row['total']}"
            f" | {row['total']} | {row['mean_score']:.3f}"
            f" | {row.get('safety_violations', 0)} |"
        )
    return "\n".join(lines)


def leaderboard_markdown(
    results_root: Path, tasks_root: Path | None = None
) -> str:
    """Full pipeline: results folder -> ranked markdown, one table per split.

    The public split renders first, exactly as pre-split leaderboards did; the
    held-out table appears only when held-out results exist (issue #115).
    With `tasks_root`, records are also hash-checked against the current
    rubrics; without it, staleness is decided by grader version alone.
    """
    grouped = split_runs(collect_records(results_root))
    hashes = current_rubric_hashes(Path(tasks_root)) if tasks_root is not None else None
    sections = [markdown_table(rank_rows(grouped.get(SPLIT_PUBLIC, {}), hashes))]
    heldout = grouped.get(SPLIT_HELDOUT)
    if heldout:
        sections.append("### Held-out split\n")
        sections.append(markdown_table(rank_rows(heldout, hashes)))
    return "\n".join(sections)


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
