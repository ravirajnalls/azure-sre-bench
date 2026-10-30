"""Fixture linting for Azure SRE Bench (issue #152).

`bench lint-fixtures --check consistency` extracts shared identities from every
fixture in a task — cluster names, regions, zone sets, subscription/tenant
placeholder GUIDs, Key Vault names, and namespace bindings — and flags
cross-file conflicts (e.g. cluster `aks-contoso-prod` in one file,
`aks-contoso-dev` in another). Values the task intends to differ can be
exempted in `fixtures/.lintignore`.

Conflict semantics, calibrated against the corpus conventions in AGENTS.md:

- Singleton categories (`cluster_name`, `region`, `zone_set`, `subscription_guid`,
  `tenant_guid`, `resource_group`, `vault_name`) describe one thing per task:
  two distinct values across files is a conflict.
- `namespace` is entity-keyed: the same object name bound to two namespaces
  across files is a conflict, while different objects legitimately live in
  different namespaces (e.g. a workload and `kube-system`).
- The literal location `global` is a DNS-zone sentinel, not a region.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

import yaml

# fixtures/.lintignore is lint bookkeeping, never agent-visible evidence.
LINTIGNORE_FILENAME = ".lintignore"

# Files that are not agent-visible evidence or lint bookkeeping.
SKIP_FILENAMES = {"_commands.yaml", LINTIGNORE_FILENAME}

# Categories where any two distinct values across files conflict.
SINGLETON_CATEGORIES = (
    "cluster_name",
    "region",
    "zone_set",
    "subscription_guid",
    "tenant_guid",
    "vault_name",
)

CHECKS = ("consistency",)

_VAULT_RE = re.compile(r"https://([a-z0-9][a-z0-9-]*)\.vault\.azure\.net", re.I)
_SUB_IN_ID_RE = re.compile(r"/subscriptions/([0-9a-fA-F-]{36})")
_SA_REF_RE = re.compile(r"system:serviceaccount:([^:\s\"']+):([^:\s\"']+)")

# az-aks-show shape marker: a `name` only means the cluster when the entry
# also carries `dnsPrefix` (node-pool show and blob outputs don't).


@dataclass(frozen=True)
class Fact:
    """One identity extracted from a fixture file."""

    category: str
    value: str
    file: str
    entity: str | None = None


@dataclass(frozen=True)
class Finding:
    """A cross-file inconsistency in a task's fixtures."""

    task_id: str
    category: str
    value: str
    files: tuple[str, ...]
    message: str


def _walk(
    node: object,
    file: str,
    facts: list[Fact],
    spatial: bool = True,
) -> None:
    """Recursively collect typed facts from parsed JSON/YAML structures.

    `spatial=False` skips region/zone extraction — a top-level list is a
    multi-resource output (az-vm-list, az-disk-list) where locations and
    zones legitimately vary per entry.
    """
    if isinstance(node, dict):
        for key, value in node.items():
            if isinstance(value, str):
                _collect_keyed(key, value, file, facts, node, spatial)
            elif key == "zones" and spatial and isinstance(value, list) and value:
                zones = sorted(str(z) for z in value)
                facts.append(Fact("zone_set", ",".join(zones), file))
            elif isinstance(value, (dict, list)):
                _walk(value, file, facts, spatial)
    elif isinstance(node, list):
        for item in node:
            _walk(item, file, facts, spatial)


def _collect_keyed(
    key: str,
    value: str,
    file: str,
    facts: list[Fact],
    siblings: dict,
    spatial: bool,
) -> None:
    """Extract facts from scalar dict entries plus strings any key hides."""
    if key == "location" and spatial and value != "global":
        facts.append(Fact("region", value, file))
    elif key == "subscriptionId":
        facts.append(Fact("subscription_guid", value, file))
    elif key == "tenantId":
        facts.append(Fact("tenant_guid", value, file))
    elif key == "namespace":
        # Entity-keyed: an object named X in namespace Y. A bare `namespace:`
        # without a sibling `name` carries no entity and is never compared.
        name = siblings.get("name")
        if isinstance(name, str):
            facts.append(Fact("namespace", value, file, entity=name))
    for match in _SUB_IN_ID_RE.finditer(value):
        facts.append(Fact("subscription_guid", match.group(1), file))
    for match in _VAULT_RE.finditer(value):
        facts.append(Fact("vault_name", match.group(1), file))
    for match in _SA_REF_RE.finditer(value):
        facts.append(Fact("namespace", match.group(1), file, entity=match.group(2)))


