"""Third-party result verification (issue #118).

`bench verify results/<agent>` re-grades every stored answer in a run folder with
the current grader and fails when any recorded score or pass flag does not match.
Run folders must carry an `agent.yaml` descriptor; a missing or invalid one fails
verification, so a tampered-or-misattributed result cannot enter the leaderboard
quietly.

This verifies integrity, not ability: it proves the stored `answer` really scores
what the result claims under the rubric shipped with the corpus.
"""

from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path

import yaml

from bench.remediation import grade_for_task

AGENT_FILE = "agent.yaml"

#: agent.yaml keys required by the #118 schema (see docs/SUBMITTING.md).
REQUIRED_AGENT_FIELDS = (
    "name",
    "version",
    "model",
    "provider",
    "framework",
    "open-source",
    "command",
    "date",
    "contact",
)

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def load_agent_metadata(run_dir: Path) -> tuple[dict | None, list[str]]:
    """Parse and validate `run_dir/agent.yaml`; returns (metadata, errors)."""
    path = run_dir / AGENT_FILE
    if not path.is_file():
        return None, [f"missing {AGENT_FILE}"]
    try:
        meta = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        return None, [f"unparseable {AGENT_FILE}: {exc}"]
    if not isinstance(meta, dict):
        return None, [f"{AGENT_FILE} must be a YAML mapping"]
    return meta, validate_agent_metadata(meta)


def validate_agent_metadata(meta: dict) -> list[str]:
    """Validate the agent.yaml schema; returns a list of human-readable errors."""
    errors: list[str] = []
    for field in REQUIRED_AGENT_FIELDS:
        value = meta.get(field)
        if value is None or (isinstance(value, str) and not value.strip()):
            errors.append(f"missing or empty field: {field}")
    if errors:
        return errors

    opensource = meta["open-source"]
    if not isinstance(opensource, bool) and str(opensource).lower() not in (
        "yes",
        "no",
        "true",
        "false",
    ):
        errors.append(
            f"'open-source' must be yes/no (or a boolean), got {opensource!r}"
        )
    if not _DATE_RE.fullmatch(str(meta["date"])):
        errors.append(f"'date' must be YYYY-MM-DD, got {meta['date']!r}")
    else:
        try:
            date.fromisoformat(str(meta["date"]))
        except ValueError:
            errors.append(f"'date' is not a real calendar day: {meta['date']!r}")
    return errors


def find_task_dir(task_id: str, tasks_root: Path) -> Path | None:
    """Locate a task directory by ID under `tasks_root` (None when absent)."""
    for candidate in sorted(Path(tasks_root).rglob(task_id)):
        if candidate.is_dir() and (candidate / "expected.json").is_file():
            return candidate
    return None


def _check_result(
    path: Path, tasks_root: Path
) -> tuple[str | None, str | None, str | None]:
    """Re-grade one stored result.

    Returns (task_id, error, mismatch) where at most one of error/mismatch is
    set: error covers structural problems, mismatch a recorded-vs-regraded
    discrepancy.
    """
    name = path.name
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return None, f"{name}: unparseable result JSON: {exc}", None
    if not isinstance(record, dict):
        return None, f"{name}: result must be a JSON object", None

    task_id = record.get("task")
    if not isinstance(task_id, str) or not task_id:
        return None, f"{name}: result has no 'task' id", None

    for field in ("answer", "score", "passed"):
        if field not in record:
            return task_id, f"{name}: result has no '{field}' field", None

    task_dir = find_task_dir(task_id, tasks_root)
    if task_dir is None:
        return (
            task_id,
            f"{name}: task '{task_id}' not found under {tasks_root} — cannot re-grade",
            None,
        )
    try:
        rubric = json.loads((task_dir / "expected.json").read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return task_id, f"task '{task_id}': unparseable expected.json: {exc}", None

    # The re-grade must go through the same dispatch as the original grading,
    # so remediation tasks re-run their patch checks (issue #110).
    from bench.run import load_task  # deferred: avoids a circular import

    task = load_task(task_dir) if (task_dir / "task.yaml").is_file() else {}
    regraded = grade_for_task(
        record["answer"] if isinstance(record["answer"], dict) else {},
        rubric,
        task_dir,
        task,
    )
    recorded_score = record["score"]
    recorded_passed = record["passed"]
    if not isinstance(recorded_score, (int, float)) or not isinstance(
        recorded_passed, bool
    ):
        return task_id, None, (
            f"{name}: recorded score={recorded_score!r} passed={recorded_passed!r} "
            "are not comparable"
        )
    if round(recorded_score, 3) != regraded["score"] or recorded_passed != regraded["passed"]:
        return task_id, None, (
            f"{name}: recorded score={round(float(recorded_score), 3)} "
            f"passed={recorded_passed}, regraded score={regraded['score']} "
            f"passed={regraded['passed']}"
        )
    return task_id, None, None


def verify_run(run_dir: Path, tasks_root: Path = Path("tasks")) -> dict:
    """Verify one run folder: agent.yaml plus a re-grade of every result.

    Returns `{"run", "agent", "errors", "mismatches", "checked"}`; the caller
    decides the exit code (any error or mismatch means failure).
    """
    run_dir = Path(run_dir)
    report: dict = {
        "run": str(run_dir),
        "agent": None,
        "errors": [],
        "mismatches": [],
        "checked": 0,
    }
    if not run_dir.is_dir():
        report["errors"].append(f"not a directory: {run_dir}")
        return report

    meta, meta_errors = load_agent_metadata(run_dir)
    report["agent"] = meta
    report["errors"].extend(meta_errors)

    for path in sorted(run_dir.glob("*.json")):
        report["checked"] += 1
        task_id, error, mismatch = _check_result(path, Path(tasks_root))
        if error:
            report["errors"].append(error)
        if mismatch:
            report["mismatches"].append(mismatch)

    if report["checked"] == 0 and not meta_errors:
        report["errors"].append(f"no result files found in {run_dir}")
    return report
