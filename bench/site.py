"""Static leaderboard site builder (`bench site`, issue #40).

Reads every run folder under `results/` — one JSON result file per task, as
written by `bench run` — and renders a single self-contained `site/index.html`:
an overall table (agent, passed, mean score, and the issue-#112 efficiency
columns — median time, median commands, total cost), a per-category breakdown,
and a task x agent pass/fail heatmap. No build step, no JavaScript, readable on
a phone. With zero result folders the page renders an empty state instead of
failing, so it can be built before the first benchmark run exists.

Same-version rendering (issue #95): a record graded under a different grader
version or an older rubric renders `stale`, a record written before the
version fields existed renders `legacy` — neither contributes to the ranking
numbers, and the heatmap marks their cells instead of claiming PASS/FAIL.
"""

from __future__ import annotations

import argparse
import html
import json
from pathlib import Path

from bench.leaderboard import (
    ADVERSARIAL_PREFIX,
    STATUS_CURRENT,
    STATUS_LEGACY,
    STATUS_STALE,
    classify_record,
    is_number,
    median,
)
from bench.tasks import CATEGORIES, current_rubric_hashes

DEFAULT_RESULTS_ROOT = "results"
DEFAULT_OUT = "site/index.html"

# Cell states for the task x agent heatmap.
CELL_PASS = "pass"
CELL_FAIL = "fail"
CELL_NONE = "none"
CELL_STALE = "stale"
CELL_LEGACY = "legacy"

# Heatmap labels per cell state (&mdash; renders as an em dash).
_CELL_LABELS = {
    CELL_PASS: "PASS",
    CELL_FAIL: "FAIL",
    CELL_NONE: "&mdash;",
    CELL_STALE: "STALE",
    CELL_LEGACY: "LEGACY",
}

_STYLE = """\
:root { color-scheme: light dark; }
* { box-sizing: border-box; }
body {
  font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
  margin: 0 auto; padding: 1.25rem; max-width: 60rem;
  background: #f6f8fa; color: #1f2328; line-height: 1.5;
}
@media (prefers-color-scheme: dark) {
  body { background: #0d1117; color: #e6edf3; }
  h1, h2 { color: #f0f6fc; }
}
h1 { font-size: 1.5rem; } h2 { font-size: 1.2rem; margin-top: 2rem; }
.tablewrap { overflow-x: auto; -webkit-overflow-scrolling: touch; }
table { border-collapse: collapse; width: 100%; margin: 0.75rem 0; }
th, td { padding: 0.45rem 0.75rem; border: 1px solid #d0d7de; text-align: left; }
th { white-space: nowrap; background: rgba(127, 127, 127, 0.08); }
td.num, th.num { text-align: right; }
td.heat { text-align: center; font-weight: 600; font-size: 0.85rem; }
.pass { color: #1a7f37; } .fail { color: #cf222e; }
@media (prefers-color-scheme: dark) {
  th, td { border-color: #30363d; }
  .pass { color: #3fb950; } .fail { color: #f85149; }
  .stale { color: #d29922; }
}
.none { color: #8c959f; font-weight: 400; }
.stale { color: #9a6700; }
.legacy { color: #8c959f; font-style: italic; }
.empty { border: 1px dashed #8c959f; padding: 1rem; border-radius: 6px; }
footer { margin-top: 2.5rem; font-size: 0.85rem; color: #8c959f; }
code { font-family: ui-monospace, monospace; }
"""


def load_results(results_root: Path) -> dict[str, list[dict]]:
    """Read every run folder under `results_root`.

    Each subdirectory is one run — its name labels the agent in the site — and
    each `*.json` file inside is one graded task record written by `bench run`.
    Unreadable or non-object files are skipped so a single bad record cannot
    take the whole site down; `adversarial-*` folders (issue #92 gate probes)
    are skipped so probe results never render as agents. Returns run name ->
    records sorted by task id.
    """
    results_root = Path(results_root)
    if not results_root.is_dir():
        return {}
    runs: dict[str, list[dict]] = {}
    for run_dir in sorted(path for path in results_root.iterdir() if path.is_dir()):
        if run_dir.name.startswith(ADVERSARIAL_PREFIX):
            continue
        records: list[dict] = []
        for path in sorted(run_dir.glob("*.json")):
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(record, dict):
                record.setdefault("task", path.stem)
                records.append(record)
        records.sort(key=lambda record: str(record.get("task", "")))
        runs[run_dir.name] = records
    return runs


