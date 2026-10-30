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
from bench.tasks import CATEGORIES, current_rubric_hashes
from bench.verify import load_agent_metadata

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


def task_category(task_id: str) -> str:
    """Derive a task's category from its `<category>-<nnn>` id.

    Categories may contain hyphens (e.g. `aks-networking`), so the longest
    known prefix wins; unknown ids fall back to `other`.
    """
    for category in sorted(CATEGORIES, key=len, reverse=True):
        if task_id.startswith(f"{category}-"):
            return category
    return "other"


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


def task_difficulties(tasks_root: Path) -> dict[str, str]:
    """Map task id -> its `task.yaml` difficulty, for tasks under `tasks_root`.

    Difficulty is public corpus metadata (it survives held-out encryption, which
    hides only `expected.json`/`solution.json`), so the site can bake it into
    the heatmap for the issue-#119 difficulty filter. A task.yaml missing the
    field, or unparseable, is skipped — the filter then shows that task as
    unknown rather than lying about it.
    """
    difficulties: dict[str, str] = {}
    for path in sorted(Path(tasks_root).rglob("task.yaml")):
        try:
            config = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError):
            continue
        if not isinstance(config, dict):
            continue
        task_id = config.get("id")
        difficulty = config.get("difficulty")
        if isinstance(task_id, str) and isinstance(difficulty, str):
            difficulties[task_id] = difficulty
    return difficulties


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
    return (
        f'<tr data-agent="{row_agent}" data-open-source="{oss}" data-mode="{mode}">'
        f"<td>{name}{sub}{contact_html}</td>"
        f'<td>{html.escape(entry.get("status", STATUS_CURRENT))}</td>'
        f"<td>{mode}</td>"
        f'<td class="num">{entry["passed"]}/{entry["total"]}</td>'
        f'<td class="num">{entry["mean_score"]:.3f}</td>'
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
    // Open-source-only and grading-mode act on agent rows AND agent columns
    // (overall rows, category/heatmap header + cells) via the same keys.
    document.querySelectorAll("[data-agent]").forEach(function (el) {
      var hide = (o && el.getAttribute("data-open-source") === "0") ||
                 (m !== "all" && el.getAttribute("data-mode") !== m);
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
            "<th class=num>Mean score</th><th class=num>Median time</th>"
            "<th class=num>Median cmds</th><th class=num>Cost</th></tr>"
        )
        parts.extend(_summary_row(entry) for entry in agg["overall"])
        parts.append("</table></div>")

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
    """CLI entry for `bench site`: load results, render, write the page."""
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
    n_results = sum(len(records) for records in results.values())
    print(f"wrote {out} ({len(results)} runs, {n_results} task results)")
    return 0
