"""Fixture lint: answer-leak and size checks (issue #154).

Registered in the `bench lint-fixtures` checker registry (issue #151) as two
error-severity checkers, `leaks` and `size` — the CLI exits non-zero on
error-severity findings, so these block CI while the warning-only `az`
checker keeps surfacing shape violations without failing the build.

This is a sibling module to `bench.fixture_lint` (issue #152, the cross-file
consistency checker): the consistency checker owns the `fixture_lint` module
name, so the leak/size checks live here and register under their own names
with the same lazy `_register()` pattern.

Evidence files must not state the answer. The `leaks` checker scans:

1. **Filenames** — a fixture named after an editorial judgment plus a rubric
   alternative (``wrong-selector-service.yaml``) hands the agent the
   diagnosis. Files named after the *resource being inspected*
   (``kubectl-get-pdb.txt``, ``secretproviderclass.yaml``) are evidence
   collection, not leaks, so the filename check fires only when the name also
   carries an editorial token (``bug``, ``wrong``, ``fix``, ...).
2. **Comment lines** — a full-line ``#``/``//`` comment containing a rubric
   alternative (root cause or fix) states the answer outright. Command
   echoes (``# kubectl get deploy``) and log-format lines (``# [error] ...``,
   ``# 2026-10-30T08:57:41 ...``) are excluded: diagnostics legitimately quote
   their own failure text, and those are evidence, not commentary.

Hint words (``bug``, ``root cause``, ``misconfigured``, ``TODO``, ``FIXME``)
are matched on word boundaries in any content line or the filename, so
``DEBUG_LOG_LEVEL`` is not flagged while ``# BUG: wrong subnet`` is.

The `size` checker enforces 2–10 evidence fixture files (as in
`bench validate`) and at most `MAX_FIXTURE_LINES` lines per file; the CLI's
``--max-lines`` overrides the ceiling for one invocation.

The rubric may be unreadable (held-out task without ``HELDOUT_KEY``); such
tasks report a SKIPPED finding — same posture as the per-task pytest —
instead of a false error.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from bench.fixture_lint import SKIP_FILENAMES
from bench.tasks import MAX_FIXTURES, MIN_FIXTURES

MAX_FIXTURE_LINES = 150

# CLI --max-lines override for one invocation; None keeps MAX_FIXTURE_LINES.
_max_lines_override: int | None = None

# Words that editorialize about the diagnosis instead of showing evidence
# (issue #154). Matched case-insensitively on word boundaries.
HINT_WORDS = ("bug", "root cause", "misconfigured", "todo", "fixme")

# A filename leaks only when it pairs a rubric alternative with one of these
# editorial tokens; "wrong-selector-service.yaml" is a leak, "pdb.yaml" is not.
EDITORIAL_TOKENS = (
    "bug",
    "wrong",
    "broken",
    "fix",
    "cause",
    "misconfig",
    "typo",
    "todo",
    "should",
    "actually",
    "leak",
)

# Comment bodies that are command echoes or log-format lines, not commentary.
# Case-insensitive: real consoles render `# Grafana panel: ...` and `# Kubectl ...`.
COMMAND_ECHO_RE = re.compile(
    r"^(?:\$\s*)?"
    r"(?:kubectl|az|helm|istioctl|openssl|curl|dig|nslookup|jq|sqlcmd|sql"
    r"|grafana|promql|kql|ssh|sudo|docker|crictl|dotnet|git)\b",
    re.IGNORECASE,
)
LOG_LINE_RE = re.compile(
    r"^(?:\[[a-z0-9._-]+\]|\d{4}-\d{2}-\d{2}[T ])"  # `# [error] ...` / `# 2026-...`
)

# Alternatives shorter than this are too token-like to scan comments for
# ("dns", "ip") without drowning real leaks in false positives.
MIN_ALTERNATIVE_LENGTH = 4


def _contains_word(text: str, phrase: str) -> bool:
    """Case-insensitive word-boundary containment (`bug` ≠ substring of `DEBUG`).

    Boundaries also exclude hyphens and extension suffixes, so an
    identifier-like compound (`replace-agents.sh` naming the script) does not
    match a hyphenated rubric alternative — prose leaks state the answer with
    words, not identifiers. A sentence-ending period still counts as a
    boundary (`... is agent-replacement.` fires; `... is agent-replacement.sh`
    does not).
    """
    return (
        re.search(
            rf"(?<![\w-]){re.escape(phrase)}(?![\w-]|\.\w)", text, re.IGNORECASE
        )
        is not None
    )


def _normalized_name(name: str) -> str:
    """Filename with `.`/`-`/`_` separators spaced out, for word matching."""
    return re.sub(r"[-_.]+", " ", name.lower())


def comment_of(line: str) -> str | None:
    """Comment text of a full-line `#`/`//` comment, or None.

    Command echoes and log-format lines return None: an error message quoted
    in a log is evidence, while `# the selector doesn't match` is commentary
    that can state the answer.
    """
    stripped = line.strip()
    for marker in ("#", "//"):
        if not stripped.startswith(marker):
            continue
        body = stripped[len(marker):].strip()
        if not body or LOG_LINE_RE.match(body) or COMMAND_ECHO_RE.match(body):
            return None
        return body
    return None


def _rubric_alternatives(rubric: dict | None) -> list[str]:
    """Lowercased root_cause and fix alternatives long enough to scan for."""
    if not rubric:
        return []
    alternatives: list[str] = []
    for key in ("root_cause", "fix"):
        for group in rubric.get(key, []):
            for alternative in group:
                if len(alternative) >= MIN_ALTERNATIVE_LENGTH:
                    alternatives.append(alternative.lower())
    return alternatives


def _filename_errors(name: str, alternatives: list[str]) -> list[str]:
    errors = []
    normalized = _normalized_name(name)
    for hint in HINT_WORDS:
        if _contains_word(normalized, hint):
            errors.append(
                f"fixtures/{name}: filename contains answer-hint word '{hint}'"
            )
    if not any(_contains_word(normalized, token) for token in EDITORIAL_TOKENS):
        return errors
    for alternative in alternatives:
        if _contains_word(normalized, alternative):
            errors.append(
                f"fixtures/{name}: filename pairs an editorial token with the "
                f"rubric alternative '{alternative}' — rename after the command "
                "or source being inspected"
            )
            break
    return errors


def set_max_lines(limit: int | None) -> None:
    """Override the size ceiling for this invocation (CLI ``--max-lines``)."""
    global _max_lines_override
    _max_lines_override = limit


def _effective_max_lines(max_lines: int | None) -> int:
    if max_lines is not None:
        return max_lines
    if _max_lines_override is not None:
        return _max_lines_override
    return MAX_FIXTURE_LINES


def _size_errors(task_dir: Path, max_lines: int | None) -> list[str]:
    """Fixture count and per-file line-ceiling errors for one task."""
    fixtures_dir = task_dir / "fixtures"
    if not fixtures_dir.is_dir():
        return [f"missing fixtures/ directory (expected at {fixtures_dir})"]
    limit = _effective_max_lines(max_lines)
    errors: list[str] = []
    entries = sorted(path for path in fixtures_dir.iterdir() if path.is_file())
    # `_commands.yaml` configures the simulated CLIs (issue #111) and
    # `.lintignore` is lint bookkeeping (issue #152): neither is agent-visible
    # evidence, so the fixture rules do not apply to them.
    files = [path for path in entries if path.name not in SKIP_FILENAMES]
    if not MIN_FIXTURES <= len(files) <= MAX_FIXTURES:
        errors.append(
            f"fixtures/ must contain {MIN_FIXTURES}-{MAX_FIXTURES} files, found {len(files)}"
        )
    for fixture in files:
        try:
            lines = fixture.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError as exc:
            errors.append(f"fixtures/{fixture.name}: unreadable ({exc})")
            continue
        if len(lines) > limit:
            errors.append(
                f"fixtures/{fixture.name}: {len(lines)} lines exceeds the {limit}-line "
                "fixture limit (issue #154; configure with --max-lines)"
            )
    return errors


def _leak_errors(task_dir: Path, rubric: dict | None) -> list[str]:
    """Hint-word and rubric-alternative leaks for one task's fixtures."""
    fixtures_dir = task_dir / "fixtures"
    if not fixtures_dir.is_dir():
        return []  # the size check reports the missing directory
    errors: list[str] = []
    entries = sorted(path for path in fixtures_dir.iterdir() if path.is_file())
    files = [path for path in entries if path.name not in SKIP_FILENAMES]
    alternatives = _rubric_alternatives(rubric)
    for fixture in files:
        name = fixture.name
        errors.extend(_filename_errors(name, alternatives))
        try:
            lines = fixture.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError as exc:
            errors.append(f"fixtures/{name}: unreadable ({exc})")
            continue
        for line_no, line in enumerate(lines, 1):
            for hint in HINT_WORDS:
                if _contains_word(line, hint):
                    errors.append(
                        f"fixtures/{name}:{line_no}: answer-hint word '{hint}'"
                    )
            comment = comment_of(line)
            if comment is None:
                continue
            for alternative in alternatives:
                if _contains_word(comment, alternative):
                    errors.append(
                        f"fixtures/{name}:{line_no}: comment states the rubric "
                        f"alternative '{alternative}'"
                    )
                    break
    return errors