def task_category(task_id: str) -> str:
    """Derive a task's category from its `<category>-<nnn>` id.

    Categories may contain hyphens (e.g. `aks-networking`), so the longest
    known prefix wins; unknown ids fall back to `other`.
    """
    for category in sorted(CATEGORIES, key=len, reverse=True):
        if task_id.startswith(f"{category}-"):
            return category
    return "other"


def aggregate(results: dict[str, list[dict]], rubric_hashes: dict[str, str] | None = None) -> dict:
    """Reduce raw run records into the tables the site renders.

    Returns `{"agents", "tasks", "overall", "by_category", "heatmap"}` where
    overall rows are per agent, by_category rows are per category with one
    sub-row per agent that ran it, and heatmap rows carry one cell per agent.

    Same-version math (issue #95): only records classified `current` contribute
    to the ranking numbers; stale and legacy records keep their heatmap cells
    (marked STALE/LEGACY) but drop out of passed/mean. Overall rows carry the
    agent's status — `current` when any of its records is current, else the
    weakest state present (`stale`, then `legacy`).
    """
    agents = sorted(results)
    # (agent, task) -> graded outcome; last record wins if a task appears twice.
    outcomes: dict[tuple[str, str], dict] = {}
    for agent, records in results.items():
        for record in records:
            task = str(record.get("task") or "")
            if not task:
                continue
            try:
                score = float(record.get("score", 0.0))
            except (TypeError, ValueError):
                score = 0.0
            outcomes[(agent, task)] = {
                "passed": bool(record.get("passed")),
                "score": score,
                "status": classify_record(record, rubric_hashes),
                # Efficiency/cost telemetry (issue #112): carried so the
                # overall row can aggregate it; absent keys stay absent.
                "elapsed_s": record.get("elapsed_s"),
                "efficiency": record.get("efficiency"),
                "usage": record.get("usage"),
            }
    tasks = sorted({task for _, task in outcomes})

    def row(agent: str, category: str | None) -> dict:
        picked = [
            outcome
            for (run, task), outcome in outcomes.items()
            if run == agent and (category is None or task_category(task) == category)
        ]
        counted = [outcome for outcome in picked if outcome["status"] == STATUS_CURRENT]
        passed = sum(1 for outcome in counted if outcome["passed"])
        mean = (
            round(sum(outcome["score"] for outcome in counted) / len(counted), 3)
            if counted
            else 0.0
        )
        # Efficiency/cost aggregates (issue #112), over current-version
        # outcomes only; a metric no record reported renders an em dash.
        elapsed = [
            outcome["elapsed_s"]
            for outcome in counted
            if is_number(outcome.get("elapsed_s"))
        ]
        commands = [
            outcome["efficiency"]["commands_run"]
            for outcome in counted
            if isinstance(outcome.get("efficiency"), dict)
            and is_number(outcome["efficiency"].get("commands_run"))
        ]
        costs = [
            outcome["usage"]["cost_usd"]
            for outcome in counted
            if isinstance(outcome.get("usage"), dict)
            and is_number(outcome["usage"].get("cost_usd"))
        ]
        return {
            "agent": agent,
            "passed": passed,
            "total": len(counted),
            "mean_score": mean,
            "median_elapsed_s": median(elapsed),
            "median_commands": median(commands),
            "total_cost_usd": sum(costs) if costs else None,
        }

    def agent_status(agent: str) -> str:
        statuses = [
            outcome["status"]
            for (run, _), outcome in outcomes.items()
            if run == agent
        ]
        if not statuses:
            # No graded outcomes at all: no current-version result to rank,
            # matching the leaderboard's empty-run status.
            return STATUS_LEGACY
        if STATUS_CURRENT in statuses:
            return STATUS_CURRENT
        return STATUS_STALE if STATUS_STALE in statuses else STATUS_LEGACY

    overall = [{**row(agent, None), "status": agent_status(agent)} for agent in agents]

    by_category: list[dict] = []
    for category in sorted({task_category(task) for task in tasks}):
        agents_rows = [row(agent, category) for agent in agents]
        by_category.append(
            {
                "category": category,
                "agents": [entry for entry in agents_rows if entry["total"]],
            }
        )

    heatmap = [
        {
            "task": task,
            "cells": [
                _cell(outcomes.get((agent, task))) for agent in agents
            ],
        }
        for task in tasks
    ]
    return {
        "agents": agents,
        "tasks": tasks,
        "overall": overall,
        "by_category": by_category,
        "heatmap": heatmap,
    }


def _cell(outcome: dict | None) -> str:
    if outcome is None:
        return CELL_NONE
    status = outcome.get("status", STATUS_CURRENT)
    if status == STATUS_STALE:
        return CELL_STALE
    if status == STATUS_LEGACY:
        return CELL_LEGACY
    return CELL_PASS if outcome["passed"] else CELL_FAIL


