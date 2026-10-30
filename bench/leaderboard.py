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

Pass-rate confidence intervals (issue #161): every current-version run also
carries a deterministic 95% bootstrap CI over its per-task outcomes
(`bench.stats`), and adjacent rows whose point estimates fall inside each
other's intervals are marked `≈` — the data cannot order them.
"""

from __future__ import annotations

import json
from pathlib import Path

from bench import GRADER_VERSION
from bench.heldout import SPLIT_HELDOUT, SPLIT_PUBLIC
from bench.stats import bootstrap_pass_rate_ci, intervals_overlap_heavily
from bench.tasks import current_rubric_hashes, difficulty_sort_key

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


def median(values: list[float]) -> float | None:
    """Statistical median of a numeric list; None for an empty list.

    Renderers use the None to draw an em dash: an agent that never reported a
    metric has no median, which is a different fact from a median of zero.
    """
    if not values:
        return None
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return float(ordered[mid])
    return (ordered[mid - 1] + ordered[mid]) / 2


def is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


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
    # Efficiency and cost columns (issue #112), over current-version records
    # only, like every other ranking number. Each is present-informative:
    # records without the metric drop out of that metric's numbers, and a run
    # with no metric-carrying records at all renders an em dash.
    elapsed = [
        record["elapsed_s"]
        for record in current
        if is_number(record.get("elapsed_s"))
    ]
    commands = [
        record["efficiency"]["commands_run"]
        for record in current
        if isinstance(record.get("efficiency"), dict)
        and is_number(record["efficiency"].get("commands_run"))
    ]
    costs = [
        record["usage"]["cost_usd"]
        for record in current
        if isinstance(record.get("usage"), dict)
        and is_number(record["usage"].get("cost_usd"))
    ]
    if not records:
        status = STATUS_LEGACY
    elif STATUS_CURRENT in classes:
        status = STATUS_CURRENT
    elif STATUS_STALE in classes:
        status = STATUS_STALE
    else:
        status = STATUS_LEGACY
    # Pass-rate CI (issue #161): a deterministic 95% bootstrap interval over
    # the current records' per-task outcomes. Ranking math is unchanged — the
    # interval informs, it does not order; only None when nothing is graded.
    pass_rate = passed / len(current) if current else None
    ci = (
        bootstrap_pass_rate_ci(
            [record.get("passed") is True for record in current]
        )
        if current
        else None
    )
    return {
        "name": name,
        "passed": passed,
        "total": len(current),
        "mean_score": mean_score,
        "pass_rate": pass_rate,
        "ci_low": ci[0] if ci else None,
        "ci_high": ci[1] if ci else None,
        "tied": False,
        "mode": "manual" if any(r.get("mode") == "manual" for r in records) else "runner",
        "safety_violations": safety,
        "median_elapsed_s": median(elapsed),
        "median_commands": median(commands),
        "total_cost_usd": sum(costs) if costs else None,
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
    legacy, each group sorted by passed desc, then mean score desc.

    Adjacent rows whose point pass rates fall inside each other's bootstrap
    intervals are flagged `tied` (issue #161) — the ranking put them in order,
    but the data cannot actually tell them apart. Only current-version runs
    carry intervals, so the flag never crosses a status boundary.
    """
    rows = [aggregate_run(name, records, rubric_hashes) for name, records in runs.items()]
    rows.sort(
        key=lambda row: (
            _STATUS_ORDER.get(row["status"], 0),
            -row["passed"],
            -row["mean_score"],
            row["name"],
        )
    )
    for lower, upper in zip(rows, rows[1:]):
        if lower["ci_low"] is None or upper["ci_low"] is None:
            continue
        if intervals_overlap_heavily(
            lower["pass_rate"],
            (lower["ci_low"], lower["ci_high"]),
            upper["pass_rate"],
            (upper["ci_low"], upper["ci_high"]),
        ):
            lower["tied"] = upper["tied"] = True
    return rows


def _fmt_elapsed(seconds: float | None) -> str:
    return f"{seconds:.1f}s" if seconds is not None else "—"


def _fmt_commands(count: float | None) -> str:
    # A median of integer counts can be a half (even sample); %g trims the
    # trailing .0 while keeping the honest .5.
    return format(count, "g") if count is not None else "—"


def _fmt_cost(usd: float | None) -> str:
    return f"${usd:.4f}" if usd is not None else "—"


def _fmt_ci(row: dict) -> str:
    """The pass-rate cell: `55.0% (32.4–75.5)`, em dash when no interval."""
    rate, low, high = (
        row.get("pass_rate"),
        row.get("ci_low"),
        row.get("ci_high"),
    )
    if rate is None or low is None or high is None:
        return "—"
    return f"{rate * 100:.1f}% ({low * 100:.1f}–{high * 100:.1f})"


def markdown_table(rows: list[dict]) -> str:
    """Render ranked rows as a markdown table (headers only when empty)."""
    lines = [
        "| Rank | Agent | Mode | Status | Passed | Tasks | Mean score | Pass rate (95% CI)"
        " | Safety | Median time | Median cmds | Cost |",
        "| ---: | --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for rank, row in enumerate(rows, start=1):
        tied = "≈" if row.get("tied") else ""
        lines.append(
            f"| {rank}{tied} | {row['name']} | {row.get('mode', 'runner')}"
            f" | {row.get('status', STATUS_CURRENT)}"
            f" | {row['passed']}/{row['total']}"
            f" | {row['total']} | {row['mean_score']:.3f}"
            f" | {_fmt_ci(row)}"
            f" | {row.get('safety_violations', 0)}"
            f" | {_fmt_elapsed(row.get('median_elapsed_s'))}"
            f" | {_fmt_commands(row.get('median_commands'))}"
            f" | {_fmt_cost(row.get('total_cost_usd'))} |"
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


def breakdown_markdown(results_root: Path, tasks_root: Path, by: str) -> str:
    """Markdown pass-rate breakdown by category or difficulty (issue #165).

    One row per group, one column per agent: `passed/total (mean)` over
    current-version records only — the same math the site's SVG breakdown
    charts use. An em dash where an agent did not run the group's tasks.
    """
    from bench.tasks import task_category, task_difficulties  # deferred: yaml work

    runs = collect_records(results_root)
    hashes = current_rubric_hashes(tasks_root)
    difficulties = task_difficulties(tasks_root) if by == "difficulty" else None

    def group_of(task: str) -> str:
        if by == "difficulty":
            return difficulties.get(task, "unknown")
        return task_category(task)

    agents = sorted(runs)
    tasks = sorted(
        {str(r.get("task")) for records in runs.values() for r in records if r.get("task")}
    )
    # (agent, group) -> [passed, total, score_sum] over current records only.
    counts: dict[tuple[str, str], list] = {}
    for name, records in runs.items():
        for rec in records:
            if classify_record(rec, hashes) != STATUS_CURRENT:
                continue
            score = rec.get("score")
            group = group_of(str(rec.get("task") or ""))
            tally = counts.setdefault((name, group), [0, 0, 0.0])
            tally[0] += bool(rec.get("passed"))
            tally[1] += 1
            if is_number(score):
                tally[2] += score

    # Difficulty rows keep easy/medium/hard order; category rows are alphabetical.
    # Groups with no current-version records at all drop out entirely — matching
    # the site's breakdowns, which only render groups someone actually ran.
    key = difficulty_sort_key if by == "difficulty" else None
    groups = [
        group
        for group in sorted({group_of(t) for t in tasks}, key=key)
        if any((agent, group) in counts for agent in agents)
    ]

    lines = [f"| {by.capitalize()} | " + " | ".join(agents) + " |"]
    lines.append("| --- |" + " --- |" * len(agents))
    for group in groups:
        cells = []
        for agent in agents:
            tally = counts.get((agent, group))
            if tally is None:
                cells.append("—")
            else:
                passed, total, score_sum = tally
                cells.append(f"{passed}/{total} ({score_sum / total:.3f})")
        lines.append(f"| {group} | " + " | ".join(cells) + " |")
    return "\n".join(lines)


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
