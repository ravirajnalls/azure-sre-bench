"""Task discovery, loading, and validation (issue #2).

A task is a directory `tasks/<category>/<task-id>/` containing `task.yaml`, a
`fixtures/` folder (2-10 files), and the hidden `expected.json` (rubric) and
`solution.json` (reference answer). `inspect_task` collects every violation for
one task so a single run reports all problems, and `load_tasks` additionally
detects duplicate ids across the repo.

`TaskResult` separates `errors` (structural, fail validation) from `warnings`
(corpus-quality judgment calls, reported but not fatal) — currently the
difficulty/`evidence_files` consistency checks from issue #101 and the
distractor coverage/grounding checks from issue #94.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from bench.grader import normalize, rubric_sha256
from bench.heldout import (
    ENCRYPTED_SUFFIX,
    HIDDEN_FILES,
    SPLIT_HELDOUT,
    SPLITS,
    HeldoutKeyError,
    decrypt_bytes,
    heldout_key,
    split_of,
)
from bench.remediation import (
    CHECK_KEYS,
    CHECK_OPS,
    COMPARISON_OPS,
    KIND_REMEDIATION,
    KINDS,
    is_scalar,
    normalize_patch_path,
)
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
        "data",
        "messaging",
        "edge",
        "serverless",
    }
)

DIFFICULTIES = frozenset({"easy", "medium", "hard"})

_DIFFICULTY_ORDER = {"easy": 0, "medium": 1, "hard": 2}


def difficulty_sort_key(name: str) -> tuple[int, str]:
    """Presentation order for difficulty labels: easy, medium, hard, then the
    rest (e.g. `unknown`) alphabetically."""
    return (_DIFFICULTY_ORDER.get(name, len(_DIFFICULTY_ORDER)), name)


def task_category(task_id: str) -> str:
    """Derive a task's category from its `<category>-<nnn>` id.

    Categories may contain hyphens (e.g. `aks-networking`), so the longest
    known prefix wins; unknown ids fall back to `other`. Lives here (not in
    the site module) so the leaderboard breakdowns (issue #165) can share it.
    """
    for category in sorted(CATEGORIES, key=len, reverse=True):
        if task_id.startswith(f"{category}-"):
            return category
    return "other"


def task_difficulties(tasks_root: Path) -> dict[str, str]:
    """Map task id -> its `task.yaml` difficulty, for tasks under `tasks_root`.

    Difficulty is public corpus metadata (it survives held-out encryption,
    which hides only `expected.json`/`solution.json`), so renderers can bake
    it into breakdowns and filters. A task.yaml missing the field, or
    unparseable, is skipped — the renderers show that task as unknown rather
    than lying about it.
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

# Fixed canary GUID placed in every task.yaml and the README (issue #114). Its
# presence in a model's training corpus is the signal that the corpus was seen;
# removing it from a task is a validator error.
CANARY_GUID = "00000000-0000-0000-0000-000000000114"
CANARY_COMMENT = (
    "Canary for training-corpus detection — DO NOT REMOVE (see README, License section)."
)

MIN_FIXTURES = 2
MAX_FIXTURES = 10

# Per-task explainer (issue #149): a spoiler document for humans — never agent
# evidence, so the runner keeps it out of the workspace like expected.json.
# Optional for the existing corpus; required from task ID 041 on so every new
# task ships its Learn page. AGENTS.md "Explainer" documents the format and
# aks-identity-008 is the filled example.
EXPLAINER_FILENAME = "explainer.md"
EXPLAINER_REQUIRED_FROM = 41
EXPLAINER_REQUIRED_HEADINGS = (
    "How to investigate",
    "Why the red herring misleads",
    "Root cause",
    "Fix",
    "Prevention",
    "Further reading",
)

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


def current_rubric_hashes(tasks_root: Path) -> dict[str, str]:
    """Map task id -> content hash of the rubric currently on disk (issue #95).

    Renderers compare a recorded `rubric_sha256` against this to mark stale
    results. A rubric that cannot be parsed — an age-encrypted held-out
    `expected.json` (issue #115), a corrupt file — contributes no entry, so
    hash-based staleness is only ever asserted on a positive mismatch, never
    on an unreadable file.
    """
    hashes: dict[str, str] = {}
    for path in sorted(Path(tasks_root).rglob("expected.json")):
        try:
            rubric = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(rubric, dict):
            hashes[path.parent.name] = rubric_sha256(rubric)
    return hashes


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
    split = config.get("split")
    if split is not None and split not in SPLITS:
        errors.append(f"task.yaml: split must be one of {sorted(SPLITS)}, got '{split}'")

    kind = config.get("kind")
    if kind is not None and kind not in KINDS:
        errors.append(f"task.yaml: kind must be one of {sorted(KINDS)}, got '{kind}'")

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


def _validate_checks(checks: list, task_dir: Path) -> list[str]:
    """Shape rules for a remediation task's `checks` list (issue #110).

    Every malformed check is an authoring error: a typo'd key or op would
    otherwise be silently ignored by the grader and weaken the task. Check
    files must already exist under fixtures/ and stay inside it.
    """
    errors: list[str] = []
    for index, check in enumerate(checks):
        label = f"expected.json: checks[{index}]"
        if not isinstance(check, dict):
            errors.append(f"{label} must be an object")
            continue
        unknown = sorted(set(check) - CHECK_KEYS)
        if unknown:
            errors.append(
                f"{label}: unknown key(s) {', '.join(unknown)} "
                "(allowed: file, path, op, value)"
            )
        op = check.get("op")
        if op not in CHECK_OPS:
            errors.append(f"{label}: 'op' must be one of {list(CHECK_OPS)}, got {op!r}")
        normalized = normalize_patch_path(check.get("file"))
        if normalized is None:
            errors.append(
                f"{label}: 'file' must be a task-relative path under fixtures/ "
                "(e.g. 'fixtures/deployment.yaml')"
            )
        elif not (task_dir / normalized).is_file():
            errors.append(f"{label}: file '{check['file']}' not found in fixtures/")
        path = check.get("path")
        if not (
            isinstance(path, str)
            and path.strip()
            and all(segment.strip() for segment in path.strip().split("."))
        ):
            errors.append(
                f"{label}: 'path' must be a non-empty dotted path with "
                "non-empty segments (e.g. 'spec.replicas' or "
                "'spec.template.spec.containers.0.image')"
            )
        if op in COMPARISON_OPS:
            if "value" not in check:
                errors.append(f"{label}: op '{op}' requires a 'value'")
            elif not is_scalar(check["value"]):
                errors.append(f"{label}: 'value' must be a string, number, or boolean")
        elif op in CHECK_OPS and "value" in check:
            errors.append(f"{label}: op '{op}' must not declare a 'value'")
    return errors


def explainer_required(task_id: str) -> bool:
    """Task IDs at and above 041 must ship an explainer (issue #149).

    The backlog number is the digits after the last hyphen; a non-numeric id
    (never produced by the validator's own id check) is simply not required.
    """
    tail = task_id.rsplit("-", 1)[-1]
    return tail.isdigit() and int(tail) >= EXPLAINER_REQUIRED_FROM


def _validate_explainer(task_dir: Path, task_id: str) -> list[str]:
    """Shape rules for `explainer.md` (issue #149).

    Required from task ID 041 on, optional before. When present, every one of
    the six section headings must appear as a `##` heading, and every
    `fixtures/<file>` the prose names must exist — a reference to a renamed
    or deleted fixture would hand readers a dead link.
    """
    path = task_dir / EXPLAINER_FILENAME
    if not path.is_file():
        if explainer_required(task_id):
            return [
                f"missing {EXPLAINER_FILENAME} (required for task IDs "
                f">= {EXPLAINER_REQUIRED_FROM:03d}; see AGENTS.md 'Explainer')"
            ]
        return []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return [f"{EXPLAINER_FILENAME}: unreadable ({exc})"]

    headings = {
        line.strip()
        for line in text.splitlines()
        if line.strip().startswith("##")
    }
    missing = [
        heading
        for heading in EXPLAINER_REQUIRED_HEADINGS
        if f"## {heading}" not in headings
    ]
    errors = [
        f"{EXPLAINER_FILENAME}: missing required '## {heading}' heading "
        "(AGENTS.md 'Explainer')"
        for heading in missing
    ]
    fixtures_dir = task_dir / "fixtures"
    fixture_names = (
        {fixture.name for fixture in fixtures_dir.iterdir() if fixture.is_file()}
        if fixtures_dir.is_dir()
        else set()
    )
    for ref in sorted(set(re.findall(r"fixtures/[A-Za-z0-9._/-]+", text))):
        if ref.rsplit("/", 1)[-1] not in fixture_names:
            errors.append(
                f"{EXPLAINER_FILENAME}: references '{ref}' but it does not "
                "exist under fixtures/"
            )
    return errors


def _validate_expected_data(
    data: dict, task_dir: Path, config: dict | None = None
) -> list[str]:
    """Rubric shape rules, applied to plaintext and decrypted content alike."""
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
    # Optional per-task unsafe-fix groups (issue #113) share the group shape
    # of the graded fields — a list of non-empty string lists.
    if "unsafe" in data and not (
        isinstance(data["unsafe"], list)
        and data["unsafe"]
        and all(_validate_group(g) for g in data["unsafe"])
    ):
        errors.append(
            "expected.json: 'unsafe' must be a non-empty list "
            "of non-empty string lists"
        )

    # Remediation checks (issue #110): only remediation tasks may declare them,
    # and every remediation task must declare at least one.
    kind = config.get("kind") if isinstance(config, dict) else None
    checks = data.get("checks")
    if kind == KIND_REMEDIATION:
        if not (isinstance(checks, list) and checks):
            errors.append(
                "expected.json: a remediation task requires a non-empty "
                "'checks' list ({file, path, op, value} assertions)"
            )
        else:
            errors.extend(_validate_checks(checks, task_dir))
    elif "checks" in data:
        errors.append("expected.json: 'checks' requires task.yaml 'kind: remediation'")
    return errors


def _fixture_texts(task_dir: Path) -> list[str]:
    """Normalized text of every fixture file, for distractor grounding.

    Unreadable or missing fixtures yield no text — grounding cannot be checked
    then, and `_validate_fixtures` already reports the structural problem.
    """
    fixtures_dir = task_dir / "fixtures"
    if not fixtures_dir.is_dir():
        return []
    texts = []
    for path in fixtures_dir.iterdir():
        if not path.is_file():
            continue
        try:
            texts.append(normalize(path.read_text(encoding="utf-8", errors="replace")))
        except OSError:
            continue
    return texts


def _validate_distractors(data: dict, task_dir: Path) -> tuple[list[str], list[str]]:
    """Distractor-group rules (issue #94): mechanical schema as errors,
    corpus-quality judgment as warnings.

    Errors: a present `distractors` key must be a non-empty list of non-empty
    string lists, and a distractor alternative may not overlap a root_cause
    alternative — checked as substring overlap in either direction, because a
    distractor containing (or contained in) a graded alternative drags correct
    answers into the trap. Warnings: a task that defines no distractor group
    at all (every task must distractor its red-herring fixture), and a
    distractor group with no alternative appearing in any fixture (a
    distractor must name the wrong cause the fixtures actually suggest).
    """
    if "distractors" not in data:
        return [], [
            "expected.json: no 'distractors' groups — every task must define at "
            "least one distractor group naming the wrong cause its red-herring "
            "fixture suggests (issue #94, AGENTS.md 'Distractor groups')"
        ]

    distractors = data["distractors"]
    if not (
        isinstance(distractors, list)
        and distractors
        and all(_validate_group(group) for group in distractors)
    ):
        return [
            "expected.json: 'distractors' must be a non-empty list of "
            "non-empty string lists"
        ], []

    root_alternatives = [
        normalize(alternative)
        for group in data.get("root_cause", [])
        if isinstance(group, list)
        for alternative in group
        if isinstance(alternative, str) and alternative.strip()
    ]
    errors: list[str] = []
    for group in distractors:
        for alternative in group:
            distractor_text = normalize(alternative)
            for root_text in root_alternatives:
                if distractor_text in root_text or root_text in distractor_text:
                    errors.append(
                        f"expected.json: distractor alternative '{alternative}' overlaps "
                        f"root_cause group alternative '{root_text}' — a distractor "
                        "must name the wrong cause, never the graded one"
                    )

    warnings: list[str] = []
    if not errors:
        fixture_texts = _fixture_texts(task_dir)
        if fixture_texts:
            for index, group in enumerate(distractors):
                grounded = any(
                    normalize(alternative) in text
                    for alternative in group
                    for text in fixture_texts
                )
                if not grounded:
                    warnings.append(
                        f"expected.json: distractor group {index} has no alternative "
                        "found in any fixture — map each distractor group to the "
                        "red-herring fixture that suggests it (AGENTS.md 'Distractor "
                        "groups')"
                    )
    return errors, warnings


def _validate_expected(task_dir: Path, config: dict | None) -> tuple[list[str], list[str]]:
    """Plaintext-rubric validation: shape, distractor (#94), and remediation
    kind/checks consistency (issue #110).

    Held-out tasks take the `_validate_heldout_hidden` path instead; the
    distractor and remediation rules run there too, on the decrypted rubric
    against the same public fixtures.
    """
    path = task_dir / "expected.json"
    if not path.is_file():
        return ["missing expected.json"], []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return [f"expected.json does not parse: {exc}"], []
    if not isinstance(data, dict):
        return ["expected.json must contain a JSON object"], []
    errors = _validate_expected_data(data, task_dir, config)
    distractor_errors, distractor_warnings = _validate_distractors(data, task_dir)
    return errors + distractor_errors, distractor_warnings


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


def _validate_heldout_hidden(
    task_dir: Path, key: str | None, config: dict | None = None
) -> tuple[list[str], list[str]]:
    """Held-out tasks ship only `<name>.age` for the hidden files (issue #115).

    Plaintext hidden files are a leak and fail validation. Rubric shape and
    solution quality are checked on the decrypted content when `key` is
    available; without it only structure is validated, so key-less
    contributors still pass `bench validate`.
    """
    errors: list[str] = []
    warnings: list[str] = []
    for name in HIDDEN_FILES:
        if (task_dir / name).is_file():
            errors.append(
                f"held-out task must not contain plaintext {name} — "
                "encrypt it (see docs/HELDOUT.md)"
            )
        encrypted = task_dir / (name + ENCRYPTED_SUFFIX)
        if not encrypted.is_file():
            errors.append(f"missing {name}{ENCRYPTED_SUFFIX} (held-out task)")
            continue
        if not key:
            continue
        try:
            data = json.loads(decrypt_bytes(encrypted.read_bytes(), key))
        except HeldoutKeyError as exc:
            errors.append(f"{name}{ENCRYPTED_SUFFIX}: {exc}")
            continue
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            errors.append(f"{name}{ENCRYPTED_SUFFIX} does not decrypt to JSON: {exc}")
            continue
        if not isinstance(data, dict):
            errors.append(f"{name}{ENCRYPTED_SUFFIX} must decrypt to a JSON object")
            continue
        if name == "expected.json":
            errors.extend(_validate_expected_data(data, task_dir, config))
            distractor_errors, distractor_warnings = _validate_distractors(
                data, task_dir
            )
            errors.extend(distractor_errors)
            warnings.extend(distractor_warnings)
        else:
            warnings.extend(_solution_warnings(data, config, task_dir))
    return errors, warnings


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
    task_id = (
        str(result.config.get("id"))
        if isinstance(result.config, dict) and result.config.get("id")
        else task_dir.name
    )
    result.errors.extend(_validate_explainer(task_dir, task_id))
    if split_of(result.config) == SPLIT_HELDOUT:
        hidden_errors, hidden_warnings = _validate_heldout_hidden(
            task_dir, heldout_key(), result.config
        )
        result.errors.extend(hidden_errors)
        result.warnings.extend(hidden_warnings)
    else:
        expected_errors, expected_warnings = _validate_expected(task_dir, result.config)
        result.errors.extend(expected_errors)
        result.warnings.extend(expected_warnings)
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
