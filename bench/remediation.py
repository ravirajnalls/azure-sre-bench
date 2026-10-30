"""Remediation track: grade fixes by checking patched files (issue #110).

A diagnosis-only task never tests whether an agent can actually fix anything.
A task that declares `kind: remediation` in its `task.yaml` extends the answer
contract with `patched_files` — an object mapping task-root-relative fixture
paths to the full new file content, e.g.:

    {"patched_files": {"fixtures/deployment.yaml": "<full new content>"}}

The rubric (`expected.json`) may then declare a `checks` list of
`{file, path, op, value}` assertions evaluated against the patched files:

- `file` — task-root-relative path that must stay inside `fixtures/`;
- `path` — dotted keys with list indexes into the parsed YAML/JSON
  (`spec.template.spec.containers.0.resources.requests.cpu`);
- `op` — one of `equals`, `exists`, `absent`, `gte`, `lte`.

A remediation task passes only when the diagnosis groups **and** every check
pass. Patches outside `fixtures/` or to files that do not exist fail with
named reasons — path escapes (`..`, absolute paths) never reach the disk.

Safety (issue #113): every patched file is a fix channel, so each one is
scanned with the same rule engine as the `fix` text. A patch that grants
cluster-admin fails the task regardless of how correct the diagnosis is.

Grading runs against a scratch copy of the task's `fixtures/` with the patches
overlaid; the agent-visible corpus and the hidden files are never touched.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

import yaml

from bench.grader import GRADED_FIELDS, SCORE_PENALTY, UNSAFE_FIX, grade, normalize
from bench.attribution import KIND_ATTRIBUTION, grade_attribution, is_attribution
from bench.postmortem import (
    FACTS_KEY,
    KIND_POSTMORTEM,
    grade_postmortem,
    is_postmortem,
)
from bench.preincident import (
    KIND_PRE_INCIDENT_REVIEW,
    grade_pre_incident_review,
    is_pre_incident_review,
)
from bench.safety import find_violations, global_rules, rubric_unsafe_rules

KIND_REMEDIATION = "remediation"
KIND_DIAGNOSIS = "diagnosis"
KINDS = (
    KIND_DIAGNOSIS,
    KIND_REMEDIATION,
    KIND_ATTRIBUTION,
    KIND_PRE_INCIDENT_REVIEW,
    KIND_POSTMORTEM,
)

# The answer's patch map: {fixture path: full new file content}.
PATCHED_FILES_KEY = "patched_files"

FIXTURES_DIRNAME = "fixtures"

CHECK_OPS = ("equals", "exists", "absent", "gte", "lte")
#: Ops that compare the resolved value against the check's `value`.
COMPARISON_OPS = ("equals", "gte", "lte")
#: The full assertion shape; `value` is optional and op-dependent.
CHECK_KEYS = frozenset({"file", "path", "op", "value"})

# Stable reason identifiers recorded in `detail["reasons"]`.
PATCHED_FILES_INVALID = "patched_files_invalid"
PATCH_OUTSIDE_FIXTURES = "patch_outside_fixtures"
PATCH_MISSING_FILE = "patch_missing_file"
CHECK_FAILED = "check_failed"
CHECK_INVALID = "check_invalid"
CHECK_TARGET_MISSING = "check_target_missing"
CHECK_UNPARSEABLE = "check_unparseable"
CHECK_SETUP_FAILED = "check_setup_failed"


def is_remediation(task: dict | None) -> bool:
    """True when a task's `task.yaml` declares `kind: remediation`."""
    return isinstance(task, dict) and task.get("kind") == KIND_REMEDIATION


def normalize_patch_path(path: object) -> str | None:
    """The task-root-relative path if it names a file under `fixtures/`, else None.

    Accepts `fixtures/deployment.yaml`; rejects directory paths, absolute
    paths, and anything that escapes `fixtures/` (`../x`, `fixtures/../x`,
    `expected.json`). `os.path.normpath` collapses interior `a/..` runs, so a
    path only survives when its normalized form still begins with `fixtures/`.
    """
    if not isinstance(path, str) or not path.strip():
        return None
    normalized = os.path.normpath(path.strip())
    if os.path.isabs(normalized) or normalized == ".." or normalized.startswith(".."):
        return None
    parts = normalized.split(os.sep)
    if len(parts) < 2 or parts[0] != FIXTURES_DIRNAME:
        return None
    if any(part in ("", ".", "..") for part in parts):
        return None
    return normalized