def _parse_documents(text: str, suffix: str) -> list[object]:
    """Parse a fixture into document trees; unparseable files yield nothing."""
    try:
        if suffix == ".json":
            return [json.loads(text)]
        if suffix in (".yaml", ".yml"):
            return [doc for doc in yaml.safe_load_all(text) if doc is not None]
    except (json.JSONDecodeError, yaml.YAMLError):
        pass
    return []


def _cluster_name(doc: object, file: str, facts: list[Fact]) -> None:
    """A `name` is a cluster name only in az-aks-show-shaped entries —
    top-level dicts and elements of list outputs (az-aks-list) alike."""
    candidates = doc if isinstance(doc, list) else [doc]
    for entry in candidates:
        if not isinstance(entry, dict) or not isinstance(entry.get("name"), str):
            continue
        type_field = entry.get("type", "")
        # Node-pool show types end with '/agentPools'; clusters end with
        # 'managedClusters' exactly.
        shaped = (
            isinstance(type_field, str) and type_field.lower().endswith("managedclusters")
        ) or "dnsPrefix" in entry
        if shaped:
            facts.append(Fact("cluster_name", entry["name"], file))


def extract_facts(text: str, filename: str) -> list[Fact]:
    """Extract identity facts from one fixture file's text."""
    facts: list[Fact] = []
    documents = _parse_documents(text, Path(filename).suffix.lower())
    for doc in documents:
        _cluster_name(doc, filename, facts)
        _walk(doc, filename, facts, spatial=not isinstance(doc, list))
    # Regex sweep over raw text: catches plain-text fixtures and strings that
    # structured extraction already visited (deduped by the caller's set use).
    for pattern, category in (
        (_VAULT_RE, "vault_name"),
        (_SUB_IN_ID_RE, "subscription_guid"),
        (_SA_REF_RE, "namespace"),
    ):
        for match in pattern.finditer(text):
            if category == "namespace":
                facts.append(Fact(category, match.group(1), filename, entity=match.group(2)))
            else:
                facts.append(Fact(category, match.group(1), filename))
    return facts


def load_ignore(task_dir: Path) -> dict[str, set[str]]:
    """Read `fixtures/.lintignore` — values intentionally exempt from checks.

    Lines are `category:value` (exempt in one category) or bare `value`
    (exempt in every category). `#` comments and blank lines are skipped.
    """
    path = task_dir / "fixtures" / ".lintignore"
    if not path.is_file():
        return {}
    ignored: dict[str, set[str]] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if ":" in line:
            category, _, value = line.partition(":")
            ignored.setdefault(category.strip(), set()).add(value.strip())
        else:
            ignored.setdefault("*", set()).add(line)
    return ignored


def _is_ignored(ignored: dict[str, set[str]], category: str, value: str) -> bool:
    return value in ignored.get("*", set()) or value in ignored.get(category, set())


