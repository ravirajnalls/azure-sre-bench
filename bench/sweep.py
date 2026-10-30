"""Baseline sweep harness (issue #125).

Runs a configured list of agents across the corpus and writes one result run
folder per agent under `results/`, plus a machine-readable sweep report. The
sweep is the *protocol* deliverable: it makes "run naive, both adversarial
agents, and the LLM agents on N models" one command, so a real sweep after the
rubric audits (#96-#99) is a configuration step, not missing code.

Credential gap, documented: the LLM/ReAct/Foundry entries in
`sweeps/baseline.yaml` require `LLM_BASE_URL`/`LLM_API_KEY`/`LLM_MODEL` (and
Foundry equivalents), which are not available to CI. Entries declare
`requires_env`; when any listed variable is unset the entry is skipped and
recorded in the report as `skipped: missing-env` — the gap is visible in the
sweep output instead of silently producing zero results. No entry makes a real
API call until those variables exist.

The harness reuses `bench.run.run_tasks` unchanged: same workspaces, same
grading, same result records — a sweep is only orchestration over the runner.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date
from pathlib import Path

import yaml

from bench.run import discover_tasks, load_task, run_tasks

#: Per-task timeout default, matching `bench run` (issue #4).
DEFAULT_TIMEOUT_S = 600
DEFAULT_RESULTS_ROOT = Path("results")
DEFAULT_TASKS_ROOT = Path("tasks")

#: Sweep reports land next to the run folders: `results/<sweep>.sweep.json`.
REPORT_SUFFIX = ".sweep.json"

#: agent.yaml defaults for sweep-produced runs (issue #118 schema; the values
#: describe the sweep itself — model/provider come from the entry when set).
AGENT_DEFAULTS = {
    "version": "1",
    "model": "none",
    "provider": "none",
    "framework": "none",
    "open-source": "yes",
    "contact": "azure-sre-bench maintainers",
}


def load_sweep_config(path: Path) -> dict:
    """Read and validate a sweep YAML file; raises ValueError on bad config."""
    try:
        config = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ValueError(f"unreadable sweep config {path}: {exc}") from exc
    errors = validate_sweep_config(config)
    if errors:
        raise ValueError(f"invalid sweep config {path}: " + "; ".join(errors))
    return config


def validate_sweep_config(config: object) -> list[str]:
    """Structural validation; returns human-readable errors (empty = valid)."""
    if not isinstance(config, dict):
        return ["config must be a YAML mapping with a 'sweep' key"]
    sweep = config.get("sweep")
    if not isinstance(sweep, dict):
        return ["missing 'sweep' mapping"]
    errors: list[str] = []
    if not str(sweep.get("name") or "").strip():
        errors.append("sweep.name is required")
    agents = sweep.get("agents")
    if not isinstance(agents, list) or not agents:
        errors.append("sweep.agents must be a non-empty list")
        return errors
    seen: set[str] = set()
    for index, entry in enumerate(agents):
        if not isinstance(entry, dict):
            errors.append(f"agents[{index}] must be a mapping")
            continue
        name = str(entry.get("name") or "").strip()
        if not name:
            errors.append(f"agents[{index}].name is required")
        elif name in seen:
            errors.append(f"duplicate agent name: {name}")
        else:
            seen.add(name)
        if not str(entry.get("command") or "").strip():
            errors.append(f"agents[{index}] ({name or 'unnamed'}): command is required")
    return errors


def missing_env(entry: dict) -> list[str]:
    """Which of the entry's `requires_env` variables are unset right now."""
    required = entry.get("requires_env") or []
    if not isinstance(required, list):
        required = [required]
    return [name for name in required if not os.environ.get(str(name))]


def selected_task_dirs(config: dict, tasks_root: Path) -> list[Path]:
    """All corpus tasks, or the subset named by `sweep.tasks` (list of ids)."""
    task_dirs = discover_tasks(tasks_root)
    wanted = (config.get("sweep") or {}).get("tasks")
    if not wanted:
        return task_dirs
    ids = {str(task_id) for task_id in wanted}
    return [d for d in task_dirs if d.name in ids]


def agent_metadata(entry: dict, sweep_name: str) -> dict:
    """Build an issue-#118 `agent.yaml` mapping for a sweep-produced run."""
    meta = dict(AGENT_DEFAULTS)
    meta.update(
        {
            "name": entry["name"],
            "model": str(entry.get("model") or meta["model"]),
            "provider": str(entry.get("provider") or meta["provider"]),
            "framework": str(entry.get("framework") or meta["framework"]),
            "command": entry["command"],
            "date": date.today().isoformat(),
            "sweep": sweep_name,
        }
    )
    if entry.get("adversarial"):
        meta["framework"] = "adversarial-probe"
    return meta