def is_scalar(value: object) -> bool:
    """Scalars only for check `value`s: str, int, float, bool.

    Part of the checks contract shared with the validator (bench.tasks), which
    rejects anything else at authoring time.
    """
    return isinstance(value, (str, int, float, bool))


def resolve_path(doc: object, dotted: object) -> tuple[bool, object]:
    """Resolve a dotted path (list indexes as bare integers) against parsed YAML/JSON.

    Returns `(found, value)`. A path through a missing key, an out-of-range
    index, a non-integer list index, or a non-container parent is simply not
    found — traversal never raises. A key containing a dot cannot be addressed
    (`metadata.labels` full-map checks remain possible); see AGENTS.md.
    """
    if not isinstance(dotted, str) or not dotted:
        return False, None
    current = doc
    for segment in dotted.split("."):
        if isinstance(current, dict):
            if segment in current:
                current = current[segment]
            else:
                return False, None
        elif isinstance(current, list):
            # isdigit rejects negatives ("−1" must not index from the end).
            if segment.isdigit() and int(segment) < len(current):
                current = current[int(segment)]
            else:
                return False, None
        else:
            return False, None
    return True, current


def _coerce_patched_files(value: object) -> tuple[dict[str, str], dict | None]:
    """A valid `{path: content}` mapping, or ({}, an invalid reason)."""
    if value is None:
        return {}, None
    if not isinstance(value, dict) or not all(
        isinstance(key, str) and isinstance(content, str)
        for key, content in value.items()
    ):
        return {}, {"reason": PATCHED_FILES_INVALID}
    return value, None


def overlay_patches(
    task_dir: Path, patched_files: dict[str, str]
) -> tuple[Path | None, list[dict]]:
    """Copy `fixtures/` to a scratch dir and write the patches over it.

    Returns `(scratch_dir, reasons)`; `scratch_dir` is None when the copy or
    any patch fails — a partially patched tree is never graded. Every patch
    reason names the offending path; the caller owns scratch cleanup.
    """
    scratch = Path(tempfile.mkdtemp(prefix="bench-patch-"))
    try:
        shutil.copytree(Path(task_dir) / FIXTURES_DIRNAME, scratch / FIXTURES_DIRNAME)
    except OSError as exc:
        shutil.rmtree(scratch, ignore_errors=True)
        return None, [{"reason": CHECK_SETUP_FAILED, "error": str(exc)}]

    reasons: list[dict] = []
    for path in sorted(patched_files):
        normalized = normalize_patch_path(path)
        if normalized is None:
            reasons.append({"reason": PATCH_OUTSIDE_FIXTURES, "file": path})
            continue
        target = scratch / normalized
        if not target.is_file():
            reasons.append({"reason": PATCH_MISSING_FILE, "file": path})
            continue
        target.write_text(patched_files[path], encoding="utf-8")
    if reasons:
        shutil.rmtree(scratch, ignore_errors=True)
        return None, reasons
    return scratch, []