def _num_cell(value: float | None, formatted: str | None = None) -> str:
    """A right-aligned numeric cell; an em dash when the metric is absent."""
    if value is None:
        return '<td class="num none">&mdash;</td>'
    return f'<td class="num">{formatted if formatted is not None else value}</td>'


def _summary_row(entry: dict) -> str:
    esc_agent = html.escape(entry["agent"])
    elapsed = entry.get("median_elapsed_s")
    commands = entry.get("median_commands")
    cost = entry.get("total_cost_usd")
    return (
        f"<tr><td>{esc_agent}</td>"
        f'<td>{html.escape(entry.get("status", STATUS_CURRENT))}</td>'
        f'<td class="num">{entry["passed"]}/{entry["total"]}</td>'
        f'<td class="num">{entry["mean_score"]:.3f}</td>'
        + _num_cell(elapsed, f"{elapsed:.1f}s" if elapsed is not None else None)
        + _num_cell(commands, format(commands, "g") if commands is not None else None)
        + _num_cell(cost, f"${cost:.4f}" if cost is not None else None)
        + "</tr>"
    )


def render_html(agg: dict) -> str:
    """Render the aggregated tables as one self-contained HTML page."""
    parts = [
        "<!DOCTYPE html>",
        '<html lang="en">',
        "<head>",
        '<meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        "<title>Azure SRE Bench — Leaderboard</title>",
        f"<style>{_STYLE}</style>",
        "</head>",
        "<body>",
        "<h1>Azure SRE Bench — Leaderboard</h1>",
    ]

    if not agg["tasks"]:
        parts.append(
            '<p class="empty">No results yet. Run an agent with '
            "<code>bench run</code> and rebuild the site with "
            "<code>bench site</code>.</p>"
        )
    else:
        parts.append("<h2>Overall</h2>")
        parts.append('<div class="tablewrap"><table>')
        parts.append(
            "<tr><th>Agent</th><th>Status</th><th class=num>Passed</th>"
            "<th class=num>Mean score</th><th class=num>Median time</th>"
            "<th class=num>Median cmds</th><th class=num>Cost</th></tr>"
        )
        parts.extend(_summary_row(entry) for entry in agg["overall"])
        parts.append("</table></div>")

        parts.append("<h2>By category</h2>")
        parts.append('<div class="tablewrap"><table>')
        header = "".join(f"<th>{html.escape(agent)}</th>" for agent in agg["agents"])
        parts.append(
            f"<tr><th>Category</th><th class=num>Passed</th>"
            f"<th class=num>Mean score</th>{header}</tr>"
        )
        for block in agg["by_category"]:
            cells = []
            for agent in agg["agents"]:
                entry = next(
                    (e for e in block["agents"] if e["agent"] == agent), None
                )
                if entry is None:
                    cells.append('<td class="num none">&mdash;</td>')
                else:
                    cells.append(
                        f'<td class="num">{entry["passed"]}/{entry["total"]}</td>'
                        f'<td class="num">{entry["mean_score"]:.3f}</td>'
                    )
            parts.append(
                f"<tr><td>{html.escape(block['category'])}</td>{''.join(cells)}</tr>"
            )
        parts.append("</table></div>")

        parts.append("<h2>Task &times; agent</h2>")
        parts.append('<div class="tablewrap"><table>')
        header = "".join(f"<th>{html.escape(agent)}</th>" for agent in agg["agents"])
        parts.append(f"<tr><th>Task</th>{header}</tr>")
        for row in agg["heatmap"]:
            cells = "".join(
                f'<td class="heat {state}">{_CELL_LABELS.get(state, "&mdash;")}</td>'
                for state in row["cells"]
            )
            parts.append(f"<tr><td>{html.escape(row['task'])}</td>{cells}</tr>")
        parts.append("</table></div>")

    parts.append(
        "<footer>Generated by <code>bench site</code> &middot; "
        "<a href=\"https://github.com/ravirajnalls/azure-sre-bench\">azure-sre-bench</a></footer>"
    )
    parts.append("</body></html>")
    return "\n".join(parts)


def cmd_site(args: argparse.Namespace) -> int:
    """CLI entry for `bench site`: load results, render, write the page."""
    results = load_results(Path(args.results_root))
    agg = aggregate(results, current_rubric_hashes(Path(args.tasks_root)))
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_html(agg), encoding="utf-8")
    n_results = sum(len(records) for records in results.values())
    print(f"wrote {out} ({len(results)} runs, {n_results} task results)")
    return 0