def write_agent_yaml(run_dir: Path, entry: dict, sweep_name: str) -> None:
    """Drop a valid agent.yaml into a run folder so verify/leaderboard accept it."""
    meta = agent_metadata(entry, sweep_name)
    (run_dir / "agent.yaml").write_text(
        yaml.safe_dump(meta, sort_keys=False), encoding="utf-8"
    )


def run_sweep(
    config: dict,
    tasks_root: Path | None = None,
    results_root: Path | None = None,
    timeout: int | None = None,
) -> dict:
    """Run every agent entry over the selected tasks; returns the sweep report.

    Sequential by design: entries may pin models via `env`, which is applied
    around each agent's run and restored afterwards — the runner stays untouched.
    """
    sweep = config["sweep"]
    sweep_name = str(sweep["name"])
    tasks_root = Path(tasks_root or DEFAULT_TASKS_ROOT)
    results_root = Path(results_root or DEFAULT_RESULTS_ROOT)
    timeout = timeout or int(sweep.get("timeout") or DEFAULT_TIMEOUT_S)
    task_dirs = selected_task_dirs(config, tasks_root)
    if not task_dirs:
        raise ValueError(f"no tasks found under {tasks_root}/")

    summary: list[dict] = []
    default_overwrite = bool(sweep.get("overwrite", False))
    for entry in sweep["agents"]:
        name = str(entry["name"])
        target = results_root / name
        if target.exists() and not bool(entry.get("overwrite", default_overwrite)):
            # A sweep must never silently clobber a committed run (the phase-1
            # `results/naive` baseline is tracked in git); refreshing one is a
            # deliberate choice via `overwrite: true`.
            raise ValueError(
                f"results/{name} already exists — remove it or set "
                "`overwrite: true` for this entry"
            )
        record: dict = {
            "name": name,
            "command": entry["command"],
            "model": entry.get("model") or AGENT_DEFAULTS["model"],
            "adversarial": bool(entry.get("adversarial")),
        }
        gap = missing_env(entry)
        if gap:
            record.update(status="skipped", reason=f"missing-env: {', '.join(gap)}")
            summary.append(record)
            continue

        restored = {key: os.environ.get(key) for key in (entry.get("env") or {})}
        try:
            os.environ.update({str(k): str(v) for k, v in (entry.get("env") or {}).items()})
            run_tasks(
                task_dirs,
                str(entry["command"]),
                name=name,
                timeout=timeout,
                results_root=results_root,
            )
        finally:
            for key, value in restored.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

        passed, total = _count_run(results_root / name)
        record.update(status="ran", passed=passed, total=total)
        if record["adversarial"] and passed:
            record["gameable"] = True
        write_agent_yaml(results_root / name, entry, sweep_name)
        summary.append(record)

    report = {
        "sweep": sweep_name,
        "date": date.today().isoformat(),
        "tasks": len(task_dirs),
        "agents": summary,
        "adversarial_clean": all(
            not entry.get("gameable") for entry in summary if entry.get("adversarial")
        ),
    }
    results_root.mkdir(parents=True, exist_ok=True)
    (results_root / f"{sweep_name}{REPORT_SUFFIX}").write_text(
        json.dumps(report, indent=2) + "\n", encoding="utf-8"
    )
    return report


def _count_run(run_dir: Path) -> tuple[int, int]:
    """Count passed/total result JSONs in one run folder."""
    passed = total = 0
    for path in sorted(run_dir.glob("*.json")):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(record, dict) and "passed" in record:
            total += 1
            passed += bool(record["passed"])
    return passed, total


def cmd_sweep(args: argparse.Namespace) -> int:
    """CLI entry for `bench sweep`."""
    try:
        config = load_sweep_config(Path(args.config))
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    sweep = config["sweep"]
    if args.dry_run:
        task_count = len(selected_task_dirs(config, Path(args.tasks_root)))
        print(f"sweep {sweep['name']}: {task_count} tasks")
        for entry in sweep["agents"]:
            gap = missing_env(entry)
            note = f" (skipped: missing-env: {', '.join(gap)})" if gap else ""
            print(f"  - {entry['name']}: {entry['command']}{note}")
        return 0
    try:
        report = run_sweep(
            config,
            tasks_root=Path(args.tasks_root),
            results_root=Path(args.results_root),
            timeout=args.timeout,
        )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    for entry in report["agents"]:
        if entry["status"] == "ran":
            print(
                f"{entry['name']}: {entry['passed']}/{entry['total']} passed"
                + ("  GAMEABLE" if entry.get("gameable") else "")
            )
        else:
            print(f"{entry['name']}: {entry['status']} ({entry['reason']})")
    if not report["adversarial_clean"]:
        print(
            "warning: adversarial agent(s) passed at least one task — gameability "
            "remains; see tests/known_gameable.txt and issues #96-#99",
            file=sys.stderr,
        )
    return 0
