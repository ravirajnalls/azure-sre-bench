"""Task discovery, loading, and validation (issue #2).

A task is a directory `tasks/<category>/<task-id>/` containing `task.yaml`, a
`fixtures/` folder (2-10 files), and the hidden `expected.json` (rubric) and
`solution.json` (reference answer). `inspect_task` collects every violation for
one task so a single run reports all problems, and `load_tasks` additionally
detects duplicate ids across the repo.

`TaskResult` separates `errors` (structural, fail validation) from `warnings`
(corpus-quality judgment calls, reported but not fatal) — currently the
difficulty/`evidence_files` consistency checks from issue #101.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from bench.sim import COMMANDS_FILENAME
from bench.sim.sim_cli import parse_entries

REQUIRED_KEYS = ("id", "title", "category", "difficulty", "tags", "symptom")

CATEGORIES = frozenset(
    {
        "aks-networking",
        "aks-identity",
        "aks-workloads",
        "aks-ops",
        "networking",
        "identity",
        "app-service",
        "key-vault",
        "ado-pipelines",
        "cost",
        "monitoring",
    }
)

DIFFICULTIES = frozenset({"easy", "medium", "hard"})

# Fixed canary GUID placed in every task.yaml and the README (issue #114). Its
# presence in a model's training corpus is the signal that the corpus was seen;
# removing it from a task is a validator error.
CANARY_GUID = "00000000-0000-0000-0000-000000000114"
CANARY_COMMENT = (
    "Canary for training-corpus detection — DO NOT REMOVE (see README, License section)."
)

MIN_FIXTURES = 2
MAX_FIXTURES = 10

# Any GUID-shaped token in a fixture must be a placeholder of the form
# 00000000-0000-0000-0000-00000000000X (issue #2; AGENTS.md "Fixtures").
GUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)
PLACEHOLDER_GUID_RE = re.compile(r"0{8}-0{4}-0{4}-0{4}-0{11}[0-9a-fA-F]")


# Difficulty rubric (issue #101 / AGENTS.md "Difficulty rubric"): the declared
# difficulty must agree with how many fixture files the reference answer needs
# to prove its cause. easy = 1 fixture; medium = 2 fixtures connected; hard =
# 3+ fixtures — or exactly 2 when a misleading signal or two interacting
# faults justify it (the validator cannot check that part; reviewers must).
DIFFICULTY_EVIDENCE_COUNTS = {"easy": 1, "medium": 2}


def difficulty_distribution(results: list[TaskResult]) -> dict[str, int]:
    """Count tasks per difficulty level in fixed easy/medium/hard order (issue #101)."""
    counts = {level: 0 for level in ("easy", "medium", "hard")}
    for result in results:
        difficulty = (result.config or {}).get("difficulty")
        if difficulty in counts:
            counts[difficulty] += 1
    return counts


@dataclass
class TaskResult:
    """Outcome of inspecting one task directory."""

    path: Path
    config: dict | None = None
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def id(self) -> str:
        if self.config and self.config.get("id"):
            return str(self.config["id"])
        return self.path.name


def _validate_group(group: object) -> bool:
    """A rubric group is a non-empty list of non-empty strings."""
    return isinstance(group, list) and bool(group) and all(
        isinstance(alternative, str) and alternative.strip() for alternative in group
    )


def _validate_config(config: dict, task_dir: Path) -> list[str]:
    errors: list[str] = []
    for key in REQUIRED_KEYS:
        if config.get(key) is None:
            errors.append(f"task.yaml: missing required key '{key}'")

    title = config.get("title")
    if title is not None and not (isinstance(title, str) and title.strip()):
        errors.append("task.yaml: 'title' must be a non-empty string")

    symptom = config.get("symptom")
    if symptom is not None and not (isinstance(symptom, str) and symptom.strip()):
        errors.append("task.yaml: 'symptom' must be a non-empty string")

    category = config.get("category")
    if category is not None and category not in CATEGORIES:
        errors.append(f"task.yaml: unknown category '{category}'")

    difficulty = config.get("difficulty")
    if difficulty is not None and difficulty not in DIFFICULTIES:
        errors.append(
            f"task.yaml: difficulty must be one of {sorted(DIFFICULTIES)}, got '{difficulty}'"
        )

    canary = config.get("canary")
    if canary != CANARY_GUID:
        errors.append(
            f"task.yaml: missing or incorrect canary (must be 'canary: {CANARY_GUID}' "
            "with the DO NOT REMOVE comment — training-corpus canary, issue #114, "
            "see README License section)"
        )

    tags = config.get("tags")
    if tags is not None:
        if not isinstance(tags, list) or not tags:
            errors.append("task.yaml: 'tags' must be a non-empty list")
        elif not all(isinstance(tag, str) and tag.strip() for tag in tags):
            errors.append("task.yaml: 'tags' must contain only non-empty strings")

    if isinstance(category, str):
        if task_dir.parent.name != category:
            errors.append(
                f"task folder sits under '{task_dir.parent.name}' but category is '{category}'"
            )

    task_id = config.get("id")
    if task_id is not None:
        if not isinstance(task_id, str) or not task_id.strip():
            errors.append("task.yaml: 'id' must be a non-empty string")
        else:
            if task_dir.name != task_id:
                errors.append(
                    f"id '{task_id}' does not match folder name '{task_dir.name}'"
                )
            if category in CATEGORIES and not re.fullmatch(
                rf"{re.escape(category)}-\d{{3,}}", task_id
            ):
                errors.append(
                    f"id '{task_id}' does not match '<category>-<nnn>' "
                    f"for category '{category}'"
                )
    return errors


def _validate_fixtures(task_dir: Path) -> list[str]:
    errors: list[str] = []
    fixtures_dir = task_dir / "fixtures"
    if not fixtures_dir.is_dir():
        return [f"missing fixtures/ directory (expected at {fixtures_dir})"]

    entries = sorted(path for path in fixtures_dir.iterdir() if path.is_file())
    # `_commands.yaml` configures the simulated CLIs (issue #111), so it is not
    # evidence: exempt from the fixture count and the placeholder-GUID rule.
    commands_file = next((path for path in entries if path.name == COMMANDS_FILENAME), None)
    files = [path for path in entries if path != commands_file]
    if not MIN_FIXTURES <= len(files) <= MAX_FIXTURES:
        errors.append(
            f"fixtures/ must contain {MIN_FIXTURES}-{MAX_FIXTURES} files, found {len(files)}"
        )

    for fixture in files:
        try:
            text = fixture.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            errors.append(f"fixtures/{fixture.name}: unreadable ({exc})")
            continue
        for match in GUID_RE.finditer(text):
            guid = match.group(0)
            if not PLACEHOLDER_GUID_RE.fullmatch(guid):
                errors.append(
                    f"fixtures/{fixture.name}: non-placeholder GUID '{guid}' "
                    "(use 00000000-0000-0000-0000-00000000000X)"
                )
    errors.extend(_validate_commands_file(commands_file))
    return errors


def _validate_commands_file(path: Path | None) -> list[str]:
    """Shape-check the simulated-CLI commands file (issue #111).

    The file is exempt from the evidence-file rules (count, GUID scan), so this
    is the corpus gate's only chance to catch a typo that would break every
    agent run's simulated commands on the task.
    """
    if path is None:
        return []
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        return [f"fixtures/{COMMANDS_FILENAME}: invalid YAML ({exc})"]
    return [
        f"fixtures/{COMMANDS_FILENAME}: {error}" for error in parse_entries(data)[1]
    ]


def _validate_expected(task_dir: Path) -> list[str]:
    path = task_dir / "expected.json"
    if not path.is_file():
        return ["missing expected.json"]
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return [f"expected.json does not parse: {exc}"]
    if not isinstance(data, dict):
        return ["expected.json must contain a JSON object"]
    errors = []
    for rubric_field in ("root_cause", "fix"):
        value = data.get(rubric_field)
        if not (isinstance(value, list) and value and all(_validate_group(g) for g in value)):
            errors.append(
                f"expected.json: '{rubric_field}' must be a non-empty list "
                "of non-empty string lists"
            )
    # The grader falls back to its default when max_chars is malformed
    # (defensive at runtime); here it is an authoring error worth flagging.
    if "max_chars" in data and not (
        isinstance(data["max_chars"], int)
        and not isinstance(data["max_chars"], bool)
        and data["max_chars"] > 0
    ):
        errors.append("expected.json: 'max_chars' must be a positive integer")
    return errors


def _load_solution(task_dir: Path) -> tuple[dict | None, list[str]]:
    path = task_dir / "solution.json"
    if not path.is_file():
        return None, ["missing solution.json"]
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return None, [f"solution.json does not parse: {exc}"]
    if not isinstance(data, dict):
        return None, ["solution.json must contain a JSON object"]
    return data, []


def _solution_warnings(
    solution: dict, config: dict | None, task_dir: Path
) -> list[str]:
    """Warnings for solution.json corpus quality (issue #101).

    All difficulty/`evidence_files` disagreement is warned, never errored:
    the counts are per-task judgment, so a stale declaration must surface in
    `bench validate` output without blocking unrelated work.
    """
    warnings: list[str] = []

    evidence_files = solution.get("evidence_files")
    if not isinstance(evidence_files, list) or not evidence_files:
        warnings.append(
            "solution.json: 'evidence_files' must be a non-empty list of "
            "fixture filenames needed to prove the cause"
        )
        evidence_files = None
    elif not all(isinstance(name, str) and name.strip() for name in evidence_files):
        warnings.append(
            "solution.json: 'evidence_files' must contain only non-empty strings"
        )
        evidence_files = None
    else:
        fixture_names = {
            path.name
            for path in (task_dir / "fixtures").iterdir()
            if path.is_file()
        } if (task_dir / "fixtures").is_dir() else set()
        missing = [name for name in evidence_files if name not in fixture_names]
        if missing:
            warnings.append(
                f"solution.json: evidence_files not found in fixtures/: {', '.join(missing)}"
            )

    difficulty = (config or {}).get("difficulty")
    if evidence_files is not None and difficulty in DIFFICULTIES:
        count = len(evidence_files)
        expected = DIFFICULTY_EVIDENCE_COUNTS.get(difficulty)
        if expected is not None and count != expected:
            warnings.append(
                f"difficulty '{difficulty}' expects {expected} evidence_files, "
                f"found {count} (AGENTS.md 'Difficulty rubric')"
            )
        elif difficulty == "hard" and count < 2:
            warnings.append(
                f"difficulty 'hard' expects 3+ evidence_files, or 2 with a "
                f"misleading signal / two interacting faults; found {count}"
            )
    return warnings


def inspect_task(task_dir: Path) -> TaskResult:
    """Validate one task directory, collecting every violation found."""
    result = TaskResult(path=task_dir)

    task_yaml = task_dir / "task.yaml"
    if not task_yaml.is_file():
        result.errors.append(f"missing task.yaml (expected at {task_yaml})")
    else:
        try:
            config = yaml.safe_load(task_yaml.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            result.errors.append(f"task.yaml does not parse: {exc}")
        else:
            if isinstance(config, dict):
                result.config = config
                result.errors.extend(_validate_config(config, task_dir))
            else:
                result.errors.append("task.yaml must contain a mapping")

    result.errors.extend(_validate_fixtures(task_dir))
    result.errors.extend(_validate_expected(task_dir))
    solution, solution_errors = _load_solution(task_dir)
    result.errors.extend(solution_errors)
    if solution is not None:
        result.warnings.extend(_solution_warnings(solution, result.config, task_dir))
    # Repeated violations (e.g. the same GUID twice in one fixture) report once.
    result.errors = list(dict.fromkeys(result.errors))
    result.warnings = list(dict.fromkeys(result.warnings))
    return result


def load_tasks(root: Path) -> list[TaskResult]:
    """Inspect every task directory under `root` (layout: <category>/<task-id>/)."""
    task_dirs = sorted(path for path in root.glob("*/*") if path.is_dir())
    results = [inspect_task(task_dir) for task_dir in task_dirs]

    paths_by_id: dict[str, list[Path]] = {}
    for result in results:
        if result.config and result.config.get("id"):
            paths_by_id.setdefault(result.id, []).append(result.path)
    for result in results:
        if result.config and result.config.get("id") and len(paths_by_id[result.id]) > 1:
            others = ", ".join(str(p) for p in paths_by_id[result.id] if p != result.path)
            result.errors.append(f"duplicate id '{result.id}' (also defined in {others})")
    return results