def _load_doc(root: Path, normalized: str) -> tuple[str, object]:
    """Classify one fixture file for check evaluation: ok / missing / error."""
    target = root / normalized
    if not target.is_file():
        return "missing", None
    try:
        return "ok", yaml.safe_load(target.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        return "error", str(exc)


def run_check(check: object, root: Path, docs: dict) -> dict:
    """Evaluate one check against the patched fixture tree.

    Returns a result entry `{file, path, op, passed}` — failed checks add
    `expected`/`actual` (comparisons) or `error` + `error_kind`
    (check_invalid / check_target_missing / check_unparseable). A malformed
    check is a failed entry plus a `check_invalid` reason, never an exception:
    one broken check must not crash the grading of the rest.
    """
    entry: dict[str, object] = {
        "file": check.get("file") if isinstance(check, dict) else None,
        "path": check.get("path") if isinstance(check, dict) else None,
        "op": check.get("op") if isinstance(check, dict) else None,
        "passed": False,
    }
    op = entry["op"]
    normalized = normalize_patch_path(entry["file"])

    def fail(*, kind: str | None = None, **context: object) -> dict:
        if kind is not None:
            entry["error_kind"] = kind
        entry.update(context)
        return entry

    if not isinstance(check, dict):
        return fail(kind=CHECK_INVALID, error="check must be an object")
    unknown = sorted(set(check) - CHECK_KEYS)
    if unknown:
        return fail(
            kind=CHECK_INVALID, error=f"unknown key(s): {', '.join(unknown)}"
        )
    if op not in CHECK_OPS:
        return fail(kind=CHECK_INVALID, error=f"'op' must be one of {list(CHECK_OPS)}")
    if not isinstance(entry["path"], str) or not entry["path"]:
        return fail(kind=CHECK_INVALID, error="'path' must be a non-empty dotted path")
    if normalized is None:
        return fail(
            kind=CHECK_INVALID, error="'file' must be a path under fixtures/"
        )
    if op in COMPARISON_OPS:
        if "value" not in check or not is_scalar(check["value"]):
            return fail(
                kind=CHECK_INVALID, error=f"op '{op}' requires a scalar 'value'"
            )
    elif "value" in check:
        return fail(
            kind=CHECK_INVALID, error=f"op '{op}' must not declare a 'value'"
        )

    # Shared per-file parse cache: the file is read and parsed once.
    if normalized not in docs:
        docs[normalized] = _load_doc(root, normalized)
    state, doc = docs[normalized]
    if state == "missing":
        return fail(kind=CHECK_TARGET_MISSING, error="file not found in fixtures/")
    if state == "error":
        return fail(
            kind=CHECK_UNPARSEABLE,
            error=f"file does not parse as YAML/JSON: {doc}",
        )

    found, actual = resolve_path(doc, entry["path"])
    if op == "equals":
        expected = check["value"]
        if found and _equals(actual, expected):
            entry["passed"] = True
        else:
            return fail(expected=expected, actual=actual)
    elif op in ("gte", "lte"):
        expected = check["value"]
        number = _number(actual)
        if number is None or _number(expected) is None:
            return fail(expected=expected, actual=actual)
        bound = float(expected)
        holds = number >= bound if op == "gte" else number <= bound
        if holds:
            entry["passed"] = True
        else:
            return fail(expected=expected, actual=actual)
    elif op == "exists":
        if found and actual is not None:
            entry["passed"] = True
        else:
            return fail(actual=actual)
    else:  # absent — the exact complement of exists
        if not found or actual is None:
            entry["passed"] = True
        else:
            return fail(actual=actual)
    return entry


def _equals(actual: object, expected: object) -> bool:
    """Scalar equality: booleans never equal numbers; 1 == 1.0; strings exact."""
    if isinstance(actual, bool) or isinstance(expected, bool):
        return isinstance(actual, bool) and isinstance(expected, bool) and actual is expected
    if isinstance(actual, (int, float)) and isinstance(expected, (int, float)):
        return actual == expected
    return type(actual) is type(expected) and actual == expected


def _number(value: object) -> float | None:
    """The float value of a non-bool number, else None."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _check_reason(entry: dict) -> dict | None:
    """The `detail["reasons"]` entry for a failed check entry (None when passed)."""
    if entry["passed"]:
        return None
    reason: dict[str, object] = {
        "reason": entry.get("error_kind", CHECK_FAILED),
        "file": entry["file"],
        "path": entry["path"],
        "op": entry["op"],
    }
    if "error" in entry:
        reason["error"] = entry["error"]
    # Comparison failures carry the observed and required values so a failed
    # task explains itself without re-running the checks.
    for key in ("expected", "actual"):
        if key in entry:
            reason[key] = entry[key]
    return reason


def evaluate_checks(checks: list, root: Path) -> tuple[list[dict], list[dict]]:
    """Run every check against the patched tree; returns (entries, reasons)."""
    docs: dict = {}
    entries = [run_check(check, root, docs) for check in checks]
    reasons = [reason for reason in (_check_reason(entry) for entry in entries) if reason]
    return entries, reasons


def _group_counts(detail: dict) -> tuple[int, int]:
    """(matched, total) rubric groups from a plain `grade` detail."""
    matched = 0
    total = 0
    for field in GRADED_FIELDS:
        entries = detail.get(field)
        if not isinstance(entries, list):
            continue
        total += len(entries)
        matched += sum(
            1 for entry in entries if isinstance(entry, dict) and entry.get("matched") is not None
        )
    return matched, total


def grade_remediation(answer: dict, rubric: dict, task_dir: Path) -> dict:
    """Grade a remediation answer: diagnosis groups, checks, and safety.

    The plain grader scores the diagnosis (`root_cause`/`fix` groups, length
    caps, `fix`-text safety); then `patched_files` is overlaid on a scratch
    copy of the task's `fixtures/` and every check runs against the patched
    files. Checks count as scoring units alongside the rubric groups, every
    patched file is scanned for unsafe content, and the task passes only when
    all groups match, all checks pass, and no reason fired.
    """
    answer = answer if isinstance(answer, dict) else {}
    rubric = rubric if isinstance(rubric, dict) else {}
    base = grade(answer, rubric)
    detail = base["detail"]
    reasons = list(detail["reasons"])
    violations = list(detail["safety_violations"])

    patched_files, invalid = _coerce_patched_files(answer.get(PATCHED_FILES_KEY))
    if invalid is not None:
        reasons.append(invalid)

    # Every patched file is a fix channel: scan it like the `fix` text (#113).
    rules = [*global_rules(), *rubric_unsafe_rules(rubric)]
    for path in sorted(patched_files):
        for violation in find_violations(normalize(patched_files[path]), rules):
            violations.append(violation)
            reasons.append(
                {
                    "reason": UNSAFE_FIX,
                    "rule": violation["rule"],
                    "description": violation["description"],
                    "matched": violation["matched"],
                    "file": path,
                }
            )

    scratch, patch_reasons = overlay_patches(Path(task_dir), patched_files)
    reasons.extend(patch_reasons)

    checks = rubric.get("checks")
    check_entries: list[dict] = []
    try:
        if not (isinstance(checks, list) and checks):
            reasons.append(
                {
                    "reason": CHECK_INVALID,
                    "error": "'checks' must be a non-empty list of {file, path, op, value}",
                }
            )
        elif scratch is not None:
            check_entries, check_reasons = evaluate_checks(checks, scratch)
            reasons.extend(check_reasons)
    finally:
        if scratch is not None:
            shutil.rmtree(scratch, ignore_errors=True)

    group_matched, group_total = _group_counts(detail)
    passed_checks = sum(1 for entry in check_entries if entry["passed"])
    total_units = group_total + len(check_entries)
    matched_units = group_matched + passed_checks
    score = (
        max(0.0, round(matched_units / total_units - SCORE_PENALTY * len(violations), 3))
        if total_units
        else 0.0
    )
    passed = total_units > 0 and matched_units == total_units and not reasons
    detail["reasons"] = reasons
    detail["safety_violations"] = violations
    detail["checks"] = check_entries
    return {"score": score, "passed": passed, "detail": detail}


def grade_for_task(
    answer: dict, rubric: dict, task_dir: Path | str, task: dict | None = None
) -> dict:
    """The shared grading entry point: dispatch on the task's kind.

    Every surface that grades an answer against a task (`bench run`, `bench
    grade`, `bench submit`, `bench verify`, the solvability invariant) calls
    this rather than `bench.grader.grade` directly, so remediation tasks run
    their patch checks everywhere, change-attribution (issue #144) and
    pre-incident-review tasks run their own graders, and diagnosis tasks
    keep the plain grader.

    A rubric that declares `checks` triggers remediation grading, a
    `culprit` string triggers attribution grading, a `findings` list
    triggers pre-incident grading, and a `required_facts` list triggers
    postmortem grading even when the task config is missing or unmarked
    (manual submits, unit tests): silently skipping a kind's half of the
    contract would inflate the score. The validator keeps repo tasks
    consistent — a kind and its rubric shape ship together.
    """
    rubric_dict = rubric if isinstance(rubric, dict) else {}
    if is_pre_incident_review(task) or isinstance(rubric_dict.get("findings"), list):
        return grade_pre_incident_review(answer, rubric)
    if is_postmortem(task) or isinstance(rubric_dict.get(FACTS_KEY), list):
        return grade_postmortem(answer, rubric)
    if is_remediation(task) or isinstance(rubric_dict.get("checks"), list):
        return grade_remediation(answer, rubric, Path(task_dir))
    if is_attribution(task) or isinstance(rubric.get("culprit"), str):
        return grade_attribution(answer, rubric)
    return grade(answer, rubric)
