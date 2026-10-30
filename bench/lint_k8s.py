"""Kubernetes fixture checker for the lint registry (issue #150).

Registers the `k8s` check on `bench.lint_fixtures.CHECKS`: it structurally
validates every Kubernetes YAML fixture — both authored manifests and
captured `kubectl get -o yaml` output — against the Kubernetes version the
task actually runs on (the `kubernetesVersion` in the task's
`fixtures/az-aks-show.json`, default 1.31.1).

Validator: the issue names kubeconform or an equivalent structural validator
"if unavailable offline". kubeconform is a Go binary (absent from CI and
contributor machines by default), so the default engine uses the
`kubernetes-validate` pip package, which ships the same standalone
kubernetes-json-schema schemas kubeconform resolves by default — no network,
no Go toolchain. Engines are injected (see `SchemaEngine`), so a kubeconform
subprocess engine can replace it without touching the check logic.

Behavior matched to kubeconform defaults:

- **Non-strict** schema validation: `kubectl get -o yaml` fixtures legitimately
  carry `status`, `managedFields`, and other server-populated fields that
  strict mode would reject as unexpected.
- **`v1/List` is unwrapped**: multi-item `kubectl get` output validates each
  entry, mirroring kubeconform's List handling.

One deliberate divergence — the point of the checker: an `apiVersion` that has
**no schema at the task's cluster version but does in an older schema set** is
an API removed in a later Kubernetes release. That is a violation (e.g.
`policy/v1beta1` PodDisruptionBudgets against 1.29), not a skip. Intentionally
removed-API fixtures — the incident itself in tasks like aks-ops-019 — are
allowlisted with a per-task `fixtures/.lintignore` file (one fixture filename
per line, `#` comments). `.lintignore` is authoring metadata: excluded from the
agent workspace (bench.run), the fixture count and GUID scan (bench.tasks),
and this check itself — never agent evidence.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Protocol

import yaml

from bench.fixture_lint import LINTIGNORE_FILENAME, load_ignore

# NOTE: no module-level import of bench.lint_fixtures here — bench.tasks
# imports this module directly, and lint_fixtures binds this module's checker
# into CHECKS from its own tail. Checker functions import the shared Finding /
# severity helpers lazily so either import order works. The .lintignore
# constant and parser come from bench.fixture_lint (#150 shares #152's
# allowlist file and format).

#: Cluster version assumed when a task ships no `az-aks-show.json` (issue #150).
DEFAULT_KUBERNETES_VERSION = "1.31.1"

#: File that states the cluster version a task runs against; its
#: `kubernetesVersion` field drives schema selection.
AZ_AKS_SHOW_FILENAME = "az-aks-show.json"

K8S_YAML_SUFFIXES = (".yaml", ".yml")

# Kubernetes builtin API groups (the core group spells its apiVersion without
# a slash, e.g. `v1`). A schema miss inside one of these is an unknown kind or
# an API removed before the packaged schema sets start (e.g. policy/v1beta1,
# gone since 1.25 while kubernetes-validate ships sets from 1.25) — never a
# custom resource, so it is reported rather than skipped.
BUILTIN_API_GROUPS = frozenset(
    {
        "admissionregistration.k8s.io",
        "apiextensions.k8s.io",
        "apiregistration.k8s.io",
        "apps",
        "authentication.k8s.io",
        "authorization.k8s.io",
        "autoscaling",
        "batch",
        "certificates.k8s.io",
        "coordination.k8s.io",
        "discovery.k8s.io",
        "events.k8s.io",
        "flowcontrol.apiserver.k8s.io",
        "internal.apiserver.k8s.io",
        "networking.k8s.io",
        "node.k8s.io",
        "policy",
        "rbac.authorization.k8s.io",
        "resource.k8s.io",
        "scheduling.k8s.io",
        "storage.k8s.io",
        "storagemigration.k8s.io",
    }
)


def _api_group(api_version: str) -> str:
    """The group part of an apiVersion; "" for the core group (`v1`)."""
    return api_version.split("/", 1)[0] if "/" in api_version else ""


# --------------------------------------------------------------------------- #
# Engine interface + default engine
# --------------------------------------------------------------------------- #


class ResourceInvalid(Exception):
    """The resource violates its schema (structural mismatch)."""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class SchemaUnavailable(Exception):
    """No schema exists for the apiVersion/kind at the target version."""


class SchemaEngine(Protocol):
    """Structural-validation engine used by the k8s fixture check."""

    def validate(self, document: dict, version: str) -> None:
        """Raise ResourceInvalid or SchemaUnavailable; return on success."""
        ...

    def serves_any_version(self, api_version: str, kind: str) -> bool:
        """True when a schema for this apiVersion/kind exists in ANY schema
        set the engine ships — the removed-API vs. custom-resource divider."""
        ...


class KubernetesValidateEngine:
    """Default engine: the `kubernetes-validate` pip package.

    Ships the standalone kubernetes-json-schema schemas (the same registry
    kubeconform defaults to) for a range of Kubernetes minors; `validate`
    picks the newest set not newer than the desired version.
    """

    # Doc ordering: metadata probes only exercise schema lookup; validation
    # errors on the probe body are proof the schema itself was found.
    _PROBE = {"metadata": {"name": "schema-probe"}}

    def __init__(self) -> None:
        try:
            import kubernetes_validate
        except ImportError as exc:  # pragma: no cover - exercised via guard
            raise RuntimeError(
                "the k8s fixture check needs the kubernetes-validate package "
                "(pip install kubernetes-validate)"
            ) from exc
        self._kv = kubernetes_validate
        # (apiVersion, kind) -> bool cache; lookups scan every schema set.
        self._serves_cache: dict[tuple[str, str], bool] = {}

    def validate(self, document: dict, version: str) -> None:
        try:
            self._kv.validate(document, version, strict=False)
        except self._kv.ValidationError as exc:
            raise ResourceInvalid(" ".join(str(exc).split())) from exc
        except self._kv.SchemaNotFoundError as exc:
            raise SchemaUnavailable(str(exc)) from exc
        except self._kv.VersionNotSupportedError as exc:
            # Newer Kubernetes than the packaged schemas: nothing to check.
            raise SchemaUnavailable(f"schemas not packaged for {version}: {exc}") from exc

    def serves_any_version(self, api_version: str, kind: str) -> bool:
        key = (api_version, kind)
        if key not in self._serves_cache:
            probe = dict(self._PROBE, apiVersion=api_version, kind=kind)
            served = False
            for schema_version in sorted(set(self._kv.utils.all_versions())):
                try:
                    self._kv.validate(probe, schema_version, strict=False)
                except self._kv.SchemaNotFoundError:
                    continue
                except self._kv.ValidationError:
                    # Validation failed but the schema clearly resolved.
                    pass
                served = True
                break
            self._serves_cache[key] = served
        return self._serves_cache[key]


def default_engine() -> KubernetesValidateEngine | None:
    """The default schema engine, or None when the package is not installed."""
    try:
        return KubernetesValidateEngine()
    except RuntimeError:
        return None


# --------------------------------------------------------------------------- #
# Per-task helpers
# --------------------------------------------------------------------------- #


def kubernetes_version_for_task(task_dir: Path) -> str:
    """The cluster version to validate against.

    Reads `kubernetesVersion` from the task's `fixtures/az-aks-show.json`
    when present; falls back to DEFAULT_KUBERNETES_VERSION otherwise.
    """
    show = task_dir / "fixtures" / AZ_AKS_SHOW_FILENAME
    if show.is_file():
        try:
            data = json.loads(show.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return DEFAULT_KUBERNETES_VERSION
        version = data.get("kubernetesVersion")
        if isinstance(version, str) and re.fullmatch(r"\d+\.\d+(\.\d+)?", version.strip()):
            value = version.strip()
            # Schema lookup needs major.minor(.patch); pad bare minors.
            return value if value.count(".") == 2 else f"{value}.0"
    return DEFAULT_KUBERNETES_VERSION


def _document_lines(text: str) -> list[int]:
    """1-based start line of every YAML document (best effort)."""
    try:
        nodes = list(yaml.compose_all(text))
    except yaml.YAMLError:
        return []
    return [node.start_mark.line + 1 for node in nodes]


# --------------------------------------------------------------------------- #
# The registered k8s check
# --------------------------------------------------------------------------- #


def _check_resource(
    *,
    document: dict,
    version: str,
    engine: SchemaEngine,
    findings: list[Finding],
    task_id: str,
    path: Path,
    line: int | None,
) -> None:
    """Validate one YAML document; appends to `findings`.

    Docs with neither apiVersion nor kind are not Kubernetes resources
    (application config also ships as YAML) and are ignored; a doc with
    exactly one of the two is a manifest with a missing key — reported.
    `v1/List` documents (multi-item `kubectl get` output) validate per item.
    """
    from bench.lint_fixtures import SKIPPED, WARNING, Finding

    api_version = document.get("apiVersion")
    kind = document.get("kind")
    has_api = isinstance(api_version, str) and api_version.strip()
    has_kind = isinstance(kind, str) and kind.strip()

    def finding(severity: str, message: str) -> None:
        location = f"{task_id}/fixtures/{path.name}" + (f":{line}" if line else "")
        findings.append(
            Finding(check="k8s", path=location, severity=severity, message=message)
        )

    if not has_api and not has_kind:
        return  # application/config YAML, not a Kubernetes resource
    if not has_api or not has_kind:
        finding(
            WARNING,
            f"not a valid Kubernetes resource: missing "
            f"{'apiVersion' if not has_api else 'kind'} (manifests must carry both)",
        )
        return

    if kind == "List" and api_version == "v1":
        # Multi-item `kubectl get -o yaml`: a List of resources. Its schema is
        # not shipped standalone, so validate the wrapped items individually.
        items = document.get("items")
        if items is None:
            return
        if not isinstance(items, list):
            finding(
                WARNING,
                f"v1/List 'items' must be a list, got {type(items).__name__}",
            )
            return
        for index, item in enumerate(items):
            if not isinstance(item, dict):
                finding(
                    WARNING,
                    f"v1/List items[{index}] must be an object",
                )
                continue
            _check_resource(
                document=item,
                version=version,
                engine=engine,
                findings=findings,
                task_id=task_id,
                path=path,
                line=line,
            )
        return

    try:
        engine.validate(document, version)
    except ResourceInvalid as exc:
        finding(WARNING, f"invalid {api_version} {kind}: {exc.message}")
    except SchemaUnavailable:
        # No schema at the task's cluster version. A schema in an older
        # packaged set means the API was removed by upgrade; a miss inside a
        # builtin group means unknown kind or a pre-1.25 removal (the packaged
        # sets start at 1.25, so e.g. policy/v1beta1 is invisible to the
        # probe). Either way the resource is invalid against this task's
        # cluster — report it; a missing schema for any other group is a
        # custom resource, skipped like kubeconform's -ignore-missing-schemas.
        removed = engine.serves_any_version(api_version, kind)
        builtin = _api_group(api_version) in BUILTIN_API_GROUPS
        if removed or builtin:
            reason = (
                "API removed in a later release"
                if removed
                else "unknown or removed API in builtin group "
                f"'{_api_group(api_version) or 'core'}'"
            )
            finding(
                WARNING,
                f"apiVersion '{api_version}' of kind '{kind}' is not served in "
                f"Kubernetes {version} ({reason}); if intentional, add "
                f"'{path.name}' to fixtures/.lintignore",
            )
        else:
            finding(
                SKIPPED,
                f"no upstream schema for custom resource {api_version} {kind} "
                "(not validated; matches kubeconform -ignore-missing-schemas)",
            )


def check_k8s_fixtures(tasks_root: Path, engine: "SchemaEngine | None" = None) -> list:
    """Validate Kubernetes YAML fixtures against each task's cluster version.

    Skips files allowlisted in the task's `fixtures/.lintignore`; ignores
    YAML that is not a Kubernetes resource (application config ships as YAML
    too). Engine-dependent: without the `kubernetes-validate` package the
    check reports one SKIPPED finding and validates nothing. `engine` is
    injectable for tests; None builds the default engine.
    """
    from bench.lint_fixtures import SKIPPED, WARNING, Finding, iter_fixture_dirs

    engine = engine if engine is not None else default_engine()
    if engine is None:
        return [
            Finding(
                check="k8s",
                path="(corpus)",
                severity=SKIPPED,
                message="kubernetes-validate is not installed; "
                "no Kubernetes fixture was validated "
                "(pip install kubernetes-validate)",
            )
        ]

    findings: list[Finding] = []
    for task_dir, fixtures_dir in iter_fixture_dirs(tasks_root):
        task_id = task_dir.name
        ignored = load_ignore(task_dir).get("*", set())
        version = kubernetes_version_for_task(task_dir)
        for path in sorted(fixtures_dir.iterdir()):
            if not path.is_file() or path.name == LINTIGNORE_FILENAME:
                continue
            if path.suffix.lower() not in K8S_YAML_SUFFIXES:
                continue
            if path.name in ignored:
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                findings.append(
                    Finding(
                        check="k8s",
                        path=f"{task_id}/fixtures/{path.name}",
                        severity=WARNING,
                        message=f"unreadable fixture: {exc}",
                    )
                )
                continue
            try:
                documents = [doc for doc in yaml.safe_load_all(text) if doc is not None]
            except yaml.YAMLError as exc:
                findings.append(
                    Finding(
                        check="k8s",
                        path=f"{task_id}/fixtures/{path.name}",
                        severity=WARNING,
                        message=f"unparseable YAML: {exc}",
                    )
                )
                continue
            lines = _document_lines(text)
            for index, document in enumerate(documents):
                line = lines[index] if index < len(lines) else None
                if not isinstance(document, dict):
                    location = f"{task_id}/fixtures/{path.name}" + (f":{line}" if line else "")
                    findings.append(
                        Finding(
                            check="k8s",
                            path=location,
                            severity=WARNING,
                            message=f"YAML document {index + 1} must be a mapping",
                        )
                    )
                    continue
                _check_resource(
                    document=document,
                    version=version,
                    engine=engine,
                    findings=findings,
                    task_id=task_id,
                    path=path,
                    line=line,
                )
    return findings
