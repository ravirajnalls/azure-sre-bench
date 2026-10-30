"""Static leaderboard site builder (`bench site`, issue #40).

Reads every run folder under `results/` — one JSON result file per task, as
written by `bench run` — and renders a single self-contained `site/index.html`:
an overall table (agent, passed, mean score, and the issue-#112 efficiency
columns — median time, median commands, total cost), a per-category breakdown,
and a task x agent pass/fail heatmap. No build step, readable on a phone. With
zero result folders the page renders an empty state instead of failing, so it
can be built before the first benchmark run exists.

Agent metadata and filters (issue #119): a run folder's `agent.yaml` (#118)
supplies the agent's model, provider, framework, open-source flag, and repo
link, rendered under the agent's name. Inline JavaScript (no framework, still
one static file) filters the tables by category, task difficulty, open-source
only, and grading mode — the site reads task difficulty from `tasks_root` at
build time and bakes it into `data-` attributes.

Cost vs accuracy (issue #164): a scatter plot of each agent's total recorded
cost against its pass rate, with the Pareto frontier (no agent dominated on
both axes) highlighted and agents without cost telemetry listed separately.

Breakdown charts (issue #165): a category × agent pass-rate heatmap and a
difficulty × agent bar chart, next to the tables; `bench leaderboard --by
category|difficulty` prints the same breakdowns as markdown. Task category
and difficulty derivation now live in `bench.tasks` and are re-exported
here for backward compatibility.

Learn pages (issue #149): `bench site` also renders `site/learn.html` — one
spoiler-collapsed (`<details>`) section per task that ships an `explainer.md`,
rendered from a minimal Markdown subset with everything HTML-escaped. Sections
stay collapsed by default so a reader cannot be spoiled by scrolling.

Same-version rendering (issue #95): a record graded under a different grader
version or an older rubric renders `stale`, a record written before the
version fields existed renders `legacy` — neither contributes to the ranking
numbers, and the heatmap marks their cells instead of claiming PASS/FAIL.
"""

from __future__ import annotations

import argparse
import html
import json
import re
from pathlib import Path

import yaml

from bench.leaderboard import (
    ADVERSARIAL_PREFIX,
    STATUS_CURRENT,
    STATUS_LEGACY,
    STATUS_STALE,
    classify_record,
    is_number,
    median,
)
from bench.stats import bootstrap_pass_rate_ci
from bench.tasks import (
    current_rubric_hashes,
    difficulty_sort_key,
    task_category,
    task_difficulties,
)
from bench.verify import load_agent_metadata

