"""Weekly results digest (issue #166).

A scheduled GitHub Actions workflow (`.github/workflows/digest.yml`) rebuilds
the aggregate snapshot of `results/` and posts it to the pinned "Results
digest" issue — summarizing new tasks, new agents, and score changes since
the previous digest.

The diff works without any external state store: each digest body ends with
a `<!-- digest-state: {...} -->` HTML comment carrying the snapshot that
produced it. The next run parses that snapshot out of the pinned issue's
body, diffs it against the current one, and embeds the fresh snapshot in the
digest it writes. The pure logic here (snapshot, diff, render, parse) is
covered by tests; `scripts/results_digest.py` is the thin `gh` wrapper the
workflow calls.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from bench.leaderboard import collect_records, current_rubric_hashes, rank_rows

DIGEST_TITLE = "Results digest"
STATE_MARKER = "<!-- digest-state: "


def build_snapshot(results_root: Path, tasks_root: Path | None = None) -> dict:
    """Aggregate `results_root` into the digest's comparable state.

    Returns `{"agents": {name: {passed, total, mean_score}}, "tasks": [ids]}`
    — the same current-version-only math the leaderboard uses (issue #95), so
    stale records never read as a score change. Mean scores are rounded to
    4 places so re-running on unchanged results round-trips bit-identically
    through the JSON comment.
    """
    runs = collect_records(Path(results_root))
    hashes = current_rubric_hashes(Path(tasks_root)) if tasks_root is not None else None
    agents = {
        row["name"]: {
            "passed": row["passed"],
            "total": row["total"],
            "mean_score": round(row["mean_score"], 4),
        }
        for row in rank_rows(runs, hashes)
    }
    tasks = sorted(
        {
            str(record.get("task"))
            for records in runs.values()
            for record in records
            if record.get("task")
        }
    )
    return {"agents": agents, "tasks": tasks}


def diff_state(old: dict | None, new: dict) -> dict:
    """What changed between two snapshots.

    Returns `{"new_agents", "new_tasks", "score_changes"}` where each score
    change names the agent plus its before/after row. A first digest (no
    previous state) reports everything present as new — nothing is "changed"
    against no baseline.
    """
    old = old or {"agents": {}, "tasks": []}
    old_agents = old.get("agents", {})
    new_agents = new.get("agents", {})
    changes = [
        {
            "agent": name,
            "before": old_agents[name],
            "after": new_agents[name],
        }
        for name in sorted(set(old_agents) & set(new_agents))
        if new_agents[name] != old_agents[name]
    ]
    return {
        "new_agents": sorted(set(new_agents) - set(old_agents)),
        "new_tasks": sorted(set(new.get("tasks", [])) - set(old.get("tasks", []))),
        "score_changes": changes,
    }


def _cell(value: str) -> str:
    """A table cell safe from a run-name's pipes breaking the column layout."""
    return value.replace("|", "\\|")


def render_digest(state: dict, diff: dict, generated: str) -> str:
    """Render one digest issue body: highlights, standings, and the state
    comment for next week's diff."""
    n_new_agents = len(diff["new_agents"])
    n_new_tasks = len(diff["new_tasks"])
    n_changes = len(diff["score_changes"])
    lines = [
        f"# {DIGEST_TITLE} — {generated}",
        "",
        f"New agents: {n_new_agents} · New tasks: {n_new_tasks} · "
        f"Score changes: {n_changes}",
        "",
    ]
    if not state["agents"] and not state["tasks"]:
        lines.append("No results yet — run an agent with `bench run`.")
    else:
        if diff["new_agents"]:
            lines += ["## New agents", ""]
            lines += [f"- {_cell(name)}" for name in diff["new_agents"]]
            lines.append("")
        if diff["new_tasks"]:
            lines += ["## New tasks", ""]
            lines += [f"- `{task}`" for task in diff["new_tasks"]]
            lines.append("")
        if diff["score_changes"]:
            lines += [
                "## Score changes",
                "",
                "| Agent | Before | After |",
                "| --- | --- | --- |",
            ]
            for change in diff["score_changes"]:
                before = change["before"]
                after = change["after"]
                lines.append(
                    f"| {_cell(change['agent'])} "
                    f"| {before.get('passed', 0)}/{before.get('total', 0)} "
                    f"({before.get('mean_score', 0):.3f}) "
                    f"| {after.get('passed', 0)}/{after.get('total', 0)} "
                    f"({after.get('mean_score', 0):.3f}) |"
                )
            lines.append("")
        lines += [
            "## Current standings",
            "",
            "| Agent | Passed | Mean score |",
            "| --- | ---: | ---: |",
        ]
        for name, row in sorted(state["agents"].items()):
            lines.append(
                f"| {_cell(name)} | {row['passed']}/{row['total']} "
                f"| {row['mean_score']:.3f} |"
            )
    # The state comment must be the last thing in the body: parse_state reads
    # it back out of the pinned issue next week. Single-line JSON so the
    # marker regex stays simple.
    state_json = json.dumps(state, sort_keys=True, separators=(",", ":"))
    lines += ["", f"{STATE_MARKER}{state_json} -->", ""]
    return "\n".join(lines)


def parse_state(body: str | None) -> dict | None:
    """The snapshot embedded in a previous digest body, or None.

    Unparseable or absent state returns None — the next digest then reports
    everything as new instead of crashing the scheduled workflow.
    """
    if not body:
        return None
    match = re.search(re.escape(STATE_MARKER) + r"(.*?)-->", body, re.DOTALL)
    if not match:
        return None
    try:
        state = json.loads(match.group(1))
    except ValueError:
        return None
    return state if isinstance(state, dict) else None