def lint_fixtures(
    task_dir: Path,
    rubric: dict | None,
    *,
    max_lines: int | None = None,
    check: str = "all",
) -> list[str]:
    """Lint one task's fixtures; every finding is an error string (issue #154).

    `check` selects `leaks` (hint words + rubric alternatives), `size` (file
    count and line ceiling), or `all`. Errors follow the `bench validate`
    convention of naming the file (and line) so an author can fix without
    spelunking. The registered checkers `check_fixture_leaks` and
    `check_fixture_size` expose the same scans to `bench lint-fixtures`.
    """
    errors: list[str] = []
    if check in ("all", "size"):
        errors.extend(_size_errors(task_dir, max_lines))
    if check in ("all", "leaks"):
        errors.extend(_leak_errors(task_dir, rubric))
    return errors


def _task_config(task_dir: Path) -> dict:
    """Parsed task.yaml, or {} when the file is missing/unparsable."""
    import yaml  # deferred: keeps `bench --help` cheap

    path = task_dir / "task.yaml"
    if not path.is_file():
        return {}
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        return {}


def _load_task_rubric(task_dir: Path) -> tuple[dict | None, str | None]:
    """Read a task's rubric, or return a skip reason.

    A held-out rubric without HELDOUT_KEY skips the leaks check for that task
    (same posture as the per-task pytest). An unreadable plain rubric is also
    reported as a skip rather than a crash; the per-task integrity test still
    loads rubrics directly and fails on unreadable files.
    """
    from bench.heldout import HeldoutKeyError, load_hidden_json

    try:
        return load_hidden_json(
            task_dir, "expected.json", config=_task_config(task_dir)
        ), None
    except HeldoutKeyError:
        return None, "rubric encrypted and HELDOUT_KEY unset — leak scan skipped"
    except (OSError, json.JSONDecodeError) as exc:
        return None, f"rubric unreadable ({exc}) — leak scan skipped"