def find_conflicts(facts: list[Fact], ignored: dict[str, set[str]], task_id: str) -> list[Finding]:
    """Flag inconsistent values across the extracted facts.

    Pure function: singleton categories conflict on any two distinct values;
    namespaces conflict only when one entity is bound to two namespaces.
    One finding per conflict group; values exempted via .lintignore are
    filtered out first, so ignoring every side of a mismatch silences it.
    """
    findings: list[Finding] = []
    by_category: dict[str, dict[str, list[str]]] = {}
    namespaces_by_entity: dict[str, dict[str, list[str]]] = {}

    for fact in facts:
        if fact.category == "namespace":
            if fact.entity is not None:
                namespaces_by_entity.setdefault(fact.entity, {}).setdefault(
                    fact.value, []
                ).append(fact.file)
            continue
        by_category.setdefault(fact.category, {}).setdefault(fact.value, []).append(fact.file)

    def group_finding(category: str, values: dict[str, list[str]]) -> Finding:
        named = ", ".join(
            f"'{v}' ({', '.join(sorted(set(files)))})" for v, files in sorted(values.items())
        )
        files = tuple(sorted({file for file_list in values.values() for file in file_list}))
        return Finding(
            task_id=task_id,
            category=category,
            value=", ".join(sorted(values)),
            files=files,
            message=f"conflicting {category} values: {named}",
        )

    for category in SINGLETON_CATEGORIES:
        values = by_category.get(category, {})
        unignored = {
            value: files
            for value, files in values.items()
            if not _is_ignored(ignored, category, value)
        }
        if len(unignored) >= 2:
            findings.append(group_finding(category, unignored))

    for entity, namespaces in sorted(namespaces_by_entity.items()):
        unignored = {
            value: files
            for value, files in namespaces.items()
            if not _is_ignored(ignored, "namespace", value)
        }
        if len(unignored) >= 2:
            findings.append(
                Finding(
                    task_id=task_id,
                    category="namespace",
                    value=f"{entity} -> {', '.join(sorted(unignored))}",
                    files=tuple(
                        sorted({file for file_list in unignored.values() for file in file_list})
                    ),
                    message=(
                        f"object '{entity}' appears in namespaces "
                        f"{sorted(unignored)} across files "
                        f"{', '.join(sorted({file for file_list in unignored.values() for file in file_list}))}"
                    ),
                )
            )

    return sorted(findings, key=lambda f: (f.category, f.value))


def lint_task(task_dir: Path, check: str = "consistency") -> list[Finding]:
    """Lint one task directory's fixtures for the requested check."""
    if check not in CHECKS:
        raise ValueError(f"unknown check '{check}'; expected one of {CHECKS}")
    fixtures_dir = task_dir / "fixtures"
    task_id = task_dir.name
    if not fixtures_dir.is_dir():
        return []
    ignored = load_ignore(task_dir)
    facts: list[Fact] = []
    for fixture in sorted(fixtures_dir.iterdir()):
        if not fixture.is_file() or fixture.name in SKIP_FILENAMES:
            continue
        facts.extend(extract_facts(fixture.read_text(encoding="utf-8"), fixture.name))
    return find_conflicts(facts, ignored, task_id)


def iter_task_dirs(tasks_root: Path) -> list[Path]:
    """Every task directory under the root (tasks/<category>/<task-id>/)."""
    return sorted(p for p in Path(tasks_root).glob("*/*") if (p / "task.yaml").is_file())


def format_findings(findings: list[Finding]) -> str:
    """Render findings as human-readable lines (empty string when clean)."""
    return "\n".join(
        f"  {f.category}: {f.message}" for f in findings
    )


def cmd_lint_fixtures(args: argparse.Namespace) -> int:
    """CLI entry point for `bench lint-fixtures` (issue #152)."""
    tasks_root = Path(args.tasks_root)
    if args.task:
        matches = [d for d in iter_task_dirs(tasks_root) if d.name == args.task]
        if not matches:
            print(f"no task '{args.task}' under {tasks_root}/", file=sys.stderr)
            return 2
        targets = matches
    else:
        targets = iter_task_dirs(tasks_root)

    total = 0
    for task_dir in targets:
        findings = lint_task(task_dir, check=args.check)
        if not findings:
            continue
        total += len(findings)
        print(f"{task_dir.name}: {len(findings)} consistency finding(s)")
        print(format_findings(findings))
    if total:
        print(f"\nbench lint-fixtures: {total} finding(s) across the corpus", file=sys.stderr)
        return 1
    print(f"bench lint-fixtures: {len(targets)} task(s) checked, no conflicts")
    return 0
