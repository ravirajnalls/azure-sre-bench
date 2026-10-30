"""Fixture lint checkers for the task corpus (issues #150-#154).

`bench lint-fixtures --check <name>` runs one registered checker over every
task's `fixtures/` directory. The `az` checker (issue #151) validates canned
`az` CLI JSON outputs against minimal JSON Schemas under `bench/schemas/az/`,
matched by fixture filename prefix (longest prefix wins, so
`az-network-vnet-subnet-show-pool2.json` maps to the subnet-show schema).

The registry is the extension point for the sibling checkers: k8s
(issue #150), consistency (#152), logs (#153), and leaks (#154) each register
a callable with the same signature. Findings carry a severity so a checker
decides its own error-vs-warning posture; the CLI fails (exit 1) only on
error-severity findings, which keeps the az check warning-only in CI by
construction.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

SCHEMA_DIR = Path(__file__).parent / "schemas"

CheckFn = Callable[[Path], list["Finding"]]

# Registered checkers, name -> callable(tasks_root) -> findings.
CHECKS: dict[str, CheckFn] = {}

# Severity values; the CLI exits non-zero only on ERROR.
ERROR = "error"
WARNING = "warning"
SKIPPED = "skipped"


@dataclass(frozen=True)
class Finding:
    """One lint result for one fixture file."""

    check: str
    path: str
    severity: str  # ERROR | WARNING | SKIPPED
    message: str


def register_check(name: str) -> Callable[[CheckFn], CheckFn]:
    """Register a checker under `bench lint-fixtures --check <name>`."""

    def decorator(fn: CheckFn) -> CheckFn:
        CHECKS[name] = fn
        return fn

    return decorator


def iter_fixture_dirs(tasks_root: Path) -> list[tuple[Path, Path]]:
    """Yield (task_dir, fixtures_dir) for every task with a fixtures/ folder."""
    pairs = []
    for task_dir in sorted(tasks_root.glob("*/*/")):
        fixtures = task_dir / "fixtures"
        if fixtures.is_dir():
            pairs.append((task_dir, fixtures))
    return pairs


# Fixture filename stem -> schema file under SCHEMA_DIR. Matched by
# longest-prefix: a stem matches when it equals the prefix or extends it with
# a `-suffix` (so task-specific variants like `-acr` or `-pool2` reuse the
# parent command's schema). Sorted longest-first at load time.
AZ_PREFIX_SCHEMAS: tuple[tuple[str, str], ...] = (
    ("az-aks-nodepool-list", "az/aks-nodepool-list.json"),
    ("az-aks-show", "az/aks-show.json"),
    ("az-identity-federated-credential-list", "az/federated-credential-list.json"),
    ("az-keyvault-show", "az/keyvault-show.json"),
    ("az-network-private-dns-link-list", "az/private-dns-link-list.json"),
    ("az-network-vnet-subnet-show", "az/vnet-subnet-show.json"),
    ("az-role-assignment-list", "az/role-assignment-list.json"),
)


def match_az_schema(stem: str) -> Path | None:
    """Return the schema path for a fixture stem, or None if no prefix maps."""
    for prefix, rel in sorted(AZ_PREFIX_SCHEMAS, key=lambda p: len(p[0]), reverse=True):
        if stem == prefix or stem.startswith(f"{prefix}-"):
            return SCHEMA_DIR / rel
    return None


def _validate_with_schema(data: object, schema_path: Path) -> list[str]:
    """Return sorted jsonschema error messages for `data` against the schema."""
    import jsonschema  # deferred: keeps `bench --help` cheap

    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    validator_cls = jsonschema.validators.validator_for(schema)
    validator_cls.check_schema(schema)
    validator = validator_cls(schema)
    errors = sorted(
        validator.iter_errors(data), key=lambda err: list(err.absolute_path)
    )
    return [
        f"{_format_path(err.absolute_path)}: {err.message}" if err.absolute_path else err.message
        for err in errors
    ]


def _format_path(parts: tuple) -> str:
    """Format a jsonschema instance path as `items.0.subject`-style text."""
    rendered = ".".join(str(part) for part in parts)
    return f"at {rendered}"


@register_check("az")
def check_az_fixtures(tasks_root: Path) -> list[Finding]:
    """Validate canned az JSON fixtures against their prefix-mapped schemas.

    Unknown `az-*` fixtures are skipped, not failed (issue #151 acceptance);
    everything this check reports is warning-severity, keeping the CI job
    warning-only. Non-`az-` JSON files are not az outputs and are ignored.
    """
    findings: list[Finding] = []
    for task_dir, fixtures in iter_fixture_dirs(tasks_root):
        for fixture in sorted(fixtures.glob("*.json")):
            rel = f"{task_dir.name}/fixtures/{fixture.name}"
            if not fixture.stem.startswith("az-"):
                continue  # not an az output (kubectl, logs, KQL, ...)
            schema_path = match_az_schema(fixture.stem)
            if schema_path is None:
                findings.append(
                    Finding(
                        check="az",
                        path=rel,
                        severity=SKIPPED,
                        message=(
                            "no schema matches this az fixture prefix "
                            "(unknown az fixtures are skipped, not failed)"
                        ),
                    )
                )
                continue
            try:
                data = json.loads(fixture.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                findings.append(
                    Finding(
                        check="az",
                        path=rel,
                        severity=WARNING,
                        message=f"fixture is not valid JSON: {exc}",
                    )
                )
                continue
            for problem in _validate_with_schema(data, schema_path):
                findings.append(
                    Finding(
                        check="az",
                        path=rel,
                        severity=WARNING,
                        message=f"shape violation against {schema_path.name}: {problem}",
                    )
                )
    return findings


def run_checks(tasks_root: Path, check_names: list[str]) -> list[Finding]:
    """Run the named checkers (registered in CHECKS) over the corpus."""
    findings: list[Finding] = []
    for name in check_names:
        if name not in CHECKS:
            raise ValueError(f"unknown check '{name}'; known: {', '.join(sorted(CHECKS))}")
        findings.extend(CHECKS[name](tasks_root))
    return findings


def print_report(findings: list[Finding]) -> None:
    """Print findings in a stable order, then a per-check summary."""
    for finding in sorted(findings, key=lambda f: (f.check, f.path, f.message)):
        tag = finding.severity.upper()
        print(f"{tag} {finding.path}: {finding.message}")
    checks_present = {f.check for f in findings}
    for name in sorted(checks_present):
        mine = [f for f in findings if f.check == name]
        errors = sum(1 for f in mine if f.severity == ERROR)
        warnings = sum(1 for f in mine if f.severity == WARNING)
        skipped = sum(1 for f in mine if f.severity == SKIPPED)
        checked = len(mine) - skipped
        print(
            f"{name}: {checked} fixture finding(s), "
            f"{warnings} warning(s), {skipped} skipped, {errors} error(s)"
        )


def cmd_lint_fixtures(args) -> int:
    """CLI entry: run the selected checkers; exit 1 only on error-severity findings."""
    tasks_root = Path(args.tasks_root)
    if not tasks_root.is_dir():
        print(f"error: tasks root '{tasks_root}' does not exist", file=sys.stderr)
        return 2
    check_names = (
        sorted(CHECKS) if args.check is None else [args.check]
    )
    findings = run_checks(tasks_root, check_names)
    print_report(findings)
    has_errors = any(f.severity == ERROR for f in findings)
    return 1 if has_errors else 0