def _register() -> None:
    """Register this module as the `leaks` and `size` checkers in the registry.

    Lazy import: `bench.lint_fixtures` owns the CLI-facing Finding/severity
    vocabulary (issue #151 registry); this module only depends on it inside
    registration, so importing `bench.fixture_leaks` standalone stays cheap.
    """
    from bench.lint_fixtures import ERROR, SKIPPED, Finding, iter_fixture_dirs, register_check

    def _task_label(task_dir: Path) -> str:
        return f"{task_dir.parent.name}/{task_dir.name}"

    @register_check("leaks")
    def check_fixture_leaks(tasks_root: Path) -> list[Finding]:
        """Scan every task's fixture filenames and comments for answer leaks."""
        findings: list[Finding] = []
        for task_dir, _fixtures in iter_fixture_dirs(tasks_root):
            rubric, skip = _load_task_rubric(task_dir)
            if skip is not None:
                findings.append(
                    Finding(
                        check="leaks",
                        path=_task_label(task_dir),
                        severity=SKIPPED,
                        message=skip,
                    )
                )
                continue
            for error in _leak_errors(task_dir, rubric):
                findings.append(
                    Finding(
                        check="leaks",
                        path=_task_label(task_dir),
                        severity=ERROR,
                        message=error,
                    )
                )
        return findings

    @register_check("size")
    def check_fixture_size(tasks_root: Path) -> list[Finding]:
        """Enforce the fixture count and per-file line ceiling for every task."""
        findings: list[Finding] = []
        for task_dir, _fixtures in iter_fixture_dirs(tasks_root):
            for error in _size_errors(task_dir, max_lines=None):
                findings.append(
                    Finding(
                        check="size",
                        path=_task_label(task_dir),
                        severity=ERROR,
                        message=error,
                    )
                )
        return findings


_register()
