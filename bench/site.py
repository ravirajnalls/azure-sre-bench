"""Static leaderboard site builder (`bench site`, issue #40).

Reads every run folder under `results/` — one JSON result file per task, as
written by `bench run` — and renders a single self-contained `site/index.html`:
an overall table (agent, passed, mean score), a per-category breakdown, and a
task x agent pass/fail heatmap. No build step, no JavaScript, readable on a
phone. With zero result folders the page renders an empty state instead of
failing, so it can be built before the first benchmark run exists.
"""

from __future__ import annotations

import argparse
import html
import json
from pathlib import Path

from bench.leaderboard import ADVERSARIAL_PREFIX
from bench.tasks import CATEGORIES

DEFAULT_RESULTS_ROOT = "results"
DEFAULT_OUT = "site/index.html"

# Cell states for the task x agent heatmap.
CELL_PASS = "pass"
CELL_FAIL = "fail"
CELL_NONE = "none"

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
}
.none { color: #8c959f; font-weight: 400; }
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


def aggregate(results: dict[str, list[dict]]) -> dict:
    """Reduce raw run records into the tables the site renders.

    Returns `{"agents", "tasks", "overall", "by_category", "heatmap"}` where
    overall rows are per agent, by_category rows are per category with one
    sub-row per agent that ran it, and heatmap rows carry one cell per agent.
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
            outcomes[(agent, task)] = {"passed": bool(record.get("passed")), "score": score}
    tasks = sorted({task for _, task in outcomes})

    def row(agent: str, category: str | None) -> dict:
        picked = [
            outcome
            for (run, task), outcome in outcomes.items()
            if run == agent and (category is None or task_category(task) == category)
        ]
        passed = sum(1 for outcome in picked if outcome["passed"])
        mean = (
            round(sum(outcome["score"] for outcome in picked) / len(picked), 3)
            if picked
            else 0.0
        )
        return {"agent": agent, "passed": passed, "total": len(picked), "mean_score": mean}

    overall = [row(agent, None) for agent in agents]

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
    return CELL_PASS if outcome["passed"] else CELL_FAIL


def _summary_row(entry: dict) -> str:
    esc_agent = html.escape(entry["agent"])
    return (
        f"<tr><td>{esc_agent}</td>"
        f'<td class="num">{entry["passed"]}/{entry["total"]}</td>'
        f'<td class="num">{entry["mean_score"]:.3f}</td></tr>'
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
            "<tr><th>Agent</th><th class=num>Passed</th><th class=num>Mean score</th></tr>"
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
                f'<td class="heat {state}">{label}</td>'
                for state, label in zip(
                    row["cells"], ("PASS", "FAIL", "&mdash;")
                )
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
    agg = aggregate(results)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_html(agg), encoding="utf-8")
    n_results = sum(len(records) for records in results.values())
    print(f"wrote {out} ({len(results)} runs, {n_results} task results)")
    return 0