DEFAULT_RESULTS_ROOT = "results"
DEFAULT_OUT = "site/index.html"
DEFAULT_LEARN_OUT = "site/learn.html"

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
/* Issue-#164 cost vs accuracy chart */
.chart { width: 100%; height: auto; }
.chart text { fill: #1f2328; font-size: 12px; font-family: system-ui, sans-serif; }
.chart .grid { stroke: #d0d7de; stroke-width: 1; }
.chart .axis { stroke: #8c959f; stroke-width: 1; }
.chart .pt { fill: #57606a; }
.chart .pt.frontier { fill: #1a7f37; }
.chart .frontier-line {
  fill: none; stroke: #1a7f37; stroke-width: 1.5; stroke-dasharray: 4 3;
}
@media (prefers-color-scheme: dark) {
  .chart text { fill: #e6edf3; }
  .chart .grid { stroke: #30363d; }
  .chart .pt { fill: #8b949e; }
  .chart .pt.frontier { fill: #3fb950; }
  .chart .frontier-line { stroke: #3fb950; }
}
/* Issue-#165 breakdown charts */
.chart .col-head { font-weight: 600; }
.chart .cell { fill: #1a7f37; }
.chart .cell.none { fill: none; stroke: #d0d7de; stroke-dasharray: 2 2; }
.chart .cell-text { fill: #1f2328; }
.chart .cell-text.none { fill: #8c959f; }
.chart .cell-text.light { fill: #ffffff; }
.chart .track { fill: rgba(127, 127, 127, 0.12); }
.chart .bar-easy { fill: #1a7f37; }
.chart .bar-medium { fill: #9a6700; }
.chart .bar-hard { fill: #cf222e; }
@media (prefers-color-scheme: dark) {
  .chart .cell { fill: #3fb950; }
  .chart .cell.none { stroke: #30363d; }
  .chart .cell-text { fill: #e6edf3; }
  .chart .cell-text.none { fill: #8b949e; }
  .chart .cell-text.light { fill: #ffffff; }
  .chart .track { fill: rgba(139, 148, 158, 0.2); }
  .chart .bar-easy { fill: #3fb950; }
  .chart .bar-medium { fill: #d29922; }
  .chart .bar-hard { fill: #f85149; }
}
/* Issue-#119 filters: wrap on phone width; .hidden hides filtered-out rows. */
.filters {
  display: flex; flex-wrap: wrap; gap: 0.75rem; align-items: center;
  margin: 1rem 0; font-size: 0.9rem;
}
.filters label { display: inline-flex; gap: 0.35rem; align-items: center; }
.filters select { font: inherit; padding: 0.2rem 0.4rem; }
.hidden { display: none; }
td .sub { font-size: 0.78rem; color: #57606a; }
@media (prefers-color-scheme: dark) {
  td .sub { color: #8b949e; }
}
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


def load_agent_metadata_map(results_root: Path) -> dict[str, dict]:
    """Map run name -> its #118 `agent.yaml` descriptor, when valid.

    A run without `agent.yaml`, or with one that fails the #118 schema, is
    simply absent from the map: the site still renders the run under its
    folder name with em-dash metadata, the same way it renders a run with no
    efficiency metrics. Verification strictness lives in `bench verify`;
    the site only enriches.
    """
    root = Path(results_root)
    if not root.is_dir():
        return {}
    metadata: dict[str, dict] = {}
    for run_dir in sorted(path for path in root.iterdir() if path.is_dir()):
        meta, errors = load_agent_metadata(run_dir)
        if meta is not None and not errors:
            metadata[run_dir.name] = meta
    return metadata


def safe_repo_url(value: object) -> str | None:
    """The agent.yaml `repository` value as a linkable URL, or None.

    Only http(s) links render as anchors — the field is attacker-controlled
    (it ships inside a submitted run folder), so a `javascript:` or `data:`
    URL must not become a clickable href.
    """
    if not isinstance(value, str):
        return None
    url = value.strip()
    if url.lower().startswith(("http://", "https://")):
        return url
    return None


def is_open_source(meta: dict | None) -> bool:
    """Interpret the #118 `open-source` field; unknown counts as closed.

    The filter hides closed-source agents, so absence of the flag must not
    default to open — an unverifiable claim renders closed, never open.
    """
    if not meta:
        return False
    value = meta.get("open-source")
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("yes", "true")


def agent_subtitle(meta: dict | None) -> str:
    """One-line human summary of the #118 descriptor for the overall table."""
    if not meta:
        return ""
    parts = [
        str(meta.get(field)).strip()
        for field in ("model", "provider", "framework", "version")
        if str(meta.get(field) or "").strip()
    ]
    return " · ".join(parts)


def aggregate(
    results: dict[str, list[dict]],
    rubric_hashes: dict[str, str] | None = None,
    agent_metadata: dict[str, dict] | None = None,
    difficulties: dict[str, str] | None = None,
) -> dict:
    """Reduce raw run records into the tables the site renders.

    Returns `{"agents", "tasks", "overall", "by_category", "heatmap"}` where
    overall rows are per agent, by_category rows are per category with one
    sub-row per agent that ran it, and heatmap rows carry one cell per agent.

    With `agent_metadata` (#119), each overall row carries the run's validated
    `agent.yaml` descriptor and its grading mode (`manual` when any record was
    submitted through the closed-agent protocol, else `runner`) — the inputs
    the open-source-only and grading-mode filters read. With `difficulties`,
    each heatmap row carries its task's category and difficulty for the
    category/difficulty filters.

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

    def row(
        agent: str, category: str | None, difficulty: str | None = None
    ) -> dict:
        picked = [
            outcome
            for (run, task), outcome in outcomes.items()
            if run == agent
            and (category is None or task_category(task) == category)
            and (
                difficulty is None
                or (difficulties or {}).get(task, "unknown") == difficulty
            )
        ]
        counted = [outcome for outcome in picked if outcome["status"] == STATUS_CURRENT]
        passed = sum(1 for outcome in counted if outcome["passed"])
        mean = (
            round(sum(outcome["score"] for outcome in counted) / len(counted), 3)
            if counted
            else 0.0
        )
        # Pass-rate CI (issue #161): the same deterministic 95% bootstrap
        # interval the markdown leaderboard shows, over current outcomes.
        ci = (
            bootstrap_pass_rate_ci([outcome["passed"] for outcome in counted])
            if counted
            else None
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
            "pass_rate": passed / len(counted) if counted else None,
            "ci_low": ci[0] if ci else None,
            "ci_high": ci[1] if ci else None,
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

    overall = [
        {
            **row(agent, None),
            "status": agent_status(agent),
            # Issue #119: grading mode straight from the records — `manual`
            # when any record went through the closed-agent protocol.
            "mode": (
                "manual"
                if any(r.get("mode") == "manual" for r in results.get(agent, []))
                else "runner"
            ),
            "metadata": (agent_metadata or {}).get(agent),
        }
        for agent in agents
    ]

    by_category: list[dict] = []
    for category in sorted({task_category(task) for task in tasks}):
        agents_rows = [row(agent, category) for agent in agents]
        by_category.append(
            {
                "category": category,
                "agents": [entry for entry in agents_rows if entry["total"]],
            }
        )

    # Issue #165: the same breakdown sliced by difficulty, in easy/medium/hard
    # order (then anything unrecognized, e.g. `unknown`, alphabetically).
    by_difficulty: list[dict] = []
    for difficulty in sorted(
        {(difficulties or {}).get(task, "unknown") for task in tasks},
        key=difficulty_sort_key,
    ):
        agents_rows = [row(agent, None, difficulty) for agent in agents]
        by_difficulty.append(
            {
                "difficulty": difficulty,
                "agents": [entry for entry in agents_rows if entry["total"]],
            }
        )

    heatmap = [
        {
            "task": task,
            # Issue #119: filter keys baked at build time — the page itself
            # never re-derives category or reads task files.
            "category": task_category(task),
            "difficulty": (difficulties or {}).get(task, "unknown"),
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
        "by_difficulty": by_difficulty,
        "heatmap": heatmap,
    }


# --- cost vs accuracy chart (issue #164) ---------------------------------------


def pass_rate(entry: dict) -> float | None:
    """An overall row's pass rate in [0, 1]; None when nothing was counted."""
    total = entry.get("total", 0)
    if not total:
        return None
    return entry["passed"] / total


def cost_accuracy_points(agg: dict) -> tuple[list[dict], list[str]]:
    """Split overall rows into chartable points and cost-less names (issue #164).

    Returns `(points, no_cost)` where each point is
    `{"agent", "cost_usd", "rate"}` — an agent's total recorded cost against
    its pass rate. An agent whose records never carried `usage` telemetry has
    no cost and cannot sit on a cost axis, so it is listed separately rather
    than silently plotted at zero (which would claim a $0 run).
    """
    points: list[dict] = []
    no_cost: list[str] = []
    for entry in agg["overall"]:
        rate = pass_rate(entry)
        cost = entry.get("total_cost_usd")
        if rate is None or cost is None:
            no_cost.append(entry["agent"])
        else:
            points.append({"agent": entry["agent"], "cost_usd": cost, "rate": rate})
    return points, no_cost


def pareto_frontier(points: list[dict]) -> set[str]:
    """Agents on the cost/accuracy Pareto frontier (issue #164).

    An agent is on the frontier when no other agent cost at most as much
    while passing at least as many tasks and beating it on at least one of
    the two. Ties (same cost and rate) dominate nothing — neither point of a
    pair hides the other.
    """
    frontier: set[str] = set()
    for a in points:
        dominated = any(
            b["cost_usd"] <= a["cost_usd"]
            and b["rate"] >= a["rate"]
            and (b["cost_usd"] < a["cost_usd"] or b["rate"] > a["rate"])
            for b in points
            if b is not a
        )
        if not dominated:
            frontier.add(a["agent"])
    return frontier


_CHART_W, _CHART_H = 640, 340
_CHART_PAD = {"left": 56, "right": 24, "top": 20, "bottom": 44}


def render_cost_chart(agg: dict) -> tuple[str | None, list[str]]:
    """Render the #164 scatter as a self-contained SVG.

    Returns `(svg, no_cost)`; `svg` is None when no agent reported cost, and
    `no_cost` then names every agent for the side list. X spans 0 to the
    largest total cost; Y is the pass rate, 0-100%. The dashed polyline links
    the Pareto frontier, cheapest-first.
    """
    points, no_cost = cost_accuracy_points(agg)
    if not points:
        return None, no_cost
    frontier = pareto_frontier(points)
    pad = _CHART_PAD
    max_cost = max(p["cost_usd"] for p in points)
    x_max = max_cost if max_cost > 0 else 1.0

    def px(x: float) -> float:
        return pad["left"] + (x / x_max) * (_CHART_W - pad["left"] - pad["right"])

    def py(rate: float) -> float:
        return _CHART_H - pad["bottom"] - rate * (
            _CHART_H - pad["top"] - pad["bottom"]
        )

    out: list[str] = [
        f'<svg class="chart" viewBox="0 0 {_CHART_W} {_CHART_H}" role="img" '
        'aria-label="Total cost vs pass rate per agent">',
        # Horizontal grid + Y tick labels at 0/25/50/75/100%.
    ]
    for frac in (0.0, 0.25, 0.5, 0.75, 1.0):
        y = py(frac)
        out.append(f'<line class="grid" x1="{pad["left"]}" y1="{y:.1f}" '
                   f'x2="{_CHART_W - pad["right"]}" y2="{y:.1f}"/>')
        out.append(f'<text x="{pad["left"] - 8}" y="{y + 4:.1f}" '
                   f'text-anchor="end">{frac * 100:g}%</text>')
    # X tick labels at 0, mid, max cost.
    for frac in (0.0, 0.5, 1.0):
        x = px(max_cost * frac)
        out.append(f'<text x="{x:.1f}" y="{_CHART_H - pad["bottom"] + 20}" '
                   f'text-anchor="middle">${max_cost * frac:g}</text>')
    # Axes.
    out.append(f'<line class="axis" x1="{pad["left"]}" y1="{pad["top"]}" '
               f'x2="{pad["left"]}" y2="{_CHART_H - pad["bottom"]}"/>')
    out.append(f'<line class="axis" x1="{pad["left"]}" '
               f'y1="{_CHART_H - pad["bottom"]}" x2="{_CHART_W - pad["right"]}" '
               f'y2="{_CHART_H - pad["bottom"]}"/>')
    out.append(f'<text x="{(_CHART_W + pad["left"]) // 2}" y="{_CHART_H - 8}" '
               'text-anchor="middle">Total cost (USD)</text>')
    # Frontier line, cheapest to most expensive along the frontier.
    on_frontier = sorted(
        (p for p in points if p["agent"] in frontier), key=lambda p: p["cost_usd"]
    )
    if len(on_frontier) > 1:
        coords = " ".join(
            f"{px(p['cost_usd']):.1f},{py(p['rate']):.1f}" for p in on_frontier
        )
        out.append(f'<polyline class="frontier-line" points="{coords}"/>')
    for p in points:
        cls = "pt frontier" if p["agent"] in frontier else "pt"
        x, y = px(p["cost_usd"]), py(p["rate"])
        out.append(f'<circle class="{cls}" cx="{x:.1f}" cy="{y:.1f}" r="5"/>')
        out.append(f'<text x="{x:.1f}" y="{y - 10:.1f}" text-anchor="middle">'
                   f'{html.escape(p["agent"])}</text>')
    out.append("</svg>")
    return "\n".join(out), no_cost


# --- breakdown charts (issue #165) ----------------------------------------------


def render_category_heatmap(agg: dict) -> str | None:
    """Category × agent pass-rate grid as a self-contained SVG (issue #165).

    One row per category, one column per agent. Cell fill opacity is the
    pass rate, the cell text the percentage, and a `<title>` tooltip carries
    the exact passed/total counts. None when there is nothing to grid.
    """
    blocks = agg["by_category"]
    agents = agg["agents"]
    if not blocks or not agents:
        return None
    label_w, cell_w, cell_h, top = 130, 84, 34, 24
    width = label_w + cell_w * len(agents) + 12
    height = top + cell_h * len(blocks) + 12
    out = [
        f'<svg class="chart heat-grid" viewBox="0 0 {width} {height}" role="img" '
        'aria-label="Pass rate by category and agent">',
    ]
    for j, agent in enumerate(agents):
        x = label_w + j * cell_w + (cell_w - 2) / 2
        out.append(
            f'<text class="col-head" x="{x:.1f}" y="{top - 8}" '
            f'text-anchor="middle">{html.escape(agent)}</text>'
        )
    for i, block in enumerate(blocks):
        y = top + i * cell_h
        out.append(
            f'<text x="{label_w - 10}" y="{y + cell_h / 2 + 4:.1f}" '
            f'text-anchor="end">{html.escape(block["category"])}</text>'
        )
        for j, agent in enumerate(agents):
            entry = next((e for e in block["agents"] if e["agent"] == agent), None)
            x = label_w + j * cell_w
            cx = x + cell_w / 2
            cy = y + cell_h / 2 + 4
            if entry is None:
                out.append(
                    f'<rect class="cell none" x="{x + 1}" y="{y + 1}" '
                    f'width="{cell_w - 2}" height="{cell_h - 2}"/>'
                )
                out.append(
                    f'<text class="cell-text none" x="{cx:.1f}" y="{cy:.1f}" '
                    'text-anchor="middle">&mdash;</text>'
                )
                continue
            rate = pass_rate(entry) or 0.0
            out.append(
                f'<rect class="cell" x="{x + 1}" y="{y + 1}" width="{cell_w - 2}" '
                f'height="{cell_h - 2}" fill-opacity="{rate:.2f}">'
                f"<title>{html.escape(agent)}: {entry['passed']}/{entry['total']} "
                "passed</title></rect>"
            )
            text_cls = "cell-text light" if rate >= 0.55 else "cell-text"
            out.append(
                f'<text class="{text_cls}" x="{cx:.1f}" y="{cy:.1f}" '
                f'text-anchor="middle">{rate * 100:.0f}%</text>'
            )
    out.append("</svg>")
    return "\n".join(out)


def render_difficulty_chart(agg: dict) -> str | None:
    """Difficulty × agent pass-rate bar chart as a self-contained SVG (#165).

    One row per difficulty (easy/medium/hard order), one grouped bar slot per
    agent; bar length is the pass rate with the percentage at the bar's end.
    None when there is nothing to chart.
    """
    blocks = agg["by_difficulty"]
    agents = agg["agents"]
    if not blocks or not agents:
        return None
    label_w, top, bar_h, row_gap = 120, 24, 18, 14
    slot_w = (640 - label_w - 16) / max(1, len(agents))
    width = label_w + slot_w * len(agents) + 16
    height = top + (bar_h + row_gap) * len(blocks) + 12
    out = [
        f'<svg class="chart diff-chart" viewBox="0 0 {width:.0f} {height}" role="img" '
        'aria-label="Pass rate by difficulty and agent">',
    ]
    for i, block in enumerate(blocks):
        y = top + i * (bar_h + row_gap)
        out.append(
            f'<text x="{label_w - 10}" y="{y + bar_h / 2 + 4:.1f}" '
            f'text-anchor="end">{html.escape(block["difficulty"])}</text>'
        )
        for j, agent in enumerate(agents):
            entry = next((e for e in block["agents"] if e["agent"] == agent), None)
            x0 = label_w + j * slot_w
            track_w = slot_w - 6
            out.append(
                f'<rect class="track" x="{x0:.1f}" y="{y}" width="{track_w:.1f}" '
                f'height="{bar_h}" rx="3"/>'
            )
            if entry is None:
                out.append(
                    f'<text class="cell-text none" x="{x0 + track_w / 2:.1f}" '
                    f'y="{y + bar_h / 2 + 4:.1f}" text-anchor="middle">'
                    "&mdash;</text>"
                )
                continue
            rate = pass_rate(entry) or 0.0
            difficulty = block["difficulty"]
            out.append(
                f'<rect class="bar bar-{html.escape(difficulty, quote=True)}" '
                f'x="{x0:.1f}" y="{y}" width="{track_w * rate:.1f}" '
                f'height="{bar_h}" rx="3">'
                f"<title>{html.escape(agent)}: {entry['passed']}/{entry['total']} "
                f"passed ({html.escape(difficulty)})</title></rect>"
            )
            out.append(
                f'<text class="cell-text" x="{x0 + track_w * rate + 4:.1f}" '
                f'y="{y + bar_h / 2 + 4:.1f}" text-anchor="start">'
                f"{rate * 100:.0f}%</text>"
            )
    out.append("</svg>")
    return "\n".join(out)


def load_explainers(tasks_root: Path) -> dict[str, dict]:
    """Read every `explainer.md` under the tasks root (issue #149).

    Returns task id -> `{"title", "html"}` for each task that ships an
    explainer; tasks without one are simply absent (the Learn page renders an
    empty state when the whole corpus has none). Unreadable explainer files
    are skipped — the same failure-isolation rule `load_results` applies to
    result records.
    """
    explainers: dict[str, dict] = {}
    tasks_root = Path(tasks_root)
    if not tasks_root.is_dir():
        return explainers
    for path in sorted(tasks_root.glob("*/*/explainer.md")):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        task_id = path.parent.name
        title = task_id
        try:
            config = yaml.safe_load(
                (path.parent / "task.yaml").read_text(encoding="utf-8")
            )
            if isinstance(config, dict) and config.get("title"):
                title = str(config["title"])
        except yaml.YAMLError:
            # No parseable task.yaml: the id is a fine summary label.
            pass
        explainers[task_id] = {"title": title, "html": render_explainer_html(text)}
    return explainers


def render_explainer_html(text: str) -> str:
    """Render an explainer's minimal Markdown subset to HTML (issue #149).

    Supported: fenced ``` code blocks, `#`/`##`/`###` headings, `- ` and
    `* ` list items, and plain paragraphs. Everything is HTML-escaped first —
    explainer prose quotes log lines and must never inject markup.
    """
    out: list[str] = []
    in_code = False
    in_list = False
    for raw_line in text.splitlines():
        line = html.escape(raw_line.rstrip())
        if line.startswith("```"):
            if in_list:
                out.append("</ul>")
                in_list = False
            out.append("</pre>" if in_code else "<pre><code>")
            in_code = not in_code
            continue
        if in_code:
            out.append(line)
            continue
        heading = re.fullmatch(r"(#{1,3}) (.+)", line)
        if heading:
            if in_list:
                out.append("</ul>")
                in_list = False
            level = len(heading.group(1))
            out.append(f"<h{level + 1}>{heading.group(2)}</h{level + 1}>")
            continue
        item = re.match(r"[-*] (.+)", line)
        if item:
            if not in_list:
                out.append("<ul>")
                in_list = True
            out.append(f"<li>{item.group(1)}</li>")
            continue
        if in_list:
            out.append("</ul>")
            in_list = False
        if line.strip():
            out.append(f"<p>{line}</p>")
    if in_list:
        out.append("</ul>")
    if in_code:
        out.append("</pre>")
    return "\n".join(out)


def render_learn_html(explainers: dict[str, dict]) -> str:
    """Render the Learn page: one spoiler-collapsed section per task."""
    parts = [
        "<!DOCTYPE html>",
        '<html lang="en">',
        "<head>",
        '<meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        "<title>Azure SRE Bench — Learn</title>",
        f"<style>{_STYLE}</style>",
        "</head>",
        "<body>",
        "<h1>Azure SRE Bench — Learn</h1>",
        '<p>Post-incident write-ups, one per task. Sections are collapsed — '
        "open only what you want spoiled.</p>",
    ]
    if not explainers:
        parts.append(
            '<p class="empty">No explainers yet. Tasks ship one as '
            "<code>explainer.md</code> (required from task ID 041 on — see "
            "AGENTS.md, issue #149).</p>"
        )
    for task_id, entry in sorted(explainers.items()):
        parts.append("<details>")
        parts.append(
            f'<summary><code>{html.escape(task_id)}</code> — '
            f"{html.escape(entry['title'])}</summary>"
        )
        parts.append(entry["html"])
        parts.append("</details>")
    parts.append("<footer>")
    parts.append(
        '<a href="index.html">Leaderboard</a> &middot; '
        "Generated by <code>bench site</code>"
    )
    parts.append("</footer>")
    parts.append("</body></html>")
    return "\n".join(parts)


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
    meta = entry.get("metadata")
    # data-* attributes drive the issue-#119 filters; every value is escaped
    # (quote-aware) because run folder names and agent.yaml are untrusted.
    row_agent = html.escape(entry["agent"], quote=True)
    mode = html.escape(str(entry.get("mode", "runner")), quote=True)
    oss = "1" if is_open_source(meta) else "0"
    repo = safe_repo_url((meta or {}).get("repository"))
    name = (
        f'<a href="{html.escape(repo, quote=True)}">{html.escape(entry["agent"])}</a>'
        if repo
        else html.escape(entry["agent"])
    )
    subtitle = agent_subtitle(meta)
    contact = str((meta or {}).get("contact") or "").strip()
    sub = f'<div class="sub">{html.escape(subtitle)}</div>' if subtitle else ""
    contact_html = f'<div class="sub">{html.escape(contact)}</div>' if contact else ""
    elapsed = entry.get("median_elapsed_s")
    commands = entry.get("median_commands")
    cost = entry.get("total_cost_usd")
    rate, low, high = (
        entry.get("pass_rate"),
        entry.get("ci_low"),
        entry.get("ci_high"),
    )
    ci_text = (
        f"{rate * 100:.1f}% ({low * 100:.1f}&ndash;{high * 100:.1f})"
        if rate is not None and low is not None and high is not None
        else "&mdash;"
    )
    return (
        f'<tr data-agent="{row_agent}" data-open-source="{oss}" data-mode="{mode}">'
        f"<td>{name}{sub}{contact_html}</td>"
        f'<td>{html.escape(entry.get("status", STATUS_CURRENT))}</td>'
        f"<td>{mode}</td>"
        f'<td class="num">{entry["passed"]}/{entry["total"]}</td>'
        f'<td class="num">{entry["mean_score"]:.3f}</td>'
        f'<td class="num">{ci_text}</td>'
        + _num_cell(elapsed, f"{elapsed:.1f}s" if elapsed is not None else None)
        + _num_cell(commands, format(commands, "g") if commands is not None else None)
        + _num_cell(cost, f"${cost:.4f}" if cost is not None else None)
        + "</tr>"
    )


_FILTER_SCRIPT = """\
(function () {
  "use strict";
  var cat = document.getElementById("filter-category");
  var diff = document.getElementById("filter-difficulty");
  var mode = document.getElementById("filter-mode");
  var oss = document.getElementById("filter-oss");
  if (!cat || !diff || !mode || !oss) return;
  function apply() {
    var c = cat.value, d = diff.value, m = mode.value, o = oss.checked;
    var anyRow = false, anyVisible = false;
    document.querySelectorAll("#tbl-heatmap tr[data-category]").forEach(function (tr) {
      anyRow = true;
      var show = (c === "all" || tr.getAttribute("data-category") === c) &&
                 (d === "all" || tr.getAttribute("data-difficulty") === d);
      tr.classList.toggle("hidden", !show);
      if (show) anyVisible = true;
    });
    document.querySelectorAll("#tbl-bycat tr[data-category]").forEach(function (tr) {
      tr.classList.toggle("hidden", c !== "all" && tr.getAttribute("data-category") !== c);
    });
    // Open-source-only and grading-mode act on agent rows AND agent columns.
    // Only the overall rows carry the per-agent keys, so build the agent
    // state from them first and apply it to every data-agent element.
    var agentOpen = {}, agentMode = {};
    document.querySelectorAll("#tbl-overall tr[data-agent]").forEach(function (tr) {
      agentOpen[tr.getAttribute("data-agent")] = tr.getAttribute("data-open-source");
      agentMode[tr.getAttribute("data-agent")] = tr.getAttribute("data-mode");
    });
    document.querySelectorAll("[data-agent]").forEach(function (el) {
      var key = el.getAttribute("data-agent");
      var os = el.hasAttribute("data-open-source")
        ? el.getAttribute("data-open-source") : agentOpen[key];
      var md = el.hasAttribute("data-mode")
        ? el.getAttribute("data-mode") : agentMode[key];
      var hide = (o && os === "0") || (m !== "all" && md !== m);
      el.classList.toggle("hidden", hide);
    });
    var note = document.getElementById("filter-empty");
    if (note) note.classList.toggle("hidden", !anyRow || anyVisible);
  }
  [cat, diff, mode, oss].forEach(function (el) {
    el.addEventListener("change", apply);
  });
})();
"""


def _filter_bar(categories: list[str]) -> list[str]:
    """The issue-#119 filter controls; inline JS below wires them."""
    cat_opts = "".join(
        f'<option value="{html.escape(category, quote=True)}">'
        f"{html.escape(category)}</option>"
        for category in categories
    )
    return [
        '<div id="filters" class="filters">',
        f'<label>Category <select id="filter-category">'
        f'<option value="all">all</option>{cat_opts}</select></label>',
        '<label>Difficulty <select id="filter-difficulty">'
        '<option value="all">all</option><option>easy</option>'
        "<option>medium</option><option>hard</option>"
        '<option value="unknown">unknown</option></select></label>',
        '<label>Grading mode <select id="filter-mode">'
        '<option value="all">all</option><option value="runner">runner</option>'
        '<option value="manual">manual</option></select></label>',
        '<label><input type="checkbox" id="filter-oss"> Open-source only</label>',
        "</div>",
        '<p id="filter-empty" class="empty hidden">No tasks match the '
        "current filters.</p>",
    ]


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
        '<p><a href="learn.html">Learn pages</a> — per-task write-ups, '
        "spoiler-collapsed.</p>",
    ]

    if not agg["tasks"]:
        parts.append(
            '<p class="empty">No results yet. Run an agent with '
            "<code>bench run</code> and rebuild the site with "
            "<code>bench site</code>.</p>"
        )
    else:
        categories = [block["category"] for block in agg["by_category"]]
        parts.extend(_filter_bar(categories))
        parts.append("<h2>Overall</h2>")
        parts.append('<div class="tablewrap"><table id="tbl-overall">')
        parts.append(
            "<tr><th>Agent</th><th>Status</th><th>Mode</th><th class=num>Passed</th>"
            "<th class=num>Mean score</th><th class=num>Pass rate (95% CI)</th>"
            "<th class=num>Median time</th>"
            "<th class=num>Median cmds</th><th class=num>Cost</th></tr>"
        )
        parts.extend(_summary_row(entry) for entry in agg["overall"])
        parts.append("</table></div>")

        parts.append("<h2>Cost vs accuracy</h2>")
        chart_svg, no_cost = render_cost_chart(agg)
        if chart_svg is not None:
            parts.append(chart_svg)
            parts.append(
                '<p class="sub">The dashed line links the Pareto frontier — '
                "no agent on it is beaten by another on both cost and pass "
                "rate.</p>"
            )
        if no_cost:
            if chart_svg is None:
                note = "No agent reported cost telemetry yet."
            else:
                note = f"No cost data: {', '.join(html.escape(a) for a in no_cost)}"
            parts.append(f'<p class="empty">{note}</p>')

        parts.append("<h2>By category</h2>")
        parts.append('<div class="tablewrap"><table id="tbl-bycat">')
        header = "".join(
            f'<th data-agent="{html.escape(agent, quote=True)}">'
            f"{html.escape(agent)}</th>"
            for agent in agg["agents"]
        )
        parts.append(
            "<tr><th>Category</th><th class=num>Passed</th>"
            f"<th class=num>Mean score</th>{header}</tr>"
        )
        for block in agg["by_category"]:
            cells = []
            for agent in agg["agents"]:
                esc_agent = html.escape(agent, quote=True)
                entry = next(
                    (e for e in block["agents"] if e["agent"] == agent), None
                )
                if entry is None:
                    cells.append(
                        f'<td class="num none" data-agent="{esc_agent}">&mdash;</td>'
                    )
                else:
                    cells.append(
                        f'<td class="num" data-agent="{esc_agent}">'
                        f'{entry["passed"]}/{entry["total"]}</td>'
                        f'<td class="num" data-agent="{esc_agent}">'
                        f'{entry["mean_score"]:.3f}</td>'
                    )
            parts.append(
                f'<tr data-category="{html.escape(block["category"], quote=True)}">'
                f"<td>{html.escape(block['category'])}</td>{''.join(cells)}</tr>"
            )
        parts.append("</table></div>")

        parts.append("<h2>Category &times; agent</h2>")
        category_heat = render_category_heatmap(agg)
        if category_heat is not None:
            parts.append(category_heat)

        parts.append("<h2>Difficulty &times; agent</h2>")
        difficulty_chart = render_difficulty_chart(agg)
        if difficulty_chart is not None:
            parts.append(difficulty_chart)

        parts.append("<h2>Task &times; agent</h2>")
        parts.append('<div class="tablewrap"><table id="tbl-heatmap">')
        header = "".join(
            f'<th data-agent="{html.escape(agent, quote=True)}">'
            f"{html.escape(agent)}</th>"
            for agent in agg["agents"]
        )
        parts.append(f"<tr><th>Task</th>{header}</tr>")
        for row in agg["heatmap"]:
            cells = ""
            for agent, state in zip(agg["agents"], row["cells"]):
                esc_agent = html.escape(agent, quote=True)
                cells += (
                    f'<td class="heat {state}" data-agent="{esc_agent}">'
                    f"{_CELL_LABELS.get(state, '&mdash;')}</td>"
                )
            parts.append(
                f'<tr data-category="{html.escape(row["category"], quote=True)}" '
                f'data-difficulty="{html.escape(row["difficulty"], quote=True)}">'
                f"<td>{html.escape(row['task'])}</td>{cells}</tr>"
            )
        parts.append("</table></div>")

    parts.append(
        "<footer>Generated by <code>bench site</code> &middot; "
        '<a href="https://github.com/ravirajnalls/azure-sre-bench">azure-sre-bench</a></footer>'
    )
    if agg["tasks"]:
        parts.append(f"<script>{_FILTER_SCRIPT}</script>")
    parts.append("</body></html>")
    return "\n".join(parts)


def cmd_site(args: argparse.Namespace) -> int:
    """CLI entry for `bench site`: load results, render, write the pages."""
    results = load_results(Path(args.results_root))
    agent_metadata = load_agent_metadata_map(Path(args.results_root))
    difficulties = task_difficulties(Path(args.tasks_root))
    agg = aggregate(
        results,
        current_rubric_hashes(Path(args.tasks_root)),
        agent_metadata=agent_metadata,
        difficulties=difficulties,
    )
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_html(agg), encoding="utf-8")
    learn_out = Path(getattr(args, "learn_out", DEFAULT_LEARN_OUT))
    explainers = load_explainers(Path(args.tasks_root))
    learn_out.parent.mkdir(parents=True, exist_ok=True)
    learn_out.write_text(render_learn_html(explainers), encoding="utf-8")
    n_results = sum(len(records) for records in results.values())
    print(f"wrote {out} ({len(results)} runs, {n_results} task results)")
    print(f"wrote {learn_out} ({len(explainers)} explainer pages)")
    return 0
